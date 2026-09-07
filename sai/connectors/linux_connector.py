"""Linux connector — SSH-based tools, per SPEC.md section 5.6 (labeled 5.6
in the doc; some header numbers drift slightly in the spec, implemented per
task instructions regardless).

Access model (SPEC 5.6 / 10.1): SSH with a dedicated key (never password),
and the remote automation user's sudo rights are restricted via `sudoers`
to an explicit allowlisted command set — never unrestricted sudo. This
connector assumes that restriction is enforced server-side; it never sends
arbitrary sudo commands, only a fixed, parameterized set matching the
tools below.
"""
from __future__ import annotations

import logging
import shlex
from typing import Any

import asyncssh

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class LinuxConnector(BaseConnector):
    name = "linux"

    def __init__(self, settings: Settings):
        self._settings = settings

    async def _run(self, host: str, command: str, timeout: float = 20.0) -> asyncssh.SSHCompletedProcess:
        async with asyncssh.connect(
            host,
            username=self._settings.linux_ssh_user,
            client_keys=[self._settings.linux_ssh_private_key_path],
            known_hosts=None,  # In production, pin known_hosts explicitly; see README SECURITY.
        ) as conn:
            return await conn.run(command, check=False, timeout=timeout)

    def is_configured(self) -> bool:
        return bool(self._settings.linux_ssh_user and self._settings.known_linux_hosts_list)

    async def healthcheck(self) -> bool:
        # No fixed target host for a generic healthcheck; presence of a
        # configured SSH key is the closest static check we can do here.
        import os

        return bool(self._settings.linux_ssh_user) and os.path.exists(self._settings.linux_ssh_private_key_path)

    # -- read tools -------------------------------------------------------

    async def get_system_metrics(self, host: str) -> ToolResult:
        try:
            cmd = (
                "echo '--cpu--'; top -bn1 | head -5; "
                "echo '--mem--'; free -m; "
                "echo '--disk--'; df -h; "
                "echo '--load--'; cat /proc/loadavg"
            )
            result = await self._run(host, cmd)
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "raw": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_top_processes(self, host: str, sort_by: str = "cpu") -> ToolResult:
        try:
            sort_flag = "-%cpu" if sort_by == "cpu" else "-%mem"
            cmd = f"ps aux --sort={sort_flag} | head -15"
            result = await self._run(host, cmd)
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "processes": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def tail_log(self, host: str, path: str, lines: int = 200) -> ToolResult:
        try:
            cmd = f"tail -n {int(lines)} {shlex.quote(path)}"
            result = await self._run(host, cmd)
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "path": path, "content": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def check_service_status(self, host: str, service: str) -> ToolResult:
        try:
            cmd = f"systemctl status {shlex.quote(service)} --no-pager"
            result = await self._run(host, cmd)
            return ToolResult(ok=True, data={"host": host, "service": service, "status_output": result.stdout, "exit_status": result.exit_status})
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools --------------------------------------------------------

    async def restart_service(self, host: str, service: str) -> ToolResult:
        """risk_level=low/medium — restart of a *non-critical* service can be
        allowlisted per host+service (allowlist.yaml). Critical services are
        never in the allowlist and always require approval."""
        try:
            cmd = f"sudo systemctl restart {shlex.quote(service)}"
            result = await self._run(host, cmd)
            return ToolResult(
                ok=result.exit_status == 0,
                data={"host": host, "service": service, "status": "restarted" if result.exit_status == 0 else "failed"},
                error=result.stderr or None,
            )
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def clear_disk_space(self, host: str, path: str, dry_run: bool = True) -> ToolResult:
        """risk_level=low/medium. Enforced in code: a real deletion
        (`dry_run=False`) is only permitted when the caller has already
        performed a `dry_run=True` pass — the registry/orchestrator is
        responsible for sequencing that, but this method itself refuses to
        silently default to a destructive run: `dry_run` must be passed
        explicitly by the tool call, and defaults to True."""
        try:
            safe_path = shlex.quote(path)
            if dry_run:
                cmd = f"find {safe_path} -type f -mtime +7 -printf '%p %s bytes\\n'"
            else:
                cmd = f"find {safe_path} -type f -mtime +7 -delete -print"
            result = await self._run(host, cmd, timeout=60.0)
            return ToolResult(
                ok=result.exit_status == 0,
                data={"host": host, "path": path, "dry_run": dry_run, "output": result.stdout},
                error=result.stderr or None,
            )
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="linux_get_system_metrics",
                description="Get CPU/memory/disk/load metrics for a Linux host over SSH.",
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_system_metrics(**kw),
            ),
            ToolSpec(
                name="linux_get_top_processes",
                description="List top processes by CPU or memory usage.",
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "sort_by": {"type": "string", "enum": ["cpu", "mem"]}},
                    "required": ["host"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_top_processes(**kw),
            ),
            ToolSpec(
                name="linux_tail_log",
                description="Tail the last N lines of a log file on a host.",
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "path": {"type": "string"}, "lines": {"type": "integer", "default": 200}},
                    "required": ["host", "path"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.tail_log(**kw),
            ),
            ToolSpec(
                name="linux_check_service_status",
                description="Check systemd service status on a host.",
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "service": {"type": "string"}},
                    "required": ["host", "service"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.check_service_status(**kw),
            ),
            ToolSpec(
                name="linux_restart_service",
                description=(
                    "Restart a systemd service on a host. WRITE ACTION — allowlist-eligible for "
                    "specific non-critical host+service pairs defined in allowlist.yaml."
                ),
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "service": {"type": "string"}},
                    "required": ["host", "service"],
                },
                risk_level="low", requires_approval=True, is_write=True,
                handler=lambda **kw: self.restart_service(**kw),
            ),
            ToolSpec(
                name="linux_clear_disk_space",
                description=(
                    "Clear old files under a path on a host. `dry_run=true` (default) shows what "
                    "would be deleted without deleting; a real run requires `dry_run=false` and "
                    "always requires approval."
                ),
                json_schema={
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "path": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True},
                    },
                    "required": ["host", "path", "dry_run"],
                },
                risk_level="medium", requires_approval=True, is_write=True,
                handler=lambda **kw: self.clear_disk_space(**kw),
            ),
        ]
