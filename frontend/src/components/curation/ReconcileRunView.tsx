// Reconcile Run & Status (ADR 0009 P2). Submits propose/apply jobs to the daemon's
// in-process curation queue (the daemon stays UP — jobs run on its one handle off
// the event loop) and polls live status. apply is off-graph only.
import { useEffect, useRef, useState } from 'react'
import { GitMerge, Search } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import {
  reconcilePropose,
  reconcileApply,
  getReconcileStatus,
  getJob,
  type CurationJob,
  type ReconcileStatus,
} from '@/services/curation-api'

const STATUS_TONE: Record<string, 'default' | 'warn' | 'accent' | 'danger'> = {
  queued: 'warn',
  running: 'warn',
  done: 'accent',
  error: 'danger',
}

function JobCard({ title, job }: { title: string; job: CurationJob | null }) {
  if (!job) {
    return (
      <div className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3 text-sm text-surface-500">
        {title}: no run yet
      </div>
    )
  }
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-surface-200">{title}</span>
        <Badge tone={STATUS_TONE[job.status] ?? 'default'}>{job.status}</Badge>
        {job.status === 'running' && (
          <span className="text-xs text-surface-500">{job.progress}</span>
        )}
      </div>
      {job.error && <p className="mt-1 text-xs text-rose-400">{job.error}</p>}
      {job.result && (
        <pre className="mt-2 overflow-auto rounded bg-surface-950 p-2 text-xs text-surface-400">
          {JSON.stringify(job.result, null, 2)}
        </pre>
      )}
    </div>
  )
}

export function ReconcileRunView() {
  const [status, setStatus] = useState<ReconcileStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [activeJob, setActiveJob] = useState<CurationJob | null>(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef<number | null>(null)

  async function refreshStatus() {
    try {
      const s = (await getReconcileStatus()) as ReconcileStatus
      setStatus(s)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    refreshStatus()
    const t = setInterval(refreshStatus, 4000)
    return () => clearInterval(t)
  }, [])

  // Poll the active job until terminal.
  useEffect(() => {
    if (!activeJobId) return
    let cancelled = false
    async function tick() {
      try {
        const { job } = await getJob(activeJobId!)
        if (cancelled) return
        setActiveJob(job)
        if (job.status === 'done' || job.status === 'error') {
          setActiveJobId(null)
          refreshStatus()
          return
        }
      } catch {
        /* keep polling */
      }
      pollRef.current = window.setTimeout(tick, 1500)
    }
    tick()
    return () => {
      cancelled = true
      if (pollRef.current) window.clearTimeout(pollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobId])

  async function run(kind: 'propose' | 'apply') {
    setBusy(true)
    setError(null)
    try {
      const resp = kind === 'propose' ? await reconcilePropose() : await reconcileApply()
      setActiveJob(resp.job)
      setActiveJobId(resp.job.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-medium text-surface-200">Reconcile run &amp; status</h2>
        <p className="text-xs text-surface-500">
          Off-graph entity reconciliation on the live daemon handle. Propose is read-only; apply
          auto-merges high-confidence clusters to the authority index and queues the rest. The
          daemon stays up the whole time.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => run('propose')}
          disabled={busy || !!activeJobId}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-4 py-2 text-sm text-surface-200 hover:bg-surface-800 disabled:opacity-50"
        >
          <Search size={14} />
          Propose (read-only)
        </button>
        <button
          onClick={() => run('apply')}
          disabled={busy || !!activeJobId}
          className="flex items-center gap-2 rounded-lg bg-accent-600 px-4 py-2 text-sm font-medium text-white hover:bg-accent-500 disabled:opacity-50"
        >
          <GitMerge size={14} />
          Apply (off-graph)
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {activeJobId && <Spinner label={`Running ${activeJob?.kind ?? 'job'}…`} />}

      <div className="space-y-3">
        {activeJob && activeJobId && <JobCard title="Active job" job={activeJob} />}
        {status && (
          <>
            <div className="flex flex-wrap gap-3 text-xs text-surface-400">
              <Badge>queue {status.queue_count}</Badge>
              <Badge>authority {status.authority_count}</Badge>
              <Badge tone={status.worker_active ? 'warn' : 'default'}>
                {status.worker_active ? 'worker active' : 'idle'}
              </Badge>
            </div>
            <JobCard title="Last propose" job={status.last_propose} />
            <JobCard title="Last apply" job={status.last_apply} />
          </>
        )}
      </div>
    </div>
  )
}
