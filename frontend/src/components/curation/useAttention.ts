// Single source for "what needs the user's decision" — combines the three
// review-queue GETs (all file-backed and cheap) plus the last-heal timestamp so
// the Overview hero, the Review tab badge, and the heal nudge agree.
import { useCallback, useEffect, useState } from 'react'
import {
  getPredicateUpkeep,
  getReconcileQueue,
  getReviewQueue,
  getRebuildStatus,
  type PredicateUpkeepSnapshot,
} from '@/services/curation-api'

export interface Attention {
  predicates: number
  entityMerges: number
  nodeCandidates: number
  total: number
  // predicate decisions (auto/confirmed) made after the last completed heal —
  // i.e. confirmed but not yet materialized into the graph
  unappliedDecisions: number
  predicateSnapshot: PredicateUpkeepSnapshot | null
}

const EMPTY: Attention = {
  predicates: 0,
  entityMerges: 0,
  nodeCandidates: 0,
  total: 0,
  unappliedDecisions: 0,
  predicateSnapshot: null,
}

export function useAttention(pollMs = 15000): { attention: Attention; refresh: () => void } {
  const [attention, setAttention] = useState<Attention>(EMPTY)

  const refresh = useCallback(() => {
    void (async () => {
      try {
        const [pred, recon, companion, heal] = await Promise.all([
          getPredicateUpkeep().catch(() => null),
          getReconcileQueue().catch(() => null),
          getReviewQueue().catch(() => null),
          getRebuildStatus('heal').catch(() => null),
        ])
        const predicates = pred?.counts.queued ?? 0
        const entityMerges = recon?.entries.length ?? 0
        const nodeCandidates = companion?.items.length ?? 0
        const lastHealDone =
          heal?.last_heal?.status === 'done' ? (heal.last_heal.finished_at ?? 0) : 0
        const decided = [
          ...(pred?.records.auto ?? []),
          ...(pred?.records.confirmed ?? []),
        ]
        const unappliedDecisions = decided.filter((r) => {
          const created = Date.parse(r.created_at ?? '') / 1000
          return Number.isFinite(created) ? created > lastHealDone : lastHealDone === 0
        }).length
        setAttention({
          predicates,
          entityMerges,
          nodeCandidates,
          total: predicates + entityMerges + nodeCandidates,
          unappliedDecisions,
          predicateSnapshot: pred,
        })
      } catch {
        /* keep last known state */
      }
    })()
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, pollMs)
    return () => clearInterval(t)
  }, [refresh, pollMs])

  return { attention, refresh }
}
