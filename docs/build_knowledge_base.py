#!/usr/bin/env python3
"""Generate the Okto Neuron knowledge base + hub from the live markdown sources.

Self-contained output (no external assets, works offline). Re-run after editing
any source doc to keep the rendered knowledge base in sync.

    uv run python docs/build_knowledge_base.py

Outputs:
    docs/index.html                 — top-level documentation hub
    docs/knowledge-base/index.html  — full source map, content rendered inline, themed
"""
from __future__ import annotations

import html
import json
import os
import posixpath
import re
import tempfile
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Durable sources may contain copied or agent-generated raw tags. Never execute
# those tags inside the aggregate offline knowledge base.
md = MarkdownIt("gfm-like", {"html": False}).disable("linkify").enable("table")

# The knowledge base renders the project's durable product/documentation corpus,
# not every Markdown file in the checkout.  Test fixtures, scratch handoffs, and
# agent/runtime instructions are intentionally outside this registry.  Keeping
# the discovery scope explicit lets the generator fail when a durable source is
# added without being curated into THEMES.
KB_ROOT_SOURCES = {
    "README.md",
    "RFC.md",
    "frontend/README.md",
    "packaging/README.md",
}
KB_SOURCE_DIRS = ("docs",)
KB_IGNORED_SOURCE_PARTS = {".venv", "node_modules", "output", "__pycache__"}
KB_EXCLUDED_SOURCES = {
    ".github/ and .githooks/": "automation and enforcement machinery, not reading material",
    ".scratchpad/": "ephemeral working state and handoffs",
    "docs/*.html": "reading surfaces generated from sources, except the separately verified walkthrough",
    "frontend_dist/": "generated wheel-ready UI output",
    "tests/": "fixtures and test corpora, not product documentation",
}

# ── Theme map: (theme, blurb, [(relative path, title, one-line summary)]) ──────
# Order = reading order. Paths are relative to the repo root.
THEMES: list[tuple[str, str, str, str, list[tuple[str, str, str]]]] = [
    (
        "design-bible", "📜", "Design bible",
        "The locked design and the requirements everything else serves.",
        [
            ("RFC.md", "RFC — Okto Neuron (written as Marginalia) v1.0",
             "The authoritative design: motivation, locked 5-primitive schema, positioning vs Basic Memory / Anytype."),
            ("docs/requirements-v1.md", "Requirements v1",
             "The v1 requirement set the RFC is held against."),
            ("README.md", "Project README",
             "Workspace-facing overview, install, and command surface."),
        ],
    ),
    (
        "schema", "🧬", "Schema & provenance",
        "The closed primitive set and the atomic provenance unit underneath it.",
        [
            ("docs/adr/0001-rejected-models.md", "ADR 0001 — rejected models",
             "Embedding, extraction, and NER model candidates considered and rejected, with the "
             "later GLiNER retirement recorded in a lifecycle disposition."),
            ("docs/adr/0002-chunking-and-provenance.md", "ADR 0002 — chunking & provenance",
             "Hybrid provenance (byte-exact text, two-layer binary); structure-aware "
             "ranged chunking sized from the embedder; overlap-free storage. "
             "Largely superseded by ADR 0003 (keeps D1/D2/D6/D8)."),
            ("docs/adr/0003-provenance-is-a-span-not-a-node.md",
             "ADR 0003 — provenance is a span, not a node",
             "Partially implemented proposal: SourceSpan and span-based provenance shipped "
             "additively, but Block remains one of six stored support nodes and query-time "
             "source-context machinery remains. The destructive schema cut was not accepted."),
            ("docs/adr/0004-within-batch-entity-resolution.md",
             "ADR 0004 — within-batch entity resolution",
             "Judge batch siblings, not just the committed store: a candidate-vs-candidate "
             "merge-judge pass collapses same-file twins (alice/agent:alice), gated by the "
             "0.82 band + same-type with the edge-connected guard; plus a dedicated "
             "judge_temperature (0.2) for stable verdicts."),
            ("docs/adr/0005-claim-entity-bridge-edges.md",
             "ADR 0005 — Claim→entity bridge edges",
             "Reify-and-link: a Claim now mints edges to the entities it asserts — "
             "rdf:subject (Claim → subject, always), rdf:object (Claim → object entity, "
             "when not a literal), plus schema:mentions (Document → entity) as a de-orphaning "
             "safety net. Pure edge addition (no schema change) that fuses the provenance and "
             "semantic layers. (Its in-place backfill migration was later retired by ADR 0007 — "
             "it corrupted populated graphs; heals now route through kg rebuild.)"),
            ("docs/adr/0006-source-link-every-entity.md",
             "ADR 0006 — source-link every entity",
             "Fixes the root cause of orphaned entities: schema:mentions (Document → entity) "
             "becomes node-derived and is minted LIVE at the remember() commit chokepoint for "
             "every committed entity (not just the claim-derived backfill), via one shared "
             "ensure_source_mentions helper that the heal path reuses. Realizes ADR 0005's F4 "
             "safety net; pure edge addition, no schema change. Healing the existing vault "
             "dropped degree-0 sourced primitive entities 301 → 0."),
            ("docs/adr/0007-rebuild-from-trust-root-retire-bulk-migrations.md",
             "ADR 0007 — rebuild from trust root, retire bulk migrations",
             "Bulk in-place migration (kg migrate bridge-edges / whole-store ensure_source_mentions) "
             "corrupts a populated Ladybug graph's edge src/dst+adjacency at the storage layer "
             "(deterministic 0→1227; the content-addressed id stays ground truth, not a reader bug, "
             "incremental ingest safe). Fix: kg rebuild re-extracts the markdown trust root via the "
             "full remember() pipeline into a FRESH graph + atomic swap (proven 1227→0 at full scale); "
             "the corrupting bulk migration is retired, the safe per-commit source-link kept."),
            ("docs/adr/0008-retroactive-entity-reconciliation.md",
             "ADR 0008 — retroactive entity reconciliation",
             "Consolidate already-committed look-alike entities AFTER ingest. Option A "
             "(default): query-time OFF-GRAPH consolidation — equivalence classes live in "
             ".marginalia/authority/index.json (skos:exactMatch, not owl:sameAs) and fold at "
             "read time via search_claims; writes ZERO graph nodes/edges (Authority is a Node, "
             "so a bulk add_node IS the ADR-0007 corruption), reversible by dropping the entry. "
             "Option B (periodic): fold aliases into a FRESH graph via kg rebuild. Auto-merge "
             "gate = same ∧ conf≥0.9 ∧ positive corroboration; recall lanes (widened embedding "
             "band, Jaro-Winkler ordering not substring, email-handle decomposition) funnel to "
             "the reused conservative judge; thin handle entities recalled lexically."),
            ("docs/adr/0009-curation-control-plane.md",
             "ADR 0009 — curation control plane (Marginalia as an application)",
             "Accepted control-plane decision for a continuously curated application: "
             "Companion.remember owns ingest-time curation, while debounced proposal-only sweeps "
             "run through daemon-owned jobs. Under ADR 0034, each immutable per-vault runtime "
             "owns its queue, writer lock, and leased Ladybug handle, so one vault's work cannot "
             "retarget or freeze another. "
             "Equivalence remains reversible and off-graph; rebuild/heal routes irreversible "
             "topology changes through a fresh graph. The shipped seven-view SPA exposes the "
             "review-first Curation surface (Overview, Review queues, and Maintenance), and the "
             "P3 topology-collapse heal shipped. P5 autonomy and P6 optional in-graph folding/MCP "
             "curation remain future product evolution."),
            ("docs/adr/0010-reconciler-recall-overhaul.md",
             "ADR 0010 — reconciler recall + judge + corroboration overhaul "
             "(accepted; P1–P5 shipped, P6 deferred)",
             "The shipped overhaul treats missed candidate recall as the dominant reconciliation "
             "defect without weakening precision: order-insensitive token subsets, length-gated "
             "lexical edges, corroboration grades, distinguishing-edge vetoes, preserved judge "
             "reasons, and real-judge band tests now cover the path. The CI recall harness and "
             "candidate lanes are live. P6 abstention and shared-neighbour experiments remain "
             "explicitly gated future product work, not release debt."),
            ("docs/adr/0011-subgraph-first-answer-assembly.md",
             "ADR 0011 — subgraph-first answer assembly (accepted; opt-in)",
             "The graph-native path is shipped behind llm.ask.enable_subgraph: it builds a "
             "relevance-capped ego graph, renders typed nodes/relationships/claims, supports "
             "multi-hop control, and fetches byte-validated source blocks on demand behind its "
             "coverage gate. Evaluation did not justify a universal default flip, so block mode "
             "remains the conservative default and ADR 0028 defines the bounded efficient-hybrid "
             "policy when subgraph mode is enabled."),
            ("docs/adr/0011-implementation-plan.md",
             "ADR 0011 implementation plan — subgraph-first answer assembly",
             "Closed file-level implementation and evaluation record for ADR 0011. The subgraph "
             "builder/renderer, multi-hop walk, coverage and abstention fallback, byte-path "
             "revalidation, on-demand source fetch, and typed configuration surface all shipped. "
             "The universal default flip was rejected by evidence: block mode stays conservative, "
             "while ADR 0028 records the opt-in efficient-hybrid resolution."),
            ("docs/adr/0012-user-configurable-ask-retrieval-policy.md",
             "ADR 0012 — user-configurable ask retrieval policy (accepted; implemented)",
             "Ask retrieval is a shipped typed policy across vault configuration, the HTTP Config "
             "API, and the Query UI: subgraph enablement, seed and hop controls, degree and token "
             "budgets, relationship filters, source-block policy, and coverage threshold. "
             "Responses report the effective retrieval trace. The conservative block default and "
             "the opt-in bounded blend from ADR 0028 are intentional current behaviour."),
            ("docs/adr/0018-differentiated-retrieval-defaults.md",
             "ADR 0018 — differentiated retrieval defaults: recall k=10 vs ask k=20 (accepted)",
             "ask and recall used to share seed_k=8. A grounded golden eval (142 corpus-grounded "
             "Q/A, scored deterministically on the key because the LLM judge panel was κ=0.14) "
             "found the dominant gap was GENERATION (21%), root-caused to ask under-retrieving vs "
             "recall: ask k=8 < recall k=10, so the #1 gold doc could fall outside ask's one-shot "
             "seed window. Decision splits the defaults on the axis that distinguishes them — "
             "whether the caller can re-query: recall stays k=10 (feeds an agent that re-queries; "
             "keep the first pass tight/cheap), ask goes to k=20 (one-shot; must seed wide enough "
             "in a single pass), and the ask answerer runs enable_thinking=false (temp-0.7 thinking "
             "inferred facts when context was thin). Defaults only — ADR 0012's per-request seed_k "
             "still overrides. Validated end-to-end: answer-presence 0.677->0.754, neg-control "
             "abstention 0.917 held (no hallucination-for-recall trade); recall@20/extraction "
             "unchanged. 0.754 is the k-axis ceiling, now bounded by recall@20 (~11% gold outside "
             "top-20) — further gains need reranking/hybrid + extraction, not a bigger k. "
             "Updated 2026-06-21: a controlled 5-sample study (15 runs, same daemon, temp 0.7) "
             "CONFIRMED k=20 as a real win over k=8 (terse 0.635->0.720, +0.085, McNemar p=0.0018, "
             "bootstrap 95% CI [+0.035,+0.140] excluding 0, neg-guard 0.883->0.917). The extractive "
             "_ASK_SYSTEM prompt was tested and REVERTED: terse->extractive +0.019, CI [-0.006,+0.046] "
             "spanning 0, McNemar p=0.22 — within the temp-0.7 noise floor (the earlier single-run "
             "0.754->0.792 win was cross-daemon noise). Block-dump ceiling is ~0.72 (controlled); k=40 "
             "overflows the answerer (infeasible); block-dump (~0.72) still beats subgraph (0.6) only "
             "because the graph is extraction-thin. Single-run temp-0.7 answer-presence is NOT reliable; "
             "the deterministic floor (recall@k) + this multi-sample study are the trustworthy signals. "
             "MCP ask fixed (k=8->20 + dropped forced subgraph) and a new CLI ask command added."),
            ("docs/adr/0019-graph-native-answer-assembly.md",
             "ADR 0019 — graph-native answer assembly (superseded; empirically resolved)",
             "Historical measurement plan that isolated the graph-native gap into assembly and "
             "upstream extraction. The program ran to completion: ranking, reach, granularity, and "
             "seed-quota experiments did not break the plateau; ADR 0028 shipped the bounded "
             "efficient-hybrid assembly, and ADRs 0030–0031 raised extraction and retention. The "
             "finale reached statistical answer-quality parity at 6.24× fewer context tokens, so "
             "the phased plan is experimental provenance rather than active release work."),
            ("docs/adr/0020-dense-fact-predicate-vocabulary.md",
             "ADR 0020 — dense-fact predicate vocabulary: lock predicates before minting (accepted)",
             "Prerequisite (GN-8.5) for ADR 0019 Phase-2 dense-fact extraction. Locks the small, "
             "stable canonical predicate set BEFORE the first dense-fact Claim is minted, because "
             "the predicate bakes into semantic_claim_id (ADR 0016) and seeds the SSSOM alias ledger "
             "(ADR 0017) — churn after a re-ingest is expensive. Six canonical predicates, each a "
             "Claim with a literal object (O_literal, v1): has_version, has_config (has_setting folds "
             "in), has_status, has_value (the catch-all for a labelled scalar), has_measurement, and "
             "row/column-aware table_cell. All lowercase snake_case via normalize_predicate; synonyms "
             "(version_is->has_version, setting->has_config, ...) fold through skos:exactMatch in the "
             "ADR 0017 ledger. Preserves the 5-primitive closed schema (these are Claims, not a sixth "
             "primitive) and Claim->Block byte anchoring. Deterministic minting (GN-9/10) emits only "
             "these; the optional LLM pass (GN-12) must map to them. New predicates require a "
             "follow-up ADR, never ad-hoc, to keep re-ingest stable. Accepted."),
            ("docs/adr/0021-dynamic-truncation-escalation-extraction.md",
             "ADR 0021 — dynamic truncation-escalation extraction (auto mode) (accepted)",
             "Single-pass extraction silently lost the tail facts of any block the model truncated "
             "(finish_reason=length cuts the JSON mid-object; parse keeps what completed, drops the "
             "rest) — measured at ~15-20% of dense blocks, exactly the dense factual surfaces behind "
             "the ADR 0019 63% extraction-gap. The fix is an 'auto' mode, now the effective default: "
             "per block, inspect the baseline finish_reason — 'stop' accepts as-is (no extra cost), "
             "'length' re-runs THAT block through the enumerate (Mode B) pipeline (the unexpected-"
             "finish guard refuses to escalate any other terminal since escalation only fixes "
             "truncation, recording it for awareness; a second-order guard logs CRITICAL if the "
             "escalation itself still truncates). Byte-anchoring unchanged. Measured: extraction "
             "completeness 0.80->0.95, subgraph answer-presence +0.035, but block-dump parity NOT "
             "reached so GN-16 stays deferred; cost ~9x on escalated blocks only (the ~80-85% that "
             "finish on stop pay nothing extra). 'baseline'/'enumerate' remain selectable. Accepted."),
            ("docs/adr/0013-durable-candidate-ledger.md",
             "ADR 0013 — durable candidate ledger and pre-commit curation boundary "
             "(accepted; core shipped)",
             "The ingest boundary now records append-only run, candidate, comparison, commit-plan, "
             "and commit records before graph writes. Safe resume, HTTP summaries, and UI "
             "inspection expose the lifecycle, and graph commits consume the curated plan. Making "
             "every legacy review artifact a pure ledger projection remains future hardening, not "
             "a 0.0.40 release gate."),
            ("docs/adr/0014-multi-vault-serve.md",
             "ADR 0014 — multi-vault serve: per-connection MCP vault selection (accepted)",
             "One daemon, many vaults. The original MCP contract added an optional ?vault= selector (registered "
             "name or loopback-only absolute path); every tool resolves its vault per-call through "
             "resolve_vault_selector — the single seam where a future multi-tenant build maps "
             "token -> tenant -> allowed vaults. ADR 0034 supersedes the process-global active-vault "
             "and writer-lock parts: browser selection is tab-local, every vault has an immutable "
             "runtime and writer lock, and a bounded lease-aware VaultPool evicts only idle handles. "
             "The init_vault MCP tool remains; switch_vault is compatibility-only."),
            ("docs/adr/0017-judge-driven-graph-upkeep.md",
             "ADR 0017 — judge-driven continuous graph upkeep: predicate canonicalization (accepted)",
             "The living-graph maintenance loop: an embedding-clustered, bias-mitigated LLM judge "
             "canonicalizes synonym predicates (same/inverse/narrower/distinct verdicts, symmetric "
             "prompting, negative cache), decisions persist as SSSOM-shaped alias records, folds "
             "apply at ingest, query, and heal time, and the debounced scheduler proposes "
             "continuously while humans gate anything not clearly safe."),
            ("docs/adr/0016-relation-claim-deduplication.md",
             "ADR 0016 — relation/claim semantic identity & deduplication (accepted)",
             "Why the same fact minted N duplicate Claims and what fixed it: Claim identity becomes "
             "the semantic triple (S, P, O); re-mentions corroborate one Claim via extra PROV edges "
             "and a corroborations facet. Tier E0 within-run exact edge collapse, Tier E1 exact "
             "store reconcile, plain-edge commit guard, and a retroactive heal pass that collapses "
             "legacy duplicates and re-keys claims to semantic ids."),
            ("docs/adr/0015-ingest-throughput-curation-concurrency-and-prefilter.md",
             "ADR 0015 — ingest throughput: curation concurrency, prefilter, batching & resume (accepted)",
             "Why one file took 5+ hours on a laptop and what fixed it: D1 bounded-concurrency "
             "curation fan-out, D2 deterministic prefilter (predicate demotion, near-dup literal "
             "collapse, established-entity fast path), D3 observability (daemon log file, per-call "
             "timing/tokens, ledger v2), D4 block-keyed batched curation (one excerpt per ~10 "
             "verdicts, strict parse + single-call fallback), D5 streamed per-batch ledgering and "
             "ledger-native mid-file resume, plus the run-B-derived endpoint pre-gate that stops "
             "curating relations whose endpoints already died. Validated live: node curation "
             "67.3 min -> 26 min and 6.18M -> 1.17M prompt tokens on the same file."),
            ("docs/adr/0022-smarter-merge-judge.md",
             "ADR 0022 — a smarter merge judge (accepted; implemented)",
             "The merge judge now uses select framing, load-bearing neighbourhood context, "
             "cluster support, and guarded integral-correction behaviour, with regression "
             "coverage for the resulting decisions. Temporal validity keeps genuine conflict "
             "dated rather than permanently parked. Later tuning is product-quality work, not an "
             "open release gate; the five-primitive schema and rebuildable trust-root model stay "
             "unchanged."),
            ("docs/adr/0023-incremental-content-hash-ingest.md",
             "ADR 0023 — incremental ingest via content-hash (accepted)",
             "Incremental ingest now skips extraction for blocks whose content hash already has "
             "live Claims, extracts only new or changed blocks, and retires removed-block claims "
             "while preserving graph equivalence with full re-extraction. The feature is default-on "
             "through IngestConfig (ingest.incremental), environment-overridable, and reinforces "
             "the markdown trust root while shrinking the synchronous extraction window."),
            ("docs/adr/0024-sub-chunk-diff-ingestion.md",
             "ADR 0024 — sub-chunk diff ingestion (accepted)",
             "Sub-chunk ingest extends ADR 0023 by diffing changed blocks on line boundaries and "
             "extracting only replace/insert hunks, byte-anchored within the parent block — about "
             "30–40× less LLM work for a common one-line edit. Removed source facts remain "
             "durably detached; corrected facts supersede prior Claims and leave an auditable "
             "temporal trail. The feature is default-on through IngestConfig (ingest.subchunk) "
             "and remains environment-overridable."),
            ("docs/adr/0025-continuous-folder-monitoring.md",
             "ADR 0025 — continuous folder monitoring (accepted)",
             "Global multi-vault polling watch loop: a single asyncio background task (started at daemon "
             "boot alongside the curation scheduler) stat-walks registered roots every poll_interval_s, "
             "detects settled changes via per-vault manifest sidecars (mtime/size/sha256), and enqueues "
             "them to the ingest queue. Polling with quiet_debounce_s settle-detection and min_interval_s "
             "anti-thrash floor. Change detection is graph-free (no vault open until a settled change "
             "triggers ingest). watchfiles rejected as load-bearing (unreliable on Docker/NFS); polling "
             "is the canonical path. Flag-gated (folder_watch.enabled default false). Reuses ADR 0014 "
             "VaultPool and ADR 0023/0024 incremental ingest. Accepted."),
            ("docs/adr/0026-rebuild-honors-source-selection-denylist.md",
             "ADR 0026 — rebuild honors the source-selection denylist (accepted)",
             "The trust-root rebuild (_deterministic_rebuild_files in cli/kg.py) bypassed the "
             "folder-watch denylist, so kg rebuild / the curation rebuild re-ingested junk the "
             "incremental path already excludes (CLAUDE.md, dot-files, tracking index pages, "
             "sync-queue/sweep-dates scaffolding, tracking/tracking duplicates) — a dirtier graph "
             "than the ingest it should reproduce, breaking ADR 0007's re-derive invariant. Decision: "
             "factor a shared basename-level predicate _is_denylisted_relpath (server/_folder_watch.py) "
             "that BOTH _iter_watch_files and _deterministic_rebuild_files call, so watch and rebuild "
             "source-selection cannot drift. Rebuild evaluates it relative to each source-key root under "
             ".marginalia/sources/ (leading hash segment stripped), and the .marginalia internal-dir rule "
             "is deliberately excluded from the shared predicate so the durable sub-tree isn't rejected "
             "wholesale. Verified live: demo-vault rebuild dropped exactly 13 junk Documents (64->51), all "
             "legitimate content preserved. Accepted."),
            ("docs/adr/0027-guard-integral-corrections.md",
             "ADR 0027 — guard the best-effort integral-corrections phase in rebuild (accepted)",
             "The ADR-0022 Lever 5 integral-corrections phase in Companion.remember (cross-file "
             "auto-supersede) ran post-commit but UNGUARDED, so one bad _apply_supersede throw "
             "propagated out and aborted a whole multi-file trust-root rebuild (observed live: "
             "reference-eval rebuild failed at file 34/48) — and the curation job runner's per-job "
             "isolation in _drain swallowed the traceback, persisting only a terse job.error. "
             "Decision: guard the correction phase two ways — fine-grained around each "
             "_apply_supersede in supersede_contradicted (_incremental.py) and an outer net around "
             "the whole phase in remember() (__init__.py), so a bad supersede or a failing "
             "correction judge is logged and skipped while committed claims stand; RebuildInterrupted "
             "still propagates. Add logger.exception at the throw sites in _jobs.py (_drain) and "
             "cli/kg.py (_build_fresh_graph) so the real traceback + cause chain surface instead of a "
             "one-liner. Consequences: rebuilds are resilient to a bad supersede and failures are "
             "diagnosable. Verified: reference-eval rebuild-2 on the fixed code cleared file 34 and "
             "completed 48/48. Accepted."),
            ("docs/adr/0028-efficient-hybrid-answer-path.md",
             "ADR 0028 — efficient-hybrid answer path: always-blend a budgeted source excerpt (accepted)",
             "Closes the eight-experiment graph-native program. The pivotal Tier-2 discovery: the "
             "subgraph arm's ~69% was propped up by its abstention fallback reading 30-66k tokens "
             "UNBOUNDED (median winning escalation ~50k = block-dump scale), so 'pure graph-native' "
             "was never pure — and volume, ranking, reach, granularity, and seed quotas were all "
             "net-zero (98, 98, 98, 95, 98, [82 confounded], 99). Owner reframe: the north star is "
             "block-parity accuracy at a fraction of block tokens. Decision: every subgraph "
             "answer context = ego-graph render + a budgeted source excerpt (default 6,000 tokens, "
             "policy > vault config > code default), snippets re-ranked by IDF-weighted query-term "
             "coverage before the trim, abstention escalation bounded at 2x with one re-ask, no "
             "unbounded read anywhere in the path; seed-diversity quotas ship default-gated; the "
             "daemon boot-warms provider deps so a venv re-sync can't 500 a running eval. Result: "
             "119/142 (83.8%) vs block 121/142 (85.2%), McNemar p=0.81 = parity, at 17.5% of block "
             "tokens (mean 8,522 vs 48,673, max hard-bounded 14,045). Subgraph stays default-off "
             "by accepted current policy; any future default change needs a new decision and fresh "
             "multi-run evidence. Accepted."),
            ("docs/adr/0029-recover-from-checkpoint-on-wal-quarantine.md",
             "ADR 0029 — recover from the last checkpoint on WAL quarantine (WAL-durability, P0, accepted)",
             "Ladybug folds the WAL (graph.lbug.wal) into the main checkpoint (graph.lbug) only on a "
             "clean Database.close(), and the daemon holds the database open for its whole life, so a "
             "kill -9 mid-write leaves a torn WAL next to an INTACT checkpoint. bootstrap_vault_graph "
             "already detected the corruption but _quarantine_graph moved the good checkpoint aside "
             "together with the torn WAL and booted a FRESH empty graph — the store came back with 0 "
             "claims despite a fully readable checkpoint (observed live: the E2/E3 reference-eval replay "
             "quarantined a 60MB checkpoint holding 5389 claims). Prod exposure was real: the live "
             "demo-vault daemon writes the WAL on every ingest, so a crash mid-write would have booted it "
             "empty. Decision: _recover_from_corruption quarantines ONLY the torn WAL/sidecars "
             "(_quarantine_sidecars keeps graph.lbug in place), retries the open, and recovers the last "
             "checkpoint (recovered_mode='checkpoint'); only if the main file itself is torn does it fall "
             "back to the empty graph (recovered_mode='empty'). recovered_from_corruption stays True in "
             "both modes and /health reports degraded with a mode-accurate reason. Verified end-to-end on "
             "the real replay files: recovery yields 5389 claims (was 0). kg rebuild from markdown "
             "(ADR 0007) remains the full-recovery path for WAL-only writes. Accepted."),
            ("docs/adr/0030-multi-sample-union-extraction.md",
             "ADR 0030 — multi-sample union extraction (samples = k) (accepted)",
             "A single extractor draw at a non-zero temperature is a lossy sample of what a Block "
             "asserts — a colder draw drops a fact a warmer sibling would have caught, which is a "
             "different miss from truncation (ADR 0021). LLMExtractor gains an opt-in llm.extraction."
             "samples knob: default None→1 short-circuits to the single-draw path, BYTE-IDENTICAL to "
             "the pre-union extractor. With samples=k it runs k independent draws per block and UNIONS "
             "their candidate sets (nodes deduped on candidate_id, claims/edges on the (type,src,dst|"
             "literal) grain) before the companion's dedup/curation. The static system prefix is shared "
             "across draws so a prefix-caching provider reuses it; at temp 0 the draws coincide and the "
             "union is a deliberate no-op. A bounded truncation-retry re-draws exactly once any draw "
             "that hit finish_reason=length with zero parsed candidates. Offline bench (reference-eval, "
             "qwen3.6-35b, temp 0.7): samples=2 lifted per-block emission 19→22 of 39 target facts. "
             "Default-off (samples=1); finale ships samples=2 on the reference-eval vault only. "
             "Amended 2026-09-03: block text sent to extraction now passes through "
             "wrap_untrusted_block() with <document> delimiters plus explicit untrusted-data system-"
             "prompt framing (finding 3.16); the judge prompt gets the equivalent <excerpt> framing. "
             "Accepted."),
            ("docs/adr/0031-retention-curator-excerpt-and-edge-endpoint-promotion.md",
             "ADR 0031 — retention: full-window curator excerpt + edge-endpoint anchor promotion (accepted)",
             "Two retention leaks from the reference-eval audit — grounded facts extracted but never "
             "committed. E2: the curator source excerpt was block[:9000] of a ~12k block, so any "
             "candidate anchored in the final ~3k chars (markdown table cells especially) drew a "
             "structurally-guaranteed 'excerpt does not contain the literal' queue verdict; "
             "CURATION_SOURCE_EXCERPT_LIMIT=16000 now spans the full extraction window, byte-identical "
             "per block so prompt-prefix cache reuse is unchanged. E3: ADR 0019 Fix A promotes a dead "
             "Claim SUBJECT as a _salience:low anchor, but a topology edge with one dead ENDPOINT still "
             "dead-lettered (grounded group-email / stack-label nodes dying); the endpoint gate now "
             "promotes an edge's single dead endpoint under the same guards (extracted this run, queued "
             "not contradicted, no accepted same-entity sibling — a queued exact-title duplicate stays "
             "with the dedup remap machinery). Both-endpoints-dead edges still dead-letter; promotion "
             "only lets the relation curator judge the edge, never mints an orphan. Part of the finale "
             "retention lift (E 70.0%→82.3%, +16 facts). Accepted."),
            ("docs/adr/0032-guard-correction-judge-non-dict-reply.md",
             "ADR 0032 — guard the correction judge against a non-dict JSON reply (accepted)",
             "ADR 0027 made the integral-corrections PHASE best-effort, but the correction-judge CALL "
             "itself had two fatal reply shapes (the non-dict-reply family). make_correction_judge expected an "
             "'index': N object, but the 35B judge sometimes replies with a bare scalar ('2' not "
             "{'index': 2}), especially on a truncated finish_reason=length reply — json.loads('2') is "
             "a bare int and int(...).get(...) raises. Fix: a parse-shape guard branches on the parsed "
             "type (bare int IS the index; dict carries it under 'index'; bool excluded before the int "
             "leg so True isn't read as index 1; anything else → no correction), and a best-effort "
             "call guard wraps correction_judge(...) so an unexpected raise is logged with claim "
             "context and the single correction is skipped, ingest continues (RebuildInterrupted still "
             "propagates). A terse valid judge reply is now read correctly, not just defended against. "
             "Accepted."),
            ("docs/adr/0033-eval-harness-pins-one-judge-model.md",
             "ADR 0033 — the eval harness pins ONE judge model across both A/B arms (accepted)",
             "A subgraph-vs-block A/B is only trustworthy if a single, recorded judge grades both "
             "arms; a judge resolved per-arm (or a provider lineup shift mid-run) confounds the verdict "
             "with a judge change rather than the arms. The A/B orchestrator (eval-run.sh) pins ONE "
             "judge for both arms, resolved exactly once at run start: --judge-model auto is resolved a "
             "single time via semantic_judge._select_chat_model, then that concrete model id grades "
             "both arms, and the RESOLVED id (never the literal 'auto') is recorded in the run manifest "
             "so the reproducibility pin is honest. The finale A/B graded both arms with one pinned "
             "judge: subgraph 122/142 vs block 120/142 (statistical parity, p=0.80) at 6.24x fewer "
             "answer tokens — parity attributable to the arms, not a mid-run judge shift. Accepted."),
            ("docs/adr/0034-application-daemon-browser-and-multi-vault-contract.md",
             "ADR 0034 — application daemon, direct loopback UI, and client-scoped vaults (accepted)",
             "Makes serve an application-level daemon with direct loopback browser access and an "
             "explicit --no-open mode; replaces process-global UI vault selection with immutable "
             "per-vault runtimes so ingest and curation can continue independently; and defines "
             "ownership, lease, and deletion rules for safely managing multiple vaults."),
            ("docs/adr/0035-dynamic-litellm-parameter-editor.md",
             "ADR 0035 — dynamic LiteLLM parameter editor (accepted)",
             "Makes provider/model selection sufficient by omitting optional generation parameters "
             "until the user adds them. One backend contract turns LiteLLM's model-aware capability "
             "data into typed, security-filtered descriptors; the frontend renders a generic advanced "
             "editor, and runtime request shaping rechecks the same capabilities. Per-step maps support "
             "inheritance and null tombstones while legacy typed YAML remains readable."),
            ("docs/adr/0036-bounded-parallel-chunk-extraction.md",
             "ADR 0036 — bounded parallel chunk extraction (accepted)",
             "Adds llm.extraction.max_concurrent (effective default 1, range 1..32) so independent "
             "chunk LLM round trips can overlap. Results are folded in source order while embedding, "
             "deduplication, ledger writes, curation, and graph commits stay single-threaded; per-worker "
             "trace context and serialized event callbacks keep observability correct. A sliding window "
             "re-reads this execution-policy value during an active extraction, so limit changes apply "
             "without restarting or mixing semantic model settings within one file."),
            ("docs/adr/0037-batched-parallel-embeddings.md",
             "ADR 0037 — batched, bounded parallel embeddings (accepted)",
             "Adds a native multi-input embedding boundary shared by ingest candidates, relationship "
             "Claims, and vectors-only re-embedding. Requests are batch-first and optionally overlap "
             "through a bounded, source-ordered scheduler; cardinality, response indices, and vector "
             "width are validated before use. Batch size and concurrent-batch limits are live execution "
             "policy and never invalidate the vector space."),
            ("docs/adr/0038-configurable-overlapping-ingest-chunks.md",
             "ADR 0038 — configurable overlapping ingest chunks (accepted)",
             "Adds configurable byte-window size and line-safe overlap while preserving exact "
             "provenance. Blocks record their chunking policy so re-ingestion never mixes stale "
             "and current source partitions. Amended 2026-09-03: the overlap:chunk_size ratio is "
             "capped at 50% (_MAX_CHUNK_OVERLAP_RATIO in ingest/markdown.py) after a measured 0.95 "
             "ratio produced a ~20x block-count blowup on a 2.4MB fixture (finding 3.15)."),
            ("docs/adr/0039-ingest-technical-correctness.md",
             "ADR 0039 — verified ingest commits and honest outcomes (accepted)",
             "Defines the technical-correctness contract exposed by the LOTR audit: one reusable "
             "graph-integrity auditor, durable write-ahead executable commit plans, idempotent "
             "per-operation receipts, extraction-unit journaling and targeted retry, separate "
             "lifecycle/result quality, integrity writer fencing, completeness-aware diagnostics, "
             "and fresh-graph recovery. Predicate and entity-quality policy remains a later track."),
            ("docs/adr/0040-semantic-graph-quality.md",
             "ADR 0040 — stable identities, governed predicates, and useful relations (proposed)",
             "Defines the conceptual-quality companion to ADR 0039: lossless surface normalization, "
             "explicit primitive-type adjudication, reversible canonical identity, a governed "
             "canonical/alias/inverse/narrower/provisional predicate lifecycle, source-grounded "
             "relationship usefulness gates, cross-document reconciliation, semantic fingerprints, "
             "and multi-corpus quality metrics and acceptance gates."),
            ("docs/adr/0041-pluggable-graph-backend.md",
             "ADR 0041 — pluggable graph backend, selectable connectors (proposed)",
             "Makes the graph backend a user-selectable connector pinned once at vault creation, "
             "while the Markdown vault stays the sole trust root regardless of engine. Splits "
             "GraphStore CRUD from a new IndexStore retrieval port, adds generation-scoped reads "
             "and writes so a rebuild's staged generation can never collide with a live write, and "
             "gates three backends of different shape (Ladybug, Okto Grafx, Neo4j) on the same "
             "contract suite and a shepherd-run LoCoMo parity check; AWS Neptune is stretch, not "
             "gating. Its step-by-step implementation plan is kept internally."),
            ("docs/adr/0042-entity-resolution-blocking-is-recall.md",
             "ADR 0042 — entity-resolution blocking is a recall stage (accepted)",
             "Makes ingest-time entity-resolution blocking disjunctive: a pair reaches the merge "
             "judge if the embedding band fires OR any reused lexical lane from "
             "reconcile/candidates.py fires (length-gated Jaro-Winkler, shared surname, ordered/"
             "unordered token-subset), never vetoed by another lane's silence, per the standard "
             "blocking-optimizes-recall/classification-optimizes-precision split (Christen 2012; "
             "Papadakis et al. 2020). Also folds diacritics into discovery_surface_key and "
             "rebuckets cross-type identity adjudication onto that key. Evidence: short given "
             "names and their fuller forms were never compared by any ingest-time tier because a "
             "single embedding key is degenerate for short names."),
            ("docs/adr/0043-agent-facing-mcp-surface.md",
             "ADR 0043 — the agent-facing MCP surface (accepted)",
             "Opens the MCP surface to agents without opening the filesystem to them. Adds a "
             "names-only list_vaults tool and an optional per-call vault= argument on ask/explore/"
             "remember (registry names only, always validated, and losing precedence to the "
             "connection's ?vault= selector, which is echoed back as vault_override_ignored). "
             "Flattens 13 AskRetrievalPolicy knobs plus include_sources onto ask with "
             "None-means-inherit semantics, emitting vault-relative provenance only. Makes a "
             "degraded answer announce itself: synthesis_status is always present (ok/empty/"
             "provider_error/truncated/abnormal_stop) alongside finish_reason and "
             "native_finish_reason, with unmapped-reason detection done where litellm's own map "
             "is in scope. Marks each assembled source block with its relative path, byte ranges "
             "and total file size so the model can say the excerpts do not cover a period. Caps "
             "vault names at 255 characters and sanitises OSError (and pathlib's ELOOP "
             "RuntimeError) at the vault-resolution and pool-lease seams behind a new "
             "vault_unavailable code, so no filesystem path reaches a client."),
            ("docs/adr/0044-rename-to-okto-neuron.md",
             "ADR 0044 — rename Marginalia to Okto Neuron (accepted)",
             "The product ships as Okto Neuron from 0.3.0: package, command and MCP server "
             "okto-neuron, import okto_neuron, OKTO_NEURON_* env, ~/.okto-neuron app home, under "
             "ELv2 with the Okto Labs addendum (releases up to 0.2.0 stay Apache 2.0). Legacy "
             "names are read until 0.5 through one compat module, vaults never move, and every "
             "stored or exported marginalia name is kept on purpose. Credits and archives João "
             "Braga's Okto Neuron MVP."),
        ],
    ),
    (
        "storage", "🗄️", "Storage & vault model",
        "How the graph is persisted and why markdown stays the trust root.",
        [
            ("docs/semantic-writer-inventory.md", "Semantic writer inventory",
             "ADR 0039 Phase 3 exit artifact: every add_node/add_edge call site in "
             "src/okto_neuron, classified as planner-routed or non-semantic infrastructure "
             "with its safety argument. Machine-enforced by an AST-scanning guard test that "
             "fails when an unclassified writer appears."),
        ],
    ),
    (
        "backends", "🔌", "Graph storage backends",
        "The pluggable GraphStore backends a vault can be created against.",
        [
            ("docs/backends/grafx.md", "Grafx backend (default)",
             "The multi-process MVCC embedded backend added in M4; default graph "
             "backend since a post-M6 owner decision retired its experimental gate (D-94)."),
            ("docs/backends/ladybug.md", "Ladybug backend",
             "The single-writer, server-side-schema embedded graph store; the resolved "
             "backend for any legacy vault predating a storage.backend key."),
            ("docs/backends/neo4j.md", "Neo4j backend",
             "The Bolt/Cypher server backend added in M5, generation-tag staging, "
             "vault_id-scoped, not experimental."),
        ],
    ),
    (
        "retrieval", "🔎", "Retrieval & read path",
        "Unified retrieval, grounded answering, and the measured graph-native evolution.",
        [
            ("docs/read-path-and-unified-retrieval.md", "Read path & unified retrieval — design",
             "The read-path problem, the sketches, and the unified search() design."),
            ("docs/eval-gates.md",
             "Eval gates — the canonical Definition-of-Done for the graph-native arc",
             "The three ship gates every graph-native task is judged against: Gate 1 the "
             "deterministic floor (provenance byte-hash + hard-recall@k + extraction-"
             "completeness + claim-coverage over a frozen vault — the ONLY CI blocker), "
             "Gate 2 multi-run A/B significance (the 5-condition REAL rule, N>=5, laptop-"
             "only, where flip-default decisions are made), Gate 3 the cost-budget tier tag "
             "(tier-1 read-path vs tier-2 re-ingest). Plus the 2026-07-07 run-validity "
             "guards: daemon boot-warm of provider dependencies and the harness abort on "
             "3 consecutive ask transport failures, born from an A/B that measured nothing "
             "because a venv re-sync pruned litellm under the running daemon."),
            ("docs/quality-gate-runbook.md",
             "Quality gate runbook — the three tiers",
             "The one command that answers 'did I break knowledge quality?'. Tier 0 is the "
             "unchanged CI deterministic floor, re-run in-process for laptop parity. Tier 1 is "
             "the new laptop gate: a suite-owned daemon live-ingests the semantic-adversarial "
             "corpus into a fresh vault — the only leg that can see an extraction regression — "
             "then gates citation byte-verify, hard-recall@k, extraction-completeness, "
             "must_contain and negative-control abstention on COUNTS against a committed "
             "baseline. Tier 2 reports the semantic judge as an advisory band that never fails "
             "the gate, and hardens only after a measured kappa >= 0.60 against human labels. "
             "The Tier 1 baseline is PROVISIONAL and re-mints when ADR 0040 adjudication closes."),
        ],
    ),
    (
        "architecture", "🏗️", "Architecture & planning",
        "The autonomous companion build plan and coverage tracking.",
        [
            ("docs/autonomous-architecture-plan.md", "Autonomous companion — implemented plan",
             "Historical Phases A–G plan with a current closeout for the shipped companion loop."),
            ("docs/chunking-implementation-plan.md", "Chunking & provenance — historical plan",
             "Closed implementation record for the ADR 0002 chunking program, with superseded "
             "and retained decisions identified at the top."),
            ("docs/architecture/cluster-4a-seams.md", "Cluster 4A seams",
             "Architectural seam notes."),
            ("docs/pulse-coverage-matrix.md", "Pulse coverage matrix",
             "How Pulse KG capabilities map onto Marginalia."),
            ("docs/onboarding-plan.md", "Onboarding plan",
             "The current application-first install flow, plus optional provider-first onboarding "
             "when an installer is explicitly asked to preseed a vault."),
            ("packaging/README.md", "Service templates",
             "Scope and support boundary for the opt-in operating-system service templates."),
        ],
    ),
    (
        "web-ui", "🖥️", "Web UI & interface",
        "The built SPA over the /api/v1 contract, how to run it, and its security posture.",
        [
            ("docs/web-ui.md", "Web UI",
             "The seven views (Query · Add · Logs · Browse · Graph · Curation · Config), the REST contract, build/serve workflow, and security posture."),
            ("frontend/README.md", "Web UI developer guide",
             "The concise frontend layout, seven-view map, development workflow, and explicit mock-mode contract."),
            ("docs/understanding/README.md", "Understanding walkthrough guide",
             "How to open and navigate the self-contained conceptual and mechanical walkthrough."),
        ],
    ),
    (
        "observability", "📡", "Observability",
        "How to see what the model layer actually did, per call, across runs.",
        [
            ("docs/observability.md", "LLM observability — MLflow GenAI traces",
             "Optional, env-gated export of every LLM call to an MLflow tracking server: what a "
             "span carries, why an absent finish reason is recorded as absent rather than as a "
             "clean stop, and the versioned worker payload that carries the provider's native "
             "response across the killable-subprocess boundary. Includes how the three CLI "
             "pseudo-providers now emit the canonical usage line, surface an honest finish "
             "reason, and report the sampling parameters they cannot carry."),
            ("docs/remote-providers.md", "Remote providers — egress gate and the ChatGPT provider",
             "Why allow_remote: false did not stop four drivers that reach hosted APIs with no "
             "api_base to inspect, and how that is now refused by driver name; plus the "
             "experimental, opt-in chatgpt provider in full — the eleven-key request whitelist that "
             "silently discards structured output and every sampler, the ~1.6K-token Codex "
             "preamble, the absent per-token cost, and why its credential file must never be "
             "shared with codex or pi."),
        ],
    ),
    (
        "benchmarks", "📊", "Benchmarks",
        "How Okto Neuron was measured on public benchmarks, and the published data behind it.",
        [
            ("docs/benchmarks/locomo.md", "LoCoMo benchmark — methodology and results",
             "Categories 1-4 of the LoCoMo long-conversation benchmark, judged by a local "
             "qwen3.8-27b with the Mem0/MemGPT judge prompt: every arm's macro and pooled score, "
             "paired McNemar comparisons with discordant counts, the measured noise between runs "
             "with identical settings, confounds, known defects, and what is not claimed. All "
             "numbers come from the published okto-neuron-locomo-bundle.json."),
        ],
    ),
]

# ── Curated external references (standards + reference systems) ────────────────
EXTERNAL: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Standards & vocabularies", [
        ("PROV-O — Provenance Ontology", "https://www.w3.org/TR/prov-o/",
         "The W3C provenance model behind Claim → Block → Activity → Agent."),
        ("SKOS — Simple Knowledge Organization System", "https://www.w3.org/TR/skos-reference/",
         "broader/narrower Concept hierarchies — the abstraction layer."),
        ("Dublin Core (DCMI Terms)", "https://www.dublincore.org/specifications/dublin-core/dcmi-terms/",
         "Descriptive metadata vocabulary."),
        ("BIBFRAME", "https://www.loc.gov/bibframe/",
         "Library of Congress bibliographic framework."),
        ("CiTO — Citation Typing Ontology", "https://sparontologies.github.io/cito/current/cito.html",
         "Typed citation relationships."),
        ("W3C Web Annotation Data Model", "https://www.w3.org/TR/annotation-model/",
         "oa:Annotation — the model behind Annotation support type."),
    ]),
    ("Reference systems (retrieval)", [
        ("Neo4j GraphRAG (VectorCypherRetriever)", "https://neo4j.com/docs/neo4j-graphrag-python/current/",
         "Vector search → seed nodes → Cypher traversal in one call — our exact shape."),
        ("Graphiti (Zep)", "https://github.com/getzep/graphiti",
         "Vector + BM25 + graph BFS fused via RRF; closest analog to the unified read path."),
        ("HippoRAG", "https://github.com/OSU-NLP-Group/HippoRAG",
         "Personalized PageRank over a graph indexing entities + passages."),
    ]),
]

CSS = """
:root{--bg:#0b0e14;--panel:#11151f;--panel2:#161b29;--ink:#e6e9f0;--dim:#8b93a7;
--line:#222a3d;--accent:#7aa2f7;--green:#9ece6a;--amber:#e0af68;--red:#f7768e;
--purple:#bb9af7;--cyan:#7dcfff;--mono:'SFMono-Regular',ui-monospace,Menlo,Consolas,monospace;}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);line-height:1.65;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background-image:radial-gradient(900px 600px at 88% -5%,#161d31 0%,transparent 60%),
radial-gradient(800px 700px at -10% 35%,#141a2b 0%,transparent 55%);}
code,.mono{font-family:var(--mono)}
nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:18px;
padding:14px 28px;border-bottom:1px solid var(--line);background:rgba(11,14,20,.85);backdrop-filter:blur(10px)}
.logo{font-weight:700;font-size:17px}.logo .dot{color:var(--accent)}
.sub{color:var(--dim);font-size:12px;font-family:var(--mono)}
.navlinks{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.navlinks a{text-decoration:none;color:var(--dim);font-size:13px;padding:7px 15px;border-radius:999px;
border:1px solid var(--line);background:var(--panel);transition:.2s}
.navlinks a:hover{color:#fff;border-color:var(--accent)}
.navlinks a.cta{color:#fff;border-color:var(--accent);background:linear-gradient(180deg,#1b2540,#141a2b)}
.navprog{position:absolute;left:0;bottom:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--purple));width:0}
"""

KB_CSS = CSS + """
.layout{display:grid;grid-template-columns:268px 1fr;max-width:1320px;margin:0 auto;gap:0}
@media(max-width:900px){.layout{grid-template-columns:1fr}.toc{display:none}}
.toc{position:sticky;top:53px;align-self:start;height:calc(100vh - 53px);overflow-y:auto;
padding:26px 18px 60px 28px;border-right:1px solid var(--line)}
.toc h4{font-family:var(--mono);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent);margin:18px 0 8px}
.toc h4:first-child{margin-top:0}
.toc a{display:block;text-decoration:none;color:var(--dim);font-size:13px;padding:4px 8px;border-radius:7px;border-left:2px solid transparent}
.toc a:hover{color:var(--ink);background:var(--panel2)}
.toc a.active{color:#fff;border-left-color:var(--accent);background:var(--panel2)}
.content{padding:34px 44px 100px;min-width:0}
@media(max-width:900px){.content{padding:28px 22px 80px}}
.kb-hero{margin-bottom:14px}
.kb-hero h1{font-size:32px;font-weight:700}.kb-hero h1 .hl{color:var(--accent)}
.kb-hero p{color:var(--dim);font-size:16px;max-width:760px;margin-top:8px}
.kb-hero p b{color:var(--ink)}
.kb-hero .scope{font-size:12.5px;max-width:1000px}.kb-hero .scope code{color:var(--cyan)}
.theme{margin-top:48px;scroll-margin-top:70px}
.theme-head{display:flex;align-items:center;gap:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.theme-head .ic{font-size:24px}
.theme-head h2{font-size:23px;font-weight:680}
.theme-head .blurb{color:var(--dim);font-size:14px;margin-left:auto;text-align:right;max-width:50%}
.doc{margin-top:22px;border:1px solid var(--line);border-radius:14px;background:var(--panel);overflow:hidden;scroll-margin-top:70px}
.doc-head{display:flex;align-items:flex-start;gap:14px;padding:16px 20px;background:var(--panel2);cursor:pointer;user-select:none}
.doc-head:hover{background:#1a2030}
.doc-head .chev{font-family:var(--mono);color:var(--accent);transition:transform .25s;margin-top:3px}
.doc.open .doc-head .chev{transform:rotate(90deg)}
.doc-head .meta{flex:1;min-width:0}
.doc-head .dt{font-weight:660;font-size:16px}
.doc-head .ds{color:var(--dim);font-size:13px;margin-top:3px}
.doc-head .src{font-family:var(--mono);font-size:11px;color:var(--accent);background:#0e1420;border:1px solid var(--line);
padding:3px 9px;border-radius:7px;text-decoration:none;white-space:nowrap;margin-top:2px}
.doc-head .src:hover{border-color:var(--accent);color:#fff}
.doc-body{display:none;padding:6px 30px 26px;border-top:1px solid var(--line)}
.doc.open .doc-body{display:block}
/* rendered markdown */
.md h1,.md h2,.md h3,.md h4{font-weight:680;line-height:1.3;margin:22px 0 10px}
.md h1{font-size:25px;color:#fff;border-bottom:1px solid var(--line);padding-bottom:8px}
.md h2{font-size:21px;color:#fff}.md h3{font-size:17px;color:var(--cyan)}.md h4{font-size:15px;color:var(--purple)}
.md p{margin:10px 0;color:#cfd5e3}
.md a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(122,162,247,.3)}
.md a:hover{border-bottom-color:var(--accent)}
.md ul,.md ol{margin:10px 0 10px 24px}.md li{margin:5px 0;color:#cfd5e3}
.md strong{color:#fff}.md em{color:var(--purple)}
.md code{font-family:var(--mono);font-size:13px;background:#0e1420;border:1px solid var(--line);padding:1px 6px;border-radius:5px;color:var(--cyan)}
.md pre{background:#0a0d15;border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto;margin:12px 0}
.md pre code{background:none;border:none;padding:0;color:#d8dee9;font-size:12.5px;line-height:1.55}
.md blockquote{border-left:3px solid var(--amber);background:var(--panel2);padding:8px 16px;margin:12px 0;border-radius:0 8px 8px 0;color:var(--dim)}
.md table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
.md th{background:var(--panel2);color:#fff;font-weight:660}
.md tr:nth-child(even) td{background:rgba(255,255,255,.015)}
.md hr{border:none;border-top:1px solid var(--line);margin:20px 0}
/* external refs */
.ext{margin-top:22px}
.ext h3{font-size:15px;color:var(--cyan);font-family:var(--mono);margin:18px 0 10px}
.extgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.extgrid{grid-template-columns:1fr}}
.extcard{display:block;text-decoration:none;padding:14px 16px;border:1px solid var(--line);border-radius:11px;background:var(--panel2);transition:.2s}
.extcard:hover{border-color:var(--accent);transform:translateY(-2px)}
.extcard .et{color:#fff;font-weight:620;font-size:14px}.extcard .et:after{content:" ↗";color:var(--accent);font-size:12px}
.extcard .ed{color:var(--dim);font-size:12.5px;margin-top:4px}
footer{padding:50px 28px 80px;text-align:center;color:var(--dim);font-size:13px;border-top:1px solid var(--line);margin-top:50px}
footer .mono{color:var(--accent);margin-top:6px}
"""

# Shared rendered-markdown styling, reused by the knowledge base and standalone
# doc pages (RFC etc.).
MD_CSS = """
.md h1,.md h2,.md h3,.md h4{font-weight:680;line-height:1.3;margin:22px 0 10px}
.md h1{font-size:25px;color:#fff;border-bottom:1px solid var(--line);padding-bottom:8px}
.md h2{font-size:21px;color:#fff}.md h3{font-size:17px;color:var(--cyan)}.md h4{font-size:15px;color:var(--purple)}
.md p{margin:10px 0;color:#cfd5e3}
.md a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(122,162,247,.3)}
.md a:hover{border-bottom-color:var(--accent)}
.md ul,.md ol{margin:10px 0 10px 24px}.md li{margin:5px 0;color:#cfd5e3}
.md strong{color:#fff}.md em{color:var(--purple)}
.md code{font-family:var(--mono);font-size:13px;background:#0e1420;border:1px solid var(--line);padding:1px 6px;border-radius:5px;color:var(--cyan)}
.md pre{background:#0a0d15;border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto;margin:12px 0}
.md pre code{background:none;border:none;padding:0;color:#d8dee9;font-size:12.5px;line-height:1.55}
.md blockquote{border-left:3px solid var(--amber);background:var(--panel2);padding:8px 16px;margin:12px 0;border-radius:0 8px 8px 0;color:var(--dim)}
.md table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
.md th{background:var(--panel2);color:#fff;font-weight:660}
.md tr:nth-child(even) td{background:rgba(255,255,255,.015)}
.md hr{border:none;border-top:1px solid var(--line);margin:20px 0}
"""

# Standalone rendered-doc page (RFC, etc.) — clean single-column reading view.
DOC_CSS = CSS + MD_CSS + """
.doc-wrap{max-width:860px;margin:0 auto;padding:40px 28px 60px}
.doc-wrap .md h1:first-child{margin-top:8px}
footer{margin-top:60px;padding-top:24px;border-top:1px solid var(--line);text-align:center;color:var(--dim);font-size:12.5px}
footer .mono{color:var(--accent)}
"""

# Standalone roadmap page — high-level, list-based, deliberately not a Kanban.
ROADMAP_CSS = CSS + """
.dot-s{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 auto}
.s-done .dot-s,.dot-s.s-done{background:var(--green)} .dot-s.s-doing{background:var(--cyan)}
.dot-s.s-next{background:var(--amber)} .dot-s.s-later{background:var(--purple)}
.rm-wrap{max-width:920px;margin:0 auto;padding:40px 28px 70px}
.rm-hero h1{font-size:36px;font-weight:700}
.rm-hero p{color:var(--dim);font-size:16px;max-width:720px;margin-top:10px}
.rm-hero .upd{display:block;font-family:var(--mono);font-size:11.5px;margin-top:10px;color:var(--dim)}
.rm-hero code{font-family:var(--mono);color:var(--cyan)}
/* milestone arc */
.ms-row{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:34px 0 8px}
@media(max-width:760px){.ms-row{grid-template-columns:1fr}}
.ms{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:linear-gradient(180deg,var(--panel2),var(--panel));position:relative}
.ms.s-done{border-top:3px solid var(--green)} .ms.s-doing{border-top:3px solid var(--cyan)} .ms.s-later{border-top:3px solid var(--purple)}
.ms-h{display:flex;align-items:baseline;gap:8px}.ms-h b{font-size:16px;color:#fff}
.ms-when{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--dim)}
.ms p{color:var(--dim);font-size:13px;margin:8px 0}
.ms-ct{font-family:var(--mono);font-size:11px;color:var(--accent)}
/* status sections (lists, not cards) */
.rm-sec{margin-top:40px}
.rm-sec h2{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:680;padding-bottom:10px;border-bottom:1px solid var(--line)}
.rm-sec h2 .sec-sub{margin-left:auto;font-family:var(--mono);font-size:11.5px;color:var(--dim);font-weight:400}
.rm-list{list-style:none;margin:14px 0 0;padding:0}
.rm-list li{display:flex;align-items:flex-start;gap:12px;padding:13px 4px;border-bottom:1px solid rgba(255,255,255,.04)}
.rm-list .ri{font-size:17px;line-height:1.3;flex:0 0 auto}
.rm-list .rl{flex:1;min-width:0}
.rm-list .rl b{color:#fff;font-size:15px;font-weight:620}
.rm-list .rl .rd{display:block;color:var(--dim);font-size:13px;margin-top:3px;line-height:1.5}
.rm-ms-tag{font-family:var(--mono);font-size:10.5px;color:var(--accent);border:1px solid rgba(122,162,247,.35);border-radius:20px;padding:2px 9px;white-space:nowrap;flex:0 0 auto;margin-top:2px}
footer{margin-top:60px;padding-top:24px;border-top:1px solid var(--line);text-align:center;color:var(--dim);font-size:12.5px}
.mono{font-family:var(--mono);color:var(--accent)}
"""

HUB_CSS = CSS + """
.wrap{max-width:1040px;margin:0 auto;padding:0 28px}
.hero{padding:84px 0 30px;text-align:center}
.hero .emoji{font-size:54px;margin-bottom:14px}
.hero h1{font-size:44px;font-weight:700;max-width:840px;margin:0 auto 16px;line-height:1.12}
.hero h1 .hl{color:var(--accent)}
.hero p{color:var(--dim);font-size:18px;max-width:680px;margin:0 auto}
.hero p b{color:var(--ink)}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:50px 0}
@media(max-width:760px){.cards{grid-template-columns:1fr}}
.card{display:block;text-decoration:none;color:inherit;padding:26px;border-radius:16px;border:1px solid var(--line);
background:linear-gradient(180deg,var(--panel2),var(--panel));transition:.22s}
.card:hover{border-color:var(--accent);transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,.35)}
.card .ic{font-size:30px}
.card h3{font-size:19px;margin:12px 0 6px;font-weight:680}
.card h3 .hl{color:var(--accent)}
.card p{color:var(--dim);font-size:14px}
.card .go{margin-top:14px;font-family:var(--mono);font-size:12px;color:var(--accent)}
.card.wide{grid-column:1 / -1}
.meta{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;padding-bottom:40px}
.pill{display:inline-block;padding:5px 13px;border-radius:8px;font-family:var(--mono);font-size:12px;border:1px solid var(--line);background:var(--panel2);color:var(--dim)}
footer{padding:40px 0 80px;text-align:center;color:var(--dim);font-size:13px;border-top:1px solid var(--line)}
footer .mono{color:var(--accent)}
"""


STATUS_ORDER = ["doing", "next", "done", "later"]
STATUS_HEAD = {
    "doing": ("🔄", "Working on now", "What's actively in flight."),
    "next": ("⏭️", "Up next", "Prioritized future work; not yet scheduled."),
    "done": ("✅", "Recently shipped", "Landed and verified."),
    "later": ("🌌", "Later / known gaps", "Named, not yet scheduled."),
}


def build_roadmap_page() -> str:
    """Standalone, lightweight roadmap page from docs/roadmap.json.

    A high-level read — milestone arc + 'now / next / shipped / later' feature
    lists. Deliberately NOT a Kanban card manager.
    """
    data = json.loads((DOCS / "roadmap.json").read_text(encoding="utf-8"))
    milestones = data.get("milestones", [])
    tracks = data.get("tracks", [])
    updated = data.get("updated", "")
    ms_label = {m["id"]: m["label"] for m in milestones}

    flat: list[dict] = []
    for tr in tracks:
        for it in tr.get("items", []):
            flat.append({**it, "track_icon": tr.get("icon", "•")})

    # ── milestone arc (high-level look-back / look-forward) ─────────────
    arc = []
    for m in milestones:
        n = sum(1 for it in flat if it.get("milestone") == m["id"])
        arc.append(
            f'<div class="ms s-{m.get("status","later")}">'
            f'<div class="ms-h"><b>{html.escape(m["label"])}</b>'
            f'<span class="ms-when">{html.escape(m.get("target",""))}</span></div>'
            f'<p>{html.escape(m.get("blurb",""))}</p>'
            f'<span class="ms-ct">{n} items</span></div>'
        )
    arc_html = f'<div class="ms-row">{"".join(arc)}</div>'

    # ── feature lists by status (no cards, no checklists) ───────────────
    blocks = []
    for st in STATUS_ORDER:
        items = [it for it in flat if it.get("status") == st]
        if not items:
            continue
        ic, label, sub = STATUS_HEAD[st]
        rows = "".join(
            f'<li><span class="ri">{html.escape(str(it.get("track_icon", "•")))}</span>'
            f'<span class="rl"><b>{html.escape(it["title"])}</b>'
            f'<span class="rd">{html.escape(it.get("detail",""))}</span></span>'
            f'<span class="rm-ms-tag">{html.escape(ms_label.get(it.get("milestone",""), ""))}</span></li>'
            for it in items
        )
        blocks.append(
            f'<section class="rm-sec s-{st}"><h2><span class="dot-s s-{st}"></span>'
            f'{ic} {html.escape(label)}<span class="sec-sub">{html.escape(sub)}</span></h2>'
            f'<ul class="rm-list">{rows}</ul></section>'
        )

    nav = (
        '<nav><div><div class="logo">Okto Neuron<span class="dot">.</span></div>'
        '<div class="sub">roadmap · what we did &amp; where we\'re going</div></div>'
        '<div class="navlinks">'
        '<a href="index.html">← Hub</a>'
        '<a href="knowledge-base/index.html">📚 Knowledge Base</a>'
        '<a href="rfc.html">📜 RFC</a>'
        '</div></nav>'
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Okto Neuron — Roadmap</title>
<style>{ROADMAP_CSS}</style></head><body>
{nav}
<main class="rm-wrap">
  <div class="rm-hero"><h1>🗺️ Roadmap</h1>
  <p>The high-level view: the milestone arc, what we're building now, and what's next.
  Not a task tracker — the headline features only. <span class="upd">updated {html.escape(updated)} · source: <code>docs/roadmap.json</code></span></p></div>
  {arc_html}
  {"".join(blocks)}
  <footer>Generated from <span class="mono">docs/roadmap.json</span> by <span class="mono">docs/build_knowledge_base.py</span>.</footer>
</main>
</body></html>"""


def build_doc_page(rel: str, title: str, subtitle: str) -> str:
    """Render a single markdown source file as a clean, standalone reading page."""
    rendered = render_doc(ROOT / rel, output_rel="docs/rfc.html")
    nav = (
        '<nav><div><div class="logo">Okto Neuron<span class="dot">.</span></div>'
        f'<div class="sub">{html.escape(subtitle)}</div></div>'
        '<div class="navlinks">'
        '<a href="index.html">← Hub</a>'
        '<a href="knowledge-base/index.html">📚 Knowledge Base</a>'
        f'<a href="../{html.escape(rel)}">view source ↗</a>'
        '</div><div class="navprog" id="navprog"></div></nav>'
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Okto Neuron — {html.escape(title)}</title>
<style>{DOC_CSS}</style></head><body>
{nav}
<main class="doc-wrap"><article class="md">{rendered}</article>
<footer>Rendered from <span class="mono">{html.escape(rel)}</span> · re-run <span class="mono">docs/build_knowledge_base.py</span> to refresh.</footer>
</main>
<script>
const prog=document.getElementById('navprog');
addEventListener('scroll',()=>{{const h=document.documentElement;
prog.style.width=(h.scrollTop/(h.scrollHeight-h.clientHeight)*100)+'%';}},{{passive:true}});
</script>
</body></html>"""


# Paths renamed after dated records were written (ADR 0044). Those records are
# never rewritten, so a link inside them to the old path is followed to the new one.
_RENAMED_PATH_PREFIXES = {"src/marginalia/": "src/okto_neuron/"}


def _follow_renamed_path(repo_relative: str) -> str:
    for old, new in _RENAMED_PATH_PREFIXES.items():
        if repo_relative == old.rstrip("/") or repo_relative.startswith(old):
            return new + repo_relative[len(old):]
    return repo_relative


def _rebase_local_url(url: str, *, source_rel: str, output_rel: str) -> str:
    """Rebase a source-relative Markdown URL for a generated HTML location."""
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return url
    if not parsed.path:
        if parsed.fragment:
            # The KB renders many Markdown documents into one page. Namespace
            # same-document heading links so identical headings in two sources
            # cannot collide in the aggregate HTML.
            return f"#{slug(source_rel)}--{unquote(parsed.fragment)}"
        return url

    source_dir = posixpath.dirname(source_rel)
    target = _follow_renamed_path(
        posixpath.normpath(posixpath.join(source_dir, unquote(parsed.path)))
    )
    output_dir = posixpath.dirname(output_rel)
    rebased = posixpath.relpath(target, output_dir)
    # The decoded path is needed for filesystem resolution, but putting it back
    # into a URL verbatim would turn encoded ``#``/``?`` filename characters
    # into fragment/query delimiters. Re-encode the path component only.
    rebased = quote(rebased, safe="/:@-._~!$&'()*+,;=")
    return urlunsplit(("", "", rebased, parsed.query, parsed.fragment))


def _walk_tokens(tokens: list[Token]) -> Iterator[Token]:
    """Yield markdown-it tokens recursively, including inline children."""
    for token in tokens:
        yield token
        if token.children:
            yield from _walk_tokens(token.children)


def _github_heading_slug(value: str) -> str:
    """Return the GitHub-style fragment used by the source Markdown links."""
    value = html.unescape(value).strip().lower()
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"\s", "-", value) or "section"


def _heading_text(token: Token) -> str:
    """Return visible heading text, excluding Markdown link destinations."""
    if not token.children:
        return token.content
    return "".join(
        child.content
        for child in token.children
        if child.type in {"text", "code_inline", "image"}
    )


def render_doc(path: Path, *, output_rel: str) -> str:
    """Render Markdown and keep every local link relative to its source file."""
    source_rel = path.relative_to(ROOT).as_posix()
    tokens = md.parse(path.read_text(encoding="utf-8"))
    heading_counts: dict[str, int] = {}
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or tokens[index + 1].type != "inline":
            continue
        base = _github_heading_slug(_heading_text(tokens[index + 1]))
        duplicate_index = heading_counts.get(base, 0)
        heading_counts[base] = duplicate_index + 1
        heading_fragment = base if duplicate_index == 0 else f"{base}-{duplicate_index}"
        token.attrSet("id", f"{slug(source_rel)}--{heading_fragment}")
    for token in _walk_tokens(tokens):
        attr = "href" if token.type == "link_open" else "src" if token.type == "image" else None
        if attr is None:
            continue
        value = token.attrGet(attr)
        if value:
            token.attrSet(
                attr,
                _rebase_local_url(value, source_rel=source_rel, output_rel=output_rel),
            )
    return md.renderer.render(tokens, md.options, {})


def slug(rel: str) -> str:
    return rel.replace("/", "__").replace(".md", "").replace(".", "_")


def registered_kb_sources() -> list[str]:
    return [rel for *_, docs in THEMES for rel, *_ in docs]


def canonical_kb_sources() -> set[str]:
    """Return the durable source set that THEMES must curate completely."""
    sources = set(KB_ROOT_SOURCES)
    for dirname in KB_SOURCE_DIRS:
        base = ROOT / dirname
        for path in base.rglob("*.md"):
            relative_parts = path.relative_to(base).parts
            if any(part in KB_IGNORED_SOURCE_PARTS for part in relative_parts):
                continue
            sources.add(path.relative_to(ROOT).as_posix())
    return sources


def validate_source_registry() -> None:
    """Fail on missing, duplicate, or silently unregistered durable sources."""
    registered = registered_kb_sources()
    duplicates = sorted({rel for rel in registered if registered.count(rel) > 1})
    missing_files = sorted(rel for rel in registered if not (ROOT / rel).is_file())
    canonical = canonical_kb_sources()
    unregistered = sorted(canonical - set(registered))
    unexpected = sorted(set(registered) - canonical)

    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate registry entries: {', '.join(duplicates)}")
    if missing_files:
        problems.append(f"registered files missing: {', '.join(missing_files)}")
    if unregistered:
        problems.append(f"durable sources not registered: {', '.join(unregistered)}")
    if unexpected:
        problems.append(f"registered sources outside the durable scope: {', '.join(unexpected)}")
    if problems:
        raise RuntimeError("knowledge-base source registry invalid:\n- " + "\n- ".join(problems))


def validate_roadmap() -> None:
    """Fail when roadmap identifiers, statuses, or milestone references drift."""
    data = json.loads((DOCS / "roadmap.json").read_text(encoding="utf-8"))
    valid_statuses = {"done", "doing", "next", "later"}
    milestones = data.get("milestones", [])
    tracks = data.get("tracks", [])

    problems: list[str] = []

    def require_text(owner: str, field: str, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{owner} has missing or blank {field}")
            return None
        return value

    milestone_ids = [
        require_text(f"milestone #{index}", "id", entry.get("id"))
        for index, entry in enumerate(milestones, start=1)
    ]
    track_ids = [
        require_text(f"track #{index}", "id", entry.get("id"))
        for index, entry in enumerate(tracks, start=1)
    ]
    item_ids = [
        require_text(
            f"item #{item_index} in track {track.get('id')!r}",
            "id",
            item.get("id"),
        )
        for track in tracks
        for item_index, item in enumerate(track.get("items", []), start=1)
    ]

    def report_duplicates(label: str, values: list[object]) -> None:
        duplicates = sorted(
            str(value)
            for value in set(values)
            if value is not None and values.count(value) > 1
        )
        if duplicates:
            problems.append(f"duplicate {label} ids: {', '.join(duplicates)}")

    report_duplicates("milestone", milestone_ids)
    report_duplicates("track", track_ids)
    report_duplicates("item", item_ids)

    known_milestones = {value for value in milestone_ids if value is not None}
    for milestone in milestones:
        require_text(f"milestone {milestone.get('id')!r}", "label", milestone.get("label"))
        status = milestone.get("status")
        if status not in valid_statuses:
            problems.append(
                f"milestone {milestone.get('id')!r} has invalid status {status!r}"
            )

    for track in tracks:
        require_text(f"track {track.get('id')!r}", "title", track.get("title"))
        for item in track.get("items", []):
            item_label = f"{track.get('id')}/{item.get('id')}"
            require_text(f"item {item_label}", "title", item.get("title"))
            status = item.get("status")
            if status not in valid_statuses:
                problems.append(f"item {item_label} has invalid status {status!r}")
            milestone = item.get("milestone")
            if milestone not in known_milestones:
                problems.append(
                    f"item {item_label} references unknown milestone {milestone!r}"
                )
            for subtask in item.get("subtasks", []):
                subtask_status = subtask.get("status")
                if subtask_status not in valid_statuses:
                    problems.append(
                        f"subtask {item_label}/{subtask.get('title')!r} "
                        f"has invalid status {subtask_status!r}"
                    )

    if problems:
        raise RuntimeError("roadmap registry invalid:\n- " + "\n- ".join(problems))


def _markdown_heading_anchors(path: Path) -> set[str]:
    """Return GitHub-style heading fragments declared by a Markdown source."""
    tokens = md.parse(path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or tokens[index + 1].type != "inline":
            continue
        base = _github_heading_slug(_heading_text(tokens[index + 1]))
        duplicate_index = counts.get(base, 0)
        counts[base] = duplicate_index + 1
        anchors.add(base if duplicate_index == 0 else f"{base}-{duplicate_index}")
    return anchors


def validate_markdown_local_links() -> None:
    """Fail when a registered Markdown source has a broken local file/fragment link."""
    broken: list[str] = []
    markdown_anchor_cache: dict[Path, set[str]] = {}
    html_anchor_cache: dict[Path, set[str]] = {}
    for relative in registered_kb_sources():
        source = ROOT / relative
        tokens = md.parse(source.read_text(encoding="utf-8"))
        for token in _walk_tokens(tokens):
            attr = "href" if token.type == "link_open" else "src" if token.type == "image" else None
            if attr is None:
                continue
            url = token.attrGet(attr)
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
                continue
            if not parsed.path:
                if parsed.fragment:
                    anchors = markdown_anchor_cache.setdefault(
                        source,
                        _markdown_heading_anchors(source),
                    )
                    if unquote(parsed.fragment) not in anchors:
                        broken.append(f"{relative}: {url}")
                continue
            target = (source.parent / unquote(parsed.path)).resolve()
            if target.is_relative_to(ROOT) and not target.exists():
                target = ROOT / _follow_renamed_path(target.relative_to(ROOT).as_posix())
            if not target.is_relative_to(ROOT) or not target.exists():
                broken.append(f"{relative}: {url}")
                continue
            if not parsed.fragment or target.is_dir():
                continue
            fragment = unquote(parsed.fragment)
            suffix = target.suffix.lower()
            if suffix == ".md":
                anchors = markdown_anchor_cache.setdefault(
                    target,
                    _markdown_heading_anchors(target),
                )
                if fragment not in anchors:
                    broken.append(f"{relative}: {url}")
            elif suffix in {".html", ".htm"}:
                anchors = html_anchor_cache.setdefault(target, _html_anchors(target))
                if fragment not in anchors:
                    broken.append(f"{relative}: {url}")
    if broken:
        raise RuntimeError("durable Markdown contains broken local links:\n- " + "\n- ".join(broken))


class _GeneratedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls.extend(value for key, value in attrs if key in {"href", "src"} and value)
        for key, value in attrs:
            if key not in {"id", "name"} or not value:
                continue
            if value in self.ids:
                self.duplicate_ids.add(value)
            self.ids.add(value)


def _html_anchors(path: Path, contents: Mapping[Path, str] | None = None) -> set[str]:
    """Return every ``id``/``name`` anchor declared by one HTML document."""
    parser = _GeneratedLinkParser()
    rendered = contents or {}
    resolved = path.resolve()
    text = rendered[resolved] if resolved in rendered else path.read_text(encoding="utf-8")
    parser.feed(text)
    return parser.ids


def validate_generated_local_links(
    pages: list[Path],
    *,
    contents: Mapping[Path, str] | None = None,
) -> None:
    """Fail on duplicate anchors or missing local files and HTML fragments."""
    broken: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    rendered = {path.resolve(): text for path, text in (contents or {}).items()}
    for page in pages:
        resolved_page = page.resolve()
        parser = _GeneratedLinkParser()
        page_text = (
            rendered[resolved_page]
            if resolved_page in rendered
            else page.read_text(encoding="utf-8")
        )
        parser.feed(page_text)
        broken.extend(
            f"{page.relative_to(ROOT)}: duplicate anchor #{anchor}"
            for anchor in sorted(parser.duplicate_ids)
        )
        for url in parser.urls:
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc:
                continue
            if not parsed.path:
                if parsed.fragment and unquote(parsed.fragment) not in parser.ids:
                    broken.append(f"{page.relative_to(ROOT)}: {url}")
                continue
            target = (page.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(ROOT) or (target not in rendered and not target.exists()):
                broken.append(f"{page.relative_to(ROOT)}: {url}")
                continue
            if not parsed.fragment:
                continue
            fragment_target = target / "index.html" if target.is_dir() else target
            if fragment_target.suffix.lower() not in {".html", ".htm"}:
                continue
            if fragment_target not in rendered and not fragment_target.is_file():
                broken.append(f"{page.relative_to(ROOT)}: {url}")
                continue
            anchors = anchor_cache.setdefault(
                fragment_target,
                _html_anchors(fragment_target, rendered),
            )
            if unquote(parsed.fragment) not in anchors:
                broken.append(f"{page.relative_to(ROOT)}: {url}")
    if broken:
        raise RuntimeError("generated docs contain broken local links:\n- " + "\n- ".join(broken))


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace one generated page atomically without leaving a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(mode)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def build_kb() -> str:
    toc_parts: list[str] = []
    body_parts: list[str] = []

    for key, ic, title, blurb, docs in THEMES:
        toc_parts.append(f'<h4>{ic} {html.escape(title)}</h4>')
        for rel, dtitle, dsummary in docs:
            toc_parts.append(
                f'<a href="#{slug(rel)}" data-target="{slug(rel)}">{html.escape(dtitle)}</a>'
            )

        cards: list[str] = []
        for rel, dtitle, dsummary in docs:
            p = ROOT / rel
            if not p.exists():
                cards.append(
                    f'<div class="doc" id="{slug(rel)}"><div class="doc-head">'
                    f'<div class="meta"><div class="dt">{html.escape(dtitle)} '
                    f'<span style="color:var(--red)">(missing: {html.escape(rel)})</span></div></div></div></div>'
                )
                continue
            rendered = render_doc(p, output_rel="docs/knowledge-base/index.html")
            # source link relative to docs/knowledge-base/index.html → repo root is ../../
            src_href = f"../../{rel}"
            cards.append(
                f'<div class="doc" id="{slug(rel)}">'
                f'<div class="doc-head" onclick="toggleDoc(this.parentNode)">'
                f'<span class="chev">▶</span>'
                f'<div class="meta"><div class="dt">{html.escape(dtitle)}</div>'
                f'<div class="ds">{html.escape(dsummary)}</div></div>'
                f'<a class="src" href="{src_href}" onclick="event.stopPropagation()">{html.escape(rel)}</a>'
                f'</div>'
                f'<div class="doc-body"><div class="md">{rendered}</div></div>'
                f'</div>'
            )

        body_parts.append(
            f'<section class="theme" id="theme-{key}">'
            f'<div class="theme-head"><span class="ic">{ic}</span>'
            f'<h2>{html.escape(title)}</h2>'
            f'<span class="blurb">{html.escape(blurb)}</span></div>'
            f'{"".join(cards)}</section>'
        )

    # external references
    toc_parts.append('<h4>🔗 External references</h4>')
    toc_parts.append('<a href="#external" data-target="external">Standards & systems</a>')
    ext_parts = ['<section class="theme ext" id="external">'
                 '<div class="theme-head"><span class="ic">🔗</span><h2>External references</h2>'
                 '<span class="blurb">Standards and reference systems Okto Neuron builds on.</span></div>']
    for group, items in EXTERNAL:
        ext_parts.append(f'<h3>{html.escape(group)}</h3><div class="extgrid">')
        for name, url, desc in items:
            ext_parts.append(
                f'<a class="extcard" href="{html.escape(url)}" target="_blank" rel="noopener">'
                f'<div class="et">{html.escape(name)}</div>'
                f'<div class="ed">{html.escape(desc)}</div></a>'
            )
        ext_parts.append('</div>')
    ext_parts.append('</section>')
    body_parts.append("".join(ext_parts))

    exclusions = " · ".join(
        f"<code>{html.escape(path)}</code> — {html.escape(reason)}"
        for path, reason in KB_EXCLUDED_SOURCES.items()
    )

    nav = (
        '<nav><div><div class="logo">Okto Neuron<span class="dot">.</span></div>'
        '<div class="sub">knowledge base · rendered from source</div></div>'
        '<div class="navlinks">'
        '<a href="../index.html">← Hub</a>'
        '<a href="../roadmap.html">🗺️ Roadmap</a>'
        '<a href="../understanding/index.html">🧠 Understanding</a>'
        '<a href="../rfc.html">📜 RFC</a>'
        '</div><div class="navprog" id="navprog"></div></nav>'
    )

    script = """
<script>
function toggleDoc(d){d.classList.toggle('open');}
// expand all / collapse all via keyboard: e / c
document.addEventListener('keydown',e=>{
  if(e.key==='e')document.querySelectorAll('.doc').forEach(d=>d.classList.add('open'));
  if(e.key==='c')document.querySelectorAll('.doc').forEach(d=>d.classList.remove('open'));
});
// scroll progress
const prog=document.getElementById('navprog');
const tocLinks=[...document.querySelectorAll('.toc a')];
const targets=tocLinks.map(a=>document.getElementById(a.dataset.target)).filter(Boolean);
function onScroll(){
  const h=document.documentElement;
  prog.style.width=(h.scrollTop/(h.scrollHeight-h.clientHeight)*100)+'%';
  const y=h.scrollTop+90;let active=null;
  targets.forEach(t=>{if(t.offsetTop<=y)active=t.id;});
  tocLinks.forEach(a=>a.classList.toggle('active',a.dataset.target===active));
}
document.addEventListener('scroll',onScroll,{passive:true});onScroll();
// TOC click opens the doc
tocLinks.forEach(a=>a.addEventListener('click',()=>{
  const el=document.getElementById(a.dataset.target);
  if(el&&el.classList.contains('doc'))el.classList.add('open');
}));
</script>
"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Okto Neuron — Knowledge Base</title>
<style>{KB_CSS}</style></head><body>
{nav}
<div class="layout">
  <aside class="toc">{"".join(toc_parts)}</aside>
  <main class="content">
    <div class="kb-hero">
      <h1>Knowledge <span class="hl">base</span></h1>
      <p>The complete durable product-documentation corpus — <b>rendered inline from an explicit source registry</b>, grouped by theme. Scratch state, test fixtures, and agent instructions are excluded by rule. Click a card to expand; press <b>e</b> to expand all, <b>c</b> to collapse.</p>
      <p class="scope"><b>Excluded by design:</b> {exclusions}</p>
    </div>
    {"".join(body_parts)}
    <footer><div>Generated from the live markdown by <span class="mono">docs/build_knowledge_base.py</span> — re-run to refresh.</div></footer>
  </main>
</div>
{script}
</body></html>"""


def build_hub() -> str:
    nav = (
        '<nav><div><div class="logo">Okto Neuron<span class="dot">.</span></div>'
        '<div class="sub">documentation hub</div></div>'
        '<div class="navlinks">'
        '<a class="cta" href="roadmap.html">🗺️ Roadmap</a>'
        '<a class="cta" href="understanding/index.html">🧠 Understanding</a>'
        '<a class="cta" href="knowledge-base/index.html">📚 Knowledge Base</a>'
        '<a href="rfc.html">📜 RFC</a>'
        '<a href="https://github.com/OktoLabsAI/okto-neuron" target="_blank" rel="noopener">GitHub ↗</a>'
        '</div><div class="navprog" id="navprog"></div></nav>'
    )
    n_docs = sum(len(d) for *_, d in THEMES)
    n_themes = len(THEMES)
    # Bump this by hand whenever the hub is meaningfully re-verified against the
    # live code — same manually-stamped convention as docs/understanding/index.html.
    HUB_LAST_VERIFIED = "2026-07-13"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Okto Neuron — Docs</title>
<style>{HUB_CSS}</style></head><body>
{nav}
<div class="wrap">
  <div class="hero">
    <div class="emoji">🪶</div>
    <h1>Okto Neuron <span class="hl">documentation</span></h1>
    <p>A standalone, local-first knowledge graph — usable as a Python library, CLI, and MCP server. This is the single entry point to how it's understood and everything it's built on.</p>
  </div>
  <div class="cards">
    <a class="card" href="understanding/index.html">
      <div class="ic">🧠</div>
      <h3>Understanding <span class="hl">→</span></h3>
      <p>The mental model in two tabs — <b>Conceptual</b> (what it is) and <b>Mechanical</b> (how the remember() loop works), verified against the live code.</p>
      <div class="go">Conceptual · Mechanical</div>
    </a>
    <a class="card" href="knowledge-base/index.html">
      <div class="ic">📚</div>
      <h3>Knowledge Base <span class="hl">→</span></h3>
      <p>Every registered durable product doc, plan, research note, subsystem guide, and ADR — rendered inline, grouped by theme, plus curated external references.</p>
      <div class="go">{n_docs} sources · {n_themes} themes · external refs</div>
    </a>
    <a class="card" href="roadmap.html">
      <div class="ic">🗺️</div>
      <h3>Roadmap <span class="hl">→</span></h3>
      <p>The high-level view — milestone arc, what we're building <b>now</b>, and what's <b>next</b>. The headline features, not a task tracker.</p>
      <div class="go">now · next · shipped · later</div>
    </a>
    <a class="card wide" href="rfc.html">
      <div class="ic">📜</div>
      <h3>RFC — the design bible <span class="hl">→</span></h3>
      <p>The authoritative design: motivation, the locked 5-primitive schema, and positioning against Basic Memory / Anytype. Everything else serves this document. <b>Rendered, not raw.</b></p>
      <div class="go">rendered · RFC.md</div>
    </a>
  </div>
  <div class="meta">
    <span class="pill">local-first</span><span class="pill">5 primitives · closed schema</span>
    <span class="pill">library · CLI · MCP</span><span class="pill">OktoLabsAI/okto-neuron</span>
  </div>
  <footer><div>Built from the design docs + an Explore pass over the live code · verified vs code · {HUB_LAST_VERIFIED}</div>
  <div class="mono">docs/index.html</div></footer>
</div>
</body></html>"""


def main() -> None:
    validate_source_registry()
    validate_roadmap()
    validate_markdown_local_links()
    (DOCS / "knowledge-base").mkdir(parents=True, exist_ok=True)
    outputs = {
        DOCS / "index.html": build_hub(),
        DOCS / "knowledge-base" / "index.html": build_kb(),
        DOCS / "rfc.html": build_doc_page(
            "RFC.md", "RFC v1.0", "design bible · rendered from RFC.md"
        ),
        DOCS / "roadmap.html": build_roadmap_page(),
    }
    validate_generated_local_links(list(outputs), contents=outputs)
    for path, content in outputs.items():
        _atomic_write_text(path, content)
    n = sum(len(d) for *_, d in THEMES)
    print(f"✓ knowledge base: {n} sources across {len(THEMES)} themes")
    print(f"  → {DOCS/'knowledge-base'/'index.html'}")
    print(f"  → {DOCS/'index.html'}")
    print(f"  → {DOCS/'rfc.html'} (rendered RFC)")
    print(f"  → {DOCS/'roadmap.html'} (roadmap)")


if __name__ == "__main__":
    main()
