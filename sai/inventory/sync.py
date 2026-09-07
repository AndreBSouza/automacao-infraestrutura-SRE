"""Continuous discovery job (SPEC.md section 8).

Runs on a schedule (see sai/scheduler/vigia.py wiring or APScheduler job
registered in sai/api/main.py startup) and:

  1. Queries Azure Resource Graph -> upsert `inventory_items` (source='azure').
  2. Queries Azure DevOps (pipelines, repos) -> upsert (source='azure_devops').
  3. Queries Zabbix (hosts) -> upsert (source='zabbix').
  4. Queries Grafana (dashboards) -> upsert (source='grafana').
  5. For each known Linux host, collects metadata via SSH -> upsert (source='linux').
  6. For each known SQL Server database, collects size/backup info -> upsert (source='sql_server').
  7. Regenerates RAG embeddings for inventory (sai/rag/ingest.py).
  8. Diffs against the previous sync and records `audit_log` entries of
     type `inventory_drift` — this job is read-only, so it needs no
     approval, but DOES need its own error handling/retry and must alert
     if a source stays unreachable too long (SPEC 8, last paragraph).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sai.config import Settings, get_settings
from sai.connectors.azure_connector import AzureConnector
from sai.connectors.devops_connector import DevOpsConnector
from sai.connectors.grafana_connector import GrafanaConnector
from sai.connectors.linux_connector import LinuxConnector
from sai.connectors.sql_connector import SqlConnector
from sai.connectors.zabbix_connector import ZabbixConnector
from sai.db.models import AuditLog, InventoryItem
from sai.db.session import session_scope
from sai.rag.ingest import ingest_inventory_items

logger = logging.getLogger(__name__)


class SourceUnavailableError(RuntimeError):
    pass


async def _upsert_item(
    session,
    source: str,
    resource_type: str,
    external_id: str,
    name: str,
    metadata: dict[str, Any],
    tags: dict[str, Any] | None = None,
    owner_hint: str | None = None,
) -> dict[str, Any] | None:
    """Upsert one inventory_items row, returning the PREVIOUS metadata
    (or None if this is a new item) so the caller can compute a diff."""
    existing = await session.execute(
        select(InventoryItem).where(InventoryItem.source == source, InventoryItem.external_id == external_id)
    )
    existing_row = existing.scalar_one_or_none()
    previous_metadata = existing_row.metadata_ if existing_row else None

    stmt = pg_insert(InventoryItem).values(
        source=source,
        resource_type=resource_type,
        external_id=external_id,
        name=name,
        metadata=metadata,
        tags=tags,
        owner_hint=owner_hint,
        last_synced_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "external_id"],
        set_={
            "resource_type": resource_type,
            "name": name,
            "metadata": metadata,
            "tags": tags,
            "owner_hint": owner_hint,
            "last_synced_at": datetime.now(timezone.utc),
        },
    )
    await session.execute(stmt)
    return previous_metadata


async def _record_drift(session, source: str, external_id: str, previous: dict | None, current: dict) -> None:
    if previous is not None and previous != current:
        session.add(
            AuditLog(
                actor="system:inventory_sync",
                event_type="inventory_drift",
                entity_type="inventory_item",
                entity_id=None,
                payload={"source": source, "external_id": external_id, "previous": previous, "current": current},
            )
        )
    elif previous is None:
        session.add(
            AuditLog(
                actor="system:inventory_sync",
                event_type="inventory_drift",
                entity_type="inventory_item",
                entity_id=None,
                payload={"source": source, "external_id": external_id, "previous": None, "current": current, "new_item": True},
            )
        )


async def sync_azure(settings: Settings) -> None:
    connector = AzureConnector(settings)
    result = await connector.query_resources(
        "Resources | project id, name, type, resourceGroup, location, tags, properties | limit 1000"
    )
    if not result.ok:
        raise SourceUnavailableError(f"azure: {result.error}")
    rows = (result.data or {}).get("rows", [])
    async with session_scope() as session:
        for row in rows:
            metadata = {"type": row.get("type"), "resourceGroup": row.get("resourceGroup"), "location": row.get("location"), "properties": row.get("properties")}
            previous = await _upsert_item(
                session, source="azure", resource_type=str(row.get("type", "unknown")), external_id=str(row.get("id")),
                name=str(row.get("name")), metadata=metadata, tags=row.get("tags"),
            )
            await _record_drift(session, "azure", str(row.get("id")), previous, metadata)


async def sync_azure_devops(settings: Settings) -> None:
    connector = DevOpsConnector(settings)
    pipelines = await connector.list_pipelines()
    repos = await connector.list_repos()
    if not pipelines.ok:
        raise SourceUnavailableError(f"azure_devops pipelines: {pipelines.error}")
    if not repos.ok:
        raise SourceUnavailableError(f"azure_devops repos: {repos.error}")

    async with session_scope() as session:
        for p in (pipelines.data or {}).get("value", []):
            metadata = {"folder": p.get("folder"), "revision": p.get("revision")}
            previous = await _upsert_item(session, "azure_devops", "pipeline", str(p.get("id")), str(p.get("name")), metadata)
            await _record_drift(session, "azure_devops", str(p.get("id")), previous, metadata)
        for r in (repos.data or {}).get("value", []):
            metadata = {"defaultBranch": r.get("defaultBranch"), "size": r.get("size")}
            previous = await _upsert_item(session, "azure_devops", "repo", str(r.get("id")), str(r.get("name")), metadata)
            await _record_drift(session, "azure_devops", str(r.get("id")), previous, metadata)


async def sync_zabbix(settings: Settings) -> None:
    connector = ZabbixConnector(settings)
    try:
        hosts = await connector._rpc("host.get", {"output": "extend", "selectGroups": "extend"})  # internal call, read-only inventory sync
    except Exception as exc:  # noqa: BLE001
        raise SourceUnavailableError(f"zabbix: {exc}") from exc

    async with session_scope() as session:
        for h in hosts:
            metadata = {"status": h.get("status"), "groups": h.get("groups")}
            previous = await _upsert_item(session, "zabbix", "host", str(h["hostid"]), str(h.get("host")), metadata)
            await _record_drift(session, "zabbix", str(h["hostid"]), previous, metadata)


async def sync_grafana(settings: Settings) -> None:
    import httpx

    headers = {"Authorization": f"Bearer {settings.grafana_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{settings.grafana_base_url}/api/search", headers=headers, params={"type": "dash-db"})
            resp.raise_for_status()
            dashboards = resp.json()
    except httpx.HTTPError as exc:
        raise SourceUnavailableError(f"grafana: {exc}") from exc

    async with session_scope() as session:
        for d in dashboards:
            metadata = {"folderId": d.get("folderId"), "tags": d.get("tags")}
            previous = await _upsert_item(session, "grafana", "dashboard", str(d.get("uid")), str(d.get("title")), metadata)
            await _record_drift(session, "grafana", str(d.get("uid")), previous, metadata)


async def sync_linux_hosts(settings: Settings) -> None:
    connector = LinuxConnector(settings)
    async with session_scope() as session:
        for host in settings.known_linux_hosts_list:
            result = await connector.get_system_metrics(host)
            if not result.ok:
                logger.warning("linux sync: host %s unreachable: %s", host, result.error)
                continue
            metadata = {"raw_metrics": result.data.get("raw")}
            previous = await _upsert_item(session, "linux", "host", host, host, metadata)
            await _record_drift(session, "linux", host, previous, metadata)


async def sync_sql_instances(settings: Settings) -> None:
    connector = SqlConnector(settings)
    async with session_scope() as session:
        for database in settings.known_sql_databases_list:
            size_result = await connector.get_db_size_and_growth(database)
            backup_result = await connector.get_last_backup_info(database)
            if not size_result.ok:
                logger.warning("sql sync: database %s unreachable: %s", database, size_result.error)
                continue
            metadata = {"files": size_result.data.get("files"), "backups": backup_result.data.get("backups") if backup_result.ok else None}
            previous = await _upsert_item(session, "sql_server", "sql_database", database, database, metadata)
            await _record_drift(session, "sql_server", database, previous, metadata)


async def run_full_sync() -> dict[str, str]:
    """Runs every source's sync with independent error handling — one
    source failing never blocks the others (SPEC 8: "deve ter seu próprio
    tratamento de erro/retry e alertar se uma fonte ficar inacessível")."""
    settings = get_settings()
    sources = {
        "azure": sync_azure,
        "azure_devops": sync_azure_devops,
        "zabbix": sync_zabbix,
        "grafana": sync_grafana,
        "linux": sync_linux_hosts,
        "sql_server": sync_sql_instances,
    }
    results: dict[str, str] = {}
    for name, fn in sources.items():
        try:
            await fn(settings)
            results[name] = "ok"
        except SourceUnavailableError as exc:
            logger.error("inventory source unavailable: %s", exc)
            results[name] = f"unavailable: {exc}"
            async with session_scope() as session:
                session.add(
                    AuditLog(
                        actor="system:inventory_sync",
                        event_type="inventory_source_unavailable",
                        entity_type="inventory_source",
                        entity_id=None,
                        payload={"source": name, "error": str(exc)},
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error syncing %s", name)
            results[name] = "error"

    try:
        n = await ingest_inventory_items(settings)
        logger.info("re-embedded %d inventory chunks for RAG", n)
    except Exception:  # noqa: BLE001
        logger.exception("failed to re-embed inventory for RAG")

    return results
