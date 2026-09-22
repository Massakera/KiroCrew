import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, type MemberRosterRow } from '../api/client'
import { membersRosterQuery } from '../api/membersQuery'
import { START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { usePreviewFlag } from './usePreviewFlag'
import { useTheme } from './useTheme'

/** The built-in agent rows that are not a user's own custom agent. */
const BUILTIN_TEMPLATES: ReadonlySet<string> = new Set(['kirocrew', 'kirocrew-lite'])

/** No crewmate beyond the always-present `default` row (the main assistant). */
export function hasNoCrewmates(rows: readonly Pick<MemberRosterRow, 'name'>[] | undefined): boolean {
  return Array.isArray(rows) && rows.every(r => r.name === 'default')
}

/** No installed agent beyond the built-in ones. */
export function hasNoCustomAgents(installed: readonly { name: string }[] | undefined): boolean {
  return Array.isArray(installed) && installed.every(a => BUILTIN_TEMPLATES.has(a.name))
}

/**
 * Decides when the "Meet CrewMates" first-run flow is on screen.
 *
 * Fires ONCE per workspace, automatically, after the other first-run chapters
 * are done, only while the Crew Members preview (`PREVIEW_CREW`, Settings ->
 * Developer -> Feature Previews) is on — the same switch that shows the
 * Crewmates page and its rail item, so the flow launches with them and never
 * introduces a page the user cannot reach — and only for a user with no
 * crewmates AND no custom agents (an
 * existing user with custom agents gets the opt-in step instead — the two
 * are gated on the same fact so they never both fire). Completion or
 * dismissal is persisted as `dashboard.crewmates_onboarded` through the
 * theme-config endpoint, the same server-backed record the other chapters
 * use, so a second machine does not replay it.
 *
 * The Crewmates page re-opens it on demand through the
 * `mc-start-meet-crewmates` window event (no eligibility check: the page is
 * itself behind the preview, and the user asked).
 */
export function useMeetCrewmatesGate(): {
  open: boolean
  onDone: (outcome: 'completed' | 'dismissed') => void
  onCreated: () => void
  /** The last attempt to persist `crewmates_onboarded` was refused; the flow
   *  renders this as an ErrorNotice and stays open for one more exit. */
  persistFailed: boolean
  /** One of the two eligibility reads (roster, installed agents) failed while
   *  the auto-fire was still possible, so the flow could not decide whether
   *  to show itself. App renders this as an ErrorNotice; the Crew Members
   *  page entry stays available regardless. */
  eligibilityError: boolean
  dismissEligibilityError: () => void
} {
  const {
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    crewmatesOnboarded,
    markCrewmatesOnboarded,
  } = useTheme()
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const firstRunDone = themeBootReady && onboarded && importOnboarded && privacyAcked
  const eligible = crewPreview && firstRunDone && !crewmatesOnboarded

  // Both reads are needed only while the auto-fire is still possible; once the
  // flag is set they never run again for this workspace.
  const roster = useQuery({ ...membersRosterQuery, enabled: eligible })
  const installed = useQuery<{ name: string }[]>({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
    enabled: eligible,
  })

  // `open` is STICKY once it fires: the flow persists "done" the moment the
  // crewmate exists (onCreated), which flips `eligible` off, and the ready step
  // must still be on screen after that. Only onDone closes it.
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const start = () => setOpen(true)
    window.addEventListener(START_MEET_CREWMATES_EVENT, start)
    return () => window.removeEventListener(START_MEET_CREWMATES_EVENT, start)
  }, [])
  const autoOpen = eligible && hasNoCrewmates(roster.data) && hasNoCustomAgents(installed.data)
  useEffect(() => {
    if (autoOpen) setOpen(true)
  }, [autoOpen])

  // A failed eligibility read is shown, not swallowed: without this the flow
  // would simply never appear and the user would not know why. Dismissal is
  // per failure -- a later refetch that fails again shows it again.
  const readFailed = eligible && (roster.isError || installed.isError)
  const [eligibilityDismissed, setEligibilityDismissed] = useState(false)
  useEffect(() => {
    if (!readFailed) setEligibilityDismissed(false)
  }, [readFailed])
  const eligibilityError = readFailed && !eligibilityDismissed
  const dismissEligibilityError = useCallback(() => setEligibilityDismissed(true), [])

  // A refused write is shown, not swallowed: the flow stays open with an
  // ErrorNotice and the next exit closes it anyway (the local mark is already
  // set, so this machine will not replay the flow; another machine might).
  const [persistFailed, setPersistFailed] = useState(false)
  const failedOnceRef = useRef(false)
  const persist = useCallback(async (): Promise<boolean> => {
    try {
      await markCrewmatesOnboarded()
      setPersistFailed(false)
      return true
    } catch {
      setPersistFailed(true)
      return false
    }
  }, [markCrewmatesOnboarded])

  const onCreated = useCallback(() => {
    void persist()
  }, [persist])

  const onDone = useCallback(() => {
    void persist().then(ok => {
      if (ok || failedOnceRef.current) {
        failedOnceRef.current = false
        setPersistFailed(false)
        setOpen(false)
        return
      }
      failedOnceRef.current = true
    })
  }, [persist])

  return { open, onDone, onCreated, persistFailed, eligibilityError, dismissEligibilityError }
}
