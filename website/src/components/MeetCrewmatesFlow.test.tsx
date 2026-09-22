import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import MeetCrewmatesFlow, { builtFromOptions, scheduleFor } from './MeetCrewmatesFlow'
import { hasNoCrewmates, hasNoCustomAgents } from '../hooks/useMeetCrewmatesGate'
import { api } from '../api/client'

// framer-motion never finishes an exit animation in jsdom, so the step
// AnimatePresence (mode="wait") would hold the next step off-screen forever.
// Same pass-through mock the other AnimatePresence consumers' tests use.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants',
    'whileHover', 'whileTap', 'whileInView', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  // Cached per tag: a fresh component type on every `motion.div` read would
  // remount the step subtree on each render and detach any element a test holds.
  const cache = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, make(tag))
      return cache.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => true,
  }
})

// Partial api mock: the two writes the Create step performs plus the two reads
// the flow makes while open. Everything else keeps its real implementation
// (ThemeProvider's ancillary fetches no-op in jsdom).
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true }),
      agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew' }]),
      getSlackConfig: vi.fn().mockResolvedValue({ configured: true, connected: false }),
      createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true, name: 'Radar', memory_store: 'm1' }),
      members: vi.fn().mockResolvedValue({ members: [] }),
      crons: vi.fn().mockResolvedValue({ jobs: [] }),
      createCron: vi.fn().mockResolvedValue({ ok: true, id: 'job-1' }),
    },
  }
})

const createAgent = vi.mocked(api.createKirocrewAgent)
const createCron = vi.mocked(api.createCron)
const members = vi.mocked(api.members)

const next = () => fireEvent.click(screen.getByTestId('meet-crewmates-next'))

describe('MeetCrewmatesFlow', () => {
  beforeEach(() => {
    createAgent.mockReset()
    createAgent.mockResolvedValue({ ok: true, name: 'Radar', memory_store: 'm1' })
    createCron.mockReset()
    createCron.mockResolvedValue({ ok: true, id: 'job-1' })
  })

  it('renders nothing while closed', () => {
    renderWithProviders(<MeetCrewmatesFlow open={false} onDone={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('step 1 shows the three example crewmates and a step counter', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: 'Meet CrewMates' })).toBeInTheDocument()
    expect(screen.getByText('CrewMates · 1 of 4')).toBeInTheDocument()
    const examples = screen.getByTestId('meet-crewmates-examples')
    expect(examples).toHaveTextContent('Radar')
    expect(examples).toHaveTextContent('Scribe')
    expect(examples).toHaveTextContent('Fixer')
  })

  it('Not now reports a dismissal and never writes', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    expect(onDone).toHaveBeenCalledWith('dismissed')
    expect(createAgent).not.toHaveBeenCalled()
  })

  it('Escape before the crewmate exists is a dismissal', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onDone).toHaveBeenCalledWith('dismissed')
  })

  it('step 2 prefills Radar, a chip swaps the name and the job, and an empty name blocks Next', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    const name = screen.getByTestId('meet-crewmates-name') as HTMLInputElement
    expect(name.value).toBe('Radar')
    fireEvent.click(screen.getByRole('button', { name: /Scribe/ }))
    expect(name.value).toBe('Scribe')
    fireEvent.change(name, { target: { value: '   ' } })
    expect(screen.getByTestId('meet-crewmates-next')).toBeDisabled()
    fireEvent.change(name, { target: { value: 'Radar' } })
    next()
    expect(screen.getByTestId('meet-crewmates-title')).toHaveTextContent('Give Radar a job')
  })

  it('Back returns to the previous step', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-back'))
    expect(screen.getByTestId('meet-crewmates-title')).toHaveTextContent('Meet CrewMates')
  })

  it('Create posts the crewmate with the job as its description, then a silent schedule bound to it, persists "done" and keeps the ready step open', async () => {
    const onDone = vi.fn()
    const onCreated = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={onCreated} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await waitFor(() => expect(createAgent).toHaveBeenCalledTimes(1))
    expect(createAgent).toHaveBeenCalledWith({
      name: 'Radar',
      kiro_agent: 'kirocrew',
      description: 'Triage new GitHub issues every morning',
      source: 'kirocrew',
    })
    await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
    const cronBody = createCron.mock.calls[0][0] as Record<string, unknown>
    expect(cronBody.member_id).toBe('Radar')
    expect(cronBody.agent).toBe('kirocrew')
    expect(cronBody.cron).toBe('0 9 * * *')
    // Delivery is mechanical: a non-silent run rings the bell, opens as the
    // crewmate's chat in the sidebar ("Its own chat" on) and reaches a
    // connected Slack through the runtime's own leg.
    expect(cronBody.silent).toBe(false)
    expect(cronBody.hide_in_chat).toBe(false)
    expect(String(cronBody.message)).toContain('Triage new GitHub issues every morning')
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(onCreated).toHaveBeenCalledTimes(1)
    // The host closes on onDone only; the ready step must still be on screen.
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('meet-crewmates-open-chat'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('the ready step also offers a quiet Done that closes without navigating', async () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await screen.findByTestId('meet-crewmates-ready')
    fireEvent.click(screen.getByTestId('meet-crewmates-done'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('a refused "done" write is shown as an ErrorNotice and the next exit reaches the host again', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} persistFailed />)
    expect(screen.getByTestId('meet-crewmates-persist-error')).toHaveTextContent('Could not save that you finished this')
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    expect(onDone).toHaveBeenCalledTimes(2)
  })

  it('a create with no usable answer is reconciled against the roster: the committed crewmate still gets its job', async () => {
    createAgent.mockRejectedValueOnce(new Error('network'))
    members.mockResolvedValueOnce({ members: [{ name: 'Radar', slug: 'radar' } as never] })
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(createCron).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('meet-crewmates-schedule-error')).toBeNull()
  })

  it('a 409 on a retry after an unanswered create is our own crewmate, not a taken name', async () => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValueOnce(new Error('network'))
    // The reconcile read fails too, so the first attempt is reported as failed...
    members.mockRejectedValueOnce(new Error('network'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-error')).toHaveTextContent('could not be created')
    expect(createCron).not.toHaveBeenCalled()
    // ...and the retry's "exists" proves the first request committed.
    createAgent.mockRejectedValueOnce(new ApiError(409, 'exists', JSON.stringify({ code: 'agent_exists' })))
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(createCron).toHaveBeenCalledTimes(1)
  })

  it('a taken name sends the user back to step 2 with the error under the name field, no completion', async () => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValue(new ApiError(409, 'exists', JSON.stringify({ code: 'agent_exists' })))
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-name-error')).toHaveTextContent('A crewmate named Radar already exists')
    expect(screen.getByTestId('meet-crewmates-step-2')).toBeInTheDocument()
    expect(screen.getByTestId('meet-crewmates-name')).toHaveAttribute('aria-invalid', 'true')
    expect(createCron).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
    // Typing a new name clears it.
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: 'Radar2' } })
    expect(screen.queryByTestId('meet-crewmates-name-error')).toBeNull()
  })

  it('a schedule the server refused (4xx) lands on the ready step saying it was not saved', async () => {
    const { ApiError } = await import('../api/apiError')
    createCron.mockRejectedValue(new ApiError(400, 'invalid_cron', '{}'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-schedule-error')).toHaveTextContent('its schedule was not saved')
  })

  it('a schedule write with no answer is reconciled against the Schedule list: a job bound to the crewmate means saved', async () => {
    createCron.mockRejectedValueOnce(new Error('network'))
    vi.mocked(api.crons).mockResolvedValueOnce({ jobs: [{ id: 'j1', name: 'Radar: standing job', message: '', member_id: 'Radar' } as never] })
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.queryByTestId('meet-crewmates-schedule-error')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-ready')).toHaveTextContent('Radar starts')
  })

  it('a schedule write with no answer and no job on the Schedule list is reported as maybe-unsaved, pointing at the Schedule page', async () => {
    createCron.mockRejectedValue(new Error('network'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-schedule-error')).toHaveTextContent('may not have been saved')
  })

  it('a suggestion chip never overwrites a job the user typed', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-job'), { target: { value: 'Water the plants' } })
    fireEvent.click(screen.getByTestId('meet-crewmates-back'))
    fireEvent.click(screen.getByRole('button', { name: /Scribe/ }))
    next()
    expect((screen.getByTestId('meet-crewmates-job') as HTMLInputElement).value).toBe('Water the plants')
  })

  it('a failed custom-agent read shows an ErrorNotice on step 2 and still offers the standard recipe', async () => {
    vi.mocked(api.agentsInstalled).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    expect(await screen.findByTestId('meet-crewmates-built-from-error')).toHaveTextContent('Could not load your other recipes')
    expect(screen.getByText('Standard recipe')).toBeInTheDocument()
  })

  it('the Slack row is status, not a switch: "Off" with the reason while Slack is not connected', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    expect(screen.queryByRole('switch', { name: 'Slack DM' })).toBeNull()
    expect(screen.getByTestId('meet-crewmates-slack-state')).toHaveTextContent('Off')
    expect(screen.getByTestId('meet-crewmates-slack-hint')).toHaveTextContent('Off until Slack is connected in Settings')
  })
})

describe('MeetCrewmatesFlow helpers', () => {
  it('scheduleFor maps the When choice to a cron body', () => {
    expect(scheduleFor('morning', 'Asia/Shanghai')).toEqual({ cron: '0 9 * * *', timezone: 'Asia/Shanghai' })
    expect(scheduleFor('hourly', 'UTC')).toEqual({ every: 3600 })
    expect(scheduleFor('ask', 'UTC')).toBeNull()
  })

  it('builtFromOptions puts the built-in first and drops private copies and kirocrew-lite', () => {
    expect(
      builtFromOptions([
        { name: 'zeta' },
        { name: 'kirocrew' },
        { name: 'kirocrew-lite' },
        { name: 'alpha', private_to: 'someone' },
        { name: 'beta' },
      ]),
    ).toEqual(['kirocrew', 'beta', 'zeta'])
    expect(builtFromOptions(undefined)).toEqual(['kirocrew'])
  })

  it('the auto-fire gate reads "no crewmates" past the default row and "no custom agents" past the built-ins', () => {
    expect(hasNoCrewmates([{ name: 'default' }])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'Radar' }])).toBe(false)
    expect(hasNoCrewmates(undefined)).toBe(false)
    expect(hasNoCustomAgents([{ name: 'kirocrew' }, { name: 'kirocrew-lite' }])).toBe(true)
    expect(hasNoCustomAgents([{ name: 'kirocrew' }, { name: 'issue-triage' }])).toBe(false)
    expect(hasNoCustomAgents(undefined)).toBe(false)
  })
})
