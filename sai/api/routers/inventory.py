"""Inventory endpoints (SPEC.md section 8)."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.routers.auth import get_current_user, require_role
from sai.db.models import InventoryItem, User
from sai.db.session import get_db_session
from sai.inventory.sync import run_full_sync

router = APIRouter(prefix="/inventory", tags=["inventory"])


class InventoryItemOut(BaseModel):
    id: uuid.UUID
    source: str
    resource_type: str
    external_id: str
    name: str
    metadata_: dict
    tags: dict | None
    owner_hint: str | None

    class Config:
        from_attributes = True
        populate_by_name = True


@router.get("", response_model=list[InventoryItemOut])
async def list_inventory(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    source: str | None = None,
    resource_type: str | None = None,
    q: str | None = None,
) -> list[InventoryItem]:
    stmt = select(InventoryItem)
    if source:
        stmt = stmt.where(InventoryItem.source == source)
    if resource_type:
        stmt = stmt.where(InventoryItem.resource_type == resource_type)
    if q:
        stmt = stmt.where(InventoryItem.name.ilike(f"%{q}%"))
    result = await session.execute(stmt.order_by(InventoryItem.last_synced_at.desc()).limit(500))
    return list(result.scalars().all())


@router.post("/sync")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(require_role("operator"))],
) -> dict[str, str]:
    """Manually triggers the discovery job (SPEC 8). Runs in the background
    since a full sync across all sources can take longer than an HTTP
    request budget."""
    background_tasks.add_task(run_full_sync)
    return {"status": "sync_started"}
