"""Aggregates every connector's ToolSpecs and exposes:

1. The list of **read** tool definitions given directly to the LLM (they
   have no side effects, so the orchestrator executes them immediately).
2. The single `propose_action` meta-tool (SPEC 6.3) that the LLM MUST use
   instead of ever calling a write tool directly — its `tool_name` field is
   constrained to the catalog of known write tools below.
3. `dispatch_read_tool` / `dispatch_write_tool` — the only two code paths
   that ever touch a real external system for a write operation, and
   `dispatch_write_tool` is only ever called by the Approval Engine
   (sai/approval/engine.py) after it has independently verified the
   corresponding `Action.status == 'approved'` (or an allowlist match).

This registry is the seam described in SPEC 6.3: "O backend intercepta esse
tool call, cria o registro em `actions`, e SÓ libera a execução real após
aprovação — o LLM nunca chama a tool de execução diretamente sem passar por
esse gate." The LLM is architecturally incapable of calling a write tool —
it is never given those tool definitions; it can only fill out a
`propose_action` proposal.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sai.config import Settings
from sai.connectors.azure_connector import AzureConnector
from sai.connectors.base import Connector, ToolResult, ToolSpec
from sai.connectors.devops_connector import DevOpsConnector
from sai.connectors.f5_connector import F5Connector
from sai.connectors.grafana_connector import GrafanaConnector
from sai.connectors.linux_connector import LinuxConnector
from sai.connectors.nginx_connector import NginxConnector
from sai.connectors.sql_connector import SqlConnector
from sai.connectors.zabbix_connector import ZabbixConnector

logger = logging.getLogger(__name__)

PROPOSE_ACTION_TOOL_NAME = "propose_action"


class UnknownToolError(RuntimeError):
    pass


class WriteToolCalledDirectlyError(RuntimeError):
    """Raised if something attempts to route a write tool through the
    read-tool dispatch path — should never happen, but checked explicitly
    as defense in depth."""


class ToolRegistry:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._connectors: list[Connector] = [
            AzureConnector(settings),
            DevOpsConnector(settings),
            SqlConnector(settings),
            GrafanaConnector(settings),
            ZabbixConnector(settings),
            LinuxConnector(settings),
            NginxConnector(settings),
            F5Connector(settings),
        ]
        self._tool_specs: dict[str, ToolSpec] = {}
        for connector in self._connectors:
            for spec in connector.list_tools():
                if spec.name in self._tool_specs:
                    raise ValueError(f"duplicate tool name registered: {spec.name}")
                self._tool_specs[spec.name] = spec

    @property
    def connectors(self) -> list[Connector]:
        return self._connectors

    def get_spec(self, tool_name: str) -> ToolSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise UnknownToolError(f"no such tool: {tool_name}")
        return spec

    def all_write_tool_names(self) -> list[str]:
        return sorted(name for name, spec in self._tool_specs.items() if spec.is_write)

    def all_read_tool_names(self) -> list[str]:
        return sorted(name for name, spec in self._tool_specs.items() if not spec.is_write)

    # -- Anthropic tool defs given to the LLM --------------------------------

    def get_read_tool_definitions(self) -> list[dict[str, Any]]:
        """Only non-write tools are ever exposed directly to the model."""
        return [spec.to_anthropic_tool() for spec in self._tool_specs.values() if not spec.is_write]

    def get_propose_action_tool_definition(self) -> dict[str, Any]:
        """The dedicated `propose_action` tool per SPEC 6.3. This is the
        ONLY mechanism by which the model can request a write action —
        write tools themselves are never in the model's tool list."""
        return {
            "name": PROPOSE_ACTION_TOOL_NAME,
            "description": (
                "Propose a write/state-changing action for human approval. This is the ONLY way to "
                "request a change to the environment — you can never call a write tool directly. "
                "The backend will create a governance record and route it through the approval "
                "workflow (or the pre-approved low-risk allowlist, where applicable)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": self.all_write_tool_names(),
                        "description": "The exact write tool this proposal would execute once approved.",
                    },
                    "risk_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Must match the tool's fixed risk_level exactly — the backend validates this.",
                    },
                    "target": {"type": "string", "description": "Human description of the affected resource."},
                    "reasoning": {"type": "string", "description": "Why this action resolves the diagnosed problem."},
                    "expected_impact": {"type": "string", "description": "What will happen, including estimated downtime."},
                    "rollback_plan": {"type": "string", "description": "How to revert if it goes wrong."},
                    "parameters": {
                        "type": "object",
                        "description": "The exact parameters that would be passed to the underlying tool.",
                    },
                },
                "required": [
                    "tool_name",
                    "risk_level",
                    "target",
                    "reasoning",
                    "expected_impact",
                    "rollback_plan",
                    "parameters",
                ],
            },
        }

    def get_all_llm_tool_definitions(self) -> list[dict[str, Any]]:
        return self.get_read_tool_definitions() + [self.get_propose_action_tool_definition()]

    # -- dispatch -------------------------------------------------------

    async def dispatch_read_tool(self, tool_name: str, kwargs: dict[str, Any]) -> ToolResult:
        spec = self.get_spec(tool_name)
        if spec.is_write:
            raise WriteToolCalledDirectlyError(
                f"tool '{tool_name}' is a write tool and must go through propose_action, not direct dispatch"
            )
        try:
            return await spec.handler(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any connector failure as a ToolResult
            logger.exception("read tool %s failed", tool_name)
            return ToolResult(ok=False, error=str(exc))

    async def dispatch_write_tool(self, tool_name: str, kwargs: dict[str, Any], approved: bool) -> ToolResult:
        """The ONLY entry point for real write-tool execution.

        Callers (exclusively sai/approval/engine.py) MUST have already
        confirmed the corresponding Action is 'approved' or allowlisted
        before calling this with `approved=True`. This method re-validates
        `spec.is_write` and forwards `approved` as `_approved` for the two
        connectors (SQL restore, F5 WAF policy) with an additional
        in-function guard.
        """
        spec = self.get_spec(tool_name)
        if not spec.is_write:
            raise WriteToolCalledDirectlyError(f"tool '{tool_name}' is not a write tool")
        if not approved:
            raise PermissionError(f"attempted to dispatch write tool '{tool_name}' without approval")

        call_kwargs = dict(kwargs)
        # Only forward `_approved` to handlers that declare it (SQL restore, F5 WAF policy);
        # harmless additional guard on top of this method's own `approved` check.
        if tool_name in {"sql_restore_database", "f5_update_waf_policy"}:
            call_kwargs["_approved"] = True

        try:
            return await spec.handler(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("write tool %s failed", tool_name)
            return ToolResult(ok=False, error=str(exc))

    async def healthcheck_all(self, timeout_seconds: float = 5.0) -> dict[str, str]:
        """Checks every *configured* connector, in parallel and time-bounded.

        Returns one of `"ok"`, `"error"`, `"timeout"` or `"not_configured"`
        per connector rather than a bare bool, because those four cases call
        for different reactions: a system this deployment doesn't use is not
        a fault, while one that is configured and unreachable is.

        Unconfigured connectors are skipped without any network call — trying
        to reach them only buys a DNS/TCP timeout each, which is what made
        this endpoint take seconds. Checks run concurrently and each is capped
        at `timeout_seconds`, so total latency is bounded by the slowest
        single connector instead of the sum of all of them.
        """

        async def check(connector: Connector) -> tuple[str, str]:
            if not connector.is_configured():
                return connector.name, "not_configured"
            try:
                async with asyncio.timeout(timeout_seconds):
                    ok = await connector.healthcheck()
                return connector.name, "ok" if ok else "error"
            except TimeoutError:
                logger.warning("healthcheck timed out for %s", connector.name)
                return connector.name, "timeout"
            except Exception:  # noqa: BLE001
                logger.exception("healthcheck failed for %s", connector.name)
                return connector.name, "error"

        pairs = await asyncio.gather(*(check(c) for c in self._connectors))
        return dict(pairs)
