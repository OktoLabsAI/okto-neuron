// Rebuild / Heal / Reembed (ADR 0009 P3). Triggers an in-process fresh-graph job
// on the daemon's one handle: the graph is rebuilt at a tmp path while the daemon
// keeps serving from the live handle, then close→replace→reopen swapped at the end.
// The daemon stays UP the whole build; the handle is unavailable only for the swap
// instant (surfaced as a "swapping" status line). Heal additionally folds the
// confirmed off-graph equivalences so the fresh graph has ONE node per merged entity.
import { useEffect, useRef, useState } from 'react'
import { Wrench, GitMerge, RefreshCw, Hammer, Undo2 } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import {
  startRebuild,
  startRollback,
  startHeal,
  startReembed,
  getRebuildStatus,
  type CurationJob,
  type RebuildStatus,
  type SemanticRebuildGate,
} from '@/services/curation-api'

const STATUS_TONE: Record<string, 'default' | 'warn' | 'accent' | 'danger'> = {
  queued: 'warn',
  running: 'warn',
  done: 'accent',
  error: 'danger',
}

function LastRun({ title, job }: { title: string; job: CurationJob | null }) {
  if (!job) {
    return (
      <div className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3 text-sm text-surface-500">
        {title}: no run yet
      </div>
    )
  }
  const heal = (job.result?.heal ?? null) as {
    records?: number
    nodes_dropped?: number
    edges_dropped?: number
  } | null
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
        <div className="mt-2 space-y-1 text-xs text-surface-400">
          {typeof job.result.files_done === 'number' && (
            <div>files: {job.result.files_done as number}</div>
          )}
          {heal && (
            <div>
              collapsed {heal.nodes_dropped ?? 0} variant nodes across{' '}
              {heal.records ?? 0} records
            </div>
          )}
          {typeof job.result.sha256 === 'string' && (
            <div className="truncate font-mono">sha256 {String(job.result.sha256).slice(0, 16)}…</div>
          )}
        </div>
      )}
    </div>
  )
}

function SemanticGate({ gate }: { gate: SemanticRebuildGate }) {
  const passed = gate.swap_allowed && gate.status === 'passed'
  const passedChecks = gate.checks.filter((check) => check.status === 'passed').length
  const failedChecks = gate.checks.filter((check) => check.status !== 'passed')

  return (
    <section
      className={`rounded-lg border p-4 ${
        passed
          ? 'border-emerald-800/60 bg-emerald-950/15'
          : 'border-rose-800/60 bg-rose-950/20'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-surface-200">Semantic rebuild gate</span>
        <Badge tone={passed ? 'accent' : 'danger'}>{gate.status}</Badge>
        <Badge tone={passed ? 'accent' : 'danger'}>
          {gate.swap_allowed ? 'swap allowed' : 'swap blocked'}
        </Badge>
        <span className="text-xs text-surface-500">
          {passedChecks}/{gate.checks.length} checks passed
        </span>
      </div>
      <p className="mt-1 font-mono text-[11px] text-surface-600">{gate.schema_version}</p>
      {failedChecks.length > 0 && (
        <ul className="mt-3 space-y-2">
          {failedChecks.map((check) => (
            <li
              key={check.code}
              className="rounded-sm border border-rose-900/50 bg-surface-950/40 px-3 py-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <code className="text-xs text-rose-200">{check.code}</code>
                <Badge tone="danger">{check.source_status || check.status}</Badge>
                {check.count !== null && (
                  <span className="text-xs text-surface-500">count {check.count}</span>
                )}
              </div>
              {check.reason && <p className="mt-1 text-xs text-surface-400">{check.reason}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export function RebuildHealView() {
  const [status, setStatus] = useState<RebuildStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef<number | null>(null)

  async function refresh() {
    try {
      const s = await getRebuildStatus()
      setStatus(s)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    refresh()
    pollRef.current = window.setInterval(refresh, 2000)
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [])

  const running = status?.running || status?.worker_active || false
  const phase = status?.phase?.phase ?? 'idle'
  const swapping =
    phase === 'swapping' ||
    status?.last_rebuild?.progress === 'swapping' ||
    status?.last_rollback?.progress === 'swapping rollback checkpoint'
  const semanticGate = status?.phase?.semantic_gate

  async function run(kind: 'rebuild' | 'rollback' | 'heal' | 'reembed') {
    if (
      kind === 'rollback' &&
      !window.confirm(
        'Restore the verified graph checkpoint before the current generation? ' +
          'The effective semantic configuration must match it; saved decision files ' +
          'are restored automatically.',
      )
    ) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (kind === 'rebuild') await startRebuild()
      else if (kind === 'rollback') await startRollback()
      else if (kind === 'heal') await startHeal()
      else await startReembed()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const disabled = busy || running

  return (
    <div className="space-y-5">
      <div>
        <h2 className="flex items-center gap-2 text-base font-medium text-surface-200">
          <Wrench size={16} /> Rebuild &amp; heal
        </h2>
        <p className="text-xs text-surface-500">
          Both build a fresh graph at a temp path while the daemon keeps serving, then swap atomically
          at the end (the handle is unavailable only for that swap instant). Rebuild re-extracts from
          the markdown trust root (full pipeline). Heal is a fast deterministic copy with NO LLM: it
          folds the confirmed off-graph equivalences into the topology so the graph has one node per
          merged entity. Reversible: re-run any time, or un-merge an authority record and re-heal.
          Rollback restores the verified graph checkpoint immediately before the current rebuild;
          restore its prior semantic configuration first. The matching decision files are
          checkpointed and restored automatically with the graph.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => run('rollback')}
          disabled={disabled}
          className="flex items-center gap-2 rounded-lg border border-amber-800/70 px-4 py-2 text-sm text-amber-200 hover:bg-amber-950/30 disabled:opacity-50"
        >
          <Undo2 size={14} />
          Roll back previous rebuild
        </button>
        <button
          onClick={() => run('rebuild')}
          disabled={disabled}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-4 py-2 text-sm text-surface-200 hover:bg-surface-800 disabled:opacity-50"
        >
          <Hammer size={14} />
          Rebuild from trust root
        </button>
        <button
          onClick={() => run('heal')}
          disabled={disabled}
          className="flex items-center gap-2 rounded-lg bg-accent-600 px-4 py-2 text-sm font-medium text-white hover:bg-accent-500 disabled:opacity-50"
        >
          <GitMerge size={14} />
          Heal — fold confirmed equivalences
        </button>
        <button
          onClick={() => run('reembed')}
          disabled={disabled}
          className="flex items-center gap-2 rounded-lg border border-surface-700 px-4 py-2 text-sm text-surface-200 hover:bg-surface-800 disabled:opacity-50"
        >
          <RefreshCw size={14} />
          Reembed (vectors only)
        </button>
      </div>

      {error && <ErrorBox message={error} />}

      {running && (
        <div className="flex items-center gap-3">
          <Spinner label={swapping ? 'Swapping graph (brief downtime)…' : `Working — ${phase}`} />
          {status?.phase?.current_file && (
            <span className="text-xs text-surface-500">{status.phase.current_file}</span>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-3 text-xs text-surface-400">
        <Badge tone={running ? 'warn' : 'default'}>{running ? 'running' : 'idle'}</Badge>
        <Badge>phase {phase}</Badge>
        {typeof status?.phase?.files_done !== 'undefined' && (
          <Badge>
            {Array.isArray(status.phase.files_done)
              ? status.phase.files_done.length
              : (status.phase.files_done as number)}{' '}
            files done
          </Badge>
        )}
      </div>

      {semanticGate && <SemanticGate gate={semanticGate} />}

      <div className="space-y-3">
        <LastRun title="Last rebuild" job={status?.last_rebuild ?? null} />
        <LastRun title="Last heal" job={status?.last_heal ?? null} />
        <LastRun title="Last reembed" job={status?.last_reembed ?? null} />
        <LastRun title="Last rollback" job={status?.last_rollback ?? null} />
      </div>
    </div>
  )
}
