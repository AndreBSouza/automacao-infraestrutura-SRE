/**
 * Typed client for the SAI backend.
 *
 * Auth: the app JWT is issued by /auth/callback and handed to the SPA in the
 * URL fragment. It is kept in sessionStorage (not localStorage) so it dies
 * with the tab rather than persisting on a shared operator workstation.
 */

const BASE = import.meta.env.VITE_API_BASE ?? '/api'
const TOKEN_KEY = 'sai.access_token'

// ---------------------------------------------------------------- types

export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'

export interface ActionOut {
  id: string
  tool_name: string
  risk_level: RiskLevel
  parameters: Record<string, unknown>
  proposed_description: string
  status: string
  requires_approval: boolean
}

export interface AlertOut {
  id: string
  source: string
  severity: 'info' | 'warning' | 'critical'
  summary: string
  diagnosis: string | null
  status: string
}

export interface InventoryItemOut {
  id: string
  source: string
  resource_type: string
  external_id: string
  name: string
  metadata: Record<string, unknown>
  tags: Record<string, unknown> | null
  owner_hint: string | null
}

export interface ConversationOut {
  id: string
  title: string | null
  created_at: string
}

/** A persisted message as returned by GET /conversations/{id}/messages. */
export interface StoredMessage {
  id: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: { text?: string; blocks?: unknown; model?: string; escalated?: boolean }
  created_at: string
}

/** Streaming events emitted by the orchestrator (sai/llm/orchestrator.py). */
export type StreamEvent =
  | { type: 'model'; model: string; escalated: boolean }
  | { type: 'text_delta'; text: string }
  | { type: 'tool_call'; name: string; input: Record<string, unknown> }
  | { type: 'tool_result'; name: string; ok: boolean }
  | { type: 'action_proposed'; action_id: string; tool_name: string; risk_level: RiskLevel }
  | { type: 'action_rejected_invalid_proposal'; tool_name: string; reason: string }
  | { type: 'injection_suspected'; marker: string; tool_name: string }
  | { type: 'final_text'; text: string }
  | { type: 'done' }

// ---------------------------------------------------------------- token

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

/**
 * Reads `#access_token=...` left by the backend's post-login redirect, stores
 * it, and scrubs it from the address bar so it is not left in the URL or in
 * browser history.
 */
export function consumeTokenFromFragment(): void {
  if (!window.location.hash) return
  const params = new URLSearchParams(window.location.hash.slice(1))
  const token = params.get('access_token')
  if (token) {
    setToken(token)
    history.replaceState(null, '', window.location.pathname + window.location.search)
  }
}

export function login(): void {
  window.location.href = `${BASE}/auth/login`
}

// ---------------------------------------------------------------- fetch

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  if (res.status === 401) {
    clearToken()
    throw new ApiError(401, 'Sessão expirada. Faça login novamente.')
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, detail.detail ?? res.statusText)
  }
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------- endpoints

export const api = {
  listActions: () => request<ActionOut[]>('/actions'),
  approveAction: (id: string) => request<ActionOut>(`/actions/${id}/approve`, { method: 'POST' }),
  rejectAction: (id: string, reason?: string) =>
    request<ActionOut>(`/actions/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  listAlerts: () => request<AlertOut[]>('/alerts'),

  listInventory: (source?: string) =>
    request<InventoryItemOut[]>(`/inventory${source ? `?source=${encodeURIComponent(source)}` : ''}`),
  syncInventory: () => request<{ status: string }>('/inventory/sync', { method: 'POST' }),

  listConversations: () => request<ConversationOut[]>('/conversations'),
  createConversation: (title?: string) =>
    request<ConversationOut>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title: title ?? null }),
    }),
  getMessages: (id: string) => request<StoredMessage[]>(`/conversations/${id}/messages`),

  /**
   * Downloads a report. The file is fetched with the auth header (a plain
   * <a href> could not carry the bearer token) and handed to the browser as a
   * temporary object URL, which is revoked immediately afterwards.
   */
  downloadReport: async (
    report: 'incidents.xlsx' | 'actions.xlsx' | 'summary.pdf',
    params: { days?: number; year?: number; month?: number },
  ): Promise<void> => {
    const qs = new URLSearchParams()
    if (params.year && params.month) {
      qs.set('year', String(params.year))
      qs.set('month', String(params.month))
    } else {
      qs.set('days', String(params.days ?? 30))
    }

    const token = getToken()
    const res = await fetch(`${BASE}/reports/${report}?${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }))
      throw new ApiError(res.status, detail.detail ?? res.statusText)
    }

    const blob = await res.blob()
    const filename =
      res.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] ?? report

    const url = URL.createObjectURL(blob)
    try {
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
    } finally {
      URL.revokeObjectURL(url)
    }
  },
}

/**
 * Sends a chat message and yields the backend's newline-delimited JSON events
 * as they arrive. A partial line at the end of a chunk is held over until the
 * rest of it arrives, so an event split across chunk boundaries is not lost.
 */
export async function* streamMessage(
  conversationId: string,
  content: string,
  options: { escalate?: boolean; signal?: AbortSignal } = {},
): AsyncGenerator<StreamEvent> {
  const { escalate = false, signal } = options
  const token = getToken()
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages`, {
    method: 'POST',
    signal,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ content, escalate }),
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, `Falha ao enviar mensagem (HTTP ${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let newlineIndex: number
    while ((newlineIndex = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, newlineIndex).trim()
      buffer = buffer.slice(newlineIndex + 1)
      if (line) yield JSON.parse(line) as StreamEvent
    }
  }
  const tail = buffer.trim()
  if (tail) yield JSON.parse(tail) as StreamEvent
}
