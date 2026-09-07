import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearToken,
  consumeTokenFromFragment,
  getToken,
  setToken,
  streamMessage,
  type StreamEvent,
} from '../api'

describe('token handling', () => {
  beforeEach(() => {
    sessionStorage.clear()
    history.replaceState(null, '', '/')
  })

  it('stores and clears the token', () => {
    expect(getToken()).toBeNull()
    setToken('abc123')
    expect(getToken()).toBe('abc123')
    clearToken()
    expect(getToken()).toBeNull()
  })

  it('reads the token from the URL fragment and scrubs it from the address bar', () => {
    history.replaceState(null, '', '/#access_token=tok-from-login')
    consumeTokenFromFragment()

    expect(getToken()).toBe('tok-from-login')
    // The token must not linger in the URL or in browser history.
    expect(window.location.hash).toBe('')
  })

  it('ignores a fragment that carries no token', () => {
    history.replaceState(null, '', '/#something-else=1')
    consumeTokenFromFragment()
    expect(getToken()).toBeNull()
  })

  it('uses sessionStorage, not localStorage, so the token dies with the tab', () => {
    setToken('abc123')
    expect(localStorage.getItem('sai.access_token')).toBeNull()
    expect(sessionStorage.getItem('sai.access_token')).toBe('abc123')
  })
})

describe('streamMessage NDJSON parsing', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function mockStream(chunks: string[]) {
    const encoder = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(encoder.encode(c))
        controller.close()
      },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(body, { status: 200 })),
    )
  }

  async function collect(): Promise<StreamEvent[]> {
    const events: StreamEvent[] = []
    for await (const ev of streamMessage('conv-1', 'oi')) events.push(ev)
    return events
  }

  it('parses one event per line', async () => {
    mockStream([
      '{"type":"text_delta","text":"Olá"}\n',
      '{"type":"final_text","text":"Olá"}\n{"type":"done"}\n',
    ])
    const events = await collect()
    expect(events.map((e) => e.type)).toEqual(['text_delta', 'final_text', 'done'])
  })

  it('reassembles an event split across chunk boundaries', async () => {
    // The critical case: a JSON object cut in half by the network.
    mockStream(['{"type":"text_de', 'lta","text":"parcial"}\n'])
    const events = await collect()
    expect(events).toEqual([{ type: 'text_delta', text: 'parcial' }])
  })

  it('emits a trailing event that arrives without a final newline', async () => {
    mockStream(['{"type":"done"}'])
    const events = await collect()
    expect(events).toEqual([{ type: 'done' }])
  })

  it('throws when the backend rejects the request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 500 })))
    await expect(collect()).rejects.toThrow()
  })
})
