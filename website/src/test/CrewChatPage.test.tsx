/**
 * CrewChatPage — the behaviours that make a PROXIED chat view safe, as opposed to
 * a chat view that happens to render.
 *
 * Four of these exist because the obvious implementation gets them wrong:
 *   - the rail must SEED from `GET /api/chat/slots`; the peer's `/api/stream`
 *     sends no snapshot on connect, so a stream-only rail starts blank forever;
 *   - every request must carry the `/api/instances/<id>/proxy` prefix, because a
 *     bare path silently addresses the LOCAL gateway — the one failure this
 *     design refuses to allow;
 *   - an unreadable slot list is a HARD fail with no composer, never a quiet
 *     fallback to running the turn locally;
 *   - a dropped event stream is NOT an unreachable crew, so it must not disable
 *     sending — and must not reload the SPA the way `useSSE` does for the local
 *     gateway.
 *
 * `EventSource` is stubbed (jsdom never opens one) so the stream handlers can be
 * driven directly, following the sibling specs' convention.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CrewChatPage from '../pages/CrewChatPage'
import { PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'

class FakeEventSource {
  static last: FakeEventSource | null = null
  url: string
  onerror: (() => void) | null = null
  private listeners: Record<string, ((e: MessageEvent<string>) => void)[]> = {}
  closed = false
  constructor(url: string) { this.url = url; FakeEventSource.last = this }
  addEventListener(type: string, cb: (e: MessageEvent<string>) => void) {
    (this.listeners[type] ||= []).push(cb)
  }
  close() { this.closed = true }
  emit(type: string, data?: string) {
    for (const cb of this.listeners[type] || []) {
      cb({ data: data ?? '' } as MessageEvent<string>)
    }
  }
}

/** ChatEmbed also fetches; answer anything it asks so it renders inertly. */
function stubFetch(slots: unknown, opts: { slotsStatus?: number } = {}) {
  return vi.fn(async (url: string) => {
    if (String(url).endsWith('/api/chat/slots')) {
      const status = opts.slotsStatus ?? 200
      return new Response(status === 200 ? JSON.stringify(slots) : 'peer said no', { status })
    }
    return new Response('{}', { status: 200 })
  })
}

function renderPage(path = '/crew/chick/chat') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/crew/:crewId/chat/:sessionId?" element={<CrewChatPage />} />
          {/* Where the hard preview gate sends an opted-out visitor. */}
          <Route path="/chat" element={<div data-testid="redirected-home" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const SLOTS = [
  { key: 'dashboard-1', title: 'proxy allowlist review', running: true },
  { key: 'dashboard-2', title: 'nightly build triage', pending_approval: true },
]

describe('CrewChatPage', () => {
  let fetchMock: ReturnType<typeof stubFetch>

  beforeEach(() => {
    // Every case below asserts the page RENDERS, so it must be past the hard
    // preview gate. The dedicated off-state case clears this again.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    fetchMock = stubFetch(SLOTS)
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    FakeEventSource.last = null
  })
  afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

  it('redirects out when the remote-crew-chat preview is off, never rendering the proxied view', () => {
    // Hard gate: an opted-out visitor (including a bookmarked URL) must not reach
    // the unreleased view at all. No slot fetch fires, and the router lands on
    // the redirect target instead of the chat page.
    localStorage.removeItem(PREVIEW_REMOTE_CREW_CHAT)
    renderPage()
    expect(screen.getByTestId('redirected-home')).toBeInTheDocument()
    expect(screen.queryByText('proxy allowlist review')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(c => String(c[0]).endsWith('/api/chat/slots'))).toBe(false)
  })

  it('seeds the rail from the slot list, because the stream sends no snapshot', async () => {
    renderPage()
    expect(await screen.findByText('proxy allowlist review')).toBeInTheDocument()
    expect(screen.getByText('nightly build triage')).toBeInTheDocument()
    const slotCall = fetchMock.mock.calls.find(c => String(c[0]).endsWith('/api/chat/slots'))
    expect(slotCall, 'the rail must fetch a seed, not wait on the stream').toBeDefined()
  })

  it('addresses the PEER through the proxy, never the local gateway', async () => {
    renderPage()
    await screen.findByText('proxy allowlist review')
    const slotCall = fetchMock.mock.calls.find(c => String(c[0]).includes('/api/chat/slots'))!
    expect(slotCall[0]).toBe('/api/instances/chick/proxy/api/chat/slots')
    expect(FakeEventSource.last?.url).toBe('/api/instances/chick/proxy/api/stream')
  })

  it('surfaces a pending approval on the rail without opening the session', async () => {
    // The state that blocks a turn from finishing has to be visible in the list;
    // that is the whole reason this layout keeps a rail.
    renderPage()
    expect(await screen.findByText('needs approval')).toBeInTheDocument()
  })

  it('applies a slots delta from the stream', async () => {
    renderPage()
    await screen.findByText('proxy allowlist review')
    act(() => {
      FakeEventSource.last!.emit('slots', JSON.stringify([
        { key: 'dashboard-1', title: 'renamed by the crew', running: false },
      ]))
    })
    expect(await screen.findByText('renamed by the crew')).toBeInTheDocument()
  })

  it('ignores a malformed stream frame instead of losing the rail', async () => {
    renderPage()
    await screen.findByText('proxy allowlist review')
    act(() => { FakeEventSource.last!.emit('slots', 'not json{') })
    expect(screen.getByText('proxy allowlist review')).toBeInTheDocument()
  })

  it('hard-fails with a retry when the slot list cannot be read', async () => {
    vi.stubGlobal('fetch', stubFetch(null, { slotsStatus: 502 }))
    renderPage()
    expect(await screen.findByTestId('chat-surface-unreachable')).toBeInTheDocument()
    expect(screen.getByText(/never silently falls back to another one/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('keeps sending available when only the event stream drops', async () => {
    // Stream loss is not an unreachable crew: the turn endpoint may be fine, so
    // the view flags staleness and stays usable rather than blocking a good send.
    renderPage()
    await screen.findByText('proxy allowlist review')
    act(() => { FakeEventSource.last!.onerror?.() })
    expect(await screen.findByText(/reconnecting/i)).toBeInTheDocument()
    expect(screen.queryByTestId('chat-surface-unreachable')).not.toBeInTheDocument()
    expect(screen.getByText(/Messages still send/i)).toBeInTheDocument()
  })

  it('does NOT reload the page when the stream recovers', async () => {
    // `useSSE` reloads on reconnect so the local gateway can pick up a new build.
    // Copying that here would let a remote blip reload the hub's whole SPA.
    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, reload },
    })
    try {
      renderPage()
      await screen.findByText('proxy allowlist review')
      act(() => { FakeEventSource.last!.onerror?.() })
      act(() => { FakeEventSource.last!.emit('dashboard', '{}') })
      expect(reload).not.toHaveBeenCalled()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })

  it('closes the stream on unmount so a navigation does not leak it', async () => {
    const { unmount } = renderPage()
    await screen.findByText('proxy allowlist review')
    const src = FakeEventSource.last!
    unmount()
    await waitFor(() => expect(src.closed).toBe(true))
  })
})
