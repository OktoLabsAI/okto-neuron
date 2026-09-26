// Authority / Equivalence records (ADR 0009 P2) over GET /api/v1/authority +
// POST /api/v1/authority/unmerge. Lists the off-graph equivalence classes that
// fold variant nodes onto a canonical on read; un-merge is reversible (drops the
// record — the graph was never touched). Jump to Browse opens the canonical node.
import { useEffect, useState } from 'react'
import { RefreshCw, Undo2, ExternalLink } from 'lucide-react'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { useApp } from '@/store/app'
import {
  getAuthority,
  authorityUnmerge,
  type AuthorityRecord,
} from '@/services/curation-api'

export function AuthorityView() {
  const [records, setRecords] = useState<AuthorityRecord[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const setView = useApp((s) => s.setView)
  const selectNode = useApp((s) => s.selectNode)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      setRecords((await getAuthority()).records)
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

  async function unmerge(id: string) {
    setBusy(id)
    setError(null)
    try {
      await authorityUnmerge(id)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  function jumpToBrowse(nodeId: string) {
    selectNode(nodeId)
    setView('browser')
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-medium text-surface-200">Authority &amp; equivalence</h2>
          <p className="text-xs text-surface-500">
            Off-graph equivalence classes. Variants fold onto the canonical on read; un-merge is
            reversible (the graph topology is never changed).
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

      {error && <ErrorBox message={error} />}
      {loading && records.length === 0 && <Spinner label="Loading records…" />}
      {!loading && records.length === 0 && !error && (
        <p className="text-sm text-surface-500">No equivalence records yet. Run reconcile apply.</p>
      )}

      <div className="space-y-2">
        {records.map((r) => (
          <div
            key={r.cluster_id}
            className="rounded-lg border border-surface-800 bg-surface-900 px-4 py-3"
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-accent-300">{r.canonical_name}</span>
              <Badge>conf {r.confidence.toFixed(2)}</Badge>
              <Badge tone="violet">{r.variants.length} variant{r.variants.length === 1 ? '' : 's'}</Badge>
            </div>
            <p className="font-mono text-xs text-surface-600">{r.canonical_id}</p>
            {r.variants.length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-xs text-surface-400">
                {r.variants.map((v, i) => (
                  <li key={i}>{v}</li>
                ))}
              </ul>
            )}
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => jumpToBrowse(r.canonical_id)}
                className="flex items-center gap-1 rounded-md border border-surface-700 px-2.5 py-1 text-xs text-surface-300 hover:bg-surface-800"
              >
                <ExternalLink size={12} /> Browse canonical
              </button>
              <button
                onClick={() => unmerge(r.cluster_id)}
                disabled={busy !== null}
                className="flex items-center gap-1 rounded-md border border-rose-700/50 px-2.5 py-1 text-xs text-rose-300 hover:bg-rose-950/40 disabled:opacity-50"
              >
                <Undo2 size={12} /> {busy === r.cluster_id ? 'Un-merging…' : 'Un-merge'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
