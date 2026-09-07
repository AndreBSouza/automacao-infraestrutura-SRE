"""Alerts endpoint (SPEC.md section 9)."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.routers.auth import get_current_user
from sai.db.models import Alert, User
from sai.db.session import get_db_session

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    id: uuid.UUID
    source: str
    severity: str
    summary: str
    diagnosis: str | None
    status: str

    class Config:
        from_attributes = True


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    status_filter: str | None = None,
) -> list[Alert]:
    stmt = select(Alert)
    if status_filter:
        stmt = stmt.where(Alert.status == status_filter)
    result = await session.execute(stmt.order_by(Alert.created_at.desc()).limit(200))
    return list(result.scalars().all())
