/**
 * The task-shape receipt on the assistant reply.
 *
 * Three things are pinned, each a way this surface could lie:
 *
 *  - the READER. Every field arrives over the wire and BOTH choices are the claim,
 *    so a record that does not name two of the three shipped shapes draws nothing
 *    rather than a line about a shape no code path produces. `agree` is recomputed,
 *    so a wire flag cannot hide a divergence behind a matching word.
 *  - the LINE. It names both arms, because the suggestion was ADVICE: a reader who
 *    cannot see whether it was taken cannot tell a useful advisory from an ignored
 *    one. The agent's arm prints its spawn COUNT, because the word is a bucket.
 *  - the ASSISTANT ROW. A turn nobody suggested for carries no record, which is
 *    every reply on a default install, so the absent-field path is the common path:
 *    the row must render exactly as it does today. The receipt arrives in the same
 *    `decisions_strip` list every other point's does, so a turn decided twice draws
 *    both rather than one instead of the other.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { queryClient } from '../api/queryClient'
import AssistantMessage from '../pages/chat/AssistantMessage'
import SplitDecisionLine from '../pages/chat/SplitDecisionLine'
import { __resetVerdicts, readSplitRecord } from '../pages/chat/decisionRecord'

/** A record as the gateway stamps it: Jev said split, and the agent split. */
const WIRE = {
  turn_id: 'ts-split-1',
  ts: '2026-09-21T07:00:00Z',
  point: 'task.split',
  jev_choice: 'split',
  agent_choice: 'split',
  spawn_calls: 2,
  spawn_helpers: 2,
  spawn_tools: ['spawn_run', 'spawn_run'],
  agree: true,
  p: 0.84,
  latency_ms: 190,
  session: 'abcd',
  scrubbed: false,
}

/** The same turn, with the agent answering inline instead. */
const IGNORED = {
  ...WIRE,
  agent_choice: 'single',
  spawn_calls: 0,
  spawn_helpers: 0,
  spawn_tools: [],
  agree: false,
}

beforeEach(() => {
  __resetVerdicts()
  queryClient.clear()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
  vi.restoreAllMocks()
})

describe('readSplitRecord', () => {
  it('reads a full record and keeps every measurement', () => {
    expect(readSplitRecord(WIRE)).toEqual({
      turnId: 'ts-split-1',
      point: 'task.split',
      jevChoice: 'split',
      agentChoice: 'split',
      spawnHelpers: 2,
      agree: true,
      p: 0.84,
      latencyMs: 190,
    })
  })

  it('ignores keys it does not know, so a producer may stamp more', () => {
    // `spawn_tools` is on the wire for the day-file fold and is deliberately NOT
    // read here: the line prints the shape word and the count, and WHICH route was
    // taken is an audit fact rather than something on a one-line receipt.
    expect(readSplitRecord({ ...WIRE, message_chars: 40, future: 'x' })?.jevChoice).toBe('split')
    expect(readSplitRecord(WIRE)).not.toHaveProperty('spawnTools')
  })

  it('recomputes agree from the two choices, never from the wire flag', () => {
    // A flag that disagreed with the words beside it would hide a real divergence
    // behind one of them, which is the one claim this surface must not invent.
    expect(readSplitRecord({ ...WIRE, agree: false })?.agree).toBe(true)
    expect(readSplitRecord({ ...IGNORED, agree: true })?.agree).toBe(false)
  })

  it('draws nothing when either choice names no shipped shape', () => {
    // Unlike an unknown skill key, a fourth word would name a code path that does
    // not exist, so the line declines rather than guessing at it.
    expect(readSplitRecord({ ...WIRE, jev_choice: 'fan-out' })).toBeNull()
    expect(readSplitRecord({ ...WIRE, agent_choice: 'fan-out' })).toBeNull()
    expect(readSplitRecord({ ...WIRE, jev_choice: '' })).toBeNull()
    expect(readSplitRecord({ ...WIRE, agent_choice: 3 })).toBeNull()
  })

  it('draws nothing when either choice is missing', () => {
    const { jev_choice: _j, ...noJev } = WIRE
    const { agent_choice: _a, ...noAgent } = WIRE
    expect(readSplitRecord(noJev)).toBeNull()
    expect(readSplitRecord(noAgent)).toBeNull()
  })

  it('draws nothing without a turn id, which the verdict POST needs', () => {
    expect(readSplitRecord({ ...WIRE, turn_id: '' })).toBeNull()
  })

  it('draws nothing for another point’s record', () => {
    expect(readSplitRecord({ ...WIRE, point: 'skills.select' })).toBeNull()
    expect(readSplitRecord({ ...WIRE, point: 'message.steer' })).toBeNull()
  })

  it('prints no score for a number that is not a probability', () => {
    expect(readSplitRecord({ ...WIRE, p: 1.4 })?.p).toBeNull()
    expect(readSplitRecord({ ...WIRE, p: 'high' })?.p).toBeNull()
  })

  it('floors an unparsable latency and spawn count to zero', () => {
    expect(readSplitRecord({ ...WIRE, latency_ms: -5 })?.latencyMs).toBe(0)
    expect(readSplitRecord({ ...WIRE, spawn_helpers: null })?.spawnHelpers).toBe(0)
  })

  it('prints the SUB-AGENT count, and never the number of spawn calls', () => {
    // One batch call can start three helpers, and "1 spawned" over three working
    // sub-agents misreports the turn. `spawn_calls` is on the wire for the day-file
    // fold and is not read here, so it cannot move the printed number.
    const batch = { ...WIRE, spawn_calls: 1, spawn_helpers: 3, spawn_tools: ['spawn_sub_agents'] }
    expect(readSplitRecord(batch)?.spawnHelpers).toBe(3)
    expect(readSplitRecord({ ...WIRE, spawn_calls: 9, spawn_helpers: 2 })?.spawnHelpers).toBe(2)
  })

  it('draws nothing for something that is not a record at all', () => {
    for (const raw of [null, undefined, 'x', 7, [WIRE]]) {
      expect(readSplitRecord(raw)).toBeNull()
    }
  })
})

describe('the line', () => {
  const record = readSplitRecord(WIRE)!

  it('names what Jev suggested and what the agent did', () => {
    render(<SplitDecisionLine record={record} />)
    const text = screen.getByTestId('split-decision-line').textContent ?? ''
    expect(text).toMatch(/Jev suggested/i)
    expect(text).toMatch(/agent/i)
  })

  it('prints the agent’s spawn COUNT, because the word is a bucket', () => {
    // `split` covers two helpers and twenty, and the number is the only part of
    // that a reader can act on.
    render(<SplitDecisionLine record={record} />)
    expect(screen.getByTestId('split-decision-line').textContent).toContain('2')
  })

  it('says agreement with a VERB, not by printing the shape twice', () => {
    // A reader could not tell whether the repeated phrase was two facts or one
    // printed twice, so the agreed case states the shape ONCE and says the agent
    // agreed (UX review).
    render(<SplitDecisionLine record={record} />)
    const text = screen.getByTestId('split-decision-sentence').textContent ?? ''
    expect(text).toMatch(/agreed/i)
    const shape = 'parallel sub-agents'
    expect(text.split(shape).length - 1, `"${shape}" is printed twice in: ${text}`).toBe(1)
  })

  it('says the agent did something ELSE, so the arms cannot read as opposites', () => {
    // The fragment form joined as "Jev suggested parallel sub-agents, agent: one
    // worker, different shape" and read as a self-contradiction: the second arm is
    // verbed instead of prefixed, and names what happened INSTEAD.
    render(<SplitDecisionLine record={readSplitRecord(IGNORED)!} />)
    const text = screen.getByTestId('split-decision-sentence').textContent ?? ''
    expect(text).toMatch(/instead/i)
    expect(text).toContain('parallel sub-agents')
    expect(text).toContain('one worker')
    expect(text).not.toMatch(/different shape/i)
  })

  it('wraps instead of truncating, so the verdict at the tail survives', () => {
    // The verdict is the LAST clause, so a clipped line loses the one fact this
    // receipt exists to show -- and a hover title is no answer, because hover is
    // unreachable on touch and silent to a screen reader.
    render(<SplitDecisionLine record={record} />)
    const span = screen.getByTestId('split-decision-sentence')
    expect(span.className).not.toContain('truncate')
    expect(span.className).toContain('break-words')
    // And the row lets a second line exist rather than holding one line's height.
    expect(screen.getByTestId('split-decision-line').className).toContain('flex-wrap')
  })

  it('drops the spawn clause when there was nothing to spawn', () => {
    // An agreed `single` has no helpers, and "(0 spawned)" would be furniture.
    const agreedSingle = { ...WIRE, jev_choice: 'single', agent_choice: 'single', spawn_helpers: 0, spawn_tools: [] }
    render(<SplitDecisionLine record={readSplitRecord(agreedSingle)!} />)
    const text = screen.getByTestId('split-decision-sentence').textContent ?? ''
    expect(text).toMatch(/agreed/i)
    expect(text).not.toMatch(/spawned/i)
  })

  it('says the two shapes matched, and says so differently when they did not', () => {
    render(<SplitDecisionLine record={record} />)
    const agreed = screen.getByTestId('split-decision-line')
    expect(agreed.dataset.agree).toBe('true')
    const same = agreed.textContent ?? ''
    cleanup()

    render(<SplitDecisionLine record={readSplitRecord(IGNORED)!} />)
    const differed = screen.getByTestId('split-decision-line')
    expect(differed.dataset.agree).toBe('false')
    expect(differed.textContent).not.toBe(same)
  })

  it('names each shape distinctly, so the two arms are readable apart', () => {
    render(<SplitDecisionLine record={readSplitRecord(IGNORED)!} />)
    const line = screen.getByTestId('split-decision-line')
    expect(line.dataset.jevChoice).toBe('split')
    expect(line.dataset.agentChoice).toBe('single')
  })

  it('carries the score and the latency in one parenthetical', () => {
    render(<SplitDecisionLine record={record} />)
    const scores = screen.getByTestId('split-decision-scores').textContent ?? ''
    expect(scores).toContain('0.84')
    expect(scores).toContain('190')
  })

  it('names the score instead of printing a bare number', () => {
    // A hover title is unreachable on touch and silent to a screen reader, so the
    // only explanation of "0.84" has to be on the line.
    render(<SplitDecisionLine record={record} />)
    expect(screen.getByTestId('split-decision-scores').textContent).toMatch(/confidence/i)
  })

  it('prints no parenthetical when the record carries neither number', () => {
    render(<SplitDecisionLine record={readSplitRecord({ ...WIRE, p: null, latency_ms: 0 })!} />)
    expect(screen.queryByTestId('split-decision-scores')).toBeNull()
  })

  it('labels the thumbs with a complete instruction naming what they rate', () => {
    // "This suggestion" read as a sentence cut off mid-way, and a bare "Jev" both
    // repeats the name the sentence opens with and fails to tell this pair from the
    // skill strip's when both stack on one reply (UX review).
    render(<SplitDecisionLine record={record} />)
    const label = screen.getByTestId('decision-strip-rate-label-jev').textContent ?? ''
    expect(label).not.toMatch(/^Jev$/)
    expect(label).toMatch(/^Rate\b/)
    expect(label.toLowerCase()).toContain('split')
  })

  it('offers the Jev thumbs and no baseline pair', () => {
    // The rateable claim is the SUGGESTION; the other arm is what the agent did,
    // which is behaviour rather than an answer to rate.
    render(<SplitDecisionLine record={record} />)
    expect(screen.getByTestId('decision-strip-right-jev')).toBeInTheDocument()
    expect(screen.queryByTestId('decision-strip-right-baseline')).toBeNull()
  })
})

describe('the assistant row', () => {
  const renderRow = (props: Record<string, unknown> = {}) =>
    render(<AssistantMessage content="here is the answer" isStreaming={false} {...props} />)

  it('renders exactly as today when the row carries no record', () => {
    // The shipping state on every default install: nothing was suggested, so there
    // is no receipt and the row must not grow an empty one.
    renderRow()
    expect(screen.queryByTestId('split-decision-line')).toBeNull()
    expect(screen.getByText('here is the answer')).toBeInTheDocument()
  })

  it('draws the line once the row carries one', () => {
    renderRow({ decisionsStrip: WIRE })
    expect(screen.getByTestId('split-decision-line')).toBeInTheDocument()
  })

  it('draws nothing for a record it cannot check', () => {
    renderRow({ decisionsStrip: { ...WIRE, jev_choice: 'fan-out' } })
    expect(screen.queryByTestId('split-decision-line')).toBeNull()
  })

  it('draws the skill strip and this line together, not one instead of the other', () => {
    // One turn can carry both, and the gateway stamps the LIST: the strip is
    // published during prompt assembly and this receipt when the turn finalizes,
    // and `decisions.outcomes` is keyed by point so neither displaces the other.
    renderRow({
      decisionsStrip: [
        WIRE,
        {
          turn_id: 'sk-1',
          point: 'skills.select',
          baseline: ['a'],
          jev: ['a'],
          p: 0.5,
        },
      ],
    })
    expect(screen.getByTestId('split-decision-line')).toBeInTheDocument()
    expect(screen.getByTestId('decision-strip')).toBeInTheDocument()
    // Two thumb pairs stack, so their labels have to name different targets --
    // otherwise a reader cannot tell which one they are about to press.
    const labels = screen.getAllByTestId('decision-strip-rate-label-jev').map(el => el.textContent)
    expect(new Set(labels).size, `both pairs are labelled ${labels[0]}`).toBe(labels.length)
  })

  it('ignores a steer record stamped on an assistant row', () => {
    // Two readers, one list. Each declines the other's shape rather than rendering
    // it as a half-read claim about a decision it never validated: a steer receipt
    // rides the USER row and has its own reader.
    renderRow({ decisionsStrip: { turn_id: 't', point: 'message.steer', choice: 'queue' } })
    expect(screen.queryByTestId('split-decision-line')).toBeNull()
  })
})
