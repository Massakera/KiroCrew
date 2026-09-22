/**
 * Screenshot harness for the Crew Members page header after the Cloud entry
 * was removed: the header row carries the brand mark, the title and the "+"
 * add-member button, and nothing else.
 *
 * Drives website/capture/members-page.html on a vite dev server, gateway-free:
 * every /api call is answered from a small route table. Each frame is preceded
 * by DOM checks -- no element carries the `member-deploy-open` test id, exactly
 * one carries `member-add` -- so a screenshot is only written for the state it
 * claims to show.
 *
 * Frames, per theme (dark, light), at deviceScaleFactor 2:
 *   01-header-<theme>   the header row alone (mark + title + "+")
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6836 --strictPort   # in another shell
 *   node scripts/capture-members-header-no-cloud.mjs http://127.0.0.1:6836 ../temp-screenshots/members-no-cloud
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6836'
const OUT = process.argv[3] || '../temp-screenshots/members-no-cloud'
mkdirSync(OUT, { recursive: true })

const ROSTER = { members: [
  { name: 'Oncall', slug: 'oncall', slot_key: 'member-oncall', running: false, last_active_ts: 0, source: 'kirocrew' },
], default_agent: 'kirocrew' }

/** Route table: a pathname test and the JSON body it earns. First match wins;
 *  anything unmatched gets an empty list or object by shape. */
const ROUTES = [
  [(p, m) => p === '/api/members' && m === 'GET', ROSTER],
  [(p, m) => /\/thread$/.test(p) && m === 'POST', { slot_key: 'member-oncall', slug: 'oncall', member: 'Oncall' }],
  [(p) => /\/activity(\?|$)/.test(p), { slug: 'oncall', member: 'Oncall', entries: [], capped: false }],
  [(p) => /\/webhooks(\?|$)/.test(p), { tokens: [] }],
  [(p) => p === '/api/agents', { agents: [], default_agent: 'kirocrew' }],
  [(p) => /commands|skills|sessions|files|history|models|artifacts|folders|crons|jobs/.test(p), []],
]

let failed = false
function check(name, ok, detail = '') {
  console.log(`${ok ? 'ok   ' : 'FAIL '} ${name}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) failed = true
}

function stub(page) {
  return page.route((u) => new URL(u).pathname.startsWith('/api/'), (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const hit = ROUTES.find(([match]) => match(path, req.method()))
    const body = hit ? hit[1] : {}
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}

async function shoot(browser, theme) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  await stub(page)
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  const roster = page.getByTestId('member-roster')
  await roster.getByText('Oncall').first().waitFor({ state: 'visible', timeout: 20000 })
  check(`[${theme}] no Cloud entry anywhere on the page`, (await page.getByTestId('member-deploy-open').count()) === 0)
  check(`[${theme}] exactly one add-member button`, (await page.getByTestId('member-add').count()) === 1)
  check(`[${theme}] no Lucide Cloud glyph in the header`, (await roster.locator('svg.lucide-cloud').count()) === 0)
  check(`[${theme}] no open dialog covers the frame`, (await page.getByRole('dialog').count()) === 0)
  await page.waitForTimeout(300)
  const headerBox = await roster.locator('h1').first().evaluate((h) => {
    const r = h.parentElement.parentElement.getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
  await page.screenshot({ path: join(OUT, `01-header-${theme}.png`), clip: headerBox })
  await page.close()
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
try {
  for (const theme of ['dark', 'light']) await shoot(browser, theme)
} finally {
  await browser.close()
}
if (failed) { console.error('CAPTURE FAILED'); process.exit(1) }
console.log('wrote', OUT)
