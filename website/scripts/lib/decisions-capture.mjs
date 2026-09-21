/**
 * The setup every Jev decision-point screenshot harness needs, spelled once.
 *
 * Three things each of these harnesses does identically, because the seam gates
 * every point the same way and every one of them photographs an English surface:
 *
 *  - CONSENT and the FLEET ANSWER. A point draws nothing until the keystone
 *    consent permits the configured endpoint and `decisions_enabled` is true, so
 *    both are stubbed, and the fleet answer is flippable so a harness can also
 *    photograph the withheld state.
 *  - the ENGLISH RELOAD. `openTranscriptHarness`'s init script clears
 *    localStorage on every navigation, so the locale pin has to be registered as
 *    a LATER init script and a reload is what makes it stick.
 *  - the ASSERTION TALLY. A capture run that only writes PNGs proves nothing; each
 *    harness asserts what its own frames show and exits non-zero on a miss.
 *
 * Extracted rather than copied: two harnesses carrying the same fifty lines is a
 * copy/paste finding, and a divergence between two stubs of one gate would make
 * one harness photograph a state the other cannot reach.
 */
import { json } from './boot-api.mjs'

/** Consent that is on and pointed at the address it was given for. */
export const CONSENTED = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
}

/**
 * A pass/fail tally whose `report` decides the process exit.
 *
 * Returned rather than exported as module state so two harnesses in one process
 * cannot share a tally, and so a test can hold one without a global reset.
 */
export function createChecks() {
  const failures = []
  return {
    check(ok, msg) {
      if (!ok) failures.push(msg)
      console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`)
    },
    /** Print every miss and return the exit code the harness should use. */
    report() {
      if (!failures.length) {
        console.log('\nall assertions passed')
        return 0
      }
      console.error(`\n${failures.length} assertion(s) failed:`)
      for (const f of failures) console.error(`  - ${f}`)
      return 1
    },
  }
}

/**
 * Stub the seam's two gates and hand back an English-pinned loader.
 *
 * *selector* is what a load waits for — the surface the harness is about to
 * photograph. `setDecisionsEnabled(false)` flips only the FLEET answer, which is
 * what the withheld pass needs: consent stays on, so a harness can show that a
 * past receipt survives a withdrawal while a future decision would not be made.
 *
 * The routes are registered AFTER the harness's own catch-all so they win —
 * Playwright matches route handlers in reverse order.
 */
export async function stubDecisionsSeam({ page, load, selector, consent = CONSENTED }) {
  let decisionsEnabled = true
  await page.route('**/api/decisions/consent', route => json(route, consent))
  await page.route('**/api/dashboard/config', route =>
    json(route, {
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
      decisions_enabled: decisionsEnabled,
    }),
  )

  let localePinned = false
  return {
    setDecisionsEnabled(value) {
      decisionsEnabled = value
    },
    /** Load *theme* with the locale pinned to English, then settle. */
    async loadInEnglish(theme) {
      await load(theme, { selector })
      if (!localePinned) {
        await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
        localePinned = true
      }
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.waitForSelector(selector, { timeout: 20000 })
      await page.waitForTimeout(800)
    },
  }
}
