"""Teams/Slack interactive callback endpoints (SPEC.md section 7.2) — lets
an operator approve/reject directly from the Adaptive Card / Block Kit
button, without opening the web app.

SECURITY MODEL (both handlers, no exceptions):

1. **Authenticity** — the request must be provably from the platform:
   Teams via the Bot Framework JWT (`Authorization: Bearer`), Slack via the
   HMAC signature. Neither check can be disabled by configuration; if the
   corresponding secret is absent the endpoint refuses every request rather
   than accepting unverified ones.
2. **Attribution** — the *person* who clicked is resolved from the payload
   (`from.aadObjectId` for Teams, `user.id` for Slack) and mapped onto a
   `users` row. An unmapped identity is refused.
3. **Authorization** — RBAC is then enforced by ApprovalEngine.approve_action
   against that real user's role, exactly as it is for the web app.

Together these ensure an approval recorded in the audit log always names the
human who actually made the decision. Approving a `critical` action still
requires an `admin`, whatever channel it arrives through.
"""
from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.deps import get_approval_engine
from sai.approval.engine import ApprovalEngine, InsufficientRoleError, InvalidActionStateError
from sai.config import Settings, get_settings
from sai.db.models import User
from sai.db.session import get_db_session
from sai.notifications import slack, teams

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _user_by_entra_object_id(session: AsyncSession, aad_object_id: str) -> User:
    """Maps a Teams `from.aadObjectId` onto a provisioned SAI user.

    Fails closed: an identity with no matching row cannot approve anything,
    even if the request itself is a cryptographically valid Bot Framework call.
    """
    result = await session.execute(select(User).where(User.entra_object_id == aad_object_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the Teams user who clicked is not provisioned in SAI; approval refused",
        )
    return user


async def _user_by_slack_id(session: AsyncSession, slack_user_id: str) -> User:
    """Maps a Slack `user.id` onto a provisioned SAI user. Fails closed."""
    result = await session.execute(select(User).where(User.slack_user_id == slack_user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the Slack user who clicked is not linked to a SAI account; approval refused",
        )
    return user


async def _apply_decision(
    engine: ApprovalEngine, action_id: uuid.UUID, decision: str, user: User, channel: str
) -> None:
    """Routes an authenticated, attributed decision into the ApprovalEngine.

    RBAC and state-machine violations are surfaced as 403/409 rather than
    500s, and are never swallowed into a silent success.
    """
    try:
        if decision == "approve":
            await engine.approve_action(action_id, user)
        else:
            await engine.reject_action(action_id, user, reason=f"rejected via {channel}")
    except InsufficientRoleError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except InvalidActionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/teams")
async def teams_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    # 1. Authenticity — unconditional; no config flag can bypass this.
    try:
        await teams.verify_bot_framework_token(settings, authorization)
    except teams.TeamsAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    payload = await request.json()
    # 2. Attribution — payload must name the human who clicked.
    try:
        decision = await teams.handle_teams_action_callback(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = await _user_by_entra_object_id(session, decision["aad_object_id"])
    # 3. Authorization — RBAC enforced inside the engine against this user.
    await _apply_decision(engine, uuid.UUID(decision["action_id"]), decision["decision"], user, "Teams")
    return {"status": "ok"}


@router.post("/slack")
async def slack_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_slack_signature: Annotated[str | None, Header()] = None,
    x_slack_request_timestamp: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    raw_body = await request.body()

    # 1. Authenticity — fail closed when the signing secret is absent, rather
    # than skipping verification (the previous behaviour, which let anyone who
    # could reach this URL approve an action).
    if not settings.slack_signing_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SLACK_SIGNING_SECRET is not configured; Slack approvals are disabled",
        )
    if not (x_slack_signature and x_slack_request_timestamp):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing Slack signature headers")
    if not slack.verify_slack_signature(settings, x_slack_request_timestamp, raw_body, x_slack_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Slack signature")

    form = await request.form()
    raw_payload = form.get("payload")
    if not raw_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing 'payload' field")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payload is not valid JSON") from exc

    # 2. Attribution.
    try:
        decision = slack.handle_slack_interaction_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = await _user_by_slack_id(session, decision["slack_user_id"])
    # 3. Authorization.
    await _apply_decision(engine, uuid.UUID(decision["action_id"]), decision["decision"], user, "Slack")
    return {"status": "ok"}
