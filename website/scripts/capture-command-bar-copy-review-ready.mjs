/**
 * Screenshot harness for the launcher row that copies the review-ready PR search link.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli -- which is what lets it run on a host where the
 * pod's port-ownership proof cannot be made.
 *
 * Two frames, the two states a reader needs to judge the row:
 *   1. the row at rest, with an empty query. `idleDemote` puts it LAST in the
 *      Commands group, so this frame also proves it does not take the default
 *      first slot from New Session.
 *   2. the row after a failed activation. The endpoint is stubbed 502, so the
 *      fetch rejects before any clipboard write: the bar stays open, the notice
 *      names the row and says Enter retries, and nothing was copied. This is the
 *      frame worth photographing most -- the row's whole contract is that a
 *      failure reaches the bar instead of leaving a wrong URL on the clipboard.
 *
 * Usage: node scripts/capture-command-bar-copy-review-ready.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-copy-review-ready'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const ROW = 'Copy Review-Ready PR Search'

/**
 * The launcher is a builtin that REPLACES the quick-search overlay, so it has to be
 * installed for Control+k to open this bar rather than the plain search. The row
 * itself is a builtin of the launcher rather than a contribution, but it is GATED on
 * Issue Radar being enabled, so that app has to be present and on for the row to
 * exist at all.
 */
const APPS = [
  {
    name: 'command-bar',
    displayName: 'Command Bar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: {
      name: 'command-bar',
      displayName: 'Command Bar',
      version: '0.1.0',
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
  },
  // Issue Radar, ENABLED. Not decoration: the row is gated on this app being on,
  // because the endpoint it calls answers 403 while the app is off. Drop this record
  // and the row correctly disappears -- and the assertions below would fail rather
  // than quietly shoot an empty group.
  {
    name: 'issue-radar',
    displayName: 'Issue Radar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: { name: 'issue-radar', displayName: 'Issue Radar', version: '0.1.0' },
  },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/**
 * @param {(path: string, route: import('playwright').Route) => unknown} [endpoint]
 *   Answers the review-ready endpoint. Omit it to leave the row unactivated.
 */
async function openBar(endpoint) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, APPS)
      return true
    }
    if (endpoint && path === '/api/apps/issue-radar/review-ready-search-url') {
      await endpoint(path, route)
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  // The quick-search chord. The overlay claims the slot, so this opens the launcher.
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

async function shot(page, name) {
  await page.waitForTimeout(350)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

// -- 1. the row at rest, last in the Commands group --------------------------
{
  const { context, page } = await openBar()
  // Assert the row is THERE before shooting: a frame of a bar that happens not to
  // render it would read as evidence of the opposite.
  await page.getByRole('option').filter({ hasText: ROW }).first().waitFor({ timeout: 10_000 })
  await shot(page, '1-row-idle.png')
  await context.close()
}

// -- 2. a failed activation: the bar stays open and says so ------------------
{
  const { context, page } = await openBar(async (_path, route) => {
    await json(route, { error: 'could not resolve a GitHub identity' }, 502)
  })
  const row = page.getByRole('option').filter({ hasText: ROW }).first()
  // Activate by clicking: mousedown is what the list binds.
  await row.dispatchEvent('mousedown')
  // Scope the wait to the bar. The SPA ships a hidden `#boot-failure` div that also
  // carries role="alert", so a bare [role="alert"] resolves it first and never
  // becomes visible.
  await page.locator('[role="dialog"] [role="alert"]').first().waitFor({ timeout: 10_000 })
  await shot(page, '2-activation-failed.png')
  await context.close()
}

await browser.close()
srv.close()
