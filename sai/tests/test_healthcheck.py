"""Testes do healthcheck e da detecção de conector configurado.

Nasceram de um bug real: `/health` tentava conectar em TODOS os conectores,
inclusive nos que a instalação nem usa, gastando um timeout de DNS em cada um.
O endpoint levava ~7s, e o readiness probe do Container Apps (timeout padrão
de 1s) nunca teria sucesso — o container jamais ficaria pronto.

E um erro de digitação em `is_configured` (um nome de setting inexistente)
derrubava o endpoint inteiro com AttributeError, porque `is_configured` roda
fora do try/except do healthcheck. `test_every_connector_is_configured_works`
existe para pegar exatamente isso.
"""
from __future__ import annotations

import asyncio

import pytest

from sai.config import Settings
from sai.connectors.registry import ToolRegistry


@pytest.fixture
def empty_registry() -> ToolRegistry:
    """Registry sem NENHUMA credencial.

    Distinto da fixture `registry` do conftest, que injeta GUIDs fictícios de
    Azure de propósito — com eles o conector Azure está, corretamente,
    "configurado" (as credenciais existem, apenas são inválidas) e tenta
    autenticar de verdade. Aqui o cenário é o oposto: nada preenchido.
    """
    return ToolRegistry(Settings(_env_file=None, ANTHROPIC_API_KEY="test-key-not-real"))


def test_every_connector_is_configured_works(registry: ToolRegistry):
    """Chama is_configured() em todos os conectores.

    Um nome de setting errado aqui é AttributeError em produção, no caminho
    do healthcheck — e antes deste teste isso só aparecia rodando o servidor.
    """
    for connector in registry.connectors:
        result = connector.is_configured()
        assert isinstance(result, bool), f"{connector.name}.is_configured() não retornou bool"


def test_connectors_report_unconfigured_with_empty_settings(empty_registry: ToolRegistry):
    """Sem credencial nenhuma, nenhum conector externo pode se declarar
    configurado — senão o healthcheck tenta conectar no vazio."""
    for connector in empty_registry.connectors:
        assert connector.is_configured() is False, (
            f"{connector.name} diz estar configurado com credenciais vazias"
        )


def test_placeholder_urls_do_not_count_as_configured():
    """As URLs de exemplo do .env.example não podem passar por configuração
    real — senão o healthcheck tenta resolver 'example.com' e trava."""
    settings = Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test",
        ZABBIX_BASE_URL="https://zabbix.internal.example.com",
        ZABBIX_API_TOKEN="token-qualquer",
        GRAFANA_BASE_URL="https://grafana.internal.example.com",
        GRAFANA_API_TOKEN="token-qualquer",
    )
    registry = ToolRegistry(settings)
    by_name = {c.name: c for c in registry.connectors}
    assert by_name["zabbix"].is_configured() is False
    assert by_name["grafana"].is_configured() is False


@pytest.mark.asyncio
async def test_healthcheck_skips_unconfigured_without_network(empty_registry: ToolRegistry):
    """Nenhuma chamada de rede para conector não configurado — o resultado
    tem de ser 'not_configured', não 'error'."""
    results = await empty_registry.healthcheck_all()

    assert set(results) == {c.name for c in empty_registry.connectors}
    assert all(state == "not_configured" for state in results.values()), results


@pytest.mark.asyncio
async def test_healthcheck_is_fast_when_nothing_is_configured(empty_registry: ToolRegistry):
    """O caso que quebrava o readiness probe: sem nada configurado, o
    healthcheck tem de retornar praticamente instantaneamente."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    await empty_registry.healthcheck_all()
    elapsed = loop.time() - start

    assert elapsed < 0.5, f"healthcheck levou {elapsed:.2f}s sem nenhum conector configurado"


@pytest.mark.asyncio
async def test_healthcheck_reports_timeout_instead_of_hanging(
    empty_registry: ToolRegistry, monkeypatch
):
    """Um conector configurado porém pendurado vira 'timeout' dentro do prazo,
    em vez de segurar a resposta indefinidamente."""
    connector = empty_registry.connectors[0]

    async def _hang() -> bool:
        await asyncio.sleep(60)
        return True

    monkeypatch.setattr(connector, "is_configured", lambda: True)
    monkeypatch.setattr(connector, "healthcheck", _hang)

    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await empty_registry.healthcheck_all(timeout_seconds=0.2)
    elapsed = loop.time() - start

    assert results[connector.name] == "timeout"
    assert elapsed < 2.0, f"healthcheck não respeitou o timeout: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_healthcheck_reports_error_when_connector_raises(
    empty_registry: ToolRegistry, monkeypatch
):
    connector = empty_registry.connectors[0]

    async def _boom() -> bool:
        raise ConnectionError("host inalcançável")

    monkeypatch.setattr(connector, "is_configured", lambda: True)
    monkeypatch.setattr(connector, "healthcheck", _boom)

    results = await empty_registry.healthcheck_all()
    assert results[connector.name] == "error"
    # Uma falha isolada não pode contaminar os demais.
    others = {k: v for k, v in results.items() if k != connector.name}
    assert all(v == "not_configured" for v in others.values())
