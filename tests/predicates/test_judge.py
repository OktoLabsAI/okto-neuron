from __future__ import annotations

import pytest

from okto_neuron.llm import _set_last_call_stats
from okto_neuron.predicates import (
    ArgumentSignature,
    LLMPredicateJudge,
    PredicateCandidate,
    PredicateSample,
    SharedArgumentEvidence,
    SharedArgumentPair,
    parse_predicate_verdict,
)


class _ReplyProvider:
    model = "fake/predicate-judge"

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.users: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    def complete(
        self, messages, *, temperature: float = 0.0, max_tokens: int = 1024, **kwargs
    ) -> str:
        self.calls += 1
        self.kwargs.append(kwargs)
        self.users.append(next((m.content for m in messages if m.role == "user"), ""))
        _set_last_call_stats(
            {
                "prompt_tokens": self.calls,
                "completion_tokens": 2,
                "total_tokens": self.calls + 2,
            }
        )
        return self._replies.pop(0)


def _candidate() -> PredicateCandidate:
    return PredicateCandidate(
        predicate_a="wrote_to",
        predicate_b="sent_to",
        count_a=3,
        count_b=2,
        string_affinity=0.5,
        shared_evidence=SharedArgumentEvidence(
            same_order=1,
            swapped_order=0,
            pairs=(SharedArgumentPair("alice", "bob", same_order=1),),
        ),
        samples_a=(PredicateSample("claim-a", "Alice wrote to Bob", "letter excerpt"),),
        samples_b=(PredicateSample("claim-b", "Alice sent to Bob", "mail excerpt"),),
        signatures_a=(ArgumentSignature("Agent", "Agent", 3),),
        signatures_b=(ArgumentSignature("Agent", "Agent", 2),),
    )


def test_parse_predicate_verdict_valid_json() -> None:
    parsed = parse_predicate_verdict(
        'Predicate A means x.\n{"verdict": "same", "canonical": "wrote_to", '
        '"confidence": 0.91, "reason": "same directed relation"}'
    )

    assert parsed is not None
    assert parsed.verdict == "same"
    assert parsed.canonical == "wrote_to"
    assert parsed.confidence == pytest.approx(0.91)


def test_judge_same_consistent_high_confidence_auto_records_telemetry() -> None:
    provider = _ReplyProvider(
        '{"verdict": "same", "canonical": "wrote_to", "confidence": 0.91, '
        '"reason": "same directed relation"}',
        '{"verdict": "same", "canonical": "wrote_to", "confidence": 0.9, '
        '"reason": "same directed relation"}',
    )

    result = LLMPredicateJudge(provider).judge(_candidate())
    record = result.to_record()

    assert result.outcome == "auto"
    assert result.status == "auto"
    assert result.mapping == "exact_match"
    assert result.confidence == pytest.approx(0.9)
    assert result.duration_s >= 0
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert record.subject_predicate == "sent_to"
    assert record.object_predicate == "wrote_to"
    assert record.votes["forward"]["duration_s"] >= 0
    assert provider.calls == 2
    assert provider.kwargs[0]["enable_thinking"] is False
    assert provider.kwargs[0]["response_format"]["json_schema"]["name"] == (
        "marginalia_predicate_judge"
    )
    assert "Directional to symmetric collapse is forbidden" in provider.users[0]
    assert "Sample claims" in provider.users[0]


def test_malformed_judge_output_queues_unparseable() -> None:
    provider = _ReplyProvider("not json", "also not json")

    result = LLMPredicateJudge(provider).judge(_candidate())

    assert result.outcome == "queue_unparseable"
    assert result.status == "queued"
    assert result.mapping == "distinct"
    assert result.to_record().status == "queued"


def test_inconsistent_symmetric_orders_queue_inconsistent() -> None:
    provider = _ReplyProvider(
        '{"verdict": "same", "canonical": "wrote_to", "confidence": 0.92, "reason": "same"}',
        '{"verdict": "distinct", "canonical": "", "confidence": 0.93, "reason": "not same"}',
    )

    result = LLMPredicateJudge(provider).judge(_candidate())

    assert result.outcome == "queue_inconsistent"
    assert result.status == "queued"
    assert result.mapping == "distinct"


# ── 3.2: an unnormalized `canonical` from the judge must never reach the ─────
# off-graph alias ledger verbatim — it feeds admit_predicate's exact_mappings,
# which hard-requires predicate_label_key(label) == label on every entry.


def test_parse_predicate_verdict_normalizes_mixed_case_canonical() -> None:
    parsed = parse_predicate_verdict(
        '{"verdict": "same", "canonical": "Wrote To", "confidence": 0.9, '
        '"reason": "same directed relation"}'
    )

    assert parsed is not None
    assert parsed.canonical == "wrote_to"
    assert parsed.canonical_malformed is False


def test_parse_predicate_verdict_flags_non_normalizable_canonical() -> None:
    # "123" lexicalizes to itself but fails predicate_label_key's
    # ^[a-z][a-z0-9_]{0,79}$ requirement (must start with a letter) — a
    # non-empty canonical that cannot be normalized at all.
    parsed = parse_predicate_verdict(
        '{"verdict": "same", "canonical": "123", "confidence": 0.9, '
        '"reason": "same directed relation"}'
    )

    assert parsed is not None
    assert parsed.canonical == ""
    assert parsed.canonical_malformed is True


def test_judge_normalizes_mismatched_case_canonical_before_persisting() -> None:
    """Live repro for 3.2: the judge echoes the same predicate back with
    different casing/spacing on each call — a plausible, non-adversarial LLM
    habit, not a parse failure. Both votes must still be treated as
    consistent (post-normalization) and the persisted record's labels must
    satisfy predicate_label_key(label) == label, or admit_predicate raises
    ValueError for every predicate pair in the vault the next time the
    corrupted alias is loaded (see PredicateAliasIndex.alias_map /
    admission._mapping_snapshot)."""
    provider = _ReplyProvider(
        '{"verdict": "same", "canonical": "Wrote To", "confidence": 0.91, '
        '"reason": "same directed relation"}',
        '{"verdict": "same", "canonical": "wrote_to", "confidence": 0.9, '
        '"reason": "same directed relation"}',
    )

    result = LLMPredicateJudge(provider).judge(_candidate())
    record = result.to_record()

    # Casing/spacing noise alone must not be mistaken for a real disagreement.
    assert result.outcome == "auto"
    assert result.status == "auto"
    assert result.canonical == "wrote_to"
    from okto_neuron.curator import predicate_label_key

    assert predicate_label_key(record.subject_predicate) == record.subject_predicate
    assert predicate_label_key(record.object_predicate) == record.object_predicate
    assert record.object_predicate == "wrote_to"


def test_judge_same_with_non_normalizable_canonical_queues_instead_of_auto() -> None:
    """A canonical the judge itself cannot lexicalize (non-ASCII/digit-led
    noise) is a red flag about the whole verdict — reject it to the queue
    rather than auto-folding on confidence alone."""
    provider = _ReplyProvider(
        '{"verdict": "same", "canonical": "123", "confidence": 0.95, '
        '"reason": "same directed relation"}',
        '{"verdict": "same", "canonical": "123", "confidence": 0.94, '
        '"reason": "same directed relation"}',
    )

    result = LLMPredicateJudge(provider).judge(_candidate())

    assert result.outcome == "queue_unnormalizable"
    assert result.status == "queued"
    assert result.mapping == "exact_match"
    assert result.to_record().status == "queued"


def test_maintenance_judge_prompt_and_schema_are_unchanged() -> None:
    """ADR 0017 boundary pin.

    ADR 0040 D6a adds ingest-time predicate resolution as a SIBLING module.
    The maintenance sweep's judge — its system prompt, its response schema and
    its verdict-to-status mapping — is owned by the propose-only
    `predicate-propose` sweep and must not move. This is the cheapest proof
    that it did not.
    """

    from okto_neuron.predicates.judge import (
        _PREDICATE_JUDGE_SYSTEM,
        PREDICATE_JUDGE_RESPONSE_FORMAT,
        ParsedPredicateVerdict,
        PredicateJudgeVote,
        _gate_result,
    )

    assert _PREDICATE_JUDGE_SYSTEM.startswith(
        "You judge knowledge-graph predicate mappings. Prefer distinct when "
        "evidence is weak. Never collapse directional predicates into symmetric "
        "predicates."
    )
    assert "UNTRUSTED DATA:" in _PREDICATE_JUDGE_SYSTEM
    schema = PREDICATE_JUDGE_RESPONSE_FORMAT["json_schema"]
    assert schema["name"] == "marginalia_predicate_judge"
    assert schema["schema"]["properties"]["verdict"]["enum"] == [
        "same",
        "inverse",
        "narrower",
        "distinct",
    ]
    # The ingest resolver's own schema carries an extra `target` field; the
    # maintenance judge's must NOT have grown one.
    assert sorted(schema["schema"]["required"]) == [
        "canonical",
        "confidence",
        "reason",
        "verdict",
    ]

    candidate = _candidate()
    mappings = {}
    for verdict in ("same", "inverse", "narrower", "distinct"):
        vote = PredicateJudgeVote(
            predicate_a=candidate.predicate_a,
            predicate_b=candidate.predicate_b,
            parsed=ParsedPredicateVerdict(
                verdict=verdict,  # type: ignore[arg-type]
                canonical="wrote_to",
                confidence=0.95,
                reason="test",
            ),
            duration_s=0.1,
        )
        result = _gate_result(
            candidate,
            vote,
            vote,
            auto_fold_threshold=0.85,
            judge_model="test/model",
        )
        mappings[verdict] = (result.mapping, result.status)
    assert mappings == {
        "same": ("exact_match", "auto"),
        "inverse": ("inverse_of", "queued"),
        "narrower": ("sub_property_of", "queued"),
        "distinct": ("distinct", "rejected"),
    }
