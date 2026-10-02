// Reconcile review queue (ADR 0009 P2) over GET /api/v1/reconcile/queue +
// confirm/reject. Parked equivalence clusters the apply pass could not auto-merge;
// confirm writes the off-graph AuthorityRecord, reject drops it. Batch select all.
// SEPARATE from the companion review queue by design.
import { useEffect, useState } from 'react'
import { RefreshCw, Check, X } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { BusyRetryNotice } from './BusyRetryNotice'
import { useBusyRetry } from '@/hooks/useBusyRetry'
import { isBusyRetryStopped } from '@/lib/busy-retry'
import {
  getReconcileQueue,
  reconcileConfirm,
  reconcileReject,
  type ReconcileQueueEntry,
} from '@/services/curation-api'

export function ReconcileReviewView() {
  const [entries, setEntries] = useState<ReconcileQueueEntry[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const { run: runBusy, wait: busyWait, stop: stopBusy } = useBusyRetry()

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      setEntries((await getReconcileQueue()).entries)
      setSelected(new Set())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggle(id: string) {
    setSelected((s) => {
      const next = new Set(s)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  async function resolveOne(id: string, action: 'confirm' | 'reject') {
    setBusy(true)
    setError(null)
    try {
      if (action === 'confirm') await runBusy(() => reconcileConfirm(id))
      else await runBusy(() => reconcileReject(id))
      await refresh()
    } catch (e) {
      if (!isBusyRetryStopped(e)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function resolveBatch(action: 'confirm' | 'reject') {
    setBusy(true)
    setError(null)
    try {
      for (const id of selected) {
        if (action === 'confirm') await runBusy(() => reconcileConfirm(id))
        else await runBusy(() => reconcileReject(id))
      }
      await refresh()
    } catch (e) {
      if (!isBusyRetryStopped(e)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-medium text-surface-200">Reconcile review queue</h2>
          <p className="text-xs text-surface-500">
            Equivalence clusters awaiting confirmation. Confirm folds them off-graph; reject drops them.
          </p>
        </div>
        <button
          onClick={refresh}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-sm text-surface-300 hover:bg-surface-800"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      <BusyRetryNotice wait={busyWait} onStop={stopBusy} />
      {error && <ErrorBox message={error} />}
      {loading && entries.length === 0 && <Spinner label="Loading queue…" />}
      {!loading && entries.length === 0 && !error && (
        <p className="text-sm text-surface-500">Reconcile queue is empty.</p>
      )}

      {selected.size > 0 && (
        <div className="flex items-center gap-2 rounded-lg border border-accent-600/40 bg-accent-700/10 px-3 py-2 text-sm">
          <span className="text-accent-300">{selected.size} selected</span>
          <button
            onClick={() => resolveBatch('confirm')}
            disabled={busy}
            className="rounded-md bg-accent-600 px-2.5 py-1 text-xs text-white hover:bg-accent-500 disabled:opacity-50"
          >
            Confirm all
          </button>
          <button
            onClick={() => resolveBatch('reject')}
            disabled={busy}
            className="rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-300 hover:bg-surface-800 disabled:opacity-50"
          >
            Reject all
          </button>
        </div>
      )}

      <div className="space-y-2">
        {entries.map((e) => (
          <div
            key={e.cluster_id}
            className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3"
          >
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={selected.has(e.cluster_id)}
                onChange={() => toggle(e.cluster_id)}
                className="h-4 w-4 accent-accent-500"
              />
              <Badge tone="violet">{e.type}</Badge>
              <Badge>conf {e.confidence.toFixed(2)}</Badge>
              <span className="text-xs text-surface-500">{e.corroboration}</span>
            </div>
            {e.reason && <p className="mt-1 text-sm text-surface-300">{e.reason}</p>}
            <ul className="mt-2 space-y-0.5 text-xs">
              {e.members.map((m) => (
                <li key={m.id} className="flex items-center gap-2">
                  <span
                    className={
                      m.id === e.canonical_id
                        ? 'text-accent-400'
                        : 'text-surface-300'
                    }
                  >
                    {m.title}
                  </span>
                  {m.id === e.canonical_id && (
                    <span className="text-[10px] uppercase text-accent-500">canonical</span>
                  )}
                  <span className="font-mono text-surface-600">{m.id}</span>
                </li>
              ))}
            </ul>
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => resolveOne(e.cluster_id, 'confirm')}
                disabled={busy}
                className="flex items-center gap-1 rounded-md bg-accent-600 px-2.5 py-1 text-xs text-white hover:bg-accent-500 disabled:opacity-50"
              >
                <Check size={12} /> Confirm
              </button>
              <button
                onClick={() => resolveOne(e.cluster_id, 'reject')}
                disabled={busy}
                className="flex items-center gap-1 rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-300 hover:bg-surface-800 disabled:opacity-50"
              >
                <X size={12} /> Reject
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
