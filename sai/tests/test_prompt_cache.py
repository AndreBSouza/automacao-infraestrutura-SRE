"""Testes da configuração de cache de prompt.

Cache é a maior alavanca de custo do sistema e falha em silêncio: se o
breakpoint sair do lugar, ou se conteúdo volátil entrar antes dele, tudo
continua funcionando — só fica caro. Nada quebra, nenhum teste falha, e a
conta chega no fim do mês.

Estes testes travam a estrutura:
  1. O breakpoint existe e usa o TTL configurado.
  2. O contexto do RAG (que muda a cada pergunta) fica DEPOIS do breakpoint.
     Antes dele, invalidaria o prefixo inteiro a cada requisição.
"""
from __future__ import annotations

import pytest

from sai.config import Settings
from sai.connectors.registry import ToolRegistry
from sai.llm.orchestrator import Orchestrator


def _orchestrator(settings: Settings, registry: ToolRegistry, retrieve_fn=None) -> Orchestrator:
    return Orchestrator(
        settings=settings,
        registry=registry,
        approval_engine=None,
        retrieve_context_fn=retrieve_fn,
    )


@pytest.mark.asyncio
async def test_system_prompt_carries_a_cache_breakpoint(settings: Settings, registry: ToolRegistry):
    blocks = await _orchestrator(settings, registry)._build_system_blocks("qualquer pergunta")

    assert blocks[0].get("cache_control") is not None, (
        "o system prompt perdeu o cache_control — todo request passaria a pagar "
        "o prompt inteiro e as definições de tools a preço cheio"
    )
    assert blocks[0]["cache_control"]["type"] == "ephemeral"


@pytest.mark.asyncio
async def test_cache_ttl_follows_configuration(registry: ToolRegistry):
    for ttl in ("5m", "1h"):
        settings = Settings(
            _env_file=None, ANTHROPIC_API_KEY="test-key-not-real", ANTHROPIC_CACHE_TTL=ttl
        )
        blocks = await _orchestrator(settings, registry)._build_system_blocks("pergunta")
        assert blocks[0]["cache_control"]["ttl"] == ttl


def test_cache_ttl_rejects_invalid_values():
    """Um TTL inválido tem de falhar na inicialização, não virar um valor que
    a API rejeita no meio de um incidente."""
    with pytest.raises(Exception):
        Settings(_env_file=None, ANTHROPIC_API_KEY="test", ANTHROPIC_CACHE_TTL="30m")


@pytest.mark.asyncio
async def test_rag_context_comes_after_the_breakpoint(settings: Settings, registry: ToolRegistry):
    """O contexto recuperado muda a cada pergunta. Se entrasse antes do
    breakpoint, invalidaria o cache em toda requisição — o oposto do objetivo.
    """

    async def _retrieve(_query: str) -> str:
        return "conteúdo do runbook que varia por pergunta"

    blocks = await _orchestrator(settings, registry, _retrieve)._build_system_blocks("pergunta")

    assert len(blocks) == 2
    # O bloco volátil não pode carregar breakpoint próprio nem vir antes do fixo.
    assert "cache_control" in blocks[0]
    assert "cache_control" not in blocks[1]
    assert "runbook" in blocks[1]["text"]


@pytest.mark.asyncio
async def test_rag_failure_does_not_break_the_prefix(settings: Settings, registry: ToolRegistry):
    """Se o RAG falhar, o bloco cacheado tem de continuar intacto — uma falha
    de busca não pode custar o cache do prompt inteiro."""

    async def _boom(_query: str) -> str:
        raise RuntimeError("vector store fora do ar")

    blocks = await _orchestrator(settings, registry, _boom)._build_system_blocks("pergunta")

    assert len(blocks) == 1
    assert "cache_control" in blocks[0]
