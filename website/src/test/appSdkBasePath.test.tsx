/**
 * AppApiProvider `basePath` — the seam that lets a component subtree issue its
 * calls against a connected peer through the instances proxy instead of this
 * gateway.
 *
 * The ordering is the whole point and is what these specs pin: the permission
 * check runs on the caller's LOGICAL path, and the prefix is applied only to the
 * final URL. Reversing it would force a remote-crew subtree to declare
 * `/api/instances` in `allowedApiPaths` — handing it the peer's control plane,
 * which is exactly what the backend proxy allowlist exists to prevent.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { AppApiProvider, useAppApi } from '../app-sdk'

const CHAT_SCOPE = ['/api/chat', '/api/stream']

function Harness({ basePath, call }: { basePath?: string; call: (api: ReturnType<typeof useAppApi>) => void }) {
  return (
    <AppApiProvider
      appName="test"
      allowedApiPaths={CHAT_SCOPE}
      allowedEvents={[]}
      basePath={basePath}
      subscribeFn={() => () => {}}
      navigateFn={() => {}}
      notifyFn={() => {}}
    >
      <Probe call={call} />
    </AppApiProvider>
  )
}

function Probe({ call }: { call: (api: ReturnType<typeof useAppApi>) => void }) {
  const api = useAppApi()
  call(api)
  return null
}

describe('AppApiProvider basePath', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('sends the request to the prefixed URL', async () => {
    let api!: ReturnType<typeof useAppApi>
    render(<Harness basePath="/api/instances/chick/proxy" call={a => { api = a }} />)
    await api.get('/api/chat/slots')
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock.mock.calls[0][0]).toBe('/api/instances/chick/proxy/api/chat/slots')
  })

  it('defaults to same-origin when omitted — existing callers are unchanged', async () => {
    let api!: ReturnType<typeof useAppApi>
    render(<Harness call={a => { api = a }} />)
    await api.get('/api/chat/slots')
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/slots')
  })

  it('checks the LOGICAL path, so the declared scope needs no proxy prefix', async () => {
    // The scope declares `/api/chat` only. If the check ran on the prefixed URL
    // this would throw, and the caller would be pushed into declaring
    // `/api/instances` — the widening this ordering exists to prevent.
    let api!: ReturnType<typeof useAppApi>
    render(<Harness basePath="/api/instances/chick/proxy" call={a => { api = a }} />)
    await expect(api.get('/api/chat/slots')).resolves.toBeDefined()
  })

  it('does NOT let basePath smuggle an undeclared path past the allowlist', async () => {
    // A prefix must not become a capability. `/api/token/local` is outside the
    // declared scope and stays refused no matter what the prefix is.
    let api!: ReturnType<typeof useAppApi>
    render(<Harness basePath="/api/instances/chick/proxy" call={a => { api = a }} />)
    await expect(api.get('/api/token/local')).rejects.toThrow(/not permitted/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('still rejects traversal that would escape the declared scope', async () => {
    // `check()` normalizes before matching, so `..` cannot climb out — and the
    // prefix does not change that, because it is applied afterwards.
    let api!: ReturnType<typeof useAppApi>
    render(<Harness basePath="/api/instances/chick/proxy" call={a => { api = a }} />)
    await expect(api.get('/api/chat/../token/local')).rejects.toThrow(/not permitted/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('still rejects an absolute URL regardless of basePath', async () => {
    let api!: ReturnType<typeof useAppApi>
    render(<Harness basePath="/api/instances/chick/proxy" call={a => { api = a }} />)
    await expect(api.get('https://evil.example/api/chat')).rejects.toThrow(/Absolute URLs/)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
