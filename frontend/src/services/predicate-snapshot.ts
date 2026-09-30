// ONE shared query for GET /api/v1/upkeep/predicates (issue #12).
//
// Three places used to poll this route on their own timers (the Curation overview's attention
// hook, the "Details" dashboard and the predicate review queue), so a slow daemon saw several
// overlapping requests per tick. Every consumer now subscribes here: one timer, one in-flight
// request, paused while the tab is hidden (lib/shared-query.ts).
//
// The daemon serves this from a maintained projection, so the body can be:
//   200 {..., stale, rebuilding}   last projection; shown as-is, never an error
//   202 {"status": "building"}     no projection yet; keep the last data (if any) and poll faster
// Transport errors keep the last data and surface `error`; they never blank the screen.
import { useSyncExternalStore } from 'react'
import { createSharedQuery } from '@/lib/shared-query'
import { useApp } from '@/store/app'
import { getPredicateUpkeep, type PredicateUpkeepSnapshot } from './curation-api'

export interface PredicateSnapshotState {
  /** Last good snapshot; kept across stale/rebuilding/202/error responses. */
  snapshot: PredicateUpkeepSnapshot | null
  /** The daemon answered 202: its first projection for this vault is still being built. */
  building: boolean
  /** The snapshot is older than the newest write; a rebuild is on its way. */
  stale: boolean
  rebuilding: boolean
  /** Last transport/HTTP failure, cleared by the next success. */
  error: string | null
  /** A request is in flight and there is nothing to show yet. */
  loading: boolean
}

export const POLL_MS = 5000
export const BUILDING_POLL_MS = 1500

const EMPTY: PredicateSnapshotState = {
  snapshot: null,
  building: false,
  stale: false,
  rebuilding: false,
  error: null,
  loading: false,
}

function isBuilding(body: unknown): boolean {
  const b = body as { status?: string; records?: unknown } | null
  return !!b && (b.status === 'building' || b.records === undefined)
}

export const predicateQuery = createSharedQuery<PredicateUpkeepSnapshot, PredicateSnapshotState>({
  initial: EMPTY,
  fetch: getPredicateUpkeep,
  loading: (prev) => (prev.snapshot ? prev : { ...prev, loading: true }),
  reduce: (prev, outcome) => {
    if (!outcome.ok) return { ...prev, loading: false, error: outcome.error }
    if (isBuilding(outcome.body)) return { ...prev, building: true, loading: false, error: null }
    return {
      snapshot: outcome.body,
      building: false,
      stale: !!outcome.body.stale,
      rebuilding: !!outcome.body.rebuilding,
      error: null,
      loading: false,
    }
  },
  pollMs: (s) => (s.building || s.rebuilding ? BUILDING_POLL_MS : POLL_MS),
  key: () => useApp.getState().selectedVaultPath ?? '',
})

/** Fetch now (e.g. after confirming a record); shares the in-flight request if there is one. */
export const refreshPredicateSnapshot = predicateQuery.refresh

export function usePredicateSnapshot(): PredicateSnapshotState {
  return useSyncExternalStore(predicateQuery.subscribe, predicateQuery.getState, predicateQuery.getState)
}
