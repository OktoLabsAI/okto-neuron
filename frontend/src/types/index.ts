// Types mirror the API contract (.scratchpad/webui-design.md §2).

export const PRIMITIVES = [
  'Agent',
  'Activity',
  'InformationObject',
  'Concept',
  'Place',
] as const

export const SUPPORT_TYPES = [
  'Document',
  'Identifier',
  'Annotation',
  'Claim',
  'Block',
  'Finding',
] as const

export type NodeKind = 'primitive' | 'support'

export interface NodeTypeInfo {
  name: string
  kind: NodeKind
  count: number
}

export interface NodeRef {
  id: string
  type: string
  name: string
  // ADR 0009 P2 equivalence fold: present on a canonical node row when the
  // read-time fold collapsed N variant nodes onto it (drives the "N variants"
  // badge in Browse). Absent on non-canonical / unreconciled rows.
  variant_count?: number
  // ADR 0024 validity subset — present only when the node's facets carry the
  // corresponding marker. Superseded claims are filtered from recall by
  // default, so seeing one here means a caller explicitly opted in.
  superseded?: boolean
  valid_until?: string
  detached?: boolean
  valid_as_of?: string
}

export interface NodeListResponse {
  status: string
  total: number
  limit: number
  offset: number
  nodes: NodeRef[]
}

export interface Provenance {
  path: string
  byte_start: number
  byte_end: number
  content_hash: string
  extraction_activity_id?: string | null
  agent_id?: string | null
  document_id?: string | null
  block_id?: string | null
}

export interface EdgeOut {
  type: string
  dst: string
}
export interface EdgeIn {
  type: string
  src: string
}

export interface NodeDetail {
  status: string
  node: {
    id: string
    type: string
    name: string
    facets?: Record<string, unknown>
  }
  edges: {
    out: EdgeOut[]
    in: EdgeIn[]
  }
  provenance: Provenance | null
  block: { id: string; facets?: Record<string, unknown> } | null
  // ADR 0009 P2 equivalence fold: set when THIS node is a folded variant — the
  // id of the canonical it collapses onto (so the detail panel can link to it).
  // null on a canonical / unreconciled node.
  canonical_id?: string | null
}

// --- Graph visualization (read-only; contract §graph) ---

// A graph node row: the NodeRef summary plus a degree for sizing.
export interface GraphNode extends NodeRef {
  degree: number
}

export interface GraphEdge {
  src: string
  dst: string
  type: string
}

// GET /api/v1/graph — capped, filtered overview subgraph.
export interface GraphResponse {
  status: string
  truncated: boolean
  total_nodes: number
  total_edges: number
  returned_nodes: number
  returned_edges: number
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// GET /api/v1/nodes/{id}/neighbors — same shape plus the seed id.
export interface NeighborsResponse {
  status: string
  seed: string
  total_nodes: number
  total_edges: number
  returned_nodes: number
  returned_edges: number
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphTypeCount {
  type: string
  count: number
}

// GET /api/v1/graph/stats — counts for the filter controls.
export interface GraphStats {
  status: string
  node_types: GraphTypeCount[]
  edge_types: GraphTypeCount[]
  total_nodes: number
  total_edges: number
  // Served from the daemon's maintained projection: may lag a write by one rebuild.
  stale?: boolean
  rebuilding?: boolean
}

// --- Config ---

export interface EmbeddingConfig {
  provider_ref?: string | null
  provider: string
  model: string
  // External (LiteLLM-routed) transport knobs. Local providers leave api_base/
  // api_key_env null. dimension is the fixed vector width — a change requires a
  // re-embed (it is in reembed_required_fields).
  api_base?: string | null
  api_key_env?: string | null
  allow_remote?: boolean
  dimension?: number
  // Execution policy only; changing either value does not invalidate vectors.
  batch_size?: number
  max_concurrent_batches?: number
}

// POST /api/v1/embedding/test — reuses the LLM test request/response shape.
export interface EmbeddingTestRequest {
  provider: string
  model: string
  dimension: number
  api_base?: string | null
  api_key_env?: string | null
  allow_remote?: boolean
}

export interface EmbeddingModelDiscoveryRequest {
  provider_ref?: string | null
  provider: string
  api_base?: string | null
  api_key_env?: string | null
  allow_remote?: boolean
}

export interface EmbeddingModelDiscoveryResponse {
  ok: boolean
  known: boolean
  source: 'litellm_gateway' | 'provider'
  models: string[]
  error: string | null
}

export interface EmbeddingTestResponse {
  ok: boolean
  models: string[]
  dimension?: number
  vectors?: number
  error: string | null
}

// GET /api/v1/embedding/reembed/status
export interface ReembedStatus {
  status: string
  running: boolean
  phase: string
  nodes_done?: number
  nodes_total?: number
  recomputed?: number
  copied?: number
  nodes?: number
  edges?: number
  embedding_dim?: number
}

// ADR 0015 D2 — deterministic pre-filter for low-value candidates.
export interface PrefilterConfig {
  enabled: boolean
  demote_predicates: string[]
  min_mentions: number
  max_trivial_node_chars: number
  established_entity_fastpath: boolean
}

export interface ConsolidationConfig {
  auto_commit_threshold: number
  review_on_contradiction: boolean
  type_adjudication_enabled: boolean
  relation_curator_enabled: boolean
  audit_superseded_nodes_with_llm: boolean
  audit_superseded_relations_with_llm: boolean
  // ADR 0015 performance knobs (D1 concurrency, D4 batching, D2 prefilter).
  curation_max_concurrent: number
  curation_call_timeout_s: number | null
  curation_batch_size: number
  prefilter: PrefilterConfig
}

// ADR 0017 — continuous predicate canonicalization knobs.
export interface UpkeepConfig {
  enabled: boolean
  max_pairs_per_run: number
  min_support: number
  cluster_threshold: number
  auto_fold_threshold: number
}

// Per-step LLM override block — every field is nullable, meaning "inherit from defaults".
export interface StepLLM {
  provider_ref?: string | null
  provider: string | null
  api_base: string | null
  model: string | null
  api_key_env: string | null
  max_tokens: number | null
  temperature: number | null
  top_p: number | null
  top_k: number | null
  min_p: number | null
  presence_penalty: number | null
  enable_thinking: boolean | null
  // Raw sampling-payload override for this step (FROZEN — see backend
  // StepLLM.sampling_payload docstring). `null` (distinct from `{}`) means
  // this step never set its own payload and inherits the default's payload
  // WHOLE; any concrete object, including `{}`, is a complete standalone
  // payload that never merges with the default.
  sampling_payload: Record<string, LlmParameterValue> | null
  system_prompt: string | null
  max_concurrent?: number | null
  enable_subgraph?: boolean | null
  max_degree_per_seed?: number | null
  neighbour_budget_tokens?: number | null
  hops?: number | null
  coverage_threshold?: number | null
  render_format?: string | null
  min_claim_confidence?: number | null
}

// Defaults block — same fields but required numeric/string params so the stack always has
// a concrete value to resolve to. No system_prompt — that lives on StepLLM only.
export interface LLMDefaults {
  provider_ref?: string | null
  provider: string
  api_base: string
  model: string
  api_key_env: string | null
  max_tokens: number | null
  temperature: number | null
  top_p: number | null
  top_k: number | null
  min_p: number | null
  presence_penalty: number | null
  enable_thinking: boolean | null
  // Raw sampling-payload override baseline, inherited WHOLE by every step
  // that leaves its own `sampling_payload` unset. Never `null` here — empty
  // (`{}`, the default) means "use provider defaults".
  sampling_payload: Record<string, LlmParameterValue>
}

export interface StepDefaultPrompts {
  extraction: string
  judge: string
  curator: string
  relation_curator: string
  ask: string
}

export interface LLMContainer {
  allow_remote: boolean
  defaults: LLMDefaults
  extraction: StepLLM
  judge: StepLLM
  curator: StepLLM
  relation_curator: StepLLM
  ask: StepLLM
  step_default_prompts: StepDefaultPrompts
}

export interface LlmTestRequest {
  provider_ref?: string | null
  provider: string
  model?: string
  api_base?: string | null
  api_key_env?: string | null
}

export interface LlmTestResponse {
  ok: boolean
  models: string[]
  error: string | null
}

export type LlmParameterValue =
  | string
  | number
  | boolean
  | null
  | LlmParameterValue[]
  | { [key: string]: LlmParameterValue }

// Round-trips one real completion through the configured provider/model —
// catches wrong model ids / broken auth that reachability-only /llm/test can't.
export type LlmTestCompletionRequest = Omit<LlmTestRequest, 'model'> & {
  model: string
  provider_ref?: string | null
  max_tokens?: number
  temperature?: number
  top_p?: number
  top_k?: number
  min_p?: number
  presence_penalty?: number
  enable_thinking?: boolean
  parameters?: Record<string, LlmParameterValue>
}

export interface ParameterPlan {
  sent: string[]
  extra_body: string[]
  omitted: Record<string, string>
}

export interface LlmTestCompletionResponse {
  ok: boolean
  reply: string | null
  error: string | null
  duration_s: number | null
  parameter_plan?: ParameterPlan | null
}

// ADR 0025 — continuous folder-monitoring policy.
export interface FolderWatchConfig {
  enabled: boolean
  poll_interval_s: number
  quiet_debounce_s: number
  min_interval_s: number
  recursive: boolean
  ignore_globs: string[]
  roots: string[]
}

export interface IngestConfig {
  incremental: boolean
  subchunk: boolean
  chunk_size_bytes: number
  chunk_overlap_bytes: number
}

/** Configured-vs-effective LLM concurrency for one fan-out. Read-only. */
export interface CapacityLane {
  /** What the vault config stores — kept verbatim, never rewritten. */
  configured: number | null
  /** What the backend will actually run in flight after the capacity clamp. */
  effective: number
  /** Model aliases gating this fan-out (curation gates on curator + relation). */
  models: string[]
  /** Human-readable reason when configured was clamped; null when it was not. */
  notice: string | null
}

/**
 * Read-only capacity report from `okto_neuron.config._capacity`, the single
 * helper both fan-outs consume. Only aliases in `parallel_capable_models` may
 * exceed one in-flight request; everything else clamps to 1.
 */
export interface CapacityReport {
  parallel_capable_models: string[]
  extraction: CapacityLane
  curation: CapacityLane
}

export interface AppConfig {
  status?: string
  scope?: ConfigScope
  inherits_application_defaults?: boolean
  embedding: EmbeddingConfig
  llm: LLMContainer
  consolidation: ConsolidationConfig
  /** Read-only; absent on older daemons that predate the capacity report. */
  capacity?: CapacityReport
  upkeep: UpkeepConfig
  folder_watch: FolderWatchConfig
  ingest: IngestConfig
  packs: string[]
  // Presence-only credential state for env references used by this config.
  // Secret values are never returned to the browser.
  credential_status?: Record<string, boolean>
  managed_credentials_supported?: boolean
  reembed_required_fields: string[]
  semantic_rebuild_required_fields: string[]
}

export interface CredentialStoreRequest {
  kind: 'llm' | 'embedding'
  provider: string
  api_base?: string | null
  api_key: string
}

export interface CredentialStoreResponse {
  status: string
  api_key_env: string
  configured: boolean
}

export interface NamedCredential {
  id: string
  name: string
  configured: boolean
  created_at: string
  updated_at: string
}

export interface ProviderProfile {
  id: string
  name: string
  driver: string
  api_base: string | null
  allow_remote: boolean
  credential_id: string | null
  credential_name: string | null
  credential_configured: boolean
  api_key_env: string | null
  parameter_mode: 'safe' | 'local_extended'
  request_timeout_s: number | null
  uses: Array<'llm' | 'embedding'>
  created_at: string
  updated_at: string
}

export interface ProviderType {
  driver: string
  label: string
  uses: Array<'llm' | 'embedding'>
  default_api_base: string | null
  managed_api_key: boolean
  local_extended_allowed: boolean
}

// ── folder-watch status (live, polled) ──────────────────────────────────────

export interface FolderWatchPendingFile {
  name: string
  settling_for_s: number
}

export interface FolderWatchRecentIngest {
  name: string
  ts: number
}

export interface FolderWatchVaultStatus {
  enabled: boolean
  roots: string[]
  last_poll_ts: number | null
  watched_file_count: number
  pending: FolderWatchPendingFile[]
  recent_ingests: FolderWatchRecentIngest[]
  poll_interval_s?: number | null
  quiet_debounce_s?: number | null
  // Non-null means this immutable vault runtime is temporarily paused (for
  // example during maintenance or deletion drain), regardless of enabled/roots.
  paused_reason?: string | null
  // Files skipped by the walker for a non-ingestible suffix (not markdown/txt).
  skipped_non_text?: number
}

export interface FolderWatchStatusResponse {
  status: string
  selected_vault: string | null
  vaults: Record<string, FolderWatchVaultStatus>
}

export type AppliedKind = 'live' | 'reembed' | 'rebuild'
export type ConfigScope = 'vault' | 'application'

export interface ConfigPatchResponse {
  status: string
  config: AppConfig
  applied: AppliedKind
  changed?: string[]
  notes: string[]
  affected_vaults?: string[]
  rebuild_required_vaults?: string[]
}

// --- Query ---

export type SourceBlockPolicy = 'never' | 'on_coverage_miss' | 'always'

export interface AskRetrievalPolicy {
  enable_subgraph: boolean | null
  seed_k: number | null
  hops: number | null
  max_degree_per_seed: number | null
  neighbour_budget_tokens: number | null
  coverage_threshold: number | null
  min_claim_confidence: number | null
  max_nodes: number | null
  max_relationships: number | null
  max_claims: number | null
  relationship_types: string[] | null
  source_block_policy: SourceBlockPolicy | null
  source_block_budget_tokens: number | null
}

export interface AskRetrievalTrace {
  mode?: string
  path?: string
  seed_k?: number
  enable_subgraph?: boolean
  hops?: number
  max_degree_per_seed?: number
  neighbour_budget_tokens?: number
  coverage_threshold?: number
  min_claim_confidence?: number
  max_nodes?: number | null
  max_relationships?: number | null
  max_claims?: number | null
  relationship_types?: string[]
  source_block_policy?: SourceBlockPolicy
  source_block_budget_tokens?: number | null
  source_blocks_used?: boolean
  context_tokens_estimate?: number
  /** "ok" for a clean answer; anything else means the answer is degraded
   * (no_llm, provider_error, truncated, abnormal_stop, empty). */
  synthesis_status?: string
  no_llm_reason?: string
  provider_error?: string
  finish_reason?: string
}

export interface QueryHit {
  node: NodeRef
  score: number
  provenance: Provenance | null
}

export interface RecallResponse {
  status: string
  hits: QueryHit[]
}

export interface AskResponse {
  status: string
  text: string
  citations: string[]
  hits: QueryHit[]
  retrieval?: AskRetrievalTrace
}

export interface ApiError {
  error: string
  detail: string
  status: number
}

// --- Vaults ---

export interface VaultInfo {
  id: string
  name: string
  path: string
  current: boolean
  managed: boolean
  deletable: boolean
  delete_reason?: string | null
  issue?: VaultIssue | null
  /** Pinned graph backend name (`storage.backend` in `okto-neuron.yaml`,
   * `"ladybug"` for the legacy absent-key vault) -- `vault_registry.py`'s
   * `VaultEntry.to_json()`. */
  backend: string
}

export interface VaultListResponse {
  status: string
  current: VaultInfo | null
  vaults: VaultInfo[]
  warning?: VaultIssue | null
  created?: VaultInfo | null
  deleted?: { id: string; name: string; path: string } | null
}

export interface VaultCreateRequest {
  name: string
  embedder?: string
  packs?: string[]
  backend?: string
  /** Required (true) when `backend` names a backend whose capabilities mark
   * it `experimental` -- server/http.py's `api_vault_create` rejects the
   * request with 400 otherwise (M4 spec section 2, D-12's web-path gate). */
  accept_experimental?: boolean
  /** Storage endpoint fields for a non-Ladybug server-side `backend` (e.g.
   * Neo4j) -- threaded to `_initialize_managed_vault` / `Vault.scaffold`. */
  storage_uri?: string
  storage_credential_env?: string
  storage_database?: string
  /** Confirms remote egress for a non-loopback `storage_uri` -- server/http.py's
   * `api_vault_create` rejects the request with 400 otherwise. */
  allow_remote_db?: boolean
}

// --- Graph backends (GET /api/v1/backends) ---

/** Mirrors `store/capabilities.py`'s `BackendCapabilities` dataclass, as
 * serialized by `asdict()` in `server/http.py`'s `api_backends`. */
export interface BackendCapabilities {
  name: string
  server_side_schema: boolean
  native_traversal: boolean
  requires_network: boolean
  concurrency_model: 'single_writer' | 'mvcc' | 'server'
  audit_supported: boolean
  checkpoint_is_noop: boolean
  /** Pre-alpha on-disk format / non-OSI-license gate (D-12) -- a hard UX
   * confirmation, not documentation-only, when true. */
  experimental: boolean
}

export interface BackendInfo {
  name: string
  /** `null` for a resolvable backend with no registered capabilities entry
   * (see `capabilities_for`'s own docstring) -- a valid, if unusual, state. */
  capabilities: BackendCapabilities | null
}

export interface VaultIssue {
  code: string
  path: string
  detail: string
  remedy?: string
}

// --- Ingest ---

export interface IngestOutcome {
  candidate_id: string
  type: string
  title: string
  action: 'committed' | 'queued'
  confidence: number
}

export type IngestOutcomeQuality =
  | 'complete'
  | 'partial'
  | 'failed'
  | 'integrity_failed'
  | 'not_applicable'
  | 'empty'
  | 'unknown'

export interface IngestFailedUnit {
  unit_id?: string
  block_id?: string | null
  byte_start?: number | null
  byte_end?: number | null
  error_class?: string | null
  retryable?: boolean
}

export interface IngestTechnicalOutcome {
  quality?: IngestOutcomeQuality
  error_class?: string | null
  retryable?: boolean
  units?: {
    scheduled?: number
    attempted?: number
    succeeded?: number
    reused?: number
    failed?: number
    empty_after_retry?: number
    skipped?: number
    source_changed?: number
    cancelled?: number
  }
  failed_units?: IngestFailedUnit[]
  failed_units_truncated?: boolean
  provider_failures?: number
  empty_after_retry_blocks?: number
  plan_id?: string | null
  receipts_complete?: boolean
  integrity?: {
    status?: string | null
    audit_id?: string | null
    graph_generation?: string | null
  }
}

export interface IngestResponse {
  status: string
  document_id: string
  filename: string
  committed: number
  queued: number
  blocks_total?: number
  nodes_extracted?: number
  edges_extracted?: number
  claims_minted?: number
  outcomes: IngestOutcome[]
  outcome?: IngestTechnicalOutcome | null
}

export type IngestItemStatus = 'queued' | 'processing' | 'done' | 'error' | 'cancelled'

export interface IngestEvent {
  ts: number
  kind: string
  summary: string
  payload: Record<string, unknown>
}

export interface IngestQueueItem {
  id: string
  name: string
  path: string
  status: IngestItemStatus
  committed: number
  queued: number
  error: string | null
  provider_error?: string | null
  outcome?: IngestTechnicalOutcome | null
  // Within-file progress (populated by the worker while status === 'processing').
  stage?: string | null
  blocks_total?: number | null
  blocks_done?: number | null
  nodes?: number | null
  edges?: number | null
  claims?: number | null
  extracted_nodes?: number | null
  extracted_edges?: number | null
  extracted_claims?: number | null
  stage_progress_label?: string | null
  stage_progress_done?: number | null
  stage_progress_total?: number | null
  event_count?: number
  last_event?: IngestEvent | null
  events?: IngestEvent[]
}

export interface IngestQueueSummary {
  total: number
  queued: number
  processing: number
  done: number
  error: number
  cancelled: number
  active: boolean
  cancel_requested: boolean
}

export interface IngestQueueResponse {
  status: string
  vault?: VaultInfo | null
  summary: IngestQueueSummary
  items: IngestQueueItem[]
  enqueued?: number
  // Exact identities created by a batch POST. Automation uses these instead of
  // queue-wide totals, which can include unrelated or historical work.
  enqueued_item_ids?: string[]
  // Dedup refreshes — sources that were already queued and got their durable
  // copy refreshed instead of being re-enqueued as a new item.
  refreshed?: number
  truncated?: boolean
  cancelled?: number
  // Files the folder walk skipped for a non-ingestible suffix (not markdown/txt).
  skipped_non_text?: number
  // Uploads the server's source-selection policy excluded (dot-directory
  // scaffolding, agent tooling notes, ignore-glob hits). The browser used to be
  // the only filter on the batch path, so these were queued silently.
  skipped_excluded?: number
  // Uploads whose content was empty/whitespace-only.
  skipped_empty?: number
  // Per-file skip report: what was dropped and under which rule. Capped
  // server-side; the counts above are always exact.
  skipped?: IngestSkippedFile[]
}

export interface IngestSkippedFile {
  filename: string
  // ignored_dir | denylisted | ignored_glob | non_text_suffix | empty
  reason: string
}

export interface IngestQueueItemResponse {
  status: string
  vault?: VaultInfo | null
  item: IngestQueueItem
}

export interface UploadFile {
  filename: string
  content: string
}

// --- ADR 0013 candidate ledger ---

export interface LedgerRunSummary {
  run_id: string
  state: string
  started_at: string | null
  completed_at: string | null
  document_id: string | null
  source: string | null
  name: string | null
  blocks_total: number
  model: string | null
  summary: Record<string, unknown>
  counts: {
    candidates: number
    comparisons: number
    commit_plans: number
    commit_records: number
  }
}

export interface LedgerCandidateSummary {
  candidate_id: string
  candidate_kind: 'node' | 'edge' | string
  state: string
  type: string
  title: string
  confidence?: number | null
  source_path?: string | null
  block_id?: string | null
}

export interface LedgerRecord {
  ledger_version: number
  ts: string
  kind: 'ingest_run' | 'candidate' | 'comparison' | 'commit_plan' | 'commit_record' | string
  run_id?: string
  candidate_id?: string
  candidate_kind?: string
  state?: string
  method?: string
  verdict?: string
  target_ref?: string | null
  score?: number | null
  reason?: string
  plan_id?: string
  document_id?: string
  source?: string
  blocks_total?: number
  model?: string
  payload?: Record<string, unknown>
  operations?: Record<string, unknown>[]
  result?: Record<string, unknown>
  summary?: Record<string, unknown>
}

export interface LedgerRunsResponse {
  status: string
  runs: LedgerRunSummary[]
}

export interface LedgerRunDetailResponse {
  status: string
  run: LedgerRunSummary | null
  records: LedgerRecord[]
  candidates: LedgerCandidateSummary[]
  comparisons: LedgerRecord[]
  commit_plans: LedgerRecord[]
  commit_records: LedgerRecord[]
}

export interface LedgerProgressRow {
  done: number
  total: number
  remaining: number
  /** Unbounded on purpose (ADR 0039 T9): > 1 means the phase reported more
   * work done than its declared population. The server never clamps it and
   * neither may the UI. `null` when the population is still 0. */
  fraction: number | null
  population?: string
  population_revision?: {
    previous_total: number
    total: number
    reason: string
    source?: string
  }
  /** Present only when `done > total`. The server omits the key entirely for
   * a healthy row. */
  progress_integrity_error?: {
    code: string
    population: string
    done: number
    total: number
    overflow: number
  }
}

export interface LedgerPendingNodeSample {
  candidate_id: string
  type: string
  title: string
}

export interface LedgerPendingRefSample {
  ref?: string
  type?: string
  title?: string
  literal?: unknown
}

export interface LedgerPendingRelationSample {
  candidate_id: string
  predicate: string
  raw_predicate: string
  terminal_action: string
  relation_kind: string
  subject: LedgerPendingRefSample
  object: LedgerPendingRefSample | null
}

export interface LedgerPendingCommitPreview {
  nodes: {
    accepted_for_write: number
    queued_or_abstained: number
    types_by_verdict: Record<string, Record<string, number>>
    sample_titles_by_verdict: Record<string, string[]>
    sample_candidates_by_verdict?: Record<string, LedgerPendingNodeSample[]>
  }
  relations: {
    accepted_for_write: number
    canonicalized_originals: number
    endpoint_dead_letters: number
    queued_or_abstained: number
    terminals_by_verdict: Record<string, Record<string, number>>
    accepted_predicates: Record<string, number>
    queued_predicates: Record<string, number>
    sample_relations_by_verdict?: Record<string, LedgerPendingRelationSample[]>
  }
}

export interface LedgerSummaryResponse {
  status: string
  run: LedgerRunSummary | null
  counts?: {
    candidates: number
    candidate_rows: number
    comparisons: number
    commit_plans: number
    commit_records: number
  }
  candidate_kinds?: Record<string, number>
  active_candidate_kinds?: Record<string, number>
  comparison_methods?: Record<string, number>
  comparison_verdicts?: Record<string, number>
  audit_modes?: Record<string, number>
  llm_timing?: Record<
    string,
    { calls: number; total_s: number; p50_s: number; p90_s: number; max_s: number; tokens?: Record<string, number> }
  >
  progress?: {
    node_curator: LedgerProgressRow
    relation_curator: LedgerProgressRow
  }
  pending_commit_preview?: LedgerPendingCommitPreview
}
