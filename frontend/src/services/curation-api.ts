// Curation control-plane service (ADR 0009 P1 + P2).
//
// All browser controls use the versioned /api/v1 contract so the production
// wheel and Vite's documented /api proxy behave identically. Bare server routes
// remain only for CLI/backward compatibility.
import { apiFetch, qs } from './http'

// ── P1: health + dashboard ──────────────────────────────────────────────────--
export interface HealthResponse {
  status: string
  vault_path: string
  uptime_s: number
  pid: number
}
export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>('/status')
}

// /api/v1/graph/stats is already served (graph-api also reads it); re-export shape.
export interface GraphStatsLite {
  status: string
  node_types: { type: string; count: number }[]
  edge_types: { type: string; count: number }[]
  total_nodes: number
  total_edges: number
}
export function getGraphStats(): Promise<GraphStatsLite> {
  return apiFetch<GraphStatsLite>('/graph/stats')
}

// ── ADR 0040: explicit semantic-quality audit ───────────────────────────────
// This endpoint performs a complete-store scan. Callers must invoke it from an
// explicit user action; it is intentionally not part of dashboard polling.
export const SEMANTIC_QUALITY_LAYERS = [
  'surface',
  'type',
  'identity',
  'predicate',
  'relation',
  'recall',
] as const

export type SemanticQualityLayerName = (typeof SEMANTIC_QUALITY_LAYERS)[number]

export interface SemanticQualityEvidence {
  scope: string
  complete: boolean
  authoritative: boolean
  reason: string | null
  integrity_status?: string
  integrity_freshness?: string
  technical_integrity_verified?: boolean
  topology_evidence_status?: string
  graph_generation?: unknown
  [key: string]: unknown
}

export interface SemanticInvariantCheck {
  code: string
  status: string
  count: number | null
  samples: unknown[]
  reason?: string
}

export interface SemanticQualityReport {
  schema_version: string
  evaluator_version: string
  evidence: SemanticQualityEvidence
  population: Record<string, unknown>
  layers: Record<SemanticQualityLayerName, Record<string, unknown>>
  hard_invariants: {
    status: string
    measured_pass: boolean
    complete: boolean
    checks: SemanticInvariantCheck[]
  }
  verdict: {
    scope: string
    status: string
    authoritative_pass: boolean
    reason: string | null
  }
  limitations: string[]
}

export interface SemanticQualityResponse {
  status: string
  semantic_quality: SemanticQualityReport
}

export interface ObservedSemanticFingerprint {
  fingerprint: string
  run_count: number
  run_ids: string[]
  latest_applied_run_count: number
}

export interface GovernancePredicateRecord {
  label: string
  lifecycle: 'canonical' | 'provisional'
  definition: string
  direction: string
  symmetric: boolean | null
  signatures: Record<string, unknown>[]
  support_count: number
  samples: Record<string, unknown>[]
  confidence: number
  provenance: Record<string, unknown>
}

export interface GovernanceIdentityDecision {
  kind: 'type_correction' | 'distinct' | 'ambiguous_review'
  decision_id: string
  candidate_id?: string
  candidate_ids?: string[]
  left_id?: string
  right_id?: string
  previous_type?: string
  corrected_type?: string
  possible_types?: string[]
  reason: string
  judge_model: string
  prompt_version: string
  semantic_policy_fingerprint: string
  created_at: string
}

export interface SemanticGovernanceResponse {
  status: string
  current_semantic_policy_fingerprint: string
  materialized_graph_generation: string | null
  materialized_semantic_policy_fingerprint: string | null
  materialization_source: string | null
  observed_semantic_policy_fingerprints: ObservedSemanticFingerprint[]
  observed_run_count: number
  runs_without_fingerprint: number
  latest_applied_run_count: number
  latest_applied_runs_without_fingerprint: number
  superseded_completed_run_count: number
  rebuild_required: boolean
  predicate_registry: {
    counts: { canonical: number; provisional: number; total: number }
    records: GovernancePredicateRecord[]
  }
  identity_decisions: {
    counts: {
      type_correction: number
      distinct: number
      ambiguous_review: number
      total: number
    }
    records: GovernanceIdentityDecision[]
  }
}

export function runSemanticQualityAudit(): Promise<SemanticQualityResponse> {
  return apiFetch<SemanticQualityResponse>('/quality/semantic', {
    method: 'POST',
    body: JSON.stringify({}),
    timeoutMs: 30 * 60_000,
  })
}

export function getSemanticGovernance(): Promise<SemanticGovernanceResponse> {
  return apiFetch<SemanticGovernanceResponse>('/quality/semantic/governance')
}

// ── P1: detect-drift ─────────────────────────────────────────────────────────
export interface DriftFinding {
  finding_id: string
  detector: string
  severity: string
  subject: { doc_path: string | null; block_id: string | null }
  message: string
  evidence_claim_ids: string[]
}
export interface DriftResponse {
  status: string
  schema_version: string
  vault_name: string
  vault_root: string
  ran_at: string
  findings: DriftFinding[]
  counts: Record<string, number>
  total: number
}
export function detectDrift(corpusRoot: string): Promise<DriftResponse> {
  return apiFetch<DriftResponse>('/detect-drift', {
    method: 'POST',
    body: JSON.stringify({ corpus_root: corpusRoot }),
  })
}

// ── P1: companion review queue ──────────────────────────────────────────────--
export interface CompanionReviewItem {
  candidate_id?: string
  id?: string
  kind?: 'node' | 'relation'
  type?: string
  title?: string
  confidence?: number | null
  reason?: string
  candidate?: Record<string, unknown>
  pinned_proposal?: Record<string, unknown>
  source_evidence?: {
    source_path?: string | null
    block_id?: string | null
    byte_start?: number | null
    byte_end?: number | null
    content_hash?: string | null
    excerpt?: string | null
    excerpt_truncated?: boolean
  }
  [k: string]: unknown
}
export interface ReviewQueueResponse {
  status: string
  items: CompanionReviewItem[]
}
export function getReviewQueue(): Promise<ReviewQueueResponse> {
  return apiFetch<ReviewQueueResponse>('/review-queue')
}
export type ReviewAction = 'commit' | 'discard' | 'merge'
export type ReviewBatchAction = 'commit' | 'discard'
export function resolveReview(candidateId: string, action: ReviewAction): Promise<unknown> {
  return apiFetch('/resolve-review', {
    method: 'POST',
    body: JSON.stringify({ candidate_id: candidateId, action }),
  })
}
export interface ReviewBatchResponse {
  status: string
  resolved: number
  skipped: number
  errors: { id: string; error: string }[]
}
export function resolveReviewBatch(
  candidateIds: string[],
  action: ReviewBatchAction,
): Promise<ReviewBatchResponse> {
  return apiFetch<ReviewBatchResponse>('/review-queue/batch', {
    method: 'POST',
    body: JSON.stringify({ candidate_ids: candidateIds, action }),
  })
}

// ── P2: curation jobs ────────────────────────────────────────────────────────
export interface CurationJob {
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'error'
  label: string
  params: Record<string, unknown>
  progress: string
  result: Record<string, unknown> | null
  error: string | null
  created_at: number
  started_at: number | null
  finished_at: number | null
}
export interface JobsSnapshot {
  status: string
  summary: { total: number; queued: number; running: number; done: number; error: number; active: boolean }
  jobs: CurationJob[]
}
export function getCurationJobs(kind?: string): Promise<JobsSnapshot> {
  return apiFetch<JobsSnapshot>(`/curation/jobs${qs({ kind })}`)
}

// ── P4: continuous curation loop (scheduler) ────────────────────────────────--
// The serve process auto-submits PROPOSE/DETECT sweeps (reconcile-propose +
// detect-drift) as the user ingests — propose/detect ONLY, never a destructive op.
// This surfaces the loop so the user SEES it working.
export interface SweepOutcome {
  at: number
  submitted: string[]
  reason: string
}
export interface SchedulerStatus {
  status: string
  enabled: boolean
  quiet_debounce_s: number
  min_interval_s: number
  last_ingest_at: number | null
  last_sweep_at: number | null
  last_sweep_outcome: SweepOutcome | null
  // float epoch when a sweep could next fire, OR a human string
  // ("disabled" | "waiting for ingest").
  next_eligible: number | string
  sweep_pending: boolean
  // the loop's OWN recent auto sweeps (trigger=scheduler), most-recent first.
  recent: CurationJob[]
}
export function getScheduler(): Promise<SchedulerStatus> {
  return apiFetch<SchedulerStatus>('/curation/scheduler')
}

// ── P2: reconcile run + status ──────────────────────────────────────────────--
export interface JobSubmitResponse {
  status: string
  job: CurationJob
}

export function companionTriage(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/curation/companion-triage', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

// ── ADR 0017: predicate upkeep ──────────────────────────────────────────────--
export type PredicateMapping = 'exact_match' | 'inverse_of' | 'sub_property_of'
export type PredicateStatus = 'auto' | 'confirmed' | 'queued' | 'rejected'

export interface PredicateEvidence {
  counts?: Record<string, number>
  shared_pairs?: {
    subject: string
    object: string
    same_order: number
    swapped_order: number
  }[]
  sample_claim_ids?: Record<string, string[]>
  [k: string]: unknown
}

export interface PredicateAliasRecord {
  id: string
  subject_predicate: string
  mapping: PredicateMapping
  object_predicate: string
  confidence: number
  justification: string
  evidence: PredicateEvidence
  judge_model: string
  votes: Record<string, unknown>
  status: PredicateStatus
  created_at: string
}

export interface PredicateUpkeepSnapshot {
  status: string
  vocabulary_size: number
  records: Record<PredicateStatus, PredicateAliasRecord[]>
  counts: Record<PredicateStatus, number>
  last_propose: CurationJob | null
  last_apply: CurationJob | null
  worker_active: boolean
}

export function getPredicateUpkeep(): Promise<PredicateUpkeepSnapshot> {
  return apiFetch<PredicateUpkeepSnapshot>('/upkeep/predicates')
}
export function predicateUpkeepPropose(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/upkeep/predicates/propose', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}
export function predicateUpkeepApply(jobId?: string): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/upkeep/predicates/apply', {
    method: 'POST',
    body: JSON.stringify(jobId ? { job_id: jobId } : {}),
  })
}
export function predicateUpkeepConfirm(recordId: string): Promise<{ status: string; record: PredicateAliasRecord }> {
  return apiFetch(`/upkeep/predicates/${encodeURIComponent(recordId)}/confirm`, {
    method: 'POST',
  })
}
export function predicateUpkeepReject(recordId: string): Promise<{ status: string; record: PredicateAliasRecord }> {
  return apiFetch(`/upkeep/predicates/${encodeURIComponent(recordId)}/reject`, {
    method: 'POST',
  })
}

export function reconcilePropose(type?: string): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/reconcile/propose', {
    method: 'POST',
    body: JSON.stringify({ type }),
  })
}
export function reconcileApply(type?: string): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/reconcile/apply', {
    method: 'POST',
    body: JSON.stringify({ type }),
  })
}
export interface ReconcileStatus {
  status: string
  last_propose: CurationJob | null
  last_apply: CurationJob | null
  queue_count: number
  authority_count: number
  worker_active: boolean
}
export function getReconcileStatus(jobId?: string): Promise<ReconcileStatus | JobSubmitResponse> {
  return apiFetch(`/reconcile/status${qs({ job_id: jobId })}`)
}
export function getJob(jobId: string): Promise<{ status: string; job: CurationJob }> {
  return apiFetch(`/reconcile/status${qs({ job_id: jobId })}`)
}

// ── P2: reconcile review queue ──────────────────────────────────────────────--
export interface ReconcileQueueEntry {
  cluster_id: string
  type: string
  confidence: number
  corroboration: string
  canonical_id: string
  reason: string
  member_ids: string[]
  members: { id: string; title: string }[]
  lanes: string[]
}
export function getReconcileQueue(): Promise<{ status: string; entries: ReconcileQueueEntry[] }> {
  return apiFetch('/reconcile/queue')
}
export function reconcileConfirm(clusterId: string): Promise<unknown> {
  return apiFetch('/reconcile/review/confirm', {
    method: 'POST',
    body: JSON.stringify({ cluster_id: clusterId }),
  })
}
export function reconcileReject(clusterId: string): Promise<unknown> {
  return apiFetch('/reconcile/review/reject', {
    method: 'POST',
    body: JSON.stringify({ cluster_id: clusterId }),
  })
}

// ── P2: authority / equivalence records ─────────────────────────────────────--
export interface AuthorityRecord {
  cluster_id: string
  canonical_id: string
  canonical_name: string
  member_ids: string[]
  variants: string[]
  exact_match_pairs: [string, string][]
  verdict: string
  confidence: number
  provenance: Record<string, unknown>
}
export function getAuthority(): Promise<{ status: string; records: AuthorityRecord[] }> {
  return apiFetch('/authority')
}
export function authorityUnmerge(clusterId: string): Promise<unknown> {
  return apiFetch('/authority/unmerge', {
    method: 'POST',
    body: JSON.stringify({ cluster_id: clusterId }),
  })
}

// ── P3: in-process rebuild / heal / reembed (fresh-graph swap) ───────────────--
// These run on the daemon's ONE handle: a fresh graph is built at a tmp path while
// the daemon keeps serving from the live handle, then close→replace→reopen swap at
// the end. The handle is unavailable only for the swap instant.
export interface RebuildPhase {
  phase: string
  files_done?: string[]
  current_file?: string | null
  started_at?: string
  completed_at?: string
  sha256?: string
  nodes_done?: number
  nodes_total?: number
  semantic_gate?: SemanticRebuildGate
  [k: string]: unknown
}
export interface SemanticRebuildGateCheck {
  code: string
  status: 'passed' | 'failed'
  source_status: string
  count: number | null
  reason: string | null
  samples: unknown[]
}
export interface SemanticRebuildGate {
  schema_version: string
  status: 'passed' | 'failed'
  swap_allowed: boolean
  registered_codes: string[]
  checks: SemanticRebuildGateCheck[]
  failed_codes: string[]
}
export interface RebuildStatus {
  status: string
  running: boolean
  worker_active: boolean
  last_rebuild: CurationJob | null
  last_heal: CurationJob | null
  last_reembed: CurationJob | null
  last_rollback: CurationJob | null
  phase: RebuildPhase
}
export function startRebuild(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/curation/rebuild', { method: 'POST' })
}
export function startRollback(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/curation/rollback', { method: 'POST' })
}
export function startHeal(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/curation/heal', { method: 'POST' })
}
export function startReembed(): Promise<JobSubmitResponse> {
  return apiFetch<JobSubmitResponse>('/curation/reembed', { method: 'POST' })
}
export function getRebuildStatus(
  kind?: 'rebuild' | 'rollback' | 'heal' | 'reembed',
): Promise<RebuildStatus> {
  return apiFetch<RebuildStatus>(`/curation/rebuild/status${qs({ kind })}`)
}
