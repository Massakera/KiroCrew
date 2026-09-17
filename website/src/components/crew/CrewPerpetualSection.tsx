/**
 * Perpetual mode, on the crewmate's detail page (the crew editor's "what wakes
 * this crew" pane), above the schedules.
 *
 * Perpetual mode is the crewmate's restart policy, so it is a SETTING and
 * lives in the HR file next to Built from and the schedules -- not in the
 * chat's side panel, which shows what the crewmate did. The owner's switch is
 * the one control: ON = the crewmate keeps waking on its own thread with no
 * cycle or time cap (it sets its own wake interval); OFF = it works only when
 * asked. The state itself is also visible where a manager looks (the roster
 * row's badge; the roster payload's `perpetual`), but the reason a loop is
 * off is read here.
 *
 * The switch holds NO truth of its own: its position is the loop registry's
 * record (`useCrewPerpetual`), and a press only asks the server
 * (`POST /api/members/{slug}/perpetual`). The registry is re-read when the
 * answer lands -- success or failure -- so a refused press settles back on
 * what the backend holds, never on an optimistic ON. While the request is out
 * the switch is disabled, not flipped: "asked, not yet answered".
 *
 * A refusal renders in plain words through `ErrorNotice`, coded answers first
 * (the server's `code`): the thread must be open once before the switch can
 * address it; a structured monitor is not this switch's loop; a gateway with
 * auto-nudge off has no loop to arm. Anything else shows the server's own
 * sentence. The host remounts this section per crew (`key={editing}`), so a
 * pending press or an error for one crewmate never shows on another.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Goal } from 'lucide-react'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { parseErrorCode } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'
import { Skeleton, Toggle } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { AUTONUDGE_LOOPS_QUERY_KEY, intervalText, nextCycle } from '../autoNudgeLoop'
import { timeAgo } from '../../utils/timeAgo'
import { fmtDateTimeNumeric } from '../../i18n/format'
import { useCrewPerpetual } from './useCrewPerpetual'

/** The plain-words sentence for each coded stop the loop record can carry.
 *  An unknown code falls back to the code itself rather than to a sentence
 *  nothing produced (the Crewmates page keeps the same table). */
const STOPPED_REASON_KEY: Record<string, string> = {
  manual: 'components.crewPerpetualSection.off_by_you',
  autonudge_stop: 'components.crewPerpetualSection.off_by_crewmate',
  cycle_cap: 'pages.membersPage.patrol_stopped_cycle_cap',
  runtime_budget: 'pages.membersPage.patrol_stopped_runtime_budget',
  approval_stalled: 'pages.membersPage.patrol_stopped_approval_stalled',
  interrupted: 'pages.membersPage.patrol_stopped_interrupted',
}

/** Coded refusals the switch can foresee, in plain words. */
const REFUSAL_KEY: Record<string, string> = {
  member_thread_not_open: 'components.crewPerpetualSection.refused_thread_not_open',
  structured_monitor_not_convertible: 'components.crewPerpetualSection.refused_structured_monitor',
  autonudge_disabled: 'components.crewPerpetualSection.refused_autonudge_disabled',
}

/** Clock for the "next wake" countdown. Coarse on purpose: this is an
 *  at-a-glance status line, not the popover's per-second readout. */
const TICK_MS = 15_000

/** How long "Saved" stays beside the switch after a press lands. */
const SAVED_MS = 2_500

export default function CrewPerpetualSection({ crew }: { crew: string }) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()
  const queryClient = useQueryClient()
  const { slug, slotKey, loop, state, loaded, failed, missing, monitor } = useCrewPerpetual(crew)

  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.memberPerpetualSet(slug, crew, enabled),
    onSettled: () => {
      // Both readers of the switch: the registry (the switch's own position and
      // readouts) and the roster (its `perpetual` field, the badge on the
      // Crewmates page). Settled, not success -- a refusal must re-read too, so
      // the switch settles on what the backend holds.
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    },
  })
  const pending = mutation.isPending
  // A press that landed says so, briefly, where "Saving…" just was: every press
  // happens beside a Save footer that stays disabled, so success is stated
  // rather than left to be inferred from the card changing. Keyed on the
  // mutation's submit time so a second press restarts the window.
  const [savedFor, setSavedFor] = useState<number | null>(null)
  useEffect(() => {
    if (!mutation.isSuccess) return
    setSavedFor(mutation.submittedAt)
    const timer = setTimeout(() => setSavedFor(null), SAVED_MS)
    return () => clearTimeout(timer)
  }, [mutation.isSuccess, mutation.submittedAt])
  const justSaved = !pending && savedFor !== null && savedFor === mutation.submittedAt
  const refusal = mutation.isError ? mutation.error : null
  const refusalText = refusal
    ? (() => {
        const code = parseErrorCode((refusal as { body?: string }).body)
        const key = code ? REFUSAL_KEY[code] : undefined
        return key ? t(key) : errMessage(refusal)
      })()
    : ''

  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  const ticking = state === 'on'
  useEffect(() => {
    if (!ticking) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), TICK_MS)
    return () => clearInterval(timer)
  }, [ticking])

  // The switch is offered once both reads answered and the crew has a roster
  // row to address: a switch over an unknown state would promise a change it
  // cannot describe. A thread never opened is the one refusal the client can
  // foresee, so the switch is shown disabled with that sentence as its
  // description instead of being pressed into a 409.
  const canSwitch = loaded && !failed && !missing && !!slug
  const threadClosed = canSwitch && !slotKey
  const stoppedReason = state === 'off' ? loop?.stopped_reason : undefined

  return (
    <section className="flex flex-col gap-2" data-testid="crew-perpetual-section" data-state={state}>
      <div className="flex items-center gap-2">
        <Goal
          size={14}
          className={`lucide-inline shrink-0 ${state === 'on' ? 'text-accent' : 'text-muted'}`}
          aria-hidden="true"
        />
        <h3 id="crew-perpetual-title" className="m-0 text-[12px] font-semibold uppercase tracking-wider text-muted">
          {t('components.crewPerpetualSection.title')}
        </h3>
        {canSwitch && (
          <span
            className="ml-auto flex items-center gap-2"
            data-testid="crew-perpetual-control"
            data-pending={pending || undefined}
            aria-busy={pending || undefined}
          >
            {(pending || justSaved) && (
              <span className="text-[11px] text-muted" aria-live="polite" data-testid="crew-perpetual-save-state">
                {pending ? t('components.jobForm.saving') : t('components.crewPerpetualSection.saved')}
              </span>
            )}
            <span data-testid="crew-perpetual-switch" data-checked={state === 'on'}>
              <Toggle
                checked={state === 'on'}
                onChange={(enabled) => { if (!pending && !threadClosed) mutation.mutate(enabled) }}
                disabled={pending || threadClosed}
                label={t('components.crewPerpetualSection.title')}
                describedBy={threadClosed ? 'crew-perpetual-hint crew-perpetual-thread-closed' : 'crew-perpetual-hint'}
              />
            </span>
          </span>
        )}
      </div>
      {/* What the switch does, stated once for both positions: no cycle or
          time cap by design, and the interval is the crewmate's to adjust. The
          second sentence names the save model by naming both controls: this
          switch writes on press, the editor's footer "Save changes" covers the
          fields it stages -- two saving ideas on one screen, so it says which
          is which. */}
      <p id="crew-perpetual-hint" className="m-0 text-[11.5px] leading-relaxed text-muted" data-testid="crew-perpetual-hint">
        {t('components.crewPerpetualSection.hint')} {t('components.crewPerpetualSection.applies_immediately')}
      </p>
      {threadClosed && (
        <p id="crew-perpetual-thread-closed" className="m-0 text-[11.5px] leading-relaxed text-muted" data-testid="crew-perpetual-thread-closed">
          {t('components.crewPerpetualSection.refused_thread_not_open')}
        </p>
      )}
      {/* No hand-off: the schedules pane below can hold an open, unsaved
          schedule draft this notice cannot see. */}
      <ErrorNotice
        variant="inline"
        title={t('components.crewPerpetualSection.change_failed')}
        message={refusalText}
        testId="crew-perpetual-error"
      />
      {!loaded ? (
        <Skeleton className="h-10" data-testid="crew-perpetual-loading" />
      ) : failed ? (
        // A read that never answered: never the affirmative "nothing wakes
        // this crewmate".
        <>
          {/* No hand-off: the schedules pane below can hold an open, unsaved
              schedule draft this notice cannot see. */}
          <ErrorNotice
            variant="inline"
            message={t('components.crewPerpetualSection.load_failed')}
            testId="crew-perpetual-load-error"
          />
        </>
      ) : (
        <AnimatePresence initial={false} mode="wait">
          {/* The verdict cross-fades on a state change: a stop that lands while
              the page is open must read as a change, not a flicker. */}
          <motion.div
            key={state}
            initial={reduceMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { opacity: 0 }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.18, ease: [0.2, 0, 0, 1] }}
            className="rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed"
            data-testid="crew-perpetual-status"
            data-state={state}
          >
            {state === 'on' && loop ? (
              <>
                <div className="font-medium text-text-strong" data-testid="crew-perpetual-verdict">
                  {t('components.crewPerpetualSection.on_verdict')}
                </div>
                <dl className="m-0 mt-1.5 space-y-1 text-[11px]">
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_interval')}</dt>
                    <dd className="m-0 min-w-0 truncate" data-testid="crew-perpetual-interval">
                      {intervalText(loop.idle_secs)}
                    </dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_cycles')}</dt>
                    <dd className="m-0 min-w-0 truncate" data-testid="crew-perpetual-cycles">
                      {loop.max_cycles > 0
                        ? t('pages.membersPage.patrol_cycles_of', { n: loop.cycle_count, max: loop.max_cycles })
                        : t('pages.membersPage.patrol_cycles_unlimited', { n: loop.cycle_count })}
                    </dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_last_wake')}</dt>
                    <dd
                      className="m-0 min-w-0 truncate"
                      title={loop.last_fire_ts ? fmtDateTimeNumeric(loop.last_fire_ts) : undefined}
                      data-testid="crew-perpetual-last"
                    >
                      {loop.last_fire_ts ? timeAgo(loop.last_fire_ts) : t('components.autoNudgePopover.never')}
                    </dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_next_wake')}</dt>
                    <dd
                      className="m-0 min-w-0 truncate"
                      title={loop.next_due_ts > 0 ? fmtDateTimeNumeric(loop.next_due_ts) : undefined}
                      data-testid="crew-perpetual-next"
                    >
                      {(() => {
                        const next = nextCycle(loop, nowTs)
                        switch (next.kind) {
                          case 'in':
                            return t('pages.membersPage.patrol_next_in', { time: next.time })
                          case 'due':
                            return t('components.autoNudgePopover.next_cycle_due')
                          default:
                            return t('components.autoNudgePopover.next_cycle_unscheduled')
                        }
                      })()}
                    </dd>
                  </div>
                </dl>
              </>
            ) : (
              <div className="text-muted">
                <span className="font-medium text-text-strong" data-testid="crew-perpetual-verdict">
                  {t('components.crewPerpetualSection.off_verdict')}
                </span>
                <span className="mt-0.5 block" data-testid="crew-perpetual-reason">
                  {state === 'off' && stoppedReason
                    ? STOPPED_REASON_KEY[stoppedReason]
                      ? t(STOPPED_REASON_KEY[stoppedReason])
                      : stoppedReason
                    : state === 'off'
                      ? t('components.crewPerpetualSection.off_by_you')
                      : monitor
                        ? t('components.crewPerpetualSection.off_monitor_running')
                        : t('components.crewPerpetualSection.off_never_armed')}
                </span>
                {/* The crewmate's own words for a stop it chose (redacted and
                    capped by the server), under the coded reason. */}
                {state === 'off' && loop?.stopped_detail && (
                  <span className="mt-0.5 block italic" data-testid="crew-perpetual-detail">
                    {loop.stopped_detail}
                  </span>
                )}
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      )}
    </section>
  )
}
