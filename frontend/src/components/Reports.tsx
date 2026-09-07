import { useState } from 'react'
import { api } from '../api'

/**
 * Exportação de relatórios (SPEC 14).
 *
 * O período é escolhido explicitamente — últimos N dias ou um mês fechado —
 * porque "o relatório do mês" e "os últimos 30 dias" são coisas diferentes no
 * fechamento, e deixar isso implícito produz números que não batem com os do
 * mês anterior.
 */

type ReportId = 'incidents.xlsx' | 'actions.xlsx' | 'summary.pdf'

const REPORTS: { id: ReportId; title: string; description: string; format: string }[] = [
  {
    id: 'summary.pdf',
    title: 'Resumo executivo',
    description:
      'Uma página: números do período, alertas críticos e ações de alto risco executadas.',
    format: 'PDF',
  },
  {
    id: 'incidents.xlsx',
    title: 'Incidentes',
    description: 'Todos os alertas do período com fonte, severidade, status e diagnóstico da IA.',
    format: 'Excel',
  },
  {
    id: 'actions.xlsx',
    title: 'Ações e auditoria',
    description:
      'Ações propostas e executadas, quem aprovou, resultado — mais a trilha de auditoria completa.',
    format: 'Excel',
  },
]

const MONTHS = [
  'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
  'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro',
]

export default function Reports() {
  const now = new Date()
  const [mode, setMode] = useState<'days' | 'month'>('days')
  const [days, setDays] = useState(30)
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth() + 1)
  const [downloading, setDownloading] = useState<ReportId | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function download(report: ReportId) {
    setDownloading(report)
    setError(null)
    try {
      await api.downloadReport(
        report,
        mode === 'month' ? { year, month } : { days },
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setDownloading(null)
    }
  }

  const years = Array.from({ length: 5 }, (_, i) => now.getFullYear() - i)

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Relatórios</h2>
      </div>

      {error && <div className="banner banner-error">{error}</div>}

      <fieldset className="period">
        <legend>Período</legend>
        <label className="radio">
          <input
            type="radio"
            checked={mode === 'days'}
            onChange={() => setMode('days')}
          />
          Últimos
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            disabled={mode !== 'days'}
          >
            {[7, 15, 30, 60, 90].map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
          dias
        </label>

        <label className="radio">
          <input
            type="radio"
            checked={mode === 'month'}
            onChange={() => setMode('month')}
          />
          Mês fechado
          <select
            value={month}
            onChange={(e) => setMonth(Number(e.target.value))}
            disabled={mode !== 'month'}
          >
            {MONTHS.map((m, i) => (
              <option key={m} value={i + 1}>{m}</option>
            ))}
          </select>
          <select
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            disabled={mode !== 'month'}
          >
            {years.map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </label>
      </fieldset>

      <div className="cards">
        {REPORTS.map((r) => (
          <div key={r.id} className="card">
            <div className="card-head">
              <strong>{r.title}</strong>
              <span className="risk-badge">{r.format}</span>
            </div>
            <p className="card-desc">{r.description}</p>
            <div className="card-actions">
              <button
                className="btn btn-primary"
                onClick={() => download(r.id)}
                disabled={downloading !== null}
              >
                {downloading === r.id ? 'Gerando…' : 'Baixar'}
              </button>
            </div>
          </div>
        ))}
      </div>

      <p className="muted small" style={{ marginTop: 20 }}>
        Cada exportação fica registrada no log de auditoria (quem exportou, qual relatório e qual
        período).
      </p>
    </div>
  )
}
