import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { renderHookWithProviders } from './helpers'
import { useFileArtifactComments } from '../components/FileArtifactComments'
import { api } from '../api/client'
import type { ArtifactComment } from '../types'

vi.mock('../api/client')

type PreviewRef = RefObject<HTMLDivElement | null>
type ScrollRef = RefObject<HTMLElement | null>
type CommentsResponse = { comments: ArtifactComment[]; remote_sync_error?: string | null }

function refs() {
  return { previewRef: { current: null } as PreviewRef, scrollRef: { current: null } as ScrollRef }
}

function mk(id: string): ArtifactComment {
  return {
    id, origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body: 'x', thread_id: id, status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '',
  }
}

/** A rendered body the hook can anchor into: one text node, in the document. */
const BODY = 'alpha beta gamma beta delta'
const mounted: HTMLElement[] = []
function mountPreview(): HTMLDivElement {
  const el = document.createElement('div')
  el.textContent = BODY
  document.body.appendChild(el)
  mounted.push(el)
  return el
}

/**
 * Back `window.getSelection` with a real `Range` over `node` from `start` for
 * `word.length` characters — the same object shape the hook reads. The test DOM
 * has no real selection, and the hook must see the LIVE range (a composer's
 * `onOpen` is the one moment it exists), so this is what `onOpen` runs against.
 */
function selectRange(node: Node, start: number, word: string) {
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: false,
    anchorNode: node,
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges: () => undefined,
    toString: () => word,
  } as unknown as Selection)
}

const noSelection = () => vi.spyOn(window, 'getSelection').mockReturnValue({
  isCollapsed: true, anchorNode: null, rangeCount: 0, getRangeAt: () => { throw new Error('no range') },
  removeAllRanges: () => undefined, toString: () => '',
} as unknown as Selection)

const postedAnchor = () => vi.mocked(api.postArtifactComment).mock.calls[0][1].anchor as Record<string, unknown> | undefined

describe('useFileArtifactComments', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.postArtifactComment).mockResolvedValue({ ok: true } as Awaited<ReturnType<typeof api.postArtifactComment>>)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    mounted.splice(0).forEach(el => el.remove())
  })

  it('is inert when slug is null', () => {
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: null, ...refs() }))
    expect(result.current.commentCount).toBe(0)
    expect(result.current.overlay).toBeNull()
    expect(result.current.sidebar).toBeNull()
    expect(result.current.popovers).toBeNull()
  })

  it('loads comments for a slug and reflects the count', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [mk('a'), mk('b')] } as CommentsResponse)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    await waitFor(() => expect(result.current.commentCount).toBe(2))
    expect(result.current.sidebar).not.toBeNull()
    expect(result.current.overlay).not.toBeNull()
  })

  it('toggles the sidebar open state', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as CommentsResponse)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    expect(result.current.sidebarOpen).toBe(true)
    act(() => result.current.toggleSidebar())
    expect(result.current.sidebarOpen).toBe(false)
  })

  describe('selection composer', () => {
    beforeEach(() => {
      vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as CommentsResponse)
    })

    it('onOpen resolves the live DOM selection into an anchor that onSubmit posts, pinned to THIS occurrence', async () => {
      const preview = mountPreview()
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      // The SECOND "beta": an indexOf-based anchor would land on the first.
      const second = BODY.indexOf('beta', BODY.indexOf('beta') + 1)
      selectRange(preview.firstChild as Text, second, 'beta')

      act(() => result.current.selectionComposer.onOpen?.('beta'))
      // What the toolbar does after `onOpen`: focus collapses the selection.
      noSelection()
      act(() => result.current.selectionComposer.onSubmit('tighten this', 'beta'))

      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      const [slug, body] = vi.mocked(api.postArtifactComment).mock.calls[0]
      expect(slug).toBe('doc')
      expect(body).toMatchObject({ text: 'tighten this', scope: 'private' })
      expect(postedAnchor()).toEqual({
        quote: 'beta',
        prefix: BODY.slice(Math.max(0, second - 32), second),
        suffix: BODY.slice(second + 4, second + 4 + 32),
        start_offset: second,
        end_offset: second + 4,
      })
    })

    it('onOpen accepts a triple-click selection whose end point is hoisted out of the preview, like the toolbar does', async () => {
      // Browsers normalize a triple-click on the last block to end "at the start
      // of the next block" — a boundary point in the preview's PARENT. The
      // toolbar opens for it (containedSelectionRange), so the resolver must
      // anchor it too, with offsets measured inside the preview.
      const wrapper = document.createElement('div')
      const preview = document.createElement('div')
      preview.textContent = BODY
      wrapper.append(preview, document.createElement('p'))
      document.body.appendChild(wrapper)
      mounted.push(wrapper)
      const previewRef: PreviewRef = { current: preview }
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', previewRef, scrollRef: { current: null } }))
      const start = BODY.indexOf('gamma')
      const range = document.createRange()
      range.setStart(preview.firstChild as Text, start)
      range.setEnd(wrapper, 1)
      vi.spyOn(window, 'getSelection').mockReturnValue({
        isCollapsed: false, anchorNode: preview.firstChild, rangeCount: 1, getRangeAt: () => range,
        removeAllRanges: () => undefined, toString: () => range.toString(),
      } as unknown as Selection)

      act(() => result.current.selectionComposer.onOpen?.(range.toString().trim()))
      noSelection()
      act(() => result.current.selectionComposer.onSubmit('last block', range.toString().trim()))
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toMatchObject({ quote: BODY.slice(start), start_offset: start, end_offset: BODY.length })
    })

    it('onOpen with no live selection anchors on the toolbar\'s text alone (no offsets)', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      noSelection()
      act(() => result.current.selectionComposer.onOpen?.('quoted words'))
      act(() => result.current.selectionComposer.onSubmit('note', 'quoted words'))
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toEqual({ quote: 'quoted words', prefix: undefined, suffix: undefined })
    })

    it('an in-iframe selection hands the toolbar an external selection and keeps the bridge anchor through onOpen', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      expect(result.current.iframeSelection).toBeNull()
      act(() => result.current.onIframeSelect({ x: 20, y: 40, quote: 'widget body', prefix: 'before ', suffix: ' after' }))
      expect(result.current.iframeSelection).toEqual({ text: 'widget body', x: 20, y: 40 })

      // The toolbar opens from that external selection: no DOM range to resolve,
      // so `onOpen` must NOT replace the bridge's prefix/suffix with the bare quote.
      noSelection()
      act(() => result.current.selectionComposer.onOpen?.('widget body'))
      act(() => result.current.selectionComposer.onSubmit('from the frame', 'widget body'))
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      // Offsets are omitted on purpose: the frame's text space is not the parent's.
      expect(postedAnchor()).toEqual({ quote: 'widget body', prefix: 'before ', suffix: ' after' })
      // The submit consumed the external selection.
      expect(result.current.iframeSelection).toBeNull()
    })

    it('a second in-iframe selection made while a box is open does not re-anchor that box\'s draft', async () => {
      // The toolbar refuses to re-target a box holding a typed draft — it never
      // calls `onOpen` for the second selection. The host must not move the
      // anchor underneath it either: the draft was written about passage A.
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      noSelection()
      act(() => result.current.onIframeSelect({ x: 1, y: 2, quote: 'passage A', prefix: 'before A ', suffix: ' after A' }))
      act(() => result.current.selectionComposer.onOpen?.('passage A'))
      // Reading gesture inside the frame while the box (with a draft) is open.
      act(() => result.current.onIframeSelect({ x: 5, y: 6, quote: 'passage B', prefix: 'before B ', suffix: ' after B' }))
      act(() => result.current.selectionComposer.onSubmit('about A', 'passage A'))
      await waitFor(() => expect(api.postArtifactComment).toHaveBeenCalledTimes(1))
      expect(postedAnchor()).toEqual({ quote: 'passage A', prefix: 'before A ', suffix: ' after A' })
    })

    it('onClose discards the pending anchor and the external selection, so a later submit posts nothing', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), usesIframe: true }))
      act(() => result.current.onIframeSelect({ x: 1, y: 2, quote: 'q', prefix: 'p', suffix: 's' }))
      act(() => result.current.selectionComposer.onClose?.())
      expect(result.current.iframeSelection).toBeNull()
      act(() => result.current.selectionComposer.onSubmit('orphan', 'q'))
      await act(async () => { await Promise.resolve() })
      expect(api.postArtifactComment).not.toHaveBeenCalled()
    })

    it('mirrors the composer draft flag and threads the host\'s discard prompt into the composer', () => {
      const confirmDiscardDraft = vi.fn(async () => true)
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs(), confirmDiscardDraft }))
      expect(result.current.hasComposerDraft()).toBe(false)
      act(() => result.current.selectionComposer.onDraftChange?.(true, { anchor: 'x', start: 0 }))
      expect(result.current.hasComposerDraft()).toBe(true)
      act(() => result.current.selectionComposer.onDraftChange?.(false, null))
      expect(result.current.hasComposerDraft()).toBe(false)
      expect(result.current.selectionComposer.confirmDiscard).toBe(confirmDiscardDraft)
    })

    it('onSubmit before any onOpen is a safe no-op', async () => {
      const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
      expect(() => act(() => result.current.selectionComposer.onSubmit('note', 'text'))).not.toThrow()
      await act(async () => { await Promise.resolve() })
      expect(api.postArtifactComment).not.toHaveBeenCalled()
    })
  })
})
