"""Escalonamento pontual de modelo no chat.

O sistema roda no modelo econômico por padrão; o operador pode pedir o modelo
mais capaz numa pergunta específica. Três propriedades importam:

  1. Sem pedido explícito, usa o modelo padrão — escalonamento nunca é
     automático, para que o custo extra seja sempre uma escolha consciente.
  2. Com `escalate: true`, usa o modelo de escalonamento.
  3. Qual modelo respondeu fica registrado — no stream (para a UI marcar) e no
     histórico (para quem reler saber quanto peso dar ao diagnóstico).
"""
from __future__ import annotations

import json
import uuid

import pytest

from sai.api.routers import chat as chat_router
from sai.config import Settings


class _FakeOrchestrator:
    """Captura o modelo recebido e emite um turno mínimo."""

    seen_models: list[str] = []

    def __init__(self, **kwargs):
        _FakeOrchestrator.seen_models.append(kwargs.get("model"))

    async def run_turn(self, messages, user_message, actor, conversation_id=None, **_kw):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "resposta"}]})
        yield {"type": "final_text", "text": "resposta"}
        yield {"type": "done"}


@pytest.fixture(autouse=True)
def _reset():
    _FakeOrchestrator.seen_models = []


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        ANTHROPIC_MODEL="claude-sonnet-5",
        ANTHROPIC_ESCALATION_MODEL="claude-opus-5",
    )


async def _run_turn(monkeypatch, settings: Settings, escalate: bool) -> list[dict]:
    """Exercita a escolha de modelo e o stream como o endpoint faz, sem
    precisar de banco: replica o trecho de decisão + streaming de send_message.
    """
    monkeypatch.setattr(chat_router, "Orchestrator", _FakeOrchestrator)

    model = settings.anthropic_escalation_model if escalate else settings.anthropic_model
    orchestrator = chat_router.Orchestrator(
        settings=settings, registry=None, approval_engine=None,
        retrieve_context_fn=None, model=model,
    )

    events: list[dict] = []
    events.append({"type": "model", "model": model, "escalated": escalate})
    async for event in orchestrator.run_turn([], "por que a memoria esta alta?",
                                             actor="user:test", conversation_id=str(uuid.uuid4())):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_default_request_uses_the_economical_model(monkeypatch, settings):
    events = await _run_turn(monkeypatch, settings, escalate=False)

    assert _FakeOrchestrator.seen_models == ["claude-sonnet-5"]
    assert events[0] == {"type": "model", "model": "claude-sonnet-5", "escalated": False}


@pytest.mark.asyncio
async def test_escalated_request_uses_the_capable_model(monkeypatch, settings):
    events = await _run_turn(monkeypatch, settings, escalate=True)

    assert _FakeOrchestrator.seen_models == ["claude-opus-5"]
    assert events[0] == {"type": "model", "model": "claude-opus-5", "escalated": True}


@pytest.mark.asyncio
async def test_model_event_is_first_so_the_ui_can_label_before_text(monkeypatch, settings):
    """A marcação precisa chegar antes do texto — se viesse depois, a resposta
    apareceria sem rótulo e só seria marcada no fim."""
    events = await _run_turn(monkeypatch, settings, escalate=True)

    assert events[0]["type"] == "model"
    assert any(e["type"] == "final_text" for e in events[1:])


@pytest.mark.asyncio
async def test_every_event_is_valid_ndjson(monkeypatch, settings):
    """O frontend parseia linha a linha; um evento não serializável quebraria
    o stream no meio."""
    events = await _run_turn(monkeypatch, settings, escalate=True)
    for event in events:
        assert json.loads(json.dumps(event)) == event


def test_send_message_request_defaults_to_no_escalation():
    """O default do contrato da API é não escalar — um cliente que não conheça
    o campo nunca gasta a mais sem querer."""
    body = chat_router.SendMessageRequest(content="pergunta")
    assert body.escalate is False

    explicit = chat_router.SendMessageRequest(content="pergunta", escalate=True)
    assert explicit.escalate is True
