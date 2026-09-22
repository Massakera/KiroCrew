/**
 * Per-slot composer drafts for ChatPane — the pane's instance of the repo's
 * slot-draft store (`createSlotDraftStore`), alongside `chatDrafts` (ChatPage
 * text) and `chatFileDrafts` (ChatPage attachments).
 *
 * Why a separate KEY rather than ChatPage's stores: ChatPage holds its draft
 * maps in memory and persists them wholesale on its own schedule, so a second
 * writer on the same key would be overwritten by ChatPage's next save (and
 * ChatPage would never see the pane's write until a reload). The pane instead
 * does read-modify-write against its own keys on every access, so several
 * panes — split view — can share them safely.
 *
 * What it holds: the composer of every slot a pane is NOT currently showing.
 * A pane can be rebound to another slot without remounting (the Members page
 * switches `slotKey` on one instance), and a send's recovery can land after
 * that switch; both park here. The on-screen slot's composer is the live
 * React state — the store is authoritative only for off-screen slots, which
 * is why the pane writes on rebind and unmount rather than per keystroke.
 *
 * A parked draft is text, attachment paths AND the collapsed paste blocks
 * behind any `[ Paste #N · M lines ]` token in that text. The three park and
 * restore together because the token is meaningless without its block: text
 * restored without blocks would show a chip that expands to nothing and send
 * the literal token string. Blocks therefore have exactly the text's lifetime
 * here — never longer (no localStorage copy outliving the tab, unlike the
 * main chat's `chatPasteDrafts`, whose text draft DOES survive a reload).
 *
 * Storage: sessionStorage for text, paths and blocks, with a small byte cap.
 * The pane's parking is a within-session hand-off (a rebind, a page change),
 * so it does not need to outlive the tab; keeping it out of localStorage
 * means ChatPage's two 2 MiB stores can never crowd a parked pane draft out
 * of a shared quota. Every entry is ALSO mirrored in memory: reads prefer
 * storage (it is what a reload sees) and fall back to the mirror, so a persist
 * that fails (quota, disabled storage) still hands the draft back for as long
 * as the tab lives.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES } from './draftConstants'
import { mergeRecoveredDraft } from './chatDrafts'
import { sanitizePasteBlocks } from './chatPasteDrafts'
import { remapCarriedBlocks, type PasteBlock } from './pasteTokens'

export const PANE_DRAFTS_KEY = 'mc-pane-drafts'
export const PANE_FILE_DRAFTS_KEY = 'mc-pane-file-drafts'
export const PANE_PASTE_DRAFTS_KEY = 'mc-pane-paste-drafts'
/** Byte budget for parked pane text — a fraction of ChatPage's 2 MiB: parked
 *  drafts are a hand-off, not an archive, and the in-memory mirror covers the
 *  eviction / persist-failure cases for the live session. */
export const PANE_DRAFTS_MAX_BYTES = 256 * 1024

const textStore = createSlotDraftStore<string>({
  key: PANE_DRAFTS_KEY,
  storage: 'session',
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: PANE_DRAFTS_MAX_BYTES,
  sanitize: (v) => (typeof v === 'string' && v ? v : null),
})

const fileStore = createSlotDraftStore<string[]>({
  key: PANE_FILE_DRAFTS_KEY,
  storage: 'session',
  sanitize: (v) => {
    if (!Array.isArray(v)) return null
    const arr = v.filter((x): x is string => typeof x === 'string')
    return arr.length ? arr.slice() : null
  },
})

// Same byte budget as the text: a block IS the text the token stands in for,
// so a parked paste costs what the same paste typed out would. The mirror
// keeps an evicted block for the tab's lifetime, exactly as it keeps text.
const pasteStore = createSlotDraftStore<PasteBlock[]>({
  key: PANE_PASTE_DRAFTS_KEY,
  storage: 'session',
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: PANE_DRAFTS_MAX_BYTES,
  sanitize: sanitizePasteBlocks,
})

export interface PaneDraft {
  text: string
  files: string[]
  /** Blocks behind the `[ Paste #N · M lines ]` tokens in `text`. */
  pastes: PasteBlock[]
}

/** In-memory mirror of every parked draft: the fallback when storage refused
 *  the write (quota, disabled) or evicted the entry. Lives as long as the tab. */
const mirror = new Map<string, PaneDraft>()

function copy(d: PaneDraft): PaneDraft {
  return { text: d.text, files: d.files.slice(), pastes: d.pastes.slice() }
}

/** The parked composer for `slot`, or empty. The mirror first — it is
 *  write-through, so within this tab it is always the latest write even when
 *  storage refused it — then storage, which is what a reloaded tab has. */
export function readPaneDraft(slot: string): PaneDraft {
  const m = mirror.get(slot)
  if (m) return copy(m)
  const text = textStore.load()[slot] ?? ''
  const files = fileStore.load()[slot] ?? []
  const pastes = pasteStore.load()[slot] ?? []
  return { text, files: files.slice(), pastes: pastes.slice() }
}

/** Read AND clear `slot`'s parked composer — for a pane that is about to show
 *  it live. Once live, the composer is the single copy; leaving the store's
 *  entry in place would let a later park overwrite what arrived in between. */
export function takePaneDraft(slot: string): PaneDraft {
  const draft = readPaneDraft(slot)
  if (draft.text || draft.files.length || draft.pastes.length) writePaneDraft(slot, { text: '', files: [], pastes: [] })
  return draft
}

/** Park `slot`'s composer verbatim. Empty text / no files / no blocks delete
 *  the entry. */
export function writePaneDraft(slot: string, draft: PaneDraft): void {
  if (draft.text || draft.files.length || draft.pastes.length) mirror.set(slot, copy(draft))
  else mirror.delete(slot)
  const texts = textStore.load()
  textStore.set(texts, slot, draft.text)
  textStore.save(texts)
  const files = fileStore.load()
  fileStore.set(files, slot, draft.files)
  fileStore.save(files)
  const pastes = pasteStore.load()
  pasteStore.set(pastes, slot, draft.pastes)
  pasteStore.save(pastes)
}

/** Bring `carried` blocks (the ones behind the tokens in `text`, a payload
 *  being handed back) into a composer that already holds `kept` blocks.
 *
 *  Tokens resolve by `seq`, and a paste made into an empty composer restarts
 *  at #1, so a naive concat can leave two blocks sharing a number — both
 *  tokens would then expand to ONE of them, silently swapping the user's
 *  content on retry. `remapCarriedBlocks` gives colliding carried blocks
 *  fresh numbers and rewrites their tokens in `text`; a carried block the
 *  composer already holds (same id — the clear had not flushed yet) is not
 *  added twice. The same rule ChatPage's failed-send restore applies. */
export function carryPastes(text: string, carried: PasteBlock[], kept: PasteBlock[]): { text: string; pastes: PasteBlock[] } {
  if (!carried.length) return { text, pastes: kept }
  const keptIds = new Set(kept.map((b) => b.id))
  const fresh = carried.filter((b) => !keptIds.has(b.id))
  const { text: remapped, blocks } = remapCarriedBlocks(text, fresh, new Set(kept.map((b) => b.seq)))
  return { text: remapped, pastes: [...kept, ...blocks] }
}

/** Panes currently SHOWING a slot, so a late arrival for that slot can be
 *  handed to the live composer instead of sitting in the store until a park
 *  from that very pane overwrites it. Module-level: the arrival comes from a
 *  closure of a pane instance that may be long gone (unmounted, remounted). */
const listeners = new Map<string, Set<() => void>>()

/** Be told when something merges into `slot`'s parked draft while the caller
 *  shows that slot. The callback should `takePaneDraft` and merge. */
export function subscribePaneDraft(slot: string, onArrival: () => void): () => void {
  let subs = listeners.get(slot)
  if (!subs) { subs = new Set(); listeners.set(slot, subs) }
  subs.add(onArrival)
  return () => {
    subs!.delete(onArrival)
    if (subs!.size === 0) listeners.delete(slot)
  }
}

/** Merge a late recovery (or a late upload) into `slot`'s parked composer:
 *  text appends under the shared recovery rule, paths union, paste blocks
 *  carry over with their tokens re-numbered past the parked ones. Any pane
 *  showing the slot is notified so it can take the merge into its live
 *  composer. */
export function mergePaneDraft(slot: string, text: string, files: string[], pastes: PasteBlock[] = []): void {
  const cur = readPaneDraft(slot)
  const carried = carryPastes(text, pastes, cur.pastes)
  writePaneDraft(slot, {
    text: text ? mergeRecoveredDraft(cur.text, carried.text) : cur.text,
    files: [...cur.files, ...files.filter((f) => !cur.files.includes(f))],
    pastes: carried.pastes,
  })
  const subs = listeners.get(slot)
  if (subs) for (const fn of Array.from(subs)) fn()
}

/** Test-only: drop the in-memory mirror and every subscriber. The mirror is
 *  read BEFORE storage by design, so `sessionStorage.clear()` between tests
 *  does not reset it — a pane unmounted at the end of one test parks its
 *  composer here and the next test's rebind to that slot would take it. */
export function __resetPaneDraftsForTests(): void {
  mirror.clear()
  listeners.clear()
  // The store is the fallback read when the mirror is empty, so a reset that
  // left the persisted entries behind would hand the previous test's park
  // straight back on the next mount. Best-effort: a suite that stubs storage
  // to throw is exercising exactly that refusal.
  try {
    sessionStorage.removeItem(PANE_DRAFTS_KEY)
    sessionStorage.removeItem(PANE_FILE_DRAFTS_KEY)
    sessionStorage.removeItem(PANE_PASTE_DRAFTS_KEY)
  } catch { /* storage unavailable or stubbed to refuse */ }
}
