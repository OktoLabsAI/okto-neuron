// Stable color palette for the 11 closed-schema node types.
// 5 primitives get warm/saturated hues; 6 support types get cooler/muted hues,
// so the primitive/support split reads at a glance in the rendered graph.
import { PRIMITIVES, SUPPORT_TYPES } from '@/types'

const TYPE_COLORS: Record<string, string> = {
  // primitives
  Agent: '#f59e0b', // amber
  Activity: '#ef4444', // red
  InformationObject: '#8b5cf6', // violet
  Concept: '#22d3ee', // cyan
  Place: '#10b981', // emerald
  // support
  Document: '#3b82f6', // blue
  Identifier: '#64748b', // slate
  Annotation: '#a78bfa', // light violet
  Claim: '#ec4899', // pink
  Block: '#475569', // dark slate
  Finding: '#eab308', // yellow
}

const FALLBACK = '#94a3b8' // slate-400 — unknown/open types

export function nodeColor(type: string): string {
  return TYPE_COLORS[type] ?? FALLBACK
}

// Ordered legend: primitives first, then support — matches the schema doc order.
export const LEGEND: { type: string; color: string; kind: 'primitive' | 'support' }[] = [
  ...PRIMITIVES.map((t) => ({ type: t, color: nodeColor(t), kind: 'primitive' as const })),
  ...SUPPORT_TYPES.map((t) => ({ type: t, color: nodeColor(t), kind: 'support' as const })),
]

// Degree → node radius. Sub-linear (sqrt) so a few very-high-degree hubs don't
// dwarf everything; clamped to a sane pixel range for sigma.
export function nodeSize(degree: number): number {
  const r = 3 + Math.sqrt(degree) * 1.6
  return Math.min(r, 22)
}
