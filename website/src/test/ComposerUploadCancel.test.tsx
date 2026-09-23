/**
 * Cancelling a composer upload that is still in flight (issue #5744).
 *
 * While `uploading` is true every attach entry point is disabled, and the
 * request itself used to run to completion whatever the user did: the only exit
 * from a slow transfer was reloading the page, which throws away the bytes
 * already sent. Both composer hosts now hold the request's AbortController and
 * hand a cancel control to ChatInput.
 *
 * Two things are pinned per host, because the second is the trap: aborting must
 * end the request AND must not be dressed up as a failure. The blanket catch
 * would otherwise tell a user who cancelled a 150 MB recording to check the
 * file type and a 50 MB cap.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn(),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

/** The DOMException `fetch` rejects with once its signal aborts. */
function abortError(): Error {
  const e = new Error('The operation was aborted.')
  e.name = 'AbortError'
  return e
}

/**
 * Stand in for an upload still on the wire: never settles on its own, and
 * rejects the way `fetch` does the moment the caller's signal aborts. Returns a
 * getter for the signal the host handed in, which is the only proof the host
 * wired one at all.
 */
function pendingUpload(): () => AbortSignal | undefined {
  let captured: AbortSignal | undefined
  vi.mocked(api.uploadFiles).mockImplementation(
    ((_files: File[], signal?: AbortSignal) => {
      captured = signal
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(abortError()))
      })
    }) as unknown as typeof api.uploadFiles,
  )
  return () => captured
}

function makeStore(activeSlot: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: activeSlot, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    } as Partial<RootState>,
  })
}

function renderHost(node: ReactNode, activeSlot: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={makeStore(activeSlot)}>
        <ThemeProvider>
          <MemoryRouter>{node}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** Start an upload the ordinary way: pick a file through the hidden input. */
function pickFile(container: HTMLElement, name = 'screencap.mp4', type = 'video/mp4') {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [new File(['x'], name, { type })], configurable: true })
  fireEvent.change(input)
}

const cancelControl = () => screen.queryByRole('button', { name: 'Cancel upload' })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

describe.each([
  ['ChatPage', (slot: string) => <ChatPage key={slot} />, 'cancel-page'],
  ['ChatPane', (slot: string) => <ChatPane slotKey={slot} />, 'cancel-pane'],
])('%s composer: an upload in flight can be cancelled (#5744)', (_name, node, slot) => {
  it('aborts the request, restores the attach controls, and raises no failure', async () => {
    const signal = pendingUpload()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())

    await act(async () => { pickFile(container) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    // The host must hand its own signal down; without one there is nothing to
    // abort and the control below would be decorative.
    await waitFor(() => expect(cancelControl()).toBeInTheDocument())
    expect(signal()).toBeInstanceOf(AbortSignal)
    expect(signal()!.aborted).toBe(false)

    await act(async () => { fireEvent.click(cancelControl()!) })

    expect(signal()!.aborted).toBe(true)
    // `uploading` is what disables every attach entry point, so its clearing is
    // the user-visible half of the fix: the control disappears with the spinner.
    await waitFor(() => expect(cancelControl()).not.toBeInTheDocument())
    // A cancel the user asked for is not an upload failure.
    expect(screen.queryByText(/max 50 MB/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Upload failed/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/aborted/i)).not.toBeInTheDocument()
  })

  it('offers no cancel control when no upload is in flight', async () => {
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    expect(cancelControl()).not.toBeInTheDocument()
  })
})
