import { memo } from 'react'
import { Split } from 'lucide-react'

import { fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import VerdictThumbs from './DecisionVerdictThumbs'
import type { SplitChoice, SplitDecisionRecord } from './decisionRecord'

/** Two decimals, so `0.84` reads as a score and not as a rounded `0.8`. */
const confidence = (p: number) => fmtNumber(p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** The word for one shape, in the reader's language. */
function shapeWord(choice: SplitChoice): string {
  if (choice === 'delegate') return i18nT('pages.chat.decisionStrip.split_shape_delegate')
  if (choice === 'split') return i18nT('pages.chat.decisionStrip.split_shape_split')
  return i18nT('pages.chat.decisionStrip.split_shape_single')
}

/**
 * ONE sentence naming both arms, in a shape a reader cannot misparse.
 *
 * Three fragments joined by commas ("Jev suggested parallel sub-agents, agent: one
 * worker, different shape") read as opposites in one breath: a reader could not tell
 * which arm actually happened, and on the agree case could not tell whether the
 * repeated phrase was two facts or one printed twice. So the second arm is VERBED
 * instead of prefixed, and agreement is stated once rather than by repetition.
 */
function sentence(record: SplitDecisionRecord): string {
  const suggested = shapeWord(record.jevChoice)
  if (!record.agree) {
    return i18nT('pages.chat.decisionStrip.split_differed_line', {
      suggested,
      actual: shapeWord(record.agentChoice),
    })
  }
  // The count only means something when helpers were actually started; on an
  // agreed `single` there is nothing to count and the clause would read as "(0
  // spawned)".
  return record.spawnHelpers > 0
    ? i18nT('pages.chat.decisionStrip.split_agreed_line_spawned', {
      shape: suggested,
      count: fmtNumber(record.spawnHelpers),
    })
    : i18nT('pages.chat.decisionStrip.split_agreed_line', { shape: suggested })
}

/**
 * The transcript's receipt for one task-shape suggestion.
 *
 * `task.split` is ADVICE: the suggestion reached the turn as one prepended line and
 * the agent chose for itself. So the line has to name BOTH arms -- otherwise a
 * reader cannot tell a suggestion that was taken from one that was ignored, and an
 * advisory nobody can score is an advisory nobody can withdraw.
 *
 * It says that in ONE sentence rather than in three joined fragments, because the
 * fragment form read as a self-contradiction (UX review): "Jev suggested parallel
 * sub-agents, agent: one worker, different shape" left a reader unable to say which
 * arm happened. The agreed case carries the spawn COUNT, because the shape word is a
 * bucket -- `split` covers two helpers and twenty.
 *
 * The sentence WRAPS rather than truncating. Its verdict is at the tail, so a clipped
 * line loses the one fact the receipt exists to show, and a hover `title` is no answer
 * to that: hover is unreachable on touch and silent to a screen reader. A second line
 * costs a row some height; a cut verdict costs the reader the receipt.
 *
 * ONE line, not a disclosure card like `DecisionStrip`. Everything this decision
 * produced fits on it: the two arms, the score, the latency. There is no menu to
 * expand, and a collapsed card that hides nothing is a control that does nothing.
 *
 * The record on the row is the ONLY condition, the rule the strip states: a stamped
 * record is history that already sits on this machine, so drawing it sends nothing,
 * and gating it on the current consent switch would lose the receipt for every past
 * turn the moment the preview is turned off.
 *
 * The thumbs are `DecisionStrip`'s own pair, shared rather than copied. Side `jev`:
 * the rateable claim is the SUGGESTION, and the other arm is what the agent did,
 * which is behaviour rather than an answer to rate. Their label NAMES what they rate
 * ("split advice") rather than repeating Jev: two thumb pairs stack on a reply that
 * also carries a skill strip, and a reader has to be able to tell which one is which.
 */
const SplitDecisionLine = memo(function SplitDecisionLine({
  record,
}: {
  record: SplitDecisionRecord
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings.
  useLanguageGeneration()

  const text = sentence(record)
  const latency = record.latencyMs > 0
    ? i18nT('pages.chat.decisionStrip.latency_value', { ms: fmtNumber(record.latencyMs) })
    : null
  // Both numbers describe the answer, so they share one parenthetical, joined the
  // way the strip joins its own: the separator between two measurements read as one
  // value is a locale's decision, not this file's. The score carries its own WORD,
  // because a bare number beside a sentence is unreadable on touch and silent to a
  // screen reader.
  const scores = [
    record.p !== null
      ? i18nT('pages.chat.decisionStrip.steer_confidence', { p: confidence(record.p) })
      : null,
    latency,
  ].filter((part): part is string => part !== null)
  // The legend names what the group actually holds, so all three cases get their
  // own sentence instead of one describing a number that is not there.
  const scoresTitle = record.p !== null && latency !== null
    ? i18nT('pages.chat.decisionStrip.confidence_latency_title')
    : record.p !== null
      ? i18nT('pages.chat.decisionStrip.confidence_title')
      : i18nT('pages.chat.decisionStrip.latency_title')

  return (
    <div
      className="inline-flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 text-[12px] leading-5 text-muted mb-1 pr-1 min-w-0"
      data-testid="split-decision-line"
      data-jev-choice={record.jevChoice}
      data-agent-choice={record.agentChoice}
      data-agree={record.agree}
    >
      <Split size={12} className="shrink-0 self-center" aria-hidden="true" />
      {/* WRAPS rather than truncates. The verdict is at the TAIL of the sentence
          ("and the agent agreed" / "used one worker instead"), so a clipped line
          loses exactly the fact this receipt exists to show -- and `title` is no
          answer to that, because hover is unreachable on touch and silent to a
          screen reader. A second line costs a row some height; a cut verdict costs
          the reader the receipt. `min-w-0` stays so the flex child may shrink to
          the row rather than forcing it wide. */}
      <span className="min-w-0 break-words" data-testid="split-decision-sentence">
        {text}
      </span>
      {scores.length > 0 && (
        <span className="shrink-0 tabular-nums" title={scoresTitle} data-testid="split-decision-scores">
          ({fmtList(scores, { type: 'unit' })})
        </span>
      )}
      <VerdictThumbs
        turnId={record.turnId}
        side="jev"
        // NOT the strip's "Jev" label, and not a bare "This suggestion" either: the
        // first repeats a name the sentence already opens with, and the second read
        // as a sentence cut off mid-way. This names WHAT the pair rates, which is
        // also what tells it from the skill strip's pair when both stack on one
        // reply.
        label={i18nT('pages.chat.decisionStrip.split_rate_label')}
        rightLabel={i18nT('pages.chat.decisionStrip.split_rate_right')}
        wrongLabel={i18nT('pages.chat.decisionStrip.split_rate_wrong')}
      />
    </div>
  )
})

export default SplitDecisionLine
