import { useEffect, useRef, useState } from 'react'
import { Loader2, ListChecks } from 'lucide-react'
import { getLedgerSummary } from '@/services/ledger-api'
import type { LedgerProgressRow, LedgerSummaryResponse } from '@/types'

// Poll cadence while an ingest-queue item is processing. The ledger summary is
// a derived read (no LLM, no writes) so 5s keeps the view live without load.
const POLL_MS = 5000

interface Activity {
  // Sum of curator "done" counts at the last observed change + when it changed.
  fingerprint: string
  at: number // epoch ms
}

// ADR 0039 T9: a progress row is telemetry, not decoration. `done > total` is
// an integrity error the server reports explicitly (`progress_integrity_error`)
// with an unbounded `fraction`; the UI must surface that state with the real
// numbers instead of clamping the bar to a reassuring 100%.
function CuratorBar({ label, row }: { label: string; row: LedgerProgressRow }) {
  const err = row.progress_integrity_error
  if (err) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-xs">
          <span className="text-rose-300">{label}</span>
          <span className="font-mono text-[11px] text-rose-300">
            {err.done}/{err.total}
            <span className="text-rose-400"> · +{err.overflow} over</span>
          </span>
        </div>
        <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1.5">
          <div className="font-mono text-[10px] text-rose-300">{err.code}</div>
          <div className="text-[11px] text-rose-200/90">
            reported more done than the declared {err.population} population
            {row.fraction !== null && (
              <span className="font-mono text-rose-300">
                {' '}
                (fraction {row.fraction.toFixed(2)})
              </span>
            )}
            . Progress for this phase is not trustworthy.
          </div>
        </div>
      </div>
    )
  }
  // Healthy row: fraction is <= 1 by the server's own invariant, so no clamp.
  const raw = row.fraction !== null ? row.fraction : row.total > 0 ? row.done / row.total : 0
  const pct = Math.round(raw * 100)
  const complete = row.total > 0 && row.remaining === 0
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-surface-300">{label}</span>
        <span className="font-mono text-[11px] text-surface-400">
          {row.done}/{row.total}
          {row.remaining > 0 && (
            <span className="text-surface-600"> · {row.remaining} left</span>
          )}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-800">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            complete ? 'bg-accent-500' : 'bg-amber-400'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function phaseLabel(s: LedgerSummaryResponse): string {
  const p = s.progress
  if (!p) return 'Extracting candidates…'
  if (p.node_curator.total > 0 && p.node_curator.remaining > 0) {
    return 'Curating nodes'
  }
  if (p.relation_curator.total > 0 && p.relation_curator.remaining > 0) {
    return 'Curating relationships & claims'
  }
  if (p.node_curator.total > 0 || p.relation_curator.total > 0) {
    return 'Committing'
  }
  return 'Extracting candidates…'
}

function ago(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s ago`
}

/**
 * Human-readable consolidation progress while an ingest item is processing:
 * current phase, a done/total bar per curator (node + relation), and how long
 * ago the curators last advanced. Polls /api/v1/ledger/summary every 5s while
 * `active` is true; renders nothing until the first summary with progress lands.
 */
export function CurationProgress({ active }: { active: boolean }) {
  const [summary, setSummary] = useState<LedgerSummaryResponse | null>(null)
  const [, setRenderTick] = useState(0) // re-render so "Xs ago" stays live
  const activityRef = useRef<Activity | null>(null)

  useEffect(() => {
    if (!active) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const s = await getLedgerSummary()
        if (cancelled) return
        if (s.progress) {
          const fp = `${s.progress.node_curator.done}:${s.progress.relation_curator.done}:${s.counts?.comparisons ?? 0}`
          if (!activityRef.current || activityRef.current.fingerprint !== fp) {
            activityRef.current = { fingerprint: fp, at: Date.now() }
          }
        }
        setSummary(s)
      } catch {
        // Ledger not present yet / server busy — keep the last view, stay quiet.
      } finally {
        if (!cancelled) {
          setRenderTick((t) => t + 1)
          timer = setTimeout(() => void tick(), POLL_MS)
        }
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [active])

  if (!active || !summary?.progress) return null
  const { node_curator, relation_curator } = summary.progress
  // Nothing curatable yet (extraction still running) — show the phase only
  // once the ledger has actual candidate totals. An integrity error is never
  // hidden by that rule: done > total == 0 is exactly the state to surface.
  const hasIntegrityError = Boolean(
    node_curator.progress_integrity_error || relation_curator.progress_integrity_error
  )
  if (node_curator.total === 0 && relation_curator.total === 0 && !hasIntegrityError)
    return null

  const lastActivity = activityRef.current

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-surface-800 bg-surface-900/40 px-4 py-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-medium text-surface-100">
          <ListChecks size={15} className="text-accent-400" />
          Curation
        </div>
        <div className="flex items-center gap-2 text-xs text-amber-300">
          <Loader2 size={13} className="animate-spin" />
          {phaseLabel(summary)}
        </div>
      </div>
      <CuratorBar label="Node curator" row={node_curator} />
      <CuratorBar label="Relationship curator" row={relation_curator} />
      <div className="flex items-center justify-between text-[11px] text-surface-500">
        <span>
          {summary.counts ? `${summary.counts.comparisons} LLM verdicts recorded` : ''}
        </span>
        {lastActivity && <span>last activity {ago(lastActivity.at)}</span>}
      </div>
    </div>
  )
}
