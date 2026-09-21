/**
 * Screenshot harness for the line `task.split` adds to an assistant reply.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. No gateway, no agent and no Jev call: only the network is stubbed, so
 * `readSplitRecord`, the transcript virtualizer and the assistant rows render
 * exactly as in production.
 *
 * Three frames, because the line's whole job is to let a reader score advice:
 *
 *  - TAKEN. Jev suggested parallel sub-agents and the agent spawned two, so the
 *    line says both arms name the same shape.
 *  - IGNORED. The same suggestion, answered inline. This is the frame the feature
 *    exists for: a reader who cannot tell it from the first one cannot tell a
 *    useful advisory from one nobody follows.
 *  - BESIDE THE STRIP. One reply carrying a `skills.select` strip AND this line,
 *    because they are two records under two keys and the receipt must not
 *    displace the older one.
 *
 * Usage: node scripts/capture-decision-task-split.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { CONSENTED, createChecks, stubDecisionsSeam } from './lib/decisions-capture.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decision-task-split'
const SLOT = 'chat-task-split'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const LINE = '[data-testid="split-decision-line"]'
const TAKEN = `${LINE}[data-agree="true"]`
const IGNORED = `${LINE}[data-agree="false"]`

/** Jev said split; ONE batch call started three helpers. */
const TAKEN_RECORD = {
  turn_id: 'ts-4b19ce',
  ts: '2026-09-21T07:04:11Z',
  point: 'task.split',
  jev_choice: 'split',
  agent_choice: 'split',
  spawn_calls: 1,
  spawn_helpers: 3,
  spawn_tools: ['spawn_sub_agents'],
  agree: true,
  p: 0.84,
  latency_ms: 190,
}

/** The same suggestion, answered inline: the arms differ. */
const IGNORED_RECORD = {
  ...TAKEN_RECORD,
  turn_id: 'ts-7c02fa',
  agent_choice: 'single',
  spawn_calls: 0,
  spawn_helpers: 0,
  spawn_tools: [],
  agree: false,
  p: 0.77,
  latency_ms: 210,
}

/** A `skills.select` strip, so one reply can be photographed carrying both. */
const SKILLS_RECORD = {
  turn_id: 'sk-2f81bd',
  ts: '2026-09-21T07:06:02Z',
  point: 'skills.select',
  baseline: ['python/testing'],
  jev: ['python/testing'],
  agree: true,
  p: 0.79,
  tokens_saved: 0,
  candidates: 34,
  message_chars: 61,
  history_chars: 0,
  latency_ms: 140,
}

const t0 = Date.now() / 1000 - 900

const slots = [
  {
    key: SLOT,
    title: 'Audit the four handler modules',
    running: false,
    last_message: 'Both audits are in.',
    messages: 6,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 6,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Audit chat_handlers.py and messaging.py independently.' },
    {
      role: 'assistant',
      ts: t0 + 64,
      content: 'Both audits are in. chat_handlers.py is clean; messaging.py has one unchecked cast at line 812.',
      meta: { decisions_strip: [TAKEN_RECORD] },
    },
    { role: 'user', ts: t0 + 120, content: 'Now rename that cast’s local and re-run its test.' },
    {
      role: 'assistant',
      ts: t0 + 154,
      content: 'Renamed it to `raw_payload` and re-ran test_messaging.py — 41 passed.',
      meta: { decisions_strip: [IGNORED_RECORD] },
    },
    { role: 'user', ts: t0 + 200, content: 'Add a regression test for the cast.' },
    {
      role: 'assistant',
      ts: t0 + 240,
      content: 'Added one in test_messaging.py that fails without the check.',
      meta: { decisions_strip: [SKILLS_RECORD, TAKEN_RECORD] },
    },
  ],
}

const { check, report } = createChecks()

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
  })

  // Consent, the fleet answer and the English reload, shared with every other
  // decision-point harness (`lib/decisions-capture.mjs`) so two stubs of one gate
  // cannot diverge.
  const { loadInEnglish, setDecisionsEnabled } = await stubDecisionsSeam({
    page,
    load,
    selector: LINE,
    consent: CONSENTED,
  })

  /** The line's own assistant row, which is the claim a clip of the line cannot make. */
  const rowOf = locator => locator.locator('xpath=ancestor::div[@data-role="assistant"][1]')

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)

    // TAKEN: both arms name the same shape.
    const taken = page.locator(TAKEN).first()
    await taken.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await rowOf(taken).screenshot({ path: `${OUT}/01-taken-${theme}.png` })
    console.log('wrote', `${OUT}/01-taken-${theme}.png`)
    const takenSentence = page.locator('[data-testid="split-decision-sentence"]').first()
    const takenText = (await takenSentence.textContent()) ?? ''
    check(
      /Jev suggested/.test(takenText) && /agreed/.test(takenText),
      `${theme}: agreement is stated with a VERB (${takenText.trim().slice(0, 90)})`,
    )
    check(
      takenText.split('parallel sub-agents').length - 1 === 1,
      `${theme}: the agreed case states the shape ONCE, not twice`,
    )
    check(
      /\b3\b/.test(takenText) && !/\b1 /.test(takenText),
      `${theme}: the line prints SUB-AGENTS (3), not the one call that started them`,
    )
    check(
      !(await takenSentence.evaluate(el => el.className)).includes('truncate'),
      `${theme}: the sentence WRAPS, so the verdict at its tail is never clipped`,
    )
    // Proven, not asserted from a class: at a narrow width the sentence must still
    // render every word rather than an ellipsis.
    check(
      await takenSentence.evaluate(el => el.scrollWidth <= el.clientWidth + 1),
      `${theme}: no word is clipped -- scrollWidth fits the box`,
    )
    const takenScores = (await taken.locator('[data-testid="split-decision-scores"]').textContent()) ?? ''
    check(
      /confidence 0\.84/.test(takenScores) && /190/.test(takenScores),
      `${theme}: the labelled score and the latency are on the line`,
    )

    // IGNORED: the frame the feature exists for.
    const ignored = page.locator(IGNORED).first()
    await ignored.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await rowOf(ignored).screenshot({ path: `${OUT}/02-ignored-${theme}.png` })
    console.log('wrote', `${OUT}/02-ignored-${theme}.png`)
    const ignoredText =
      (await ignored.locator('[data-testid="split-decision-sentence"]').textContent()) ?? ''
    check(
      ignoredText.trim() !== takenText.trim(),
      `${theme}: advice taken and advice ignored are told apart in words`,
    )
    check(
      /instead/.test(ignoredText) && !/different shape/.test(ignoredText),
      `${theme}: the ignored case says what happened INSTEAD, not two opposites in a row (${ignoredText.trim().slice(0, 90)})`,
    )
    check(
      (await ignored.getAttribute('data-agent-choice')) === 'single'
        && (await ignored.getAttribute('data-jev-choice')) === 'split',
      `${theme}: the two arms are readable apart`,
    )

    // BESIDE THE STRIP: two records, two keys, one reply.
    const strip = page.locator('[data-testid="decision-strip"]').first()
    await strip.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await rowOf(strip).screenshot({ path: `${OUT}/03-beside-strip-${theme}.png` })
    console.log('wrote', `${OUT}/03-beside-strip-${theme}.png`)
    check(
      await strip.isVisible(),
      `${theme}: the skill strip is still drawn on a reply that also carries this line`,
    )
    // Two thumb pairs stack on that reply, so their labels must name different
    // targets -- otherwise a reader cannot tell which one they are about to press.
    const stackedLabels = await rowOf(strip)
      .locator('[data-testid="decision-strip-rate-label-jev"]')
      .allTextContents()
    check(
      stackedLabels.length >= 2 && new Set(stackedLabels).size === stackedLabels.length,
      `${theme}: the stacked thumb pairs name different targets (${stackedLabels.join(' | ')})`,
    )
  }

  // ── The withheld pass: the fleet says no, and the receipts survive ──
  setDecisionsEnabled(false)
  await loadInEnglish('dark')
  check(
    (await page.locator(LINE).count()) >= 2,
    'withheld: the receipts for past suggestions are still drawn',
  )
  await rowOf(page.locator(TAKEN).first()).screenshot({ path: `${OUT}/04-withheld.png` })
  console.log('wrote', `${OUT}/04-withheld.png`)

  await close()

  process.exitCode = report()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
