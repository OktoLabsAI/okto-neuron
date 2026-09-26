import { useState } from 'react'
import { RefreshCw, ScanSearch } from 'lucide-react'
import { Badge, ErrorBox, Spinner } from '@/components/ui'
import {
  SEMANTIC_QUALITY_LAYERS,
  getSemanticGovernance,
  runSemanticQualityAudit,
  type GovernanceIdentityDecision,
  type GovernancePredicateRecord,
  type SemanticInvariantCheck,
  type SemanticGovernanceResponse,
  type SemanticQualityLayerName,
  type SemanticQualityReport,
} from '@/services/curation-api'

const LAYER_LABELS: Record<SemanticQualityLayerName, string> = {
  surface: 'Surface',
  type: 'Type',
  identity: 'Identity',
  predicate: 'Predicate',
  relation: 'Relation',
  recall: 'Recall',
}

type BadgeTone = 'default' | 'accent' | 'violet' | 'warn' | 'danger'

function statusTone(status: string): BadgeTone {
  if (['passed', 'measured', 'verified'].includes(status)) return 'accent'
  if (['failed', 'rejected'].includes(status)) return 'danger'
  if (
    [
      'incomplete',
      'not_measured',
      'not_supplied',
      'unverified',
      'unavailable',
      'inconsistent',
      'pre_commit',
      'cached',
    ].includes(status)
  ) {
    return 'warn'
  }
  return 'default'
}

function label(key: string): string {
  return key.replace(/_/g, ' ')
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function primitive(value: unknown): string | null {
  if (value === null) return 'not measured (null)'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number' || typeof value === 'string') return String(value)
  return null
}

function EvidenceValue({ value }: { value: unknown }) {
  const direct = primitive(value)
  if (direct !== null) {
    return <span className="break-all font-mono text-xs text-surface-200">{direct}</span>
  }

  const record = isRecord(value) ? value : null
  const status = record && typeof record.status === 'string' ? record.status : null
  const reason = record && typeof record.reason === 'string' ? record.reason : null
  const count = record && (typeof record.count === 'number' || record.count === null)
    ? record.count
    : undefined

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        {status && <Badge tone={statusTone(status)}>{status}</Badge>}
        {count !== undefined && (
          <span className="text-xs text-surface-300">
            count: {count === null ? 'not measured' : count.toLocaleString()}
          </span>
        )}
        <span className="text-xs text-surface-500">
          {Array.isArray(value) ? `${value.length} item${value.length === 1 ? '' : 's'}` : null}
        </span>
      </div>
      {reason && <p className="text-xs leading-relaxed text-surface-500">{reason}</p>}
      <details className="text-xs text-surface-500">
        <summary className="cursor-pointer select-none hover:text-surface-300">View evidence</summary>
        <pre className="mt-2 max-h-72 overflow-auto rounded bg-surface-950 p-3 font-mono text-[11px] leading-relaxed text-surface-300">
          {JSON.stringify(value, null, 2)}
        </pre>
      </details>
    </div>
  )
}

function FieldGrid({ values }: { values: Record<string, unknown> }) {
  return (
    <dl className="grid gap-px overflow-hidden rounded-lg border border-surface-800 bg-surface-800 sm:grid-cols-2">
      {Object.entries(values).map(([key, value]) => (
        <div key={key} className="min-w-0 bg-surface-900 p-3">
          <dt className="mb-1 text-[11px] font-medium uppercase tracking-wide text-surface-500">
            {label(key)}
          </dt>
          <dd><EvidenceValue value={value} /></dd>
        </div>
      ))}
    </dl>
  )
}

function LayerCard({ name, values }: {
  name: SemanticQualityLayerName
  values: Record<string, unknown>
}) {
  const status = typeof values.status === 'string' ? values.status : null
  return (
    <section className="rounded-lg border border-surface-800 bg-surface-900 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-surface-100">{LAYER_LABELS[name]}</h3>
        {status && <Badge tone={statusTone(status)}>{status}</Badge>}
      </div>
      {Object.keys(values).length > 0 ? (
        <FieldGrid values={values} />
      ) : (
        <p className="text-xs text-surface-500">No fields returned for this layer.</p>
      )}
    </section>
  )
}

function InvariantRow({ check }: { check: SemanticInvariantCheck }) {
  return (
    <li className="rounded-md border border-surface-800 bg-surface-950/50 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <code className="text-xs text-surface-200">{check.code}</code>
        <Badge tone={statusTone(check.status)}>{check.status}</Badge>
      </div>
      <div className="mt-1 text-xs text-surface-500">
        count: {check.count === null ? 'not measured' : check.count.toLocaleString()}
      </div>
      {check.reason && <p className="mt-1 text-xs text-surface-500">{check.reason}</p>}
      {check.samples.length > 0 && (
        <details className="mt-2 text-xs text-surface-500">
          <summary className="cursor-pointer select-none hover:text-surface-300">
            Evidence samples ({check.samples.length})
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto rounded bg-surface-950 p-3 font-mono text-[11px] text-surface-300">
            {JSON.stringify(check.samples, null, 2)}
          </pre>
        </details>
      )}
    </li>
  )
}

function Report({ report, ranAt }: { report: SemanticQualityReport; ranAt: Date }) {
  const evidence = report.evidence
  const verdict = report.verdict
  const invariants = report.hard_invariants

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-surface-800 bg-surface-900 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={statusTone(verdict.status)}>{verdict.status}</Badge>
              <Badge tone={evidence.authoritative ? 'accent' : 'warn'}>
                {evidence.authoritative ? 'authoritative' : 'non-authoritative'}
              </Badge>
              <span className="text-xs text-surface-400">scope: {verdict.scope}</span>
            </div>
            {verdict.reason && (
              <p className="mt-2 max-w-3xl text-sm text-surface-400">{verdict.reason}</p>
            )}
          </div>
          <div className="text-right text-[11px] text-surface-500">
            <div>{report.schema_version}</div>
            <div>{ranAt.toLocaleString()}</div>
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-semibold text-surface-100">Evidence and integrity</h2>
          <p className="mt-1 text-xs text-surface-500">
            Authority, completeness, generation, fingerprints, and supporting audit evidence as
            returned by the evaluator.
          </p>
        </div>
        <FieldGrid values={evidence} />
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-surface-100">Population</h2>
        <FieldGrid values={report.population} />
      </section>

      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-semibold text-surface-100">Quality by semantic layer</h2>
          <p className="mt-1 text-xs text-surface-500">
            Independent measurements only. Okto Neuron does not combine these layers into one score.
          </p>
        </div>
        <div className="grid gap-4 xl:grid-cols-2">
          {SEMANTIC_QUALITY_LAYERS.map((name) => (
            <LayerCard key={name} name={name} values={report.layers[name] ?? {}} />
          ))}
        </div>
      </section>

      <section className="rounded-lg border border-surface-800 bg-surface-900 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-surface-100">Hard invariants</h2>
            <p className="mt-1 text-xs text-surface-500">
              Complete: {String(invariants.complete)} · measured checks pass:{' '}
              {String(invariants.measured_pass)}
            </p>
          </div>
          <Badge tone={statusTone(invariants.status)}>{invariants.status}</Badge>
        </div>
        <ul className="mt-3 grid gap-2 lg:grid-cols-2">
          {invariants.checks.map((check) => <InvariantRow key={check.code} check={check} />)}
        </ul>
      </section>

      {report.limitations.length > 0 && (
        <section className="rounded-lg border border-amber-900/50 bg-amber-950/10 p-4">
          <h2 className="text-sm font-semibold text-amber-200">Limitations</h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-surface-400">
            {report.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}
          </ul>
        </section>
      )}
    </div>
  )
}

function PredicateRecord({ record }: { record: GovernancePredicateRecord }) {
  return (
    <li className="rounded-md border border-surface-800 bg-surface-950/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="text-xs text-surface-200">{record.label}</code>
        <Badge tone={record.lifecycle === 'canonical' ? 'accent' : 'warn'}>
          {record.lifecycle}
        </Badge>
        <span className="text-xs text-surface-500">
          support {record.support_count} · confidence {record.confidence.toFixed(2)}
        </span>
      </div>
      <p className="mt-1 text-xs text-surface-400">{record.definition}</p>
      <p className="mt-1 text-[11px] text-surface-600">
        direction {record.direction} · {record.signatures.length} signature
        {record.signatures.length === 1 ? '' : 's'} · {record.samples.length} evidence sample
        {record.samples.length === 1 ? '' : 's'}
      </p>
    </li>
  )
}

function IdentityDecision({ decision }: { decision: GovernanceIdentityDecision }) {
  const subjects = [
    decision.candidate_id,
    ...(decision.candidate_ids ?? []),
    decision.left_id,
    decision.right_id,
  ].filter((value): value is string => Boolean(value))
  const typeChange = decision.previous_type && decision.corrected_type
    ? `${decision.previous_type} → ${decision.corrected_type}`
    : null

  return (
    <li className="rounded-md border border-surface-800 bg-surface-950/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="violet">{label(decision.kind)}</Badge>
        {typeChange && <span className="text-xs text-surface-300">{typeChange}</span>}
        <code className="break-all text-[11px] text-surface-600">{decision.decision_id}</code>
      </div>
      <p className="mt-1 text-xs text-surface-400">{decision.reason}</p>
      {subjects.length > 0 && (
        <p className="mt-1 break-all font-mono text-[11px] text-surface-600">
          {subjects.join(' · ')}
        </p>
      )}
    </li>
  )
}

function Governance({ governance }: { governance: SemanticGovernanceResponse }) {
  const observed = governance.observed_semantic_policy_fingerprints
  const registry = governance.predicate_registry
  const decisions = governance.identity_decisions

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-surface-100">Semantic governance</h2>
        <p className="mt-1 text-xs text-surface-500">
          Current policy identity and the durable predicate and entity decisions that shape ingest.
        </p>
      </div>

      <div className={`rounded-lg border p-4 ${
        governance.rebuild_required
          ? 'border-amber-800/60 bg-amber-950/15'
          : 'border-surface-800 bg-surface-900'
      }`}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-surface-100">Semantic policy</h3>
          <Badge tone={governance.rebuild_required ? 'warn' : 'accent'}>
            {governance.rebuild_required ? 'rebuild required' : 'current'}
          </Badge>
          <span className="text-xs text-surface-500">
            {governance.latest_applied_run_count} document
            {governance.latest_applied_run_count === 1 ? '' : 's'} with an applied run
          </span>
        </div>
        <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
          <dt className="text-surface-500">current fingerprint</dt>
          <dd className="break-all font-mono text-surface-200">
            {governance.current_semantic_policy_fingerprint}
          </dd>
        </dl>
        {observed.length > 0 ? (
          <details className="mt-3 text-xs text-surface-500">
            <summary className="cursor-pointer select-none hover:text-surface-300">
              Observed policy fingerprints ({observed.length})
            </summary>
            <ul className="mt-2 space-y-2">
              {observed.map((item) => (
                <li key={item.fingerprint} className="rounded border border-surface-800 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      tone={
                        item.fingerprint === governance.current_semantic_policy_fingerprint
                          ? 'accent'
                          : 'warn'
                      }
                    >
                      {item.latest_applied_run_count} latest · {item.run_count} historical
                    </Badge>
                    <code className="break-all text-[11px] text-surface-300">
                      {item.fingerprint}
                    </code>
                  </div>
                </li>
              ))}
            </ul>
          </details>
        ) : (
          <p className="mt-3 text-xs text-surface-500">
            No ingest run with a semantic-policy fingerprint has been recorded yet.
          </p>
        )}
        {governance.runs_without_fingerprint > 0 && (
          <p className="mt-2 text-xs text-amber-300">
            {governance.latest_applied_runs_without_fingerprint > 0
              ? `${governance.latest_applied_runs_without_fingerprint} latest applied legacy run${
                governance.latest_applied_runs_without_fingerprint === 1 ? '' : 's'
              } lack a fingerprint and require rebuilding.`
              : `${governance.runs_without_fingerprint} superseded legacy run${
                governance.runs_without_fingerprint === 1 ? '' : 's'
              } lack a fingerprint but do not affect the rebuild decision.`}
          </p>
        )}
        {governance.superseded_completed_run_count > 0 && (
          <p className="mt-2 text-xs text-surface-500">
            {governance.superseded_completed_run_count} completed run
            {governance.superseded_completed_run_count === 1 ? ' is' : 's are'} retained only as
            history; rebuild state uses the latest completed run per document.
          </p>
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-lg border border-surface-800 bg-surface-900 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-surface-100">Predicate registry</h3>
            <Badge tone="accent">{registry.counts.canonical} canonical</Badge>
            <Badge tone={registry.counts.provisional > 0 ? 'warn' : 'default'}>
              {registry.counts.provisional} provisional
            </Badge>
          </div>
          <details className="mt-3 text-xs text-surface-500">
            <summary className="cursor-pointer select-none hover:text-surface-300">
              View all {registry.counts.total} registered predicates
            </summary>
            <ul className="mt-2 max-h-[32rem] space-y-2 overflow-auto pr-1">
              {registry.records.map((record) => (
                <PredicateRecord key={record.label} record={record} />
              ))}
            </ul>
          </details>
        </div>

        <div className="rounded-lg border border-surface-800 bg-surface-900 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-surface-100">Identity decisions</h3>
            <Badge tone="violet">{decisions.counts.total} total</Badge>
            <span className="text-xs text-surface-500">
              {decisions.counts.type_correction} corrections · {decisions.counts.distinct} distinct ·{' '}
              {decisions.counts.ambiguous_review} ambiguous
            </span>
          </div>
          {decisions.records.length > 0 ? (
            <details className="mt-3 text-xs text-surface-500">
              <summary className="cursor-pointer select-none hover:text-surface-300">
                View durable identity decisions
              </summary>
              <ul className="mt-2 max-h-[32rem] space-y-2 overflow-auto pr-1">
                {decisions.records.map((decision) => (
                  <IdentityDecision key={decision.decision_id} decision={decision} />
                ))}
              </ul>
            </details>
          ) : (
            <p className="mt-3 text-xs text-surface-500">No durable identity decisions yet.</p>
          )}
        </div>
      </div>
    </section>
  )
}

export function SemanticQualityView() {
  const [report, setReport] = useState<SemanticQualityReport | null>(null)
  const [governance, setGovernance] = useState<SemanticGovernanceResponse | null>(null)
  const [ranAt, setRanAt] = useState<Date | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function runAudit() {
    setRunning(true)
    setError(null)
    setReport(null)
    setGovernance(null)
    setRanAt(null)
    try {
      const [response, governanceResponse] = await Promise.all([
        runSemanticQualityAudit(),
        getSemanticGovernance(),
      ])
      setReport(response.semantic_quality)
      setGovernance(governanceResponse)
      setRanAt(new Date())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Semantic quality audit failed')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="space-y-5">
      <p className="sr-only" role="status" aria-live="polite">
        {report ? `Semantic quality audit complete. Verdict: ${report.verdict.status}.` : ''}
      </p>
      <section className="rounded-lg border border-surface-800 bg-surface-900 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <div className="flex items-center gap-2">
              <ScanSearch size={18} className="text-accent-400" />
              <h2 className="text-base font-semibold text-surface-100">Semantic quality</h2>
            </div>
            <p className="mt-2 text-sm leading-relaxed text-surface-400">
              Run a complete semantic audit of the selected vault. It records a fresh integrity result
              and fences further writes if corruption is found, but does not edit graph rows. This can
              take time and only runs when you request it. Recall remains explicitly not measured unless
              recall-cost samples are supplied by an evaluation run.
            </p>
          </div>
          <button
            onClick={runAudit}
            disabled={running}
            className="flex shrink-0 items-center gap-2 rounded-lg bg-accent-700 px-4 py-2 text-sm font-medium text-white hover:bg-accent-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {report ? <RefreshCw size={15} /> : <ScanSearch size={15} />}
            {running ? 'Running audit…' : report ? 'Run again' : 'Run semantic audit'}
          </button>
        </div>
      </section>

      {running && (
        <div className="rounded-lg border border-surface-800 bg-surface-900 p-5">
          <Spinner label="Scanning the complete graph and verifying semantic evidence…" />
        </div>
      )}
      {error && <ErrorBox message={error} />}
      {governance && <Governance governance={governance} />}
      {report && ranAt && <Report report={report} ranAt={ranAt} />}
      {!running && !error && !report && (
        <div className="rounded-lg border border-dashed border-surface-700 p-8 text-center text-sm text-surface-500">
          No semantic audit has been run in this view.
        </div>
      )}
    </div>
  )
}
