import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Command Bar's "Copy Review-Ready PR Search" row.
 *
 * The row is an `invoke` builtin: the whole query runs server-side (the browser
 * holds no GitHub token) and only the finished search URL comes back, which the
 * row copies. Two properties are pinned here because both are the difference
 * between "useful" and "actively misleading":
 *
 *  - a SUCCESS copies EXACTLY the string the endpoint returned — never a
 *    client-built fallback, because a URL that still contains conflicted PRs is
 *    the one wrong answer this feature can give;
 *  - a FAILURE (fetch rejects, or the clipboard write fails) surfaces in the bar
 *    and copies nothing, so the user is never told they hold a URL they do not.
 */

const dispatch = vi.fn()
const navigate = vi.fn()

const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({
    navigate,
    enterInsertOrNewSession: vi.fn(),
    newSessionWithToken: vi.fn(),
  }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<
    typeof import('../../components/commandPalette/providers/recentsProvider')
  >()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

const listApps = vi.fn(async () => [])
const chatFolders = vi.fn(async () => [])
const artifacts = vi.fn(async () => ({ artifacts: [] }))
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
    artifacts: (...a: unknown[]) => artifacts(...(a as [])),
  },
}))

/** The endpoint call and the clipboard write — the two seams the row's `run()`
 *  wires together, spied so we can drive success and failure and read what was
 *  handed to the clipboard. */
const reviewReadySearchUrl = vi.fn()
vi.mock('../issue-radar/api', () => ({
  issueRadarApi: { reviewReadySearchUrl: (...a: unknown[]) => reviewReadySearchUrl(...(a as [])) },
}))
const copyWithOutcome = vi.fn()
vi.mock('../../utils/clipboard', () => ({
  copyWithOutcome: (...a: unknown[]) => copyWithOutcome(...(a as [])),
}))

const URL_FROM_SERVER =
  'https://github.com/kirodotdev/KiroCrew/pulls?q=' +
  'is%3Apr+state%3Aopen+author%3Achenmingwei23+label%3A%22readiness%3A+passed%22' +
  '+-head%3Afeat%2Fa+-head%3Afeat%2Fb'

/** Issue Radar ENABLED, which is what makes the row exist at all: the row calls an
 *  Issue Radar endpoint, and that app ships `defaultEnabled: false` with every route
 *  behind an enablement check, so the overlay offers the row only while it is on. */
const RADAR_ON = [{ name: 'issue-radar', displayName: 'Issue Radar', enabled: true }]

function mount(apps: unknown[] = RADAR_ON) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The overlay READS this key -- its query is `enabled: false`, so the shell's own
  // response is what populates it and `listApps` is never called here. Seeding the
  // cache is therefore the only way the app list reaches the component.
  client.setQueryData(['apps'], apps)
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose }
}

const rowByText = (text: string): HTMLElement => {
  const rows = screen.queryAllByRole('option')
  const hit = rows.find(r => (r.textContent || '').includes(text))
  if (!hit) {
    throw new Error(
      `no option row containing "${text}"; rows: ${rows.map(r => r.textContent).join(' | ')}`,
    )
  }
  return hit
}

/** Presence WITHOUT throwing, for the cases whose whole assertion is absence. */
const hasRow = (text: string): boolean =>
  screen.queryAllByRole('option').some(r => (r.textContent || '').includes(text))

const ROW = 'Copy Review-Ready PR Search'

beforeEach(() => {
  vi.clearAllMocks()
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
})

describe('CommandBarOverlay — copy review-ready PRs row', () => {
  it('is present in the root launcher', () => {
    mount()
    expect(rowByText(ROW)).toBeTruthy()
  })

  /**
   * The row is the only builtin that stands on an APP, so it is the only one whose
   * existence is conditional. Issue Radar ships `defaultEnabled: false` and answers
   * 403 while disabled, and an `invoke` rejection renders as "Press Enter to try
   * again" -- a retry that, in this state, can never succeed and names no cause. An
   * absent row is the honest surface; these two cases are what keep it absent.
   */
  it('is ABSENT when Issue Radar is installed but disabled', () => {
    mount([{ name: 'issue-radar', displayName: 'Issue Radar', enabled: false }])
    expect(hasRow(ROW)).toBe(false)
    // The rest of the launcher is unaffected: this gates one row, not the group.
    expect(hasRow('New Session')).toBe(true)
  })

  it('is ABSENT when Issue Radar is not installed at all', () => {
    mount([])
    expect(hasRow(ROW)).toBe(false)
    expect(hasRow('New Session')).toBe(true)
  })

  it('copies EXACTLY the string the endpoint returned, then closes', async () => {
    reviewReadySearchUrl.mockResolvedValue({
      owner: 'kirodotdev',
      repo: 'KiroCrew',  // brand-ok: literal repository name
      author: 'chenmingwei23',
      url: URL_FROM_SERVER,
    })
    copyWithOutcome.mockResolvedValue({ ok: true, hadAsyncApi: true })
    const { onClose } = mount()
    fireEvent.mouseDown(rowByText(ROW))
    await waitFor(() => expect(copyWithOutcome).toHaveBeenCalledWith(URL_FROM_SERVER))
    // The bar closes on a successful invoke; no failure alert is shown.
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('surfaces the failure and copies NOTHING when the fetch rejects', async () => {
    reviewReadySearchUrl.mockRejectedValue(new Error('no authenticated GitHub identity'))
    const { onClose } = mount()
    fireEvent.mouseDown(rowByText(ROW))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    // Never fell back to copying anything, and the bar stayed open so Enter retries.
    expect(copyWithOutcome).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('surfaces the failure when the clipboard write fails', async () => {
    reviewReadySearchUrl.mockResolvedValue({
      owner: 'kirodotdev',
      repo: 'KiroCrew',  // brand-ok: literal repository name
      author: 'chenmingwei23',
      url: URL_FROM_SERVER,
    })
    // The fetch succeeded but the clipboard write did not land: the row must NOT
    // report success, or the user walks away believing they hold the URL.
    copyWithOutcome.mockResolvedValue({ ok: false, hadAsyncApi: true })
    const { onClose } = mount()
    fireEvent.mouseDown(rowByText(ROW))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(onClose).not.toHaveBeenCalled()
  })
})
