// Graph visualization service — read-only (contract §graph).
//
// Unlike kg-api, these calls do NOT gate on mock mode: lib/mock.ts ships no
// graph fixtures, so the Graph view detects mock mode itself (lib/mode.isMock)
// and renders a clean disabled state instead of issuing a live request.
import { apiFetch, qs } from './http'
import { fetchGraphStats } from './graph-stats'
import type { GraphResponse, GraphStats, NeighborsResponse } from '@/types'

export interface GraphFilters {
  types?: string[]
  relations?: string[]
  limit?: number
  min_degree?: number
}

const csv = (xs?: string[]): string | undefined =>
  xs && xs.length ? xs.join(',') : undefined

export function getGraph(filters: GraphFilters = {}): Promise<GraphResponse> {
  return apiFetch<GraphResponse>(
    `/graph${qs({
      types: csv(filters.types),
      relations: csv(filters.relations),
      limit: filters.limit,
      min_degree: filters.min_degree,
    })}`,
  )
}

export function getNeighbors(
  id: string,
  opts: { hops?: number; types?: string[]; relations?: string[]; limit?: number } = {},
): Promise<NeighborsResponse> {
  return apiFetch<NeighborsResponse>(
    `/nodes/${encodeURIComponent(id)}/neighbors${qs({
      hops: opts.hops,
      types: csv(opts.types),
      relations: csv(opts.relations),
      limit: opts.limit,
    })}`,
  )
}

export function getGraphStats(): Promise<GraphStats> {
  return fetchGraphStats()
}
