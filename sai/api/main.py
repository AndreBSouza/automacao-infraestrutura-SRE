"""FastAPI application entrypoint (SPEC.md section 2).

Run with: `uvicorn sai.api.main:app --reload` (see README.md).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from sai.api.deps import get_allowlist, get_registry
from sai.api.routers import actions, alerts, auth, chat, inventory, reports, webhooks
from sai.config import get_settings
from sai.db.session import dispose_engine, session_scope
from sai.scheduler.vigia import start_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = get_registry()
    allowlist = get_allowlist()

    scheduler = start_scheduler(registry, allowlist, settings)
    app.state.scheduler = scheduler
    logger.info("SAI started (env=%s); scheduler running", settings.app_env)

    yield

    scheduler.shutdown(wait=False)
    await dispose_engine()
    logger.info("SAI shutdown complete")


app = FastAPI(title="SAI — Infrastructure Copilot", version="1.0.0", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_allowed_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(actions.router)
app.include_router(inventory.router)
app.include_router(alerts.router)
app.include_router(reports.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health(response: Response) -> dict:
    """Deep health check (SPEC 12): connectivity with every configured
    connector, for dashboards and humans.

    Deliberately NOT the endpoint used by the container's readiness probe —
    see `/health/ready`. This one talks to external systems, so it is slower
    and its result depends on third parties being up.

    Returns 200 while the app itself is serving, even when a connector is
    down: read `status` for the distinction. `degraded` means some configured
    system is unreachable — the app still works, minus that system's tools.
    Connectors that this deployment doesn't use report `not_configured` and
    do not count as failures.
    """
    registry = get_registry()
    results = await registry.healthcheck_all()
    failed = [name for name, state in results.items() if state in ("error", "timeout")]
    if failed:
        response.headers["X-Degraded-Connectors"] = ",".join(sorted(failed))
    return {
        "status": "degraded" if failed else "ok",
        "connectors": results,
        "degraded": sorted(failed),
    }


@app.get("/health/ready")
async def readiness(response: Response) -> dict:
    """Readiness/liveness probe: can THIS instance serve requests?

    Checks only what the app cannot work without — the database. External
    systems are deliberately excluded: if readiness depended on Zabbix or the
    F5 being reachable, a third-party outage would take SAI's own instances
    out of rotation, turning someone else's incident into ours precisely when
    the operators need the tool most.
    """
    try:
        async with asyncio.timeout(3):
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - probe must answer, not raise
        logger.warning("readiness probe failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "database unreachable"}
    return {"status": "ready"}
