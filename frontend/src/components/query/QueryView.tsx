import { useEffect, useState } from 'react'
import { AlertTriangle, ChevronRight, CircleDot, GitBranch, Info, Loader2, Network, Search, Send, Settings2, Trash2, X } from 'lucide-react'
import { ask, recall } from '@/services/query-api'
import { getGraphStats, getNeighbors } from '@/services/graph-api'
import { isMock } from '@/lib/mode'
import type {
  AskResponse,
  AskRetrievalPolicy,
  AskRetrievalTrace,
  GraphEdge,
  GraphNode,
  GraphTypeCount,
  QueryHit,
  RecallResponse,
  SourceBlockPolicy,
} from '@/types'
import { Spinner, ErrorBox, Badge, ProvenanceChip } from '@/components/ui'
import { useApp } from '@/store/app'

type Mode = 'ask' | 'recall'
type PolicyNumberKey =
  | 'seed_k'
  | 'hops'
  | 'max_degree_per_seed'
  | 'neighbour_budget_tokens'
  | 'coverage_threshold'
  | 'min_claim_confidence'
  | 'max_nodes'
  | 'max_relationships'
  | 'max_claims'
  | 'source_block_budget_tokens'

interface Turn {
  q: string
  mode: Mode
  loading: boolean
  error?: string
  ask?: AskResponse
  recall?: RecallResponse
  related?: Record<string, RelatedState>
}

interface RelatedGraph {
  seed: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  distances: Record<string, number>
  hops: number
}

interface RelatedState {
  loading: boolean
  error?: string
  graph?: RelatedGraph
}

const POLICY_STORAGE_KEY = 'okto-neuron.ask.retrievalPolicy.v1' // gitleaks:allow (storage key name, not a secret)
const LEGACY_THREAD_STORAGE_KEY = 'okto-neuron.query.thread.v1'
const THREAD_STORAGE_PREFIX = 'okto-neuron.query.thread.v2'

const DEFAULT_POLICY: AskRetrievalPolicy = {
  enable_subgraph: false,
  seed_k: 8,
  hops: 1,
  max_degree_per_seed: 8,
  neighbour_budget_tokens: 2000,
  coverage_threshold: 0.4,
  min_claim_confidence: 0,
  max_nodes: 80,
  max_relationships: 80,
  max_claims: 80,
  relationship_types: [],
  source_block_policy: 'on_coverage_miss',
  source_block_budget_tokens: 4000,
}

const RELATED_HIT_LIMIT = 12
const MAX_PERSISTED_TURNS = 40
const MOCK_EDGE_TYPES: GraphTypeCount[] = [
  { type: 'rdf:subject', count: 1597 },
  { type: 'rdf:object', count: 781 },
  { type: 'schema:mentions', count: 312 },
  { type: 'prov:wasDerivedFrom', count: 1597 },
]

function loadPolicy(): AskRetrievalPolicy {
  try {
    const raw = window.localStorage.getItem(POLICY_STORAGE_KEY)
    if (!raw) return DEFAULT_POLICY
    return { ...DEFAULT_POLICY, ...(JSON.parse(raw) as Partial<AskRetrievalPolicy>) }
  } catch {
    return DEFAULT_POLICY
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isMode(value: unknown): value is Mode {
  return value === 'ask' || value === 'recall'
}

function persistableRelated(related?: Record<string, unknown>): Record<string, RelatedState> | undefined {
  if (!related) return undefined
  const entries = Object.entries(related)
    .filter(([, state]) => isObject(state) && state.loading !== true)
    .map(([id, state]) => [id, { ...(state as RelatedState), loading: false }] as const)
  return entries.length ? Object.fromEntries(entries) : undefined
}

function persistableTurns(turns: Turn[]): Turn[] {
  return turns
    .filter((turn) => !turn.loading)
    .slice(-MAX_PERSISTED_TURNS)
    .map((turn) => {
      const related = persistableRelated(turn.related as Record<string, unknown> | undefined)
      return {
        ...turn,
        loading: false,
        ...(related ? { related } : { related: undefined }),
      }
    })
}

function normalizeLoadedTurn(value: unknown): Turn | null {
  if (!isObject(value) || typeof value.q !== 'string' || !isMode(value.mode)) return null
  const turn: Turn = {
    q: value.q,
    mode: value.mode,
    loading: false,
  }
  if (typeof value.error === 'string') turn.error = value.error
  if (isObject(value.ask)) turn.ask = value.ask as unknown as AskResponse
  if (isObject(value.recall)) turn.recall = value.recall as unknown as RecallResponse
  if (isObject(value.related)) {
    const related = persistableRelated(value.related as Record<string, RelatedState>)
    if (related) turn.related = related
  }
  return turn.ask || turn.recall || turn.error ? turn : null
}

function threadStorageKey(vaultPath: string): string {
  return `${THREAD_STORAGE_PREFIX}:${encodeURIComponent(vaultPath)}`
}

function loadTurns(storageKey: string): Turn[] {
  try {
    // v1 mixed answers from every vault into one browser-global thread. Never
    // import that ambiguous data into a selected vault.
    window.localStorage.removeItem(LEGACY_THREAD_STORAGE_KEY)
    const raw = window.localStorage.getItem(storageKey)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.map(normalizeLoadedTurn).filter((turn): turn is Turn => Boolean(turn))
  } catch {
    return []
  }
}

function saveTurns(storageKey: string, turns: Turn[]) {
  const persisted = persistableTurns(turns)
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(persisted))
  } catch {
    const compact = persisted.map(({ related: _related, ...turn }) => turn)
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(compact.slice(-MAX_PERSISTED_TURNS)))
    } catch {
      // localStorage persistence is best-effort.
    }
  }
}

function policySnapshot(policy: AskRetrievalPolicy): AskRetrievalPolicy {
  return {
    ...policy,
    relationship_types: policy.relationship_types ? [...policy.relationship_types] : [],
  }
}

function fmt(n: number | null | undefined) {
  if (n === null || n === undefined) return 'inherit'
  return Number.isInteger(n) ? String(n) : n.toFixed(3).replace(/\.?0+$/, '')
}

function edgeKey(edge: GraphEdge): string {
  return `${edge.src}|${edge.type}|${edge.dst}`
}

function buildHopDistances(seed: string, nodes: GraphNode[], edges: GraphEdge[]): Record<string, number> {
  const adjacency = new Map<string, string[]>()
  for (const node of nodes) adjacency.set(node.id, [])
  for (const edge of edges) {
    adjacency.get(edge.src)?.push(edge.dst)
    adjacency.get(edge.dst)?.push(edge.src)
  }
  const distances: Record<string, number> = { [seed]: 0 }
  const queue = [seed]
  for (let i = 0; i < queue.length; i += 1) {
    const cur = queue[i]
    const nextDistance = distances[cur] + 1
    for (const next of adjacency.get(cur) ?? []) {
      if (distances[next] !== undefined) continue
      distances[next] = nextDistance
      queue.push(next)
    }
  }
  return distances
}

function makeRelatedGraph(resp: { seed: string; nodes: GraphNode[]; edges: GraphEdge[] }, hops: number): RelatedGraph {
  return {
    seed: resp.seed,
    nodes: resp.nodes,
    edges: resp.edges,
    hops,
    distances: buildHopDistances(resp.seed, resp.nodes, resp.edges),
  }
}

function mockRelatedGraph(hit: QueryHit, hops: number): RelatedGraph {
  const seed: GraphNode = { ...hit.node, degree: 2 }
  const evidence: GraphNode = {
    id: `${hit.node.id}:evidence`,
    type: 'Claim',
    name: `evidence for ${hit.node.name}`,
    degree: 1,
  }
  const source: GraphNode = {
    id: `${hit.node.id}:source`,
    type: 'Document',
    name: hit.provenance?.path ?? 'source note',
    degree: 1,
  }
  const nodes = hops > 1 ? [seed, evidence, source] : [seed, evidence]
  const edges: GraphEdge[] = [
    { src: seed.id, dst: evidence.id, type: 'related_to' },
    ...(hops > 1 ? [{ src: evidence.id, dst: source.id, type: 'grounded_in' }] : []),
  ]
  return makeRelatedGraph({ seed: seed.id, nodes, edges }, hops)
}

function nodeTone(type: string): 'default' | 'accent' | 'violet' | 'warn' {
  if (type === 'Claim') return 'accent'
  if (type === 'Document' || type === 'InformationObject') return 'warn'
  if (type === 'Agent' || type === 'Concept') return 'violet'
  return 'default'
}

function relatedOptionsFromPolicy(policy: AskRetrievalPolicy): NonNullable<Parameters<typeof getNeighbors>[1]> {
  const hops = Math.max(1, Math.min(5, Math.round(Number(policy.hops ?? DEFAULT_POLICY.hops ?? 1))))
  const limit = Math.max(1, Math.min(2000, Math.round(Number(policy.max_nodes ?? DEFAULT_POLICY.max_nodes ?? 80))))
  const relations = policy.relationship_types?.length ? [...policy.relationship_types] : undefined
  return { hops, limit, relations }
}

function seedRelated(hits: QueryHit[]): Record<string, RelatedState> {
  return Object.fromEntries(hits.slice(0, RELATED_HIT_LIMIT).map((hit) => [hit.node.id, { loading: true }]))
}

function FieldHelp({ text }: { text: string }) {
  return (
    <span
      className="group/help relative inline-flex h-4 w-4 items-center justify-center rounded-full text-surface-500 hover:text-accent-300 focus:text-accent-300 focus:outline-hidden"
      tabIndex={0}
      aria-label={text}
      title={text}
    >
      <Info size={12} />
      <span className="pointer-events-none absolute left-0 top-full z-30 mt-2 hidden w-64 rounded-md border border-surface-700 bg-surface-950 px-3 py-2 text-left text-[11px] leading-relaxed text-surface-300 shadow-xl group-hover/help:block group-focus/help:block">
        {text}
      </span>
    </span>
  )
}

function LabelWithHelp({ label, help }: { label: string; help?: string }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className="truncate">{label}</span>
      {help && <FieldHelp text={help} />}
    </span>
  )
}

function ControlField({
  label,
  help,
  children,
}: {
  label: string
  help?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 text-xs text-surface-400">
      <LabelWithHelp label={label} help={help} />
      {children}
    </div>
  )
}

function RelationshipTypePicker({
  selected,
  options,
  loading,
  error,
  onChange,
}: {
  selected: string[]
  options: GraphTypeCount[]
  loading: boolean
  error?: string
  onChange: (values: string[]) => void
}) {
  const [query, setQuery] = useState('')
  const selectedSet = new Set(selected)
  const sorted = [...options].sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
  const q = query.trim().toLowerCase()
  const filtered = q ? sorted.filter((option) => option.type.toLowerCase().includes(q)) : sorted

  const toggle = (type: string) => {
    onChange(selectedSet.has(type) ? selected.filter((x) => x !== type) : [...selected, type])
  }
  const remove = (type: string) => onChange(selected.filter((x) => x !== type))
  const selectFirstMatch = () => {
    const first = filtered[0]
    if (!first) return
    if (!selectedSet.has(first.type)) onChange([...selected, first.type])
    setQuery('')
  }

  return (
    <div className="overflow-hidden rounded-lg border border-surface-700 bg-surface-900">
      <div className="flex h-10 items-center gap-2 border-b border-surface-800 px-2">
        <Search size={13} className="shrink-0 text-surface-500" />
        <input
          value={query}
          placeholder={loading ? 'loading edge types...' : 'search available edge types'}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              selectFirstMatch()
            }
            if (e.key === 'Escape') setQuery('')
          }}
          className="min-w-0 flex-1 bg-transparent text-xs text-surface-100 placeholder:text-surface-600 focus:outline-hidden"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery('')}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-surface-500 hover:bg-surface-800 hover:text-surface-200"
            title="Clear search"
          >
            <X size={13} />
          </button>
        )}
      </div>

      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1 border-b border-surface-800 px-2 py-2">
          {selected.map((type) => (
            <span
              key={type}
              className="inline-flex max-w-full items-center gap-1 rounded-md border border-accent-700/50 bg-accent-950/40 px-2 py-1 text-[11px] text-accent-200"
            >
              <span className="truncate">{type}</span>
              <button
                type="button"
                onClick={() => remove(type)}
                className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-sm text-accent-300 hover:bg-accent-800/50 hover:text-white"
                title={`Remove ${type}`}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="max-h-44 overflow-y-auto p-1">
        {loading && <div className="px-2 py-2 text-xs text-surface-500">Loading relationship types...</div>}
        {error && <div className="px-2 py-2 text-xs text-rose-300">{error}</div>}
        {!loading && !error && filtered.length === 0 && (
          <div className="px-2 py-2 text-xs text-surface-500">No matching relationship types.</div>
        )}
        {!loading &&
          !error &&
          filtered.map((option) => (
            <label
              key={option.type}
              className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs text-surface-300 hover:bg-surface-800/70"
            >
              <input
                type="checkbox"
                checked={selectedSet.has(option.type)}
                onChange={() => toggle(option.type)}
                className="h-3.5 w-3.5 shrink-0 accent-accent-500"
              />
              <span className="min-w-0 flex-1 truncate">{option.type}</span>
              <span className="shrink-0 tabular-nums text-surface-500">{option.count}</span>
            </label>
          ))}
      </div>
    </div>
  )
}

function NumberControl({
  policy,
  keyName,
  label,
  min,
  max,
  step,
  help,
  onChange,
}: {
  policy: AskRetrievalPolicy
  keyName: PolicyNumberKey
  label: string
  min: number
  max: number
  step?: number
  help?: string
  onChange: (patch: Partial<AskRetrievalPolicy>) => void
}) {
  const value = Number(policy[keyName] ?? DEFAULT_POLICY[keyName] ?? min)
  return (
    <ControlField label={`${label}: ${fmt(value)}`} help={help}>
      <div className="grid grid-cols-[1fr_74px] gap-2">
        <input
          type="range"
          min={min}
          max={max}
          step={step ?? 1}
          value={value}
          onChange={(e) => onChange({ [keyName]: Number(e.target.value) })}
          className="min-w-0 accent-accent-500"
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step ?? 1}
          value={value}
          onChange={(e) => onChange({ [keyName]: Number(e.target.value) })}
          className="h-8 rounded-lg border border-surface-700 bg-surface-900 px-2 text-xs text-surface-100 focus:border-accent-600 focus:outline-hidden"
        />
      </div>
    </ControlField>
  )
}

function RetrievalControls({
  policy,
  edgeTypeOptions,
  edgeTypesLoading,
  edgeTypesError,
  onChange,
}: {
  policy: AskRetrievalPolicy
  edgeTypeOptions: GraphTypeCount[]
  edgeTypesLoading: boolean
  edgeTypesError?: string
  onChange: (patch: Partial<AskRetrievalPolicy>) => void
}) {
  return (
    <div className="mb-3 grid max-h-[38vh] gap-3 overflow-y-auto rounded-lg border border-surface-800 bg-surface-950/60 p-3 md:grid-cols-2">
      <div className="flex items-center justify-between gap-3 rounded-lg border border-surface-800 bg-surface-900/40 px-3 py-2">
        <span className="text-xs font-medium text-surface-300">
          <LabelWithHelp
            label="Subgraph-first"
            help="Use graph neighbors and Claims as the first answer context instead of starting from raw source blocks."
          />
        </span>
        <input
          type="checkbox"
          checked={Boolean(policy.enable_subgraph)}
          onChange={(e) => onChange({ enable_subgraph: e.target.checked })}
          className="h-4 w-4 accent-accent-500"
        />
      </div>
      <ControlField
        label="Source blocks"
        help="Controls when raw source text is added to the answer context: never, only when graph coverage is thin, or always."
      >
        <select
          value={policy.source_block_policy ?? 'on_coverage_miss'}
          onChange={(e) => onChange({ source_block_policy: e.target.value as SourceBlockPolicy })}
          className="h-9 rounded-lg border border-surface-700 bg-surface-900 px-2 text-xs text-surface-100 focus:border-accent-600 focus:outline-hidden"
        >
          <option value="never">never</option>
          <option value="on_coverage_miss">on coverage miss</option>
          <option value="always">always</option>
        </select>
      </ControlField>
      <NumberControl
        policy={policy}
        keyName="seed_k"
        label="Seed k"
        min={1}
        max={100}
        help="How many top search hits seed the answer. Higher values improve recall but add noise and cost."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="hops"
        label="Graph hops"
        min={1}
        max={5}
        help="How far to walk from each seed through graph edges. One hop is tight; more hops find indirect context."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="max_degree_per_seed"
        label="Degree cap"
        min={1}
        max={50}
        help="Limits how many edges a single seed can expand through so hub nodes do not flood the answer."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="neighbour_budget_tokens"
        label="Subgraph tokens"
        min={200}
        max={20000}
        step={100}
        help="Token budget for rendered graph context: nodes, relationships, and Claims before source text is added."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="source_block_budget_tokens"
        label="Source tokens"
        min={200}
        max={50000}
        step={100}
        help="Token budget for raw source blocks when the selected source-block policy includes them."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="coverage_threshold"
        label="Coverage"
        min={0}
        max={1}
        step={0.05}
        help="Minimum graph-context density before source-block fallback is considered unnecessary."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="min_claim_confidence"
        label="Min confidence"
        min={0}
        max={1}
        step={0.05}
        help="Drops Claims below this extraction confidence from the rendered subgraph context."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="max_nodes"
        label="Max nodes"
        min={1}
        max={500}
        help="Maximum node rows included in the rendered subgraph and related-hop previews."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="max_relationships"
        label="Max relations"
        min={0}
        max={500}
        help="Maximum relationship rows included in the rendered subgraph answer context."
        onChange={onChange}
      />
      <NumberControl
        policy={policy}
        keyName="max_claims"
        label="Max claims"
        min={0}
        max={500}
        help="Maximum Claim rows included in the rendered subgraph answer context."
        onChange={onChange}
      />
      <ControlField
        label="Relationship types"
        help="Optional edge allowlist. Search the live edge types and check the relationships this request may traverse."
      >
        <RelationshipTypePicker
          selected={policy.relationship_types ?? []}
          options={edgeTypeOptions}
          loading={edgeTypesLoading}
          error={edgeTypesError}
          onChange={(relationshipTypes) => onChange({ relationship_types: relationshipTypes })}
        />
      </ControlField>
      <div className="flex items-end">
        <button
          type="button"
          onClick={() => onChange(DEFAULT_POLICY)}
          title="Restore the conservative default retrieval policy."
          className="h-9 rounded-lg border border-surface-700 px-3 text-xs text-surface-300 hover:bg-surface-800"
        >
          Reset
        </button>
      </div>
    </div>
  )
}

function RetrievalTrace({ trace }: { trace?: AskRetrievalTrace }) {
  if (!trace) return null
  return (
    <div className="flex flex-wrap gap-2 text-xs">
      {trace.path && <Badge tone="default">{trace.path}</Badge>}
      {trace.seed_k !== undefined && <Badge tone="accent">k {trace.seed_k}</Badge>}
      {trace.hops !== undefined && <Badge tone="violet">{trace.hops} hop</Badge>}
      <Badge tone={trace.source_blocks_used ? 'warn' : 'default'}>
        blocks {trace.source_blocks_used ? 'used' : 'off'}
      </Badge>
      {trace.context_tokens_estimate !== undefined && (
        <Badge tone="default">~{trace.context_tokens_estimate} tok</Badge>
      )}
    </div>
  )
}

const DEGRADED_LABELS: Record<string, string> = {
  no_llm: 'No answer was generated: no LLM is configured for this vault.',
  provider_error: 'No answer was generated: the LLM provider failed.',
  truncated: 'The answer was cut off: the model hit its token limit.',
  abnormal_stop: 'The model stopped early, so the answer may be incomplete.',
  empty: 'The model returned an empty answer.',
}

// The server marks any answer that is not a clean synthesis as "degraded"
// (retrieval.synthesis_status says which). Never show it as a normal answer.
function DegradedNotice({ answer }: { answer: AskResponse }) {
  if (answer.status !== 'degraded') return null
  const trace = answer.retrieval ?? {}
  const status = trace.synthesis_status ?? 'unknown'
  const reason = trace.no_llm_reason || trace.provider_error || trace.finish_reason || ''
  return (
    <div
      role="alert"
      data-testid="ask-degraded"
      className="flex gap-2 rounded-lg border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200"
    >
      <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-400" />
      <div className="flex flex-col gap-1">
        <span className="font-medium">
          Degraded answer ({status}). {DEGRADED_LABELS[status] ?? 'The answer did not complete normally.'}
        </span>
        {reason && <span className="text-amber-300/80">{reason}</span>}
        {answer.hits.length > 0 && (
          <span className="text-amber-300/80">The sources below are the retrieval hits only.</span>
        )}
      </div>
    </div>
  )
}

function RelationshipPreview({
  related,
  onSelect,
}: {
  related?: RelatedState
  onSelect: (id: string) => void
}) {
  if (!related) return null
  if (related.loading) {
    return (
      <div className="mt-3 border-t border-surface-800 pt-3 text-xs text-surface-500">
        <span className="inline-flex items-center gap-2">
          <Loader2 size={13} className="animate-spin text-accent-400" />
          loading graph hops
        </span>
      </div>
    )
  }
  if (related.error) {
    return (
      <div className="mt-3 border-t border-surface-800 pt-3 text-xs text-rose-300">
        relation preview failed: {related.error}
      </div>
    )
  }
  if (!related.graph) return null

  const graph = related.graph
  const byId = new Map(graph.nodes.map((node) => [node.id, node]))
  const relatedNodes = graph.nodes.filter((node) => node.id !== graph.seed)
  const direct = relatedNodes.filter((node) => graph.distances[node.id] === 1)
  const deeper = relatedNodes.filter((node) => (graph.distances[node.id] ?? 0) > 1)
  const seedEdges = graph.edges
    .filter((edge) => edge.src === graph.seed || edge.dst === graph.seed)
    .slice(0, 4)
  const deeperEdges = graph.edges
    .filter((edge) => edge.src !== graph.seed && edge.dst !== graph.seed)
    .slice(0, 3)

  return (
    <div className="mt-3 border-t border-surface-800 pt-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-2 text-[11px] uppercase tracking-wide text-surface-500">
          <Network size={13} className="text-accent-400" />
          Related graph
        </span>
        <div className="flex gap-1">
          <Badge tone="default">{graph.nodes.length} nodes</Badge>
          <Badge tone="default">{graph.edges.length} edges</Badge>
          <Badge tone="violet">{graph.hops} hop{graph.hops === 1 ? '' : 's'}</Badge>
        </div>
      </div>

      {relatedNodes.length === 0 ? (
        <p className="text-xs text-surface-500">No related nodes returned for this hit.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 text-xs text-surface-500">
              <CircleDot size={12} className="text-accent-400" />
              1-hop
            </span>
            {direct.length === 0 && <span className="text-xs text-surface-600">none</span>}
            {direct.slice(0, 6).map((node) => (
              <button
                key={node.id}
                type="button"
                onClick={() => onSelect(node.id)}
                className="inline-flex max-w-[16rem] items-center gap-1 truncate rounded-md border border-surface-700 bg-surface-900 px-2 py-1 text-xs text-surface-200 hover:border-accent-600/60 hover:text-accent-300"
                title={node.name}
              >
                <Badge tone={nodeTone(node.type)}>{node.type}</Badge>
                <span className="truncate">{node.name}</span>
              </button>
            ))}
            {direct.length > 6 && <span className="text-xs text-surface-500">+{direct.length - 6}</span>}
          </div>

          {graph.hops > 1 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1 text-xs text-surface-500">
                <GitBranch size={12} className="text-violet-400" />
                deeper
              </span>
              {deeper.length === 0 && <span className="text-xs text-surface-600">none</span>}
              {deeper.slice(0, 6).map((node) => (
                <button
                  key={node.id}
                  type="button"
                  onClick={() => onSelect(node.id)}
                  className="inline-flex max-w-[16rem] items-center gap-1 truncate rounded-md border border-surface-700 bg-surface-900 px-2 py-1 text-xs text-surface-200 hover:border-violet-500/60 hover:text-violet-300"
                  title={node.name}
                >
                  <span className="text-surface-500">h{graph.distances[node.id]}</span>
                  <Badge tone={nodeTone(node.type)}>{node.type}</Badge>
                  <span className="truncate">{node.name}</span>
                </button>
              ))}
              {deeper.length > 6 && <span className="text-xs text-surface-500">+{deeper.length - 6}</span>}
            </div>
          )}

          {(seedEdges.length > 0 || deeperEdges.length > 0) && (
            <div className="flex flex-col gap-1 text-xs text-surface-400">
              {[...seedEdges, ...deeperEdges].slice(0, 7).map((edge) => {
                const src = byId.get(edge.src)
                const dst = byId.get(edge.dst)
                return (
                  <button
                    key={edgeKey(edge)}
                    type="button"
                    onClick={() => onSelect(edge.dst === graph.seed ? edge.src : edge.dst)}
                    className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 rounded-md px-1 py-1 text-left hover:bg-surface-800/50"
                  >
                    <span className="truncate text-surface-300">{src?.name ?? edge.src}</span>
                    <span className="inline-flex items-center gap-1 text-accent-300">
                      <ChevronRight size={12} />
                      {edge.type}
                      <ChevronRight size={12} />
                    </span>
                    <span className="truncate text-surface-300">{dst?.name ?? edge.dst}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function HitCard({ hit, related }: { hit: QueryHit; related?: RelatedState }) {
  const selectNode = useApp((s) => s.selectNode)
  const setView = useApp((s) => s.setView)
  const open = (id = hit.node.id) => {
    selectNode(id)
    setView('browser')
  }
  return (
    <div className="rounded-lg border border-surface-800 bg-surface-900/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <button onClick={() => open()} className="text-left text-sm font-medium text-surface-100 hover:text-accent-400">
          {hit.node.name}
        </button>
        <Badge tone="accent">{hit.score.toFixed(2)}</Badge>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <Badge tone="violet">{hit.node.type}</Badge>
        <code className="font-mono text-[11px] text-surface-500">{hit.node.id}</code>
        {hit.node.superseded && (
          <Badge tone="warn">
            superseded{hit.node.valid_until ? ` until ${hit.node.valid_until}` : ''}
          </Badge>
        )}
        {hit.node.detached && (
          <Badge tone="default">
            detached{hit.node.valid_as_of ? ` as of ${hit.node.valid_as_of}` : ''}
          </Badge>
        )}
      </div>
      {hit.provenance && (
        <div className="mt-2">
          <ProvenanceChip
            path={hit.provenance.path}
            byteStart={hit.provenance.byte_start}
            byteEnd={hit.provenance.byte_end}
          />
        </div>
      )}
      <RelationshipPreview related={related} onSelect={open} />
    </div>
  )
}

export function QueryView() {
  const selectedVaultPath = useApp((state) => state.selectedVaultPath)
  const storageKey = threadStorageKey(selectedVaultPath ?? 'no-vault')
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<Mode>('ask')
  const [turns, setTurns] = useState<Turn[]>(() => loadTurns(storageKey))
  const [controlsOpen, setControlsOpen] = useState(true)
  const [policy, setPolicy] = useState<AskRetrievalPolicy>(loadPolicy)
  const [edgeTypeOptions, setEdgeTypeOptions] = useState<GraphTypeCount[]>([])
  const [edgeTypesLoading, setEdgeTypesLoading] = useState(true)
  const [edgeTypesError, setEdgeTypesError] = useState<string | undefined>()

  useEffect(() => {
    try {
      window.localStorage.setItem(POLICY_STORAGE_KEY, JSON.stringify(policy))
    } catch {
      // localStorage persistence is best-effort.
    }
  }, [policy])

  useEffect(() => {
    saveTurns(storageKey, turns)
  }, [storageKey, turns])

  useEffect(() => {
    let cancelled = false
    const loadEdgeTypes = async () => {
      if (isMock()) {
        setEdgeTypeOptions(MOCK_EDGE_TYPES)
        setEdgeTypesLoading(false)
        return
      }
      try {
        const stats = await getGraphStats()
        if (cancelled) return
        setEdgeTypeOptions(stats.edge_types ?? [])
        setEdgeTypesError(undefined)
      } catch (e) {
        if (cancelled) return
        setEdgeTypesError(e instanceof Error ? e.message : 'failed to load relationship types')
      } finally {
        if (!cancelled) setEdgeTypesLoading(false)
      }
    }

    void loadEdgeTypes()
    return () => {
      cancelled = true
    }
  }, [])

  const patchPolicy = (patch: Partial<AskRetrievalPolicy>) => {
    setPolicy((p) => ({ ...p, ...patch }))
  }

  const clearThread = () => {
    setTurns([])
    try {
      window.localStorage.removeItem(storageKey)
    } catch {
      // localStorage persistence is best-effort.
    }
  }

  const loadRelated = async (
    turnIdx: number,
    hits: QueryHit[],
    opts: NonNullable<Parameters<typeof getNeighbors>[1]>,
  ) => {
    const visibleHits = hits.slice(0, RELATED_HIT_LIMIT)
    if (visibleHits.length === 0) return

    const entries = await Promise.all(
      visibleHits.map(async (hit) => {
        try {
          const graph = isMock()
            ? mockRelatedGraph(hit, opts.hops ?? 1)
            : makeRelatedGraph(await getNeighbors(hit.node.id, opts), opts.hops ?? 1)
          return [hit.node.id, { loading: false, graph }] as const
        } catch (e) {
          const error = e instanceof Error ? e.message : 'failed to load related graph'
          return [hit.node.id, { loading: false, error }] as const
        }
      }),
    )

    setTurns((t) =>
      t.map((x, i) =>
        i === turnIdx ? { ...x, related: { ...(x.related ?? {}), ...Object.fromEntries(entries) } } : x,
      ),
    )
  }

  const submit = async () => {
    const q = input.trim()
    if (!q) return
    setInput('')
    const idx = turns.length
    const policyForRequest = policySnapshot(policy)
    const graphOpts = relatedOptionsFromPolicy(policyForRequest)
    setTurns((t) => [...t, { q, mode, loading: true }])
    try {
      if (mode === 'ask') {
        const res = await ask(q, policyForRequest)
        setTurns((t) =>
          t.map((x, i) => (i === idx ? { ...x, loading: false, ask: res, related: seedRelated(res.hits) } : x)),
        )
        void loadRelated(idx, res.hits, graphOpts)
      } else {
        const res = await recall(q, policyForRequest.seed_k ?? 10)
        setTurns((t) =>
          t.map((x, i) => (i === idx ? { ...x, loading: false, recall: res, related: seedRelated(res.hits) } : x)),
        )
        void loadRelated(idx, res.hits, graphOpts)
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'request failed'
      setTurns((t) => t.map((x, i) => (i === idx ? { ...x, loading: false, error: msg } : x)))
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-surface-800 px-6 py-4">
        <h1 className="text-base font-semibold">Query</h1>
        <p className="text-xs text-surface-500">
          Ask a question or recall claims — answers come back with byte-anchored source provenance, the same
          way Claude queries the vault.
        </p>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-5">
        {turns.length === 0 && (
          <div className="mx-auto mt-16 max-w-md text-center text-surface-500">
            <p className="text-sm">Ask the knowledge graph anything.</p>
            <p className="mt-1 text-xs">
              <span className="text-surface-400">ask</span> synthesises an answer + citations;{' '}
              <span className="text-surface-400">recall</span> returns matching claims with scores.
            </p>
          </div>
        )}
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          {turns.map((t, i) => (
            <div key={i} className="flex flex-col gap-3">
              <div className="flex justify-end">
                <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-accent-700/30 px-4 py-2 text-sm ring-1 ring-accent-600/30">
                  <span className="mr-2 text-[10px] uppercase tracking-wide text-accent-400/70">{t.mode}</span>
                  {t.q}
                </div>
              </div>

              <div className="rounded-2xl rounded-bl-sm border border-surface-800 bg-surface-900/40 px-4 py-3">
                {t.loading && <Spinner label="thinking…" />}
                {t.error && <ErrorBox message={t.error} />}

                {t.ask && (
                  <div className="flex flex-col gap-3">
                    <RetrievalTrace trace={t.ask.retrieval} />
                    <DegradedNotice answer={t.ask} />
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-surface-100">{t.ask.text}</p>
                    {t.ask.hits.length > 0 && (
                      <div className="flex flex-col gap-2">
                        <div className="text-[11px] uppercase tracking-wide text-surface-500">Sources</div>
                        {t.ask.hits.map((h, j) => (
                          <HitCard key={j} hit={h} related={t.related?.[h.node.id]} />
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {t.recall && (
                  <div className="flex flex-col gap-2">
                    {t.recall.hits.length === 0 && (
                      <p className="text-sm text-surface-500">No matching claims.</p>
                    )}
                    {t.recall.hits.map((h, j) => (
                      <HitCard key={j} hit={h} related={t.related?.[h.node.id]} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Composer */}
      <div className="border-t border-surface-800 px-6 py-4">
        <div className="mx-auto max-w-3xl">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setControlsOpen((v) => !v)}
                className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-300 hover:bg-surface-800"
              >
                <Settings2 size={13} />
                Retrieval
              </button>
              {turns.length > 0 && (
                <button
                  type="button"
                  onClick={clearThread}
                  className="flex items-center gap-2 rounded-lg border border-surface-700 px-3 py-1.5 text-xs text-surface-400 hover:bg-surface-800 hover:text-surface-200"
                  title="Clear saved query thread"
                >
                  <Trash2 size={13} />
                  Clear thread
                </button>
              )}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <Badge tone={policy.enable_subgraph ? 'accent' : 'default'}>
                {policy.enable_subgraph ? 'subgraph' : 'block'}
              </Badge>
              <Badge tone="default">k {policy.seed_k}</Badge>
              <Badge tone={policy.source_block_policy === 'never' ? 'default' : 'warn'}>
                blocks {policy.source_block_policy}
              </Badge>
            </div>
          </div>
          {controlsOpen && (
            <RetrievalControls
              policy={policy}
              edgeTypeOptions={edgeTypeOptions}
              edgeTypesLoading={edgeTypesLoading}
              edgeTypesError={edgeTypesError}
              onChange={patchPolicy}
            />
          )}

          <div className="flex items-end gap-2">
            <div className="flex overflow-hidden rounded-lg ring-1 ring-surface-700">
              {(['ask', 'recall'] as Mode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`px-3 py-2 text-xs font-medium ${
                    mode === m ? 'bg-accent-700/40 text-accent-300' : 'bg-surface-900 text-surface-400 hover:text-surface-200'
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void submit()
                }
              }}
              rows={1}
              placeholder={mode === 'ask' ? 'Ask a question…' : 'Recall claims about…'}
              aria-label={mode === 'ask' ? 'Ask a question' : 'Recall claims'}
              className="max-h-32 min-h-[40px] flex-1 resize-none rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 text-sm text-surface-100 placeholder:text-surface-600 focus:border-accent-600 focus:outline-hidden"
            />
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!input.trim()}
              aria-label={mode === 'ask' ? 'Submit question' : 'Submit recall query'}
              className="flex h-10 items-center gap-2 rounded-lg bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500 disabled:opacity-40"
            >
              <Send size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
