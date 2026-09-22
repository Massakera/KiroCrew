/**
 * Screenshots of Settings › Notifications › **System notifications** in every
 * permission state (capture/system-notifications-row.html) — the row whose
 * `host-managed` state this PR adds.
 *
 * Self-checking: each frame asserts the copy that state is SUPPOSED to show, and
 * asserts the copy the other states show is absent. A screenshot of a row stuck
 * in one state, or captured before i18n initialised, is worse evidence than none
 * — and the whole point of the `host-managed` frame is that it says neither
 * "blocked in your browser" nor "Allowed".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort    # in another shell
 *   node scripts/capture-system-notifications-row.mjs http://127.0.0.1:6842 ../temp-screenshots/system-notifications-row
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/system-notifications-row'
mkdirSync(OUT, { recursive: true })

const PAGE = '/capture/system-notifications-row.html'

/** `expect` is text that MUST be on the frame; `absent` is text that must not. */
const STATES = [
  {
    name: 'default',
    query: 'permission=default',
    expect: [/Allow system notifications/i],
    absent: [/Blocked in your browser/i, /main Kiro Crew window/i],
  },
  {
    name: 'granted',
    query: 'permission=granted',
    expect: [/Allowed/],
    absent: [/Blocked in your browser/i, /main Kiro Crew window/i],
  },
  {
    name: 'denied',
    query: 'permission=denied',
    expect: [/Blocked in your browser/i],
    absent: [/main Kiro Crew window/i, /Allow system notifications/i],
  },
  {
    // The new state, reached by really being in a frame rather than by a stub.
    name: 'host-managed-pane',
    query: 'embed=1',
    frame: true,
    expect: [/main Kiro Crew window/i, /Settings → Notifications/],
    absent: [/Blocked in your browser/i, /Allow system notifications/i],
  },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 620, height: 200 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  for (const state of STATES) {
    await page.goto(`${BASE}${PAGE}?theme=${theme}&${state.query}`)

    // The row lives in the iframe for the embedded state, so assert against
    // whichever document actually renders it.
    const scope = state.frame ? page.frameLocator('iframe') : page
    await scope.locator('[data-testid="system-notifications-row"]').waitFor({ state: 'visible' })

    const text = await scope.locator('[data-testid="system-notifications-row"]').innerText()
    // "System notifications" is an i18n label; blank means i18n never initialised.
    if (!/System notifications/i.test(text)) {
      throw new Error(`${state.name}/${theme}: row label missing — i18n likely uninitialised (got ${JSON.stringify(text)})`)
    }
    for (const re of state.expect) {
      if (!re.test(text)) throw new Error(`${state.name}/${theme}: expected ${re} in ${JSON.stringify(text)}`)
    }
    for (const re of state.absent) {
      if (re.test(text)) throw new Error(`${state.name}/${theme}: ${re} must NOT appear, got ${JSON.stringify(text)}`)
    }

    await page.screenshot({ path: join(OUT, `system-notifications-${state.name}-${theme}.png`) })
    console.log(`captured system-notifications-${state.name}-${theme}.png`)
  }
}

await browser.close()
console.log(`done → ${OUT}`)
