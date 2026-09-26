// Graph-view state slice.
//
// The app's convention is that view-local UI state lives in component useState
// (KGBrowser, QueryView), while cross-cutting state sits in a Zustand store
// (useApp). The Graph view is data-heavy and has several interacting controls
// (filters, the loaded subgraph, expansion set, selection), so it gets its own
// store rather than bloating useApp — same `create<T>` pattern as useApp.
import { create } from 'zustand'
import { PRIMITIVES } from '@/types'
import type { GraphNode, GraphEdge } from '@/types'

export interface GraphFilterState {
  types: string[] // empty = no filter (all types)
  relations: string[] // empty = no filter (all relations)
  limit: number
  minDegree: number
}

const GRAPH_BOUNDS_DEFAULTS_KEY = 'okto-neuron.graph.boundsDefaults.v1'

export const DEFAULT_GRAPH_TYPES = [...PRIMITIVES, 'Claim'] as const
export const PROVENANCE_ANCHOR_TYPES = ['Document', 'Block'] as const

const FACTORY_FILTERS: GraphFilterState = {
  types: [...DEFAULT_GRAPH_TYPES],
  relations: [],
  limit: 1500,
  minDegree: 0,
}

function normalizedBounds(bounds: Partial<Pick<GraphFilterState, 'limit' | 'minDegree'>>) {
  return {
    limit: Math.max(1, Number(bounds.limit ?? FACTORY_FILTERS.limit) || FACTORY_FILTERS.limit),
    minDegree: Math.max(0, Number(bounds.minDegree ?? FACTORY_FILTERS.minDegree) || FACTORY_FILTERS.minDegree),
  }
}

function loadBoundsDefaults() {
  if (typeof window === 'undefined') return normalizedBounds({})
  try {
    const raw = window.localStorage.getItem(GRAPH_BOUNDS_DEFAULTS_KEY)
    return raw ? normalizedBounds(JSON.parse(raw) as Partial<Pick<GraphFilterState, 'limit' | 'minDegree'>>) : normalizedBounds({})
  } catch {
    return normalizedBounds({})
  }
}

export function getGraphFilterDefaults(): GraphFilterState {
  return { ...FACTORY_FILTERS, ...loadBoundsDefaults() }
}

export function saveGraphBoundsDefaults(bounds: Pick<GraphFilterState, 'limit' | 'minDegree'>): GraphFilterState {
  const normalized = normalizedBounds(bounds)
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(GRAPH_BOUNDS_DEFAULTS_KEY, JSON.stringify(normalized))
  }
  return { ...FACTORY_FILTERS, ...normalized }
}

interface GraphState {
  // The displayed subgraph (overview load + merged expansions).
  nodes: Map<string, GraphNode>
  edges: Map<string, GraphEdge> // keyed by `${src}|${type}|${dst}` to dedup
  truncated: boolean
  totalNodes: number
  totalEdges: number

  filters: GraphFilterState
  selectedId: string | null

  // Replace the whole subgraph (overview load or filter refetch — blows away expansions).
  setGraph: (g: {
    nodes: GraphNode[]
    edges: GraphEdge[]
    truncated: boolean
    totalNodes: number
    totalEdges: number
  }) => void
  setFilters: (patch: Partial<GraphFilterState>) => void
  select: (id: string | null) => void
  reset: () => void
}

const edgeKey = (e: GraphEdge): string => `${e.src}|${e.type}|${e.dst}`

export const useGraph = create<GraphState>((set) => ({
  nodes: new Map(),
  edges: new Map(),
  truncated: false,
  totalNodes: 0,
  totalEdges: 0,
  filters: getGraphFilterDefaults(),
  selectedId: null,

  setGraph: (g) =>
    set({
      nodes: new Map(g.nodes.map((n) => [n.id, n])),
      edges: new Map(g.edges.map((e) => [edgeKey(e), e])),
      truncated: g.truncated,
      totalNodes: g.totalNodes,
      totalEdges: g.totalEdges,
    }),

  setFilters: (patch) => set((s) => ({ filters: { ...s.filters, ...patch } })),
  select: (selectedId) => set({ selectedId }),
  reset: () =>
    set({
      nodes: new Map(),
      edges: new Map(),
      truncated: false,
      totalNodes: 0,
      totalEdges: 0,
      filters: getGraphFilterDefaults(),
      selectedId: null,
    }),
}))
