"""Scheduler / vigia proativo (SPEC.md section 9).

- Only runs within the configured business-hours window (gated purely by
  wall-clock/timezone — no API calls wasted outside of it).
- Every `VIGIA_POLL_INTERVAL_SECONDS`, checks simple, code-defined
  thresholds against Zabbix / Azure Monitor (no LLM involved in the
  threshold check itself — SPEC 9: "sem IA").
- On a breach: creates an `alerts` row (status='open'), and ONLY THEN
  invokes the LLM orchestrator to diagnose + optionally propose an action
  (through the normal `propose_action` -> Approval Engine path — the
  scheduler never bypasses governance).
- Deduplicates against already-open alerts for the same source+resource
  +metric so a persistent condition doesn't spam notifications or burn API
  budget (SPEC 9's dedup requirement).
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from sai.approval.allowlist import Allowlist
from sai.approval.engine import ApprovalEngine
from sai.config import Settings, get_settings
from sai.connectors.azure_connector import AzureConnector
from sai.connectors.registry import ToolRegistry
from sai.connectors.zabbix_connector import ZabbixConnector
from sai.db.models import Alert
from sai.db.session import session_scope
from sai.llm.orchestrator import Orchestrator
from sai.rag.retrieve import retrieve_context

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class ThresholdBreach:
    source: str
    resource: str
    metric: str
    severity: str  # 'info' | 'warning' | 'critical'
    summary: str
    raw_payload: dict[str, Any]


def is_within_business_hours(settings: Settings, now: datetime | None = None) -> bool:
    tz = ZoneInfo(settings.business_hours.timezone)
    local_now = (now or datetime.now(tz)).astimezone(tz)
    if local_now.weekday() not in settings.business_hours.weekday_set:
        return False
    return settings.business_hours.start_hour <= local_now.hour < settings.business_hours.end_hour


async def _check_zabbix_thresholds(settings: Settings) -> list[ThresholdBreach]:
    """Pure threshold check, no LLM: any Zabbix problem at severity >= 3
    (Average/High/Disaster per Zabbix's own severity scale) is a breach."""
    connector = ZabbixConnector(settings)
    result = await connector.get_problems(severity_min=3)
    if not result.ok:
        logger.warning("vigia: zabbix threshold check failed: %s", result.error)
        return []

    breaches = []
    severity_map = {"3": "warning", "4": "critical", "5": "critical"}
    for problem in (result.data or {}).get("problems", []):
        breaches.append(
            ThresholdBreach(
                source="zabbix",
                resource=str(problem.get("objectid", problem.get("eventid"))),
                metric=str(problem.get("name")),
                severity=severity_map.get(str(problem.get("severity")), "warning"),
                summary=f"Zabbix problem: {problem.get('name')}",
                raw_payload=problem,
            )
        )
    return breaches


async def _check_azure_alerts(settings: Settings) -> list[ThresholdBreach]:
    connector = AzureConnector(settings)
    result = await connector.list_alerts()
    if not result.ok:
        logger.warning("vigia: azure threshold check failed: %s", result.error)
        return []

    breaches = []
    for row in (result.data or {}).get("rows", []):
        severity_raw = str(row.get("severity", "")).lower()
        severity = "critical" if "sev0" in severity_raw or "sev1" in severity_raw else "warning"
        breaches.append(
            ThresholdBreach(
                source="azure",
                resource=str(row.get("resource")),
                metric=str(row.get("name")),
                severity=severity,
                summary=f"Azure alert fired: {row.get('name')}",
                raw_payload=row,
            )
        )
    return breaches


async def _is_duplicate(session, breach: ThresholdBreach) -> bool:
    result = await session.execute(
        select(Alert).where(
            Alert.source == breach.source,
            Alert.status.in_(["open", "diagnosing", "awaiting_approval"]),
        )
    )
    for existing in result.scalars().all():
        if existing.raw_payload.get("_resource") == breach.resource and existing.summary == breach.summary:
            return True
    return False


async def _diagnose_and_maybe_propose(
    breach: ThresholdBreach, alert_id, registry: ToolRegistry, allowlist: Allowlist, settings: Settings, notify: Callable[[Any], Awaitable[None]] | None
) -> str:
    """Runs the LLM diagnosis for a breach, inside its own DB session/approval
    engine instance (system actor). Any `propose_action` the model emits
    still goes through the normal Approval Engine gate."""
    async with session_scope() as session:
        engine = ApprovalEngine(session=session, registry=registry, allowlist=allowlist, settings=settings, notify=notify)
        # Triagem de alerta roda no modelo do vigia (ANTHROPIC_VIGIA_MODEL),
        # separado do modelo do chat.
        orchestrator = Orchestrator(
            settings=settings,
            registry=registry,
            approval_engine=engine,
            retrieve_context_fn=retrieve_context,
            model=settings.anthropic_vigia_model,
        )

        prompt = (
            f"A monitoring threshold was breached.\nSource: {breach.source}\nResource: {breach.resource}\n"
            f"Metric: {breach.metric}\nSeverity: {breach.severity}\nRaw payload: {breach.raw_payload}\n\n"
            "Investigate using the available read tools, determine the likely root cause, and if a "
            "corrective write action is warranted, file it via propose_action. Otherwise, summarize your "
            "diagnosis."
        )

        messages: list[dict] = []
        final_text = ""
        # Whether the model actually filed a proposal is decided by observing
        # the event stream, not by string-matching the message history: the
        # history records the tool *call* (`propose_action`), never the
        # `action_proposed` event name, so matching on it would never fire and
        # every alert would be left stuck in 'diagnosing'.
        proposed_action = False
        async for event in orchestrator.run_turn(messages, prompt, actor="system:vigia", conversation_id=None):
            if event["type"] == "final_text":
                final_text = event["text"]
            elif event["type"] == "action_proposed":
                proposed_action = True

        alert = await session.get(Alert, alert_id)
        if alert is not None:
            alert.diagnosis = final_text
            # An alert with no corrective action pending is diagnosed and left
            # open for a human to read, not silently closed.
            alert.status = "awaiting_approval" if proposed_action else "open"
            await session.flush()

        return final_text


async def poll_once(
    registry: ToolRegistry,
    allowlist: Allowlist,
    settings: Settings | None = None,
    notify: Callable[[Any], Awaitable[None]] | None = None,
) -> list[str]:
    """One vigia tick. Returns a list of alert IDs created this tick."""
    settings = settings or get_settings()
    if not is_within_business_hours(settings):
        logger.debug("vigia: outside business hours, skipping tick")
        return []

    breaches = await _check_zabbix_thresholds(settings) + await _check_azure_alerts(settings)
    created_alert_ids: list[str] = []

    for breach in breaches:
        async with session_scope() as session:
            if await _is_duplicate(session, breach):
                continue

            alert = Alert(
                source=breach.source,
                severity=breach.severity,
                summary=breach.summary,
                raw_payload={**breach.raw_payload, "_resource": breach.resource},
                status="diagnosing",
            )
            session.add(alert)
            await session.flush()
            alert_id = alert.id

        created_alert_ids.append(str(alert_id))
        try:
            await _diagnose_and_maybe_propose(breach, alert_id, registry, allowlist, settings, notify)
        except Exception:  # noqa: BLE001 - a diagnosis failure must not crash the scheduler loop
            logger.exception("vigia: diagnosis failed for alert %s", alert_id)

    return created_alert_ids


def start_scheduler(
    registry: ToolRegistry,
    allowlist: Allowlist,
    settings: Settings | None = None,
    notify: Callable[[Any], Awaitable[None]] | None = None,
) -> AsyncIOScheduler:
    """Registers and starts the periodic vigia job plus the inventory-sync
    job (SPEC 8) on the same AsyncIOScheduler. Returns the scheduler so the
    caller (sai/api/main.py) can shut it down on app shutdown."""
    from sai.inventory.sync import run_full_sync

    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.business_hours.timezone)

    scheduler.add_job(
        poll_once,
        "interval",
        seconds=settings.vigia_poll_interval_seconds,
        kwargs={"registry": registry, "allowlist": allowlist, "settings": settings, "notify": notify},
        id="vigia_poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_full_sync,
        "interval",
        hours=settings.inventory_sync_interval_hours,
        id="inventory_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
