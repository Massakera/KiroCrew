/**
 * `lib/nativeNotify` — the single seam every OS notification goes through.
 *
 * The case worth stating: inside an embedded remote-instance pane the
 * page-context constructor is refused by Electron's main-frame-only permission
 * gate, so the pane must RELAY to its parent instead of constructing. If that
 * branch regresses, a remote crew's completions and approvals reach the user on
 * no surface at all, and nothing throws to say so.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'
import {
  NATIVE_NOTIFY_BODY_MAX,
  NATIVE_NOTIFY_MESSAGE,
  NATIVE_NOTIFY_TITLE_MAX,
  canShowNativeNotification,
  clampNotifyText,
  requestNativeNotificationPermission,
  showNativeNotification,
} from '../lib/nativeNotify'

type Built = { title: string; options: NotificationOptions }

const realNotification = (globalThis as { Notification?: unknown }).Notification
let built: Built[]
let requested: number

function installNotification(permission: string, opts: { throws?: boolean } = {}) {
  class FakeNotification {
    static permission = permission
    static requestPermission() {
      requested += 1
      return Promise.resolve(permission)
    }
    constructor(title: string, options: NotificationOptions = {}) {
      if (opts.throws) throw new TypeError('Illegal constructor')
      built.push({ title, options })
    }
  }
  Object.defineProperty(globalThis, 'Notification', {
    value: FakeNotification,
    configurable: true,
    writable: true,
  })
}

function removeNotification() {
  delete (globalThis as { Notification?: unknown }).Notification
}

beforeEach(() => {
  built = []
  requested = 0
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
})

afterEach(() => {
  if (realNotification === undefined) removeNotification()
  else {
    Object.defineProperty(globalThis, 'Notification', {
      value: realNotification,
      configurable: true,
      writable: true,
    })
  }
  vi.restoreAllMocks()
})

describe('showNativeNotification — top-level frame', () => {
  it('constructs a silent, tagged notification when permission is granted', () => {
    installNotification('granted')
    showNativeNotification({ title: 'Done', body: 'Response ready', tag: 't1' })
    expect(built).toEqual([
      { title: 'Done', options: { body: 'Response ready', silent: true, tag: 't1' } },
    ])
  })

  it('defaults silent to true and honours an explicit false', () => {
    installNotification('granted')
    showNativeNotification({ title: 'a' })
    showNativeNotification({ title: 'b', silent: false })
    // Silent by default because WebAudio owns notification sound; an
    // un-silenced toast plays the OS chime on top of our own tone.
    expect(built.map(b => b.options.silent)).toEqual([true, false])
  })

  it('does not construct when permission is denied or the API is absent', () => {
    installNotification('denied')
    showNativeNotification({ title: 'nope' })
    removeNotification()
    showNativeNotification({ title: 'nope' })
    expect(built).toHaveLength(0)
  })

  it('swallows a throwing constructor', () => {
    // Android Chrome throws "Illegal constructor" with permission granted. An
    // uncaught throw here kills the caller — for the approval path, the rest of
    // the WebSocket message handler, so the approval never reaches the feed.
    installNotification('granted', { throws: true })
    expect(() => showNativeNotification({ title: 'boom' })).not.toThrow()
  })

  it('drops a titleless notification rather than posting a blank card', () => {
    installNotification('granted')
    showNativeNotification({ title: '' })
    showNativeNotification({ title: undefined as unknown as string })
    expect(built).toHaveLength(0)
  })

  it('clamps title and body', () => {
    installNotification('granted')
    showNativeNotification({ title: 'x'.repeat(9999), body: 'y'.repeat(9999) })
    expect(built[0].title).toHaveLength(NATIVE_NOTIFY_TITLE_MAX)
    expect(built[0].options.body).toHaveLength(NATIVE_NOTIFY_BODY_MAX)
  })
})

describe('showNativeNotification — embedded pane', () => {
  it('relays to the parent instead of constructing', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    installNotification('denied') // what the frame gate actually reports
    const postMessage = vi.fn()
    const parentSpy = vi
      .spyOn(window, 'parent', 'get')
      .mockReturnValue({ postMessage } as unknown as Window)

    showNativeNotification({ title: 'Crew done', body: 'Response ready', tag: 't9' })

    expect(built).toHaveLength(0)
    expect(postMessage).toHaveBeenCalledWith(
      { type: NATIVE_NOTIFY_MESSAGE, v: 1, title: 'Crew done', body: 'Response ready', tag: 't9', silent: true },
      '*',
    )
    parentSpy.mockRestore()
  })

  it('does not throw when the parent cannot be reached', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const parentSpy = vi.spyOn(window, 'parent', 'get').mockImplementation(() => {
      throw new Error('cross-origin')
    })
    expect(() => showNativeNotification({ title: 'x' })).not.toThrow()
    parentSpy.mockRestore()
  })

  it('reports itself capable, and asks for no permission', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    installNotification('denied')
    // 'denied' is what the frame gate always reports in a pane; reading it
    // literally would switch the feature off for exactly the crews whose
    // completions the user cannot otherwise see.
    expect(canShowNativeNotification()).toBe(true)
    requestNativeNotificationPermission()
    expect(requested).toBe(0)
  })
})

describe('permission plumbing in the top-level frame', () => {
  it('asks only from the default state', () => {
    installNotification('default')
    requestNativeNotificationPermission()
    expect(requested).toBe(1)

    installNotification('denied')
    requestNativeNotificationPermission()
    installNotification('granted')
    requestNativeNotificationPermission()
    expect(requested).toBe(1)
  })

  it('reports capability from the live permission value', () => {
    installNotification('granted')
    expect(canShowNativeNotification()).toBe(true)
    installNotification('default')
    expect(canShowNativeNotification()).toBe(false)
    removeNotification()
    expect(canShowNativeNotification()).toBe(false)
  })
})

describe('clampNotifyText', () => {
  it('truncates a string and rejects a non-string', () => {
    expect(clampNotifyText('abcdef', 3)).toBe('abc')
    expect(clampNotifyText(42, 10)).toBe('')
    expect(clampNotifyText(undefined, 10)).toBe('')
    expect(clampNotifyText(null, 10)).toBe('')
  })
})
