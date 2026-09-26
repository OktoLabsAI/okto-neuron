"""Regression test: curator_batch's ``_fallback`` path must honor ``timeout_s``.

``iter_batched_curation`` scopes ``curation_call_timeout_s`` around the batch
call (``_run_batch``, ≈line 368) via ``_scoped_call_timeout``. Singleton
batches (and, when no ``fallback_runner`` is supplied, the whole
``max_concurrent`` fan-out) instead go through ``_fallback`` ->
``_bounded_single`` -> ``item.single_call()``. Before this fix,
``_bounded_single`` only entered ``call_capacity`` (the concurrency
semaphore) and never ``_scoped_call_timeout``, so a caller of
``iter_batched_curation``/``run_batched_curation`` that does not pass its own
``fallback_runner`` got no deadline propagation on this path at all —
``_current_call_timeout_s()`` read back ``None`` inside ``single_call``
regardless of the configured ``timeout_s``.

Note on the production call sites (``companion/__init__.py``, node and
relation curation): both always pass a ``fallback_runner`` built on
``_fan_out_verdicts``/``_iter_fan_out_verdicts``, whose own ``_run_one``
already opens ``_scoped_call_timeout(remaining)`` immediately before invoking
the callable it was given — the same thread, so ``_bounded_single`` inherited
a correct deadline there even pre-fix. This test exercises the two branches
of ``_fallback`` that run with ``fallback_runner=None`` (the sequential
list-comprehension and the ``max_concurrent > 1`` ``ThreadPoolExecutor``
branch), which is where ``_bounded_single`` is the *only* thing that can
install the deadline, and which any caller — direct library/test use, or a
future production call site — can reach without a ``fallback_runner``.
"""

from __future__ import annotations

from okto_neuron.curator import CuratorVerdict
from okto_neuron.curator_batch import BatchCurationItem, run_batched_curation
from okto_neuron.llm import LLMProviderError, _current_call_timeout_s


def _item(
    candidate_id: str, observed: list[float | None], *, block_id: str | None = None
) -> BatchCurationItem:
    def single_call() -> CuratorVerdict:
        observed.append(_current_call_timeout_s())
        return CuratorVerdict(action="commit", confidence=0.9, reason="ok")

    return BatchCurationItem(
        candidate_id=candidate_id,
        block_id=block_id,
        excerpt="some excerpt",
        prompt="some prompt",
        trace_context={},
        single_call=single_call,
    )


class _AlwaysFailingProvider:
    """Fake provider whose batch call always raises, forcing every member to fall back."""

    def complete(self, *_args: object, **_kwargs: object) -> str:
        raise LLMProviderError("simulated provider failure", category="unknown")


def _assert_all_scoped(observed: list[float | None], *, expected_count: int) -> None:
    assert len(observed) == expected_count
    for remaining in observed:
        assert remaining is not None, (
            "single_call ran with no active call deadline — "
            "curation_call_timeout_s did not reach the _fallback/_bounded_single path"
        )
        assert 0.0 < remaining <= 30.0, (
            f"expected deadline derived from timeout_s=30.0, got {remaining!r}"
        )


def test_fallback_single_call_sees_scoped_timeout_sequential() -> None:
    """max_concurrent=1: _fallback's sequential ``[call() for call in calls]`` branch."""

    observed: list[float | None] = []
    item = _item("c1", observed)

    verdicts, _meta = run_batched_curation(
        [item],
        provider=None,  # never called: a singleton batch skips the batch LLM call
        system_prompt="sys",
        relation=False,
        batch_size=4,
        max_concurrent=1,
        timeout_s=30.0,
        temperature=0.0,
        max_tokens=None,
    )

    assert len(verdicts) == 1
    _assert_all_scoped(observed, expected_count=1)


def test_fallback_single_call_sees_scoped_timeout_threadpool() -> None:
    """max_concurrent>1, no fallback_runner: _fallback's own ThreadPoolExecutor branch.

    ``_call_deadline`` (the deadline ``_scoped_call_timeout`` installs) is a
    ``threading.local`` — a deadline set on the submitting thread is invisible
    to a pool worker thread unless something inside that worker's call chain
    installs it too. This pins that ``_bounded_single`` does so on every
    worker, not just incidentally on a single-threaded run.

    Two items share one ``block_id`` so they form a single multi-member batch;
    the fake provider always raises, so ``_run_batch`` returns ``{}`` and
    ``_complete`` calls ``_fallback`` ONCE with both indices — that is what
    drives ``_fallback``'s own ``elif max_concurrent > 1 and len(calls) > 1``
    branch (its ``ThreadPoolExecutor``, distinct from the driver's).
    """

    observed: list[float | None] = []
    items = [
        _item("c1", observed, block_id="block-1"),
        _item("c2", observed, block_id="block-1"),
    ]

    verdicts, _meta = run_batched_curation(
        items,
        provider=_AlwaysFailingProvider(),
        system_prompt="sys",
        relation=False,
        batch_size=4,
        max_concurrent=2,
        timeout_s=30.0,
        temperature=0.0,
        max_tokens=None,
    )

    assert len(verdicts) == 2
    _assert_all_scoped(observed, expected_count=2)
