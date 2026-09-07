"""Registry tests — tool dispatch correctness and risk_level enforcement."""
from __future__ import annotations

import pytest

from sai.connectors.base import ToolResult
from sai.connectors.registry import (
    PROPOSE_ACTION_TOOL_NAME,
    ToolRegistry,
    UnknownToolError,
    WriteToolCalledDirectlyError,
)


def test_read_tools_never_include_write_tools(registry: ToolRegistry):
    """Architectural guarantee (SPEC 6.3): the model must never be handed a
    write tool definition directly."""
    read_defs = registry.get_read_tool_definitions()
    read_names = {d["name"] for d in read_defs}
    write_names = set(registry.all_write_tool_names())
    assert read_names.isdisjoint(write_names)
    assert PROPOSE_ACTION_TOOL_NAME not in read_names


def test_propose_action_definition_lists_every_write_tool(registry: ToolRegistry):
    definition = registry.get_propose_action_tool_definition()
    enum_values = set(definition["input_schema"]["properties"]["tool_name"]["enum"])
    assert enum_values == set(registry.all_write_tool_names())
    # sanity: known critical tools are present
    assert "sql_restore_database" in enum_values
    assert "f5_update_waf_policy" in enum_values


def test_all_llm_tool_definitions_include_propose_action_exactly_once(registry: ToolRegistry):
    defs = registry.get_all_llm_tool_definitions()
    names = [d["name"] for d in defs]
    assert names.count(PROPOSE_ACTION_TOOL_NAME) == 1


@pytest.mark.asyncio
async def test_dispatch_read_tool_executes_handler(registry: ToolRegistry, monkeypatch):
    spec = registry.get_spec("zabbix_get_problems")

    async def fake_handler(**kwargs):
        return ToolResult(ok=True, data={"problems": []})

    monkeypatch.setattr(spec, "handler", fake_handler)
    result = await registry.dispatch_read_tool("zabbix_get_problems", {"severity_min": 3})
    assert result.ok is True
    assert result.data == {"problems": []}


@pytest.mark.asyncio
async def test_dispatch_read_tool_rejects_write_tool_name(registry: ToolRegistry):
    with pytest.raises(WriteToolCalledDirectlyError):
        await registry.dispatch_read_tool("sql_restore_database", {})


@pytest.mark.asyncio
async def test_dispatch_write_tool_rejects_read_tool_name(registry: ToolRegistry):
    with pytest.raises(WriteToolCalledDirectlyError):
        await registry.dispatch_write_tool("zabbix_get_problems", {}, approved=True)


def test_unknown_tool_raises(registry: ToolRegistry):
    with pytest.raises(UnknownToolError):
        registry.get_spec("does_not_exist")


@pytest.mark.parametrize(
    "tool_name,expected_risk",
    [
        ("azure_resize_disk", "high"),
        ("azure_restart_vm", "medium"),
        ("sql_restore_database", "critical"),
        ("sql_kill_session", "medium"),
        ("f5_update_waf_policy", "critical"),
        ("f5_disable_pool_member", "high"),
        ("zabbix_acknowledge_problem", "low"),
        ("linux_restart_service", "low"),
        ("nginx_reload", "medium"),
    ],
)
def test_fixed_risk_levels_match_spec(registry: ToolRegistry, tool_name: str, expected_risk: str):
    """risk_level is fixed in code (SPEC 4.2 rule #1) — assert the exact
    classification from SPEC.md section 5 for a representative sample."""
    spec = registry.get_spec(tool_name)
    assert spec.risk_level == expected_risk
    assert spec.is_write is True


def test_read_tools_never_require_approval(registry: ToolRegistry):
    for name in registry.all_read_tool_names():
        spec = registry.get_spec(name)
        assert spec.requires_approval is False
        assert spec.is_write is False
