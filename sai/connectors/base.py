"""Common connector interface: `Connector` protocol, `ToolSpec`, `ToolResult`.

Per SPEC.md section 5:

    class Connector(Protocol):
        name: str
        def list_tools(self) -> list[ToolSpec]: ...
        def healthcheck(self) -> bool: ...

`ToolSpec.risk_level` is **fixed in code** (SPEC 4.2 rule #1) — it is never
decided by the LLM at runtime, and the registry (sai/connectors/registry.py)
uses it, together with the allowlist, to decide whether a write tool call
must be gated by the Approval Engine.
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Any, Awaitable, Callable, Literal, Protocol, runtime_checkable

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclasses.dataclass(slots=True)
class ToolSpec:
    """Describes one callable tool exposed to the LLM.

    - `name`: unique tool name, matches the Anthropic tool-use `name` field.
    - `description`: shown to the model; should be precise about side effects.
    - `json_schema`: JSON Schema for `input_schema` in the Anthropic tool def.
    - `risk_level`: fixed classification per SPEC 5.x. Read tools are
      conventionally "low" but `requires_approval=False` and are never
      routed through the approval engine at all (they have no side effects).
    - `requires_approval`: True for any tool that mutates external state,
      unless later found in the allowlist for the exact scope requested.
    - `is_write`: True if this tool has side effects (write tools always
      go through the registry's approval gate; read tools are dispatched
      directly).
    - `handler`: the actual async callable `(**kwargs) -> ToolResult` that
      performs the real "action" (only invoked once the approval gate
      allows it, for write tools).
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    risk_level: RiskLevel
    requires_approval: bool
    is_write: bool
    handler: Callable[..., Awaitable["ToolResult"]]

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Render as an Anthropic API tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.json_schema,
        }


@dataclasses.dataclass(slots=True)
class ToolResult:
    """Uniform result wrapper returned by every tool handler."""

    ok: bool
    data: Any = None
    error: str | None = None

    def to_tool_result_content(self) -> str:
        """Serialize for the Anthropic `tool_result` content block.

        Sanitization note (SPEC 10.8): callers MUST NOT put secrets,
        connection strings, or customer PII into `data`/`error`; connectors
        are responsible for redacting before returning a ToolResult.
        """
        import json

        if self.ok:
            return json.dumps(self.data, default=str)
        return json.dumps({"error": self.error or "unknown error"})


@runtime_checkable
class Connector(Protocol):
    name: str

    def list_tools(self) -> list[ToolSpec]: ...

    async def healthcheck(self) -> bool: ...

    def is_configured(self) -> bool: ...


class BaseConnector(abc.ABC):
    """Convenience base class implementing the `Connector` protocol."""

    name: str = "base"

    @abc.abstractmethod
    def list_tools(self) -> list[ToolSpec]: ...

    def is_configured(self) -> bool:
        """Whether this connector has the credentials it needs.

        Used to skip the healthcheck entirely for systems the deployment
        simply doesn't use: attempting to reach an unconfigured endpoint only
        buys a DNS/TCP timeout, which is what made `/health` take seconds
        instead of milliseconds. Connectors override this with a check of
        their own required settings.
        """
        return True

    async def healthcheck(self) -> bool:
        """Default no-op healthcheck; connectors should override with a
        cheap read call against their backing system."""
        return True
