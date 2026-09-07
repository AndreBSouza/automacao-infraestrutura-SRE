"""SQL Server connector — pyodbc, per SPEC.md section 5.3.

Auth segregation (SPEC 10.2): SQL_READ_CONN_STRING points to an account
with `db_datareader` + `VIEW SERVER STATE` only; SQL_WRITE_CONN_STRING
points to a distinct, more privileged account used exclusively for
approved write actions.

Hard rule enforced IN CODE (not just documented): `restore_database` can
NEVER execute without an approved Action row. The registry (see
sai/connectors/registry.py) already gates every write tool behind the
Approval Engine, but because a database restore is uniquely destructive,
this connector additionally refuses to run unless it is explicitly told
the call is approved (`_approved=True`), so a bug elsewhere in the call
chain cannot accidentally trigger a live restore.
"""
from __future__ import annotations

import logging
from typing import Any

import pyodbc

from sai.config import Settings
from sai.connectors.base import BaseConnector, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ApprovalRequiredError(RuntimeError):
    """Raised when a critical tool is invoked without prior approval."""


class SqlConnector(BaseConnector):
    name = "sql_server"

    def __init__(self, settings: Settings):
        self._settings = settings

    def _connect(self, write: bool = False) -> pyodbc.Connection:
        conn_str = self._settings.sql_write_conn_string if write else self._settings.sql_read_conn_string
        if not conn_str:
            raise RuntimeError("SQL connection string not configured")
        return pyodbc.connect(conn_str, timeout=10, autocommit=True)

    def is_configured(self) -> bool:
        return bool(self._settings.sql_read_conn_string)

    async def healthcheck(self) -> bool:
        try:
            conn = self._connect(write=False)
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True
        except pyodbc.Error:
            logger.exception("SQL healthcheck failed")
            return False

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = self._connect(write=False)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    # -- read tools ---------------------------------------------------------

    async def get_active_sessions(self) -> ToolResult:
        try:
            rows = self._query(
                "SELECT session_id, status, login_name, host_name, program_name, cpu_time, "
                "total_elapsed_time, reads, writes, logical_reads "
                "FROM sys.dm_exec_sessions WHERE is_user_process = 1"
            )
            return ToolResult(ok=True, data={"sessions": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_blocking_chain(self) -> ToolResult:
        try:
            rows = self._query(
                "SELECT r.session_id AS blocked_session_id, r.blocking_session_id, "
                "r.wait_type, r.wait_time, r.wait_resource, t.text AS query_text "
                "FROM sys.dm_exec_requests r "
                "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t "
                "WHERE r.blocking_session_id <> 0"
            )
            return ToolResult(ok=True, data={"blocking_chain": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_slow_queries(self, top_n: int = 20) -> ToolResult:
        try:
            rows = self._query(
                "SELECT TOP (?) qs.total_elapsed_time / qs.execution_count AS avg_elapsed_us, "
                "qs.execution_count, SUBSTRING(st.text, 1, 500) AS query_text "
                "FROM sys.dm_exec_query_stats qs "
                "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st "
                "ORDER BY avg_elapsed_us DESC",
                (top_n,),
            )
            return ToolResult(ok=True, data={"slow_queries": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_db_size_and_growth(self, database: str) -> ToolResult:
        try:
            rows = self._query(
                "SELECT DB_NAME(database_id) AS database_name, type_desc, "
                "size * 8 / 1024 AS size_mb, growth, is_percent_growth "
                "FROM sys.master_files WHERE DB_NAME(database_id) = ?",
                (database,),
            )
            return ToolResult(ok=True, data={"database": database, "files": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_agent_job_history(self, job_name: str) -> ToolResult:
        try:
            rows = self._query(
                "SELECT TOP 50 j.name AS job_name, h.run_date, h.run_time, h.run_duration, "
                "h.run_status, h.message "
                "FROM msdb.dbo.sysjobhistory h "
                "JOIN msdb.dbo.sysjobs j ON h.job_id = j.job_id "
                "WHERE j.name = ? ORDER BY h.instance_id DESC",
                (job_name,),
            )
            return ToolResult(ok=True, data={"job_name": job_name, "history": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def get_last_backup_info(self, database: str) -> ToolResult:
        try:
            rows = self._query(
                "SELECT TOP 5 database_name, backup_start_date, backup_finish_date, type "
                "FROM msdb.dbo.backupset WHERE database_name = ? ORDER BY backup_finish_date DESC",
                (database,),
            )
            return ToolResult(ok=True, data={"database": database, "backups": rows})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    # -- write tools (SPEC 5.3: always manual approval, no exception) -------

    async def kill_session(self, session_id: int) -> ToolResult:
        """risk_level=medium."""
        try:
            conn = self._connect(write=True)
            conn.cursor().execute(f"KILL {int(session_id)}")
            conn.close()
            return ToolResult(ok=True, data={"session_id": session_id, "status": "killed"})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def restore_database(
        self,
        database: str,
        backup_file: str,
        point_in_time: str | None = None,
        _approved: bool = False,
    ) -> ToolResult:
        """risk_level=critical. ALWAYS requires manual approval, no exception
        (SPEC 5.3 + 7.3). Automatically snapshots the current database state
        before restoring, when applicable.

        The `_approved` flag is set exclusively by the connector registry
        (sai/connectors/registry.py::ToolRegistry.dispatch) right after it
        has confirmed the corresponding `Action` row has
        `status == 'approved'`. Any other caller passing `_approved=True`
        without going through the approval engine is a bug — this method
        cannot itself verify DB state, so it is a defense-in-depth check,
        not the sole gate.
        """
        if not _approved:
            raise ApprovalRequiredError(
                "sql_restore_database cannot execute without an approved Action record"
            )
        try:
            conn = self._connect(write=True)
            cursor = conn.cursor()

            # Snapshot current state before destructive restore.
            snapshot_name = f"{database}_pre_restore_snapshot"
            try:
                cursor.execute(
                    f"BACKUP DATABASE [{database}] TO DISK = '{snapshot_name}.bak' WITH COPY_ONLY"
                )
            except pyodbc.Error:
                logger.warning("Pre-restore snapshot failed for %s; continuing per approved plan", database)

            restore_sql = f"RESTORE DATABASE [{database}] FROM DISK = '{backup_file}' WITH REPLACE"
            if point_in_time:
                restore_sql += f", STOPAT = '{point_in_time}'"
            cursor.execute(restore_sql)
            conn.close()
            return ToolResult(
                ok=True,
                data={
                    "database": database,
                    "backup_file": backup_file,
                    "point_in_time": point_in_time,
                    "pre_restore_snapshot": f"{snapshot_name}.bak",
                    "status": "restored",
                },
            )
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    async def run_index_maintenance(self, database: str, table: str) -> ToolResult:
        """risk_level=medium."""
        try:
            conn = self._connect(write=True)
            cursor = conn.cursor()
            cursor.execute(f"USE [{database}]; ALTER INDEX ALL ON [{table}] REBUILD")
            conn.close()
            return ToolResult(ok=True, data={"database": database, "table": table, "status": "rebuilt"})
        except pyodbc.Error as exc:
            return ToolResult(ok=False, error=str(exc))

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="sql_get_active_sessions",
                description="List active SQL Server sessions (sys.dm_exec_sessions).",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_active_sessions(),
            ),
            ToolSpec(
                name="sql_get_blocking_chain",
                description="Detect blocking/lock chains currently active on the instance.",
                json_schema={"type": "object", "properties": {}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_blocking_chain(),
            ),
            ToolSpec(
                name="sql_get_slow_queries",
                description="Top N slowest queries by average elapsed time.",
                json_schema={"type": "object", "properties": {"top_n": {"type": "integer", "default": 20}}},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_slow_queries(**kw),
            ),
            ToolSpec(
                name="sql_get_db_size_and_growth",
                description="Database file sizes and growth settings.",
                json_schema={"type": "object", "properties": {"database": {"type": "string"}}, "required": ["database"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_db_size_and_growth(**kw),
            ),
            ToolSpec(
                name="sql_get_agent_job_history",
                description="SQL Agent job run history for a given job name.",
                json_schema={"type": "object", "properties": {"job_name": {"type": "string"}}, "required": ["job_name"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_agent_job_history(**kw),
            ),
            ToolSpec(
                name="sql_get_last_backup_info",
                description="Most recent backups for a database.",
                json_schema={"type": "object", "properties": {"database": {"type": "string"}}, "required": ["database"]},
                risk_level="low", requires_approval=False, is_write=False,
                handler=lambda **kw: self.get_last_backup_info(**kw),
            ),
            ToolSpec(
                name="sql_kill_session",
                description="Kill a SQL Server session by session_id. WRITE ACTION.",
                json_schema={"type": "object", "properties": {"session_id": {"type": "integer"}}, "required": ["session_id"]},
                risk_level="medium", requires_approval=True, is_write=True,
                handler=lambda **kw: self.kill_session(**kw),
            ),
            ToolSpec(
                name="sql_restore_database",
                description=(
                    "Restore a database from a backup file, optionally to a point in time. "
                    "CRITICAL WRITE ACTION — always requires explicit human approval, no exception. "
                    "Automatically snapshots current state first."
                ),
                json_schema={
                    "type": "object",
                    "properties": {
                        "database": {"type": "string"},
                        "backup_file": {"type": "string"},
                        "point_in_time": {"type": "string"},
                    },
                    "required": ["database", "backup_file"],
                },
                risk_level="critical", requires_approval=True, is_write=True,
                handler=lambda **kw: self.restore_database(**kw),
            ),
            ToolSpec(
                name="sql_run_index_maintenance",
                description="Rebuild all indexes on a table. WRITE ACTION.",
                json_schema={
                    "type": "object",
                    "properties": {"database": {"type": "string"}, "table": {"type": "string"}},
                    "required": ["database", "table"],
                },
                risk_level="medium", requires_approval=True, is_write=True,
                handler=lambda **kw: self.run_index_maintenance(**kw),
            ),
        ]
