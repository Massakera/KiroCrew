/**
 * ChatSurface — the properties that make ONE chat view serve two origins.
 *
 * The point of this file is the first test: the surface must issue LOGICAL paths
 * and let the provider decide which gateway answers. If someone later hardcodes
 * `/api/instances/...` inside the component, remote chat keeps working and local
 * chat silently breaks — so that regression has to fail here, loudly, rather than
 * in a future migration.
 *
 * The rest pin the parity affordances added with mockup option A: creating a
 * session actually POSTs (the previous button only navigated), Stop reaches the
 * gateway that is running the turn, and the activity timeline is fed by
 * `chat_message` frames rather than invented.
 *
 * `EventSource` is stubbed because jsdom never opens one.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppApiProvider } from '../app-sdk'
import ChatSurface from '../pages/chat/ChatSurface'

class FakeEventSource {
  static last: FakeEventSource | null = null
  url: string
  onerror: (() => void) | null = null
  private listeners: Record<string, ((e: MessageEvent<string>) => void)[]> = {}
  closed = false
  constructor(url: string) { this.url = url; FakeEventSource.last = this }
  addEventListener(t: string, cb: (e: MessageEvent<string>) => void) { (this.listeners[t] ||= []).push(cb) }
  close() { this.closed = true }
  emit(t: string, data?: string) {
    for (const cb of this.listeners[t] || []) cb({ data: data ?? '' } as MessageEvent<string>)
  }
}

const SLOTS = [{ key: 'dashboard-1', title: 'index rebuild', running: true, queue_depth: 2 }]

function stubFetch(extra?: (url: string) => Response | undefined) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    const hit = extra?.(u)
    if (hit) return hit
    if (u.endsWith('/stop')) return new Response('{}', { status: 200 })
    if (u.endsWith('/api/chat/slots') && (init?.method ?? 'GET') === 'GET') {
      return new Response(JSON.stringify(SLOTS), { status: 200 })
    }
    if (u.endsWith('/api/chat/slots')) {
      return new Response(JSON.stringify({ key: 'dashboard-9' }), { status: 200 })
    }
    // Slot DETAIL — this, not the list, is what ChatEmbed polls to decide
    // `running`, which in turn gates Stop and the activity line.
    if (u.includes('/api/chat/slots/')) {
      return new Response(JSON.stringify({ running: true, messages: [], title: 'index rebuild' }), { status: 200 })
    }
    return new Response('{}', { status: 200 })
  })
}

/** Mount the surface under a provider, with `basePath` as the only origin knob. */
function mount(basePath: string, onOpenSlot = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const r = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AppApiProvider
          appName="test-surface"
          allowedApiPaths={['/api/chat', '/api/stream']}
          allowedEvents={[]}
          basePath={basePath}
          subscribeFn={() => () => {}}
          navigateFn={() => {}}
          notifyFn={() => {}}
        >
          <ChatSurface
            origin={basePath ? { kind: 'crew', label: 'chick' } : { kind: 'local', label: 'Local' }}
            streamBase={basePath}
            slotKey="dashboard-1"
            onOpenSlot={onOpenSlot}
          />
        </AppApiProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...r, onOpenSlot }
}

describe('ChatSurface', () => {
  let fetchMock: ReturnType<typeof stubFetch>
  beforeEach(() => {
    fetchMock = stubFetch()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    FakeEventSource.last = null
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('is origin-agnostic: the SAME component addresses local or a crew purely by basePath', async () => {
    // This is the property that lets local chat adopt this view later. The
    // component must never build a proxy path itself.
    const remote = mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    expect(fetchMock.mock.calls.some(c => String(c[0]) === '/api/instances/chick/proxy/api/chat/slots')).toBe(true)
    remote.unmount()

    fetchMock.mockClear()
    mount('')
    await screen.findByText('index rebuild')
    expect(fetchMock.mock.calls.some(c => String(c[0]) === '/api/chat/slots')).toBe(true)
    // The give-away regression: a hardcoded proxy prefix would still appear here.
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/api/instances/'))).toBe(false)
  })

  it('creates a session on the answering gateway instead of only navigating', async () => {
    // The previous New button routed to a bare URL and created nothing, so the
    // rail never gained a row. It must POST and then open what came back.
    const { onOpenSlot } = mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    fireEvent.click(screen.getByTestId('surface-new-session'))
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        c => String(c[0]).endsWith('/api/chat/slots') && (c[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(post, 'New must POST /api/chat/slots').toBeDefined()
    })
    await waitFor(() => expect(onOpenSlot).toHaveBeenCalledWith('dashboard-9'))
  })

  it('reports a failed create inline instead of blanking the rail', async () => {
    // Fail only the POST; the GET seed must keep working.
    const failing = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url)
      if (u.endsWith('/api/chat/slots') && init?.method === 'POST') {
        return new Response('no room', { status: 500 })
      }
      if (u.endsWith('/api/chat/slots')) return new Response(JSON.stringify(SLOTS), { status: 200 })
      if (u.includes('/api/chat/slots/')) {
        return new Response(JSON.stringify({ running: true, messages: [] }), { status: 200 })
      }
      return new Response('{}', { status: 200 })
    })
    vi.stubGlobal('fetch', failing)
    mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    fireEvent.click(screen.getByTestId('surface-new-session'))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // The rail the user was reading is still there.
    expect(screen.getByText('index rebuild')).toBeInTheDocument()
  })

  it('stops the turn on the gateway that is running it', async () => {
    mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    // ChatEmbed renders Stop only while its own poll says the slot is running.
    const stop = await screen.findByRole('button', { name: /stop the current turn/i })
    fireEvent.click(stop)
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(c => String(c[0]).includes('/stop'))
      expect(call?.[0]).toBe('/api/instances/chick/proxy/api/chat/slots/dashboard-1/stop')
    })
  })

  it('builds the activity timeline from chat_message frames, and admits when it has none', async () => {
    mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    // Running comes from ChatEmbed's poll of the slot, which SLOTS marks running.
    const toggle = await screen.findByTestId('surface-activity-toggle')
    fireEvent.click(toggle)
    // Honest empty state before any tool frame arrives — never a fabricated row.
    expect(screen.getByText(/no tool activity received yet/i)).toBeInTheDocument()

    act(() => {
      FakeEventSource.last!.emit('chat_message', JSON.stringify({
        slot: 'dashboard-1', role: 'tool_call', content: 'shell kirocrew consolidate',
      }))
    })
    // The name appears twice by design — as the current-tool summary on the
    // collapsed line AND as a timeline row — so assert the transition, not a
    // single node: the empty state is gone and the tool is now named.
    await waitFor(() =>
      expect(screen.queryByText(/no tool activity received yet/i)).not.toBeInTheDocument(),
    )
    expect(screen.getAllByText(/shell kirocrew consolidate/).length).toBeGreaterThan(0)
  })

  it('ignores tool frames belonging to a different session', async () => {
    // Tool rows are per-turn per-slot; attributing another session's work to the
    // open one would be worse than showing nothing.
    mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    fireEvent.click(await screen.findByTestId('surface-activity-toggle'))
    act(() => {
      FakeEventSource.last!.emit('chat_message', JSON.stringify({
        slot: 'some-other-slot', role: 'tool_call', content: 'rm -rf somewhere',
      }))
    })
    expect(screen.queryByText(/rm -rf somewhere/)).not.toBeInTheDocument()
    expect(screen.getByText(/no tool activity received yet/i)).toBeInTheDocument()
  })

  it('keeps the origin chip labelled and enabled while every other chip disables', async () => {
    // Losing track of WHICH MACHINE a command lands on is a correctness failure,
    // so the identity chip never collapses or disables with the rest of the shelf.
    mount('/api/instances/chick/proxy')
    await screen.findByText('index rebuild')
    const chip = await screen.findByTestId('surface-origin-chip')
    expect(chip).toHaveTextContent('chick')
    expect(chip).not.toHaveAttribute('disabled')
  })
})
