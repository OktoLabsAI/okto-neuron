import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { getNode } from '@/services/kg-api'
import type { NodeDetail } from '@/types'
import { Spinner, ErrorBox, Badge, ProvenanceChip } from '@/components/ui'
import { NeighborhoodGraph } from './NeighborhoodGraph'

export function NodeDetailPanel({
  nodeId,
  onSelect,
  onClose,
}: {
  nodeId: string
  onSelect: (id: string) => void
  onClose: () => void
}) {
  const [detail, setDetail] = useState<NodeDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    setDetail(null)
    getNode(nodeId)
      .then((d) => active && setDetail(d))
      .catch((e) => active && setError(e instanceof Error ? e.message : 'failed to load node'))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [nodeId])

  return (
    <aside className="flex w-[28rem] shrink-0 flex-col overflow-y-auto border-l border-surface-800 bg-surface-900/60">
      <div className="sticky top-0 flex items-center justify-between border-b border-surface-800 bg-surface-900/90 px-4 py-3 backdrop-blur">
        <h2 className="text-sm font-semibold">Node detail</h2>
        <button onClick={onClose} className="rounded p-1 text-surface-400 hover:bg-surface-800 hover:text-surface-200">
          <X size={16} />
        </button>
      </div>

      <div className="flex flex-col gap-4 p-4">
        {loading && <Spinner label="loading node…" />}
        {error && <ErrorBox message={error} />}

        {detail && (
          <>
            <div>
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <Badge tone="violet">{detail.node.type}</Badge>
                {detail.node.facets?._superseded ? (
                  <Badge tone="warn">
                    superseded
                    {typeof detail.node.facets.valid_until === 'string'
                      ? ` until ${detail.node.facets.valid_until}`
                      : ''}
                  </Badge>
                ) : null}
                {detail.node.facets?._detached ? (
                  <Badge tone="default">
                    detached
                    {typeof detail.node.facets.valid_as_of === 'string'
                      ? ` as of ${detail.node.facets.valid_as_of}`
                      : ''}
                  </Badge>
                ) : null}
              </div>
              <div className="text-sm font-medium text-surface-100">{detail.node.name}</div>
              <code className="mt-1 block font-mono text-[11px] text-surface-500">{detail.node.id}</code>
            </div>

            {detail.canonical_id && (
              <div className="flex flex-col gap-1 rounded-lg border border-accent-600/40 bg-accent-700/15 px-3 py-2 text-xs">
                <span className="flex items-center gap-2 text-accent-300">
                  <Badge tone="accent">variant</Badge>
                  folded onto canonical
                </span>
                <button
                  onClick={() => onSelect(detail.canonical_id as string)}
                  className="self-start truncate font-mono text-[11px] text-accent-400 hover:text-accent-300 hover:underline"
                >
                  {detail.canonical_id}
                </button>
              </div>
            )}

            {detail.provenance && (
              <section>
                <h3 className="mb-2 text-[11px] uppercase tracking-wide text-surface-500">Provenance</h3>
                <div className="flex flex-col gap-2 rounded-lg border border-surface-800 bg-surface-950 p-3">
                  <ProvenanceChip
                    path={detail.provenance.path}
                    byteStart={detail.provenance.byte_start}
                    byteEnd={detail.provenance.byte_end}
                  />
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 font-mono text-[11px] text-surface-400">
                    <dt className="text-surface-500">hash</dt>
                    <dd className="truncate">{detail.provenance.content_hash}</dd>
                    {detail.provenance.document_id && (
                      <>
                        <dt className="text-surface-500">doc</dt>
                        <dd className="truncate">{detail.provenance.document_id}</dd>
                      </>
                    )}
                    {detail.provenance.block_id && (
                      <>
                        <dt className="text-surface-500">block</dt>
                        <dd className="truncate">{detail.provenance.block_id}</dd>
                      </>
                    )}
                    {detail.provenance.extraction_activity_id && (
                      <>
                        <dt className="text-surface-500">activity</dt>
                        <dd className="truncate">{detail.provenance.extraction_activity_id}</dd>
                      </>
                    )}
                    {detail.provenance.agent_id && (
                      <>
                        <dt className="text-surface-500">agent</dt>
                        <dd className="truncate">{detail.provenance.agent_id}</dd>
                      </>
                    )}
                  </dl>
                </div>
              </section>
            )}

            <section>
              <h3 className="mb-2 text-[11px] uppercase tracking-wide text-surface-500">
                Neighborhood ({detail.edges.out.length + detail.edges.in.length} edges)
              </h3>
              <NeighborhoodGraph detail={detail} onSelect={onSelect} />
            </section>

            <section className="flex flex-col gap-3">
              {detail.edges.out.length > 0 && (
                <EdgeList title="Outgoing" items={detail.edges.out.map((e) => ({ type: e.type, id: e.dst }))} onSelect={onSelect} />
              )}
              {detail.edges.in.length > 0 && (
                <EdgeList title="Incoming" items={detail.edges.in.map((e) => ({ type: e.type, id: e.src }))} onSelect={onSelect} />
              )}
            </section>

            {detail.node.facets && Object.keys(detail.node.facets).length > 0 && (
              <section>
                <h3 className="mb-2 text-[11px] uppercase tracking-wide text-surface-500">Facets</h3>
                <pre className="overflow-x-auto rounded-lg border border-surface-800 bg-surface-950 p-3 font-mono text-[11px] text-surface-300">
                  {JSON.stringify(detail.node.facets, null, 2)}
                </pre>
              </section>
            )}
          </>
        )}
      </div>
    </aside>
  )
}

function EdgeList({
  title,
  items,
  onSelect,
}: {
  title: string
  items: { type: string; id: string }[]
  onSelect: (id: string) => void
}) {
  return (
    <div>
      <h3 className="mb-1 text-[11px] uppercase tracking-wide text-surface-500">{title}</h3>
      <ul className="flex flex-col gap-1">
        {items.map((e, i) => (
          <li key={i} className="flex items-center gap-2 text-xs">
            <Badge>{e.type}</Badge>
            <button onClick={() => onSelect(e.id)} className="truncate font-mono text-surface-400 hover:text-accent-400">
              {e.id}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
