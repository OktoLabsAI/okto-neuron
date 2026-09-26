"""ADR 0039 D5 transient-provider retry for the non-extraction ingest steps.

Extraction units and ask synthesis retried a retryable provider error once;
the other LLM steps inside ``remember()`` did not. A single ChatGPT 503
('Unable to verify ... access. Please try again.') on a merge-judge, curator,
relation-curator, predicate-resolution or type-adjudication call silently
degraded that decision (``llm-unavailable`` / abstain / distinct). Every one
of them now goes through ``okto_neuron.llm.complete_with_retry``: one retry for
a retryable error, none for anything else, and the retry is recorded next to
the step's outcome (``provider_retries``) and in the document outcome.

Model-free: scripted providers raising the exact ``LLMProviderError`` shape
the LLM layer builds for that 503.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.ledger import LEDGER_FILENAME
from okto_neuron.core.schema import Provenance
from okto_neuron.curator import LLMCandidateCurator, LLMRelationCurator
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import LLMProviderError
from okto_neuron.predicates import PredicateResolutionRequest
from okto_neuron.predicates.resolve import LLMPredicateResolver
from okto_neuron.reconcile.type_adjudication import LLMTypeAdjudicator, TypeAdjudicationCase
from okto_neuron.resolve import LLMMergeJudge
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


def _transient_error() -> LLMProviderError:
    """The error the LLM layer raised for the ChatGPT backend's 503, classified
    by the provider layer's own classifier (not a hand-set flag)."""
    import litellm

    from okto_neuron.llm import classify_provider_exception

    cause = litellm.ServiceUnavailableError(
        message='ChatgptException - {"detail":"Unable to verify access. Please try again."}',
        llm_provider="chatgpt",
        model="gpt-5.6-luna",
    )
    classification = classify_provider_exception(cause)
    assert classification.category == "unavailable" and classification.retryable
    return LLMProviderError(
        f"litellm completion failed for chatgpt/gpt-5.6-luna: {cause}",
        category=classification.category,
        retryable=classification.retryable,
        cause=cause,
    )


def _permanent_error() -> LLMProviderError:
    return LLMProviderError("invalid api key", category="authentication")


class _Scripted:
    """Raises ``error`` on the listed 1-based call numbers, else ``reply``."""

    model = "test/scripted"

    def __init__(self, reply: str, *, raise_at: set[int], error: LLMProviderError) -> None:
        self.reply = reply
        self.raise_at = raise_at
        self.error = error
        self.calls = 0

    def complete(self, messages: Any, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls in self.raise_at:
            raise self.error
        return self.reply


def _node(title: str) -> NodeCandidate:
    return NodeCandidate(type="Agent", title=title, content=f"{title} is a person.")


# Each step: (reply that parses, run(provider) -> step result, degraded(result) -> bool)
_STEPS: dict[str, tuple[str, Callable[[Any], Any], Callable[[Any], bool]]] = {
    "curator": (
        '{"action":"commit","confidence":0.9,"reason":"grounded"}',
        lambda p: LLMCandidateCurator(p).curate(
            None,  # type: ignore[arg-type] - unused with a prebuilt prompt
            None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            edges=[],
            user_prompt="review this candidate",
        ),
        lambda v: v.action == "abstain" and v.reason == "llm-unavailable",
    ),
    "relation_curator": (
        '{"action":"queue","confidence":0.4,"reason":"weak"}',
        lambda p: LLMRelationCurator(p).curate(
            None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            node_candidates={},
            user_prompt="review this relation",
        ),
        lambda v: v.action == "abstain" and v.reason == "llm-unavailable",
    ),
    "judge": (
        '{"same":true,"confidence":0.95,"reason":"same person"}',
        lambda p: LLMMergeJudge(p).judge(_node("Casey"), _node("Casey Buck").to_node()),
        lambda v: v.reason == "llm-unavailable",
    ),
    "predicate_resolution": (
        '{"verdict":"distinct","target":"","canonical":"","confidence":0.2,"reason":"new"}',
        lambda p: LLMPredicateResolver(p).resolve(
            PredicateResolutionRequest(
                proposed_label="estado_atual",
                proposed_definition="The subject carries the stated status.",
                proposed_direction="subject_to_object",
                source_excerpt="Widget service is currently active.",
                incumbents=(),
            )
        ),
        lambda r: r.reason == "llm-unavailable",
    ),
    "type_adjudication": (
        json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": "one",
                        "primitive_type": "Agent",
                        "confidence": 0.99,
                        "reason": "acts",
                    }
                ]
            }
        ),
        lambda p: LLMTypeAdjudicator(p).adjudicate(
            "bilbo",
            (
                TypeAdjudicationCase(
                    candidate_id="one",
                    reported_type="Concept",
                    title="Bilbo",
                    content="A hobbit.",
                    source_excerpt="Bilbo pocketed the stone.",
                ),
            ),
        ),
        lambda r: r.error.startswith("llm-unavailable:"),
    ),
}


@pytest.mark.parametrize("step", sorted(_STEPS))
def test_one_transient_error_is_retried_and_recovers(step: str) -> None:
    reply, run, degraded = _STEPS[step]
    provider = _Scripted(reply, raise_at={1}, error=_transient_error())
    result = run(provider)
    assert provider.calls == 2
    assert not degraded(result)
    (retry,) = result.provider_retries
    assert retry["attempt"] == 1
    assert retry["category"] == "unavailable"
    assert retry["delay_s"] == 0.0
    assert "Please try again" in retry["error"]


@pytest.mark.parametrize("step", sorted(_STEPS))
def test_transient_error_on_both_attempts_degrades_as_before(step: str) -> None:
    reply, run, degraded = _STEPS[step]
    provider = _Scripted(reply, raise_at={1, 2, 3}, error=_transient_error())
    result = run(provider)
    assert provider.calls == 2  # bounded: never a third attempt
    assert degraded(result)
    assert len(result.provider_retries) == 1


@pytest.mark.parametrize("step", sorted(_STEPS))
def test_non_retryable_error_is_not_retried(step: str) -> None:
    reply, run, degraded = _STEPS[step]
    provider = _Scripted(reply, raise_at={1, 2}, error=_permanent_error())
    result = run(provider)
    assert provider.calls == 1
    assert degraded(result)
    assert result.provider_retries == ()


def test_retry_wait_is_cut_short_by_the_callers_cancel_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Retry-After wait (up to 60 s) must not hold a Stop hostage: the
    ingest tracing wrapper's ``should_cancel`` ends the wait, and its next
    ``complete`` raises the cancellation itself."""
    import okto_neuron.llm as llm_module
    from okto_neuron.llm import complete_with_retry

    slept: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", slept.append)
    cancelled = {"now": False}

    class _Cancelling(_Scripted):
        def __init__(self) -> None:
            error = _transient_error()
            error.retry_after_s = 30.0
            super().__init__("ok", raise_at={1}, error=error)
            self.noted: list[tuple[str, dict[str, object]]] = []

        def should_cancel(self) -> bool:
            return cancelled["now"]

        def note_retry(self, step: str, record: dict[str, object]) -> None:
            self.noted.append((step, dict(record)))
            cancelled["now"] = True

    provider = _Cancelling()
    assert complete_with_retry(provider, [], step="curator") == "ok"
    assert slept == []  # cancelled before the first poll slice
    assert provider.noted == [
        (
            "curator",
            {
                "attempt": 1,
                "category": "unavailable",
                "retry_after_s": 30.0,
                "delay_s": 30.0,
                "error": provider.noted[0][1]["error"],
            },
        )
    ]


# ── through a real remember(): ledger row + document outcome ────────────────


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _OneNodeExtractor:
    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        return ExtractionResult(
            node_candidates=[
                NodeCandidate(
                    type="Concept",
                    title="Retry Policy",
                    content="Retry Policy bounds provider retries to two attempts.",
                    provenance=provenance,
                )
            ]
        )


class _CuratorFlaky:
    """Every curator call fails its first attempt with the transient 503."""

    model = "test/curator-flaky"

    def __init__(self, failures_per_call: int) -> None:
        self.failures_per_call = failures_per_call
        self.curator_attempts = 0

    def complete(self, messages: Any, *, response_format: Any = None, **kwargs: Any) -> str:
        name = ((response_format or {}).get("json_schema") or {}).get("name")
        if name != "marginalia_candidate_curator":
            return "{}"
        self.curator_attempts += 1
        if self.curator_attempts <= self.failures_per_call:
            raise _transient_error()
        return '{"action":"commit","confidence":0.99,"reason":"grounded"}'


def _curator_rows(vault: Vault) -> list[dict[str, Any]]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in records if r["kind"] == "comparison" and r["method"] == "curator"]


def test_remember_records_a_recovered_curator_retry(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nRetry Policy bounds retries.\n", encoding="utf-8")
        provider = _CuratorFlaky(failures_per_call=1)
        result = Companion(vault, provider=provider, extractor=_OneNodeExtractor()).remember(doc)
        assert provider.curator_attempts == 2
        (row,) = _curator_rows(vault)
        assert row["verdict"] == "commit"
        (retry,) = row["payload"]["provider_retries"]
        assert retry["category"] == "unavailable"
        assert result.outcome["provider_retries"] == {"curator": 1}
    finally:
        vault.close()


def test_remember_exhausted_curator_retry_degrades_to_abstain(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nRetry Policy bounds retries.\n", encoding="utf-8")
        provider = _CuratorFlaky(failures_per_call=99)
        result = Companion(vault, provider=provider, extractor=_OneNodeExtractor()).remember(doc)
        assert provider.curator_attempts == 2
        (row,) = _curator_rows(vault)
        assert row["verdict"] == "abstain"
        assert row["reason"] == "llm-unavailable"
        assert len(row["payload"]["provider_retries"]) == 1
        assert result.outcome["provider_retries"] == {"curator": 1}
    finally:
        vault.close()


# ── the predicate-propose sweep judge and the reconcile cluster judge ───────


def _predicate_candidate() -> Any:
    from okto_neuron.predicates.candidates import PredicateCandidate, SharedArgumentEvidence

    return PredicateCandidate(
        predicate_a="works_at",
        predicate_b="employed_by",
        count_a=3,
        count_b=2,
        string_affinity=0.2,
        shared_evidence=SharedArgumentEvidence(),
    )


_PREDICATE_VERDICT = json.dumps(
    {
        "verdict": "distinct",
        "canonical": "",
        "confidence": 0.9,
        "reason": "different relations",
    }
)


def test_predicate_sweep_judge_retries_a_transient_error_and_records_it() -> None:
    from okto_neuron.predicates.judge import LLMPredicateJudge

    provider = _Scripted(_PREDICATE_VERDICT, raise_at={1}, error=_transient_error())
    result = LLMPredicateJudge(provider).judge(_predicate_candidate())
    assert provider.calls == 3  # forward (failed + retried) + reverse
    forward, reverse = result.votes
    assert forward.parsed is not None and forward.reason == ""
    (retry,) = forward.provider_retries
    assert retry["category"] == "unavailable"
    assert reverse.provider_retries == ()
    votes = result.to_record().votes
    assert votes["forward"]["provider_retries"][0]["attempt"] == 1
    assert "provider_retries" not in votes["reverse"]  # unretried vote unchanged


def test_predicate_sweep_judge_exhausted_retry_degrades_as_before() -> None:
    from okto_neuron.predicates.judge import LLMPredicateJudge

    provider = _Scripted(_PREDICATE_VERDICT, raise_at={1, 2}, error=_transient_error())
    result = LLMPredicateJudge(provider).judge(_predicate_candidate())
    forward, _reverse = result.votes
    assert provider.calls == 3  # never a third attempt on the forward vote
    assert forward.parsed is None and forward.reason == "llm-unavailable"
    assert len(forward.provider_retries) == 1
    assert result.outcome == "queue_unparseable"


def test_cluster_judge_is_not_retried_because_it_falls_back_to_pairwise() -> None:
    """The cluster call's failure is not a degraded decision: it falls back to
    the pairwise judge, whose calls retry. A retry here would stack on that."""
    from okto_neuron.reconcile.propose import _try_cluster_judge

    provider = _Scripted('{"same": []}', raise_at={1}, error=_transient_error())
    canonical = _node("Casey Buck").to_node()
    assert (
        _try_cluster_judge(canonical, [_node("Casey").to_node()], LLMMergeJudge(provider)) is None
    )
    assert provider.calls == 1


# ── the incremental-ingest correction judge ─────────────────────────────────


class _NotingScripted(_Scripted):
    """A scripted provider that also implements the tracing wrapper's
    ``note_retry`` hook, which is the only place the correction judge's retry
    is recorded (it returns a bare index, no verdict object)."""

    def __init__(self, reply: str, *, raise_at: set[int], error: LLMProviderError) -> None:
        super().__init__(reply, raise_at=raise_at, error=error)
        self.noted: list[tuple[str, dict[str, object]]] = []

    def note_retry(self, step: str, record: dict[str, object]) -> None:
        self.noted.append((step, dict(record)))


def test_correction_judge_retries_a_transient_error_once() -> None:
    from okto_neuron.companion._incremental import make_correction_judge

    provider = _NotingScripted('{"index": 1}', raise_at={1}, error=_transient_error())
    judge = make_correction_judge(provider, resolved=None)
    assert judge("Atlas ships in May", ["Atlas is blue", "Atlas ships in April"]) == 1
    assert provider.calls == 2
    ((step, record),) = provider.noted
    assert step == "correction_judge"
    assert record["attempt"] == 1 and record["category"] == "unavailable"


def test_correction_judge_exhausted_retry_degrades_to_no_correction() -> None:
    from okto_neuron.companion._incremental import make_correction_judge

    provider = _NotingScripted('{"index": 1}', raise_at={1, 2, 3}, error=_transient_error())
    judge = make_correction_judge(provider, resolved=None)
    assert judge("Atlas ships in May", ["Atlas is blue", "Atlas ships in April"]) == -1
    assert provider.calls == 2
    assert len(provider.noted) == 1


def test_correction_judge_does_not_retry_a_permanent_error() -> None:
    from okto_neuron.companion._incremental import make_correction_judge

    provider = _NotingScripted('{"index": 1}', raise_at={1, 2}, error=_permanent_error())
    judge = make_correction_judge(provider, resolved=None)
    assert judge("Atlas ships in May", ["Atlas ships in April"]) == -1
    assert provider.calls == 1
    assert provider.noted == []


# ── a scoped task deadline bounds the step, not each attempt ────────────────


class _TimesOutAtTheDeadline:
    """Behaves like the LiteLLM path under ``_scoped_call_timeout``: the call
    runs until the scoped deadline, then fails as a retryable ``timeout``."""

    model = "test/deadline"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Any, **kwargs: Any) -> str:
        import time

        from okto_neuron.llm import _current_call_timeout_s

        self.calls += 1
        remaining = _current_call_timeout_s()
        if remaining is not None and remaining <= 0.0:
            raise LLMProviderError(
                "LLM task deadline expired before provider execution",
                category="timeout",
                retryable=True,
            )
        time.sleep((remaining or 0.0) + 0.01)
        raise LLMProviderError("request timed out", category="timeout", retryable=True)


def test_no_retry_once_the_scoped_deadline_has_passed() -> None:
    """A curator call that timed out at ``curation_call_timeout_s`` must not be
    retried inside the same expired scope (that only produced an immediate
    'deadline expired' error span)."""
    from okto_neuron.llm import _scoped_call_timeout, complete_with_retry

    provider = _TimesOutAtTheDeadline()
    retries: list[dict[str, object]] = []
    with _scoped_call_timeout(0.05), pytest.raises(LLMProviderError, match="timed out"):
        complete_with_retry(provider, [], step="curator", retries=retries)
    assert provider.calls == 1
    assert retries == []


def test_no_retry_when_retry_after_outlasts_the_scoped_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.llm as llm_module
    from okto_neuron.llm import _scoped_call_timeout, complete_with_retry

    slept: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", slept.append)
    error = _transient_error()
    error.retry_after_s = 30.0
    provider = _Scripted("ok", raise_at={1}, error=error)
    with _scoped_call_timeout(5.0), pytest.raises(LLMProviderError):
        complete_with_retry(provider, [], step="curator")
    assert provider.calls == 1
    assert slept == []


def test_retry_still_happens_inside_a_scoped_deadline_with_time_left() -> None:
    from okto_neuron.llm import _scoped_call_timeout, complete_with_retry

    provider = _Scripted("ok", raise_at={1}, error=_transient_error())
    retries: list[dict[str, object]] = []
    with _scoped_call_timeout(30.0):
        assert complete_with_retry(provider, [], step="curator", retries=retries) == "ok"
    assert provider.calls == 2
    assert len(retries) == 1


# ── remember(): both dedup on_pair callers, the judge retry, the ingest span ─


class _AliasExtractor:
    """First document: "Casey Buck" alone (no judge call). Second: "Dana Ray",
    "Dana" and "Casey", so the within-batch judge compares "Dana" to "Dana Ray"
    (``judge_batch``) and the store judge compares "Casey" to a stored "Casey
    Buck" (``judge_store``)."""

    def __init__(self) -> None:
        self.documents = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.documents += 1
        titles = ["Casey Buck"] if "first" in text else ["Dana Ray", "Dana", "Casey"]
        return ExtractionResult(
            node_candidates=[
                NodeCandidate(
                    type="Agent",
                    title=title,
                    content=f"{title} is a person.",
                    provenance=provenance,
                )
                for title in titles
            ]
        )


class _JudgeFlakyOnce:
    """Commits every curator candidate; the first merge-judge call of the run
    fails with the transient 503, every judge verdict is 'distinct'."""

    model = "test/judge-flaky"

    def __init__(self) -> None:
        self.judge_attempts = 0

    def complete(self, messages: Any, *, response_format: Any = None, **kwargs: Any) -> str:
        name = ((response_format or {}).get("json_schema") or {}).get("name")
        if name == "marginalia_candidate_curator":
            return '{"action":"commit","confidence":0.99,"reason":"grounded"}'
        if name == "marginalia_merge_verdict":
            self.judge_attempts += 1
            if self.judge_attempts == 1:
                raise _transient_error()
            return '{"same":false,"confidence":0.9,"reason":"different people"}'
        return "{}"


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_outputs(self, outputs: Any) -> None:
        pass


def test_remember_records_per_pair_judge_rows_from_both_dedup_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises every in-repo ``on_pair`` caller (companion's ``judge_batch``
    and ``judge_store`` closures) through a real remember(). ``on_pair``
    failures are logged, not raised, so a signature mismatch shows up here as
    missing per-pair rows."""
    import contextlib

    import okto_neuron.llm as llm_module

    spans: list[_Span] = []

    @contextlib.contextmanager
    def _fake_trace_parent(name: str, **_kwargs: Any) -> Iterator[_Span]:
        span = _Span()
        spans.append(span)
        yield span

    monkeypatch.setattr(llm_module, "trace_parent", _fake_trace_parent)

    vault = Vault.init(tmp_path / "v")
    try:
        extractor = _AliasExtractor()
        provider = _JudgeFlakyOnce()
        companion = Companion(vault, provider=provider, extractor=extractor)
        first = Path(vault.path) / "first.md"
        first.write_text("# First\n\nfirst mention.\n", encoding="utf-8")
        second = Path(vault.path) / "second.md"
        second.write_text("# Second\n\nsecond mention.\n", encoding="utf-8")

        first_result = companion.remember(first)
        assert provider.judge_attempts == 0
        assert "provider_retries" not in first_result.outcome
        assert "marginalia.provider_retries" not in spans[0].attributes  # absent = none
        # The liveness gate queues a relationship-less node, so seed the
        # committed look-alike the store judge compares against directly.
        vault.store.add_node(_node("Casey Buck").to_node())

        result = companion.remember(second)
        path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        per_pair = {
            r["method"]: r
            for r in records
            if r["kind"] == "comparison"
            and r["method"] in {"judge_batch", "judge_store"}
            and r["candidate_id"] not in {"batch", "store"}
        }
        assert set(per_pair) == {"judge_batch", "judge_store"}
        batch_row = per_pair["judge_batch"]
        assert batch_row["verdict"] == "distinct"
        assert batch_row["score"] == 0.9
        # The first judge call was the retried one: its row says so and keeps
        # the recovered verdict's reason; the unretried row is unchanged.
        (retry,) = batch_row["payload"]["provider_retries"]
        assert retry["category"] == "unavailable"
        assert batch_row["reason"] == "different people"
        store_row = per_pair["judge_store"]
        assert store_row["verdict"] == "distinct"
        assert not (store_row.get("payload") or {}).get("provider_retries")
        assert result.outcome["provider_retries"] == {"judge": 1}
        assert spans[-1].attributes["marginalia.provider_retries"] == {"judge": 1}
    finally:
        vault.close()
