// GET /api/v1/graph/stats is served from the daemon's maintained projection (issue #12):
//   200 {..., stale, rebuilding}   last projection, possibly a rebuild behind: use it
//   202 {"status": "building"}     first projection for this vault is still being built
// A 202 is not an error. Show the last counts we already have for this vault/variant, or wait
// a little for the first build; only give up (and throw) after a bounded wait.
import { useSyncExternalStore } from 'react'
import { apiFetch } from './http'
import { createSharedQuery } from '@/lib/shared-query'
import { useApp } from '@/store/app'

export interface GraphStatsBody {
  status: string
  node_types: { type: string; count: number }[]
  edge_types: { type: string; count: number }[]
  total_nodes: number
  total_edges: number
  stale?: boolean
  rebuilding?: boolean
  projection_age_s?: number
}

const BUILD_WAIT_MS = 1500
const BUILD_ATTEMPTS = 20

const lastGood = new Map<string, GraphStatsBody>()

function isBuilding(body: GraphStatsBody | { status?: string }): boolean {
  return body.status === 'building' || !('node_types' in body)
}

export async function fetchGraphStats(): Promise<GraphStatsBody> {
  const key = useApp.getState().selectedVaultPath ?? ''
  for (let attempt = 0; attempt < BUILD_ATTEMPTS; attempt += 1) {
    const body = await apiFetch<GraphStatsBody>('/graph/stats')
    if (!isBuilding(body)) {
      lastGood.set(key, body)
      return body
    }
    const previous = lastGood.get(key)
    if (previous) return previous
    await new Promise((resolve) => setTimeout(resolve, BUILD_WAIT_MS))
  }
  throw new Error('The daemon is still building the graph statistics; try again in a moment.')
}

// ONE shared poll for every panel that shows the graph counts (Curation Overview + Details):
// one timer, one in-flight request, paused while the tab is hidden (lib/shared-query.ts).
export interface GraphStatsState {
  stats: GraphStatsBody | null
  stale: boolean
  rebuilding: boolean
  error: string | null
}

export const GRAPH_STATS_POLL_MS = 5000

const EMPTY: GraphStatsState = { stats: null, stale: false, rebuilding: false, error: null }

export const graphStatsQuery = createSharedQuery<GraphStatsBody, GraphStatsState>({
  initial: EMPTY,
  fetch: fetchGraphStats,
  loading: (prev) => prev,
  reduce: (prev, outcome) =>
    outcome.ok
      ? { stats: outcome.body, stale: !!outcome.body.stale, rebuilding: !!outcome.body.rebuilding, error: null }
      : { ...prev, error: outcome.error },
  pollMs: () => GRAPH_STATS_POLL_MS,
  key: () => useApp.getState().selectedVaultPath ?? '',
})

export function useGraphStats(): GraphStatsState {
  return useSyncExternalStore(graphStatsQuery.subscribe, graphStatsQuery.getState, graphStatsQuery.getState)
}
