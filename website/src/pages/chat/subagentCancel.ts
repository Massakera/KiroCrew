import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import type { AppDispatch } from '../../store'
import { sseSubagentDone } from '../../store/chatSlice'

type DoneFrame = Parameters<typeof sseSubagentDone>[0]

function isDoneFrameFor(id: string, frame: unknown): frame is DoneFrame {
  if (!frame || typeof frame !== 'object') return false
  const f = frame as Record<string, unknown>
  return f.id === id && typeof f.slot === 'string' && typeof f.elapsed === 'number'
}

/**
 * Cancel one subagent card, and end the card when the run turns out to be over.
 *
 * A running run converges through its live `subagent_done` frame. A run that
 * already ended emits nothing more, so a card that missed that frame would keep
 * its running label however often Cancel is pressed. Two answers settle it here:
 * the gateway returns the finished run's terminal frame, and a 404 means the
 * gateway no longer tracks the run at all, so nothing is running under that id.
 * Any other failure is rethrown for the caller to report.
 */
export async function cancelSubagentCard(
  dispatch: AppDispatch,
  card: { id: string; startedAt: number },
  slot: string,
): Promise<void> {
  try {
    const reply: unknown = await api.spawnDelete(card.id)
    const done = reply && typeof reply === 'object' ? (reply as { done?: unknown }).done : undefined
    if (isDoneFrameFor(card.id, done)) dispatch(sseSubagentDone(done))
  } catch (e) {
    if (!isNotFoundError(e) || !slot) throw e
    dispatch(sseSubagentDone({
      slot,
      id: card.id,
      elapsed: Math.max(0, Math.round((Date.now() - card.startedAt) / 1000)),
      stopped: true,
      outcome: 'stopped',
    }))
  }
}
