import { useEffect, useState } from 'react'
import { Search, ChevronLeft, ChevronRight } from 'lucide-react'
import { listNodeTypes, listNodes } from '@/services/kg-api'
import type { NodeRef, NodeTypeInfo } from '@/types'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { useApp } from '@/store/app'
import { NodeDetailPanel } from './NodeDetailPanel'
import { isMock } from '@/lib/mode'

const PAGE = 50

export function KGBrowser() {
  const { selectedNodeId, selectNode } = useApp()
  const mock = useApp((s) => s.mock) // re-render on toggle

  const [types, setTypes] = useState<NodeTypeInfo[]>([])
  const [activeType, setActiveType] = useState<string | undefined>(undefined)
  const [q, setQ] = useState('')
  const [nodes, setNodes] = useState<NodeRef[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Structural anchors (deterministic has_heading Claims) are real graph nodes but
  // not knowledge — they were ~1/3 of every Claim page. Hidden unless asked for.
  const [includeStructural, setIncludeStructural] = useState(false)

  // load type facets
  useEffect(() => {
    listNodeTypes(includeStructural)
      .then(setTypes)
      .catch(() => setTypes([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mock, includeStructural])

  // load nodes on filter/search/page change
  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    listNodes({ type: activeType, q: q || undefined, limit: PAGE, offset, includeStructural })
      .then((r) => {
        if (!active) return
        setNodes(r.nodes)
        setTotal(r.total)
      })
      .catch((e) => active && setError(e instanceof Error ? e.message : 'failed to list nodes'))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType, q, offset, mock, includeStructural])

  const onFilter = (t: string | undefined) => {
    setActiveType(t)
    setOffset(0)
  }

  return (
    <div className="flex h-full">
      {/* Type sidebar */}
      <div className="flex w-52 shrink-0 flex-col overflow-y-auto border-r border-surface-800 bg-surface-900/40 p-3">
        <div className="mb-2 text-[11px] uppercase tracking-wide text-surface-500">Types</div>
        <button
          onClick={() => onFilter(undefined)}
          className={`mb-1 flex items-center justify-between rounded-md px-3 py-1.5 text-sm ${
            !activeType ? 'bg-accent-700/30 text-accent-300' : 'text-surface-400 hover:bg-surface-800'
          }`}
        >
          All
        </button>
        {(['primitive', 'support'] as const).map((kind) => (
          <div key={kind} className="mt-2">
            <div className="px-3 pb-1 text-[10px] uppercase tracking-wide text-surface-600">{kind}s</div>
            {types
              .filter((t) => t.kind === kind)
              .map((t) => (
                <button
                  key={t.name}
                  onClick={() => onFilter(t.name)}
                  className={`flex w-full items-center justify-between rounded-md px-3 py-1.5 text-sm ${
                    activeType === t.name ? 'bg-accent-700/30 text-accent-300' : 'text-surface-400 hover:bg-surface-800'
                  }`}
                >
                  <span className="truncate">{t.name}</span>
                  <span className="text-[11px] text-surface-600">{t.count}</span>
                </button>
              ))}
          </div>
        ))}
      </div>

      {/* List */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-surface-800 px-6 py-4">
          <div>
            <h1 className="text-base font-semibold">Browse</h1>
            <p className="text-xs text-surface-500">
              {activeType ? <Badge tone="violet">{activeType}</Badge> : 'all types'} · {total} nodes
              {isMock() && <span className="ml-2 text-amber-400/70">(mock)</span>}
            </p>
          </div>
          <div className="ml-auto flex items-center rounded-lg border border-surface-700 bg-surface-900 px-3">
            <Search size={15} className="text-surface-500" />
            <input
              value={q}
              onChange={(e) => {
                setQ(e.target.value)
                setOffset(0)
              }}
              placeholder="filter by name…"
              className="w-56 bg-transparent px-2 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:outline-none"
            />
          </div>
          <label
            className="flex cursor-pointer select-none items-center gap-2 text-xs text-surface-500 hover:text-surface-300"
            title="Deterministic structural anchors (has_heading Claims and friends). Real graph nodes, but section labels rather than knowledge."
          >
            <input
              type="checkbox"
              checked={includeStructural}
              onChange={(e) => {
                setIncludeStructural(e.target.checked)
                setOffset(0)
              }}
              className="accent-accent-500"
            />
            structural anchors
          </label>
        </header>

        <div className="flex-1 overflow-y-auto p-4">
          {loading && <Spinner label="loading nodes…" />}
          {error && <ErrorBox message={error} />}
          {!loading && !error && nodes.length === 0 && (
            <p className="text-sm text-surface-500">No nodes match.</p>
          )}
          <ul className="flex flex-col gap-1">
            {nodes.map((n) => (
              <li key={n.id}>
                <button
                  onClick={() => selectNode(n.id)}
                  className={`flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                    selectedNodeId === n.id
                      ? 'border-accent-600/40 bg-accent-700/20'
                      : 'border-surface-800 bg-surface-900/40 hover:border-surface-700 hover:bg-surface-800/60'
                  }`}
                >
                  <Badge tone="violet">{n.type}</Badge>
                  <span className="truncate text-surface-100">{n.name}</span>
                  {n.variant_count ? (
                    <span title={`${n.variant_count} variant${n.variant_count === 1 ? '' : 's'} folded onto this canonical node`}>
                      <Badge tone="accent">
                        +{n.variant_count} variant{n.variant_count === 1 ? '' : 's'}
                      </Badge>
                    </span>
                  ) : null}
                  <code className="ml-auto shrink-0 font-mono text-[11px] text-surface-600">{n.id}</code>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* Pagination */}
        {total > PAGE && (
          <div className="flex items-center justify-between border-t border-surface-800 px-6 py-3 text-xs text-surface-400">
            <span>
              {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
            </span>
            <div className="flex gap-1">
              <button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE))}
                className="flex items-center gap-1 rounded-md border border-surface-700 px-2 py-1 disabled:opacity-40 hover:bg-surface-800"
              >
                <ChevronLeft size={14} /> Prev
              </button>
              <button
                disabled={offset + PAGE >= total}
                onClick={() => setOffset(offset + PAGE)}
                className="flex items-center gap-1 rounded-md border border-surface-700 px-2 py-1 disabled:opacity-40 hover:bg-surface-800"
              >
                Next <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </div>

      {selectedNodeId && (
        <NodeDetailPanel nodeId={selectedNodeId} onSelect={selectNode} onClose={() => selectNode(null)} />
      )}
    </div>
  )
}
