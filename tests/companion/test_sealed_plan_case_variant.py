"""Regression: a re-mention of an entity that differs from the stored node only
in display casing must not wedge the vault.

``_candidate_id`` hashes the folded title (ADR 0040), so "Turtles" and
"turtles" with the same content share one node id. The sealed-plan applier
used to compare the RAW title of the node already on disk against the
candidate, raise "node artifact differs from sealed plan", leave the plan
unreceipted, and fail every later ``remember`` on the vault (they must resume
that plan first). Seen on LoCoMo conv-44 with a hosted model, where a later
session re-extracted "Jack Russell mixes" after an earlier one had committed
"Jack Russell Mixes".

Model-free: a scripted extractor and StubLLM run the real ``remember`` path
(Tier-0/1/2 dedup, curation, plan sealing, apply, receipts).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron import companion as companion_module
from okto_neuron.companion import Companion
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.errors import IngestError
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import StubLLM
from okto_neuron.semantic_surface import exact_surface_key
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

_EMBED_DIM = 384
_CONTENT = "Pets owned by the narrator."


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _KeyedEmbedder:
    """Deterministic: texts with the same folded form embed identically,
    unrelated texts land far apart, so only true re-mentions look similar."""

    dim = _EMBED_DIM

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(exact_surface_key(text).encode("utf-8")).digest()
        return [(digest[i % len(digest)] - 127.5) / 127.5 for i in range(_EMBED_DIM)]


class _TitleExtractor:
    """One ``ENTITY <title>`` line -> one Concept candidate with fixed content,
    plus one literal claim about it (a node with no accepted relationship is
    parked for review, never committed)."""

    _RE = re.compile(r"ENTITY\s+(.+)")

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        match = self._RE.search(text)
        if match is None:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        node = NodeCandidate(
            type="Concept",
            title=match.group(1).strip(),
            content=_CONTENT,
            provenance=provenance or Provenance(),
        )
        claim = EdgeCandidate(
            type="has_value",
            src_ref=node.candidate_id,
            dst_literal="kept as pets",
            provenance=provenance or Provenance(),
        )
        return ExtractionResult(node_candidates=[node], edge_candidates=[claim])


def _companion(vault: Vault) -> Companion:
    return Companion(
        vault, provider=StubLLM(), extractor=_TitleExtractor(), embedder=_KeyedEmbedder()
    )


def _note(vault: Vault, name: str, title: str) -> Path:
    path = Path(vault.path) / name
    path.write_text(f"ENTITY {title}\n", encoding="utf-8")
    return path


def _ledger_path(vault: Vault) -> Path:
    return Path(vault.path) / ".marginalia" / "candidate-ledger.jsonl"


def _id(title: str) -> str:
    return NodeCandidate(type="Concept", title=title, content=_CONTENT).candidate_id


def test_case_variant_remention_does_not_wedge_later_ingests(tmp_path: Path) -> None:
    assert _id("Turtles") == _id("turtles"), "precondition: identity folds the title"
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _companion(vault)
        companion.remember(_note(vault, "session-1.md", "Turtles"))
        stored = vault.store.get_node(_id("Turtles"))
        assert stored is not None and stored.title == "Turtles"

        # The re-mention with different casing: same id, node already on disk.
        companion.remember(_note(vault, "session-2.md", "turtles"))
        # Nothing may be left sealed-but-unreceipted, or every later ingest
        # has to resume it first.
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
        # The collision actually happened: the second plan created the same
        # node id and its receipt says the artifact was already present.
        receipts = [
            row
            for row in map(json.loads, _ledger_path(vault).read_text().splitlines())
            if row.get("kind") == "operation_receipt"
            and row.get("operation") == "create_node"
            and (row.get("result") or {}).get("node_id") == _id("Turtles")
        ]
        assert [r["status"] for r in receipts] == ["applied", "already_present"]
        # The stored display title is not rewritten by the later mention.
        assert vault.store.get_node(_id("Turtles")).title == "Turtles"

        # A later, unrelated ingest still goes through and commits.
        companion.remember(_note(vault, "session-3.md", "Snakes"))
        assert vault.store.get_node(_id("Snakes")) is not None
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_same_id_with_different_content_still_fails_the_guard(tmp_path: Path) -> None:
    """The guard still catches a node that is NOT the artifact the plan names:
    same id, different content can only mean the store was altered."""
    vault = Vault.init(tmp_path / "v")
    try:
        vault.store.add_node(
            Node(id=_id("Turtles"), type="Concept", title="Turtles", content="something else")
        )
        with pytest.raises(ValueError, match="node artifact differs from sealed plan"):
            _companion(vault).remember(_note(vault, "session-1.md", "turtles"))
    finally:
        vault.close()


class _ReviewOrClaimExtractor(_TitleExtractor):
    """``PARK <title>`` yields the Concept with no claim, so it is parked for
    manual review; ``ENTITY <title>`` yields it with a claim, so it commits."""

    _PARK = re.compile(r"PARK\s+(.+)")

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        match = self._PARK.search(text)
        if match is None:
            return super().extract(text, provenance=provenance)
        node = NodeCandidate(
            type="Concept",
            title=match.group(1).strip(),
            content=_CONTENT,
            provenance=provenance or Provenance(),
        )
        return ExtractionResult(node_candidates=[node], edge_candidates=[])


def test_review_commit_after_another_document_created_the_same_id(tmp_path: Path) -> None:
    """A parked review item whose id another document has since committed
    (different casing, different provenance) commits as a re-mention of the
    stored node instead of failing the manual-review guard."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_ReviewOrClaimExtractor(),
            embedder=_KeyedEmbedder(),
        )
        parked = Path(vault.path) / "session-1.md"
        parked.write_text("PARK Turtles\n", encoding="utf-8")
        companion.remember(parked)
        assert [item.candidate_id for item in companion.review_queue()] == [_id("Turtles")]
        assert vault.store.get_node(_id("Turtles")) is None

        other = companion.remember(_note(vault, "session-2.md", "turtles"))
        stored = vault.store.get_node(_id("Turtles"))
        assert stored is not None and stored.title == "turtles"
        assert stored.provenance.source == other.document_id

        outcome = companion.resolve_review(_id("Turtles"), "commit")
        assert outcome.action == "committed"
        assert companion.review_queue() == []
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
        # The node already present is not rewritten by the review commit.
        kept = vault.store.get_node(_id("Turtles"))
        assert kept is not None and kept.title == "turtles"
        assert kept.provenance.source == other.document_id

        # A later ingest still commits.
        companion.remember(_note(vault, "session-3.md", "Snakes"))
        assert vault.store.get_node(_id("Snakes")) is not None
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_review_commit_onto_same_id_with_different_content_still_fails(tmp_path: Path) -> None:
    """The review guard still refuses a stored node under the pinned id that is
    not the artifact the id names (content differs: the store was altered)."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = Companion(
            vault,
            provider=StubLLM(),
            extractor=_ReviewOrClaimExtractor(),
            embedder=_KeyedEmbedder(),
        )
        parked = Path(vault.path) / "session-1.md"
        parked.write_text("PARK Turtles\n", encoding="utf-8")
        companion.remember(parked)
        vault.store.add_node(
            Node(id=_id("Turtles"), type="Concept", title="Turtles", content="something else")
        )
        with pytest.raises(ValueError, match="manual review node differs from the sealed artifact"):
            companion.resolve_review(_id("Turtles"), "commit")
        assert [item.candidate_id for item in companion.review_queue()] == [_id("Turtles")]
    finally:
        vault.close()


def _review_companion(vault: Vault) -> Companion:
    return Companion(
        vault,
        provider=StubLLM(),
        extractor=_ReviewOrClaimExtractor(),
        embedder=_KeyedEmbedder(),
    )


def _wedge_with_pre_fix_guard(
    vault: Vault, companion: Companion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the ledger state the pre-fix code left behind: a parked
    "Turtles", another document committing "turtles" under the same id, then a
    review commit whose guard compared the whole stored node and refused it."""
    parked = Path(vault.path) / "session-1.md"
    parked.write_text("PARK Turtles\n", encoding="utf-8")
    companion.remember(parked)
    companion.remember(_note(vault, "session-2.md", "turtles"))
    with monkeypatch.context() as patch:
        # The pre-fix verdict on this vault: the stored node's title and
        # provenance differ from the pinned candidate, so it was refused.
        patch.setattr(companion_module, "_is_stored_node_artifact", lambda *a, **k: False)
        with pytest.raises(ValueError, match="manual review node differs from the sealed artifact"):
            companion.resolve_review(_id("Turtles"), "commit")
    (open_plan,) = companion._candidate_ledger().unreceipted_commit_plans()
    assert open_plan.context.get("intent") == "manual_review_resolution"
    assert companion._candidate_ledger().operation_receipts(open_plan) == {}


def test_rerunning_the_review_resolution_heals_a_wedged_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery for a vault the pre-fix code already wedged: re-running the
    same resolution (same id, same action) resumes the open manual plan."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _review_companion(vault)
        _wedge_with_pre_fix_guard(vault, companion, monkeypatch)
        with pytest.raises(
            IngestError,
            match=(
                "a different sealed semantic plan must be resumed before new ingest work: "
                f"manual review {_id('Turtles')}"
            ),
        ):
            companion.remember(_note(vault, "session-3.md", "Snakes"))

        outcome = companion.resolve_review(_id("Turtles"), "commit")
        assert outcome.action == "committed"
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
        assert companion.review_queue() == []
        kept = vault.store.get_node(_id("Turtles"))
        assert kept is not None and kept.title == "turtles"

        companion.remember(_note(vault, "session-3.md", "Snakes"))
        assert vault.store.get_node(_id("Snakes")) is not None
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_rerun_keeps_a_reproposed_entry_it_did_not_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope mismatch: the open plan pinned one queue entry, the id was then
    re-proposed with different evidence, and the node already exists. The plan
    receipts already_present and leaves the re-proposed entry queued (it has
    no authority over an entry the operator never resolved); resolving that
    entry afterwards is a graph no-op that closes it."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = _review_companion(vault)
        _wedge_with_pre_fix_guard(vault, companion, monkeypatch)
        reproposal = NodeCandidate(
            type="Concept",
            title="TURTLES",
            content=_CONTENT,
            provenance=Provenance(source="session-4"),
        )
        assert reproposal.candidate_id == _id("Turtles")
        companion._review_queue().enqueue(reproposal, "contradiction")

        outcome = companion.resolve_review(_id("Turtles"), "commit")
        assert outcome.action == "committed"
        ledger = companion._candidate_ledger()
        assert ledger.unreceipted_commit_plans() == ()
        receipts = [
            row
            for row in map(json.loads, _ledger_path(vault).read_text().splitlines())
            if row.get("kind") == "operation_receipt" and row.get("operation") == "review_commit"
        ]
        assert [r["status"] for r in receipts] == ["already_present"]
        # The re-proposed entry is still queued, with its own evidence.
        (queued,) = companion.review_queue()
        assert queued.candidate_id == _id("Turtles")
        assert queued.title == "TURTLES" and queued.reason == "contradiction"
        before = vault.store.get_node(_id("Turtles"))

        # The operator resolves it; the stored node is not touched.
        assert companion.resolve_review(_id("Turtles"), "commit").action == "committed"
        assert companion.review_queue() == []
        assert ledger.unreceipted_commit_plans() == ()
        after = vault.store.get_node(_id("Turtles"))
        assert after is not None and before is not None
        assert after.model_dump(exclude={"created_at"}) == before.model_dump(exclude={"created_at"})
    finally:
        vault.close()
