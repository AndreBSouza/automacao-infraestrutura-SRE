import { useCallback, useEffect, useState } from 'react'
import { api, type ActionOut } from '../api'

/**
 * The approval dashboard — the human gate in front of every state-changing
 * action (SPEC 7.2).
 *
 * Deliberate UI decisions:
 *  - The exact parameters that will be executed are always shown, never
 *    hidden behind a summary. The operator approves what will actually run.
 *  - High/critical actions require typing a confirmation before the Approve
 *    button arms, so a database restore cannot be triggered by a stray click
 *    in a list of routine items.
 */
export default function Approvals({
  refreshKey,
  onDecision,
}: {
  refreshKey: number
  onDecision: () => void
}) {
  const [actions, setActions] = useState<ActionOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    api
      .listActions()
      .then(setActions)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load, refreshKey])

  async function decide(id: string, decision: 'approve' | 'reject', reason?: string) {
    setPendingId(id)
    setError(null)
    try {
      if (decision === 'approve') await api.approveAction(id)
      else await api.rejectAction(id, reason)
      onDecision()
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPendingId(null)
    }
  }

  const pending = actions.filter((a) => a.status === 'proposed')

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Aprovações pendentes {pending.length > 0 && <span className="count">{pending.length}</span>}</h2>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          Atualizar
        </button>
      </div>

      {error && <div className="banner banner-error">{error}</div>}
      {loading && <p className="muted">Carregando…</p>}
      {!loading && pending.length === 0 && <p className="muted">Nenhuma ação aguardando aprovação.</p>}

      <div className="cards">
        {pending.map((action) => (
          <ApprovalCard
            key={action.id}
            action={action}
            busy={pendingId === action.id}
            onDecide={decide}
          />
        ))}
      </div>

      <RecentDecisions actions={actions.filter((a) => a.status !== 'proposed')} />
    </div>
  )
}

function ApprovalCard({
  action,
  busy,
  onDecide,
}: {
  action: ActionOut
  busy: boolean
  onDecide: (id: string, decision: 'approve' | 'reject', reason?: string) => void
}) {
  const highRisk = action.risk_level === 'high' || action.risk_level === 'critical'
  const [confirmText, setConfirmText] = useState('')
  const [reason, setReason] = useState('')
  // High/critical actions demand an explicit typed confirmation.
  const armed = !highRisk || confirmText.trim().toUpperCase() === 'CONFIRMO'

  return (
    <div className={`card risk-${action.risk_level}`}>
      <div className="card-head">
        <code>{action.tool_name}</code>
        <span className={`risk-badge risk-badge-${action.risk_level}`}>
          {action.risk_level.toUpperCase()}
        </span>
      </div>

      <p className="card-desc">{action.proposed_description}</p>

      <details className="params">
        <summary>Parâmetros exatos que serão executados</summary>
        <pre>{JSON.stringify(action.parameters, null, 2)}</pre>
      </details>

      {highRisk && (
        <div className="confirm-gate">
          <label>
            Ação de risco <strong>{action.risk_level}</strong>. Digite <code>CONFIRMO</code> para
            habilitar a aprovação:
          </label>
          <input
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder="CONFIRMO"
            disabled={busy}
          />
        </div>
      )}

      <input
        className="reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Motivo (opcional, registrado na auditoria)"
        disabled={busy}
      />

      <div className="card-actions">
        <button
          className="btn btn-danger"
          disabled={busy}
          onClick={() => onDecide(action.id, 'reject', reason || undefined)}
        >
          Rejeitar
        </button>
        <button
          className="btn btn-primary"
          disabled={busy || !armed}
          title={armed ? undefined : 'Digite CONFIRMO para habilitar'}
          onClick={() => onDecide(action.id, 'approve')}
        >
          {busy ? 'Executando…' : 'Aprovar e executar'}
        </button>
      </div>
    </div>
  )
}

function RecentDecisions({ actions }: { actions: ActionOut[] }) {
  if (actions.length === 0) return null
  return (
    <div className="recent">
      <h3>Decisões recentes</h3>
      <table>
        <thead>
          <tr>
            <th>Ferramenta</th>
            <th>Risco</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => (
            <tr key={a.id}>
              <td>
                <code>{a.tool_name}</code>
              </td>
              <td>{a.risk_level}</td>
              <td>
                <span className={`status status-${a.status}`}>{a.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
