/**
 * Evidence for the channel approval card driven by the message's structured
 * `meta` (#5250): the server's own verdict on which trust tiers it can record.
 *
 * Mounts the REAL MessageBubble (real body, real ApprovalCard, real
 * TrustDropdown) from messages shaped exactly as `_stream_task` posts them,
 * prose AND meta. One scene per state the card can be in:
 *   simple    - `ls -la /workplace/project`: meta grants every tier; the base
 *               tier names the server-derived binary.
 *   compound  - `cat f | wc -l`: the server derives no base, so the base tier
 *               is withheld on its word (the prose path offered "cat").
 *   nonshell  - `cron_add`: no per-command tier at all; the one control IS the
 *               blanket channel grant.
 *   legacy    - the same simple command persisted BEFORE meta existed (no meta):
 *               the prose path, unchanged.
 *
 *   ?theme=dark|light&scene=simple|compound|nonshell|legacy
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import { MessageBubble } from '../src/pages/ChannelPage'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import type { RootState } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'simple'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SIMPLE = 'ls -la /workplace/project'
const COMPOUND = 'cat /workplace/project/README.md | wc -l'

function shell(cmd: string, base: string | null) {
  const title = `Running: ${cmd}`
  const input = JSON.stringify({ command: cmd })
  return {
    content: `⚠️ Approval needed: **${title}**\n\`\`\`\n${input}\n\`\`\``,
    meta: {
      request_id: '7', tool_title: title, tool_input: input, is_shell: '1', command_grantable: '1',
      base_derivable: base ? '1' : '', base_command: base ?? '',
    },
  }
}

const SCENES: Record<string, { content: string; meta?: Record<string, string> }> = {
  simple: shell(SIMPLE, 'ls'),
  compound: shell(COMPOUND, null),
  nonshell: {
    content: '⚠️ Approval needed: **cron_add**\n```\n{"name": "nightly-digest", "cron_expr": "0 9 * * *"}\n```',
    meta: {
      request_id: '7', tool_title: 'cron_add', tool_input: '{"name": "nightly-digest", "cron_expr": "0 9 * * *"}',
      is_shell: '', command_grantable: '', base_derivable: '', base_command: '',
    },
  },
  legacy: { content: shell(SIMPLE, 'ls').content },
}
const picked = SCENES[scene] ?? SCENES.simple

await initI18n()

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer },
  preloadedState: {
    // `normal` is the mode that renders the decision controls at all.
    dashboard: { approvalMode: 'normal' } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
  },
})

const msg = {
  id: 'm1',
  fromId: 'a1',
  fromRole: 'dev',
  content: picked.content,
  meta: picked.meta,
  msgType: 'approval',
  timestamp: '2026-09-23T10:00:00Z',
  replyCount: 0,
} as unknown as Parameters<typeof MessageBubble>[0]['msg']

const agents = [{ id: 'a1', role: 'dev' }] as unknown as Parameters<typeof MessageBubble>[0]['agents']

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <div data-capture-root className="bg-bg text-text p-5 w-[760px]">
      <MessageBubble msg={msg} agents={agents} onApprove={() => Promise.resolve()} />
    </div>
  </Provider>,
)
