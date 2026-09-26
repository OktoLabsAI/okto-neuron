"""Contract tests for the autonomous companion.

These pin the signed surface (shapes + error modes). The companion is pinned to
the deterministic :class:`StubLLM` so the tests stay offline and independent of
whether a local oMLX is running: with StubLLM the LLMExtractor parses no JSON,
so the candidate pipeline yields zero candidates — exercising the
graceful-degrade path the contract guarantees.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from okto_neuron import Vault
from okto_neuron.companion import (
    Answer,
    Companion,
    Correlation,
    RememberResult,
    ReviewItemNotFoundError,
)
from okto_neuron.llm import StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _companion(vault: Vault) -> Companion:
    return Companion(vault, provider=StubLLM())


def _doc(vault: Vault, text: str = "# Title\n\nbody about knowledge graphs.\n") -> Path:
    p = Path(vault.path) / "note.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_remember_returns_result_with_document_id(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        result = _companion(vault).remember(_doc(vault))
        assert isinstance(result, RememberResult)
        assert result.document_id
        # StubLLM yields no parseable candidates -> empty pipeline (a clean
        # parse failure, NOT a provider error — StubLLM.complete() succeeds).
        assert result.committed == 0
        assert result.queued == 0
        assert result.outcomes == ()
        # Fix 2 (issue #4 — visible zero-parse): both counters are visible on
        # every RememberResult, pinning the shape the MCP remember tool and
        # REST /remember payload rely on. ``provider_failures`` is 0 (StubLLM
        # never raises); ``empty_after_retry_blocks`` is 1 — StubLLM's reply
        # never parses as candidates, so the sole block hits the clean
        # empty-after-retry path (a real anomaly, correctly surfaced here —
        # NOT a provider error).
        assert result.provider_failures == 0
        assert result.empty_after_retry_blocks == 1
    finally:
        vault.close()


def test_get_provider_wraps_transparently_and_remember_unchanged_without_callbacks(
    tmp_path: Path,
) -> None:
    """Fix 1 regression pin: ``_get_provider`` now always wraps every
    provider (including the injected fast path) in ``_StepLabelledProvider``
    so every completion's log line can be correlated to its pipeline step.
    The wrap must be fully transparent — ``.model`` preserved — and
    ``remember()`` with ``on_progress``/``on_event`` both ``None`` (the
    default) must behave byte-for-byte as pinned before the observability
    changes landed."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _companion(vault)
        provider = companion._get_provider("extraction")
        assert provider.model == StubLLM.model

        result = companion.remember(_doc(vault))
        assert isinstance(result, RememberResult)
        assert result.document_id
        assert result.committed == 0
        assert result.queued == 0
        assert result.outcomes == ()
        assert result.provider_failures == 0
        assert result.empty_after_retry_blocks == 1
    finally:
        vault.close()


def test_vault_config_defaults_when_config_absent(tmp_path: Path) -> None:
    # A configless vault is unchanged — defaults apply, no error.
    vault = Vault.init(tmp_path / "v")
    try:
        cfg = _companion(vault)._vault_config()
        assert cfg.llm is not None
    finally:
        vault.close()


def test_vault_config_raises_on_malformed_config(tmp_path: Path) -> None:
    # A *present but malformed* config must fail loud, not silently fall back to
    # defaults (a typo'd llm.model would otherwise route every call to a wrong
    # model). Phase 1 / ADR 0002 D8 fail-loud directive.
    from okto_neuron.errors import ConfigParseError

    vault = Vault.init(tmp_path / "v")
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nbad:\n\tchild: value\n", encoding="utf-8"
        )
        with pytest.raises(ConfigParseError):
            _companion(vault)._vault_config()
    finally:
        vault.close()


def test_remember_accepts_sensitivity_flag(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        result = _companion(vault).remember(_doc(vault), sensitivity="local_only")
        assert isinstance(result, RememberResult)
    finally:
        vault.close()


def test_recall_proxies_vault_query(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _companion(vault)
        companion.remember(_doc(vault))
        hits = companion.recall("knowledge graphs", k=5)
        assert isinstance(hits, list)
        assert len(hits) <= 5
    finally:
        vault.close()


def test_ask_returns_answer_with_citations_matching_hits(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _companion(vault)
        companion.remember(_doc(vault))
        answer = companion.ask("what is this about?", k=5)
        assert isinstance(answer, Answer)
        # Citations/hits are exactly the query hits; only `text` is synthesised.
        assert tuple(h.node.id for h in answer.hits) == answer.citations
        # StubLLM synthesises a deterministic non-empty answer when there are hits.
        if answer.hits:
            assert answer.text
    finally:
        vault.close()


class _PartnerExtractor:
    """Deterministic stand-in for the LLM extractor: emits the single
    ``Okto Neuron -[project_partner]-> Jordan Lee Carter`` relationship for any
    block whose text names the partner, so ``remember`` mints exactly one
    byte-anchored ``project_partner`` Claim with no network."""

    def extract(self, text: str, *, provenance=None):  # type: ignore[no-untyped-def]
        from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
        from okto_neuron.core.schema import Provenance
        from okto_neuron.extract import ExtractionResult

        prov = provenance or Provenance()
        if "Jordan Lee Carter" not in text:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        subj = NodeCandidate(type="Concept", title="Okto Neuron", content=text, provenance=prov)
        obj = NodeCandidate(type="Agent", title="Jordan Lee Carter", content=text, provenance=prov)
        edge = EdgeCandidate(
            type="project_partner",
            src_ref=subj.candidate_id,
            dst_ref=obj.candidate_id,
            provenance=prov,
        )
        return ExtractionResult(node_candidates=[subj, obj], edge_candidates=[edge])


_PARTNER_NOTE = "# Okto Neuron\n\nThe project partner on Okto Neuron is Jordan Lee Carter.\n"


def test_recall_partner_returns_jl_carter_with_byte_provenance(tmp_path: Path) -> None:
    """Deterministic contract for the headline recall:

    ``remember`` the partner note (StubLLM + stub embedder + a deterministic
    extractor) → a ``project_partner`` Claim naming Jordan Lee Carter is minted,
    anchored to the partner Block with a VALID byte range → ``recall`` surfaces
    it with path + byte-range provenance.
    """
    import hashlib

    from okto_neuron.companion import Companion

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "partner.md"
        note.write_text(_PARTNER_NOTE, encoding="utf-8")

        companion = Companion(vault, provider=StubLLM(), extractor=_PartnerExtractor())
        companion.remember(note)

        # A project_partner Claim naming the partner was minted.
        claims = list(vault.store.list_nodes(type="Claim"))
        partner_claims = [
            c
            for c in claims
            if c.facets.get("P") == "project_partner" and "Jordan Lee Carter" in c.title
        ]
        assert partner_claims, f"no project_partner Claim minted; got {[c.title for c in claims]}"
        claim = partner_claims[0]

        # The Claim anchors to a Block whose byte range hashes back to the source.
        raw = note.read_bytes()
        block = vault.store.get_node(claim.facets["block_id"])
        assert block is not None and block.type == "Block"
        bs = int(block.facets["byte_start"])
        be = int(block.facets["byte_end"])
        assert be > bs
        assert hashlib.sha256(raw[bs:be]).hexdigest() == str(block.facets["content_hash"])

        # recall surfaces Jordan Lee Carter WITH byte-range provenance.
        hits = companion.recall("who is the partner on Okto Neuron?", k=10)
        assert hits
        partner_hits = [h for h in hits if "Jordan Lee Carter" in h.node.title]
        assert partner_hits, (
            f"recall did not surface the partner; titles={[h.node.title for h in hits]}"
        )
        hit = partner_hits[0]
        assert hit.provenance.path == str(note.resolve())
        assert hit.byte_end > hit.byte_start
        assert len(hit.content_hash) == 64
        assert hashlib.sha256(raw[hit.byte_start : hit.byte_end]).hexdigest() == hit.content_hash
    finally:
        vault.close()


def test_review_queue_empty_when_nothing_parked(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        assert _companion(vault).review_queue() == []
    finally:
        vault.close()


def test_resolve_review_raises_when_nothing_parked(tmp_path: Path) -> None:
    from okto_neuron.consolidate.ledger import CandidateLedger

    vault = Vault.init(tmp_path / "v")
    try:
        with pytest.raises(ReviewItemNotFoundError):
            _companion(vault).resolve_review("missing", "commit")
        records = CandidateLedger(Path(vault.path) / ".marginalia").records()
        assert not any(record["kind"] == "commit_plan" for record in records)
    finally:
        vault.close()


def _seal_legacy_review_link(vault: Vault, candidate, target_id: str):  # type: ignore[no-untyped-def]
    """Create one schema-valid pre-ADR-0040 link plan for recovery tests."""
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.core.schema import Edge

    qdir = Path(vault.path) / ".marginalia"
    correlation = Correlation(kind="similar", target_id=target_id, score=0.9)
    queue = ReviewQueue(qdir, vault.store)
    queue.enqueue(candidate, "low_confidence", (correlation,))
    scope = queue.resolution_scope(candidate.candidate_id)
    ledger = CandidateLedger(qdir)
    run_id = ledger.start_run(
        document_id=f"review:{candidate.candidate_id}",
        source="review_queue",
        blocks_total=0,
        model="manual",
    )
    pinned_edge = Edge(type="relates_to", src=candidate.candidate_id, dst=target_id)
    plan_id = ledger.record_commit_plan(
        run_id,
        operations=[
            {
                "operation": "review_link",
                "candidate_kind": "node",
                "candidate_id": candidate.candidate_id,
                "type": candidate.type,
                "title": candidate.title,
                "confidence": 0.9,
                "target_ref": target_id,
                "reason": "low_confidence",
                "review_item": {
                    "candidate": candidate.model_dump(mode="json"),
                    "reason": "low_confidence",
                    "correlations": [correlation.model_dump(mode="json")],
                },
                "node": candidate.to_node().model_dump(mode="json"),
                "edge": pinned_edge.model_dump(mode="json"),
            }
        ],
        context={
            "intent": "manual_review_resolution",
            "document_id": f"review:{candidate.candidate_id}",
            "review_action": "link",
            "target_ref": target_id,
            **scope,
        },
    )
    plan = next(value for value in ledger.unreceipted_commit_plans() if value.plan_id == plan_id)
    return ledger, queue, plan, pinned_edge


def test_manual_link_action_is_retired_without_sealing_or_writing(tmp_path: Path) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "retired-link")
    try:
        candidate = NodeCandidate(type="Concept", title="similar is not a relation")
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence", ())

        with pytest.raises(ValueError, match="unknown review action 'link'"):
            _companion(vault).resolve_review(candidate.candidate_id, "link")  # type: ignore[arg-type]

        assert vault.store.get_node(candidate.candidate_id) is None
        assert ReviewQueue(qdir, vault.store).read(candidate.candidate_id)
        assert not any(row["kind"] == "commit_plan" for row in CandidateLedger(qdir).records())
    finally:
        vault.close()


def test_legacy_review_link_plan_is_readable_but_never_executable(tmp_path: Path) -> None:
    from okto_neuron.companion import _apply_sealed_semantic_plan
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.core.schema import Node
    from okto_neuron.predicates import PredicateRegistry

    vault = Vault.init(tmp_path / "legacy-link-fail-closed")
    try:
        candidate = NodeCandidate(type="Concept", title="legacy candidate")
        target_id = "legacy-target"
        vault.store.add_node(Node(id=target_id, type="Concept", title="target"))
        ledger, queue, plan, pinned_edge = _seal_legacy_review_link(vault, candidate, target_id)

        with pytest.raises(ValueError, match="not executable under semantic policy"):
            _apply_sealed_semantic_plan(
                plan,
                store=vault.store,
                ledger=ledger,
                registry=PredicateRegistry(vault.path),
                review_queue=queue,
            )

        assert vault.store.get_node(candidate.candidate_id) is None
        assert (
            list(
                vault.store.list_edges(
                    src=pinned_edge.src,
                    type=pinned_edge.type,
                    dst=pinned_edge.dst,
                )
            )
            == []
        )
        assert ledger.operation_receipts(plan) == {}
        assert ledger.unreceipted_commit_plans() == (plan,)
    finally:
        vault.close()


def test_untouched_legacy_review_link_can_be_abandoned_for_supported_action(
    tmp_path: Path,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / "legacy-link-abandon")
    try:
        candidate = NodeCandidate(type="Concept", title="legacy candidate")
        target_id = "legacy-target"
        vault.store.add_node(Node(id=target_id, type="Concept", title="target"))
        ledger, _queue, old_plan, pinned_edge = _seal_legacy_review_link(
            vault, candidate, target_id
        )

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "discard")

        assert outcome.action == "queued"
        assert ReviewQueue(Path(vault.path) / ".marginalia", vault.store).list() == []
        assert vault.store.get_node(candidate.candidate_id) is None
        assert (
            list(
                vault.store.list_edges(
                    src=pinned_edge.src,
                    type=pinned_edge.type,
                    dst=pinned_edge.dst,
                )
            )
            == []
        )
        abandoned = [row for row in ledger.records() if row["kind"] == "plan_abandoned"]
        assert len(abandoned) == 1
        assert abandoned[0]["plan_id"] == old_plan.plan_id
        assert abandoned[0]["reason"] == "unsupported_legacy_review_link"
        assert ledger.unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_touched_legacy_review_link_plan_requires_explicit_recovery(tmp_path: Path) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / "legacy-link-partial")
    try:
        candidate = NodeCandidate(type="Concept", title="partially written candidate")
        target_id = "legacy-target"
        vault.store.add_node(Node(id=target_id, type="Concept", title="target"))
        ledger, queue, old_plan, pinned_edge = _seal_legacy_review_link(vault, candidate, target_id)
        # Models a crash after the old applier wrote the candidate node but
        # before it wrote the generic edge or recorded an operation receipt.
        vault.store.add_node(candidate.to_node())

        with pytest.raises(RuntimeError, match="partially applied legacy review_link"):
            _companion(vault).resolve_review(candidate.candidate_id, "discard")

        assert queue.read(candidate.candidate_id)
        assert vault.store.get_node(candidate.candidate_id) is not None
        assert (
            list(
                vault.store.list_edges(
                    src=pinned_edge.src,
                    type=pinned_edge.type,
                    dst=pinned_edge.dst,
                )
            )
            == []
        )
        assert ledger.operation_receipts(old_plan) == {}
        assert ledger.unreceipted_commit_plans() == (old_plan,)
        assert not any(row["kind"] == "plan_abandoned" for row in ledger.records())
    finally:
        vault.close()


@pytest.mark.parametrize(
    ("action", "expected_events"),
    [
        ("commit", ["enter", "exit"]),
        ("discard", []),
        ("merge", []),
    ],
)
def test_resolve_review_guards_only_graph_writing_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_events: list[str],
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / action)
    try:
        candidate = NodeCandidate(type="Concept", title=f"candidate {action}")
        qdir = Path(vault.path) / ".marginalia"
        target_id = f"target-{action}"
        vault.store.add_node(Node(id=target_id, type="Concept", title=f"target for {action}"))
        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (Correlation(kind="similar", target_id=target_id, score=0.9),),
        )
        events: list[str] = []
        original_acknowledge = ReviewQueue.acknowledge
        store_type = type(vault.store)
        original_add_node = store_type.add_node
        resolve_snapshots: list[list[dict[str, object]]] = []

        def snapshot_plan() -> None:
            records = CandidateLedger(qdir).records()
            resolve_snapshots.append(records)
            plans = [record for record in records if record["kind"] == "commit_plan"]
            assert plans[-1]["operations"][0]["operation"] == f"review_{action}"
            assert not any(record["kind"] == "commit_record" for record in records)
            assert not any(
                record["kind"] == "candidate"
                and record.get("candidate_id") == candidate.candidate_id
                for record in records
            )

        def acknowledge_after_plan(queue: ReviewQueue, candidate_id: str) -> None:
            if action in {"discard", "merge"}:
                snapshot_plan()
            original_acknowledge(queue, candidate_id)

        def add_node_after_plan(store, node):
            if node.id == candidate.candidate_id:
                snapshot_plan()
            return original_add_node(store, node)

        monkeypatch.setattr(ReviewQueue, "acknowledge", acknowledge_after_plan)
        monkeypatch.setattr(store_type, "add_node", add_node_after_plan)

        @contextmanager
        def guard():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        monkeypatch.setattr(vault, "_integrity_write_guard", guard)

        _companion(vault).resolve_review(candidate.candidate_id, action)  # type: ignore[arg-type]

        assert events == expected_events
        assert len(resolve_snapshots) == 1
        records = CandidateLedger(qdir).records()
        plan_offset, plan = next(
            (offset, record)
            for offset, record in enumerate(records)
            if record["kind"] == "commit_plan"
        )
        operation_receipt_offset = next(
            offset for offset, record in enumerate(records) if record["kind"] == "operation_receipt"
        )
        commit_offset = next(
            offset for offset, record in enumerate(records) if record["kind"] == "commit_record"
        )
        terminal_offset = next(
            offset
            for offset, record in enumerate(records)
            if record["kind"] == "candidate"
            and record.get("candidate_id") == candidate.candidate_id
        )
        assert plan["context"]["intent"] == "manual_review_resolution"
        assert plan["context"]["candidate_id"] == candidate.candidate_id
        assert plan["context"]["document_id"] == f"review:{candidate.candidate_id}"
        assert plan["context"]["review_action"] == action
        assert plan["context"]["entry_sha256"].startswith("sha256:")
        assert plan_offset < operation_receipt_offset < commit_offset < terminal_offset
    finally:
        vault.close()


def test_resolve_review_resumes_when_post_write_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "audit-failure")
    try:
        candidate = NodeCandidate(type="Concept", title="retain after failed audit")
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence")

        @contextmanager
        def failing_guard():
            yield
            raise RuntimeError("post-write audit failed")

        with monkeypatch.context() as fault:
            fault.setattr(vault, "_integrity_write_guard", failing_guard)
            with pytest.raises(RuntimeError, match="post-write audit failed"):
                _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert vault.store.get_node(candidate.candidate_id) is not None
        assert [item.candidate_id for item in ReviewQueue(qdir, vault.store).list()] == [
            candidate.candidate_id
        ]
        ledger = CandidateLedger(qdir)
        assert len(ledger.unreceipted_commit_plans()) == 1
        records = ledger.records()
        assert any(record["kind"] == "commit_plan" for record in records)
        assert not any(record["kind"] == "operation_receipt" for record in records)
        assert not any(record["kind"] == "commit_record" for record in records)

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert outcome.action == "committed"
        assert ReviewQueue(qdir, vault.store).list() == []
        assert vault.store.get_node(candidate.candidate_id) is not None
        records = CandidateLedger(qdir).records()
        assert sum(record["kind"] == "commit_plan" for record in records) == 1
        assert sum(record["kind"] == "operation_receipt" for record in records) == 1
        commit = next(record for record in records if record["kind"] == "commit_record")
        assert commit["result"]["state"] == "committed"
        assert CandidateLedger(qdir).unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_resolve_review_resumes_when_queue_mutation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "queue-mutation-failure")
    try:
        candidate = NodeCandidate(type="Concept", title="retry after failure")
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence")

        def fail_acknowledge(*args, **kwargs):
            raise OSError("queue mutation failed")

        with monkeypatch.context() as fault:
            fault.setattr(ReviewQueue, "acknowledge", fail_acknowledge)
            with pytest.raises(OSError, match="queue mutation failed"):
                _companion(vault).resolve_review(candidate.candidate_id, "discard")

        assert [item.candidate_id for item in ReviewQueue(qdir, vault.store).list()] == [
            candidate.candidate_id
        ]
        ledger = CandidateLedger(qdir)
        assert len(ledger.unreceipted_commit_plans()) == 1
        records = ledger.records()
        assert not any(record["kind"] == "operation_receipt" for record in records)
        assert not any(record["kind"] == "commit_record" for record in records)

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "discard")

        assert outcome.action == "queued"
        assert ReviewQueue(qdir, vault.store).list() == []
        records = ledger.records()
        assert sum(record["kind"] == "commit_plan" for record in records) == 1
        assert sum(record["kind"] == "operation_receipt" for record in records) == 1
        commit = next(record for record in records if record["kind"] == "commit_record")
        assert commit["result"]["state"] == "dropped"
        assert ledger.unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_resolve_review_refuses_nested_write_guard(tmp_path: Path) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "nested-review")
    try:
        candidate = NodeCandidate(type="Concept", title="nested candidate")
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence")

        with vault._integrity_write_guard():  # noqa: SLF001 - regression boundary
            with pytest.raises(RuntimeError, match="must own the live-write"):
                _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert vault.store.get_node(candidate.candidate_id) is None
        assert [item.candidate_id for item in ReviewQueue(qdir, vault.store).list()] == [
            candidate.candidate_id
        ]
    finally:
        vault.close()


def test_concurrent_resolve_review_commits_candidate_once(tmp_path: Path) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "concurrent-review")
    candidate = NodeCandidate(type="Concept", title="one decision")
    qdir = Path(vault.path) / ".marginalia"
    ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence")
    start = threading.Barrier(3)
    outcomes = []
    errors: list[BaseException] = []

    def resolve() -> None:
        start.wait(timeout=5)
        try:
            outcomes.append(_companion(vault).resolve_review(candidate.candidate_id, "commit"))
        except BaseException as exc:  # noqa: BLE001 - concurrent outcome capture
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        assert [outcome.action for outcome in outcomes] == ["committed"]
        assert len(errors) == 1
        assert isinstance(errors[0], ReviewItemNotFoundError)
        assert ReviewQueue(qdir, vault.store).list() == []
        assert vault.store.get_node(candidate.candidate_id) is not None
        records = CandidateLedger(qdir).records()
        assert sum(record["kind"] == "commit_plan" for record in records) == 1
        assert sum(record["kind"] == "commit_record" for record in records) == 1
        assert (
            sum(
                record["kind"] == "candidate"
                and record.get("candidate_id") == candidate.candidate_id
                for record in records
            )
            == 1
        )
    finally:
        for thread in threads:
            thread.join(timeout=5)
        vault.close()


def test_resolve_review_dead_letter_remains_queued_and_records_queued_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "dead-letter-review")
    try:
        candidate = NodeCandidate(type="   ", title="invalid candidate")
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(candidate, "low_confidence")
        original_receipt = CandidateLedger.record_operation_receipt
        apply_snapshots: list[list[dict[str, object]]] = []

        def receipt_after_plan(ledger: CandidateLedger, *args, **kwargs) -> None:
            records = CandidateLedger(qdir).records()
            apply_snapshots.append(records)
            assert any(record["kind"] == "commit_plan" for record in records)
            assert not any(record["kind"] == "commit_record" for record in records)
            assert not any(
                record["kind"] == "candidate"
                and record.get("candidate_id") == candidate.candidate_id
                for record in records
            )
            original_receipt(ledger, *args, **kwargs)

        monkeypatch.setattr(
            CandidateLedger,
            "record_operation_receipt",
            receipt_after_plan,
        )

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert outcome.action == "queued"
        assert vault.store.get_node(candidate.candidate_id) is None
        assert [item.candidate_id for item in ReviewQueue(qdir, vault.store).list()] == [
            candidate.candidate_id
        ]
        records = CandidateLedger(qdir).records()
        candidate_records = [
            record
            for record in records
            if record.get("kind") == "candidate"
            and record.get("candidate_id") == candidate.candidate_id
        ]
        commit_records = [record for record in records if record.get("kind") == "commit_record"]
        assert len(apply_snapshots) == 1
        assert candidate_records[-1]["state"] == "queued"
        assert commit_records[-1]["result"]["state"] == "queued"
        plan_offset = next(
            offset for offset, record in enumerate(records) if record["kind"] == "commit_plan"
        )
        receipt_offset = next(
            offset for offset, record in enumerate(records) if record["kind"] == "operation_receipt"
        )
        assert (
            plan_offset
            < receipt_offset
            < records.index(commit_records[-1])
            < records.index(candidate_records[-1])
        )
    finally:
        vault.close()


@pytest.mark.parametrize("action", ["merge"])
def test_resolve_review_with_missing_target_preserves_intent(
    tmp_path: Path,
    action: str,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "missing-link-target")
    try:
        candidate = NodeCandidate(type="Concept", title="link later")
        correlation = Correlation(kind="similar", target_id="missing", score=0.8)
        qdir = Path(vault.path) / ".marginalia"
        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (correlation,),
        )

        outcome = _companion(vault).resolve_review(
            candidate.candidate_id,
            action,  # type: ignore[arg-type]
        )

        assert outcome.action == "queued"
        assert vault.store.get_node(candidate.candidate_id) is None
        assert [item.candidate_id for item in ReviewQueue(qdir, vault.store).list()] == [
            candidate.candidate_id
        ]
        records = CandidateLedger(qdir).records()
        candidate_records = [
            record
            for record in records
            if record.get("kind") == "candidate"
            and record.get("candidate_id") == candidate.candidate_id
        ]
        assert candidate_records[-1]["state"] == "queued"
        receipt = next(record for record in records if record["kind"] == "operation_receipt")
        assert receipt["status"] == "dead_lettered"
        assert CandidateLedger(qdir).unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_resolve_review_abandons_untouched_stale_queue_scope_and_reseals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.companion as companion_module
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "review-stale-untouched")
    try:
        qdir = Path(vault.path) / ".marginalia"
        candidate = NodeCandidate(type="Concept", title="scope changed before apply")
        old_evidence = Correlation(kind="similar", target_id="old-evidence", score=0.6)
        new_evidence = Correlation(kind="similar", target_id="new-evidence", score=0.95)
        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (old_evidence,),
        )
        old_scope = ReviewQueue(qdir, vault.store).resolution_scope(candidate.candidate_id)

        def crash_after_seal(*args, **kwargs):
            raise KeyboardInterrupt("crash after untouched review seal")

        with monkeypatch.context() as crash:
            crash.setattr(
                companion_module,
                "_apply_sealed_semantic_plan",
                crash_after_seal,
            )
            with pytest.raises(KeyboardInterrupt, match="untouched review seal"):
                _companion(vault).resolve_review(candidate.candidate_id, "commit")

        ledger = CandidateLedger(qdir)
        old_plan = ledger.unreceipted_commit_plans()[0]
        assert ledger.operation_receipts(old_plan) == {}

        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (new_evidence,),
        )
        new_scope = ReviewQueue(qdir, vault.store).resolution_scope(candidate.candidate_id)
        assert new_scope["entry_sha256"] != old_scope["entry_sha256"]

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert outcome.action == "committed"
        assert tuple(item.target_id for item in outcome.correlations) == ("new-evidence",)
        assert ReviewQueue(qdir, vault.store).list() == []
        assert vault.store.get_node(candidate.candidate_id) is not None
        records = ledger.records()
        plans = [row for row in records if row["kind"] == "commit_plan"]
        abandoned = [row for row in records if row["kind"] == "plan_abandoned"]
        receipts = [row for row in records if row["kind"] == "operation_receipt"]
        commits = [row for row in records if row["kind"] == "commit_record"]
        assert len(plans) == 2
        assert len(abandoned) == 1
        assert abandoned[0]["plan_id"] == old_plan.plan_id
        assert abandoned[0]["reason"] == "manual_review_queue_entry_changed"
        assert plans[1]["context"]["entry_sha256"] == new_scope["entry_sha256"]
        assert [row["plan_id"] for row in receipts] == [plans[1]["plan_id"]]
        assert [row["plan_id"] for row in commits] == [plans[1]["plan_id"]]
        assert ledger.unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_resolve_review_preserves_reenqueued_evidence_after_old_commit_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue

    vault = Vault.init(tmp_path / "review-reenqueued-after-ack")
    try:
        qdir = Path(vault.path) / ".marginalia"
        candidate = NodeCandidate(type="Concept", title="new evidence survives old commit")
        old_evidence = Correlation(kind="similar", target_id="old-evidence", score=0.6)
        new_evidence = Correlation(kind="similar", target_id="new-evidence", score=0.95)
        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (old_evidence,),
        )
        old_scope = ReviewQueue(qdir, vault.store).resolution_scope(candidate.candidate_id)

        def crash_before_receipt(*args, **kwargs):
            raise KeyboardInterrupt("crash after acknowledge before receipt")

        with monkeypatch.context() as crash:
            crash.setattr(
                CandidateLedger,
                "record_operation_receipt",
                crash_before_receipt,
            )
            with pytest.raises(KeyboardInterrupt, match="before receipt"):
                _companion(vault).resolve_review(candidate.candidate_id, "commit")

        ledger = CandidateLedger(qdir)
        old_plan = ledger.unreceipted_commit_plans()[0]
        assert ledger.operation_receipts(old_plan) == {}
        assert ReviewQueue(qdir, vault.store).list() == []
        assert vault.store.get_node(candidate.candidate_id) is not None

        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            (new_evidence,),
        )
        new_scope = ReviewQueue(qdir, vault.store).resolution_scope(candidate.candidate_id)
        assert new_scope["entry_sha256"] != old_scope["entry_sha256"]

        outcome = _companion(vault).resolve_review(candidate.candidate_id, "commit")

        assert outcome.action == "committed"
        assert tuple(item.target_id for item in outcome.correlations) == ("old-evidence",)
        queued = ReviewQueue(qdir, vault.store).list()
        assert len(queued) == 1
        assert queued[0].candidate_id == candidate.candidate_id
        assert tuple(item.target_id for item in queued[0].correlations) == ("new-evidence",)
        assert ReviewQueue(qdir, vault.store).resolution_scope(candidate.candidate_id) == new_scope
        records = ledger.records()
        assert sum(row["kind"] == "commit_plan" for row in records) == 1
        assert not any(row["kind"] == "plan_abandoned" for row in records)
        assert sum(row["kind"] == "operation_receipt" for row in records) == 1
        assert sum(row["kind"] == "commit_record" for row in records) == 1
        assert ledger.unreceipted_commit_plans() == ()
        assert sum(node.id == candidate.candidate_id for node in vault.store.list_nodes()) == 1
    finally:
        vault.close()


@pytest.mark.parametrize(
    ("action", "crash_boundary"),
    [
        *((action, "after_seal") for action in ("commit", "discard", "merge")),
        ("commit", "after_graph_write"),
        *((action, "before_ack") for action in ("commit", "discard", "merge")),
        *((action, "after_ack") for action in ("commit", "discard", "merge")),
        *((action, "after_receipt") for action in ("commit", "discard", "merge")),
    ],
)
def test_resolve_review_crash_boundaries_resume_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    crash_boundary: str,
) -> None:
    """Every durable manual-review boundary resumes the one sealed decision.

    ``KeyboardInterrupt`` deliberately escapes ordinary ``Exception`` handlers,
    matching the persistence shape left by SIGKILL while still letting pytest
    unwind process-local locks. Closing and reopening the vault before retry
    discards every in-memory queue/store snapshot.
    """

    import okto_neuron.companion as companion_module
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.core.schema import Node

    vault_path = tmp_path / f"review-{action}-{crash_boundary}"
    vault = Vault.init(vault_path)
    qdir = vault_path / ".marginalia"
    candidate = NodeCandidate(type="Concept", title=f"{action} crash candidate")
    target_id = f"target-{action}"
    try:
        vault.store.add_node(Node(id=target_id, type="Concept", title=f"target for {action}"))
        correlations = (
            (Correlation(kind="similar", target_id=target_id, score=0.9),)
            if action == "merge"
            else ()
        )
        ReviewQueue(qdir, vault.store).enqueue(
            candidate,
            "low_confidence",
            correlations,
        )

        with monkeypatch.context() as crash:
            if crash_boundary == "after_seal":

                def crash_after_seal(*args, **kwargs):
                    raise KeyboardInterrupt("crash after review plan seal")

                crash.setattr(
                    companion_module,
                    "_apply_sealed_semantic_plan",
                    crash_after_seal,
                )
            elif crash_boundary == "after_graph_write":
                store_type = type(vault.store)
                method_name = "add_node"
                original_write = getattr(store_type, method_name)

                def crash_after_graph_write(store, artifact, *args, **kwargs):
                    result = original_write(store, artifact, *args, **kwargs)
                    is_candidate_node = (
                        method_name == "add_node" and artifact.id == candidate.candidate_id
                    )
                    if is_candidate_node:
                        raise KeyboardInterrupt("crash after review graph write")
                    return result

                crash.setattr(store_type, method_name, crash_after_graph_write)
            elif crash_boundary in {"before_ack", "after_ack"}:
                original_acknowledge = ReviewQueue.acknowledge

                def crash_at_acknowledge(queue, resolved_id):
                    assert resolved_id == candidate.candidate_id
                    if crash_boundary == "before_ack":
                        raise KeyboardInterrupt("crash before review dequeue")
                    original_acknowledge(queue, resolved_id)
                    raise KeyboardInterrupt("crash after review dequeue")

                crash.setattr(ReviewQueue, "acknowledge", crash_at_acknowledge)
            elif crash_boundary == "after_receipt":

                def crash_before_commit_record(*args, **kwargs):
                    raise KeyboardInterrupt("crash after review receipt")

                crash.setattr(
                    CandidateLedger,
                    "record_commit",
                    crash_before_commit_record,
                )
            else:  # pragma: no cover - parameter table is closed above
                raise AssertionError(f"unknown crash boundary: {crash_boundary}")

            with pytest.raises(KeyboardInterrupt, match="crash"):
                _companion(vault).resolve_review(
                    candidate.candidate_id,
                    action,  # type: ignore[arg-type]
                )

        ledger = CandidateLedger(qdir)
        assert len(ledger.unreceipted_commit_plans()) == 1
        interrupted_records = ledger.records()
        assert sum(row["kind"] == "commit_plan" for row in interrupted_records) == 1
        assert not any(row["kind"] == "commit_record" for row in interrupted_records)
        expected_receipts = 1 if crash_boundary == "after_receipt" else 0
        assert (
            sum(row["kind"] == "operation_receipt" for row in interrupted_records)
            == expected_receipts
        )

        queue_present = bool(ReviewQueue(qdir, vault.store).list())
        assert queue_present is (crash_boundary not in {"after_ack", "after_receipt"})
        graph_should_exist = action == "commit" and crash_boundary != "after_seal"
        assert (vault.store.get_node(candidate.candidate_id) is not None) is graph_should_exist

        vault.close()
        vault = Vault.open(vault_path)
        outcome = _companion(vault).resolve_review(
            candidate.candidate_id,
            action,  # type: ignore[arg-type]
        )

        assert outcome.action == ("committed" if action == "commit" else "queued")
        assert ReviewQueue(qdir, vault.store).list() == []
        assert (vault.store.get_node(candidate.candidate_id) is not None) is (action == "commit")
        assert sum(node.id == candidate.candidate_id for node in vault.store.list_nodes()) == (
            1 if action == "commit" else 0
        )
        link_edges = list(
            vault.store.list_edges(
                src=candidate.candidate_id,
                type="relates_to",
                dst=target_id,
            )
        )
        assert len(link_edges) == 0

        records = CandidateLedger(qdir).records()
        assert sum(row["kind"] == "commit_plan" for row in records) == 1
        assert sum(row["kind"] == "operation_receipt" for row in records) == 1
        assert sum(row["kind"] == "commit_record" for row in records) == 1
        expected_state = {
            "commit": "committed",
            "discard": "dropped",
            "merge": "merged",
        }[action]
        commit = next(row for row in records if row["kind"] == "commit_record")
        assert commit["result"]["state"] == expected_state
        assert CandidateLedger(qdir).unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_integrity_scan_serializes_with_direct_writes(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "scan-lock")
    scan_entered = threading.Event()
    release_scan = threading.Event()
    write_entered = threading.Event()

    def scan() -> None:
        with vault._integrity_scan_guard():  # noqa: SLF001 - concurrency contract
            scan_entered.set()
            assert release_scan.wait(timeout=5)

    def write() -> None:
        with vault._integrity_write_guard():  # noqa: SLF001 - concurrency contract
            write_entered.set()

    scan_thread = threading.Thread(target=scan)
    write_thread = threading.Thread(target=write)
    try:
        scan_thread.start()
        assert scan_entered.wait(timeout=5)
        write_thread.start()
        assert not write_entered.wait(timeout=0.2)
        release_scan.set()
        assert write_entered.wait(timeout=5)
    finally:
        release_scan.set()
        scan_thread.join(timeout=5)
        write_thread.join(timeout=5)
        vault.close()
