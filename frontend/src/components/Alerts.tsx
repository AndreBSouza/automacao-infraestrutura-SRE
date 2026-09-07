import { useCallback, useEffect, useState } from 'react'
import { api, type AlertOut } from '../api'

const SEVERITY_ORDER: Record<AlertOut['severity'], number> = {
  critical: 0,
  warning: 1,
  info: 2,
}

export default function Alerts() {
  const [alerts, setAlerts] = useState<AlertOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    api
      .listAlerts()
      .then((rows) =>
        setAlerts([...rows].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])),
      )
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
    // The vigia polls every few minutes; refreshing on that order keeps the
    // board current without hammering the API.
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [load])

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Alertas</h2>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          Atualizar
        </button>
      </div>

      {error && <div className="banner banner-error">{error}</div>}
      {loading && alerts.length === 0 && <p className="muted">Carregando…</p>}
      {!loading && alerts.length === 0 && <p className="muted">Nenhum alerta aberto.</p>}

      <div className="cards">
        {alerts.map((alert) => (
          <div key={alert.id} className={`card severity-${alert.severity}`}>
            <div className="card-head">
              <span className={`risk-badge risk-badge-${alert.severity}`}>
                {alert.severity.toUpperCase()}
              </span>
              <span className="muted">{alert.source}</span>
              <span className={`status status-${alert.status}`}>{alert.status}</span>
            </div>
            <p className="card-desc">{alert.summary}</p>
            {alert.diagnosis && (
              <details className="params" open>
                <summary>Diagnóstico da IA</summary>
                <p className="diagnosis">{alert.diagnosis}</p>
              </details>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
