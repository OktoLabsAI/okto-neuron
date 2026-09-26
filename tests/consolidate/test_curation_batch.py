"""ADR 0015 D4 — block-keyed batched curation with single-call fallback.

Unit tests for ``okto_neuron.curator_batch``: the deterministic validation gate
(``parse_batch_verdicts``), the cache-friendly batch prompt (excerpt section is
a byte-identical prefix vs the single-call prompt), the batching layer driven
end-to-end through a fake provider (batch chunking, fallback re-curation, order
alignment, batch_id/batch_size provenance, usage attribution), and the
``curation_batch_size`` config knob (default 1 = off, batching inert).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Sequence as _Sequence

import pytest
from pydantic import ValidationError

from okto_neuron.config._vault import ConsolidationConfig
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.curator import (
    CuratorVerdict,
    _build_curator_prompt,
    _build_relation_prompt,
)
from okto_neuron.curator_batch import (
    BatchCurationItem,
    batch_system_prompt,
    batch_verdict_response_format,
    build_batch_user_prompt,
    parse_batch_verdicts,
    plan_batches,
    relation_shared_prefix,
    run_batched_curation,
    shared_excerpt_section,
)
from okto_neuron.llm import _set_last_call_stats
from okto_neuron.resolve import ResolveOutcome
from okto_neuron.store.memory import InMemoryStore

# ── parse_batch_verdicts: deterministic validation gate ───────────────────────


def _reply(verdicts: list[dict]) -> str:
    return json.dumps({"verdicts": verdicts})


def _relation_verdict(candidate_id: str, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "action": "commit",
        "confidence": 0.9,
        "canonical_predicate": "lives_in",
        "predicate_definition": "The subject lives in the object place.",
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
        "reason": "directly stated",
    }
    payload.update(updates)
    return payload


def test_parse_batch_verdicts_happy_path() -> None:
    reply = _reply(
        [
            {"candidate_id": "a", "action": "commit", "confidence": 0.9, "reason": "ok"},
            {
                "candidate_id": "b",
                "action": "queue",
                "confidence": 0.2,
                "reason": "dup",
                "canonical_predicate": "Defined_As",
            },
        ]
    )
    out = parse_batch_verdicts(reply, ["a", "b"])
    assert set(out) == {"a", "b"}
    assert out["a"].action == "commit"
    assert out["a"].confidence == pytest.approx(0.9)
    assert out["a"].reason == "ok"
    # canonical_predicate goes through normalize_predicate, same as single-call.
    assert out["b"].canonical_predicate == "defines"


def test_parse_relation_batch_requires_complete_structured_evidence() -> None:
    complete = parse_batch_verdicts(
        _reply([_relation_verdict("r")]),
        ["r"],
        relation=True,
    )
    missing = _relation_verdict("r")
    del missing["predicate_supported"]
    incomplete = parse_batch_verdicts(
        _reply([missing]),
        ["r"],
        relation=True,
    )

    assert complete["r"].canonical_predicate == "lives_in"
    assert complete["r"].predicate_definition.startswith("The subject")
    assert complete["r"].subject_supported is True
    assert complete["r"].useful is True
    assert incomplete == {}


def test_parse_relation_batch_rejects_additional_fields() -> None:
    payload = _relation_verdict("r", unexpected="not in schema")

    assert (
        parse_batch_verdicts(
            _reply([payload]),
            ["r"],
            relation=True,
        )
        == {}
    )


def test_parse_batch_verdicts_missing_id_absent() -> None:
    reply = _reply([{"candidate_id": "a", "action": "commit", "confidence": 1.0, "reason": "r"}])
    out = parse_batch_verdicts(reply, ["a", "b"])
    assert "a" in out
    assert "b" not in out


def test_parse_batch_verdicts_duplicate_id_drops_all_entries() -> None:
    reply = _reply(
        [
            {"candidate_id": "a", "action": "commit", "confidence": 0.9, "reason": "x"},
            {"candidate_id": "a", "action": "queue", "confidence": 0.1, "reason": "y"},
        ]
    )
    assert parse_batch_verdicts(reply, ["a"]) == {}


def test_parse_batch_verdicts_invalid_then_valid_duplicate_still_drops_id() -> None:
    reply = _reply(
        [
            {"candidate_id": "a", "action": "invalid", "confidence": 0.9, "reason": "x"},
            {"candidate_id": "a", "action": "commit", "confidence": 0.9, "reason": "y"},
        ]
    )

    assert parse_batch_verdicts(reply, ["a"]) == {}


def test_parse_batch_verdicts_bad_action_or_confidence_dropped() -> None:
    reply = _reply(
        [
            {"candidate_id": "a", "action": "promote", "confidence": 0.9, "reason": "r"},
            {"candidate_id": "b", "action": "commit", "confidence": "high", "reason": "r"},
            {"candidate_id": "c", "action": "commit", "confidence": 7.0, "reason": "r"},
        ]
    )
    out = parse_batch_verdicts(reply, ["a", "b", "c"])
    assert "a" not in out
    assert "b" not in out
    # Out-of-range confidence clamps (mirrors single-call parsing), not dropped.
    assert out["c"].confidence == 1.0


def test_parse_batch_verdicts_unknown_id_and_extra_fields_not_fatal() -> None:
    reply = _reply(
        [
            {
                "candidate_id": "a",
                "action": "commit",
                "confidence": 0.5,
                "reason": "r",
                "extra": {"nested": True},
            },
            {"candidate_id": "ghost", "action": "commit", "confidence": 0.5, "reason": "r"},
        ]
    )
    out = parse_batch_verdicts(reply, ["a"])
    assert set(out) == {"a"}


def test_parse_batch_verdicts_non_json_returns_empty() -> None:
    assert parse_batch_verdicts("sorry, I cannot do that", ["a"]) == {}
    assert parse_batch_verdicts("", ["a"]) == {}
    assert parse_batch_verdicts('{"verdicts": "nope"}', ["a"]) == {}


def test_parse_batch_verdicts_tolerates_wrapping_text() -> None:
    reply = "Here you go:\n" + _reply(
        [{"candidate_id": "a", "action": "queue", "confidence": 0.3, "reason": "r"}]
    )
    assert parse_batch_verdicts(reply, ["a"])["a"].action == "queue"


def test_parse_batch_verdicts_tolerates_markdown_fence() -> None:
    reply = (
        "```json\n"
        + _reply([{"candidate_id": "a", "action": "queue", "confidence": 0.7, "reason": "r"}])
        + "\n```"
    )

    assert parse_batch_verdicts(reply, ["a"])["a"].action == "queue"


# ── batch prompt: excerpt prefix is byte-identical to the single-call prompt ──


def test_batch_prompt_excerpt_prefix_byte_identical_to_single_call() -> None:
    store = InMemoryStore()
    excerpt = "Paragraph one about Okto Neuron.\n\nParagraph two with detail."
    store.add_node(Node(id="block-1", type="Block", title="b", content=excerpt))
    candidate = NodeCandidate(
        type="Concept",
        title="Okto Neuron",
        content="a local-first knowledge graph",
        facets={"block_id": "block-1"},
    )
    single = _build_curator_prompt(
        candidate, ResolveOutcome(), store=store, edges=[], proposed_action=""
    )
    prefix = shared_excerpt_section(excerpt)
    assert single.startswith(prefix)
    batch = build_batch_user_prompt(excerpt, [(candidate.candidate_id, single), ("other", single)])
    # The cacheable [system + excerpt] prefix is preserved byte-for-byte.
    assert batch.startswith(prefix)
    assert batch[: len(prefix)] == single[: len(prefix)]
    # And the per-candidate body is tagged with its candidate_id.
    assert f"=== candidate_id: {candidate.candidate_id} ===" in batch
    assert batch.count("Source excerpt:\n") == 1  # excerpt paid once per batch


def test_batch_system_prompt_appends_without_rewriting_rules() -> None:
    base = "BASE CURATOR RULES"
    prompt = batch_system_prompt(base, 4)
    assert prompt.startswith(base)
    assert "independent" in prompt and "4" in prompt


def test_batch_verdict_response_format_shape() -> None:
    fmt = batch_verdict_response_format(3, relation=True)
    schema = fmt["json_schema"]["schema"]
    verdicts = schema["properties"]["verdicts"]
    assert verdicts["minItems"] == 3 and verdicts["maxItems"] == 3
    item_props = verdicts["items"]["properties"]
    assert "canonical_predicate" in item_props
    assert "predicate_definition" in item_props
    assert "subject_supported" in item_props
    assert "unsupported_inference" in item_props
    assert item_props["action"]["enum"] == ["commit", "queue"]
    node_fmt = batch_verdict_response_format(2, relation=False)
    node_props = node_fmt["json_schema"]["schema"]["properties"]["verdicts"]["items"]["properties"]
    assert "canonical_predicate" not in node_props
    assert node_props["action"]["enum"] == ["commit", "queue", "abstain"]


# ── plan_batches: block-keyed grouping ────────────────────────────────────────


def _item(cid: str, block: str | None, calls: list[str] | None = None) -> BatchCurationItem:
    def _single() -> CuratorVerdict:
        if calls is not None:
            calls.append(cid)
        return CuratorVerdict(action="queue", confidence=0.0, reason="single")

    excerpt = f"excerpt for {block}" if block else ""
    prompt = shared_excerpt_section(excerpt) + f"Candidate:\n  id: {cid}\n"
    return BatchCurationItem(
        candidate_id=cid,
        block_id=block,
        excerpt=excerpt,
        prompt=prompt,
        trace_context={"candidate_id": cid},
        single_call=_single,
    )


def test_plan_batches_groups_by_block_and_chunks() -> None:
    items = [
        _item("a", "b1"),
        _item("b", "b2"),
        _item("c", "b1"),
        _item("d", None),
        _item("e", "b1"),
    ]
    batches = plan_batches(items, 2)
    assert batches == [[0, 2], [4], [1], [3]]


# ── end-to-end through a fake provider ────────────────────────────────────────

_CANDIDATE_ID_RE = re.compile(r"=== candidate_id: (\S+) ===")


class _FakeBatchProvider:
    """Schema-honoring fake: answers every candidate_id found in the prompt,
    except ids listed in ``omit`` (simulating a model that drops one)."""

    def __init__(self, omit: set[str] | None = None) -> None:
        self.omit = omit or set()
        self.calls: list[list[str]] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        user = messages[-1].content
        ids = _CANDIDATE_ID_RE.findall(user)
        self.calls.append(ids)
        _set_last_call_stats({"prompt_tokens": 100, "completion_tokens": 10 * len(ids)})
        verdicts = [
            {
                "candidate_id": cid,
                "action": "commit",
                "confidence": 0.8,
                "reason": f"batched:{cid}",
            }
            for cid in ids
            if cid not in self.omit
        ]
        return json.dumps({"verdicts": verdicts})


def test_run_batched_curation_chunks_and_aligns_order() -> None:
    single_calls: list[str] = []
    items = [_item(f"c{i}", "block-1", single_calls) for i in range(6)]
    provider = _FakeBatchProvider()
    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=False,
        batch_size=4,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.2,
        max_tokens=2000,
    )
    # 6 same-block candidates at K=4 → exactly 2 LLM calls (4 + 2).
    assert [len(c) for c in provider.calls] == [4, 2]
    assert single_calls == []  # no fallback needed
    # Verdicts aligned to the original input order.
    assert [v.reason for v in verdicts] == [f"batched:c{i}" for i in range(6)]
    # Batch provenance: batch_id/batch_size on every batched verdict.
    assert all(m is not None and "batch_id" in m for m in meta)
    assert [m["batch_size"] for m in meta] == [4, 4, 4, 4, 2, 2]
    assert len({m["batch_id"] for m in meta}) == 2
    # Usage attribution: full batch usage on the FIRST member only; the shared
    # wall-clock duration lands on every member.
    assert verdicts[0].usage is not None and verdicts[4].usage is not None
    assert all(verdicts[i].usage is None for i in (1, 2, 3, 5))
    assert all(v.duration_s is not None for v in verdicts)


def test_run_batched_curation_falls_back_per_candidate() -> None:
    single_calls: list[str] = []
    items = [_item(f"c{i}", "block-1", single_calls) for i in range(3)]
    provider = _FakeBatchProvider(omit={"c1"})
    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=False,
        batch_size=4,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.2,
        max_tokens=2000,
    )
    assert len(provider.calls) == 1  # one batch call
    assert single_calls == ["c1"]  # the omitted candidate re-curated singly
    assert verdicts[1].reason == "single"
    assert meta[1] is None  # single-call provenance: no batch_id/batch_size
    assert meta[0] is not None and meta[2] is not None


def test_run_batched_curation_no_block_id_uses_single_path() -> None:
    single_calls: list[str] = []
    items = [_item("a", None, single_calls), _item("b", None, single_calls)]
    provider = _FakeBatchProvider()
    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=False,
        batch_size=4,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.2,
        max_tokens=2000,
    )
    assert provider.calls == []  # singleton batches never hit the batch path
    assert single_calls == ["a", "b"]
    assert meta == [None, None]
    assert len(verdicts) == 2


# ── ADR 0040 D6a: the registry block rides inside the stripped batch prefix ──


_REGISTRY_BLOCK = (
    "includes | subject_to_object | support=3 | The subject includes the object as a part.\n"
    "requires | subject_to_object | support=1 | The subject requires the object."
)


class _FakeRelationBatchProvider:
    """Schema-honoring relation fake that also records the prompts it was sent."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        user = messages[-1].content
        self.prompts.append(user)
        ids = _CANDIDATE_ID_RE.findall(user)
        _set_last_call_stats({"prompt_tokens": 100, "completion_tokens": 10 * len(ids)})
        return _reply([_relation_verdict(cid) for cid in ids])


def _relation_item(
    cid: str,
    excerpt: str,
    registry_block: str,
    calls: list[str],
) -> BatchCurationItem:
    def _single() -> CuratorVerdict:
        calls.append(cid)
        return CuratorVerdict(action="queue", confidence=0.0, reason="single")

    prompt = relation_shared_prefix(excerpt, registry_block) + (
        f"Relationship kind: topology\nPredicate/type: engloba\n\nCandidate:\n  id: {cid}\n"
    )
    return BatchCurationItem(
        candidate_id=cid,
        block_id="block-1",
        excerpt=excerpt,
        prompt=prompt,
        trace_context={"candidate_id": cid},
        single_call=_single,
    )


def test_relation_batch_pays_the_registry_block_once_not_once_per_member() -> None:
    """The whole point of the `curator_batch` change, tested where it is wired.

    ``iter_batched_curation`` must hand ``build_batch_user_prompt`` the RELATION
    prefix (excerpt + known predicates), not the excerpt alone. The strip is a
    silent no-op on mismatch: drop the ``prefix=`` argument and nothing raises —
    the batch simply carries K copies of the registry block, which is the cost
    regression this exists to prevent. So assert on the prompt the provider
    actually received.
    """

    excerpt = "excerpt for block-1"
    single_calls: list[str] = []
    items = [_relation_item(f"c{i}", excerpt, _REGISTRY_BLOCK, single_calls) for i in range(3)]
    provider = _FakeRelationBatchProvider()

    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=True,
        registry_block=_REGISTRY_BLOCK,
        batch_size=3,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.0,
        max_tokens=2000,
    )

    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    # Paid once, for three members — not three times.
    assert prompt.count("Known predicates (reuse by definition):") == 1
    assert prompt.count(_REGISTRY_BLOCK) == 1
    assert prompt.count("Source excerpt:\n") == 1
    # And it is still a byte-identical PREFIX, so the provider prefix cache the
    # single-call path warms is reused by the batch call.
    assert prompt.startswith(relation_shared_prefix(excerpt, _REGISTRY_BLOCK))
    # The batch really did the work: no per-candidate fallback.
    assert single_calls == []
    assert [verdict.action for verdict in verdicts] == ["commit", "commit", "commit"]
    assert all(item is not None and "batch_id" in item for item in meta)


def test_relation_shared_prefix_is_byte_identical_to_the_single_call_prompt() -> None:
    """The prefix is only strippable while it matches `_build_relation_prompt`
    byte for byte; a reordering there turns the strip into a silent no-op."""

    store = InMemoryStore()
    excerpt = "Okto Neuron includes a ledger."
    store.add_node(Node(id="block-1", type="Block", title="b", content=excerpt))
    store.add_node(Node(id="src-1", type="Concept", title="Okto Neuron", content="kg"))
    store.add_node(Node(id="dst-1", type="Concept", title="Ledger", content="audit trail"))
    candidate = EdgeCandidate(
        type="engloba",
        src_ref="src-1",
        dst_ref="dst-1",
        block_id="block-1",
    )
    single = _build_relation_prompt(
        candidate,
        store=store,
        node_candidates={},
        registry_block=_REGISTRY_BLOCK,
    )

    assert single.startswith(relation_shared_prefix(excerpt, _REGISTRY_BLOCK))


def test_run_batched_curation_inert_at_batch_size_one() -> None:
    single_calls: list[str] = []
    items = [_item(f"c{i}", "block-1", single_calls) for i in range(3)]
    provider = _FakeBatchProvider()
    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=False,
        batch_size=1,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.2,
        max_tokens=2000,
    )
    # Inert: the provider sees zero batch calls; every candidate goes through
    # its own single call, exactly today's behavior.
    assert provider.calls == []
    assert single_calls == ["c0", "c1", "c2"]
    assert all(m is None for m in meta)
    assert all(v.reason == "single" for v in verdicts)


def test_batch_timeout_and_single_fallback_share_total_concurrency() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def _enter() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)

    def _leave() -> None:
        nonlocal active
        with lock:
            active -= 1

    class _SlowBatchProvider:
        def complete(self, messages: _Sequence, **kwargs: object) -> str:
            _enter()
            try:
                time.sleep(0.08)
                ids = _CANDIDATE_ID_RE.findall(messages[-1].content)
                return _reply(
                    [
                        {
                            "candidate_id": cid,
                            "action": "commit",
                            "confidence": 0.9,
                            "reason": "late batch",
                        }
                        for cid in ids
                    ]
                )
            finally:
                _leave()

    def _slow_item(cid: str, block: str) -> BatchCurationItem:
        def _single() -> CuratorVerdict:
            _enter()
            try:
                time.sleep(0.02)
                return CuratorVerdict(action="queue", confidence=0.0, reason="fallback")
            finally:
                _leave()

        return BatchCurationItem(
            candidate_id=cid,
            block_id=block,
            excerpt=f"excerpt {block}",
            prompt=f"Candidate:\n  id: {cid}\n",
            trace_context={"candidate_id": cid},
            single_call=_single,
        )

    items = [
        _slow_item(f"c{block}-{member}", f"b{block}") for block in range(3) for member in range(2)
    ]
    verdicts, meta = run_batched_curation(
        items,
        provider=_SlowBatchProvider(),
        system_prompt="SYS",
        relation=False,
        batch_size=2,
        max_concurrent=2,
        timeout_s=0.02,
        temperature=0.2,
        max_tokens=2000,
    )

    assert peak == 2
    assert [verdict.reason for verdict in verdicts] == ["fallback"] * 6
    assert meta == [None] * 6


def test_one_failed_batch_uses_configured_fallback_parallelism() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def _single() -> CuratorVerdict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return CuratorVerdict(action="queue", confidence=0.0, reason="fallback")
        finally:
            with lock:
                active -= 1

    items = [
        BatchCurationItem(
            candidate_id=f"c{index}",
            block_id="only-block",
            excerpt="shared excerpt",
            prompt=f"Candidate:\n  id: c{index}\n",
            trace_context={"candidate_id": f"c{index}"},
            single_call=_single,
        )
        for index in range(8)
    ]

    class _InvalidBatchProvider:
        def complete(self, messages: _Sequence, **kwargs: object) -> str:
            return "not json"

    verdicts, meta = run_batched_curation(
        items,
        provider=_InvalidBatchProvider(),
        system_prompt="SYS",
        relation=False,
        batch_size=8,
        max_concurrent=4,
        timeout_s=1.0,
        temperature=0.2,
        max_tokens=2000,
    )

    assert peak == 4
    assert [verdict.reason for verdict in verdicts] == ["fallback"] * 8
    assert meta == [None] * 8


# ── config knob ───────────────────────────────────────────────────────────────


def test_curation_batch_size_config_defaults_and_bounds() -> None:
    assert ConsolidationConfig().curation_batch_size == 1
    assert ConsolidationConfig(curation_batch_size=8).curation_batch_size == 8
    with pytest.raises(ValidationError):
        ConsolidationConfig(curation_batch_size=0)
    with pytest.raises(ValidationError):
        ConsolidationConfig(curation_batch_size=33)


# ── RELATION_BATCH_MAX: relation batches are capped, and the capped batches run
# in parallel. Measured 2026-09-22 (docs/remote-providers.md): verdicts inside
# one relation batch move together, so a 25-edge single-block document at
# curation_batch_size 32 went out as ONE call and came back 0/25 committed. ────

from okto_neuron.config._capacity import (  # noqa: E402 - grouped with the tests that use them
    RELATION_BATCH_MAX,
    curation_batch_notice,
    effective_curation_batch_size,
)
from okto_neuron.curator_batch import RELATION_BATCH_MAX as REEXPORTED_RELATION_BATCH_MAX  # noqa: E402


def test_relation_batches_are_capped_whatever_is_configured() -> None:
    assert RELATION_BATCH_MAX == 4
    assert REEXPORTED_RELATION_BATCH_MAX == RELATION_BATCH_MAX
    assert effective_curation_batch_size(32, relation=True) == RELATION_BATCH_MAX
    assert effective_curation_batch_size(3, relation=True) == 3
    # Node curation keeps what the user configured: no measured cliff there.
    assert effective_curation_batch_size(32, relation=False) == 32
    # Absent or unusable values mean "batching off", never a crash.
    assert effective_curation_batch_size(None, relation=True) == 1
    assert effective_curation_batch_size("x", relation=True) == 1
    assert effective_curation_batch_size(0, relation=False) == 1


def test_curation_batch_notice_names_the_clamp_and_is_silent_otherwise() -> None:
    assert curation_batch_notice(None) is None
    assert curation_batch_notice(1) is None
    assert curation_batch_notice(RELATION_BATCH_MAX) is None
    notice = curation_batch_notice(32)
    assert notice is not None
    assert "32" in notice and f"capped at {RELATION_BATCH_MAX}" in notice


def test_a_25_edge_single_block_document_plans_into_several_batches() -> None:
    items = [_relation_item(f"c{i:02d}", "x", _REGISTRY_BLOCK, []) for i in range(25)]
    # The plan the directive names: cap 8 → 4 batches (8 + 8 + 8 + 1).
    assert [len(b) for b in plan_batches(items, 8)] == [8, 8, 8, 1]
    # What actually ships for a configured 32: capped to 4 → 7 batches.
    size = effective_curation_batch_size(32, relation=True)
    assert [len(b) for b in plan_batches(items, size)] == [4, 4, 4, 4, 4, 4, 1]


class _ConcurrencyTrackingRelationProvider(_FakeRelationBatchProvider):
    """Relation fake that records how many batch calls were in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.batch_sizes: list[int] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(0.05)
            ids = _CANDIDATE_ID_RE.findall(messages[-1].content)
            with self._lock:
                self.batch_sizes.append(len(ids))
            return super().complete(messages, **kwargs)
        finally:
            with self._lock:
                self._in_flight -= 1


def test_capped_relation_batches_actually_run_concurrently() -> None:
    """Earlier the same session, curation_max_concurrent=8 sat idle because each
    document's relations were ONE batch. Capped, a 25-edge block becomes six
    4-candidate batches (plus one singleton on the single-call path), and those
    six must overlap in flight, not run one after another."""

    excerpt = "excerpt for block-1"
    single_calls: list[str] = []
    items = [_relation_item(f"c{i:02d}", excerpt, _REGISTRY_BLOCK, single_calls) for i in range(25)]
    provider = _ConcurrencyTrackingRelationProvider()

    verdicts, meta = run_batched_curation(
        items,
        provider=provider,
        system_prompt="SYS",
        relation=True,
        registry_block=_REGISTRY_BLOCK,
        batch_size=effective_curation_batch_size(32, relation=True),
        max_concurrent=4,
        timeout_s=30.0,
        temperature=0.2,
        max_tokens=2000,
    )

    assert sorted(provider.batch_sizes) == [4, 4, 4, 4, 4, 4]
    assert provider.max_in_flight > 1
    # The 25th candidate is a singleton batch: it takes the single-call path.
    assert single_calls == ["c24"]
    assert len(verdicts) == 25
    assert [m["batch_size"] for m in meta if m] == [4] * 24
