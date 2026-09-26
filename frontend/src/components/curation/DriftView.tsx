// Detect-drift view (ADR 0009 P1) over the existing POST /detect-drift. The
// detector needs an absolute corpus_root — we seed it from /health's vault_path
// (the daemon runs detectors server-side, like ingest-folder reads its own disk).
import { useEffect, useState } from 'react'
import { Play } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import {
  getHealth,
  detectDrift,
  type DriftResponse,
  type DriftFinding,
} from '@/services/curation-api'

const SEV_TONE: Record<string, 'default' | 'warn' | 'danger'> = {
  info: 'default',
  warning: 'warn',
  error: 'danger',
  critical: 'danger',
}

function FindingRow({ f }: { f: DriftFinding }) {
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3">
      <div className="flex items-center gap-2">
        <Badge tone={SEV_TONE[f.severity] ?? 'default'}>{f.severity}</Badge>
        <span className="text-xs font-mono text-surface-500">{f.detector}</span>
      </div>
      <p className="mt-1 text-sm text-surface-200">{f.message}</p>
      {f.subject.doc_path && (
        <p className="mt-1 text-xs font-mono text-surface-500">{f.subject.doc_path}</p>
      )}
    </div>
  )
}

export function DriftView() {
  const [corpusRoot, setCorpusRoot] = useState('')
  const [result, setResult] = useState<DriftResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    getHealth()
      .then((h) => setCorpusRoot((cur) => cur || h.vault_path))
      .catch(() => {})
  }, [])

  async function run() {
    setRunning(true)
    setError(null)
    try {
      setResult(await detectDrift(corpusRoot))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-medium text-surface-200">Detect drift</h2>
        <p className="text-xs text-surface-500">
          Runs all detectors against the vault and lists their Findings. Read-only discovery.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={corpusRoot}
          onChange={(e) => setCorpusRoot(e.target.value)}
          placeholder="/absolute/path/to/vault"
          className="min-w-[20rem] flex-1 rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 font-mono text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-none"
        />
        <button
          onClick={run}
          disabled={running || !corpusRoot}
          className="flex items-center gap-2 rounded-lg bg-accent-600 px-4 py-2 text-sm font-medium text-white hover:bg-accent-500 disabled:opacity-50"
        >
          <Play size={14} />
          Run now
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {running && <Spinner label="Running detectors…" />}

      {result && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-3 text-xs text-surface-400">
            <span>total {result.total}</span>
            {Object.entries(result.counts).map(([name, n]) => (
              <span key={name}>
                {name}: {n}
              </span>
            ))}
            <span className="text-surface-600">ran {result.ran_at}</span>
          </div>
          {result.findings.length === 0 ? (
            <p className="text-sm text-surface-500">No drift findings.</p>
          ) : (
            <div className="space-y-2">
              {result.findings.map((f) => (
                <FindingRow key={f.finding_id} f={f} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
