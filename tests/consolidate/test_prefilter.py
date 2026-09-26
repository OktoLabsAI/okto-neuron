"""ADR 0015 D2 — deterministic curation pre-filter.

Pure-helper unit tests for ``okto_neuron.consolidate.prefilter`` plus
integration tests through the real ``Companion.remember()`` path (fake
extractor, stub embedder, fake curator providers — NO network, NO mocks of
the pipeline itself). Demotion must always mean *queue* (reviewable), never
silent discard.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.config import VaultConfig
from okto_neuron.config._vault import ConsolidationConfig, PrefilterConfig
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import LEDGER_FILENAME
from okto_neuron.consolidate.prefilter import (
    collapse_near_dup_literals,
    count_title_mentions,
    demoted_predicate_reason,
    is_established_re_mention,
    is_near_dup,
    is_trivial_node,
    normalize_for_near_dup,
)
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.curator import normalize_predicate
from okto_neuron.extract import ExtractionResult
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

DEMOTE_DEFAULTS = ["discusses", "mentions", "mentioned", "states", "stated"]


# ── pure helpers ───────────────────────────────────────────────────────────────


def test_normalize_for_near_dup_folds_case_whitespace_punctuation() -> None:
    assert normalize_for_near_dup("  Alex,   Rivera! ") == "alex rivera"
    assert is_near_dup("19% slower.", "19  % SLOWER")
    assert not is_near_dup("19% slower", "20% slower")


def test_rule1_demoted_predicate_raw_and_canonical() -> None:
    # Raw casefolded predicate on the list.
    assert demoted_predicate_reason("Mentions", DEMOTE_DEFAULTS) is not None
    # Canonical form on the list (caller passes normalize_predicate output).
    reason = demoted_predicate_reason(
        "Mentioned",
        DEMOTE_DEFAULTS,
        canonical_predicate=normalize_predicate("Mentioned"),
    )
    assert reason is not None
    # Knowledge-bearing predicates are untouched.
    assert demoted_predicate_reason("defines", DEMOTE_DEFAULTS) is None
    # Empty demote list disables the rule.
    assert demoted_predicate_reason("mentions", []) is None


def test_rule2_trivial_node_predicate() -> None:
    trivial = NodeCandidate(type="Concept", title="Foo", content="a short sentence")
    multi = NodeCandidate(type="Concept", title="Foo", content="One. Two sentences.")
    long_one = NodeCandidate(type="Concept", title="Foo", content="x" * 200)
    kwargs = {"min_mentions": 2, "max_trivial_node_chars": 160}
    assert is_trivial_node(trivial, 1, **kwargs)
    # Enough mentions → keep.
    assert not is_trivial_node(trivial, 2, **kwargs)
    # Multi-sentence or long content → keep.
    assert not is_trivial_node(multi, 1, **kwargs)
    assert not is_trivial_node(long_one, 1, **kwargs)
    # min_mentions=1 means the rule is off.
    assert not is_trivial_node(trivial, 1, min_mentions=1, max_trivial_node_chars=160)


def test_rule2_count_title_mentions_uses_normalized_titles() -> None:
    cands = [
        NodeCandidate(type="Agent", title="Alex"),
        NodeCandidate(type="Agent", title=" ALEX "),
        NodeCandidate(type="Concept", title="Graph_Store"),
        NodeCandidate(type="Concept", title="Graph Store"),
        NodeCandidate(type="Concept", title="GraphStore"),
        NodeCandidate(type="Agent", title="Mariana"),
    ]
    counts = count_title_mentions(cands)
    assert counts["alex"] == 2
    assert counts["graph store"] == 2
    assert counts["graphstore"] == 1
    assert counts["mariana"] == 1


def test_rule3_collapses_same_block_near_dup_literals_only() -> None:
    first = EdgeCandidate(type="states", src_ref="n1", dst_literal="19% slower", block_id="b1")
    dup = EdgeCandidate(type="States", src_ref="n1", dst_literal="19%  Slower.", block_id="b1")
    other_block = EdgeCandidate(
        type="states", src_ref="n1", dst_literal="19% slower", block_id="b2"
    )
    other_literal = EdgeCandidate(
        type="states", src_ref="n1", dst_literal="20% slower", block_id="b1"
    )
    topology = EdgeCandidate(type="uses", src_ref="n1", dst_ref="n2", block_id="b1")
    no_block = EdgeCandidate(type="states", src_ref="n1", dst_literal="19% slower")

    kept, superseded = collapse_near_dup_literals(
        [first, dup, other_block, other_literal, topology, no_block]
    )
    assert kept == [first, other_block, other_literal, topology, no_block]
    assert superseded == [(dup, first)]


def test_rule4_established_re_mention_predicate() -> None:
    node = Node(
        id="agent-1",
        type="Agent",
        title="Alex",
        content="a chat participant",
        facets={"role": "principal engineer"},
    )
    same_content = NodeCandidate(type="Agent", title="Alex", content="A chat participant!")
    facet_dup = NodeCandidate(type="Agent", title="Alex", content="Principal Engineer")
    title_only = NodeCandidate(type="Agent", title="Alex", content="alex")
    empty = NodeCandidate(type="Agent", title="Alex", content="")
    novel = NodeCandidate(type="Agent", title="Alex", content="moved to Lisbon in 2024")
    assert is_established_re_mention(same_content, node)
    assert is_established_re_mention(facet_dup, node)
    assert is_established_re_mention(title_only, node)
    assert is_established_re_mention(empty, node)
    assert not is_established_re_mention(novel, node)


# ── config ─────────────────────────────────────────────────────────────────────


def test_prefilter_config_defaults() -> None:
    cfg = ConsolidationConfig().prefilter
    assert cfg.enabled is False
    assert cfg.demote_predicates == DEMOTE_DEFAULTS
    assert cfg.min_mentions == 1
    assert cfg.max_trivial_node_chars == 160
    assert cfg.established_entity_fastpath is True


def test_prefilter_config_rejects_unknown_keys_and_bad_values() -> None:
    with pytest.raises(ValidationError):
        PrefilterConfig(unknown_knob=True)
    with pytest.raises(ValidationError):
        PrefilterConfig(min_mentions=0)
    with pytest.raises(ValidationError):
        PrefilterConfig(max_trivial_node_chars=0)


def test_prefilter_config_parses_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "okto-neuron.yaml"
    config_path.write_text(
        "\n".join(
            [
                "marginalia_yaml_version: 1",
                "consolidation:",
                "  prefilter:",
                "    enabled: true",
                "    demote_predicates: [mentions]",
                "    min_mentions: 2",
                "    max_trivial_node_chars: 120",
                "    established_entity_fastpath: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cfg = VaultConfig.load(config_path).consolidation.prefilter
    assert cfg.enabled is True
    assert cfg.demote_predicates == ["mentions"]
    assert cfg.min_mentions == 2
    assert cfg.max_trivial_node_chars == 120
    assert cfg.established_entity_fastpath is False


# ── integration through Companion.remember() ──────────────────────────────────


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _CountingCuratorProvider:
    """Fake curator/judge provider that counts node- and relation-curator calls."""

    model = "prefilter-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self) -> None:
        self.node_curator_calls = 0
        self.relation_curator_calls = 0

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        user = getattr(messages[-1], "content", "") if messages else ""
        if "candidate curator" in system:
            self.node_curator_calls += 1
            return '{"action":"commit","confidence":0.95,"reason":"test commit"}'
        if "relationship curator" in system:
            self.relation_curator_calls += 1
            predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in user.splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return json.dumps(
                {
                    "action": "commit",
                    "confidence": 0.95,
                    "canonical_predicate": predicate,
                    "predicate_definition": "The subject has the relation to the object.",
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
                }
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _FakeExtractor:
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
    path = Path(vault.path) / "note.md"
    path.write_text(text, encoding="utf-8")
    return path


def _ledger_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_config(vault: Vault, *lines: str) -> None:
    (Path(vault.path) / "okto-neuron.yaml").write_text(
        "\n".join(["marginalia_yaml_version: 1", *lines, ""]), encoding="utf-8"
    )


def _prefilter_comparisons(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if record["kind"] == "comparison" and record["method"] == "prefilter"
    ]


def _edge_ids_by(records: list[dict], **payload_match: object) -> set[str]:
    """Ledger edge-candidate ids whose payload matches the given fields.

    The companion stamps provenance into candidate payloads before hashing, so
    ids can't be precomputed from the raw EdgeCandidate in tests."""

    return {
        record["candidate_id"]
        for record in records
        if record["kind"] == "candidate"
        and record["candidate_kind"] == "edge"
        and all(record["payload"].get(k) == v for k, v in payload_match.items())
    }


def test_rule1_demoted_predicate_is_queued_without_llm_call(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _write_config(vault, "consolidation:", "  prefilter:", "    enabled: true")
        src = NodeCandidate(type="Agent", title="Alex", content="a participant")
        dst = NodeCandidate(type="Concept", title="Lisbon", content="a city")
        demoted = EdgeCandidate(type="mentions", src_ref=src.candidate_id, dst_ref=dst.candidate_id)
        kept = EdgeCandidate(type="defines", src_ref=src.candidate_id, dst_literal="a person")

        provider = _CountingCuratorProvider()
        companion = Companion(
            vault, provider=provider, extractor=_FakeExtractor([src, dst], [demoted, kept])
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        demoted_ids = _edge_ids_by(records, type="mentions")
        assert len(demoted_ids) == 1
        demoted_id = next(iter(demoted_ids))
        prefiltered = [
            r for r in _prefilter_comparisons(records) if r["candidate_id"] == demoted_id
        ]
        assert len(prefiltered) == 1
        assert prefiltered[0]["verdict"] == "queue"
        assert "demote_predicates" in prefiltered[0]["reason"]
        # Demoted candidate never reached the relation curator…
        relation_reviewed = {
            r["candidate_id"]
            for r in records
            if r["kind"] == "comparison" and r["method"] == "relation_curator"
        }
        assert demoted_id not in relation_reviewed
        # …but the kept candidate did (the fan-out still ran for it).
        assert provider.relation_curator_calls >= 1
        # Queued, not dropped: terminal candidate state is queued.
        demoted_states = [
            r["state"]
            for r in records
            if r["kind"] == "candidate" and r["candidate_id"] == demoted_id
        ]
        assert demoted_states[-1] == "queued"
    finally:
        vault.close()


def test_rule1_inert_when_prefilter_disabled(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        src = NodeCandidate(type="Agent", title="Alex", content="a participant")
        dst = NodeCandidate(type="Concept", title="Lisbon", content="a city")
        edge = EdgeCandidate(type="mentions", src_ref=src.candidate_id, dst_ref=dst.candidate_id)

        companion = Companion(
            vault,
            provider=_CountingCuratorProvider(),
            extractor=_FakeExtractor([src, dst], [edge]),
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert not [r for r in _prefilter_comparisons(records) if r["verdict"] == "queue"]
        edge_ids = _edge_ids_by(records, type="mentions")
        assert edge_ids
        relation_reviewed = {
            r["candidate_id"]
            for r in records
            if r["kind"] == "comparison" and r["method"] == "relation_curator"
        }
        assert edge_ids & relation_reviewed
    finally:
        vault.close()


def test_rule2_low_signal_node_queued_without_curator_call(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _write_config(
            vault,
            "consolidation:",
            "  prefilter:",
            "    enabled: true",
            "    min_mentions: 2",
        )
        trivial = NodeCandidate(type="Concept", title="Foo", content="a short sentence")
        companion = Companion(
            vault,
            provider=_CountingCuratorProvider(),
            extractor=_FakeExtractor([trivial]),
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        prefiltered = [
            r for r in _prefilter_comparisons(records) if r["candidate_id"] == trivial.candidate_id
        ]
        assert len(prefiltered) == 1
        assert prefiltered[0]["verdict"] == "queue"
        # Skipped the node-curator LLM but stayed in the flow (queued ≙ 0.0).
        node_curated = {
            r["candidate_id"]
            for r in records
            if r["kind"] == "comparison" and r["method"] == "curator"
        }
        assert trivial.candidate_id not in node_curated
        states = [
            r["state"]
            for r in records
            if r["kind"] == "candidate" and r["candidate_id"] == trivial.candidate_id
        ]
        assert states[-1] == "queued"
    finally:
        vault.close()


def test_rule3_near_dup_literals_superseded_before_relation_curation(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _write_config(vault, "consolidation:", "  prefilter:", "    enabled: true")
        src = NodeCandidate(type="Concept", title="Latency", content="a measurement")
        # NOTE: the per-block extraction dedup already collapses edges keyed on
        # the RAW (type, src_ref, dst_ref, block) — so the literal near-dups
        # that survive to rule 3 are predicate surface variants of the same
        # claim, like "defines" vs "Defines".
        first = EdgeCandidate(
            type="defines", src_ref=src.candidate_id, dst_literal="19% slower", block_id="b1"
        )
        dup = EdgeCandidate(
            type="Defines", src_ref=src.candidate_id, dst_literal="19%  SLOWER.", block_id="b1"
        )
        companion = Companion(
            vault,
            provider=_CountingCuratorProvider(),
            extractor=_FakeExtractor([src], [first, dup]),
        )
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        dup_ids = _edge_ids_by(records, dst_literal="19%  SLOWER.")
        first_ids = _edge_ids_by(records, dst_literal="19% slower")
        assert len(dup_ids) == 1 and len(first_ids) == 1
        dup_id, first_id = next(iter(dup_ids)), next(iter(first_ids))
        superseded = [
            r
            for r in _prefilter_comparisons(records)
            if r["candidate_id"] == dup_id and r["verdict"] == "superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0]["target_ref"] == first_id
        # The dup never reached the live relation-curation fan-out; the
        # survivor did. (The dup still gets a synthetic audit_only record from
        # the existing superseded-relationship machinery — no LLM call.)
        relation_reviewed = {
            r["candidate_id"]
            for r in records
            if r["kind"] == "comparison"
            and r["method"] == "relation_curator"
            and not r["payload"].get("audit_only")
        }
        assert dup_id not in relation_reviewed
        assert first_id in relation_reviewed
        dup_states = [
            r["state"] for r in records if r["kind"] == "candidate" and r["candidate_id"] == dup_id
        ]
        assert dup_states[-1] == "superseded"
    finally:
        vault.close()


def test_rule4_established_entity_fastpath_skips_curator_audit(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        # Audit LLM ON so the fast path is what skips the call, not the default.
        _write_config(vault, "consolidation:", "  audit_superseded_nodes_with_llm: true")
        vault.store.add_node(
            Node(
                id="agent-alex",
                type="Agent",
                title="Alex",
                content="a chat participant",
            )
        )
        re_mention = NodeCandidate(type="Agent", title="Alex", content="A chat participant")
        provider = _CountingCuratorProvider()
        companion = Companion(vault, provider=provider, extractor=_FakeExtractor([re_mention]))
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        fastpath = [
            r
            for r in _prefilter_comparisons(records)
            if r["candidate_id"] == re_mention.candidate_id
        ]
        assert len(fastpath) == 1
        assert fastpath[0]["verdict"] == "fastpath_commit"
        assert fastpath[0]["reason"] == ("established entity re-mention; curator verdict foregone")
        # No curator LLM call happened (rule 4 default-on, audit skipped).
        assert provider.node_curator_calls == 0
        # Treated as a curator commit → merged-away terminal state "superseded".
        states = [
            r["state"]
            for r in records
            if r["kind"] == "candidate" and r["candidate_id"] == re_mention.candidate_id
        ]
        assert states[-1] == "superseded"
    finally:
        vault.close()


def test_rule4_skipped_when_candidate_brings_novel_content(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _write_config(vault, "consolidation:", "  audit_superseded_nodes_with_llm: true")
        vault.store.add_node(
            Node(
                id="agent-alex",
                type="Agent",
                title="Alex",
                content="a chat participant",
            )
        )
        novel = NodeCandidate(type="Agent", title="Alex", content="moved to Lisbon in 2024")
        provider = _CountingCuratorProvider()
        companion = Companion(vault, provider=provider, extractor=_FakeExtractor([novel]))
        companion.remember(_doc(vault))

        records = _ledger_records(vault)
        assert not [
            r for r in _prefilter_comparisons(records) if r["candidate_id"] == novel.candidate_id
        ]
        # Novel content went through the curator audit LLM as before.
        assert provider.node_curator_calls >= 1
    finally:
        vault.close()
