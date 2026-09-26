"""End-to-end loop tests for the Companion (Phase F).

These drive the real remember -> resolve -> gate -> commit/queue path with a
**fake extractor** (so candidates are deterministic) over an InMemory-backed
vault, the ``stub`` embedder, and :class:`StubLLM`. NO network.

The confidence arithmetic (resolve + GateConfig 0.75):
  - a novel candidate scores 0.7 + 0.2 = 0.9 -> auto-commit;
  - a contradicted Claim scores 0.7 - 0.5 = 0.2 + contradiction flag -> queued.
So one novel node + one contradicting Claim => committed>=1 AND queued>=1.
"""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron import Vault
from okto_neuron.companion import (
    Companion,
    CompanionError,
    LLMUnavailableError,
    RememberResult,
    _guard_live_graph_write,
)
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger, LEDGER_FILENAME, edge_candidate_id
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.curator import _build_relation_prompt

from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import LLMProviderError, StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import GraphIntegrityState, IntegrityFenceError


def test_entity_key_uses_exact_surface_without_cross_type_or_discovery_merge() -> None:
    from okto_neuron.companion import _entity_key

    first = NodeCandidate(type="Concept", title="\u0390")
    canonically_equivalent = NodeCandidate(type="Concept", title="\u03aa\u0301")
    underscore = NodeCandidate(type="Concept", title="Graph_Store")
    spaced = NodeCandidate(type="Concept", title="Graph Store")
    camel = NodeCandidate(type="Concept", title="GraphStore")
    other_type = NodeCandidate(type="InformationObject", title="\u0390")

    assert _entity_key(first) == _entity_key(canonically_equivalent)
    assert _entity_key(underscore) != _entity_key(spaced)
    assert _entity_key(camel) != _entity_key(spaced)
    assert _entity_key(first) != _entity_key(other_type)


class _FakeProvider:
    """Minimal LLMProvider-compatible object with a configurable api_base.

    Used to test the local-vs-hosted locality gate without bringing in the
    real LiteLLMProvider (which requires litellm installed) or the removed
    OpenAICompatProvider.
    """

    model = "fake"

    def __init__(self, api_base: str) -> None:
        self.api_base = api_base

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        return "[fake]"


def _relation_curator_reply(
    action: str,
    *,
    canonical_predicate: str = "",
    predicate_definition: str = "The subject has the proposed relation to the object.",
    predicate_direction: str = "subject_to_object",
    inverse_direction_required: bool = False,
    reason: str = "test relation curator decision",
) -> str:
    supported = action == "commit"
    return json.dumps(
        {
            "action": action,
            "confidence": 0.95,
            "canonical_predicate": canonical_predicate,
            "predicate_definition": predicate_definition,
            "predicate_direction": predicate_direction,
            "inverse_direction_required": inverse_direction_required,
            "subject_supported": True,
            "predicate_supported": supported,
            "object_supported": True,
            "direction_supported": supported,
            "unsupported_inference": False,
            "structural_noise": False,
            "redundant": False,
            "useful": True,
            "reason": reason,
        },
        separators=(",", ":"),
    )


class _CuratorProvider:
    model = "curator-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(
        self,
        action: str,
        *,
        relation_action: str | None = None,
        canonical_predicate: str | None = None,
        predicate_definition: str = "The subject has the proposed relation to the object.",
        predicate_direction: str = "subject_to_object",
        inverse_direction_required: bool = False,
    ) -> None:
        self.action = action
        self.relation_action = relation_action or action
        self.canonical_predicate = canonical_predicate
        self.predicate_definition = predicate_definition
        self.predicate_direction = predicate_direction
        self.inverse_direction_required = inverse_direction_required

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        if "candidate curator" in system:
            return (
                '{"action":"%s","confidence":0.95,"reason":"test curator decision"}' % self.action
            )
        if "relationship curator" in system:
            fallback_predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in getattr(messages[-1], "content", "").splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return _relation_curator_reply(
                self.relation_action,
                canonical_predicate=(
                    fallback_predicate
                    if self.canonical_predicate is None
                    else self.canonical_predicate
                ),
                predicate_definition=self.predicate_definition,
                predicate_direction=self.predicate_direction,
                inverse_direction_required=self.inverse_direction_required,
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _TypeCorrectingCuratorProvider(_CuratorProvider):
    model = "test/type-correcting-curator"

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        if "adjudicate primitive types" in system:
            request = json.loads(getattr(messages[-1], "content", "{}"))
            return json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "primitive_type": "Agent",
                            "confidence": 0.99,
                            "reason": "The source identifies an acting character.",
                        }
                        for candidate in request["candidates"]
                    ]
                }
            )
        return super().complete(messages, **kwargs)


class _CountingStageProvider(_CuratorProvider):
    def __init__(self) -> None:
        super().__init__("commit")
        self.type_adjudication_calls = 0
        self.relation_curator_calls = 0

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        if "adjudicate primitive types" in system:
            self.type_adjudication_calls += 1
        if "relationship curator" in system:
            self.relation_curator_calls += 1
        return super().complete(messages, **kwargs)


class _ConflictingDuplicateRelationProvider(_CuratorProvider):
    """Commit one canonical relation, then mark its duplicate redundant."""

    def __init__(self) -> None:
        super().__init__("commit")
        self.relation_curator_calls = 0

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        if "relationship curator" not in system:
            return super().complete(messages, **kwargs)
        self.relation_curator_calls += 1
        payload = json.loads(
            _relation_curator_reply(
                "commit",
                canonical_predicate="includes",
            )
        )
        if self.relation_curator_calls == 2:
            payload["redundant"] = True
            payload["reason"] = "same canonical relation already accepted in this batch"
        return json.dumps(payload, separators=(",", ":"))


class _MalformedCuratorProvider:
    model = "malformed-curator-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self, *, malformed_step: str) -> None:
        self.malformed_step = malformed_step

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        if "candidate curator" in system:
            if self.malformed_step == "candidate":
                return "not-json"
            return '{"action":"commit","confidence":0.95,"reason":"test curator commit"}'
        if "relationship curator" in system:
            if self.malformed_step == "relation":
                return "not-json"
            fallback_predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in getattr(messages[-1], "content", "").splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return _relation_curator_reply(
                "commit",
                canonical_predicate=fallback_predicate,
                reason="test relation curator commit",
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _QueueDuplicateCuratorProvider:
    model = "queue-duplicate-curator-test"
    api_base = "http://127.0.0.1:8123/v1"

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        user = getattr(messages[1], "content", "") if len(messages) > 1 else ""
        if "candidate curator" in system:
            action = "queue" if "queued duplicate" in user else "commit"
            return (
                '{"action":"%s","confidence":0.95,'
                '"reason":"test duplicate curator decision"}' % action
            )
        if "relationship curator" in system:
            return _relation_curator_reply(
                "commit",
                canonical_predicate="states",
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _FakeExtractor:
    """Returns a fixed set of candidates, stamping the provenance the companion
    passes in (mirrors what a real extractor does), so committed nodes carry the
    document link the companion supplies."""

    def __init__(
        self,
        nodes: list[NodeCandidate],
        edges: list[EdgeCandidate] | None = None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[n.model_copy(update={"provenance": prov}) for n in self._nodes],
            edge_candidates=[e.model_copy(update={"provenance": prov}) for e in self._edges],
        )


def _doc(vault: Vault, text: str = "# Note\n\nsome body text.\n") -> Path:
    p = Path(vault.path) / "note.md"
    p.write_text(text, encoding="utf-8")
    return p


def _ledger_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_curator_coverage(records: list[dict]) -> None:
    terminal_states = {"committed", "queued", "dead_lettered", "superseded"}
    proposed: dict[tuple[str, str], dict] = {}
    terminal: set[tuple[str, str]] = set()
    reviewed: set[tuple[str, str]] = set()
    for record in records:
        if record["kind"] == "candidate":
            key = (record["candidate_kind"], record["candidate_id"])
            if record["state"] == "proposed":
                proposed[key] = record
            elif record["state"] in terminal_states:
                terminal.add(key)
        elif record["kind"] == "comparison":
            if record["method"] == "curator":
                reviewed.add(("node", record["candidate_id"]))
            elif record["method"] in {
                "relation_curator",
                "semantic_relation_gate",
            }:
                reviewed.add(("edge", record["candidate_id"]))

    assert proposed
    assert set(proposed) <= reviewed
    assert reviewed <= set(proposed)
    assert set(proposed) <= terminal


def _claim_candidate(obj: str) -> NodeCandidate:
    return NodeCandidate(
        type="Claim",
        title=f"sky is {obj}",
        content=f"the sky is {obj}",
        facets={"subject": "sky", "predicate": "is", "object": obj},
    )


def test_remember_commits_and_queues_with_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "v")
    try:
        # Pre-seed a contradicting Claim: sky is blue.
        vault.store.add_node(
            Node(
                id="existing-claim",
                type="Claim",
                title="sky is blue",
                content="the sky is blue",
                facets={"subject": "sky", "predicate": "is", "object": "blue"},
            )
        )
        novel = NodeCandidate(
            type="Concept", title="Photosynthesis", content="how plants eat light"
        )
        novel_claim = EdgeCandidate(
            type="defines",
            src_ref=novel.candidate_id,
            dst_literal="plants use light",
        )
        contradicting = _claim_candidate("green")

        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([novel, contradicting], [novel_claim]),
        )
        original_enqueue = ReviewQueue.enqueue
        enqueue_snapshots: list[list[dict[str, object]]] = []

        def enqueue_after_plan(
            queue: ReviewQueue,
            candidate: NodeCandidate,
            reason: str,
            correlations=(),
        ):
            records = _ledger_records(vault)
            enqueue_snapshots.append(records)
            assert any(record["kind"] == "commit_plan" for record in records)
            assert not any(record["kind"] == "commit_record" for record in records)
            assert not any(
                record["kind"] == "candidate"
                and record.get("candidate_id") == candidate.candidate_id
                and record.get("state") != "proposed"
                for record in records
            )
            return original_enqueue(
                queue,
                candidate,
                reason,  # type: ignore[arg-type]
                correlations,
            )

        monkeypatch.setattr(ReviewQueue, "enqueue", enqueue_after_plan)
        result = companion.remember(_doc(vault))

        assert enqueue_snapshots
        assert result.committed >= 1
        assert result.queued >= 1
        assert len(result.outcomes) == 2
        actions = {o.title: o.action for o in result.outcomes}
        assert actions["Photosynthesis"] == "committed"
        assert actions["sky is green"] == "queued"

        # The committed novel node is in the graph AND traces back to the document.
        committed = vault.store.get_node(novel.candidate_id)
        assert committed is not None
        assert committed.provenance.source == result.document_id
    finally:
        vault.close()


def test_remember_apply_failure_leaves_plan_without_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.companion as companion_module

    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        novel_claim = EdgeCandidate(
            type="defines",
            src_ref=novel.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([novel], [novel_claim]),
        )

        def fail_after_plan(*args, **kwargs):  # noqa: ANN002, ANN003
            del args, kwargs
            records = _ledger_records(vault)
            assert any(record["kind"] == "commit_plan" for record in records)
            raise RuntimeError("apply failed")

        monkeypatch.setattr(
            companion_module,
            "_apply_sealed_semantic_plan",
            fail_after_plan,
        )

        with pytest.raises(RuntimeError, match="apply failed"):
            companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert sum(record["kind"] == "commit_plan" for record in records) == 1
        assert not any(record["kind"] == "commit_record" for record in records)
        assert not any(
            record["kind"] == "candidate"
            and record.get("candidate_id") == novel.candidate_id
            and record.get("state") != "proposed"
            for record in records
        )
    finally:
        vault.close()


def test_remember_cancellation_after_plan_finishes_apply_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.companion as companion_module

    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        novel_claim = EdgeCandidate(
            type="defines",
            src_ref=novel.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([novel], [novel_claim]),
        )
        cancel_requested = False
        original_apply = companion_module._apply_sealed_semantic_plan

        def apply_then_cancel(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal cancel_requested
            result = original_apply(*args, **kwargs)
            cancel_requested = True
            return result

        monkeypatch.setattr(
            companion_module,
            "_apply_sealed_semantic_plan",
            apply_then_cancel,
        )

        result = companion.remember(
            _doc(vault),
            should_cancel=lambda: cancel_requested,
        )

        assert result.committed == 1
        records = _ledger_records(vault)
        assert any(record["kind"] == "commit_record" for record in records)
        assert vault.store.get_node(novel.candidate_id) is not None
    finally:
        vault.close()


def test_remember_persists_candidate_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.companion as companion_module

    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        novel_claim = EdgeCandidate(
            type="defines",
            src_ref=novel.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([novel], [novel_claim]),
        )
        original_apply = companion_module._apply_sealed_semantic_plan
        apply_snapshots: list[list[dict[str, object]]] = []

        def apply_after_plan(*args, **kwargs):  # noqa: ANN002, ANN003
            records = _ledger_records(vault)
            apply_snapshots.append(records)
            assert any(record["kind"] == "commit_plan" for record in records)
            assert not any(
                record["kind"] == "candidate"
                and record.get("candidate_id") == novel.candidate_id
                and record.get("state") != "proposed"
                for record in records
            )
            assert not any(record["kind"] == "commit_record" for record in records)
            return original_apply(*args, **kwargs)

        monkeypatch.setattr(
            companion_module,
            "_apply_sealed_semantic_plan",
            apply_after_plan,
        )
        result = companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert len(apply_snapshots) == 1
        kinds = {record["kind"] for record in records}
        assert {
            "ingest_run",
            "candidate",
            "comparison",
            "commit_plan",
            "commit_record",
        } <= kinds

        started = next(
            record
            for record in records
            if record["kind"] == "ingest_run" and record["state"] == "started"
        )
        for field in (
            "semantic_policy_fingerprint",
            "config_fingerprint",
            "extraction_fingerprint",
        ):
            assert started[field].startswith("sha256:")
            assert len(started[field]) == len("sha256:") + 64

        run_ids = {
            record["run_id"]
            for record in records
            if "run_id" in record and record.get("candidate_id") != "batch"
        }
        assert len(run_ids) == 1

        candidate_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate" and record["candidate_id"] == novel.candidate_id
        ]
        assert "proposed" in candidate_states
        assert "committed" in candidate_states

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operation = next(
            op
            for op in plan["operations"]
            if op.get("candidate_kind") == "node" and op.get("candidate_id") == novel.candidate_id
        )
        assert operation["candidate_id"] == novel.candidate_id
        assert operation["candidate_kind"] == "node"
        assert operation["confidence"] == pytest.approx(0.9)
        assert operation["operation"] == "create_node"
        assert operation["candidate"]["title"] == "Photosynthesis"
        assert operation["candidate"]["type"] == "Concept"

        commit = next(record for record in records if record["kind"] == "commit_record")
        assert novel.candidate_id in commit["result"]["consolidation"]["committed_node_ids"]
        plan_offset = records.index(plan)
        terminal_offsets = [
            offset
            for offset, record in enumerate(records)
            if record["kind"] == "candidate"
            and record.get("candidate_id") == novel.candidate_id
            and record.get("state") != "proposed"
        ]
        assert terminal_offsets
        assert plan_offset < min(terminal_offsets) < records.index(commit)
        completed = [
            record
            for record in records
            if record["kind"] == "ingest_run" and record["state"] == "completed"
        ]
        assert completed[-1]["summary"]["committed"] == result.committed
    finally:
        vault.close()


class _AlwaysFailingExtractor:
    """Every ``extract`` call raises ``LLMProviderError`` — simulates a
    totally unreachable/misconfigured LLM provider (fix 1, issue #4)."""

    def __init__(self, message: str = "simulated total provider failure") -> None:
        self.message = message
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        raise LLMProviderError(self.message)


class _FirstCallFailingExtractor:
    """The FIRST ``extract`` call raises ``LLMProviderError``; every
    subsequent call succeeds with the fixed candidate set — a transient
    provider blip that recovers, exercising the partial-yield keep-success
    path (fix 1, issue #4): >=1 block succeeded, so remember() must still
    return a success-shaped result with ``provider_error`` set, not raise."""

    def __init__(
        self,
        nodes: list[NodeCandidate],
        edges: list[EdgeCandidate] | None = None,
        message: str = "simulated transient provider failure",
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []
        self.message = message
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        if self.calls == 1:
            raise LLMProviderError(self.message)
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[n.model_copy(update={"provenance": prov}) for n in self._nodes],
            edge_candidates=[e.model_copy(update={"provenance": prov}) for e in self._edges],
        )


def test_remember_raises_on_total_provider_failure_and_finishes_ledger_run(
    tmp_path: Path,
) -> None:
    """Every attempted block failing with a provider error is a LOUD failure
    (``LLMUnavailableError``), not a success-shaped ``RememberResult`` with
    ``provider_error`` quietly set — that silent "success" is exactly the
    has_heading-only bug (issue #4): only the LLM-free structural claim
    vault.add() mints survives, with no visible error. The ledger run must
    still be finished (closed, not left ``started``) so a crashed-run resume
    never mistakes this for an interrupted-but-recoverable run."""
    vault = Vault.init(tmp_path / "v")
    try:
        extractor = _AlwaysFailingExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        with pytest.raises(LLMUnavailableError) as exc_info:
            companion.remember(_doc(vault))

        integrity = exc_info.value.outcome["integrity"]
        assert integrity["status"] == "verified"
        assert integrity["audit_id"]

        assert extractor.calls == 1  # single-block note -> one attempt

        records = _ledger_records(vault)
        run_records = [r for r in records if r["kind"] == "ingest_run"]
        assert run_records, "expected an ingest_run record even on total failure"
        # The ledger is append-only, so the run's OWN "started" row (from
        # start_run) still exists — what must NOT exist is a run whose LAST
        # record is "started" (that is exactly ledger._open_runs()'s
        # crash-interrupted definition, mirrored here without reaching into
        # the private helper).
        last_state_by_run: dict[str, str] = {}
        for r in run_records:
            last_state_by_run[r["run_id"]] = r["state"]
        assert all(state != "started" for state in last_state_by_run.values())
        assert any(state == "failed" for state in last_state_by_run.values())
        failed = next(r for r in run_records if r["state"] == "failed")
        assert failed["summary"]["provider_failures"] == 1
        assert failed["summary"]["outcome"]["quality"] == "failed"
        assert len(failed["summary"]["outcome"]["failed_units"]) == 1
        run_summary = CandidateLedger(Path(vault.path) / ".marginalia").run_summaries(limit=1)[0]
        assert run_summary["summary"]["outcome"]["integrity"] == integrity
    finally:
        vault.close()


def test_post_write_integrity_failure_is_appended_to_finished_run(tmp_path: Path) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger = CandidateLedger(ledger_dir)
    run_id = ledger.start_run(
        document_id="doc",
        source="source.md",
        blocks_total=1,
        model="fixture",
    )
    ledger.finish_run(
        run_id,
        state="completed",
        summary={"outcome": {"quality": "complete"}},
    )
    failed_state = GraphIntegrityState(
        status=AuditStatus.FAILED,
        graph_generation="generation-1",
        writer_fenced=True,
        reason="fixture failure",
        audit_id="audit-failed",
    )

    @contextmanager
    def failing_guard() -> Iterator[None]:
        yield
        raise IntegrityFenceError(failed_state)

    class GuardedWriter:
        def __init__(self) -> None:
            self._vault = SimpleNamespace(
                path=tmp_path,
                store=SimpleNamespace(_graph_handle=None),
                _integrity_write_guard=failing_guard,
            )

        @_guard_live_graph_write
        def write(self) -> RememberResult:
            return RememberResult(
                document_id="doc",
                outcome={"quality": "complete"},
                ledger_run_id=run_id,
            )

    with pytest.raises(IntegrityFenceError):
        GuardedWriter().write()

    run = ledger.run_summaries(limit=1)[0]
    assert run["summary"]["outcome"]["quality"] == "integrity_failed"
    assert run["summary"]["outcome"]["integrity"] == {
        "status": "failed",
        "audit_id": "audit-failed",
        "graph_generation": "generation-1",
    }


def test_remember_total_failure_on_configless_vault_names_vault_and_defaults(
    tmp_path: Path,
) -> None:
    """Fix 3 (issue #4 — LLM-disabled/unconfigured honesty). A vault with NO
    ``okto-neuron.yaml`` silently resolves to the hard-coded ``LLMDefaults``
    (provider=openai, api_base=127.0.0.1:8123) — a phantom endpoint on most
    machines. When every block then fails, the error must name the vault path
    and state the observable facts (resolved provider/api_base match the
    built-in defaults), instead of reading like a generic transient network
    blip.

    Defect K: value-equality against ``LLMDefaults`` cannot tell "no explicit
    llm: section" apart from "an explicit config that happens to match the
    built-ins" — so the message must NOT claim the vault has no config (that
    would be a false claim for the latter case); it only reports the match
    and points at the remedy."""
    vault = Vault.init(tmp_path / "v")
    try:
        # Vault.init() DOES scaffold a okto-neuron.yaml, but it has no ``llm:``
        # section at all — the vault never explicitly configured an LLM, so
        # every resolved step falls back to the hard-coded LLMDefaults. That
        # is the "unconfigured" state this fix detects (not "file absent").
        from okto_neuron.config import LLMDefaults

        cfg = Companion(vault, provider=StubLLM())._vault_config()
        resolved = cfg.llm.resolved("extraction")
        assert resolved.provider == LLMDefaults().provider
        assert resolved.api_base == LLMDefaults().api_base

        extractor = _AlwaysFailingExtractor("connection refused")
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        with pytest.raises(LLMUnavailableError) as exc_info:
            companion.remember(_doc(vault))

        message = str(exc_info.value)
        assert "connection refused" in message
        assert str(vault.path) in message
        assert "no explicit llm config" not in message
        assert "matches the built-in defaults" in message
        assert "127.0.0.1:8123" in message
    finally:
        vault.close()


def test_remember_skips_extraction_when_llm_disabled(tmp_path: Path) -> None:
    """Fix 3 (issue #4). ``llm.enabled: false`` must skip extraction ENTIRELY
    (structural ingest — vault.add()'s has_heading/has_tag claims — still
    happens) and never dial the provider. Uses a spy provider that fails the
    test if ``complete()`` is ever invoked, proving no completion call was
    attempted, and injects NO extractor so remember() would otherwise build
    one from the (disabled) vault config."""
    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nllm:\n  enabled: false\n",
            encoding="utf-8",
        )

        class _NeverDialProvider:
            model = "spy"
            api_base = "http://127.0.0.1:8123/v1"

            def complete(self, *args: object, **kwargs: object) -> str:
                raise AssertionError(
                    "provider.complete() must never be called when llm.enabled=False"
                )

        companion = Companion(vault, provider=_NeverDialProvider())
        result = companion.remember(_doc(vault, "# Title\n\nsome body text.\n"))

        assert result.committed == 0
        assert result.queued == 0
        assert result.provider_failures == 0
        # Defect A fix: llm.enabled=false is a deliberate, healthy config, not
        # a provider failure — it must NOT ride ``provider_error`` (the ingest
        # queue's F4 zero-yield rule treats any truthy provider_error with no
        # yield as a failed item, mislabeling every disabled-vault ingest as
        # an error). The dedicated ``llm_disabled`` flag carries the signal
        # instead.
        assert result.llm_disabled is True
        assert result.provider_error is None
        assert result.outcome["quality"] == "not_applicable"

        # Structural ingest still happened: vault.add()'s LLM-free has_heading
        # claim is present even though extraction never ran.
        headings = [
            n for n in vault.store.list_nodes(type="Claim") if n.facets.get("P") == "has_heading"
        ]
        assert headings, "structural has_heading claim missing — vault.add() must still run"
    finally:
        vault.close()


def test_remember_partial_provider_failure_keeps_success_with_provider_error(
    tmp_path: Path,
) -> None:
    """A transient provider blip that fails only the FIRST of several blocks
    (>=1 block still succeeded) must keep the watcher-retry-friendly success
    shape: a ``RememberResult`` with ``committed``/``queued`` reflecting the
    blocks that DID extract, plus ``provider_error`` and ``provider_failures``
    surfacing the partial failure — never raise."""
    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        extractor = _FirstCallFailingExtractor([novel])
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        # Multi-block document (same "big_text" convention as
        # tests/consolidate/test_resume.py) so the first block's failure
        # leaves later blocks to extract successfully.
        big_text = "# Note\n\n" + ("lorem ipsum dolor sit amet " * 800)
        result = companion.remember(_doc(vault, big_text))

        assert extractor.calls >= 2
        assert result.provider_error is not None
        assert result.provider_failures == 1
        assert result.outcome["quality"] == "partial"
        assert result.outcome["units"]["failed"] == 1
        assert len(result.outcome["failed_units"]) == 1
        # >=1 block succeeded -> the candidate it proposed reached a real
        # gate outcome (committed OR queued; which one is prefilter/gate
        # arithmetic unrelated to this fix — the point is extraction yielded
        # something real instead of remember() raising or the pipeline
        # silently discarding it).
        assert len(result.outcomes) >= 1
        assert result.outcomes[0].title == "Photosynthesis"

        records = _ledger_records(vault)
        run_records = [r for r in records if r["kind"] == "ingest_run"]
        last_state_by_run: dict[str, str] = {}
        for r in run_records:
            last_state_by_run[r["run_id"]] = r["state"]
        assert any(state == "completed" for state in last_state_by_run.values())
        assert all(state != "started" for state in last_state_by_run.values())
        completed = next(row for row in reversed(run_records) if row["state"] == "completed")
        assert completed["summary"]["outcome"]["quality"] == "partial"
    finally:
        vault.close()


def test_candidate_curator_can_force_review(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        companion = Companion(
            vault,
            provider=_CuratorProvider("queue"),
            extractor=_FakeExtractor([novel]),
        )
        result = companion.remember(_doc(vault))

        assert result.committed == 0
        assert result.queued == 1
        assert vault.store.get_node(novel.candidate_id) is None
        records = _ledger_records(vault)
        curator = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == novel.candidate_id
            and record["method"] == "curator"
        ]
        assert curator
        assert curator[-1]["verdict"] == "queue"
    finally:
        vault.close()


def test_candidate_curator_abstain_fails_closed_to_review(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        novel = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        companion = Companion(
            vault,
            provider=_MalformedCuratorProvider(malformed_step="candidate"),
            extractor=_FakeExtractor([novel]),
        )
        result = companion.remember(_doc(vault))

        assert result.committed == 0
        assert result.queued == 1
        assert vault.store.get_node(novel.candidate_id) is None
        assert {item.candidate_id for item in companion.review_queue()} == {novel.candidate_id}

        records = _ledger_records(vault)
        curator = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == novel.candidate_id
            and record["method"] == "curator"
        ]
        assert curator
        assert curator[-1]["verdict"] == "abstain"
        assert curator[-1]["reason"] == "unparseable"
        states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["candidate_id"] == novel.candidate_id
        ]
        assert states[-1] == "queued"
    finally:
        vault.close()


def test_relation_curator_can_queue_literal_claim(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        claim = EdgeCandidate(
            type="has_fact",
            src_ref=subject.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit", relation_action="queue"),
            extractor=_FakeExtractor([subject], [claim]),
        )
        result = companion.remember(_doc(vault, "# Photosynthesis\n\nPlants use light.\n"))

        assert result.committed == 0
        assert result.queued == 1
        assert result.claims_minted == 0
        assert result.outcome["construction_cost"]["completion_calls"] >= 1
        assert result.outcome["construction_cost"]["status"] == "partial"
        assert vault.store.get_node(subject.candidate_id) is None
        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        )
        relation_id = edge_candidate_id(proposed_payload)
        relation_comparisons = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == relation_id
            and record["method"] == "relation_curator"
        ]
        assert relation_comparisons
        assert relation_comparisons[-1]["verdict"] == "queue"
        liveness_gate = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == subject.candidate_id
            and record["method"] == "relationship_liveness_gate"
        ]
        assert liveness_gate
        assert liveness_gate[-1]["verdict"] == "queue"
        terminal_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == relation_id
        ]
        assert terminal_states[-1] == "queued"
    finally:
        vault.close()


def test_exact_duplicate_relation_is_reviewed_and_materialized_once(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        provider = _ConflictingDuplicateRelationProvider()
        subject = NodeCandidate(type="Concept", title="Compatibility")
        object_ = NodeCandidate(type="Concept", title="Deprecation Timeline")
        relation = EdgeCandidate(
            type="includes",
            src_ref=subject.candidate_id,
            dst_ref=object_.candidate_id,
        )

        result = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor(
                [subject, object_],
                [relation, relation],
            ),
        ).remember(_doc(vault, "Compatibility includes a deprecation timeline."))

        relation_id = edge_candidate_id(
            next(
                record["payload"]
                for record in _ledger_records(vault)
                if record.get("kind") == "candidate"
                and record.get("candidate_kind") == "edge"
                and record.get("state") == "proposed"
            )
        )
        gates = [
            record
            for record in _ledger_records(vault)
            if record.get("method") == "semantic_relation_gate"
            and record.get("candidate_id") == relation_id
        ]

        assert provider.relation_curator_calls == 1
        assert result.claims_minted == 1
        assert [(record["verdict"], record["reason"]) for record in gates] == [("commit", "commit")]
    finally:
        vault.close()


def test_canonical_relation_collision_keeps_the_committed_trace(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        provider = _ConflictingDuplicateRelationProvider()
        subject = NodeCandidate(type="Concept", title="Compatibility")
        object_ = NodeCandidate(type="Concept", title="Deprecation Timeline")
        relations = [
            EdgeCandidate(
                type=predicate,
                src_ref=subject.candidate_id,
                dst_ref=object_.candidate_id,
            )
            for predicate in ("contains", "includes")
        ]

        result = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], relations),
        ).remember(_doc(vault, "Compatibility includes a deprecation timeline."))

        records = _ledger_records(vault)
        canonical_gates = [
            record
            for record in records
            if record.get("method") == "semantic_relation_gate"
            and record.get("payload", {}).get("predicate") == "includes"
        ]
        canonical_ids = {record["candidate_id"] for record in canonical_gates}
        plan = next(record for record in records if record.get("kind") == "commit_plan")
        mint = next(
            operation
            for operation in plan["operations"]
            if operation.get("operation") == "mint_claim"
            and operation.get("candidate_id") in canonical_ids
        )

        assert provider.relation_curator_calls == 2
        assert result.claims_minted == 1
        assert len(canonical_ids) == 1
        assert {record["verdict"] for record in canonical_gates} == {"commit", "reject"}
        assert mint["reason"] == "commit"
        assert mint["decision_trace"]["d7"]["action"] == "commit"
        assert mint["decision_trace"]["d7"]["reason"] == "commit"
    finally:
        vault.close()


def test_disabled_relation_curator_makes_zero_calls_and_queues_relation(
    tmp_path: Path,
) -> None:
    from okto_neuron.config import VaultConfig

    vault = Vault.init(tmp_path / "v")
    try:
        VaultConfig.apply_patch(
            vault.path,
            {"consolidation": {"relation_curator_enabled": False}},
        )
        provider = _CountingStageProvider()
        subject = NodeCandidate(
            type="Concept",
            title="Photosynthesis",
            content="plants use light",
        )
        claim = EdgeCandidate(
            type="has_fact",
            src_ref=subject.candidate_id,
            dst_literal="plants use light",
        )

        result = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject], [claim]),
        ).remember(_doc(vault, "# Photosynthesis\n\nPlants use light.\n"))

        assert provider.relation_curator_calls == 0
        assert result.committed == 0
        assert result.claims_minted == 0
        records = _ledger_records(vault)
        disabled = [
            record for record in records if record.get("method") == "relation_curator_disabled"
        ]
        assert len(disabled) == 1
        assert disabled[0]["verdict"] == "queue"
        assert any(
            record.get("method") == "semantic_relation_gate" and record.get("verdict") == "queue"
            for record in records
        )
        assert not any(
            node.facets.get("P") == "has_fact" for node in vault.store.list_nodes(type="Claim")
        )
    finally:
        vault.close()


def test_literal_inverse_conflict_queues_one_relation_without_aborting_ingest(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Agent", title="Smaug", content="a dragon")
        claim = EdgeCandidate(
            type="slain_by",
            src_ref=subject.candidate_id,
            dst_literal="the Black Arrow",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="slain_by",
                inverse_direction_required=True,
            ),
            extractor=_FakeExtractor([subject], [claim]),
        )

        result = companion.remember(_doc(vault, "# Smaug\n\nThe Black Arrow slew Smaug.\n"))

        assert result.committed == 0
        assert result.queued == 1
        assert result.claims_minted == 0
        assert vault.store.get_node(subject.candidate_id) is None
        assert [
            node
            for node in vault.store.list_nodes(type="Claim")
            if node.facets.get("P") == "slain_by"
        ] == []

        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        )
        relation_id = edge_candidate_id(proposed_payload)
        gate = next(
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == relation_id
            and record["method"] == "semantic_relation_gate"
        )
        assert gate["verdict"] == "queue"
        assert gate["reason"] == "queue_predicate"
        assert gate["payload"]["d6_reason"] == "queue_direction_conflict"
        assert gate["payload"]["swapped"] is False

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operations = {
            (operation.get("candidate_id"), operation["operation"])
            for operation in plan["operations"]
        }
        assert (relation_id, "queue_review") in operations
        assert (relation_id, "mint_claim") not in operations
    finally:
        vault.close()


def test_relation_curator_abstain_fails_closed_before_graph_write(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        object_ = NodeCandidate(type="Concept", title="Chlorophyll", content="green pigment")
        relation = EdgeCandidate(
            type="uses",
            src_ref=subject.candidate_id,
            dst_ref=object_.candidate_id,
        )
        companion = Companion(
            vault,
            provider=_MalformedCuratorProvider(malformed_step="relation"),
            extractor=_FakeExtractor([subject, object_], [relation]),
        )
        result = companion.remember(_doc(vault, "# Photosynthesis\n\nPlants use chlorophyll.\n"))

        assert result.committed == 0
        assert result.queued == 2
        assert result.claims_minted == 0
        assert vault.store.get_node(subject.candidate_id) is None
        assert vault.store.get_node(object_.candidate_id) is None
        assert not list(vault.store.list_edges(src=subject.candidate_id, type="uses"))

        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        )
        relation_id = edge_candidate_id(proposed_payload)
        relation_reviews = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == relation_id
            and record["method"] == "relation_curator"
        ]
        assert relation_reviews
        assert relation_reviews[-1]["verdict"] == "abstain"
        assert relation_reviews[-1]["reason"] == "unparseable"
        states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == relation_id
        ]
        assert states[-1] == "queued"

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operations = {(op.get("candidate_id"), op["operation"]): op for op in plan["operations"]}
        assert (relation_id, "queue_review") in operations
        assert (relation_id, "create_edge_or_claim") not in operations
    finally:
        vault.close()


def test_relation_curator_prompt_keeps_late_block_evidence(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        block_id = "block:long"
        late_fact = "Surface to team, find domain expert, don't pretend"
        block_text = ("context filler\n" * 350) + f"| 1 | Unknown | {late_fact} |\n"
        vault.store.add_node(
            Node(id=block_id, type="Block", title="long block", content=block_text)
        )
        subject = NodeCandidate(type="Concept", title="Confidence Scale", content="expertise scale")
        claim = EdgeCandidate(
            type="score_1_action",
            src_ref=subject.candidate_id,
            dst_literal=late_fact,
            block_id=block_id,
        )

        prompt = _build_relation_prompt(
            claim,
            store=vault.store,
            node_candidates={subject.candidate_id: subject},
        )

        assert late_fact in prompt
    finally:
        vault.close()


def test_relation_curator_prompt_excludes_mutable_stored_endpoint_summary(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        block_id = "block:correction"
        source = "[2026-04-06T10:00:00Z] Malik Daro owns the Aster launch handoff now."
        vault.store.add_node(Node(id=block_id, type="Block", title="correction", content=source))
        vault.store.add_node(
            Node(
                id="agent:malik",
                type="Agent",
                title="Malik Daro",
                content="Current handoff owner.",
            )
        )
        vault.store.add_node(
            Node(
                id="concept:aster-handoff",
                type="Concept",
                title="Aster Launch Handoff",
                content="A project or process owned by Rina Vale.",
            )
        )
        relation = EdgeCandidate(
            type="owns",
            src_ref="agent:malik",
            dst_ref="concept:aster-handoff",
            block_id=block_id,
        )

        prompt = _build_relation_prompt(
            relation,
            store=vault.store,
            node_candidates={},
        )

        assert source in prompt
        assert "type: Agent\n  title: Malik Daro" in prompt
        assert "type: Concept\n  title: Aster Launch Handoff" in prompt
        assert "owned by Rina Vale" not in prompt
    finally:
        vault.close()


def test_remember_persists_terminal_edge_candidate_state(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Marginalia", content="local KG")
        object_ = NodeCandidate(type="Agent", title="Jordan Lee Carter", content="partner")
        edge = EdgeCandidate(
            type="project_partner",
            src_ref=subject.candidate_id,
            dst_ref=object_.candidate_id,
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([subject, object_], [edge]),
        )
        result = companion.remember(_doc(vault, "# Marginalia\n\nJoão is the partner.\n"))

        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        )
        edge_id = edge_candidate_id(proposed_payload)
        edge_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == edge_id
        ]
        assert result.edges_extracted == 1
        assert "proposed" in edge_states
        assert edge_states[-1] == "committed"
    finally:
        vault.close()


def _proposed_edge_payloads(vault: Vault) -> list[dict]:
    return [
        record["payload"]
        for record in _ledger_records(vault)
        if record["kind"] == "candidate"
        and record["candidate_kind"] == "edge"
        and record["state"] == "proposed"
    ]


def _collapse_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["kind"] == "extraction_edge_collapse"]


def test_distinct_literal_claims_on_one_block_all_survive_extraction(
    tmp_path: Path,
) -> None:
    """Two literal Claims sharing predicate + subject + block but asserting
    DIFFERENT values are different assertions and must both reach the candidate
    stream. The per-block accumulator used to key on ``dst_ref`` only — empty for
    every literal Claim — so the second value was dropped with no counter, no
    event and no ledger row."""
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Solar bill", content="the power bill")
        first = EdgeCandidate(
            type="has_amount",
            src_ref=subject.candidate_id,
            dst_literal="R$ 412,30",
        )
        second = EdgeCandidate(
            type="has_amount",
            src_ref=subject.candidate_id,
            dst_literal="R$ 517,80",
        )
        events: list[dict] = []
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([subject], [first, second]),
        )
        result = companion.remember(_doc(vault), on_event=events.append)

        literals = {payload["dst_literal"] for payload in _proposed_edge_payloads(vault)}
        assert literals == {"R$ 412,30", "R$ 517,80"}
        assert result.edges_extracted == 2
        # Nothing was folded, so the collapse telemetry must stay silent.
        assert _collapse_events(events) == []
    finally:
        vault.close()


def test_identical_literal_claims_on_one_block_collapse_to_one(tmp_path: Path) -> None:
    """Dedup still works: two literal Claims identical in EVERY field, including
    ``dst_literal``, are the same assertion emitted twice and fold to one — now
    with a counted, reported collapse instead of a bare ``continue``."""
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Solar bill", content="the power bill")
        claim = EdgeCandidate(
            type="has_amount",
            src_ref=subject.candidate_id,
            dst_literal="R$ 412,30",
        )
        events: list[dict] = []
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([subject], [claim, claim.model_copy()]),
        )
        result = companion.remember(_doc(vault), on_event=events.append)

        payloads = _proposed_edge_payloads(vault)
        assert [payload["dst_literal"] for payload in payloads] == ["R$ 412,30"]
        assert result.edges_extracted == 1
        assert [event["payload"] for event in _collapse_events(events)] == [
            {"collapsed": 1, "accumulated": 1}
        ]
    finally:
        vault.close()


def test_topology_edges_still_dedupe_on_dst_ref(tmp_path: Path) -> None:
    """Topology edges (``dst_ref`` set, ``dst_literal`` None) keep their previous
    behaviour: a repeated edge folds, a different object does not."""
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Marginalia", content="local KG")
        first_object = NodeCandidate(type="Agent", title="Jordan Lee Carter", content="partner")
        second_object = NodeCandidate(type="Agent", title="Alex Rivera", content="decider")
        repeated = EdgeCandidate(
            type="project_partner",
            src_ref=subject.candidate_id,
            dst_ref=first_object.candidate_id,
        )
        other = EdgeCandidate(
            type="project_partner",
            src_ref=subject.candidate_id,
            dst_ref=second_object.candidate_id,
        )
        events: list[dict] = []
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor(
                [subject, first_object, second_object],
                [repeated, repeated.model_copy(), other],
            ),
        )
        result = companion.remember(_doc(vault), on_event=events.append)

        payloads = _proposed_edge_payloads(vault)
        assert sorted(payload["dst_ref"] for payload in payloads) == sorted(
            [first_object.candidate_id, second_object.candidate_id]
        )
        assert all(payload["dst_literal"] is None for payload in payloads)
        assert result.edges_extracted == 2
        assert [event["payload"] for event in _collapse_events(events)] == [
            {"collapsed": 1, "accumulated": 2}
        ]
    finally:
        vault.close()


def test_same_entity_twice_yields_one_node(tmp_path: Path) -> None:
    """Two candidates for the SAME entity (same type + normalized title) extracted
    in one remember() collapse to a single committed node."""
    vault = Vault.init(tmp_path / "v")
    try:
        first = NodeCandidate(
            type="InformationObject", title="Marginalia", content="a knowledge graph"
        )
        # same title, different surrounding content -> different candidate_id,
        # so without collapse this would commit a second node.
        second = NodeCandidate(
            type="InformationObject", title="Marginalia ", content="local-first KG"
        )
        fact = EdgeCandidate(
            type="defines",
            src_ref=first.candidate_id,
            dst_literal="a knowledge graph",
        )
        companion = Companion(
            vault, provider=StubLLM(), extractor=_FakeExtractor([first, second], [fact])
        )
        result = companion.remember(_doc(vault))

        assert result.committed == 1
        marginalia_nodes = [
            n
            for n in vault.store.list_nodes(type="InformationObject")
            if n.title.strip().casefold() == "marginalia"
        ]
        assert len(marginalia_nodes) == 1
    finally:
        vault.close()


def test_type_correction_derives_candidate_and_remaps_edge_before_resolution(
    tmp_path: Path,
) -> None:
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import IdentityDecisionIndex, TypeCorrection
    from okto_neuron.semantic_surface import build_surface_record

    vault = Vault.init(tmp_path / "v")
    try:
        original = NodeCandidate(
            type="Agent",
            title="Rivendell",
            content="an elven refuge",
            surface=build_surface_record("Rivendell", "Rivendell"),
        )
        derived = original.model_copy(update={"type": "Place"})
        edge = EdgeCandidate(
            type="described_as",
            src_ref=original.candidate_id,
            dst_literal="an elven refuge",
        )
        authority_dir = Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME
        decisions = IdentityDecisionIndex(authority_dir)
        decisions.append(
            TypeCorrection(
                decision_id="type-rivendell-concept",
                candidate_id=original.candidate_id,
                previous_type="Agent",
                corrected_type="Concept",
                reason="the source rejects the extracted agent type",
            )
        )
        decisions.append(
            TypeCorrection(
                decision_id="type-rivendell-place",
                candidate_id=original.candidate_id,
                previous_type="Concept",
                corrected_type="Place",
                reason="source identifies a location",
            )
        )
        # The same surface under another type is safe here because this candidate
        # has an explicit type decision.
        vault.store.add_node(Node(id="concept-rivendell", type="Concept", title="Rivendell"))

        result = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([original], [edge]),
        ).remember(_doc(vault, "# Rivendell\n\nAn elven refuge.\n"))

        assert derived.candidate_id in {outcome.candidate_id for outcome in result.outcomes}
        assert vault.store.get_node(original.candidate_id) is None
        records = _ledger_records(vault)
        correction = next(
            record for record in records if record.get("method") == "identity_type_correction"
        )
        assert correction["candidate_id"] == original.candidate_id
        assert correction["target_ref"] == derived.candidate_id
        assert correction["payload"]["decision_ids"] == [
            "type-rivendell-concept",
            "type-rivendell-place",
        ]
        derived_node = next(
            record
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["candidate_id"] == derived.candidate_id
            and record["state"] == "derived"
        )
        assert derived_node["payload"]["candidate"]["surface"] == original.surface.model_dump(
            mode="json"
        )
        remapped_edge = next(
            record
            for record in records
            if record.get("method") == "identity_type_correction_edge_remap"
        )
        derived_edge = next(
            record
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "derived"
            and record["candidate_id"] == remapped_edge["target_ref"]
        )
        assert derived_edge["payload"]["derived_from"] == remapped_edge["candidate_id"]
        assert derived_edge["payload"]["src_ref"] == derived.candidate_id
        assert not any(
            record.get("method") == "identity_cross_type"
            for record in records
            if record.get("candidate_id") == derived.candidate_id
        )
    finally:
        vault.close()


def test_identity_decisions_are_snapshotted_with_the_run_fingerprint(tmp_path: Path) -> None:
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import IdentityDecisionIndex, TypeCorrection

    vault = Vault.init(tmp_path / "v")
    try:
        candidate = NodeCandidate(type="Agent", title="Rivendell", content="a refuge")
        decisions = IdentityDecisionIndex(Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME)

        class _MutatingExtractor(_FakeExtractor):
            mutated = False

            def extract(
                self, text: str, *, provenance: Provenance | None = None
            ) -> ExtractionResult:
                if not self.mutated:
                    decisions.append(
                        TypeCorrection(
                            decision_id="late-type-rivendell",
                            candidate_id=candidate.candidate_id,
                            previous_type="Agent",
                            corrected_type="Place",
                            reason="arrived after the run snapshot",
                        )
                    )
                    self.mutated = True
                return super().extract(text, provenance=provenance)

        companion = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_MutatingExtractor([candidate]),
        )
        pinned = companion._semantic_fingerprints()

        result = companion.remember(_doc(vault, "# Rivendell\n\nA refuge.\n"))

        assert candidate.candidate_id in {outcome.candidate_id for outcome in result.outcomes}
        records = _ledger_records(vault)
        started = next(
            record
            for record in records
            if record["kind"] == "ingest_run" and record["state"] == "started"
        )
        assert started["semantic_policy_fingerprint"] == pinned.semantic_policy_fingerprint
        assert not any(
            record.get("method") == "identity_type_correction"
            and record.get("verdict") == "type_corrected"
            for record in records
        )
    finally:
        vault.close()


def test_invalid_identity_decisions_fail_before_extraction(tmp_path: Path) -> None:
    from okto_neuron.errors import IngestError
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import IDENTITY_DECISIONS

    vault = Vault.init(tmp_path / "v")
    try:
        authority_dir = Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME
        authority_dir.mkdir(parents=True, exist_ok=True)
        (authority_dir / IDENTITY_DECISIONS).write_text("not-json", encoding="utf-8")

        class _NeverExtractor(_FakeExtractor):
            calls = 0

            def extract(
                self, text: str, *, provenance: Provenance | None = None
            ) -> ExtractionResult:
                self.calls += 1
                return super().extract(text, provenance=provenance)

        extractor = _NeverExtractor([])
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        with pytest.raises(IngestError, match="identity decisions are invalid"):
            companion.remember(_doc(vault))
        assert extractor.calls == 0
    finally:
        vault.close()


def test_type_correction_cannot_collapse_an_explicitly_distinct_identity(
    tmp_path: Path,
) -> None:
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import (
        DistinctDecision,
        IdentityDecisionIndex,
        TypeCorrection,
    )

    vault = Vault.init(tmp_path / "v")
    try:
        agent = NodeCandidate(type="Agent", title="Mercury", content="same evidence")
        place = NodeCandidate(type="Place", title="Mercury", content="same evidence")
        decisions = IdentityDecisionIndex(Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME)
        decisions.append(
            TypeCorrection(
                decision_id="type-mercury",
                candidate_id=place.candidate_id,
                previous_type="Place",
                corrected_type="Agent",
                reason="candidate type correction",
            )
        )
        decisions.append(
            DistinctDecision(
                decision_id="distinct-mercury-candidates",
                left_id=agent.candidate_id,
                right_id=place.candidate_id,
                reason="the two extracted senses must remain distinct",
            )
        )

        result = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([agent, place]),
        ).remember(_doc(vault, "# Mercury\n\nTwo senses.\n"))

        assert {outcome.candidate_id for outcome in result.outcomes} == {
            agent.candidate_id,
            place.candidate_id,
        }
        review = next(
            record
            for record in _ledger_records(vault)
            if record.get("method") == "identity_type_correction"
            and record.get("verdict") == "queue_review"
        )
        assert review["candidate_id"] == place.candidate_id
        assert review["payload"]["code"] == ("identity_type_correction_distinct_collision")
        assert review["payload"]["proposed_candidate_id"] == agent.candidate_id
    finally:
        vault.close()


def test_cross_type_exact_collision_forces_review_without_sidecar_write(
    tmp_path: Path,
) -> None:
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import IDENTITY_DECISIONS

    vault = Vault.init(tmp_path / "v")
    try:
        organization = NodeCandidate(type="Agent", title="Mordor", content="an organization")
        place = NodeCandidate(type="Place", title="Mordor", content="a region")
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([organization, place]),
        )

        result = companion.remember(_doc(vault, "# Mordor\n\nTwo distinct senses.\n"))

        assert result.committed == 0
        assert result.queued == 2
        assert {item.candidate_id for item in companion.review_queue()} == {
            organization.candidate_id,
            place.candidate_id,
        }
        records = _ledger_records(vault)
        intents = [record for record in records if record.get("method") == "identity_cross_type"]
        assert {record["candidate_id"] for record in intents} == {
            organization.candidate_id,
            place.candidate_id,
        }
        assert all(record["payload"]["sidecar_write"] is False for record in intents)
        assert all(record["payload"]["disposition"] == "queue_review" for record in intents)
        assert not (
            Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME / IDENTITY_DECISIONS
        ).exists()
    finally:
        vault.close()


def test_disabled_type_adjudication_makes_zero_calls_and_queues_conflicts(
    tmp_path: Path,
) -> None:
    from okto_neuron.config import VaultConfig

    vault = Vault.init(tmp_path / "v")
    try:
        VaultConfig.apply_patch(
            vault.path,
            {"consolidation": {"type_adjudication_enabled": False}},
        )
        provider = _CountingStageProvider()
        agent = NodeCandidate(type="Agent", title="Mordor", content="an organization")
        place = NodeCandidate(type="Place", title="Mordor", content="a region")

        result = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([agent, place]),
        ).remember(_doc(vault, "# Mordor\n\nTwo distinct senses.\n"))

        assert provider.type_adjudication_calls == 0
        assert result.committed == 0
        assert result.queued == 2
        records = _ledger_records(vault)
        assert {
            record["candidate_id"]
            for record in records
            if record.get("method") == "identity_cross_type"
        } == {agent.candidate_id, place.candidate_id}
    finally:
        vault.close()


def test_high_confidence_type_adjudication_remaps_relation_before_identity(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        agent = NodeCandidate(
            type="Agent",
            title="Bilbo Baggins",
            content="A hobbit who acts as a burglar.",
        )
        mistaken = NodeCandidate(
            type="Concept",
            title="Bilbo Baggins",
            content="A hobbit who secretly takes the Arkenstone.",
        )
        fact = EdgeCandidate(
            type="states",
            src_ref=mistaken.candidate_id,
            dst_literal="Bilbo put the Arkenstone in his deepest pocket.",
        )

        result = Companion(
            vault,
            provider=_TypeCorrectingCuratorProvider("commit"),
            extractor=_FakeExtractor([agent, mistaken], [fact]),
        ).remember(
            _doc(
                vault,
                "# Bilbo Baggins\n\nBilbo put the Arkenstone in his deepest pocket.",
            )
        )

        assert result.committed > 0
        records = _ledger_records(vault)
        correction = next(
            record
            for record in records
            if record.get("method") == "identity_type_adjudicator"
            and record.get("verdict") == "type_corrected"
        )
        assert correction["candidate_id"] == mistaken.candidate_id
        assert correction["payload"]["previous_type"] == "Concept"
        assert correction["payload"]["adjudicated_type"] == "Agent"
        assert not any(record.get("method") == "identity_cross_type" for record in records)
        assert any(
            record.get("method") == "semantic_relation_gate"
            and record.get("verdict") == "commit"
            and record.get("payload", {}).get("object_literal")
            == "Bilbo put the Arkenstone in his deepest pocket."
            for record in records
        )
    finally:
        vault.close()


def test_cross_type_exact_store_collision_forces_new_candidate_review(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        vault.store.add_node(Node(id="stored-concept", type="Concept", title="Mordor"))
        candidate = NodeCandidate(type="Place", title="Mordor", content="a region")
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([candidate]),
        )

        result = companion.remember(_doc(vault, "# Mordor\n\nA region.\n"))

        assert result.committed == 0
        assert result.queued == 1
        intent = next(
            record
            for record in _ledger_records(vault)
            if record.get("method") == "identity_cross_type"
        )
        assert intent["candidate_id"] == candidate.candidate_id
        assert intent["payload"]["conflicts"] == [
            {
                "source": "store",
                "target_ref": "stored-concept",
                "target_type": "Concept",
            }
        ]
    finally:
        vault.close()


def test_cross_type_store_collision_does_not_requeue_established_remention(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        candidate = NodeCandidate(type="Place", title="Mordor", content="a region")
        vault.store.add_node(candidate.to_node())
        vault.store.add_node(Node(id="stored-concept", type="Concept", title="Mordor"))

        Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([candidate]),
        ).remember(_doc(vault, "# Mordor\n\nA region.\n"))

        assert not any(
            record.get("method") == "identity_cross_type" for record in _ledger_records(vault)
        )
    finally:
        vault.close()


def test_distinct_decision_blocks_all_same_title_batch_merge_paths(tmp_path: Path) -> None:
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.decisions import DistinctDecision, IdentityDecisionIndex

    vault = Vault.init(tmp_path / "v")
    try:
        first = NodeCandidate(type="Concept", title="Mercury", content="the planet")
        second = NodeCandidate(type="Concept", title="Mercury", content="the element")
        authority_dir = Path(vault.path) / ".marginalia" / AUTHORITY_DIRNAME
        IdentityDecisionIndex(authority_dir).append(
            DistinctDecision(
                decision_id="distinct-mercury",
                left_id=first.candidate_id,
                right_id=second.candidate_id,
                reason="different senses",
            )
        )

        result = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([first, second]),
        ).remember(_doc(vault, "# Mercury\n\nPlanet and element.\n"))

        assert {outcome.candidate_id for outcome in result.outcomes} == {
            first.candidate_id,
            second.candidate_id,
        }
        records = _ledger_records(vault)
        assert any(
            record.get("method") == "identity_negative_cache"
            and record.get("verdict") == "distinct_cached"
            for record in records
        )
        assert not any(
            record.get("method") == "post_curator_exact_title"
            and record.get("verdict") == "superseded"
            for record in records
        )
    finally:
        vault.close()


def test_candidate_curator_audits_exact_batch_drops(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        first = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        second = NodeCandidate(
            type="Concept", title="Photosynthesis ", content="light-driven plant metabolism"
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit"),
            extractor=_FakeExtractor([first, second]),
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        proposed_node_ids = {
            record["candidate_id"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["state"] == "proposed"
        }
        curated_node_ids = {
            record["candidate_id"]
            for record in records
            if record["kind"] == "comparison" and record["method"] == "curator"
        }

        assert {first.candidate_id, second.candidate_id} <= proposed_node_ids
        assert proposed_node_ids <= curated_node_ids
        audit_records = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "curator"
            and record["payload"].get("audit_only") is True
        ]
        assert len(audit_records) == 1
        assert audit_records[0]["payload"]["proposed_terminal_action"] == "supersede_candidate"
        assert audit_records[0]["payload"]["llm_skipped"] is True

        superseded_states = [
            record
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["candidate_id"] == second.candidate_id
            and record["state"] == "superseded"
        ]
        assert superseded_states
        assert superseded_states[-1]["payload"]["curator_verdict"]["action"] == "commit"

        plan = next(record for record in records if record["kind"] == "commit_plan")
        supersede_ops = [
            op
            for op in plan["operations"]
            if op.get("candidate_id") == second.candidate_id
            and op["operation"] == "supersede_candidate"
        ]
        assert supersede_ops
    finally:
        vault.close()


def test_candidate_curator_queue_vetoes_exact_batch_supersede(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "\n".join(
                [
                    "marginalia_yaml_version: 1",
                    "consolidation:",
                    "  audit_superseded_nodes_with_llm: true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        first = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        second = NodeCandidate(
            type="Concept", title="Photosynthesis ", content="light-driven plant metabolism"
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("queue"),
            extractor=_FakeExtractor([first, second]),
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        second_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["candidate_id"] == second.candidate_id
        ]
        assert second_states[-1] == "queued"
        assert "superseded" not in second_states

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operations = {(op.get("candidate_id"), op["operation"]): op for op in plan["operations"]}
        assert (second.candidate_id, "queue_review") in operations
        assert (second.candidate_id, "supersede_candidate") not in operations

        queued_ids = {item.candidate_id for item in companion.review_queue()}
        assert {first.candidate_id, second.candidate_id} <= queued_ids
    finally:
        vault.close()


def test_ledger_curator_coverage_for_deterministic_remaps(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        vault.store.add_node(
            Node(
                id="existing-topic",
                type="Concept",
                title="Existing Topic",
                content="already in the graph",
            )
        )
        exact_store_match = NodeCandidate(
            type="Concept",
            title="Existing Topic",
            content="re-mentioned in this source",
        )
        subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
        object_ = NodeCandidate(type="Concept", title="Context Window", content="available context")
        object_duplicate = NodeCandidate(
            type="Concept",
            title="Context Window ",
            content="available prompt context",
        )
        edge = EdgeCandidate(
            type="includes_mechanism",
            src_ref=subject.candidate_id,
            dst_ref=object_duplicate.candidate_id,
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="includes",
            ),
            extractor=_FakeExtractor(
                [exact_store_match, subject, object_, object_duplicate],
                [edge],
            ),
        )
        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        _assert_curator_coverage(_ledger_records(vault))
    finally:
        vault.close()


def test_endpoint_gate_skips_relation_curation_for_dead_endpoints(tmp_path: Path) -> None:
    """A relation whose endpoint was queued during node curation is gated
    BEFORE the relation curator (no wasted LLM review) and dead-lettered with
    the same ``endpoint_gate``/``skipped_endpoint`` ledger semantics.

    Uses an entity->entity edge so Fix A's literal-claim subject promotion does
    not apply (that path is covered by
    ``tests/consolidate/test_endpoint_promote.py``)."""
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        object_ = NodeCandidate(type="Concept", title="Chlorophyll", content="a green pigment")
        edge = EdgeCandidate(
            type="involves",
            src_ref=subject.candidate_id,
            dst_ref=object_.candidate_id,
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("queue", relation_action="commit"),
            extractor=_FakeExtractor([subject, object_], [edge]),
        )
        result = companion.remember(_doc(vault, "# Photosynthesis\n\nPlants use light.\n"))

        assert result.committed == 0
        assert result.queued == 2
        assert result.claims_minted == 0

        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        )
        relation_id = edge_candidate_id(proposed_payload)

        relation_reviews = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == relation_id
            and record["method"] == "relation_curator"
        ]
        # Pre-curation endpoint gate: the doomed relation never reaches the
        # relation curator at all.
        assert relation_reviews == []

        endpoint_gate = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == relation_id
            and record["method"] == "endpoint_gate"
        ]
        assert endpoint_gate
        assert endpoint_gate[-1]["verdict"] == "skipped_endpoint"

        terminal_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == relation_id
        ]
        assert terminal_states[-1] == "dead_lettered"
    finally:
        vault.close()


def test_relation_remaps_queued_exact_title_endpoint_to_accepted_sibling(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        accepted = NodeCandidate(type="Agent", title="Galadriel", content="accepted identity")
        duplicate = NodeCandidate(type="Agent", title="Galadriel", content="queued duplicate")
        guard_edge = EdgeCandidate(
            type="related_to",
            src_ref=accepted.candidate_id,
            dst_ref=duplicate.candidate_id,
        )
        claim = EdgeCandidate(
            type="said",
            src_ref=duplicate.candidate_id,
            dst_literal="the skill of the Dwarves is in their hands",
        )
        companion = Companion(
            vault,
            provider=_QueueDuplicateCuratorProvider(),
            extractor=_FakeExtractor([accepted, duplicate], [guard_edge, claim]),
        )
        result = companion.remember(_doc(vault, "# Galadriel\n\nShe spoke to Gimli.\n"))

        assert result.committed == 1
        assert result.claims_minted == 1
        assert vault.store.get_node(accepted.candidate_id) is not None
        assert vault.store.get_node(duplicate.candidate_id) is None

        claims = [
            node
            for node in vault.store.list_nodes(type="Claim")
            if dict(node.facets).get("S_id") == accepted.candidate_id
        ]
        assert len(claims) == 1
        assert claims[0].facets["S_id"] == accepted.candidate_id
        assert claims[0].facets["P"] == "states"

        records = _ledger_records(vault)
        remapped_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"].get("derived") is True
            and record["payload"]["src_ref"] == accepted.candidate_id
            and record["payload"]["dst_literal"] == claim.dst_literal
        )
        remapped_id = edge_candidate_id(remapped_payload)
        endpoint_gate = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["candidate_id"] == remapped_id
            and record["method"] == "endpoint_gate"
        ]
        assert endpoint_gate == []

        raw_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"]["src_ref"] == duplicate.candidate_id
            and record["payload"]["dst_literal"] == claim.dst_literal
        )
        raw_id = edge_candidate_id(raw_payload)
        raw_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == raw_id
        ]
        assert raw_states[-1] == "superseded"
    finally:
        vault.close()


def test_post_curator_exact_title_duplicate_commit_canonicalizes_node_and_relations(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        survivor = NodeCandidate(type="Agent", title="Strider", content="first accepted")
        duplicate = NodeCandidate(type="Agent", title="Strider", content="second accepted")
        self_loop_after_remap = EdgeCandidate(
            type="related_to",
            src_ref=survivor.candidate_id,
            dst_ref=duplicate.candidate_id,
        )
        claim = EdgeCandidate(
            type="said",
            src_ref=duplicate.candidate_id,
            dst_literal="not all those who wander are lost",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="states",
            ),
            extractor=_FakeExtractor([survivor, duplicate], [self_loop_after_remap, claim]),
        )
        result = companion.remember(_doc(vault, "# Strider\n\nHe spoke.\n"))

        assert result.committed == 1
        assert result.claims_minted == 1
        assert vault.store.get_node(survivor.candidate_id) is not None
        assert vault.store.get_node(duplicate.candidate_id) is None
        assert list(vault.store.list_edges(src=survivor.candidate_id)) == []

        claims = [
            node
            for node in vault.store.list_nodes(type="Claim")
            if dict(node.facets).get("S_id") == survivor.candidate_id
        ]
        assert len(claims) == 1
        assert claims[0].facets["P"] == "states"

        records = _ledger_records(vault)
        duplicate_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "node"
            and record["candidate_id"] == duplicate.candidate_id
        ]
        assert duplicate_states[-1] == "superseded"
        canonicalization = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "post_curator_exact_title"
            and record["candidate_id"] == duplicate.candidate_id
        ]
        assert canonicalization
        assert canonicalization[-1]["target_ref"] == survivor.candidate_id
    finally:
        vault.close()


def test_relation_curator_can_canonicalize_topology_predicate(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
        object_ = NodeCandidate(type="Concept", title="Context Window", content="available context")
        edge = EdgeCandidate(
            type="includes_mechanism",
            src_ref=subject.candidate_id,
            dst_ref=object_.candidate_id,
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="includes",
            ),
            extractor=_FakeExtractor([subject, object_], [edge]),
        )
        result = companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert result.committed == 2
        graph_edges = list(vault.store.list_edges(src=subject.candidate_id))
        assert any(
            stored.type == "includes" and stored.dst == object_.candidate_id
            for stored in graph_edges
        )
        assert not any(stored.type == "includes_mechanism" for stored in graph_edges)

        records = _ledger_records(vault)
        original_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"]["type"] == "includes_mechanism"
        )
        canonical_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"]["type"] == "includes"
            and record["payload"].get("derived") is True
        )
        original_id = edge_candidate_id(original_payload)
        canonical_id = edge_candidate_id(canonical_payload)

        original_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == original_id
        ]
        canonical_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == canonical_id
        ]
        assert original_states[-1] == "superseded"
        assert "proposed" in canonical_states
        assert canonical_states[-1] == "committed"

        canonical_comparison = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["candidate_id"] == canonical_id
        ]
        assert canonical_comparison
        assert canonical_comparison[-1]["payload"]["predicate"] == "includes"

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operations = {(op.get("candidate_id"), op["operation"]): op for op in plan["operations"]}
        assert (original_id, "supersede_candidate") in operations
        assert operations[(original_id, "supersede_candidate")]["target_ref"] == canonical_id
        assert (canonical_id, "mint_claim") in operations
    finally:
        vault.close()


def test_registered_provisional_predicate_ignores_definition_paraphrase(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        first_subject = NodeCandidate(type="Agent", title="Rina Vale")
        first_object = NodeCandidate(type="Concept", title="Aster Handoff")
        first_relation = EdgeCandidate(
            type="owns",
            src_ref=first_subject.candidate_id,
            dst_ref=first_object.candidate_id,
        )
        first = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="owns",
                predicate_definition="Indicates ownership or responsibility.",
            ),
            extractor=_FakeExtractor(
                [first_subject, first_object],
                [first_relation],
            ),
        ).remember(_doc(vault, "Rina Vale owns the Aster Handoff."))

        second_subject = NodeCandidate(type="Agent", title="Malik Daro")
        second_object = NodeCandidate(type="Concept", title="Beacon Project")
        second_relation = EdgeCandidate(
            type="owns",
            src_ref=second_subject.candidate_id,
            dst_ref=second_object.candidate_id,
        )
        second = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="owns",
                predicate_definition="Holds primary responsibility for a project.",
            ),
            extractor=_FakeExtractor(
                [second_subject, second_object],
                [second_relation],
            ),
        ).remember(_doc(vault, "Malik Daro owns the Beacon Project."))

        assert first.committed == 2
        assert second.committed == 2
        assert any(
            edge.type == "owns" and edge.dst == second_object.candidate_id
            for edge in vault.store.list_edges(src=second_subject.candidate_id)
        )
        gate = next(
            record
            for record in reversed(_ledger_records(vault))
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["payload"]["src_ref"] == second_subject.candidate_id
            and record["payload"]["dst_ref"] == second_object.candidate_id
        )
        assert gate["verdict"] == "commit"
        assert gate["payload"]["d6_reason"] == "provisional"
    finally:
        vault.close()


def test_registered_provisional_predicate_still_queues_direction_conflict(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        first_subject = NodeCandidate(type="Agent", title="Rina Vale")
        first_object = NodeCandidate(type="Concept", title="Aster Handoff")
        first_relation = EdgeCandidate(
            type="owns",
            src_ref=first_subject.candidate_id,
            dst_ref=first_object.candidate_id,
        )
        Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="owns",
                predicate_definition="Indicates ownership or responsibility.",
            ),
            extractor=_FakeExtractor(
                [first_subject, first_object],
                [first_relation],
            ),
        ).remember(_doc(vault, "Rina Vale owns the Aster Handoff."))

        second_subject = NodeCandidate(type="Agent", title="Malik Daro")
        second_object = NodeCandidate(type="Concept", title="Beacon Project")
        second_relation = EdgeCandidate(
            type="owns",
            src_ref=second_subject.candidate_id,
            dst_ref=second_object.candidate_id,
        )
        second = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="owns",
                predicate_definition="Links two mutually owning peers.",
                predicate_direction="symmetric",
            ),
            extractor=_FakeExtractor(
                [second_subject, second_object],
                [second_relation],
            ),
        ).remember(_doc(vault, "Malik Daro owns the Beacon Project."))

        assert second.committed == 0
        assert not list(vault.store.list_edges(src=second_subject.candidate_id))
        gate = next(
            record
            for record in reversed(_ledger_records(vault))
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["payload"]["src_ref"] == second_subject.candidate_id
            and record["payload"]["dst_ref"] == second_object.candidate_id
        )
        assert gate["verdict"] == "queue"
        assert gate["payload"]["d6_reason"] == "queue_mapping_conflict"
    finally:
        vault.close()


def test_relation_curator_missing_canonical_predicate_fails_closed(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(
            type="Concept", title="Problem Decomposition", content="slicing work"
        )
        claim = EdgeCandidate(
            type="uses_analogy",
            src_ref=subject.candidate_id,
            dst_literal="cutting a cake into slices",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider(
                "commit",
                relation_action="commit",
                canonical_predicate="",
            ),
            extractor=_FakeExtractor([subject], [claim]),
        )
        result = companion.remember(
            _doc(vault, "# Problem Decomposition\n\nThink of decomposition like cutting a cake.\n")
        )

        assert result.committed == 0
        assert result.claims_minted == 0
        assert [
            claim for claim in vault.store.list_nodes(type="Claim") if claim.facets.get("model_id")
        ] == []

        records = _ledger_records(vault)
        proposed_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"]["type"] == "uses_analogy"
        )
        original_id = edge_candidate_id(proposed_payload)
        relation_comparison = next(
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "relation_curator"
            and record["candidate_id"] == original_id
        )
        assert relation_comparison["verdict"] == "abstain"
        gate_comparison = next(
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["candidate_id"] == original_id
        )
        assert gate_comparison["verdict"] == "queue"
        plan = next(record for record in records if record["kind"] == "commit_plan")
        queue_operation = next(
            operation
            for operation in plan["operations"]
            if operation["operation"] == "queue_review" and operation["candidate_id"] == original_id
        )
        assert queue_operation["reason"] == gate_comparison["reason"]
        assert queue_operation["review_item"]["reason"] == gate_comparison["reason"]
        assert (
            queue_operation["review_item"]["pinned_proposal"]["admission"]["reason"]
            == gate_comparison["payload"]["d6_reason"]
        )
    finally:
        vault.close()


def test_relation_curator_audits_remapped_raw_edges(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        first = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        second = NodeCandidate(
            type="Concept", title="Photosynthesis ", content="light-driven plant metabolism"
        )
        edge = EdgeCandidate(
            type="has_fact",
            src_ref=second.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit", relation_action="commit"),
            extractor=_FakeExtractor([first, second], [edge]),
        )
        companion.remember(_doc(vault, "# Photosynthesis\n\nPlants use light.\n"))

        records = _ledger_records(vault)
        proposed_edge_ids = {
            record["candidate_id"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
        }
        relation_reviewed_ids = {
            record["candidate_id"]
            for record in records
            if record["kind"] == "comparison" and record["method"] == "relation_curator"
        }

        assert proposed_edge_ids
        assert proposed_edge_ids <= relation_reviewed_ids
        assert relation_reviewed_ids <= proposed_edge_ids
        audit_records = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "relation_curator"
            and record["payload"].get("audit_only") is True
        ]
        assert len(audit_records) == 1
        assert audit_records[0]["payload"]["proposed_terminal_action"] == "supersede_candidate"

        superseded_states = [
            record
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] in proposed_edge_ids
            and record["state"] == "superseded"
        ]
        assert superseded_states
    finally:
        vault.close()


def test_relation_curator_queue_vetoes_remapped_raw_edge_supersede(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "\n".join(
                [
                    "marginalia_yaml_version: 1",
                    "consolidation:",
                    "  audit_superseded_relations_with_llm: true",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        first = NodeCandidate(type="Concept", title="Photosynthesis", content="plants use light")
        second = NodeCandidate(
            type="Concept", title="Photosynthesis ", content="light-driven plant metabolism"
        )
        edge = EdgeCandidate(
            type="has_fact",
            src_ref=second.candidate_id,
            dst_literal="plants use light",
        )
        companion = Companion(
            vault,
            provider=_CuratorProvider("commit", relation_action="queue"),
            extractor=_FakeExtractor([first, second], [edge]),
        )
        companion.remember(_doc(vault, "# Photosynthesis\n\nPlants use light.\n"))

        records = _ledger_records(vault)
        raw_payload = next(
            record["payload"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["state"] == "proposed"
            and record["payload"]["src_ref"] == second.candidate_id
        )
        raw_id = edge_candidate_id(raw_payload)
        raw_states = [
            record["state"]
            for record in records
            if record["kind"] == "candidate"
            and record["candidate_kind"] == "edge"
            and record["candidate_id"] == raw_id
        ]
        assert raw_states[-1] == "queued"
        assert "superseded" not in raw_states

        plan = next(record for record in records if record["kind"] == "commit_plan")
        operations = {(op.get("candidate_id"), op["operation"]): op for op in plan["operations"]}
        assert (raw_id, "queue_review") in operations
        assert (raw_id, "supersede_candidate") not in operations
    finally:
        vault.close()


def test_distinct_entities_both_commit(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        jordan = NodeCandidate(type="Agent", title="Jordan Lee Carter", content="the partner")
        other = NodeCandidate(type="Agent", title="Alex", content="the maintainer")
        relation = EdgeCandidate(
            type="works_with",
            src_ref=jordan.candidate_id,
            dst_ref=other.candidate_id,
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([jordan, other], [relation]),
        )
        result = companion.remember(_doc(vault))
        assert result.committed == 2
    finally:
        vault.close()


def test_llm_infra_nodes_carry_infra_facet(tmp_path: Path) -> None:
    from okto_neuron._internal.infra import INFRA_FACET_KEY, is_infra

    vault = Vault.init(tmp_path / "v")
    try:
        ent = NodeCandidate(type="Concept", title="Topic", content="a concept")
        companion = Companion(vault, provider=StubLLM(), extractor=_FakeExtractor([ent]))
        companion.remember(_doc(vault))
        infra = [
            n for n in vault.store.list_nodes() if n.type in ("Agent", "Activity") and is_infra(n)
        ]
        assert infra, "expected at least one LLM infra node"
        for node in infra:
            assert node.facets.get(INFRA_FACET_KEY) is True
    finally:
        vault.close()


def test_contradiction_lands_in_queue_and_resolves(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        vault.store.add_node(
            Node(
                id="existing-claim",
                type="Claim",
                title="sky is blue",
                content="the sky is blue",
                facets={"subject": "sky", "predicate": "is", "object": "blue"},
            )
        )
        contradicting = _claim_candidate("green")
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([contradicting]),
        )
        companion.remember(_doc(vault))

        queue = companion.review_queue()
        assert len(queue) == 1
        item = queue[0]
        assert item.reason == "contradiction"
        assert item.candidate_id == contradicting.candidate_id

        # Committing the parked candidate writes it and clears the queue.
        outcome = companion.resolve_review(item.candidate_id, "commit")
        assert outcome.action == "committed"
        assert companion.review_queue() == []
        assert vault.store.get_node(contradicting.candidate_id) is not None
        ledger_states = [
            record["state"]
            for record in _ledger_records(vault)
            if record["kind"] == "candidate"
            and record["candidate_id"] == contradicting.candidate_id
        ]
        assert ledger_states[-1] == "committed"
    finally:
        vault.close()


def test_local_only_refuses_hosted_provider(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        hosted = _FakeProvider(api_base="https://api.example.com/v1")
        companion = Companion(vault, provider=hosted, extractor=_FakeExtractor([]))
        with pytest.raises(CompanionError):
            companion.remember(_doc(vault), sensitivity="local_only")
    finally:
        vault.close()


def test_local_only_allows_local_provider(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        local = _FakeProvider(api_base="http://127.0.0.1:8123/v1")
        companion = Companion(vault, provider=local, extractor=_FakeExtractor([]))
        result = companion.remember(_doc(vault), sensitivity="local_only")
        assert result.committed == 0
    finally:
        vault.close()


def test_remember_queues_relation_instead_of_aborting_on_corrupted_alias(
    tmp_path: Path,
) -> None:
    """Regression for 3.2's second half: a corrupted off-graph predicate alias
    record (subject/object not predicate_label_key-normalized — e.g. hand-
    edited, or written before the judge.py normalization fix) makes
    ``admit_predicate`` raise ``ValueError`` for the whole ``exact_mappings``
    snapshot, on EVERY relation in the run, not just the one touching that
    alias (``_mapping_snapshot`` validates the full dict up front). remember()
    must queue the affected relation instead of letting that one bad alias
    abort the entire run."""
    from okto_neuron.predicates import PredicateAliasIndex, PredicateAliasRecord

    vault = Vault.init(tmp_path / "v")
    try:
        index = PredicateAliasIndex(vault.path)
        index.upsert(
            PredicateAliasRecord(
                id="predicate-corrupt",
                subject_predicate="wrote_to",
                mapping="exact_match",
                object_predicate="Wrote To",  # not predicate_label_key-normalized
                confidence=0.9,
                justification="corrupted alias fixture",
                status="auto",
            )
        )

        novel = NodeCandidate(type="Concept", title="Ledger", content="a durable record")
        novel_claim = EdgeCandidate(
            type="defines",  # unrelated to wrote_to/Wrote To — proves blast radius
            src_ref=novel.candidate_id,
            dst_literal="an unrelated relation",
        )
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_FakeExtractor([novel], [novel_claim]),
        )

        result = companion.remember(_doc(vault))  # must not raise ValueError

        assert result.queued >= 1  # the relation is queued, not aborted
        records = _ledger_records(vault)
        gate_payloads = [
            record["payload"]
            for record in records
            if record["kind"] == "comparison" and record["method"] == "semantic_relation_gate"
        ]
        assert gate_payloads
        assert gate_payloads[0]["d6_reason"] == "queue_mapping_conflict"
        assert gate_payloads[0]["d6_state"] == "queued"
    finally:
        vault.close()


# ── ADR 0040 D6a: ingest-time predicate resolution ────────────────────────────


def _resolution_reply(
    verdict: str,
    *,
    target: str = "",
    canonical: str | None = None,
    confidence: float = 0.95,
    reason: str = "test predicate resolution",
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "target": target,
            "canonical": target if canonical is None else canonical,
            "confidence": confidence,
            "reason": reason,
        },
        separators=(",", ":"),
    )


class _ResolvingCuratorProvider(_CuratorProvider):
    """`_CuratorProvider` plus a scripted ingest-time predicate resolver.

    Keyed on the response-format schema name rather than on prompt prose, so the
    stub answers exactly the call the production code makes.
    """

    model = "resolution-test"

    def __init__(
        self,
        *,
        canonical_predicate: str,
        resolution: str | None,
        predicate_direction: str = "subject_to_object",
        resolution_error: bool = False,
    ) -> None:
        super().__init__(
            "commit",
            relation_action="commit",
            canonical_predicate=canonical_predicate,
            predicate_direction=predicate_direction,
        )
        self._resolution = resolution
        self._resolution_error = resolution_error
        self.resolution_calls = 0
        self.resolution_prompts: list[str] = []
        self.relation_prompts: list[str] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        response_format = kwargs.get("response_format")
        schema_name = None
        if isinstance(response_format, dict):
            schema = response_format.get("json_schema")
            if isinstance(schema, dict):
                schema_name = schema.get("name")
        if schema_name == "marginalia_predicate_resolution":
            self.resolution_calls += 1
            self.resolution_prompts.append(getattr(messages[-1], "content", ""))
            if self._resolution_error:
                raise LLMProviderError("resolver endpoint down")
            assert self._resolution is not None
            return self._resolution
        if schema_name == "marginalia_relation_curator":
            self.relation_prompts.append(getattr(messages[-1], "content", ""))
        return super().complete(messages, **kwargs)


def _registry_payload(vault: Vault) -> dict:
    path = Path(vault.path) / ".marginalia" / "predicates" / "registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _registry_labels(vault: Vault) -> set[str]:
    return {record["label"] for record in _registry_payload(vault)["records"]}


def _registry_record(vault: Vault, label: str) -> dict | None:
    return next(
        (record for record in _registry_payload(vault)["records"] if record["label"] == label),
        None,
    )


def _alias_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / "predicates" / "aliases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def _resolution_rows(vault: Vault) -> list[dict]:
    return [
        record
        for record in _ledger_records(vault)
        if record["kind"] == "comparison" and record["method"] == "predicate_resolution"
    ]


def _two_concept_edge(predicate: str) -> tuple[NodeCandidate, NodeCandidate, EdgeCandidate]:
    subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
    object_ = NodeCandidate(type="Concept", title="Context Window", content="available context")
    edge = EdgeCandidate(
        type=predicate,
        src_ref=subject.candidate_id,
        dst_ref=object_.candidate_id,
    )
    return subject, object_, edge


def test_predicate_resolution_folds_novel_label_onto_incumbent(tmp_path: Path) -> None:
    """The negative-space test: this is what proves the step actually fires.

    `engloba` is a perfectly good novel label that today would be minted as a
    provisional record and would live in the vault forever. With D6a it is
    resolved against the live registry, folded onto `includes`, and never
    reaches `registry.json` at all.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        result = companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert result.committed == 2
        assert provider.resolution_calls == 1
        stored = list(vault.store.list_edges(src=subject.candidate_id))
        assert [(item.type, item.dst) for item in stored] == [
            ("includes", object_.candidate_id)
        ]
        # (c) the novel label never entered the registry at all
        assert "engloba" not in _registry_labels(vault)
        # (d) the incumbent absorbed the support instead
        includes = _registry_record(vault, "includes")
        assert includes is not None
        assert includes["support_count"] == 1
        # The fold is durable and prospective, with real election evidence.
        alias = _alias_records(vault)
        assert len(alias) == 1
        assert alias[0]["subject_predicate"] == "engloba"
        assert alias[0]["object_predicate"] == "includes"
        assert alias[0]["mapping"] == "exact_match"
        assert alias[0]["status"] == "auto"
        assert alias[0]["evidence"]["counts"]["includes"] > (
            alias[0]["evidence"]["counts"]["engloba"]
        )
    finally:
        vault.close()


def test_predicate_resolver_not_called_for_already_registered_predicate(
    tmp_path: Path,
) -> None:
    """No `queue_unregistered`, no resolution: the gate is the mint, not the call."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("includes")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="includes",
            resolution=None,
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert provider.resolution_calls == 0
        assert _resolution_rows(vault) == []
        assert _alias_records(vault) == []
    finally:
        vault.close()


def test_predicate_resolver_called_once_per_label_across_many_relations(
    tmp_path: Path,
) -> None:
    """Per-LABEL keying, not per-relation: `has_value` alone carries support in
    the hundreds, so per-relation keying would be a ~100x cost error."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
        objects = [
            NodeCandidate(type="Concept", title=f"Facet {index}", content=f"facet {index}")
            for index in range(5)
        ]
        edges = [
            EdgeCandidate(
                type="engloba",
                src_ref=subject.candidate_id,
                dst_ref=obj.candidate_id,
            )
            for obj in objects
        ]
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, *objects], edges),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nFive facets matter.\n"))

        assert provider.resolution_calls == 1
        # One ledger row too, not five: the row is keyed on the label.
        rows = _resolution_rows(vault)
        assert len(rows) == 1
        assert rows[0]["candidate_id"] == "predicate:engloba"
        assert len(_alias_records(vault)) == 1
        includes = _registry_record(vault, "includes")
        assert includes is not None
        assert includes["support_count"] == 5
    finally:
        vault.close()


def test_predicate_resolver_never_called_when_relation_curator_disabled(
    tmp_path: Path,
) -> None:
    """Synthetic verdicts issue no relation-curator call, and must issue no
    resolution call either (ADR 0015 D5b)."""

    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nconsolidation:\n  relation_curator_enabled: false\n",
            encoding="utf-8",
        )
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=None,
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert provider.resolution_calls == 0
        assert _resolution_rows(vault) == []
    finally:
        vault.close()


def test_predicate_resolution_provider_error_reproduces_baseline_mint(
    tmp_path: Path,
) -> None:
    """Fail-closed to the status quo.

    "Mint" here means "register a provisional label and QUEUE the relation for
    review" (`_ACTION_BY_REASON["queue_unregistered"] == "queue_review"`), not
    "commit". So falling back to it is failing closed.
    """

    def _run(vault_dir: Path, *, resolution_error: bool) -> tuple[set[str], list[dict]]:
        vault = Vault.init(vault_dir)
        try:
            subject, object_, edge = _two_concept_edge("engloba")
            provider = _ResolvingCuratorProvider(
                canonical_predicate="engloba",
                resolution=_resolution_reply("distinct"),
                resolution_error=resolution_error,
            )
            companion = Companion(
                vault,
                provider=provider,
                extractor=_FakeExtractor([subject, object_], [edge]),
            )
            companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))
            assert provider.resolution_calls == 1
            return _registry_labels(vault), _alias_records(vault)
        finally:
            vault.close()

    failed_labels, failed_aliases = _run(tmp_path / "failed", resolution_error=True)
    distinct_labels, distinct_aliases = _run(tmp_path / "distinct", resolution_error=False)

    assert failed_labels == distinct_labels
    assert "engloba" in failed_labels
    assert failed_aliases == distinct_aliases == []


def test_predicate_resolution_low_confidence_same_does_not_fold(tmp_path: Path) -> None:
    from okto_neuron.predicates.resolve import FOLD_CONFIDENCE_GATE

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply(
                "same",
                target="includes",
                confidence=FOLD_CONFIDENCE_GATE - 0.01,
            ),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert "engloba" in _registry_labels(vault)
        assert _alias_records(vault) == []
        row = _resolution_rows(vault)[0]
        assert row["verdict"] == "same"
        assert row["score"] == pytest.approx(FOLD_CONFIDENCE_GATE - 0.01)
        assert row["payload"]["folded"] is False
        assert row["payload"]["gate"] == FOLD_CONFIDENCE_GATE
    finally:
        vault.close()


def test_predicate_resolution_hallucinated_target_does_not_fold(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="not_in_this_vault"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert "engloba" in _registry_labels(vault)
        assert "not_in_this_vault" not in _registry_labels(vault)
        assert _alias_records(vault) == []
        row = _resolution_rows(vault)[0]
        assert row["payload"]["target"] == "not_in_this_vault"
        assert row["payload"]["target_registered"] is False
        assert row["payload"]["folded"] is False
    finally:
        vault.close()


def test_predicate_direction_conflict_guard_covers_canonical_incumbents(
    tmp_path: Path,
) -> None:
    """Pre-D6a this guard required `state == "provisional"`, so a curator that
    called a directed CANONICAL predicate symmetric committed silently: the
    planned-record loop keeps `base.direction` verbatim and only advances
    signatures/support/samples/confidence."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("includes")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="includes",
            resolution=None,
            predicate_direction="symmetric",
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert list(vault.store.list_edges(src=subject.candidate_id)) == []
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["payload"].get("predicate") == "includes"
        ]
        assert gate_rows
        assert gate_rows[-1]["payload"]["d6_reason"] == "queue_mapping_conflict"
        # The incumbent's own shape is untouched.
        includes = _registry_record(vault, "includes")
        assert includes is not None
        assert includes["direction"] == "subject_to_object"
        assert includes["definition"] == "The subject includes the object as a member or part."
    finally:
        vault.close()


def test_matching_direction_on_canonical_incumbent_still_commits(tmp_path: Path) -> None:
    """The positive half of the widened guard: a bug there would queue
    everything, and every pre-existing test would still pass."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("includes")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="includes",
            resolution=None,
            predicate_direction="subject_to_object",
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert [
            (item.type, item.dst) for item in vault.store.list_edges(src=subject.candidate_id)
        ] == [("includes", object_.candidate_id)]
    finally:
        vault.close()


def test_predicate_inverse_verdict_mints_and_queues_without_swapping(
    tmp_path: Path,
) -> None:
    """An inverse is a legitimately distinct label — `author_of`/`authored_by`
    are BOTH core canonicals — so nothing folds and no endpoint is swapped.

    `inverse_map` reads only `status == "confirmed"`, so this buys zero
    predicate-count reduction at ingest, by construction. It writes the queued
    record that makes `apply_confirmed_inverse` reachable one human
    confirmation later.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("inverse", target="includes"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert "engloba" in _registry_labels(vault)
        stored = [
            (item.type, item.src, item.dst)
            for item in vault.store.list_edges(src=subject.candidate_id)
        ]
        assert stored == [("engloba", subject.candidate_id, object_.candidate_id)]
        alias = _alias_records(vault)
        assert len(alias) == 1
        assert alias[0]["mapping"] == "inverse_of"
        assert alias[0]["status"] == "queued"
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison" and record["method"] == "semantic_relation_gate"
        ]
        assert all(record["payload"]["swapped"] is False for record in gate_rows)

        from okto_neuron.predicates import PredicateAliasIndex

        assert PredicateAliasIndex(vault.path).inverse_map() == {}
        assert PredicateAliasIndex(vault.path).alias_map() == {}
    finally:
        vault.close()


def test_predicate_narrower_verdict_mints_and_queues_sub_property(tmp_path: Path) -> None:
    """`narrower` folds nothing (ADR 0017: wrote_to may be narrower than
    communicates_with but never same) and `alias_map` must ignore it."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba_estrito")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba_estrito",
            resolution=_resolution_reply("narrower", target="includes"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert "engloba_estrito" in _registry_labels(vault)
        alias = _alias_records(vault)
        assert len(alias) == 1
        assert alias[0]["mapping"] == "sub_property_of"
        assert alias[0]["status"] == "queued"

        from okto_neuron.predicates import PredicateAliasIndex

        assert PredicateAliasIndex(vault.path).alias_map() == {}
    finally:
        vault.close()


def test_predicate_incumbent_supersede_queues_record_and_keeps_lifecycle(
    tmp_path: Path,
) -> None:
    """Superseding a bad incumbent is PROPOSED, never applied (ADR 0040 D6a.5).

    A lifecycle demotion would be a functional no-op anyway — canonical and
    provisional both map to `commit_topology` — so the queued mapping record is
    the whole mechanism.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("calculates_tax_for")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="calculates_tax_for",
            resolution=_resolution_reply(
                "same",
                target="includes",
                canonical="calculates_tax_for",
            ),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert "calculates_tax_for" in _registry_labels(vault)
        includes = _registry_record(vault, "includes")
        assert includes is not None
        assert includes["lifecycle"] == "canonical"
        assert includes["support_count"] == 0
        alias = _alias_records(vault)
        assert len(alias) == 1
        assert alias[0]["subject_predicate"] == "includes"
        assert alias[0]["object_predicate"] == "calculates_tax_for"
        assert alias[0]["mapping"] == "exact_match"
        assert alias[0]["status"] == "queued"

        from okto_neuron.predicates import PredicateAliasIndex

        assert PredicateAliasIndex(vault.path).alias_map() == {}
    finally:
        vault.close()


def test_predicate_alias_plan_operation_is_receipted_and_idempotent(
    tmp_path: Path,
) -> None:
    """The alias write is a Phase 1 plan operation next to `register_predicate`,
    so it moves in the same phase as the registry mints and re-applies cleanly."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )
        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        records = _ledger_records(vault)
        plan = next(record for record in records if record["kind"] == "commit_plan")
        alias_ops = [
            operation
            for operation in plan["operations"]
            if operation["operation"] == "register_predicate_alias"
        ]
        assert len(alias_ops) == 1
        assert alias_ops[0]["predicate"] == "engloba"
        receipts = {
            record["operation_id"]: record
            for record in records
            if record["kind"] == "operation_receipt"
        }
        assert receipts[alias_ops[0]["operation_id"]]["status"] == "applied"

        # Deterministic record id => re-applying the same record is a no-op.
        from okto_neuron.predicates import PredicateAliasIndex, PredicateAliasRecord

        index = PredicateAliasIndex(vault.path)
        index.upsert(PredicateAliasRecord.from_json(alias_ops[0]["record"]))
        assert len(_alias_records(vault)) == 1
    finally:
        vault.close()


def test_second_run_folds_via_alias_map_without_any_resolver_call(
    tmp_path: Path,
) -> None:
    """Once the fold is durable, admission resolves it with reason
    `exact_mapping` and the resolver is never reached at all."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        first = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        Companion(
            vault,
            provider=first,
            extractor=_FakeExtractor([subject, object_], [edge]),
        ).remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))
        assert first.resolution_calls == 1

        other = NodeCandidate(type="Concept", title="Prompt Budget", content="token budget")
        second_edge = EdgeCandidate(
            type="engloba",
            src_ref=subject.candidate_id,
            dst_ref=other.candidate_id,
        )
        second = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=None,
        )
        second_doc = Path(vault.path) / "second.md"
        second_doc.write_text("# LLM Mechanics\n\nPrompt budget matters.\n", encoding="utf-8")
        Companion(
            vault,
            provider=second,
            extractor=_FakeExtractor([subject, other], [second_edge]),
        ).remember(second_doc)

        assert second.resolution_calls == 0
        assert "engloba" not in _registry_labels(vault)
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison"
            and record["method"] == "semantic_relation_gate"
            and record["payload"].get("raw_predicate") == "engloba"
        ]
        assert gate_rows[-1]["payload"]["d6_reason"] == "exact_mapping"
        assert gate_rows[-1]["payload"]["predicate"] == "includes"
    finally:
        vault.close()


def test_resolution_ledger_row_preserves_original_proposed_label(
    tmp_path: Path,
) -> None:
    """Re-admitting under the folded target overwrites `admission.raw_predicate`,
    the only other place the model's own label reaches the D6 trace."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        ).remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        row = _resolution_rows(vault)[0]
        assert row["payload"]["proposed_label"] == "engloba"
        assert row["payload"]["target"] == "includes"
        assert row["payload"]["folded"] is True
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison" and record["method"] == "semantic_relation_gate"
        ]
        # The D6 trace now carries the FOLDED label — which is exactly why the
        # resolution row has to keep the original one explicitly.
        assert any(record["payload"]["raw_predicate"] == "includes" for record in gate_rows)
    finally:
        vault.close()


def test_registry_block_rendered_once_per_run(tmp_path: Path) -> None:
    """Byte-stable across the whole run even though the run mints predicates.

    A block that grew with each mint would break the provider's prefix cache on
    every novel predicate — the opposite of the point.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
        first_object = NodeCandidate(type="Concept", title="Facet One", content="one")
        second_object = NodeCandidate(type="Concept", title="Facet Two", content="two")
        edges = [
            EdgeCandidate(
                type="engloba",
                src_ref=subject.candidate_id,
                dst_ref=first_object.candidate_id,
            ),
            EdgeCandidate(
                type="engloba",
                src_ref=subject.candidate_id,
                dst_ref=second_object.candidate_id,
            ),
        ]
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("distinct"),
        )
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, first_object, second_object], edges),
        ).remember(_doc(vault, "# LLM Mechanics\n\nTwo facets matter.\n"))

        blocks = []
        for prompt in provider.relation_prompts:
            assert "Known predicates (reuse by definition):" in prompt
            body = prompt.split("Known predicates (reuse by definition):\n", 1)[1]
            blocks.append(body.split("\n\nRelationship kind:", 1)[0])
        assert len(blocks) >= 2
        assert len(set(blocks)) == 1
        assert "engloba" not in blocks[0]
    finally:
        vault.close()


def test_direction_crossing_fold_queues_instead_of_committing(tmp_path: Path) -> None:
    """A fold whose direction crosses its incumbent's must leave NOTHING durable.

    The curator proposes the novel label `engloba` and calls the relation
    `symmetric`; the resolver folds it onto `includes`, which the vault governs
    as `subject_to_object`. Two separate things must hold, and they used to
    disagree:

    1. the relation must NOT commit — the widened direction guard re-admits
       under `includes`, sees the direction cross, and queues it; and
    2. nothing durable may survive the rejected fold. The `auto` exact_match
       record is prospective and binding (`alias_map` acts on `auto`), so
       staging it before the guard ran meant a fold this run refused would be
       applied silently by the NEXT run — the relation queued once, then
       committed wrong forever after.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
            predicate_direction="symmetric",
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        # (1) the fold was attempted, and the relation did not commit.
        assert provider.resolution_calls == 1
        assert list(vault.store.list_edges(src=subject.candidate_id)) == []
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison" and record["method"] == "semantic_relation_gate"
        ]
        assert gate_rows[-1]["payload"]["d6_reason"] == "queue_mapping_conflict"
        assert gate_rows[-1]["payload"]["predicate"] == "includes"

        # (2) the durable artifacts: no alias record, and the incumbent's shape
        # is untouched. `_alias_records` reads `aliases.json` off disk, so this
        # is the sidefile a later run would actually load — not an in-run value.
        assert _alias_records(vault) == []
        from okto_neuron.predicates import PredicateAliasIndex

        assert PredicateAliasIndex(vault.path).alias_map() == {}
        includes = _registry_record(vault, "includes")
        assert includes is not None
        assert includes["direction"] == "subject_to_object"
        assert includes["support_count"] == 0
        # The novel label folded, so it was never minted either: the run leaves
        # the vocabulary exactly as it found it.
        assert "engloba" not in _registry_labels(vault)

        # The resolution row still records what the resolver decided — it is a
        # decision trace keyed per label, not a receipt for the staging.
        row = _resolution_rows(vault)[0]
        assert row["verdict"] == "same"
        assert row["payload"]["target"] == "includes"
    finally:
        vault.close()


def test_absent_direction_on_canonical_incumbent_still_commits(tmp_path: Path) -> None:
    """`unknown` is absence of direction evidence, not a direction CONFLICT.

    `CuratorVerdict.predicate_direction` defaults to `"unknown"`
    (src/okto_neuron/curator.py:325), so a curator that simply does not speak to
    direction must not have every relation on a governed core predicate queued
    out from under it. The widened guard fires on disagreement only.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("includes")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="includes",
            resolution=None,
            predicate_direction="unknown",
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        )

        companion.remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert [
            (item.type, item.dst) for item in vault.store.list_edges(src=subject.candidate_id)
        ] == [("includes", object_.candidate_id)]
        gate_rows = [
            record
            for record in _ledger_records(vault)
            if record["kind"] == "comparison" and record["method"] == "semantic_relation_gate"
        ]
        assert gate_rows[-1]["payload"]["d6_reason"] == "canonical"
        # No mint was in play, so the resolver was never reached.
        assert provider.resolution_calls == 0
    finally:
        vault.close()


class _PerLabelResolvingProvider(_CuratorProvider):
    """Echoes each candidate's own predicate; resolves per proposed label.

    The resolution reply is chosen by a callback that is handed the resolution
    PROMPT, so a stub can behave like a real model and fold only onto a
    predicate it was actually shown.
    """

    model = "per-label-resolution-test"

    def __init__(self, reply_for) -> None:
        super().__init__(
            "commit",
            relation_action="commit",
            canonical_predicate=None,
            predicate_direction="subject_to_object",
        )
        self._reply_for = reply_for
        self.resolution_prompts: list[str] = []
        self.resolved_labels: list[str] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        response_format = kwargs.get("response_format")
        schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
        if isinstance(schema, dict) and schema.get("name") == "marginalia_predicate_resolution":
            prompt = getattr(messages[-1], "content", "")
            self.resolution_prompts.append(prompt)
            label = next(
                line.partition(":")[2].strip()
                for line in prompt.splitlines()
                if line.startswith("  label:")
            )
            self.resolved_labels.append(label)
            return self._reply_for(label, prompt)
        return super().complete(messages, **kwargs)


def test_resolution_matches_against_the_live_in_run_registry(tmp_path: Path) -> None:
    """The design's central premise: the proposal is weighed against the LIVE
    registry, so a fold is binding against labels THIS SAME RUN minted.

    Two novel labels in one document. `engloba` resolves `distinct` and is
    minted; `abarca` is then offered the registry and folds onto `engloba` — a
    label that did not exist when the run started. The stub only folds onto a
    predicate it can actually see in its prompt, exactly like a real model, so
    swapping the resolver's `incumbents=` to the start-of-run snapshot makes
    `abarca` resolve `distinct` and lands BOTH labels in the registry with no
    alias record: the durable artifacts below are the witness, not the prompt.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(type="Concept", title="LLM Mechanics", content="model behavior")
        first_object = NodeCandidate(type="Concept", title="Context Window", content="context")
        second_object = NodeCandidate(type="Concept", title="Prompt Budget", content="budget")
        edges = [
            EdgeCandidate(
                type="engloba",
                src_ref=subject.candidate_id,
                dst_ref=first_object.candidate_id,
            ),
            EdgeCandidate(
                type="abarca",
                src_ref=subject.candidate_id,
                dst_ref=second_object.candidate_id,
            ),
        ]

        def _reply_for(label: str, prompt: str) -> str:
            if label == "abarca" and "engloba | " in prompt:
                return _resolution_reply("same", target="engloba")
            return _resolution_reply("distinct")

        provider = _PerLabelResolvingProvider(_reply_for)
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, first_object, second_object], edges),
        ).remember(_doc(vault, "# LLM Mechanics\n\nContext and budget matter.\n"))

        assert provider.resolved_labels == ["engloba", "abarca"]
        # Durable artifact 1: the second label never entered the registry,
        # because it folded onto one minted moments earlier in the same run.
        labels = _registry_labels(vault)
        assert "engloba" in labels
        assert "abarca" not in labels
        # Durable artifact 2: the fold record names the in-run mint as target.
        alias = _alias_records(vault)
        assert [(item["subject_predicate"], item["object_predicate"]) for item in alias] == [
            ("abarca", "engloba")
        ]
        assert alias[0]["status"] == "auto"
        # Durable artifact 3: both relations committed under `engloba`.
        assert sorted(item.type for item in vault.store.list_edges(src=subject.candidate_id)) == [
            "engloba",
            "engloba",
        ]
        # And the premise itself, stated directly: the second prompt listed the
        # first label; the first prompt could not have.
        assert "engloba | " in provider.resolution_prompts[1]
        assert "engloba | " not in provider.resolution_prompts[0]
    finally:
        vault.close()


def test_third_label_canonical_writes_no_alias_record_at_ingest(tmp_path: Path) -> None:
    """End-to-end half of `to_alias_record`'s shared fold test: a `same` verdict
    naming a third `canonical` folds nothing and leaves no durable record."""

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply(
                "same",
                target="includes",
                canonical="contains_everything",
            ),
        )
        Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        ).remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"))

        assert _alias_records(vault) == []
        # Nothing folded, so the status quo holds: the label is minted and the
        # relation is queued for review.
        assert "engloba" in _registry_labels(vault)
        row = _resolution_rows(vault)[0]
        assert row["payload"]["canonical"] == "contains_everything"
        assert row["payload"]["folded"] is False
    finally:
        vault.close()


def test_predicate_resolver_llm_call_is_traced_and_costed(tmp_path: Path) -> None:
    """The resolver is an LLM caller in the ingest run, so it is wrapped in
    `_tracked_provider` like every sibling (merge judge, relation curator,
    correction judge). An untracked caller is how an unbounded judge loop once
    ran for 30+ minutes here without appearing in a single event or in the
    construction-cost total.
    """

    vault = Vault.init(tmp_path / "v")
    try:
        subject, object_, edge = _two_concept_edge("engloba")
        provider = _ResolvingCuratorProvider(
            canonical_predicate="engloba",
            resolution=_resolution_reply("same", target="includes"),
        )
        events: list[dict] = []
        result = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject, object_], [edge]),
        ).remember(_doc(vault, "# LLM Mechanics\n\nContext window matters.\n"), on_event=events.append)

        requests = [
            event
            for event in events
            if event["kind"] == "llm_request"
            and event["summary"].startswith("Predicate Resolver")
        ]
        responses = [
            event
            for event in events
            if event["kind"] == "llm_response"
            and event["summary"].startswith("Predicate Resolver")
        ]
        assert len(requests) == 1
        assert len(responses) == 1
        assert requests[0]["payload"]["block"]["stage"] == "predicate_resolution"
        # The resolution prompt really is what was traced.
        assert any(
            "Registered predicates" in message["content"]
            for message in requests[0]["payload"]["messages"]
        )
        # And the call is counted, not free.
        assert result.outcome["construction_cost"]["completion_calls"] >= 1
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# Fix B1: a vault that resolves to the built-in defaults with an EMPTY model
# must be refused BEFORE any provider is dialed.
# ─────────────────────────────────────────────────────────────────────────────


def test_remember_refuses_empty_model_default_vault_before_dialing_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LLMDefaults.model`` is "" (discovery-first), so a vault created with
    the application defaults resolves to provider=openai + a real api_base and
    NO model. Ingesting used to dial ``openai/`` and fail per block with a
    misleading ``provider_unavailable`` ledger reason. The pre-flight must raise
    CompanionError naming the vault path and ``llm.defaults.model``, with zero
    provider construction and zero completion calls."""
    import okto_neuron.llm as llm_mod

    vault = Vault.init(tmp_path / "v")
    try:
        from okto_neuron.config import LLMDefaults

        resolved = Companion(vault, provider=StubLLM())._vault_config().llm.resolved("extraction")
        assert resolved.provider == LLMDefaults().provider
        assert not str(resolved.model or "").strip(), "fixture assumption: empty default model"

        class _NeverDialProvider:
            model = ""
            api_base = "http://127.0.0.1:8123/v1"
            model_id = ""

            def complete(self, *args: object, **kwargs: object) -> str:
                raise AssertionError(
                    "no completion may be attempted for a vault with no model configured"
                )

        monkeypatch.setattr(llm_mod, "get_provider", lambda *a, **k: _NeverDialProvider())

        # No injected provider and no injected extractor: exactly the shape a
        # fresh MCP-created vault has.
        companion = Companion(vault)
        with pytest.raises(CompanionError) as exc_info:
            companion.remember(_doc(vault))

        message = str(exc_info.value)
        assert str(vault.path) in message
        assert "llm.defaults.model" in message
    finally:
        vault.close()


def test_remember_with_configured_model_is_not_refused_by_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The B1 pre-flight is narrow: a vault that DOES name a model proceeds to
    build its provider exactly as before."""
    import okto_neuron.llm as llm_mod

    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nllm:\n  defaults:\n    model: some-real-model\n",
            encoding="utf-8",
        )
        built: list[object] = []

        class _SpyProvider:
            model = "some-real-model"
            api_base = "http://127.0.0.1:8123/v1"
            model_id = "some-real-model"

            def complete(self, *args: object, **kwargs: object) -> str:
                return "{}"

        def _get_provider(resolved: object) -> object:
            built.append(resolved)
            return _SpyProvider()

        monkeypatch.setattr(llm_mod, "get_provider", _get_provider)

        companion = Companion(vault)
        try:
            companion.remember(_doc(vault))
        except CompanionError as exc:
            assert "llm.defaults.model" not in str(exc), (
                "the empty-model pre-flight must not fire for a configured vault"
            )
        assert built, "a provider must still be built for a configured vault"
        assert getattr(built[0], "model", None) == "some-real-model", (
            "the configured model must be what gets dialed"
        )
    finally:
        vault.close()
