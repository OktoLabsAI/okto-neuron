// Mock data conforming to the API contract shapes. Used when mock mode is on.
import type {
  AppConfig,
  AskResponse,
  AskRetrievalPolicy,
  ConfigPatchResponse,
  NodeDetail,
  NodeListResponse,
  NodeTypeInfo,
  NodeRef,
  Provenance,
  RecallResponse,
  IngestResponse,
  IngestQueueResponse,
  IngestQueueItem,
  LedgerRunDetailResponse,
  LedgerRunsResponse,
  UploadFile,
  VaultCreateRequest,
  VaultInfo,
  VaultListResponse,
} from '@/types'

// Stateful mock queue: ingestFolder/Batch seed it, ingestQueue advances one
// item per poll so the bulk UI animates without a backend.
let _mockQueue: IngestQueueItem[] = []
let _mockCancelRequested = false
let _mockVaults: VaultInfo[] = [
  {
    id: 'mock-demo-notes',
    name: 'demo-notes',
    path: '/demo/vaults/demo-notes',
    current: true,
    managed: true,
    deletable: true,
    backend: 'grafx',
  },
  {
    id: 'mock-reading-list',
    name: 'reading-list',
    path: '/demo/vaults/reading-list',
    current: false,
    managed: true,
    deletable: true,
    backend: 'grafx',
  },
  {
    id: 'external-project-journal',
    name: 'project-journal',
    path: '/demo/external/project-journal',
    current: false,
    managed: false,
    deletable: false,
    delete_reason: 'External vaults are not deleted by Okto Neuron.',
    backend: 'ladybug',
  },
]

const MOCK_LEDGER_RUN_ID = 'run-demo-001'

const MOCK_LEDGER_DETAIL: LedgerRunDetailResponse = {
  status: 'ok',
  run: {
    run_id: MOCK_LEDGER_RUN_ID,
    state: 'completed',
    started_at: '2026-06-08T12:00:00+00:00',
    completed_at: '2026-06-08T12:00:04+00:00',
    document_id: 'doc:demo',
    source: '/demo/vaults/demo-notes/sources/example.md',
    name: 'demo.md',
    blocks_total: 2,
    model: 'openai/gpt-4.1-mini',
    summary: { committed: 2, queued: 1, claims_minted: 1 },
    counts: { candidates: 5, comparisons: 4, commit_plans: 1, commit_records: 1 },
  },
  candidates: [
    {
      candidate_id: 'cand-agent',
      candidate_kind: 'node',
      state: 'committed',
      type: 'Agent',
      title: 'Demo Curator',
      confidence: 0.9,
      source_path: '/demo/vaults/demo-notes/sources/example.md',
      block_id: 'block:1',
    },
    {
      candidate_id: 'cand-concept',
      candidate_kind: 'node',
      state: 'queued',
      type: 'Concept',
      title: 'Source Grounding',
      confidence: 0.52,
      source_path: '/demo/vaults/demo-notes/sources/example.md',
      block_id: 'block:1',
    },
  ],
  comparisons: [],
  commit_plans: [],
  commit_records: [],
  records: [
    {
      ledger_version: 1,
      ts: '2026-06-08T12:00:00+00:00',
      kind: 'ingest_run',
      run_id: MOCK_LEDGER_RUN_ID,
      state: 'started',
      document_id: 'doc:demo',
      source: '/demo/vaults/demo-notes/sources/example.md',
      blocks_total: 2,
    },
    {
      ledger_version: 1,
      ts: '2026-06-08T12:00:01+00:00',
      kind: 'candidate',
      run_id: MOCK_LEDGER_RUN_ID,
      candidate_id: 'cand-agent',
      candidate_kind: 'node',
      state: 'proposed',
      payload: {
        type: 'Agent',
        title: 'Demo Curator',
        content: 'The demo curator reviewed source grounding.',
        embedding_dim: 384,
      },
    },
    {
      ledger_version: 1,
      ts: '2026-06-08T12:00:02+00:00',
      kind: 'comparison',
      run_id: MOCK_LEDGER_RUN_ID,
      candidate_id: 'cand-agent',
      method: 'resolver',
      verdict: 'novel',
      score: 0.9,
      payload: { confidence: 0.9 },
    },
    {
      ledger_version: 1,
      ts: '2026-06-08T12:00:03+00:00',
      kind: 'commit_plan',
      run_id: MOCK_LEDGER_RUN_ID,
      plan_id: 'plan-demo',
      operations: [
        {
          operation: 'create_node',
          candidate_id: 'cand-agent',
          type: 'Agent',
          title: 'Demo Curator',
        },
      ],
    },
    {
      ledger_version: 1,
      ts: '2026-06-08T12:00:04+00:00',
      kind: 'commit_record',
      run_id: MOCK_LEDGER_RUN_ID,
      plan_id: 'plan-demo',
      result: { committed_node_ids: ['cand-agent'], claims_minted: 1 },
    },
  ],
}

function _mockSnapshot(): IngestQueueResponse {
  const s = {
    total: _mockQueue.length,
    queued: _mockQueue.filter((i) => i.status === 'queued').length,
    processing: _mockQueue.filter((i) => i.status === 'processing').length,
    done: _mockQueue.filter((i) => i.status === 'done').length,
    error: _mockQueue.filter((i) => i.status === 'error').length,
    cancelled: _mockQueue.filter((i) => i.status === 'cancelled').length,
    active: _mockQueue.some((i) => i.status === 'queued' || i.status === 'processing'),
    cancel_requested: _mockCancelRequested,
  }
  return { status: 'ok', summary: s, items: _mockQueue }
}

const delay = <T>(v: T, ms = 220): Promise<T> =>
  new Promise((res) => setTimeout(() => res(v), ms))

const TYPES: NodeTypeInfo[] = [
  { name: 'Agent', kind: 'primitive', count: 4 },
  { name: 'Activity', kind: 'primitive', count: 9 },
  { name: 'InformationObject', kind: 'primitive', count: 18 },
  { name: 'Concept', kind: 'primitive', count: 42 },
  { name: 'Place', kind: 'primitive', count: 3 },
  { name: 'Document', kind: 'support', count: 12 },
  { name: 'Identifier', kind: 'support', count: 7 },
  { name: 'Annotation', kind: 'support', count: 5 },
  { name: 'Claim', kind: 'support', count: 64 },
  { name: 'Block', kind: 'support', count: 88 },
  { name: 'Finding', kind: 'support', count: 6 },
]

function mkNodes(type: string | undefined, q: string | undefined): NodeRef[] {
  const t = type || 'Concept'
  const names = [
    'knowledge graph',
    'provenance model',
    'byte-range anchoring',
    'closed schema',
    'PROV-O alignment',
    'claim extraction',
    'ladybug store',
    'embedding space',
    'vault canonical markdown',
    'consolidation gate',
  ]
  return names
    .filter((n) => !q || n.toLowerCase().includes(q.toLowerCase()))
    .map((n, i) => ({ id: `node:${t.toLowerCase()}-${i}`, type: t, name: n }))
}

const PROV: Provenance = {
  path: 'notes/knowledge-graphs.md',
  byte_start: 1204,
  byte_end: 1456,
  content_hash: 'sha256:9f2c1ab3de45',
  extraction_activity_id: 'act:extract-001',
  agent_id: 'agent:ingestor',
  document_id: 'doc:kg-notes',
  block_id: 'block:kg-7',
}

export const mock = {
  listVaults: (): Promise<VaultListResponse> =>
    delay<VaultListResponse>({
      status: 'ok',
      current: _mockVaults.find((v) => v.current) ?? null,
      vaults: _mockVaults,
    }),

  createVault: (request: VaultCreateRequest): Promise<VaultListResponse> => {
    const name = request.name.trim()
    const path = `/demo/vaults/${name}`
    const created: VaultInfo = {
      id: `mock-${name}`,
      name,
      path,
      current: false,
      managed: true,
      deletable: true,
      backend: request.backend ?? 'grafx',
    }
    _mockVaults = [..._mockVaults, created]
    _mockQueue = []
    _mockCancelRequested = false
    return delay({
      status: 'ok',
      current: _mockVaults.find((vault) => vault.current) ?? null,
      vaults: _mockVaults,
      created,
    })
  },

  deleteVault: (vaultId: string, confirmName: string): Promise<VaultListResponse> => {
    const target = _mockVaults.find((vault) => vault.id === vaultId)
    if (!target || !target.deletable || target.name !== confirmName) {
      return Promise.reject(new Error('Vault deletion confirmation did not match.'))
    }
    _mockVaults = _mockVaults.filter((vault) => vault.id !== vaultId)
    if (target.current) {
      _mockVaults = _mockVaults.map((vault) => ({ ...vault, current: false }))
    }
    return delay({
      status: 'ok',
      current: _mockVaults.find((vault) => vault.current) ?? null,
      vaults: _mockVaults,
      deleted: { id: target.id, name: target.name, path: target.path },
    })
  },

  startVaultReembed: (vault: string) =>
    delay({
      status: 'started',
      vault,
      reembed: { status: 'ok', running: true, phase: 'embedding', nodes_done: 0, nodes_total: 12 },
    }, 180),

  getVaultReembedStatus: (_vault: string) =>
    delay({
      status: 'ok',
      running: false,
      phase: 'complete',
      nodes_done: 12,
      nodes_total: 12,
      recomputed: 12,
      embedding_dim: 384,
    }, 400),

  ledgerRuns: (): Promise<LedgerRunsResponse> =>
    delay<LedgerRunsResponse>({
      status: 'ok',
      runs: [MOCK_LEDGER_DETAIL.run!],
    }),

  ledgerRun: (_runId: string): Promise<LedgerRunDetailResponse> =>
    delay<LedgerRunDetailResponse>(MOCK_LEDGER_DETAIL),

  listNodeTypes: () => delay(TYPES),

  listNodes: (params: { type?: string; q?: string; limit?: number; offset?: number }) => {
    const all = mkNodes(params.type, params.q)
    const offset = params.offset ?? 0
    const limit = params.limit ?? 50
    const nodes = all.slice(offset, offset + limit)
    return delay<NodeListResponse>({
      status: 'ok',
      total: all.length,
      limit,
      offset,
      nodes,
    })
  },

  getNode: (id: string): Promise<NodeDetail> => {
    const isClaim = id.includes('claim')
    return delay<NodeDetail>({
      status: 'ok',
      node: {
        id,
        type: isClaim ? 'Claim' : 'Concept',
        name: isClaim ? 'a knowledge graph is a derived view of a vault' : 'knowledge graph',
        facets: { kind_of: 'Concept', extends: 'skos:Concept' },
      },
      edges: {
        out: [
          { type: 'prov:wasDerivedFrom', dst: 'block:kg-7' },
          { type: 'skos:broader', dst: 'node:concept-1' },
        ],
        in: [{ type: 'cito:cites', src: 'node:claim-3' }],
      },
      provenance: isClaim ? PROV : null,
      block: isClaim ? { id: 'block:kg-7', facets: { content_hash: PROV.content_hash } } : null,
    })
  },

  getConfig: (): Promise<AppConfig> => {
    const emptyStep = {
      provider: null, api_base: null, model: null, api_key_env: null,
      max_tokens: null, temperature: null, top_p: null, top_k: null,
      min_p: null, presence_penalty: null, enable_thinking: null, system_prompt: null,
      sampling_payload: null,
    }
    return delay<AppConfig>({
      embedding: {
        provider: 'fastembed',
        model: 'BAAI/bge-small-en-v1.5',
        dimension: 384,
        batch_size: 32,
        max_concurrent_batches: 1,
      },
      llm: {
        allow_remote: false,
        defaults: {
          provider: 'openai',
          api_base: 'http://127.0.0.1:8123/v1',
          // Discovery-first: mirrors the backend LLMDefaults baseline — no
          // hardcoded model the configured endpoint may not serve.
          model: '',
          api_key_env: null,
          max_tokens: null,
          temperature: null,
          top_p: null,
          top_k: null,
          min_p: null,
          presence_penalty: null,
          enable_thinking: null,
          sampling_payload: {},
        },
        extraction: { ...emptyStep },
        judge:      { ...emptyStep },
        curator:    { ...emptyStep },
        relation_curator: { ...emptyStep },
        ask:        { ...emptyStep },
        step_default_prompts: {
          extraction: 'Extract all claims, entities, and relationships from the text into structured knowledge graph nodes.',
          judge: 'You are a knowledge graph merge judge. Decide whether two candidates should merge, stay separate, or one supersede the other.',
          curator: 'You are a candidate curator. Decide whether one extracted candidate is ready to commit or should be queued for review.',
          relation_curator: 'You are a relationship curator. Decide whether one extracted edge or literal claim is ready to commit or should be queued for review.',
          ask: 'You are a knowledge retrieval assistant. Answer the question using only the provided context from the knowledge graph.',
        },
      },
      consolidation: {
        auto_commit_threshold: 0.75,
        review_on_contradiction: true,
        type_adjudication_enabled: true,
        relation_curator_enabled: true,
        audit_superseded_nodes_with_llm: false,
        audit_superseded_relations_with_llm: false,
        curation_max_concurrent: 1,
        curation_call_timeout_s: null,
        curation_batch_size: 1,
        prefilter: {
          enabled: false,
          demote_predicates: ['discusses', 'mentions', 'mentioned', 'states', 'stated'],
          min_mentions: 1,
          max_trivial_node_chars: 160,
          established_entity_fastpath: true,
        },
      },
      upkeep: {
        enabled: true,
        max_pairs_per_run: 30,
        min_support: 2,
        cluster_threshold: 0.8,
        auto_fold_threshold: 0.85,
      },
      folder_watch: {
        enabled: false,
        poll_interval_s: 5,
        quiet_debounce_s: 3,
        min_interval_s: 10,
        recursive: true,
        ignore_globs: ['*.swp', '*.tmp', '4913', '*~', '.#*'],
        roots: [],
      },
      ingest: {
        incremental: true,
        subchunk: true,
        chunk_size_bytes: 12000,
        chunk_overlap_bytes: 0,
      },
      packs: ['core', 'research', 'personal'],
      credential_status: {},
      managed_credentials_supported: true,
      reembed_required_fields: [
        'embedding.provider',
        'embedding.model',
        'embedding.dimension',
      ],
      semantic_rebuild_required_fields: [
        'consolidation.type_adjudication_enabled',
        'consolidation.relation_curator_enabled',
      ],
    })
  },

  patchConfig: async (patch: Record<string, unknown>): Promise<ConfigPatchResponse> => {
    const cfg = await mock.getConfig()
    const merged: AppConfig = { ...cfg }
    for (const [k, v] of Object.entries(patch)) {
      if (k === 'llm') {
        // Deep-merge llm: field-level for allow_remote + step-level call sites.
        const llmPatch = v as Record<string, unknown>
        const newLlm = { ...cfg.llm }
        for (const [lk, lv] of Object.entries(llmPatch)) {
          if (typeof lv === 'object' && lv !== null && !Array.isArray(lv)) {
            ;(newLlm as unknown as Record<string, unknown>)[lk] = {
              ...((cfg.llm as unknown as Record<string, unknown>)[lk] as object),
              ...(lv as object),
            }
          } else {
            ;(newLlm as unknown as Record<string, unknown>)[lk] = lv
          }
        }
        merged.llm = newLlm
      } else {
        const cur = (cfg as unknown as Record<string, unknown>)[k]
        ;(merged as unknown as Record<string, unknown>)[k] =
          cur && typeof cur === 'object' && !Array.isArray(cur)
            ? { ...cur, ...(v as object) }
            : v
      }
    }
    const touchesEmbedding = 'embedding' in patch
    return delay<ConfigPatchResponse>({
      status: 'ok',
      config: merged,
      applied: touchesEmbedding ? 'reembed' : 'live',
      changed: Object.keys(patch),
      notes: touchesEmbedding
        ? ['Embedding model changed — stored embeddings invalidated, re-embed required.']
        : ['Change takes effect on next request.'],
    })
  },

  recall: (query: string, k: number): Promise<RecallResponse> =>
    delay<RecallResponse>({
      status: 'ok',
      hits: Array.from({ length: Math.min(k, 4) }).map((_, i) => ({
        node: { id: `node:claim-${i}`, type: 'Claim', name: `claim matching "${query}" #${i + 1}` },
        score: 0.92 - i * 0.07,
        provenance: { ...PROV, byte_start: PROV.byte_start + i * 200, byte_end: PROV.byte_end + i * 200 },
      })),
    }),

  ask: (question: string, k: number, policy?: AskRetrievalPolicy): Promise<AskResponse> =>
    delay<AskResponse>({
      status: 'ok',
      text: `Based on the vault, the answer to "${question}" is synthesised from ${Math.min(k, 3)} grounded claims. The knowledge graph is a derived view; the markdown vault is the trust root, and each claim is byte-anchored to its source block.`,
      citations: ['node:claim-0', 'node:claim-1'],
      retrieval: {
        mode: policy?.enable_subgraph ? 'subgraph' : 'block',
        path: policy?.enable_subgraph ? 'subgraph_then_sources' : 'block_with_sources',
        seed_k: k,
        enable_subgraph: Boolean(policy?.enable_subgraph),
        hops: policy?.hops ?? 1,
        max_degree_per_seed: policy?.max_degree_per_seed ?? 8,
        neighbour_budget_tokens: policy?.neighbour_budget_tokens ?? 2000,
        coverage_threshold: policy?.coverage_threshold ?? 0.4,
        min_claim_confidence: policy?.min_claim_confidence ?? 0,
        max_nodes: policy?.max_nodes ?? null,
        max_relationships: policy?.max_relationships ?? null,
        max_claims: policy?.max_claims ?? null,
        relationship_types: policy?.relationship_types ?? [],
        source_block_policy: policy?.source_block_policy ?? 'on_coverage_miss',
        source_block_budget_tokens: policy?.source_block_budget_tokens ?? 4000,
        source_blocks_used: policy?.source_block_policy !== 'never',
        context_tokens_estimate: policy?.enable_subgraph ? 900 : 6400,
      },
      hits: Array.from({ length: Math.min(k, 3) }).map((_, i) => ({
        node: { id: `node:claim-${i}`, type: 'Claim', name: `supporting claim #${i + 1}` },
        score: 0.9 - i * 0.1,
        provenance: { ...PROV, byte_start: PROV.byte_start + i * 200, byte_end: PROV.byte_end + i * 200 },
      })),
    }),

  ingest: (content: string, filename?: string): Promise<IngestResponse> =>
    delay<IngestResponse>(
      {
        status: 'ok',
        document_id: 'doc:mock-ingested',
        filename: filename || 'note-mock.md',
        committed: 3,
        queued: 1,
        blocks_total: 5,
        nodes_extracted: 4,
        edges_extracted: 3,
        claims_minted: 2,
        outcome: {
          quality: 'complete',
          units: { scheduled: 5, succeeded: 5, reused: 0, failed: 0, skipped: 0 },
          failed_units: [],
        },
        outcomes: [
          { candidate_id: 'c0', type: 'Agent', title: 'extracted entity', action: 'committed', confidence: 0.91 },
          { candidate_id: 'c1', type: 'Concept', title: content.slice(0, 28) || 'concept', action: 'committed', confidence: 0.83 },
          { candidate_id: 'c2', type: 'Claim', title: 'relationship claim', action: 'committed', confidence: 0.79 },
          { candidate_id: 'c3', type: 'Concept', title: 'low-confidence candidate', action: 'queued', confidence: 0.52 },
        ],
      },
      900,
    ),

  ingestFolder: (path: string): Promise<IngestQueueResponse> => {
    _mockCancelRequested = false
    const base = path.replace(/\/+$/, '').split('/').pop() || 'folder'
    _mockQueue = Array.from({ length: 6 }).map((_, i) => ({
      id: `mq-${i}`,
      name: `${base}/note-${i + 1}.md`,
      path: `${path}/note-${i + 1}.md`,
      status: 'queued' as const,
      committed: 0,
      queued: 0,
      nodes: 0,
      edges: 0,
      claims: 0,
      extracted_nodes: 0,
      extracted_edges: 0,
      extracted_claims: 0,
      error: null,
      event_count: 1,
      last_event: {
        ts: Date.now() / 1000,
        kind: 'queued',
        summary: 'Queued for ingest',
        payload: { path: `${path}/note-${i + 1}.md` },
      },
    }))
    return delay({ ..._mockSnapshot(), enqueued: _mockQueue.length }, 400)
  },

  ingestBatch: (files: UploadFile[]): Promise<IngestQueueResponse> => {
    _mockCancelRequested = false
    _mockQueue = files.map((f, i) => ({
      id: `mq-up-${i}`,
      name: f.filename,
      path: `(upload)/${f.filename}`,
      status: 'queued' as const,
      committed: 0,
      queued: 0,
      nodes: 0,
      edges: 0,
      claims: 0,
      extracted_nodes: 0,
      extracted_edges: 0,
      extracted_claims: 0,
      error: null,
      event_count: 1,
      last_event: {
        ts: Date.now() / 1000,
        kind: 'queued',
        summary: 'Queued for ingest',
        payload: { path: `(upload)/${f.filename}` },
      },
    }))
    return delay({ ..._mockSnapshot(), enqueued: _mockQueue.length }, 400)
  },

  // Each poll advances one queued item to done so the bulk UI animates.
  ingestQueue: (): Promise<IngestQueueResponse> => {
    const processing = _mockQueue.find((i) => i.status === 'processing')
    if (processing && _mockCancelRequested) {
      processing.status = 'cancelled'
      processing.stage = 'cancelled'
      processing.error = 'cancelled by user'
      _mockCancelRequested = false
      return delay(_mockSnapshot(), 250)
    }
    if (processing) {
      const total = processing.blocks_total ?? 5
      const done = (processing.blocks_done ?? 0) + 2
      if (done < total) {
        // Still chewing through blocks — advance within-file progress.
        processing.blocks_total = total
        processing.blocks_done = done
        processing.stage = done < total / 2 ? 'extracting' : 'dedup'
        processing.extracted_nodes = (processing.extracted_nodes ?? 0) + 4
        processing.extracted_edges = (processing.extracted_edges ?? 0) + 3
        processing.extracted_claims = (processing.extracted_claims ?? 0) + 2
        return delay(_mockSnapshot(), 250)
      }
      processing.status = 'done'
      processing.committed = 2 + (parseInt(processing.id.replace(/\D/g, ''), 10) % 3)
      processing.queued = 1
      processing.nodes = processing.committed + processing.queued
      processing.edges = Math.max(1, processing.committed - 1)
      processing.claims = Math.max(1, processing.committed)
      processing.stage = 'done'
      const terminalVariant = parseInt(processing.id.replace(/\D/g, ''), 10) % 5
      if (terminalVariant === 1) {
        processing.provider_error = 'mock timeout after partial extraction'
        processing.outcome = {
          quality: 'partial',
          units: { scheduled: total, succeeded: total - 1, failed: 1, skipped: 0 },
          failed_units: [
            { unit_id: `${processing.id}:4`, error_class: 'timeout', retryable: true },
          ],
        }
      } else if (terminalVariant === 2) {
        processing.status = 'error'
        processing.error = 'mock authentication failure'
        processing.outcome = {
          quality: 'failed',
          units: { scheduled: total, succeeded: 0, failed: total, skipped: 0 },
          failed_units: Array.from({ length: total }).map((_, index) => ({
            unit_id: `${processing.id}:${index}`,
            error_class: 'authentication',
            retryable: false,
          })),
        }
      } else if (terminalVariant === 3) {
        processing.outcome = { quality: 'not_applicable' }
      } else if (terminalVariant === 0) {
        processing.outcome = {
          quality: 'complete',
          units: { scheduled: total, succeeded: total, failed: 0, skipped: 0 },
          failed_units: [],
        }
      }
      processing.event_count = 8
      processing.last_event = {
        ts: Date.now() / 1000,
        kind: 'commit',
        summary: 'Committed graph changes',
        payload: { nodes: processing.nodes, edges: processing.edges, claims: processing.claims },
      }
    }
    const next = _mockQueue.find((i) => i.status === 'queued')
    if (next) {
      next.status = 'processing'
      next.stage = 'parsing'
      next.blocks_total = 5
      next.blocks_done = 0
      next.extracted_nodes = 0
      next.extracted_edges = 0
      next.extracted_claims = 0
      next.event_count = 3
      next.last_event = {
        ts: Date.now() / 1000,
        kind: 'llm_request',
        summary: 'Extraction LLM request',
        payload: { model: 'stub', block: { index: 0 } },
      }
    }
    return delay(_mockSnapshot(), 250)
  },

  ingestQueueItem: (itemId: string) => {
    const item = _mockQueue.find((i) => i.id === itemId) ?? _mockQueue[0]
    const events = [
      {
        ts: Date.now() / 1000 - 8,
        kind: 'chunks',
        summary: 'Parsed 2 extraction chunks',
        payload: {
          chunks: [
            { index: 0, text: '# Intro\n\nOkto Neuron extracts claims.', anchor: { block_id: 'block:1' } },
            { index: 1, text: 'Jordan from NX cited METR 2025.', anchor: { block_id: 'block:2' } },
          ],
        },
      },
      {
        ts: Date.now() / 1000 - 6,
        kind: 'llm_request',
        summary: 'Extraction LLM request',
        payload: {
          model: 'stub',
          messages: [
            { role: 'system', content: 'You extract knowledge-graph nodes...' },
            { role: 'user', content: 'Jordan from NX cited METR 2025.' },
          ],
          params: { temperature: 0.7, max_tokens: 16000 },
        },
      },
      {
        ts: Date.now() / 1000 - 5,
        kind: 'llm_response',
        summary: 'Extraction LLM response',
        payload: {
          response: '{"nodes":[{"type":"Agent","title":"Jordan"}],"edges":[],"claims":[]}',
        },
      },
      {
        ts: Date.now() / 1000 - 2,
        kind: 'dedup_store_exact',
        summary: 'Exact cross-file store reconcile',
        payload: { merged_into: {}, survivors: [{ type: 'Agent', title: 'Jordan' }] },
      },
    ]
    return delay({ status: 'ok', item: { ...item, events, event_count: events.length } }, 180)
  },

  retryIngestQueueItem: (itemId: string): Promise<IngestQueueResponse> => {
    const item = _mockQueue.find((candidate) => candidate.id === itemId)
    if (item) {
      item.status = 'queued'
      item.stage = 'queued'
      item.error = null
      item.provider_error = null
      item.outcome = undefined
      item.blocks_total = 0
      item.blocks_done = 0
    }
    return delay(_mockSnapshot(), 250)
  },

  cancelIngest: (): Promise<IngestQueueResponse> => {
    _mockCancelRequested = _mockQueue.some((item) => item.status === 'processing')
    let cancelled = 0
    _mockQueue = _mockQueue.map((item) => {
      if (item.status !== 'queued') return item
      cancelled += 1
      return {
        ...item,
        status: 'cancelled',
        stage: 'cancelled',
        error: 'cancelled by user',
      }
    })
    return delay({ ..._mockSnapshot(), cancelled }, 250)
  },
}
