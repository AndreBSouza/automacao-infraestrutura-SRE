"""Pytest fixtures with mocked/in-memory infrastructure — NO real network,
SSH, SQL, or Anthropic API calls happen in the test suite. Connectors are
constructed with dummy settings (their constructors do no I/O), and a
lightweight in-memory fake stands in for the SQLAlchemy AsyncSession so
the Approval Engine's state machine can be tested without a real Postgres
instance.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from sai.approval.allowlist import Allowlist, AllowlistEntry
from sai.approval.engine import ApprovalEngine
from sai.config import Settings
from sai.connectors.registry import ToolRegistry
from sai.db.models import Action, AuditLog, User


@pytest.fixture
def settings() -> Settings:
    # No real secrets: connector constructors don't perform I/O, only
    # capture config, so empty/placeholder values are safe for unit tests.
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        AZURE_TENANT_ID="00000000-0000-0000-0000-000000000000",
        AZURE_CLIENT_ID="00000000-0000-0000-0000-000000000000",
        AZURE_CLIENT_SECRET="not-a-real-secret",
        AZURE_SUBSCRIPTION_ID="00000000-0000-0000-0000-000000000000",
    )


@pytest.fixture
def registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(settings)


@pytest.fixture
def empty_allowlist() -> Allowlist:
    return Allowlist(entries=[])


@pytest.fixture
def sample_allowlist() -> Allowlist:
    return Allowlist(
        entries=[
            AllowlistEntry(tool_name="linux_restart_service", scope={"host": "web-01.internal", "service": "nginx"}),
            AllowlistEntry(tool_name="zabbix_acknowledge_problem", scope={}),
        ]
    )


class _ScalarsResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _ExecResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def scalars(self):
        return _ScalarsResult(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


@dataclass
class FakeAsyncSession:
    """Minimal stand-in for sqlalchemy.ext.asyncio.AsyncSession, sufficient
    for exercising ApprovalEngine without a real database. Supports
    `.add`, `.flush`, `.get`, and a narrow `.execute` that recognizes the
    one query shape ApprovalEngine.expire_timed_out_actions issues
    (`select(Action).where(Action.status == 'proposed')`)."""

    objects: dict[type, dict[Any, Any]] = field(default_factory=dict)

    def add(self, obj: Any) -> None:
        if isinstance(obj, AuditLog):
            if getattr(obj, "id", None) is None:
                obj.id = len(self.objects.get(AuditLog, {})) + 1
        elif getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.objects.setdefault(type(obj), {})[obj.id] = obj
        if not hasattr(obj, "created_at") or obj.created_at is None:
            import datetime

            obj.created_at = datetime.datetime.now(datetime.timezone.utc)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def get(self, model: type, id_: Any) -> Any:
        return self.objects.get(model, {}).get(id_)

    async def execute(self, stmt: Any) -> _ExecResult:
        # Only used by ApprovalEngine.expire_timed_out_actions in tests;
        # return every stored Action with status == 'proposed'.
        actions = list(self.objects.get(Action, {}).values())
        proposed = [a for a in actions if a.status == "proposed"]
        return _ExecResult(proposed)


@pytest.fixture
def fake_session() -> FakeAsyncSession:
    return FakeAsyncSession()


@pytest.fixture
def approval_engine(fake_session: FakeAsyncSession, registry: ToolRegistry, empty_allowlist: Allowlist, settings: Settings) -> ApprovalEngine:
    return ApprovalEngine(session=fake_session, registry=registry, allowlist=empty_allowlist, settings=settings, notify=None)


@pytest.fixture
def operator_user() -> User:
    return User(id=uuid.uuid4(), email="op@example.com", display_name="Operator", role="operator", entra_object_id="oid-op")


@pytest.fixture
def admin_user() -> User:
    return User(id=uuid.uuid4(), email="admin@example.com", display_name="Admin", role="admin", entra_object_id="oid-admin")
