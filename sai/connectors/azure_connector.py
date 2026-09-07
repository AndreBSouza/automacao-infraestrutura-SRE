"""Azure connector — Resource Graph, Monitor, Activity Log, Compute, SQL.

Per SPEC.md section 5.1:
  - Read tools use a Reader-scoped Service Principal (least privilege).
  - Write tools use a SEPARATE, more privileged credential
    (AZURE_WRITE_CLIENT_ID/SECRET) granted only the specific roles needed
    (Virtual Machine Contributor, etc.) — never Owner/Contributor at the
    subscription scope. This mirrors SPEC 10.2 (segregation of read/write).

Web Apps (Azure App Service) are explicitly covered via
`azure_query_resources` (KQL against `Microsoft.Web/sites`) and the
dedicated `azure_scale_app_service` write tool, since the user specifically
operates web apps in Azure.
"""
from __future__ import annotations

import logging
from typing import Any

from azure.core.exceptions import AzureError
from azure.identity import ClientSecretCredential
from azure.mgmt.compute.aio import ComputeManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.monitor.query import LogsQueryStatus
from azure.monitor.query.aio import LogsQueryClient, MetricsQueryClient

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class AzureConnector(BaseConnector):
    name = "azure"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._read_credential: ClientSecretCredential | None = None
        self._write_credential: ClientSecretCredential | None = None

    # -- credential helpers -------------------------------------------------

    def _get_read_credential(self) -> ClientSecretCredential:
        if self._read_credential is None:
            self._read_credential = ClientSecretCredential(
                tenant_id=self._settings.azure_tenant_id,
                client_id=self._settings.azure_client_id,
                client_secret=self._settings.azure_client_secret,
            )
        return self._read_credential

    def _get_write_credential(self) -> ClientSecretCredential:
        """Distinct, more privileged credential — used ONLY by write tools,
        and only ever invoked after the Approval Engine has cleared the
        action (see sai/approval/engine.py)."""
        if self._write_credential is None:
            self._write_credential = ClientSecretCredential(
                tenant_id=self._settings.azure_tenant_id,
                client_id=self._settings.azure_write_client_id or self._settings.azure_client_id,
                client_secret=self._settings.azure_write_client_secret or self._settings.azure_client_secret,
            )
        return self._write_credential

    def is_configured(self) -> bool:
        s = self._settings
        return bool(s.azure_tenant_id and s.azure_client_id and s.azure_client_secret and s.azure_subscription_id)

    async def healthcheck(self) -> bool:
        try:
            client = ResourceGraphClient(credential=self._get_read_credential())
            client.resources(QueryRequest(subscriptions=[self._settings.azure_subscription_id], query="Resources | limit 1"))
            return True
        except AzureError:
            logger.exception("Azure healthcheck failed")
            return False

    # -- read tools -----------------------------------------------------

    async def query_resources(self, kql_query: str) -> ToolResult:
        """Run a KQL query against Azure Resource Graph.

        Example for enumerating Web Apps (App Service):
            "Resources | where type =~ 'microsoft.web/sites' | project name, resourceGroup, location, properties.state"
        """
        try:
            client = ResourceGraphClient(credential=self._get_read_credential())
            request = QueryRequest(subscriptions=[self._settings.azure_subscription_id], query=kql_query)
            response = client.resources(request)
            return ToolResult(ok=True, data={"rows": response.data, "count": response.total_records})
        except AzureError as exc:
            logger.exception("azure_query_resources failed")
            return ToolResult(ok=False, error=str(exc))

    async def list_web_apps(self) -> ToolResult:
        """Convenience wrapper: enumerate every App Service (Web App) in
        the subscription via Resource Graph. Explicitly requested by the
        infra team since Web Apps are one of their primary Azure assets."""
        return await self.query_resources(
            "Resources | where type =~ 'microsoft.web/sites' "
            "| project name, resourceGroup, location, kind, "
            "state=properties.state, defaultHostName=properties.defaultHostName, "
            "sku=properties.sku"
        )

    async def get_metrics(self, resource_id: str, metric_names: list[str], timespan: str) -> ToolResult:
        """Query Azure Monitor metrics for a resource.

        `timespan` is an ISO 8601 interval, e.g. "PT1H" trailing or
        "2026-01-01T00:00:00Z/2026-01-01T06:00:00Z".
        """
        try:
            client = MetricsQueryClient(credential=self._get_read_credential())
            response = await client.query_resource(
                resource_uri=resource_id, metric_names=metric_names, timespan=timespan
            )
            metrics_out = []
            for metric in response.metrics:
                series = []
                for ts in metric.timeseries:
                    series.append(
                        [
                            {"timestamp": str(dp.timestamp), "average": dp.average, "total": dp.total}
                            for dp in ts.data
                        ]
                    )
                metrics_out.append({"name": metric.name, "unit": str(metric.unit), "series": series})
            await client.close()
            return ToolResult(ok=True, data={"metrics": metrics_out})
        except AzureError as exc:
            logger.exception("azure_get_metrics failed")
            return ToolResult(ok=False, error=str(exc))

    async def get_activity_log(self, resource_id: str, timespan: str) -> ToolResult:
        """Query the native Azure Activity Log for a resource via a Log
        Analytics workspace-backed KQL query (AzureActivity table)."""
        try:
            client = LogsQueryClient(credential=self._get_read_credential())
            query = (
                "AzureActivity | where ResourceId =~ '%s' "
                "| project TimeGenerated, OperationNameValue, ActivityStatusValue, Caller "
                "| order by TimeGenerated desc | take 200" % resource_id
            )
            # The Log Analytics workspace GUID is a distinct identifier from the
            # subscription id; querying with the wrong one fails every time.
            workspace_id = self._settings.azure_log_analytics_workspace_id
            if not workspace_id:
                await client.close()
                return ToolResult(
                    ok=False,
                    error=(
                        "AZURE_LOG_ANALYTICS_WORKSPACE_ID is not configured; "
                        "azure_get_activity_log requires a Log Analytics workspace GUID."
                    ),
                )
            response = await client.query_workspace(workspace_id=workspace_id, query=query, timespan=timespan)
            if response.status == LogsQueryStatus.SUCCESS:
                tables = [
                    {"columns": t.columns, "rows": t.rows} for t in response.tables
                ]
                await client.close()
                return ToolResult(ok=True, data={"tables": tables})
            await client.close()
            return ToolResult(ok=False, error=f"partial failure: {response.partial_error}")
        except AzureError as exc:
            logger.exception("azure_get_activity_log failed")
            return ToolResult(ok=False, error=str(exc))

    async def list_alerts(self) -> ToolResult:
        """List active Azure Monitor Alerts via Resource Graph (alertsmanagementresources)."""
        return await self.query_resources(
            "AlertsManagementResources | where type =~ 'microsoft.alertsmanagement/alerts' "
            "| where properties.essentials.monitorCondition == 'Fired' "
            "| project name, severity=properties.essentials.severity, "
            "resource=properties.essentials.targetResource, firedTime=properties.essentials.startDateTime"
        )

    # -- write tools (SPEC 5.1: medium/high risk, gated by Approval Engine) --

    async def restart_vm(self, resource_id: str) -> ToolResult:
        """Restart an Azure VM. risk_level=medium. MUST only be invoked by
        the registry after an approved Action (or allowlist match, though
        VM restarts are not part of the default allowlist)."""
        try:
            # resource_id format: /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}
            parts = resource_id.split("/")
            rg = parts[parts.index("resourceGroups") + 1]
            vm_name = parts[-1]
            async with ComputeManagementClient(
                credential=self._get_write_credential(), subscription_id=self._settings.azure_subscription_id
            ) as client:
                poller = await client.virtual_machines.begin_restart(rg, vm_name)
                result = await poller.result()
            return ToolResult(ok=True, data={"resource_id": resource_id, "status": "restarted", "raw": str(result)})
        except (AzureError, ValueError, IndexError) as exc:
            logger.exception("azure_restart_vm failed")
            return ToolResult(ok=False, error=str(exc))

    async def scale_app_service(self, resource_id: str, tier: str) -> ToolResult:
        """Scale an App Service Plan (Web App) to a new SKU tier. risk_level=medium.

        Performs a real update via `azure.mgmt.web.aio.WebSiteManagementClient`
        using the segregated write credential. `azure-mgmt-web` is imported
        inside the method so that deployments which never scale App Service
        Plans do not pay for the import at startup; the package is listed in
        requirements.txt, so an ImportError here means a broken install rather
        than an unimplemented feature.
        """
        try:
            from azure.mgmt.web.aio import WebSiteManagementClient  # local import: optional heavy dep

            parts = resource_id.split("/")
            rg = parts[parts.index("resourceGroups") + 1]
            plan_name = parts[-1]
            async with WebSiteManagementClient(
                credential=self._get_write_credential(), subscription_id=self._settings.azure_subscription_id
            ) as client:
                plan = await client.app_service_plans.get(rg, plan_name)
                plan.sku.name = tier
                plan.sku.tier = tier
                updated = await client.app_service_plans.begin_create_or_update(rg, plan_name, plan)
                result = await updated.result()
            return ToolResult(ok=True, data={"resource_id": resource_id, "new_tier": tier, "raw": str(result)})
        except (AzureError, ImportError, ValueError, IndexError) as exc:
            logger.exception("azure_scale_app_service failed")
            return ToolResult(ok=False, error=str(exc))

    async def resize_disk(self, resource_id: str, new_size_gb: int) -> ToolResult:
        """Resize a managed disk. risk_level=high — often requires downtime.
        This tool is NEVER allowlisted; always requires explicit approval."""
        try:
            parts = resource_id.split("/")
            rg = parts[parts.index("resourceGroups") + 1]
            disk_name = parts[-1]
            async with ComputeManagementClient(
                credential=self._get_write_credential(), subscription_id=self._settings.azure_subscription_id
            ) as client:
                poller = await client.disks.begin_update(rg, disk_name, {"disk_size_gb": new_size_gb})
                result = await poller.result()
            return ToolResult(ok=True, data={"resource_id": resource_id, "new_size_gb": new_size_gb, "raw": str(result)})
        except (AzureError, ValueError, IndexError) as exc:
            logger.exception("azure_resize_disk failed")
            return ToolResult(ok=False, error=str(exc))

    # -- tool registration -----------------------------------------------

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="azure_query_resources",
                description=(
                    "Run a KQL query against Azure Resource Graph to inventory or inspect resources "
                    "(VMs, Web Apps/App Service, disks, networking, etc.)."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"kql_query": {"type": "string"}},
                    "required": ["kql_query"],
                },
                risk_level="low",
                requires_approval=False,
                is_write=False,
                handler=self._h_query_resources,
            ),
            ToolSpec(
                name="azure_list_web_apps",
                description="List every Azure Web App (App Service) in the subscription, with state and SKU.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low",
                requires_approval=False,
                is_write=False,
                handler=self._h_list_web_apps,
            ),
            ToolSpec(
                name="azure_get_metrics",
                description="Fetch Azure Monitor metrics (CPU, memory, requests, etc.) for a resource over a timespan.",
                json_schema={
                    "type": "object",
                    "properties": {
                        "resource_id": {"type": "string"},
                        "metric_names": {"type": "array", "items": {"type": "string"}},
                        "timespan": {"type": "string", "description": "ISO8601 interval, e.g. PT1H"},
                    },
                    "required": ["resource_id", "metric_names", "timespan"],
                },
                risk_level="low",
                requires_approval=False,
                is_write=False,
                handler=self._h_get_metrics,
            ),
            ToolSpec(
                name="azure_get_activity_log",
                description="Fetch the Azure Activity Log (native audit trail) for a resource over a timespan.",
                json_schema={
                    "type": "object",
                    "properties": {
                        "resource_id": {"type": "string"},
                        "timespan": {"type": "string"},
                    },
                    "required": ["resource_id", "timespan"],
                },
                risk_level="low",
                requires_approval=False,
                is_write=False,
                handler=self._h_get_activity_log,
            ),
            ToolSpec(
                name="azure_list_alerts",
                description="List currently firing Azure Monitor Alerts.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low",
                requires_approval=False,
                is_write=False,
                handler=self._h_list_alerts,
            ),
            ToolSpec(
                name="azure_restart_vm",
                description="Restart an Azure Virtual Machine. WRITE ACTION — requires approval unless allowlisted.",
                json_schema={
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}},
                    "required": ["resource_id"],
                },
                risk_level="medium",
                requires_approval=True,
                is_write=True,
                handler=self._h_restart_vm,
            ),
            ToolSpec(
                name="azure_scale_app_service",
                description="Scale an App Service Plan to a new SKU tier. WRITE ACTION — requires approval.",
                json_schema={
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}, "tier": {"type": "string"}},
                    "required": ["resource_id", "tier"],
                },
                risk_level="medium",
                requires_approval=True,
                is_write=True,
                handler=self._h_scale_app_service,
            ),
            ToolSpec(
                name="azure_resize_disk",
                description=(
                    "Resize a managed disk to a new size in GB. High risk — often requires downtime. "
                    "WRITE ACTION — always requires explicit human approval."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}, "new_size_gb": {"type": "integer"}},
                    "required": ["resource_id", "new_size_gb"],
                },
                risk_level="high",
                requires_approval=True,
                is_write=True,
                handler=self._h_resize_disk,
            ),
        ]

    # -- thin async handler adapters (registry calls handler(**kwargs)) --

    async def _h_query_resources(self, **kwargs: Any) -> ToolResult:
        return await self.query_resources(**kwargs)

    async def _h_list_web_apps(self, **kwargs: Any) -> ToolResult:
        return await self.list_web_apps()

    async def _h_get_metrics(self, **kwargs: Any) -> ToolResult:
        return await self.get_metrics(**kwargs)

    async def _h_get_activity_log(self, **kwargs: Any) -> ToolResult:
        return await self.get_activity_log(**kwargs)

    async def _h_list_alerts(self, **kwargs: Any) -> ToolResult:
        return await self.list_alerts()

    async def _h_restart_vm(self, **kwargs: Any) -> ToolResult:
        return await self.restart_vm(**kwargs)

    async def _h_scale_app_service(self, **kwargs: Any) -> ToolResult:
        return await self.scale_app_service(**kwargs)

    async def _h_resize_disk(self, **kwargs: Any) -> ToolResult:
        return await self.resize_disk(**kwargs)
