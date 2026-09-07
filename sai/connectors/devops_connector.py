"""Azure DevOps connector — REST API v7.x, per SPEC.md section 5.2.

Auth: Personal Access Token with minimal scopes (Code: Read, Build: Read &
Execute, Release: Read & Execute, Work Items: Read), stored in Key Vault in
production (see README SECURITY section) and read here from
AZURE_DEVOPS_PAT.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

API_VERSION = "7.1"


class DevOpsConnector(BaseConnector):
    name = "azure_devops"

    def __init__(self, settings: Settings):
        self._settings = settings

    def _base_url(self, project: bool = True) -> str:
        org = self._settings.devops_org
        if project:
            return f"https://dev.azure.com/{org}/{self._settings.devops_project}/_apis"
        return f"https://dev.azure.com/{org}/_apis"

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f":{self._settings.devops_pat}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(url, headers=self._headers(), params=params or {})

    async def _post(self, url: str, json_body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, headers=self._headers(), json=json_body)

    def is_configured(self) -> bool:
        s = self._settings
        return bool(s.devops_org and s.devops_pat)

    async def healthcheck(self) -> bool:
        try:
            resp = await self._get(f"{self._base_url(project=False)}/projects", {"api-version": API_VERSION})
            return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("DevOps healthcheck failed")
            return False

    # -- read tools -------------------------------------------------------

    async def list_pipelines(self) -> ToolResult:
        try:
            resp = await self._get(f"{self._base_url()}/pipelines", {"api-version": API_VERSION})
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_pipeline_runs(self, pipeline_id: int) -> ToolResult:
        try:
            resp = await self._get(
                f"{self._base_url()}/pipelines/{pipeline_id}/runs", {"api-version": API_VERSION}
            )
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_repos(self) -> ToolResult:
        try:
            resp = await self._get(f"{self._base_url()}/git/repositories", {"api-version": API_VERSION})
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_file(self, repo: str, path: str) -> ToolResult:
        try:
            resp = await self._get(
                f"{self._base_url()}/git/repositories/{repo}/items",
                {"path": path, "api-version": API_VERSION, "includeContent": "true"},
            )
            resp.raise_for_status()
            return ToolResult(ok=True, data={"path": path, "content": resp.text})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_service_connections(self) -> ToolResult:
        try:
            resp = await self._get(
                f"{self._base_url()}/serviceendpoint/endpoints", {"api-version": API_VERSION}
            )
            resp.raise_for_status()
            data = resp.json()
            # Never leak connection secrets even if the API were to include any.
            for endpoint in data.get("value", []):
                endpoint.pop("authorization", None)
            return ToolResult(ok=True, data=data)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_variable_groups(self) -> ToolResult:
        """Lists variable groups WITHOUT exposing values marked as secret
        (SPEC 5.2 requirement)."""
        try:
            resp = await self._get(
                f"{self._base_url()}/distributedtask/variablegroups", {"api-version": API_VERSION}
            )
            resp.raise_for_status()
            data = resp.json()
            for group in data.get("value", []):
                variables = group.get("variables", {})
                for var_name, var_def in variables.items():
                    if isinstance(var_def, dict) and var_def.get("isSecret"):
                        var_def["value"] = "***REDACTED***"
            return ToolResult(ok=True, data=data)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def search_work_items(self, query: str) -> ToolResult:
        try:
            resp = await self._post(
                f"{self._base_url()}/wit/wiql?api-version={API_VERSION}", {"query": query}
            )
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools (SPEC 5.2: medium/high, always with approval) --------

    async def trigger_pipeline(self, pipeline_id: int, branch: str, parameters: dict[str, Any] | None = None) -> ToolResult:
        try:
            body: dict[str, Any] = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{branch}"}}}}
            if parameters:
                body["templateParameters"] = parameters
            resp = await self._post(
                f"{self._base_url()}/pipelines/{pipeline_id}/runs?api-version={API_VERSION}", body
            )
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    async def approve_release(self, release_id: int, stage_id: int) -> ToolResult:
        try:
            resp = await self._post(
                f"{self._base_url()}/release/releases/{release_id}/environments/{stage_id}?api-version={API_VERSION}",
                {"status": "inProgress"},
            )
            resp.raise_for_status()
            return ToolResult(ok=True, data=resp.json())
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="devops_list_pipelines",
                description="List Azure DevOps pipelines in the configured project.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_pipelines(),
            ),
            ToolSpec(
                name="devops_get_pipeline_runs",
                description="Get recent runs for a given pipeline.",
                json_schema={"type": "object", "properties": {"pipeline_id": {"type": "integer"}}, "required": ["pipeline_id"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_pipeline_runs(**kw),
            ),
            ToolSpec(
                name="devops_list_repos",
                description="List Git repositories in the configured project.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_repos(),
            ),
            ToolSpec(
                name="devops_get_file",
                description="Read a file's content from a repo (e.g. pipeline YAML, deploy scripts).",
                json_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["repo", "path"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_file(**kw),
            ),
            ToolSpec(
                name="devops_list_service_connections",
                description="List service connections (secrets never exposed).",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_service_connections(),
            ),
            ToolSpec(
                name="devops_list_variable_groups",
                description="List variable groups; values marked secret are redacted.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.list_variable_groups(),
            ),
            ToolSpec(
                name="devops_search_work_items",
                description="Search work items using a WIQL query.",
                json_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.search_work_items(**kw),
            ),
            ToolSpec(
                name="devops_trigger_pipeline",
                description="Trigger a pipeline run on a branch, with optional parameters. WRITE ACTION.",
                json_schema={
                    "type": "object",
                    "properties": {
                        "pipeline_id": {"type": "integer"},
                        "branch": {"type": "string"},
                        "parameters": {"type": "object"},
                    },
                    "required": ["pipeline_id", "branch"],
                },
                risk_level="medium", requires_approval=True, is_write=True,
                handler=lambda **kw: self.trigger_pipeline(**kw),
            ),
            ToolSpec(
                name="devops_approve_release",
                description=(
                    "Approve a release stage/environment, allowing the deployment to proceed. "
                    "High risk and IRREVERSIBLE — once a stage is approved the release moves "
                    "forward and cannot be un-approved; rolling back means deploying a previous "
                    "version. WRITE ACTION — always requires explicit human approval."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"release_id": {"type": "integer"}, "stage_id": {"type": "integer"}},
                    "required": ["release_id", "stage_id"],
                },
                risk_level="high", requires_approval=True, is_write=True,
                handler=lambda **kw: self.approve_release(**kw),
            ),
        ]
