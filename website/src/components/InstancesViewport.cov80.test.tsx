import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import InstancesViewport from './InstancesViewport'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Zzq One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok2' }),
  },
}))
import { api } from '../api/client'

const ORIGIN = 'http://127.0.0.1:7778'

function warmStore(activeId: string | null = 'cd-1') {
  return createTestStore({
    instances: {
      warm: { 'cd-1': { port: 7778, token: 'tok' } },
      activeId,
      mru: ['cd-1'],
      unread: {},
      ready: {},
      host: null,
    },
  })
}

function post(data: unknown, origin = ORIGIN) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data, origin }))
  })
}

describe('InstancesViewport relay listener', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
  })

  it('records an unread count relayed from a warm pane', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 3 })
    expect(store.getState().instances.unread['cd-1']).toBe(3)
  })

  it('rejects a non-finite or negative unread count', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 2 })
    post({ type: 'mc-unread-slots', count: 'zzq' })
    post({ type: 'mc-unread-slots', count: -1 })
    expect(store.getState().instances.unread['cd-1']).toBe(2)
  })

  it('ignores a message from an origin that is not a warm tunnel, and non-object data', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-unread-slots', count: 9 }, 'http://127.0.0.1:9999')
    post({ type: 'mc-unread-slots', count: 9 }, 'https://evil.example')
    post('zzq-string')
    post(null)
    expect(store.getState().instances.unread['cd-1']).toBeUndefined()
  })

  it('re-mints the token when the pane reports an expired session', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(api.refreshInstanceToken).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1'].token).toBe('tok2'))
  })

  it('honours a switch request to Local and to a known instance, but not to an unknown id', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-switch-instance', id: null })
    expect(store.getState().instances.activeId).toBeNull()

    post({ type: 'mc-switch-instance', id: 'cd-1' })
    expect(store.getState().instances.activeId).toBe('cd-1')

    post({ type: 'mc-switch-instance', id: 'zzq-unknown' })
    post({ type: 'mc-switch-instance', id: 42 })
    expect(store.getState().instances.activeId).toBe('cd-1')
  })

  it('marks a pane ready when it announces itself', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    expect(store.getState().instances.ready['cd-1']).toBeFalsy()
    post({ type: 'mc-embedded-ready' })
    expect(store.getState().instances.ready['cd-1']).toBe(true)
  })

  it('sanitizes relayed drag gaps and keeps serving later messages', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({
      type: 'mc-drag-gaps',
      gaps: [
        { x: 10, w: 40 },
        { x: -1, w: 10 },
        { x: 5, w: 0 },
        { x: 'zzq', w: 10 },
        null,
        ...Array.from({ length: 64 }, () => ({ x: 1, w: 1 })),
      ],
    })
    post({ type: 'mc-drag-gaps', gaps: 'not-an-array' })

    // Drag strips are Electron-only, so nothing is painted here — the point is
    // that a hostile payload neither throws nor kills the listener.
    expect(document.querySelectorAll('.host-drag-strip')).toHaveLength(0)
    post({ type: 'mc-unread-slots', count: 5 })
    expect(store.getState().instances.unread['cd-1']).toBe(5)
  })

  it('ignores an unrecognised message type', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'zzq-unknown-type', count: 4 })
    expect(store.getState().instances.unread['cd-1']).toBeUndefined()
    expect(store.getState().instances.ready['cd-1']).toBeFalsy()
  })

  it('rate-limits a burst of expiry reports to a single re-mint', async () => {
    const store = warmStore()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(api.refreshInstanceToken).toHaveBeenCalledTimes(1))
    post({ type: 'mc-auth-expired' })
    post({ type: 'mc-auth-expired' })
    await waitFor(() => expect(store.getState().instances.warm['cd-1'].token).toBe('tok2'))
    expect(api.refreshInstanceToken).toHaveBeenCalledTimes(1)
  })
})

/**
 * The parent posts OS notifications on an embedded pane's behalf, because a
 * pane's own page-context constructor is refused by Electron's main-frame-only
 * permission gate. Without this relay a remote crew's completions and approvals
 * reach the user on no surface at all.
 */
describe('InstancesViewport native notification relay', () => {
  let constructed: Array<{ title: string; options: NotificationOptions }>
  const realNotification = (globalThis as { Notification?: unknown }).Notification

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    constructed = []
    class FakeNotification {
      static permission = 'granted'
      constructor(title: string, options: NotificationOptions = {}) {
        constructed.push({ title, options })
      }
    }
    Object.defineProperty(globalThis, 'Notification', {
      value: FakeNotification,
      configurable: true,
      writable: true,
    })
  })

  afterEach(() => {
    // Restore rather than delete: jsdom has no Notification, so an unconditional
    // delete is right here, but a future jsdom that ships one must not lose it.
    if (realNotification === undefined) {
      delete (globalThis as { Notification?: unknown }).Notification
    } else {
      Object.defineProperty(globalThis, 'Notification', {
        value: realNotification,
        configurable: true,
        writable: true,
      })
    }
  })

  /**
   * Render and wait until the instances query has actually reached the
   * component, not merely until an iframe exists: the pane is rendered straight
   * from the warm store, so the iframe appears BEFORE the crew list resolves,
   * and a relay arriving in that window legitimately gets no crew name. Waiting
   * on the rendered name is an observable signal for "the data landed" — never a
   * sleep standing in for it.
   */
  async function renderWithCrewLoaded() {
    renderWithProviders(<InstancesViewport />, { store: warmStore() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    await waitFor(() => expect(document.body.textContent).toContain('Zzq One'))
  }

  it('posts a relayed notification, led by the crew name and tagged per instance', async () => {
    await renderWithCrewLoaded()

    post({ type: 'mc-native-notify', v: 1, title: 'Refactor done', body: 'Response ready', tag: 'kirocrew-chat-done:s1' })

    await waitFor(() => expect(constructed).toHaveLength(1))
    // The crew name leads: without it two crews showing the same session title
    // are indistinguishable in Notification Center.
    expect(constructed[0].title).toBe('Zzq One · Refactor done')
    expect(constructed[0].options.body).toBe('Response ready')
    // Namespaced so a pane cannot collapse the local dashboard's own banner.
    expect(constructed[0].options.tag).toBe('mc-instance:cd-1:kirocrew-chat-done:s1')
  })

  it('falls back to the instance id when the crew has no name', async () => {
    // `name` is optional. Dropping the prefix when it is absent would make a
    // remote crew's banner look exactly like a local one -- the confusion the
    // prefix exists to prevent -- so an id stands in rather than nothing.
    vi.mocked(api.listInstances).mockResolvedValueOnce({
      instances: [{
        id: 'cd-1', ssh_host: 'cd-1-alias', remote_port: 7777, local_port: 7778,
        ttl: '20h', remote_bin: '',
        status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
      }],
      warm_set_cap: 5,
    } as unknown as Awaited<ReturnType<typeof api.listInstances>>)
    renderWithProviders(<InstancesViewport />, { store: warmStore() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    post({ type: 'mc-native-notify', v: 1, title: 'Turn finished' })

    await waitFor(() => expect(constructed).toHaveLength(1))
    expect(constructed[0].title).toBe('cd-1 · Turn finished')
  })

  it('re-clamps an oversized relayed payload instead of trusting the sender', async () => {
    await renderWithCrewLoaded()

    post({ type: 'mc-native-notify', v: 1, title: 'T'.repeat(5000), body: 'B'.repeat(5000) })

    await waitFor(() => expect(constructed).toHaveLength(1))
    // Clamped twice — once on the relayed title, then again on the composed
    // string — so the cap holds whatever the crew name costs. Clamping keeps a
    // PREFIX and the crew name leads, so the name is never what gets dropped.
    expect(constructed[0].title).toHaveLength(200)
    expect(constructed[0].title.startsWith('Zzq One · ')).toBe(true)
    expect(constructed[0].options.body).toHaveLength(500)
  })

  it('drops a relay with no usable title, and one from an untrusted origin', async () => {
    await renderWithCrewLoaded()

    post({ type: 'mc-native-notify', v: 1, title: '' })
    post({ type: 'mc-native-notify', v: 1, title: 42 })
    post({ type: 'mc-native-notify', v: 1, title: 'From nowhere' }, 'http://127.0.0.1:9999')
    post({ type: 'mc-native-notify', v: 1, title: 'From evil' }, 'https://evil.example')

    // Nothing to wait for — assert the absence after the synchronous dispatches.
    expect(constructed).toHaveLength(0)
  })
})
