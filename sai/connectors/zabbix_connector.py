"""Zabbix connector — JSON-RPC API, per SPEC.md section 5.4 (labeled 5.5 in
the doc's own numbering; implemented fully regardless, per task instructions).

`acknowledge_problem` and `create_maintenance_window` are low-risk,
allowlist-eligible write tools (SPEC 7.3) — they still ALWAYS create an
`actions` row and audit_log entries; only the "requires human approval
before execution" gate is skipped when the allowlist matches.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ZabbixConnector(BaseConnector):
    name = "zabbix"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._request_id = 0

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._request_id,
        }
        headers = {
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {self._settings.zabbix_api_token}",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self._settings.zabbix_base_url}/api_jsonrpc.php", headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"Zabbix API error: {body['error']}")
            return body["result"]

    def is_configured(self) -> bool:
        s = self._settings
        return bool(s.zabbix_api_token and s.zabbix_base_url and "example.com" not in s.zabbix_base_url)

    async def healthcheck(self) -> bool:
        try:
            await self._rpc("apiinfo.version", {})
            return True
        except (httpx.HTTPError, RuntimeError):
            logger.exception("Zabbix healthcheck failed")
            return False

    # -- read tools -----------------------------------------------------

    async def get_problems(self, severity_min: int = 0) -> ToolResult:
        try:
            result = await self._rpc(
                "problem.get",
                {"output": "extend", "severities": list(range(severity_min, 6)), "recent": False, "sortfield": ["eventid"], "sortorder": "DESC"},
            )
            return ToolResult(ok=True, data={"problems": result})
        except (httpx.HTTPError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_host_items(self, host: str, item_keys: list[str]) -> ToolResult:
        try:
            hosts = await self._rpc("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
            if not hosts:
                return ToolResult(ok=False, error=f"host not found: {host}")
            host_id = hosts[0]["hostid"]
            items = await self._rpc(
                "item.get",
                {"hostids": host_id, "output": "extend", "filter": {"key_": item_keys} if item_keys else {}},
            )
            return ToolResult(ok=True, data={"host": host, "items": items})
        except (httpx.HTTPError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_history(self, item_id: str, timerange: dict[str, Any]) -> ToolResult:
        try:
            result = await self._rpc(
                "history.get",
                {
                    "itemids": item_id,
                    "history": 0,
                    "time_from": timerange.get("from"),
                    "time_till": timerange.get("to"),
                    "output": "extend",
                    "sortfield": "clock",
                    "sortorder": "ASC",
                },
            )
            return ToolResult(ok=True, data={"item_id": item_id, "history": result})
        except (httpx.HTTPError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_triggers(self, host: str) -> ToolResult:
        try:
            hosts = await self._rpc("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
            if not hosts:
                return ToolResult(ok=False, error=f"host not found: {host}")
            triggers = await self._rpc(
                "trigger.get", {"hostids": hosts[0]["hostid"], "output": "extend", "expandDescription": True}
            )
            return ToolResult(ok=True, data={"host": host, "triggers": triggers})
        except (httpx.HTTPError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools (low risk, allowlist-eligible) ----------------------

    async def acknowledge_problem(self, event_id: str, message: str) -> ToolResult:
        try:
            result = await self._rpc(
                "event.acknowledge", {"eventids": event_id, "message": message, "action": 6}
            )
            return ToolResult(ok=True, data=result)
        except (httpx.HTTPError, RuntimeError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def create_maintenance_window(self, host_group: str, start: str, end: str, reason: str) -> ToolResult:
        try:
            groups = await self._rpc("hostgroup.get", {"filter": {"name": [host_group]}, "output": ["groupid"]})
            if not groups:
                return ToolResult(ok=False, error=f"host group not found: {host_group}")
            import datetime

            start_ts = int(datetime.datetime.fromisoformat(start).timestamp())
            end_ts = int(datetime.datetime.fromisoformat(end).timestamp())
            result = await self._rpc(
                "maintenance.create",
                {
                    "name": f"SAI-{reason[:40]}",
                    "active_since": start_ts,
                    "active_till": end_ts,
                    "groupids": [groups[0]["groupid"]],
                    "timeperiods": [
                        {"timeperiod_type": 0, "start_date": start_ts, "period": max(end_ts - start_ts, 60)}
                    ],
                    "description": reason,
                },
            )
            return ToolResult(ok=True, data=result)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="zabbix_get_problems",
                description="List active Zabbix problems at or above a minimum severity (0-5).",
                json_schema={"type": "object", "properties": {"severity_min": {"type": "integer", "default": 0}}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_problems(**kw),
            ),
            ToolSpec(
                name="zabbix_get_host_items",
                description="Get current item values (CPU, memory, disk, etc.) for a host.",
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "item_keys": {"type": "array", "items": {"type": "string"}}},
                    "required": ["host", "item_keys"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_host_items(**kw),
            ),
            ToolSpec(
                name="zabbix_get_history",
                description="Get historical time series for an item over a timerange.",
                json_schema={
                    "type": "object",
                    "properties": {"item_id": {"type": "string"}, "timerange": {"type": "object"}},
                    "required": ["item_id", "timerange"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_history(**kw),
            ),
            ToolSpec(
                name="zabbix_get_triggers",
                description="List triggers configured for a host.",
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_triggers(**kw),
            ),
            ToolSpec(
                name="zabbix_acknowledge_problem",
                description=(
                    "Acknowledge an active Zabbix problem with a message. "
                    "LOW-RISK WRITE ACTION — allowlist-eligible (see allowlist.yaml)."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}, "message": {"type": "string"}},
                    "required": ["event_id", "message"],
                },
                risk_level="low", requires_approval=True, is_write=True,
                handler=lambda **kw: self.acknowledge_problem(**kw),
            ),
            ToolSpec(
                name="zabbix_create_maintenance_window",
                description=(
                    "Create a Zabbix maintenance window for a host group. "
                    "LOW-RISK WRITE ACTION — allowlist-eligible."
                ),
                json_schema={
                    "type": "object",
                    "properties": {
                        "host_group": {"type": "string"},
                        "start": {"type": "string", "description": "ISO8601 datetime"},
                        "end": {"type": "string", "description": "ISO8601 datetime"},
                        "reason": {"type": "string"},
                    },
                    "required": ["host_group", "start", "end", "reason"],
                },
                risk_level="low", requires_approval=True, is_write=True,
                handler=lambda **kw: self.create_maintenance_window(**kw),
            ),
        ]
