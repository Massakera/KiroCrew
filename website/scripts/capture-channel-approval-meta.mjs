/**
 * Screenshots of the channel approval card driven by message `meta` (#5250).
 *
 * Drives website/capture/channel-approval-meta.html, which mounts the REAL
 * MessageBubble from messages shaped as `_stream_task` posts them. Every frame
 * ASSERTS the state it claims before the file is written, so a regression
 * produces no misleading image:
 *   simple    - open menu: exact tier names the command, base tier names `ls`
 *               (the server-derived binary), blanket channel tier; then the
 *               scope-accurate confirmation after the base tier is chosen.
 *   compound  - open menu: exact + blanket only; nothing offers a `cat` base.
 *   nonshell  - no menu; the one control is the blanket channel grant.
 *   legacy    - message without meta: the prose path, three tiers as before.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6851 --strictPort   # in another shell
 *   node scripts/capture-channel-approval-meta.mjs http://127.0.0.1:6851 ../temp-screenshots/channel-approval-meta
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6851'
const OUT = process.argv[3] || '../temp-screenshots/channel-approval-meta'
mkdirSync(OUT, { recursive: true })

const SCENES = []
for (const theme of ['dark', 'light']) {
  SCENES.push(
    { scene: 'simple', theme, open: true, expectItems: 3, confirmBase: true },
    { scene: 'compound', theme, open: true, expectItems: 2 },
    { scene: 'nonshell', theme, open: false },
    { scene: 'legacy', theme, open: true, expectItems: 3 },
  )
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 800, height: 380 }, deviceScaleFactor: 2 })

async function settledMenu() {
  await page.waitForSelector('[role="menuitem"]')
  // Radix animates the menu in; a mid-animation frame shows the card bleeding
  // through the menu.
  await page.locator('[role="menu"]').first().evaluate(
    el => Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished)),
  )
  return (await page.locator('[role="menuitem"]').allInnerTexts()).map(t => t.trim())
}

let failed = false
for (const s of SCENES) {
  const name = `channel-approval-meta-${s.scene}-${s.theme}`
  await page.goto(`${BASE}/capture/channel-approval-meta.html?theme=${s.theme}&scene=${s.scene}`)
  await page.waitForSelector('[data-capture-root]')
  await page.waitForSelector('[data-capture-root] button')
  // No stray dialog may sit on top of the frame.
  if (await page.locator('[role="dialog"]').count()) { console.log(`${name}: unexpected dialog`); failed = true; continue }

  let ok
  let detail
  if (!s.open) {
    const trust = page.locator('[data-capture-root] button', { hasText: /^Trust/ })
    const label = (await trust.first().innerText()).trim()
    ok = /Trust all tools in this channel/.test(label) && (await page.locator('[role="menuitem"]').count()) === 0
    detail = `trust=${JSON.stringify(label)}`
  } else {
    await page.getByRole('button', { name: /^Trust$/ }).click()
    const items = await settledMenu()
    const exact = items[0] ?? ''
    const offersCommand = exact.includes('ls -la /workplace/project') || exact.includes('cat /workplace/project/README.md | wc -l')
    const baseItems = items.filter(t => /commands/.test(t))
    const hasAll = items.some(t => /Trust all tools in this channel/.test(t))
    if (s.scene === 'simple') {
      ok = items.length === 3 && offersCommand && /until restart/.test(exact)
        && baseItems.length === 1 && /Trust all ls commands for @dev — until restart/.test(baseItems[0]) && hasAll
    } else if (s.scene === 'compound') {
      ok = items.length === 2 && offersCommand && baseItems.length === 0 && hasAll
        && !items.some(t => /\bcat\b commands/.test(t))
    } else {
      // legacy: prose path, unchanged -- three tiers, base from the first token.
      ok = items.length === 3 && offersCommand && baseItems.length === 1 && /\bls\b/.test(baseItems[0]) && hasAll
    }
    detail = `items=${JSON.stringify(items)}`
  }
  console.log(`${name}: ${detail} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.screenshot({ path: `${OUT}/${name}.png` })

  if (s.confirmBase) {
    await page.locator('[role="menuitem"]', { hasText: /commands/ }).click()
    const confirmation = page.locator('[data-capture-root]', { hasText: /Trusted — ls commands are auto-approved for @dev until restart/ })
    await confirmation.waitFor({ timeout: 5000 })
    const gone = (await page.locator('[data-capture-root] button', { hasText: /^Approve$/ }).count()) === 0
    console.log(`${name}-confirmed: buttons-gone=${gone} ${gone ? 'OK' : 'MISMATCH'}`)
    if (!gone) { failed = true; continue }
    await page.screenshot({ path: `${OUT}/${name}-confirmed.png` })
  }
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state — no misleading frame written')
  process.exit(1)
}
console.log(`wrote screenshots to ${OUT}`)
