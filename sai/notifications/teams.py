"""Microsoft Teams notifier (SPEC.md section 7.2) — Adaptive Card with
inline Approve/Reject buttons, delivered via an Incoming Webhook, plus a
callback handler stub for the Bot Framework path (invoked from
sai/api/routers/webhooks.py) that a full Bot Framework `CloudAdapter`
integration would call into.

This module implements the real HTTP call to a Teams Incoming Webhook
(functional today, once TEAMS_WEBHOOK_URL is configured) and documents the
Adaptive Card JSON contract used for the approve/reject affordance. The
full Bot Framework SDK bot registration (app manifest, `CloudAdapter`,
Azure Bot resource) is environment-specific infra setup that lives outside
this codebase — `handle_teams_action_callback` is the real, functional
piece that would be wired to that bot's messaging endpoint.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from sai.config import Settings

logger = logging.getLogger(__name__)


def build_approval_adaptive_card(
    action_id: uuid.UUID,
    tool_name: str,
    risk_level: str,
    proposed_description: str,
    expected_impact: str = "",
    rollback_plan: str = "",
) -> dict[str, Any]:
    """Builds an Adaptive Card (schema 1.5) with Approve/Reject Action.Submit
    buttons. `data` on each action carries the action_id + decision, which
    `sai/api/routers/webhooks.py::teams_webhook` reads back when Teams posts
    the user's click."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {"type": "TextBlock", "text": "SAI — Action pending approval", "weight": "Bolder", "size": "Medium"},
                        {"type": "TextBlock", "text": f"Tool: {tool_name}", "wrap": True},
                        {"type": "TextBlock", "text": f"Risk level: {risk_level.upper()}", "wrap": True, "color": "Attention" if risk_level in ("high", "critical") else "Default"},
                        {"type": "TextBlock", "text": proposed_description, "wrap": True},
                        {"type": "TextBlock", "text": f"Expected impact: {expected_impact}", "wrap": True, "isSubtle": True},
                        {"type": "TextBlock", "text": f"Rollback plan: {rollback_plan}", "wrap": True, "isSubtle": True},
                    ],
                    "actions": [
                        {"type": "Action.Submit", "title": "Approve", "data": {"action_id": str(action_id), "decision": "approve"}},
                        {"type": "Action.Submit", "title": "Reject", "data": {"action_id": str(action_id), "decision": "reject"}},
                    ],
                },
            }
        ],
    }


async def send_approval_card(settings: Settings, action_id: uuid.UUID, tool_name: str, risk_level: str, proposed_description: str, **kwargs: Any) -> bool:
    """POSTs the Adaptive Card to the configured Teams Incoming Webhook."""
    if not settings.teams_webhook_url:
        logger.info("TEAMS_WEBHOOK_URL not configured; skipping Teams notification for action %s", action_id)
        return False

    card = build_approval_adaptive_card(action_id, tool_name, risk_level, proposed_description, **kwargs)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(settings.teams_webhook_url, json=card)
            resp.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("failed to send Teams approval card for action %s", action_id)
        return False


class TeamsAuthError(RuntimeError):
    """Raised when a Teams callback cannot be cryptographically attributed to
    the configured bot. The caller MUST refuse the approval — never fall back
    to an unauthenticated path."""


# Bot Framework's OpenID metadata for tokens sent from the Bot Connector to a
# bot's messaging endpoint. https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
BOT_FRAMEWORK_OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"

_jwks_client: Any | None = None


async def _get_jwks_client() -> Any:
    """Lazily builds (and caches) a PyJWKClient pointed at the Bot Framework
    signing keys. PyJWKClient caches keys internally and refreshes on
    unknown-kid, so this is safe to reuse across requests."""
    global _jwks_client
    if _jwks_client is None:
        import jwt as pyjwt

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BOT_FRAMEWORK_OPENID_CONFIG)
            resp.raise_for_status()
            jwks_uri = resp.json()["jwks_uri"]
        _jwks_client = pyjwt.PyJWKClient(jwks_uri)
    return _jwks_client


async def verify_bot_framework_token(settings: Settings, authorization_header: str | None) -> dict[str, Any]:
    """Verifies the `Authorization: Bearer <jwt>` header that the Bot Framework
    Connector attaches to every call into a bot's messaging endpoint.

    Fails closed in every branch: a missing bot app id configuration, a missing
    header, a bad signature, a wrong issuer/audience, or an expired token all
    raise TeamsAuthError. There is deliberately no "verification disabled"
    mode — an unauthenticated request must never be able to approve an action.
    """
    if not settings.teams_bot_app_id:
        raise TeamsAuthError(
            "TEAMS_BOT_APP_ID is not configured; refusing to accept Teams approval callbacks. "
            "Approvals via Teams are disabled until the bot is registered."
        )
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise TeamsAuthError("missing or malformed Authorization header on Teams callback")

    token = authorization_header.split(" ", 1)[1].strip()
    import jwt as pyjwt

    try:
        jwks_client = await _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.teams_bot_app_id,
            issuer=BOT_FRAMEWORK_ISSUER,
        )
    except Exception as exc:  # noqa: BLE001 - any validation failure is a hard refusal
        raise TeamsAuthError(f"Teams callback token failed validation: {exc}") from exc
    return claims


async def handle_teams_action_callback(payload: dict[str, Any]) -> dict[str, str]:
    """Parses an Adaptive Card `Action.Submit` callback body from Teams into a
    normalized dict.

    Returns `action_id`, `decision` AND `aad_object_id` — the Entra object id
    of the person who actually clicked the button, taken from the activity's
    `from` field. The caller (sai/api/routers/webhooks.py) maps that onto a
    `users.entra_object_id` row so the approval is attributed to a real,
    specific person. A payload without it is rejected rather than accepted
    anonymously.
    """
    value = payload.get("value", payload)
    action_id = value.get("action_id")
    decision = value.get("decision")
    if not action_id or decision not in ("approve", "reject"):
        raise ValueError(f"malformed Teams callback payload: {payload}")

    aad_object_id = (payload.get("from") or {}).get("aadObjectId")
    if not aad_object_id:
        raise ValueError(
            "Teams callback is missing from.aadObjectId; cannot attribute the approval to a user"
        )
    return {"action_id": action_id, "decision": decision, "aad_object_id": aad_object_id}
