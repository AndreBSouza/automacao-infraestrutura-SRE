"""Grafana connector — REST API, per SPEC.md section 5.4 (spec numbers this
5.5 in the "Grafana" heading; implemented regardless of the doc's numbering
quirk, per task instructions).

Auth: Service Account token, Viewer role for reads (Editor only if the
system is also asked to manage dashboards/alerts — not implemented here).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class GrafanaConnector(BaseConnector):
    name = "grafana"

    def __init__(self, settings: Settings):
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.grafana_api_token}", "Content-Type": "application/json"}

    def is_configured(self) -> bool:
        s = self._settings
        return bool(s.grafana_api_token and s.grafana_base_url and "example.com" not in s.grafana_base_url)

    async def healthcheck(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._settings.grafana_base_url}/api/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("Grafana healthcheck failed")
            return False

    async def query_datasource(self, datasource_uid: str, query: str, timerange: dict[str, Any]) -> ToolResult:
        try:
            body = {
                "queries": [{"datasource": {"uid": datasource_uid}, "expr": query, "refId": "A"}],
                "from": str(timerange.get("from", "now-1h")),
                "to": str(timerange.get("to", "now")),
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._settings.grafana_base_url}/api/ds/query", headers=self._headers(), json=body
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_alert_rules(self) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._settings.grafana_base_url}/api/v1/provisioning/alert-rules", headers=self._headers()
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_alert_state(self, rule_uid: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._settings.grafana_base_url}/api/v1/provisioning/alert-rules/{rule_uid}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_dashboard(self, uid: str) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._settings.grafana_base_url}/api/dashboards/uid/{uid}", headers=self._headers()
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_annotations(self, timerange: dict[str, Any]) -> ToolResult:
        try:
            params = {"from": timerange.get("from"), "to": timerange.get("to")}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._settings.grafana_base_url}/api/annotations", headers=self._headers(), params=params
                )
                resp.raise_for_status()
                return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="grafana_query_datasource",
                description="Query a Grafana-connected datasource (e.g. Prometheus/InfluxDB) directly.",
                json_schema={
                    "type": "object",
                    "properties": {
                        "datasource_uid": {"type": "string"},
                        "query": {"type": "string"},
                        "timerange": {"type": "object"},
                    },
                    "required": ["datasource_uid", "query", "timerange"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.query_datasource(**kw),
            ),
            ToolSpec(
                name="grafana_list_alert_rules",
                description="List configured Grafana alert rules.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_alert_rules(),
            ),
            ToolSpec(
                name="grafana_get_alert_state",
                description="Get the current state of a specific alert rule.",
                json_schema={"type": "object", "properties": {"rule_uid": {"type": "string"}}, "required": ["rule_uid"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_alert_state(**kw),
            ),
            ToolSpec(
                name="grafana_get_dashboard",
                description="Fetch a dashboard definition by uid, to understand what each panel represents.",
                json_schema={"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_dashboard(**kw),
            ),
            ToolSpec(
                name="grafana_list_annotations",
                description="List manually marked annotations (deploys, incidents) within a timerange.",
                json_schema={"type": "object", "properties": {"timerange": {"type": "object"}}, "required": ["timerange"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_annotations(**kw),
            ),
        ]
