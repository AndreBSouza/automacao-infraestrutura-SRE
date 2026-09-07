import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  streamMessage,
  type StreamEvent,
  type ConversationOut,
  type StoredMessage,
} from '../api'

/**
 * Chat view with a conversation sidebar.
 *
 * Conversations persist per user (SPEC 14): the sidebar lists them, selecting
 * one loads its stored history, and a new one is only created when the
 * operator asks for it — so switching tabs or reloading the page returns to
 * the conversation in progress instead of silently starting a fresh one.
 *
 * The orchestrator's stream is rendered as it arrives, including the tool
 * calls it makes: the operator sees *what the agent looked at*, not just its
 * conclusion, which is what makes a diagnosis auditable. A proposed action is
 * surfaced as a distinct card rather than blending into prose, so it can never
 * be mistaken for something already done — approval happens in the Aprovações
 * tab (or from Teams/Slack).
 */

interface Trace {
  kind: 'tool_call' | 'tool_result' | 'action_proposed' | 'injection' | 'invalid_proposal'
  label: string
  detail?: string
  ok?: boolean
  risk?: string
}

interface Turn {
  question: string
  text: string
  traces: Trace[]
  streaming: boolean
  error?: string
  /** True for turns loaded from history rather than streamed in this session. */
  historical?: boolean
  /** Which model answered — shown only when escalated, so the reader knows. */
  model?: string
  escalated?: boolean
}

export default function Chat({ onActionProposed }: { onActionProposed: () => void }) {
  const [conversations, setConversations] = useState<ConversationOut[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [escalate, setEscalate] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Load the conversation list, resuming the most recent one rather than
  // creating a new conversation on every mount.
  useEffect(() => {
    api
      .listConversations()
      .then((list) => {
        setConversations(list)
        if (list.length > 0) setActiveId(list[0].id)
      })
      .catch((e) => setError(e.message))
  }, [])

  const loadHistory = useCallback((conversationId: string) => {
    setLoadingHistory(true)
    api
      .getMessages(conversationId)
      .then((messages) => setTurns(messagesToTurns(messages)))
      .catch((e) => setError(e.message))
      .finally(() => setLoadingHistory(false))
  }, [])

  useEffect(() => {
    if (activeId) loadHistory(activeId)
    else setTurns([])
  }, [activeId, loadHistory])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  async function newConversation() {
    setError(null)
    try {
      const conv = await api.createConversation()
      setConversations((c) => [conv, ...c])
      setActiveId(conv.id)
      setTurns([])
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function send() {
    const question = input.trim()
    if (!question || busy) return

    // First message with no conversation yet: create one on demand.
    let conversationId = activeId
    if (!conversationId) {
      try {
        const conv = await api.createConversation()
        setConversations((c) => [conv, ...c])
        setActiveId(conv.id)
        conversationId = conv.id
      } catch (e) {
        setError((e as Error).message)
        return
      }
    }

    setInput('')
    setBusy(true)
    setError(null)
    const index = turns.length
    setTurns((t) => [...t, { question, text: '', traces: [], streaming: true }])

    const patch = (fn: (t: Turn) => Turn) =>
      setTurns((all) => all.map((t, i) => (i === index ? fn(t) : t)))

    const wasEscalated = escalate
    try {
      for await (const ev of streamMessage(conversationId, question, { escalate: wasEscalated })) {
        applyEvent(ev, patch, onActionProposed)
      }
    } catch (e) {
      patch((t) => ({ ...t, error: (e as Error).message }))
    } finally {
      patch((t) => ({ ...t, streaming: false }))
      setBusy(false)
      // O escalonamento vale por mensagem: desarma sozinho depois de usado,
      // para o custo extra nunca ficar ligado por esquecimento.
      setEscalate(false)
    }
  }

  return (
    <div className="chat-layout">
      <aside className="conversations">
        <button className="btn btn-primary btn-block" onClick={newConversation}>
          + Nova conversa
        </button>
        <ul>
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                className={`conv-item ${c.id === activeId ? 'conv-active' : ''}`}
                onClick={() => setActiveId(c.id)}
              >
                <span className="conv-title">{c.title ?? 'Sem título'}</span>
                <span className="conv-date">
                  {new Date(c.created_at).toLocaleDateString('pt-BR', {
                    day: '2-digit',
                    month: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              </button>
            </li>
          ))}
          {conversations.length === 0 && <li className="muted small">Nenhuma conversa ainda.</li>}
        </ul>
      </aside>

      <div className="chat">
        {error && <div className="banner banner-error">{error}</div>}

        <div className="chat-log">
          {loadingHistory && <p className="muted">Carregando histórico…</p>}

          {!loadingHistory && turns.length === 0 && (
            <div className="empty">
              <p>Pergunte qualquer coisa sobre o ambiente.</p>
              <ul className="examples">
                <li>"Por que a CPU do servidor web-01 está alta?"</li>
                <li>"Quais bancos SQL estão sem backup há mais de 24h?"</li>
                <li>"Mostre os alertas críticos abertos no Zabbix"</li>
              </ul>
            </div>
          )}

          {turns.map((turn, i) => (
            <div key={i} className="turn">
              <div className="bubble bubble-user">{turn.question}</div>

              {turn.traces.length > 0 && (
                <div className="traces">
                  {turn.traces.map((tr, j) => (
                    <TraceRow key={j} trace={tr} />
                  ))}
                </div>
              )}

              <div className={`bubble bubble-assistant ${turn.escalated ? 'bubble-escalated' : ''}`}>
                {turn.escalated && (
                  <div className="escalated-tag" title={turn.model}>
                    Análise aprofundada
                  </div>
                )}
                {turn.text || (turn.streaming ? <span className="dots">…</span> : null)}
                {turn.error && <div className="inline-error">{turn.error}</div>}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            void send()
          }}
        >
          <div className="composer-row">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Pergunte sobre o ambiente…"
              disabled={busy}
            />
            <button className="btn btn-primary" disabled={busy || !input.trim()}>
              {busy ? 'Analisando…' : 'Enviar'}
            </button>
          </div>
          <label className={`escalate ${escalate ? 'escalate-on' : ''}`}>
            <input
              type="checkbox"
              checked={escalate}
              onChange={(e) => setEscalate(e.target.checked)}
              disabled={busy}
            />
            Análise aprofundada
            <span className="escalate-hint">
              usa o modelo mais capaz nesta pergunta — mais caro e mais lento; vale quando a
              resposta padrão não convenceu
            </span>
          </label>
        </form>
      </div>
    </div>
  )
}

/**
 * Folds a flat stored message list into question/answer turns. Tool traces are
 * not replayed from history — only the persisted text — so historical turns
 * show the conclusion without the intermediate steps.
 */
function messagesToTurns(messages: StoredMessage[]): Turn[] {
  const turns: Turn[] = []
  for (const m of messages) {
    if (m.role === 'user') {
      turns.push({
        question: m.content.text ?? '',
        text: '',
        traces: [],
        streaming: false,
        historical: true,
      })
    } else if (m.role === 'assistant' && turns.length > 0) {
      const turn = turns[turns.length - 1]
      turn.text = m.content.text ?? ''
      turn.model = m.content.model
      turn.escalated = m.content.escalated
    }
  }
  return turns
}

function applyEvent(
  ev: StreamEvent,
  patch: (fn: (t: Turn) => Turn) => void,
  onActionProposed: () => void,
) {
  const addTrace = (trace: Trace) => patch((t) => ({ ...t, traces: [...t.traces, trace] }))

  switch (ev.type) {
    case 'model':
      patch((t) => ({ ...t, model: ev.model, escalated: ev.escalated }))
      break
    case 'text_delta':
      patch((t) => ({ ...t, text: t.text + ev.text }))
      break
    case 'tool_call':
      addTrace({ kind: 'tool_call', label: ev.name, detail: JSON.stringify(ev.input) })
      break
    case 'tool_result':
      addTrace({ kind: 'tool_result', label: ev.name, ok: ev.ok })
      break
    case 'action_proposed':
      addTrace({
        kind: 'action_proposed',
        label: ev.tool_name,
        risk: ev.risk_level,
        detail: ev.action_id,
      })
      onActionProposed()
      break
    case 'action_rejected_invalid_proposal':
      addTrace({ kind: 'invalid_proposal', label: ev.tool_name, detail: ev.reason })
      break
    case 'injection_suspected':
      addTrace({ kind: 'injection', label: ev.tool_name, detail: ev.marker })
      break
    case 'final_text':
      patch((t) => ({ ...t, text: ev.text || t.text }))
      break
    case 'done':
      break
  }
}

function TraceRow({ trace }: { trace: Trace }) {
  if (trace.kind === 'action_proposed') {
    return (
      <div className={`trace trace-action risk-${trace.risk}`}>
        <strong>Ação proposta — aguardando aprovação</strong>
        <div>
          <code>{trace.label}</code> <span className="risk-badge">{trace.risk?.toUpperCase()}</span>
        </div>
        <div className="trace-hint">Aprove ou rejeite na aba "Aprovações".</div>
      </div>
    )
  }
  if (trace.kind === 'injection') {
    return (
      <div className="trace trace-warn">
        ⚠ Conteúdo suspeito de tentativa de injeção detectado no retorno de <code>{trace.label}</code>{' '}
        (marcador: {trace.detail}). Tratado como dado, não como instrução.
      </div>
    )
  }
  if (trace.kind === 'invalid_proposal') {
    return (
      <div className="trace trace-warn">
        Proposta inválida para <code>{trace.label}</code> recusada: {trace.detail}
      </div>
    )
  }
  if (trace.kind === 'tool_result') {
    return (
      <div className="trace trace-muted">
        {trace.ok ? '✓' : '✗'} <code>{trace.label}</code>
      </div>
    )
  }
  return (
    <div className="trace trace-muted" title={trace.detail}>
      → consultando <code>{trace.label}</code>
    </div>
  )
}
