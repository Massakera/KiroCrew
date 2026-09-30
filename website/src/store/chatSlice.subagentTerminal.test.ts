import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseSubagentSpawn,
  sseSubagentTool,
  sseSubagentDone,
  sseSubagentStalled,
  sseSubagentRetrying,
  sseSubagentSnapshot,
  sseSubagentBatchUpdate,
  sseSubagentBatchChunks,
  isTerminalSubagent,
} from './chatSlice'

// A card that has ended must stay ended: a late incremental frame or a stale
// running snapshot used to flip it back to "Running Tool", and nothing could end
// it again, because the run is over and Stop / Cancel find nothing to stop.
function endedCard(outcome: 'completed' | 'failed' | 'stopped') {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot('active'))
  store.dispatch(sseSubagentSpawn({ slot: 'active', id: 'r1', task: 't', agent: 'a' }))
  store.dispatch(sseSubagentTool({ slot: 'active', id: 'r1', tool: 'bash' }))
  store.dispatch(sseSubagentDone({ slot: 'active', id: 'r1', elapsed: 9, outcome }))
  return store
}

const STATUS = { completed: 'done', failed: 'error', stopped: 'stopped' } as const

describe('a terminal subagent card stays terminal', () => {
  it.each(['completed', 'failed', 'stopped'] as const)('ignores a late subagent_tool after %s', (outcome) => {
    const store = endedCard(outcome)
    store.dispatch(sseSubagentTool({ slot: 'active', id: 'r1', tool: 'bash', tool_count: 7 }))
    const row = store.getState().chat.subagents['r1']
    expect(row.status).toBe(STATUS[outcome])
    expect(isTerminalSubagent(row)).toBe(true)
  })

  it.each(['completed', 'failed', 'stopped'] as const)('ignores a stale running snapshot after %s', (outcome) => {
    const store = endedCard(outcome)
    store.dispatch(sseSubagentSnapshot({
      slot: 'active', id: 'r1', task: 't', agent: 'a', streaming: '', last_tool: 'bash', started: 1,
    }))
    expect(store.getState().chat.subagents['r1'].status).toBe(STATUS[outcome])
  })

  it('ignores late stalled, retrying, batch and chunk frames', () => {
    const store = endedCard('stopped')
    store.dispatch(sseSubagentStalled({ slot: 'active', id: 'r1', stalled: true, idle_secs: 40 }))
    store.dispatch(sseSubagentRetrying({ slot: 'active', id: 'r1', attempt: 2 }))
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ slot: 'active', id: 'r1', tool: 'bash', stalled: true, attempt: 1 }] }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot: 'active', id: 'r1', text: 'late' }] }))
    const row = store.getState().chat.subagents['r1']
    expect(row.status).toBe('stopped')
    expect(row.stalled).toBeFalsy()
    expect(row.retrying).toBeFalsy()
    expect(row.streaming).toBe('')
  })

  it('still lets a running card pick up tool activity', () => {
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseSubagentSpawn({ slot: 'active', id: 'r2', task: 't', agent: 'a' }))
    store.dispatch(sseSubagentTool({ slot: 'active', id: 'r2', tool: 'read' }))
    expect(store.getState().chat.subagents['r2'].status).toBe('tool')
    expect(store.getState().chat.subagents['r2'].lastTool).toBe('read')
  })
})
