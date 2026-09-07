"""Endpoints de exportação de relatórios (SPEC.md seção 14).

Três formatos, sempre sobre uma janela de tempo explícita:
  GET /reports/incidents.xlsx   — alertas do período
  GET /reports/actions.xlsx     — ações + trilha de auditoria
  GET /reports/summary.pdf      — resumo executivo de uma página

A janela é informada por `days` (padrão 30) ou pelo par `year`/`month`.

Acesso: qualquer usuário autenticado pode exportar. Os relatórios contêm o
histórico operacional (hosts, bancos, quem aprovou o quê) — nunca segredos —
mas continuam sendo documento interno; a exportação em si é registrada na
auditoria para que se saiba quem extraiu o quê e quando.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.routers.auth import get_current_user
from sai.db.models import AuditLog, User
from sai.db.session import get_db_session
from sai.reports import (
    ReportPeriod,
    build_actions_xlsx,
    build_incidents_xlsx,
    build_summary_pdf,
    collect_report_data,
)

router = APIRouter(prefix="/reports", tags=["reports"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _resolve_period(days: int, year: int | None, month: int | None) -> ReportPeriod:
    if (year is None) != (month is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="informe 'year' e 'month' juntos, ou nenhum dos dois",
        )
    if year is not None and month is not None:
        if not 1 <= month <= 12:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'month' deve estar entre 1 e 12")
        return ReportPeriod.month(year, month)
    return ReportPeriod.last_days(days)


async def _log_export(session: AsyncSession, user: User, report: str, period: ReportPeriod) -> None:
    session.add(
        AuditLog(
            actor=f"user:{user.id}",
            event_type="report_exported",
            entity_type="report",
            entity_id=None,
            payload={
                "report": report,
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
            },
        )
    )
    await session.commit()


PeriodParams = tuple[int, int | None, int | None]


async def _build(
    session: AsyncSession,
    user: User,
    report: str,
    days: int,
    year: int | None,
    month: int | None,
):
    period = _resolve_period(days, year, month)
    data = await collect_report_data(session, period)
    await _log_export(session, user, report, period)
    return period, data


def _filename(prefix: str, period: ReportPeriod, ext: str) -> str:
    return f"sai-{prefix}-{period.start:%Y%m%d}-{period.end:%Y%m%d}.{ext}"


@router.get("/incidents.xlsx")
async def incidents_xlsx(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    year: int | None = None,
    month: int | None = None,
) -> Response:
    period, data = await _build(session, user, "incidents.xlsx", days, year, month)
    return Response(
        content=build_incidents_xlsx(data),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{_filename("incidentes", period, "xlsx")}"'},
    )


@router.get("/actions.xlsx")
async def actions_xlsx(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    year: int | None = None,
    month: int | None = None,
) -> Response:
    period, data = await _build(session, user, "actions.xlsx", days, year, month)
    return Response(
        content=build_actions_xlsx(data),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{_filename("acoes", period, "xlsx")}"'},
    )


@router.get("/summary.pdf")
async def summary_pdf(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    year: int | None = None,
    month: int | None = None,
) -> Response:
    period, data = await _build(session, user, "summary.pdf", days, year, month)
    return Response(
        content=build_summary_pdf(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_filename("resumo", period, "pdf")}"'},
    )
