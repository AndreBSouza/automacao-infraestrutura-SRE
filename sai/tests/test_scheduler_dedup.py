"""SPEC 13.5 — carga no scheduler: múltiplos alertas simultâneos devem ser
deduplicados, sem enxurrada de notificações nem gasto desnecessário de API.

O que estes testes travam:
  1. Uma condição persistente (o mesmo problema aparecendo em ticks
     consecutivos) gera UM alerta, não um por tick.
  2. Uma rajada de 50 breaches, das quais só 3 são distintas, gera 3 alertas —
     e portanto só 3 diagnósticos via LLM.
  3. Breaches de recursos diferentes com o mesmo resumo NÃO são colapsadas
     (dedup por recurso + resumo, não só por resumo).
  4. Fora do horário comercial nenhum tick roda — nenhuma chamada de API.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from sai.config import BusinessHoursConfig, Settings
from sai.db.models import Alert
from sai.scheduler import vigia
from sai.scheduler.vigia import ThresholdBreach, is_within_business_hours


class FakeAlertSession:
    """Sessão em memória suficiente para o caminho de dedup do vigia:
    `select(Alert).where(...)` seguido de filtragem em Python."""

    def __init__(self, store: list[Alert]):
        self.store = store

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        self.store.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def get(self, _model: type, id_: Any) -> Any:
        return next((a for a in self.store if a.id == id_), None)

    async def execute(self, _stmt: Any):
        open_states = {"open", "diagnosing", "awaiting_approval"}
        rows = [a for a in self.store if a.status in open_states]

        class _R:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return rows

                return _S()

        return _R()


@pytest.fixture
def alert_store() -> list[Alert]:
    return []


@pytest.fixture
def patched_vigia(monkeypatch, alert_store):
    """Substitui session_scope e o diagnóstico via LLM, contando quantas vezes
    o LLM seria invocado — a métrica que realmente importa para custo."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield FakeAlertSession(alert_store)

    diagnosis_calls: list[Any] = []

    async def _fake_diagnose(breach, alert_id, *_args, **_kwargs):
        diagnosis_calls.append((breach.resource, alert_id))
        return "diagnóstico simulado"

    monkeypatch.setattr(vigia, "session_scope", _scope)
    monkeypatch.setattr(vigia, "_diagnose_and_maybe_propose", _fake_diagnose)
    return diagnosis_calls


def _breach(resource: str, summary: str = "CPU acima de 90%") -> ThresholdBreach:
    return ThresholdBreach(
        source="zabbix",
        resource=resource,
        metric="system.cpu.util",
        severity="critical",
        summary=summary,
        raw_payload={"eventid": resource},
    )


def _settings_in_business_hours() -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="test-key-not-real")


def _patch_breaches(monkeypatch, breaches: list[ThresholdBreach]) -> None:
    async def _zabbix(_settings):
        return breaches

    async def _azure(_settings):
        return []

    monkeypatch.setattr(vigia, "_check_zabbix_thresholds", _zabbix)
    monkeypatch.setattr(vigia, "_check_azure_alerts", _azure)
    monkeypatch.setattr(vigia, "is_within_business_hours", lambda *_a, **_k: True)


@pytest.mark.asyncio
async def test_persistent_condition_creates_one_alert_across_ticks(
    monkeypatch, registry, empty_allowlist, alert_store, patched_vigia
):
    """O mesmo problema em 5 ticks seguidos = 1 alerta, 1 diagnóstico."""
    _patch_breaches(monkeypatch, [_breach("host-01")])
    settings = _settings_in_business_hours()

    for _ in range(5):
        await vigia.poll_once(registry, empty_allowlist, settings=settings)

    assert len(alert_store) == 1
    assert len(patched_vigia) == 1, "o LLM não pode ser chamado de novo para uma condição já aberta"


@pytest.mark.asyncio
async def test_burst_of_duplicates_collapses_to_distinct_alerts(
    monkeypatch, registry, empty_allowlist, alert_store, patched_vigia
):
    """Rajada de 50 breaches com apenas 3 recursos distintos → 3 alertas."""
    burst = [_breach(f"host-0{i % 3}") for i in range(50)]
    _patch_breaches(monkeypatch, burst)

    await vigia.poll_once(registry, empty_allowlist, settings=_settings_in_business_hours())

    assert len(alert_store) == 3
    assert len(patched_vigia) == 3
    assert {a.raw_payload["_resource"] for a in alert_store} == {"host-00", "host-01", "host-02"}


@pytest.mark.asyncio
async def test_same_summary_different_resources_are_not_collapsed(
    monkeypatch, registry, empty_allowlist, alert_store, patched_vigia
):
    """Dois servidores com o mesmo sintoma são dois incidentes, não um."""
    _patch_breaches(monkeypatch, [_breach("web-01"), _breach("web-02")])

    await vigia.poll_once(registry, empty_allowlist, settings=_settings_in_business_hours())

    assert len(alert_store) == 2


@pytest.mark.asyncio
async def test_resolved_alert_allows_a_new_one_for_the_same_resource(
    monkeypatch, registry, empty_allowlist, alert_store, patched_vigia
):
    """Depois de resolvido, o mesmo problema voltando gera um novo alerta —
    caso contrário uma recorrência ficaria invisível."""
    _patch_breaches(monkeypatch, [_breach("host-01")])
    settings = _settings_in_business_hours()

    await vigia.poll_once(registry, empty_allowlist, settings=settings)
    assert len(alert_store) == 1

    alert_store[0].status = "resolved"
    await vigia.poll_once(registry, empty_allowlist, settings=settings)

    assert len(alert_store) == 2


@pytest.mark.asyncio
async def test_outside_business_hours_no_work_and_no_api_calls(
    monkeypatch, registry, empty_allowlist, alert_store, patched_vigia
):
    _patch_breaches(monkeypatch, [_breach("host-01")])
    monkeypatch.setattr(vigia, "is_within_business_hours", lambda *_a, **_k: False)

    created = await vigia.poll_once(registry, empty_allowlist, settings=_settings_in_business_hours())

    assert created == []
    assert alert_store == []
    assert patched_vigia == []


# -- janela de horário comercial ------------------------------------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        (datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc), True),   # segunda, 10h
        (datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc), False),   # segunda, madrugada
        (datetime(2026, 9, 7, 23, 0, tzinfo=timezone.utc), False),  # segunda, noite
        (datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc), False),  # sábado
        (datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc), False),  # domingo
    ],
)
def test_business_hours_window(moment, expected):
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        business_hours=BusinessHoursConfig(
            _env_file=None, timezone="UTC", start_hour=8, end_hour=18, weekdays="0,1,2,3,4"
        ),
    )
    assert is_within_business_hours(settings, moment) is expected
