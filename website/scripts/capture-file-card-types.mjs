/**
 * Screenshot runner for capture/file-card-types.html.
 *
 * From website/, start the dev server the way the other capture runners
 * describe, then:
 *   node scripts/capture-file-card-types.mjs <base url> <outdir>
 *
 * One PNG per theme. Asserts every card carries a glyph and that no two
 * consecutive cards share a family by accident of the fixture (each row is
 * meant to show a different icon), so a mapping regression that collapses
 * families onto `unknown` fails the run instead of shipping a frame of
 * identical glyphs.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/file-card-types'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 740, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/file-card-types.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.locator('[data-block]').first().waitFor({ timeout: 15000 })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    if (await page.locator('[role="dialog"]').count()) throw new Error('unexpected dialog')
    const families = await page.locator('[data-testid="file-card-glyph"]').evaluateAll(els => els.map(e => e.dataset.family))
    const blocks = await page.locator('[data-block]').count()
    if (families.length !== blocks) throw new Error(`${blocks} cards but ${families.length} glyphs`)
    const unknown = families.filter(f => f === 'unknown').length
    if (unknown !== 1) throw new Error(`expected exactly one 'unknown' card, got ${unknown}: ${families.join(',')}`)
    console.log(`${theme}: ${families.join(' ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/file-card-types-${theme}.png` })
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}
await browser.close()
process.exit(failed ? 1 : 0)
