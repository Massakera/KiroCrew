/**
 * CrewChatPage — talk to a remote crew, with nothing persisted locally.
 *
 * Layout is mockup option A (session rail + chat), chosen 2026-08-29.
 *
 * Everything here reads and writes the PEER's gateway through the instances
 * proxy: `AppApiProvider`'s `basePath` prefixes each request with
 * `/api/instances/<id>/proxy`, so the transcript, the slot list and the turn all
 * live on the crew. The hub keeps no copy — that is what makes the
 * remote-content-in-local-memory leak structurally impossible rather than
 * policy-dependent.
 *
 * The declared API scope is deliberately the same narrow pair the backend
 * allowlist forwards (`api/chat`, `api/stream`). `basePath` does not widen it —
 * see `createScopedApi` — so this page cannot reach the peer's control plane
 * even by accident.
 *
 * Two data paths, and they are not interchangeable:
 *   - the RAIL seeds from `GET /api/chat/slots` and then applies `slots` /
 *     `slot_title` deltas from the peer's `/api/stream` SSE. The seed is
 *     mandatory: `api_stream` hands a new client an EMPTY queue and emits only a
 *     `dashboard` heartbeat on connect, so a stream-only rail would start blank.
 *   - the TRANSCRIPT is ChatEmbed's own polling. Messages do not arrive over
 *     `/api/stream`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Plus, RefreshCw, Radio, Loader2 } from 'lucide-react'
import { AppApiProvider, useAppApi } from '../app-sdk'
import ChatEmbed from '../app-sdk/ChatEmbed'

/** The peer's `/api/stream` emits a `dashboard` heartbeat on this cadence. */
const HEARTBEAT_SECS = 5
/** Missed heartbeats before the view stops claiming to be live. */
const STALE_AFTER_MS = HEARTBEAT_SECS * 3 * 1000

/**
 * The slice of the peer's slot payload this view uses. The real payload carries
 * ~50 fields; narrowing here keeps the version-skew surface small — a peer on a
 * different edition can add or drop anything outside these without breaking the
 * rail.
 */
interface CrewSlot {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean
  last_message?: string
}

type StreamState = 'connecting' | 'live' | 'stale'

function isSlotArray(v: unknown): v is CrewSlot[] {
  return Array.isArray(v) && v.every(s => typeof s === 'object' && s !== null && typeof (s as CrewSlot).key === 'string')
}

export default function CrewChatPage() {
  const { crewId, sessionId } = useParams<{ crewId: string; sessionId?: string }>()
  // A missing :crewId cannot happen via the route, but an empty basePath would
  // silently address the LOCAL gateway — the one failure this design refuses to
  // make possible — so fail closed instead of proxying to ourselves.
  if (!crewId) {
    return <Unreachable crewId="" detail="No crew was named in the URL." onRetry={null} />
  }
  const basePath = `/api/instances/${encodeURIComponent(crewId)}/proxy`
  return (
    <AppApiProvider
      appName="remote-crew-chat"
      allowedApiPaths={['/api/chat', '/api/stream']}
      allowedEvents={[]}
      basePath={basePath}
      subscribeFn={() => () => {}}
      navigateFn={() => {}}
      notifyFn={() => {}}
    >
      <CrewChatInner crewId={crewId} sessionId={sessionId} basePath={basePath} />
    </AppApiProvider>
  )
}

function CrewChatInner({ crewId, sessionId, basePath }: { crewId: string; sessionId?: string; basePath: string }) {
  const api = useAppApi()
  const navigate = useNavigate()
  const [streamState, setStreamState] = useState<StreamState>('connecting')
  const [liveSlots, setLiveSlots] = useState<CrewSlot[] | null>(null)
  const lastBeat = useRef<number>(Date.now())

  const seed = useQuery({
    queryKey: ['crew-slots', crewId],
    queryFn: () => api.get<unknown>('/api/chat/slots'),
    retry: false,
  })

  const slots: CrewSlot[] = useMemo(() => {
    if (liveSlots) return liveSlots
    return isSlotArray(seed.data) ? seed.data : []
  }, [liveSlots, seed.data])

  // Peer event stream: rail liveness only. Deliberately NOT modelled on
  // `useSSE`, which reloads the SPA when the stream returns — correct for the
  // local gateway picking up a new build, wrong here, where a remote blip must
  // never reload the hub.
  useEffect(() => {
    const src = new EventSource(`${basePath}/api/stream`)
    const beat = () => { lastBeat.current = Date.now(); setStreamState('live') }
    src.addEventListener('open', beat)
    src.addEventListener('dashboard', beat)
    src.addEventListener('slots', e => {
      beat()
      try {
        const parsed: unknown = JSON.parse((e as MessageEvent<string>).data)
        if (isSlotArray(parsed)) setLiveSlots(parsed)
      } catch { /* a malformed frame must not kill the rail */ }
    })
    src.addEventListener('slot_title', e => {
      beat()
      try {
        const { key, title } = JSON.parse((e as MessageEvent<string>).data) as { key?: string; title?: string }
        if (!key) return
        setLiveSlots(prev => (prev ?? []).map(s => (s.key === key ? { ...s, title } : s)))
      } catch { /* ignore */ }
    })
    // EventSource reconnects on its own; surface the gap rather than hiding it.
    src.onerror = () => setStreamState('stale')
    const tick = window.setInterval(() => {
      if (Date.now() - lastBeat.current > STALE_AFTER_MS) setStreamState('stale')
    }, HEARTBEAT_SECS * 1000)
    return () => { window.clearInterval(tick); src.close() }
  }, [basePath])

  const selected = sessionId ?? slots[0]?.key
  const open = useCallback(
    (key: string) => navigate(`/crew/${encodeURIComponent(crewId)}/chat/${encodeURIComponent(key)}`),
    [navigate, crewId],
  )

  // Hard fail, never a silent local fallback: if the peer's slot list cannot be
  // read the crew is unreachable, and a composer that looked usable would send a
  // turn to the wrong machine.
  if (seed.isError) {
    return (
      <Unreachable
        crewId={crewId}
        detail={seed.error instanceof Error ? seed.error.message : 'The crew did not answer.'}
        onRetry={() => void seed.refetch()}
      />
    )
  }

  return (
    <div className="flex flex-col h-full min-h-0" data-testid="crew-chat-page">
      <header className="flex items-center gap-2 px-3 py-2 border-b border-border text-xs text-muted shrink-0">
        <span className="font-medium text-text-strong">{crewId}</span>
        <span>/ chat</span>
        <span className="flex-1" />
        <StreamChip state={streamState} />
      </header>

      <div className="flex flex-1 min-h-0">
        <aside className="w-[214px] shrink-0 border-r border-border flex flex-col min-h-0" aria-label="Sessions">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-[11px] uppercase tracking-wide text-muted">
            Sessions
            <button
              type="button"
              className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold"
              style={{ background: 'var(--accent)', color: 'var(--accent-fg)' }}
              onClick={() => navigate(`/crew/${encodeURIComponent(crewId)}/chat`)}
            >
              <Plus className="lucide-inline" /> New
            </button>
          </div>
          <div className="flex-1 overflow-y-auto">
            {seed.isLoading && <div className="px-3 py-2 text-xs text-muted">Loading sessions…</div>}
            {!seed.isLoading && slots.length === 0 && (
              <div className="px-3 py-2 text-xs text-muted">No sessions on this crew yet.</div>
            )}
            {slots.map(s => (
              <button
                type="button"
                key={s.key}
                onClick={() => open(s.key)}
                aria-current={s.key === selected ? 'true' : undefined}
                className="w-full text-left px-3 py-2 border-b border-border hover:bg-bg-hover"
                style={s.key === selected ? { background: 'var(--bg-elevated)', boxShadow: 'inset 2px 0 0 var(--accent)' } : undefined}
              >
                <div className="text-xs text-text-strong truncate">{s.title || s.key}</div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted">
                  {s.pending_approval ? (
                    <span
                      className="rounded-full px-1.5"
                      style={{ color: 'var(--warn, #f59e0b)', border: '1px solid var(--warn, #f59e0b)' }}
                    >
                      needs approval
                    </span>
                  ) : s.running ? (
                    <>
                      <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--accent)' }} />
                      running
                    </>
                  ) : (
                    <span className="truncate">{s.last_message || 'idle'}</span>
                  )}
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="flex-1 min-w-0 flex flex-col">
          {selected ? (
            <ChatEmbed
              key={selected}
              slotKey={selected}
              frameless
              startAtBottom
              placeholder={`Message ${crewId}…`}
              aboveComposer={streamState === 'stale' ? <StaleNotice /> : undefined}
            />
          ) : (
            <div className="flex-1 grid place-items-center text-xs text-muted">
              Pick a session, or start a new one.
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function StreamChip({ state }: { state: StreamState }) {
  if (state === 'live') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]"
        style={{ color: 'var(--accent)', border: '1px solid var(--accent)', background: 'var(--accent-subtle)' }}
      >
        <Radio className="lucide-inline" /> live
      </span>
    )
  }
  if (state === 'connecting') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted border border-border-strong">
        <Loader2 className="lucide-inline animate-spin" /> connecting…
      </span>
    )
  }
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]"
      style={{ color: 'var(--warn, #f59e0b)', border: '1px solid var(--warn, #f59e0b)' }}
    >
      reconnecting…
    </span>
  )
}

/**
 * Stream loss is NOT the same as an unreachable crew: the turn endpoint may be
 * perfectly healthy while the event stream is gone, so the composer stays usable
 * and the rail is merely flagged as possibly behind. Blanket-disabling here would
 * block a working send on a cosmetic signal.
 */
function StaleNotice() {
  return (
    <div
      className="mx-3 mb-2 rounded px-2.5 py-1.5 text-[11px]"
      style={{ color: 'var(--warn, #f59e0b)', border: '1px solid var(--warn, #f59e0b)' }}
    >
      Session list may be out of date — the crew's event stream dropped. Messages still send.
    </div>
  )
}

function Unreachable({ crewId, detail, onRetry }: { crewId: string; detail: string; onRetry: (() => void) | null }) {
  return (
    <div className="h-full grid place-items-center p-6" data-testid="crew-chat-unreachable">
      <div className="max-w-md text-center">
        <div className="text-sm text-text-strong">
          {crewId ? `Can't reach ${crewId}` : 'No crew selected'}
        </div>
        <p className="mt-1 text-xs text-muted">{detail}</p>
        <p className="mt-2 text-xs text-muted">
          This view always talks to the crew itself — it never falls back to running the turn locally.
        </p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 inline-flex items-center gap-1.5 rounded border border-border-strong px-2.5 py-1 text-xs"
          >
            <RefreshCw className="lucide-inline" /> Retry
          </button>
        )}
      </div>
    </div>
  )
}
