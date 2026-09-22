import { api, type CrewTeam } from './client'

/**
 * The Crewmates page's team list (`GET /api/teams`).
 *
 * One module so the page, its dialog and any later consumer spell the key
 * exactly once. The list is small and changes only through this page's own
 * create / edit / delete mutations, which invalidate it; the finite staleTime
 * is the floor for a return to the page, matching the roster's.
 */
export const TEAMS_QUERY_KEY = ['crew-teams'] as const

const TEAMS_STALE_MS = 30_000

export const teamsQuery = {
  queryKey: TEAMS_QUERY_KEY,
  queryFn: async (): Promise<CrewTeam[]> => (await api.teams.list()).teams,
  staleTime: TEAMS_STALE_MS,
}
