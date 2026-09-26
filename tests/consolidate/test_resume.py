"""ADR 0015 D5b — ledger-native mid-file resume.

Unit tests for the CandidateLedger resume helpers plus integration tests
through the real ``Companion.remember()`` path (fake extractor + counting
curator provider — the harness pattern from ``test_prefilter.py``).

Crash simulation: the provider raises ``KeyboardInterrupt`` after N curator
calls. ``curate()`` only converts ``LLMProviderError`` into abstain verdicts,
so a ``KeyboardInterrupt`` propagates out of ``remember()`` exactly like a
real process interruption — and ``remember()`` records no failure
``ingest_run`` row on the way out, leaving the run in state ``started``
(the precondition resume keys on). Thanks to D5a streamed ledgering, every
verdict consumed before the crash is already durable in the ledger.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.cli.kg import _bootstrap_graph_at_path
from okto_neuron.companion import (
    Companion,
    _candidate_replay_lineage,
    _prior_verdicts_for_method,
    _record_edge_candidate_replay_derivations,
    _replay_meta,
    _replay_verdict,
    _semantic_relation_replay_index,
    _terminal_node_replay_record,
)
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import (
    FRESH_REBUILD_MATERIALIZATION_SCOPE,
    LEDGER_FILENAME,
    CandidateLedger,
)
from okto_neuron.core.schema import Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import Message
from okto_neuron.semantic_fingerprint import (
    load_semantic_materialization,
    publish_semantic_materialization,
    semantic_materialization_path,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import LadybugStore, VaultConnection

# ── ledger helper unit tests ───────────────────────────────────────────────────

_EXTRACTION_FINGERPRINT = "sha256:" + "a" * 64
_SEMANTIC_POLICY_FINGERPRINT = "sha256:" + "b" * 64
_CONFIG_FINGERPRINT = "sha256:" + "c" * 64


def test_legacy_policy_replay_rows_recover_and_persist_original_method() -> None:
    node = {
        "method": "policy_replay",
        "verdict": "commit",
        "payload": {"resolver_confidence": 1.0},
    }
    relation = {
        "method": "policy_replay",
        "verdict": "commit",
        "payload": {
            "canonical_predicate": "performed_at",
            "predicate_definition": "The activity occurred at the place.",
            "predicate_direction": "subject_to_object",
            "inverse_direction_required": False,
            "subject_supported": True,
            "predicate_supported": True,
            "object_supported": True,
            "direction_supported": True,
            "unsupported_inference": False,
            "structural_noise": False,
            "redundant": False,
            "useful": True,
        },
    }
    verdicts = {"policy_replay": {"node": node, "relation": relation}}

    node_replays = _prior_verdicts_for_method(verdicts, "curator")
    relation_replays = _prior_verdicts_for_method(
        verdicts,
        "relation_curator",
        relation=True,
    )

    assert set(node_replays) == {"node"}
    assert node_replays["node"]["payload"]["original_method"] == "curator"
    assert set(relation_replays) == {"relation"}
    assert relation_replays["relation"]["payload"]["original_method"] == ("relation_curator")


def test_semantic_relation_replay_conflict_fails_closed() -> None:
    evidence = {
        "canonical_predicate": "instructs",
        "predicate_definition": "The subject gives the instruction.",
        "predicate_direction": "subject_to_object",
        "inverse_direction_required": False,
        "subject_supported": True,
        "predicate_supported": True,
        "object_supported": True,
        "direction_supported": True,
        "unsupported_inference": False,
        "structural_noise": False,
        "redundant": False,
        "useful": True,
    }
    candidate = {
        "type": "instructed",
        "src_ref": "jo-a",
        "dst_ref": "",
        "dst_literal": "Use South Annex",
        "block_id": "block-1",
    }
    records = {
        "edge-a": {"payload": candidate},
        "edge-b": {"payload": {**candidate, "src_ref": "jo-b"}},
    }
    verdicts = {
        "edge-a": {
            "verdict": "commit",
            "score": 0.95,
            "reason": "supported",
            "payload": evidence,
        },
        "edge-b": {
            "verdict": "queue",
            "score": 0.2,
            "reason": "ambiguous",
            "payload": evidence,
        },
    }

    class _EmptyStore:
        def get_node(self, node_id: str) -> None:  # noqa: ARG002
            return None

    index = _semantic_relation_replay_index(
        verdicts,
        records,
        node_context={},
        store=_EmptyStore(),
        prior_node_identity_by_id={
            "jo-a": ("Agent", "Jo Merek"),
            "jo-b": ("Agent", "Jo Merek"),
        },
    )

    assert len(index) == 1
    conflict = next(iter(index.values()))
    verdict = _replay_verdict(conflict, relation=True)
    assert verdict.action == "queue"
    assert verdict.unsupported_inference is True
    assert conflict["payload"]["semantic_replay_conflict"] is True


def test_terminal_node_replay_uses_only_sealed_exact_candidate_outcomes() -> None:
    candidate = {
        "candidate_id": "node-1",
        "ts": "2026-07-18T00:00:00+00:00",
        "run_id": "run-1",
        "payload": {
            "candidate": {"type": "Agent", "title": "Jo Merek"},
            "reason": "candidate folded into the same-title survivor",
            "terminal_state": "superseded",
            "llm_skipped": True,
            "curator_verdict": {
                "action": "commit",
                "confidence": 0.0,
                "reason": "established entity re-mention; verdict foregone",
            },
        },
    }

    replay = _terminal_node_replay_record(candidate)

    assert replay is not None
    assert replay["verdict"] == "commit"
    assert replay["payload"] == {
        "original_method": "curator",
        "terminal_candidate_replay": True,
        "terminal_state": "superseded",
    }
    assert _replay_meta(replay, mode="policy_replay")["terminal_candidate_replay"] is True
    assert (
        _terminal_node_replay_record(
            {
                **candidate,
                "payload": {**candidate["payload"], "llm_skipped": False},
            }
        )
        is None
    )


def test_candidate_replay_lineage_follows_explicit_derivations_only() -> None:
    assert _candidate_replay_lineage(
        "corrected-twice",
        {
            "corrected-twice": "corrected-once",
            "corrected-once": "raw",
        },
    ) == ("corrected-once", "raw")
    assert _candidate_replay_lineage(
        "cycle-a",
        {"cycle-a": "cycle-b", "cycle-b": "cycle-a"},
    ) == ("cycle-b",)


def test_edge_replay_lineage_survives_multiple_endpoint_remaps() -> None:
    raw = EdgeCandidate(
        type="has_booking",
        src_ref="raw-concept",
        dst_literal="rehearsal booked for May 14",
        block_id="block-1",
    )
    ancestry: dict[str, str] = {}

    _record_edge_candidate_replay_derivations(
        [raw],
        {"raw-concept": "corrected-place"},
        ancestry,
    )
    corrected = raw.model_copy(update={"src_ref": "corrected-place"})
    _record_edge_candidate_replay_derivations(
        [corrected],
        {"corrected-place": "stored-place"},
        ancestry,
    )
    stored = corrected.model_copy(update={"src_ref": "stored-place"})

    from okto_neuron.consolidate.ledger import edge_candidate_id

    raw_id = edge_candidate_id(raw.model_dump(mode="json"))
    corrected_id = edge_candidate_id(corrected.model_dump(mode="json"))
    stored_id = edge_candidate_id(stored.model_dump(mode="json"))
    assert _candidate_replay_lineage(stored_id, ancestry) == (
        corrected_id,
        raw_id,
    )

    conflicting = raw.model_copy(update={"src_ref": "other-raw-concept"})
    _record_edge_candidate_replay_derivations(
        [conflicting],
        {"other-raw-concept": "corrected-place"},
        ancestry,
    )
    assert ancestry[corrected_id] == ""
    assert _candidate_replay_lineage(stored_id, ancestry) == (corrected_id,)


def _start_resumable_run(
    ledger: CandidateLedger,
    *,
    document_id: str = "d",
    blocks_total: int = 3,
    model: str = "m",
) -> str:
    return ledger.start_run(
        document_id=document_id,
        source="s",
        blocks_total=blocks_total,
        model=model,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )


def _find_resumable_run(
    ledger: CandidateLedger,
    *,
    document_id: str = "d",
    blocks_total: int = 3,
    model: str = "m",
    extraction_fingerprint: str = _EXTRACTION_FINGERPRINT,
    semantic_policy_fingerprint: str = _SEMANTIC_POLICY_FINGERPRINT,
) -> str | None:
    return ledger.find_resumable_run(
        document_id=document_id,
        blocks_total=blocks_total,
        model=model,
        extraction_fingerprint=extraction_fingerprint,
        semantic_policy_fingerprint=semantic_policy_fingerprint,
    )


def test_find_resumable_run_matches_and_guards(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    run = _start_resumable_run(ledger)
    assert _find_resumable_run(ledger) == run
    # Guard rails: any extraction-parameter mismatch ⇒ no resume.
    assert _find_resumable_run(ledger, blocks_total=4) is None
    assert _find_resumable_run(ledger, model="other") is None
    assert _find_resumable_run(ledger, document_id="x") is None
    assert _find_resumable_run(ledger, extraction_fingerprint="sha256:" + "c" * 64) is None
    assert _find_resumable_run(ledger, semantic_policy_fingerprint="sha256:" + "d" * 64) is None
    # The most recent matching started run wins.
    run2 = _start_resumable_run(ledger)
    assert _find_resumable_run(ledger) == run2
    # A finished run is closed — never resumed.
    ledger.finish_run(run2, state="completed", summary={})
    assert _find_resumable_run(ledger) == run
    ledger.finish_run(run, state="failed", summary={})
    assert _find_resumable_run(ledger) is None


def test_find_resumable_run_rejects_legacy_rows_without_fingerprints(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.start_run(document_id="d", source="s", blocks_total=3, model="m")

    assert _find_resumable_run(ledger) is None
    assert _find_resumable_run(ledger, extraction_fingerprint="") is None
    assert _find_resumable_run(ledger, semantic_policy_fingerprint="") is None


def test_find_completed_decision_run_requires_exact_policy_and_closed_plan(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    lookup = {
        "document_id": "d",
        "blocks_total": 3,
        "model": "m",
        "config_fingerprint": _CONFIG_FINGERPRINT,
        "extraction_fingerprint": _EXTRACTION_FINGERPRINT,
        "semantic_policy_fingerprint": _SEMANTIC_POLICY_FINGERPRINT,
        "materialization_scope": FRESH_REBUILD_MATERIALIZATION_SCOPE,
    }
    assert ledger.find_completed_decision_run(**lookup) is None
    run = ledger.start_run(
        document_id="d",
        source="s",
        blocks_total=3,
        model="m",
        config_fingerprint=_CONFIG_FINGERPRINT,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
        materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
    )
    plan_id = ledger.record_commit_plan(run, operations=[])
    ledger.record_commit(run, plan_id=plan_id, result={})
    ledger.finish_run(
        run,
        state="completed",
        summary={
            "outcome": {
                "quality": "complete",
                "receipts_complete": True,
            }
        },
        post_semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )

    kwargs = lookup
    assert ledger.find_completed_decision_run(**kwargs) == run
    assert (
        ledger.find_completed_decision_run(**{**kwargs, "config_fingerprint": "sha256:" + "d" * 64})
        is None
    )
    assert (
        ledger.find_completed_decision_run(
            **{
                **kwargs,
                "semantic_policy_fingerprint": "sha256:" + "e" * 64,
            }
        )
        is None
    )

    incremental = ledger.start_run(
        document_id="d",
        source="s",
        blocks_total=3,
        model="m",
        config_fingerprint=_CONFIG_FINGERPRINT,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )
    incremental_plan = ledger.record_commit_plan(incremental, operations=[])
    ledger.record_commit(incremental, plan_id=incremental_plan, result={})
    ledger.finish_run(
        incremental,
        state="completed",
        summary={"outcome": {"quality": "complete", "receipts_complete": True}},
        post_semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )
    assert ledger.find_completed_decision_run(**kwargs) == run

    newer = ledger.start_run(
        document_id="d",
        source="s",
        blocks_total=3,
        model="m",
        config_fingerprint=_CONFIG_FINGERPRINT,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
        materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
    )
    ledger.finish_run(
        newer,
        state="completed",
        summary={"outcome": {"quality": "complete", "receipts_complete": False}},
        post_semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )
    assert ledger.find_completed_decision_run(**kwargs) == run

    redraw = ledger.start_run(
        document_id="d",
        source="s",
        blocks_total=3,
        model="m",
        config_fingerprint=_CONFIG_FINGERPRINT,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
        materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
    )
    redraw_plan = ledger.record_commit_plan(redraw, operations=[])
    ledger.record_commit(redraw, plan_id=redraw_plan, result={})
    ledger.finish_run(
        redraw,
        state="completed",
        summary={"outcome": {"quality": "complete", "receipts_complete": True}},
        post_semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
    )
    assert ledger.find_completed_decision_run(**kwargs) == run
    assert ledger.find_completed_decision_runs(**kwargs) == (run, redraw)


def test_has_open_run_ignores_blocks_total(tmp_path: Path) -> None:
    """has_open_run backs the resume-aware narrowing bypass (2026-07-02
    remediation): a crashed run must be findable REGARDLESS of blocks_total —
    sub-chunk narrowing changes the fingerprint, which is exactly why
    find_resumable_run alone stranded crashed runs under the incremental
    default. Model-independent too (adversarial finding: a model switch
    after a crash otherwise re-opens the data-loss path)."""
    ledger = CandidateLedger(tmp_path)
    run = ledger.start_run(document_id="d", source="s", blocks_total=3, model="m")
    # blocks_total- AND model-independent by design: the bypass is cost-only-
    # safe, so a model switch between crash and re-ingest must still recover
    # the doc (replay correctness stays guarded by find_resumable_run).
    assert ledger.has_open_run(document_id="d") is True
    assert ledger.has_open_run(document_id="x") is False
    # A closed run is not open.
    ledger.finish_run(run, state="completed", summary={})
    assert ledger.has_open_run(document_id="d") is False


def test_resume_snapshot_collects_verdicts_and_candidate_ids(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    ledger.record_candidate(
        run, candidate_id="c1", candidate_kind="node", state="proposed", payload={}
    )
    ledger.record_comparison(
        run, candidate_id="c1", method="curator", verdict="commit", score=0.9, reason="ok"
    )
    ledger.record_comparison(
        run,
        candidate_id="e1",
        method="relation_curator",
        verdict="queue",
        score=0.1,
        reason="weak",
        payload={"canonical_predicate": "defines"},
    )
    # audit_only comparisons belong to the audit pass — excluded from replay.
    ledger.record_comparison(
        run,
        candidate_id="a1",
        method="curator",
        verdict="commit",
        payload={"audit_only": True},
    )
    # Other methods and other runs are ignored.
    ledger.record_comparison(run, candidate_id="c1", method="prefilter", verdict="queue")
    other = ledger.start_run(document_id="d2", source="s", blocks_total=1, model="m")
    ledger.record_comparison(other, candidate_id="zz", method="curator", verdict="commit")

    snap = ledger.resume_snapshot(run)
    assert snap.run_id == run
    assert snap.candidate_ids == {"c1"}
    assert set(snap.verdicts_by_method["curator"]) == {"c1"}
    assert snap.verdicts_by_method["curator"]["c1"]["verdict"] == "commit"
    assert set(snap.verdicts_by_method["relation_curator"]) == {"e1"}
    assert (
        snap.verdicts_by_method["relation_curator"]["e1"]["payload"]["canonical_predicate"]
        == "defines"
    )


def test_resume_snapshot_ignores_per_pair_dedup_judge_rows(tmp_path: Path) -> None:
    """Fix 2 pins the resume-semantics guard: the new per-pair dedup ledger
    rows (companion's ``_dedup_on_pair`` closure records ``method="judge_batch"``
    / ``method="judge_store"`` comparisons — one per judged pair, PLUS the
    pre-existing end-of-pass aggregate rows recorded under the same method
    names) must never surface in ``resume_snapshot``'s default replay set.
    ``resume_snapshot`` only ever collects the methods it is asked for
    (default ``("curator", "relation_curator")``), so any per-pair or
    aggregate dedup row — regardless of candidate_id — is excluded by
    construction. A resumed run must re-run dedup fresh every time; it must
    never replay a merge/distinct verdict as if it were a curator decision."""
    ledger = CandidateLedger(tmp_path)
    run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    ledger.record_candidate(
        run, candidate_id="c1", candidate_kind="node", state="proposed", payload={}
    )
    # Per-pair dedup rows (Fix 2's new on_pair-driven writes) — real
    # candidate_ids, tiny ids/verdict/confidence payloads.
    ledger.record_comparison(
        run,
        candidate_id="c1",
        method="judge_batch",
        target_ref="c0",
        verdict="same",
        score=0.95,
    )
    ledger.record_comparison(
        run,
        candidate_id="c1",
        method="judge_store",
        target_ref="existing-1",
        verdict="distinct",
        score=0.2,
    )
    # The pre-existing end-of-pass aggregate rows (candidate_id="batch"/"store")
    # under the SAME method names — also must never leak into replay.
    ledger.record_comparison(run, candidate_id="batch", method="judge_batch", verdict="merge_pass")
    ledger.record_comparison(run, candidate_id="store", method="judge_store", verdict="merge_pass")
    # A genuine curator verdict for c1, so the snapshot has something to
    # positively assert on alongside the negative dedup-exclusion assertion.
    ledger.record_comparison(run, candidate_id="c1", method="curator", verdict="commit", score=0.9)

    snap = ledger.resume_snapshot(run)

    assert "judge_batch" not in snap.verdicts_by_method
    assert "judge_store" not in snap.verdicts_by_method
    assert set(snap.verdicts_by_method["curator"]) == {"c1"}
    assert snap.verdicts_by_method["curator"]["c1"]["verdict"] == "commit"
    # candidate rows are still tracked for dup-row suppression regardless of
    # method — c1's "candidate" row (not a "comparison" row) is what feeds
    # candidate_ids, so the dedup comparison rows above don't add any ids.
    assert snap.candidate_ids == {"c1"}


# ── integration through Companion.remember() ──────────────────────────────────


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _CountingCuratorProvider:
    """Counts node/relation curator calls; optionally crashes mid-phase.

    ``crash_after_node_calls`` / ``crash_after_relation_calls``: raise
    ``KeyboardInterrupt`` on the call AFTER that many successful calls of the
    given kind — the simulated mid-file crash.
    """

    model = "resume-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(
        self,
        *,
        crash_after_node_calls: int | None = None,
        crash_after_relation_calls: int | None = None,
    ) -> None:
        self.node_curator_calls = 0
        self.relation_curator_calls = 0
        self.predicate_resolution_calls = 0
        self.predicate_resolution_reply = (
            '{"verdict":"distinct","target":"","canonical":"",'
            '"confidence":0.0,"reason":"stub distinct"}'
        )
        self._crash_node = crash_after_node_calls
        self._crash_relation = crash_after_relation_calls

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        assert isinstance(messages[0], Message) or hasattr(messages[0], "content")
        if "candidate curator" in system:
            if self._crash_node is not None and self.node_curator_calls >= self._crash_node:
                raise KeyboardInterrupt("simulated crash during node curation")
            self.node_curator_calls += 1
            return '{"action":"commit","confidence":0.95,"reason":"test commit"}'
        schema = (
            kwargs.get("response_format", {}).get("json_schema")  # type: ignore[union-attr]
            if isinstance(kwargs.get("response_format"), dict)
            else None
        )
        if isinstance(schema, dict) and schema.get("name") == "marginalia_predicate_resolution":
            # ADR 0040 D6a ingest-time predicate resolution. Counted separately
            # so the resume tests can pin that a replayed verdict issues none.
            self.predicate_resolution_calls += 1
            return self.predicate_resolution_reply
        if "relationship curator" in system:
            if (
                self._crash_relation is not None
                and self.relation_curator_calls >= self._crash_relation
            ):
                raise KeyboardInterrupt("simulated crash during relation curation")
            self.relation_curator_calls += 1
            predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in getattr(messages[-1], "content", "").splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return json.dumps(
                {
                    "action": "commit",
                    "confidence": 0.95,
                    "canonical_predicate": predicate,
                    "predicate_definition": (
                        "The subject has the proposed relation to the object."
                    ),
                    "predicate_direction": "subject_to_object",
                    "inverse_direction_required": False,
                    "subject_supported": True,
                    "predicate_supported": True,
                    "object_supported": True,
                    "direction_supported": True,
                    "unsupported_inference": False,
                    "structural_noise": False,
                    "redundant": False,
                    "useful": True,
                    "reason": "test commit",
                },
                separators=(",", ":"),
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _CanonicalizingCuratorProvider(_CountingCuratorProvider):
    """Makes an observable predicate correction for semantic replay tests."""

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        response = super().complete(messages, **kwargs)
        system = getattr(messages[0], "content", "") if messages else ""
        if "relationship curator" not in system:
            return response
        payload = json.loads(response)
        if payload.get("canonical_predicate") == "instructed":
            payload["canonical_predicate"] = "instructs"
        return json.dumps(payload, separators=(",", ":"))


class _FakeExtractor:
    def __init__(
        self,
        nodes: list[NodeCandidate],
        edges: list[EdgeCandidate] | None = None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []
        self.call_count = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.call_count += 1
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[n.model_copy(update={"provenance": prov}) for n in self._nodes],
            edge_candidates=[e.model_copy(update={"provenance": prov}) for e in self._edges],
        )


def _doc(vault: Vault, text: str = "# Note\n\nsome body text.\n") -> Path:
    path = Path(vault.path) / "note.md"
    path.write_text(text, encoding="utf-8")
    return path


def _ledger_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _started_run_ids(records: list[dict]) -> list[str]:
    return [
        r["run_id"] for r in records if r["kind"] == "ingest_run" and r.get("state") == "started"
    ]


def _comparisons(records: list[dict], method: str) -> list[dict]:
    return [r for r in records if r["kind"] == "comparison" and r["method"] == method]


def _candidates() -> tuple[list[NodeCandidate], list[EdgeCandidate]]:
    a = NodeCandidate(type="Agent", title="Alex", content="a chat participant who builds things")
    b = NodeCandidate(
        type="Concept", title="Okto Neuron", content="a local-first knowledge graph product"
    )
    c = NodeCandidate(
        type="Concept", title="Ledger", content="an append-only JSONL audit trail format"
    )
    e1 = EdgeCandidate(type="defines", src_ref=b.candidate_id, dst_literal="a knowledge graph")
    e2 = EdgeCandidate(type="uses", src_ref=a.candidate_id, dst_ref=b.candidate_id)
    return [a, b, c], [e1, e2]


def test_crash_then_resume_replays_judged_candidates(tmp_path: Path) -> None:
    """Crash during the RELATION phase: all node verdicts + one relation verdict
    are durable (D5a); the resumed run replays them with zero LLM calls and
    judges only the remaining relation fresh (D5b)."""
    vault = Vault.init(tmp_path / "v")
    try:
        nodes, edges = _candidates()
        crasher = _CountingCuratorProvider(crash_after_relation_calls=1)
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes, edges))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))

        records = _ledger_records(vault)
        run_ids = _started_run_ids(records)
        assert len(run_ids) == 1
        # D5a durability: verdicts consumed before the crash are on disk.
        assert len(_comparisons(records, "curator")) == 3
        assert len(_comparisons(records, "relation_curator")) == 1
        assert crasher.node_curator_calls == 3

        # Resume: same file, same extraction, same model.
        resumer = _CountingCuratorProvider()
        companion = Companion(vault, provider=resumer, extractor=_FakeExtractor(nodes, edges))
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        # The started run was REUSED — no second started ingest_run row.
        assert _started_run_ids(records) == run_ids
        completed = [
            r for r in records if r["kind"] == "ingest_run" and r.get("state") == "completed"
        ]
        assert [r["run_id"] for r in completed] == run_ids

        # Already-judged candidates produced ZERO LLM calls; only the one
        # un-judged relation was judged fresh.
        assert resumer.node_curator_calls == 0
        assert resumer.relation_curator_calls == 1

        replays = _comparisons(records, "resume_replay")
        replay_methods = sorted(r["payload"]["original_method"] for r in replays)
        assert replay_methods == [
            "curator",
            "curator",
            "curator",
            "relation_curator",
        ]
        for replay in replays:
            assert replay["run_id"] == run_ids[0]
            assert replay["payload"]["replayed_from_ts"]
            assert replay["verdict"] == "commit"

        # No duplicate candidate rows after the resumed run (v0.0.9 bug).
        proposed = [
            (r["candidate_id"], r["state"])
            for r in records
            if r["kind"] == "candidate" and r["state"] == "proposed"
        ]
        assert len(proposed) == len(set(proposed))
        for node in nodes:
            rows = [cid for cid, state in proposed if cid == node.candidate_id]
            assert len(rows) == 1
    finally:
        vault.close()


def test_crash_mid_node_phase_resumes_remaining_nodes(tmp_path: Path) -> None:
    """Crash after 2 of 3 node verdicts: resume judges exactly the third."""
    vault = Vault.init(tmp_path / "v")
    try:
        nodes, _ = _candidates()
        crasher = _CountingCuratorProvider(crash_after_node_calls=2)
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert len(_comparisons(records, "curator")) == 2  # streamed before crash

        resumer = _CountingCuratorProvider()
        companion = Companion(vault, provider=resumer, extractor=_FakeExtractor(nodes))
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert len(_started_run_ids(records)) == 1
        assert resumer.node_curator_calls == 1
        assert len(_comparisons(records, "resume_replay")) == 2
    finally:
        vault.close()


def test_completed_policy_replay_materializes_fresh_graph_without_curator_calls(
    tmp_path: Path,
) -> None:
    """A completed exact-policy run is immutable decision evidence, not a write replay.

    Rebuilds use a new staging graph while retaining the vault ledger.  The next
    materialization must therefore create a new run and fresh commit plan, but it
    must not redraw curator decisions that were already completed under the exact
    same config, extraction, and semantic-policy fingerprints.
    """

    vault_path = tmp_path / "v"
    source: Path
    nodes, edges = _candidates()
    first_provider = _CountingCuratorProvider()
    vault = Vault.init(vault_path)
    try:
        source = _doc(vault)
        Companion(
            vault,
            provider=first_provider,
            extractor=_FakeExtractor(nodes, edges),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
        assert first_provider.node_curator_calls == len(nodes)
        assert first_provider.relation_curator_calls == len(edges)
    finally:
        vault.close()

    first_records = _ledger_records(vault)
    first_run_ids = _started_run_ids(first_records)
    assert len(first_run_ids) == 1

    staging_path = vault_path / "graph.policy-replay-test.lbug"
    staging_handle = _bootstrap_graph_at_path(vault_path, staging_path)
    staging_store = LadybugStore(staging_path, graph_handle=staging_handle)
    staging_vault = Vault(vault_path, staging_store, allow_external_sources=True)
    replay_provider = _CountingCuratorProvider(
        crash_after_node_calls=0,
        crash_after_relation_calls=0,
    )
    replay_extractor = _FakeExtractor(nodes, edges)
    try:
        Companion(
            staging_vault,
            provider=replay_provider,
            extractor=replay_extractor,
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
    finally:
        staging_vault.close()

    records = _ledger_records(staging_vault)
    run_ids = _started_run_ids(records)
    assert len(run_ids) == 2
    assert run_ids[0] == first_run_ids[0]
    assert run_ids[1] != run_ids[0]
    assert replay_provider.node_curator_calls == 0
    assert replay_provider.relation_curator_calls == 0
    assert replay_extractor.call_count == 0

    replays = [
        record
        for record in _comparisons(records, "policy_replay")
        if record["run_id"] == run_ids[1]
    ]
    assert len(replays) == len(nodes) + len(edges)
    assert {record["payload"]["original_method"] for record in replays} == {
        "curator",
        "relation_curator",
    }
    assert {record["payload"]["replayed_from_run_id"] for record in replays} == {run_ids[0]}

    commit_plans = [
        record
        for record in records
        if record["kind"] == "commit_plan" and record["run_id"] in set(run_ids)
    ]
    assert {record["run_id"] for record in commit_plans} == set(run_ids)
    assert len({record["plan_id"] for record in commit_plans}) == 2

    # A third generation must retain the first sealed decision run as its
    # authority. This is the boundary a pairwise test misses and a repeated
    # rebuild exercises.
    third_path = vault_path / "graph.policy-replay-third-test.lbug"
    third_handle = _bootstrap_graph_at_path(vault_path, third_path)
    third_store = LadybugStore(third_path, graph_handle=third_handle)
    third_vault = Vault(vault_path, third_store, allow_external_sources=True)
    third_provider = _CountingCuratorProvider()
    try:
        Companion(
            third_vault,
            provider=third_provider,
            extractor=_FakeExtractor(nodes, edges),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
    finally:
        third_vault.close()

    third_records = _ledger_records(third_vault)
    third_run_ids = _started_run_ids(third_records)
    assert len(third_run_ids) == 3
    assert third_provider.node_curator_calls == 0
    assert third_provider.relation_curator_calls == 0
    chained_replays = [
        record
        for record in _comparisons(third_records, "policy_replay")
        if record["run_id"] == third_run_ids[2]
    ]
    assert len(chained_replays) == len(nodes) + len(edges)
    assert {record["payload"]["original_method"] for record in chained_replays} == {
        "curator",
        "relation_curator",
    }
    assert {record["payload"]["replayed_from_run_id"] for record in chained_replays} == {
        third_run_ids[0]
    }


def test_completed_policy_replay_composes_candidate_gaps_across_compatible_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later compatible runs fill gaps without redrawing earlier decisions.

    Extraction can emit a different candidate subset for identical source bytes.
    A single completed-run pointer loses whichever candidates appeared only in a
    different compatible run, making source-order rebuilds call the curator again.
    The composed authority keeps the first verdict per candidate and adds only
    previously unseen candidate ids from later sealed runs.
    """

    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "0")
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "0")
    # Simulate compatible historical runs created before fresh rebuild reused
    # content-addressed extraction units. Current rebuilds do not redraw these
    # stochastic subsets, but the decision-composition reader must remain able
    # to consume an existing ledger that already contains them.
    monkeypatch.setattr(
        CandidateLedger,
        "successful_extraction_units",
        lambda self, **kwargs: {},  # noqa: ARG005
    )
    vault_path = tmp_path / "v"
    nodes, _ = _candidates()
    source: Path
    first_provider = _CountingCuratorProvider()
    vault = Vault.init(vault_path)
    try:
        source = _doc(vault)
        Companion(
            vault,
            provider=first_provider,
            extractor=_FakeExtractor([nodes[0]]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
    finally:
        vault.close()
    assert first_provider.node_curator_calls == 1

    second_path = vault_path / "graph.policy-gap-second.lbug"
    second_handle = _bootstrap_graph_at_path(vault_path, second_path)
    second_store = LadybugStore(second_path, graph_handle=second_handle)
    second_vault = Vault(vault_path, second_store, allow_external_sources=True)
    second_provider = _CountingCuratorProvider()
    try:
        Companion(
            second_vault,
            provider=second_provider,
            extractor=_FakeExtractor([nodes[1]]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
    finally:
        second_vault.close()
    assert second_provider.node_curator_calls == 1

    first_two_run_ids = _started_run_ids(_ledger_records(second_vault))
    assert len(first_two_run_ids) == 2

    third_path = vault_path / "graph.policy-gap-third.lbug"
    third_handle = _bootstrap_graph_at_path(vault_path, third_path)
    third_store = LadybugStore(third_path, graph_handle=third_handle)
    third_vault = Vault(vault_path, third_store, allow_external_sources=True)
    replay_provider = _CountingCuratorProvider(crash_after_node_calls=0)
    try:
        Companion(
            third_vault,
            provider=replay_provider,
            extractor=_FakeExtractor([nodes[0], nodes[1]]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source)
    finally:
        third_vault.close()

    records = _ledger_records(third_vault)
    run_ids = _started_run_ids(records)
    assert len(run_ids) == 3
    assert replay_provider.node_curator_calls == 0
    replays = [
        record
        for record in _comparisons(records, "policy_replay")
        if record["run_id"] == run_ids[2]
    ]
    assert {record["candidate_id"] for record in replays} == {
        nodes[0].candidate_id,
        nodes[1].candidate_id,
    }
    assert {record["payload"]["replayed_from_run_id"] for record in replays} == set(
        first_two_run_ids
    )


def test_completed_relation_replay_survives_exact_entity_endpoint_remap(
    tmp_path: Path,
) -> None:
    """Semantic authority follows S-P-O evidence when source order changes ids.

    The first materialization sees Jo in source A, then reconciles source B's
    richer Jo candidate onto A's physical id before relation curation.  A fresh
    staging graph that sees B first has a different endpoint id.  Its exact
    relation candidate id therefore misses, but the unambiguous semantic key
    must replay the sealed ``instructs`` verdict without another relation call.
    """

    vault_path = tmp_path / "v"
    first_jo = NodeCandidate(
        type="Agent",
        title="Jo Merek",
        content="a reviewer of the Aurora Notes checklist",
    )
    later_jo = NodeCandidate(
        type="Agent",
        title="Jo Merek",
        content="a participant confirming the corrected rehearsal plan",
    )
    source_a_edge = EdgeCandidate(
        type="reviews",
        src_ref=first_jo.candidate_id,
        dst_literal="Aurora Notes checklist",
    )
    source_b_edge = EdgeCandidate(
        type="instructed",
        src_ref=later_jo.candidate_id,
        dst_literal="Use South Annex for the May 14 rehearsal",
    )

    vault = Vault.init(vault_path)
    try:
        source_a = Path(vault.path) / "a.md"
        source_b = Path(vault.path) / "b.md"
        source_a.write_text("# A\n\nJo reviews Aurora Notes.\n", encoding="utf-8")
        source_b.write_text("# B\n\nJo says to use South Annex.\n", encoding="utf-8")
        provider = _CanonicalizingCuratorProvider()
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([first_jo], [source_a_edge]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source_a)
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([later_jo], [source_b_edge]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source_b)
        assert provider.relation_curator_calls == 2
    finally:
        vault.close()

    # The first pass legitimately evolves the provisional predicate registry,
    # so its pre/post policy fingerprints differ. Materialize once more under
    # the now-stable policy to produce replay-safe sealed authority.
    settled_path = vault_path / "graph.semantic-endpoint-authority-test.lbug"
    settled_handle = _bootstrap_graph_at_path(vault_path, settled_path)
    settled_store = LadybugStore(settled_path, graph_handle=settled_handle)
    settled_vault = Vault(vault_path, settled_store, allow_external_sources=True)
    settled_provider = _CanonicalizingCuratorProvider()
    try:
        Companion(
            settled_vault,
            provider=settled_provider,
            extractor=_FakeExtractor([first_jo], [source_a_edge]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source_a)
        Companion(
            settled_vault,
            provider=settled_provider,
            extractor=_FakeExtractor([later_jo], [source_b_edge]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source_b)
        assert settled_provider.relation_curator_calls == 2
    finally:
        settled_vault.close()

    staging_path = vault_path / "graph.semantic-endpoint-replay-test.lbug"
    staging_handle = _bootstrap_graph_at_path(vault_path, staging_path)
    staging_store = LadybugStore(staging_path, graph_handle=staging_handle)
    staging_vault = Vault(vault_path, staging_store, allow_external_sources=True)
    replay_provider = _CanonicalizingCuratorProvider(
        crash_after_node_calls=0,
        crash_after_relation_calls=0,
    )
    try:
        Companion(
            staging_vault,
            provider=replay_provider,
            extractor=_FakeExtractor([later_jo], [source_b_edge]),
            materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
        ).remember(source_b)
        claims = list(staging_store.list_nodes(type="Claim"))
        assert any(claim.facets.get("P") == "instructs" for claim in claims)
    finally:
        staging_vault.close()

    assert replay_provider.node_curator_calls == 0
    assert replay_provider.relation_curator_calls == 0
    records = _ledger_records(staging_vault)
    latest_run_id = _started_run_ids(records)[-1]
    semantic_replays = [
        record
        for record in _comparisons(records, "policy_replay")
        if record["run_id"] == latest_run_id
        and record["payload"].get("semantic_candidate_replay") is True
    ]
    assert len(semantic_replays) == 1
    assert semantic_replays[0]["payload"]["canonical_predicate"] == "instructs"


def test_identical_reingest_preserves_generation_materialization_receipt(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        nodes, edges = _candidates()
        source = _doc(vault)
        provider = _CountingCuratorProvider()
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor(nodes, edges),
        )
        companion.remember(source)
        generation = vault.store._graph_handle.graph_generation  # noqa: SLF001
        assert generation
        expected = publish_semantic_materialization(
            vault.path,
            graph_generation=generation,
            fingerprints={
                "config": _CONFIG_FINGERPRINT,
                "extraction": _EXTRACTION_FINGERPRINT,
                "semantic_policy": _SEMANTIC_POLICY_FINGERPRINT,
            },
            source="test",
        )

        repeated = companion.remember(source)

        assert repeated.outcome["units"]["scheduled"] == 0
        assert (
            load_semantic_materialization(
                semantic_materialization_path(vault.path),
                expected_graph_generation=generation,
            )
            == expected
        )
    finally:
        vault.close()


_BATCH_CANDIDATE_ID_RE = re.compile(r"=== candidate_id: (\S+) ===")


class _BatchCrashingProvider:
    """Batch-aware curator fake for the ADR 0015 D4 path.

    Answers every candidate_id found in a batch prompt with a commit verdict;
    raises ``KeyboardInterrupt`` on node-curator batch call number
    ``crash_on_node_batch`` (1-based) — the simulated mid-batched-phase crash.
    """

    model = "resume-batch-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self, *, crash_on_node_batch: int | None = None) -> None:
        self.node_batch_calls: list[list[str]] = []
        self._crash_on = crash_on_node_batch

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        user = getattr(messages[-1], "content", "")
        if "candidate curator" in system and "Batch mode" in system:
            ids = _BATCH_CANDIDATE_ID_RE.findall(user)
            if self._crash_on is not None and len(self.node_batch_calls) + 1 >= self._crash_on:
                raise KeyboardInterrupt("simulated crash during batched node curation")
            self.node_batch_calls.append(ids)
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "candidate_id": cid,
                            "action": "commit",
                            "confidence": 0.95,
                            "reason": "test batch commit",
                        }
                        for cid in ids
                    ]
                }
            )
        if "candidate curator" in system:
            return '{"action":"commit","confidence":0.95,"reason":"test commit"}'
        if "relationship curator" in system:
            predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in getattr(messages[-1], "content", "").splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return json.dumps(
                {
                    "action": "commit",
                    "confidence": 0.95,
                    "canonical_predicate": predicate,
                    "predicate_definition": (
                        "The subject has the proposed relation to the object."
                    ),
                    "predicate_direction": "subject_to_object",
                    "inverse_direction_required": False,
                    "subject_supported": True,
                    "predicate_supported": True,
                    "object_supported": True,
                    "direction_supported": True,
                    "unsupported_inference": False,
                    "structural_noise": False,
                    "redundant": False,
                    "useful": True,
                    "reason": "test commit",
                },
                separators=(",", ":"),
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


def test_crash_mid_batched_phase_resumes_from_last_completed_batch(
    tmp_path: Path,
) -> None:
    """Batched curation (D4) streams per batch: a crash on the SECOND batch
    leaves the first batch's verdicts durable in the ledger, and the resumed
    run replays them — only the remaining batch is re-judged."""
    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "\n".join(
                [
                    "marginalia_yaml_version: 1",
                    "consolidation:",
                    "  curation_batch_size: 2",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        # Four same-block candidates at K=2 → exactly two node batches.
        nodes = [
            NodeCandidate(
                type="Concept",
                title=f"Topic {i}",
                content=f"a distinct concept number {i} with enough body text",
                facets={"block_id": "blk-1"},
            )
            for i in range(4)
        ]
        crasher = _BatchCrashingProvider(crash_on_node_batch=2)
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))

        records = _ledger_records(vault)
        run_ids = _started_run_ids(records)
        assert len(run_ids) == 1
        # Per-batch streaming durability: the completed batch's verdicts are
        # already on disk, even though the phase never finished.
        durable = _comparisons(records, "curator")
        assert len(durable) == 2
        assert len(crasher.node_batch_calls) == 1
        judged_ids = {r["candidate_id"] for r in durable}
        assert judged_ids == set(crasher.node_batch_calls[0])

        # Resume: same file, same extraction, same model, batching still on.
        resumer = _BatchCrashingProvider()
        companion = Companion(vault, provider=resumer, extractor=_FakeExtractor(nodes))
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert _started_run_ids(records) == run_ids
        # Completed-batch candidates were NEVER re-judged: the resumed run made
        # exactly one batch call, covering only the remaining two candidates.
        assert len(resumer.node_batch_calls) == 1
        resumed_ids = set(resumer.node_batch_calls[0])
        assert resumed_ids == {n.candidate_id for n in nodes} - judged_ids
        replays = _comparisons(records, "resume_replay")
        assert {r["candidate_id"] for r in replays} == judged_ids
    finally:
        vault.close()


def test_blocks_total_mismatch_starts_fresh_run(tmp_path: Path) -> None:
    """Changing the document (different blocks_total) ⇒ NO resume: a new
    run_id, full re-judge, zero replays."""
    vault = Vault.init(tmp_path / "v")
    try:
        nodes, _ = _candidates()
        crasher = _CountingCuratorProvider(crash_after_node_calls=2)
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))
        first_run_ids = _started_run_ids(_ledger_records(vault))
        assert len(first_run_ids) == 1

        # Rewrite the doc large enough to chunk into MORE extraction blocks.
        big_text = "# Note\n\n" + ("lorem ipsum dolor sit amet " * 800)
        fresh = _CountingCuratorProvider()
        companion = Companion(vault, provider=fresh, extractor=_FakeExtractor(nodes))
        companion.remember(_doc(vault, big_text))

        records = _ledger_records(vault)
        run_ids = _started_run_ids(records)
        assert len(run_ids) == 2  # fresh run, the crashed one untouched
        new_run = next(rid for rid in run_ids if rid not in first_run_ids)
        assert not _comparisons(records, "resume_replay")
        assert fresh.node_curator_calls == 3  # full re-judge
        fresh_curator_rows = [r for r in _comparisons(records, "curator") if r["run_id"] == new_run]
        assert len(fresh_curator_rows) == 3
    finally:
        vault.close()


def test_g6_close_stale_runs_after_fresh_run_closes_crashed_leftover(
    tmp_path: Path,
) -> None:
    """T6 (ledger hygiene): a crashed run whose ``blocks_total`` no longer
    matches (``find_resumable_run`` misses it, so the next ingest starts a
    FRESH run — the exact scenario in
    ``test_blocks_total_mismatch_starts_fresh_run``) must be CLOSED by
    ``close_stale_runs`` once the fresh run completes. Otherwise the crashed
    run lingers ``started`` forever and keeps the resume-narrowing bypass
    (``has_open_run``) permanently engaged for this document. Kills removing
    the ``ledger.close_stale_runs(document_id=..., keep_run_id=run_id)`` call
    on the successful-completion path."""
    vault = Vault.init(tmp_path / "v")
    try:
        nodes, _ = _candidates()
        crasher = _CountingCuratorProvider(crash_after_node_calls=2)
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))
        first_run_ids = _started_run_ids(_ledger_records(vault))
        assert len(first_run_ids) == 1

        # Rewrite the doc large enough to chunk into MORE extraction blocks —
        # blocks_total mismatches, so find_resumable_run misses the crashed
        # run and a FRESH run is started and completes normally.
        big_text = "# Note\n\n" + ("lorem ipsum dolor sit amet " * 800)
        fresh = _CountingCuratorProvider()
        companion = Companion(vault, provider=fresh, extractor=_FakeExtractor(nodes))
        companion.remember(_doc(vault, big_text))

        doc_id = next(iter(vault.store.list_nodes(type="Document"))).id
        ledger = CandidateLedger(Path(vault.path) / ".marginalia")
        stray_started = [
            run_id
            for run_id, record in ledger._open_runs().items()
            if str(record.get("document_id")) == str(doc_id)
        ]
        assert stray_started == [], (
            f"stale 'started' ingest_run row(s) left open for the document "
            f"after a completed fresh run: {stray_started}"
        )
    finally:
        vault.close()


# ── ADR 0040 D6a: ingest-time predicate resolution is replayable ─────────────


def _resolution_rows(records: list[dict]) -> list[dict]:
    return _comparisons(records, "predicate_resolution")


def _folding_candidates() -> tuple[list[NodeCandidate], list[EdgeCandidate]]:
    a = NodeCandidate(type="Agent", title="Alex", content="a chat participant who builds things")
    b = NodeCandidate(
        type="Concept", title="Okto Neuron", content="a local-first knowledge graph product"
    )
    c = NodeCandidate(
        type="Concept", title="Ledger", content="an append-only JSONL audit trail format"
    )
    # Both relations propose the SAME novel label, so one resolution covers both.
    e1 = EdgeCandidate(type="engloba", src_ref=b.candidate_id, dst_ref=c.candidate_id)
    e2 = EdgeCandidate(type="engloba", src_ref=a.candidate_id, dst_ref=b.candidate_id)
    return [a, b, c], [e1, e2]


def _fold_reply() -> str:
    return (
        '{"verdict":"same","target":"includes","canonical":"includes",'
        '"confidence":0.95,"reason":"same containment relation"}'
    )


def test_resolution_row_is_durable_and_replayed_without_a_second_call(
    tmp_path: Path,
) -> None:
    """ADR 0015 D5b for vocabulary.

    A resumed run must NOT re-ask the judge for a label it already decided —
    re-asking is exactly the non-determinism D6a exists to remove. The entity
    judge's own rows are deliberately not replayable, which is fine for merges
    (recomputed from scratch, fail-closed) and not fine for vocabulary.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        nodes, edges = _folding_candidates()
        crasher = _CountingCuratorProvider(crash_after_relation_calls=1)
        crasher.predicate_resolution_reply = _fold_reply()
        companion = Companion(vault, provider=crasher, extractor=_FakeExtractor(nodes, edges))
        with pytest.raises(KeyboardInterrupt):
            companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert crasher.predicate_resolution_calls == 1
        first_rows = _resolution_rows(records)
        assert [row["candidate_id"] for row in first_rows] == ["predicate:engloba"]
        assert first_rows[0]["verdict"] == "same"
        assert first_rows[0]["payload"]["target"] == "includes"

        # Resume. The resolver must never be dialed again: a replayed verdict
        # issues no call, and the one un-judged relation reuses the prior row.
        resumer = _CountingCuratorProvider()

        def _explode(*args: object, **kwargs: object) -> str:
            raise AssertionError("resolver must not be dialed on a resumed run")

        resumer.predicate_resolution_reply = ""  # never returned; see _explode
        companion = Companion(vault, provider=resumer, extractor=_FakeExtractor(nodes, edges))
        original_complete = resumer.complete

        def _guarded(messages, **kwargs):  # type: ignore[no-untyped-def]
            schema = (
                kwargs.get("response_format", {}).get("json_schema")
                if isinstance(kwargs.get("response_format"), dict)
                else None
            )
            if isinstance(schema, dict) and schema.get("name") == (
                "marginalia_predicate_resolution"
            ):
                return _explode()
            return original_complete(messages, **kwargs)

        resumer.complete = _guarded  # type: ignore[method-assign]
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        # The substantive outcome first: the fold survived the crash, so the
        # novel label never reached the registry. Drop the row-replay path and
        # THIS is what breaks — the resumed run mints what the first attempt
        # folded, which is the divergence the replay wiring exists to prevent.
        registry = json.loads(
            (Path(vault.path) / ".marginalia" / "predicates" / "registry.json").read_text(
                encoding="utf-8"
            )
        )
        assert "engloba" not in {record["label"] for record in registry["records"]}
        assert "includes" in {record["label"] for record in registry["records"]}
        assert resumer.predicate_resolution_calls == 0
        rows = _resolution_rows(records)
        # The decision was REUSED, not re-derived: the row is re-recorded for
        # this run marked replayed. (`predicate_resolution_calls == 0` alone is
        # vacuous here — `_guarded` intercepts before the counter — so this is
        # the assertion that carries the claim.)
        assert {row["payload"]["target"] for row in rows} == {"includes"}
        assert any(row["payload"].get("replayed") is True for row in rows)
    finally:
        vault.close()


def test_resume_snapshot_collects_predicate_resolution_rows(tmp_path: Path) -> None:
    """The ledger default-methods change is additive and actually reaches the
    snapshot; `_prior_verdicts_for_method` would have dropped these rows,
    because `_REPLAYABLE_ACTIONS` is {"commit", "queue"}."""

    ledger = CandidateLedger(tmp_path / "ledger.jsonl")
    run_id = ledger.start_run(
        document_id="doc-1",
        source="note.md",
        blocks_total=1,
        model="test/model",
        semantic_policy_fingerprint=_SEMANTIC_POLICY_FINGERPRINT,
        config_fingerprint=_CONFIG_FINGERPRINT,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
    )
    ledger.record_comparison(
        run_id,
        candidate_id="predicate:engloba",
        method="predicate_resolution",
        verdict="same",
        score=0.95,
        reason="same containment relation",
        payload={"proposed_label": "engloba", "target": "includes", "canonical": "includes"},
    )

    snapshot = ledger.resume_snapshot(run_id)

    rows = snapshot.verdicts_by_method["predicate_resolution"]
    assert set(rows) == {"predicate:engloba"}
    assert rows["predicate:engloba"]["verdict"] == "same"
    assert _prior_verdicts_for_method(
        snapshot.verdicts_by_method, "predicate_resolution"
    ) == {}
