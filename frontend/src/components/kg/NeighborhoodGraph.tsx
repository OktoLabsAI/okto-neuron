import { useMemo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  MarkerType,
} from '@xyflow/react'
import type { NodeDetail } from '@/types'

// Renders the 1-hop neighborhood of the inspected node from its edge lists.
export function NeighborhoodGraph({
  detail,
  onSelect,
}: {
  detail: NodeDetail
  onSelect: (id: string) => void
}) {
  const { nodes, edges } = useMemo(() => {
    const centerId = detail.node.id
    const ns: Node[] = [
      {
        id: centerId,
        position: { x: 0, y: 0 },
        data: { label: `${detail.node.type}\n${detail.node.name}` },
        style: centerStyle,
        type: 'default',
      },
    ]
    const es: Edge[] = []
    const seen = new Set<string>([centerId])

    const place = (id: string, idx: number, side: 'out' | 'in') => {
      if (seen.has(id)) return
      seen.add(id)
      const dir = side === 'out' ? 1 : -1
      ns.push({
        id,
        position: { x: dir * 260, y: idx * 90 - 120 },
        data: { label: id },
        style: nodeStyle,
        type: 'default',
      })
    }

    detail.edges.out.forEach((e, i) => {
      place(e.dst, i, 'out')
      es.push(edge(`${centerId}->${e.dst}-${i}`, centerId, e.dst, e.type))
    })
    detail.edges.in.forEach((e, i) => {
      place(e.src, i, 'in')
      es.push(edge(`${e.src}->${centerId}-${i}`, e.src, centerId, e.type))
    })
    return { nodes: ns, edges: es }
  }, [detail])

  return (
    <div className="h-72 w-full overflow-hidden rounded-lg border border-surface-800 bg-surface-950">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, n) => onSelect(n.id)}
        nodesDraggable={false}
        nodesConnectable={false}
      >
        <Background color="#1e293b" gap={18} />
        <Controls showInteractive={false} className="bg-surface-800! !text-surface-200" />
      </ReactFlow>
    </div>
  )
}

const baseStyle = {
  fontSize: 11,
  borderRadius: 8,
  padding: 6,
  whiteSpace: 'pre-line' as const,
  textAlign: 'center' as const,
  width: 150,
}
const centerStyle = {
  ...baseStyle,
  background: '#0369a1',
  color: '#e0f2fe',
  border: '1px solid #38bdf8',
}
const nodeStyle = {
  ...baseStyle,
  background: '#1e293b',
  color: '#cbd5e1',
  border: '1px solid #334155',
}

function edge(id: string, source: string, target: string, label: string): Edge {
  return {
    id,
    source,
    target,
    label,
    labelStyle: { fill: '#94a3b8', fontSize: 9 },
    labelBgStyle: { fill: '#0f172a' },
    style: { stroke: '#475569' },
    markerEnd: { type: MarkerType.ArrowClosed, color: '#475569' },
  }
}
