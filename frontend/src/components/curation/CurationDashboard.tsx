// Dashboard tile (ADR 0009 P1): health + graph counts over existing endpoints
// (/health, /api/v1/graph/stats), plus the curation job-queue summary (P2). The
// run-now buttons live in the dedicated tabs; this is the orientation view.
import { useEffect, useRef, useState } from 'react'
import { GitMerge, RefreshCw, Search } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import {
  getHealth,
  getGraphStats,
  getCurationJobs,
  getScheduler,
  getJob,
  predicateUpkeepApply,
  predicateUpkeepPropose,
  type HealthResponse,
  type GraphStatsLite,
  type JobsSnapshot,
  type SchedulerStatus,
  type CurationJob,
  type PredicateUpkeepSnapshot,
} from '@/services/curation-api'
import { refreshPredicateSnapshot, usePredicateSnapshot } from '@/services/predicate-snapshot'

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3">
      <div className="text-2xl font-semibold text-surface-100">{value}</div>
      <div className="text-xs text-surface-500">{label}</div>
    </div>
  )
}

function fmtTime(epoch: number | null | undefined): string {
  if (!epoch) return '—'
  return new Date(epoch * 1000).toLocaleString()
}

function fmtNextEligible(next: number | string): string {
  if (typeof next === 'string') return next
  const now = Date.now() / 1000
  if (next <= now) return 'now'
  const secs = Math.round(next - now)
  if (secs < 60) return `in ${secs}s`
  if (secs < 3600) return `in ${Math.round(secs / 60)}m`
  return `in ${Math.round(secs / 3600)}h`
}

const STATUS_TONE: Record<string, 'default' | 'warn' | 'accent' | 'danger'> = {
  queued: 'warn',
  running: 'warn',
  done: 'accent',
  error: 'danger',
}

// Render the outcome of one auto sweep job from its RESULT payload — the propose
// job carries candidate-cluster counts, the detect-drift job carries Finding
// counts. This is how the user SEES the loop working (propose writes nothing; the
// value is in the result, not a populated queue).
function sweepOutcome(job: CurationJob): string {
  if (job.status === 'error') return job.error || 'error'
  if (job.status !== 'done') return job.progress || job.status
  const r = job.result || {}
  if (job.kind === 'reconcile-propose') {
    const count = (r['count'] as number) ?? (Array.isArray(r['clusters']) ? (r['clusters'] as unknown[]).length : 0)
    return `${count} candidate cluster${count === 1 ? '' : 's'}`
  }
  if (job.kind === 'detect-drift') {
    const total = (r['total'] as number) ?? 0
    return `${total} finding${total === 1 ? '' : 's'}`
  }
  return 'done'
}

function predicateJobOutcome(job: CurationJob | null): string {
  if (!job) return 'no run yet'
  if (job.status === 'error') return job.error || 'error'
  if (job.status !== 'done') return job.progress || job.status
  const r = job.result || {}
  if (job.kind === 'predicate-propose') {
    const judged = Number(r['judged'] ?? 0)
    const queued = Number(r['queued'] ?? 0)
    const autoEligible = Number(r['auto_eligible'] ?? 0)
    if (r['disabled']) return 'disabled'
    return `${judged} judged · ${queued} queued · ${autoEligible} auto`
  }
  if (job.kind === 'predicate-apply') {
    const counts = r['counts'] as Record<string, unknown> | undefined
    const total = Number(counts?.total ?? 0)
    const queued = Number(counts?.queued ?? 0)
    const auto = Number(counts?.auto ?? 0)
    return `${total} written · ${queued} queued · ${auto} auto`
  }
  return 'done'
}

function JobCard({ title, job }: { title: string; job: CurationJob | null }) {
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-950 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-surface-300">{title}</span>
        {job && <Badge tone={STATUS_TONE[job.status] ?? 'default'}>{job.status}</Badge>}
        <span className="text-xs text-surface-500">{predicateJobOutcome(job)}</span>
      </div>
    </div>
  )
}

function PredicateUpkeepPanel({
  snapshot,
  activeJob,
  active,
  busy,
  onRun,
  onNavigate,
}: {
  snapshot: PredicateUpkeepSnapshot
  activeJob: CurationJob | null
  active: boolean
  busy: boolean
  onRun: (kind: 'propose' | 'apply') => void
  onNavigate?: (tab: string) => void
}) {
  const counts = snapshot.counts
  const latestProposeDone =
    snapshot.last_propose?.status === 'done' ? snapshot.last_propose.id : undefined
  return (
    <div className="rounded-lg border border-cyan-900/60 bg-cyan-950/10 p-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium text-surface-200">Predicate upkeep</h3>
            <Badge tone={snapshot.worker_active ? 'warn' : 'default'}>
              {snapshot.worker_active ? 'worker active' : 'idle'}
            </Badge>
          </div>
          <p className="mt-1 text-xs text-surface-500">
            Relation-name canonicalization runs through a separate predicate queue.
          </p>
        </div>
        <button
          onClick={() => onNavigate?.('predicate-upkeep')}
          className="shrink-0 rounded-lg border border-cyan-700/60 px-3 py-1.5 text-xs text-cyan-200 hover:bg-cyan-900/30"
        >
          Review queue →
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Stat label="vocabulary" value={snapshot.vocabulary_size} />
        <Stat label="auto" value={counts.auto ?? 0} />
        <Stat label="confirmed" value={counts.confirmed ?? 0} />
        <Stat label="queued" value={counts.queued ?? 0} />
        <Stat label="rejected" value={counts.rejected ?? 0} />
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        {active && activeJob && <JobCard title="Active predicate job" job={activeJob} />}
        <JobCard title="Last propose" job={snapshot.last_propose} />
        <JobCard title="Last apply" job={snapshot.last_apply} />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          onClick={() => onRun('propose')}
          disabled={busy || active}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-2 text-sm text-surface-200 hover:bg-surface-800 disabled:opacity-50"
        >
          <Search size={14} />
          Propose
        </button>
        <button
          onClick={() => onRun('apply')}
          disabled={busy || active}
          title={latestProposeDone ? 'Applies the latest completed propose result' : undefined}
          className="flex items-center gap-2 rounded-lg bg-cyan-700 px-3 py-2 text-sm font-medium text-white hover:bg-cyan-600 disabled:opacity-50"
        >
          <GitMerge size={14} />
          Apply
        </button>
      </div>
    </div>
  )
}

function SchedulerPanel({ sched }: { sched: SchedulerStatus }) {
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900 p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium text-surface-200">Continuous curation</h3>
        <Badge tone={sched.enabled ? 'accent' : 'default'}>
          {sched.enabled ? 'loop on' : 'loop off'}
        </Badge>
      </div>
      <p className="mb-3 text-xs text-surface-500">
        Auto-runs propose + drift sweeps as you ingest (read-only; never auto-merges
        or rebuilds). You just review.
      </p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-surface-400 sm:grid-cols-3">
        <div>
          <span className="text-surface-500">last sweep</span>{' '}
          {fmtTime(sched.last_sweep_at)}
        </div>
        <div>
          <span className="text-surface-500">next eligible</span>{' '}
          {sched.enabled ? fmtNextEligible(sched.next_eligible) : 'disabled'}
        </div>
        <div>
          <span className="text-surface-500">cadence</span>{' '}
          {sched.quiet_debounce_s}s quiet · {Math.round(sched.min_interval_s / 60)}m floor
        </div>
      </div>

      {sched.recent.length > 0 ? (
        <div className="mt-3 space-y-1">
          <div className="text-xs font-medium text-surface-500">recent auto sweeps</div>
          {sched.recent.slice(0, 8).map((j) => (
            <div
              key={j.id}
              className="flex items-center justify-between rounded border border-surface-800 bg-surface-950 px-2 py-1 text-xs"
            >
              <span className="font-mono text-surface-400">{j.kind}</span>
              <span className="text-surface-500">{fmtTime(j.finished_at ?? j.created_at)}</span>
              <span
                className={
                  j.status === 'error' ? 'text-rose-400' : 'text-surface-300'
                }
              >
                {sweepOutcome(j)}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-3 text-xs text-surface-500">
          {sched.enabled
            ? sched.sweep_pending
              ? 'a sweep is in flight…'
              : 'no auto sweep yet — ingest something and the loop will run'
            : 'enable the loop in okto-neuron.yaml (curation.enabled) to offload curation'}
        </div>
      )}
    </div>
  )
}

export function CurationDashboard({
  onNavigate,
}: {
  onNavigate?: (tab: string) => void
}) {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [stats, setStats] = useState<GraphStatsLite | null>(null)
  const [jobs, setJobs] = useState<JobsSnapshot | null>(null)
  const [sched, setSched] = useState<SchedulerStatus | null>(null)
  // Shared predicate query: one timer for every panel, paused while the tab is hidden.
  const { snapshot: predicate } = usePredicateSnapshot()
  const [activePredicateJobId, setActivePredicateJobId] = useState<string | null>(null)
  const [activePredicateJob, setActivePredicateJob] = useState<CurationJob | null>(null)
  const [predicateBusy, setPredicateBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const predicatePollRef = useRef<number | null>(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const [h, s, j, sc] = await Promise.all([
        getHealth(),
        getGraphStats(),
        getCurationJobs(),
        getScheduler(),
      ])
      setHealth(h)
      setStats(s)
      setJobs(j)
      setSched(sc)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    const t = setInterval(() => {
      if (document.visibilityState !== 'hidden') refresh()
    }, 5000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!activePredicateJobId) return
    let cancelled = false
    async function tick() {
      try {
        const { job } = await getJob(activePredicateJobId!)
        if (cancelled) return
        setActivePredicateJob(job)
        if (job.status === 'done' || job.status === 'error') {
          setActivePredicateJobId(null)
          void refreshPredicateSnapshot()
          refresh()
          return
        }
      } catch {
        /* keep polling */
      }
      predicatePollRef.current = window.setTimeout(tick, 1500)
    }
    tick()
    return () => {
      cancelled = true
      if (predicatePollRef.current) window.clearTimeout(predicatePollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePredicateJobId])

  async function runPredicate(kind: 'propose' | 'apply') {
    setPredicateBusy(true)
    setError(null)
    try {
      const latestPropose =
        kind === 'apply' && predicate?.last_propose?.status === 'done'
          ? predicate.last_propose.id
          : undefined
      const resp =
        kind === 'propose'
          ? await predicateUpkeepPropose()
          : await predicateUpkeepApply(latestPropose)
      setActivePredicateJob(resp.job)
      setActivePredicateJobId(resp.job.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPredicateBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium text-surface-200">Overview</h2>
        <button
          onClick={() => {
            void refreshPredicateSnapshot()
            refresh()
          }}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-sm text-surface-300 hover:bg-surface-800"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {!health && !error && <Spinner label="Loading health…" />}

      {health && (
        <div className="space-y-1 text-sm text-surface-400">
          <div className="flex items-center gap-2">
            <Badge tone={health.status === 'ok' ? 'accent' : 'danger'}>
              {health.status === 'ok' ? 'daemon up' : health.status}
            </Badge>
            <span className="font-mono text-xs text-surface-500">{health.vault_path}</span>
          </div>
          <div className="text-xs text-surface-500">
            uptime {health.uptime_s}s · pid {health.pid}
          </div>
        </div>
      )}

      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="nodes" value={stats.total_nodes} />
          <Stat label="edges" value={stats.total_edges} />
          <Stat label="node types" value={stats.node_types.length} />
          <Stat label="edge types" value={stats.edge_types.length} />
        </div>
      )}

      {sched && <SchedulerPanel sched={sched} />}

      {predicate && (
        <PredicateUpkeepPanel
          snapshot={predicate}
          activeJob={activePredicateJob}
          active={!!activePredicateJobId}
          busy={predicateBusy}
          onRun={(kind) => void runPredicate(kind)}
          onNavigate={onNavigate}
        />
      )}

      {jobs && (
        <div className="rounded-lg border border-surface-800 bg-surface-900 p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium text-surface-200">Curation jobs</h3>
            <Badge tone={jobs.summary.active ? 'warn' : 'default'}>
              {jobs.summary.active ? 'worker active' : 'idle'}
            </Badge>
          </div>
          <div className="flex flex-wrap gap-4 text-xs text-surface-400">
            <span>queued {jobs.summary.queued}</span>
            <span>running {jobs.summary.running}</span>
            <span>done {jobs.summary.done}</span>
            <span className={jobs.summary.error ? 'text-rose-400' : ''}>
              error {jobs.summary.error}
            </span>
          </div>
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => onNavigate?.('drift')}
          className="rounded-lg border border-surface-700 px-3 py-2 text-sm text-surface-300 hover:bg-surface-800"
        >
          Detect drift →
        </button>
        <button
          onClick={() => onNavigate?.('reconcile-run')}
          className="rounded-lg border border-surface-700 px-3 py-2 text-sm text-surface-300 hover:bg-surface-800"
        >
          Run reconcile →
        </button>
        <button
          onClick={() => onNavigate?.('companion-review')}
          className="rounded-lg border border-surface-700 px-3 py-2 text-sm text-surface-300 hover:bg-surface-800"
        >
          Companion review →
        </button>
      </div>
    </div>
  )
}
