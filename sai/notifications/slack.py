"""Slack notifier (SPEC.md section 7.2) — Block Kit message with inline
Approve/Reject buttons, sent via the Slack Web API (`chat.postMessage`),
plus a callback handler for Slack's `block_actions` interactivity payload
(invoked from sai/api/routers/webhooks.py, which a full Slack Bolt app or
a raw interactivity-request-URL handler would route to).

This module makes real HTTP calls to Slack's Web API (functional once
SLACK_BOT_TOKEN is configured); request signature verification uses
SLACK_SIGNING_SECRET per Slack's documented HMAC scheme.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Any

import httpx

from sai.config import Settings

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def build_approval_blocks(
    action_id: uuid.UUID,
    tool_name: str,
    risk_level: str,
    proposed_description: str,
    expected_impact: str = "",
    rollback_plan: str = "",
) -> list[dict[str, Any]]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "SAI — Action pending approval"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Tool:* `{tool_name}`\n*Risk level:* `{risk_level.upper()}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": proposed_description}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Expected impact:* {expected_impact}"}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Rollback plan:* {rollback_plan}"}]},
        {
            "type": "actions",
            "block_id": f"sai_action_{action_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "sai_approve",
                    "value": str(action_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "sai_reject",
                    "value": str(action_id),
                },
            ],
        },
    ]


async def send_approval_message(
    settings: Settings, channel: str, action_id: uuid.UUID, tool_name: str, risk_level: str, proposed_description: str, **kwargs: Any
) -> bool:
    if not settings.slack_bot_token:
        logger.info("SLACK_BOT_TOKEN not configured; skipping Slack notification for action %s", action_id)
        return False

    blocks = build_approval_blocks(action_id, tool_name, risk_level, proposed_description, **kwargs)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                json={"channel": channel, "text": "SAI action pending approval", "blocks": blocks},
            )
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                logger.error("slack chat.postMessage failed: %s", body)
                return False
        return True
    except httpx.HTTPError:
        logger.exception("failed to send Slack approval message for action %s", action_id)
        return False


def verify_slack_signature(settings: Settings, timestamp: str, body: bytes, signature: str) -> bool:
    """Verifies Slack's `X-Slack-Signature` per Slack's documented HMAC-SHA256
    scheme. Rejects requests older than 5 minutes to mitigate replay."""
    if abs(time.time() - float(timestamp)) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode()
    computed = "v0=" + hmac.new(settings.slack_signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def handle_slack_interaction_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Parses a Slack `block_actions` interactivity payload into a normalized
    dict.

    Returns `action_id`, `decision` AND `slack_user_id` — the id of the person
    who actually clicked, from the payload's `user` field. The caller maps it
    onto a `users.slack_user_id` row so the approval is attributed to a real,
    specific person; a payload without it is rejected rather than accepted
    anonymously.
    """
    actions = payload.get("actions", [])
    if not actions:
        raise ValueError("no actions in Slack interactivity payload")
    clicked = actions[0]
    action_id = clicked.get("value")
    decision = "approve" if clicked.get("action_id") == "sai_approve" else "reject"
    if not action_id:
        raise ValueError(f"malformed Slack interactivity payload: {payload}")

    slack_user_id = (payload.get("user") or {}).get("id")
    if not slack_user_id:
        raise ValueError(
            "Slack interactivity payload is missing user.id; cannot attribute the approval to a user"
        )
    return {"action_id": action_id, "decision": decision, "slack_user_id": slack_user_id}
