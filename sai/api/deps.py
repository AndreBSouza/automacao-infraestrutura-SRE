"""Shared FastAPI dependencies: process-wide ToolRegistry/Allowlist
singletons, and a factory for a request-scoped ApprovalEngine bound to the
request's DB session.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sai.approval.allowlist import Allowlist
from sai.approval.engine import ApprovalEngine
from sai.config import Settings, get_settings
from sai.connectors.registry import ToolRegistry
from sai.db.session import get_db_session
from sai.notifications.teams import send_approval_card


@lru_cache
def get_registry() -> ToolRegistry:
    return ToolRegistry(get_settings())


@lru_cache
def get_allowlist() -> Allowlist:
    return Allowlist.load(get_settings().allowlist_path)


async def _notify_teams(action) -> None:
    settings = get_settings()
    await send_approval_card(
        settings,
        action.id,
        action.tool_name,
        action.risk_level,
        action.proposed_description,
    )


def get_approval_engine(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    allowlist: Annotated[Allowlist, Depends(get_allowlist)],
) -> ApprovalEngine:
    return ApprovalEngine(session=session, registry=registry, allowlist=allowlist, settings=settings, notify=_notify_teams)
