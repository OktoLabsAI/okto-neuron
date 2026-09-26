import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Graph from 'graphology'
import { Sigma } from 'sigma'
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { inferSettings } from 'graphology-layout-forceatlas2'
import { Search, RotateCcw, Loader2, Filter, MonitorX, X, Route, AlertTriangle } from 'lucide-react'
import { getGraph, getGraphStats, getNeighbors } from '@/services/graph-api'
import { getIngestQueueItem } from '@/services/ingest-api'
import { getLedgerSummary } from '@/services/ledger-api'
import { listNodes } from '@/services/kg-api'
import {
  DEFAULT_GRAPH_TYPES,
  PROVENANCE_ANCHOR_TYPES,
  getGraphFilterDefaults,
  saveGraphBoundsDefaults,
  useGraph,
} from '@/store/graph'
import type { GraphStats, GraphNode, GraphEdge, NodeRef, LedgerSummaryResponse } from '@/types'
import type { IngestEvent } from '@/types'
import { Spinner, ErrorBox, Badge } from '@/components/ui'
import { isMock } from '@/lib/mode'
import { useApp } from '@/store/app'
import { NodeDetailPanel } from './NodeDetailPanel'
import { nodeColor, nodeSize, LEGEND } from './graphTheme'

// Sigma renders via WebGL and throws (e.g. `null.blendFunc`) the moment it can't
// get a context — headless/older browsers, GPU disabled. Probe up front so we
// can render a fallback instead of crashing (and taking the whole SPA down).
function hasWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return Boolean(
      canvas.getContext('webgl2') ||
        canvas.getContext('webgl') ||
        canvas.getContext('experimental-webgl'),
    )
  } catch {
    return false
  }
}

// Deterministic-ish initial placement on a circle. Sigma needs x/y on every
// node before render or it piles everything at the origin; FA2 then spreads them.
function seedCoords(i: number, n: number): { x: number; y: number } {
  const a = (2 * Math.PI * i) / Math.max(n, 1)
  const r = 50 + (i % 7) * 8
  return { x: Math.cos(a) * r, y: Math.sin(a) * r }
}

const graphEdgeKey = (e: GraphEdge): string => `${e.src}|${e.type}|${e.dst}`

const unique = (values: readonly string[]) => Array.from(new Set(values))
const isProvenanceAnchorType = (type: string) =>
  (PROVENANCE_ANCHOR_TYPES as readonly string[]).includes(type)

type ExtractionCounts = { nodes: number; edges: number; claims: number }

function listLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0
}

function countExtractionEvents(events: readonly IngestEvent[]): ExtractionCounts {
  const totals: ExtractionCounts = { nodes: 0, edges: 0, claims: 0 }
  for (const event of events) {
    if (event.kind !== 'extraction_result') continue
    const payload = event.payload ?? {}
    totals.nodes += listLength(payload.nodes)
    totals.claims += listLength(payload.claims)
    const edges = Array.isArray(payload.edges) ? payload.edges : []
    for (const edge of edges) {
      const record = edge && typeof edge === 'object' ? (edge as Record<string, unknown>) : null
      if (record && record.dst_literal != null) totals.claims += 1
      else totals.edges += 1
    }
  }
  return totals
}

function topRecordEntries(record: Record<string, number> | undefined, limit = 6): [string, number][] {
  if (!record) return []
  return Object.entries(record)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit)
}

export function GraphView() {
  const mock = useApp((s) => s.mock)
  const dataVersion = useApp((s) => s.dataVersion)
  const ingestQueue = useApp((s) => s.ingestQueue)
  const setView = useApp((s) => s.setView)

  const {
    nodes,
    edges,
    truncated,
    totalNodes,
    filters,
    selectedId,
    setGraph,
    setFilters,
    select,
    reset,
  } = useGraph()

  const [stats, setStats] = useState<GraphStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [settling, setSettling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<NodeRef[]>([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [focusedSeed, setFocusedSeed] = useState<NodeRef | null>(null)
  const [selectedEdgeKey, setSelectedEdgeKey] = useState<string | null>(null)
  const [boundsSaved, setBoundsSaved] = useState(false)
  const [ledgerSummary, setLedgerSummary] = useState<LedgerSummaryResponse | null>(null)
  const [activeItemEvents, setActiveItemEvents] = useState<IngestEvent[]>([])
  // Evaluated once — WebGL support doesn't change within a page load.
  const [webglOk] = useState(hasWebGL)

  // Imperative sigma/graphology refs — kept out of React state on purpose.
  const containerRef = useRef<HTMLDivElement | null>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)
  const layoutRef = useRef<FA2Layout | null>(null)
  const hoveredRef = useRef<string | null>(null)
  const hoveredEdgeRef = useRef<string | null>(null)
  // True when the next rebuild should re-center the camera.
  const freshLoadRef = useRef(true)
  const lastQueueGraphRefreshKeyRef = useRef<string | null>(null)
  // Read selection inside sigma reducers without re-binding them every render.
  const selectedRef = useRef<string | null>(selectedId)
  selectedRef.current = selectedId
  const selectedEdgeRef = useRef<string | null>(selectedEdgeKey)
  selectedEdgeRef.current = selectedEdgeKey

  // The nodeReducer only runs on a sigma render; a bare click changes selection
  // but doesn't move the camera, so force a refresh to paint the selected style.
  useEffect(() => {
    sigmaRef.current?.refresh()
  }, [selectedId, selectedEdgeKey])

  useEffect(() => {
    if (selectedEdgeKey && !edges.has(selectedEdgeKey)) setSelectedEdgeKey(null)
  }, [edges, selectedEdgeKey])

  const ingestActive = Boolean(ingestQueue?.summary.active)
  const activeItem = ingestQueue?.items.find((item) => item.status === 'processing') ?? null
  const queueGraphRefreshKey = ingestQueue
    ? [
        ingestQueue.summary.total,
        ingestQueue.summary.done,
        ingestQueue.summary.error,
        ingestQueue.summary.cancelled,
      ].join(':')
    : ''

  useEffect(() => {
    if (isMock() || !ingestActive) {
      setLedgerSummary(null)
      return
    }

    let cancelled = false
    let timer: number | undefined
    const tick = () => {
      getLedgerSummary()
        .then((summary) => {
          if (!cancelled) setLedgerSummary(summary)
        })
        .catch(() => {
          if (!cancelled) setLedgerSummary(null)
        })
    }

    tick()
    timer = window.setInterval(tick, 4000)
    return () => {
      cancelled = true
      if (timer) window.clearInterval(timer)
    }
  }, [mock, ingestActive, dataVersion])

  useEffect(() => {
    if (isMock() || !activeItem?.id) {
      setActiveItemEvents([])
      return
    }

    let cancelled = false
    let timer: number | undefined
    const tick = () => {
      getIngestQueueItem(activeItem.id)
        .then((detail) => {
          if (!cancelled) setActiveItemEvents(detail.item.events ?? [])
        })
        .catch(() => {
          if (!cancelled) setActiveItemEvents([])
        })
    }

    tick()
    timer = window.setInterval(tick, 3500)
    return () => {
      cancelled = true
      if (timer) window.clearInterval(timer)
    }
  }, [mock, activeItem?.id, activeItem?.event_count, dataVersion])

  useEffect(() => {
    const q = search.trim()
    if (isMock() || q.length < 2) {
      setSearchResults([])
      setSearchLoading(false)
      return
    }

    let active = true
    const timer = window.setTimeout(() => {
      setSearchLoading(true)
      listNodes({ q, limit: 8, offset: 0 })
        .then((r) => {
          if (active) setSearchResults(r.nodes)
        })
        .catch(() => {
          if (active) setSearchResults([])
        })
        .finally(() => {
          if (active) setSearchLoading(false)
        })
    }, 180)

    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [search, mock, dataVersion])

  useEffect(() => {
    if (!boundsSaved) return
    const timer = window.setTimeout(() => setBoundsSaved(false), 1800)
    return () => window.clearTimeout(timer)
  }, [boundsSaved])

  // ── data fetch ─────────────────────────────────────────────────────────────
  const loadStats = useCallback(() => {
    if (isMock()) {
      setStats(null)
      return
    }
    getGraphStats()
      .then(setStats)
      .catch(() => setStats(null))
  }, [])

  const loadOverview = useCallback(() => {
    if (isMock()) return
    setLoading(true)
    setError(null)
    setFocusedSeed(null)
    setSelectedEdgeKey(null)
    getGraph({
      types: filters.types,
      relations: filters.relations,
      limit: filters.limit,
      min_degree: filters.minDegree,
    })
      .then((r) => {
        freshLoadRef.current = true
        setGraph({
          nodes: r.nodes,
          edges: r.edges,
          truncated: r.truncated,
          totalNodes: r.total_nodes,
          totalEdges: r.total_edges,
        })
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'failed to load graph'))
      .finally(() => setLoading(false))
  }, [filters, setGraph])

  // Stats for the filter rail — refetch only on mock toggle / vault reset, not on filter change.
  useEffect(() => {
    loadStats()
  }, [mock, dataVersion, loadStats])

  // Overview (re)load on filter change / mock toggle / vault reset.
  useEffect(() => {
    loadOverview()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, mock, dataVersion])

  // Bulk ingest commits one file at a time. Refresh the committed graph when a
  // queue item reaches a terminal state, but do not yank the user out of a
  // focused neighborhood.
  useEffect(() => {
    if (isMock() || !ingestQueue) return
    const previous = lastQueueGraphRefreshKeyRef.current
    lastQueueGraphRefreshKeyRef.current = queueGraphRefreshKey
    if (previous === null || previous === queueGraphRefreshKey || focusedSeed) return
    loadStats()
    loadOverview()
  }, [mock, ingestQueue, queueGraphRefreshKey, focusedSeed, loadStats, loadOverview])

  // ── sigma lifecycle ──────────────────────────────────────────────────────────
  // One sigma instance for the life of the mounted view; we rebuild the
  // graphology graph data on every nodes/edges change and re-run layout.
  useEffect(() => {
    // No WebGL → never instantiate sigma (it would throw and unmount the app).
    // The fallback panel renders instead; data still loads into the store.
    if (!webglOk || !containerRef.current) return
    const graph = new Graph({ multi: true })
    graphRef.current = graph
    const renderer = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      renderEdgeLabels: true,
      enableEdgeEvents: true,
      labelRenderedSizeThreshold: 7, // labels appear as nodes grow on zoom-in
      defaultEdgeColor: '#1e293b',
      edgeLabelColor: { color: '#93c5fd' },
      edgeLabelFont: 'ui-sans-serif, system-ui, sans-serif',
      edgeLabelSize: 9,
      labelColor: { color: '#cbd5e1' },
      labelFont: 'ui-sans-serif, system-ui, sans-serif',
      labelSize: 11,
      minCameraRatio: 0.05,
      maxCameraRatio: 12,
    })
    sigmaRef.current = renderer

    // Hover highlight: dim everything not adjacent to the hovered node.
    renderer.on('enterNode', ({ node }) => {
      hoveredRef.current = node
      renderer.refresh()
    })
    renderer.on('leaveNode', () => {
      hoveredRef.current = null
      renderer.refresh()
    })
    renderer.on('enterEdge', ({ edge }) => {
      hoveredEdgeRef.current = edge
      renderer.refresh()
    })
    renderer.on('leaveEdge', () => {
      hoveredEdgeRef.current = null
      renderer.refresh()
    })
    renderer.on('clickNode', ({ node }) => {
      setSelectedEdgeKey(null)
      select(node)
    })
    renderer.on('doubleClickNode', ({ node, event }) => {
      event.preventSigmaDefault()
      void focusNode({ id: node, type: 'Node', name: graph.getNodeAttribute(node, 'label') as string })
    })
    renderer.on('clickEdge', ({ edge, event }) => {
      event.preventSigmaDefault()
      setSelectedEdgeKey(edge)
      select(null)
    })
    renderer.on('clickStage', () => {
      setSelectedEdgeKey(null)
      select(null)
    })

    // Reducers compute per-render appearance from hover/selection — graph data
    // stays clean (idiomatic sigma v3).
    renderer.setSetting('nodeReducer', (node, data) => {
      const hov = hoveredRef.current
      const sel = selectedRef.current
      const res = { ...data }
      if (sel === node) {
        res.highlighted = true
        res.zIndex = 2
        res.size = (data.size as number) + 3
      }
      if (hov && graph.hasNode(hov)) {
        const neighbors = new Set(graph.neighbors(hov))
        neighbors.add(hov)
        if (!neighbors.has(node)) {
          res.color = '#1e293b'
          res.label = ''
        }
      } else if (hov) {
        hoveredRef.current = null
      }
      return res
    })
    renderer.setSetting('edgeReducer', (edge, data) => {
      const hov = hoveredRef.current
      const hoveredEdge = hoveredEdgeRef.current
      const selectedEdge = selectedEdgeRef.current
      const selectedNode = selectedRef.current
      const res = { ...data }
      if (selectedEdge === edge) {
        res.color = '#38bdf8'
        res.forceLabel = true
        res.size = 2.5
        res.zIndex = 3
      } else if (hoveredEdge === edge) {
        res.color = '#93c5fd'
        res.forceLabel = true
        res.size = 2
        res.zIndex = 2
      }
      if (selectedNode && graph.hasNode(selectedNode)) {
        const [s, t] = graph.extremities(edge)
        if (s === selectedNode || t === selectedNode) {
          res.forceLabel = true
          res.color = selectedEdge === edge ? res.color : '#64748b'
          res.size = selectedEdge === edge ? res.size : 1.5
        } else if (selectedEdge !== edge) {
          res.color = '#0f172a'
        }
      }
      if (hov && graph.hasNode(hov)) {
        const [s, t] = graph.extremities(edge)
        if (s === hov || t === hov) {
          res.color = selectedEdge === edge ? res.color : '#64748b'
          res.forceLabel = true
        }
        else res.hidden = true
      } else if (hov) {
        hoveredRef.current = null
      }
      return res
    })

    return () => {
      // Guard every handle — when sigma was never instantiated (no WebGL) this
      // closure isn't created, but stay defensive for the rebuild/HMR paths.
      layoutRef.current?.kill()
      layoutRef.current = null
      sigmaRef.current?.kill()
      sigmaRef.current = null
      graphRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [webglOk])

  // Rebuild graphology data whenever the loaded subgraph changes, then re-run FA2.
  useEffect(() => {
    const graph = graphRef.current
    const renderer = sigmaRef.current
    if (!graph || !renderer) return

    // Stop any running layout before mutating the graph.
    layoutRef.current?.kill()
    layoutRef.current = null

    const nodeArr = Array.from(nodes.values())
    // Preserve coords for nodes already placed (avoids a full re-fling on merge).
    const prevCoords = new Map<string, { x: number; y: number }>()
    graph.forEachNode((id, attr) => {
      prevCoords.set(id, { x: attr.x as number, y: attr.y as number })
    })

    graph.clear()
    hoveredRef.current = null
    hoveredEdgeRef.current = null
    nodeArr.forEach((n, i) => {
      const c = prevCoords.get(n.id) ?? seedCoords(i, nodeArr.length)
      graph.addNode(n.id, {
        x: c.x,
        y: c.y,
        size: nodeSize(n.degree),
        label: n.name || n.id,
        color: nodeColor(n.type),
      })
    })
    for (const e of edges.values()) {
      if (graph.hasNode(e.src) && graph.hasNode(e.dst)) {
        graph.addEdgeWithKey(graphEdgeKey(e), e.src, e.dst, {
          type: 'line',
          label: e.type,
          size: 0.8,
          color: '#1e293b',
        })
      }
    }
    renderer.refresh()

    if (graph.order < 2) {
      setSettling(false)
      return
    }
    // Worker-based FA2 so the main thread never freezes; stop it after settling.
    const layout = new FA2Layout(graph, {
      settings: { ...inferSettings(graph), slowDown: 10 },
    })
    layoutRef.current = layout
    setSettling(true)
    layout.start()
    const recenter = freshLoadRef.current
    const timer = window.setTimeout(() => {
      layout.stop()
      setSettling(false)
      if (recenter) renderer.getCamera().animatedReset({ duration: 300 })
    }, 2500)

    return () => {
      window.clearTimeout(timer)
    }
  }, [nodes, edges])

  // ── interactions ───────────────────────────────────────────────────────────
  const focusNode = useCallback(async (node: NodeRef | GraphNode) => {
    if (isMock()) return
    const f = useGraph.getState().filters
    setLoading(true)
    setError(null)
    setSelectedEdgeKey(null)
    try {
      const r = await getNeighbors(node.id, {
        hops: 1,
        types: f.types,
        relations: f.relations,
        limit: Math.min(Math.max(f.limit, 1), 2000),
      })
      freshLoadRef.current = true
      setGraph({
        nodes: r.nodes,
        edges: r.edges,
        truncated: false,
        totalNodes: r.total_nodes,
        totalEdges: r.total_edges,
      })
      select(node.id)
      setFocusedSeed(r.nodes.find((n) => n.id === node.id) ?? node)
      setSearch('')
      setSearchResults([])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed to focus node')
    } finally {
      setLoading(false)
    }
  }, [select, setGraph])

  const runSearch = async (term: string) => {
    const t = term.trim().toLowerCase()
    if (!t) return
    let hit: GraphNode | undefined
    for (const n of nodes.values()) {
      if ((n.name || '').toLowerCase().includes(t) || n.id.toLowerCase().includes(t)) {
        hit = n
        break
      }
    }
    if (hit) {
      await focusNode(hit)
      return
    }

    const result = searchResults[0]
    if (result) {
      await focusNode(result)
      return
    }

    try {
      const r = await listNodes({ q: term.trim(), limit: 1, offset: 0 })
      if (r.nodes[0]) {
        await focusNode(r.nodes[0])
      } else {
        setError(`no node matches "${term}"`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : `no node matches "${term}"`)
    }
  }

  const clearFocus = () => {
    setSearch('')
    setSearchResults([])
    setFocusedSeed(null)
    setSelectedEdgeKey(null)
    select(null)
    loadOverview()
  }

  const toggleType = (value: string) => {
    const cur =
      filters.types.length === 0
        ? unique([...DEFAULT_GRAPH_TYPES, ...PROVENANCE_ANCHOR_TYPES])
        : filters.types
    const next = cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value]
    setFilters({ types: next.length > 0 ? next : [...DEFAULT_GRAPH_TYPES] })
  }

  const toggleRelation = (value: string) => {
    const cur = filters.relations
    setFilters({ relations: cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value] })
  }

  const provenanceAnchorsShown =
    filters.types.length === 0 || PROVENANCE_ANCHOR_TYPES.every((type) => filters.types.includes(type))

  const setProvenanceAnchorsShown = (shown: boolean) => {
    const cur = filters.types.length === 0 ? [...DEFAULT_GRAPH_TYPES] : filters.types
    setFilters({
      types: shown
        ? unique([...cur, ...PROVENANCE_ANCHOR_TYPES])
        : cur.filter((type) => !isProvenanceAnchorType(type)),
    })
  }

  const rerunLayout = () => {
    // Touch the store identity so the rebuild effect re-runs (re-flings nodes).
    freshLoadRef.current = true // explicit re-layout → recenter is wanted
    const ng = new Map(nodes)
    useGraph.setState({ nodes: ng })
  }

  const saveBoundsDefault = () => {
    saveGraphBoundsDefaults({
      limit: filters.limit,
      minDegree: filters.minDegree,
    })
    setBoundsSaved(true)
  }

  const nodeCount = nodes.size
  const edgeCount = edges.size
  const selectedEdge = selectedEdgeKey ? edges.get(selectedEdgeKey) ?? null : null
  const queueSummary = ingestQueue?.summary ?? null
  const queueProcessed = queueSummary ? queueSummary.done + queueSummary.error + queueSummary.cancelled : 0
  const pendingPreview = ledgerSummary?.pending_commit_preview
  const pendingNodes = pendingPreview?.nodes.accepted_for_write ?? 0
  const pendingNodeQueued = pendingPreview?.nodes.queued_or_abstained ?? 0
  const pendingRelations = pendingPreview?.relations.accepted_for_write ?? 0
  const pendingRelationQueued = pendingPreview?.relations.queued_or_abstained ?? 0
  const pendingNodeTypes = topRecordEntries(pendingPreview?.nodes.types_by_verdict.commit)
  const pendingPredicates = topRecordEntries(pendingPreview?.relations.accepted_predicates)
  const hasActiveLedgerTotals = Boolean(ledgerSummary?.active_candidate_kinds)
  const nodeProgress = hasActiveLedgerTotals ? ledgerSummary?.progress?.node_curator : null
  const relationProgress = hasActiveLedgerTotals ? ledgerSummary?.progress?.relation_curator : null
  const retainedExtractionCounts = useMemo(
    () => countExtractionEvents(activeItemEvents),
    [activeItemEvents],
  )
  const hasCumulativeExtractionFields =
    activeItem?.extracted_nodes != null ||
    activeItem?.extracted_edges != null ||
    activeItem?.extracted_claims != null
  const extractedNodes = Math.max(activeItem?.extracted_nodes ?? 0, retainedExtractionCounts.nodes)
  const extractedEdges = Math.max(activeItem?.extracted_edges ?? 0, retainedExtractionCounts.edges)
  const extractedClaims = Math.max(activeItem?.extracted_claims ?? 0, retainedExtractionCounts.claims)
  const hasExtractionCounts = extractedNodes > 0 || extractedEdges > 0 || extractedClaims > 0
  const committedSemanticNodes = (stats?.node_types ?? [])
    .filter((typeCount) => (DEFAULT_GRAPH_TYPES as readonly string[]).includes(typeCount.type))
    .reduce((sum, typeCount) => sum + typeCount.count, 0)
  const showPendingGraphNotice = ingestActive && Boolean(activeItem || pendingNodes || pendingRelations)

  const legend = useMemo(
    () => LEGEND.filter((l) => Array.from(nodes.values()).some((n) => n.type === l.type)),
    [nodes],
  )

  // ── mock state ───────────────────────────────────────────────────────────────
  if (mock) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center">
        <div className="max-w-md">
          <Filter className="mx-auto mb-3 text-amber-400/70" size={28} />
          <h2 className="text-base font-semibold">Graph view needs the live API</h2>
          <p className="mt-2 text-sm text-surface-500">
            The graph endpoints have no mock fixtures. Turn off{' '}
            <span className="text-amber-400/80">Mock data</span> in the sidebar to explore the live
            knowledge graph.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full">
      {/* Left control rail */}
      <div className="flex w-60 shrink-0 flex-col overflow-y-auto border-r border-surface-800 bg-surface-900/40 p-3 text-sm">
        <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wide text-surface-500">
          <Filter size={12} /> Filters
        </div>

        {/* Search */}
        <div className="mb-3">
          <div className="flex items-center rounded-lg border border-surface-700 bg-surface-900 px-2">
            <Search size={14} className="text-surface-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void runSearch(search)
                if (e.key === 'Escape') {
                  setSearch('')
                  setSearchResults([])
                }
              }}
              placeholder="find node to focus…"
              className="w-full bg-transparent px-2 py-1.5 text-sm text-surface-100 placeholder:text-surface-600 focus:outline-none"
            />
            {search && (
              <button
                type="button"
                onClick={() => {
                  setSearch('')
                  setSearchResults([])
                }}
                className="rounded p-1 text-surface-500 hover:bg-surface-800 hover:text-surface-200"
                title="Clear node search"
              >
                <X size={13} />
              </button>
            )}
          </div>

          {(search.trim().length >= 2 || searchLoading) && (
            <div className="mt-2 max-h-52 overflow-y-auto rounded-lg border border-surface-800 bg-surface-950/80 p-1">
              {searchLoading && (
                <div className="flex items-center gap-2 px-2 py-2 text-xs text-surface-500">
                  <Loader2 size={12} className="animate-spin" /> searching nodes…
                </div>
              )}
              {!searchLoading && searchResults.length === 0 && (
                <div className="px-2 py-2 text-xs text-surface-500">No matching nodes.</div>
              )}
              {!searchLoading &&
                searchResults.map((n) => (
                  <button
                    key={n.id}
                    type="button"
                    onClick={() => void focusNode(n)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-800/70"
                  >
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: nodeColor(n.type) }}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs text-surface-200">{n.name}</span>
                      <span className="block truncate font-mono text-[10px] text-surface-600">{n.id}</span>
                    </span>
                    <Badge tone="default">{n.type}</Badge>
                  </button>
                ))}
            </div>
          )}

          {focusedSeed && (
            <div className="mt-2 rounded-lg border border-accent-700/40 bg-accent-950/30 px-2 py-2 text-xs">
              <div className="mb-1 flex items-center gap-1.5 text-accent-300">
                <Route size={12} />
                focused neighborhood
              </div>
              <div className="truncate text-surface-200" title={focusedSeed.name}>
                {focusedSeed.name}
              </div>
              <button
                type="button"
                onClick={clearFocus}
                className="mt-2 rounded-md border border-accent-800/60 px-2 py-1 text-[11px] text-accent-200 hover:bg-accent-900/60"
              >
                Back to overview
              </button>
            </div>
          )}
        </div>

        {/* Provenance anchor toggle */}
        <label className="mb-3 flex cursor-pointer items-center gap-2 rounded-lg border border-surface-800 bg-surface-950/50 px-2 py-2 hover:bg-surface-800/70">
          <input
            type="checkbox"
            checked={provenanceAnchorsShown}
            onChange={(e) => setProvenanceAnchorsShown(e.target.checked)}
            className="h-3.5 w-3.5 accent-accent-500"
          />
          <span className="min-w-0 flex-1">
            <span className="block text-xs text-surface-200">Provenance anchors</span>
            <span className="block text-[10px] text-surface-600">Document + Block</span>
          </span>
        </label>

        {/* Node-type toggles */}
        <div className="mb-1 text-[10px] uppercase tracking-wide text-surface-600">Node types</div>
        <div className="mb-3 flex flex-col gap-0.5">
          {(stats?.node_types ?? []).map((t) => {
            const on = filters.types.length === 0 || filters.types.includes(t.type)
            return (
              <label
                key={t.type}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-surface-800"
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => toggleType(t.type)}
                  className="h-3.5 w-3.5 accent-accent-500"
                />
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: nodeColor(t.type) }}
                />
                <span className={`flex-1 truncate ${on ? 'text-surface-200' : 'text-surface-500'}`}>
                  {t.type}
                </span>
                <span className="text-[11px] text-surface-600">{t.count}</span>
              </label>
            )
          })}
          {(!stats || stats.node_types.length === 0) && (
            <span className="px-2 text-xs text-surface-600">no node-type stats</span>
          )}
        </div>

        {/* Relation-type toggles */}
        <div className="mb-1 text-[10px] uppercase tracking-wide text-surface-600">Relations</div>
        <div className="mb-3 flex max-h-48 flex-col gap-0.5 overflow-y-auto">
          {(stats?.edge_types ?? []).map((t) => (
            <label
              key={t.type}
              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-surface-800"
            >
              <input
                type="checkbox"
                checked={filters.relations.includes(t.type)}
                onChange={() => toggleRelation(t.type)}
                className="h-3.5 w-3.5 accent-accent-500"
              />
              <span className="flex-1 truncate font-mono text-[11px] text-surface-300">
                {t.type}
              </span>
              <span className="text-[11px] text-surface-600">{t.count}</span>
            </label>
          ))}
          {(!stats || stats.edge_types.length === 0) && (
            <span className="px-2 text-xs text-surface-600">no relation stats</span>
          )}
        </div>

        {/* limit + min_degree */}
        <div className="mb-1 text-[10px] uppercase tracking-wide text-surface-600">Bounds</div>
        <label className="mb-2 flex items-center justify-between gap-2 px-1 text-xs text-surface-400">
          <span>limit</span>
          <input
            type="number"
            min={1}
            max={5000}
            value={filters.limit}
            onChange={(e) => setFilters({ limit: Math.max(1, Number(e.target.value) || 1) })}
            className="w-20 rounded border border-surface-700 bg-surface-900 px-2 py-1 text-right text-surface-100 focus:border-accent-600 focus:outline-none"
          />
        </label>
        <label className="mb-3 flex items-center justify-between gap-2 px-1 text-xs text-surface-400">
          <span>min degree</span>
          <input
            type="number"
            min={0}
            value={filters.minDegree}
            onChange={(e) => setFilters({ minDegree: Math.max(0, Number(e.target.value) || 0) })}
            className="w-20 rounded border border-surface-700 bg-surface-900 px-2 py-1 text-right text-surface-100 focus:border-accent-600 focus:outline-none"
          />
        </label>
        <button
          type="button"
          onClick={saveBoundsDefault}
          className="mb-2 flex items-center justify-center rounded-lg border border-accent-700/50 px-3 py-1.5 text-xs text-accent-300 hover:bg-accent-950/40"
        >
          {boundsSaved ? 'Bounds default saved' : 'Save bounds default'}
        </button>

        <button
          onClick={() => {
            setSearch('')
            setSearchResults([])
            setFocusedSeed(null)
            setSelectedEdgeKey(null)
            setError(null)
            reset()
          }}
          className="mt-1 flex items-center justify-center gap-2 rounded-lg border border-surface-700 px-3 py-2 text-xs text-surface-300 hover:bg-surface-800"
        >
          <RotateCcw size={13} /> Reset filters
        </button>

        {/* Legend */}
        {legend.length > 0 && (
          <div className="mt-4">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-surface-600">Legend</div>
            <div className="flex flex-col gap-0.5">
              {legend.map((l) => (
                <div key={l.type} className="flex items-center gap-2 px-2 py-0.5 text-[11px]">
                  <span
                    className="h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: l.color }}
                  />
                  <span className="text-surface-400">{l.type}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Canvas */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-surface-800 px-6 py-3">
          <div>
            <h1 className="text-base font-semibold">Committed graph</h1>
            <p className="text-xs text-surface-500">
              {focusedSeed ? `Focused on ${focusedSeed.name}` : `${nodeCount} displayed committed nodes · ${edgeCount} displayed committed edges`}
              {focusedSeed && <span className="ml-1 text-surface-600">({nodeCount} nodes · {edgeCount} edges)</span>}
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            {settling && (
              <span className="flex items-center gap-1.5 text-xs text-accent-400">
                <Loader2 size={13} className="animate-spin" /> settling layout…
              </span>
            )}
            <button
              onClick={rerunLayout}
              disabled={nodeCount < 2}
              className="flex items-center gap-1.5 rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-800 disabled:opacity-40"
            >
              <RotateCcw size={13} /> Re-run layout
            </button>
          </div>
        </header>

        {truncated && (
          <div className="border-b border-amber-600/30 bg-amber-950/20 px-6 py-1.5 text-xs text-amber-300">
            Showing top {nodeCount} of {totalNodes} nodes — narrow filters or raise the limit to see
            more.
          </div>
        )}

        {showPendingGraphNotice && (
          <div className="border-b border-amber-700/40 bg-amber-950/20 px-6 py-2.5 text-xs text-amber-100">
            <div className="flex flex-wrap items-start gap-3">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-300" />
              <div className="min-w-[16rem] flex-1">
                <div className="font-medium text-amber-50">Committed graph is behind active ingest</div>
                <div className="mt-0.5 text-amber-100/80">
                  {activeItem ? `${activeItem.name}${activeItem.stage ? ` · ${activeItem.stage}` : ''}` : 'Active ingest'}
                  {queueSummary
                    ? ` · files finished ${queueProcessed}/${queueSummary.total}, queued ${queueSummary.queued}, processing ${queueSummary.processing}`
                    : ''}
                  {` · committed semantic nodes ${committedSemanticNodes}`}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setView('logs')}
                className="rounded-md border border-amber-500/40 px-2 py-1 text-amber-100 hover:bg-amber-900/40"
              >
                Open logs
              </button>
            </div>

            <div className="mt-2 grid gap-2 lg:grid-cols-3">
              <div className="rounded-md border border-amber-700/30 bg-surface-950/40 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wide text-amber-200/60">Pending commit</div>
                {ledgerSummary ? (
                  <>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-amber-50">
                      <span>{pendingNodes} nodes accepted</span>
                      <span>{pendingRelations} relations accepted</span>
                    </div>
                    <div className="mt-1 text-amber-100/60">
                      {pendingNodeQueued} nodes queued · {pendingRelationQueued} relations queued
                    </div>
                  </>
                ) : (
                  <div className="mt-1 text-amber-100/70">Ledger summary loading.</div>
                )}
              </div>

              <div className="rounded-md border border-amber-700/30 bg-surface-950/40 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wide text-amber-200/60">Accepted node types</div>
                {pendingNodeTypes.length > 0 ? (
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {pendingNodeTypes.map(([type, count]) => (
                      <span key={type} className="rounded border border-amber-700/30 px-1.5 py-0.5 text-amber-50">
                        {type} {count}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1 text-amber-100/60">No accepted node types yet.</div>
                )}
              </div>

              <div className="rounded-md border border-amber-700/30 bg-surface-950/40 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wide text-amber-200/60">Accepted predicates</div>
                {pendingPredicates.length > 0 ? (
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {pendingPredicates.map(([predicate, count]) => (
                      <span
                        key={predicate}
                        className="rounded border border-amber-700/30 px-1.5 py-0.5 font-mono text-[11px] text-amber-50"
                      >
                        {predicate} {count}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1 text-amber-100/60">No accepted predicates yet.</div>
                )}
              </div>
            </div>

            {(hasExtractionCounts || nodeProgress || relationProgress) && (
              <div className="mt-2 text-amber-100/70">
                {hasExtractionCounts
                  ? `${hasCumulativeExtractionFields ? 'Extracted candidates so far' : 'Recent extracted candidates in logs'}: ${extractedNodes} node mentions, ${extractedEdges} relation candidates, ${extractedClaims} claim candidates. `
                  : ''}
                {nodeProgress && nodeProgress.total > 0
                  ? `Node review ${nodeProgress.done}/${nodeProgress.total}, ${nodeProgress.remaining} remaining. `
                  : ''}
                {relationProgress && relationProgress.total > 0
                  ? `Relation review ${relationProgress.done}/${relationProgress.total}, ${relationProgress.remaining} remaining.`
                  : ''}
              </div>
            )}
          </div>
        )}

        {webglOk ? (
          <div className="relative min-h-0 flex-1">
            {/* sigma mounts here */}
            <div ref={containerRef} className="absolute inset-0" />

            {/* overlays */}
            {loading && (
              <div className="absolute left-1/2 top-6 -translate-x-1/2">
                <Spinner label="loading graph…" />
              </div>
            )}
            {error && (
              <div className="absolute left-1/2 top-6 w-[28rem] max-w-[80%] -translate-x-1/2">
                <ErrorBox message={error} />
              </div>
            )}
            {!loading && !error && nodeCount === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-center">
                <div className="max-w-sm">
                  <p className="text-sm text-surface-400">No nodes to display.</p>
                  <p className="mt-1 text-xs text-surface-600">
                    The vault may be empty, or the current filters exclude everything. Try{' '}
                    <button
                      onClick={() => setFilters({ ...getGraphFilterDefaults() })}
                      className="text-accent-400 hover:underline"
                    >
                      resetting filters
                    </button>
                    .
                  </p>
                </div>
              </div>
            )}
            <p className="pointer-events-none absolute bottom-2 left-3 text-[10px] text-surface-600">
              scroll to zoom · drag to pan · click node or edge to inspect · double-click node for 1-hop focus
            </p>
          </div>
        ) : (
          // WebGL unavailable → never instantiate sigma. Show a contained fallback;
          // the control rail / stats stay visible so the data is still inspectable.
          <div className="flex min-h-0 flex-1 items-center justify-center p-8 text-center">
            <div className="max-w-md">
              <MonitorX className="mx-auto mb-3 text-amber-400/70" size={28} />
              <h2 className="text-base font-semibold">Graph rendering needs WebGL</h2>
              <p className="mt-2 text-sm text-surface-500">
                This browser doesn't expose a WebGL context, so the interactive graph can't render
                here. The graph holds {nodeCount} nodes and {edgeCount} edges
                {totalNodes > nodeCount ? ` (top of ${totalNodes})` : ''}.
              </p>
              <p className="mt-2 text-xs text-surface-600">
                Try enabling hardware acceleration / WebGL, or use the{' '}
                <button
                  onClick={() => useApp.getState().setView('browser')}
                  className="text-accent-400 hover:underline"
                >
                  Browse
                </button>{' '}
                tab to explore the same data in a list view.
              </p>
            </div>
          </div>
        )}
      </div>

      {selectedEdge ? (
        <EdgeDetailPanel
          edge={selectedEdge}
          source={nodes.get(selectedEdge.src)}
          target={nodes.get(selectedEdge.dst)}
          onClose={() => setSelectedEdgeKey(null)}
          onSelectNode={(id) => {
            setSelectedEdgeKey(null)
            select(id)
          }}
          onFocusNode={(node) => void focusNode(node)}
        />
      ) : selectedId ? (
        <NodeDetailPanel nodeId={selectedId} onSelect={select} onClose={() => select(null)} />
      ) : null}
    </div>
  )
}

function EdgeDetailPanel({
  edge,
  source,
  target,
  onClose,
  onSelectNode,
  onFocusNode,
}: {
  edge: GraphEdge
  source?: GraphNode
  target?: GraphNode
  onClose: () => void
  onSelectNode: (id: string) => void
  onFocusNode: (node: NodeRef) => void
}) {
  const src: NodeRef = source ?? { id: edge.src, type: 'Node', name: edge.src }
  const dst: NodeRef = target ?? { id: edge.dst, type: 'Node', name: edge.dst }

  return (
    <aside className="flex w-[28rem] shrink-0 flex-col overflow-y-auto border-l border-surface-800 bg-surface-900/60">
      <div className="sticky top-0 flex items-center justify-between border-b border-surface-800 bg-surface-900/90 px-4 py-3 backdrop-blur">
        <h2 className="text-sm font-semibold">Edge detail</h2>
        <button onClick={onClose} className="rounded p-1 text-surface-400 hover:bg-surface-800 hover:text-surface-200">
          <X size={16} />
        </button>
      </div>

      <div className="flex flex-col gap-4 p-4">
        <section className="rounded-lg border border-accent-700/40 bg-accent-950/20 p-3">
          <div className="mb-2 text-[11px] uppercase tracking-wide text-surface-500">Relationship</div>
          <Badge tone="accent">{edge.type}</Badge>
          <code className="mt-3 block break-all font-mono text-[11px] text-surface-500">{graphEdgeKey(edge)}</code>
        </section>

        <EndpointCard title="Source" node={src} onSelectNode={onSelectNode} onFocusNode={onFocusNode} />
        <div className="flex justify-center">
          <span className="rounded-full border border-surface-700 bg-surface-950 px-3 py-1 font-mono text-xs text-accent-300">
            {edge.type}
          </span>
        </div>
        <EndpointCard title="Target" node={dst} onSelectNode={onSelectNode} onFocusNode={onFocusNode} />
      </div>
    </aside>
  )
}

function EndpointCard({
  title,
  node,
  onSelectNode,
  onFocusNode,
}: {
  title: string
  node: NodeRef
  onSelectNode: (id: string) => void
  onFocusNode: (node: NodeRef) => void
}) {
  return (
    <section className="rounded-lg border border-surface-800 bg-surface-950 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[11px] uppercase tracking-wide text-surface-500">{title}</h3>
        <Badge tone="violet">{node.type}</Badge>
      </div>
      <div className="text-sm font-medium text-surface-100">{node.name}</div>
      <code className="mt-1 block break-all font-mono text-[11px] text-surface-500">{node.id}</code>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onSelectNode(node.id)}
          className="rounded-md border border-surface-700 px-2 py-1 text-xs text-surface-300 hover:bg-surface-800"
        >
          Inspect node
        </button>
        <button
          type="button"
          onClick={() => onFocusNode(node)}
          className="rounded-md border border-accent-700/50 px-2 py-1 text-xs text-accent-300 hover:bg-accent-950/50"
        >
          Focus from here
        </button>
      </div>
    </section>
  )
}
