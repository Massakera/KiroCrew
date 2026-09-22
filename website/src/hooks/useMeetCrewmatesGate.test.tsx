import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders } from '../test/helpers'
import { useMeetCrewmatesGate } from './useMeetCrewmatesGate'
import { useTheme } from './useTheme'
import { START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { api } from '../api/client'

// A brand-new workspace: first-run chapters not yet done on the server, only
// the built-in `default` row on the roster, only the built-in agent installed.
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      themeBoot: vi.fn().mockResolvedValue({
        mode: '', color: '', onboarded: false, import_onboarded: true, privacy_acked: true,
      }),
      updateThemeConfig: vi.fn().mockResolvedValue({}),
      members: vi.fn().mockResolvedValue({ members: [{ name: 'default', slug: 'default' }] }),
      agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew' }]),
    },
  }
})

const useBoth = () => ({ gate: useMeetCrewmatesGate(), theme: useTheme() })

describe('useMeetCrewmatesGate', () => {
  beforeEach(() => {
    localStorage.clear()
    // The Crew Members preview is the launch switch; every case below opts in.
    localStorage.setItem(PREVIEW_CREW, '1')
    vi.mocked(api.updateThemeConfig).mockClear()
    vi.mocked(api.members).mockReset()
    vi.mocked(api.agentsInstalled).mockReset()
    vi.mocked(api.members).mockResolvedValue({ members: [{ name: 'default', slug: 'default' }] } as never)
    vi.mocked(api.agentsInstalled).mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew' }])
  })

  it('stays closed until the tour ends, then opens, stays open through onCreated, and closes only on onDone', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.gate.open).toBe(false)

    // The Customize tour finishes in this session.
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))

    // The crewmate exists: "done" is persisted, but the ready step is still up.
    act(() => result.current.gate.onCreated())
    await waitFor(() =>
      expect(api.updateThemeConfig).toHaveBeenCalledWith({ crewmates_onboarded: true }),
    )
    expect(result.current.gate.open).toBe(true)

    // onDone awaits the persist before closing.
    act(() => result.current.gate.onDone('completed'))
    await waitFor(() => expect(result.current.gate.open).toBe(false))
  })

  it('a refused persist keeps the flow open with persistFailed; the next exit closes it', async () => {
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.open).toBe(true))
    // Armed only now: markOnboarded's own PUT above must not consume it.
    vi.mocked(api.updateThemeConfig).mockRejectedValueOnce(new Error('500'))
    act(() => result.current.gate.onDone('dismissed'))
    await waitFor(() => expect(result.current.gate.persistFailed).toBe(true))
    expect(result.current.gate.open).toBe(true)
    act(() => result.current.gate.onDone('dismissed'))
    await waitFor(() => expect(result.current.gate.open).toBe(false))
  })

  it('does not open while the Crew Members preview is off', async () => {
    localStorage.removeItem(PREVIEW_CREW)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    // Neither gating read is made: the switch is off, so nothing is eligible.
    expect(api.members).not.toHaveBeenCalled()
    expect(result.current.gate.open).toBe(false)
  })

  it('does not open when a crewmate already exists', async () => {
    vi.mocked(api.members).mockResolvedValue({
      members: [{ name: 'default', slug: 'default' }, { name: 'Radar', slug: 'radar' }],
    } as never)
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(api.members).toHaveBeenCalled())
    expect(result.current.gate.open).toBe(false)
  })

  it('a failed eligibility read is surfaced, not swallowed: eligibilityError until dismissed, flow stays closed', async () => {
    // React Query retries by default; the providers' test client disables retries, so one rejection is the failure.
    vi.mocked(api.members).mockRejectedValue(new Error('network'))
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.gate.eligibilityError).toBe(false)
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(result.current.gate.eligibilityError).toBe(true))
    expect(result.current.gate.open).toBe(false)
    act(() => result.current.gate.dismissEligibilityError())
    expect(result.current.gate.eligibilityError).toBe(false)
    // The Crew Members page entry still works regardless of the failed read.
    act(() => window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT)))
    expect(result.current.gate.open).toBe(true)
  })

  it('does not open when a custom agent is installed (that user gets the opt-in step)', async () => {
    vi.mocked(api.agentsInstalled).mockResolvedValue([{ name: 'kirocrew' }, { name: 'issue-triage' }])
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    act(() => result.current.theme.markOnboarded())
    await waitFor(() => expect(api.agentsInstalled).toHaveBeenCalled())
    expect(result.current.gate.open).toBe(false)
  })

  it('a workspace already onboarded on the server is treated as done, but the Crewmates page can still open it', async () => {
    vi.mocked(api.themeBoot).mockResolvedValueOnce({
      mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true,
    })
    const { result } = renderHookWithProviders(useBoth)
    await waitFor(() => expect(result.current.theme.themeBootReady).toBe(true))
    expect(result.current.theme.crewmatesOnboarded).toBe(true)
    expect(result.current.gate.open).toBe(false)
    act(() => {
      window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT))
    })
    expect(result.current.gate.open).toBe(true)
  })
})
