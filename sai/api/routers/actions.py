"""Actions endpoints — the human side of the Approval Engine (SPEC.md
section 7). Approve/reject require role >= the action's risk-level minimum,
enforced both here (fast 403) and again inside ApprovalEngine (defense in
depth — SPEC 10.4).
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.deps import get_approval_engine
from sai.api.routers.auth import get_current_user
from sai.approval.engine import (
    ActionNotFoundError,
    ApprovalEngine,
    InsufficientRoleError,
    InvalidActionStateError,
)
from sai.db.models import Action, User
from sai.db.session import get_db_session

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionOut(BaseModel):
    id: uuid.UUID
    tool_name: str
    risk_level: str
    parameters: dict
    proposed_description: str
    status: str
    requires_approval: bool

    class Config:
        from_attributes = True


class RejectRequest(BaseModel):
    reason: str | None = None


@router.get("", response_model=list[ActionOut])
async def list_pending_actions(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[Action]:
    result = await session.execute(select(Action).where(Action.status == "proposed").order_by(Action.created_at.desc()))
    return list(result.scalars().all())


@router.post("/{action_id}/approve", response_model=ActionOut)
async def approve_action(
    action_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
) -> Action:
    try:
        return await engine.approve_action(action_id, user)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InsufficientRoleError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{action_id}/reject", response_model=ActionOut)
async def reject_action(
    action_id: uuid.UUID,
    body: RejectRequest,
    user: Annotated[User, Depends(get_current_user)],
    engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
) -> Action:
    try:
        return await engine.reject_action(action_id, user, reason=body.reason)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
