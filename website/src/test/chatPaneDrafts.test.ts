import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readPaneDraft, writePaneDraft, takePaneDraft, mergePaneDraft, subscribePaneDraft, carryPastes, PANE_DRAFTS_KEY, PANE_PASTE_DRAFTS_KEY, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import type { PasteBlock } from '../utils/pasteTokens'

/* The pane's parked drafts must survive the storage layer refusing a write:
 * a quota that ChatPage's own 2 MiB stores may already have filled, or a
 * browser with storage disabled. The in-memory mirror is what hands the draft
 * back in that case, for as long as the tab lives. */

const block = (seq: number, content: string): PasteBlock => ({ id: `p${seq}-${content.replace(/\n/g, '_')}`, seq, lines: content.split('\n').length, content })

describe('chatPaneDrafts', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('parks and takes per slot; take clears the entry', () => {
    writePaneDraft('a', { text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(readPaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(takePaneDraft('a')).toEqual({ text: 'for a', files: ['/tmp/a.png'], pastes: [] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
  })

  it('lives in sessionStorage, not the localStorage quota ChatPage shares', () => {
    writePaneDraft('a', { text: 'for a', files: [], pastes: [] })
    expect(sessionStorage.getItem(PANE_DRAFTS_KEY)).toContain('for a')
    expect(localStorage.getItem(PANE_DRAFTS_KEY)).toBeNull()
  })

  it('parks the paste blocks with the text, in sessionStorage as well, and takes them back', () => {
    const b = block(1, 'l1\nl2\nl3')
    writePaneDraft('a', { text: '[ Paste #1 · 3 lines ]', files: [], pastes: [b] })
    expect(sessionStorage.getItem(PANE_PASTE_DRAFTS_KEY)).toContain('l1\\nl2\\nl3')
    expect(localStorage.getItem(PANE_PASTE_DRAFTS_KEY)).toBeNull()
    expect(takePaneDraft('a')).toEqual({ text: '[ Paste #1 · 3 lines ]', files: [], pastes: [b] })
    expect(readPaneDraft('a')).toEqual({ text: '', files: [], pastes: [] })
  })

  it('hands the draft back from the mirror when storage refuses the write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    const b = block(1, 'x\ny\nz')
    writePaneDraft('quota', { text: 'kept despite quota [ Paste #1 · 3 lines ]', files: ['/tmp/q.png'], pastes: [b] })
    expect(readPaneDraft('quota')).toEqual({ text: 'kept despite quota [ Paste #1 · 3 lines ]', files: ['/tmp/q.png'], pastes: [b] })
    // A merge on top of a refused write still accumulates.
    mergePaneDraft('quota', 'and this', [])
    expect(readPaneDraft('quota').text).toContain('kept despite quota')
    expect(readPaneDraft('quota').text).toContain('and this')
    expect(readPaneDraft('quota').pastes).toEqual([b])
  })

  it('notifies a subscriber of the slot when a late merge lands, and not other slots', () => {
    const onA = vi.fn(); const onB = vi.fn()
    const offA = subscribePaneDraft('a', onA); const offB = subscribePaneDraft('b', onB)
    mergePaneDraft('a', 'late for a', [])
    expect(onA).toHaveBeenCalledTimes(1)
    expect(onB).not.toHaveBeenCalled()
    offA(); offB()
    mergePaneDraft('a', 'after unsubscribe', [])
    expect(onA).toHaveBeenCalledTimes(1)
  })

  it('a late merge re-numbers a carried block that collides with a parked one', () => {
    const parked = block(1, 'parked\ncontent\nhere')
    writePaneDraft('a', { text: '[ Paste #1 · 3 lines ]', files: [], pastes: [parked] })
    const carried = block(1, 'late\ncontent\ntoo')
    mergePaneDraft('a', '[ Paste #1 · 3 lines ]', [], [carried])
    const merged = readPaneDraft('a')
    // Both tokens survive, and the carried one now points at seq 2.
    expect(merged.text).toContain('[ Paste #1 · 3 lines ]')
    expect(merged.text).toContain('[ Paste #2 · 3 lines ]')
    expect(merged.pastes).toEqual([parked, { ...carried, seq: 2 }])
  })
})

describe('carryPastes', () => {
  it('passes the kept blocks through untouched when nothing is carried', () => {
    const kept = [block(1, 'a\nb\nc')]
    expect(carryPastes('typed', [], kept)).toEqual({ text: 'typed', pastes: kept })
  })

  it('keeps a carried seq that is free, re-numbers one that is taken, and never adds a block twice', () => {
    const kept = [block(1, 'k\nk\nk')]
    const free = block(3, 'f\nf\nf')
    const taken = block(1, 't\nt\nt')
    const { text, pastes } = carryPastes('[ Paste #3 · 3 lines ] [ Paste #1 · 3 lines ]', [free, taken, kept[0]], kept)
    expect(pastes).toEqual([kept[0], free, { ...taken, seq: 2 }])
    expect(text).toBe('[ Paste #3 · 3 lines ] [ Paste #2 · 3 lines ]')
  })
})
