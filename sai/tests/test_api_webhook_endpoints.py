"""End-to-end HTTP tests for the approval webhooks.

The unit tests in test_webhook_security.py verify the individual guards; these
drive the real FastAPI endpoints through a TestClient to prove the guards are
actually *wired in* — a correct helper that the route forgets to call would
pass the unit tests and still leave the hole open.

The scenario that must never succeed: an unauthenticated POST approving a
critical action.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from sai.api import deps
from sai.api.main import app
from sai.config import Settings, get_settings
from sai.db.session import get_db_session


@pytest.fixture
def client(fake_session, approval_engine, monkeypatch):
    """TestClient with the database and approval engine replaced by the
    in-memory fakes, so no Postgres or external service is touched."""

    async def _fake_db():
        yield fake_session

    def _fake_settings():
        return Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key-not-real",
            # Deliberately blank: exercises the fail-closed paths.
            SLACK_SIGNING_SECRET="",
            TEAMS_BOT_APP_ID="",
        )

    app.dependency_overrides[get_db_session] = _fake_db
    app.dependency_overrides[deps.get_approval_engine] = lambda: approval_engine
    app.dependency_overrides[get_settings] = _fake_settings

    # The app's lifespan starts the scheduler and a DB engine; skip it.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_lifespan(monkeypatch):
    """Neutralize the startup hook so tests don't spin up the scheduler or
    connect to Postgres."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop)


# -- the core regression --------------------------------------------------


def test_teams_webhook_rejects_unauthenticated_approval(client):
    """THE defect this suite exists for: an anonymous POST must not approve."""
    resp = client.post(
        "/webhooks/teams",
        json={
            "value": {"action_id": str(uuid.uuid4()), "decision": "approve"},
            "from": {"aadObjectId": "oid-admin"},
        },
    )
    assert resp.status_code == 401
    assert "TEAMS_BOT_APP_ID" in resp.json()["detail"]


def test_slack_webhook_rejects_when_signing_secret_missing(client):
    """Absent signing secret must disable approvals, not disable the check."""
    resp = client.post(
        "/webhooks/slack",
        data={"payload": "{}"},
    )
    assert resp.status_code == 401
    assert "SLACK_SIGNING_SECRET" in resp.json()["detail"]


def test_slack_webhook_rejects_missing_signature_headers(client, monkeypatch):
    """With a secret configured, a request lacking signature headers fails."""

    def _settings_with_secret():
        return Settings(
            _env_file=None,
            ANTHROPIC_API_KEY="test-key-not-real",
            SLACK_SIGNING_SECRET="s3cr3t",
        )

    app.dependency_overrides[get_settings] = _settings_with_secret
    resp = client.post("/webhooks/slack", data={"payload": "{}"})
    assert resp.status_code == 401
    assert "signature" in resp.json()["detail"].lower()


def test_actions_endpoint_requires_authentication(client):
    """The web-app approval path is equally gated."""
    resp = client.post(f"/actions/{uuid.uuid4()}/approve")
    assert resp.status_code in (401, 403)
