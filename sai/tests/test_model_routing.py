"""Testes do roteamento de modelo por rota.

O vigia (triagem automática, alto volume) e o chat (leitura humana do
raciocínio) podem rodar em modelos diferentes. Duas propriedades precisam
valer, e ambas falham em silêncio se quebrarem:

  1. Cada rota usa o modelo certo — um vigia que caia no modelo do chat
     multiplica o custo sem ninguém perceber.
  2. O modelo é FIXO por instância do orquestrador. Caches de prompt são por
     modelo; alternar modelo dentro de uma rota destruiria o prefixo cacheado
     a cada requisição, que é o oposto do objetivo de economizar.
"""
from __future__ import annotations

import pytest

from sai.config import Settings
from sai.connectors.registry import ToolRegistry
from sai.llm.orchestrator import Orchestrator


def _orchestrator(settings: Settings, registry: ToolRegistry, model: str | None = None) -> Orchestrator:
    return Orchestrator(
        settings=settings,
        registry=registry,
        approval_engine=None,
        retrieve_context_fn=None,
        model=model,
    )


def test_chat_route_uses_the_default_model(settings: Settings, registry: ToolRegistry):
    """Sem override, vale ANTHROPIC_MODEL — o caminho do chat."""
    assert _orchestrator(settings, registry)._model == settings.anthropic_model


def test_defaults_keep_both_routes_on_one_model(registry: ToolRegistry):
    """O padrão de fábrica mantém chat e vigia no mesmo modelo, de propósito:
    um único cache de prompt, aquecido pelas duas rotas. Se alguém divergir os
    padrões sem querer, a conta sobe silenciosamente."""
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="test-key-not-real")
    assert settings.anthropic_model == settings.anthropic_vigia_model
    assert settings.anthropic_escalation_model != settings.anthropic_model, (
        "o modelo de escalonamento precisa ser diferente do padrão, senão escalar não faz nada"
    )


def test_vigia_route_uses_its_own_model(registry: ToolRegistry):
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        ANTHROPIC_MODEL="claude-opus-5",
        ANTHROPIC_VIGIA_MODEL="claude-sonnet-5",
    )
    assert _orchestrator(settings, registry, settings.anthropic_vigia_model)._model == "claude-sonnet-5"
    assert _orchestrator(settings, registry)._model == "claude-opus-5"


def test_escalation_model_is_configurable(registry: ToolRegistry):
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        ANTHROPIC_MODEL="claude-sonnet-5",
        ANTHROPIC_ESCALATION_MODEL="claude-opus-5",
    )
    normal = _orchestrator(settings, registry, settings.anthropic_model)
    escalated = _orchestrator(settings, registry, settings.anthropic_escalation_model)

    assert normal._model == "claude-sonnet-5"
    assert escalated._model == "claude-opus-5"


def test_single_model_setup_keeps_one_cache_namespace(registry: ToolRegistry):
    """Configurar os dois iguais é uma escolha legítima — e a mais barata em
    volume baixo, porque mantém um único cache de prompt."""
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        ANTHROPIC_MODEL="claude-sonnet-5",
        ANTHROPIC_VIGIA_MODEL="claude-sonnet-5",
    )
    chat = _orchestrator(settings, registry)
    vigia = _orchestrator(settings, registry, settings.anthropic_vigia_model)
    assert chat._model == vigia._model


def test_model_is_pinned_for_the_instance_lifetime(settings: Settings, registry: ToolRegistry):
    """O modelo é resolvido uma vez, na construção. Se passasse a ser lido a
    cada requisição, uma troca de configuração em runtime invalidaria o cache
    no meio de um diagnóstico."""
    orchestrator = _orchestrator(settings, registry, "claude-sonnet-5")
    before = orchestrator._model

    # Alterar as settings depois não pode mudar o modelo já fixado.
    object.__setattr__(orchestrator._settings, "anthropic_model", "claude-opus-5")

    assert orchestrator._model == before == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_vigia_passes_its_model_to_the_orchestrator(monkeypatch, registry, empty_allowlist):
    """Verifica a ligação real dentro do vigia — não basta a configuração
    existir, ela precisa chegar ao orquestrador que o vigia constrói."""
    from contextlib import asynccontextmanager

    from sai.scheduler import vigia as vigia_module

    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        ANTHROPIC_MODEL="claude-opus-5",
        ANTHROPIC_VIGIA_MODEL="claude-sonnet-5",
    )

    captured: dict[str, str] = {}

    class _SpyOrchestrator:
        def __init__(self, **kwargs):
            captured["model"] = kwargs.get("model")

        async def run_turn(self, *_args, **_kwargs):
            if False:  # pragma: no cover - generator vazio
                yield {}

    @asynccontextmanager
    async def _scope():
        class _S:
            def add(self, _obj): ...
            async def flush(self): ...
            async def get(self, *_a): return None
        yield _S()

    monkeypatch.setattr(vigia_module, "Orchestrator", _SpyOrchestrator)
    monkeypatch.setattr(vigia_module, "session_scope", _scope)

    breach = vigia_module.ThresholdBreach(
        source="zabbix", resource="host-01", metric="cpu",
        severity="critical", summary="CPU alta", raw_payload={},
    )
    await vigia_module._diagnose_and_maybe_propose(
        breach, "alert-id", registry, empty_allowlist, settings, None
    )

    assert captured["model"] == "claude-sonnet-5", (
        "o vigia não está usando ANTHROPIC_VIGIA_MODEL — rodaria no modelo do chat"
    )
