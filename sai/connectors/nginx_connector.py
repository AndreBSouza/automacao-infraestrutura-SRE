"""Nginx connector — over the same SSH channel as Linux, per SPEC.md 5.7.

Hard rule enforced IN CODE: `reload()` always runs `nginx -t` first and
ABORTS the reload if config validation fails — never a documentation-only
guarantee.
"""
from __future__ import annotations

import logging

import asyncssh

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class NginxConfigTestFailed(RuntimeError):
    """Raised when `nginx -t` fails, aborting a reload."""


class NginxConnector(BaseConnector):
    name = "nginx"

    def __init__(self, settings: Settings):
        self._settings = settings

    async def _run(self, host: str, command: str, timeout: float = 20.0) -> asyncssh.SSHCompletedProcess:
        async with asyncssh.connect(
            host,
            username=self._settings.linux_ssh_user,
            client_keys=[self._settings.linux_ssh_private_key_path],
            known_hosts=None,
        ) as conn:
            return await conn.run(command, check=False, timeout=timeout)

    def is_configured(self) -> bool:
        return bool(self._settings.linux_ssh_user and self._settings.known_linux_hosts_list)

    async def healthcheck(self) -> bool:
        import os

        return os.path.exists(self._settings.linux_ssh_private_key_path)

    # -- read tools -------------------------------------------------------

    async def get_active_config(self, host: str) -> ToolResult:
        try:
            result = await self._run(host, "sudo nginx -T 2>&1")
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "config_dump": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_status(self, host: str) -> ToolResult:
        try:
            # Assumes the `stub_status` module is exposed at /nginx_status locally on the host.
            result = await self._run(host, "curl -s http://127.0.0.1/nginx_status")
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "status": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def tail_access_log(self, host: str, filter: str | None = None) -> ToolResult:
        try:
            cmd = "sudo tail -n 200 /var/log/nginx/access.log"
            if filter:
                import shlex

                cmd += f" | grep {shlex.quote(filter)}"
            result = await self._run(host, cmd)
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "log": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def tail_error_log(self, host: str) -> ToolResult:
        try:
            result = await self._run(host, "sudo tail -n 200 /var/log/nginx/error.log")
            return ToolResult(ok=result.exit_status == 0, data={"host": host, "log": result.stdout}, error=result.stderr or None)
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools (SPEC 5.7: medium, `nginx -t` gate enforced in code) --

    async def reload(self, host: str) -> ToolResult:
        try:
            test_result = await self._run(host, "sudo nginx -t 2>&1")
            if test_result.exit_status != 0:
                raise NginxConfigTestFailed(f"nginx -t failed on {host}: {test_result.stdout}{test_result.stderr}")

            reload_result = await self._run(host, "sudo systemctl reload nginx")
            return ToolResult(
                ok=reload_result.exit_status == 0,
                data={"host": host, "nginx_t_output": test_result.stdout, "status": "reloaded"},
                error=reload_result.stderr or None,
            )
        except NginxConfigTestFailed as exc:
            logger.warning("nginx reload aborted: %s", exc)
            return ToolResult(ok=False, error=str(exc))
        except (asyncssh.Error, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="nginx_get_active_config",
                description="Dump the active Nginx configuration via `nginx -T`.",
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_active_config(**kw),
            ),
            ToolSpec(
                name="nginx_get_status",
                description="Get Nginx stub_status metrics for a host.",
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_status(**kw),
            ),
            ToolSpec(
                name="nginx_tail_access_log",
                description="Tail the Nginx access log, optionally filtered by a grep pattern.",
                json_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string"}, "filter": {"type": "string"}},
                    "required": ["host"],
                },
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.tail_access_log(**kw),
            ),
            ToolSpec(
                name="nginx_tail_error_log",
                description="Tail the Nginx error log.",
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.tail_error_log(**kw),
            ),
            ToolSpec(
                name="nginx_reload",
                description=(
                    "Reload Nginx after validating config with `nginx -t`; aborts automatically if "
                    "validation fails. WRITE ACTION — allowlist-eligible."
                ),
                json_schema={"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
                risk_level="medium", requires_approval=True, is_write=True,
                handler=lambda **kw: self.reload(**kw),
            ),
        ]
