"""Loads the low-risk allowlist from a versioned YAML config (SPEC 7.3).

The allowlist is NEVER editable via chat/API — only by editing the file in
the repo and deploying, which is the point: an LLM (or a compromised
conversation) cannot grant itself pre-approval by talking. This module only
reads and evaluates it.
"""
from __future__ import annotations

import dataclasses
import fnmatch
from pathlib import Path
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True, slots=True)
class AllowlistEntry:
    tool_name: str
    # Scope matchers, e.g. {"host": "web-*.internal", "service": "nginx"}.
    # A key missing from the entry means "match any" for that key.
    scope: dict[str, str]


class Allowlist:
    def __init__(self, entries: list[AllowlistEntry]):
        self._entries = entries

    @classmethod
    def load(cls, path: str | Path) -> "Allowlist":
        p = Path(path)
        if not p.exists():
            return cls(entries=[])
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        entries = []
        for item in raw.get("allowlist", []):
            entries.append(AllowlistEntry(tool_name=item["tool_name"], scope=item.get("scope", {}) or {}))
        return cls(entries=entries)

    def is_allowlisted(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        """True if `tool_name` called with `parameters` matches a
        pre-approved low-risk entry exactly (scope-wise). Uses fnmatch-style
        globs on scope values so entries like `host: "web-*.internal"` work.
        """
        for entry in self._entries:
            if entry.tool_name != tool_name:
                continue
            if all(
                fnmatch.fnmatch(str(parameters.get(key, "")), pattern)
                for key, pattern in entry.scope.items()
            ):
                return True
        return False

    @property
    def entries(self) -> list[AllowlistEntry]:
        return list(self._entries)
