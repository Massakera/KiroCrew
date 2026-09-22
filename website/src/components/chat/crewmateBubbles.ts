/**
 * crewmateBubbles — how a crewmate's chat renders (Members page, member-mode
 * slots only; ordinary chats never route through this module).
 *
 * Two rules live here, and ONLY here, so every surface that draws a crewmate's
 * messages (the chat today, a reply thread's footer later) reads the same
 * answer:
 *
 * 1. WHAT SHOWS. A crewmate's chat shows only what the crewmate says to the
 *    user, plus the user's own messages. The machinery a member-mode slot
 *    accumulates — `[auto-nudge cycle N]` turns, `[Cron notification …]` and
 *    `[Subagent completion event]` envelopes, tool-call rows, reasoning
 *    bursts, and the say-nothing rows a quiet patrol ends on — is filtered at
 *    RENDER time by `filterCrewmateChat`. Nothing is deleted: the rows stay in
 *    the slot's transcript and the Work log reads them from there.
 *
 * 2. HOW A RUN LOOKS. Consecutive messages from the crewmate form a run,
 *    Slack-style: one avatar + name + time on the first message, one bubble per
 *    message, grouped corners on the run's (left) side. A run breaks on a user
 *    message or after `CREWMATE_RUN_GAP_MS` of silence. `crewmateRunPosition`
 *    stamps each message's place in its run; `crewmateBubbleClass` turns that
 *    into the corner utilities. Right-side corners are always full.
 *
 * Both are pure functions over the transcript so they can be unit-tested and
 * reused by a reply-thread footer without dragging the pane along.
 */
import { isSystemNoticeRow } from '../../pages/chat/CompactionCard'
import { isWorkflowCompletionMessage } from '../../pages/chat/WorkflowCompletionCard'
import { isSubagentCompletionMessage } from '../../pages/chat/subagentCompletion'
import type { ChatMessage } from '../../types'
import { isHiddenInvisibleAssistantRow } from '../../utils/invisibleText'

/** Where a message sits in a run of consecutive crewmate messages. */
export type CrewmateRunPosition = 'single' | 'start' | 'cont' | 'end'

/** Avatar edge on the author line, px. Matches the roster row's face size. */
export const CREWMATE_AVATAR_PX = 28

/** A run breaks after this much silence — one avatar per exchange, not per day. */
export const CREWMATE_RUN_GAP_MS = 5 * 60_000

/** Roles that are the crewmate's own machinery: the transcript keeps them, the
 *  chat does not draw them. `tool_call` / `tool_result` are the SDK's
 *  lifecycle spellings of a tool row; `done` is the turn-end marker, which no
 *  surface draws — left in, an all-machinery transcript would count as
 *  non-empty and the crewmate's empty hint would never show. */
const MACHINERY_ROLES: ReadonlySet<string> = new Set([
  'nudge', 'inject', 'subagent', 'tool', 'tool_call', 'tool_result', 'thinking', 'done',
])

/** The stop card travels under `system`; every other `system` row is state
 *  no surface draws. */
function isStopCard(m: ChatMessage): boolean {
  return m.kind === 'stop_event' || m.meta?.kind === 'stop_event'
}

/** Rows that carry state, not a message, and never draw on any surface. A run
 *  reads THROUGH them: a resolved approval between two of the crewmate's
 *  messages does not split its avatar in two. The stop card is the exception
 *  among `system` rows: it IS drawn (the user pressed Stop and sees the card),
 *  so it is a boundary like an error row, not state the run reads past. */
function isRunTransparent(m: ChatMessage): boolean {
  if (m.role === 'permission') return !!m.meta?.resolved
  if (isStopCard(m)) return false
  return m.role === 'system' || m.role === 'done' || m.role === 'queued'
}

/** The crewmate speaking: an assistant row with visible words, or the live
 *  streaming row. A say-nothing assistant row (the bare U+200B a quiet patrol
 *  ends on), a gateway system notice written under the assistant role
 *  (compaction, session reload), an injected workflow completion and a
 *  sub-agent completion envelope written under the assistant role are status,
 *  not speech. Together with the user's own rows this is the twin of the
 *  backend's `is_speech_row` (`dashboard/system_notices.py`), which decides
 *  what the Crew Members roster quotes; the two are pinned to one verdict per
 *  row by `test/fixtures/crewmate_speech_rows.json`, read by both test suites. */
export function isCrewmateSpeech(m: ChatMessage): boolean {
  if (m.role === 'streaming') return true
  if (m.role !== 'assistant') return false
  // A sub-agent completion envelope also reaches the transcript under the
  // assistant role (the Slack gateway's delivery-timeout and orphan variants).
  return !isHiddenInvisibleAssistantRow(m) && !isSystemNoticeRow(m) && !isWorkflowCompletionMessage(m) && !isSubagentCompletionMessage(m)
}

/** Whether a row is drawn in a crewmate's chat at all. */
export function isCrewmateChatRow(m: ChatMessage): boolean {
  if (MACHINERY_ROLES.has(m.role)) return false
  if (m.role === 'assistant') return isCrewmateSpeech(m)
  if (m.role === 'system') return isStopCard(m)
  // A pending approval is the approval surface and stays; a RESOLVED one draws
  // nothing (its renderer returns null) and must not count as content — kept,
  // it would hide the quiet hint behind a blank chat. Same rule `isRunTransparent`
  // applies when a run reads past it.
  if (m.role === 'permission') return !m.meta?.resolved
  // Old scrollback persisted the sub-agent completion envelope under the user
  // role; it is machinery there too (the backend twin judges it by content).
  if (isSubagentCompletionMessage(m)) return false
  return true
}

/** The rows a crewmate's chat draws, in transcript order. Same array identity
 *  back when nothing was dropped, so a memo on the result stays stable. */
export function filterCrewmateChat(messages: ChatMessage[]): ChatMessage[] {
  const kept = messages.filter(isCrewmateChatRow)
  return kept.length === messages.length ? messages : kept
}

function tsOf(m: ChatMessage | undefined): number {
  if (!m?.ts) return NaN
  return new Date(m.ts).getTime()
}

/** Nearest row in `dir` that is not run-transparent, or undefined at an end. */
function neighbour(messages: ChatMessage[], index: number, dir: -1 | 1): ChatMessage | undefined {
  for (let j = index + dir; j >= 0 && j < messages.length; j += dir) {
    if (!isRunTransparent(messages[j])) return messages[j]
  }
  return undefined
}

/** Two adjacent crewmate messages are one run only when the second follows
 *  within `gapMs` of the first. A missing or unparsable timestamp (a streaming
 *  row has none yet) does not break the run. */
function chained(a: ChatMessage | undefined, b: ChatMessage | undefined, gapMs: number): boolean {
  if (!a || !b || !isCrewmateSpeech(a) || !isCrewmateSpeech(b)) return false
  const gap = tsOf(b) - tsOf(a)
  return Number.isNaN(gap) ? true : gap >= 0 && gap <= gapMs
}

/**
 * Position of `messages[index]` — which must be a crewmate speech row — within
 * its run. `messages` is the list the chat draws (already filtered), so the
 * neighbours are the rows drawn next to it.
 */
export function crewmateRunPosition(
  messages: ChatMessage[],
  index: number,
  gapMs: number = CREWMATE_RUN_GAP_MS,
): CrewmateRunPosition {
  const m = messages[index]
  const first = !chained(neighbour(messages, index, -1), m, gapMs)
  const last = !chained(m, neighbour(messages, index, 1), gapMs)
  if (first && last) return 'single'
  if (first) return 'start'
  if (last) return 'end'
  return 'cont'
}

/** True for the message that carries the run's avatar, name and time. */
export function opensCrewmateRun(pos: CrewmateRunPosition): boolean {
  return pos === 'single' || pos === 'start'
}

/**
 * The corner rule for a left-aligned run, as Tailwind utilities: single = all
 * four corners full; first = bottom-left small; middle = top-left and
 * bottom-left small; last = top-left small. Right corners stay full. The
 * per-corner utilities are more specific than `rounded-2xl`, so they win
 * whatever order the class list ends up in.
 */
const CORNERS: Record<CrewmateRunPosition, string> = {
  single: 'rounded-2xl',
  start: 'rounded-2xl rounded-bl-md',
  cont: 'rounded-2xl rounded-l-md',
  end: 'rounded-2xl rounded-tl-md',
}

/** Surface + padding + measure, every bubble alike. Tokens only (`bg-card`,
 *  `border-border`), so light and dark each pick their own palette. The
 *  markdown's outermost first/last block margins are zeroed so the bubble's own
 *  padding is the whole inset. */
const BUBBLE_BASE =
  'bg-card border border-border px-3.5 py-1.5 max-w-[72ch] [&>.group>:first-child]:mt-0 [&>.group>:last-child]:mb-0'

/** Classes for the crewmate's message bubble at `pos`. */
export function crewmateBubbleClass(pos: CrewmateRunPosition): string {
  return `${BUBBLE_BASE} ${CORNERS[pos]}`
}

/** Vertical rhythm of a row: a run opens with a little air above its author
 *  line; bubbles inside a run sit close. */
export function crewmateRowClass(pos: CrewmateRunPosition): string {
  return opensCrewmateRun(pos) ? 'mt-1.5' : 'mt-0.5'
}
