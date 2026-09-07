"""Approval Engine tests — SPEC.md section 13, item 3: the mandatory,
CI-blocking regression test proving that no `high`/`critical` risk tool
(and no `low` tool outside the allowlist) can ever execute without
`Action.status == 'approved'`.
"""
from __future__ import annotations

import uuid

import pytest

from sai.approval.engine import (
    ApprovalEngine,
    InsufficientRoleError,
    InvalidActionStateError,
)
from sai.connectors.f5_connector import ApprovalRequiredError as F5ApprovalRequiredError
from sai.connectors.f5_connector import F5Connector
from sai.connectors.registry import ToolRegistry
from sai.connectors.sql_connector import ApprovalRequiredError as SqlApprovalRequiredError
from sai.connectors.sql_connector import SqlConnector
from sai.db.models import User


@pytest.mark.asyncio
async def test_critical_tool_cannot_execute_without_approved_status(approval_engine: ApprovalEngine):
    """THE non-negotiable regression test (SPEC 13.3): a critical-risk
    action must sit at status='proposed' after creation, and any attempt
    to run execute_approved_action on it before approval must raise —
    never silently execute."""
    action = await approval_engine.create_action(
        tool_name="sql_restore_database",
        parameters={"database": "orders", "backup_file": "orders_2026.bak"},
        proposed_description="Restore orders DB to recover from corruption",
        actor="user:test",
    )

    assert action.status == "proposed"
    assert action.risk_level == "critical"
    assert action.requires_approval is True

    with pytest.raises(InvalidActionStateError):
        await approval_engine.execute_approved_action(action.id, actor="user:test")

    # still not executed
    assert action.status == "proposed"
    assert action.executed_at is None


@pytest.mark.asyncio
async def test_high_risk_tool_cannot_execute_without_approved_status(approval_engine: ApprovalEngine):
    action = await approval_engine.create_action(
        tool_name="f5_update_waf_policy",
        parameters={"policy_name": "prod-waf", "change_description": "loosen rule X"},
        proposed_description="Loosen WAF rule causing false positives",
        actor="user:test",
    )
    assert action.status == "proposed"
    with pytest.raises(InvalidActionStateError):
        await approval_engine.execute_approved_action(action.id, actor="user:test")


@pytest.mark.asyncio
async def test_approval_then_execution_succeeds(approval_engine: ApprovalEngine, admin_user: User, monkeypatch):
    """Sanity check for the opposite path: once approved by a sufficiently
    privileged user, execution proceeds and reaches a terminal status."""

    async def fake_kill_session(**kwargs):
        from sai.connectors.base import ToolResult

        return ToolResult(ok=True, data={"session_id": kwargs["session_id"], "status": "killed"})

    action = await approval_engine.create_action(
        tool_name="sql_kill_session",
        parameters={"session_id": 55},
        proposed_description="Kill runaway session blocking others",
        actor="user:test",
    )
    # monkeypatch the underlying connector call so no real DB is touched
    spec = approval_engine._registry.get_spec("sql_kill_session")
    monkeypatch.setattr(spec, "handler", fake_kill_session)

    approved = await approval_engine.approve_action(action.id, admin_user)
    assert approved.status == "succeeded"
    assert approved.approved_by == admin_user.id
    assert approved.executed_at is not None


@pytest.mark.asyncio
async def test_operator_cannot_approve_critical_action(approval_engine: ApprovalEngine, operator_user: User):
    action = await approval_engine.create_action(
        tool_name="sql_restore_database",
        parameters={"database": "orders", "backup_file": "x.bak"},
        proposed_description="restore",
        actor="user:test",
    )
    with pytest.raises(InsufficientRoleError):
        await approval_engine.approve_action(action.id, operator_user)
    assert action.status == "proposed"


@pytest.mark.asyncio
async def test_registry_dispatch_write_tool_refuses_without_approved_flag(registry: ToolRegistry):
    with pytest.raises(PermissionError):
        await registry.dispatch_write_tool(
            "sql_restore_database", {"database": "orders", "backup_file": "x.bak"}, approved=False
        )


def test_sql_connector_refuses_restore_without_approval(settings):
    """Defense-in-depth: even if something bypassed the registry entirely
    and called the connector method directly, it still refuses."""
    import asyncio

    connector = SqlConnector(settings)
    with pytest.raises(SqlApprovalRequiredError):
        asyncio.run(connector.restore_database(database="orders", backup_file="x.bak", _approved=False))


def test_f5_connector_refuses_waf_update_without_approval(settings):
    import asyncio

    connector = F5Connector(settings)
    with pytest.raises(F5ApprovalRequiredError):
        asyncio.run(connector.update_waf_policy(policy_name="prod-waf", change_description="x", _approved=False))


@pytest.mark.asyncio
async def test_allowlisted_low_risk_action_auto_executes(fake_session, registry, sample_allowlist, settings, monkeypatch):
    """Only a `low` risk tool matching the exact allowlist scope may skip
    human approval — and it must still be fully audited."""

    async def fake_ack(**kwargs):
        from sai.connectors.base import ToolResult

        return ToolResult(ok=True, data={"event_id": kwargs["event_id"], "status": "acknowledged"})

    spec = registry.get_spec("zabbix_acknowledge_problem")
    monkeypatch.setattr(spec, "handler", fake_ack)

    engine = ApprovalEngine(session=fake_session, registry=registry, allowlist=sample_allowlist, settings=settings, notify=None)
    action = await engine.create_action(
        tool_name="zabbix_acknowledge_problem",
        parameters={"event_id": "123", "message": "auto-ack"},
        proposed_description="Acknowledge known transient problem",
        actor="system:vigia",
    )

    assert action.requires_approval is False
    assert action.status == "succeeded"

    from sai.db.models import AuditLog

    audit_entries = list(fake_session.objects.get(AuditLog, {}).values())
    event_types = {e.event_type for e in audit_entries}
    assert "action_proposed" in event_types
    assert "action_auto_approved" in event_types
    assert "action_executing" in event_types
    assert "action_execution_finished" in event_types


@pytest.mark.asyncio
async def test_non_allowlisted_low_risk_action_still_requires_approval(approval_engine: ApprovalEngine):
    """A low-risk tool NOT matching the allowlist must still wait for a human."""
    action = await approval_engine.create_action(
        tool_name="linux_restart_service",
        parameters={"host": "some-other-host.internal", "service": "nginx"},
        proposed_description="restart nginx",
        actor="user:test",
    )
    assert action.status == "proposed"
    assert action.requires_approval is True
