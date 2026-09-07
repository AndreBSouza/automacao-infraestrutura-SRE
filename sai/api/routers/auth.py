"""Entra ID (Azure AD) OIDC login flow + app session JWT issuance, and the
`get_current_user` dependency enforcing RBAC (SPEC.md sections 3, 10.4, 10.5).

Flow:
  GET  /auth/login    -> redirects to Entra ID's authorize endpoint (MSAL)
  GET  /auth/callback -> exchanges the auth code for tokens via MSAL,
                         upserts a `users` row keyed by `entra_object_id`,
                         issues a short-lived app JWT (HS256, signed with
                         APP_SECRET_KEY) carrying {sub, role, email}.

MFA (SPEC 10.5) is enforced by Entra ID's own Conditional Access policies
at the tenant level — this app only ever sees the resulting id_token; it
does not implement MFA itself.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
import msal
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.config import Settings, get_settings
from sai.db.models import AuditLog, User
from sai.db.session import get_db_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)

JWT_ALGORITHM = "HS256"
JWT_TTL_MINUTES = 60


def _msal_app(settings: Settings) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=settings.entra_client_id,
        client_credential=settings.entra_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.entra_tenant_id}",
    )


@router.get("/login")
async def login(settings: Annotated[Settings, Depends(get_settings)]) -> RedirectResponse:
    app_ = _msal_app(settings)
    auth_url = app_.get_authorization_request_url(
        scopes=["User.Read"],
        redirect_uri=settings.entra_redirect_uri,
    )
    return RedirectResponse(auth_url)


# response_model=None: this endpoint returns either a redirect (SPA login) or
# a JSON token (API/CLI login), and FastAPI cannot derive a response model
# from that union.
@router.get("/callback", response_model=None)
async def callback(
    code: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str] | RedirectResponse:
    app_ = _msal_app(settings)
    result = app_.acquire_token_by_authorization_code(
        code=code, scopes=["User.Read"], redirect_uri=settings.entra_redirect_uri
    )
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.get("error_description", "OIDC exchange failed"))

    id_token_claims = result.get("id_token_claims", {})
    entra_object_id = id_token_claims.get("oid")
    email = id_token_claims.get("preferred_username") or id_token_claims.get("email")
    display_name = id_token_claims.get("name", email or "unknown")

    if not entra_object_id or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ID token missing required claims")

    existing = await session.execute(select(User).where(User.entra_object_id == entra_object_id))
    user = existing.scalar_one_or_none()
    if user is None:
        # New users default to 'viewer' — role upgrades are an admin action,
        # never self-service (SPEC 10.4).
        #
        # The one exception is bootstrapping: an empty `users` table has no
        # admin to grant the first one, so with BOOTSTRAP_FIRST_ADMIN enabled
        # the first login becomes admin. Both conditions are required, and the
        # emptiness check makes it self-limiting — the second user in is a
        # 'viewer' whether or not the flag is still set.
        role = "viewer"
        if settings.bootstrap_first_admin:
            user_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            if user_count == 0:
                role = "admin"

        user = User(email=email, display_name=display_name, role=role, entra_object_id=entra_object_id)
        session.add(user)
        await session.flush()

        if role == "admin":
            logger.warning(
                "BOOTSTRAP: %s became the first admin via BOOTSTRAP_FIRST_ADMIN. "
                "Disable that setting now.",
                email,
            )
            session.add(
                AuditLog(
                    actor="system:bootstrap",
                    event_type="user_created",
                    entity_type="user",
                    entity_id=user.id,
                    payload={
                        "email": email,
                        "role": "admin",
                        "reason": "BOOTSTRAP_FIRST_ADMIN on an empty users table",
                    },
                )
            )
        await session.commit()
        await session.refresh(user)

    token = _issue_app_jwt(settings, user)
    if settings.frontend_url:
        # Fragment, not query string: fragments are not sent to servers, so the
        # token never lands in access logs, proxy logs or Referer headers.
        return RedirectResponse(
            url=f"{settings.frontend_url.rstrip('/')}/#access_token={token}",
            status_code=status.HTTP_302_FOUND,
        )
    return {"access_token": token, "token_type": "bearer"}


def _issue_app_jwt(settings: Settings, user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_TTL_MINUTES),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


async def get_current_user(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """FastAPI dependency enforcing authentication; returns the current
    `User` ORM row. Use `require_role()` on top of this for RBAC checks."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return user


ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


def require_role(min_role: str):
    """Dependency factory: `Depends(require_role("operator"))` etc."""

    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"requires role >= '{min_role}'")
        return user

    return _check
