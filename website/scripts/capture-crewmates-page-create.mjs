/**
 * Crewmates page create flow — evidence frames for the PR.
 *
 *   01-empty-<theme>    zero crewmates: the hero in the chat column
 *   02-dialog-<theme>   the real New crewmate dialog, opened from the hero,
 *                       Name + job typed into the real form
 *   02b-dialog-advanced-<theme>  the same dialog with Advanced unfolded
 *                       (workspace / model / triggers / session colour)
 *   03-created-<theme>  Radar is the only row; its chat is open with the
 *                       seeded first turn and Radar's greeting
 *   04-empty-mobile-<theme>  390x844: the hero inside the roster list, the
 *                       below-md copy (the chat column is hidden there)
 *   05-post-create-error-<theme>  the create landed but the roster re-read
 *                       failed: the notice + Try again above the chat column
 *   05b-post-create-error-mobile-<theme>  390x844, the same failure after a
 *                       FIRST create: the roster yields, the notice + Refresh
 *                       the list are the screen (nothing off-screen to the right)
 *   06-greeting-failed-<theme>  the create and the roster re-read landed, the
 *                       seeded first turn was REFUSED: Radar's chat is open
 *                       under a notice with a dismiss and "Send it again"
 *   07a-dialog-name-required-<theme>  Create pressed on a blank name: the hint
 *                       under the field, no request
 *   07b-dialog-name-taken-<theme>  the server's 409 agent_exists, said in the
 *                       dialog with the name
 *   07c-dialog-create-failed-<theme>  a 500 on the create, said in the dialog
 *   07d-dialog-options-failed-<theme>  the installed-agents read failed: the
 *                       defaults are shown with the notice
 *   07e-dialog-creating-<theme>  the create in flight: "Creating…", fields
 *                       and Cancel disabled
 *
 * Real components against stubbed /api (Vite + Playwright); the backend is
 * exercised by CI only.
 *
 * Usage:
 *   cd website && node scripts/capture-crewmates-page-create.mjs http://127.0.0.1:6931 ../temp-screenshots/crewmates-page-create
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6931'
const OUT = process.argv[3] || '../temp-screenshots/crewmates-page-create'
mkdirSync(OUT, { recursive: true })

const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30,
  last_message: "Hi! I'm Radar. Each morning I'll read the new issues, sort them by what they need, and bring you only the ones that need a decision.",
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, scene, viewport = { width: 1440, height: 900 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  const members = scene === 'done' ? [RADAR] : []
  let rosterReads = 0
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') {
      rosterReads += 1
      // roster-fail: the first read (arrival) is empty and fine; the re-read
      // after the create rejects, which is the failure the notice is about.
      if (scene === 'roster-fail' && rosterReads > 1) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"roster unavailable"}' })
      // greeting-fail: the re-read after the create finds Radar; only the
      // seeded first turn is refused (POST /api/chat above).
      // A refused greeting produced no reply, so the roster row has no preview.
      if (scene === 'greeting-fail' && rosterReads > 1) return json({ members: [{ ...RADAR, last_message: '', last_active_ts: null }], default_agent: 'kirocrew' })
      return json({ members, default_agent: 'kirocrew' })
    }
    if (path === '/api/agents' && route.request().method() === 'POST') {
      if (scene === 'taken') return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: "Agent 'Radar' already exists", code: 'agent_exists' }) })
      if (scene === 'create-fail') return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"Couldn\u2019t create the crewmate."}' })
      if (scene === 'slow') return new Promise(() => {}) // never answers: the in-flight frame
      return json({ ok: true })
    }
    if (path === '/api/chat' && route.request().method() === 'POST') {
      if (scene === 'greeting-fail') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"backend unavailable"}' })
      return json({ ok: true })
    }
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/agents/catalog' && scene === 'options-fail') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"Agent choices could not be loaded. Retry the catalog.","code":"agent_catalog_unavailable"}' })
    if (path === '/api/agents/catalog') return json({
      agents: [
        { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-autofix', selection_kind: 'template', kiro_agent: 'kirocrew-autofix', scope: 'global' },
        { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
      ],
      default_agent: 'kirocrew',
    })
    if (path === '/api/workspaces') return json({ workspaces: [{ name: 'default' }] })
    if (path === '/api/config/default-agent') return json({ default_agent: 'kirocrew' })
    if (path === '/api/autonudge') return json({ enabled: true, loops: [] })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1]).toLowerCase()
      return json({ slot_key: `member-${slug}`, slug, member: 'Radar', created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'Radar', capped: false, entries: [] })
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return json({ panel: null, html: null })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return json({ key: 'member-radar', title: 'Radar', running: false, messages: [] })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/crewmates-page-create.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Crewmates', { exact: true }).first().waitFor()
  return page
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

async function assertClean(page, tag) {
  const dialogs = await page.getByRole('dialog').count()
  check(`${tag} no stray dialog`, dialogs === 0, `dialogs=${dialogs}`)
  const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
  check(`${tag} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
  const legacy = await page.getByText(/crew member|agent template|pick a member/i).count()
  check(`${tag} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
}

for (const theme of ['dark', 'light']) {
  // 01 — empty
  {
    const page = await newPage(theme, 'empty')
    const hero = page.locator('section [data-testid=crewmate-empty-hero]')
    await hero.waitFor()
    check(`01-empty-${theme} headline`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero headline')
    check(`01-empty-${theme} sentence`, await hero.getByText(/keeps working while you are away/).isVisible(), 'hero sentence')
    check(`01-empty-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
    check(`01-empty-${theme} count`, (await page.getByTestId('member-count').textContent()) === '0 crewmates', `count=${await page.getByTestId('member-count').textContent()}`)
    // Below-md copy of the hero must not show on a wide viewport.
    const rosterHero = await page.locator('ul [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
    check(`01-empty-${theme} one hero`, !rosterHero, `roster copy visible=${rosterHero}`)
    await assertClean(page, `01-empty-${theme}`)
    await assertTheme(page, theme, `01-empty-${theme}`)
    await page.screenshot({ path: `${OUT}/01-empty-${theme}.png` })
    await page.close()
  }
  // 02 — dialog, opened from the hero's real button, filled through the real form
  {
    const page = await newPage(theme, 'empty')
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    check(`02-dialog-${theme} title`, await dlg.getByText('New crewmate', { exact: true }).isVisible(), 'dialog title')
    check(`02-dialog-${theme} built from`, await dlg.getByText('Built from', { exact: true }).isVisible() && await dlg.getByRole('combobox', { name: 'Built from' }).getByText('kirocrew (default)').isVisible(), 'Built from + default option')
    check(`02-dialog-${theme} advanced`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'false', 'Advanced folded')
    check(`02-dialog-${theme} submit`, await dlg.getByRole('button', { name: 'Create crewmate' }).isVisible(), 'primary button')
    check(`02-dialog-${theme} no options error`, (await dlg.getByTestId('crewmate-create-options-error').count()) === 0, 'options loaded')
    await page.waitForTimeout(400) // modal entrance motion
    const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
    check(`02-dialog-${theme} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
    await assertTheme(page, theme, `02-dialog-${theme}`)
    await page.screenshot({ path: `${OUT}/02-dialog-${theme}.png` })
    await page.close()
  }
  // 02b — the same dialog, Advanced unfolded. Taller viewport: the unfolded
  // dialog is ~810px and the modal caps at 90vh, so at 900 the last field
  // (session colour) sits below the fold inside the modal's own scroll.
  {
    const page = await newPage(theme, 'empty', { width: 1440, height: 1100 })
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByTestId('crewmate-create-advanced-toggle').click()
    const adv = dlg.getByTestId('crewmate-create-advanced')
    await adv.waitFor()
    check(`02b-dialog-advanced-${theme} expanded`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'true', 'aria-expanded=true')
    check(`02b-dialog-advanced-${theme} workspace`, await adv.getByRole('combobox', { name: 'Workspace' }).isVisible(), 'workspace field')
    check(`02b-dialog-advanced-${theme} model`, await adv.getByRole('combobox', { name: 'Edit default model' }).isVisible(), 'model field')
    check(`02b-dialog-advanced-${theme} triggers`, await adv.getByLabel('Triggers').isVisible(), 'triggers field')
    const colour = adv.getByLabel('Session color hex value')
    check(`02b-dialog-advanced-${theme} colour`, await colour.isVisible(), 'session colour field')
    const box = await colour.boundingBox()
    check(`02b-dialog-advanced-${theme} colour in frame`, !!box && box.y + box.height <= 1100, `colour bottom=${box ? Math.round(box.y + box.height) : 'n/a'}`)
    const legacy = await dlg.getByText(/this member|member color|crew member/i).count()
    check(`02b-dialog-advanced-${theme} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
    await page.waitForTimeout(500) // disclosure + modal motion
    await assertTheme(page, theme, `02b-dialog-advanced-${theme}`)
    await page.screenshot({ path: `${OUT}/02b-dialog-advanced-${theme}.png` })
    await page.close()
  }
  // 03 — created: Radar's chat open with the greeting
  {
    const page = await newPage(theme, 'done')
    await page.getByTestId('member-thread-header').waitFor()
    await page.getByText(/I'm Radar\./).first().waitFor()
    const rows = await page.locator('[data-testid=member-roster] li').count()
    check(`03-created-${theme} one row`, rows === 1, `rows=${rows}`)
    check(`03-created-${theme} count`, (await page.getByTestId('member-count').textContent()) === '1 crewmate', `count=${await page.getByTestId('member-count').textContent()}`)
    check(`03-created-${theme} greeting`, await page.getByText(/I'm Radar\./).first().isVisible(), 'greeting visible')
    check(`03-created-${theme} seed`, await page.getByText(/Your job: Triage new GitHub issues/).first().isVisible(), 'seeded first turn visible')
    await assertClean(page, `03-created-${theme}`)
    await page.waitForTimeout(400)
    await assertTheme(page, theme, `03-created-${theme}`)
    await page.screenshot({ path: `${OUT}/03-created-${theme}.png` })
    await page.close()
  }
}

// 05 — post-create failure: the create landed, the roster re-read did not
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail')
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' }) // exit motion
  check(`05-post-create-error-${theme} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`05-post-create-error-${theme} retry`, await notice.getByTestId('member-post-create-retry').isVisible() && (await notice.getByTestId('member-post-create-retry').textContent()) === 'Refresh the list', 'Refresh the list')
  check(`05-post-create-error-${theme} no dismiss`, (await notice.getByRole('button', { name: /dismiss/i }).count()) === 0, 'a roster failure has no dismiss')
  const textBox = await notice.getByText("Radar was created, but the list didn't refresh.").boundingBox()
  const retryBox = await notice.getByTestId('member-post-create-retry').boundingBox()
  check(`05-post-create-error-${theme} retry adjacent`, !!textBox && !!retryBox && retryBox.x - (textBox.x + textBox.width) < 120, `gap=${textBox && retryBox ? Math.round(retryBox.x - (textBox.x + textBox.width)) : 'n/a'}px`)
  check(`05-post-create-error-${theme} no hero`, (await page.locator('section [data-testid=crewmate-empty-hero]').count()) === 0, 'hero yields to the notice')
  check(`05-post-create-error-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'dialog closed')
  check(`05-post-create-error-${theme} no stale count`, (await page.getByTestId('member-count').count()) === 0, 'count steps aside while the list is stale')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `05-post-create-error-${theme}`)
  await page.screenshot({ path: `${OUT}/05-post-create-error-${theme}.png` })
  await page.close()
}

// 05b — below md, first create, roster re-read failed: no chat is open, so
// only the notice keeps the column shown; the full-width roster must yield
// or the notice and its retry sit off-screen to the right (GPT round 4).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail', { width: 390, height: 844 })
  await page.locator('ul [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  const retry = notice.getByTestId('member-post-create-retry')
  const box = await retry.boundingBox()
  check(`05b-post-create-error-mobile-${theme} retry on screen`, !!box && box.x >= 0 && box.x + box.width <= 390 && box.y + box.height <= 844, `retry box=${JSON.stringify(box)}`)
  check(`05b-post-create-error-mobile-${theme} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`05b-post-create-error-mobile-${theme} roster yields`, !(await page.getByTestId('member-roster').isVisible()), 'roster hidden below md while the notice is up')
  check(`05b-post-create-error-mobile-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'dialog closed')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `05b-post-create-error-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/05b-post-create-error-mobile-${theme}.png` })
  await page.close()
}

// 06 — greeting refused: chat open, notice with dismiss + "Send it again"
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'greeting-fail')
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' })
  await page.getByTestId('member-thread-header').waitFor()
  check(`06-greeting-failed-${theme} text`, await notice.getByText("Radar was created, but its first message didn't send.").isVisible(), 'notice text')
  check(`06-greeting-failed-${theme} retry`, (await notice.getByTestId('member-post-create-retry').textContent()) === 'Send it again', 'Send it again')
  check(`06-greeting-failed-${theme} dismiss`, (await notice.getByRole('button', { name: /dismiss/i }).count()) === 1, 'a greeting failure can be dismissed')
  check(`06-greeting-failed-${theme} chat open`, await page.getByTestId('member-thread-header').getByText('Radar', { exact: true }).isVisible(), "Radar's chat is open under the notice")
  check(`06-greeting-failed-${theme} no preview`, (await page.getByText(/I'm Radar\./).count()) === 0, 'no invented reply preview in the roster')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `06-greeting-failed-${theme}`)
  await page.screenshot({ path: `${OUT}/06-greeting-failed-${theme}.png` })
  await page.close()
}

// 07 — the dialog's own states
async function openDialog(theme, scene) {
  const page = await newPage(theme, scene)
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await page.waitForTimeout(400) // modal entrance motion
  return { page, dlg }
}
for (const theme of ['dark', 'light']) {
  {
    const { page, dlg } = await openDialog(theme, 'empty')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const hint = dlg.getByText('Give your crewmate a name.', { exact: true })
    await hint.waitFor()
    check(`07a-dialog-name-required-${theme} hint`, await hint.isVisible(), 'hint under the field')
    check(`07a-dialog-name-required-${theme} invalid`, (await dlg.getByLabel('Name').getAttribute('aria-invalid')) === 'true', 'aria-invalid')
    await page.waitForTimeout(400) // the field's border-color transition (focus-ring, 0.2s) settles
    const hintRole = await dlg.getByTestId('crewmate-create-name-hint').getAttribute('role')
    const nameBorder = await dlg.getByLabel('Name').evaluate((el) => getComputedStyle(el).borderColor)
    const jobBorder = await dlg.getByLabel('What it looks after').evaluate((el) => getComputedStyle(el).borderColor)
    check(`07a-dialog-name-required-${theme} error tone`, hintRole === 'alert' && nameBorder !== jobBorder, `role=${hintRole} name-border=${nameBorder} job-border=${jobBorder}`)
    check(`07a-dialog-name-required-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    await assertTheme(page, theme, `07a-dialog-name-required-${theme}`)
    await page.screenshot({ path: `${OUT}/07a-dialog-name-required-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'taken')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const err = dlg.getByTestId('crewmate-create-error')
    await err.waitFor()
    check(`07b-dialog-name-taken-${theme} text`, await err.getByText('A crewmate named Radar already exists.', { exact: true }).isVisible(), 'said with the name')
    check(`07b-dialog-name-taken-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    check(`07b-dialog-name-taken-${theme} name kept`, (await dlg.getByLabel('Name').inputValue()) === 'Radar', 'draft kept')
    await assertTheme(page, theme, `07b-dialog-name-taken-${theme}`)
    await page.screenshot({ path: `${OUT}/07b-dialog-name-taken-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'create-fail')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const err = dlg.getByTestId('crewmate-create-error')
    await err.waitFor()
    check(`07c-dialog-create-failed-${theme} text`, await err.getByText(/create the crewmate/).isVisible(), 'failure said in the dialog')
    check(`07c-dialog-create-failed-${theme} still open`, (await page.getByRole('dialog').count()) === 1, 'dialog stays')
    await assertTheme(page, theme, `07c-dialog-create-failed-${theme}`)
    await page.screenshot({ path: `${OUT}/07c-dialog-create-failed-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'options-fail')
    const err = dlg.getByTestId('crewmate-create-options-error')
    await err.waitFor()
    check(`07d-dialog-options-failed-${theme} text`, await err.getByText('Couldn\'t load the Built from choices; the default is shown.', { exact: true }).isVisible(), 'options notice names the list')
    check(`07d-dialog-options-failed-${theme} default kept`, await dlg.getByRole('combobox', { name: 'Built from' }).getByText('kirocrew (default)').isVisible(), 'built-in default still offered')
    await assertTheme(page, theme, `07d-dialog-options-failed-${theme}`)
    await page.screenshot({ path: `${OUT}/07d-dialog-options-failed-${theme}.png` })
    await page.close()
  }
  {
    const { page, dlg } = await openDialog(theme, 'slow')
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByRole('button', { name: 'Create crewmate' }).click()
    const busy = dlg.getByRole('button', { name: 'Creating…' })
    await busy.waitFor()
    check(`07e-dialog-creating-${theme} busy label`, await busy.isDisabled(), 'Creating… disabled')
    check(`07e-dialog-creating-${theme} fields locked`, await dlg.getByLabel('Name').isDisabled(), 'name field disabled')
    check(`07e-dialog-creating-${theme} cancel locked`, await dlg.getByRole('button', { name: 'Cancel' }).isDisabled(), 'Cancel disabled while pending')
    check(`07e-dialog-creating-${theme} advanced locked`, await dlg.getByTestId('crewmate-create-advanced-toggle').isDisabled(), 'Advanced toggle disabled while pending (whole-form fieldset)')
    await assertTheme(page, theme, `07e-dialog-creating-${theme}`)
    await page.screenshot({ path: `${OUT}/07e-dialog-creating-${theme}.png` })
    await page.close()
  }
}

// 04 — below md: the hero inside the roster list (dark only is enough for the
// layout question, light proves the tokens; shoot both).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'empty', { width: 390, height: 844 })
  const hero = page.locator('ul [data-testid=crewmate-empty-hero]')
  await hero.waitFor()
  check(`04-empty-mobile-${theme} hero`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero in the roster list')
  check(`04-empty-mobile-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
  const desktopHero = await page.locator('section [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
  check(`04-empty-mobile-${theme} one hero`, !desktopHero, `chat-column copy visible=${desktopHero}`)
  await assertTheme(page, theme, `04-empty-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/04-empty-mobile-${theme}.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
