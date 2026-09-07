"""F5 BIG-IP connector — iControl REST, per SPEC.md section 5.8, plus WAF
tools per section 5.9 (Azure WAF is covered by the Azure connector; F5 ASM
policies are covered here).

Hard rule enforced IN CODE: `update_waf_policy` raises unless called with
`_approved=True`, which is only ever set by the connector registry after
confirming an `Action` row has `status == 'approved'`. This mirrors the
sql_connector.restore_database defense-in-depth pattern for the other
uniquely destructive tool in the system.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ApprovalRequiredError(RuntimeError):
    pass


class F5Connector(BaseConnector):
    name = "f5"

    def __init__(self, settings: Settings):
        self._settings = settings

    def _auth(self) -> tuple[str, str]:
        return (self._settings.f5_api_user, self._settings.f5_api_password)

    def _base_url(self) -> str:
        return f"https://{self._settings.f5_base_url}/mgmt/tm"

    def is_configured(self) -> bool:
        s = self._settings
        return bool(s.f5_api_user and s.f5_api_password and s.f5_base_url and "example.com" not in s.f5_base_url)

    async def healthcheck(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                resp = await client.get(f"{self._base_url()}/sys/version", auth=self._auth())
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("F5 healthcheck failed")
            return False

    # -- read tools -------------------------------------------------------

    async def get_pool_status(self, pool_name: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/ltm/pool/{pool_name}/stats", auth=self._auth())
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_node_status(self, node: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/ltm/node/{node}/stats", auth=self._auth())
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_virtual_server_stats(self, vs_name: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/ltm/virtual/{vs_name}/stats", auth=self._auth())
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_active_connections(self, vs_name: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    f"{self._base_url()}/ltm/virtual/{vs_name}/stats",
                    auth=self._auth(),
                    params={"select": "clientside.curConns"},
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_blocked_requests(self, timerange: dict[str, Any]) -> ToolResult:
        """WAF read tool (SPEC 5.9) — reads ASM event log summary."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/asm/events/requests", auth=self._auth(), params=timerange)
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_active_rules(self) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/asm/policies", auth=self._auth())
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_rule_hit_counts(self) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{self._base_url()}/asm/events/requests", auth=self._auth(), params={"select": "violations"})
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools (SPEC 5.8/5.9: high/critical, always approval) -------

    async def disable_pool_member(self, pool: str, member: str) -> ToolResult:
        """risk_level=high — draining for maintenance."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.patch(
                    f"{self._base_url()}/ltm/pool/{pool}/members/{member}",
                    auth=self._auth(),
                    json={"session": "user-disabled", "state": "user-down"},
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data={"pool": pool, "member": member, "status": "disabled"})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def enable_pool_member(self, pool: str, member: str) -> ToolResult:
        """risk_level=high."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.patch(
                    f"{self._base_url()}/ltm/pool/{pool}/members/{member}",
                    auth=self._auth(),
                    json={"session": "user-enabled", "state": "user-up"},
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data={"pool": pool, "member": member, "status": "enabled"})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def update_waf_policy(
        self, policy_name: str, change_description: str, _approved: bool = False
    ) -> ToolResult:
        """risk_level=critical — ALWAYS requires approval, no exception.
        Snapshots the previous policy before applying any change.

        `_approved` is set exclusively by the registry after verifying an
        approved `Action` row exists — see sql_connector.restore_database
        for the identical pattern and rationale.
        """
        if not _approved:
            raise ApprovalRequiredError("f5_update_waf_policy cannot execute without an approved Action record")
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                snapshot_resp = await client.get(f"{self._base_url()}/asm/policies/{policy_name}", auth=self._auth())
                snapshot_resp.raise_for_status()
                previous_policy_snapshot = snapshot_resp.json()

                apply_resp = await client.patch(
                    f"{self._base_url()}/asm/policies/{policy_name}",
                    auth=self._auth(),
                    json={"description": change_description},
                )
                apply_resp.raise_for_status()
                return ToolResult(
                    ok=True,
                    data={
                        "policy_name": policy_name,
                        "change_description": change_description,
                        "previous_policy_snapshot": previous_policy_snapshot,
                        "status": "updated",
                    },
                )
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def toggle_waf_rule(self, rule_id: str, enabled: bool) -> ToolResult:
        """risk_level=critical (SPEC 5.9)."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.patch(
                    f"{self._base_url()}/asm/policies/rules/{rule_id}", auth=self._auth(), json={"enabled": enabled}
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data={"rule_id": rule_id, "enabled": enabled})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def add_waf_exclusion(self, rule_id: str, condition: str) -> ToolResult:
        """risk_level=critical (SPEC 5.9)."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{self._base_url()}/asm/policies/rules/{rule_id}/exclusions",
                    auth=self._auth(),
                    json={"condition": condition},
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data={"rule_id": rule_id, "condition": condition})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="f5_get_pool_status",
                description="Get F5 LTM pool status/statistics.",
                json_schema={"type": "object", "properties": {"pool_name": {"type": "string"}}, "required": ["pool_name"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_pool_status(**kw),
            ),
            ToolSpec(
                name="f5_get_node_status",
                description="Get F5 node status/statistics.",
                json_schema={"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_node_status(**kw),
            ),
            ToolSpec(
                name="f5_get_virtual_server_stats",
                description="Get statistics for an F5 virtual server.",
                json_schema={"type": "object", "properties": {"vs_name": {"type": "string"}}, "required": ["vs_name"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_virtual_server_stats(**kw),
            ),
            ToolSpec(
                name="f5_get_active_connections",
                description="Get current active connection count for a virtual server.",
                json_schema={"type": "object", "properties": {"vs_name": {"type": "string"}}, "required": ["vs_name"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_active_connections(**kw),
            ),
            ToolSpec(
                name="waf_get_blocked_requests",
                description="List WAF-blocked requests over a timerange.",
                json_schema={"type": "object", "properties": {"timerange": {"type": "object"}}, "required": ["timerange"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_blocked_requests(**kw),
            ),
            ToolSpec(
                name="waf_get_active_rules",
                description="List active WAF (ASM) policies/rules.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_active_rules(),
            ),
            ToolSpec(
                name="waf_get_rule_hit_counts",
                description="Get hit counts per WAF rule/violation type.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_rule_hit_counts(),
            ),
            ToolSpec(
                name="f5_disable_pool_member",
                description="Disable (drain) a pool member for maintenance. WRITE ACTION — always requires approval.",
                json_schema={
                    "type": "object",
                    "properties": {"pool": {"type": "string"}, "member": {"type": "string"}},
                    "required": ["pool", "member"],
                },
                risk_level="high", requires_approval=True, is_write=True,
                handler=lambda **kw: self.disable_pool_member(**kw),
            ),
            ToolSpec(
                name="f5_enable_pool_member",
                description="Re-enable a pool member. WRITE ACTION — always requires approval.",
                json_schema={
                    "type": "object",
                    "properties": {"pool": {"type": "string"}, "member": {"type": "string"}},
                    "required": ["pool", "member"],
                },
                risk_level="high", requires_approval=True, is_write=True,
                handler=lambda **kw: self.enable_pool_member(**kw),
            ),
            ToolSpec(
                name="f5_update_waf_policy",
                description=(
                    "Update an F5 ASM WAF policy. CRITICAL WRITE ACTION — always requires explicit "
                    "human approval; snapshots the previous policy before applying."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"policy_name": {"type": "string"}, "change_description": {"type": "string"}},
                    "required": ["policy_name", "change_description"],
                },
                risk_level="critical", requires_approval=True, is_write=True,
                handler=lambda **kw: self.update_waf_policy(**kw),
            ),
            ToolSpec(
                name="waf_toggle_rule",
                description="Enable/disable a WAF rule. CRITICAL WRITE ACTION — always requires approval.",
                json_schema={
                    "type": "object",
                    "properties": {"rule_id": {"type": "string"}, "enabled": {"type": "boolean"}},
                    "required": ["rule_id", "enabled"],
                },
                risk_level="critical", requires_approval=True, is_write=True,
                handler=lambda **kw: self.toggle_waf_rule(**kw),
            ),
            ToolSpec(
                name="waf_add_exclusion",
                description="Add an exclusion condition to a WAF rule. CRITICAL WRITE ACTION — always requires approval.",
                json_schema={
                    "type": "object",
                    "properties": {"rule_id": {"type": "string"}, "condition": {"type": "string"}},
                    "required": ["rule_id", "condition"],
                },
                risk_level="critical", requires_approval=True, is_write=True,
                handler=lambda **kw: self.add_waf_exclusion(**kw),
            ),
        ]
