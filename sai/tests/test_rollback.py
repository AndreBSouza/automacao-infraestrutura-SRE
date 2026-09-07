"""SPEC 13.6 — para cada tool crítica, validar que o plano de rollback é real.

Um `rollback_plan` que só existe como texto na proposta é teatro: se ninguém
verificou que ele é executável, ele não vale nada às 3h da manhã de um
incidente. Estes testes travam três propriedades:

  1. **Existe caminho de volta.** Toda tool `high`/`critical` tem uma operação
     inversa registrada, ou é explicitamente declarada como irreversível (e
     nesse caso precisa gerar um snapshot antes de executar).
  2. **O caminho de volta funciona.** Executando ação → rollback contra
     conectores simulados, o estado final volta ao inicial.
  3. **A proposta obriga o plano.** O schema de `propose_action` exige
     `rollback_plan`; uma proposta sem ele é recusada antes de virar `Action`.

Os conectores são simulados: o objetivo é validar o *contrato de reversão* e
o fluxo de governança, não a API do fornecedor.
"""
from __future__ import annotations

import pytest

from sai.approval.engine import ApprovalEngine
from sai.connectors.base import ToolResult
from sai.connectors.registry import PROPOSE_ACTION_TOOL_NAME, ToolRegistry
from sai.db.models import User

# Mapa explícito e revisado por humano: para cada tool de alto risco, qual é a
# operação que a desfaz. `None` = irreversível por natureza, e nesse caso a
# tool DEVE capturar um snapshot antes de agir.
ROLLBACK_MAP: dict[str, str | None] = {
    "f5_disable_pool_member": "f5_enable_pool_member",
    "f5_enable_pool_member": "f5_disable_pool_member",
    "f5_update_waf_policy": None,       # snapshot da policy anterior
    "waf_toggle_rule": "waf_toggle_rule",  # inverso é o mesmo toggle
    "waf_add_exclusion": None,          # remoção manual da exclusão
    "sql_restore_database": None,       # snapshot pré-restore
    "azure_resize_disk": None,          # discos não encolhem no Azure
    "devops_approve_release": None,     # release aprovado não "desaprova"
}

IRREVERSIBLE_REQUIRING_SNAPSHOT = {name for name, inv in ROLLBACK_MAP.items() if inv is None}


def _high_risk_tools(registry: ToolRegistry) -> set[str]:
    return {
        name
        for name in registry.all_write_tool_names()
        if registry.get_spec(name).risk_level in ("high", "critical")
    }


# -- 1. cobertura ---------------------------------------------------------


def test_every_high_risk_tool_has_a_documented_rollback_position(registry: ToolRegistry):
    """Nenhuma tool de alto risco pode existir sem uma decisão consciente
    sobre como revertê-la. Uma tool nova quebra este teste de propósito."""
    uncovered = _high_risk_tools(registry) - ROLLBACK_MAP.keys()
    assert not uncovered, (
        f"tools de alto risco sem posição de rollback definida: {sorted(uncovered)}. "
        "Defina a operação inversa em ROLLBACK_MAP, ou declare como irreversível "
        "(None) e garanta que a tool capture um snapshot antes de executar."
    )


def test_rollback_map_has_no_stale_entries(registry: ToolRegistry):
    """O inverso também vale: uma entrada apontando para tool inexistente
    indica que o mapa envelheceu junto com um rename."""
    known = set(registry.all_write_tool_names())
    stale = ROLLBACK_MAP.keys() - known
    assert not stale, f"ROLLBACK_MAP referencia tools inexistentes: {sorted(stale)}"

    for tool, inverse in ROLLBACK_MAP.items():
        if inverse is not None:
            assert inverse in known, f"rollback '{inverse}' de '{tool}' não existe"


# -- 2. o rollback realmente reverte --------------------------------------


class FakeF5State:
    """Estado mínimo de um pool F5 para exercitar disable → enable."""

    def __init__(self):
        self.enabled: dict[str, bool] = {"web-01": True, "web-02": True}

    async def disable(self, pool: str, member: str) -> ToolResult:  # noqa: ARG002
        self.enabled[member] = False
        return ToolResult(ok=True, data={"member": member, "state": "disabled"})

    async def enable(self, pool: str, member: str) -> ToolResult:  # noqa: ARG002
        self.enabled[member] = True
        return ToolResult(ok=True, data={"member": member, "state": "enabled"})


@pytest.mark.asyncio
async def test_f5_disable_then_rollback_restores_original_state(
    approval_engine: ApprovalEngine, admin_user: User, registry: ToolRegistry, monkeypatch
):
    """Fluxo completo: propõe → aprova → executa → reverte, tudo pelo engine,
    conferindo que o estado final é idêntico ao inicial."""
    state = FakeF5State()
    before = dict(state.enabled)

    monkeypatch.setattr(registry.get_spec("f5_disable_pool_member"), "handler", state.disable)
    monkeypatch.setattr(registry.get_spec("f5_enable_pool_member"), "handler", state.enable)

    params = {"pool": "pool_web", "member": "web-01"}

    action = await approval_engine.create_action(
        tool_name="f5_disable_pool_member",
        parameters=params,
        proposed_description="Drenar web-01 para manutenção",
        actor="system:test",
    )
    await approval_engine.approve_action(action.id, admin_user)
    assert action.status == "succeeded"
    assert state.enabled["web-01"] is False, "a ação precisa ter tido efeito real"

    rollback = await approval_engine.create_action(
        tool_name="f5_enable_pool_member",
        parameters=params,
        proposed_description="Rollback: reabilitar web-01",
        actor="system:test",
    )
    await approval_engine.approve_action(rollback.id, admin_user)

    assert rollback.status == "succeeded"
    assert state.enabled == before, "o rollback deve restaurar exatamente o estado inicial"


@pytest.mark.asyncio
async def test_rollback_itself_goes_through_approval(
    approval_engine: ApprovalEngine, registry: ToolRegistry, monkeypatch
):
    """Reverter também é mudar produção: o rollback não tem passe livre."""
    state = FakeF5State()
    monkeypatch.setattr(registry.get_spec("f5_enable_pool_member"), "handler", state.enable)

    rollback = await approval_engine.create_action(
        tool_name="f5_enable_pool_member",
        parameters={"pool": "pool_web", "member": "web-01"},
        proposed_description="Rollback",
        actor="system:test",
    )
    assert rollback.status == "proposed"
    assert rollback.requires_approval is True

    from sai.approval.engine import InvalidActionStateError

    with pytest.raises(InvalidActionStateError):
        await approval_engine.execute_approved_action(rollback.id, actor="system:test")


@pytest.mark.asyncio
async def test_failed_action_is_recorded_not_silently_swallowed(
    approval_engine: ApprovalEngine, admin_user: User, registry: ToolRegistry, monkeypatch
):
    """Se a execução falha, o status precisa dizer isso — um rollback só pode
    ser decidido por quem sabe o que de fato aconteceu."""

    async def _boom(**_kwargs):
        raise RuntimeError("F5 fora do ar")

    monkeypatch.setattr(registry.get_spec("f5_disable_pool_member"), "handler", _boom)

    action = await approval_engine.create_action(
        tool_name="f5_disable_pool_member",
        parameters={"pool": "p", "member": "m"},
        proposed_description="Drenar",
        actor="system:test",
    )
    await approval_engine.approve_action(action.id, admin_user)

    assert action.status == "failed"
    assert action.execution_result is not None
    assert "F5 fora do ar" in str(action.execution_result)


# -- 3. a proposta obriga o plano de rollback ------------------------------


def test_propose_action_schema_requires_rollback_plan(registry: ToolRegistry):
    """O modelo não consegue propor uma ação sem declarar como revertê-la."""
    schema = registry.get_propose_action_tool_definition()
    assert schema["name"] == PROPOSE_ACTION_TOOL_NAME
    required = schema["input_schema"]["required"]
    for field in ("rollback_plan", "expected_impact", "reasoning"):
        assert field in required, f"'{field}' deveria ser obrigatório em propose_action"


def test_irreversible_tools_are_flagged_in_their_description(registry: ToolRegistry):
    """Tools sem volta precisam dizer isso na própria descrição — é o texto que
    o modelo lê ao decidir, e que o operador lê ao aprovar."""
    for tool_name in IRREVERSIBLE_REQUIRING_SNAPSHOT:
        if tool_name not in registry.all_write_tool_names():
            continue
        spec = registry.get_spec(tool_name)
        description = spec.description.lower()
        assert any(
            word in description
            for word in ("approval", "aprovação", "irreversible", "snapshot", "critical", "high risk")
        ), f"a descrição de '{tool_name}' não sinaliza o risco/irreversibilidade"
