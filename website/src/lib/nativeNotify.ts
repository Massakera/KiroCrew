/**
 * Native (OS) notification posting that works from an embedded instance pane.
 *
 * An instance pane is the full dashboard SPA inside a cross-origin <iframe>
 * (`InstancesViewport.srcFor`). In that frame `Notification.permission` reads
 * `'denied'`: the desktop's permission handler grants `notifications` to the
 * main frame only (`website/electron/permission-handler.js`,
 * MAIN_FRAME_ONLY_PERMISSIONS), and a browser tab denies it to a cross-origin
 * iframe on its own. So a page-context `new Notification(...)` in a pane is a
 * silent no-op, and a remote crew's approvals and finished turns never reach
 * the OS banner.
 *
 * The frame that DOES hold the grant is the parent. This module relays the note
 * there instead of widening the permission: an embedded pane posts an
 * `mc-native-notify` envelope to `window.parent`, and the parent
 * (`InstancesViewport` onMessage) validates the sender's origin against its
 * currently-warm tunnel ports (`resolveTunnelOrigin`) before constructing the
 * `Notification` itself. The pane keeps every one of its own guards (mute rule,
 * `document.hidden`, `silent`) -- only a note the pane would have shown is
 * relayed, so no policy is duplicated here. The parent is the gate.
 *
 * Three call sites: `hooks/useNativeNotification.ts` (bell notes),
 * `hooks/useWebSocket.ts` (approval required, chat finished).
 */
import { isEmbeddedPane } from './embedded'

export const NATIVE_NOTIFY_TYPE = 'mc-native-notify'
export const NATIVE_NOTIFY_VERSION = 1

/** Bounds on the relayed strings; a banner never needs more. */
export const NATIVE_NOTIFY_MAX_TITLE = 200
export const NATIVE_NOTIFY_MAX_BODY = 1000
export const NATIVE_NOTIFY_MAX_TAG = 200

export interface NativeNotifyOptions {
  body?: string
  tag?: string
  /** OS sound. Defaults to true: WebAudio is the single source of sound. */
  silent?: boolean
  /** Pane-local URL; not relayed (the parent shows its own app icon). */
  icon?: string
}

/** Wire shape a pane posts to its parent. */
export interface NativeNotifyEnvelope {
  type: typeof NATIVE_NOTIFY_TYPE
  v: typeof NATIVE_NOTIFY_VERSION
  title: string
  body: string
  tag: string
  silent: boolean
}

/**
 * Whether a call site may proceed to post a native notification.
 *
 * Embedded: always true -- the pane's own permission is irrelevant (it is
 * denied by design) and the parent applies its own `Notification.permission`
 * check before posting. Top-level: the usual granted check.
 */
export function nativeNotificationPermitted(): boolean {
  if (isEmbeddedPane()) return true
  return typeof Notification !== 'undefined' && Notification.permission === 'granted'
}

/**
 * The parent's origin when the browser tells us (an iframe's `document.referrer`
 * is the embedding page), else `'*'`. Same narrowing as the unread-count relay
 * in `store/dashboardSlice.ts`: a note body is user content, so avoid
 * broadcasting it wider than the hub that embedded us.
 */
function parentTargetOrigin(): string {
  try {
    if (document.referrer) return new URL(document.referrer).origin
  } catch {
    /* keep '*' */
  }
  return '*'
}

/**
 * Post a native notification, or relay it to the parent when embedded.
 *
 * Never throws: Android Chrome throws "Illegal constructor" for page-context
 * Notification even with permission granted, and an uncaught throw on the
 * WebSocket message path kills the rest of the handler.
 */
export function postNativeNotification(title: string, options: NativeNotifyOptions = {}): void {
  const silent = options.silent ?? true
  if (isEmbeddedPane()) {
    try {
      const envelope: NativeNotifyEnvelope = {
        type: NATIVE_NOTIFY_TYPE,
        v: NATIVE_NOTIFY_VERSION,
        title: String(title).slice(0, NATIVE_NOTIFY_MAX_TITLE),
        body: String(options.body ?? '').slice(0, NATIVE_NOTIFY_MAX_BODY),
        tag: String(options.tag ?? '').slice(0, NATIVE_NOTIFY_MAX_TAG),
        silent,
      }
      window.parent?.postMessage(envelope, parentTargetOrigin())
    } catch {
      /* never let the relay break the caller */
    }
    return
  }
  if (typeof Notification === 'undefined') return
  try {
    new Notification(title, { ...options, silent })
  } catch {
    /* unsupported platform */
  }
}

/**
 * Parse an untrusted `postMessage` payload as a native-notify envelope.
 * Returns null unless every field has exactly the expected type. Origin is NOT
 * checked here -- the caller (`InstancesViewport` onMessage) has already
 * resolved `event.origin` to a warm tunnel before it reaches this.
 */
export function parseNativeNotifyEnvelope(data: unknown): NativeNotifyEnvelope | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (d.type !== NATIVE_NOTIFY_TYPE || d.v !== NATIVE_NOTIFY_VERSION) return null
  if (typeof d.title !== 'string' || typeof d.body !== 'string' || typeof d.tag !== 'string') return null
  if (typeof d.silent !== 'boolean') return null
  return {
    type: NATIVE_NOTIFY_TYPE,
    v: NATIVE_NOTIFY_VERSION,
    title: d.title.slice(0, NATIVE_NOTIFY_MAX_TITLE),
    body: d.body.slice(0, NATIVE_NOTIFY_MAX_BODY),
    tag: d.tag.slice(0, NATIVE_NOTIFY_MAX_TAG),
    silent: d.silent,
  }
}

/**
 * Parent side: post the banner for a note relayed by a warm instance pane.
 * The title carries the instance's name so a user with several crews can tell
 * them apart, and the tag is namespaced per instance so two crews' notes never
 * collapse onto one. Only posts when this (main) frame holds the grant; it
 * never prompts -- prompting belongs to a user gesture in Settings.
 */
export function postRelayedNativeNotification(
  instanceName: string,
  instanceId: string,
  note: NativeNotifyEnvelope,
): boolean {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return false
  try {
    new Notification(`${instanceName}: ${note.title}`, {
      body: note.body,
      tag: `${instanceId}:${note.tag}`,
      silent: note.silent,
    })
    return true
  } catch {
    return false
  }
}
