"""E6 multi-sample union (``samples`` = k independent draws per block).

Pins three properties of :class:`okto_neuron.extract.LLMExtractor`:

1. ``samples=1`` is byte-identical to the pre-union extractor — exactly ONE
   provider call, the same ``[system, user]`` messages, the same candidates.
2. ``samples=2`` UNIONS two draws' candidate sets, deduped (nodes on
   ``candidate_id``; claims/edges on ``(type, src_ref, dst_ref|literal)``).
3. A union draw that truncates (``finish_reason="length"``) to ZERO candidates
   is re-drawn ONCE, then given up on (bounded by ``_UNION_TRUNCATION_RETRY_MAX``).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_neuron.extract import _UNION_TRUNCATION_RETRY_MAX, LLMExtractor, wrap_untrusted_block
from okto_neuron.llm import Message, _set_last_call_stats


@pytest.fixture(autouse=True)
def _reset_call_stats():
    yield
    _set_last_call_stats(None)


_DRAW_A = (
    '{"nodes":['
    '{"type":"Agent","title":"Evan","content":"CDTO at ExampleCorp"},'
    '{"type":"Concept","title":"ROI","content":"return on investment"}'
    '],"edges":[{"type":"discusses","src":"Evan","dst":"ROI"}]}'
)
# Shares ROI + the Evan->ROI edge with _DRAW_A (must dedupe), adds a fresh node
# Bob and a fresh edge Bob->ROI (must survive the union).
_DRAW_B = (
    '{"nodes":['
    '{"type":"Concept","title":"ROI","content":"return on investment"},'
    '{"type":"Agent","title":"Bob","content":"analyst"}'
    '],"edges":['
    '{"type":"discusses","src":"Evan","dst":"ROI"},'
    '{"type":"discusses","src":"Bob","dst":"ROI"}]}'
)
_EMPTY = '{"nodes":[],"edges":[]}'


class _SequencedLLM:
    """Fake provider: one reply per call, in order. Records messages + kwargs so
    tests can assert call count, message shape, and sampling temperature."""

    model = "fake"

    def __init__(
        self,
        replies: Sequence[str],
        finish_reasons: Sequence[str | None] | None = None,
    ) -> None:
        self._replies = list(replies)
        self._finish_reasons = list(finish_reasons) if finish_reasons else ["stop"] * len(replies)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kwargs,
    ) -> str:
        idx = len(self.calls)
        self.calls.append({"temperature": temperature, "messages": messages, **kwargs})
        _set_last_call_stats({"finish_reason": self._finish_reasons[idx]})
        return self._replies[idx]

    @property
    def call_count(self) -> int:
        return len(self.calls)


def test_samples_1_is_byte_identical_single_draw() -> None:
    """samples=1 (and the unset default) must issue exactly one call with the
    unmodified [system, user] messages and return the same candidates — the
    union path must not perturb the shipped single-pass behavior."""
    text = "Evan discussed ROI."

    default = LLMExtractor(_SequencedLLM([_DRAW_A]))  # no samples arg
    prov = _SequencedLLM([_DRAW_A])
    one = LLMExtractor(prov, samples=1)

    res_default = default.extract(text)
    res_one = one.extract(text)

    # Exactly one provider call, the original two-message shape. The user
    # message wraps the block text in the untrusted-data delimiters (finding
    # 3.16) rather than sending it verbatim — the wrapped text still carries
    # the original content byte-for-byte inside the delimiters.
    assert prov.call_count == 1
    msgs = prov.calls[0]["messages"]
    assert [m.role for m in msgs] == ["system", "user"]
    assert msgs[0].content == one._system_prompt  # noqa: SLF001
    assert msgs[1].content == wrap_untrusted_block(text)
    # Candidates identical to the sample-unaware extractor.
    assert [n.candidate_id for n in res_one.node_candidates] == [
        n.candidate_id for n in res_default.node_candidates
    ]
    assert {n.title for n in res_one.node_candidates} == {"Evan", "ROI"}
    assert len(res_one.edge_candidates) == 1


def test_samples_2_unions_and_dedupes_two_draws() -> None:
    prov = _SequencedLLM([_DRAW_A, _DRAW_B])
    extractor = LLMExtractor(prov, samples=2, temperature=0.7)

    result = extractor.extract("Evan and Bob discussed ROI.")

    assert prov.call_count == 2
    # Union of {Evan, ROI} and {ROI, Bob} → ROI deduped once.
    titles = [n.title for n in result.node_candidates]
    assert sorted(titles) == ["Bob", "Evan", "ROI"]
    assert titles.count("ROI") == 1
    # Edges: draw A has 1 (Evan->ROI); draw B has 2 (Evan->ROI dup + Bob->ROI).
    # Naive concat = 3; union dedupes the shared Evan->ROI → 2 distinct edges.
    # (src_ref/dst_ref are resolved to candidate_id hashes at parse time.)
    assert len(result.edge_candidates) == 2
    keys = {(e.type, e.src_ref, e.dst_ref) for e in result.edge_candidates}
    assert len(keys) == 2


def test_union_truncation_retry_fires_once_then_gives_up() -> None:
    """Every draw truncates (finish_reason=length) to 0 candidates. Each of the
    2 draws is re-drawn exactly once then given up on → 2 draws * (1 + 1) = 4
    calls. The bound is ``_UNION_TRUNCATION_RETRY_MAX`` (== 1)."""
    assert _UNION_TRUNCATION_RETRY_MAX == 1
    n_calls = 2 * (1 + _UNION_TRUNCATION_RETRY_MAX)
    prov = _SequencedLLM([_EMPTY] * n_calls, finish_reasons=["length"] * n_calls)
    extractor = LLMExtractor(prov, samples=2, temperature=0.7)

    result = extractor.extract("dense truncating block")

    assert prov.call_count == n_calls  # 4: no unbounded re-draw
    assert result.node_candidates == []
    assert result.edge_candidates == []
    assert result.truncated is True  # the anomaly still propagates


def test_config_key_is_llm_extraction_samples() -> None:
    """Pin the real YAML key: ``llm.extraction.samples`` (NOT ``llm.extract.*``).
    The block is ``extra="forbid"``, so a wrong key would raise — this locks the
    exact string the docs + operators must use to arm the union mode."""
    from okto_neuron.config._vault import LLMConfig

    cfg = LLMConfig.model_validate({"extraction": {"samples": 2}})
    assert cfg.extraction.samples == 2
    # Unset → None (companion falls back to samples=1, byte-identical).
    assert LLMConfig().extraction.samples is None


def test_union_truncation_retry_recovers_then_unions() -> None:
    """Draw #1 truncates to 0, its single re-draw recovers (_DRAW_A); draw #2 is
    clean (_DRAW_B). The union is the recovered draw ∪ draw #2."""
    prov = _SequencedLLM(
        [_EMPTY, _DRAW_A, _DRAW_B],
        finish_reasons=["length", "stop", "stop"],
    )
    extractor = LLMExtractor(prov, samples=2, temperature=0.7)

    result = extractor.extract("Evan and Bob discussed ROI.")

    assert prov.call_count == 3  # draw1: 1 trunc + 1 retry; draw2: 1 clean
    assert sorted(n.title for n in result.node_candidates) == ["Bob", "Evan", "ROI"]
    assert result.truncated is False
