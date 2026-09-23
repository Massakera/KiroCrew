import { useState } from 'react'
import { Zap } from 'lucide-react'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'

export default function RestartButton({ askAgent = false }: {
  /**
   * Forwarded to the failure notice's agent hand-off. **Off by default, and the
   * mount site decides** — the hand-off navigates to the chat, which unmounts
   * the page this button sits in, so it is safe only where that page holds no
   * unsaved draft. The Connections header keeps it off: the MCP servers tab
   * beneath it carries an editable server form.
   */
  askAgent?: boolean
}) {
  const [restarting, setRestarting] = useState(false)
  const [ok, setOk] = useState('')
  const [err, setErr] = useState('')

  const restart = async () => {
    // The reassurance lives at the moment of the click, not only in a hover
    // title: "Restart" reads as breaking something in progress, and a reader
    // who will not press it never gets a saved template change into a running
    // chat. The confirm says what stays (chats, history) and what stops (a
    // reply in progress) before anything happens.
    if (!window.confirm(i18nT('components.restartButton.confirm'))) return
    setRestarting(true)
    setErr('')
    setOk('')
    try {
      const res = await api.restartSessions()
      // The sessions DID restart, but a failed reconcile means they restarted
      // against a config that may not match the sources — reporting "config
      // applied" there would be the exact lie this button exists to avoid.
      if (res && res.mcp_sync_ok === false) {
        setErr(i18nT('components.restartButton.sessions_restarted_but_mcp_sync_failed'))
      } else {
        setOk(i18nT('components.restartButton.sessions_restarted_config_applied'))
        // Success is a passing confirmation, so it clears itself. A failure
        // stays until dismissed or the next attempt: an error that vanishes
        // after five seconds is one the reader may never have seen.
        setTimeout(() => setOk(''), 5000)
      }
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : i18nT('components.restartButton.restart_failed'))
    } finally {
      setRestarting(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      {ok && <span className="text-[13px] animate-rise text-ok">{ok}</span>}
      {/* The failure goes through ErrorNotice like every other error the user
          sees, so the journal lookup and the hand-off apply; `inline` because
          this sits in a header row, not at the top of a panel. */}
      <ErrorNotice
        message={err}
        variant="inline"
        askAgent={askAgent}
        onDismiss={() => setErr('')}
        testId="restart-button-error"
      />
      <button
        onClick={restart}
        disabled={restarting}
        title={i18nT('components.restartButton.apply_restart_hint')}
        className={`group relative inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[13px] font-semibold font-body cursor-pointer transition-all duration-300 overflow-hidden border-none ${
          restarting
            ? 'bg-accent/60 text-accent-fg/80 cursor-wait'
            : 'bg-gradient-to-r from-accent to-accent-hover text-accent-fg shadow-[0_2px_8px_var(--accent-glow)] hover:shadow-[0_4px_20px_var(--accent-glow)]'
        }`}
      >
        {restarting && <span className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />}
        <span className={`transition-transform duration-300 ${restarting ? 'animate-spin' : 'group-hover:rotate-12'}`}><Zap className="lucide-inline" /></span>
        {restarting
          ? <span>{i18nT('components.restartButton.restarting')}</span>
          : <span>{i18nT('components.restartButton.apply_restart')}</span>
        }
      </button>
    </div>
  )
}
