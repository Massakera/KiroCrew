/**
 * ChatPane's paste-block sidecar (#11337).
 *
 * `ChatInput` collapses a large paste into a `[ Paste #N · M lines ]` token
 * only when its host passes `onPasteBlocksChange`; the pane rendered it
 * without that prop, so a big paste stayed raw text in split view and member
 * DMs while the main chat showed a chip. These scenes drive the REAL pane and
 * assert against the wire: the token in the composer, the EXPANDED text in
 * the API call, the blocks on the bubble, and the composer clear — plus the
 * lifetime cases that make a token safe: a rebind parks the blocks with the
 * text, and a refused send hands them back with it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests, readPaneDraft } from '../utils/chatPaneDrafts'
import { readStoredPaste } from '../utils/pasteTokens'

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
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
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
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const PASTED = 'line1\nline2\nline3\nline4\nline5' // >= PASTE_THRESHOLD_LINES
const TOKEN = /\[ Paste #1 · 5 lines \]/

function makeStore(slotKeys: string[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: slotKeys.map(key => ({ key, messages: 0, running: false, subagents_running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string, extraSlots: string[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore([slotKey, ...extraSlots])
  const tree = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const utils = render(tree(slotKey))
  return { ...utils, store, rebind: (key: string) => utils.rerender(tree(key)) }
}

async function composer(): Promise<HTMLTextAreaElement> {
  return (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement
}

/** Paste through the real handler: ChatInput reads `getData('text')`. */
async function pasteInto(box: HTMLTextAreaElement, text: string) {
  await act(async () => {
    fireEvent.paste(box, { clipboardData: { items: [], getData: (t: string) => (t === 'text' ? text : '') } })
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  __resetPaneDraftsForTests()
  localStorage.clear()
  sessionStorage.clear()
})

describe('ChatPane paste sidecar', () => {
  it('collapses a large paste to a token, sends it expanded and clears the composer', async () => {
    const { store } = renderPane('pane-paste')
    const box = await composer()
    fireEvent.change(box, { target: { value: 'please read ' } })
    await pasteInto(box, PASTED)
    // The pill: the composer holds the token, not the pasted lines.
    await waitFor(() => expect(box.value).toMatch(TOKEN))
    expect(box.value).not.toContain('line3')

    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot, , , meta] = vi.mocked(api.sendChat).mock.calls[0]
    expect(slot).toBe('pane-paste')
    // The model gets the CONTENT, never the token string.
    expect(wireText).toContain(PASTED)
    expect(wireText).not.toMatch(TOKEN)
    // The bubble keeps the token and carries the block so it renders a chip;
    // the side table lets history load re-collapse the server's expanded echo.
    expect(meta.pastes).toEqual([expect.objectContaining({ seq: 1, lines: 5, content: PASTED })])
    const bubble = store.getState().chat.slotMessages['pane-paste']?.find(m => m.role === 'user')
    expect(bubble?.content).toMatch(TOKEN)
    expect(readStoredPaste(wireText as string)?.pastes[0]?.content).toBe(PASTED)
    // Composer and blocks are gone: a second paste starts again at #1.
    await waitFor(() => expect(box.value).toBe(''))
    await pasteInto(box, PASTED)
    await waitFor(() => expect(box.value).toMatch(/\[ Paste #1 · 5 lines \]/))
  })

  it('parks the blocks with the text on a rebind and restores both, so the token still expands', async () => {
    const { rebind } = renderPane('slot-a', ['slot-b'])
    const box = await composer()
    await pasteInto(box, PASTED)
    await waitFor(() => expect(box.value).toMatch(TOKEN))

    // Rebind the same pane instance to another slot: A's composer is parked.
    rebind('slot-b')
    await waitFor(() => expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe(''))
    const parked = readPaneDraft('slot-a')
    expect(parked.text).toMatch(TOKEN)
    expect(parked.pastes).toEqual([expect.objectContaining({ seq: 1, content: PASTED })])

    // Back to A: the token is back AND still backed by its block.
    rebind('slot-a')
    const back = screen.getAllByRole('textbox')[0] as HTMLTextAreaElement
    await waitFor(() => expect(back.value).toMatch(TOKEN))
    fireEvent.keyDown(back, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = vi.mocked(api.sendChat).mock.calls[0]
    expect(wireText).toContain(PASTED)
    expect(wireText).not.toMatch(TOKEN)
  })

  it('hands the blocks back with the text when the server refuses the send', async () => {
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as never)
    renderPane('pane-refused')
    const box = await composer()
    await pasteInto(box, PASTED)
    await waitFor(() => expect(box.value).toMatch(TOKEN))
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // The refused payload is restored — token text AND its block — so the
    // retry sends the content, not a dead token.
    await waitFor(() => expect(box.value).toMatch(TOKEN))
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    const [retryText] = vi.mocked(api.sendChat).mock.calls[1]
    expect(retryText).toContain(PASTED)
    expect(retryText).not.toMatch(TOKEN)
  })
})
