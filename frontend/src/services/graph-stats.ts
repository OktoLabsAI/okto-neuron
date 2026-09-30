// GET /api/v1/graph/stats is served from the daemon's maintained projection (issue #12):
//   200 {..., stale, rebuilding}   last projection, possibly a rebuild behind: use it
//   202 {"status": "building"}     first projection for this vault is still being built
// A 202 is not an error. Show the last counts we already have for this vault/variant, or wait
// a little for the first build; only give up (and throw) after a bounded wait.
import { apiFetch } from './http'
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
