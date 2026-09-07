"""ApprovalEngine — the governance core (SPEC.md sections 4.2 and 7).

Non-negotiable invariant, enforced here and regression-tested in
sai/tests/test_approval_engine.py: **no tool with `risk_level` in
{"medium","high","critical"} — and no "low" tool outside the allowlist —
ever executes unless its `Action.status == 'approved'`** (i.e. a human with
sufficient RBAC role called `approve_action`), or the exact tool+scope
combination is present in the versioned allowlist (SPEC 7.3), in which case
it is allowed to auto-execute with `requires_approval=False` from the
start — but still fully audited and still notified (informationally).

Flow (SPEC 7.1):
  create_action() -> [allowlisted? execute immediately : await approval]
  approve_action() -> execute_approved_action()
  reject_action()  -> terminal, no execution
  expire_timed_out_actions() -> periodic sweep, rejects with reason=timeout
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.approval.allowlist import Allowlist
from sai.config import Settings
from sai.connectors.registry import ToolRegistry
from sai.db.models import Action, AuditLog, User

logger = logging.getLogger(__name__)

# Roles allowed to approve each risk level (SPEC 10.4).
ROLE_APPROVAL_RANK = {"viewer": 0, "operator": 1, "admin": 2}
RISK_LEVEL_MIN_ROLE = {"low": "operator", "medium": "operator", "high": "admin", "critical": "admin"}

NotifyFn = Callable[[Action], Awaitable[None]]


class ActionNotFoundError(RuntimeError):
    pass


class InsufficientRoleError(RuntimeError):
    pass


class InvalidActionStateError(RuntimeError):
    pass


class RiskLevelMismatchError(RuntimeError):
    """Raised if a proposal's risk_level doesn't match the tool's
    code-fixed risk_level (SPEC 4.2 rule #1 — the LLM cannot pick its own
    risk classification)."""


class ApprovalEngine:
    def __init__(
        self,
        session: AsyncSession,
        registry: ToolRegistry,
        allowlist: Allowlist,
        settings: Settings,
        notify: NotifyFn | None = None,
    ):
        self._session = session
        self._registry = registry
        self._allowlist = allowlist
        self._settings = settings
        self._notify = notify

    async def _write_audit(self, actor: str, event_type: str, entity_type: str, entity_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
        self._session.add(
            AuditLog(actor=actor, event_type=event_type, entity_type=entity_type, entity_id=entity_id, payload=payload)
        )
        await self._session.flush()

    # -- creation -----------------------------------------------------------

    async def create_action(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        proposed_description: str,
        actor: str,
        risk_level: str | None = None,
        alert_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> Action:
        """Create a governance record BEFORE any execution (SPEC 4.2 rule #2).

        `risk_level`, if provided by the caller (e.g. echoed from the LLM's
        `propose_action` call), is validated against the tool's fixed
        risk_level and rejected on mismatch — the code-registered value
        always wins.
        """
        spec = self._registry.get_spec(tool_name)
        if not spec.is_write:
            raise ValueError(f"'{tool_name}' is not a write tool; nothing to approve")
        if risk_level is not None and risk_level != spec.risk_level:
            raise RiskLevelMismatchError(
                f"proposal risk_level='{risk_level}' does not match fixed risk_level="
                f"'{spec.risk_level}' for tool '{tool_name}'"
            )

        is_allowlisted = spec.risk_level == "low" and self._allowlist.is_allowlisted(tool_name, parameters)
        requires_approval = not is_allowlisted

        action = Action(
            alert_id=alert_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            risk_level=spec.risk_level,
            parameters=parameters,
            proposed_description=proposed_description,
            requires_approval=requires_approval,
            status="proposed",
        )
        self._session.add(action)
        await self._session.flush()

        await self._write_audit(
            actor=actor,
            event_type="action_proposed",
            entity_type="action",
            entity_id=action.id,
            payload={"tool_name": tool_name, "risk_level": spec.risk_level, "parameters": parameters, "allowlisted": is_allowlisted},
        )

        if self._notify is not None:
            await self._notify(action)

        if is_allowlisted:
            # Auto-approved by policy, not by a human — still fully audited
            # and still notified (informational, non-blocking per SPEC 7.3).
            action.status = "approved"
            action.approved_at = datetime.now(timezone.utc)
            await self._session.flush()
            await self._write_audit(
                actor="system:allowlist",
                event_type="action_auto_approved",
                entity_type="action",
                entity_id=action.id,
                payload={"tool_name": tool_name, "reason": "matched low-risk allowlist"},
            )
            await self.execute_approved_action(action.id, actor="system:allowlist")

        return action

    # -- human decisions ------------------------------------------------

    async def approve_action(self, action_id: uuid.UUID, user: User) -> Action:
        action = await self._get_action(action_id)
        if action.status != "proposed":
            raise InvalidActionStateError(f"action {action_id} is '{action.status}', not 'proposed'")

        min_role = RISK_LEVEL_MIN_ROLE[action.risk_level]
        if ROLE_APPROVAL_RANK[user.role] < ROLE_APPROVAL_RANK[min_role]:
            raise InsufficientRoleError(
                f"role '{user.role}' cannot approve a '{action.risk_level}' action (requires >= '{min_role}')"
            )

        action.status = "approved"
        action.approved_by = user.id
        action.approved_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._write_audit(
            actor=f"user:{user.id}",
            event_type="action_approved",
            entity_type="action",
            entity_id=action.id,
            payload={"tool_name": action.tool_name},
        )
        return await self.execute_approved_action(action_id, actor=f"user:{user.id}")

    async def reject_action(self, action_id: uuid.UUID, user: User, reason: str | None = None) -> Action:
        action = await self._get_action(action_id)
        if action.status != "proposed":
            raise InvalidActionStateError(f"action {action_id} is '{action.status}', not 'proposed'")

        action.status = "rejected"
        await self._session.flush()
        await self._write_audit(
            actor=f"user:{user.id}",
            event_type="action_rejected",
            entity_type="action",
            entity_id=action.id,
            payload={"tool_name": action.tool_name, "reason": reason or "unspecified"},
        )
        return action

    # -- execution (THE gated path) ---------------------------------------

    async def execute_approved_action(self, action_id: uuid.UUID, actor: str) -> Action:
        """The ONLY method that ever calls `registry.dispatch_write_tool`.

        Non-negotiable regression-tested invariant: raises
        InvalidActionStateError instead of executing unless
        `action.status == 'approved'`. There is no other path to execution.
        """
        action = await self._get_action(action_id)
        if action.status != "approved":
            raise InvalidActionStateError(
                f"refusing to execute action {action_id}: status='{action.status}', expected 'approved'"
            )

        action.status = "executing"
        await self._session.flush()
        await self._write_audit(actor=actor, event_type="action_executing", entity_type="action", entity_id=action.id, payload={})

        try:
            result = await self._registry.dispatch_write_tool(action.tool_name, action.parameters, approved=True)
            action.execution_result = {"ok": result.ok, "data": result.data, "error": result.error}
            action.status = "succeeded" if result.ok else "failed"
        except Exception as exc:  # noqa: BLE001 - any connector-level failure is recorded, never silently swallowed
            logger.exception("execution failed for action %s", action_id)
            action.execution_result = {"ok": False, "error": str(exc)}
            action.status = "failed"
        finally:
            action.executed_at = datetime.now(timezone.utc)
            await self._session.flush()
            await self._write_audit(
                actor=actor,
                event_type="action_execution_finished",
                entity_type="action",
                entity_id=action.id,
                payload={"status": action.status, "result": action.execution_result},
            )

        return action

    # -- timeout sweep (SPEC 7.2) -----------------------------------------

    def _timeout_for(self, risk_level: str) -> timedelta:
        mapping = {
            "low": self._settings.approval_timeout_minutes_low,
            "medium": self._settings.approval_timeout_minutes_medium,
            "high": self._settings.approval_timeout_minutes_high,
            "critical": self._settings.approval_timeout_minutes_critical,
        }
        return timedelta(minutes=mapping[risk_level])

    async def expire_timed_out_actions(self) -> list[Action]:
        """Reject any 'proposed' action whose approval window has elapsed.
        Never executes on timeout — the default is always rejection
        (SPEC 7.2: "nunca executa por default")."""
        now = datetime.now(timezone.utc)
        result = await self._session.execute(select(Action).where(Action.status == "proposed"))
        expired: list[Action] = []
        for action in result.scalars().all():
            created_at = action.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if now - created_at >= self._timeout_for(action.risk_level):
                action.status = "rejected"
                await self._session.flush()
                await self._write_audit(
                    actor="system:scheduler",
                    event_type="action_rejected",
                    entity_type="action",
                    entity_id=action.id,
                    payload={"tool_name": action.tool_name, "reason": "timeout"},
                )
                expired.append(action)
        return expired

    async def _get_action(self, action_id: uuid.UUID) -> Action:
        action = await self._session.get(Action, action_id)
        if action is None:
            raise ActionNotFoundError(f"no such action: {action_id}")
        return action
