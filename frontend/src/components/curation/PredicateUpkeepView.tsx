// Predicate upkeep review queue (ADR 0017). Separate from entity reconciliation:
// these records canonicalize relation names, not nodes.
import { useEffect, useState } from 'react'
import { Check, RefreshCw, X } from 'lucide-react'
import { Badge, ErrorBox, Spinner } from '@/components/ui'
import {
  getPredicateUpkeep,
  predicateUpkeepConfirm,
  predicateUpkeepReject,
  type PredicateAliasRecord,
  type PredicateMapping,
} from '@/services/curation-api'

const mappingStyles: Record<PredicateMapping, string> = {
  exact_match: 'border-cyan-500/50 bg-cyan-600/20 text-cyan-200',
  inverse_of: 'border-amber-500/50 bg-amber-600/20 text-amber-200',
  sub_property_of: 'border-violet-500/50 bg-violet-600/20 text-violet-200',
}

function mappingLabel(mapping: PredicateMapping): string {
  if (mapping === 'exact_match') return 'same'
  if (mapping === 'inverse_of') return 'inverse'
  return 'narrower'
}

function fmtConfidence(n: number): string {
  return Number.isFinite(n) ? n.toFixed(2) : '0.00'
}

function evidenceText(record: PredicateAliasRecord): string {
  const counts = record.evidence.counts ?? {}
  const subjectCount = counts[record.subject_predicate] ?? 0
  const objectCount = counts[record.object_predicate] ?? 0
  const shared = record.evidence.shared_pairs ?? []
  const sameOrder = shared.reduce((sum, pair) => sum + (pair.same_order ?? 0), 0)
  const swappedOrder = shared.reduce((sum, pair) => sum + (pair.swapped_order ?? 0), 0)
  const sampleClaimIds = record.evidence.sample_claim_ids ?? {}
  const samples = Object.values(sampleClaimIds).reduce((sum, ids) => sum + ids.length, 0)
  return `uses ${subjectCount}/${objectCount} | shared ${sameOrder} same-order / ${swappedOrder} swapped | samples ${samples}`
}

export function PredicateUpkeepView() {
  const [records, setRecords] = useState<PredicateAliasRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const snapshot = await getPredicateUpkeep()
      setRecords(snapshot.records.queued ?? [])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function resolve(recordId: string, action: 'confirm' | 'reject') {
    setBusyId(recordId)
    setError(null)
    try {
      if (action === 'confirm') await predicateUpkeepConfirm(recordId)
      else await predicateUpkeepReject(recordId)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-medium text-surface-200">Predicate upkeep queue</h2>
          <p className="text-xs text-surface-500">
            Relation-name mappings awaiting confirmation. Confirm writes a predicate alias; reject keeps the negative decision.
          </p>
        </div>
        <button
          onClick={() => void refresh()}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-sm text-surface-300 hover:bg-surface-800"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && records.length === 0 && <Spinner label="Loading predicate queue..." />}
      {!loading && records.length === 0 && !error && (
        <p className="text-sm text-surface-500">Predicate queue is empty.</p>
      )}

      <div className="space-y-3">
        {records.map((record) => (
          <div
            key={record.id}
            className="rounded-lg border border-cyan-900/60 bg-cyan-950/10 px-4 py-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="accent">predicate fold</Badge>
              <span
                className={`rounded-md border px-2 py-0.5 text-xs font-medium ${mappingStyles[record.mapping]}`}
              >
                {mappingLabel(record.mapping)}
              </span>
              <Badge>conf {fmtConfidence(record.confidence)}</Badge>
              {record.judge_model && (
                <span className="font-mono text-xs text-surface-600">{record.judge_model}</span>
              )}
            </div>

            <div className="mt-3 grid gap-2 text-sm md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] md:items-center">
              <code className="truncate rounded-md border border-surface-800 bg-surface-950 px-2 py-1 text-cyan-200">
                {record.subject_predicate}
              </code>
              <span className="text-xs uppercase text-surface-600">
                to canonical
              </span>
              <code className="truncate rounded-md border border-surface-800 bg-surface-950 px-2 py-1 text-surface-200">
                {record.object_predicate}
              </code>
            </div>

            {record.justification && (
              <p className="mt-3 text-sm leading-6 text-surface-300">{record.justification}</p>
            )}
            <p className="mt-2 text-xs text-surface-500">{evidenceText(record)}</p>

            <div className="mt-3 flex gap-2">
              <button
                onClick={() => void resolve(record.id, 'confirm')}
                disabled={busyId !== null}
                className="flex items-center gap-1 rounded-md bg-cyan-700 px-2.5 py-1 text-xs text-white hover:bg-cyan-600 disabled:opacity-50"
              >
                <Check size={12} /> Confirm
              </button>
              <button
                onClick={() => void resolve(record.id, 'reject')}
                disabled={busyId !== null}
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
