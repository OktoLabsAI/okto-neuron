// Single source for "what needs the user's decision" — combines the three
// review-queue GETs (all file-backed and cheap) plus the last-heal timestamp so
// the Overview hero, the Review tab badge, and the heal nudge agree. The predicate
// snapshot comes from the shared predicate query (services/predicate-snapshot.ts),
// not from a timer of its own.
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getReconcileQueue,
  getReviewQueue,
  getRebuildStatus,
  type PredicateUpkeepSnapshot,
} from '@/services/curation-api'
import { usePredicateSnapshot } from '@/services/predicate-snapshot'

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

interface OtherQueues {
  entityMerges: number
  nodeCandidates: number
  lastHealDone: number
}

const NO_QUEUES: OtherQueues = { entityMerges: 0, nodeCandidates: 0, lastHealDone: 0 }

export function useAttention(pollMs = 15000): { attention: Attention; refresh: () => void } {
  const [queues, setQueues] = useState<OtherQueues>(NO_QUEUES)
  const { snapshot: pred } = usePredicateSnapshot()

  const refresh = useCallback(() => {
    void (async () => {
      try {
        const [recon, companion, heal] = await Promise.all([
          getReconcileQueue().catch(() => null),
          getReviewQueue().catch(() => null),
          getRebuildStatus('heal').catch(() => null),
        ])
        setQueues({
          entityMerges: recon?.entries.length ?? 0,
          nodeCandidates: companion?.items.length ?? 0,
          lastHealDone:
            heal?.last_heal?.status === 'done' ? (heal.last_heal.finished_at ?? 0) : 0,
        })
      } catch {
        /* keep last known state */
      }
    })()
  }, [])

  useEffect(() => {
    refresh()
    // Hidden tabs do not poll; the next visibility change refreshes everything.
    const tick = () => {
      if (document.visibilityState !== 'hidden') refresh()
    }
    const t = setInterval(tick, pollMs)
    document.addEventListener('visibilitychange', tick)
    return () => {
      clearInterval(t)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [refresh, pollMs])

  const attention = useMemo<Attention>(() => {
    const predicates = pred?.counts.queued ?? 0
    const decided = [...(pred?.records.auto ?? []), ...(pred?.records.confirmed ?? [])]
    const unappliedDecisions = decided.filter((r) => {
      const created = Date.parse(r.created_at ?? '') / 1000
      return Number.isFinite(created) ? created > queues.lastHealDone : queues.lastHealDone === 0
    }).length
    return {
      predicates,
      entityMerges: queues.entityMerges,
      nodeCandidates: queues.nodeCandidates,
      total: predicates + queues.entityMerges + queues.nodeCandidates,
      unappliedDecisions,
      predicateSnapshot: pred,
    }
  }, [pred, queues])

  return { attention, refresh }
}
