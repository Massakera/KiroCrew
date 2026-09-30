import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({
  api: { spawnDelete: vi.fn() },
}))

import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentTool } from '../store/chatSlice'
import { cancelSubagentCard } from '../pages/chat/subagentCancel'

// A card whose run already ended must still end when Cancel is pressed: the
// gateway has nothing left to stop, so no live frame will ever arrive for it.
function storeWithRunningCard() {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot('chat-1'))
  store.dispatch(sseSubagentSpawn({ slot: 'chat-1', id: 'r1', task: 't', agent: 'a' }))
  store.dispatch(sseSubagentTool({ slot: 'chat-1', id: 'r1', tool: 'bash' }))
  return store
}

describe('cancelSubagentCard', () => {
  beforeEach(() => { vi.mocked(api.spawnDelete).mockReset() })

  it('applies the terminal frame the gateway returns for an ended run', async () => {
    const store = storeWithRunningCard()
    vi.mocked(api.spawnDelete).mockResolvedValue({
      ok: true, cancelled: false,
      done: { id: 'r1', slot: 'chat-1', elapsed: 31, outcome: 'completed', error: null },
    })
    const card = store.getState().chat.subagents['r1']
    await cancelSubagentCard(store.dispatch, card, 'chat-1')
    expect(store.getState().chat.subagents['r1'].status).toBe('done')
    expect(store.getState().chat.subagents['r1'].elapsed).toBe(31)
  })

  it('leaves a running card to its live done frame', async () => {
    const store = storeWithRunningCard()
    vi.mocked(api.spawnDelete).mockResolvedValue({ ok: true, cancelled: true })
    await cancelSubagentCard(store.dispatch, store.getState().chat.subagents['r1'], 'chat-1')
    expect(store.getState().chat.subagents['r1'].status).toBe('tool')
  })

  it('ignores a returned frame that names a different run', async () => {
    const store = storeWithRunningCard()
    vi.mocked(api.spawnDelete).mockResolvedValue({
      ok: true, cancelled: false, done: { id: 'other', slot: 'chat-1', elapsed: 1 },
    })
    await cancelSubagentCard(store.dispatch, store.getState().chat.subagents['r1'], 'chat-1')
    expect(store.getState().chat.subagents['r1'].status).toBe('tool')
  })

  it('ends the card as stopped when the gateway no longer tracks the run', async () => {
    const store = storeWithRunningCard()
    vi.mocked(api.spawnDelete).mockRejectedValue(new ApiError(404, 'not found', '{"error":"not found"}'))
    await cancelSubagentCard(store.dispatch, store.getState().chat.subagents['r1'], 'chat-1')
    const row = store.getState().chat.subagents['r1']
    expect(row.status).toBe('stopped')
    expect(row.error).toBeUndefined()
  })

  it('rethrows any other refusal so the caller can report it', async () => {
    const store = storeWithRunningCard()
    vi.mocked(api.spawnDelete).mockRejectedValue(new ApiError(409, 'pending', '{"code":"completion_delivery_pending"}'))
    await expect(cancelSubagentCard(store.dispatch, store.getState().chat.subagents['r1'], 'chat-1')).rejects.toBeInstanceOf(ApiError)
    expect(store.getState().chat.subagents['r1'].status).toBe('tool')
  })
})
