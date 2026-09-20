/**
 * ChatSurface — the chat view, with its ORIGIN as a parameter.
 *
 * This component contains no notion of "remote". Every request it makes is a
 * LOGICAL path (`/api/chat/slots`, never `/api/instances/<id>/proxy/...`); which
 * gateway answers is decided entirely by the `AppApiProvider` the host wraps it
 * in. A remote host supplies `basePath='/api/instances/<id>/proxy'`; a local host
 * supplies nothing at all and the same code addresses the local gateway.
 *
 * That is the whole point of the split: the divergence between "chat with a
 * crew" and "chat locally" was a maintenance problem, so origin is data, not a
 * fork in the component tree. `origin` below is presentational only — it decides
 * what the identity chip says, never where a request goes.
 *
 * Layout is mockup option A + the expandable timeline, chosen 2026-08-29:
 * ChatEmbed owns the transcript and composer; this file supplies the activity
 * line above it (via `aboveComposer`) and the context shelf below it (via
 * `belowComposer`), matching the main chat's real anatomy where the shelf is a
 * full-width row UNDER the input rather than pills inside it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, ChevronDown, FolderOpen, Loader2, Plus, Radio, RefreshCw, Server } from 'lucide-react'
import { useAppApi } from '../../app-sdk'
import ChatEmbed from '../../app-sdk/ChatEmbed'
import { i18nT } from '../../i18n/t'
import { fmtUnit } from '../../i18n/format'

/** Cadence of the `dashboard` heartbeat on the gateway's `/api/stream`. */
const HEARTBEAT_SECS = 5
/** Missed heartbeats before the view stops claiming to be live. */
const STALE_AFTER_MS = HEARTBEAT_SECS * 3 * 1000
/** Tool rows kept for the timeline. Enough to show the shape of a turn. */
const TIMELINE_CAP = 8

/**
 * The slice of the slot payload this view uses. The real payload carries ~54
 * fields; narrowing keeps the version-skew surface small, which matters when the
 * answering gateway may be a different edition than the one serving this bundle.
 */
export interface SurfaceSlot {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean
  queue_depth?: number
  last_message?: string
  agent?: string
  model?: string
  project?: string
}

export type StreamState = 'connecting' | 'live' | 'stale' | 'unavailable'

/** Where this surface is pointed. Presentational only — never a request path. */
export interface SurfaceOrigin {
  /** 'crew' shows the accented identity chip; 'local' shows a neutral one. */
  kind: 'local' | 'crew'
  /** Display name: a crew id, or "Local". */
  label: string
}

export interface ChatSurfaceProps {
  origin: SurfaceOrigin
  /** Logical prefix for the event stream ONLY. Empty string = same origin. */
  streamBase: string
  /** Currently open slot, or undefined to show the picker placeholder. */
  slotKey?: string
  /** Navigate to a slot. The host owns routing. */
  onOpenSlot: (key: string) => void
}

function isSlotArray(v: unknown): v is SurfaceSlot[] {
  return Array.isArray(v) && v.every(s => typeof s === 'object' && s !== null && typeof (s as SurfaceSlot).key === 'string')
}

/** One tool row in the activity timeline, derived from `chat_message` frames. */
interface ToolEvent {
  id: number
  name: string
  done: boolean
}

export default function ChatSurface({ origin, streamBase, slotKey, onOpenSlot }: ChatSurfaceProps) {
  const api = useAppApi()
  const [streamState, setStreamState] = useState<StreamState>('connecting')
  const [liveSlots, setLiveSlots] = useState<SurfaceSlot[] | null>(null)
  const [tools, setTools] = useState<ToolEvent[]>([])
  const [timelineOpen, setTimelineOpen] = useState(false)
  const [running, setRunning] = useState(false)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [now, setNow] = useState(Date.now())
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const lastBeat = useRef<number>(Date.now())
  const toolSeq = useRef(0)

  const seed = useQuery({
    queryKey: ['chat-surface-slots', origin.kind, origin.label],
    queryFn: () => api.get<unknown>('/api/chat/slots'),
    retry: false,
  })

  const slots: SurfaceSlot[] = useMemo(
    () => (liveSlots ? liveSlots : isSlotArray(seed.data) ? seed.data : []),
    [liveSlots, seed.data],
  )
  // Landing without an explicit slot opens the first one rather than showing an
  // empty pane: arriving at the surface and seeing nothing reads as broken, and
  // the rail is right there to switch with. `open` is what the rest of this
  // component keys off — never the raw prop — so the timeline, the shelf and the
  // stream filter all agree on which session is on screen.
  const openKey = slotKey ?? slots[0]?.key
  const active = useMemo(() => slots.find(s => s.key === openKey), [slots, openKey])

  // Event stream: rail freshness and the tool timeline. Deliberately NOT the
  // shared `useSSE` hook, which reloads the SPA when the stream returns — right
  // for a local gateway that just shipped a new build, wrong for a peer, where a
  // remote blip must never reload the hub out from under the user.
  useEffect(() => {
    const src = new EventSource(`${streamBase}/api/stream`)
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
        if (key) setLiveSlots(prev => (prev ?? []).map(s => (s.key === key ? { ...s, title } : s)))
      } catch { /* ignore */ }
    })
    // `chat_message` is the documented row-level progress channel: a tool_call is
    // "tool started" and the matching tool_result is "finished". Token-level and
    // thinking deltas are NOT here — they are WebSocket-only on the gateway and
    // cannot cross the instances proxy, so this timeline shows tool granularity
    // and never pretends to show reasoning.
    src.addEventListener('chat_message', e => {
      beat()
      try {
        const f = JSON.parse((e as MessageEvent<string>).data) as { slot?: string; role?: string; content?: string }
        if (!openKey || f.slot !== openKey) return
        if (f.role === 'tool_call') {
          toolSeq.current += 1
          const id = toolSeq.current
          const name = (f.content || 'tool').split('\n')[0].slice(0, 48)
          setTools(prev => [...prev, { id, name, done: false }].slice(-TIMELINE_CAP))
        } else if (f.role === 'tool_result') {
          setTools(prev => {
            const i = [...prev].reverse().findIndex(t => !t.done)
            if (i === -1) return prev
            const at = prev.length - 1 - i
            return prev.map((t, n) => (n === at ? { ...t, done: true } : t))
          })
        }
      } catch { /* ignore */ }
    })
    // EventSource cannot see the response status, so a policy refusal and a
    // network blip both surface as `onerror` and it retries forever either way.
    // Probe once to tell them apart: a 4xx (the proxy refusing a path outside
    // its allowlist) is TERMINAL, and claiming to reconnect from it is a promise
    // the view cannot keep.
    src.onerror = () => {
      setStreamState(prev => (prev === 'unavailable' ? prev : 'stale'))
      void fetch(`${streamBase}/api/stream`, { method: 'GET' })
        .then(r => { if (r.status >= 400 && r.status < 500) setStreamState('unavailable') })
        .catch(() => { /* transport failure: a blip, so leave it retrying */ })
    }
    const tick = window.setInterval(() => {
      if (Date.now() - lastBeat.current > STALE_AFTER_MS) setStreamState('stale')
    }, HEARTBEAT_SECS * 1000)
    return () => { window.clearInterval(tick); src.close() }
  }, [streamBase, openKey])

  // Switching sessions clears the timeline: tool rows belong to one turn on one
  // slot, and carrying them across would attribute another session's work here.
  useEffect(() => { setTools([]); setTimelineOpen(false) }, [openKey])

  // Elapsed ticks only while a turn is live, so an idle tab is not re-rendering
  // once a second forever.
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [running])

  const onRunningChange = useCallback((r: boolean) => {
    setRunning(r)
    setStartedAt(r ? Date.now() : null)
    if (r) setTools([])
  }, [])

  /** Create a session ON THE ANSWERING GATEWAY, then open it. */
  const createSession = useCallback(async () => {
    setCreating(true)
    setCreateError(null)
    try {
      const created = await api.post<{ key?: string }>('/api/chat/slots', {})
      await seed.refetch()
      if (created?.key) onOpenSlot(created.key)
      else setCreateError(i18nT('pages.chat.chatSurface.create_no_key'))
    } catch (err) {
      // Surfaced inline rather than thrown: a failed create must not blank the
      // rail the user is still reading.
      setCreateError(err instanceof Error ? err.message : i18nT('pages.chat.chatSurface.create_failed'))
    } finally {
      setCreating(false)
    }
  }, [api, seed, onOpenSlot])

  const stopTurn = useCallback(async () => {
    if (!openKey) return
    await api.post(`/api/chat/slots/${encodeURIComponent(openKey)}/stop`, {})
  }, [api, openKey])

  // Hard fail, never a silent fallback: if the slot list cannot be read the
  // gateway is unreachable, and a composer that still looked usable would invite
  // sending a turn that goes nowhere — or worse, somewhere else.
  if (seed.isError) {
    return (
      <Unreachable
        label={origin.label}
        detail={seed.error instanceof Error ? seed.error.message : i18nT('pages.chat.chatSurface.gateway_no_answer')}
        onRetry={() => void seed.refetch()}
      />
    )
  }

  const elapsed = running && startedAt ? Math.max(0, Math.round((now - startedAt) / 1000)) : 0

  return (
    <div className="flex flex-col h-full min-h-0" data-testid="chat-surface">
      <header className="flex items-center gap-2 px-3 py-2 border-b border-border text-xs text-muted shrink-0">
        <span className="font-medium text-text-strong">{origin.label}</span>
        <span>/ chat</span>
        <span className="flex-1" />
        <StreamChip state={streamState} />
      </header>

      <div className="flex flex-1 min-h-0">
        <aside className="w-[214px] shrink-0 border-r border-border flex flex-col min-h-0" aria-label={i18nT('pages.chatSidebar.sessions')}>
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-[11px] uppercase tracking-wide text-muted">
            {i18nT('pages.chatSidebar.sessions')}
            <button
              type="button"
              data-testid="surface-new-session"
              disabled={creating}
              className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold disabled:opacity-50"
              style={{ background: 'var(--accent)', color: 'var(--accent-fg)' }}
              onClick={() => void createSession()}
            >
              {creating ? <Loader2 className="lucide-inline animate-spin" /> : <Plus className="lucide-inline" />} {i18nT('pages.chatSidebar.new')}
            </button>
          </div>
          {createError && (
            <div className="px-3 py-1.5 text-[11px]" style={{ color: 'var(--danger)' }} role="alert">
              {createError}
            </div>
          )}
          <div className="flex-1 overflow-y-auto">
            {seed.isLoading && <div className="px-3 py-2 text-xs text-muted">{i18nT('pages.chat.chatSurface.loading_sessions')}</div>}
            {!seed.isLoading && slots.length === 0 && (
              // The empty state names the button next to it, so it interpolates
              // that button's OWN label rather than spelling it again — a second
              // spelling is what drifts when the control is renamed or translated.
              <div className="px-3 py-2 text-xs text-muted">
                {i18nT('pages.chat.chatSurface.no_sessions_yet', { action: i18nT('pages.chatSidebar.new') })}
              </div>
            )}
            {slots.map(s => (
              <button
                type="button"
                key={s.key}
                onClick={() => onOpenSlot(s.key)}
                aria-current={s.key === openKey ? 'true' : undefined}
                className="w-full text-left px-3 py-2 border-b border-border hover:bg-bg-hover"
                style={s.key === openKey ? { background: 'var(--bg-elevated)', boxShadow: 'inset 2px 0 0 var(--accent)' } : undefined}
              >
                <div className="text-xs text-text-strong truncate">{s.title || s.key}</div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted">
                  {s.pending_approval ? (
                    <span className="rounded-full px-1.5" style={{ color: 'var(--warn)', border: '1px solid var(--warn)' }}>
                      needs approval
                    </span>
                  ) : s.running ? (
                    <>
                      <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--accent)' }} />
                      running
                      {!!s.queue_depth && <span>· {s.queue_depth} queued</span>}
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
          {openKey ? (
            <ChatEmbed
              key={openKey}
              slotKey={openKey}
              frameless
              startAtBottom
              placeholder={i18nT('pages.chat.chatSurface.placeholder_message', { label: origin.label })}
              onStop={stopTurn}
              onRunningChange={onRunningChange}
              aboveComposer={
                <>
                  {(streamState === 'stale' || streamState === 'unavailable') && (
                    <StaleNotice terminal={streamState === 'unavailable'} />
                  )}
                  {running && (
                    <ActivityLine
                      tools={tools}
                      elapsed={elapsed}
                      open={timelineOpen}
                      onToggle={() => setTimelineOpen(o => !o)}
                      queued={active?.queue_depth ?? 0}
                    />
                  )}
                </>
              }
              belowComposer={<ContextShelf origin={origin} slot={active} running={running} />}
            />
          ) : (
            <div className="flex-1 grid place-items-center text-xs text-muted">
              {i18nT('pages.chat.chatSurface.pick_a_session')}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

/**
 * The activity line: one row while a turn runs, expanding into the tool
 * timeline on click. Collapsed it answers "is anything happening"; expanded it
 * answers "what has it done so far".
 */
function ActivityLine({
  tools, elapsed, open, onToggle, queued,
}: { tools: ToolEvent[]; elapsed: number; open: boolean; onToggle: () => void; queued: number }) {
  const current = [...tools].reverse().find(t => !t.done)
  return (
    <div className="mx-3 mb-2 rounded border border-border" style={{ borderLeft: '2px solid var(--accent)', background: 'var(--bg-accent)' }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        data-testid="surface-activity-toggle"
        className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] font-mono text-muted text-left"
      >
        <Loader2 className="lucide-inline animate-spin" style={{ color: 'var(--accent)' }} />
        <span className="text-text truncate">{current ? current.name : i18nT('pages.chat.chatSurface.working')}</span>
        <span className="ml-auto flex items-center gap-2">
          {queued > 0 && <span>{queued} queued</span>}
          <span>{fmtUnit(elapsed, 'second', { maximumFractionDigits: 0 })}</span>
          {tools.length > 0 && <span>· {tools.length} tools</span>}
          <ChevronDown className="lucide-inline" style={{ transform: open ? 'rotate(180deg)' : undefined }} />
        </span>
      </button>
      {open && (
        <div className="border-t border-border px-2.5 py-1.5 flex flex-col gap-1">
          {tools.length === 0 ? (
            // Honest empty state: the timeline is fed by the gateway's event
            // stream, so "no rows" means no tool frames have arrived, which also
            // covers the case where the stream is not reachable at all.
            <div className="text-[11px] font-mono text-muted">
              {i18nT('pages.chat.chatSurface.no_tool_activity')}
            </div>
          ) : (
            tools.map(t => (
              <div key={t.id} className="flex items-center gap-2 text-[11px] font-mono text-muted">
                {t.done
                  ? <span style={{ color: 'var(--accent)' }}>✓</span>
                  : <Loader2 className="lucide-inline animate-spin" />}
                <span className="text-text truncate">{t.name}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The context shelf: a plain full-width row BELOW the input, mirroring the main
 * chat's own anatomy (bindings left, state right) rather than inventing pills
 * inside the composer.
 *
 * The identity chip is deliberately different from the others: it renders in
 * every state, keeps its label when the shelf is narrow, and is never disabled.
 * Losing track of which machine a command lands on is a correctness failure, not
 * a cosmetic one. Every other chip follows the main chat's rule and goes
 * disabled while a turn is running.
 */
function ContextShelf({ origin, slot, running }: { origin: SurfaceOrigin; slot?: SurfaceSlot; running: boolean }) {
  const chip = 'inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md border-none bg-transparent text-[12px] font-mono text-muted transition-colors'
  // These chips REPORT the slot's bindings; they are not yet controls. Rendering
  // them as <button> made them read as actionable and then swallow the click, so
  // they are plain text that dims while a turn runs (matching the main chat's
  // disabled look) until the pickers are genuinely wired — see ChatInput, whose
  // real model/agent dropdowns this surface should adopt rather than reimplement.
  const runDim = running ? { opacity: 0.45 } : undefined
  const project = slot?.project ? slot.project.split('/').filter(Boolean).pop() : undefined
  return (
    <div className="flex items-center gap-0.5 px-3 pb-2 pt-1.5 min-h-[34px]" data-testid="surface-context-shelf">
      <span
        className={chip}
        data-testid="surface-origin-chip"
        style={origin.kind === 'crew' ? { color: 'var(--accent)' } : undefined}
        title={origin.kind === 'crew'
          ? i18nT('pages.chat.chatSurface.commands_run_on', { label: origin.label })
          : i18nT('pages.chat.chatSurface.commands_run_here')}
      >
        <Server className="lucide-inline" />
        <span className="truncate max-w-[160px]">{origin.label}</span>
      </span>
      {slot?.agent && (
        <span className={chip} style={runDim} title={i18nT('components.jobForm.agent')}>
          <Bot className="lucide-inline" />
          <span className="truncate max-w-[160px]">{slot.agent}</span>
        </span>
      )}
      {project && (
        <span className={chip} style={runDim} title={slot?.project}>
          <FolderOpen className="lucide-inline" />
          <span className="truncate max-w-[160px]">{project}</span>
        </span>
      )}
      <span className="ml-auto flex items-center gap-0.5">
        {slot?.model && (
          <span className={chip} style={runDim} title={i18nT('components.chatInput.model')}>
            <span className="truncate max-w-[160px]">{slot.model}</span>
          </span>
        )}
      </span>
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
        <Loader2 className="lucide-inline animate-spin" /> {i18nT('pages.chat.chatSurface.connecting')}
      </span>
    )
  }
  if (state === 'unavailable') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]"
        style={{ color: 'var(--text-muted)', border: '1px solid var(--border-strong)' }}
        title={i18nT('pages.chat.chatSurface.live_updates_unavailable_title')}
      >
        live updates unavailable
      </span>
    )
  }
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]"
      style={{ color: 'var(--warn)', border: '1px solid var(--warn)' }}
    >
      {i18nT('pages.chat.chatSurface.reconnecting')}
    </span>
  )
}

/**
 * Stream loss is NOT an unreachable gateway: the turn endpoint may be perfectly
 * healthy while the event stream is gone, so the composer stays usable and only
 * the rail is flagged as possibly behind. Blanket-disabling the composer here
 * would block a working send on a cosmetic signal.
 */
function StaleNotice({ terminal }: { terminal?: boolean }) {
  return (
    <div className="mx-3 mb-2 rounded px-2.5 py-1.5 text-[11px]" style={{ color: 'var(--warn)', border: '1px solid var(--warn)' }}>
      {terminal
        ? i18nT('pages.chat.chatSurface.stale_terminal')
        : i18nT('pages.chat.chatSurface.stale_dropped')}
    </div>
  )
}

export function Unreachable({ label, detail, onRetry }: { label: string; detail: string; onRetry: (() => void) | null }) {
  return (
    <div className="h-full grid place-items-center p-6" data-testid="chat-surface-unreachable">
      <div className="max-w-md text-center">
        <div className="text-sm text-text-strong">
          {label
            ? i18nT('pages.chat.chatSurface.cannot_reach', { label })
            : i18nT('pages.chat.chatSurface.no_gateway_selected')}
        </div>
        <p className="mt-1 text-xs text-muted">{detail}</p>
        <p className="mt-2 text-xs text-muted">
          {i18nT('pages.chat.chatSurface.never_falls_back')}
        </p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 inline-flex items-center gap-1.5 rounded border border-border-strong px-2.5 py-1 text-xs"
          >
            <RefreshCw className="lucide-inline" /> {i18nT('components.chatPane.retry')}
          </button>
        )}
      </div>
    </div>
  )
}
