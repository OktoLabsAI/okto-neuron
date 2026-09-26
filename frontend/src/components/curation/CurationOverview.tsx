// Review-first Overview: leads with what needs the user's decision, states
// graph health in plain language, nudges to heal when confirmed decisions are
// not yet materialized. The old dashboard survives intact under "Details".
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Wrench } from 'lucide-react'
import { Badge } from '@/components/ui'
import {
  getHealth,
  getGraphStats,
  getScheduler,
  type HealthResponse,
  type GraphStatsLite,
  type SchedulerStatus,
} from '@/services/curation-api'
import { CurationDashboard } from './CurationDashboard'
import type { Attention } from './useAttention'

function agoLabel(epoch: number | null | undefined): string {
  if (!epoch) return 'never'
  const secs = Math.max(0, Math.round(Date.now() / 1000 - epoch))
  if (secs < 90) return 'just now'
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

export function CurationOverview({
  attention,
  onGoReview,
  onGoMaintenance,
}: {
  attention: Attention
  onGoReview: () => void
  onGoMaintenance: () => void
}) {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [stats, setStats] = useState<GraphStatsLite | null>(null)
  const [sched, setSched] = useState<SchedulerStatus | null>(null)
  const [showDetails, setShowDetails] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [h, s, sc] = await Promise.all([
          getHealth().catch(() => null),
          getGraphStats().catch(() => null),
          getScheduler().catch(() => null),
        ])
        if (cancelled) return
        setHealth(h)
        setStats(s)
        setSched(sc)
      } catch {
        /* transient */
      }
    }
    load()
    const t = setInterval(load, 30000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const claimCount = stats?.node_types.find((t) => t.type === 'Claim')?.count
  const lastDrift = sched?.recent.find((j) => j.kind === 'detect-drift')
  const driftFindings =
    lastDrift?.status === 'done' ? Number((lastDrift.result ?? {})['total'] ?? 0) : null

  const breakdown = [
    attention.predicates > 0 &&
      `${attention.predicates} predicate fold${attention.predicates === 1 ? '' : 's'}`,
    attention.entityMerges > 0 &&
      `${attention.entityMerges} entity merge${attention.entityMerges === 1 ? '' : 's'}`,
    attention.nodeCandidates > 0 &&
      `${attention.nodeCandidates} node candidate${attention.nodeCandidates === 1 ? '' : 's'}`,
  ].filter(Boolean) as string[]

  return (
    <div className="space-y-4">
      {/* ── needs-your-decision hero ─────────────────────────────────── */}
      {attention.total > 0 ? (
        <div className="flex items-center justify-between gap-4 rounded-lg border border-amber-800/60 bg-amber-950/20 p-4">
          <div>
            <div className="text-base font-medium text-amber-200">
              ⚠ {attention.total} item{attention.total === 1 ? '' : 's'} need your decision
            </div>
            <div className="mt-1 text-sm text-surface-400">{breakdown.join(' · ')}</div>
          </div>
          <button
            onClick={onGoReview}
            className="shrink-0 rounded-lg bg-amber-700 px-4 py-2 text-sm font-medium text-white hover:bg-amber-600"
          >
            Review →
          </button>
        </div>
      ) : (
        <div className="rounded-lg border border-surface-800 bg-surface-900 p-4 text-sm text-surface-300">
          ✅ Nothing waiting for you — all review queues are empty.
        </div>
      )}

      {/* ── plain-language health line ───────────────────────────────── */}
      <div className="rounded-lg border border-surface-800 bg-surface-900 p-4">
        <div className="flex flex-wrap items-center gap-2 text-sm text-surface-300">
          <Badge tone={health?.status === 'ok' ? 'accent' : 'danger'}>
            {health?.status === 'ok' ? 'healthy' : (health?.status ?? 'unreachable')}
          </Badge>
          {stats && (
            <span>
              {stats.total_nodes.toLocaleString()} nodes
              {claimCount != null && <> · {claimCount.toLocaleString()} claims</>} ·{' '}
              {stats.total_edges.toLocaleString()} edges
            </span>
          )}
        </div>
        <div className="mt-2 text-xs text-surface-500">
          {sched?.enabled
            ? `Auto-curation on — last sweep ${agoLabel(sched.last_sweep_at)}`
            : 'Auto-curation is off'}
          {driftFindings != null &&
            (driftFindings === 0
              ? ' · no source drift'
              : ` · ⚠ ${driftFindings} drifted source${driftFindings === 1 ? '' : 's'}`)}
        </div>
      </div>

      {/* ── heal nudge ───────────────────────────────────────────────── */}
      {attention.unappliedDecisions > 0 && (
        <div className="flex items-center justify-between gap-4 rounded-lg border border-cyan-900/60 bg-cyan-950/10 p-4">
          <div className="text-sm text-surface-300">
            ⏳ {attention.unappliedDecisions} confirmed decision
            {attention.unappliedDecisions === 1 ? '' : 's'} not yet applied to the graph
          </div>
          <button
            onClick={onGoMaintenance}
            className="flex shrink-0 items-center gap-2 rounded-lg bg-cyan-700 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-600"
          >
            <Wrench size={14} />
            Apply (heal) →
          </button>
        </div>
      )}

      {/* ── power-user details (the old dashboard, mounted on demand) ── */}
      <div className="rounded-lg border border-surface-800">
        <button
          onClick={() => setShowDetails((v) => !v)}
          className="flex w-full items-center gap-2 px-4 py-3 text-sm text-surface-400 hover:text-surface-200"
        >
          {showDetails ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          Details — stats, scheduler history, job queue
        </button>
        {showDetails && (
          <div className="border-t border-surface-800 p-4">
            <CurationDashboard
              onNavigate={(t) =>
                ['drift', 'reconcile-run', 'rebuild-heal'].includes(t)
                  ? onGoMaintenance()
                  : onGoReview()
              }
            />
          </div>
        )}
      </div>
    </div>
  )
}
