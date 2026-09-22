/**
 * The one seam through which this dashboard posts an OS notification.
 *
 * Two jobs, and the second is the reason the module exists.
 *
 * 1. **Best-effort construction.** Page-context `new Notification()` is
 *    desktop-only — Android Chrome throws "Illegal constructor" even with
 *    permission granted, because the platform requires
 *    `ServiceWorkerRegistration.showNotification` and this app registers no
 *    service worker. Every call site previously carried its own try/catch and
 *    its own permission check (see `notificationConstructorGuard.test.ts` for
 *    what an uncaught throw costs: it kills the rest of the WebSocket message
 *    handler, so the approval never even reaches the in-app feed).
 *
 * 2. **The embedded remote-instance pane cannot post at all.** A connected
 *    remote crew renders as a full dashboard inside an `<iframe>`
 *    (`InstancesViewport`), and Electron's permission handler puts
 *    `notifications` in `MAIN_FRAME_ONLY_PERMISSIONS` — deliberately, so a page
 *    the dashboard embeds cannot forge OS toasts wearing this app's identity.
 *    The consequence for a legitimate first-party pane is that
 *    `Notification.permission` is pinned to `'denied'` there and every toast
 *    no-ops silently: a remote crew finishing a background turn could never
 *    tell the user. So when embedded we RELAY the intent up to the parent
 *    dashboard, which is the main frame and holds the grant, and it posts on the
 *    pane's behalf. No permission is widened.
 *
 * The relay reuses the existing pane → parent `postMessage` channel and its
 * rules (see `docs/system-specs/modules/instances.md` § postMessage relay): the
 * child addresses `'*'` because it does not know the parent's origin, and the
 * PARENT is what validates — it trusts a message only from an exact loopback
 * origin whose port belongs to a currently-warm tunnel. Who may frame us at all
 * is bounded separately by the CSP `frame-ancestors` claim in the minted token,
 * which is what keeps a title/body pair from reaching an arbitrary embedder.
 */
import { isEmbeddedPane } from './embedded'

/** Pane → parent relay message type. Handled in `InstancesViewport`. */
export const NATIVE_NOTIFY_MESSAGE = 'mc-native-notify'

/**
 * Length caps applied at BOTH ends.
 *
 * Applied here so a pane cannot relay an unbounded payload, and re-applied by
 * the parent because a cap enforced only by the sender is not a cap. macOS
 * truncates a long banner anyway, so nothing legible is lost.
 */
export const NATIVE_NOTIFY_TITLE_MAX = 200
export const NATIVE_NOTIFY_BODY_MAX = 500

export interface NativeNotifyInput {
  title: string
  body?: string
  /** OS-level coalescing key; a repeat with the same tag replaces the banner. */
  tag?: string
  icon?: string
  /**
   * Default TRUE, and deliberately: WebAudio (`useNotificationSound`) is the
   * single source of notification sound, so an un-silenced toast plays the OS
   * chime on top of our own tone.
   */
  silent?: boolean
}

/** Clamp + drop-if-empty, shared by the sender and the parent's relay handler. */
export function clampNotifyText(value: unknown, max: number): string {
  return typeof value === 'string' ? value.slice(0, max) : ''
}

/**
 * Show an OS notification, or relay it to the parent when embedded.
 *
 * Never throws: every failure mode here (no platform support, permission not
 * granted, a throwing constructor, a parent that refuses the post) is a
 * best-effort miss, and the in-app notification feed has already recorded the
 * event by the time this is called.
 */
export function showNativeNotification(input: NativeNotifyInput): void {
  const title = clampNotifyText(input.title, NATIVE_NOTIFY_TITLE_MAX)
  // A titleless banner is a blank card on macOS — drop it rather than post it.
  if (!title) return
  const body = clampNotifyText(input.body, NATIVE_NOTIFY_BODY_MAX)
  const silent = input.silent !== false
  const tag = typeof input.tag === 'string' ? input.tag : ''

  if (isEmbeddedPane()) {
    try {
      // The child cannot address the parent's origin: it is the host gateway's
      // loopback port, which the pane is never told. Every pane -> parent
      // channel in this app posts to '*' for that reason and the PARENT is what
      // validates (exact loopback origin on a warm tunnel); who may frame us at
      // all is bounded by the token's CSP `frame-ancestors` claim. The payload
      // is a clamped title/body pair, never a credential.
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      window.parent?.postMessage(
        { type: NATIVE_NOTIFY_MESSAGE, v: 1, title, body, tag, silent },
        '*',
      )
    } catch {
      /* no reachable parent — the in-app feed still has the event */
    }
    return
  }

  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return
  try {
    new Notification(title, {
      body,
      silent,
      tag: tag || 'kirocrew-notif',
      ...(input.icon ? { icon: input.icon } : {}),
    })
  } catch {
    /* page-context Notification is desktop-only */
  }
}

/**
 * Whether a native toast can plausibly be delivered from this frame.
 *
 * Call sites gate on this instead of reading `Notification.permission`
 * directly: in an embedded pane that value is always `'denied'` (see the module
 * header), so a permission check there is a check that the feature is off.
 */
export function canShowNativeNotification(): boolean {
  if (isEmbeddedPane()) return true
  return typeof Notification !== 'undefined' && Notification.permission === 'granted'
}

/**
 * Ask for the OS permission, when asking can do anything.
 *
 * A no-op in an embedded pane: the request is refused by the Electron frame
 * gate, and the relay does not need it.
 */
export function requestNativeNotificationPermission(): void {
  if (isEmbeddedPane()) return
  if (typeof Notification === 'undefined' || Notification.permission !== 'default') return
  try {
    void Notification.requestPermission()
  } catch {
    /* unsupported platform (a callback-only implementation returns nothing) */
  }
}
