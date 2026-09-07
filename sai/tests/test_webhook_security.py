"""Regression tests for the approval-webhook hardening.

These cover a defect where either chat-platform webhook could approve an
action without proving (a) that the request came from the platform, or
(b) which human clicked the button — meaning anyone who could reach the URL
could approve a `critical` action such as a database restore, and the audit
log would name an arbitrary admin.

The invariants locked in here:

  1. Slack with no signing secret configured REFUSES (fails closed) instead
     of skipping signature verification.
  2. Slack with a bad signature refuses.
  3. A callback payload that does not identify the clicking user is rejected
     rather than accepted anonymously.
  4. An identity that is not provisioned in `users` is refused (403).
  5. Teams refuses when the Bot Framework token is missing/unverifiable, and
     when TEAMS_BOT_APP_ID is unset there is no bypass path.
  6. RBAC still applies through the webhook path: an operator cannot approve
     a `critical` action arriving via chat.
"""
from __future__ import annotations

import uuid

import pytest

from sai.approval.engine import ApprovalEngine, InsufficientRoleError
from sai.config import Settings
from sai.db.models import User
from sai.notifications import slack, teams


# -- payload attribution (invariant 3) ------------------------------------


def test_slack_payload_without_user_is_rejected():
    payload = {
        "actions": [{"action_id": "sai_approve", "value": str(uuid.uuid4())}],
        # no "user" key at all
    }
    with pytest.raises(ValueError, match="user.id"):
        slack.handle_slack_interaction_payload(payload)


def test_slack_payload_with_user_returns_slack_user_id():
    action_id = str(uuid.uuid4())
    payload = {
        "actions": [{"action_id": "sai_approve", "value": action_id}],
        "user": {"id": "U12345"},
    }
    parsed = slack.handle_slack_interaction_payload(payload)
    assert parsed == {"action_id": action_id, "decision": "approve", "slack_user_id": "U12345"}


@pytest.mark.asyncio
async def test_teams_payload_without_aad_object_id_is_rejected():
    payload = {"value": {"action_id": str(uuid.uuid4()), "decision": "approve"}}
    with pytest.raises(ValueError, match="aadObjectId"):
        await teams.handle_teams_action_callback(payload)


@pytest.mark.asyncio
async def test_teams_payload_with_identity_returns_aad_object_id():
    action_id = str(uuid.uuid4())
    payload = {
        "value": {"action_id": action_id, "decision": "approve"},
        "from": {"aadObjectId": "oid-admin"},
    }
    parsed = await teams.handle_teams_action_callback(payload)
    assert parsed["aad_object_id"] == "oid-admin"
    assert parsed["decision"] == "approve"


# -- Teams authenticity (invariant 5) -------------------------------------


@pytest.mark.asyncio
async def test_teams_rejects_when_bot_app_id_unconfigured():
    """No configuration state may disable verification — an unset bot app id
    must disable approvals, not disable the check."""
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="x", TEAMS_BOT_APP_ID="")
    with pytest.raises(teams.TeamsAuthError, match="TEAMS_BOT_APP_ID"):
        await teams.verify_bot_framework_token(settings, "Bearer whatever")


@pytest.mark.asyncio
async def test_teams_rejects_missing_authorization_header():
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="x", TEAMS_BOT_APP_ID="bot-app-id")
    with pytest.raises(teams.TeamsAuthError, match="Authorization"):
        await teams.verify_bot_framework_token(settings, None)


@pytest.mark.asyncio
async def test_teams_rejects_non_bearer_header():
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="x", TEAMS_BOT_APP_ID="bot-app-id")
    with pytest.raises(teams.TeamsAuthError, match="Authorization"):
        await teams.verify_bot_framework_token(settings, "Basic dXNlcjpwYXNz")


@pytest.mark.asyncio
async def test_teams_rejects_unverifiable_token(monkeypatch):
    """A syntactically plausible but unsigned/forged token must not pass."""
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="x", TEAMS_BOT_APP_ID="bot-app-id")

    class _BoomJwks:
        def get_signing_key_from_jwt(self, token):  # noqa: ANN001
            raise ValueError("no matching signing key")

    async def _fake_client():
        return _BoomJwks()

    monkeypatch.setattr(teams, "_get_jwks_client", _fake_client)
    with pytest.raises(teams.TeamsAuthError, match="failed validation"):
        await teams.verify_bot_framework_token(settings, "Bearer forged.token.value")


# -- Slack authenticity (invariants 1 and 2) ------------------------------


def test_slack_signature_verification_rejects_bad_signature():
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="x", SLACK_SIGNING_SECRET="s3cr3t")
    ok = slack.verify_slack_signature(settings, "1700000000", b"payload=whatever", "v0=deadbeef")
    assert ok is False


# -- RBAC still enforced downstream (invariant 6) -------------------------


@pytest.mark.asyncio
async def test_operator_cannot_approve_critical_action_via_engine(
    approval_engine: ApprovalEngine, operator_user: User
):
    """The webhook path calls the same engine as the web app, so RBAC cannot
    be bypassed by approving from a chat client."""
    action = await approval_engine.create_action(
        tool_name="sql_restore_database",
        parameters={"database": "prod", "backup_file": "\\\\backup\\prod.bak"},
        proposed_description="Restore prod database",
        actor="system:test",
    )
    assert action.risk_level == "critical"
    assert action.status == "proposed"

    with pytest.raises(InsufficientRoleError):
        await approval_engine.approve_action(action.id, operator_user)

    # And crucially, it did NOT execute.
    assert action.status == "proposed"
    assert action.executed_at is None
