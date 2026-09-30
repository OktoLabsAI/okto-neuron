"""Autonomous memory companion — the public contract.

This module owns the surface the autonomous companion exposes:
``remember`` / ``recall`` / ``ask`` / ``review_queue`` / ``resolve_review``.
The contract (parameter and return shapes, error modes) remains stable while the
implementation runs the complete production pipeline: source anchoring, LLM
extraction, durable candidate ledgering, deduplication and resolution, curator
gates, atomic commit or review, and grounded retrieval/answer assembly.

See ``docs/autonomous-architecture-plan.md`` for the phased plan.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import nullcontext
from dataclasses import dataclass
from functools import wraps
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Literal, Mapping, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from okto_neuron._internal.infra import LOW_SALIENCE_FACET, is_infra
from okto_neuron.config._capacity import (
    EXTRACTION_STEPS,
    capacity_notice,
    curation_batch_notice,
    curation_effective_max_concurrent,
    effective_curation_batch_size,
    extraction_effective_max_concurrent,
    step_model,
)
from okto_neuron.consolidate._claim_identity import claim_object_identity, semantic_claim_id
from okto_neuron.errors import ConfigParseError, IngestError, OktoNeuronError

# ADR 0039 D5 transient-provider retry policy. It lives in ``okto_neuron.llm``
# so every LLM step shares one definition: extraction units (per-attempt unit
# journal below), ask synthesis, and the ingest judge / curator /
# relation-curator / type-adjudication calls (``complete_with_retry``).
from okto_neuron.llm import (
    PROVIDER_MAX_ATTEMPTS as _PROVIDER_MAX_ATTEMPTS,
)
from okto_neuron.llm import (
    provider_error_summary as _provider_error_summary,
)
from okto_neuron.llm import (
    provider_retry_delay as _provider_retry_delay,
)
from okto_neuron.models import QueryHit
from okto_neuron.semantic_surface import discovery_surface_key, exact_surface_key
from okto_neuron.vault import Vault

_LOG = logging.getLogger("okto_neuron.companion")

_EXTRACTION_CONFIG_POLL_SECONDS = 0.25
_EXTRACTION_MAX_CONCURRENT = 32

if TYPE_CHECKING:
    from okto_neuron.config import IngestConfig, VaultConfig
    from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import RelationReviewItem, ReviewQueue
    from okto_neuron.core.schema import Node
    from okto_neuron.curator import CuratorVerdict
    from okto_neuron.embed import EmbeddingProvider
    from okto_neuron.extract import ExtractionResult, Extractor
    from okto_neuron.llm import LLMProvider, ResponseFormat
    from okto_neuron.reconcile.decisions import IdentityDecisionIndex
    from okto_neuron.schema.support import SourceSpan
    from okto_neuron.semantic_fingerprint import SemanticFingerprints
    from okto_neuron.store.protocol import GraphStore

# ── vocabulary ────────────────────────────────────────────────────────────────
Sensitivity = Literal["local_only", "default"]
"""``local_only`` pins extraction to a local model; sensitive vaults never
escalate to a hosted provider."""

ProgressCallback = Callable[[str, int, int], None]
"""Within-file ingest progress sink: ``(stage, blocks_done, blocks_total)``.
Stages advance monotonically through ``parsing → extracting → embedding →
dedup → committing``. Optional; ``remember`` works unchanged without one."""
IngestEventCallback = Callable[[dict[str, Any]], None]
"""Optional rich ingest event sink used by the web UI inspector."""

CorrelationKind = Literal["similar", "contradicts", "answers"]
OutcomeAction = Literal["committed", "queued"]
ReviewAction = Literal["commit", "discard", "merge"]
ReviewReason = Literal["low_confidence", "contradiction"]
SourceBlockPolicy = Literal["never", "on_coverage_miss", "always", "blend"]
RELATION_PROGRESS_EVENT_EVERY = 100
NODE_PROGRESS_EVENT_EVERY = 25
DEDUP_PROGRESS_EVENT_EVERY = 25
# Keep-alive cadence for the dedup/curation sub-stages. Those phases run one
# LLM call per candidate/pair (curation_max_concurrent and curation_batch_size
# both default to 1) and used to emit exactly ONE on_progress call each, so an
# MCP client's 300s idle timer fired mid-phase and discarded the payload of a
# call the daemon was still happily working on. Ticking on item count ALONE
# cannot bound the silence (N items = N unbounded serial calls), so the gate is
# "every N items OR X seconds since the last tick, whichever comes first": the
# count floor keeps a fast phase from flooding the client, the clock keeps a
# slow phase from going dark.
SUBSTAGE_PROGRESS_EVERY = 5
SUBSTAGE_PROGRESS_INTERVAL_S = 20.0


# ── value models ────────────────────────────────────────────────────────────--
class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Correlation(_Frozen):
    """One thing the graph said back about a candidate at resolve time."""

    kind: CorrelationKind
    target_id: str
    score: float = Field(ge=0.0, le=1.0)
    summary: str = ""


class CandidateOutcome(_Frozen):
    """The fate of one proposed node/edge after the gate."""

    candidate_id: str
    type: str
    title: str
    action: OutcomeAction
    confidence: float = Field(ge=0.0, le=1.0)
    correlations: tuple[Correlation, ...] = ()


class RememberResult(_Frozen):
    """Outcome of one ``remember`` call."""

    document_id: str
    committed: int = Field(default=0, ge=0)
    queued: int = Field(default=0, ge=0)
    dead_lettered: int = Field(default=0, ge=0)
    blocks_total: int = Field(default=0, ge=0)
    nodes_extracted: int = Field(default=0, ge=0)
    edges_extracted: int = Field(default=0, ge=0)
    claims_minted: int = Field(default=0, ge=0)
    provider_error: str | None = None
    # Additive (issue #4 fix 2 — visible zero-parse): how many attempted
    # blocks hit an ``LLMProviderError`` during extraction, and how many had
    # non-empty text but still yielded zero candidates after the extractor's
    # own empty-result retry. Both default to 0 so pre-existing callers that
    # construct a ``RememberResult`` without them are unaffected. Surfaced on
    # the MCP ``remember`` tool and the REST ``/remember`` payload next to
    # ``provider_error`` so a silent-zero-yield ingest is visible, not just
    # logged.
    provider_failures: int = Field(default=0, ge=0)
    empty_after_retry_blocks: int = Field(default=0, ge=0)
    outcomes: tuple[CandidateOutcome, ...] = ()
    # Defect A fix — llm.enabled=false is a deliberate, healthy configuration,
    # NOT a provider failure. Previously this path rode ``provider_error``
    # (a truthy string), which the ingest queue's F4 zero-yield rule
    # (server/_ingest_queue.py) reads as "every ingest on this vault failed",
    # marking the item "error" and retry-looping forever. This flag lets
    # callers distinguish "LLM deliberately off" from "LLM broke" while
    # ``provider_error`` stays ``None`` for the disabled case.
    llm_disabled: bool = False
    # ADR 0039 D6: worker lifecycle (done/error/cancelled) is not technical
    # completeness. This additive object carries unit, plan, and integrity
    # evidence while the legacy counters remain stable for older clients.
    outcome: dict[str, Any] = Field(default_factory=dict)
    # Internal correlation only. The public REST/MCP contract stays unchanged;
    # the write guard uses this id to append the definitive post-write audit to
    # the already-finished ledger run.
    ledger_run_id: str | None = Field(default=None, exclude=True, repr=False)


class Answer(_Frozen):
    """Answer to an ``ask`` call, grounded in retrieved nodes."""

    text: str
    # ALWAYS the retrieval-seed node ids computed before subgraph expansion —
    # exactly ``hits``, never the expanded ego-graph. When ``enable_subgraph``
    # is on (default off; ``llm.ask.enable_subgraph`` / per-call
    # ``AskRetrievalPolicy``), ``text`` is synthesised over a 1-hop+ ego-graph
    # render (see ``_subgraph_context``) and is instructed to cite
    # ``claim:<id>`` anchors from that wider render, not from ``hits``. There
    # is no resolver from those free-text anchors back to node/claim ids, so
    # ``citations`` alone under-represents what actually grounds the text in
    # that mode — read ``subgraph_evidence_ids`` alongside it when present.
    citations: tuple[str, ...] = ()
    hits: tuple[QueryHit, ...] = ()
    # Populated only when the answer came from the subgraph path
    # (``retrieval["mode"] == "subgraph"``): every node id and claim id
    # present in the ego-graph rendered into the prompt (the full pool the
    # model could cite from — entity NODES rows are never budget-trimmed;
    # RELATIONSHIPS/CLAIMS rows can be, so this may be a superset of what
    # survived truncation into the actual prompt text). Empty for the default
    # block-dump path, where ``citations`` already is the grounding set.
    subgraph_evidence_ids: tuple[str, ...] = ()
    retrieval: dict[str, Any] = Field(default_factory=dict)


class AskRetrievalPolicy(BaseModel):
    """Per-request ask retrieval controls.

    This is deliberately separate from ``llm.ask`` YAML config: the UI can sweep
    retrieval policy without rewriting the vault's persistent defaults. ``None``
    means "inherit the vault/config/code default for this call."
    """

    model_config = ConfigDict(extra="forbid")

    enable_subgraph: bool | None = None
    seed_k: int | None = Field(default=None, ge=1)
    hops: int | None = Field(default=None, ge=1, le=5)
    max_degree_per_seed: int | None = Field(default=None, ge=1)
    neighbour_budget_tokens: int | None = Field(default=None, ge=1)
    coverage_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    min_claim_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    max_nodes: int | None = Field(default=None, ge=1)
    max_relationships: int | None = Field(default=None, ge=0)
    max_claims: int | None = Field(default=None, ge=0)
    relationship_types: tuple[str, ...] | None = None
    source_block_policy: SourceBlockPolicy | None = None
    source_block_budget_tokens: int | None = Field(default=None, ge=1)
    # ── Fix B (task-12): seed-quality diversification overrides ────────────────
    # ``seed_diversity`` None → inherit ``llm.ask.seed_diversity``, else follow
    # ``enable_subgraph`` (diversified seeds ride the subgraph path only).
    # ``False`` restores the pre-Fix-B seed ordering (A/B attribution switch).
    seed_diversity: bool | None = None
    seed_subject_cap: int | None = Field(default=None, ge=1)
    seed_entity_min: int | None = Field(default=None, ge=0)
    seed_rel_min: int | None = Field(default=None, ge=0)
    seed_scalar_max: int | None = Field(default=None, ge=0)

    @field_validator("relationship_types", mode="after")
    @classmethod
    def _v_relationship_types(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(item.strip() for item in value if item and item.strip())
        return cleaned or None


class _TracingLLMProvider:
    """LLMProvider wrapper that emits request/response events for ingest inspect.

    The ``llm_request`` event reports the EFFECTIVE request parameters — what
    the wrapped provider actually sends — not this wrapper's own method
    arguments. Those two used to be assumed identical and are not: a role
    configured with a raw ``sampling_payload`` has its per-call sampler
    arguments discarded inside the provider, so the trace reported values that
    were never sent (typically ``LLMExtractor``'s class defaults
    ``temperature=0.0`` / ``max_tokens=16000``) and an operator could not
    verify their own configuration from the inspector.

    A provider that advertises ``traces_effective_request`` reports its
    assembled request through ``okto_neuron.llm._set_request_observer`` at the
    moment it is built and before it is issued; this wrapper emits that. Any
    other provider (the CLI providers, ``StubLLM``, test doubles) is traced
    from the caller's arguments up front, exactly as before, and the event says
    so via ``params_source``.

    INVARIANT: exactly one ``llm_request`` event per ``complete()`` call, on
    every exit path — success, provider error, and cancellation alike.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        emit: Callable[[str, str, dict[str, Any] | None], None],
        context: Callable[[], dict[str, Any]],
        *,
        should_cancel: Callable[[], bool] | None = None,
        label: str = "Extraction",
        trace_events: bool = True,
        on_completion: Callable[[Mapping[str, Any] | None], None] | None = None,
        on_retry: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._provider = provider
        self._emit = emit
        self._context = context
        self._should_cancel = should_cancel
        self._label = label
        self._trace_events = trace_events
        self._on_completion = on_completion
        self._on_retry = on_retry
        self._reports_effective_request = bool(getattr(provider, "traces_effective_request", False))
        self.model = str(getattr(provider, "model", "unknown"))
        if hasattr(provider, "api_base"):
            self.api_base = getattr(provider, "api_base")

    # The two hooks ``okto_neuron.llm.complete_with_retry`` looks for: the
    # remember's cancel predicate cuts a Retry-After wait short, and every
    # retried failure reaches the remember's per-step retry tally and the
    # ingest inspector.
    @property
    def should_cancel(self) -> Callable[[], bool] | None:
        return self._should_cancel

    def note_retry(self, step: str, record: Mapping[str, Any]) -> None:
        if self._trace_events:
            self._emit(
                "llm_retry",
                f"{self._label} LLM retry",
                {"block": self._context(), "model": self.model, "step": step, **record},
            )
        if self._on_retry is not None:
            self._on_retry(step, record)

    def complete(
        self,
        messages: object,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: "ResponseFormat | None" = None,
    ) -> str:
        if self._should_cancel is not None and self._should_cancel():
            raise RememberCancelled()
        from okto_neuron.llm import (
            LLMCallCancelled,
            _set_call_cancel_predicate,
            _set_request_observer,
        )

        requested_params: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "top_k": top_k,
            "min_p": min_p,
            "presence_penalty": presence_penalty,
            "enable_thinking": enable_thinking,
            "response_format": response_format,
        }
        emitted_request = False

        def _emit_request(effective: Mapping[str, Any] | None) -> None:
            """Emit the single ``llm_request`` event for this call.

            ``effective`` is the provider's own report of the request it
            assembled; ``None`` means fall back to the caller's arguments,
            either because the provider does not report (CLI providers,
            ``StubLLM``) or because it failed before it finished building.
            """

            nonlocal emitted_request
            if emitted_request or not self._trace_events:
                emitted_request = True
                return
            emitted_request = True
            requested_view = dict(requested_params)
            if effective is not None and requested_view["response_format"] == effective[
                "params"
            ].get("response_format"):
                # ``llm_request`` is the chattiest event kind on a thousand-call
                # ingest and the extraction schema is the largest thing in it.
                # Repeating an identical schema in both views buys no signal:
                # requested-vs-effective divergence is carried by the sampler
                # keys, and ``response_format`` is Okto Neuron's own contract
                # with the parser, not an operator-tunable preference.
                requested_view["response_format"] = "<same as params.response_format>"
            payload: dict[str, Any] = {
                "block": self._context(),
                "model": self.model,
                "messages": [
                    {
                        "role": getattr(message, "role", ""),
                        "content": getattr(message, "content", ""),
                    }
                    for message in messages  # type: ignore[union-attr]
                ],
                "params": dict(effective["params"]) if effective else dict(requested_params),
                # "effective" == what the provider is sending. "requested" ==
                # this wrapper's own arguments, which the provider may still
                # reshape. Never let the reader guess which one they're seeing.
                "params_source": "effective" if effective else "requested",
                "requested_params": requested_view,
            }
            if effective is not None:
                payload["extra_body"] = dict(effective["extra_body"])
                payload["omitted_params"] = dict(effective["omitted"])
                # Shows the operator that their raw payload WON, rather than
                # leaving a merged blob that hides whose values these are.
                payload["sampling_payload_applied"] = bool(effective["sampling_payload_applied"])
            self._emit("llm_request", f"{self._label} LLM request", payload)

        if not self._reports_effective_request:
            # Unchanged behaviour for a provider that cannot report: emit up
            # front so a slow call still shows its prompt while in flight.
            _emit_request(None)

        observer = _emit_request if self._trace_events else None
        previous_observer = _set_request_observer(observer)
        previous_cancel = _set_call_cancel_predicate(self._should_cancel)
        try:
            try:
                response = self._provider.complete(
                    messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    top_k=top_k,
                    min_p=min_p,
                    presence_penalty=presence_penalty,
                    enable_thinking=enable_thinking,
                    response_format=response_format,
                )
            except LLMCallCancelled as exc:
                _emit_request(None)
                raise RememberCancelled() from exc
            except Exception as exc:
                _emit_request(None)
                if self._trace_events:
                    self._emit(
                        "llm_error",
                        f"{self._label} LLM error",
                        {"block": self._context(), "model": self.model, "error": str(exc)},
                    )
                raise
        finally:
            _set_call_cancel_predicate(previous_cancel)
            _set_request_observer(previous_observer)
        # Safety net: a provider that advertises reporting but returned without
        # ever calling the observer must still produce its one request event.
        _emit_request(None)
        from okto_neuron.llm import last_call_stats

        usage = last_call_stats()
        if self._on_completion is not None:
            self._on_completion(usage)
        if self._trace_events:
            self._emit(
                "llm_response",
                f"{self._label} LLM response",
                {
                    "block": self._context(),
                    "model": self.model,
                    "response": response,
                    "usage": usage,
                },
            )
        if self._should_cancel is not None and self._should_cancel():
            raise RememberCancelled()
        return response


class _StepLabelledProvider:
    """LLMProvider wrapper that stamps the calling pipeline step onto every
    completion, so per-call log lines (the ``codex call model=... duration=
    ...`` / ``litellm usage ...`` lines) say WHICH step made the call instead
    of reading as one undifferentiated stream. Delegates every call
    unchanged; the only side effect is ``okto_neuron.llm.set_call_step``
    before each ``complete()`` — the label is read back by the log line
    itself via ``current_call_step()``. Always sets the label on entry
    (never relies on a caller resetting it), so one step's label can never
    leak onto a neighbouring call's log line."""

    def __init__(
        self,
        provider: "LLMProvider",
        step: str,
        *,
        on_completion: Callable[[Mapping[str, Any] | None], None] | None = None,
    ) -> None:
        self._provider = provider
        self._step = step
        self._on_completion = on_completion
        # Forwarded, not re-declared: this wrapper sits between the tracing
        # wrapper and the real provider, so the underlying provider's ability
        # to report its effective request has to survive the hop.
        self.traces_effective_request = bool(getattr(provider, "traces_effective_request", False))
        self.model = str(getattr(provider, "model", "unknown"))
        if hasattr(provider, "api_base"):
            self.api_base = getattr(provider, "api_base")

    def complete(
        self,
        messages: object,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: "ResponseFormat | None" = None,
    ) -> str:
        # Defect I fix: save/restore the PREVIOUS step label around the
        # delegated call instead of just setting it. Without a restore, the
        # label leaks past this call onto the thread — CONFIRMED to break the
        # combined pytest run (tests/companion leaves e.g. "curator" set on
        # the main thread; tests/llm then fails asserting the default "-").
        # ``okto_neuron.llm`` only exposes set_call_step()/current_call_step()
        # (write-only / read-with-default-"-"), neither of which returns the
        # prior raw value, so the raw thread-local is read directly here
        # rather than widening that module's public signature.
        from okto_neuron.llm import _call_step, set_call_step

        prev_step = getattr(_call_step, "value", None)
        set_call_step(self._step)
        try:
            response = self._provider.complete(
                messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                enable_thinking=enable_thinking,
                response_format=response_format,
            )
            if self._on_completion is not None:
                from okto_neuron.llm import last_call_stats

                self._on_completion(last_call_stats())
            return response
        finally:
            _call_step.value = prev_step


# Per-thread extraction context for ADR 0036 fan-out. The tracing wrapper reads
# this instead of the caller's loop variable so concurrent requests keep the
# correct byte anchor and block index.
_extraction_trace_context = threading.local()


def _extract_with_trace_context(
    extractor: "Extractor",
    text: str,
    provenance: object,
    context: "dict[str, Any]",
) -> "ExtractionResult":
    previous = getattr(_extraction_trace_context, "value", None)
    _extraction_trace_context.value = context
    try:
        return extractor.extract(text, provenance=provenance)  # type: ignore[arg-type]
    finally:
        _extraction_trace_context.value = previous


# Per-thread trace context for curation fan-out (ADR 0015 D1). Workers set
# this before each curate() call; the _TracingLLMProvider context callbacks
# read it first and fall back to the loop-local dict in sequential mode.
_curation_trace_context = threading.local()
_type_adjudication_trace_context = threading.local()


def _set_curation_trace_context(context: "dict[str, Any]") -> None:
    """Trace-context setter handed to the ADR 0015 D4 batch runner — same
    thread-local the per-candidate closures set before single calls."""
    _curation_trace_context.value = context


def _iter_fan_out_verdicts(
    calls: "Sequence[Callable[[], CuratorVerdict]]",
    *,
    max_concurrent: int,
    timeout_s: float | None,
) -> "Iterator[CuratorVerdict]":
    """Run curator calls with bounded concurrency, yielding in input order.

    ADR 0015 D1: the LLM round-trip is the only thing that runs off-thread —
    prompts are prebuilt serially (store reads stay on the calling thread) and
    all gate decisions + ledger writes happen on the calling thread afterwards,
    in original candidate order. A call that times out or raises degrades to
    the same abstain verdict a provider failure produces today. Transient
    provider retry is NOT done here: each curator call already applies the
    ADR 0039 D5 policy itself (``complete_with_retry``) and records it on the
    verdict, so a second retry layer here would double the attempts.

    ADR 0015 D5a: this is a generator — verdict *i* is yielded as soon as the
    ordered prefix ``0..i`` of futures completes, so the consuming post-pass
    can ledger each verdict immediately instead of waiting for all N. Workers
    are oblivious (throughput unchanged); sequential mode degenerates to
    call → yield → call → yield, the pre-D1 behavior.
    """
    from okto_neuron.curator import CuratorVerdict

    def _timeout_verdict() -> "CuratorVerdict":
        return CuratorVerdict(action="abstain", confidence=0.0, reason="curation-timeout")

    def _run_one(call: "Callable[[], CuratorVerdict]") -> "CuratorVerdict":
        from okto_neuron.llm import _scoped_call_timeout

        started = time.monotonic()
        deadline = started + timeout_s if timeout_s is not None else None

        def _invoke() -> "CuratorVerdict":
            remaining = max(deadline - time.monotonic(), 0.0) if deadline else None
            if remaining is not None and remaining <= 0.0:
                return _timeout_verdict()
            with _scoped_call_timeout(remaining):
                return call()

        verdict = _invoke()
        if deadline is not None and time.monotonic() >= deadline:
            return _timeout_verdict()
        return verdict

    if max_concurrent <= 1 or len(calls) <= 1:
        for call in calls:
            yield _run_one(call)
        return

    from okto_neuron.llm import bind_parent

    with ThreadPoolExecutor(max_workers=min(max_concurrent, len(calls))) as pool:
        # Only reached when curation concurrency is raised above 1; the inline
        # branch above keeps the parent for free. Without this, turning that
        # knob up would silently empty the ingest trace.
        futures = [pool.submit(bind_parent(_run_one), call) for call in calls]
        for future in futures:
            try:
                yield future.result(timeout=timeout_s)
            except FuturesTimeoutError:
                yield _timeout_verdict()
            except Exception as exc:  # noqa: BLE001 — fail closed per candidate
                yield CuratorVerdict(
                    action="abstain",
                    confidence=0.0,
                    reason=f"curation-error: {exc}",
                )


def _fan_out_verdicts(
    calls: "Sequence[Callable[[], CuratorVerdict]]",
    *,
    max_concurrent: int,
    timeout_s: float | None,
) -> "list[CuratorVerdict]":
    """List-collecting wrapper over :func:`_iter_fan_out_verdicts`.

    Kept for callers that need the whole verdict set at once (the D4 batch
    runner's fallback path); semantics are identical.
    """
    return list(_iter_fan_out_verdicts(calls, max_concurrent=max_concurrent, timeout_s=timeout_s))


# Verdict actions that can be replayed verbatim from a prior run's ledger
# record (ADR 0015 D5b). Anything else (e.g. derived "superseded" rows) is
# simply judged fresh.
_REPLAYABLE_ACTIONS = frozenset({"commit", "queue"})

_RELATION_VERDICT_FIELDS = frozenset(
    {
        "canonical_predicate",
        "predicate_definition",
        "predicate_direction",
        "inverse_direction_required",
        "subject_supported",
        "predicate_supported",
        "object_supported",
        "direction_supported",
        "unsupported_inference",
        "structural_noise",
        "redundant",
        "useful",
    }
)


def _replayable_verdicts(
    records: "dict[str, dict[str, Any]]",
    *,
    relation: bool = False,
) -> "dict[str, dict[str, Any]]":
    return {
        candidate_id: record
        for candidate_id, record in records.items()
        if record.get("verdict") in _REPLAYABLE_ACTIONS
        and (
            not relation
            or (
                isinstance(record.get("payload"), dict)
                and _RELATION_VERDICT_FIELDS <= set(record["payload"])
            )
        )
    }


def _prior_verdicts_for_method(
    verdicts_by_method: "dict[str, dict[str, dict[str, Any]]]",
    method: str,
    *,
    relation: bool = False,
) -> "dict[str, dict[str, Any]]":
    """Resolve original and already-replayed verdicts without breaking chains."""

    records = dict(verdicts_by_method.get(method, {}))
    for replay_method in ("resume_replay", "policy_replay"):
        for candidate_id, record in verdicts_by_method.get(replay_method, {}).items():
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            original_method = payload.get("original_method")
            if not original_method:
                # Early policy-replay rows predate explicit origin metadata.
                # Their structured relation evidence is nevertheless sufficient
                # to distinguish relation verdicts from node verdicts. Normalize
                # the record in memory so the next replay persists the recovered
                # origin and an arbitrary number of rebuild generations can chain.
                original_method = (
                    "relation_curator" if _RELATION_VERDICT_FIELDS <= set(payload) else "curator"
                )
                record = {
                    **record,
                    "payload": {**payload, "original_method": original_method},
                }
            if original_method == method:
                records[candidate_id] = record
    return _replayable_verdicts(records, relation=relation)


def _replay_verdict(
    record: "dict[str, Any]",
    *,
    relation: bool = False,
) -> "CuratorVerdict":
    """Reconstruct a CuratorVerdict from a prior run's comparison record."""
    from okto_neuron.curator import CuratorVerdict

    payload = record.get("payload")
    canonical = payload.get("canonical_predicate") if isinstance(payload, dict) else None
    try:
        confidence = float(record.get("score") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    structured: dict[str, Any] = {}
    if relation:
        if not isinstance(payload, dict) or not _RELATION_VERDICT_FIELDS <= set(payload):
            raise ValueError("relation replay is missing structured curator evidence")
        structured = {
            field: payload[field]
            for field in _RELATION_VERDICT_FIELDS
            if field != "canonical_predicate"
        }
    return CuratorVerdict(
        action=str(record.get("verdict")),  # pre-filtered to _REPLAYABLE_ACTIONS
        confidence=confidence,
        reason=str(record.get("reason") or ""),
        canonical_predicate=str(canonical or ""),
        **structured,
    )


def _replay_meta(
    record: "dict[str, Any]",
    *,
    mode: str = "resume_replay",
) -> "dict[str, Any]":
    payload = record.get("payload")
    original_method = (
        payload.get("original_method")
        if isinstance(payload, dict) and payload.get("original_method")
        else record.get("method")
    )
    meta = {
        "replayed_from_ts": record.get("ts"),
        "replayed_from_run_id": record.get("run_id"),
        "original_method": original_method,
        "replay_mode": mode,
    }
    if isinstance(payload, dict):
        for flag in ("terminal_candidate_replay", "semantic_replay_conflict"):
            if payload.get(flag) is True:
                meta[flag] = True
    return meta


def _candidate_record_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    candidate = payload.get("candidate")
    return candidate if isinstance(candidate, Mapping) else payload


def _terminal_node_replay_record(
    record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project one sealed exact-candidate outcome back onto the node gate.

    A fresh rebuild can encounter a raw node before the same-title survivor
    that caused it to be deterministically folded in the authority run.  Its
    audit-only curator row is intentionally not live decision authority, but
    the final candidate row is still a sealed, exact-id materialization
    outcome.  Replaying that conservative terminal state avoids redrawing an
    LLM verdict while the rebuild still reruns reconciliation, relation
    liveness, planning, and every graph write.
    """

    if record is None:
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("candidate"), Mapping):
        return None

    action = ""
    confidence = 0.0
    terminal_state = str(payload.get("terminal_state") or "")
    reason = str(payload.get("reason") or "")
    outcome = payload.get("outcome")
    if isinstance(outcome, Mapping):
        outcome_action = str(outcome.get("action") or "")
        if outcome_action == "committed":
            action = "commit"
        elif outcome_action == "queued":
            action = "queue"
        try:
            confidence = float(outcome.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        terminal_state = terminal_state or outcome_action
    elif terminal_state == "superseded" and payload.get("llm_skipped") is True:
        # The exact raw candidate was already accepted as a foregone
        # same-identity re-mention, then folded into its survivor.
        action = "commit"
        curator_verdict = payload.get("curator_verdict")
        if isinstance(curator_verdict, Mapping):
            try:
                confidence = float(curator_verdict.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(curator_verdict.get("reason") or reason)

    if action not in _REPLAYABLE_ACTIONS:
        return None
    return {
        "ts": record.get("ts"),
        "run_id": record.get("run_id"),
        "candidate_id": record.get("candidate_id"),
        "method": "candidate_terminal",
        "verdict": action,
        "score": confidence,
        "reason": reason or f"sealed exact-candidate terminal state: {terminal_state}",
        "payload": {
            "original_method": "curator",
            "terminal_candidate_replay": True,
            "terminal_state": terminal_state,
        },
    }


def _candidate_replay_lineage(
    candidate_id: str,
    derived_from_by_id: Mapping[str, str],
) -> tuple[str, ...]:
    """Return deterministic candidate ancestors, nearest first.

    Type correction derives new node and edge ids without changing the source
    evidence being adjudicated.  Exact-policy decisions therefore follow the
    explicit ``derived_from`` chain; a cycle or blank link fails closed.
    """

    ancestors: list[str] = []
    seen = {candidate_id}
    current = candidate_id
    while True:
        parent = str(derived_from_by_id.get(current) or "")
        if not parent or parent in seen:
            return tuple(ancestors)
        ancestors.append(parent)
        seen.add(parent)
        current = parent


def _record_candidate_replay_parent(
    derived_id: str,
    source_id: str,
    derived_from_by_id: dict[str, str],
) -> None:
    """Record one unambiguous derivation; conflicting ancestry fails closed."""

    if not derived_id or not source_id or derived_id == source_id:
        return
    if derived_id not in derived_from_by_id:
        derived_from_by_id[derived_id] = source_id
    elif derived_from_by_id[derived_id] != source_id:
        derived_from_by_id[derived_id] = ""


def _relation_replay_key(
    candidate: object,
    *,
    node_context: Mapping[str, object],
    store: object,
    prior_node_identity_by_id: Mapping[str, tuple[str, str]],
) -> tuple[object, ...] | None:
    """Return a conservative relation identity independent of physical node ids.

    Candidate ids include descriptive node content, so exact-title entity
    reconciliation can legitimately remap the same extracted relation onto a
    different physical endpoint in another source order.  Completed semantic
    authority must follow the supported S-P-O evidence, not that incidental id.
    The source block remains part of the key and ambiguous/missing endpoints do
    not match, preserving fail-closed replay.
    """

    def _field(name: str, default: object = "") -> object:
        if isinstance(candidate, Mapping):
            return candidate.get(name, default)
        return getattr(candidate, name, default)

    def _endpoint(ref: object) -> tuple[str, str] | None:
        node_id = str(ref or "")
        if not node_id:
            return None
        node = node_context.get(node_id)
        if node is not None:
            type_ = str(getattr(node, "type", "") or "").strip()
            title = exact_surface_key(getattr(node, "title", ""))
            if type_ and title:
                return (type_, title)
        get_node = getattr(store, "get_node", None)
        stored = get_node(node_id) if callable(get_node) else None
        if stored is not None:
            type_ = str(getattr(stored, "type", "") or "").strip()
            title = exact_surface_key(getattr(stored, "title", ""))
            if type_ and title:
                return (type_, title)
        prior = prior_node_identity_by_id.get(node_id)
        if prior is None:
            return None
        type_, raw_title = prior
        title = exact_surface_key(raw_title)
        return (str(type_).strip(), title) if type_ and title else None

    subject = _endpoint(_field("src_ref"))
    predicate = exact_surface_key(_field("type"))
    block_id = str(_field("block_id") or "")
    literal = _field("dst_literal", None)
    if literal is not None:
        object_key: tuple[object, ...] = (
            "literal",
            type(literal).__name__,
            literal,
        )
    else:
        object_endpoint = _endpoint(_field("dst_ref"))
        if object_endpoint is None:
            return None
        object_key = ("node", *object_endpoint)
    if subject is None or not predicate or not block_id:
        return None
    return (block_id, subject, predicate, object_key)


def _semantic_relation_replay_index(
    verdicts: Mapping[str, dict[str, Any]],
    candidate_records: Mapping[str, dict[str, Any]],
    *,
    node_context: Mapping[str, object],
    store: object,
    prior_node_identity_by_id: Mapping[str, tuple[str, str]],
) -> dict[tuple[object, ...], dict[str, Any]]:
    """Index semantic relation authority, rejecting sealed conflicts."""

    indexed: dict[tuple[object, ...], dict[str, Any]] = {}
    signatures: dict[tuple[object, ...], str] = {}
    ambiguous: set[tuple[object, ...]] = set()
    for candidate_id, record in verdicts.items():
        candidate_record = candidate_records.get(candidate_id)
        if candidate_record is None:
            continue
        key = _relation_replay_key(
            _candidate_record_payload(candidate_record),
            node_context=node_context,
            store=store,
            prior_node_identity_by_id=prior_node_identity_by_id,
        )
        if key is None or key in ambiguous:
            continue
        verdict = _replay_verdict(record, relation=True)
        signature = repr(
            (
                verdict.action,
                verdict.confidence,
                verdict.reason,
                tuple(sorted(_relation_verdict_evidence(verdict).items())),
            )
        )
        prior_signature = signatures.get(key)
        if prior_signature is None:
            indexed[key] = record
            signatures[key] = signature
        elif prior_signature != signature:
            prior = indexed[key]
            indexed[key] = {
                "ts": record.get("ts"),
                "run_id": record.get("run_id"),
                "candidate_id": record.get("candidate_id"),
                "method": "semantic_replay_conflict",
                "verdict": "queue",
                "score": 0.0,
                "reason": (
                    "conflicting sealed semantic relation verdicts; "
                    "fail closed without another model call"
                ),
                "payload": {
                    "original_method": "relation_curator",
                    "semantic_replay_conflict": True,
                    "conflicting_run_ids": sorted(
                        {
                            str(prior.get("run_id") or ""),
                            str(record.get("run_id") or ""),
                        }
                        - {""}
                    ),
                    "canonical_predicate": "",
                    "predicate_definition": "",
                    "predicate_direction": "unknown",
                    "inverse_direction_required": False,
                    "subject_supported": False,
                    "predicate_supported": False,
                    "object_supported": False,
                    "direction_supported": False,
                    "unsupported_inference": True,
                    "structural_noise": False,
                    "redundant": False,
                    "useful": False,
                },
            }
            signatures.pop(key, None)
            ambiguous.add(key)
    return indexed


def _claims_for_contradiction_scan(
    store: "GraphStore", candidates: "list[NodeCandidate]"
) -> "list[Node] | None":
    """One ``list_nodes("Claim")`` shared by a batch of ``resolve()`` calls.

    ``find_contradictions`` otherwise re-reads every Claim once per Claim
    candidate. The two resolve loops in ``remember`` only read the store, so one
    snapshot taken right before a loop is what each call would have seen. ``None``
    when the batch holds no Claim candidate (nothing would scan)."""
    if not any(candidate.type == "Claim" for candidate in candidates):
        return None
    return list(store.list_nodes(type="Claim"))


def _verdict_telemetry(verdict: object) -> dict[str, object]:
    """LLM call telemetry from a CuratorVerdict for ledger comparison payloads.

    ADR 0015 D3.3: duration + provider-reported token usage per curator call,
    so profiling never needs log archaeology again. Empty for synthetic
    verdicts (no LLM call was made).
    """
    telemetry: dict[str, object] = {}
    duration_s = getattr(verdict, "duration_s", None)
    if duration_s is not None:
        telemetry["duration_s"] = duration_s
    usage = getattr(verdict, "usage", None)
    if usage:
        telemetry["usage"] = usage
    # ADR 0039 D5: a transient provider failure the call retried. Present only
    # when a retry happened, so an unretried row is byte-identical to before.
    provider_retries = getattr(verdict, "provider_retries", None)
    if provider_retries:
        telemetry["provider_retries"] = [dict(retry) for retry in provider_retries]
    return telemetry


def _relation_verdict_evidence(verdict: object) -> dict[str, object]:
    """Complete replay-pinned evidence emitted by the structured relation curator."""

    return {field: getattr(verdict, field) for field in sorted(_RELATION_VERDICT_FIELDS)}


def _anchor_payload(anchor: object | None) -> dict[str, object] | None:
    if anchor is None:
        return None
    return {
        "block_id": getattr(anchor, "block_id", None),
        "byte_start": getattr(anchor, "byte_start", None),
        "byte_end": getattr(anchor, "byte_end", None),
        "content_hash": getattr(anchor, "content_hash", None),
        "source_path": getattr(anchor, "source_path", None),
    }


def _node_candidate_payload(candidate: object) -> dict[str, object]:
    return {
        "candidate_id": getattr(candidate, "candidate_id", ""),
        "type": getattr(candidate, "type", ""),
        "title": getattr(candidate, "title", ""),
        "content": getattr(candidate, "content", ""),
        "facets": dict(getattr(candidate, "facets", {}) or {}),
    }


def _edge_candidate_payload(candidate: object) -> dict[str, object]:
    return {
        "type": getattr(candidate, "type", ""),
        "src_ref": getattr(candidate, "src_ref", ""),
        "dst_ref": getattr(candidate, "dst_ref", ""),
        "dst_literal": getattr(candidate, "dst_literal", None),
        "block_id": getattr(candidate, "block_id", None),
        "byte_start": getattr(candidate, "byte_start", None),
        "byte_end": getattr(candidate, "byte_end", None),
        "content_hash": getattr(candidate, "content_hash", None),
        "confidence": getattr(candidate, "confidence", None),
    }


def _extraction_result_payload(extraction: object) -> dict[str, object]:
    """Replay payload for a successful unit; never includes source text."""

    def _candidate_payload(candidate: object) -> dict[str, object]:
        payload = candidate.model_dump(mode="json")
        payload.pop("embedding", None)
        return payload

    return {
        "nodes": [
            _candidate_payload(candidate)
            for candidate in getattr(extraction, "node_candidates", ())
        ],
        "edges": [
            _candidate_payload(candidate)
            for candidate in getattr(extraction, "edge_candidates", ())
        ],
        "truncated": bool(getattr(extraction, "truncated", False)),
        "unexpected_finish": getattr(extraction, "unexpected_finish", None),
        "empty_after_retry": bool(getattr(extraction, "empty_after_retry", False)),
        "parse_failed": bool(getattr(extraction, "parse_failed", False)),
    }


def _extraction_result_from_payload(payload: object) -> "ExtractionResult":
    """Rehydrate one validated unit result without invoking a provider."""

    from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
    from okto_neuron.extract import ExtractionResult

    if not isinstance(payload, dict):
        raise ValueError("extraction unit replay result must be an object")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("extraction unit replay candidates must be lists")
    unexpected_finish = payload.get("unexpected_finish")
    if unexpected_finish is not None and not isinstance(unexpected_finish, str):
        raise ValueError("extraction unit unexpected_finish must be text or null")
    return ExtractionResult(
        node_candidates=[NodeCandidate.model_validate(candidate) for candidate in nodes],
        edge_candidates=[EdgeCandidate.model_validate(candidate) for candidate in edges],
        truncated=bool(payload.get("truncated", False)),
        unexpected_finish=unexpected_finish,
        empty_after_retry=bool(payload.get("empty_after_retry", False)),
        parse_failed=bool(payload.get("parse_failed", False)),
    )


def _entity_key(candidate: object) -> tuple[str, str] | None:
    title = exact_surface_key(getattr(candidate, "title", ""))
    type_ = str(getattr(candidate, "type", "")).strip()
    if not title or not type_:
        return None
    return (type_, title)


def _type_evidence_excerpt(candidate: object, source_text: str, *, limit: int = 900) -> str:
    """Return bounded local source evidence for one primitive-type decision."""

    text = source_text.strip()
    if not text:
        return ""
    title = str(getattr(candidate, "title", "")).strip()
    offset = text.casefold().find(title.casefold()) if title else -1
    if offset < 0:
        return text[:limit]
    half = limit // 2
    start = max(0, offset - half)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return text[start:end]


_IDENTITY_PRIMITIVES = frozenset(("Agent", "Activity", "InformationObject", "Concept", "Place"))


@dataclass(frozen=True)
class _IdentityCorrectionResult:
    nodes: list[NodeCandidate]
    edges: list[EdgeCandidate]
    source_text_by_id: dict[str, str]
    original_ids_by_current: dict[str, frozenset[str]]
    corrected_candidate_ids: frozenset[str]
    correction_review_intents: dict[str, dict[str, object]]
    applications: tuple[dict[str, object], ...]
    edge_derivations: tuple[tuple[EdgeCandidate, EdgeCandidate], ...]


def _apply_identity_type_corrections(
    nodes: list[NodeCandidate],
    edges: list[EdgeCandidate],
    source_text_by_id: dict[str, str],
    decisions: IdentityDecisionIndex,
    *,
    runtime_corrections: Mapping[str, str] | None = None,
    runtime_reasons: Mapping[str, str] | None = None,
    original_ids_by_current: Mapping[str, frozenset[str]] | None = None,
    include_pinned: bool = True,
) -> _IdentityCorrectionResult:
    """Apply pinned or runtime type corrections before identity resolution."""

    from okto_neuron.reconcile.decisions import TypeCorrection

    corrections_by_id: dict[str, list[TypeCorrection]] = {}
    if include_pinned:
        for decision in decisions.records():
            if isinstance(decision, TypeCorrection):
                corrections_by_id.setdefault(decision.candidate_id, []).append(decision)

    proposed: list[tuple[NodeCandidate, NodeCandidate, tuple[TypeCorrection, ...]]] = []
    for candidate in nodes:
        if candidate.type not in _IDENTITY_PRIMITIVES:
            proposed.append((candidate, candidate, ()))
            continue
        chain = tuple(corrections_by_id.get(candidate.candidate_id, ()))
        corrected_type = (
            str(runtime_corrections.get(candidate.candidate_id, candidate.type))
            if runtime_corrections is not None
            else decisions.corrected_type(  # type: ignore[arg-type]
                candidate.candidate_id,
                candidate.type,
            )
        )
        if corrected_type not in _IDENTITY_PRIMITIVES:
            corrected_type = candidate.type
        corrected = (
            candidate
            if corrected_type == candidate.type
            else candidate.model_copy(update={"type": corrected_type})
        )
        proposed.append((candidate, corrected, chain))

    blocked_corrections: dict[str, set[str]] = {}
    for index, (source, derived, _) in enumerate(proposed):
        for other_source, other_derived, _ in proposed[index + 1 :]:
            if derived.candidate_id != other_derived.candidate_id:
                continue
            source_originals = (original_ids_by_current or {}).get(
                source.candidate_id,
                frozenset((source.candidate_id,)),
            )
            other_originals = (original_ids_by_current or {}).get(
                other_source.candidate_id,
                frozenset((other_source.candidate_id,)),
            )
            if any(
                decisions.is_distinct(left, right)
                for left in source_originals
                for right in other_originals
                if left != right
            ):
                if source.candidate_id != derived.candidate_id:
                    blocked_corrections.setdefault(source.candidate_id, set()).add(
                        other_source.candidate_id
                    )
                if other_source.candidate_id != other_derived.candidate_id:
                    blocked_corrections.setdefault(other_source.candidate_id, set()).add(
                        source.candidate_id
                    )

    corrected_nodes: list[NodeCandidate] = []
    node_targets: dict[str, str] = {}
    source_by_current: dict[str, str] = {}
    originals_by_current: dict[str, set[str]] = {}
    corrected_ids: set[str] = set()
    correction_review_intents: dict[str, dict[str, object]] = {}
    applications: list[dict[str, object]] = []

    for candidate, proposed_candidate, chain in proposed:
        source_id = candidate.candidate_id
        conflicts = blocked_corrections.get(source_id)
        if conflicts:
            corrected_nodes.append(candidate)
            node_targets[source_id] = source_id
            originals_by_current.setdefault(source_id, set()).update(
                (original_ids_by_current or {}).get(
                    source_id,
                    frozenset((source_id,)),
                )
            )
            source_text = source_text_by_id.get(source_id)
            if source_text is not None:
                source_by_current.setdefault(source_id, source_text)
            correction_review_intents[source_id] = {
                "code": "identity_type_correction_distinct_collision",
                "candidate_type": candidate.type,
                "proposed_type": proposed_candidate.type,
                "proposed_candidate_id": proposed_candidate.candidate_id,
                "conflicts": tuple(sorted(conflicts)),
                "disposition": "queue_review",
                "sidecar_write": False,
            }
            continue
        corrected = proposed_candidate
        current_id = corrected.candidate_id
        corrected_nodes.append(corrected)
        node_targets[source_id] = current_id
        originals_by_current.setdefault(current_id, set()).update(
            {
                *(original_ids_by_current or {}).get(
                    source_id,
                    frozenset((source_id,)),
                ),
                current_id,
            }
        )
        source_text = source_text_by_id.get(source_id)
        if source_text is not None:
            source_by_current.setdefault(current_id, source_text)
        runtime_applied = (
            runtime_corrections is not None
            and corrected.type != candidate.type
            and candidate.candidate_id in runtime_corrections
        )
        if chain or runtime_applied:
            corrected_ids.add(current_id)
            applications.append(
                {
                    "source_candidate": candidate,
                    "derived_candidate": corrected,
                    "source_candidate_id": source_id,
                    "derived_candidate_id": current_id,
                    "previous_type": candidate.type,
                    "corrected_type": corrected.type,
                    "decision_ids": (
                        tuple(item.decision_id for item in chain)
                        if chain
                        else ("runtime:type_adjudication.v2",)
                    ),
                    "reasons": (
                        tuple(item.reason for item in chain)
                        if chain
                        else (str((runtime_reasons or {}).get(source_id) or "type adjudication"),)
                    ),
                }
            )

    remapped_edges = _remap_edge_candidates_to_node_targets(edges, node_targets)
    edge_derivations: list[tuple[EdgeCandidate, EdgeCandidate]] = []
    for edge in edges:
        src_ref = node_targets.get(edge.src_ref, edge.src_ref)
        dst_ref = (
            edge.dst_ref
            if edge.dst_literal is not None
            else node_targets.get(edge.dst_ref, edge.dst_ref)
        )
        if src_ref == edge.src_ref and dst_ref == edge.dst_ref:
            continue
        if edge.dst_literal is None and src_ref == dst_ref:
            continue
        edge_derivations.append(
            (edge, edge.model_copy(update={"src_ref": src_ref, "dst_ref": dst_ref}))
        )

    return _IdentityCorrectionResult(
        nodes=corrected_nodes,
        edges=remapped_edges,
        source_text_by_id=source_by_current,
        original_ids_by_current={
            current_id: frozenset(originals)
            for current_id, originals in originals_by_current.items()
        },
        corrected_candidate_ids=frozenset(corrected_ids),
        correction_review_intents=correction_review_intents,
        applications=tuple(applications),
        edge_derivations=tuple(edge_derivations),
    )


def _is_explicitly_distinct(
    decisions: IdentityDecisionIndex,
    original_ids_by_current: dict[str, frozenset[str]],
    left_id: str,
    right_id: str,
) -> bool:
    left_ids = original_ids_by_current.get(left_id, frozenset((left_id,)))
    right_ids = original_ids_by_current.get(right_id, frozenset((right_id,)))
    return any(
        decisions.is_distinct(left, right)
        for left in left_ids
        for right in right_ids
        if left != right
    )


def _cross_type_identity_review_intents(
    nodes: list[NodeCandidate],
    store: GraphStore,
    decisions: IdentityDecisionIndex,
    original_ids_by_current: dict[str, frozenset[str]],
    corrected_candidate_ids: frozenset[str],
) -> dict[str, dict[str, object]]:
    """Return unresolved cross-type surface conflicts for new candidates.

    Buckets on the DISCOVERY key, not the exact key: the exact key keeps
    diacritic variants apart by design, so an Agent "Renato André ..." and a
    Concept "Renato Andre ..." landed in different buckets, no conflict was ever
    recorded, and type adjudication never ran on the one pair that most needed
    it. Recall here is discovery-only — every conflict it raises is still
    settled by the adjudicating LLM.
    """

    by_surface: dict[str, list[tuple[str, str, str]]] = {}
    for candidate in nodes:
        if candidate.type not in _IDENTITY_PRIMITIVES:
            continue
        surface = discovery_surface_key(candidate.title)
        if surface:
            by_surface.setdefault(surface, []).append(
                (candidate.candidate_id, candidate.type, "run")
            )
    for primitive in ("Agent", "Activity", "InformationObject", "Concept", "Place"):
        for node in store.list_nodes(type=primitive):
            if is_infra(node):
                continue
            surface = discovery_surface_key(node.title)
            if surface:
                by_surface.setdefault(surface, []).append((node.id, node.type, "store"))

    intents: dict[str, dict[str, object]] = {}
    for candidate in nodes:
        if candidate.type not in _IDENTITY_PRIMITIVES:
            continue
        if candidate.candidate_id in corrected_candidate_ids:
            continue  # this candidate already has an explicit type decision
        if store.get_node(candidate.candidate_id) is not None:
            continue  # established re-mentions are not new review work
        surface = discovery_surface_key(candidate.title)
        conflicts: list[dict[str, str]] = []
        for target_id, target_type, source in by_surface.get(surface, []):
            if target_id == candidate.candidate_id or target_type == candidate.type:
                continue
            if _is_explicitly_distinct(
                decisions,
                original_ids_by_current,
                candidate.candidate_id,
                target_id,
            ):
                continue
            conflicts.append(
                {
                    "target_ref": target_id,
                    "target_type": target_type,
                    "source": source,
                }
            )
        if conflicts:
            intents[candidate.candidate_id] = {
                "code": "identity_cross_type_exact_collision",
                "exact_surface_key": surface,
                "candidate_type": candidate.type,
                "conflicts": tuple(
                    sorted(
                        conflicts,
                        key=lambda item: (item["source"], item["target_type"], item["target_ref"]),
                    )
                ),
                "disposition": "queue_review",
                "sidecar_write": False,
            }
    return intents


def _remap_edge_candidates_to_node_targets(
    candidates: list[EdgeCandidate],
    node_targets: dict[str, str],
) -> list[EdgeCandidate]:
    if not node_targets:
        return candidates

    remapped: list[EdgeCandidate] = []
    seen: set[tuple[object, ...]] = set()
    for candidate in candidates:
        src_ref = node_targets.get(candidate.src_ref, candidate.src_ref)
        dst_ref = (
            candidate.dst_ref
            if candidate.dst_literal is not None
            else node_targets.get(candidate.dst_ref, candidate.dst_ref)
        )
        if candidate.dst_literal is None and src_ref == dst_ref:
            continue
        if src_ref == candidate.src_ref and dst_ref == candidate.dst_ref:
            new_candidate = candidate
        else:
            new_candidate = candidate.model_copy(update={"src_ref": src_ref, "dst_ref": dst_ref})
        key = (
            new_candidate.type,
            new_candidate.src_ref,
            new_candidate.dst_ref,
            new_candidate.dst_literal,
            new_candidate.block_id,
        )
        if key in seen:
            continue
        seen.add(key)
        remapped.append(new_candidate)
    return remapped


def _record_edge_candidate_replay_derivations(
    candidates: Sequence[EdgeCandidate],
    node_targets: Mapping[str, str],
    derived_from_by_id: dict[str, str],
) -> None:
    """Carry relation decision ancestry through deterministic endpoint remaps.

    Candidate ids include endpoint ids. Every deterministic node merge can
    therefore derive a new relation id even though the source evidence is
    unchanged. Record only a unique parent; competing parents deliberately
    poison the link so replay falls closed instead of choosing arbitrarily.
    """

    if not node_targets:
        return

    from okto_neuron.consolidate.ledger import edge_candidate_id

    for candidate in candidates:
        src_ref = node_targets.get(candidate.src_ref, candidate.src_ref)
        dst_ref = (
            candidate.dst_ref
            if candidate.dst_literal is not None
            else node_targets.get(candidate.dst_ref, candidate.dst_ref)
        )
        if candidate.dst_literal is None and src_ref == dst_ref:
            continue
        if src_ref == candidate.src_ref and dst_ref == candidate.dst_ref:
            continue
        derived = candidate.model_copy(update={"src_ref": src_ref, "dst_ref": dst_ref})
        source_id = edge_candidate_id(candidate.model_dump(mode="json"))
        derived_id = edge_candidate_id(derived.model_dump(mode="json"))
        _record_candidate_replay_parent(
            derived_id,
            source_id,
            derived_from_by_id,
        )


def _semantic_edge_key(
    candidate: object,
    *,
    packs: Sequence[str] | None = None,
    predicate_aliases: dict[str, str] | None = None,
) -> tuple[str, str, str] | None:
    from okto_neuron.curator import normalize_predicate

    src_ref = str(getattr(candidate, "src_ref", "")).strip()
    predicate = normalize_predicate(
        getattr(candidate, "type", ""),
        packs=packs,
        predicate_aliases=predicate_aliases,
    )
    if not predicate:
        predicate = str(getattr(candidate, "type", "")).strip()
    literal = getattr(candidate, "dst_literal", None)
    object_identity = (
        claim_object_identity(literal=literal)
        if literal is not None
        else claim_object_identity(object_id=getattr(candidate, "dst_ref", ""))
    )
    if not src_ref or not predicate or not object_identity:
        return None
    return (src_ref, predicate, object_identity)


def _collapse_exact_edge_candidates(
    candidates: list[EdgeCandidate],
    *,
    packs: Sequence[str] | None = None,
    predicate_aliases: dict[str, str] | None = None,
) -> tuple[list[EdgeCandidate], list[tuple[EdgeCandidate, EdgeCandidate]]]:
    """Collapse accepted relation candidates with the same resolved S-P-O key.

    The first candidate is the survivor. Folded candidates are returned with
    their survivor so the caller can record ledger merges and carry their source
    anchors into Claim provenance minting.
    """

    survivors: list[EdgeCandidate] = []
    survivor_by_key: dict[tuple[str, str, str], EdgeCandidate] = {}
    folded: list[tuple[EdgeCandidate, EdgeCandidate]] = []
    for candidate in candidates:
        key = _semantic_edge_key(
            candidate,
            packs=packs,
            predicate_aliases=predicate_aliases,
        )
        if key is None:
            survivors.append(candidate)
            continue
        survivor = survivor_by_key.get(key)
        if survivor is None:
            survivor_by_key[key] = candidate
            survivors.append(candidate)
            continue
        folded.append((candidate, survivor))
    return survivors, folded


def _predicate_alias_map_for_vault(vault_path: Path) -> dict[str, str]:
    from okto_neuron.predicates import PredicateAliasIndex

    return PredicateAliasIndex(vault_path).alias_map()


class ReviewItem(_Frozen):
    """A candidate parked for human/LLM curation."""

    candidate_id: str
    type: str
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: ReviewReason
    correlations: tuple[Correlation, ...] = ()
    kind: Literal["node"] = "node"
    source_path: str | None = None
    block_id: str | None = None
    byte_start: int | None = Field(default=None, ge=0)
    byte_end: int | None = Field(default=None, ge=0)
    content_hash: str | None = None


# ── errors ──────────────────────────────────────────────────────────────────--
class CompanionError(OktoNeuronError):
    """Base for autonomous-companion failures."""

    default_message = "Companion operation failed"


class LLMUnavailableError(CompanionError):
    """The configured LLM provider could not be reached."""

    default_message = "LLM provider unavailable"

    def __init__(
        self,
        message: str | None = None,
        *,
        outcome: dict[str, Any] | None = None,
        ledger_run_id: str | None = None,
        document_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.outcome = dict(outcome) if outcome is not None else {}
        self.ledger_run_id = ledger_run_id
        self.document_id = document_id
        super().__init__(message, cause=cause)


class ReviewItemNotFoundError(CompanionError):
    """No parked candidate with the given id."""

    default_message = "Review item not found"


class _ManualReviewScopeChangedError(ValueError):
    """The queue entry bound to a sealed manual plan was replaced before apply."""


class SourceOutsideVaultError(CompanionError):
    """A ``remember`` source path resolved outside the vault root and every
    configured folder-watch root — a filesystem-read escape (e.g. ``../../etc/
    passwd``). Rejected before any ingest so an attacker cannot pull arbitrary
    on-disk files into the graph (and then exfiltrate them via recall/ask)."""

    default_message = "source path is outside the vault and configured watch roots"


class RememberCancelled(BaseException):
    """Cooperative pre-commit stop signal.

    This intentionally bypasses fail-open ``except Exception`` paths in the
    extraction and curation pipeline. Server workers catch it at the outer
    ``remember()`` boundary and mark the active queue item cancelled.
    """


# ── locality policy ───────────────────────────────────────────────────────────
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


_ASK_SYSTEM = "You answer grounded in the provided notes. Be concise."
"""Default system prompt for the ask path. Overridden by ``llm.ask.system_prompt``
in the vault config. Exposed here so callers can reference the code default.

An extractive variant ("state exact figures/names verbatim, combine across notes,
correct false premises") was tested against this terse default in a controlled
5-sample study (ADR 0018, 2026-06-21) and showed NO significant answer-presence
gain (+0.019, 95% CI [-0.006, +0.046], McNemar p=0.22 — within the temp-0.7 noise
floor) at the cost of a longer prompt. Kept terse. The single-run +0.038 that
first looked like a win was cross-daemon noise. Real answer-presence gains come
from k (ADR 0018) and graph-native answering (ADR 0019), not prompt phrasing."""

# ── GN-13 (ADR 0019 Phase 3): graph-native answerer system prompt ─────────────
# Used ONLY when the subgraph path is active (enable_subgraph=True) AND the
# context was rendered from a typed ego-graph (NODES/RELATIONSHIPS/CLAIMS
# sections), NOT from the raw-block path.  The block-dump path always uses
# _ASK_SYSTEM; this prompt is NEVER selected by default (enable_subgraph
# defaults False).  Overridden per-vault via ``llm.ask.system_prompt_graph``.
#
# Design rationale: the typed render has a very different shape from a prose
# block dump — labelled node indices (N0, N1…), edge arrows (N0 -[predicate]->
# N1), and CLAIMS rows with confidence + block provenance.  A prompt that
# mentions these structures helps the LLM parse them natively rather than
# treating them as opaque text.  Kept terse (ADR 0018: longer prompts gave no
# measurable gain on this corpus; extractive verbosity regressed).
_ASK_SYSTEM_GRAPH = (
    "You answer questions from a structured knowledge graph rendered below. "
    "The graph has three sections: NODES (labelled N0, N1…), RELATIONSHIPS "
    "(N_i -[predicate]-> N_j), and CLAIMS (subject-predicate-object triples "
    "with confidence and block provenance). "
    "Trace entity-predicate-object chains to compose your answer. "
    "Cite claim or block IDs when available (e.g. 'per claim abc123'). "
    "If the graph does not contain the fact, say so — do not invent details "
    "not present in the nodes, relationships, or claims."
)
"""Graph-native system prompt (GN-13, ADR 0019 Phase 3).

Selected only when enable_subgraph=True and the Tier-1 context is a typed
ego-graph render.  The default block-dump path always uses _ASK_SYSTEM.
Override per-vault with ``llm.ask.system_prompt_graph`` in okto-neuron.yaml.

Phase-3 isolation A/B validated this prompt against the generic prompt on the
same L1 graph (+0.061 answer presence; ADR 0019).  The completed evaluation
program accepted subgraph mode as opt-in and retained block mode as the default.
Changing that policy in the future requires a new decision and new evidence; no
default-flip gate remains active in the current program."""

# ── GN-14 (ADR 0019 Phase 3): coverage-fallback threshold ────────────────────
# When the subgraph ego-graph's coverage signal falls BELOW this threshold the
# path skips the Tier-1 graph-native answer and goes straight to the Tier-2
# raw-block fallback (same hits, same vault-root re-validation).  A value of
# 0.0 disables the pre-gate entirely (post-hoc abstention is the only trigger),
# preserving the existing behaviour.  Default is 0.0 so NO behaviour change
# until explicitly configured.  Sweep values {0.2, 0.4, 0.6, 0.8} via the
# GN-3 A/B harness after Phase 2 re-ingest; lock to the value that maximises
# answer-presence while holding neg-guard ≥ 0.91.
#
# The coverage signal itself is _subgraph_context_thin (substantive-row count
# vs floor derived from coverage_threshold) — see that function's docstring.
# This constant is the CODE DEFAULT for the graph-native path; the existing
# _ASK_COVERAGE_DEFAULT (0.4) remains unchanged as the default for the generic
# subgraph-path coverage gate (which was always part of the Tier-1 → Tier-2
# decision).  The graph-native-specific knob is read from
# ``llm.ask.coverage_threshold_graph``; if absent, falls back to this constant.
_ASK_COVERAGE_GRAPH_DEFAULT = 0.0

# ── ADR 0011 subgraph-first ask: code-default fallbacks for the ask-only knobs.
# Read STEP-DIRECT (``cfg.llm.ask.X or default``) — never via ``resolved("ask")``,
# which drops StepLLM-only fields. ``enable_subgraph`` defaults False so the
# default ask path stays byte-identical to the block-dump behaviour.
_ASK_DEGREE_CAP_DEFAULT = 8
_ASK_NEIGHBOUR_BUDGET_DEFAULT = 2000
# Default ego-graph depth. 2 (was 1) so the answering claim can sit one entity hop
# over from the seed and still enter the pool; the render budget cap keeps the
# rendered context bounded regardless of reach. Tunable per-request via
# ``AskRetrievalPolicy.hops`` / ``llm.ask.hops`` (None → this default). The HTTP
# /ask path leaves policy.hops=None so this default flows through; the MCP ``ask``
# tool pins its own ``hops`` default in the tool signature.
_ASK_HOPS_DEFAULT = 2
_ASK_COVERAGE_DEFAULT = 0.4
_ASK_RENDER_DEFAULT = "typed_nodes"
# ``ask`` degrades gracefully to ``text=""`` when the provider fails. Without a
# marker that empty answer is indistinguishable from "the graph knows nothing",
# so every ask path stamps ``retrieval["synthesis_status"]`` and, on a provider
# failure, a BOUNDED ``retrieval["provider_error"]`` summary
# (``_provider_error_summary``, capped at ``llm.PROVIDER_ERROR_SUMMARY_MAX``).


def ask_status(retrieval: Mapping[str, Any]) -> str:
    """Top-level status of an ``ask`` response: ``"ok"`` only for a clean answer.

    Every other ``synthesis_status`` (``no_llm``, ``provider_error``,
    ``truncated``, ``abnormal_stop``, ``empty``) is ``"degraded"``, so a caller
    that reads only the top-level field can never mistake an empty or cut-off
    answer for a success. ``retrieval["synthesis_status"]`` says which.
    """
    return "ok" if retrieval.get("synthesis_status") == "ok" else "degraded"


def _mark_provider_error(trace: dict[str, Any], exc: Exception) -> None:
    """Stamp + log a provider failure that degraded an ask to empty text.

    Purely additive: existing trace keys (notably ``path``) are untouched.
    """
    summary = _provider_error_summary(exc)
    trace["synthesis_status"] = "provider_error"
    trace["provider_error"] = summary
    _LOG.warning(
        "ask synthesis failed — LLM provider error, answer degraded to empty text "
        "(synthesis_status=provider_error, path=%s): %s",
        trace.get("path"),
        summary,
    )


def _completion_finish_state() -> tuple[str | None, str | None, bool]:
    """(normalized, native) finish reason of THIS thread's most recent completion.

    Reads the existing per-call stats channel (``llm.last_call_stats()``, set by
    every provider via ``_set_last_call_stats``) — no change to
    ``_complete_ask``'s return type. The value is overwritten by the next
    completion, so callers must read it IMMEDIATELY after the call returns.
    """
    from okto_neuron.llm import last_call_stats

    stats = last_call_stats() or {}
    normalized = stats.get("finish_reason")
    native = stats.get("native_finish_reason")
    # Set by the LLM layer only when litellm had NO mapping for the provider's
    # raw reason — i.e. an abnormal stop normalized into a clean-looking "stop".
    unmapped = bool(stats.get("finish_reason_unmapped"))
    return (
        normalized if isinstance(normalized, str) else None,
        native if isinstance(native, str) else None,
        unmapped,
    )


def _mark_finish_reason(
    trace: dict[str, Any], state: tuple[str | None, str | None, bool]
) -> None:
    """Stamp the finish reason of the completion whose text we are RETURNING.

    Any finish reason other than ``stop`` is a potential problem, so the caller
    must be able to see it without reading the server log. ``finish_reason`` is
    always recorded; ``native_finish_reason`` only when litellm normalized it
    away. litellm defaults UNMAPPED reasons to ``"stop"``, so a ``stop`` whose
    raw value was unrecognised is still flagged (via the LLM layer's
    ``finish_reason_unmapped``); a raw value litellm KNOWS to be a stop alias
    (``end_turn``, ``COMPLETE``, ``eos_token``, ``STOP``) stays ``ok``.

    Status precedence: ``provider_error`` (the call failed) > ``truncated``
    (``length``: we have text but it is cut off) > ``abnormal_stop`` (any other
    non-stop reason) > ``empty`` > ``ok``. ``provider_error`` is never
    overwritten; everything else is a plain assignment so it pre-empts the
    ``setdefault("ok"/"empty")`` backstop — a truncated answer that stripped to
    empty text reports ``truncated``, because the cause is known and actionable
    whereas ``empty`` would imply the model chose to say nothing.
    """
    normalized, native, unmapped = state
    if normalized is not None:
        trace["finish_reason"] = normalized
    if native is not None and native != normalized:
        trace["native_finish_reason"] = native
    # A native value that litellm DID map (``end_turn``, ``COMPLETE``,
    # ``eos_token``, ``STOP`` → ``stop``) is a clean stop under another name;
    # only an UNMAPPED one is a hidden abnormal stop.
    abnormal = unmapped or (normalized is not None and normalized != "stop")
    if not abnormal:
        return
    if trace.get("synthesis_status") == "provider_error":
        return
    trace["synthesis_status"] = "truncated" if normalized == "length" else "abnormal_stop"
_ASK_MIN_CLAIM_CONFIDENCE_DEFAULT = 0.0
# Efficient-hybrid (task-12 fix A, owner reframe 2026-07-07): the subgraph
# default is ALWAYS-BLEND — every subgraph answer context carries a budgeted
# source-block excerpt beside the ego-graph render, replacing the binary
# abstain→unbounded-dump cliff. The 7-experiment diagnosis showed the pinned
# 69% depended on a Tier-2 fallback reading 30–66k tokens UNBOUNDED, gated on
# Tier-1 abstention PHRASING (a confident-wrong Tier-1 suppressed the rescue).
# Legacy ``"on_coverage_miss"`` is kept as an explicit opt-in for A/B
# comparability — but its Tier-2 read is now bounded too (see
# _ASK_ESCALATION_BUDGET_FACTOR): no unbounded reads anywhere in the subgraph
# path.
_ASK_SOURCE_BLOCK_POLICY_DEFAULT: SourceBlockPolicy = "blend"
# Efficient-hybrid: the source-excerpt token budget for the subgraph path.
# Resolution: per-request policy (``AskRetrievalPolicy.source_block_budget_tokens``)
# > vault config (``llm.ask.source_block_budget_tokens``) > this default.
# ALWAYS an int on the subgraph path — the pre-fix ``None`` (= unbounded Tier-2
# read, median winning escalation ~50k tokens = block-dump scale) is gone; the
# block-dump arm keeps its own unbounded behaviour (explicitly untouched).
_ASK_SOURCE_BLOCK_BUDGET_DEFAULT: int = 6000
# Objective bounded escalation: a Tier-1 blend answer that ABSTAINS may retry
# once with the excerpt budget raised by at most this factor. Abstention can
# raise the budget, never remove it.
_ASK_ESCALATION_BUDGET_FACTOR = 2
# Hard guard: a rendered subgraph context is normally ~100-500 tokens. If it ever
# blows past this (a degree-cap/budget bug), truncate seeds-first rather than ship
# a megacontext that OOMs the provider. char/4 × the reference-host token factor.
_ASK_CONTEXT_TOKEN_CEILING = 100_000

# ── ADR 0011 Phase 3: coverage/abstain gate ───────────────────────────────────
# Substring patterns (case-insensitive) that mark a Tier-1 subgraph answer as an
# ABSTENTION — the LLM declined because the compact structured context did not
# carry the answerable detail. An abstaining Tier-1 answer triggers the Tier-2
# pass (re-ask over the raw blocks of the SAME hits). A Tier-2 answer that ALSO
# matches is kept verbatim (the abstention floor — never fabricate). False
# positives are harmless: they only cost one extra Tier-2 completion.
_ASK_ABSTENTION_PATTERNS: frozenset[str] = frozenset(
    {
        "not in the notes",
        "not in the provided notes",
        "not in your notes",
        "does not contain",
        "do not contain",
        "doesn't contain",
        "don't contain",
        "cannot find",
        "can't find",
        "could not find",
        "couldn't find",
        "no information",
        "not available",
        "not mentioned",
        "not specified",
        "no explicit",
        "no mention",
        "not provided",
    }
)


def _is_abstention(text: str) -> bool:
    """True when an answer declines to answer (matches an abstention pattern) or
    is empty. Case-insensitive substring match — deliberately liberal: a false
    positive only costs one extra Tier-2 completion, while a miss leaves a
    recoverable question unanswered."""
    if not text or not text.strip():
        return True
    low = text.lower()
    return any(pat in low for pat in _ASK_ABSTENTION_PATTERNS)


def _subgraph_context_thin(context: str, coverage_threshold: float) -> bool:
    """A coverage pre-gate signal: the rendered ego-graph is *thin* (too sparse to
    have grounded an answer) when it carries no relationship/claim rows — only the
    ``=== NODES ===`` index or nothing. ``coverage_threshold`` scales the floor:
    higher thresholds demand more substantive rows before trusting Tier-1. Cheap,
    pure, model-free; the post-hoc abstention check remains the PRIMARY trigger.

    A row is substantive if it is a content line (``-`` claim or ``N… -[`` rel),
    not a section header or a NODES index entry."""
    if not context or not context.strip():
        return True
    substantive = 0
    for line in context.splitlines():
        s = line.strip()
        if not s or s.startswith("==="):
            continue
        if s.startswith("- ") or "-[" in s:
            substantive += 1
    # Map the 0..1 threshold to a minimum-substantive-rows floor: 0 disables the
    # pre-gate (post-hoc abstention only), 0.4 → need ≥1 row, scaling up linearly.
    min_rows = int(coverage_threshold * 2.5 + 0.5)
    return substantive < min_rows


def _first_int(*values: int | None) -> int:
    for value in values:
        if value is not None:
            return int(value)
    raise ValueError("expected at least one integer fallback")


def _first_float(*values: float | None) -> float:
    for value in values:
        if value is not None:
            return float(value)
    raise ValueError("expected at least one float fallback")


def _effective_source_policy(
    cfg: "VaultConfig",
    policy: AskRetrievalPolicy | None,
    enable_subgraph: bool,
) -> SourceBlockPolicy:
    if policy and policy.source_block_policy is not None:
        return policy.source_block_policy
    if enable_subgraph:
        return _ASK_SOURCE_BLOCK_POLICY_DEFAULT
    return "always"


def _effective_source_budget(cfg: "VaultConfig", policy: AskRetrievalPolicy | None) -> int:
    """Efficient-hybrid: the source-excerpt token budget for the subgraph path.

    Resolution: per-request policy > ``llm.ask.source_block_budget_tokens`` >
    ``_ASK_SOURCE_BLOCK_BUDGET_DEFAULT``. Always returns an int — the subgraph
    path can never read sources unbounded (the pre-fix ``None`` cliff)."""
    if policy is not None and policy.source_block_budget_tokens is not None:
        return int(policy.source_block_budget_tokens)
    step_budget = cfg.llm.ask.source_block_budget_tokens
    if step_budget is not None:
        return int(step_budget)
    return _ASK_SOURCE_BLOCK_BUDGET_DEFAULT


def _effective_seed_diversity(
    cfg: "VaultConfig",
    policy: AskRetrievalPolicy | None,
    *,
    enable_subgraph: bool,
) -> bool:
    """Fix B (task-12) gate resolution: per-request policy > ``llm.ask`` step
    config > default-follows-``enable_subgraph``. The diversified seed cut rides
    the subgraph/explore path only; the default block-dump ask and plain recall
    stay byte-identical unless explicitly configured."""
    if policy is not None and policy.seed_diversity is not None:
        return bool(policy.seed_diversity)
    step = cfg.llm.ask
    if step.seed_diversity is not None:
        return bool(step.seed_diversity)
    return enable_subgraph


def _effective_seed_quotas(
    cfg: "VaultConfig", policy: AskRetrievalPolicy | None
) -> dict[str, int | None]:
    """Quota override resolution (policy > ``llm.ask`` > None → query.py code
    defaults), shaped as kwargs for ``vault.query`` / ``vault.query_seeds``."""
    step = cfg.llm.ask

    def _pick(policy_value: int | None, step_value: int | None) -> int | None:
        return policy_value if policy_value is not None else step_value

    return {
        "seed_subject_cap": _pick(
            policy.seed_subject_cap if policy else None, step.seed_subject_cap
        ),
        "seed_entity_min": _pick(policy.seed_entity_min if policy else None, step.seed_entity_min),
        "seed_rel_min": _pick(policy.seed_rel_min if policy else None, step.seed_rel_min),
        "seed_scalar_max": _pick(policy.seed_scalar_max if policy else None, step.seed_scalar_max),
    }


def _estimate_context_tokens(text: str) -> int:
    return int((len(text) / 4) * 1.117)


def _source_for_ledger(source: str | PathLike[str]) -> str:
    try:
        return str(Path(source).expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        return str(source)


def _diversified_hit_order(hits: list[QueryHit]) -> list[QueryHit]:
    """T2c (Fix B, task-12): source-assembly hygiene for the Tier-2 fallback.

    Two pure, deterministic transforms so one flooded document cannot consume
    the whole source budget:

    1. **Anchor dedup** — hits sharing the exact provenance byte anchor
       ``(path, byte_start, byte_end)`` decode to the same snippet; keep the
       first (best-ranked) only. Anchorless hits are never merged.
    2. **Path round-robin** — interleave hits across distinct source paths
       (paths ordered by first appearance, hits within a path keep rank order)
       so the budget trim samples every source instead of exhausting on the
       first document's flood.
    """
    groups: dict[str, list[QueryHit]] = {}
    path_order: list[str] = []
    seen_anchors: set[tuple[str, int, int]] = set()
    for hit in hits:
        prov = hit.provenance
        path = str(prov.path or "") if prov else ""
        if prov and path and prov.byte_end > prov.byte_start:
            anchor = (path, prov.byte_start, prov.byte_end)
            if anchor in seen_anchors:
                continue
            seen_anchors.add(anchor)
        if path not in groups:
            groups[path] = []
            path_order.append(path)
        groups[path].append(hit)
    ordered: list[QueryHit] = []
    depth = 0
    while True:
        advanced = False
        for path in path_order:
            bucket = groups[path]
            if depth < len(bucket):
                ordered.append(bucket[depth])
                advanced = True
        if not advanced:
            return ordered
        depth += 1


def _query_term_ranked_snippets(
    snippets: list[str], query_terms: "frozenset[str] | set[str]"
) -> list[str]:
    """Efficient-hybrid: stable re-rank of candidate source snippets by
    IDF-weighted query-term coverage, so a small excerpt budget lands on the
    blocks most likely to carry the gold bytes instead of flood-ranked order
    (the diagnosis's pit-104: 14 blocks fetched, none with the answer).

    IDF is computed LOCALLY over the snippet pool with the same formula as
    ``_answer_ranked`` (``query.py``) — rare discriminative terms dominate; the
    common topic term every block shares is discounted. The sort is STABLE:
    zero-coverage snippets keep their incoming order, so this composes with the
    T2c anchor-dedup + path round-robin (ties preserve source diversity).
    Pure, deterministic, zero LLM tokens."""
    if not query_terms or len(snippets) <= 1:
        return snippets
    toks = [set(re.findall(r"[a-z0-9]+", snippet.lower())) for snippet in snippets]
    n = len(snippets)
    df = {t: sum(1 for tok in toks if t in tok) for t in query_terms}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in query_terms}
    scores = [sum(idf[t] for t in query_terms if t in toks[i]) for i in range(n)]
    order = sorted(range(n), key=lambda i: (-scores[i], i))
    return [snippets[i] for i in order]


def _budgeted_source_lines(
    snippets: "Iterator[str] | list[str]", max_token_budget: int | None
) -> str:
    """The budget-trim assembly shared by both selection orders. Empty snippets
    are skipped; the last line that would overflow is trimmed (ellipsis-aware)
    to the remaining budget. The unbudgeted path is byte-identical to the
    pre-refactor inline loop; the budgeted path STRICTLY honors the knob —
    ``_estimate_context_tokens(result + "\\n") <= max_token_budget`` always
    (float char accounting, no per-line int-floor drift, the ``...`` marker
    counted inside the remaining budget). The knob is the efficient-hybrid
    acceptance property: the excerpt may never exceed it."""
    lines: list[str] = []
    used = 0.0
    for snippet in snippets:
        if not snippet:
            continue
        line = f"- {snippet}"
        cost = (len(line) + 1) / 4 * 1.117
        if max_token_budget is not None and used + cost > max_token_budget:
            remaining = max_token_budget - used
            if remaining <= 0:
                break
            # chars affordable within the remaining budget, reserving the
            # ellipsis (3 chars) + the joining newline (1 char).
            char_budget = int((remaining / 1.117) * 4) - 4
            if char_budget > 12:
                lines.append(line[:char_budget].rstrip() + "...")
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


def _reattach_excerpt_markers(
    ordered: "list[str]",
    marked: "list[tuple[str, str, bool]]",
) -> "list[str]":
    """Prefix each ranked snippet with its EXCERPT marker.

    ``_query_term_ranked_snippets`` returns reordered snippet TEXT (it has no
    place to carry side data), so markers are matched back by value in FIFO
    order. That re-pairing is exact: the re-rank is STABLE, so equal-text
    snippets keep their relative order and consuming their markers in the same
    order restores the original pairing.

    Note the budget trim downstream may truncate a block AFTER its marker, so a
    marker can slightly over-claim what survived; the trim appends ``...`` which
    already signals that. The marker's job is coverage of the SOURCE, and that
    claim stays true."""
    from collections import defaultdict, deque

    pending: "defaultdict[str, deque[str]]" = defaultdict(deque)
    for marker, text, _from_source in marked:
        if text:
            pending[text].append(marker)
    out: list[str] = []
    for text in ordered:
        queue = pending.get(text)
        marker = queue.popleft() if queue else ""
        out.append(f"{marker}\n{text}" if marker else text)
    return out


def _source_context_for_hits(
    hits: list[QueryHit],
    *,
    vault_root: "str | PathLike[str] | None" = None,
    max_token_budget: int | None = None,
    diversify: bool = False,
    query_terms: "frozenset[str] | set[str] | None" = None,
    source_reads: "list[int] | None" = None,
) -> str:
    # ``diversify`` (Fix B T2c) is enabled ONLY on the subgraph source branches;
    # the default False keeps every other caller (block-dump path) byte-identical.
    # ``query_terms`` (efficient-hybrid) additionally re-ranks the snippets by
    # IDF-weighted query-term coverage BEFORE the budget trim; ``None`` keeps
    # the lazy legacy order byte-identical (block-dump regression pin).
    #
    # ``source_reads`` is an optional out-param (a one-slot list the caller
    # supplies) reporting how many of ``hits`` contributed REAL source bytes
    # (as opposed to a name fallback, or nothing at all for a rotted anchor —
    # see ``_hit_context_snippet``). Added rather than changing the return
    # type: this function's plain-``str`` contract is pinned by several
    # existing callers/tests. ``None`` (the default) skips the bookkeeping
    # entirely — behaviour for existing callers is unchanged.
    if diversify:
        hits = _diversified_hit_order(hits)
    marked = [_hit_excerpt_marker(hit, vault_root=vault_root) for hit in hits]
    snippet_pairs = [(snippet, from_source) for _, snippet, from_source in marked]
    # EXCERPT MARKERS: attached AFTER ranking, never before. ``_query_term_ranked_snippets``
    # scores on the snippet TEXT; letting marker tokens (path segments, byte digits)
    # into that input would reorder source blocks, i.e. change retrieval behaviour.
    if query_terms:
        snippets = [snippet for snippet, _ in snippet_pairs if snippet]
        ordered = _query_term_ranked_snippets(snippets, query_terms)
        result = _budgeted_source_lines(
            _reattach_excerpt_markers(ordered, marked), max_token_budget
        )
    else:
        result = _budgeted_source_lines(
            _reattach_excerpt_markers([s for s, _ in snippet_pairs], marked),
            max_token_budget,
        )
    if source_reads is not None:
        # Defect D fix: count REAL source reads only if they survived the
        # budget trim. Counting over all snippet_pairs before the trim let a
        # tiny budget wipe the assembled context to "" while callers still
        # reported ``source_blocks_used=True`` from a non-empty read count —
        # an honest-grounding claim about context that was never sent to the
        # LLM. An empty final context always means zero USED reads, regardless
        # of how many snippets contributed real bytes before the trim.
        source_reads.append(
            sum(1 for _, from_source in snippet_pairs if from_source) if result.strip() else 0
        )
    return result


def _combine_contexts(subgraph_context: str, source_context: str) -> str:
    parts: list[str] = []
    if subgraph_context.strip():
        parts.append("=== SUBGRAPH ===\n" + subgraph_context)
    if source_context.strip():
        parts.append("=== SOURCE BLOCKS ===\n" + source_context)
    return "\n\n".join(parts)


def _trace_for_policy(
    cfg: "VaultConfig",
    policy: AskRetrievalPolicy | None,
    *,
    mode: str,
    seed_k: int,
    source_policy: SourceBlockPolicy,
    context: str,
    source_budget: int | None = None,
) -> dict[str, Any]:
    ask = cfg.llm.ask
    return {
        "mode": mode,
        "path": "subgraph",
        "seed_k": seed_k,
        "enable_subgraph": mode == "subgraph",
        "hops": _first_int(policy.hops if policy else None, ask.hops, _ASK_HOPS_DEFAULT),
        "max_degree_per_seed": _first_int(
            policy.max_degree_per_seed if policy else None,
            ask.max_degree_per_seed,
            _ASK_DEGREE_CAP_DEFAULT,
        ),
        "neighbour_budget_tokens": _first_int(
            policy.neighbour_budget_tokens if policy else None,
            ask.neighbour_budget_tokens,
            _ASK_NEIGHBOUR_BUDGET_DEFAULT,
        ),
        "coverage_threshold": _first_float(
            policy.coverage_threshold if policy else None,
            ask.coverage_threshold,
            _ASK_COVERAGE_DEFAULT,
        ),
        "min_claim_confidence": _first_float(
            policy.min_claim_confidence if policy else None,
            ask.min_claim_confidence,
            _ASK_MIN_CLAIM_CONFIDENCE_DEFAULT,
        ),
        "max_nodes": policy.max_nodes if policy else None,
        "max_relationships": policy.max_relationships if policy else None,
        "max_claims": policy.max_claims if policy else None,
        "relationship_types": list(policy.relationship_types or ()) if policy else [],
        "source_block_policy": source_policy,
        # Efficient-hybrid: the EFFECTIVE budget (policy > config > code default),
        # never the raw policy field — the A/B computes per-arm mean context
        # tokens from this + context_tokens_estimate + path.
        "source_block_budget_tokens": (
            source_budget
            if source_budget is not None
            else (policy.source_block_budget_tokens if policy else None)
        ),
        "source_blocks_used": False,
        "context_tokens_estimate": _estimate_context_tokens(context),
    }


def _is_within_root(path: str, vault_root: "str | PathLike[str] | None") -> bool:
    """True when ``path`` resolves to a location under ``vault_root`` (or when no
    root is supplied — the guard is dormant for the default OFF path). Pure,
    model-free; the load-bearing security check before any Tier-2 byte read.
    A traversal path (``../../etc/passwd``) outside the vault resolves to ``False``."""
    if vault_root is None:
        return True
    if not path:
        return False
    try:
        root = Path(vault_root).resolve(strict=False)
        Path(path).resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _source_is_ingestable_path(
    source: str | PathLike[str],
    vault_root: "str | PathLike[str]",
    watch_roots: "list[str]",
) -> bool:
    """True when ``source`` names an EXISTING file that resolves under the vault
    root or one of the configured folder-watch ``watch_roots``.

    Constrains ``remember(path)`` to trusted trees: the vault (the trust root)
    plus any watched folders the operator opted into. A traversal path that
    resolves to a real file OUTSIDE all of these (``../../etc/passwd``) returns
    False → the caller rejects it. Sources that are not existing files (raw text,
    non-existent paths, URLs) are left to the downstream ingest reader, which
    fails closed on a missing file; this guard only closes the *escape* case
    where an out-of-tree path DOES resolve to readable bytes."""
    try:
        resolved = Path(source).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return False
    if not resolved.is_file():
        # Not a real on-disk file here — no arbitrary-read escape to guard. The
        # ingest reader will fail closed if it turns out to need a missing file.
        return True
    roots = [vault_root, *watch_roots]
    return any(_is_within_root(str(resolved), root) for root in roots)


def _is_local_provider(provider: "LLMProvider") -> bool:
    """A provider is local if it has no hosted ``api_base`` (StubLLM) or its
    ``api_base`` host is loopback. Pure attribute check — never touches the wire."""
    api_base = getattr(provider, "api_base", None)
    if not api_base:
        return True
    host = urlparse(str(api_base)).hostname or ""
    return host in _LOCAL_HOSTS


# ── the companion ───────────────────────────────────────────────────────────--
def _guard_live_graph_write(method):  # type: ignore[no-untyped-def]
    """Audit one public Companion write; rebuild staging uses a no-op guard."""

    @wraps(method)
    def guarded(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        from okto_neuron.consolidate.ledger import CandidateLedger
        from okto_neuron.store.integrity_state import IntegrityFenceError

        guard_factory = getattr(self._vault, "_integrity_write_guard", None)
        guard = guard_factory() if callable(guard_factory) else nullcontext()
        ledger = CandidateLedger(Path(self._vault.path) / ".marginalia")
        result: Any = None
        try:
            with guard, ledger.semantic_writer_lease():
                result = method(self, *args, **kwargs)
        except IntegrityFenceError as exc:
            integrity = {
                "status": exc.state.status.value,
                "audit_id": exc.state.audit_id,
                "graph_generation": exc.state.graph_generation,
            }
            if isinstance(result, RememberResult) and result.ledger_run_id:
                ledger.record_integrity_outcome(
                    result.ledger_run_id,
                    document_id=result.document_id,
                    integrity=integrity,
                    quality="integrity_failed",
                )
            raise
        except LLMUnavailableError as exc:
            integrity = _current_integrity_outcome(self._vault)
            exc.outcome["integrity"] = integrity
            if exc.ledger_run_id and exc.document_id:
                ledger.record_integrity_outcome(
                    exc.ledger_run_id,
                    document_id=exc.document_id,
                    integrity=integrity,
                )
            raise
        if not isinstance(result, RememberResult):
            return result

        integrity = _current_integrity_outcome(self._vault)
        outcome = dict(result.outcome)
        outcome["integrity"] = integrity
        if result.ledger_run_id:
            ledger.record_integrity_outcome(
                result.ledger_run_id,
                document_id=result.document_id,
                integrity=integrity,
            )
        return result.model_copy(update={"outcome": outcome})

    return guarded


def _current_integrity_outcome(vault: Any) -> dict[str, Any]:
    handle = getattr(vault.store, "_graph_handle", None)
    if handle is None:
        return {
            "status": "not_applicable",
            "audit_id": None,
            "graph_generation": None,
        }

    from okto_neuron.store.integrity_state import load_integrity_state

    generation = vault.store.generation() or None
    state = load_integrity_state(
        vault.path,
        expected_graph_generation=generation,
    )
    return {
        "status": state.status.value,
        "audit_id": state.audit_id,
        "graph_generation": state.graph_generation,
    }


class Companion:
    """Autonomous memory companion over a :class:`~okto_neuron.vault.Vault`.

    One :meth:`remember` call runs the full autonomous loop: ingest the source
    to anchored Blocks (the deterministic path), propose candidates from the
    document text via an :class:`~okto_neuron.extract.Extractor`, open a
    consolidation session, resolve each candidate against the graph (talk-back),
    run the confidence gate (auto-commit high confidence, queue the rest), and
    return a populated :class:`RememberResult`. ``recall`` proxies retrieval;
    ``ask`` synthesises an answer over retrieved hits via the LLM (gracefully
    degrading to empty text when the provider is down). The review queue is the
    Phase E queue persisted under the vault's ``.marginalia/``.

    Providers, extractor and embedder are injectable for tests; defaults are the
    real local oMLX provider, an :class:`LLMExtractor`, and the configured
    embedder.
    """

    def __init__(
        self,
        vault: Vault,
        *,
        provider: "LLMProvider | None" = None,
        extractor: "Extractor | None" = None,
        embedder: "EmbeddingProvider | None" = None,
        materialization_scope: str | None = None,
    ) -> None:
        if materialization_scope is not None and not materialization_scope.strip():
            raise ValueError("materialization scope cannot be empty")
        self._vault = vault
        self._provider = provider
        self._extractor = extractor
        self._embedder = embedder
        self._materialization_scope = materialization_scope

    # ── lazy real dependencies (defaults, overridable via injection) ────────────
    def _vault_config(self) -> "VaultConfig":
        """Load this vault's typed config.

        A *missing* ``okto-neuron.yaml`` is fine — fall back to defaults that mirror
        the historical hard-coded constants, so a configless vault is unchanged.
        A *malformed* config (present but unparseable / wrong shape) must NOT be
        silently swallowed: a typo in `llm.model` would otherwise route every call
        to a wrong/absent model. Per ADR 0002 D8 and the fail-loud directive, let
        ``ConfigParseError`` propagate."""
        from okto_neuron.config import VaultConfig
        from okto_neuron.errors import ConfigNotFound

        try:
            return VaultConfig.load(self._vault.path)
        except (ConfigNotFound, FileNotFoundError):
            return VaultConfig()

    def _get_provider(self, step: str = "extraction") -> "LLMProvider":
        """Return the injected provider for any step, or build a fresh per-step
        provider from the resolved config. Injected providers win for all steps
        (enables deterministic test injection without per-step config).

        Always wrapped in ``_StepLabelledProvider`` — including the injected
        fast path, deliberately, so injected test providers get step labels
        too — so every completion's log line can be correlated back to the
        pipeline step that made the call."""
        config_step = "curator" if step == "type_adjudication" else step
        if self._provider is not None:
            return _StepLabelledProvider(self._provider, step)
        from okto_neuron.llm import get_provider

        return _StepLabelledProvider(
            get_provider(self._vault_config().llm.resolved(config_step)),  # type: ignore[arg-type]
            step,
        )

    def _semantic_fingerprints(
        self,
        config: "VaultConfig | None" = None,
        *,
        ingest_config: "IngestConfig | None" = None,
    ) -> "SemanticFingerprints":
        """Fingerprint the effective semantic policy at the run-start boundary."""

        from okto_neuron.semantic_fingerprint import semantic_fingerprints

        from . import _incremental

        cfg = config or self._vault_config()
        ingest = ingest_config or cfg.ingest
        return semantic_fingerprints(
            cfg,
            Path(self._vault.path),
            ingest_config=ingest,
            effective_incremental=_incremental.incremental_enabled(ingest),
            effective_subchunk=_incremental.subchunk_enabled(ingest),
            runtime_provider=self._provider,
            runtime_embedder=self._embedder,
        )

    def _fingerprinted_identity_snapshot(
        self,
        config: "VaultConfig",
        *,
        ingest_config: "IngestConfig",
    ) -> tuple["SemanticFingerprints", "IdentityDecisionIndex"]:
        """Load one immutable decision view consistent with the run fingerprint."""

        from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
        from okto_neuron.reconcile.decisions import (
            IdentityDecisionIndex,
            IdentityDecisionStoreError,
        )

        authority_dir = Path(self._vault.path) / ".marginalia" / AUTHORITY_DIRNAME
        for _ in range(3):
            before = self._semantic_fingerprints(config, ingest_config=ingest_config)
            try:
                decisions = IdentityDecisionIndex(authority_dir)
            except IdentityDecisionStoreError as exc:
                raise IngestError(
                    vault_path=self._vault.path,
                    message="identity decisions are invalid; ingest did not start",
                    cause=exc,
                ) from exc
            after = self._semantic_fingerprints(config, ingest_config=ingest_config)
            if before.semantic_policy_fingerprint == after.semantic_policy_fingerprint:
                return before, decisions
        raise IngestError(
            vault_path=self._vault.path,
            message="semantic decision files changed while ingest was starting; retry",
        )

    def _get_extractor(self) -> "Extractor":
        if self._extractor is None:
            self._extractor = self._build_extractor()
        return self._extractor

    def _build_extractor(self, provider: "LLMProvider | None" = None) -> "Extractor":
        from okto_neuron.extract import LLMExtractor
        from okto_neuron.llm import sampler_overrides

        cfg = self._vault_config()
        resolved = cfg.llm.resolved("extraction")
        # Mode + caps are StepLLM-only fields (no LLMDefaults equivalent), so
        # they are read STEP-DIRECT off cfg.llm.extraction, not via resolved().
        ext = cfg.llm.extraction
        # Effective extraction default is "auto" (escalate-on-truncation): when
        # the vault has not pinned an explicit mode, recover facts from blocks the
        # model truncated. "baseline"/"enumerate" remain selectable when set.
        extra: dict[str, object] = {"mode": ext.mode or "auto"}
        # E6 multi-sample union: k independent draws per block, candidate sets
        # UNIONED before dedup/curation. None/1 → single-pass (byte-identical).
        if ext.samples is not None:
            extra["samples"] = ext.samples
        if ext.enumerate_max_handles is not None:
            extra["enumerate_max_handles"] = ext.enumerate_max_handles
        if ext.enumerate_describe_batch is not None:
            extra["enumerate_describe_batch"] = ext.enumerate_describe_batch
        if ext.enumerate_max_describe_batches is not None:
            extra["enumerate_max_describe_batches"] = ext.enumerate_max_describe_batches
        return LLMExtractor(
            provider or self._get_provider("extraction"),
            # max_tokens/temperature come from sampler_overrides(): an unset
            # config value must NOT override the class default (see helper).
            **sampler_overrides(resolved),
            top_p=resolved.top_p,
            top_k=resolved.top_k,
            min_p=resolved.min_p,
            presence_penalty=resolved.presence_penalty,
            enable_thinking=resolved.enable_thinking,
            # Step-level system_prompt only — resolved() would merge the
            # defaults.system_prompt down and could clobber the extraction
            # JSON prompt with a generic per-vault default.
            system_prompt=cfg.llm.extraction.system_prompt,
            packs=cfg.packs,
            **extra,  # type: ignore[arg-type]
        )

    @_guard_live_graph_write
    def remember(
        self,
        source: str | PathLike[str],
        *,
        sensitivity: Sensitivity = "default",
        on_progress: "ProgressCallback | None" = None,
        on_event: "IngestEventCallback | None" = None,
        should_cancel: "Callable[[], bool] | None" = None,
    ) -> RememberResult:
        """Ingest ``source``, as ONE trace covering every call it makes.

        The work itself is :meth:`_remember_document`; this wraps it in a
        parent span so extraction, curation, type adjudication and the dedup
        judge appear as children of the document rather than as dozens of
        unrelated root traces. One document is the right unit: it is what the
        HTTP handler offloads to a single thread, and everything below it is a
        loop that fans out.

        A no-op wrapper when telemetry is off: same call, same result, no
        span, nothing imported.
        """
        from okto_neuron.llm import trace_parent

        with trace_parent(
            "ingest",
            span_type="CHAIN",
            inputs={"source": str(source)},
            # On the TRACE, because a child span cannot carry tags and this is
            # what the trace list filters on.
            tags={"marginalia.step": "ingest"},
        ) as span:
            result = self._remember_document(
                source,
                sensitivity=sensitivity,
                on_progress=on_progress,
                on_event=on_event,
                should_cancel=should_cancel,
            )
            span.set_attribute("marginalia.document_id", result.document_id)
            span.set_attribute("marginalia.committed", result.committed)
            span.set_attribute("marginalia.queued", result.queued)
            span.set_attribute("marginalia.claims_minted", result.claims_minted)
            span.set_attribute("marginalia.blocks_total", result.blocks_total)
            # Retried judge / curator / relation-curator calls, by step. Each
            # attempt is also its own child call span (failed, then clean).
            # Set only when a retry happened, like the outcome field it copies
            # and the ledger rows, so "absent" is the one way to say "none".
            provider_retries = result.outcome.get("provider_retries")
            if provider_retries:
                span.set_attribute("marginalia.provider_retries", dict(provider_retries))
            span.set_outputs(
                {
                    "document_id": result.document_id,
                    "committed": result.committed,
                    "queued": result.queued,
                    "claims_minted": result.claims_minted,
                }
            )
            # Same rule the call spans follow: a provider error during ingest
            # means this document did not get the graph it should have, so the
            # trace says so instead of showing a green document full of
            # red calls.
            if result.provider_error:
                span.set_attribute("marginalia.provider_error", result.provider_error)
                span.set_status("ERROR")
            return result

    def _remember_document(
        self,
        source: str | PathLike[str],
        *,
        sensitivity: Sensitivity = "default",
        on_progress: "ProgressCallback | None" = None,
        on_event: "IngestEventCallback | None" = None,
        should_cancel: "Callable[[], bool] | None" = None,
    ) -> RememberResult:
        """Ingest ``source`` and curate it into the graph autonomously.

        Ingests to anchored Blocks, proposes candidates from the document text,
        resolves each against the graph, and gates them: high-confidence
        candidates auto-commit, the rest are parked on the review queue. Returns
        committed/queued counts and one :class:`CandidateOutcome` per node
        candidate. ``sensitivity="local_only"`` forces a local provider and
        refuses to run against a hosted ``base_url``.

        ``on_progress`` is an OPTIONAL keyword-only callback invoked at each
        stage boundary and once per block in the per-block extraction loop, as
        ``on_progress(stage, blocks_done, blocks_total)``. Stages advance
        monotonically: ``parsing → extracting → embedding → dedup → committing``.
        ``should_cancel`` is an optional cooperative stop predicate. It runs at
        safe pre-commit boundaries and before and after each built-in LLM call.
        Once the graph commit starts, core writes and ledger finalization finish;
        remaining best-effort correction calls still wind down cooperatively.
        When callbacks are omitted, behaviour is exactly as before.
        """
        # Local import, matching this module's lazy-llm convention: used at the
        # two fan-out submit sites below (extraction, type adjudication) to
        # carry the ingest span into pool threads.
        from okto_neuron.llm import bind_parent

        # ADR-observability: the live 7-hour ingest that motivated this had no
        # way to tell, from the serve log or the ingest-history events[], when
        # a stage actually CHANGED — items sat at "dedup 100%" with frozen
        # counts for the whole judge tail because nothing marked the
        # parsing→extracting→embedding→dedup→committing transitions. _emit is
        # called once per stage boundary AND many times per block within a
        # stage (the per-block extraction loop); only fire the stage event +
        # log line on an actual transition, not on every per-block call.
        _remember_started = time.monotonic()
        _last_stage_emitted: list[str | None] = [None]
        # ADR 0036: tracing callbacks can now arrive from extraction workers.
        # Serialize the user callback because the server mutates and persists
        # one ingest-history item from inside it.
        _event_lock = threading.Lock()

        def _check_cancelled() -> None:
            if should_cancel is not None and should_cancel():
                raise RememberCancelled()

        def _emit(stage: str, blocks_done: int, blocks_total: int) -> None:
            _check_cancelled()
            if stage != _last_stage_emitted[0]:
                _last_stage_emitted[0] = stage
                elapsed = time.monotonic() - _remember_started
                _LOG.info(
                    "remember stage=%s blocks=%d/%d elapsed=%.1fs",
                    stage,
                    blocks_done,
                    blocks_total,
                    elapsed,
                )
                _event(
                    "stage",
                    f"Stage → {stage}",
                    {
                        "stage": stage,
                        "blocks_done": blocks_done,
                        "blocks_total": blocks_total,
                        "elapsed_s": round(elapsed, 1),
                    },
                )
            if on_progress is not None:
                on_progress(stage, blocks_done, blocks_total)

        # Sub-stage keep-alive. The dedup and curation phases have no block
        # population to count, so these ticks report an item ordinal with an
        # EXPLICITLY UNDECLARED total (0 — ADR 0039 T9's "no denominator
        # exists yet" case), never a blocks_done/blocks_total pair: the server
        # queue must not fold an ordinal of 212 into blocks_done=212/3.
        # The counter is monotonic PER STAGE across phases so the MCP bridge's
        # (stage, done) dedupe can never coalesce the node phase's last tick
        # with the relation phase's first one.
        _substage_counts: dict[str, int] = {}
        _substage_last_tick: dict[str, tuple[int, float]] = {}

        def _emit_substage(stage: str) -> None:
            done = _substage_counts.get(stage, 0) + 1
            _substage_counts[stage] = done
            if on_progress is None:
                return
            last_done, last_at = _substage_last_tick.get(stage, (0, 0.0))
            now = time.monotonic()
            if (
                last_at
                and done - last_done < SUBSTAGE_PROGRESS_EVERY
                and now - last_at < SUBSTAGE_PROGRESS_INTERVAL_S
            ):
                return
            _substage_last_tick[stage] = (done, now)
            try:
                on_progress(stage, done, 0)
            except Exception:  # noqa: BLE001 - telemetry must never fail ingest
                _LOG.debug("on_progress sub-stage tick failed", exc_info=True)

        def _event(kind: str, summary: str, payload: dict[str, Any] | None = None) -> None:
            if on_event is not None:
                with _event_lock:
                    on_event({"kind": kind, "summary": summary, "payload": payload or {}})

        from okto_neuron.construction_cost import ConstructionCostTracker

        construction_cost = ConstructionCostTracker()
        # ADR 0039 D5: transient-provider retries taken by the non-extraction
        # steps (judge, curator, relation_curator, ...), by step. Extraction's
        # own retries live in the unit journal. Written from fan-out workers.
        provider_retry_counts: dict[str, int] = {}
        provider_retry_lock = threading.Lock()

        def _count_provider_retry(step: str, _record: Mapping[str, Any]) -> None:
            with provider_retry_lock:
                provider_retry_counts[step] = provider_retry_counts.get(step, 0) + 1

        def _tracked_provider(
            provider: "LLMProvider",
            *,
            label: str,
            context: Callable[[], dict[str, Any]],
        ) -> _TracingLLMProvider:
            return _TracingLLMProvider(
                provider,
                _event,
                context,
                should_cancel=should_cancel,
                label=label,
                trace_events=on_event is not None,
                on_completion=construction_cost.record_completion,
                on_retry=_count_provider_retry,
            )

        # SECURITY: constrain a path-shaped source to the vault root or a
        # configured folder-watch root BEFORE any read/ingest. Defence in depth
        # on top of Vault._ensure_under_vault: that backstop is bypassable when a
        # vault opts into compat_allow_external_sources, and it does not know the
        # watch roots. This guard is purely additive — it only ever REJECTS an
        # out-of-tree readable file, never widens what _ensure_under_vault permits.
        watch_roots = list(self._vault_config().folder_watch.roots)
        if not _source_is_ingestable_path(source, self._vault.path, watch_roots):
            raise SourceOutsideVaultError(
                f"refusing to remember source outside the vault and watch roots: {source!r}"
            )

        _emit("parsing", 0, 0)

        from typing import cast

        from okto_neuron.consolidate._dedup import EdgeEndpointGuard
        from okto_neuron.consolidate.gate import GateConfig, decide
        from okto_neuron.consolidate.ledger import edge_candidate_id
        from okto_neuron.consolidate.prefilter import (
            collapse_near_dup_literals,
            count_title_mentions,
            demoted_predicate_reason,
            is_established_re_mention,
            is_trivial_node,
        )
        from okto_neuron.consolidate.relation_gate import (
            EndpointDecision,
            LiteralObject,
            PinnedPredicateAdmission,
            RelationGateInput,
            SourceGrounding,
            TopologyObject,
            decide_relation,
        )
        from okto_neuron.consolidate.review_queue import PinnedRelationProposal
        from okto_neuron.core.schema import Edge, Provenance
        from okto_neuron.curator import (
            RELATION_CURATOR_EVIDENCE_VERSION,
            CuratorVerdict,
            LLMCandidateCurator,
            LLMRelationCurator,
            _edge_source_excerpt,
            _source_excerpt,
            candidate_curator_system,
            normalize_predicate,
        )
        from okto_neuron.curator_batch import (
            BatchCurationItem,
            iter_batched_curation,
        )
        from okto_neuron.extract import ExtractionResult
        from okto_neuron.llm import LLMProviderError, sampler_overrides
        from okto_neuron.predicates import (
            LLMPredicateResolver,
            PredicateAdmissionDecision,
            PredicateAliasIndex,
            PredicateAliasRecord,
            PredicateDecisionProvenance,
            PredicateEvidenceSample,
            PredicateProposal,
            PredicateRecord,
            PredicateRegistry,
            PredicateResolution,
            PredicateResolutionRequest,
            PredicateTypeSignature,
            admit_predicate,
            builtin_predicate_aliases,
            folds_onto_incumbent,
            render_registry_block,
            to_alias_record,
        )
        from okto_neuron.predicates.resolve import FOLD_CONFIDENCE_GATE
        from okto_neuron.resolve import (
            LLMMergeJudge,
            ResolveOutcome,
            judge_against_store,
            judge_within_batch,
            reconcile_against_store,
            resolve,
        )

        from . import _incremental

        store = cast("GraphStore", self._vault.store)

        # ADR 0039 apply-resume lane: a sealed plan outranks curation resume.
        # It is replayed from pinned operations without extraction, LLM calls,
        # admission, relation gating, or a fresh policy fingerprint.
        resume_ledger = self._candidate_ledger()
        try:
            pending_plans = resume_ledger.unreceipted_commit_plans()
        except ValueError as exc:
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="candidate ledger cannot safely resume a sealed plan",
                cause=exc,
            ) from exc
        resolved_source = _resolved_source_path(source)
        matching_plans = [
            plan
            for plan in pending_plans
            if str(
                (plan.context.get("source_binding") or {}).get("resolved_source_path")
                if isinstance(plan.context.get("source_binding"), dict)
                else _resolved_source_path(str(plan.context.get("source") or ""))
            )
            == resolved_source
        ]
        if len(pending_plans) > 1:
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="multiple unreceipted semantic plans require operator recovery",
            )
        if len(matching_plans) > 1:
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="multiple unreceipted plans target the same document",
            )
        if pending_plans and not matching_plans:
            owner = pending_plans[0].context
            owner_description = (
                f"manual review {owner.get('candidate_id')}"
                if owner.get("intent") == "manual_review_resolution"
                else str(owner.get("source") or owner.get("document_id") or "unknown")
            )
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message=(
                    "a different sealed semantic plan must be resumed before new "
                    f"ingest work: {owner_description}"
                ),
            )
        if matching_plans:
            plan = matching_plans[0]
            resume_document_id = str(plan.context.get("document_id") or "")
            if not resume_document_id:
                raise IngestError(
                    source,
                    vault_path=self._vault.path,
                    message="sealed semantic plan has no document identity",
                )
            binding_matches, binding_evidence = _source_binding_evidence(
                store,
                source,
                plan.context.get("source_binding"),
            )
            existing_receipts = resume_ledger.operation_receipts(plan)
            if not binding_matches and not existing_receipts:
                resume_ledger.record_plan_abandoned(
                    plan.run_id,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    reason=str(binding_evidence["reason"]),
                    evidence=binding_evidence,
                )
                resume_ledger.finish_run(
                    plan.run_id,
                    state="abandoned",
                    summary={
                        "reason": "sealed_plan_source_binding_changed",
                        "source_binding": binding_evidence,
                    },
                )
                _event(
                    "resume_plan_abandoned",
                    "Source changed before semantic apply; starting a fresh ingest",
                    binding_evidence,
                )
                matching_plans = []
            elif not binding_matches:
                _event(
                    "resume_then_refresh",
                    "Completing a partially applied sealed plan before fresh ingest",
                    binding_evidence,
                )
            if matching_plans:
                must_refresh_source = not binding_matches
                try:
                    resumed = _apply_sealed_semantic_plan(
                        plan,
                        store=store,
                        ledger=resume_ledger,
                        registry=PredicateRegistry(self._vault.path),
                        review_queue=self._review_queue(),
                    )
                except (TypeError, ValueError, RuntimeError) as exc:
                    raise IngestError(
                        source,
                        vault_path=self._vault.path,
                        message="sealed semantic plan could not satisfy its postconditions",
                        cause=exc,
                    ) from exc
                resume_summary = {
                    "resumed_commit_plan": plan.plan_id,
                    "committed": len(resumed["committed_node_ids"]),
                    "queued": sum(
                        1
                        for operation in plan.operations
                        if operation["operation"] == "queue_review"
                        and operation.get("candidate_kind") == "node"
                    ),
                    "dead_lettered": sum(
                        1
                        for operation in plan.operations
                        if operation["operation"] == "dead_letter"
                    ),
                    "claims_minted": resumed["claims_minted"],
                    "claims_detached": resumed["claims_detached"],
                    "claims_resurrected": resumed["claims_resurrected"],
                    "claims_superseded": resumed["claims_superseded"],
                    "detachment_annotations": resumed["detachment_annotations"],
                    "operation_receipts": resumed["receipts"],
                }
                raw_resume_outcome = plan.context.get("outcome")
                resume_outcome = (
                    dict(raw_resume_outcome)
                    if isinstance(raw_resume_outcome, dict)
                    else {
                        "quality": "unknown",
                        "units": {},
                        "failed_units": [],
                        "provider_failures": int(plan.context.get("provider_failures") or 0),
                        "empty_after_retry_blocks": int(
                            plan.context.get("empty_after_retry_blocks") or 0
                        ),
                    }
                )
                resume_outcome["plan_id"] = plan.plan_id
                resume_outcome["receipts_complete"] = True
                resume_summary["outcome"] = resume_outcome
                resume_ledger.finish_run(
                    plan.run_id,
                    state="completed",
                    summary=resume_summary,
                    post_semantic_policy_fingerprint=str(
                        plan.context.get("semantic_policy_fingerprint") or ""
                    ),
                )
                resume_ledger.close_stale_runs(
                    document_id=resume_document_id,
                    keep_run_id=plan.run_id,
                )
                _event(
                    "resume_commit_plan",
                    "Applied sealed semantic commit plan",
                    resume_summary,
                )
                if not must_refresh_source:
                    outcomes = tuple(
                        CandidateOutcome.model_validate(value) for value in resumed["outcomes"]
                    )
                    return RememberResult(
                        document_id=resume_document_id,
                        committed=resume_summary["committed"],
                        queued=resume_summary["queued"],
                        dead_lettered=resume_summary["dead_lettered"],
                        blocks_total=int(plan.context.get("blocks_total") or 0),
                        nodes_extracted=int(plan.context.get("nodes_extracted") or 0),
                        edges_extracted=int(plan.context.get("edges_extracted") or 0),
                        claims_minted=resumed["claims_minted"],
                        provider_error=plan.context.get("provider_error"),
                        provider_failures=int(plan.context.get("provider_failures") or 0),
                        empty_after_retry_blocks=int(
                            plan.context.get("empty_after_retry_blocks") or 0
                        ),
                        llm_disabled=bool(plan.context.get("llm_disabled")),
                        outcomes=outcomes,
                        outcome=resume_outcome,
                        ledger_run_id=plan.run_id,
                    )

        # Provider/embedder resolution is intentionally below apply-resume. A
        # sealed plan is a graph-only transaction and never touches an adapter.
        embedder = cast("EmbeddingProvider", self._embedder or self._vault.embedder)
        self._vault._ensure_embedding_compatible(embedder)
        ingest_cfg = self._vault_config().ingest
        _incremental_on = _incremental.incremental_enabled(
            ingest_cfg
        ) or _incremental.subchunk_enabled(ingest_cfg)
        prior_snapshot = (
            _incremental.capture_prior_snapshot(
                self._vault.store,
                source,
                chunk_size_bytes=ingest_cfg.chunk_size_bytes,
                chunk_overlap_bytes=ingest_cfg.chunk_overlap_bytes,
            )
            if _incremental_on
            else _incremental.PriorSnapshot()
        )
        from datetime import datetime as _adt
        from datetime import timezone as _atz

        try:
            asserted_at = (
                _adt.fromtimestamp(Path(str(source)).stat().st_mtime, _atz.utc).date().isoformat()
            )
        except (OSError, ValueError, OverflowError):
            asserted_at = _adt.now(_atz.utc).date().isoformat()

        document = self._vault.add(source)
        _event(
            "document",
            "Document and block anchors written",
            {"id": document.id, "title": document.title, "path": document.path},
        )
        if sensitivity == "local_only" and not _is_local_provider(self._get_provider()):
            raise CompanionError(
                "sensitivity='local_only' refuses a hosted LLM provider; "
                "pin a local base_url for sensitive vaults"
            )
        provider = self._get_provider()
        llm, llm_node_artifacts = _llm_node_artifacts(provider)

        # Document-level provenance: LLM candidates link back to the source
        # document so an autonomous write is always traceable.
        provenance = Provenance(
            source=document.id,
            rule_id="companion-remember",
            layer="llm-extraction",
        )

        # A1: per-block extraction. Extract ONCE PER anchored Block so every
        # candidate/edge carries the byte-range of the Block it came from. A
        # relationship spanning two blocks anchors to the block where its
        # subject and object co-occur (the one the extractor saw together);
        # cross-block relations are not stitched (known limitation). When the
        # source produced no Blocks (e.g. a non-file URL), fall back to a single
        # document-level extraction with no Block anchor (no Claim minted).
        units = _extraction_units(
            store,
            source,
            chunk_size_bytes=ingest_cfg.chunk_size_bytes,
            chunk_overlap_bytes=ingest_cfg.chunk_overlap_bytes,
        )
        intentionally_skipped_units: list[tuple[_BlockAnchor | None, str, str]] = []
        # ADR 0023 Layer 1: skip re-extraction of Blocks whose content-hash
        # already carries live LLM Claims. ``plan_extraction`` partitions the
        # post-add Blocks into extract / skip / orphan; we keep only the units
        # whose Block must be (re-)extracted. Behaviour-neutral when the flag is
        # off (``_incremental_on`` False ⇒ no partition, every unit extracted).
        incremental_plan = None
        # Resume-aware bypass (ADR 0015 D5b × ADR 0024, detective-confirmed
        # defect): a crashed run leaves Blocks committed but Claims uncommitted
        # (verdicts streamed to the ledger, commit batched at the end). On
        # re-ingest the incremental partition/sub-chunk diff then sees content
        # already in the store and collapses the unit list, changing
        # blocks_total and defeating find_resumable_run's exact-match guard —
        # stranding the crashed run's verdicts forever. When ANY open
        # 'started' run exists for this document (model-independent: recovery
        # must survive an LLM reconfig; replay stays model-guarded), extract
        # FULL units so the resume fingerprint lines up and verdicts replay.
        _resume_pending = False
        if _incremental_on and units:
            _resume_pending = self._candidate_ledger().has_open_run(
                document_id=document.id,
            )
            incremental_plan = _incremental.plan_extraction(
                store,
                source,
                prior_snapshot,
                chunk_size_bytes=ingest_cfg.chunk_size_bytes,
                chunk_overlap_bytes=ingest_cfg.chunk_overlap_bytes,
            )
            if _resume_pending:
                # Bypass mode still drops ORPHAN blocks (stale content no
                # longer in the file — re-minting it would be mint-then-
                # maybe-detach in one run); extract + skipped blocks go WHOLE
                # so the resume fingerprint stays stable.
                retained: list[tuple[_BlockAnchor | None, str]] = []
                for anchor, text in units:
                    if anchor is not None and anchor.block_id in incremental_plan.orphan_block_ids:
                        intentionally_skipped_units.append((anchor, text, "orphaned"))
                    else:
                        retained.append((anchor, text))
                units = retained
            else:
                retained = []
                for anchor, text in units:
                    if anchor is None or anchor.block_id in incremental_plan.extract_block_ids:
                        retained.append((anchor, text))
                    elif anchor.block_id in incremental_plan.skipped_block_ids:
                        intentionally_skipped_units.append((anchor, text, "unchanged_block"))
                    else:
                        intentionally_skipped_units.append((anchor, text, "orphaned"))
                units = retained
            _event(
                "incremental_partition",
                "Incremental ingest partitioned blocks",
                {
                    **incremental_plan.counts,
                    **({"resume_bypass": True} if _resume_pending else {}),
                },
            )
            # ADR 0024 Feature 1: narrow each CHANGED Block to its changed Hunks.
            # The parent Block id is unchanged; only the byte range / hash / text
            # shrink to the edited fragment, so a one-line edit extracts ~one line
            # instead of the whole 12k window. Unmatched/new Blocks fall back to
            # whole-Block extraction (correctness never depends on a clean diff).
            if _incremental.subchunk_enabled(ingest_cfg) and not _resume_pending:
                file_bytes = _read_source_bytes(source)
                hunks_extracted = 0
                narrowed: list[tuple[_BlockAnchor | None, str]] = []
                for anchor, text in units:
                    if anchor is None or file_bytes is None:
                        narrowed.append((anchor, text))
                        continue
                    block_node = store.get_node(anchor.block_id)
                    block_index = int(
                        (block_node.facets.get("block_index") if block_node else 0) or 0
                    )
                    new_raw = file_bytes[anchor.byte_start : anchor.byte_end]
                    sub_units = _incremental.subchunk_units_for_block(
                        block_id=anchor.block_id,
                        block_index=block_index,
                        new_byte_start=anchor.byte_start,
                        new_raw=new_raw,
                        new_text=text,
                        prior=prior_snapshot,
                    )
                    for su in sub_units:
                        if not su.whole:
                            hunks_extracted += 1
                        narrowed.append(
                            (
                                _BlockAnchor(
                                    block_id=su.block_id,
                                    byte_start=su.byte_start,
                                    byte_end=su.byte_end,
                                    content_hash=su.content_hash,
                                    source_path=anchor.source_path,
                                ),
                                su.text,
                            )
                        )
                units = narrowed
                _event(
                    "subchunk_partition",
                    "Sub-chunk diff narrowed changed blocks",
                    {"hunks_extracted": hunks_extracted, "units": len(units)},
                )
        trace_context: dict[str, Any] = {}
        curator_context: dict[str, Any] = {}
        relation_context: dict[str, Any] = {}
        # Fix 3 (issue #4 — LLM-disabled/unconfigured honesty). Resolve the
        # vault's llm config ONCE here, before any provider is dialed:
        #   - ``cfg.llm.enabled`` gates whether extraction runs at all. A
        #     vault that has explicitly turned the LLM off must not silently
        #     dial anything — structural ingest (vault.add / has_heading)
        #     still happens, but extraction is skipped with an honest note
        #     instead of quietly returning an empty, success-shaped result.
        #   - a vault with NO explicit llm config falls back to the
        #     hard-coded ``LLMDefaults`` (provider=openai, api_base=
        #     127.0.0.1:8123) — a phantom endpoint on most machines. When
        #     every block then fails to connect, the raw exception reads like
        #     a transient network blip rather than "this vault was never
        #     configured"; detect that case so the error message can say so.
        from okto_neuron.config import LLMDefaults as _LLMDefaults

        cfg = self._vault_config()
        PredicateRegistry(self._vault.path).seed_builtins(cfg.packs)
        fingerprints, identity_decisions = self._fingerprinted_identity_snapshot(
            cfg,
            ingest_config=ingest_cfg,
        )
        llm_enabled = cfg.llm.enabled
        _extraction_resolved = cfg.llm.resolved("extraction")
        _llm_defaults = _LLMDefaults()
        _llm_is_unconfigured_default = (
            _extraction_resolved.provider == _llm_defaults.provider
            and _extraction_resolved.api_base == _llm_defaults.api_base
        )
        if llm_enabled:
            if self._extractor is None:
                # Pre-flight (Fix B1). A vault created with the built-in
                # defaults inherits provider=openai + a real api_base but an
                # EMPTY model (``LLMDefaults.model`` is "" on purpose, so the
                # defaults never claim a model the endpoint may not serve).
                # Dialing that resolves to ``openai/`` and every block dies with
                # a per-block "provider_unavailable" ledger row — a misleading
                # reason, since the provider was up and only the model id was
                # missing. Refuse BEFORE the extractor is built: no completion
                # call, no ledger row, no per-block failure, and an error that
                # names the vault and the exact key to set. Only the
                # unconfigured-default shape is caught; a vault that pinned its
                # own provider/api_base is left alone (an empty model there is
                # the operator's explicit choice). ``self._provider is not
                # None`` means a provider was injected directly (tests,
                # embedded callers); the resolved config is not what gets
                # dialed then, so the config-shaped guard does not apply.
                if (
                    self._provider is None
                    and _llm_is_unconfigured_default
                    and not str(_extraction_resolved.model or "").strip()
                ):
                    raise CompanionError(
                        f"vault {self._vault.path} has no LLM model configured: it "
                        f"resolved to provider={_extraction_resolved.provider!r} "
                        f"api_base={_extraction_resolved.api_base!r} with an empty "
                        "model, which matches the built-in defaults. Set "
                        "llm.defaults.model in okto-neuron.yaml to a model the "
                        "endpoint serves before ingesting."
                    )
                extractor = self._build_extractor(
                    _tracked_provider(
                        self._get_provider("extraction"),
                        label="Extraction",
                        context=lambda: dict(
                            getattr(_extraction_trace_context, "value", None) or trace_context
                        ),
                    )
                )
            else:
                extractor = self._get_extractor()
        else:
            extractor = None

        blocks_total = len(units)
        ledger = self._candidate_ledger()
        # ADR 0015 D5b — ledger-native mid-file resume. If a prior run for this
        # document is still "started" with matching extraction parameters
        # (blocks_total + model), reuse its run_id and replay its recorded
        # curation verdicts instead of re-judging. Any mismatch ⇒ fresh run.
        ledger_model = str(getattr(provider, "model", "unknown"))
        try:
            resume_run_id = ledger.find_resumable_run(
                document_id=document.id,
                blocks_total=blocks_total,
                model=ledger_model,
                extraction_fingerprint=fingerprints.extraction_fingerprint,
                semantic_policy_fingerprint=fingerprints.semantic_policy_fingerprint,
            )
            completed_replay_run_ids = (
                ()
                if resume_run_id is not None or self._materialization_scope is None
                else ledger.find_completed_decision_runs(
                    document_id=document.id,
                    blocks_total=blocks_total,
                    model=ledger_model,
                    config_fingerprint=fingerprints.config_fingerprint,
                    extraction_fingerprint=fingerprints.extraction_fingerprint,
                    semantic_policy_fingerprint=(fingerprints.semantic_policy_fingerprint),
                    materialization_scope=self._materialization_scope,
                )
            )
        except ValueError as exc:
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="candidate ledger cannot safely replay semantic decisions",
                cause=exc,
            ) from exc
        prior_node_verdicts: dict[str, dict[str, Any]] = {}
        prior_relation_verdicts: dict[str, dict[str, Any]] = {}
        # Read STRAIGHT off the snapshot, not through `_prior_verdicts_for_method`:
        # that helper funnels through `_replayable_verdicts`, whose
        # `_REPLAYABLE_ACTIONS = {"commit", "queue"}` filter would drop every
        # `same`/`inverse`/`narrower`/`distinct` row. Keyed by predicate label,
        # not candidate id (the rows use `candidate_id="predicate:<label>"`).
        prior_predicate_resolutions: dict[str, dict[str, Any]] = {}
        prior_candidate_records: dict[str, dict[str, Any]] = {}
        prior_node_identity_by_id: dict[str, tuple[str, str]] = {}
        resumed_candidate_rows: set[str] = set()
        replay_comparison_method = "resume_replay"
        if resume_run_id is not None:
            run_id = resume_run_id
            snapshot = ledger.resume_snapshot(run_id)
            prior_node_verdicts = _prior_verdicts_for_method(
                snapshot.verdicts_by_method,
                "curator",
            )
            prior_relation_verdicts = _prior_verdicts_for_method(
                snapshot.verdicts_by_method,
                "relation_curator",
                relation=True,
            )
            prior_predicate_resolutions = dict(
                snapshot.verdicts_by_method.get("predicate_resolution", {})
            )
            prior_candidate_records = dict(snapshot.candidate_records)
            prior_node_identity_by_id = dict(snapshot.node_identity_by_id)
            resumed_candidate_rows = snapshot.candidate_ids
            _event(
                "resume_run",
                f"Resuming interrupted ingest run {run_id}",
                {
                    "run_id": run_id,
                    "prior_node_verdicts": len(prior_node_verdicts),
                    "prior_relation_verdicts": len(prior_relation_verdicts),
                    "prior_candidate_rows": len(resumed_candidate_rows),
                },
            )
        else:
            run_id = ledger.start_run(
                document_id=document.id,
                source=_source_for_ledger(source),
                blocks_total=blocks_total,
                model=ledger_model,
                semantic_policy_fingerprint=(fingerprints.semantic_policy_fingerprint),
                config_fingerprint=fingerprints.config_fingerprint,
                extraction_fingerprint=fingerprints.extraction_fingerprint,
                materialization_scope=self._materialization_scope,
            )
            if completed_replay_run_ids:
                for snapshot in ledger.resume_snapshots(completed_replay_run_ids):
                    for key, record in snapshot.verdicts_by_method.get(
                        "predicate_resolution", {}
                    ).items():
                        prior_predicate_resolutions.setdefault(key, record)
                    for candidate_id, record in snapshot.candidate_records.items():
                        prior_candidate_records.setdefault(candidate_id, record)
                    for node_id, identity in snapshot.node_identity_by_id.items():
                        prior_node_identity_by_id.setdefault(node_id, identity)
                    for candidate_id, record in _prior_verdicts_for_method(
                        snapshot.verdicts_by_method,
                        "curator",
                    ).items():
                        prior_node_verdicts.setdefault(candidate_id, record)
                    for candidate_id, record in _prior_verdicts_for_method(
                        snapshot.verdicts_by_method,
                        "relation_curator",
                        relation=True,
                    ).items():
                        prior_relation_verdicts.setdefault(candidate_id, record)
                replay_comparison_method = "policy_replay"
                _event(
                    "policy_replay",
                    "Replaying completed semantic decisions under the same policy",
                    {
                        "run_id": run_id,
                        "replayed_from_run_id": completed_replay_run_ids[0],
                        "replayed_from_run_ids": list(completed_replay_run_ids),
                        "prior_node_verdicts": len(prior_node_verdicts),
                        "prior_relation_verdicts": len(prior_relation_verdicts),
                    },
                )
        extraction_unit_identities = {
            block_index: _extraction_unit_identity(
                anchor,
                text,
                document_id=document.id,
                source=source,
                extraction_fingerprint=fingerprints.extraction_fingerprint,
            )
            for block_index, (anchor, text) in enumerate(units)
        }
        for skipped_anchor, skipped_text, skipped_reason in intentionally_skipped_units:
            skipped_identity = _extraction_unit_identity(
                skipped_anchor,
                skipped_text,
                document_id=document.id,
                source=source,
                extraction_fingerprint=fingerprints.extraction_fingerprint,
            )
            ledger.record_extraction_unit(
                run_id,
                document_id=document.id,
                **skipped_identity,
                attempt=0,
                status="intentionally_skipped",
                reason=skipped_reason,
            )
        _event(
            "chunks",
            f"Parsed {blocks_total} extraction chunk(s)",
            {
                "chunks": [
                    {
                        "index": index,
                        "anchor": _anchor_payload(anchor),
                        "text": text,
                    }
                    for index, (anchor, text) in enumerate(units)
                ]
            },
        )
        _emit("extracting", 0, blocks_total)

        node_by_id: dict[str, NodeCandidate] = {}
        node_source_text_by_id: dict[str, str] = {}
        edge_by_key: dict[tuple[str, str, str, str | None], EdgeCandidate] = {}
        # Genuine same-key relationship re-emissions folded by the accumulator
        # below. Surfaced after the loop so a collapse is never silent.
        collapsed_duplicate_edge_candidates = 0
        provider_failures = 0
        provider_error: str | None = None
        # "auto" extraction-mode awareness counters. ``unexpected_finish_blocks``
        # = blocks whose extraction terminated on a finish_reason other than
        # stop/length (the data-loss guard fired); ``still_truncated_blocks`` =
        # blocks where the enumerate escalation ITSELF still truncated. Both are
        # surfaced (warning + trace) so an operator sees data-integrity anomalies.
        unexpected_finish_blocks = 0
        still_truncated_blocks = 0
        # Blocks that had non-empty text but still yielded 0 candidates after
        # the extractor's single empty-result retry (``empty_after_retry``) —
        # surfaced as its own anomaly so it reads as "extraction lost this"
        # rather than "this block was legitimately empty".
        empty_after_retry_blocks = 0
        invalid_output_blocks = 0
        successful_extraction_units = 0
        failed_unit_summaries: list[dict[str, object]] = []
        # Non-empty blocks LLM extraction was actually attempted on (fix 1 —
        # loud total failure). Distinguishes "every attempted block failed"
        # from "nothing needed extracting" or "llm disabled" — both of the
        # latter leave this at 0, so they never trip the total-failure raise
        # below even though provider_failures is also 0 in both cases.
        attempted_blocks = 0
        # Defect A fix — llm.enabled=false is a deliberate, healthy config, not
        # a provider failure. Track it via ``llm_disabled`` instead of stuffing
        # a message into ``provider_error``: the ingest queue's F4 zero-yield
        # rule treats any truthy ``provider_error`` with no yield as a failed
        # item, which previously mislabeled every disabled-vault ingest as an
        # error and retry-looped forever. The log line + trace event still
        # surface the fact for observability.
        llm_disabled = not llm_enabled
        if llm_disabled:
            _event(
                "llm_disabled",
                "LLM disabled for this vault — structural ingest only",
                {"vault_path": str(self._vault.path)},
            )
        extraction_contexts = {
            block_index: {
                "index": block_index,
                "anchor": _anchor_payload(anchor),
            }
            for block_index, (anchor, _text) in enumerate(units)
        }
        from okto_neuron.consolidate.ledger import FRESH_REBUILD_MATERIALIZATION_SCOPE

        replayable_units = (
            ledger.successful_extraction_units(
                document_id=document.id,
                extraction_fingerprint=fingerprints.extraction_fingerprint,
            )
            if (
                _incremental_on
                or resume_run_id is not None
                or self._materialization_scope == FRESH_REBUILD_MATERIALIZATION_SCOPE
            )
            else {}
        )
        reused_extractions: dict[int, ExtractionResult] = {}
        source_changed_blocks: set[int] = set()
        current_source_bytes = _read_source_bytes(source)
        for block_index, (anchor, text) in enumerate(units):
            identity = extraction_unit_identities[block_index]
            if not text.strip():
                ledger.record_extraction_unit(
                    run_id,
                    document_id=document.id,
                    **identity,
                    attempt=0,
                    status="intentionally_skipped",
                    reason="empty_text",
                )
                continue
            if not llm_enabled or extractor is None:
                ledger.record_extraction_unit(
                    run_id,
                    document_id=document.id,
                    **identity,
                    attempt=0,
                    status="intentionally_skipped",
                    reason="llm_disabled",
                )
                continue
            if anchor is not None and (
                current_source_bytes is None
                or anchor.byte_start < 0
                or anchor.byte_end > len(current_source_bytes)
                or anchor.byte_end < anchor.byte_start
                or hashlib.sha256(
                    current_source_bytes[anchor.byte_start : anchor.byte_end]
                ).hexdigest()
                != anchor.content_hash
            ):
                source_changed_blocks.add(block_index)
                failed_unit_summaries.append(
                    {
                        "unit_id": identity["unit_id"],
                        "block_id": identity["block_id"],
                        "byte_start": identity["byte_start"],
                        "byte_end": identity["byte_end"],
                        "error_class": "source_changed",
                        "retryable": True,
                    }
                )
                ledger.record_extraction_unit(
                    run_id,
                    document_id=document.id,
                    **identity,
                    attempt=0,
                    status="source_changed",
                    reason="source_span_no_longer_matches",
                )
                continue
            replay = replayable_units.get(str(identity["unit_id"]))
            if replay is None:
                continue
            try:
                reused = _extraction_result_from_payload(replay.get("result"))
            except (TypeError, ValueError) as exc:
                ledger.record_extraction_unit(
                    run_id,
                    document_id=document.id,
                    **identity,
                    attempt=0,
                    status="invalid_output",
                    reason="stored_replay_incompatible",
                    error_class=type(exc).__name__,
                )
                continue
            reused_extractions[block_index] = reused
            ledger.record_extraction_unit(
                run_id,
                document_id=document.id,
                **identity,
                attempt=0,
                status="succeeded",
                reason="reused",
                anomalies={
                    "truncated": reused.truncated,
                    "unexpected_finish": reused.unexpected_finish,
                    "empty_after_retry": reused.empty_after_retry,
                    "parse_failed": reused.parse_failed,
                },
                result=_extraction_result_payload(reused),
            )
        extractable_blocks = [
            (block_index, text)
            for block_index, (_anchor, text) in enumerate(units)
            if text.strip()
            and llm_enabled
            and extractor is not None
            and block_index not in reused_extractions
            and block_index not in source_changed_blocks
        ]
        extraction_max_concurrent = extraction_effective_max_concurrent(cfg)
        # Make the clamp visible in the ingest event stream. Without this an
        # operator who configured 4 and got 1 sees only silence, because the
        # extraction_schedule event below fires only when the limit is > 1.
        _extraction_capacity_notice = capacity_notice(
            configured=cfg.llm.extraction.max_concurrent,
            effective=extraction_max_concurrent,
            models=[step_model(cfg, step) for step in EXTRACTION_STEPS],
            allowlist=cfg.llm.parallel_capable_models,
        )
        if _extraction_capacity_notice:
            _LOG.warning("extraction capacity: %s", _extraction_capacity_notice)
            _event(
                "extraction_capacity_clamped",
                _extraction_capacity_notice,
                {
                    "configured_max_concurrent": cfg.llm.extraction.max_concurrent,
                    "max_concurrent": extraction_max_concurrent,
                },
            )
        extraction_pool: ThreadPoolExecutor | None = None
        extraction_futures = {}
        extraction_pending = iter(extractable_blocks)
        extraction_pending_exhausted = False
        extraction_config_error: str | None = None

        def _extract_unit_task(block_index: int, text: str) -> ExtractionResult:
            identity = extraction_unit_identities[block_index]
            for attempt in range(1, _PROVIDER_MAX_ATTEMPTS + 1):
                started = time.monotonic()
                try:
                    extraction = _extract_with_trace_context(
                        extractor,
                        text,
                        provenance,
                        extraction_contexts[block_index],
                    )
                except RememberCancelled:
                    ledger.record_extraction_unit(
                        run_id,
                        document_id=document.id,
                        **identity,
                        attempt=attempt,
                        status="cancelled",
                        reason="cooperative_stop",
                        duration_ms=(time.monotonic() - started) * 1000.0,
                    )
                    raise
                except LLMProviderError as exc:
                    category = str(getattr(exc, "category", "unknown") or "unknown")
                    retryable = bool(getattr(exc, "retryable", False))
                    retry_after = _provider_retry_delay(exc, attempt)
                    will_retry = retry_after is not None
                    ledger.record_extraction_unit(
                        run_id,
                        document_id=document.id,
                        **identity,
                        attempt=attempt,
                        status="provider_failed",
                        reason=f"provider_{category}",
                        error_class=category,
                        retry_disposition=(
                            "retried"
                            if will_retry
                            else "exhausted"
                            if retryable
                            else "not_retryable"
                        ),
                        duration_ms=(time.monotonic() - started) * 1000.0,
                    )
                    if retry_after is None:
                        raise
                    deadline = time.monotonic() + retry_after
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        _check_cancelled()
                        time.sleep(min(_EXTRACTION_CONFIG_POLL_SECONDS, remaining))
                    _check_cancelled()
                    continue

                anomalies = {
                    "truncated": extraction.truncated,
                    "unexpected_finish": extraction.unexpected_finish,
                    "empty_after_retry": extraction.empty_after_retry,
                    "parse_failed": extraction.parse_failed,
                }
                if extraction.empty_after_retry:
                    status = "empty_after_retry"
                    reason = "clean_empty_after_internal_retry"
                elif (
                    extraction.parse_failed
                    or extraction.truncated
                    or extraction.unexpected_finish is not None
                ):
                    status = "invalid_output"
                    reason = "malformed_or_incomplete_provider_output"
                else:
                    status = "succeeded"
                    reason = None
                ledger.record_extraction_unit(
                    run_id,
                    document_id=document.id,
                    **identity,
                    attempt=attempt,
                    status=status,
                    reason=reason,
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    anomalies=anomalies,
                    result=(
                        _extraction_result_payload(extraction) if status == "succeeded" else None
                    ),
                )
                return extraction
            raise AssertionError("bounded extraction retry did not terminate")

        def _submit_extraction_window() -> None:
            nonlocal extraction_pending_exhausted
            if extraction_pool is None or extraction_pending_exhausted:
                return
            effective_limit = min(extraction_max_concurrent, len(extractable_blocks))
            while len(extraction_futures) < effective_limit:
                try:
                    block_index, text = next(extraction_pending)
                except StopIteration:
                    extraction_pending_exhausted = True
                    return
                # bind_parent: the pool's threads cannot see the ingest span
                # this document opened, so every extraction call would export
                # as its own root trace.
                extraction_futures[block_index] = extraction_pool.submit(
                    bind_parent(_extract_unit_task),
                    block_index,
                    text,
                )

        def _refresh_extraction_concurrency() -> None:
            nonlocal extraction_max_concurrent, extraction_config_error
            try:
                latest = extraction_effective_max_concurrent(self._vault_config())
            except ConfigParseError as exc:
                detail = str(exc)
                if detail != extraction_config_error:
                    extraction_config_error = detail
                    _LOG.warning(
                        "could not reload extraction concurrency; keeping %d: %s",
                        extraction_max_concurrent,
                        exc,
                    )
                    _event(
                        "extraction_config_reload_error",
                        "Could not reload extraction concurrency; keeping the current limit",
                        {"max_concurrent": extraction_max_concurrent, "error": detail},
                    )
                return
            extraction_config_error = None
            if latest == extraction_max_concurrent:
                return
            previous = extraction_max_concurrent
            extraction_max_concurrent = latest
            _event(
                "extraction_concurrency_changed",
                f"Extraction concurrency changed from {previous} to {latest}",
                {
                    "previous_max_concurrent": previous,
                    "max_concurrent": latest,
                    "effective_concurrent": min(latest, len(extractable_blocks)),
                    "scheduled_blocks": len(extractable_blocks),
                },
            )
            _submit_extraction_window()

        if len(extractable_blocks) > 1:
            effective_concurrency = min(
                extraction_max_concurrent,
                len(extractable_blocks),
            )
            if extraction_max_concurrent > 1:
                _event(
                    "extraction_schedule",
                    f"Extracting up to {effective_concurrency} chunks concurrently",
                    {
                        "max_concurrent": extraction_max_concurrent,
                        "effective_concurrent": effective_concurrency,
                        "scheduled_blocks": len(extractable_blocks),
                    },
                )
            extraction_pool = ThreadPoolExecutor(
                # Threads are created lazily. Provision the validated ceiling so
                # an active ingest can adopt a higher limit without replacing the
                # process or the executor.
                max_workers=min(_EXTRACTION_MAX_CONCURRENT, len(extractable_blocks)),
                thread_name_prefix="okto-neuron-extract",
            )
            _submit_extraction_window()

        try:
            for block_index, (anchor, text) in enumerate(units):
                trace_context = extraction_contexts[block_index]
                if not text.strip():
                    _emit("extracting", block_index + 1, blocks_total)
                    continue
                if not llm_enabled or extractor is None:
                    # Structural ingest still ran (vault.add already happened);
                    # extraction is deliberately skipped, not silently failed.
                    _emit("extracting", block_index + 1, blocks_total)
                    continue
                if block_index in source_changed_blocks:
                    _emit("extracting", block_index + 1, blocks_total)
                    continue
                provider_failed = False
                try:
                    if block_index in reused_extractions:
                        extraction = reused_extractions[block_index]
                        _event(
                            "extraction_reused",
                            f"Reused durable extraction {block_index + 1}/{blocks_total}",
                            {"block": trace_context},
                        )
                    elif extraction_pool is None:
                        attempted_blocks += 1
                        extraction = _extract_unit_task(block_index, text)
                    else:
                        attempted_blocks += 1
                        # Futures are consumed in source order even if later
                        # chunks finish first. All shared-state folding below
                        # therefore remains deterministic and single-threaded.
                        future = extraction_futures[block_index]
                        schedule_more = False
                        try:
                            while not future.done():
                                try:
                                    extraction = future.result(
                                        timeout=_EXTRACTION_CONFIG_POLL_SECONDS
                                    )
                                    break
                                except FuturesTimeoutError:
                                    _check_cancelled()
                                    _refresh_extraction_concurrency()
                            else:
                                extraction = future.result()
                            schedule_more = True
                        except LLMProviderError:
                            # Provider failures are per-block and the existing
                            # partial-failure contract continues with later work.
                            schedule_more = True
                            raise
                        finally:
                            extraction_futures.pop(block_index, None)
                            # Re-read at every completed-chunk scheduling boundary,
                            # then keep the bounded window full. A lower limit stops
                            # new submissions; already-running provider calls finish.
                            if schedule_more and (should_cancel is None or not should_cancel()):
                                _refresh_extraction_concurrency()
                                _submit_extraction_window()
                except LLMProviderError as exc:
                    extraction = ExtractionResult()
                    provider_failed = True
                    provider_failures += 1
                    identity = extraction_unit_identities[block_index]
                    failed_unit_summaries.append(
                        {
                            "unit_id": identity["unit_id"],
                            "block_id": identity["block_id"],
                            "byte_start": identity["byte_start"],
                            "byte_end": identity["byte_end"],
                            "error_class": str(getattr(exc, "category", "unknown") or "unknown"),
                            "retryable": bool(getattr(exc, "retryable", False)),
                        }
                    )
                    if provider_error is None:
                        provider_error = str(exc)
                        if _llm_is_unconfigured_default:
                            # Fix 3 / Defect K — value-equality against LLMDefaults
                            # cannot distinguish "no explicit llm: section" from
                            # "an explicit config that happens to match the
                            # built-in defaults" (e.g. a real local
                            # provider=openai/api_base=127.0.0.1:8123 setup). State
                            # only the observable facts — resolved provider/api_base
                            # match the built-ins — and point at the remedy, instead
                            # of falsely claiming the vault has no config at all.
                            provider_error = (
                                f"{provider_error} — vault {self._vault.path} "
                                f"resolved to provider={_extraction_resolved.provider!r} "
                                f"api_base={_extraction_resolved.api_base!r}, which "
                                "matches the built-in defaults; add an explicit "
                                "llm: section to okto-neuron.yaml if this vault "
                                "should use a different endpoint"
                            )
                    # Don't fail the ingest silently. A provider error on the FIRST
                    # block almost always means a misconfigured/unreachable LLM (bad
                    # api_base, missing api_key, model not served) — it would repeat
                    # for every block and yield zero extraction with no explanation.
                    # Surface it loudly once, and as a trace event the UI/ledger shows.
                    if provider_failures == 1:
                        _LOG.warning(
                            "LLM extraction failed on block %d/%d (%s). Check the vault's "
                            "llm config (api_base reachable? model served? api_key set?). "
                            "Remaining blocks will be retried but likely fail the same way.",
                            block_index + 1,
                            blocks_total,
                            exc,
                        )
                        _event(
                            "extraction_provider_error",
                            "LLM extraction failed — entities cannot be extracted",
                            {"block": trace_context, "error": str(exc)},
                        )
                if getattr(extraction, "unexpected_finish", None):
                    unexpected_finish_blocks += 1
                if extraction.truncated:
                    # In "auto" this is the escalated-and-STILL-truncated case (the
                    # baseline-truncated case was already escalated away); in
                    # baseline/enumerate it is the existing truncation flag.
                    still_truncated_blocks += 1
                if getattr(extraction, "empty_after_retry", False):
                    empty_after_retry_blocks += 1
                unit_invalid = bool(
                    not extraction.empty_after_retry
                    and (
                        extraction.parse_failed
                        or extraction.truncated
                        or extraction.unexpected_finish is not None
                    )
                )
                if unit_invalid:
                    invalid_output_blocks += 1
                if (
                    block_index not in source_changed_blocks
                    and not getattr(extraction, "empty_after_retry", False)
                    and not unit_invalid
                    and not provider_failed
                ):
                    successful_extraction_units += 1
                if unit_invalid or getattr(extraction, "empty_after_retry", False):
                    identity = extraction_unit_identities[block_index]
                    failed_unit_summaries.append(
                        {
                            "unit_id": identity["unit_id"],
                            "block_id": identity["block_id"],
                            "byte_start": identity["byte_start"],
                            "byte_end": identity["byte_end"],
                            "error_class": (
                                "empty_after_retry"
                                if extraction.empty_after_retry
                                else "malformed_output"
                            ),
                            "retryable": False,
                        }
                    )
                _event(
                    "extraction_result",
                    f"Extracted block {block_index + 1}/{blocks_total}",
                    {
                        "block": trace_context,
                        "unexpected_finish": getattr(extraction, "unexpected_finish", None),
                        "truncated": extraction.truncated,
                        "empty_after_retry": getattr(extraction, "empty_after_retry", False),
                        "nodes": [
                            _node_candidate_payload(cand) for cand in extraction.node_candidates
                        ],
                        "edges": [
                            _edge_candidate_payload(edge) for edge in extraction.edge_candidates
                        ],
                    },
                )

                anchor_facets = anchor.facets() if anchor else {}
                for cand in extraction.node_candidates:
                    cid = cand.candidate_id
                    if cid in node_by_id:
                        continue  # dedupe re-mentions across blocks by content hash
                    node_by_id[cid] = cand.model_copy(
                        update={
                            "facets": {**cand.facets, **anchor_facets},
                        }
                    )
                    node_source_text_by_id[cid] = text
                for ecand in extraction.edge_candidates:
                    # The object term, not ``dst_ref``, is half the identity of an
                    # assertion. A literal-object Claim carries its value in
                    # ``dst_literal`` and leaves ``dst_ref`` empty, so keying on
                    # ``dst_ref`` alone made every literal claim sharing a
                    # predicate, a subject and a block collide: two claims that
                    # assert DIFFERENT values about the same subject are different
                    # assertions, and folding them here silently discarded 46% of
                    # all literal claims in a real vault (165 of 358 over 76 docs)
                    # before anything downstream could see them. ``semantic_claim_id``
                    # and ``_semantic_edge_key`` already fold the object into
                    # identity via ``claim_object_identity``; this accumulator was
                    # the one place that did not.
                    key = (
                        ecand.type,
                        ecand.src_ref,
                        (
                            claim_object_identity(literal=ecand.dst_literal)
                            if ecand.dst_literal is not None
                            else claim_object_identity(object_id=ecand.dst_ref)
                        ),
                        anchor.block_id if anchor else None,
                    )
                    if key in edge_by_key:
                        collapsed_duplicate_edge_candidates += 1
                        continue
                    edge_by_key[key] = ecand.model_copy(
                        update={
                            "block_id": anchor.block_id if anchor else None,
                            "byte_start": anchor.byte_start if anchor else None,
                            "byte_end": anchor.byte_end if anchor else None,
                            "content_hash": anchor.content_hash if anchor else None,
                            "model_id": llm.model_id,
                            "prompt_hash": llm.prompt_hash,
                        }
                    )
                _emit("extracting", block_index + 1, blocks_total)
        finally:
            if extraction_pool is not None:
                cancellation_requested = bool(should_cancel is not None and should_cancel())
                if cancellation_requested:
                    for pending_index, pending_future in list(extraction_futures.items()):
                        if pending_future.done():
                            continue
                        ledger.record_extraction_unit(
                            run_id,
                            document_id=document.id,
                            **extraction_unit_identities[pending_index],
                            attempt=0,
                            status="cancelled",
                            reason="cancelled_before_source_order_fold",
                        )
                extraction_pool.shutdown(wait=True, cancel_futures=True)

        scheduled_units = sum(
            1 for _anchor, text in units if text.strip() and llm_enabled and extractor is not None
        )
        skipped_units = len(intentionally_skipped_units) + sum(
            1 for _anchor, text in units if not text.strip() or not llm_enabled or extractor is None
        )
        unresolved_units = (
            provider_failures
            + empty_after_retry_blocks
            + invalid_output_blocks
            + len(source_changed_blocks)
        )
        # A genuine technical failure (provider_failures, invalid_output_blocks,
        # source_changed_blocks) is disjoint from "the extractor ran cleanly and
        # legitimately found no entity-grade content" (empty_after_retry with
        # NONE of those). The latter is a checksum manifest or a two-line ticket
        # -- nothing broke, there was simply nothing to extract -- and must not
        # read as an error: it used to collapse into "failed" alongside real
        # failures, inflating queue_errors and holding /health permanently
        # degraded for documents where nothing actually went wrong (see ADR
        # 0039 T8 addendum). ``genuine_failures`` excludes empty_after_retry_blocks
        # on purpose; it is the count of units that failed for a REAL technical
        # reason.
        genuine_failures = provider_failures + invalid_output_blocks + len(source_changed_blocks)
        all_empty = (
            not genuine_failures
            and empty_after_retry_blocks > 0
            and successful_extraction_units == 0
        )
        # Zero-operation honesty. A run that scheduled nothing, extracted
        # nothing, reused nothing AND recorded no intentional skip did no work
        # and has no account of why — the shape produced by the sub-chunk
        # narrowing defect, where a block was dropped with neither a ledger row
        # nor an ``intentionally_skipped_units`` entry. It must not read as
        # "complete". ``skipped_units`` is the discriminator: a healthy
        # incremental no-op (every block unchanged and already extracted) also
        # has scheduled/succeeded/reused all zero, but carries skipped > 0 and
        # legitimately stays "complete". Deliberately NOT "empty": ADR 0039
        # reserves that for "every unresolved unit's reason is
        # empty_after_retry", and here there are no unresolved units at all.
        # "no_units" stays outside the {failed, integrity_failed} error
        # lifecycle (nothing technically broke) while still failing the
        # ``quality == "complete"`` replay/short-circuit guard in
        # ``consolidate.ledger``, so a caller can always tell it from a healthy
        # no-op.
        nothing_happened = (
            scheduled_units == 0
            and successful_extraction_units == 0
            and not reused_extractions
            and skipped_units == 0
        )
        technical_quality = (
            "not_applicable"
            if llm_disabled
            else "empty"
            if all_empty
            else "failed"
            if unresolved_units and successful_extraction_units == 0
            else "partial"
            if unresolved_units
            else "no_units"
            if nothing_happened
            else "complete"
        )
        extraction_outcome: dict[str, Any] = {
            "quality": technical_quality,
            "units": {
                "scheduled": scheduled_units,
                "attempted": (
                    max(0, successful_extraction_units - len(reused_extractions))
                    + provider_failures
                    + empty_after_retry_blocks
                    + invalid_output_blocks
                ),
                "succeeded": successful_extraction_units,
                "reused": len(reused_extractions),
                "failed": provider_failures + invalid_output_blocks,
                "empty_after_retry": empty_after_retry_blocks,
                "skipped": skipped_units,
                "source_changed": len(source_changed_blocks),
                "cancelled": 0,
            },
            "failed_units": failed_unit_summaries[:50],
            "failed_units_truncated": len(failed_unit_summaries) > 50,
            "provider_failures": provider_failures,
            "empty_after_retry_blocks": empty_after_retry_blocks,
            "plan_id": None,
            "receipts_complete": False,
            "integrity": {
                "status": "pending_post_write_audit",
                "audit_id": None,
                "graph_generation": store.generation() or None,
            },
        }
        if source_changed_blocks:
            ledger.finish_run(
                run_id,
                state="abandoned",
                summary={
                    "reason": "source_changed_during_extraction",
                    "outcome": extraction_outcome,
                },
            )
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="source changed during extraction; retry the current bytes",
            )

        if unexpected_finish_blocks or still_truncated_blocks or empty_after_retry_blocks:
            _LOG.warning(
                "extraction data-integrity anomalies: %d block(s) with an "
                "unexpected finish_reason (guard fired), %d block(s) still "
                "truncated after escalation, %d block(s) still empty after "
                "the empty-result retry",
                unexpected_finish_blocks,
                still_truncated_blocks,
                empty_after_retry_blocks,
            )
            _event(
                "extraction_anomalies",
                "Extraction finished with data-integrity anomalies",
                {
                    "unexpected_finish_blocks": unexpected_finish_blocks,
                    "still_truncated_blocks": still_truncated_blocks,
                    "empty_after_retry_blocks": empty_after_retry_blocks,
                },
            )

        if collapsed_duplicate_edge_candidates:
            # Both figures are document-wide totals: the key is per-block, so a
            # fold in one block sits alongside every other block's survivors.
            _event(
                "extraction_edge_collapse",
                "Folded duplicate relationship candidates during extraction",
                {
                    "collapsed": collapsed_duplicate_edge_candidates,
                    "accumulated": len(edge_by_key),
                },
            )

        node_candidates = list(node_by_id.values())
        edge_candidates = list(edge_by_key.values())
        _emit("embedding", 0, blocks_total)

        embedding_batch_size = cfg.embedding.batch_size
        embedding_max_concurrent_batches = cfg.embedding.max_concurrent_batches
        embedding_config_error: str | None = None

        def _embedding_settings(*, allow_cancel: bool = True) -> tuple[int, int]:
            nonlocal embedding_batch_size
            nonlocal embedding_max_concurrent_batches
            nonlocal embedding_config_error
            if allow_cancel:
                _check_cancelled()
            try:
                latest = self._vault_config().embedding
            except ConfigParseError as exc:
                detail = str(exc)
                if detail != embedding_config_error:
                    embedding_config_error = detail
                    _LOG.warning(
                        "could not reload embedding batch settings; keeping "
                        "batch_size=%d max_concurrent_batches=%d: %s",
                        embedding_batch_size,
                        embedding_max_concurrent_batches,
                        exc,
                    )
                    _event(
                        "embedding_config_reload_error",
                        "Could not reload embedding batch settings; keeping current limits",
                        {
                            "batch_size": embedding_batch_size,
                            "max_concurrent_batches": embedding_max_concurrent_batches,
                            "error": detail,
                        },
                    )
                return embedding_batch_size, embedding_max_concurrent_batches
            embedding_config_error = None
            current = (embedding_batch_size, embedding_max_concurrent_batches)
            updated = (latest.batch_size, latest.max_concurrent_batches)
            if updated != current:
                embedding_batch_size, embedding_max_concurrent_batches = updated
                _event(
                    "embedding_concurrency_changed",
                    "Embedding batch execution settings changed",
                    {
                        "previous_batch_size": current[0],
                        "batch_size": updated[0],
                        "previous_max_concurrent_batches": current[1],
                        "max_concurrent_batches": updated[1],
                    },
                )
            return updated

        if node_candidates:
            from okto_neuron.embed import embed_in_batches

            _event(
                "embedding_schedule",
                f"Embedding {len(node_candidates)} node candidates in bounded batches",
                {
                    "nodes": len(node_candidates),
                    "batch_size": embedding_batch_size,
                    "max_concurrent_batches": embedding_max_concurrent_batches,
                },
            )

            def _embedding_progress(done: int, total: int) -> None:
                mapped = blocks_total if done == total else int(blocks_total * done / total)
                _emit("embedding", mapped, blocks_total)
                _event(
                    "embedding_progress",
                    f"Embedded {done}/{total} node candidates",
                    {"nodes_done": done, "nodes_total": total},
                )

            vectors = embed_in_batches(
                embedder,
                [embedding_text_for(candidate) for candidate in node_candidates],
                batch_size=embedding_batch_size,
                max_concurrent_batches=embedding_max_concurrent_batches,
                settings=_embedding_settings,
                progress=_embedding_progress,
                on_usage=construction_cost.record_embedding,
            )
            node_candidates = [
                candidate.model_copy(update={"embedding": tuple(vector)})
                for candidate, vector in zip(node_candidates, vectors, strict=True)
            ]
        _emit("embedding", blocks_total, blocks_total)

        # ADR 0040 Phase 2: apply the pinned, off-graph type-decision chain before
        # any semantic dedup or identity tier. The candidate-id formula is
        # unchanged; changing the primitive naturally derives a new id, and all
        # candidate edge refs are remapped before resolution sees them.
        relation_replay_source_ids: dict[str, str] = {}
        identity_corrections = _apply_identity_type_corrections(
            node_candidates,
            edge_candidates,
            node_source_text_by_id,
            identity_decisions,
        )
        node_candidates = identity_corrections.nodes
        edge_candidates = identity_corrections.edges
        node_source_text_by_id = identity_corrections.source_text_by_id
        for application in identity_corrections.applications:
            source_candidate = application["source_candidate"]
            derived_candidate = application["derived_candidate"]
            source_id = str(application["source_candidate_id"])
            derived_id = str(application["derived_candidate_id"])
            reason = "; ".join(str(item) for item in application["reasons"])
            ledger.record_comparison(
                run_id,
                candidate_id=source_id,
                method="identity_type_correction",
                target_ref=derived_id,
                verdict="type_corrected",
                reason=reason,
                payload={
                    "previous_type": application["previous_type"],
                    "corrected_type": application["corrected_type"],
                    "decision_ids": application["decision_ids"],
                    "source_candidate": source_candidate.model_dump(mode="json"),
                },
            )
            ledger.record_candidate(
                run_id,
                candidate_id=derived_id,
                candidate_kind="node",
                state="derived",
                payload={
                    "candidate": derived_candidate.model_dump(mode="json"),
                    "derived_from": source_id,
                    "derivation_reason": "identity_type_correction",
                    "decision_ids": application["decision_ids"],
                },
            )
        for source_edge, derived_edge in identity_corrections.edge_derivations:
            source_payload = source_edge.model_dump(mode="json")
            derived_payload = derived_edge.model_dump(mode="json")
            source_id = edge_candidate_id(source_payload)
            derived_id = edge_candidate_id(derived_payload)
            _record_candidate_replay_parent(
                derived_id,
                source_id,
                relation_replay_source_ids,
            )
            ledger.record_comparison(
                run_id,
                candidate_id=source_id,
                method="identity_type_correction_edge_remap",
                target_ref=derived_id,
                verdict="derived",
                reason="edge endpoint remapped to a type-corrected candidate id",
            )
            ledger.record_candidate(
                run_id,
                candidate_id=derived_id,
                candidate_kind="edge",
                state="derived",
                payload={
                    **derived_payload,
                    "derived_from": source_id,
                    "derivation_reason": "identity_type_correction_edge_remap",
                },
            )

        # D3 owning boundary: extraction can assign one exact surface to
        # competing primitives. Before identity resolution, adjudicate only
        # those conflicts from bounded source evidence. High-confidence type
        # corrections derive new candidates and remap edges; incomplete,
        # malformed, low-confidence, or still-cross-type groups remain queued.
        preliminary_cross_type = _cross_type_identity_review_intents(
            node_candidates,
            store,
            identity_decisions,
            identity_corrections.original_ids_by_current,
            identity_corrections.corrected_candidate_ids,
        )
        runtime_identity_corrections: _IdentityCorrectionResult | None = None
        if preliminary_cross_type and not cfg.consolidation.type_adjudication_enabled:
            _event(
                "type_adjudication_disabled",
                "Type adjudication disabled; exact-surface conflicts queued for review",
                {
                    "candidates": len(preliminary_cross_type),
                    "provider_calls": 0,
                },
            )
        if preliminary_cross_type and cfg.consolidation.type_adjudication_enabled:
            from okto_neuron.llm import _scoped_call_timeout
            from okto_neuron.reconcile.type_adjudication import (
                TYPE_ADJUDICATION_PROMPT_VERSION,
                LLMTypeAdjudicator,
                TypeAdjudicationCase,
                TypeAdjudicationResult,
            )

            current_by_id = {candidate.candidate_id: candidate for candidate in node_candidates}
            cases_by_surface: dict[str, tuple[TypeAdjudicationCase, ...]] = {}
            for candidate_id, intent in preliminary_cross_type.items():
                candidate = current_by_id.get(candidate_id)
                surface = str(intent.get("exact_surface_key") or "")
                if candidate is None or not surface:
                    continue
                existing = list(cases_by_surface.get(surface, ()))
                existing.append(
                    TypeAdjudicationCase(
                        candidate_id=candidate_id,
                        reported_type=candidate.type,  # type: ignore[arg-type]
                        title=candidate.title,
                        content=candidate.content,
                        source_excerpt=_type_evidence_excerpt(
                            candidate,
                            node_source_text_by_id.get(candidate_id, ""),
                        ),
                    )
                )
                cases_by_surface[surface] = tuple(existing)

            type_resolved = cfg.llm.resolved("curator")
            type_provider = _tracked_provider(
                self._get_provider("type_adjudication"),
                label="Type adjudication",
                context=lambda: dict(
                    getattr(_type_adjudication_trace_context, "value", None) or {}
                ),
            )
            type_adjudicator = LLMTypeAdjudicator(
                type_provider,
                **sampler_overrides(type_resolved),
                top_p=type_resolved.top_p,
                top_k=type_resolved.top_k,
                min_p=type_resolved.min_p,
                presence_penalty=type_resolved.presence_penalty,
                enable_thinking=type_resolved.enable_thinking,
            )
            group_items = sorted(cases_by_surface.items())
            _event(
                "type_adjudication_schedule",
                f"Adjudicating {len(group_items)} exact-surface type conflict(s)",
                {
                    "groups": len(group_items),
                    "candidates": sum(len(cases) for _, cases in group_items),
                    "max_concurrent": curation_effective_max_concurrent(cfg),
                },
            )

            def _adjudicate_group(
                surface: str,
                cases: tuple[TypeAdjudicationCase, ...],
            ) -> TypeAdjudicationResult:
                previous = getattr(_type_adjudication_trace_context, "value", None)
                _type_adjudication_trace_context.value = {
                    "exact_surface": surface,
                    "candidates": len(cases),
                }
                try:
                    _check_cancelled()
                    with _scoped_call_timeout(cfg.consolidation.curation_call_timeout_s):
                        return type_adjudicator.adjudicate(surface, cases)
                finally:
                    _type_adjudication_trace_context.value = previous

            group_results: dict[str, TypeAdjudicationResult] = {}
            max_workers = min(
                curation_effective_max_concurrent(cfg),
                max(1, len(group_items)),
            )
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    (surface, pool.submit(bind_parent(_adjudicate_group), surface, cases))
                    for surface, cases in group_items
                ]
                for completed, (surface, future) in enumerate(futures, start=1):
                    try:
                        result = future.result(timeout=cfg.consolidation.curation_call_timeout_s)
                    except FuturesTimeoutError:
                        result = TypeAdjudicationResult(error="type-adjudication-timeout")
                    except Exception as exc:  # noqa: BLE001 - queue the group
                        result = TypeAdjudicationResult(
                            error=f"type-adjudication-error:{type(exc).__name__}"
                        )
                    group_results[surface] = result
                    _event(
                        "type_adjudication_progress",
                        f"Adjudicated {completed}/{len(group_items)} type conflict(s)",
                        {
                            "groups_done": completed,
                            "groups_total": len(group_items),
                            "exact_surface": surface,
                            "error": result.error or None,
                        },
                    )

            decisions_by_id = {
                decision.candidate_id: decision
                for result in group_results.values()
                for decision in result.decisions
            }
            runtime_types = {
                candidate_id: decision.primitive_type
                for candidate_id, decision in decisions_by_id.items()
                if decision.confidence >= 0.95
                and candidate_id in current_by_id
                and current_by_id[candidate_id].type != decision.primitive_type
            }
            runtime_reasons = {
                candidate_id: decision.reason
                for candidate_id, decision in decisions_by_id.items()
                if candidate_id in runtime_types
            }
            runtime_identity_corrections = _apply_identity_type_corrections(
                node_candidates,
                edge_candidates,
                node_source_text_by_id,
                identity_decisions,
                runtime_corrections=runtime_types,
                runtime_reasons=runtime_reasons,
                original_ids_by_current=identity_corrections.original_ids_by_current,
                include_pinned=False,
            )
            applied_by_source = {
                str(application["source_candidate_id"]): application
                for application in runtime_identity_corrections.applications
            }
            for candidate_id, intent in preliminary_cross_type.items():
                candidate = current_by_id.get(candidate_id)
                if candidate is None:
                    continue
                surface = str(intent.get("exact_surface_key") or "")
                result = group_results.get(surface, TypeAdjudicationResult())
                decision = decisions_by_id.get(candidate_id)
                application = applied_by_source.get(candidate_id)
                if application is not None:
                    verdict = "type_corrected"
                    reason = decision.reason if decision is not None else "type corrected"
                    target_ref = str(application["derived_candidate_id"])
                elif decision is not None and decision.confidence >= 0.95:
                    verdict = "type_confirmed"
                    reason = decision.reason
                    target_ref = None
                else:
                    verdict = "queue_review"
                    reason = result.error or (
                        decision.reason if decision is not None else "missing decision"
                    )
                    target_ref = None
                excerpt = _type_evidence_excerpt(
                    candidate,
                    node_source_text_by_id.get(candidate_id, ""),
                )
                ledger.record_comparison(
                    run_id,
                    candidate_id=candidate_id,
                    method="identity_type_adjudicator",
                    target_ref=target_ref,
                    verdict=verdict,
                    score=(decision.confidence if decision is not None else 0.0),
                    reason=reason,
                    payload={
                        "exact_surface_key": surface,
                        "previous_type": candidate.type,
                        "adjudicated_type": (
                            decision.primitive_type if decision is not None else None
                        ),
                        "threshold": 0.95,
                        "prompt_version": TYPE_ADJUDICATION_PROMPT_VERSION,
                        "model": type_adjudicator.model,
                        "duration_s": result.duration_s,
                        "usage": result.usage,
                        **(
                            {"provider_retries": [dict(r) for r in result.provider_retries]}
                            if result.provider_retries
                            else {}
                        ),
                        "source_evidence": {
                            "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                            "bytes": len(excerpt.encode("utf-8")),
                        },
                    },
                )
            for application in runtime_identity_corrections.applications:
                source_candidate = application["source_candidate"]
                derived_candidate = application["derived_candidate"]
                source_id = str(application["source_candidate_id"])
                derived_id = str(application["derived_candidate_id"])
                ledger.record_candidate(
                    run_id,
                    candidate_id=derived_id,
                    candidate_kind="node",
                    state="derived",
                    payload={
                        "candidate": derived_candidate.model_dump(mode="json"),
                        "derived_from": source_id,
                        "derivation_reason": "identity_type_adjudicator",
                        "source_candidate": source_candidate.model_dump(mode="json"),
                    },
                )
            for source_edge, derived_edge in runtime_identity_corrections.edge_derivations:
                source_payload = source_edge.model_dump(mode="json")
                derived_payload = derived_edge.model_dump(mode="json")
                source_id = edge_candidate_id(source_payload)
                derived_id = edge_candidate_id(derived_payload)
                _record_candidate_replay_parent(
                    derived_id,
                    source_id,
                    relation_replay_source_ids,
                )
                ledger.record_comparison(
                    run_id,
                    candidate_id=source_id,
                    method="identity_type_adjudicator_edge_remap",
                    target_ref=derived_id,
                    verdict="derived",
                    reason="edge endpoint remapped to a type-adjudicated candidate id",
                )
                ledger.record_candidate(
                    run_id,
                    candidate_id=derived_id,
                    candidate_kind="edge",
                    state="derived",
                    payload={
                        **derived_payload,
                        "derived_from": source_id,
                        "derivation_reason": "identity_type_adjudicator_edge_remap",
                    },
                )

            prior_identity_corrections = identity_corrections
            node_candidates = runtime_identity_corrections.nodes
            edge_candidates = runtime_identity_corrections.edges
            node_source_text_by_id = runtime_identity_corrections.source_text_by_id
            identity_corrections = _IdentityCorrectionResult(
                nodes=node_candidates,
                edges=edge_candidates,
                source_text_by_id=node_source_text_by_id,
                original_ids_by_current=(runtime_identity_corrections.original_ids_by_current),
                corrected_candidate_ids=frozenset(
                    prior_identity_corrections.corrected_candidate_ids
                    | runtime_identity_corrections.corrected_candidate_ids
                ),
                correction_review_intents={
                    **prior_identity_corrections.correction_review_intents,
                    **runtime_identity_corrections.correction_review_intents,
                },
                applications=(
                    prior_identity_corrections.applications
                    + runtime_identity_corrections.applications
                ),
                edge_derivations=(
                    prior_identity_corrections.edge_derivations
                    + runtime_identity_corrections.edge_derivations
                ),
            )

        identity_review_intents = dict(identity_corrections.correction_review_intents)
        cross_type_review_intents = _cross_type_identity_review_intents(
            node_candidates,
            store,
            identity_decisions,
            identity_corrections.original_ids_by_current,
            identity_corrections.corrected_candidate_ids,
        )
        for candidate_id, intent in cross_type_review_intents.items():
            identity_review_intents.setdefault(candidate_id, intent)
        for candidate_id, intent in identity_review_intents.items():
            is_cross_type = intent.get("code") == "identity_cross_type_exact_collision"
            ledger.record_comparison(
                run_id,
                candidate_id=candidate_id,
                method=("identity_cross_type" if is_cross_type else "identity_type_correction"),
                verdict="queue_review",
                reason=(
                    "exact surface occurs under multiple primitive types"
                    if is_cross_type
                    else "type correction would collapse an explicitly distinct identity"
                ),
                payload=intent,
            )
        if identity_corrections.applications or identity_review_intents:
            _event(
                "identity_decisions",
                "Applied identity type decisions before resolution",
                {
                    "type_corrections": len(identity_corrections.applications),
                    "identity_review_intents": len(identity_review_intents),
                },
            )

        negative_cache_hits: set[tuple[str, str]] = set()

        def _identity_merge_blocked(left_id: str, right_id: str) -> bool:
            if left_id in identity_review_intents or right_id in identity_review_intents:
                return True
            if not _is_explicitly_distinct(
                identity_decisions,
                identity_corrections.original_ids_by_current,
                left_id,
                right_id,
            ):
                return False
            pair = tuple(sorted((left_id, right_id)))
            if pair not in negative_cache_hits:
                negative_cache_hits.add(pair)
                ledger.record_comparison(
                    run_id,
                    candidate_id=left_id,
                    method="identity_negative_cache",
                    target_ref=right_id,
                    verdict="distinct_cached",
                    reason="explicit DistinctDecision blocked identity merge",
                )
            return True

        # Keep the raw post-extraction proposal set for audit coverage. Later
        # deterministic remaps can drop or rewrite candidates before the normal
        # curator/relation-curator pass sees them; the audit pass records an LLM
        # verdict for those hidden proposals without changing write semantics.
        raw_node_candidates = list(node_candidates)
        raw_edge_candidates = list(edge_candidates)
        _event(
            "embedding",
            "Embedded extracted node candidates",
            {
                "nodes": len(node_candidates),
                "edges": len(edge_candidates),
                "embedding_dim": (
                    len(node_candidates[0].embedding)
                    if node_candidates and node_candidates[0].embedding is not None
                    else None
                ),
            },
        )
        # ADR 0015 D5b: on a resumed run, re-extraction reproduces the same
        # candidate ids — skip "proposed" rows already in the ledger (the
        # v0.0.9 duplicate-rows bug). Terminal-state rows later in the pipeline
        # are state transitions and still append normally.
        for candidate in node_candidates:
            if candidate.candidate_id in resumed_candidate_rows:
                continue
            ledger.record_candidate(
                run_id,
                candidate_id=candidate.candidate_id,
                candidate_kind="node",
                state="proposed",
                payload=candidate.model_dump(mode="json"),
            )
        for candidate in edge_candidates:
            payload = candidate.model_dump(mode="json")
            edge_cid = edge_candidate_id(payload)
            if edge_cid in resumed_candidate_rows:
                continue
            ledger.record_candidate(
                run_id,
                candidate_id=edge_cid,
                candidate_kind="edge",
                state="proposed",
                payload=payload,
            )
        if not node_candidates and not edge_candidates:
            # Fix 1 (issue #4 — loud total failure). Every block that had
            # something to extract from failed with a provider error: this is
            # the has_heading-only bug — a configured/reachable-looking LLM
            # that silently extracted nothing, leaving only the structural
            # claim vault.add() mints LLM-free. Mirrors the ingest queue's F4
            # zero-yield rule (_ingest_queue.py: a provider error with zero
            # commits/claims/queued is an error, not a quiet "done"). Partial
            # yield (>=1 block succeeded, even if it happened to propose zero
            # candidates) is NOT total failure and keeps returning success
            # below with ``provider_error`` set (watcher retry semantics).
            _total_provider_failure = attempted_blocks > 0 and provider_failures == attempted_blocks
            _early_detached = 0
            _early_resurrections: tuple[dict[str, Any], ...] = ()
            _early_artifact: list[str] = []
            if not _total_provider_failure:
                # A genuine pure deletion/revert still needs lifecycle writes,
                # but those writes now use the same seal/apply/receipt contract
                # as a normal semantic run. A total provider failure must never
                # reinterpret an unextracted replacement as a deletion.
                from datetime import datetime as _edt
                from datetime import timezone as _etz

                _check_cancelled()
                _early_valid_as_of = _edt.now(_etz.utc).date().isoformat()
                _early_source_bytes = _read_source_bytes(source)
                if _early_source_bytes is None:
                    raise IngestError(
                        source,
                        vault_path=self._vault.path,
                        message="source bytes became unreadable before lifecycle plan sealing",
                    )
                _early_source_sha256 = hashlib.sha256(_early_source_bytes).hexdigest()
                _early_source_binding = _source_binding(
                    store,
                    source,
                    document.id,
                    source_bytes=_early_source_bytes,
                )
                _early_reconciliation = _plan_claim_reconciliation(
                    store,
                    source=source,
                    vault_path=self._vault.path,
                    incremental_plan=incremental_plan,
                    ingest_cfg=ingest_cfg,
                    base_operations=[],
                    valid_as_of=_early_valid_as_of,
                    asserted_at=asserted_at,
                    source_bytes=_early_source_bytes,
                    source_generation_sha256=_early_source_sha256,
                )
                extraction_outcome["construction_cost"] = construction_cost.snapshot()
                _early_plan_id = ledger.record_commit_plan(
                    run_id,
                    operations=list(_early_reconciliation.operations),
                    context={
                        "document_id": document.id,
                        "source": str(source),
                        "source_binding": _early_source_binding,
                        "asserted_at": asserted_at,
                        "blocks_total": blocks_total,
                        "nodes_extracted": 0,
                        "edges_extracted": 0,
                        "provider_error": provider_error,
                        "provider_failures": provider_failures,
                        "empty_after_retry_blocks": empty_after_retry_blocks,
                        "llm_disabled": llm_disabled,
                        "config_fingerprint": fingerprints.config_fingerprint,
                        "extraction_fingerprint": fingerprints.extraction_fingerprint,
                        "semantic_policy_fingerprint": (fingerprints.semantic_policy_fingerprint),
                        "outcome": extraction_outcome,
                    },
                )
                _early_plan = next(
                    plan
                    for plan in ledger.unreceipted_commit_plans(document_id=document.id)
                    if plan.plan_id == _early_plan_id
                )
                _early_binding_matches, _early_binding_evidence = _source_binding_evidence(
                    store,
                    source,
                    _early_plan.context.get("source_binding"),
                )
                if not _early_binding_matches:
                    ledger.record_plan_abandoned(
                        run_id,
                        plan_id=_early_plan.plan_id,
                        plan_hash=_early_plan.plan_hash,
                        reason=str(_early_binding_evidence["reason"]),
                        evidence=_early_binding_evidence,
                    )
                    ledger.finish_run(
                        run_id,
                        state="abandoned",
                        summary={
                            "reason": "source_changed_before_lifecycle_apply",
                            "source_binding": _early_binding_evidence,
                        },
                    )
                    raise IngestError(
                        source,
                        vault_path=self._vault.path,
                        message="source changed while lifecycle plan was sealed; retry",
                    )
                _early_applied = _apply_sealed_semantic_plan(
                    _early_plan,
                    store=store,
                    ledger=ledger,
                    registry=PredicateRegistry(self._vault.path),
                    review_queue=self._review_queue(),
                )
                _early_detached = int(_early_applied["claims_detached"])
                extraction_outcome["plan_id"] = _early_plan_id
                # Same zero-operation honesty as the main sealed-plan path.
                extraction_outcome["receipts_complete"] = not nothing_happened
                _early_resurrections = _early_reconciliation.resurrections
                _early_artifact = list(_early_reconciliation.annotation_artifacts)
                if _early_detached or _early_resurrections:
                    _event(
                        "claims_reconciled",
                        "Reconciled lifecycle from a zero-candidate plan",
                        {
                            "claims_detached": _early_detached,
                            "claims_superseded": 0,
                            "claims_resurrected": len(_early_resurrections),
                            "stale_source": any(
                                row.get("was_superseded") for row in _early_resurrections
                            ),
                            "valid_as_of": _early_valid_as_of,
                            "annotation": _early_artifact,
                        },
                    )
            if _total_provider_failure:
                _event(
                    "extraction_total_failure",
                    "LLM extraction failed on every attempted block — "
                    "structural ingest only, no entities extracted",
                    {
                        "provider_error": provider_error,
                        "attempted_blocks": attempted_blocks,
                        "provider_failures": provider_failures,
                    },
                )
            else:
                _event("done", "No extractable graph candidates", {})
            ledger.finish_run(
                run_id,
                state="failed" if _total_provider_failure else "completed",
                summary={
                    "committed": 0,
                    "queued": 0,
                    "dead_lettered": 0,
                    "nodes_extracted": 0,
                    "edges_extracted": 0,
                    "claims_minted": 0,
                    "claims_detached": _early_detached,
                    "claims_superseded": 0,
                    "relations_corroborated": 0,
                    "provider_error": provider_error,
                    "provider_failures": provider_failures,
                    "empty_after_retry_blocks": empty_after_retry_blocks,
                    "outcome": extraction_outcome,
                },
            )
            # Ledger bookkeeping (finish_run + close_stale_runs) MUST complete
            # before the raise below — a crashed-run resume depends on this
            # run being closed, exactly as the success path requires it.
            ledger.close_stale_runs(document_id=document.id, keep_run_id=run_id)
            if _total_provider_failure:
                raise LLMUnavailableError(
                    f"LLM extraction failed on every attempted block "
                    f"({provider_failures}/{attempted_blocks}) while remembering "
                    f"{document.id}: {provider_error}",
                    outcome=extraction_outcome,
                    ledger_run_id=run_id,
                    document_id=document.id,
                )
            return RememberResult(
                document_id=document.id,
                committed=0,
                queued=0,
                blocks_total=blocks_total,
                provider_error=provider_error,
                provider_failures=provider_failures,
                empty_after_retry_blocks=empty_after_retry_blocks,
                llm_disabled=llm_disabled,
                outcomes=(),
                outcome=extraction_outcome,
                ledger_run_id=run_id,
            )

        # Collapse intra-session duplicates BEFORE staging: two candidates for the
        # same entity extracted in one remember() each read as novel against the
        # committed store (resolve runs pre-commit), so both would clear the gate
        # and commit. Merging here, with edge-ref remap to the survivor, keeps it
        # at one node and leaves no dangling edges.
        from okto_neuron.consolidate import collapse_duplicates

        _emit("dedup", blocks_total, blocks_total)

        before_exact = {"nodes": len(node_candidates), "edges": len(edge_candidates)}
        node_candidates, edge_candidates = collapse_duplicates(
            node_candidates,
            edge_candidates,
            embedder=embedder,
            source_text_by_id=node_source_text_by_id,
            merge_blocked=_identity_merge_blocked,
        )
        _event(
            "dedup_exact_batch",
            "Exact within-file duplicate collapse",
            {
                "before": before_exact,
                "after": {"nodes": len(node_candidates), "edges": len(edge_candidates)},
            },
        )
        ledger.record_comparison(
            run_id,
            candidate_id="batch",
            method="exact_batch",
            verdict="dedup_pass",
            payload={
                "before": before_exact,
                "after": {"nodes": len(node_candidates), "edges": len(edge_candidates)},
            },
        )

        # The merge judge is shared across the within-batch and store passes:
        # both ask the same same-vs-distinct question, so one conservatively-built
        # LLMMergeJudge serves both (and is reused, not rebuilt per pass).
        cfg = self._vault_config()
        judge_resolved = cfg.llm.resolved("judge")
        # Dedup is where a 7-hour live ingest went dark for 30+ minutes while
        # actively burning LLM calls. Keep it traced and costed at the call
        # boundary just like extraction and curation.
        dedup_judge_provider = _tracked_provider(
            self._get_provider("judge"),
            label="Merge Judge",
            context=lambda: {"stage": "dedup", "pairs_judged": _dedup_pairs_judged[0]},
        )
        merge_judge = LLMMergeJudge(
            dedup_judge_provider,
            **sampler_overrides(judge_resolved),
            top_p=judge_resolved.top_p,
            top_k=judge_resolved.top_k,
            min_p=judge_resolved.min_p,
            presence_penalty=judge_resolved.presence_penalty,
            enable_thinking=judge_resolved.enable_thinking,
            # Step-level only — avoids defaults.system_prompt clobbering the
            # structured judge prompt.
            system_prompt=cfg.llm.judge.system_prompt,
        )

        # Per-pair dedup visibility (companion candidate-ledger going silent
        # for 30+ minutes during dedup while LLM calls were still firing): one
        # tiny ledger row (ids/verdict/confidence only — never an embedding
        # vector) per judged pair, plus a dedup_progress on_event every
        # DEDUP_PROGRESS_EVENT_EVERY pairs, mirroring the relation/node
        # progress cadence below. These per-pair rows are ADDITIONAL to the
        # existing end-of-pass aggregate "judge_batch"/"judge_store" summary
        # rows recorded further down, which are left unchanged. Ledger writes
        # run unconditionally (visibility in the ledger doesn't depend on the
        # web UI's on_event); the dedup_progress event itself is a no-op via
        # _event() when on_event is None.
        _dedup_pairs_judged = [0]

        def _dedup_on_pair(method: str) -> Callable[[str, str, Any], None]:
            def _on_pair(candidate_id: str, target_id: str, verdict: Any) -> None:
                _dedup_pairs_judged[0] += 1
                _emit_substage("dedup")
                # ADR 0039 D5: a judge call that needed a transient-provider
                # retry says so on its own row; the reason tells a recovered
                # verdict from one that degraded (``llm-unavailable``).
                retried = getattr(verdict, "provider_retries", ())
                ledger.record_comparison(
                    run_id,
                    candidate_id=candidate_id,
                    method=method,
                    target_ref=target_id,
                    verdict="same" if verdict.same else "distinct",
                    score=verdict.confidence,
                    reason=verdict.reason if retried else "",
                    payload=(
                        {"provider_retries": [dict(r) for r in retried]} if retried else None
                    ),
                )
                if _dedup_pairs_judged[0] % DEDUP_PROGRESS_EVENT_EVERY == 0:
                    _event(
                        "dedup_progress",
                        f"Dedup judged {_dedup_pairs_judged[0]} pair(s)",
                        {"pairs_judged": _dedup_pairs_judged[0]},
                    )

            return _on_pair

        # Within-batch fuzzy dedup (Option B): collapse_duplicates above is exact
        # normalized-title only, so two look-alikes born in THIS remember() (e.g.
        # an `alice` node from frontmatter and an `agent:alice` node from prose)
        # are both novel to the store and would both commit — the store-facing
        # judge below never compares batch siblings to each other. This pass does:
        # same-type, embedding-band-gated, edge-connected pairs never fused, judge
        # decides same-vs-distinct, later candidate merges into the earlier one
        # with edge-ref remap. Runs before the store tiers so the survivor is what
        # gets reconciled and judged against the committed graph.
        within = judge_within_batch(
            node_candidates,
            edge_candidates,
            judge=merge_judge,
            embedder=embedder,
            on_pair=_dedup_on_pair("judge_batch"),
            merge_blocked=_identity_merge_blocked,
        )
        _record_edge_candidate_replay_derivations(
            edge_candidates,
            within.merged_into,
            relation_replay_source_ids,
        )
        node_candidates = within.survivors
        edge_candidates = within.edges
        _event(
            "dedup_judge_batch",
            "Judge within-batch duplicate pass",
            {
                "merged_into": within.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
            },
        )
        ledger.record_comparison(
            run_id,
            candidate_id="batch",
            method="judge_batch",
            verdict="merge_pass",
            payload={
                "merged_into": within.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
            },
        )

        # Cross-tier edge-endpoint guard: reconcile_against_store (Tier 0) and
        # judge_against_store (Tier 1/2) each build their OWN EdgeEndpointGuard,
        # scoped to that single call — see judge_against_store's docstring
        # ("It does NOT reach across the call boundary from a PRIOR
        # reconcile_against_store pass"). Without this, a candidate B that is
        # edge-connected (subject/object of one staged relationship) to a
        # candidate A already folded by Tier 0 into an existing store node can
        # still independently merge into that SAME store node at Tier 1/2 — the
        # A-B relationship becomes a self-loop and is silently dropped, even
        # though A and B were never actually the same entity as each other.
        # Threaded here at the call site (not inside resolve/__init__.py, which
        # only exposes the shared ``EdgeEndpointGuard`` primitive): one guard,
        # built from the edges as staged BEFORE Tier 0's remap (so A and B are
        # still distinct ids in its connectivity set, unlike the post-remap
        # edges each tier's own internal guard sees), wired into both calls via
        # their existing ``merge_blocked`` veto hook and folded with Tier 0's
        # merges before Tier 1 runs.
        cross_tier_guard = EdgeEndpointGuard(edge_candidates)

        def _cross_tier_merge_blocked(candidate_id: str, target_id: str) -> bool:
            return _identity_merge_blocked(
                candidate_id, target_id
            ) or cross_tier_guard.blocks_external(candidate_id, target_id)

        # Tier 0 cross-file dedup: collapse candidates that EXACTLY match an
        # already-committed node into it. collapse_duplicates only dedupes within
        # this remember() call, so the same entity mentioned across files would
        # otherwise commit once per file (e.g. one repeated person → 9 nodes). Edges are
        # remapped onto the surviving store node so a re-mention's relationship
        # still lands (and still mints its Claim) without a duplicate entity.
        reconciliation = reconcile_against_store(
            node_candidates,
            edge_candidates,
            store,
            merge_blocked=_cross_tier_merge_blocked,
        )
        for _folded_id, _survivor_id in reconciliation.merged_into.items():
            cross_tier_guard.fold(_folded_id, _survivor_id)
        _record_edge_candidate_replay_derivations(
            edge_candidates,
            reconciliation.merged_into,
            relation_replay_source_ids,
        )
        node_candidates = reconciliation.survivors
        edge_candidates = reconciliation.edges
        _event(
            "dedup_store_exact",
            "Exact cross-file store reconcile",
            {
                "merged_into": reconciliation.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
            },
        )
        ledger.record_comparison(
            run_id,
            candidate_id="store",
            method="exact_store",
            verdict="merge_pass",
            payload={
                "merged_into": reconciliation.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
            },
        )

        # Tier 1 + Tier 2 fuzzy dedup: an LLM judge adjudicates the embedding band
        # that Tier 0's exact match missed (first-name to full-name, surface
        # variants), merging only unmistakable same-entity look-alikes and leaving
        # genuinely distinct ones (NX vs NX Lab) to the review gate. The judge is
        # conservative by construction (biased "distinct when unsure"; any LLM
        # failure becomes distinct), so it can only reduce duplication, never
        # fabricate a merge.
        judgement = judge_against_store(
            node_candidates,
            edge_candidates,
            store,
            judge=merge_judge,
            embedder=embedder,
            on_pair=_dedup_on_pair("judge_store"),
            merge_blocked=_cross_tier_merge_blocked,
        )
        for _folded_id, _survivor_id in judgement.merged_into.items():
            cross_tier_guard.fold(_folded_id, _survivor_id)
        _record_edge_candidate_replay_derivations(
            edge_candidates,
            judgement.merged_into,
            relation_replay_source_ids,
        )
        node_candidates = judgement.survivors
        edge_candidates = judgement.edges
        _event(
            "dedup_store_judge",
            "Judge cross-file store reconcile",
            {
                "merged_into": judgement.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
                "edges": [_edge_candidate_payload(edge) for edge in edge_candidates],
            },
        )
        ledger.record_comparison(
            run_id,
            candidate_id="store",
            method="judge_store",
            verdict="merge_pass",
            payload={
                "merged_into": judgement.merged_into,
                "survivors": [_node_candidate_payload(cand) for cand in node_candidates],
                "edges": [_edge_candidate_payload(edge) for edge in edge_candidates],
            },
        )
        node_merge_targets: dict[str, str] = {}
        node_merge_targets.update(within.merged_into)
        node_merge_targets.update(reconciliation.merged_into)
        node_merge_targets.update(judgement.merged_into)

        _emit("committing", blocks_total, blocks_total)

        # Serial pre-pass, not a loop with verdicts: one resolve() (embed +
        # store scan) per candidate before a single curation call starts. Tick
        # it too — the keep-alive is worthless if the phase's first silent
        # stretch already outlasts the client's idle timer.
        pairs = []
        claim_snapshot = _claims_for_contradiction_scan(store, node_candidates)
        for candidate in node_candidates:
            pairs.append(
                (candidate, resolve(candidate, store, embedder=embedder, claims=claim_snapshot))
            )
            _emit_substage("committing")
        for candidate, outcome in pairs:
            if outcome.correlations:
                for correlation in outcome.correlations:
                    ledger.record_comparison(
                        run_id,
                        candidate_id=candidate.candidate_id,
                        method="resolver",
                        target_ref=correlation.target_id,
                        verdict=correlation.kind,
                        score=correlation.score,
                        reason=correlation.summary,
                        payload={"confidence": outcome.confidence},
                    )
            else:
                ledger.record_comparison(
                    run_id,
                    candidate_id=candidate.candidate_id,
                    method="resolver",
                    verdict="novel",
                    score=outcome.confidence,
                    payload={"confidence": outcome.confidence},
                )
        confidences = {cand.candidate_id: outcome.confidence for cand, outcome in pairs}
        cfg = self._vault_config()
        consolidation = cfg.consolidation
        predicate_alias_index = PredicateAliasIndex(self._vault.path)
        predicate_aliases = {
            **builtin_predicate_aliases(cfg.packs),
            **predicate_alias_index.alias_map(),
        }
        predicate_inverse_mappings = predicate_alias_index.inverse_map()
        predicate_registry = PredicateRegistry(self._vault.path)
        predicate_records_at_start = {
            record.label: record for record in predicate_registry.records()
        }
        predicate_records = dict(predicate_records_at_start)
        # Rendered ONCE per run from the START-OF-RUN snapshot and handed to the
        # relation curator as part of its shared user-prompt prefix. Deliberately
        # asymmetric with `predicate_records`, which GROWS as this run mints
        # (below and in the planned-record loop) so a fold is binding against
        # labels this same run already coined: the curator's block must stay
        # byte-stable or every mint would break the provider's prefix cache
        # mid-run, while the resolver wants the live view.
        predicate_registry_block = render_registry_block(predicate_records_at_start)
        # Per-LABEL, never per-relation: `has_value` alone carries support in the
        # hundreds, so per-relation keying would be a ~100x cost error.
        predicate_resolutions: dict[str, PredicateResolution] = {}
        predicate_proposal_counts: dict[str, int] = {}
        pending_alias_records: dict[str, PredicateAliasRecord] = {}
        # Fold records held back until the fold has actually survived the
        # direction guard in `_admit_relation`. Keyed by the NOVEL label rather
        # than by record id so it survives `_resolve_novel_predicate`'s per-label
        # memo: the second relation proposing the same label returns from the
        # memo without recomputing anything, and must still be able to stage.
        deferred_alias_records: dict[str, PredicateAliasRecord] = {}
        gate_config = GateConfig(
            auto_commit_threshold=consolidation.auto_commit_threshold,
            review_on_contradiction=consolidation.review_on_contradiction,
        )
        curator_resolved = cfg.llm.resolved("curator")
        curator_provider = _tracked_provider(
            self._get_provider("curator"),
            label="Curator",
            # Fan-out workers stamp a thread-local context before each curate();
            # the main thread falls back to the closure variable.
            context=lambda: dict(
                getattr(_curation_trace_context, "value", None) or curator_context
            ),
        )
        curator = LLMCandidateCurator(
            curator_provider,
            **sampler_overrides(curator_resolved),
            top_p=curator_resolved.top_p,
            top_k=curator_resolved.top_k,
            min_p=curator_resolved.min_p,
            presence_penalty=curator_resolved.presence_penalty,
            enable_thinking=curator_resolved.enable_thinking,
            system_prompt=cfg.llm.curator.system_prompt,
            packs=cfg.packs,
        )
        curated_pairs: list[tuple[NodeCandidate, ResolveOutcome]] = []
        curator_counts = {"commit": 0, "queue": 0, "abstain": 0}
        curated_node_ids: set[str] = set()
        # ADR 0015 D1: serial pre-pass builds prompts (store reads stay on the
        # main thread), then only the LLM round-trips fan out with bounded
        # concurrency. Gate decisions and ledger writes consume the verdicts
        # afterwards, on the main thread, in original candidate order.
        node_proposed_action = (
            "  resolver_proposal: pass this candidate to the confidence gate\n"
            f"  gate_threshold: {gate_config.auto_commit_threshold:.3f}\n"
            "  curator_role: approve commit, or queue for human review"
        )
        # ADR 0015 D2 rule 2 — low-signal node demotion. Deterministic, no-LLM:
        # an under-mentioned candidate whose content is one trivial sentence is
        # queued (reviewable, never dropped) without a curator call. Mention
        # counts use the RAW per-block candidate sets (pre-collapse), so
        # recurrence across blocks is what the threshold measures.
        prefilter_cfg = consolidation.prefilter
        node_prefilter_reasons: dict[str, str] = {}
        if prefilter_cfg.enabled and prefilter_cfg.min_mentions >= 2:
            raw_mention_counts = count_title_mentions(raw_node_candidates)
            for candidate, _outcome in pairs:
                mention_count = raw_mention_counts.get(discovery_surface_key(candidate.title), 0)
                if is_trivial_node(
                    candidate,
                    mention_count,
                    min_mentions=prefilter_cfg.min_mentions,
                    max_trivial_node_chars=prefilter_cfg.max_trivial_node_chars,
                ):
                    node_prefilter_reasons[candidate.candidate_id] = (
                        "prefilter: low-signal node — title mentioned "
                        f"{mention_count}x (< min_mentions={prefilter_cfg.min_mentions}) "
                        "and content is a single trivial sentence; queued without a "
                        "curator call"
                    )
        # ADR 0015 D4 — block-keyed batched curation. >1 groups same-block
        # candidates into one schema-constrained LLM call; any member failing
        # the deterministic validation gate falls back to the single-call path.
        curation_batch_size = consolidation.curation_batch_size
        node_curation_calls: list[Callable[[], CuratorVerdict]] = []
        node_batch_items: list[BatchCurationItem] = []
        node_replays: dict[str, dict[str, Any]] = {}
        for candidate, outcome in pairs:
            # Before the ``continue``s: a resume-heavy run replays most
            # candidates and would otherwise go silent through the whole
            # prompt pre-pass.
            _emit_substage("committing")
            curated_node_ids.add(candidate.candidate_id)
            if candidate.candidate_id in identity_review_intents:
                continue
            if candidate.candidate_id in node_prefilter_reasons:
                continue
            # ADR 0015 D5b: a candidate already judged by the resumed run's
            # curator is never re-submitted to the LLM (nor to a D4 batch) —
            # its recorded verdict is replayed through the gate below.
            replay_record = prior_node_verdicts.get(candidate.candidate_id)
            if replay_record is None:
                replay_record = _terminal_node_replay_record(
                    prior_candidate_records.get(candidate.candidate_id)
                )
            if replay_record is not None:
                node_replays[candidate.candidate_id] = replay_record
                continue
            curator_context = {
                "candidate_id": candidate.candidate_id,
                "type": candidate.type,
                "title": candidate.title,
            }
            prebuilt_prompt = curator.build_prompt(
                candidate,
                outcome,
                store=store,
                edges=edge_candidates,
                proposed_action=node_proposed_action,
            )

            def _node_call(
                candidate: "NodeCandidate" = candidate,
                outcome: ResolveOutcome = outcome,
                context: dict[str, Any] = curator_context,
                prompt: str = prebuilt_prompt,
            ) -> "CuratorVerdict":
                _curation_trace_context.value = context
                return curator.curate(
                    candidate,
                    outcome,
                    store=store,
                    edges=edge_candidates,
                    proposed_action=node_proposed_action,
                    user_prompt=prompt,
                )

            node_curation_calls.append(_node_call)
            if curation_batch_size > 1:
                raw_block_id = candidate.facets.get("block_id")
                node_batch_items.append(
                    BatchCurationItem(
                        candidate_id=candidate.candidate_id,
                        block_id=str(raw_block_id) if raw_block_id else None,
                        excerpt=_source_excerpt(candidate, store),
                        prompt=prebuilt_prompt,
                        trace_context=curator_context,
                        single_call=_node_call,
                    )
                )
        # ADR 0015 D5a: the post-pass drains verdicts as an ordered-prefix
        # STREAM — each candidate's gate decision + ledger write happens as
        # soon as its verdict is available, restoring live progress and the
        # per-verdict durability window. The D4 batch path streams too: each
        # batch's verdicts (including its per-member single-call fallback) are
        # yielded as that batch returns, so ledger rows land per batch instead
        # of at phase end and D5b resume picks up from the last completed
        # batch.
        node_pair_iter: Iterator[tuple[CuratorVerdict, dict[str, Any] | None]]
        if curation_batch_size > 1:
            node_pair_iter = iter_batched_curation(
                node_batch_items,
                provider=curator_provider,
                system_prompt=(
                    cfg.llm.curator.system_prompt or candidate_curator_system(cfg.packs)
                ),
                relation=False,
                batch_size=effective_curation_batch_size(curation_batch_size, relation=False),
                max_concurrent=curation_effective_max_concurrent(cfg),
                timeout_s=consolidation.curation_call_timeout_s,
                temperature=curator_resolved.temperature,
                max_tokens=curator_resolved.max_tokens,
                top_p=curator_resolved.top_p,
                top_k=curator_resolved.top_k,
                min_p=curator_resolved.min_p,
                presence_penalty=curator_resolved.presence_penalty,
                enable_thinking=curator_resolved.enable_thinking,
                set_trace_context=_set_curation_trace_context,
                fallback_runner=lambda calls: _fan_out_verdicts(
                    calls,
                    max_concurrent=curation_effective_max_concurrent(cfg),
                    timeout_s=consolidation.curation_call_timeout_s,
                ),
            )
        else:
            node_pair_iter = (
                (verdict, None)
                for verdict in _iter_fan_out_verdicts(
                    node_curation_calls,
                    max_concurrent=curation_effective_max_concurrent(cfg),
                    timeout_s=consolidation.curation_call_timeout_s,
                )
            )
        node_reviewed = 0
        node_review_total = len(pairs)
        for candidate, outcome in pairs:
            identity_review_intent = identity_review_intents.get(candidate.candidate_id)
            prefilter_reason = node_prefilter_reasons.get(candidate.candidate_id)
            replay_record = node_replays.get(candidate.candidate_id)
            if identity_review_intent is not None:
                verdict = CuratorVerdict(
                    action="queue",
                    confidence=0.0,
                    reason=str(identity_review_intent["code"]),
                )
                comparison_method = "identity_policy"
                batch_meta = {"identity_review_intent": identity_review_intent}
            elif prefilter_reason is not None:
                # Demotion = queue, never silent discard (ADR 0015 D2): the
                # candidate stays in curated_pairs at confidence 0.0, exactly
                # what a curator "queue" verdict produces today.
                verdict = CuratorVerdict(action="queue", confidence=0.0, reason=prefilter_reason)
                comparison_method = "prefilter"
                batch_meta = None
            elif replay_record is not None:
                # ADR 0015 D5b: replay the resumed run's recorded verdict —
                # no LLM call happened; gating below is identical.
                verdict = _replay_verdict(replay_record)
                comparison_method = replay_comparison_method
                batch_meta = _replay_meta(
                    replay_record,
                    mode=replay_comparison_method,
                )
            else:
                verdict, batch_meta = next(node_pair_iter)
                comparison_method = "curator"
            curator_counts[verdict.action] += 1
            ledger.record_comparison(
                run_id,
                candidate_id=candidate.candidate_id,
                method=comparison_method,
                verdict=verdict.action,
                score=verdict.confidence,
                reason=verdict.reason,
                payload={
                    "resolver_confidence": outcome.confidence,
                    **_verdict_telemetry(verdict),
                    # ADR 0015 D4: batch provenance is auditable — present only
                    # when the verdict came from a batch call.
                    **(batch_meta or {}),
                },
            )
            if verdict.action == "commit":
                curated_outcome = ResolveOutcome(
                    correlations=outcome.correlations,
                    confidence=max(outcome.confidence, verdict.confidence),
                )
            else:
                curated_outcome = ResolveOutcome(
                    correlations=outcome.correlations,
                    confidence=0.0,
                )
            curated_pairs.append((candidate, curated_outcome))
            node_reviewed += 1
            _emit_substage("committing")
            # ADR 0015 D5a: with the streamed drain this ticks live, mirroring
            # the relation phase's progress events.
            if node_reviewed % NODE_PROGRESS_EVENT_EVERY == 0:
                _event(
                    "curator_progress",
                    "Candidate curator progress",
                    {
                        "reviewed": node_reviewed,
                        "total": node_review_total,
                        "remaining": max(node_review_total - node_reviewed, 0),
                        "decisions": dict(curator_counts),
                    },
                )
        pairs = curated_pairs
        confidences = {cand.candidate_id: outcome.confidence for cand, outcome in pairs}
        _event("curator", "Candidate curator decisions", curator_counts)
        superseded_node_candidates: dict[str, dict[str, Any]] = {}
        superseded_node_reviews: dict[str, tuple[NodeCandidate, tuple[Correlation, ...]]] = {}
        canonical_pairs: list[tuple[NodeCandidate, ResolveOutcome]] = []
        accepted_node_by_key: dict[tuple[str, str], NodeCandidate] = {}
        post_curator_node_merges: dict[str, str] = {}
        for candidate, outcome in pairs:
            would_commit, _ = decide(outcome, gate_config)
            key = _entity_key(candidate)
            if would_commit and key is not None:
                survivor = accepted_node_by_key.get(key)
                if (
                    survivor is not None
                    and survivor.candidate_id != candidate.candidate_id
                    and not _identity_merge_blocked(candidate.candidate_id, survivor.candidate_id)
                ):
                    reason = (
                        "accepted exact-title duplicate canonicalized to the first "
                        "accepted same-type/same-title node"
                    )
                    post_curator_node_merges[candidate.candidate_id] = survivor.candidate_id
                    node_merge_targets[candidate.candidate_id] = survivor.candidate_id
                    superseded_payload = {
                        "candidate": candidate.model_dump(mode="json"),
                        "reason": reason,
                        "terminal_state": "superseded",
                        "target_ref": survivor.candidate_id,
                        "curator_verdict": {
                            "action": "commit",
                            "confidence": outcome.confidence,
                            "reason": reason,
                        },
                    }
                    superseded_node_candidates[candidate.candidate_id] = superseded_payload
                    ledger.record_comparison(
                        run_id,
                        candidate_id=candidate.candidate_id,
                        method="post_curator_exact_title",
                        target_ref=survivor.candidate_id,
                        verdict="superseded",
                        score=outcome.confidence,
                        reason=reason,
                        payload={
                            "canonical_key": {"type": key[0], "title": key[1]},
                            "proposed_terminal_action": "supersede_candidate",
                        },
                    )
                    continue
                accepted_node_by_key[key] = candidate
            canonical_pairs.append((candidate, outcome))
        pairs = canonical_pairs
        confidences = {cand.candidate_id: outcome.confidence for cand, outcome in pairs}
        _record_edge_candidate_replay_derivations(
            edge_candidates,
            post_curator_node_merges,
            relation_replay_source_ids,
        )
        edge_candidates = _remap_edge_candidates_to_node_targets(
            edge_candidates,
            post_curator_node_merges,
        )
        if post_curator_node_merges:
            _event(
                "dedup_post_curator_exact",
                "Canonicalized accepted exact-title duplicates",
                {"merged_into": post_curator_node_merges},
            )
        curator_audit_counts = {"commit": 0, "queue": 0, "abstain": 0}
        claim_snapshot = _claims_for_contradiction_scan(
            store,
            [c for c in raw_node_candidates if c.candidate_id not in curated_node_ids],
        )
        for candidate in raw_node_candidates:
            if candidate.candidate_id in curated_node_ids:
                continue
            outcome = resolve(candidate, store, embedder=embedder, claims=claim_snapshot)
            target_ref = node_merge_targets.get(candidate.candidate_id)
            audit_reason = (
                "candidate remapped to an existing/surviving node before the final node gate"
                if target_ref
                else "candidate removed by deterministic exact duplicate collapse before the final node gate"
            )
            curator_context = {
                "candidate_id": candidate.candidate_id,
                "type": candidate.type,
                "title": candidate.title,
                "audit_only": True,
            }
            # ADR 0015 D2 rule 4 — established-entity fast path. A Tier-0
            # exact_store re-mention of a live store node that brings no novel
            # content (its content is a near-dup of what the node already
            # carries) makes the curator verdict a foregone conclusion: skip
            # the LLM audit and treat it as a curator commit. Open point per
            # the ADR: "established" should ideally mean the matched node was
            # itself curator-COMMITTED, not merely present; node rows carry no
            # curator provenance today, so this implements the documented
            # fallback "exists in store + no novel content".
            fastpath_node = None
            if (
                prefilter_cfg.established_entity_fastpath
                and candidate.candidate_id in reconciliation.merged_into
                and target_ref
            ):
                fastpath_node = store.get_node(target_ref)
                if fastpath_node is not None and not is_established_re_mention(
                    candidate, fastpath_node
                ):
                    fastpath_node = None
            if fastpath_node is not None:
                verdict = CuratorVerdict(
                    action="commit",
                    confidence=outcome.confidence,
                    reason="established entity re-mention; curator verdict foregone",
                )
                llm_skipped = True
                comparison_method = "prefilter"
                comparison_verdict = "fastpath_commit"
            elif consolidation.audit_superseded_nodes_with_llm:
                verdict = curator.curate(
                    candidate,
                    outcome,
                    store=store,
                    edges=raw_edge_candidates,
                    proposed_action=(
                        "  resolver_proposal: supersede this raw candidate before graph write\n"
                        f"  target_ref: {target_ref or '(not recorded)'}\n"
                        f"  reason: {audit_reason}\n"
                        "  curator_role: evaluate whether the raw candidate is grounded/useful "
                        "and whether superseding it is semantically safe"
                    ),
                )
                llm_skipped = False
                comparison_method = "curator"
                comparison_verdict = verdict.action
            else:
                verdict = CuratorVerdict(
                    action="commit",
                    confidence=0.0,
                    reason="LLM audit disabled for superseded node candidates",
                )
                llm_skipped = True
                comparison_method = "curator"
                comparison_verdict = verdict.action
            curator_audit_counts[verdict.action] += 1
            terminal_state = "superseded" if verdict.action == "commit" else "queued"
            ledger.record_comparison(
                run_id,
                candidate_id=candidate.candidate_id,
                method=comparison_method,
                verdict=comparison_verdict,
                score=verdict.confidence,
                reason=verdict.reason,
                payload={
                    "resolver_confidence": outcome.confidence,
                    "audit_only": True,
                    "audit_reason": audit_reason,
                    "target_ref": target_ref,
                    "proposed_terminal_action": "supersede_candidate",
                    "llm_skipped": llm_skipped,
                    **_verdict_telemetry(verdict),
                },
            )
            superseded_payload = {
                "candidate": candidate.model_dump(mode="json"),
                "reason": audit_reason,
                "terminal_state": terminal_state,
                "target_ref": target_ref,
                "llm_skipped": llm_skipped,
                "curator_verdict": {
                    "action": verdict.action,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                },
            }
            superseded_node_candidates[candidate.candidate_id] = superseded_payload
            if terminal_state == "queued":
                superseded_node_reviews[candidate.candidate_id] = (
                    candidate,
                    outcome.correlations,
                )
        if any(curator_audit_counts.values()):
            _event(
                "curator_audit",
                "Candidate curator audit decisions",
                curator_audit_counts,
            )
        accepted_candidates = [
            candidate for candidate, outcome in pairs if decide(outcome, gate_config)[0]
        ]
        accepted_refs: set[str] = {candidate.candidate_id for candidate in accepted_candidates}
        accepted_refs |= set(reconciliation.merged_into.values())
        accepted_refs |= set(judgement.merged_into.values())
        node_context = {candidate.candidate_id: candidate for candidate in node_candidates}
        semantic_relation_replay_index = _semantic_relation_replay_index(
            prior_relation_verdicts,
            prior_candidate_records,
            node_context=node_context,
            store=store,
            prior_node_identity_by_id=prior_node_identity_by_id,
        )
        semantic_relation_replays: set[str] = set()
        derived_relation_replays: set[str] = set()
        accepted_by_key: dict[tuple[str, str], str] = {}
        ambiguous_accepted_keys: set[tuple[str, str]] = set()
        for candidate in accepted_candidates:
            key = _entity_key(candidate)
            if key is None:
                continue
            existing = accepted_by_key.get(key)
            if existing is None:
                accepted_by_key[key] = candidate.candidate_id
            elif existing != candidate.candidate_id:
                ambiguous_accepted_keys.add(key)

        relation_resolved = None
        relation_provider = None
        relation_curator = None
        predicate_resolver = None
        if consolidation.relation_curator_enabled:
            relation_resolved = cfg.llm.resolved("relation_curator")
            relation_provider = _tracked_provider(
                self._get_provider("relation_curator"),
                label="Relation Curator",
                context=lambda: dict(
                    getattr(_curation_trace_context, "value", None) or relation_context
                ),
            )
            relation_curator = LLMRelationCurator(
                relation_provider,
                **sampler_overrides(relation_resolved),
                top_p=relation_resolved.top_p,
                top_k=relation_resolved.top_k,
                min_p=relation_resolved.min_p,
                presence_penalty=relation_resolved.presence_penalty,
                enable_thinking=relation_resolved.enable_thinking,
                system_prompt=cfg.llm.relation_curator.system_prompt,
                packs=cfg.packs,
                registry_block=predicate_registry_block,
            )
            # ADR 0040 D6a: one resolution call per NOVEL predicate label per
            # run, on the same provider step the ADR 0017 maintenance judge
            # uses. Only reachable from the live-curator branch below, so it is
            # built under the same guard.
            #
            # Traced and costed at the call boundary like every other LLM caller
            # in this run (merge judge, relation curator, correction judge). An
            # untracked caller is exactly how an unbounded judge loop once went
            # dark here for 30+ minutes while actively burning calls.
            predicate_resolver = LLMPredicateResolver(
                _tracked_provider(
                    self._get_provider("judge"),
                    label="Predicate Resolver",
                    context=lambda: {
                        "stage": "predicate_resolution",
                        "labels_resolved": len(predicate_resolutions),
                    },
                ),
                **sampler_overrides(cfg.llm.resolved("judge")),
                top_p=cfg.llm.resolved("judge").top_p,
                top_k=cfg.llm.resolved("judge").top_k,
                min_p=cfg.llm.resolved("judge").min_p,
                presence_penalty=cfg.llm.resolved("judge").presence_penalty,
                enable_thinking=cfg.llm.resolved("judge").enable_thinking,
            )

        def _edge_ref_live(ref: str) -> bool:
            return ref in accepted_refs or store.get_node(ref) is not None

        def _accepted_equivalent_ref(ref: str) -> str:
            if _edge_ref_live(ref):
                return ref
            candidate = node_context.get(ref)
            if candidate is None:
                return ref
            key = _entity_key(candidate)
            if key is None or key in ambiguous_accepted_keys:
                return ref
            return accepted_by_key.get(key, ref)

        def _remap_edge_to_accepted_equivalent(candidate: EdgeCandidate) -> EdgeCandidate:
            src_ref = _accepted_equivalent_ref(candidate.src_ref)
            dst_ref = (
                candidate.dst_ref
                if candidate.dst_literal is not None
                else _accepted_equivalent_ref(candidate.dst_ref)
            )
            if src_ref == candidate.src_ref and dst_ref == candidate.dst_ref:
                return candidate
            if candidate.dst_literal is None and src_ref == dst_ref:
                return candidate
            return candidate.model_copy(update={"src_ref": src_ref, "dst_ref": dst_ref})

        edge_candidates = [
            _remap_edge_to_accepted_equivalent(candidate) for candidate in edge_candidates
        ]

        # ADR 0015 D2 rule 3 — near-dup literal collapse. Same-block Claim
        # candidates whose normalized literals are near-identical keep the
        # first representative; the rest are superseded BEFORE relation
        # curation (each supersession is a ledger comparison, and the dropped
        # raw candidate still gets its terminal state via the existing
        # superseded-relationship audit loop below — never a silent discard).
        if prefilter_cfg.enabled:
            edge_candidates, literal_superseded = collapse_near_dup_literals(edge_candidates)
            for superseded_edge, survivor_edge in literal_superseded:
                superseded_id = edge_candidate_id(superseded_edge.model_dump(mode="json"))
                survivor_id = edge_candidate_id(survivor_edge.model_dump(mode="json"))
                ledger.record_comparison(
                    run_id,
                    candidate_id=superseded_id,
                    method="prefilter",
                    target_ref=survivor_id,
                    verdict="superseded",
                    score=0.0,
                    reason=(
                        "prefilter: near-duplicate literal from the same block; "
                        "collapsed onto the first representative claim"
                    ),
                    payload={
                        "relation_kind": "claim",
                        "proposed_terminal_action": "supersede_candidate",
                        **superseded_edge.model_dump(mode="json"),
                    },
                )

        # ``edge_candidate_id`` is the durable identity of one extracted
        # relation. Extractors may repeat the exact same candidate within a
        # block; reviewing it twice can produce two terminal decisions for one
        # id and make a later decision overwrite the trace owned by the first.
        # Keep the first occurrence before any curator call. Distinct source
        # anchors retain distinct ids and still become Claim corroborations.
        unique_edge_candidates: list[EdgeCandidate] = []
        seen_edge_candidate_ids: set[str] = set()
        for candidate in edge_candidates:
            candidate_id = edge_candidate_id(candidate.model_dump(mode="json"))
            if candidate_id in seen_edge_candidate_ids:
                continue
            seen_edge_candidate_ids.add(candidate_id)
            unique_edge_candidates.append(candidate)
        edge_candidates = unique_edge_candidates

        relation_counts = {"commit": 0, "queue": 0, "abstain": 0, "skipped": 0, "prefiltered": 0}
        relation_terminal: dict[str, dict[str, Any]] = {}
        all_edge_candidates = list(edge_candidates)
        raw_edge_ids = {
            edge_candidate_id(candidate.model_dump(mode="json"))
            for candidate in raw_edge_candidates
        }
        derived_edge_ids: set[str] = set()
        for candidate in all_edge_candidates:
            payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(payload)
            if candidate_id in raw_edge_ids or candidate_id in derived_edge_ids:
                continue
            derived_edge_ids.add(candidate_id)
            ledger.record_candidate(
                run_id,
                candidate_id=candidate_id,
                candidate_kind="edge",
                state="proposed",
                payload={
                    **payload,
                    "derived": True,
                    "derivation_reason": "deterministic endpoint remap before relation review",
                },
            )
        curated_edge_candidates: list[EdgeCandidate] = []
        canonicalized_edge_candidates: list[EdgeCandidate] = []
        relation_reviewed_ids: set[str] = set()
        relation_review_total = len(all_edge_candidates)
        relation_reviewed = 0

        def _emit_relation_progress(done: int, *, force: bool = False) -> None:
            if relation_review_total == 0:
                return
            if not force and done % RELATION_PROGRESS_EVENT_EVERY != 0:
                return
            _event(
                "relation_curator_progress",
                "Relationship curator progress",
                {
                    "reviewed": done,
                    "total": relation_review_total,
                    "remaining": max(relation_review_total - done, 0),
                    "decisions": dict(relation_counts),
                },
            )

        # ADR 0015 D1: same shape as the node curator — serial pre-pass
        # (payloads, endpoint liveness, prompts: all store reads on the main
        # thread), bounded fan-out of the LLM calls only, then the existing
        # post-verdict body in original order on the main thread.
        relation_prepass: list[
            tuple["EdgeCandidate", dict[str, Any], str, str, bool, bool, str]
        ] = []
        relation_curation_calls: list[Callable[[], CuratorVerdict]] = []
        relation_batch_items: list[BatchCurationItem] = []
        relation_replays: dict[str, dict[str, Any]] = {}
        relation_synthetic_verdicts: dict[str, CuratorVerdict] = {}
        relation_synthetic_methods: dict[str, str] = {}
        # Fix A lookups: the node curator's outcome per candidate (carries the
        # contradiction signal) and the running set of endpoints auto-promoted
        # to anchor relationships (see the endpoint gate below).
        node_outcome_by_id = {cand.candidate_id: outcome for cand, outcome in pairs}
        promoted_endpoint_ids: set[str] = set()
        for candidate in all_edge_candidates:
            # Top of the loop: the relation pre-pass builds payloads, prompts
            # and excerpts serially and skips (``continue``) many candidates,
            # so a tick placed further down would leave the skipped ones dark.
            _emit_substage("committing")
            payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(payload)
            relation_reviewed_ids.add(candidate_id)
            relation_kind = "claim" if candidate.dst_literal is not None else "edge"
            # ADR 0015 D2 rule 1 — predicate demotion. Conversation-mechanics
            # predicates are queued (reviewable) without a relation-curator
            # LLM call. Checked on the RAW predicate and its canonical form.
            if prefilter_cfg.enabled:
                demote_reason = demoted_predicate_reason(
                    candidate.type,
                    prefilter_cfg.demote_predicates,
                    canonical_predicate=normalize_predicate(
                        candidate.type,
                        packs=cfg.packs,
                        predicate_aliases=predicate_aliases,
                    ),
                )
                if demote_reason is not None:
                    relation_counts["prefiltered"] += 1
                    ledger.record_comparison(
                        run_id,
                        candidate_id=candidate_id,
                        method="prefilter",
                        verdict="queue",
                        score=0.0,
                        reason=demote_reason,
                        payload={
                            "relation_kind": relation_kind,
                            "proposed_terminal_action": "queue_review",
                            **payload,
                        },
                    )
                    relation_synthetic_verdicts[candidate_id] = CuratorVerdict(
                        action="queue",
                        confidence=0.0,
                        reason=demote_reason,
                        canonical_predicate=candidate.type.strip(),
                        structural_noise=False,
                    )
                    relation_synthetic_methods[candidate_id] = "semantic_prefilter"
                    relation_prepass.append(
                        (
                            candidate,
                            payload,
                            candidate_id,
                            relation_kind,
                            _edge_ref_live(candidate.src_ref),
                            candidate.dst_literal is not None or _edge_ref_live(candidate.dst_ref),
                            "",
                        )
                    )
                    continue
            src_live = _edge_ref_live(candidate.src_ref)
            dst_live = candidate.dst_literal is not None or _edge_ref_live(candidate.dst_ref)
            missing = "" if src_live and dst_live else "src_ref" if not src_live else "dst_ref"
            endpoint_reason = (
                f"relationship endpoint not accepted by node gate: {missing}" if missing else ""
            )

            # ── Fix A (+ task-13 E3 extension): auto-promote a queued
            # structural endpoint so a grounded relationship is not
            # dead-lettered here. Original Fix A scope: literal-fact Claims
            # (dst_literal set, so dst is always live) whose SUBJECT was
            # queued. E3 extends the SAME mechanism to topology edges with
            # exactly ONE dead endpoint (src or dst) — the audited reference-eval
            # graph lost grounded facts at this gate because the edge's
            # object node (a group-email/stack-label entity) was merely
            # queued by the node curator. Guards unchanged from Fix A: the
            # dead endpoint must be an extracted candidate from THIS run (a
            # dangling ref to nothing stays dead-lettered) and merely queued
            # — NOT contradicted; an edge with BOTH endpoints dead stays
            # dead-lettered; a queued endpoint with an ACCEPTED same-entity
            # sibling is a duplicate the remap machinery already declined
            # (self-loop), never promoted. The promoted node is stamped
            # LOW-SALIENCE at the
            # node gate (below) so it anchors the relationship without
            # surfacing in recall/ask. Necessary-not-sufficient by design:
            # clearing the endpoint reason only lets the relation curator
            # judge the relationship; the endpoint commits only if a
            # relationship survives (relationship-liveness gate), so
            # promotion never mints an orphan node.
            if missing and (src_live or dst_live):
                dead_ref = candidate.src_ref if missing == "src_ref" else candidate.dst_ref
                dead_candidate = node_context.get(dead_ref)
                dead_outcome = node_outcome_by_id.get(dead_ref)
                if (
                    dead_ref not in promoted_endpoint_ids
                    and dead_candidate is not None
                    and dead_outcome is not None
                    and not dead_outcome.contradicted
                    and _accepted_equivalent_ref(dead_ref) == dead_ref
                ):
                    promoted_endpoint_ids.add(dead_ref)
                    accepted_refs.add(dead_ref)
                    ledger.record_comparison(
                        run_id,
                        candidate_id=dead_ref,
                        method="endpoint_gate_promote",
                        verdict=("promoted_subject" if missing == "src_ref" else "promoted_object"),
                        score=0.0,
                        reason=(
                            "Fix A: queued structural subject auto-promoted "
                            "(low-salience) to anchor a literal-fact claim"
                            if relation_kind == "claim"
                            else (
                                "Fix A/E3: queued structural endpoint "
                                "auto-promoted (low-salience) to anchor a "
                                "topology edge"
                            )
                        ),
                        payload={
                            "relation_kind": relation_kind,
                            "claim_candidate_id": candidate_id,
                            "src_ref": candidate.src_ref,
                            "dst_ref": candidate.dst_ref,
                            "dst_literal": candidate.dst_literal,
                        },
                    )
                    if missing == "src_ref":
                        src_live = True
                    else:
                        dst_live = True
                    missing = ""
                    endpoint_reason = ""

            # ADR 0015 D5b: relations already judged by the resumed run skip
            # prompt building and the LLM/batch entirely; the recorded verdict
            # is replayed through the identical post-pass below. (D4 interplay:
            # the candidate never becomes a batch item, so an all-replayed
            # batch never calls the LLM.)
            replay_record = prior_relation_verdicts.get(candidate_id)
            if replay_record is None:
                for source_candidate_id in _candidate_replay_lineage(
                    candidate_id,
                    relation_replay_source_ids,
                ):
                    replay_record = prior_relation_verdicts.get(source_candidate_id)
                    if replay_record is not None:
                        derived_relation_replays.add(candidate_id)
                        break
            if replay_record is None:
                replay_key = _relation_replay_key(
                    candidate,
                    node_context=node_context,
                    store=store,
                    prior_node_identity_by_id=prior_node_identity_by_id,
                )
                if replay_key is not None:
                    replay_record = semantic_relation_replay_index.get(replay_key)
                    if replay_record is not None:
                        semantic_relation_replays.add(candidate_id)
            if replay_record is not None:
                relation_replays[candidate_id] = replay_record
                relation_prepass.append(
                    (
                        candidate,
                        payload,
                        candidate_id,
                        relation_kind,
                        src_live,
                        dst_live,
                        endpoint_reason,
                    )
                )
                continue

            # Endpoint gate BEFORE relation curation: a relation whose endpoint
            # was queued/rejected during node curation can only ever be
            # dead-lettered, so judging it with the LLM is pure waste (run B:
            # 24% of relation candidates, ~18 min + ~700K tokens). Record the
            # SAME comparison method/verdict the post-curation safety-net gate
            # uses and tick progress immediately — ledger semantics and totals
            # are unchanged, the candidate just never reaches the curator.
            if endpoint_reason:
                relation_counts["skipped"] += 1
                ledger.record_comparison(
                    run_id,
                    candidate_id=candidate_id,
                    method="endpoint_gate",
                    verdict="skipped_endpoint",
                    score=0.0,
                    reason=endpoint_reason,
                    payload={
                        "relation_kind": relation_kind,
                        "src_live": src_live,
                        "dst_live": dst_live,
                        "proposed_terminal_action": "dead_letter",
                        **payload,
                    },
                )
                relation_synthetic_verdicts[candidate_id] = CuratorVerdict(
                    action="queue",
                    confidence=0.0,
                    reason=endpoint_reason,
                    canonical_predicate=candidate.type.strip(),
                )
                relation_synthetic_methods[candidate_id] = "semantic_prefilter"
                relation_prepass.append(
                    (
                        candidate,
                        payload,
                        candidate_id,
                        relation_kind,
                        src_live,
                        dst_live,
                        endpoint_reason,
                    )
                )
                continue

            # ADR 0040 ablation: disabling the relation curator is an explicit
            # fail-closed materialization policy. Do not build prompts, call the
            # provider, or replay a verdict produced under the enabled policy.
            # The relation remains visible in the review queue and no predicate
            # can be minted from an unreviewed model output.
            if not consolidation.relation_curator_enabled:
                reason = "relationship curator disabled; queued for review"
                relation_synthetic_verdicts[candidate_id] = CuratorVerdict(
                    action="queue",
                    confidence=0.0,
                    reason=reason,
                    canonical_predicate=candidate.type.strip(),
                )
                relation_synthetic_methods[candidate_id] = "relation_curator_disabled"
                relation_prepass.append(
                    (
                        candidate,
                        payload,
                        candidate_id,
                        relation_kind,
                        src_live,
                        dst_live,
                        endpoint_reason,
                    )
                )
                continue

            relation_context = {
                "candidate_id": candidate_id,
                "relation_kind": relation_kind,
                "type": candidate.type,
                "src_ref": candidate.src_ref,
                "dst_ref": candidate.dst_ref,
                "dst_literal": candidate.dst_literal,
                "src_live": src_live,
                "dst_live": dst_live,
            }
            relation_proposed_action = (
                "  resolver_proposal: dead-letter after relation review\n"
                f"  reason: {endpoint_reason}\n"
                "  curator_role: still evaluate whether the source excerpt supports "
                "the proposed relationship"
                if endpoint_reason
                else (
                    "  resolver_proposal: eligible for graph write or literal claim mint\n"
                    "  curator_role: approve commit, or queue for human review"
                )
            )
            assert relation_curator is not None
            prebuilt_prompt = relation_curator.build_prompt(
                candidate,
                store=store,
                node_candidates=node_context,
                proposed_action=relation_proposed_action,
            )

            def _relation_call(
                candidate: "EdgeCandidate" = candidate,
                context: dict[str, Any] = relation_context,
                proposed_action: str = relation_proposed_action,
                prompt: str = prebuilt_prompt,
            ) -> "CuratorVerdict":
                _curation_trace_context.value = context
                return relation_curator.curate(
                    candidate,
                    store=store,
                    node_candidates=node_context,
                    proposed_action=proposed_action,
                    user_prompt=prompt,
                )

            relation_curation_calls.append(_relation_call)
            if curation_batch_size > 1:
                relation_batch_items.append(
                    BatchCurationItem(
                        candidate_id=candidate_id,
                        block_id=str(candidate.block_id) if candidate.block_id else None,
                        excerpt=_edge_source_excerpt(candidate, store),
                        prompt=prebuilt_prompt,
                        trace_context=relation_context,
                        single_call=_relation_call,
                    )
                )
            relation_prepass.append(
                (
                    candidate,
                    payload,
                    candidate_id,
                    relation_kind,
                    src_live,
                    dst_live,
                    endpoint_reason,
                )
            )
        # ADR 0015 D5a: same ordered-prefix stream as the node phase — each
        # relation's ledger write + gate decision lands as soon as its verdict
        # is available (the D4 batch branch streams per batch too; see the
        # node phase note).
        relation_pair_iter: Iterator[tuple[CuratorVerdict, dict[str, Any] | None]]
        if not consolidation.relation_curator_enabled:
            relation_pair_iter = iter(())
        elif curation_batch_size > 1:
            assert relation_provider is not None
            assert relation_curator is not None
            assert relation_resolved is not None
            # Relation batches are capped (RELATION_BATCH_MAX) where the size is
            # consumed. Surfaced on the event stream, never silent: an operator
            # who configured 32 must be able to see that relations went out 4 at
            # a time — and, with curation_max_concurrent > 1, in parallel.
            relation_batch_size = effective_curation_batch_size(
                curation_batch_size, relation=True
            )
            _relation_batch_notice = curation_batch_notice(curation_batch_size)
            if _relation_batch_notice:
                _LOG.info("relation curation batch: %s", _relation_batch_notice)
                _event(
                    "relation_batch_capped",
                    _relation_batch_notice,
                    {
                        "configured_batch_size": curation_batch_size,
                        "batch_size": relation_batch_size,
                        "candidates": len(relation_batch_items),
                    },
                )
            relation_pair_iter = iter_batched_curation(
                relation_batch_items,
                provider=relation_provider,
                system_prompt=relation_curator.system_prompt,
                relation=True,
                registry_block=predicate_registry_block,
                batch_size=relation_batch_size,
                max_concurrent=curation_effective_max_concurrent(cfg),
                timeout_s=consolidation.curation_call_timeout_s,
                temperature=relation_resolved.temperature,
                max_tokens=relation_resolved.max_tokens,
                top_p=relation_resolved.top_p,
                top_k=relation_resolved.top_k,
                min_p=relation_resolved.min_p,
                presence_penalty=relation_resolved.presence_penalty,
                enable_thinking=relation_resolved.enable_thinking,
                set_trace_context=_set_curation_trace_context,
                fallback_runner=lambda calls: _fan_out_verdicts(
                    calls,
                    max_concurrent=curation_effective_max_concurrent(cfg),
                    timeout_s=consolidation.curation_call_timeout_s,
                ),
            )
        else:
            relation_pair_iter = (
                (verdict, None)
                for verdict in _iter_fan_out_verdicts(
                    relation_curation_calls,
                    max_concurrent=curation_effective_max_concurrent(cfg),
                    timeout_s=consolidation.curation_call_timeout_s,
                )
            )

        def _endpoint_decision(ref: str) -> EndpointDecision:
            node = node_context.get(ref)
            primitive_type = node.type if node is not None else None
            if node is None:
                stored = store.get_node(ref)
                primitive_type = stored.type if stored is not None else None
            if ref in identity_review_intents:
                type_status = "conflict"
            elif primitive_type not in _IDENTITY_PRIMITIVES:
                type_status = "ambiguous"
                primitive_type = None
            else:
                type_status = "accepted"
            return EndpointDecision(
                entity_id=ref,
                type_status=type_status,
                primitive_type=primitive_type,
                live=_edge_ref_live(ref),
            )

        def _queue_mapping_conflict(
            proposal: PredicateProposal,
            predicate: str,
        ) -> PredicateAdmissionDecision:
            return PredicateAdmissionDecision(
                reason="queue_mapping_conflict",
                state="queued",
                raw_predicate=proposal.raw_predicate,
                predicate=predicate,
                subject_id=proposal.subject_id,
                object_id=proposal.object_id,
                object_literal=proposal.object_literal,
                swapped=False,
            )

        def _queue_direction_conflict(
            proposal: PredicateProposal,
            predicate: str,
        ) -> PredicateAdmissionDecision:
            return PredicateAdmissionDecision(
                reason="queue_direction_conflict",
                state="queued",
                raw_predicate=proposal.raw_predicate,
                predicate=predicate,
                subject_id=proposal.subject_id,
                object_id=proposal.object_id,
                object_literal=proposal.object_literal,
                swapped=False,
            )

        def _admit_predicate_safe(
            proposal: PredicateProposal,
        ) -> PredicateAdmissionDecision:
            """``admit_predicate`` validates the whole ``exact_mappings``
            snapshot on every call, so one off-graph predicate alias record
            that predates normalization (or was hand-edited) into a
            non-``predicate_label_key``-normalized label makes it raise
            ``ValueError`` for every relation in the run, not just the one
            touching that alias. Queue this one relation for review instead
            of letting that corruption abort the whole ``remember()`` run.

            The queued decision falls back to ``proposal.raw_predicate``
            (never empty — enforced by ``PredicateProposal``) rather than an
            empty predicate: the D7 gate input downstream resolves an empty
            ``admission.predicate`` to the sentinel ``"unknown"`` while the
            admission decision itself would stay ``""``, and that mismatch
            trips ``PinnedRelationProposal``'s own admission/gate parity
            check.
            """
            try:
                return admit_predicate(
                    proposal,
                    registry=predicate_records,
                    exact_mappings=predicate_aliases,
                    inverse_mappings=predicate_inverse_mappings,
                )
            except ValueError:
                return _queue_mapping_conflict(proposal, proposal.raw_predicate)

        def _would_mint(
            admission: PredicateAdmissionDecision,
            verdict: CuratorVerdict,
        ) -> bool:
            """The exact precondition under which this run mints a novel label.

            One definition, two callers: ``_provisional_record`` (which does the
            minting) and the ingest-time resolver gate (which must fire on
            exactly the same relations, never a superset). Inlining it twice is
            how the two drift.
            """

            return not (
                admission.reason != "queue_unregistered"
                or verdict.action != "commit"
                or not verdict.predicate_supported
                or verdict.unsupported_inference
                or verdict.structural_noise
                or not verdict.predicate_definition
                or verdict.predicate_direction == "unknown"
            )

        def _provisional_record(
            proposal: PredicateProposal,
            admission: PredicateAdmissionDecision,
            verdict: CuratorVerdict,
            candidate_id: str,
        ) -> PredicateRecord | None:
            if not _would_mint(admission, verdict):
                return None
            from datetime import UTC, datetime

            return PredicateRecord(
                label=admission.predicate,
                lifecycle="provisional",
                definition=verdict.predicate_definition,
                direction=verdict.predicate_direction,
                symmetric=(verdict.predicate_direction == "symmetric"),
                signatures=(),
                support_count=0,
                samples=(),
                confidence=verdict.confidence,
                provenance=PredicateDecisionProvenance(
                    source="model",
                    decision_id=f"relation-curator:{candidate_id}:{admission.predicate}",
                    judge_model=str(getattr(relation_provider, "model", "unknown")),
                    prompt_version=RELATION_CURATOR_EVIDENCE_VERSION,
                    semantic_policy_fingerprint=(fingerprints.semantic_policy_fingerprint),
                    created_at=datetime.now(UTC).isoformat(),
                ),
            )

        def _resolution_from_row(row: dict[str, Any]) -> PredicateResolution | None:
            """Rebuild a prior run's resolution from its durable ledger row."""

            payload = row.get("payload")
            verdict = str(row.get("verdict") or "")
            if not isinstance(payload, dict) or verdict not in {
                "same",
                "inverse",
                "narrower",
                "distinct",
            }:
                return None
            try:
                score = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            return PredicateResolution(
                verdict=verdict,  # type: ignore[arg-type]
                target=str(payload.get("target") or ""),
                canonical=str(payload.get("canonical") or ""),
                confidence=score,
                reason=str(row.get("reason") or ""),
            )

        def _resolve_novel_predicate(
            candidate: EdgeCandidate,
            candidate_id: str,
            admission: PredicateAdmissionDecision,
            verdict: CuratorVerdict,
            *,
            may_call: bool,
        ) -> PredicateResolution | None:
            """Resolve ONE novel predicate label against the live registry.

            Memoized per normalized label: a label proposed by five relations
            costs one call, not five. With ``may_call=False`` (a synthetic or
            replayed verdict) this consults the memo and the prior run's durable
            ledger rows and returns ``None`` rather than dialing anything, so a
            resumed run reproduces its first attempt's folds byte-for-byte while
            issuing zero LLM calls (ADR 0015 D5b). Every other failure mode
            resolves ``distinct``, which is the pre-existing mint-and-queue
            status quo, not a new risk.
            """

            label = admission.predicate
            predicate_proposal_counts[label] = predicate_proposal_counts.get(label, 0) + 1
            memo = predicate_resolutions.get(label)
            if memo is not None:
                return memo
            request = PredicateResolutionRequest(
                proposed_label=label,
                proposed_definition=verdict.predicate_definition,
                proposed_direction=verdict.predicate_direction,
                source_excerpt=_edge_source_excerpt(candidate, store),
                # The LIVE in-run registry, so a fold is binding against labels
                # this same run already minted, not only against start state.
                incumbents=tuple(predicate_records[key] for key in sorted(predicate_records)),
            )
            prior_row = prior_predicate_resolutions.get(f"predicate:{label}")
            replayed = False
            resolution: PredicateResolution | None = None
            if prior_row is not None:
                resolution = _resolution_from_row(prior_row)
                replayed = resolution is not None
            if resolution is None:
                if not may_call or predicate_resolver is None:
                    return None
                resolution = predicate_resolver.resolve(request)
            predicate_resolutions[label] = resolution
            incumbent = predicate_records.get(resolution.target)
            alias_record = (
                to_alias_record(
                    request,
                    resolution,
                    incumbent=incumbent,
                    proposed_count=predicate_proposal_counts[label],
                    judge_model=str(getattr(predicate_resolver, "judge_model", "")),
                )
                if incumbent is not None
                else None
            )
            if alias_record is not None:
                if folds_onto_incumbent(resolution, label):
                    # The `auto` exact_match record — the ONLY status
                    # `alias_map` acts on, so it is binding on every later run.
                    # It is NOT staged here: this fold has not yet passed the
                    # direction guard in `_admit_relation`, and a guard that
                    # queues the relation must not leave a durable, prospective
                    # fold behind for the next run to apply silently.
                    deferred_alias_records[label] = alias_record
                else:
                    # `inverse`, `narrower` and supersession records are all
                    # `queued`, which `alias_map` ignores by construction, so
                    # they bind nothing and can be staged immediately.
                    pending_alias_records[alias_record.id] = alias_record
            ledger.record_comparison(
                run_id,
                candidate_id=f"predicate:{label}",
                method="predicate_resolution",
                verdict=resolution.verdict,
                score=resolution.confidence,
                reason=resolution.reason,
                payload={
                    # The model's OWN label, kept explicitly: re-admitting under
                    # a folded target overwrites `admission.raw_predicate`, the
                    # only other place it reaches the D6 trace.
                    "proposed_label": label,
                    "proposed_definition": verdict.predicate_definition,
                    "proposed_direction": verdict.predicate_direction,
                    "target": resolution.target,
                    "canonical": resolution.canonical,
                    "target_registered": incumbent is not None,
                    "incumbent_support": (
                        incumbent.support_count if incumbent is not None else None
                    ),
                    "gate": FOLD_CONFIDENCE_GATE,
                    # The RESOLVER's decision, not proof that a fold landed: this
                    # row is written once per label at resolution time, and the
                    # direction guard in `_admit_relation` can still reject the
                    # re-admission afterwards, in which case no alias record is
                    # staged. Count fold RATE from `aliases.json`, not from here.
                    "folded": folds_onto_incumbent(resolution, label) and incumbent is not None,
                    "alias_record_id": (alias_record.id if alias_record is not None else None),
                    "alias_record_status": (
                        alias_record.status if alias_record is not None else None
                    ),
                    "block_id": candidate.block_id,
                    "first_candidate_id": candidate_id,
                    "replayed": replayed,
                    **(
                        {"provider_retries": [dict(r) for r in resolution.provider_retries]}
                        if resolution.provider_retries
                        else {}
                    ),
                },
            )
            return resolution

        def _admit_relation(
            candidate: EdgeCandidate,
            candidate_id: str,
            verdict: CuratorVerdict,
            *,
            resolution_mode: str = "off",
        ) -> tuple[EdgeCandidate, PredicateAdmissionDecision]:
            proposed_predicate = verdict.canonical_predicate.strip() or candidate.type.strip()
            admission_candidate = candidate.model_copy(update={"type": proposed_predicate})
            literal_inverse_conflict = (
                candidate.dst_literal is not None and verdict.inverse_direction_required
            )
            proposal = PredicateProposal(
                raw_predicate=proposed_predicate,
                subject_id=candidate.src_ref,
                object_id=(candidate.dst_ref if candidate.dst_literal is None else None),
                object_literal=candidate.dst_literal,
                # A model can return an internally inconsistent inverse flag for
                # a literal claim. PredicateProposal remains strict; this caller
                # converts that one relation to review instead of aborting the
                # entire ingest.
                apply_confirmed_inverse=(
                    verdict.inverse_direction_required and not literal_inverse_conflict
                ),
            )
            admission = _admit_predicate_safe(proposal)
            if literal_inverse_conflict and admission.action != "reject":
                return admission_candidate, _queue_direction_conflict(
                    proposal,
                    admission.predicate,
                )
            # ADR 0040 D6a. The only point in the run where the proposed label,
            # the curator's own `predicate_definition`, the direction, the
            # inverse flag, the source excerpt and the full in-run registry are
            # all in scope at once — and strictly upstream of the mint.
            folded_novel_label: str | None = None
            if resolution_mode != "off" and _would_mint(admission, verdict):
                resolution = _resolve_novel_predicate(
                    candidate,
                    candidate_id,
                    admission,
                    verdict,
                    may_call=(resolution_mode == "call"),
                )
                if (
                    resolution is not None
                    and folds_onto_incumbent(resolution, admission.predicate)
                    and resolution.target in predicate_records
                ):
                    folded_novel_label = admission.predicate
                    # Re-admit under the incumbent, and move `admission_candidate`
                    # with it. The two must not drift: `_RelationEntry` enforces
                    # `candidate.type == pinned_proposal.raw_predicate`
                    # (consolidate/review_queue.py:295), and the pinned proposal
                    # carries the POST-fold `admission.raw_predicate`. Leaving the
                    # pre-fold label on the candidate is invisible while the
                    # relation commits — the commit path rebuilds the semantic
                    # candidate from `admission.predicate` — and aborts the whole
                    # ingest with "relation candidate type and raw predicate
                    # differ" the moment anything downstream QUEUES the folded
                    # relation instead, which the direction guard below does by
                    # design. The model's own proposed label is not lost: the
                    # `predicate_resolution` ledger row keeps it explicitly.
                    proposal = PredicateProposal(
                        raw_predicate=resolution.target,
                        subject_id=proposal.subject_id,
                        object_id=proposal.object_id,
                        object_literal=proposal.object_literal,
                        apply_confirmed_inverse=proposal.apply_confirmed_inverse,
                    )
                    admission = _admit_predicate_safe(proposal)
                    admission_candidate = admission_candidate.model_copy(
                        update={"type": admission.raw_predicate}
                    )
            prototype = _provisional_record(
                proposal,
                admission,
                verdict,
                candidate_id,
            )
            if prototype is not None:
                existing = predicate_records.get(prototype.label)
                if existing is None:
                    predicate_records[prototype.label] = prototype
                elif (
                    existing.lifecycle != "provisional"
                    or existing.definition.casefold() != prototype.definition.casefold()
                    or existing.direction != prototype.direction
                    or existing.symmetric is not prototype.symmetric
                ):
                    return admission_candidate, _queue_mapping_conflict(
                        proposal,
                        prototype.label,
                    )
                admission = _admit_predicate_safe(proposal)
            elif (
                admission.action.startswith("commit")
                and not admission.swapped
                # Widened from `== "provisional"`: a CANONICAL incumbent whose
                # direction disagrees with the curator's must queue too. This is
                # the single choke point for direction drift — the planned-record
                # loop keeps `base.direction`/`base.definition` verbatim, so a
                # symmetric relation would otherwise commit as directed with no
                # trace. It also covers the fold path above, which is why the
                # resolver does NOT re-check direction itself: one rule, one place.
                #
                # The widened arm fires on genuine CONFLICT only. `unknown` is
                # `CuratorVerdict.predicate_direction`'s own default (curator.py
                # :325) and means the curator asserted nothing about direction —
                # absence of evidence, which must not queue a relation whose
                # predicate the vault already governs. (Whether absence should
                # queue on the pre-existing `provisional` arm is a separate,
                # older question; this change deliberately does not touch it.)
                and (
                    admission.state == "provisional"
                    or (
                        admission.state == "canonical"
                        and verdict.predicate_direction != "unknown"
                    )
                )
            ):
                existing = predicate_records.get(admission.predicate)
                if existing is not None and (
                    existing.direction != verdict.predicate_direction
                    or existing.symmetric is not (verdict.predicate_direction == "symmetric")
                ):
                    admission = _queue_mapping_conflict(
                        proposal,
                        admission.predicate,
                    )
            # The fold survived every gate above and this relation commits under
            # the incumbent, so — and only so — the fold becomes durable. A fold
            # the direction guard just turned into `queue_mapping_conflict`
            # stages nothing, which is the whole point of holding it back: the
            # alias record is prospective and binding, so shipping one for a
            # rejected fold would apply on the NEXT run what this run refused.
            if folded_novel_label is not None and admission.action.startswith("commit"):
                deferred = deferred_alias_records.get(folded_novel_label)
                if deferred is not None:
                    pending_alias_records[deferred.id] = deferred
            return admission_candidate, admission

        # Only committed relations own a materialization trace. Queue/reject
        # decisions remain durable in the ledger/review intents, but must never
        # overwrite the D6/D7 trace for an accepted candidate with the same
        # canonical id.
        relation_pinned: dict[str, tuple[Any, ...]] = {}
        committed_relation_ids: set[str] = set()
        relation_review_intents: dict[str, tuple[Any, ...]] = {}
        for (
            candidate,
            payload,
            candidate_id,
            relation_kind,
            src_live,
            dst_live,
            endpoint_reason,
        ) in relation_prepass:
            synthetic_verdict = relation_synthetic_verdicts.get(candidate_id)
            replay_record = relation_replays.get(candidate_id)
            if synthetic_verdict is not None:
                verdict = synthetic_verdict
                relation_meta = {"synthetic": True}
                relation_method = relation_synthetic_methods.get(candidate_id, "semantic_prefilter")
            elif replay_record is not None:
                # ADR 0015 D5b: replayed verdict — no LLM call happened.
                verdict = _replay_verdict(replay_record, relation=True)
                relation_meta = _replay_meta(
                    replay_record,
                    mode=replay_comparison_method,
                )
                if candidate_id in semantic_relation_replays:
                    relation_meta["semantic_candidate_replay"] = True
                if candidate_id in derived_relation_replays:
                    relation_meta["derived_candidate_replay"] = True
                relation_method = replay_comparison_method
            else:
                verdict, relation_meta = next(relation_pair_iter)
                relation_method = "relation_curator"
            relation_counts[verdict.action] += 1
            admission_candidate, admission = _admit_relation(
                candidate,
                candidate_id,
                verdict,
                # ADR 0015 D5b: a synthetic or replayed verdict issued no LLM
                # call, and a resumed run must issue none either — but it still
                # consults this run's prior `predicate_resolution` rows, so a
                # resume reproduces the first attempt's folds exactly.
                resolution_mode=(
                    "call" if relation_method == "relation_curator" else "replay"
                ),
            )
            predicate_status = (
                admission.state
                if admission.state
                in {"canonical", "provisional", "queued", "placeholder", "structural_noise"}
                else "placeholder"
            )
            subject = _endpoint_decision(admission.subject_id)
            if admission.object_literal is not None:
                relation_object = LiteralObject(admission.object_literal)
            else:
                assert admission.object_id is not None
                relation_object = TopologyObject(_endpoint_decision(admission.object_id))
            gate_input = RelationGateInput(
                subject=subject,
                predicate=PinnedPredicateAdmission(
                    predicate=admission.predicate or "unknown",
                    status=predicate_status,
                ),
                object=relation_object,
                grounding=SourceGrounding(
                    block_id=candidate.block_id,
                    subject_supported=verdict.subject_supported,
                    predicate_supported=verdict.predicate_supported,
                    object_supported=verdict.object_supported,
                    direction_supported=verdict.direction_supported,
                    unsupported_inference=verdict.unsupported_inference,
                ),
                structural_noise=verdict.structural_noise,
                redundant=verdict.redundant,
            )
            gate_decision = decide_relation(gate_input)
            semantic_candidate = admission_candidate
            if gate_decision.action == "commit" and admission.action.startswith("commit"):
                semantic_candidate = admission_candidate.model_copy(
                    update={
                        "type": admission.predicate,
                        "src_ref": admission.subject_id,
                        "dst_ref": admission.object_id or "",
                        "dst_literal": admission.object_literal,
                        "confidence": verdict.confidence,
                    }
                )
            semantic_payload = semantic_candidate.model_dump(mode="json")
            semantic_candidate_id = edge_candidate_id(semantic_payload)
            derived = semantic_candidate_id != candidate_id
            ledger.record_comparison(
                run_id,
                candidate_id=candidate_id,
                method=relation_method,
                verdict=verdict.action,
                score=verdict.confidence,
                reason=verdict.reason,
                payload={
                    "relation_kind": relation_kind,
                    "src_live": src_live,
                    "dst_live": dst_live,
                    "proposed_terminal_action": gate_decision.action,
                    "canonical_candidate_id": (semantic_candidate_id if derived else None),
                    **_relation_verdict_evidence(verdict),
                    **_verdict_telemetry(verdict),
                    # ADR 0015 D4: batch provenance (batch_id/batch_size) —
                    # present only when the verdict came from a batch call.
                    **(relation_meta or {}),
                    **payload,
                },
            )
            if derived:
                relation_reviewed_ids.add(semantic_candidate_id)
                canonicalized_edge_candidates.append(semantic_candidate)
                if semantic_candidate_id not in resumed_candidate_rows:
                    ledger.record_candidate(
                        run_id,
                        candidate_id=semantic_candidate_id,
                        candidate_kind="edge",
                        state="proposed",
                        payload={
                            **semantic_payload,
                            "derived": True,
                            "derived_from": candidate_id,
                            "derivation_reason": ("D6 pinned predicate and endpoint direction"),
                        },
                    )
                relation_terminal[candidate_id] = {
                    "state": "superseded",
                    "reason": "D6 pinned predicate and endpoint direction",
                    "target_ref": semantic_candidate_id,
                }
            terminal_id = semantic_candidate_id
            gate_reason = gate_decision.reason
            ledger.record_comparison(
                run_id,
                candidate_id=terminal_id,
                method="semantic_relation_gate",
                verdict=gate_decision.action,
                score=verdict.confidence,
                reason=gate_reason,
                payload={
                    "d6_reason": admission.reason,
                    "d6_state": admission.state,
                    "raw_predicate": admission.raw_predicate,
                    "predicate": admission.predicate,
                    "subject_id": admission.subject_id,
                    "object_id": admission.object_id,
                    "object_literal": admission.object_literal,
                    "swapped": admission.swapped,
                    "d7_reason": gate_reason,
                    "endpoint_reason": endpoint_reason,
                    "useful": verdict.useful,
                    **semantic_payload,
                },
            )
            pinned_relation = (
                semantic_candidate,
                admission,
                gate_input,
                gate_decision,
                verdict,
            )
            if gate_decision.action == "commit":
                if terminal_id not in committed_relation_ids:
                    committed_relation_ids.add(terminal_id)
                    relation_pinned[terminal_id] = pinned_relation
                    curated_edge_candidates.append(semantic_candidate)
                    relation_terminal.pop(terminal_id, None)
                    relation_review_intents.pop(terminal_id, None)
            elif terminal_id not in committed_relation_ids and gate_decision.action == "queue":
                relation_terminal[terminal_id] = {
                    "state": "queued",
                    "reason": gate_reason,
                }
                review_reason = gate_reason
                proposal = PinnedRelationProposal(
                    admission=admission,
                    gate_input=gate_input,
                    predicate_definition=verdict.predicate_definition,
                    predicate_direction=verdict.predicate_direction,
                    inverse_direction_required=verdict.inverse_direction_required,
                    useful=verdict.useful,
                )
                relation_review_intents[terminal_id] = (
                    semantic_candidate,
                    review_reason,
                    proposal,
                )
            elif terminal_id not in committed_relation_ids:
                relation_terminal[terminal_id] = {
                    "state": "dead_lettered",
                    "reason": gate_reason,
                }
            relation_reviewed += 1
            _emit_substage("committing")
            _emit_relation_progress(relation_reviewed)

        edge_candidates, exact_edge_folds = _collapse_exact_edge_candidates(
            curated_edge_candidates,
            packs=cfg.packs,
            predicate_aliases=predicate_aliases,
        )
        extra_mention_anchors: dict[str, list[EdgeCandidate]] = {}
        for folded_candidate, survivor_candidate in exact_edge_folds:
            folded_payload = folded_candidate.model_dump(mode="json")
            survivor_payload = survivor_candidate.model_dump(mode="json")
            folded_id = edge_candidate_id(folded_payload)
            survivor_id = edge_candidate_id(survivor_payload)
            if folded_id == survivor_id:
                continue
            extra_mention_anchors.setdefault(survivor_id, []).append(folded_candidate)
            reason = "exact duplicate relationship folded within run"
            relation_terminal[folded_id] = {
                "state": "superseded",
                "reason": reason,
                "target_ref": survivor_id,
            }
            ledger.record_comparison(
                run_id,
                candidate_id=folded_id,
                method="exact_edge_batch",
                verdict="merge",
                score=1.0,
                target_ref=survivor_id,
                reason=reason,
                payload={
                    "survivor": survivor_payload,
                    "folded": folded_payload,
                },
            )

        planned_predicate_records: list[tuple[PredicateRecord | None, PredicateRecord]] = []
        predicate_plan_current = dict(predicate_records_at_start)
        for candidate in edge_candidates:
            candidate_payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(candidate_payload)
            pinned = relation_pinned.get(candidate_id)
            if pinned is None:
                raise RuntimeError(
                    f"committed relation lacks pinned D6/D7 evidence: {candidate_id}"
                )
            _candidate, admission, _gate_input, _gate_decision, verdict = pinned
            before = predicate_plan_current.get(admission.predicate)
            base = before or predicate_records.get(admission.predicate)
            if base is None:
                raise RuntimeError(
                    f"committed predicate lacks registry record: {admission.predicate!r}"
                )
            subject_type = _endpoint_decision(candidate.src_ref).primitive_type
            object_type = (
                "literal"
                if candidate.dst_literal is not None
                else _endpoint_decision(candidate.dst_ref).primitive_type
            )
            if subject_type is None or object_type is None:
                raise RuntimeError(
                    f"committed relation lacks accepted endpoint types: {candidate_id}"
                )
            signature_counts = {
                (signature.subject_type, signature.object_type): signature.count
                for signature in base.signatures
            }
            signature_key = (subject_type, object_type)
            signature_counts[signature_key] = signature_counts.get(signature_key, 0) + 1
            signatures = tuple(
                PredicateTypeSignature(
                    subject_type=key[0],
                    object_type=key[1],
                    count=count,
                )
                for key, count in sorted(signature_counts.items())
            )
            claim_id = (
                semantic_claim_id(
                    candidate.src_ref,
                    candidate.type,
                    literal=candidate.dst_literal,
                )
                if candidate.dst_literal is not None
                else semantic_claim_id(
                    candidate.src_ref,
                    candidate.type,
                    object_id=candidate.dst_ref,
                )
            )
            sample = PredicateEvidenceSample(
                claim_id=claim_id,
                source_id=str(candidate.block_id),
            )
            samples = list(base.samples)
            if sample not in samples and len(samples) < 20:
                samples.append(sample)
            after = PredicateRecord(
                label=base.label,
                lifecycle=base.lifecycle,
                definition=base.definition,
                direction=base.direction,
                symmetric=base.symmetric,
                signatures=signatures,
                support_count=base.support_count + 1,
                samples=tuple(samples),
                confidence=max(base.confidence, verdict.confidence),
                provenance=base.provenance,
            )
            planned_predicate_records.append((before, after))
            predicate_plan_current[after.label] = after
            predicate_records[after.label] = after
        all_edge_candidates.extend(canonicalized_edge_candidates)
        _emit_relation_progress(relation_reviewed, force=True)
        _event("relation_curator", "Relationship curator decisions", relation_counts)
        relation_audit_counts = {"commit": 0, "queue": 0, "abstain": 0}
        superseded_edge_candidates: dict[str, dict[str, Any]] = {}
        raw_node_context = {candidate.candidate_id: candidate for candidate in raw_node_candidates}
        for candidate in raw_edge_candidates:
            payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(payload)
            if candidate_id in relation_reviewed_ids:
                continue
            relation_kind = "claim" if candidate.dst_literal is not None else "edge"
            relation_context = {
                "candidate_id": candidate_id,
                "relation_kind": relation_kind,
                "type": candidate.type,
                "src_ref": candidate.src_ref,
                "dst_ref": candidate.dst_ref,
                "dst_literal": candidate.dst_literal,
                "audit_only": True,
            }
            if not consolidation.relation_curator_enabled:
                verdict = CuratorVerdict(
                    action="queue",
                    confidence=0.0,
                    reason="relationship curator disabled; queued for review",
                )
                llm_skipped = True
            elif consolidation.audit_superseded_relations_with_llm:
                assert relation_curator is not None
                verdict = relation_curator.curate(
                    candidate,
                    store=store,
                    node_candidates=raw_node_context,
                    proposed_action=(
                        "  resolver_proposal: supersede this raw relationship before graph write\n"
                        "  reason: relationship removed or remapped before the final relation gate\n"
                        "  curator_role: evaluate whether the source excerpt supports the raw "
                        "relationship and whether superseding/remapping it is semantically safe"
                    ),
                )
                llm_skipped = False
            else:
                verdict = CuratorVerdict(
                    action="commit",
                    confidence=0.0,
                    reason="LLM audit disabled for superseded relationship candidates",
                )
                llm_skipped = True
            relation_audit_counts[verdict.action] += 1
            terminal_state = "superseded" if verdict.action == "commit" else "queued"
            ledger.record_comparison(
                run_id,
                candidate_id=candidate_id,
                method="relation_curator",
                verdict=verdict.action,
                score=verdict.confidence,
                reason=verdict.reason,
                payload={
                    "relation_kind": relation_kind,
                    "audit_only": True,
                    "audit_reason": "relationship removed or remapped before the final relation gate",
                    "proposed_terminal_action": "supersede_candidate",
                    "llm_skipped": llm_skipped,
                    **_verdict_telemetry(verdict),
                    **payload,
                },
            )
            superseded_payload = {
                "candidate": payload,
                "reason": "relationship removed or remapped before the final relation gate",
                "terminal_state": terminal_state,
                "llm_skipped": llm_skipped,
                "curator_verdict": {
                    "action": verdict.action,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                },
            }
            superseded_edge_candidates[candidate_id] = superseded_payload
            if terminal_state == "queued":
                _admission_candidate, admission = _admit_relation(
                    candidate,
                    candidate_id,
                    verdict,
                )
                predicate_status = (
                    admission.state
                    if admission.state
                    in {
                        "canonical",
                        "provisional",
                        "queued",
                        "placeholder",
                        "structural_noise",
                    }
                    else "placeholder"
                )
                subject = _endpoint_decision(admission.subject_id)
                relation_object = (
                    LiteralObject(admission.object_literal)
                    if admission.object_literal is not None
                    else TopologyObject(_endpoint_decision(str(admission.object_id)))
                )
                gate_input = RelationGateInput(
                    subject=subject,
                    predicate=PinnedPredicateAdmission(
                        predicate=admission.predicate or "unknown",
                        status=predicate_status,
                    ),
                    object=relation_object,
                    grounding=SourceGrounding(
                        block_id=candidate.block_id,
                        subject_supported=verdict.subject_supported,
                        predicate_supported=verdict.predicate_supported,
                        object_supported=verdict.object_supported,
                        direction_supported=verdict.direction_supported,
                        unsupported_inference=verdict.unsupported_inference,
                    ),
                    structural_noise=verdict.structural_noise,
                    redundant=verdict.redundant,
                )
                relation_review_intents[candidate_id] = (
                    candidate,
                    "queue_supersession_audit",
                    PinnedRelationProposal(
                        admission=admission,
                        gate_input=gate_input,
                        predicate_definition=verdict.predicate_definition,
                        predicate_direction=verdict.predicate_direction,
                        inverse_direction_required=verdict.inverse_direction_required,
                        useful=verdict.useful,
                    ),
                )
        if any(relation_audit_counts.values()):
            _event(
                "relation_curator_audit",
                "Relationship curator audit decisions",
                relation_audit_counts,
            )

        relationship_supported_refs: set[str] = set()
        for candidate in edge_candidates:
            relationship_supported_refs.add(candidate.src_ref)
            if candidate.dst_literal is None and candidate.dst_ref:
                relationship_supported_refs.add(candidate.dst_ref)

        # ── Fix A: realize promoted structural endpoints in the node gate.
        # Each was auto-promoted at the relation endpoint gate so its literal
        # Claim or topology edge could be curated. Override its node outcome to
        # commit and stamp it LOW-SALIENCE so it anchors the relationship
        # without surfacing as an entity. The relationship-liveness gate below
        # still applies: a promoted endpoint whose relationship the relation
        # curator did NOT commit is not in relationship_supported_refs, so it
        # is queued back — promotion never mints an orphan node.
        if promoted_endpoint_ids:
            promote_conf = max(gate_config.auto_commit_threshold, 0.0)
            realized_pairs: list[tuple[NodeCandidate, ResolveOutcome]] = []
            for candidate, outcome in pairs:
                if candidate.candidate_id in promoted_endpoint_ids and not outcome.contradicted:
                    candidate = candidate.model_copy(
                        update={"facets": {**candidate.facets, **LOW_SALIENCE_FACET}}
                    )
                    outcome = ResolveOutcome(
                        correlations=outcome.correlations,
                        confidence=promote_conf,
                    )
                realized_pairs.append((candidate, outcome))
            pairs = realized_pairs

        liveness_filtered_pairs: list[tuple[NodeCandidate, ResolveOutcome]] = []
        liveness_forced_review = 0
        for candidate, outcome in pairs:
            would_commit, _ = decide(outcome, gate_config)
            already_live = store.get_node(candidate.candidate_id) is not None
            relationship_supported = candidate.candidate_id in relationship_supported_refs
            if would_commit and not already_live and not relationship_supported:
                liveness_forced_review += 1
                reason = (
                    "candidate has no accepted relationship or literal claim after "
                    "relationship curation"
                )
                ledger.record_comparison(
                    run_id,
                    candidate_id=candidate.candidate_id,
                    method="relationship_liveness_gate",
                    verdict="queue",
                    score=0.0,
                    reason=reason,
                    payload={
                        "previous_confidence": outcome.confidence,
                        "accepted_relationships": len(edge_candidates),
                    },
                )
                liveness_filtered_pairs.append(
                    (
                        candidate,
                        ResolveOutcome(
                            correlations=outcome.correlations,
                            confidence=0.0,
                        ),
                    )
                )
                continue
            liveness_filtered_pairs.append((candidate, outcome))
        pairs = liveness_filtered_pairs
        confidences = {cand.candidate_id: outcome.confidence for cand, outcome in pairs}
        if liveness_forced_review:
            _event(
                "relationship_liveness_gate",
                "Queued candidates without accepted relationships",
                {"queued": liveness_forced_review},
            )

        # Last cooperative boundary: from the durable plan onward, applying the
        # plan, claim minting, and ledger close form one write tail that must
        # finish.  The plan is derived from the pure gate decisions, not from
        # post-commit outcomes, so a failed/dead-lettered apply remains an
        # observable divergence between intent and receipt.
        _check_cancelled()
        # This run owns the immutable policy and decision snapshots captured
        # before extraction. Concurrent sidefile changes belong to the next
        # run; recomputing the broad fingerprint here would either mix policies
        # or abort work correctly adjudicated against the pinned snapshot.
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        valid_as_of = _dt.now(_tz.utc).date().isoformat()
        planning_source_bytes = _read_source_bytes(source)
        if planning_source_bytes is None:
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="source bytes became unreadable before semantic plan sealing",
            )
        source_generation_sha256 = hashlib.sha256(planning_source_bytes).hexdigest()
        source_binding = _source_binding(
            store,
            source,
            document.id,
            source_bytes=planning_source_bytes,
        )
        _ask_min_conf = cfg.llm.ask.min_claim_confidence
        min_claim_confidence = (
            _ask_min_conf if _ask_min_conf is not None else _ASK_MIN_CLAIM_CONFIDENCE_DEFAULT
        )
        plan_operations: list[dict[str, Any]] = []
        # ADR 0040 D6a: the durable half of ingest-time predicate resolution.
        # Written HERE as a plan operation rather than mid-relation-loop: the
        # alias sidefile is content-hashed into the semantic policy payload
        # against a fingerprint sealed once at bootstrap, and a mid-loop write
        # would also be invisible to `_collapse_exact_edge_candidates`, which
        # reads the alias snapshot taken before the loop.
        for alias_record in (
            pending_alias_records[key] for key in sorted(pending_alias_records)
        ):
            plan_operations.append(
                {
                    "operation": "register_predicate_alias",
                    "predicate": alias_record.subject_predicate,
                    "record": alias_record.to_json(),
                }
            )
        for expected_before, predicate_record in planned_predicate_records:
            plan_operations.append(
                {
                    "operation": "register_predicate",
                    "predicate": predicate_record.label,
                    "expected_before": (
                        expected_before.to_json() if expected_before is not None else None
                    ),
                    "record": predicate_record.to_json(),
                }
            )
        for llm_node in llm_node_artifacts:
            plan_operations.append(
                {
                    "operation": "ensure_node",
                    "node": llm_node.model_dump(mode="json"),
                    "reason": "llm_extraction_provenance",
                }
            )
        source_mention_operations: list[dict[str, Any]] = []
        for candidate, outcome in pairs:
            intended_commit, review_reason = decide(outcome, gate_config)
            candidate_node = candidate.to_node()
            plan_operations.append(
                {
                    "operation": "create_node" if intended_commit else "queue_review",
                    "candidate_kind": "node",
                    "candidate_id": candidate.candidate_id,
                    "candidate": candidate.model_dump(mode="json"),
                    "confidence": outcome.confidence,
                    "correlations": [
                        correlation.model_dump(mode="json") for correlation in outcome.correlations
                    ],
                    "reason": review_reason,
                    **({"node": candidate_node.model_dump(mode="json")} if intended_commit else {}),
                }
            )
            if intended_commit and candidate.type in {
                "Agent",
                "Activity",
                "Concept",
                "InformationObject",
                "Place",
            }:
                source_path = str((candidate.facets or {}).get("source_path") or "")
                if source_path:
                    from okto_neuron.ingest.markdown import sha256_hex

                    document_id = sha256_hex("document", source_path)
                    if store.get_node(document_id) is not None:
                        mention_edge = Edge(
                            id=sha256_hex(
                                "edge",
                                document_id,
                                "schema:mentions",
                                candidate.candidate_id,
                            ),
                            type="schema:mentions",
                            src=document_id,
                            dst=candidate.candidate_id,
                            provenance=Provenance(
                                source="migration",
                                rule_id="adr0006-source-mentions",
                            ),
                        )
                        source_mention_operations.append(
                            {
                                "operation": "ensure_source_mention",
                                "edge": mention_edge.model_dump(mode="json"),
                                "reason": "entity_source_grounding",
                            }
                        )
        for candidate_id, payload in superseded_node_candidates.items():
            candidate_payload = payload.get("candidate", {})
            operation = (
                "queue_review"
                if payload.get("terminal_state") == "queued"
                else "supersede_candidate"
            )
            plan_operations.append(
                {
                    "operation": operation,
                    "candidate_kind": "node",
                    "candidate_id": candidate_id,
                    "candidate": candidate_payload,
                    "correlations": [
                        correlation.model_dump(mode="json")
                        for correlation in (
                            superseded_node_reviews[candidate_id][1]
                            if candidate_id in superseded_node_reviews
                            else ()
                        )
                    ],
                    "target_ref": payload.get("target_ref"),
                    "reason": (
                        "low_confidence" if operation == "queue_review" else payload.get("reason")
                    ),
                }
            )
        active_edge_ids = {
            edge_candidate_id(candidate.model_dump(mode="json")) for candidate in edge_candidates
        }
        for candidate in all_edge_candidates:
            payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(payload)
            terminal = relation_terminal.get(candidate_id)
            if candidate_id in active_edge_ids:
                continue
            plan_operations.append(
                {
                    "operation": (
                        "queue_review"
                        if terminal and terminal.get("state") == "queued"
                        else "supersede_candidate"
                        if terminal and terminal.get("state") == "superseded"
                        else "dead_letter"
                    ),
                    "candidate_kind": "edge",
                    "candidate_id": candidate_id,
                    "candidate": payload,
                    "reason": terminal.get("reason") if terminal else None,
                    "target_ref": terminal.get("target_ref") if terminal else None,
                    **(
                        {
                            "review_item": {
                                "candidate": relation_review_intents[candidate_id][0].model_dump(
                                    mode="json"
                                ),
                                "reason": relation_review_intents[candidate_id][1],
                                "pinned_proposal": relation_review_intents[candidate_id][
                                    2
                                ].to_json(),
                            }
                        }
                        if candidate_id in relation_review_intents
                        else {}
                    ),
                }
            )
        planned_node_ids = {
            candidate.candidate_id
            for candidate, outcome in pairs
            if decide(outcome, gate_config)[0]
        }
        planned_node_ids |= set(reconciliation.merged_into.values())
        planned_node_ids |= set(judgement.merged_into.values())
        pinned_predicates = {
            candidate_id: pinned[1].predicate
            for candidate_id, pinned in relation_pinned.items()
            if candidate_id in active_edge_ids
        }
        pinned_relation_traces = {
            candidate_id: _relation_decision_trace(pinned[1], pinned[3])
            for candidate_id, pinned in relation_pinned.items()
            if candidate_id in active_edge_ids
        }
        claim_plan = _plan_relationship_claims(
            store,
            edge_candidates,
            planned_node_ids=planned_node_ids,
            titles={candidate.candidate_id: candidate.title for candidate in node_candidates},
            confidences=confidences,
            pinned_predicates=pinned_predicates,
            pinned_relation_traces=pinned_relation_traces,
            llm=llm,
            embedder=embedder,
            vault_root=self._vault.path,
            min_claim_confidence=min_claim_confidence,
            extra_mention_anchors=extra_mention_anchors,
            asserted_at=asserted_at,
            embedding_settings=lambda: _embedding_settings(allow_cancel=False),
            on_embedding_usage=construction_cost.record_embedding,
        )
        plan_operations.extend(claim_plan.operations)
        plan_operations.extend(source_mention_operations)
        for candidate_id, payload in superseded_edge_candidates.items():
            candidate_payload = payload.get("candidate", {})
            operation = (
                "queue_review"
                if payload.get("terminal_state") == "queued"
                else "supersede_candidate"
            )
            plan_operations.append(
                {
                    "operation": operation,
                    "candidate_kind": "edge",
                    "candidate_id": candidate_id,
                    "candidate": candidate_payload,
                    "reason": payload.get("reason"),
                    **(
                        {
                            "review_item": {
                                "candidate": relation_review_intents[candidate_id][0].model_dump(
                                    mode="json"
                                ),
                                "reason": relation_review_intents[candidate_id][1],
                                "pinned_proposal": relation_review_intents[candidate_id][
                                    2
                                ].to_json(),
                            }
                        }
                        if operation == "queue_review" and candidate_id in relation_review_intents
                        else {}
                    ),
                }
            )
        correction_judge = None
        if any(operation.get("operation") == "mint_claim" for operation in plan_operations) and any(
            (node.facets or {}).get("model_id") for node in store.list_nodes(type="Claim")
        ):
            try:
                correction_judge = _incremental.make_correction_judge(
                    _tracked_provider(
                        self._get_provider("judge"),
                        label="Correction Judge",
                        context=lambda: {"stage": "claim_reconciliation"},
                    ),
                    cfg.llm.resolved("judge"),
                )
            except Exception:  # noqa: BLE001 - conservative no-correction fallback
                _LOG.exception(
                    "ADR-0022 correction judge unavailable before plan seal; "
                    "continuing without automatic supersedence for source=%s",
                    source,
                )
        reconciliation_plan = _plan_claim_reconciliation(
            store,
            source=source,
            vault_path=self._vault.path,
            incremental_plan=incremental_plan,
            ingest_cfg=ingest_cfg,
            base_operations=plan_operations,
            valid_as_of=valid_as_of,
            asserted_at=asserted_at,
            source_bytes=planning_source_bytes,
            source_generation_sha256=source_generation_sha256,
            correction_judge=correction_judge,
            should_stop=should_cancel,
        )
        _check_cancelled()
        plan_operations = list(reconciliation_plan.operations)
        extraction_outcome["construction_cost"] = construction_cost.snapshot()
        plan_id = ledger.record_commit_plan(
            run_id,
            operations=plan_operations,
            context={
                "document_id": document.id,
                "source": str(source),
                "source_binding": source_binding,
                "asserted_at": asserted_at,
                "blocks_total": blocks_total,
                "nodes_extracted": len(node_candidates),
                "edges_extracted": len(all_edge_candidates),
                "provider_error": provider_error,
                "provider_failures": provider_failures,
                "empty_after_retry_blocks": empty_after_retry_blocks,
                "llm_disabled": llm_disabled,
                "config_fingerprint": fingerprints.config_fingerprint,
                "extraction_fingerprint": fingerprints.extraction_fingerprint,
                "semantic_policy_fingerprint": (fingerprints.semantic_policy_fingerprint),
                "outcome": extraction_outcome,
            },
        )
        extraction_outcome["plan_id"] = plan_id
        sealed_plan = next(
            plan
            for plan in ledger.unreceipted_commit_plans(document_id=document.id)
            if plan.plan_id == plan_id
        )
        plan_receipts = ledger.operation_receipts(sealed_plan)
        binding_matches, binding_evidence = _source_binding_evidence(
            store,
            source,
            sealed_plan.context.get("source_binding"),
        )
        if not binding_matches:
            ledger.record_plan_abandoned(
                run_id,
                plan_id=sealed_plan.plan_id,
                plan_hash=sealed_plan.plan_hash,
                reason=str(binding_evidence["reason"]),
                evidence=binding_evidence,
            )
            ledger.finish_run(
                run_id,
                state="abandoned",
                summary={
                    "reason": "source_changed_before_semantic_apply",
                    "source_binding": binding_evidence,
                },
            )
            raise IngestError(
                source,
                vault_path=self._vault.path,
                message="source changed while the semantic plan was being sealed; retry",
            )

        # The sealed applier is the only semantic writer. It applies governed
        # predicates, nodes, review intents, Claims, lifecycle evidence, and
        # topology from pinned artifacts, recording one receipt per operation.
        review_queue = self._review_queue()
        applied_plan = _apply_sealed_semantic_plan(
            sealed_plan,
            store=store,
            ledger=ledger,
            registry=predicate_registry,
            review_queue=review_queue,
            close_plan=False,
        )
        plan_receipts = ledger.operation_receipts(sealed_plan)
        # A zero-operation plan produced by a run that did nothing at all
        # (see ``nothing_happened``) must not claim complete receipts: 0 == 0 is
        # vacuously true and would let ``consolidate.ledger``'s replay guard
        # treat the no-op as a finished run. A healthy zero-operation plan (all
        # blocks skipped as unchanged) keeps receipts_complete True.
        extraction_outcome["receipts_complete"] = (
            len(plan_receipts) == len(sealed_plan.operations) and not nothing_happened
        )
        outcomes = tuple(
            CandidateOutcome.model_validate(value) for value in applied_plan["outcomes"]
        )
        outcome_by_id = {outcome.candidate_id: outcome for outcome in outcomes}
        committed_count = sum(1 for outcome in outcomes if outcome.action == "committed")
        queued_count = sum(1 for outcome in outcomes if outcome.action == "queued")
        node_dead_lettered = sum(
            1
            for operation in sealed_plan.operations
            if operation["operation"] == "dead_letter" and operation.get("candidate_kind") == "node"
        )
        _event(
            "gate",
            "Applied sealed confidence decisions",
            {
                "committed": committed_count,
                "queued": queued_count,
                "dead_lettered": node_dead_lettered,
                "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
            },
        )
        for candidate, _outcome in pairs:
            outcome = outcome_by_id.get(candidate.candidate_id)
            if outcome is None:
                raise RuntimeError("sealed plan omitted a gated node candidate")
            ledger.record_candidate(
                run_id,
                candidate_id=candidate.candidate_id,
                candidate_kind="node",
                state=outcome.action,
                payload={
                    "candidate": candidate.model_dump(mode="json"),
                    "outcome": outcome.model_dump(mode="json"),
                },
            )
        for candidate_id, payload in superseded_node_candidates.items():
            ledger.record_candidate(
                run_id,
                candidate_id=candidate_id,
                candidate_kind="node",
                state=str(payload["terminal_state"]),
                payload=payload,
            )
        for candidate_id, payload in superseded_edge_candidates.items():
            ledger.record_candidate(
                run_id,
                candidate_id=candidate_id,
                candidate_kind="edge",
                state=str(payload["terminal_state"]),
                payload=payload,
            )

        committed_node_ids = set(applied_plan["committed_node_ids"])
        committed_node_ids |= set(reconciliation.merged_into.values())
        committed_node_ids |= set(judgement.merged_into.values())
        claims_minted = int(applied_plan["claims_minted"])
        topology_committed_edge_ids = list(applied_plan["topology_edge_ids"])
        edge_commit_results = claim_plan.edge_results
        relations_corroborated = claim_plan.relations_corroborated
        for candidate in all_edge_candidates:
            payload = candidate.model_dump(mode="json")
            candidate_id = edge_candidate_id(payload)
            terminal = relation_terminal.get(candidate_id)
            if terminal is not None and candidate_id not in active_edge_ids:
                state = str(terminal["state"])
                reason = str(terminal["reason"])
            else:
                edge_result = edge_commit_results.get(candidate_id, {})
                state = str(edge_result.get("state") or "committed")
                reason = str(edge_result.get("reason") or "topology edge accepted")
            ledger.record_candidate(
                run_id,
                candidate_id=candidate_id,
                candidate_kind="edge",
                state=state,
                payload={
                    "candidate": payload,
                    "reason": reason,
                    "claim_id": edge_commit_results.get(candidate_id, {}).get("claim_id"),
                },
            )
        relation_queued = sum(
            1 for terminal in relation_terminal.values() if terminal.get("state") == "queued"
        )
        relation_dead_lettered = sum(
            1 for terminal in relation_terminal.values() if terminal.get("state") == "dead_lettered"
        )
        total_dead_lettered = node_dead_lettered + relation_dead_lettered

        if len(plan_receipts) != len(sealed_plan.operations):
            raise RuntimeError("semantic commit plan closed with missing operation receipts")
        _event(
            "commit",
            "Committed graph changes",
            {
                "committed_node_ids": sorted(committed_node_ids),
                "claims_minted": claims_minted,
                "nodes_extracted": len(node_candidates),
                "edges_extracted": len(all_edge_candidates),
                "relationships_accepted": len(edge_candidates),
                "relationships_queued": relation_queued,
                "relationships_dead_lettered": relation_dead_lettered,
                "relations_corroborated": relations_corroborated,
                "provider_error": provider_error,
                "provider_failures": provider_failures,
                "empty_after_retry_blocks": empty_after_retry_blocks,
                "outcome": extraction_outcome,
            },
        )
        consolidation_payload = {
            "session_id": sealed_plan.plan_id,
            "committed_node_ids": sorted(committed_node_ids),
            "committed_edge_ids": topology_committed_edge_ids,
            "dead_letter": [],
        }
        claims_detached = int(applied_plan["claims_detached"])
        claims_superseded = int(applied_plan["claims_superseded"])
        claims_supersede_deferred = len(reconciliation_plan.deferred_pairs)
        resurrections = reconciliation_plan.resurrections
        artifact = list(reconciliation_plan.annotation_artifacts)
        if claims_detached or claims_superseded or claims_supersede_deferred or resurrections:
            _event(
                "claims_reconciled",
                "Reconciled — corrections superseded, removals detached",
                {
                    "claims_detached": claims_detached,
                    "claims_superseded": claims_superseded,
                    # Cross-document supersede proposals the lineage bound (Task 5)
                    # would not auto-apply — surfaced for triage, NOT hidden. The
                    # ReviewQueue is NodeCandidate-shaped and has no "apply
                    # supersede" resolution, so telemetry is the honest inbox here.
                    "claims_supersede_deferred": claims_supersede_deferred,
                    "claims_resurrected": len(resurrections),
                    # A resurrected previously-SUPERSEDED claim reverses a
                    # correction — the fact that beat it may now be the stale
                    # one (surfaced for future companion-inbox triage).
                    "stale_source": any(r.get("was_superseded") for r in resurrections),
                    "valid_as_of": valid_as_of,
                    "annotation": artifact,
                },
            )
        ledger.record_commit(
            run_id,
            plan_id=plan_id,
            result={
                "operation_receipts_complete": True,
                "operation_receipts": len(plan_receipts),
                "consolidation": consolidation_payload,
                "committed_node_ids": sorted(committed_node_ids),
                "claims_minted": claims_minted,
                "nodes_extracted": len(node_candidates),
                "edges_extracted": len(all_edge_candidates),
                "relationships_accepted": len(edge_candidates),
                "relationships_queued": relation_queued,
                "relationships_dead_lettered": relation_dead_lettered,
                "relations_corroborated": relations_corroborated,
                "claims_detached": claims_detached,
                "claims_superseded": claims_superseded,
                "claims_supersede_deferred": claims_supersede_deferred,
                "provider_error": provider_error,
                "provider_failures": provider_failures,
                "empty_after_retry_blocks": empty_after_retry_blocks,
                "outcome": extraction_outcome,
            },
        )
        post_fingerprints = self._semantic_fingerprints(
            cfg,
            ingest_config=ingest_cfg,
        )
        if provider_retry_counts:
            # Additive ADR 0039 D6 outcome field: a document whose judge or
            # curator calls needed a retry says so without reading the ledger.
            extraction_outcome["provider_retries"] = dict(sorted(provider_retry_counts.items()))
        ledger.finish_run(
            run_id,
            state="completed",
            summary={
                "committed": committed_count,
                "queued": queued_count,
                "dead_lettered": total_dead_lettered,
                "nodes_extracted": len(node_candidates),
                "edges_extracted": len(all_edge_candidates),
                "relationships_accepted": len(edge_candidates),
                "relationships_queued": relation_queued,
                "relationships_dead_lettered": relation_dead_lettered,
                "claims_minted": claims_minted,
                "claims_detached": claims_detached,
                "claims_superseded": claims_superseded,
                "claims_supersede_deferred": claims_supersede_deferred,
                "corrections_stopped": False,
                "relations_corroborated": relations_corroborated,
                "provider_error": provider_error,
                "provider_failures": provider_failures,
                "empty_after_retry_blocks": empty_after_retry_blocks,
                "outcome": extraction_outcome,
            },
            post_semantic_policy_fingerprint=(post_fingerprints.semantic_policy_fingerprint),
        )

        ledger.close_stale_runs(document_id=document.id, keep_run_id=run_id)
        return RememberResult(
            document_id=document.id,
            committed=committed_count,
            queued=queued_count,
            dead_lettered=total_dead_lettered,
            blocks_total=blocks_total,
            nodes_extracted=len(node_candidates),
            edges_extracted=len(all_edge_candidates),
            claims_minted=claims_minted,
            provider_error=provider_error,
            provider_failures=provider_failures,
            empty_after_retry_blocks=empty_after_retry_blocks,
            llm_disabled=llm_disabled,
            outcomes=outcomes,
            outcome=extraction_outcome,
            ledger_run_id=run_id,
        )

    def recall(self, query: str, *, k: int = 10) -> list[QueryHit]:
        """Retrieve the ``k`` most relevant nodes for ``query``."""
        return self._vault.query(query, k=k)

    def ask(
        self,
        question: str,
        *,
        k: int = 20,
        retrieval_policy: AskRetrievalPolicy | None = None,
    ) -> Answer:
        """Answer ``question`` grounded in retrieved nodes, as ONE trace.

        The work itself is :meth:`_ask_answer`; this wraps it in a parent span
        so the LLM calls it makes — synthesis, and both tiers of it on the
        subgraph path — are children of the question rather than unrelated
        root traces in a flat list. ``retrieval`` is timed as a sibling child,
        which is the only way to see how an answer's latency divided between
        finding the evidence and writing the prose.

        A no-op wrapper when telemetry is off: same call, same result, no
        span, nothing imported.
        """
        from okto_neuron.llm import trace_parent

        with trace_parent(
            "ask",
            span_type="CHAIN",
            inputs={"question": question, "k": k},
            # On the TRACE, because a child span cannot carry tags and this is
            # what the trace list filters on.
            tags={"marginalia.step": "ask"},
        ) as span:
            answer = self._ask_answer(question, k=k, retrieval_policy=retrieval_policy)
            status = answer.retrieval.get("synthesis_status")
            span.set_attribute("marginalia.synthesis_status", status)
            # Each retried attempt is also its own failed child call span; this
            # count lets the question itself be filtered for "needed a retry".
            span.set_attribute(
                "marginalia.synthesis_retries",
                len(answer.retrieval.get("synthesis_retries") or ()),
            )
            span.set_attribute("marginalia.citations", len(answer.citations))
            span.set_attribute("marginalia.mode", answer.retrieval.get("mode"))
            span.set_outputs({"text": answer.text, "citations": list(answer.citations)})
            # Same rule the call spans follow: OK is reserved for a clean
            # answer, and anything the synthesis classified as degraded says so
            # on the question too, or a flat trace list would show a green
            # question containing a red call.
            if status is not None and status != "ok":
                span.set_status("ERROR")
            return answer

    def _ask_answer(
        self,
        question: str,
        *,
        k: int = 20,
        retrieval_policy: AskRetrievalPolicy | None = None,
    ) -> Answer:
        """Answer ``question`` grounded in retrieved nodes.

        Retrieves the top hits, then synthesises a textual answer over them via
        the LLM provider. ``citations`` and ``hits`` are ALWAYS exactly the
        query hits — even when ``enable_subgraph`` (config or
        ``retrieval_policy``) routes ``text`` through the wider 1-hop+
        ego-graph context instead: in that mode the model is asked to ground
        ``text`` in that expanded graph, not just ``hits``, so also read
        ``Answer.subgraph_evidence_ids`` (empty outside subgraph mode) rather
        than treating ``citations`` alone as the full grounding set. Degrades
        gracefully (``text=""``) when the provider is unavailable — but never
        silently: ``retrieval["synthesis_status"]`` is ALWAYS one of ``"ok"``,
        ``"provider_error"`` (the provider failed; ``retrieval["provider_error"]``
        carries a short redacted summary), ``"truncated"`` (the provider hit the
        token budget — ``finish_reason == "length"`` — so ``text`` is cut off),
        ``"abnormal_stop"`` (it stopped for some other non-``stop`` reason),
        ``"no_llm"`` (no usable LLM is configured, so no provider call was made;
        ``retrieval["no_llm_reason"]`` says why) or
        ``"empty"`` (the provider answered with nothing). ``retrieval["finish_reason"]``
        carries the normalized reason whenever the provider reported one, and
        ``retrieval["native_finish_reason"]`` the provider's raw value when
        litellm normalized it away. ``text == ""`` with
        ``synthesis_status == "provider_error"``
        means the MODEL was unreachable, NOT that the graph lacks the answer.
        """
        from okto_neuron.llm import LLMProviderError

        # Config drives the retrieval-render branch. Loaded per call (PATCH lands
        # instantly); enable_subgraph is read STEP-DIRECT (never via resolved()).
        cfg = self._vault_config()
        seed_k = retrieval_policy.seed_k if retrieval_policy and retrieval_policy.seed_k else k
        enable_subgraph = (
            retrieval_policy.enable_subgraph
            if retrieval_policy and retrieval_policy.enable_subgraph is not None
            else bool(cfg.llm.ask.enable_subgraph)
        )
        source_policy = _effective_source_policy(cfg, retrieval_policy, enable_subgraph)

        # The default (subgraph OFF) path expands same-file text neighbours into
        # context_spans; the subgraph path supplies its own graph context, so the
        # legacy text-neighbour expansion is suppressed (3.3). citations are ALWAYS
        # the original hit node ids, computed BEFORE the branch — never the
        # expanded subgraph nodes (HTTP contract: Answer.text/citations/hits).
        expand_context = not enable_subgraph and source_policy != "never"
        # Fix B (task-12): the diversified seed cut rides the subgraph path only
        # (policy/config can override either way); default block-dump ask stays
        # byte-identical. The same flag+quotas feed query_seeds inside
        # _subgraph_context so Tier-1 seeds and Tier-2 hits agree.
        seed_diversity = _effective_seed_diversity(
            cfg, retrieval_policy, enable_subgraph=enable_subgraph
        )
        seed_quotas = _effective_seed_quotas(cfg, retrieval_policy)
        from okto_neuron.llm import trace_child

        # Retrieval never passes the provider seam, so without this the trace
        # shows only the completion and an answer's time looks like it was all
        # generation.
        with trace_child(
            "retrieval",
            span_type="RETRIEVER",
            inputs={"question": question, "k": seed_k},
        ) as retrieval_span:
            hits = self._vault.query(
                question,
                k=seed_k,
                expand_context=expand_context,
                seed_diversity=seed_diversity,
                **seed_quotas,
            )
            retrieval_span.set_attribute("marginalia.hits", len(hits))
            retrieval_span.set_outputs({"hits": [hit.node.id for hit in hits]})
        citations = tuple(hit.node.id for hit in hits)

        text = ""
        trace: dict[str, Any] = {
            "mode": "subgraph" if enable_subgraph else "block",
            "seed_k": seed_k,
            "source_block_policy": source_policy,
            "source_blocks_used": False,
            "seed_diversity": seed_diversity,
        }
        no_llm_reason = self._ask_no_llm_reason(cfg)
        if no_llm_reason is not None:
            # No usable LLM: never dial the provider (it would only fail, and
            # the built-in default base is a real loopback port that may belong
            # to something else). The hits are still returned; the answer is
            # explicitly degraded rather than an "ok" with empty text.
            trace["synthesis_status"] = "no_llm"
            trace["no_llm_reason"] = no_llm_reason
            _LOG.info("ask answered without synthesis: %s", no_llm_reason)
        elif hits:
            if enable_subgraph:
                text, trace = self._ask_subgraph(
                    question,
                    cfg,
                    hits,
                    k=seed_k,
                    policy=retrieval_policy,
                    source_policy=source_policy,
                )
            else:
                _source_reads: list[int] = []
                context = (
                    ""
                    if source_policy == "never"
                    else _source_context_for_hits(
                        hits,
                        # SECURITY: the byte-read traversal guard is dormant unless
                        # vault_root is threaded through. The subgraph path already
                        # passes it (see _ask_subgraph); the default block path must
                        # too, or a Block whose stored source_path was tampered to
                        # point outside the vault would be read + leaked into the
                        # answer. _is_within_root re-validates every byte read.
                        vault_root=self._vault.path,
                        max_token_budget=(
                            retrieval_policy.source_block_budget_tokens
                            if retrieval_policy
                            else None
                        ),
                        source_reads=_source_reads,
                    )
                )
                trace.update(
                    {
                        "path": "block_with_sources"
                        if context.strip()
                        else "block_without_sources",
                        # HONEST grounding claim: True only when at least one hit
                        # contributed REAL source bytes, not merely a non-empty
                        # context (a rotted anchor no longer falls back to the
                        # node's name — see _hit_context_snippet).
                        "source_blocks_used": bool(_source_reads and _source_reads[0] > 0),
                        "source_block_budget_tokens": (
                            retrieval_policy.source_block_budget_tokens
                            if retrieval_policy
                            else None
                        ),
                        "context_tokens_estimate": _estimate_context_tokens(context),
                    }
                )
                try:
                    text = self._complete_ask(question, context, cfg, trace=trace)
                    _mark_finish_reason(trace, _completion_finish_state())
                except LLMProviderError as exc:
                    # Graceful degradation STAYS (text=""), but it is no longer
                    # silent: without this marker an empty answer is
                    # indistinguishable from "the graph knows nothing".
                    text = ""
                    _mark_provider_error(trace, exc)

        # 3.19: subgraph mode grounds the answer in a wider ego-graph than
        # ``citations`` (the retrieval seeds) alone can represent — see the
        # ``Answer`` docstring. ``trace["subgraph_evidence_ids"]`` is only
        # ever set by ``_ask_subgraph``, so this stays () on the block path.
        subgraph_evidence_ids = tuple(trace.get("subgraph_evidence_ids") or ())
        # ALWAYS present: a field that only appears on failure is one clients
        # forget to check. Paths that already classified themselves (every
        # provider-error handler) win; everything else is judged on the text.
        trace.setdefault("synthesis_status", "ok" if text.strip() else "empty")
        return Answer(
            text=text,
            citations=citations,
            hits=tuple(hits),
            subgraph_evidence_ids=subgraph_evidence_ids,
            retrieval=trace,
        )

    def explore(
        self,
        topic: str = "",
        *,
        node_id: str | None = None,
        hops: int = 1,
        k: int = 12,
        relationship_types: tuple[str, ...] | None = None,
        min_claim_confidence: float | None = None,
        max_degree_per_seed: int | None = None,
    ) -> dict[str, Any]:
        """Drill into the ego-graph around a topic or a specific node.

        Two entry modes:

        - ``topic`` (free text): seed via semantic search, then expand the
          relevance-capped ego-graph around the hits.
        - ``node_id`` (an id returned by a prior ``explore``/``ask`` citation):
          expand directly from that node, ignoring ``topic``. This is how an
          agent walks outward — call ``explore`` again on any returned node id
          to drill deeper, raising ``hops`` to widen the neighbourhood.

        Returns a structured ego-graph (``nodes`` / ``relationships`` / ``claims``),
        NOT prose, so the agent navigates by id. Reuses the ADR 0011
        :func:`okto_neuron.subgraph.build_ego_graph` primitive — the same degree
        cap, relevance rank, and quality gates the subgraph ``ask`` path uses.

        ``relationship_types`` / ``min_claim_confidence`` / ``max_degree_per_seed``
        are per-call overrides of the corresponding ``llm.ask`` vault config
        values (a caller-supplied value wins; ``None`` inherits config exactly as
        before). They are passed straight through to ``build_ego_graph``, which
        has always accepted them — only ``explore`` never exposed them.
        """
        from okto_neuron.subgraph import build_ego_graph

        cfg = self._vault_config()
        ask = cfg.llm.ask
        # Caller override first, then vault config, then the code default
        # (``_first_int``/``_first_float`` take the first non-None, so an explicit
        # 0/0.0 from the caller is honoured rather than falling through).
        degree_cap = _first_int(
            max_degree_per_seed, ask.max_degree_per_seed, _ASK_DEGREE_CAP_DEFAULT
        )
        neighbour_budget = _first_int(ask.neighbour_budget_tokens, _ASK_NEIGHBOUR_BUDGET_DEFAULT)
        effective_min_conf = _first_float(
            min_claim_confidence, ask.min_claim_confidence, _ASK_MIN_CLAIM_CONFIDENCE_DEFAULT
        )
        hops = max(1, min(int(hops), 5))

        if node_id:
            seed_ids: list[tuple[str, float]] = [(node_id, 1.0)]
        elif topic.strip():
            # Fix B (task-12): explore always builds an ego-graph, so it is a
            # subgraph seed path — diversity defaults ON (llm.ask.seed_diversity
            # in the vault yaml switches it off; explore has no per-request policy).
            seed_ids = self._vault.query_seeds(
                topic,
                k=k,
                seed_diversity=_effective_seed_diversity(cfg, None, enable_subgraph=True),
                **_effective_seed_quotas(cfg, None),
            )
        else:
            raise CompanionError("explore requires either a topic or a node_id")

        ego = build_ego_graph(
            seed_ids,
            self._vault.store,
            degree_cap=degree_cap,
            neighbour_budget_tokens=neighbour_budget,
            hops=hops,
            min_claim_confidence=effective_min_conf,
            relationship_types=relationship_types,
        )
        return {
            "seeds": [nid for nid, _ in seed_ids],
            "hops": hops,
            # AUDITABILITY: explore is the fallback when ask is under suspicion,
            # and it returned nothing describing HOW it retrieved. This mirrors
            # ask's ``retrieval`` block where the fields are meaningful and omits
            # what would be a lie: explore makes NO LLM call, so there is no
            # ``synthesis_status`` and no ``source_block_policy`` here. Every
            # value is the EFFECTIVE one (caller override > vault config > code
            # default), so a caller can see what was applied, not what they asked
            # for. ``hops`` is duplicated from the top level deliberately — the
            # top-level key is an existing contract and stays.
            "retrieval": {
                "mode": "node" if node_id else "topic",
                # ``k`` seeds a topic search only; in node_id mode it is unused,
                # so reporting it would misrepresent the call.
                "seed_k": None if node_id else k,
                "hops": hops,
                "max_degree_per_seed": degree_cap,
                "min_claim_confidence": effective_min_conf,
                # ``relationship_types`` has NO config fallback — it goes to
                # build_ego_graph as caller-or-None — so this is the applied
                # value, and [] means "no restriction".
                "relationship_types": list(relationship_types or ()),
            },
            "nodes": [
                {
                    "id": rec.id,
                    "type": rec.type,
                    "name": rec.name,
                    "is_seed": rec.is_seed,
                    "score": rec.score,
                    "block_id": rec.block_id,
                    "span": rec.span,
                }
                for rec in ego.nodes.values()
            ],
            "relationships": [
                {
                    "claim_id": rel.claim_id,
                    "subject_id": rel.subject_id,
                    "predicate": rel.predicate,
                    "object_id": rel.object_id,
                    "confidence": rel.confidence,
                    # RelRecord has always carried the source block_id; only the
                    # serializer dropped it, leaving relationships unanchorable
                    # while ``nodes`` entries were anchorable.
                    "block_id": rel.block_id,
                }
                for rel in ego.relationships
            ],
            "claims": [
                {
                    "claim_id": c.claim_id,
                    "subject_id": c.subject_id,
                    "subject_name": c.subject_name,
                    "predicate": c.predicate,
                    "literal": c.literal,
                    "confidence": c.confidence,
                    # See the relationships note above — ClaimRecord.block_id.
                    "block_id": c.block_id,
                }
                for c in ego.claims
            ],
        }

    def _ask_no_llm_reason(self, cfg: "VaultConfig") -> str | None:
        """Why ``ask`` has no usable LLM, or ``None`` when it has one.

        Same test as the ingest pre-flight (Fix B1): the LLM is switched off
        (``llm.enabled: false``), or the ask step resolves to the built-in
        default provider and base with an empty model. A provider injected
        directly (tests, embedded callers) always counts as usable.
        """
        if self._provider is not None:
            return None
        if not cfg.llm.enabled:
            return "the LLM is disabled for this vault (llm.enabled is false)"
        from okto_neuron.config import LLMDefaults

        resolved = cfg.llm.resolved("ask")
        defaults = LLMDefaults()
        if (
            resolved.provider == defaults.provider
            and resolved.api_base == defaults.api_base
            and not str(resolved.model or "").strip()
        ):
            return (
                "no LLM model is configured for this vault; set llm.defaults.model "
                "(or llm.ask.model) in okto-neuron.yaml, or run "
                "'okto-neuron onboard --reconfigure'"
            )
        return None

    def _complete_ask(
        self,
        question: str,
        context: str,
        cfg: "VaultConfig",
        *,
        system_prompt_override: str | None = None,
        trace: dict[str, Any] | None = None,
    ) -> str:
        """One grounded ask completion over ``context``. Shared by the default
        block-dump path and both Tier-1/Tier-2 subgraph passes so the prompt
        scaffold + provider kwargs are byte-identical across them. Raises
        ``LLMProviderError`` on a provider failure (callers map it to ``text=""``;
        a provider error is NEVER an abstention).

        A transient provider failure is retried under the same ADR 0039 D5
        policy extraction units use (``_provider_retry_delay``); each retried
        failure is appended to ``trace["synthesis_retries"]`` so a recovered
        answer still shows that it needed a second attempt. Only the final
        failure propagates.

        ``system_prompt_override`` is used by the GN-13 graph-native path to
        pass ``_ASK_SYSTEM_GRAPH`` (or ``llm.ask.system_prompt_graph``) without
        touching the block-dump path.  When ``None`` the existing selection logic
        applies: ``llm.ask.system_prompt`` → ``_ASK_SYSTEM``."""
        from okto_neuron.llm import Message, complete_with_retry

        # The excerpt sentence is CONDITIONAL on a marker actually being present,
        # so marker-free contexts (subgraph tiers, ``source_block_policy="never"``)
        # keep their existing prompt byte-for-byte. It lives here rather than in
        # ``_ASK_SYSTEM`` because that prompt is overridable per vault
        # (``llm.ask.system_prompt``) — a configured vault would silently lose it.
        excerpt_note = (
            "A note marked [EXCERPT source=... bytes=X-Y of N] is a FRAGMENT of that "
            "source, not the whole file; a fact missing from the byte ranges shown is "
            "not thereby absent from the record, so say the excerpts do not cover it "
            "rather than inferring a value.\n\n"
            if _EXCERPT_MARKER_TOKEN in context
            else ""
        )
        prompt = (
            f"Answer the question using only the retrieved notes below.\n\n"
            f"{excerpt_note}"
            f"Question: {question}\n\nNotes:\n{context}"
        )
        ask_resolved = cfg.llm.resolved("ask")
        # Step-level system_prompt only — avoids defaults.system_prompt
        # clobbering the ask code default (_ASK_SYSTEM).
        if system_prompt_override is not None:
            system_prompt = system_prompt_override
        else:
            system_prompt = cfg.llm.ask.system_prompt or _ASK_SYSTEM
        retries: list[dict[str, object]] = []
        try:
            return complete_with_retry(
                self._get_provider("ask"),
                [
                    Message("system", system_prompt),
                    Message("user", prompt),
                ],
                step="ask synthesis",
                retries=retries,
                temperature=ask_resolved.temperature,
                max_tokens=ask_resolved.max_tokens,
                top_p=ask_resolved.top_p,
                top_k=ask_resolved.top_k,
                min_p=ask_resolved.min_p,
                presence_penalty=ask_resolved.presence_penalty,
                enable_thinking=ask_resolved.enable_thinking,
            ).strip()
        finally:
            # Recorded on success AND on the exhausted retry's re-raise, so a
            # degraded answer still shows it was attempted twice.
            if retries and trace is not None:
                trace.setdefault("synthesis_retries", []).extend(retries)

    def _ask_subgraph(
        self,
        question: str,
        cfg: "VaultConfig",
        hits: list[QueryHit],
        *,
        k: int,
        policy: AskRetrievalPolicy | None,
        source_policy: SourceBlockPolicy,
    ) -> tuple[str, dict[str, Any]]:
        """Efficient-hybrid subgraph answer path (task-12 fix A, owner reframe).

        The default policy is ``"blend"``: Tier 1 answers over the ego-graph
        render PLUS a budgeted, query-term-selected source-block excerpt
        (``_effective_source_budget``, code default 6000 tokens) — replacing the
        pre-fix binary cliff (~650-token render OR an UNBOUNDED 30–66k Tier-2
        dump gated on abstention phrasing). If the blended Tier-1 answer
        abstains, ONE bounded escalation re-asks with the excerpt budget raised
        by at most ``_ASK_ESCALATION_BUDGET_FACTOR`` (2x) — abstention may raise
        the budget, never remove it. The render is KEPT in the escalated context.

        Legacy ``"on_coverage_miss"`` (explicit opt-in, A/B comparability) keeps
        the two-pass render-then-sources flow, but its Tier-2 read is now also
        bounded at the escalation budget. There are no unbounded source reads
        anywhere in the subgraph path; the block-dump arm is untouched.

        An answer that also abstains after escalation is kept — the abstention
        floor; never fabricate. A provider error at either tier returns
        gracefully (Tier-1 error → ``""``; escalation error → fall back to the
        Tier-1 answer) and is kept orthogonal to abstention.
        """
        from okto_neuron.llm import LLMProviderError
        from okto_neuron.query import _question_terms

        context, evidence_ids = self._subgraph_context(question, cfg, k=k, policy=policy)
        # Efficient-hybrid: the bounded excerpt budgets + the query's
        # discriminative content terms for block selection (None for
        # non-interrogative queries → selection falls back to the diversified
        # anchor order).
        source_budget = _effective_source_budget(cfg, policy)
        escalation_budget = source_budget * _ASK_ESCALATION_BUDGET_FACTOR
        query_terms = _question_terms(question)
        trace: dict[str, Any] = _trace_for_policy(
            cfg,
            policy,
            mode="subgraph",
            seed_k=k,
            source_policy=source_policy,
            context=context,
            source_budget=source_budget,
        )
        # 3.19: surface the full ego-graph evidence pool alongside the trace so
        # ask() can populate Answer.subgraph_evidence_ids — every downstream
        # return in this method shares this same ``trace`` dict.
        trace["subgraph_evidence_ids"] = evidence_ids

        # ── GN-13: select system prompt for the Tier-1 graph-native pass ────────
        # When enable_subgraph is True the context is a typed ego-graph render
        # (NODES/RELATIONSHIPS/CLAIMS).  Use _ASK_SYSTEM_GRAPH so the LLM parses
        # the typed structure natively.  llm.ask.system_prompt_graph overrides the
        # code default; llm.ask.system_prompt is NOT consulted here (it overrides
        # the generic block-dump prompt; graph-native has its own knob).
        # This selection is ONLY active when _ask_subgraph is called (i.e. when
        # enable_subgraph=True) — the block-dump path always goes through
        # _complete_ask without system_prompt_override, so _ASK_SYSTEM is used.
        graph_system_prompt: str | None = cfg.llm.ask.system_prompt_graph or _ASK_SYSTEM_GRAPH

        # ── GN-14: graph-native coverage-fallback threshold ───────────────────
        # A separate knob from the existing coverage_threshold so the graph-native
        # path can be tuned independently.  Code default = 0.0 (disabled) so no
        # behaviour change until explicitly configured.  When non-zero, a thin
        # render (below the density floor set by coverage_threshold_graph) short-
        # circuits directly to Tier-2 raw-block fallback, skipping the LLM call
        # on a render that can't possibly answer.
        coverage_threshold_graph: float = (
            cfg.llm.ask.coverage_threshold_graph
            if cfg.llm.ask.coverage_threshold_graph is not None
            else _ASK_COVERAGE_GRAPH_DEFAULT
        )
        # Apply the graph-native pre-gate: if the render is already thin at this
        # threshold, jump straight to Tier-2 (only when source_policy != "never").
        if (
            coverage_threshold_graph > 0.0
            and source_policy not in ("never", "always")
            and _subgraph_context_thin(context, coverage_threshold_graph)
        ):
            # Log that the pre-gate fired so A/B traces can attribute the fallback.
            _LOG.debug(
                "GN-14 graph-native pre-gate: thin render below %.2f, "
                "short-circuiting to Tier-2 raw-block fallback",
                coverage_threshold_graph,
            )
            # Fix B (task-12) T2b+T2c: neighbor-span parity with the block arm +
            # anchor-dedup/path-round-robin, plus query-term-aware selection
            # (efficient-hybrid). Budget: per-request policy wins; otherwise the
            # BOUNDED escalation budget — the pre-fix unbounded read is gone.
            t2_budget = (
                policy.source_block_budget_tokens
                if policy and policy.source_block_budget_tokens is not None
                else escalation_budget
            )
            _t2_reads: list[int] = []
            t2_context = _source_context_for_hits(
                self._vault.expand_hit_context_spans(hits),
                vault_root=self._vault.path,
                max_token_budget=t2_budget,
                diversify=True,
                query_terms=query_terms,
                source_reads=_t2_reads,
            )
            trace.update(
                {
                    "path": "graph_native_pregate_to_sources",
                    "coverage_threshold_graph": coverage_threshold_graph,
                    "source_blocks_used": bool(_t2_reads and _t2_reads[0] > 0),
                    "source_block_budget_tokens": t2_budget,
                    "context_tokens_estimate": _estimate_context_tokens(t2_context),
                }
            )
            if not t2_context.strip():
                # Nothing to fall back to — return empty, let caller handle.
                return "", trace
            try:
                t2_text = self._complete_ask(question, t2_context, cfg, trace=trace)
                _mark_finish_reason(trace, _completion_finish_state())
                return t2_text, trace
            except LLMProviderError as exc:
                _mark_provider_error(trace, exc)
                return "", trace

        if source_policy == "always":
            # Efficient-hybrid: the "always" combined pass is now BOUNDED by the
            # effective budget (policy > config > 6000 default) — previously an
            # absent policy meant an unbounded read on the subgraph path.
            _always_reads: list[int] = []
            source_context = _source_context_for_hits(
                hits,
                vault_root=self._vault.path,
                max_token_budget=source_budget,
                source_reads=_always_reads,
            )
            combined = _combine_contexts(context, source_context)
            trace.update(
                {
                    "path": "subgraph_with_sources",
                    "source_blocks_used": bool(_always_reads and _always_reads[0] > 0),
                    "source_block_budget_tokens": source_budget,
                    "context_tokens_estimate": _estimate_context_tokens(combined),
                }
            )
            try:
                # GN-13: source_policy="always" combines graph + raw blocks;
                # use the graph-native prompt since the primary context is the
                # typed ego-graph render.
                always_text = self._complete_ask(
                    question,
                    combined,
                    cfg,
                    system_prompt_override=graph_system_prompt,
                    trace=trace,
                )
                _mark_finish_reason(trace, _completion_finish_state())
                return always_text, trace
            except LLMProviderError as exc:
                _mark_provider_error(trace, exc)
                return "", trace

        if source_policy == "blend":
            # ── Efficient-hybrid ALWAYS-BLEND (the new subgraph default) ──────
            # Tier 1 answers over render + a budgeted, query-term-selected
            # source excerpt of the SAME hits (T2b neighbor-span parity, T2c
            # dedup/round-robin, IDF query-term ranking under the budget). The
            # abstention-phrasing gate no longer decides WHETHER sources are
            # read — sources are always present; abstention may only RAISE the
            # excerpt budget (bounded at _ASK_ESCALATION_BUDGET_FACTOR × base).
            enriched_hits = self._vault.expand_hit_context_spans(hits)
            _blend_reads: list[int] = []
            blend_source = _source_context_for_hits(
                enriched_hits,
                vault_root=self._vault.path,
                max_token_budget=source_budget,
                diversify=True,
                query_terms=query_terms,
                source_reads=_blend_reads,
            )
            combined = _combine_contexts(context, blend_source)
            trace.update(
                {
                    "path": "subgraph_blend",
                    "escalated": False,
                    "source_blocks_used": bool(_blend_reads and _blend_reads[0] > 0),
                    "source_block_budget_tokens": source_budget,
                    "context_tokens_estimate": _estimate_context_tokens(combined),
                }
            )
            try:
                tier1 = self._complete_ask(
                    question,
                    combined,
                    cfg,
                    system_prompt_override=graph_system_prompt,
                    trace=trace,
                )
                # Captured IMMEDIATELY: last_call_stats() is per-thread state that
                # the (possible) Tier-2 completion below overwrites.
                tier1_finish = _completion_finish_state()
            except LLMProviderError as exc:
                trace["path"] = "subgraph_provider_error"
                _mark_provider_error(trace, exc)
                return "", trace  # provider down ≠ abstention — no escalation
            if not _is_abstention(tier1):
                _mark_finish_reason(trace, tier1_finish)
                return tier1, trace

            # Objective bounded escalation: ONE re-ask with the excerpt budget
            # doubled, render KEPT. Skipped when the doubled budget yields no
            # new source bytes (identical context → identical prompt → a
            # guaranteed-duplicate completion is a pure waste).
            _escalated_reads: list[int] = []
            escalated_source = _source_context_for_hits(
                enriched_hits,
                vault_root=self._vault.path,
                max_token_budget=escalation_budget,
                diversify=True,
                query_terms=query_terms,
                source_reads=_escalated_reads,
            )
            if not escalated_source.strip() or escalated_source == blend_source:
                trace["escalation_skipped_no_new_source"] = True
                _mark_finish_reason(trace, tier1_finish)
                return tier1, trace
            escalated = _combine_contexts(context, escalated_source)
            trace.update(
                {
                    "path": "subgraph_blend_escalated",
                    "escalated": True,
                    "source_blocks_used": bool(_escalated_reads and _escalated_reads[0] > 0),
                    "source_block_budget_tokens": escalation_budget,
                    "context_tokens_estimate": _estimate_context_tokens(escalated),
                }
            )
            try:
                tier2 = self._complete_ask(
                    question,
                    escalated,
                    cfg,
                    system_prompt_override=graph_system_prompt,
                    trace=trace,
                )
                tier2_finish = _completion_finish_state()
            except LLMProviderError:
                _mark_finish_reason(trace, tier1_finish)
                return tier1, trace  # provider down on retry — keep Tier-1
            # Abstention floor: only a real, non-abstaining escalated answer
            # replaces the Tier-1 abstention — never fabricate.
            if not tier2 or _is_abstention(tier2):
                _mark_finish_reason(trace, tier1_finish)
                return tier1, trace
            _mark_finish_reason(trace, tier2_finish)
            return tier2, trace

        try:
            # GN-13: Tier-1 graph-native pass uses the graph-structured prompt.
            tier1 = self._complete_ask(
                question,
                context,
                cfg,
                system_prompt_override=graph_system_prompt,
                trace=trace,
            )
            tier1_finish = _completion_finish_state()
        except LLMProviderError as exc:
            trace["path"] = "subgraph_provider_error"
            _mark_provider_error(trace, exc)
            return "", trace  # provider down ≠ abstention — graceful, no Tier-2 retry

        if source_policy == "never":
            trace["path"] = "subgraph_only"
            _mark_finish_reason(trace, tier1_finish)
            return tier1, trace

        # Coverage pre-gate: a thin/header-only Tier-1 context can't have grounded
        # the answer. ``coverage_threshold`` (knob, code-default 0.4) is the
        # density floor; the post-hoc abstention check below is the PRIMARY signal.
        coverage_threshold = (
            policy.coverage_threshold
            if policy and policy.coverage_threshold is not None
            else (
                cfg.llm.ask.coverage_threshold
                if cfg.llm.ask.coverage_threshold is not None
                else _ASK_COVERAGE_DEFAULT
            )
        )
        insufficient = _is_abstention(tier1) or _subgraph_context_thin(context, coverage_threshold)
        if not insufficient:
            trace["path"] = "subgraph_only"
            _mark_finish_reason(trace, tier1_finish)
            return tier1, trace

        # Tier 2 (legacy "on_coverage_miss" opt-in): raw blocks of the SAME hits
        # (do NOT re-query). vault_root is the load-bearing security gate —
        # every byte read is re-validated under it. Fix B (task-12) T2b+T2c:
        # same-document neighbor spans + dedup/round-robin, plus query-term-
        # aware selection (efficient-hybrid). Budget: per-request policy wins;
        # otherwise the BOUNDED escalation budget — the pre-fix unbounded read
        # (30–66k tokens, block-dump scale) is gone from the subgraph path.
        t2_budget = (
            policy.source_block_budget_tokens
            if policy and policy.source_block_budget_tokens is not None
            else escalation_budget
        )
        _t2b_reads: list[int] = []
        t2_context = _source_context_for_hits(
            self._vault.expand_hit_context_spans(hits),
            vault_root=self._vault.path,
            max_token_budget=t2_budget,
            diversify=True,
            query_terms=query_terms,
            source_reads=_t2b_reads,
        )
        trace.update(
            {
                "path": "subgraph_then_sources",
                "source_blocks_used": bool(_t2b_reads and _t2b_reads[0] > 0),
                "source_block_budget_tokens": t2_budget,
                "context_tokens_estimate": _estimate_context_tokens(t2_context),
            }
        )
        if not t2_context.strip():
            _mark_finish_reason(trace, tier1_finish)
            return tier1, trace  # nothing new to read — keep the Tier-1 answer
        try:
            tier2 = self._complete_ask(question, t2_context, cfg, trace=trace)
            tier2_finish = _completion_finish_state()
        except LLMProviderError:
            _mark_finish_reason(trace, tier1_finish)
            return tier1, trace  # provider down on retry — keep Tier-1, never fabricate
        # If Tier-2 ALSO abstains, keep Tier-1 — this preserves the abstention floor
        # (Tier-1 was itself an abstention) AND avoids over-decline: the abstention
        # patterns are liberal, so a substantive Tier-1 answer that merely *contains*
        # a phrase like "not specified" must NOT be overwritten by a Tier-2 "no
        # information" when the raw block also lacks the detail. Only return Tier-2
        # when it is a real, non-abstaining answer.
        if not tier2 or _is_abstention(tier2):
            _mark_finish_reason(trace, tier1_finish)
            return tier1, trace
        _mark_finish_reason(trace, tier2_finish)
        return tier2, trace

    def _subgraph_context(
        self,
        question: str,
        cfg: "VaultConfig",
        *,
        k: int,
        policy: AskRetrievalPolicy | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        """ADR 0011 Tier-1 context: a relevance-capped ego-graph rendered as a
        compact typed NODES/RELATIONSHIPS/CLAIMS block, instead of the raw-block
        dump. All knobs are read STEP-DIRECT from ``cfg.llm.ask`` with code-default
        fallbacks (never via ``resolved("ask")``, which drops StepLLM-only fields).

        Seeds are the raw ``(node_id, score)`` pairs from ``search_claims`` (NOT the
        truncated QueryHits) so the ego-graph is built from the full ``max(k,20)``
        recall, not the thinner top-k.

        Returns ``(rendered_text, evidence_ids)``: ``evidence_ids`` is every
        entity node id plus every relationship/claim id in the built
        ``EgoGraph`` (see 3.19) — the full pool ``rendered_text`` draws from.
        ``render_ego_subgraph`` can still trim RELATIONSHIPS/CLAIMS rows (and
        NODES rows too, if ``policy.max_nodes`` is set) to fit the token
        budget, so ``evidence_ids`` may be a superset of what actually made it
        into ``rendered_text`` — surfacing the full pool is cheap here and
        correct-or-generous, never correct-or-missing.
        """
        from okto_neuron.query import _question_terms
        from okto_neuron.subgraph import build_ego_graph, render_ego_subgraph

        ask = cfg.llm.ask
        degree_cap = _first_int(
            policy.max_degree_per_seed if policy else None,
            ask.max_degree_per_seed,
            _ASK_DEGREE_CAP_DEFAULT,
        )
        neighbour_budget = _first_int(
            policy.neighbour_budget_tokens if policy else None,
            ask.neighbour_budget_tokens,
            _ASK_NEIGHBOUR_BUDGET_DEFAULT,
        )
        hops = _first_int(policy.hops if policy else None, ask.hops, _ASK_HOPS_DEFAULT)
        min_claim_confidence = _first_float(
            policy.min_claim_confidence if policy else None,
            ask.min_claim_confidence,
            _ASK_MIN_CLAIM_CONFIDENCE_DEFAULT,
        )
        # render_format=="typed_nodes" is the only v1 renderer. coverage_threshold
        # is read+applied by the Phase 3 gate in _ask_subgraph (over this render),
        # not here — _subgraph_context only builds the Tier-1 context.
        _render_format = ask.render_format or _ASK_RENDER_DEFAULT

        # Fix B (task-12): _subgraph_context only runs on the subgraph path, so
        # the seed-diversity default is ON here; policy/config can switch it off
        # to reproduce the pre-Fix-B seed ordering (A/B attribution).
        seed_ids = self._vault.query_seeds(
            question,
            k=k,
            seed_diversity=_effective_seed_diversity(cfg, policy, enable_subgraph=True),
            **_effective_seed_quotas(cfg, policy),
        )
        # GN-4: compute query embedding for relevance-aware degree-cap (GN-5) and
        # block-colocation ranking (GN-7). Reuses the vault-configured embedder
        # (same provider/normalization as ingest+stored embeddings).  On any error
        # (embedder not configured, provider unavailable) fall back gracefully to
        # None — build_ego_graph degrades to the original id-tiebreak sort.
        query_embedding: list[float] | None = None
        try:
            embedder = self._vault.embedder
            if embedder is not None:
                raw = embedder.embed(question)
                if raw:
                    query_embedding = list(raw)
        except Exception:  # noqa: BLE001 — embedding failure must not break ask
            query_embedding = None
        # Answer-aware ego-graph assembly: the question's discriminative (IDF) content
        # terms steer the degree_cap cut + render truncation toward the Claim that
        # actually answers the question, instead of the highest-confidence generic
        # sibling. None for non-interrogative queries → ranking is byte-identical.
        _qterms = _question_terms(question)
        answer_terms = frozenset(_qterms) if _qterms else None
        ego = build_ego_graph(
            seed_ids,
            self._vault.store,
            degree_cap=degree_cap,
            neighbour_budget_tokens=neighbour_budget,
            hops=hops,
            min_claim_confidence=min_claim_confidence,
            relationship_types=policy.relationship_types if policy else None,
            query_embedding=query_embedding,
            answer_terms=answer_terms,
        )
        # The render budget IS the swept knob (neighbour_budget_tokens, OQ2) — the
        # renderer truncates rel/claim rows seeds-first to fit it. The 100K ceiling
        # is a separate hard guard against a degree-cap/budget bug shipping a
        # megacontext that OOMs the provider; it never overrides a smaller budget.
        budget = min(neighbour_budget, _ASK_CONTEXT_TOKEN_CEILING)
        rendered = render_ego_subgraph(
            ego,
            max_token_budget=budget,
            max_nodes=policy.max_nodes if policy else None,
            max_relationships=policy.max_relationships if policy else None,
            max_claims=policy.max_claims if policy else None,
        )
        evidence_ids = tuple(
            sorted(
                set(ego.nodes)
                | {rel.claim_id for rel in ego.relationships}
                | {c.claim_id for c in ego.claims}
            )
        )
        return rendered, evidence_ids

    def review_queue(self) -> list[ReviewItem]:
        """List candidates parked for curation."""
        return self._review_queue().list()

    def review_queue_all(self) -> list[ReviewItem | "RelationReviewItem"]:
        """List node and relation reviews without exposing new write actions."""

        queue = self._review_queue()
        return [*queue.list(), *queue.list_relations()]

    def resolve_review(self, candidate_id: str, action: ReviewAction) -> CandidateOutcome:
        """Act on a parked node candidate (commit / discard / merge).

        Raises :class:`ReviewItemNotFoundError` for an unknown id.
        """
        for attempt in range(2):
            try:
                return self._resolve_review_once(candidate_id, action)
            except _ManualReviewScopeChangedError:
                if attempt:
                    raise
        raise RuntimeError("manual review scope retry did not terminate")

    def _resolve_review_once(
        self,
        candidate_id: str,
        action: ReviewAction,
    ) -> CandidateOutcome:
        """Apply one sealed manual plan, abandoning a stale untouched scope."""
        if action not in {"commit", "discard", "merge"}:
            raise ValueError(f"unknown review action {action!r}")
        writes_graph = action == "commit"
        guard_active = getattr(self._vault, "_integrity_write_guard_active", None)
        if writes_graph and callable(guard_active) and guard_active():
            raise RuntimeError("resolve_review must own the live-write integrity boundary")

        scan_factory = getattr(self._vault, "_integrity_scan_guard", None)
        scan_guard = scan_factory() if callable(scan_factory) else nullcontext()
        guard_factory = getattr(self._vault, "_integrity_write_guard", None)
        guard = guard_factory() if writes_graph and callable(guard_factory) else nullcontext()
        ledger = self._candidate_ledger()
        with scan_guard, ledger.semantic_writer_lease():
            # ReviewQueue is an in-memory snapshot of its durable file. Load it
            # only after the writer lease so a waiting resolver cannot replay a
            # candidate that the preceding transaction already acknowledged.
            queue = self._review_queue()
            open_plans = ledger.unreceipted_commit_plans()
            matching_plans = [
                plan
                for plan in open_plans
                if plan.context.get("intent") == "manual_review_resolution"
                and plan.context.get("candidate_id") == candidate_id
            ]
            if open_plans and (len(open_plans) != 1 or len(matching_plans) != 1):
                raise RuntimeError("manual review resolution found a different open commit plan")

            if matching_plans:
                plan = matching_plans[0]
                if plan.context.get("review_action") == "link":
                    if ledger.operation_receipts(plan):
                        raise RuntimeError(
                            "partially applied legacy review_link plan requires recovery"
                        )
                    operation = next(
                        (
                            value
                            for value in plan.operations
                            if value.get("operation") == "review_link"
                        ),
                        None,
                    )
                    if operation is None:
                        raise ValueError("legacy review_link plan has no link operation")
                    node_payload = operation.get("node")
                    edge_payload = operation.get("edge")
                    if not isinstance(node_payload, dict):
                        raise ValueError("legacy review_link plan has no pinned node")
                    from okto_neuron.core.schema import Edge, Node

                    pinned_node = Node.model_validate(node_payload)
                    pinned_edge = (
                        Edge.model_validate(edge_payload)
                        if isinstance(edge_payload, dict)
                        else None
                    )
                    graph_touched = self._vault.store.get_node(pinned_node.id) is not None
                    if pinned_edge is not None:
                        graph_touched = graph_touched or any(
                            edge.id == pinned_edge.id
                            for edge in self._vault.store.list_edges(
                                src=pinned_edge.src,
                                type=pinned_edge.type,
                                dst=pinned_edge.dst,
                            )
                        )
                    if graph_touched:
                        raise RuntimeError(
                            "partially applied legacy review_link plan requires recovery"
                        )
                    ledger.record_plan_abandoned(
                        plan.run_id,
                        plan_id=plan.plan_id,
                        plan_hash=plan.plan_hash,
                        reason="unsupported_legacy_review_link",
                        evidence={
                            "candidate_id": candidate_id,
                            "replacement_action": action,
                        },
                    )
                    ledger.finish_run(
                        plan.run_id,
                        state="abandoned",
                        summary={"reason": "unsupported_legacy_review_link"},
                    )
                    matching_plans = []
                    open_plans = []
                elif plan.context.get("review_action") != action:
                    raise ValueError("manual review action differs from the open sealed plan")
            if not matching_plans:
                item = queue.read(candidate_id)
                if not isinstance(item, ReviewItem):
                    raise ValueError(
                        "relation review items are read/acknowledge only; "
                        "graph resolution belongs to the orchestrator"
                    )
                candidate = next(
                    (value for value in queue.candidates() if value.candidate_id == candidate_id),
                    None,
                )
                if candidate is None:
                    raise ValueError("manual review queue lost its full candidate")
                fingerprints = self._semantic_fingerprints()
                target_ref = (
                    max(
                        item.correlations,
                        key=lambda correlation: correlation.score,
                    ).target_id
                    if action == "merge" and item.correlations
                    else None
                )
                pinned_node = candidate.to_node()
                pinned_edge = None
                resolution_scope = queue.resolution_scope(candidate_id)
                run_id = ledger.start_run(
                    document_id=f"review:{candidate_id}",
                    source="review_queue",
                    blocks_total=0,
                    model="manual",
                    semantic_policy_fingerprint=(fingerprints.semantic_policy_fingerprint),
                    config_fingerprint=fingerprints.config_fingerprint,
                    extraction_fingerprint=fingerprints.extraction_fingerprint,
                )
                plan_id = ledger.record_commit_plan(
                    run_id,
                    operations=[
                        {
                            "operation": f"review_{action}",
                            "candidate_kind": "node",
                            "candidate_id": candidate_id,
                            "type": item.type,
                            "title": item.title,
                            "confidence": item.confidence,
                            "target_ref": target_ref,
                            "reason": item.reason,
                            "review_item": {
                                "candidate": candidate.model_dump(mode="json"),
                                "reason": item.reason,
                                "correlations": [
                                    correlation.model_dump(mode="json")
                                    for correlation in item.correlations
                                ],
                            },
                            "node": pinned_node.model_dump(mode="json"),
                            "edge": (
                                pinned_edge.model_dump(mode="json")
                                if pinned_edge is not None
                                else None
                            ),
                        }
                    ],
                    context={
                        "intent": "manual_review_resolution",
                        "document_id": f"review:{candidate_id}",
                        "review_action": action,
                        "target_ref": target_ref,
                        **resolution_scope,
                    },
                )
                plan = next(
                    value for value in ledger.unreceipted_commit_plans() if value.plan_id == plan_id
                )

            from okto_neuron.predicates import PredicateRegistry

            try:
                applied = _apply_sealed_semantic_plan(
                    plan,
                    store=self._vault.store,
                    ledger=ledger,
                    registry=PredicateRegistry(self._vault.path),
                    review_queue=queue,
                    manual_graph_guard=guard,
                )
            except _ManualReviewScopeChangedError:
                if ledger.operation_receipts(plan):
                    raise
                ledger.record_plan_abandoned(
                    plan.run_id,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    reason="manual_review_queue_entry_changed",
                    evidence={
                        "candidate_id": candidate_id,
                        "review_action": action,
                    },
                )
                ledger.finish_run(
                    plan.run_id,
                    state="abandoned",
                    summary={"reason": "manual_review_queue_entry_changed"},
                )
                raise
            outcomes = [
                CandidateOutcome.model_validate(
                    {
                        **value,
                        "correlations": tuple(
                            Correlation.model_validate(correlation)
                            for correlation in value.get("correlations") or ()
                        ),
                    }
                )
                for value in applied["outcomes"]
            ]
            if len(outcomes) != 1:
                raise RuntimeError("manual review plan produced an invalid outcome")
            outcome = outcomes[0]
            try:
                queue.read(candidate_id)
                remains_queued = True
            except ReviewItemNotFoundError:
                remains_queued = False
            terminal_state = (
                "committed"
                if outcome.action == "committed"
                else "dropped"
                if action == "discard" and not remains_queued
                else "merged"
                if action == "merge" and not remains_queued
                else "queued"
            )
            summary = {
                "review_action": action,
                "candidate_id": candidate_id,
                "state": terminal_state,
                "outcome": outcome.model_dump(mode="json"),
                "operation_receipts": applied["receipts"],
            }
            try:
                ledger.record_candidate(
                    plan.run_id,
                    candidate_id=candidate_id,
                    candidate_kind="node",
                    state=terminal_state,
                    payload=summary,
                )
            finally:
                ledger.finish_run(
                    plan.run_id,
                    state="completed",
                    summary=summary,
                )
            return outcome

    def _review_queue(self) -> "ReviewQueue":
        from okto_neuron.consolidate.review_queue import ReviewQueue

        return ReviewQueue(Path(self._vault.path) / ".marginalia", self._vault.store)

    def _candidate_ledger(self) -> "CandidateLedger":
        from okto_neuron.consolidate.ledger import CandidateLedger

        return CandidateLedger(Path(self._vault.path) / ".marginalia")


# ── byte-anchored Claim minting (Track A) ─────────────────────────────────────
_CLAIM_BASELINE_CONFIDENCE = 0.7
"""Fallback Claim confidence when a relationship's subject has no resolved
outcome (mirrors resolve._BASE_CONFIDENCE)."""


@dataclass(frozen=True)
class _BlockAnchor:
    """The byte-range provenance of one source Block, carried onto every
    candidate/edge an extractor produces from that Block's text."""

    block_id: str
    byte_start: int
    byte_end: int
    content_hash: str
    source_path: str

    def facets(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "content_hash": self.content_hash,
            "source_path": self.source_path,
        }


def _extraction_unit_identity(
    anchor: _BlockAnchor | None,
    text: str,
    *,
    document_id: str,
    source: str | PathLike[str],
    extraction_fingerprint: str,
) -> dict[str, object]:
    """Canonical ledger identity for an anchored Block or fallback document span."""

    import hashlib

    from okto_neuron.consolidate.ledger import extraction_unit_id

    if anchor is None:
        encoded = text.encode("utf-8")
        block_id = document_id
        byte_start = 0
        byte_end = len(encoded)
        content_hash = hashlib.sha256(encoded).hexdigest()
        try:
            source_path = str(Path(source).expanduser().resolve(strict=False))
        except (OSError, TypeError, ValueError):
            source_path = str(source)
    else:
        block_id = anchor.block_id
        byte_start = anchor.byte_start
        byte_end = anchor.byte_end
        content_hash = anchor.content_hash
        source_path = anchor.source_path
    return {
        "unit_id": extraction_unit_id(
            block_id=block_id,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=content_hash,
            extraction_fingerprint=extraction_fingerprint,
        ),
        "block_id": block_id,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "content_hash": content_hash,
        "source_path": source_path,
        "extraction_fingerprint": extraction_fingerprint,
    }


@dataclass(frozen=True)
class _LLMNodes:
    """Deterministic ids of the LLM extraction Agent/Activity, plus the model
    fingerprint every minted Claim records."""

    activity_id: str
    agent_id: str
    model_id: str
    prompt_hash: str


def _embedding_text(title: str, content: str) -> str:
    """The text embedded for a node candidate — identical to resolve's
    ``_candidate_vector`` formula so a stored embedding equals what the resolver
    would have computed (no silent dim/text drift)."""
    return f"{title}\n{content}".strip()


def embedding_text_for(node: object) -> str:
    """The exact text that was (or should be) embedded for ``node``, by type.

    Load-bearing for re-embedding: a stored Claim is minted with
    ``title == content == rel_text`` and embedded as the BARE ``rel_text`` — NOT
    ``f"{title}\\n{content}"`` — so recomputing it through the primitive formula
    would embed the relation text twice (``f"{rel}\\n{rel}"``), shifting every
    Claim cosine and silently corrupting retrieval. Both write sites and the
    reembed engine route through this one helper so the stored vector always
    equals what a recompute produces.

    Duck-typed on ``.type``/``.title``/``.content`` so it accepts both a stored
    ``Node`` and an extraction ``NodeCandidate``.
    """
    if getattr(node, "type", None) == "Claim":
        return str(getattr(node, "content", "") or "")
    title = str(getattr(node, "title", "") or "")
    content = str(getattr(node, "content", "") or "")
    return _embedding_text(title, content)


def _extraction_units(
    store: "GraphStore",
    source: str | PathLike[str],
    *,
    chunk_size_bytes: int = 12_000,
    chunk_overlap_bytes: int = 0,
) -> list[tuple["_BlockAnchor | None", str]]:
    """One ``(anchor, text)`` unit per anchored Block of this document, in block
    order. Falls back to a single document-level unit (no anchor) when the source
    produced no Blocks (e.g. a non-file URL)."""
    resolved: str | None
    try:
        resolved = str(Path(source).expanduser().resolve(strict=False))
    except (OSError, ValueError, TypeError):
        resolved = None

    units: list[tuple[_BlockAnchor | None, str]] = []
    if resolved is not None:
        from okto_neuron.ingest.markdown import block_uses_chunking_policy

        blocks = [
            node
            for node in store.list_nodes(type="Block")
            if node.facets.get("source_path") == resolved
            and block_uses_chunking_policy(
                node.facets,
                chunk_size_bytes=chunk_size_bytes,
                chunk_overlap_bytes=chunk_overlap_bytes,
            )
        ]
        blocks.sort(key=lambda node: node.facets.get("block_index", 0))
        for block in blocks:
            facets = block.facets
            anchor = _BlockAnchor(
                block_id=block.id,
                byte_start=int(facets.get("byte_start") or 0),
                byte_end=int(facets.get("byte_end") or 0),
                content_hash=str(facets.get("content_hash") or ""),
                source_path=resolved,
            )
            units.append((anchor, block.content))
    if units:
        return units
    return [(None, _read_source_text(source))]


def _llm_node_artifacts(provider: "LLMProvider") -> tuple["_LLMNodes", tuple[object, ...]]:
    """Build the pinned LLM provenance identity without mutating the graph."""
    import hashlib

    from okto_neuron._internal.infra import INFRA_FACET
    from okto_neuron.core.schema import Node, Provenance
    from okto_neuron.extract import _SYSTEM
    from okto_neuron.ingest.markdown import sha256_hex

    model_id = str(getattr(provider, "model", None) or "unknown")
    prompt_hash = hashlib.sha256(_SYSTEM.encode("utf-8")).hexdigest()
    agent_id = sha256_hex("agent", "llm", model_id)
    activity_id = sha256_hex("activity", "llm-extraction", model_id)

    prov = Provenance(source="llm", rule_id="companion-remember", layer="llm-extraction")
    nodes = (
        Node(
            id=agent_id,
            type="Agent",
            title=f"llm:{model_id}",
            content="Marginalia LLM extraction agent",
            provenance=prov,
            facets=dict(INFRA_FACET),
        ),
        Node(
            id=activity_id,
            type="Activity",
            title=f"ExtractionActivity llm {model_id}",
            content="LLM-backed knowledge-graph extraction.",
            provenance=prov,
            facets=dict(INFRA_FACET),
        ),
    )
    return (
        _LLMNodes(
            activity_id=activity_id,
            agent_id=agent_id,
            model_id=model_id,
            prompt_hash=prompt_hash,
        ),
        nodes,
    )


def _vault_relative(abs_path: str, vault_root: "str | PathLike[str] | None") -> str | None:
    """Best-effort vault-relative form of an absolute source path, for
    :class:`SourceSpan` (which requires a vault-relative, non-absolute path).
    Returns ``None`` when relativization is impossible — never raises, so claim
    minting stays crash-proof. The absolute facet is unchanged."""
    if not abs_path or vault_root is None:
        return None
    try:
        rel = (
            Path(abs_path)
            .resolve(strict=False)
            .relative_to(Path(vault_root).expanduser().resolve(strict=False))
        )
    except (OSError, ValueError, TypeError):
        return None
    text = rel.as_posix()
    return text or None


def _build_source_span(
    *,
    source_path: str,
    byte_start: "int | None",
    byte_end: "int | None",
    content_hash: "str | None",
) -> "SourceSpan | None":
    """Construct a SourceSpan from the SAME provenance already used for the
    claim's block_id/byte fields. Returns ``None`` (never raises) when any piece
    is missing or the value-object rejects it — minting must not start failing."""
    from pydantic import ValidationError

    from okto_neuron.schema.support import SourceSpan

    if not source_path or not content_hash or byte_start is None or byte_end is None:
        return None
    try:
        return SourceSpan(
            source_path=source_path,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=content_hash,
        )
    except (ValidationError, ValueError):
        return None


@dataclass(frozen=True)
class _ClaimEvidence:
    """The narrowest deterministic source evidence for one relationship."""

    byte_start: int | None
    byte_end: int | None
    content_hash: str | None
    source_asserted_at: str | None = None


_RFC3339_BRACKETED = re.compile(
    rb"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\]"
)


def _normalized_source_timestamp(raw: bytes) -> str | None:
    """Normalize an explicitly zoned RFC 3339 timestamp to UTC."""

    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(raw.decode("ascii").replace("Z", "+00:00"))
    except (UnicodeDecodeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _claim_evidence(
    block: object,
    *,
    subject_title: str,
    object_label: str,
    fallback_byte_start: int | None,
    fallback_byte_end: int | None,
    fallback_content_hash: str | None,
) -> _ClaimEvidence:
    """Narrow a block-wide relationship anchor to one unambiguous source line.

    Extractors currently anchor relationships to their complete input Block.  A
    chat/log line can safely provide a narrower span only when that exact line
    contains both endpoint surfaces and is unique inside the Block.  Anything
    ambiguous retains the original Block span.  A bracketed RFC 3339 timestamp
    on that same line becomes explicit correction-order evidence; it is never
    inferred from filenames, claim ids, or source traversal order.
    """

    facets = getattr(block, "facets", {}) or {}
    source_path = str(facets.get("source_path") or "")
    fallback = _ClaimEvidence(
        byte_start=fallback_byte_start,
        byte_end=fallback_byte_end,
        content_hash=fallback_content_hash,
    )
    if (
        not source_path
        or fallback_byte_start is None
        or fallback_byte_end is None
        or not subject_title.strip()
        or not object_label.strip()
    ):
        return fallback
    try:
        source_bytes = Path(source_path).read_bytes()
    except OSError:
        return fallback
    if not (0 <= fallback_byte_start < fallback_byte_end <= len(source_bytes)):
        return fallback
    block_bytes = source_bytes[fallback_byte_start:fallback_byte_end]
    if fallback_content_hash and hashlib.sha256(block_bytes).hexdigest() != fallback_content_hash:
        return fallback

    subject_key = subject_title.casefold()
    object_key = object_label.casefold()
    matches: list[tuple[int, bytes]] = []
    offset = fallback_byte_start
    for raw_line in block_bytes.splitlines(keepends=True):
        evidence_bytes = raw_line.rstrip(b"\r\n")
        try:
            line_key = evidence_bytes.decode("utf-8").casefold()
        except UnicodeDecodeError:
            offset += len(raw_line)
            continue
        if subject_key in line_key and object_key in line_key:
            matches.append((offset, evidence_bytes))
        offset += len(raw_line)
    if len(matches) != 1:
        return fallback

    byte_start, evidence_bytes = matches[0]
    timestamps = _RFC3339_BRACKETED.findall(evidence_bytes)
    source_asserted_at = (
        _normalized_source_timestamp(timestamps[0]) if len(timestamps) == 1 else None
    )
    return _ClaimEvidence(
        byte_start=byte_start,
        byte_end=byte_start + len(evidence_bytes),
        content_hash=hashlib.sha256(evidence_bytes).hexdigest(),
        source_asserted_at=source_asserted_at,
    )


@dataclass(frozen=True)
class _ClaimPlan:
    operations: tuple[dict[str, Any], ...]
    edge_results: dict[str, dict[str, Any]]
    relations_corroborated: int


@dataclass(frozen=True)
class _ReconciliationPlan:
    operations: tuple[dict[str, Any], ...]
    claims_detached: int
    superseded_pairs: tuple[tuple[str, str], ...]
    deferred_pairs: tuple[tuple[str, str], ...]
    resurrections: tuple[dict[str, Any], ...]
    annotation_artifacts: tuple[str, ...]


class _PlanningGraphOverlay:
    """Minimal in-memory GraphStore overlay used only while compiling a plan.

    Existing ADR 0022/0024 rules are expressed as store transformations. The
    overlay lets those mature rules run against the exact post-Claim view while
    retaining every mutation as intent. No live graph write occurs until the
    resulting operations have been sealed and fsynced.
    """

    def __init__(self, base: "GraphStore") -> None:
        self._base = base
        self._nodes: dict[str, Any] = {}
        self._edges: dict[str, Any] = {}

    @property
    def planned_nodes(self) -> tuple[Any, ...]:
        return tuple(self._nodes[node_id] for node_id in sorted(self._nodes))

    @property
    def planned_edges(self) -> tuple[Any, ...]:
        return tuple(self._edges[edge_id] for edge_id in sorted(self._edges))

    def add_node(self, node: Any, clear_embedding: bool = False) -> None:
        if getattr(node, "embedding", None) is None and not clear_embedding:
            # Same upsert contract as the store: no vector on the node keeps the one
            # the planned-or-stored node already has.
            current = self.get_node(str(node.id))
            if current is not None and getattr(current, "embedding", None) is not None:
                node = node.model_copy(update={"embedding": current.embedding})
        self._nodes[str(node.id)] = node

    def add_edge(self, edge: Any) -> None:
        if self.get_node(str(edge.src)) is None or self.get_node(str(edge.dst)) is None:
            raise ValueError("planned edge has a missing endpoint")
        self._edges[str(edge.id)] = edge

    def get_node(self, node_id: str, include_embedding: bool = True) -> Any:
        return self._nodes.get(node_id) or self._base.get_node(
            node_id, include_embedding=include_embedding
        )

    def get_nodes(self, node_ids: Iterable[str], include_embedding: bool = False) -> list[Any]:
        """``get_node`` per id, with the base reads batched into one call: input
        order, duplicates collapsed, missing ids skipped (the protocol contract)."""
        ids = list(dict.fromkeys(node_ids))
        base = {
            str(node.id): node
            for node in self._base.get_nodes(
                [i for i in ids if i not in self._nodes], include_embedding=include_embedding
            )
        }
        found = (self._nodes.get(node_id) or base.get(node_id) for node_id in ids)
        return [node for node in found if node is not None]

    def list_nodes(self, type: str | None = None, include_embedding: bool = False) -> list[Any]:
        nodes = {
            str(node.id): node
            for node in self._base.list_nodes(type=type, include_embedding=include_embedding)
        }
        for node in self._nodes.values():
            if type is None or node.type == type:
                nodes[str(node.id)] = node
            else:
                nodes.pop(str(node.id), None)
        return [nodes[node_id] for node_id in sorted(nodes)]

    def list_edges(
        self,
        src: str | None = None,
        dst: str | None = None,
        type: str | None = None,
    ) -> list[Any]:
        edges = {str(edge.id): edge for edge in self._base.list_edges(src=src, dst=dst, type=type)}
        for edge in self._edges.values():
            if (
                (src is None or str(edge.src) == src)
                and (dst is None or str(edge.dst) == dst)
                and (type is None or edge.type == type)
            ):
                edges[str(edge.id)] = edge
        return [edges[edge_id] for edge_id in sorted(edges)]


def _seed_planning_overlay(
    overlay: _PlanningGraphOverlay,
    operations: list[dict[str, Any]],
) -> set[str]:
    """Materialize already-planned graph artifacts into the planning overlay."""

    from okto_neuron.core.schema import Edge, Node

    minted_claim_ids: set[str] = set()
    for operation in operations:
        kind = operation.get("operation")
        payload: object | None = None
        if kind == "ensure_node":
            payload = operation.get("node")
        elif kind == "create_node":
            payload = operation.get("node")
        elif kind == "mint_claim":
            payload = operation.get("claim")
            minted_claim_ids.add(str(operation.get("expected_claim_id") or ""))
        elif kind == "update_node_state":
            payload = operation.get("node")
        if isinstance(payload, dict):
            overlay.add_node(Node.model_validate(payload))

    for operation in operations:
        kind = operation.get("operation")
        if kind not in {
            "attach_claim_provenance",
            "create_topology_edge",
            "ensure_source_mention",
        }:
            continue
        payload = operation.get("edge")
        if isinstance(payload, dict):
            overlay.add_edge(Edge.model_validate(payload))
    return minted_claim_ids


def _claim_state_reason(before: Any, desired: Any) -> str:
    from ._incremental import (
        _DETACHED_KEY,
        _SUPERSEDED_KEY,
    )

    before_facets = before.facets or {}
    desired_facets = desired.facets or {}
    if desired_facets.get(_SUPERSEDED_KEY) and not before_facets.get(_SUPERSEDED_KEY):
        return "claim_superseded"
    if desired_facets.get(_DETACHED_KEY) and not before_facets.get(_DETACHED_KEY):
        return "claim_detached"
    if (before_facets.get(_SUPERSEDED_KEY) or before_facets.get(_DETACHED_KEY)) and not (
        desired_facets.get(_SUPERSEDED_KEY) or desired_facets.get(_DETACHED_KEY)
    ):
        return "claim_resurrected"
    if before_facets.get("corroborations") != desired_facets.get("corroborations"):
        return "claim_corroborated"
    return "claim_state_reconciled"


def _plan_claim_reconciliation(
    store: "GraphStore",
    *,
    source: "str | PathLike[str]",
    vault_path: "str | PathLike[str]",
    incremental_plan: Any,
    ingest_cfg: Any,
    base_operations: list[dict[str, Any]],
    valid_as_of: str,
    asserted_at: str,
    source_bytes: bytes,
    source_generation_sha256: str,
    correction_judge: Any = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> _ReconciliationPlan:
    """Compile all Claim lifecycle mutations without touching the live graph."""

    import json

    from okto_neuron.core.schema import Node
    from okto_neuron.ingest.markdown import sha256_hex

    from . import _incremental

    overlay = _PlanningGraphOverlay(store)
    minted_claim_ids = _seed_planning_overlay(overlay, base_operations)
    existing_claim_ids: frozenset[str] = frozenset()
    if _incremental.subchunk_enabled(ingest_cfg) and incremental_plan is not None:
        current_block_ids = frozenset(
            incremental_plan.extract_block_ids | incremental_plan.skipped_block_ids
        )
        existing_claim_ids = _incremental._correction_candidate_claim_ids(
            store,
            frozenset(incremental_plan.orphan_block_ids),
            current_block_ids,
        )
    superseded_pairs: list[tuple[str, str]] = []
    deferred_pairs: list[tuple[str, str]] = []
    if minted_claim_ids and existing_claim_ids and correction_judge is not None:
        from okto_neuron.errors import RebuildInterrupted

        try:
            superseded_pairs, deferred_pairs = _incremental.supersede_contradicted(
                overlay,
                frozenset(minted_claim_ids),
                existing_claim_ids,
                valid_as_of=valid_as_of,
                correction_judge=correction_judge,
                source_path=_incremental._resolve(source),
                should_stop=should_stop,
            )
        except RebuildInterrupted:
            raise
        except Exception:  # noqa: BLE001 - preserve conservative correction policy
            _LOG.exception(
                "ADR-0022 correction planning failed for source=%s; "
                "sealing the primary Claims without automatic supersedence",
                source,
            )
            # The helper may have advanced the overlay before an unexpected
            # failure. Rebuild from the immutable base operations so no partial
            # correction intent can leak into the sealed plan.
            overlay = _PlanningGraphOverlay(store)
            minted_claim_ids = _seed_planning_overlay(overlay, base_operations)
            superseded_pairs = []
            deferred_pairs = []

    detached_ids: list[str] = []
    resurrections: list[dict[str, Any]] = []
    current_lines = _incremental._normalized_lines(source_bytes.decode("utf-8", errors="ignore"))
    if _incremental.subchunk_enabled(ingest_cfg) and incremental_plan is not None:
        current_block_ids = incremental_plan.extract_block_ids | incremental_plan.skipped_block_ids
        if incremental_plan.orphan_block_ids:
            detached_ids = _incremental.detach_orphan_removals(
                overlay,
                incremental_plan.orphan_block_ids,
                current_block_ids,
                valid_as_of=valid_as_of,
                current_lines=current_lines,
            )
        if current_block_ids:
            resurrections = _incremental.resurrect_reverted_claims(
                overlay,
                current_block_ids | incremental_plan.orphan_block_ids,
                asserted_at=asserted_at,
                current_lines=current_lines,
            )

    # Remove provisional Claim CAS operations. The overlay now contains their
    # effects plus every lifecycle transition, so emit one final CAS per Claim.
    operations: list[dict[str, Any]] = []
    minted_operations: dict[str, dict[str, Any]] = {}
    for operation in base_operations:
        kind = operation.get("operation")
        if kind == "mint_claim":
            claim_id = str(operation["expected_claim_id"])
            minted_operations[claim_id] = operation
            desired = overlay.get_node(claim_id)
            if desired is None or desired.type != "Claim":
                raise ValueError("planned minted Claim disappeared from reconciliation overlay")
            operations.append({**operation, "claim": desired.model_dump(mode="json")})
            continue
        if kind == "update_node_state":
            desired_payload = operation.get("node")
            if isinstance(desired_payload, dict):
                desired = Node.model_validate(desired_payload)
                if desired.type == "Claim":
                    continue
        operations.append(operation)

    supersede_edges = [
        edge
        for edge in overlay.planned_edges
        if edge.type == "supersedes"
        and not any(store.list_edges(src=edge.src, dst=edge.dst, type="supersedes"))
    ]
    for edge in supersede_edges:
        operations.append(
            {
                "operation": "supersede",
                "old_claim_id": str(edge.dst),
                "new_claim_id": str(edge.src),
                "expected_edge_id": str(edge.id),
                "edge": edge.model_dump(mode="json"),
                "reason": "judge_confirmed_correction",
            }
        )

    for desired in overlay.planned_nodes:
        if desired.type != "Claim" or desired.id in minted_operations:
            continue
        before = store.get_node(desired.id)
        if before is None or before.model_dump(mode="json") == desired.model_dump(mode="json"):
            continue
        operations.append(
            {
                "operation": "update_node_state",
                "node_id": desired.id,
                "expected_before": before.model_dump(mode="json"),
                "node": desired.model_dump(mode="json"),
                "reason": _claim_state_reason(before, desired),
            }
        )

    resolved_source = _incremental._resolve(source) or str(source)
    artifact_rel = f"detached/{sha256_hex(resolved_source)[:16]}.jsonl"
    annotation_artifacts: list[str] = []
    for claim_id in sorted(set(detached_ids)):
        desired = overlay.get_node(claim_id)
        if desired is None:
            raise ValueError("detached Claim disappeared from reconciliation overlay")
        desired_json = desired.model_dump(mode="json")
        desired_hash = sha256_hex(json.dumps(desired_json, sort_keys=True, separators=(",", ":")))
        annotation_id = sha256_hex(
            "detachment-annotation",
            source_generation_sha256,
            claim_id,
            desired_hash,
        )
        facets = desired.facets or {}
        record = {
            "annotation_id": annotation_id,
            "claim_id": claim_id,
            "S_id": facets.get("S_id"),
            "P": facets.get("P"),
            "O_id": facets.get("O_id"),
            "O_literal": facets.get("O_literal"),
            "title": desired.title,
            "source_path": resolved_source,
            "source_generation_sha256": source_generation_sha256,
            "valid_as_of": valid_as_of,
            "desired_node_sha256": desired_hash,
            "reason": "source-line-removed",
        }
        operations.append(
            {
                "operation": "append_detachment_annotation",
                "annotation_id": annotation_id,
                "artifact": artifact_rel,
                "record": record,
                "reason": "source-line-removed",
            }
        )
        annotation_artifacts.append(str(Path(vault_path) / ".marginalia" / artifact_rel))

    return _ReconciliationPlan(
        operations=tuple(operations),
        claims_detached=len(set(detached_ids)),
        superseded_pairs=tuple(superseded_pairs),
        deferred_pairs=tuple(deferred_pairs),
        resurrections=tuple(resurrections),
        annotation_artifacts=tuple(sorted(set(annotation_artifacts))),
    )


def _claim_provenance_edges(
    claim_id: str,
    block_id: str,
    *,
    llm: _LLMNodes,
    subject_id: str,
    object_id: str | None,
) -> tuple[object, ...]:
    """Return the deterministic provenance graph artifacts for one Claim mention."""

    from okto_neuron.core.schema import Edge, Provenance
    from okto_neuron.ingest.markdown import sha256_hex

    provenance = Provenance(
        source="llm",
        rule_id="companion-remember",
        layer="llm-extraction",
    )
    targets = {
        "prov:wasDerivedFrom": block_id,
        "prov:wasGeneratedBy": llm.activity_id,
        "prov:wasAttributedTo": llm.agent_id,
        "rdf:subject": subject_id,
    }
    if object_id:
        targets["rdf:object"] = object_id
    return tuple(
        Edge(
            id=sha256_hex("edge", claim_id, edge_type, target_id),
            type=edge_type,
            src=claim_id,
            dst=target_id,
            provenance=provenance,
        )
        for edge_type, target_id in targets.items()
    )


def _relation_decision_trace(admission: Any, decision: Any) -> dict[str, Any]:
    """Serialize the exact D6/D7 decision consumed by one sealed relation plan."""

    object_id = getattr(admission, "object_id", None)
    object_literal = getattr(admission, "object_literal", None)
    return {
        "d6": {
            "reason": admission.reason,
            "state": admission.state,
            "action": admission.action,
            "raw_predicate": admission.raw_predicate,
            "predicate": admission.predicate,
            "subject_id": admission.subject_id,
            "object_id": object_id,
            "object_literal": object_literal,
            "swapped": admission.swapped,
        },
        "d7": {
            "reason": decision.reason,
            "action": decision.action,
            "relation_kind": decision.relation_kind,
            "subject_id": decision.subject_id,
            "predicate": decision.predicate,
            "object_id": object_id,
            "object_literal": object_literal,
            "liveness_support_ids": sorted(decision.liveness_support_ids),
        },
    }


def _plan_relationship_claims(
    store: "GraphStore",
    edges: "list[EdgeCandidate]",
    *,
    planned_node_ids: set[str],
    titles: dict[str, str],
    confidences: dict[str, float],
    pinned_predicates: dict[str, str],
    pinned_relation_traces: dict[str, dict[str, Any]],
    llm: _LLMNodes,
    embedder: "EmbeddingProvider",
    vault_root: "str | PathLike[str]",
    min_claim_confidence: float,
    extra_mention_anchors: dict[str, list["EdgeCandidate"]],
    asserted_at: str | None,
    embedding_settings: Callable[[], tuple[int, int]] | None,
    on_embedding_usage: Callable[[Mapping[str, int]], None] | None = None,
) -> _ClaimPlan:
    """Compile complete Claim artifacts before the durable plan is sealed.

    Every embedding, predicate, state transition, provenance edge, and topology
    edge needed by replay is pinned here. Applying the resulting operations is a
    graph-only postcondition check; it never calls an LLM or embedder.
    """

    from pydantic import ValidationError

    from okto_neuron.consolidate.ledger import edge_candidate_id
    from okto_neuron.core.schema import Edge, Node, Provenance
    from okto_neuron.embed import embed_in_batches
    from okto_neuron.ingest.markdown import sha256_hex
    from okto_neuron.schema.support import Claim

    def _live(node_id: str) -> bool:
        return node_id in planned_node_ids or store.get_node(node_id) is not None

    def _title(node_id: str) -> str:
        if node_id in titles:
            return titles[node_id]
        node = store.get_node(node_id)
        return node.title if node is not None else node_id

    claim_operations: list[dict[str, Any]] = []
    provenance_operations: list[dict[str, Any]] = []
    topology_operations: list[dict[str, Any]] = []
    pending_embeddings: list[tuple[str, str, Node, Node | None, dict[str, Any]]] = []
    edge_results: dict[str, dict[str, Any]] = {}
    provenance_edge_ids: set[str] = set()
    relations_corroborated = 0
    claim_provenance = Provenance(
        source="llm",
        rule_id="companion-remember",
        layer="llm-extraction",
    )

    for candidate in edges:
        payload = candidate.model_dump(mode="json")
        candidate_id = edge_candidate_id(payload)
        predicate = str(pinned_predicates.get(candidate_id) or "").strip()
        if not predicate or predicate != candidate.type:
            raise ValueError("sealed Claim predicate differs from pinned admission")
        decision_trace = pinned_relation_traces.get(candidate_id)
        if not isinstance(decision_trace, dict):
            raise ValueError("sealed Claim lacks a pinned D6/D7 decision trace")
        if not candidate.block_id or not candidate.content_hash:
            raise ValueError("accepted relationship has no byte-grounded Claim anchor")
        is_literal = candidate.dst_literal is not None
        if not _live(candidate.src_ref) or (not is_literal and not _live(candidate.dst_ref)):
            raise ValueError("accepted relationship has a non-live endpoint")

        subject_confidence = max(
            0.0,
            min(1.0, confidences.get(candidate.src_ref, _CLAIM_BASELINE_CONFIDENCE)),
        )
        confidence = (
            subject_confidence
            if candidate.confidence is None
            else min(subject_confidence, max(0.0, min(1.0, candidate.confidence)))
        )
        if confidence < min_claim_confidence:
            raise ValueError("accepted relationship is below Claim confidence policy")
        claim_id = (
            semantic_claim_id(candidate.src_ref, predicate, literal=candidate.dst_literal)
            if is_literal
            else semantic_claim_id(candidate.src_ref, predicate, object_id=candidate.dst_ref)
        )
        block = store.get_node(candidate.block_id)
        if block is None:
            raise ValueError("accepted relationship Claim block is missing")
        source_path = str((block.facets or {}).get("source_path") or "")
        object_label = str(candidate.dst_literal) if is_literal else _title(candidate.dst_ref)
        evidence = _claim_evidence(
            block,
            subject_title=_title(candidate.src_ref),
            object_label=object_label,
            fallback_byte_start=candidate.byte_start,
            fallback_byte_end=candidate.byte_end,
            fallback_content_hash=candidate.content_hash,
        )
        source_span = _build_source_span(
            source_path=_vault_relative(source_path, vault_root) or "",
            byte_start=evidence.byte_start,
            byte_end=evidence.byte_end,
            content_hash=evidence.content_hash,
        )
        try:
            claim = Claim(
                id=claim_id,
                S_id=candidate.src_ref,
                P=predicate,
                O_id=None if is_literal else candidate.dst_ref,
                O_literal=candidate.dst_literal if is_literal else None,
                confidence=confidence,
                block_id=candidate.block_id,
                source_span=source_span,
                extraction_activity_id=llm.activity_id,
                agent_id=llm.agent_id,
                model_id=llm.model_id,
                prompt_hash=llm.prompt_hash,
            )
        except (ValidationError, ValueError) as exc:
            raise ValueError("accepted relationship cannot form a valid Claim") from exc

        mentions = [candidate, *extra_mention_anchors.get(candidate_id, ())]
        unique_mentions: dict[str, EdgeCandidate] = {}
        for mention in mentions:
            if mention.block_id:
                unique_mentions.setdefault(mention.block_id, mention)
        if not unique_mentions:
            raise ValueError("accepted relationship has no durable Claim mention")

        existing = store.get_node(claim_id)
        relation_text = f"{_title(candidate.src_ref)} {predicate} {object_label}".strip()
        operation_context = {
            "candidate_kind": "edge",
            "candidate_id": candidate_id,
            "candidate": payload,
            "expected_claim_id": claim_id,
            "min_claim_confidence": min_claim_confidence,
            "mention_candidates": [
                mention.model_dump(mode="json")
                for mention in extra_mention_anchors.get(candidate_id, ())
            ],
            "reason": "commit",
            "pinned_semantics": {
                "predicate": predicate,
                "subject_id": candidate.src_ref,
                "object_id": None if is_literal else candidate.dst_ref,
                "object_literal": candidate.dst_literal if is_literal else None,
            },
            "decision_trace": decision_trace,
        }

        if existing is None:
            # The first mention mints the Claim; every distinct additional
            # block in the same sealed plan is a corroboration of that Claim.
            relations_corroborated += max(0, len(unique_mentions) - 1)
            claim_node = Node(
                id=claim_id,
                type="Claim",
                title=relation_text,
                content=relation_text,
                facets={
                    **claim.model_dump(mode="json"),
                    "source_path": source_path,
                    "corroborations": len(unique_mentions),
                    **({"asserted_at": asserted_at} if asserted_at else {}),
                    **(
                        {
                            "source_asserted_at": evidence.source_asserted_at,
                            "source_time_evidence": source_span.model_dump(mode="json"),
                        }
                        if evidence.source_asserted_at and source_span is not None
                        else {}
                    ),
                },
                provenance=claim_provenance,
            )
            pending_embeddings.append(
                ("mint_claim", candidate_id, claim_node, None, operation_context)
            )
            edge_results[candidate_id] = {
                "state": "committed",
                "reason": "relationship claim minted",
                "claim_id": claim_id,
            }
        else:
            facets = existing.facets or {}
            expected_object = candidate.dst_literal
            if (
                existing.type != "Claim"
                or facets.get("S_id") != candidate.src_ref
                or facets.get("P") != predicate
                or (
                    is_literal
                    and (
                        type(facets.get("O_literal")) is not type(expected_object)
                        or facets.get("O_literal") != expected_object
                    )
                )
                or (not is_literal and facets.get("O_id") != candidate.dst_ref)
            ):
                raise ValueError("existing Claim differs from sealed semantic identity")

            existing_blocks = {
                str(edge.dst)
                for edge in store.list_edges(
                    src=claim_id,
                    type="prov:wasDerivedFrom",
                )
            }
            new_blocks = [
                block_id for block_id in unique_mentions if block_id not in existing_blocks
            ]
            desired_facets = dict(facets)
            if new_blocks:
                if asserted_at:
                    desired_facets["asserted_at"] = max(
                        str(desired_facets.get("asserted_at") or ""),
                        asserted_at,
                    )
                if evidence.source_asserted_at:
                    prior_source_time = str(desired_facets.get("source_asserted_at") or "")
                    if evidence.source_asserted_at > prior_source_time:
                        desired_facets["source_asserted_at"] = evidence.source_asserted_at
                        if source_span is not None:
                            latest_span = source_span.model_dump(mode="json")
                            desired_facets["source_time_evidence"] = latest_span
                            # Keep the Claim's public primary provenance aligned
                            # with its newest explicit source time. Older mentions
                            # remain durable through prov:wasDerivedFrom edges.
                            desired_facets["block_id"] = candidate.block_id
                            desired_facets["source_span"] = latest_span
                            desired_facets["source_path"] = source_path
                relations_corroborated += len(new_blocks)
            desired_facets["corroborations"] = max(
                1,
                len(existing_blocks | set(unique_mentions)),
            )

            from ._incremental import (
                _DETACHED_KEY,
                _SUPERSEDED_KEY,
                _VALID_AS_OF_KEY,
                _VALID_UNTIL_KEY,
                _superseders_all_stale,
            )

            was_superseded = bool(desired_facets.get(_SUPERSEDED_KEY))
            was_detached = bool(desired_facets.get(_DETACHED_KEY))
            stamp = str(
                desired_facets.get(_VALID_UNTIL_KEY) or desired_facets.get(_VALID_AS_OF_KEY) or ""
            )
            can_resurrect = (
                (was_superseded or was_detached)
                and (not stamp or not asserted_at or asserted_at >= stamp)
                and (not was_superseded or _superseders_all_stale(store, claim_id))
            )
            if can_resurrect:
                for key in (
                    _SUPERSEDED_KEY,
                    _DETACHED_KEY,
                    _VALID_UNTIL_KEY,
                    _VALID_AS_OF_KEY,
                ):
                    desired_facets.pop(key, None)

            desired = existing.model_copy(update={"facets": desired_facets})
            if desired != existing and existing.embedding is not None:
                claim_operations.append(
                    {
                        "operation": "update_node_state",
                        "node_id": desired.id,
                        "expected_before": existing.model_dump(mode="json"),
                        "node": desired.model_dump(mode="json"),
                        "reason": "claim_corroboration_or_resurrection",
                    }
                )
            elif existing.embedding is None:
                pending_embeddings.append(
                    (
                        "update_node_state",
                        candidate_id,
                        desired,
                        existing,
                        operation_context,
                    )
                )
            edge_results[candidate_id] = {
                "state": "merged",
                "reason": "relationship claim already exists",
                "claim_id": claim_id,
            }

        for mention in unique_mentions.values():
            for edge in _claim_provenance_edges(
                claim_id,
                str(mention.block_id),
                llm=llm,
                subject_id=mention.src_ref,
                object_id=None if mention.dst_literal is not None else mention.dst_ref,
            ):
                if str(edge.id) in provenance_edge_ids:
                    continue
                provenance_edge_ids.add(str(edge.id))
                provenance_operations.append(
                    {
                        "operation": "attach_claim_provenance",
                        "candidate_id": candidate_id,
                        "claim_id": claim_id,
                        "edge": edge.model_dump(mode="json"),
                        "reason": "byte_grounded_claim_provenance",
                    }
                )

        if not is_literal:
            topology_edge = Edge(
                id=sha256_hex("edge", candidate.src_ref, predicate, candidate.dst_ref),
                type=predicate,
                src=candidate.src_ref,
                dst=candidate.dst_ref,
                weight=candidate.weight,
                provenance=candidate.provenance,
            )
            topology_operations.append(
                {
                    "operation": "create_topology_edge",
                    "candidate_kind": "edge",
                    "candidate_id": candidate_id,
                    "candidate": payload,
                    "edge": topology_edge.model_dump(mode="json"),
                    "expected_edge_id": topology_edge.id,
                    "required_claim_id": claim_id,
                    "reason": "commit",
                    "pinned_semantics": operation_context["pinned_semantics"],
                    "decision_trace": decision_trace,
                }
            )

    if pending_embeddings:
        vectors = embed_in_batches(
            embedder,
            [embedding_text_for(item[2]) for item in pending_embeddings],
            settings=embedding_settings,
            on_usage=on_embedding_usage,
        )
        for (operation_kind, candidate_id, node, expected, context), vector in zip(
            pending_embeddings,
            vectors,
            strict=True,
        ):
            pinned_node = node.model_copy(update={"embedding": vector})
            if operation_kind == "mint_claim":
                claim_operations.append(
                    {
                        "operation": "mint_claim",
                        **context,
                        "claim": pinned_node.model_dump(mode="json"),
                        "expected_before": None,
                    }
                )
            else:
                assert expected is not None
                claim_operations.append(
                    {
                        "operation": "update_node_state",
                        "node_id": pinned_node.id,
                        "expected_before": expected.model_dump(mode="json"),
                        "node": pinned_node.model_dump(mode="json"),
                        "reason": "claim_corroboration_or_resurrection",
                    }
                )

    return _ClaimPlan(
        operations=tuple([*claim_operations, *provenance_operations, *topology_operations]),
        edge_results=edge_results,
        relations_corroborated=relations_corroborated,
    )


def _is_stored_node_artifact(observed: Node, *, type_: str, title: str, content: str) -> bool:
    """True when a node already stored under a planned id IS the planned artifact.

    Compares exactly the fields the node id is derived from, in the form the id
    derives them (``_candidate_id``: type, the FOLDED title, content). A later
    mention of an entity that differs only in display casing ("Turtles" /
    "turtles") lands on the same id by design (ADR 0040 D2: matching keys and
    display forms are separate data), and another document that committed the
    same id first leaves its own provenance, facets and display title on it.
    Either way it is this node, not a different artifact, and the stored node is
    kept as-is (ADR 0039 T4: replay never rewrites an artifact already present).
    A type or content mismatch under the same id cannot come from a re-mention,
    so it still fails the caller's guard.
    """
    return (
        observed.type == type_
        and exact_surface_key(observed.title) == exact_surface_key(title)
        and observed.content == content
    )


def _apply_sealed_semantic_plan(
    plan: object,
    *,
    store: "GraphStore",
    ledger: "CandidateLedger",
    registry: object,
    review_queue: "ReviewQueue",
    close_plan: bool = True,
    manual_graph_guard: Any = None,
) -> dict[str, Any]:
    """Apply or resume one sealed semantic plan without semantic re-adjudication."""

    from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
    from okto_neuron.consolidate.ledger import CommitPlanSnapshot
    from okto_neuron.consolidate.review_queue import PinnedRelationProposal
    from okto_neuron.core.schema import Edge, Node
    from okto_neuron.predicates import (
        PredicateAliasIndex,
        PredicateAliasRecord,
        PredicateRecord,
        PredicateRegistry,
    )

    if not isinstance(plan, CommitPlanSnapshot):
        raise TypeError("plan must be a CommitPlanSnapshot")
    if not isinstance(registry, PredicateRegistry):
        raise TypeError("registry must be a PredicateRegistry")
    receipts = ledger.operation_receipts(plan)

    def _receipt(
        operation: dict[str, Any],
        status: str,
        result: dict[str, Any],
    ) -> None:
        operation_id = str(operation["operation_id"])
        ledger.record_operation_receipt(
            plan.run_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            operation_id=operation_id,
            operation=str(operation["operation"]),
            status=status,
            result=result,
        )
        receipts[operation_id] = {"status": status, "result": result}

    # Phase 1: governed sidefile CAS before any graph or queue mutation.
    final_predicates: dict[str, object] = {}
    for operation in plan.operations:
        if operation["operation"] != "register_predicate":
            continue
        operation_id = str(operation["operation_id"])
        record = PredicateRecord.from_json(operation["record"])
        final_predicates[record.label] = record
        if operation_id in receipts:
            continue
        expected_payload = operation.get("expected_before")
        expected = (
            PredicateRecord.from_json(expected_payload) if expected_payload is not None else None
        )
        before = registry.get(record.label)
        registry.apply_planned(record, expected_before=expected)
        _receipt(
            operation,
            "already_present" if before == record else "applied",
            {"predicate": record.label},
        )
    for predicate, final_record in final_predicates.items():
        if registry.get(predicate) != final_record:
            raise ValueError("predicate state differs from the final sealed plan")

    # Phase 1a: off-graph predicate mapping records from ingest-time resolution
    # (ADR 0040 D6a). Same phase as the registry mints above, so this introduces
    # no NEW fingerprint desync: both sidefiles move together, once, here.
    # `PredicateAliasIndex.upsert` takes its own cross-process lock and is keyed
    # by a deterministic record id, so re-applying a replayed plan is a no-op.
    alias_index: PredicateAliasIndex | None = None
    for operation in plan.operations:
        if operation["operation"] != "register_predicate_alias":
            continue
        operation_id = str(operation["operation_id"])
        if operation_id in receipts:
            continue
        record = PredicateAliasRecord.from_json(operation["record"])
        if alias_index is None:
            alias_index = PredicateAliasIndex(registry.vault)
        before = {existing.id for existing in alias_index.records()}
        alias_index.upsert(record)
        _receipt(
            operation,
            "already_present" if record.id in before else "applied",
            {"predicate_alias": record.id},
        )

    # Phase 1b: provenance infrastructure is itself a pinned graph artifact.
    for operation in plan.operations:
        if operation["operation"] != "ensure_node":
            continue
        operation_id = str(operation["operation_id"])
        expected = Node.model_validate(operation["node"])
        observed = store.get_node(expected.id)
        if observed is None:
            if operation_id in receipts:
                raise ValueError("receipted infrastructure node is missing")
            store.add_node(expected)
            status = "applied"
        else:
            stable_observed = observed.model_dump(mode="json", exclude={"created_at"})
            stable_expected = expected.model_dump(mode="json", exclude={"created_at"})
            if stable_observed != stable_expected:
                raise ValueError("infrastructure node differs from sealed plan")
            status = "already_present"
        if operation_id not in receipts:
            _receipt(operation, status, {"node_id": expected.id})

    committed_node_ids: set[str] = set()
    node_titles: dict[str, str] = {}
    node_confidences: dict[str, float] = {}
    outcomes: list[dict[str, Any]] = []

    # Phase 2: endpoints and durable review intents.
    for operation in plan.operations:
        kind = str(operation["operation"])
        if kind not in {
            "create_node",
            "queue_review",
            "dead_letter",
            "supersede_candidate",
        }:
            continue
        operation_id = str(operation["operation_id"])
        if kind == "create_node":
            candidate = NodeCandidate.model_validate(operation["candidate"])
            pinned_node = (
                Node.model_validate(operation["node"])
                if isinstance(operation.get("node"), dict)
                else candidate.to_node()
            )
            observed = store.get_node(candidate.candidate_id)
            if observed is None:
                if operation_id in receipts:
                    raise ValueError("receipted node artifact is missing")
                store.add_node(pinned_node)
                status = "applied"
            else:
                if not _is_stored_node_artifact(
                    observed,
                    type_=candidate.type,
                    title=candidate.title,
                    content=candidate.content,
                ):
                    raise ValueError("node artifact differs from sealed plan")
                status = "already_present"
            committed_node_ids.add(candidate.candidate_id)
            node_titles[candidate.candidate_id] = candidate.title
            node_confidences[candidate.candidate_id] = float(operation.get("confidence") or 0.0)
            outcomes.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "type": candidate.type,
                    "title": candidate.title,
                    "action": "committed",
                    "confidence": node_confidences[candidate.candidate_id],
                    "correlations": tuple(operation.get("correlations") or ()),
                }
            )
            if operation_id not in receipts:
                _receipt(operation, status, {"node_id": candidate.candidate_id})
            continue
        if kind == "queue_review":
            candidate_id = str(operation["candidate_id"])
            review_item = operation.get("review_item")
            node_candidate = (
                None
                if isinstance(review_item, dict)
                else NodeCandidate.model_validate(operation["candidate"])
            )
            try:
                review_queue.read(candidate_id)
                exists = True
            except ReviewItemNotFoundError:
                exists = False
            if not exists:
                if operation_id in receipts:
                    raise ValueError("receipted review item is missing")
                if isinstance(review_item, dict):
                    candidate = EdgeCandidate.model_validate(review_item["candidate"])
                    review_queue.enqueue_relation(
                        candidate,
                        review_item["reason"],
                        PinnedRelationProposal.from_json(review_item["pinned_proposal"]),
                    )
                else:
                    assert node_candidate is not None
                    correlations = tuple(
                        Correlation.model_validate(value)
                        for value in operation.get("correlations") or []
                    )
                    review_queue.enqueue(
                        node_candidate,
                        operation["reason"],
                        correlations,
                    )
            if node_candidate is not None:
                outcomes.append(
                    {
                        "candidate_id": node_candidate.candidate_id,
                        "type": node_candidate.type,
                        "title": node_candidate.title,
                        "action": "queued",
                        "confidence": float(operation.get("confidence") or 0.0),
                        "correlations": tuple(operation.get("correlations") or ()),
                    }
                )
            if operation_id not in receipts:
                _receipt(
                    operation,
                    "already_present" if exists else "applied",
                    {"candidate_id": candidate_id},
                )
            continue
        if operation_id not in receipts:
            _receipt(
                operation,
                "dead_lettered" if kind == "dead_letter" else "applied",
                {
                    "candidate_id": operation.get("candidate_id"),
                    "reason": operation.get("reason"),
                    "target_ref": operation.get("target_ref"),
                },
            )

    # Phase 2b: manual review actions use the same resumable applier. The plan
    # pins the full queue entry, while the queue-entry digest in context detects
    # any operator edit between seal and apply.
    for operation in plan.operations:
        kind = str(operation["operation"])
        if kind not in {
            "review_commit",
            "review_link",
            "review_discard",
            "review_merge",
        }:
            continue
        if kind == "review_link":
            # Legacy plans remain schema-readable for audit/recovery, but the
            # old action inferred a generic relates_to edge from similarity
            # alone. It has no admitted predicate, direction, grounding gate,
            # or anchored Claim and is therefore never executable under ADR
            # 0040. The owning resolve_review boundary may abandon an untouched
            # plan before sealing one of the supported replacement actions.
            raise ValueError("legacy review_link plans are not executable under semantic policy")
        operation_id = str(operation["operation_id"])
        action = kind.removeprefix("review_")
        review_item = operation["review_item"]
        candidate = NodeCandidate.model_validate(review_item["candidate"])
        correlations = tuple(
            Correlation.model_validate(value) for value in review_item["correlations"]
        )
        target_ref = operation.get("target_ref")
        pinned_node = Node.model_validate(operation["node"])
        edge_payload = operation.get("edge")
        pinned_edge = Edge.model_validate(edge_payload) if isinstance(edge_payload, dict) else None
        if (
            candidate.candidate_id != str(operation["candidate_id"])
            or candidate.type != str(operation["type"])
            or candidate.title != str(operation["title"])
        ):
            raise ValueError("manual review operation differs from its pinned candidate")

        def _stable_node(value: Node) -> dict[str, Any]:
            return value.model_dump(mode="json", exclude={"created_at"})

        if pinned_node.id != candidate.candidate_id or _stable_node(pinned_node) != _stable_node(
            candidate.to_node()
        ):
            raise ValueError("manual review node differs from its pinned candidate")
        if pinned_edge is not None:
            raise ValueError("manual review operation has an unexpected edge artifact")

        try:
            queued_item = review_queue.read(candidate.candidate_id)
            queue_present = True
        except ReviewItemNotFoundError:
            queued_item = None
            queue_present = False
        queue_scope_matches = True
        if queue_present:
            if not isinstance(queued_item, ReviewItem):
                raise ValueError("manual review plan points to a relation review item")
            scope = review_queue.resolution_scope(candidate.candidate_id)
            expected_scope = {
                "queue_file": plan.context.get("queue_file"),
                "candidate_id": plan.context.get("candidate_id"),
                "entry_sha256": plan.context.get("entry_sha256"),
            }
            queue_scope_matches = scope == expected_scope

        def _is_pinned_artifact(observed: Node) -> bool:
            # The pinned node itself was checked in full against the queued
            # candidate above. What is on disk under that id may have been
            # written first by another document (its provenance, facets and
            # display casing), which is the same artifact, so compare only the
            # id inputs, like the create_node applier.
            return _is_stored_node_artifact(
                observed,
                type_=pinned_node.type,
                title=pinned_node.title,
                content=pinned_node.content,
            )

        def _manual_graph_postcondition() -> bool:
            if action != "commit":
                return True
            node = store.get_node(candidate.candidate_id)
            if node is None:
                return False
            if not _is_pinned_artifact(node):
                raise ValueError("manual review node differs from the sealed artifact")
            return True

        graph_complete = _manual_graph_postcondition()
        receipt = receipts.get(operation_id)
        if queue_present and not queue_scope_matches:
            # A watcher may re-propose the same content-addressed candidate
            # after the old entry was durably acknowledged. Preserve that new
            # evidence. A completed graph action or an existing receipt proves
            # the old entry is gone; otherwise the untouched stale plan is
            # abandoned by the owning resolve_review boundary and resealed.
            # The re-proposed entry is deliberately left queued even when the
            # node under its id now exists: the plan is bound to the entry it
            # pinned (resolution_scope), so it has no authority over an entry
            # the operator never resolved, and acknowledging it would drop that
            # entry's own provenance and reason unseen (ADR 0039: acknowledge
            # only the item whose requested operation completed). Resolving
            # the re-proposed entry later is a graph no-op for any action.
            if receipt is None and not (action == "commit" and graph_complete):
                raise _ManualReviewScopeChangedError(
                    "manual review queue entry differs from the sealed plan"
                )
            queue_present = False
        if receipt is None:
            before_complete = graph_complete and not queue_present
            if action == "commit":
                target_live = True
                structurally_valid = bool(candidate.type.strip())
                if not graph_complete and queue_present and target_live and structurally_valid:
                    with manual_graph_guard or nullcontext():
                        observed = store.get_node(candidate.candidate_id)
                        if observed is None:
                            store.add_node(pinned_node)
                        elif not _is_pinned_artifact(observed):
                            raise ValueError("manual review node differs from the sealed artifact")
                        if not _manual_graph_postcondition():
                            raise ValueError("manual review graph postcondition failed")
                    graph_complete = True
                if graph_complete and queue_present:
                    review_queue.acknowledge(candidate.candidate_id)
                    queue_present = False
                elif not queue_present and not graph_complete:
                    raise ValueError("manual review candidate disappeared before apply")
            elif action == "discard" and queue_present:
                review_queue.acknowledge(candidate.candidate_id)
                queue_present = False
            elif (
                action == "merge"
                and queue_present
                and target_ref is not None
                and store.get_node(str(target_ref)) is not None
            ):
                review_queue.acknowledge(candidate.candidate_id)
                queue_present = False

            terminal = (
                graph_complete and not queue_present if action == "commit" else not queue_present
            )
            terminal_state = (
                "committed"
                if terminal and action == "commit"
                else "dropped"
                if terminal and action == "discard"
                else "merged"
                if terminal and action == "merge"
                else "queued"
            )
            receipt_status = (
                "already_present"
                if terminal and before_complete
                else "applied"
                if terminal
                else "dead_lettered"
            )
            outcome = CandidateOutcome(
                candidate_id=candidate.candidate_id,
                type=candidate.type,
                title=candidate.title,
                action=("committed" if terminal_state == "committed" else "queued"),
                confidence=float(operation.get("confidence") or 0.0),
                correlations=correlations,
            )
            _receipt(
                operation,
                receipt_status,
                {
                    "review_action": action,
                    "candidate_id": candidate.candidate_id,
                    "state": terminal_state,
                    "outcome": outcome.model_dump(mode="json"),
                },
            )
        else:
            result_payload = receipt.get("result")
            if not isinstance(result_payload, dict):
                raise ValueError("manual review receipt has no result")
            terminal_state = str(result_payload.get("state") or "")
            completed = terminal_state in {"committed", "dropped", "merged"}
            if completed and queue_present:
                raise ValueError("receipted manual review item is still queued")
            if not completed and not queue_present:
                raise ValueError("receipted queued manual review item is missing")
            if terminal_state == "committed" and not graph_complete:
                raise ValueError("receipted manual review graph write is missing")
            outcome_payload = dict(result_payload["outcome"])
            outcome_payload["correlations"] = tuple(
                Correlation.model_validate(value)
                for value in outcome_payload.get("correlations") or ()
            )
            outcome = CandidateOutcome.model_validate(outcome_payload)

        if outcome.action == "committed":
            committed_node_ids.add(candidate.candidate_id)
        outcomes.append(
            {
                **outcome.model_dump(mode="python"),
                "correlations": outcome.correlations,
            }
        )

    # Phase 3: apply only fully materialized Claim nodes and CAS state updates.
    # No semantic derivation or embedding is permitted beyond the sealed plan.
    mint_operations = [
        operation for operation in plan.operations if operation["operation"] == "mint_claim"
    ]
    for operation in mint_operations:
        operation_id = str(operation["operation_id"])
        claim_id = str(operation["expected_claim_id"])
        if not isinstance(operation.get("claim"), dict):
            raise ValueError("sealed mint_claim operation lacks a pinned Claim artifact")
        expected = Node.model_validate(operation["claim"])
        if expected.id != claim_id or expected.type != "Claim":
            raise ValueError("sealed mint_claim artifact identity is invalid")
        observed = store.get_node(claim_id)
        if observed is None:
            if operation_id in receipts:
                raise ValueError("receipted Claim artifact is missing")
            store.add_node(expected)
            status = "applied"
        else:
            if observed.model_dump(mode="json") != expected.model_dump(mode="json"):
                raise ValueError("Claim artifact differs from sealed plan")
            status = "already_present"
        if operation_id not in receipts:
            _receipt(
                operation,
                status,
                {
                    "claim_id": claim_id,
                    "reason": operation["reason"],
                    **(
                        {"decision_trace": operation["decision_trace"]}
                        if "decision_trace" in operation
                        else {}
                    ),
                },
            )

    for operation in plan.operations:
        if operation["operation"] != "attach_claim_provenance":
            continue
        operation_id = str(operation["operation_id"])
        edge = Edge.model_validate(operation["edge"])
        if edge.src != str(operation["claim_id"]):
            raise ValueError("sealed Claim provenance edge has the wrong source")
        matches = list(store.list_edges(src=edge.src, type=edge.type, dst=edge.dst))
        if not matches:
            if operation_id in receipts:
                raise ValueError("receipted Claim provenance artifact is missing")
            store.add_edge(edge)
            status = "applied"
            result_edge = edge
        else:
            status = "already_present"
            result_edge = matches[0]
        if operation_id not in receipts:
            _receipt(operation, status, {"edge_id": str(result_edge.id)})

    # Phase 3b: append-only lifecycle evidence is idempotent by annotation id.
    # It is durable before the corresponding Claim CAS, so a crash can never
    # leave a detached Claim whose trust-root annotation was lost forever. The
    # new row is installed through an atomic same-directory replacement: a
    # process death exposes either the old complete JSONL or the old complete
    # JSONL plus the new complete row, never a torn append.
    for operation in plan.operations:
        if operation["operation"] != "append_detachment_annotation":
            continue
        import json
        import os
        from uuid import uuid4

        operation_id = str(operation["operation_id"])
        annotation_id = str(operation["annotation_id"])
        record = operation["record"]
        if not isinstance(record, dict) or record.get("annotation_id") != annotation_id:
            raise ValueError("sealed detachment annotation identity is invalid")
        relative = Path(str(operation["artifact"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("sealed detachment annotation path escapes the ledger")
        ledger_root = ledger.dir.resolve()
        artifact = (ledger_root / relative).resolve()
        try:
            artifact.relative_to(ledger_root)
        except ValueError as exc:
            raise ValueError("sealed detachment annotation path escapes the ledger") from exc
        artifact.parent.mkdir(parents=True, exist_ok=True)

        def _annotation_records() -> list[dict[str, Any]]:
            if not artifact.exists():
                return []
            rows: list[dict[str, Any]] = []
            for line in artifact.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise ValueError("detachment annotation artifact is malformed") from exc
                if not isinstance(value, dict):
                    raise ValueError("detachment annotation row must be an object")
                rows.append(value)
            return rows

        matching = [
            row for row in _annotation_records() if row.get("annotation_id") == annotation_id
        ]
        if len(matching) > 1:
            raise ValueError("detachment annotation id is duplicated")
        if matching and matching[0] != record:
            raise ValueError("detachment annotation id has conflicting content")
        if matching:
            status = "already_present"
        else:
            if operation_id in receipts:
                raise ValueError("receipted detachment annotation is missing")
            encoded = json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            existing = artifact.read_bytes() if artifact.exists() else b""
            separator = b"\n" if existing and not existing.endswith(b"\n") else b""
            replacement = existing + separator + encoded + b"\n"
            temp_path = artifact.with_name(f".{artifact.name}.{uuid4().hex}.tmp")
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    handle.write(replacement)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, artifact)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
            try:
                directory_fd = os.open(artifact.parent, os.O_RDONLY)
            except OSError:
                if os.name != "nt":
                    raise
            else:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            matching = [
                row for row in _annotation_records() if row.get("annotation_id") == annotation_id
            ]
            if matching != [record]:
                raise ValueError("detachment annotation postcondition failed")
            status = "applied"
        if operation_id not in receipts:
            _receipt(
                operation,
                status,
                {"annotation_id": annotation_id, "artifact": str(relative)},
            )

    # Phase 3c: expose correction history before hiding the old assertion.
    for operation in plan.operations:
        if operation["operation"] != "supersede":
            continue
        operation_id = str(operation["operation_id"])
        old_claim_id = str(operation["old_claim_id"])
        new_claim_id = str(operation["new_claim_id"])
        edge = Edge.model_validate(operation["edge"])
        if (
            edge.id != str(operation["expected_edge_id"])
            or edge.type != "supersedes"
            or edge.src != new_claim_id
            or edge.dst != old_claim_id
        ):
            raise ValueError("sealed supersedes edge identity is invalid")
        old_claim = store.get_node(old_claim_id)
        new_claim = store.get_node(new_claim_id)
        if (
            old_claim is None
            or new_claim is None
            or old_claim.type != "Claim"
            or new_claim.type != "Claim"
        ):
            raise ValueError("sealed supersedes edge requires two live Claims")
        matches = list(store.list_edges(src=new_claim_id, dst=old_claim_id, type="supersedes"))
        if not matches:
            if operation_id in receipts:
                raise ValueError("receipted supersedes edge is missing")
            store.add_edge(edge)
            status = "applied"
        else:
            if len(matches) != 1 or matches[0].model_dump(mode="json") != edge.model_dump(
                mode="json"
            ):
                raise ValueError("supersedes edge differs from sealed plan")
            status = "already_present"
        if operation_id not in receipts:
            _receipt(operation, status, {"edge_id": str(edge.id)})

    # Phase 3d: one final compare-and-swap per existing Claim. All earlier
    # corroboration, supersedence, detachment, and resurrection intents were
    # coalesced by the planner into this final state.
    updated_node_ids: set[str] = set()
    for operation in plan.operations:
        if operation["operation"] != "update_node_state":
            continue
        operation_id = str(operation["operation_id"])
        expected_before = Node.model_validate(operation["expected_before"])
        desired = Node.model_validate(operation["node"])
        if desired.id in updated_node_ids:
            raise ValueError("sealed plan contains multiple state updates for one node")
        updated_node_ids.add(desired.id)
        if desired.id != str(operation["node_id"]) or expected_before.id != desired.id:
            raise ValueError("sealed node-state update identity is invalid")
        observed = store.get_node(desired.id)
        if observed is None:
            raise ValueError("sealed node-state update target is missing")
        if observed.model_dump(mode="json") == desired.model_dump(mode="json"):
            status = "already_present"
        elif observed.model_dump(mode="json") == expected_before.model_dump(mode="json"):
            if operation_id in receipts:
                raise ValueError("receipted node-state update was not applied")
            store.add_node(desired)
            status = "applied"
        else:
            raise ValueError("sealed node-state update failed compare-and-swap")
        if operation_id not in receipts:
            _receipt(operation, status, {"node_id": desired.id})

    # Phase 4: topology is exposed only after its required Claim is present.
    topology_edge_ids: list[str] = []
    for operation in plan.operations:
        if operation["operation"] != "create_topology_edge":
            continue
        operation_id = str(operation["operation_id"])
        required_claim = str(operation["required_claim_id"])
        if store.get_node(required_claim) is None:
            raise ValueError("topology operation requires a missing Claim")
        edge = Edge.model_validate(operation["edge"])
        matches = list(store.list_edges(src=edge.src, type=edge.type, dst=edge.dst))
        if not matches:
            if operation_id in receipts:
                raise ValueError("receipted topology artifact is missing")
            store.add_edge(edge)
            matches = [edge]
            status = "applied"
        else:
            status = "already_present"
        topology_edge_ids.append(str(matches[0].id))
        if operation_id not in receipts:
            _receipt(
                operation,
                status,
                {
                    "edge_id": str(matches[0].id),
                    "reason": operation["reason"],
                    **(
                        {"decision_trace": operation["decision_trace"]}
                        if "decision_trace" in operation
                        else {}
                    ),
                },
            )

    for operation in plan.operations:
        if operation["operation"] != "ensure_source_mention":
            continue
        operation_id = str(operation["operation_id"])
        edge = Edge.model_validate(operation["edge"])
        matches = list(store.list_edges(src=edge.src, type=edge.type, dst=edge.dst))
        if not matches:
            if operation_id in receipts:
                raise ValueError("receipted source-mention artifact is missing")
            store.add_edge(edge)
            status = "applied"
            result_edge = edge
        else:
            status = "already_present"
            result_edge = matches[0]
        if operation_id not in receipts:
            _receipt(operation, status, {"edge_id": str(result_edge.id)})

    # Phase 5: source removal closes its planned group. The detach/retract work
    # is carried by the update_node_state operations above; this limb only
    # read-verifies that no artifact derived solely from the removed source is
    # still live, then writes the ADR 0039 ``source_removed`` receipt. It never
    # writes, so a replay re-verifies against the same store and stays silent.
    from ._incremental import _DETACHED_KEY, _SUPERSEDED_KEY

    for operation in plan.operations:
        if operation["operation"] != "source_removed":
            continue
        operation_id = str(operation["operation_id"])
        source_id = str(operation["source_id"])
        artifact_ids = [str(value) for value in operation["derived_artifact_ids"]]
        survivors: list[str] = []
        for artifact_id in artifact_ids:
            observed = store.get_node(artifact_id)
            if observed is None:
                continue
            facets = dict(observed.facets or {})
            # Memory-accretes (ADR 0024): a retired artifact is KEPT in the graph
            # and stamped, so liveness — not presence — is the survival test.
            if facets.get(_DETACHED_KEY) or facets.get(_SUPERSEDED_KEY):
                continue
            survivors.append(artifact_id)
        if survivors:
            raise ValueError(
                f"source removal left live artifacts derived from {source_id}: {sorted(survivors)}"
            )
        if operation_id not in receipts:
            _receipt(
                operation,
                "applied",
                {
                    "source_id": source_id,
                    "derived_artifact_ids": artifact_ids,
                    "survivor_count": len(survivors),
                    "reason": operation["reason"],
                },
            )

    if len(receipts) != len(plan.operations):
        raise ValueError("sealed plan has missing operation receipts")
    lifecycle_counts = {
        "claims_detached": sum(
            1
            for operation in plan.operations
            if operation["operation"] == "update_node_state"
            and operation.get("reason") == "claim_detached"
        ),
        "claims_resurrected": sum(
            1
            for operation in plan.operations
            if operation["operation"] == "update_node_state"
            and operation.get("reason") == "claim_resurrected"
        ),
        "claims_superseded": sum(
            1 for operation in plan.operations if operation["operation"] == "supersede"
        ),
        "detachment_annotations": sum(
            1
            for operation in plan.operations
            if operation["operation"] == "append_detachment_annotation"
        ),
    }
    manual_results = [
        receipts[str(operation["operation_id"])]["result"]
        for operation in plan.operations
        if str(operation["operation"]).startswith("review_")
    ]
    if len(manual_results) > 1:
        raise ValueError("sealed plan contains multiple manual review actions")
    manual_result = manual_results[0] if manual_results else {}
    if close_plan:
        ledger.record_commit(
            plan.run_id,
            plan_id=plan.plan_id,
            result={
                "operation_receipts_complete": True,
                "operation_receipts": len(receipts),
                "committed_node_ids": sorted(committed_node_ids),
                "claims_minted": len(mint_operations),
                "topology_edge_ids": topology_edge_ids,
                **lifecycle_counts,
                **manual_result,
                "resumed": True,
            },
        )
    return {
        "committed_node_ids": sorted(committed_node_ids),
        "claims_minted": len(mint_operations),
        "topology_edge_ids": topology_edge_ids,
        "outcomes": outcomes,
        "receipts": len(receipts),
        **lifecycle_counts,
    }


def _read_source_text(source: str | PathLike[str]) -> str:
    """Best-effort read of the source's text for extraction. Returns ``""`` for
    non-file sources or unreadable paths — extraction then yields no candidates."""
    try:
        path = Path(source)
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    return ""


def _read_source_bytes(source: str | PathLike[str]) -> bytes | None:
    """Best-effort read of the source's RAW bytes (the sub-chunk diff anchors over
    raw bytes, since Block ``content_hash`` is ``sha256(raw_slice)``). ``None`` for
    non-file or unreadable sources — the caller then keeps whole-Block units."""
    try:
        path = Path(source)
        if path.is_file():
            return path.read_bytes()
    except (OSError, ValueError):
        return None


def _resolved_source_path(source: str | PathLike[str]) -> str:
    try:
        return str(Path(source).expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        return str(source)


def _source_binding(
    store: "GraphStore",
    source: str | PathLike[str],
    document_id: str,
    *,
    source_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Pin the exact source generation and every current Block anchor."""

    import hashlib

    resolved = _resolved_source_path(source)
    raw = source_bytes if source_bytes is not None else _read_source_bytes(source)
    if raw is None:
        raise ValueError("a semantic commit plan requires readable source bytes")
    blocks = []
    for block in store.list_nodes(type="Block"):
        facets = block.facets or {}
        if str(facets.get("source_path") or "") != resolved:
            continue
        byte_start = int(facets.get("byte_start") or 0)
        byte_end = int(facets.get("byte_end") or 0)
        content_hash = str(facets.get("content_hash") or "")
        # Vault.add is append/upsert and intentionally retains orphan Blocks.
        # Bind only anchors that belong to these exact source bytes; otherwise
        # every edited source would seal a plan that can never be resumed.
        if (
            byte_start < 0
            or byte_end < byte_start
            or byte_end > len(raw)
            or hashlib.sha256(raw[byte_start:byte_end]).hexdigest() != content_hash
        ):
            continue
        blocks.append(
            {
                "block_id": block.id,
                "content_hash": content_hash,
                "byte_start": byte_start,
                "byte_end": byte_end,
            }
        )
    blocks.sort(key=lambda item: (item["byte_start"], item["byte_end"], item["block_id"]))
    return {
        "schema_version": "source_binding.v1",
        "resolved_source_path": resolved,
        "document_id": document_id,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "blocks": blocks,
    }


def _source_binding_evidence(
    store: "GraphStore",
    source: str | PathLike[str],
    binding: object,
) -> tuple[bool, dict[str, Any]]:
    """Validate current bytes and Block artifacts against one pinned generation."""

    import hashlib

    if not isinstance(binding, dict) or binding.get("schema_version") != "source_binding.v1":
        return False, {"reason": "missing_or_unsupported_source_binding"}
    resolved = _resolved_source_path(source)
    raw = _read_source_bytes(source)
    if raw is None:
        return False, {"reason": "source_bytes_unreadable", "resolved_source_path": resolved}
    observed = {
        "resolved_source_path": resolved,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
    }
    if (
        resolved != str(binding.get("resolved_source_path") or "")
        or observed["sha256"] != str(binding.get("sha256") or "")
        or observed["byte_length"] != int(binding.get("byte_length") or -1)
    ):
        return False, {"reason": "source_generation_changed", **observed}
    blocks = binding.get("blocks")
    if not isinstance(blocks, list):
        return False, {"reason": "invalid_bound_block_manifest", **observed}
    for item in blocks:
        if not isinstance(item, dict):
            return False, {"reason": "invalid_bound_block", **observed}
        block_id = str(item.get("block_id") or "")
        block = store.get_node(block_id)
        if block is None or block.type != "Block":
            return False, {"reason": "bound_block_missing", "block_id": block_id, **observed}
        facets = block.facets or {}
        byte_start = int(item.get("byte_start") or 0)
        byte_end = int(item.get("byte_end") or 0)
        content_hash = str(item.get("content_hash") or "")
        if (
            str(facets.get("content_hash") or "") != content_hash
            or int(facets.get("byte_start") or 0) != byte_start
            or int(facets.get("byte_end") or 0) != byte_end
            or hashlib.sha256(raw[byte_start:byte_end]).hexdigest() != content_hash
        ):
            return False, {
                "reason": "bound_block_generation_changed",
                "block_id": block_id,
                **observed,
            }
    return True, {"reason": "source_binding_verified", **observed}
    return None


def _read_slice(
    path: str,
    byte_start: int,
    byte_end: int,
    *,
    vault_root: "str | PathLike[str] | None" = None,
) -> str:
    """Decode a source byte-slice, ``""`` on any read/anchor problem.

    ``vault_root`` is the ADR 0011 Phase 3 security gate: when supplied (Tier-2
    block reads only), the resolved ``path`` is re-validated to be under the vault
    root BEFORE any bytes are read — a traversal/out-of-vault path returns ``""``
    instead of leaking file contents. When ``None`` (the default OFF block-dump
    path) the guard is dormant and behaviour is byte-identical to before."""
    try:
        if path and byte_end > byte_start and _is_within_root(path, vault_root):
            raw = Path(path).read_bytes()[byte_start:byte_end]
            return raw.decode("utf-8", errors="replace").strip()
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def _source_total_size(path: str, vault_root: "str | PathLike[str] | None" = None) -> int | None:
    """Byte size of a source file, or ``None`` when it cannot be trusted/read.

    SECURITY: goes through the SAME ``_is_within_root`` gate as ``_read_slice``.
    Stat-ing a path the read guard would have refused would disclose the
    existence/size of a file outside the vault."""
    try:
        if path and _is_within_root(path, vault_root):
            return Path(path).stat().st_size
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _hit_source_spans(
    hit: QueryHit, *, vault_root: "str | PathLike[str] | None" = None
) -> "tuple[str, list[tuple[int, int]]]":
    """``_hit_source_pieces`` plus the byte spans that actually READ.

    Returns ``(text, spans)`` where ``spans`` are the ``(byte_start, byte_end)``
    pairs whose ``_read_slice`` came back non-empty, in document order — i.e.
    exactly the ranges the assembled text covers. Spans whose read failed are
    omitted, so a marker built from them never claims bytes the model was not
    shown."""
    prov = hit.provenance
    pieces: list[tuple[int, int, str]] = []
    if prov and prov.path:
        primary = _read_slice(prov.path, prov.byte_start, prov.byte_end, vault_root=vault_root)
        if primary:
            pieces.append((prov.byte_start, prov.byte_end, primary))
        for span in getattr(hit, "context_spans", ()):
            if span.path == prov.path:
                text = _read_slice(span.path, span.byte_start, span.byte_end, vault_root=vault_root)
                if text:
                    pieces.append((span.byte_start, span.byte_end, text))
    if not pieces:
        return "", []
    pieces.sort(key=lambda p: p[0])
    return (
        "\n".join(text for _, _, text in pieces),
        [(start, end) for start, end, _ in pieces],
    )


def _hit_source_pieces(hit: QueryHit, *, vault_root: "str | PathLike[str] | None" = None) -> str:
    """Read-only assembly of the source byte-slice(s) anchored to ``hit``: the
    primary provenance span plus any same-document neighbor ``context_spans``
    (D5 read-time context recovery), in document order.

    Returns ``""`` when there is no anchor at all, OR when the anchor's path/
    byte-range no longer reads (file moved/deleted, a stale ``byte_end=0``
    Document anchor, a blanked ``path``). This function NEVER falls back to
    the node's name — callers decide whether a name fallback is honest for
    their use (``_hit_text`` does; ``_hit_context_snippet`` does not, for the
    anchor-present-but-rotted case).

    Thin wrapper over :func:`_hit_source_spans`, which carries the byte spans
    the excerpt markers need. The plain-``str`` contract here is pinned by
    ``_hit_text`` and existing tests, so it stays."""
    text, _spans = _hit_source_spans(hit, vault_root=vault_root)
    return text


def _hit_text(hit: QueryHit, *, vault_root: "str | PathLike[str] | None" = None) -> str:
    """The grounding text for a retrieved hit: the source byte-slice the hit's
    provenance anchors to (the real paragraph), expanded with any same-document
    neighbor blocks (D5 read-time context recovery) in document order, falling
    back to the node's name.

    Neighbor slices come from ``hit.context_spans`` — same ``path`` as the
    primary anchor, so no new source is read beyond what provenance already
    points at. Without this, ``ask`` would feed the LLM node *titles* instead of
    content, starving synthesis of the facts it just ingested.

    Kept name-fallback-always for backward compatibility (existing direct
    callers/tests pin this contract). The ask-trace path that needs to know
    whether a name was silently substituted for missing source bytes uses
    ``_hit_context_snippet`` instead — see that function for why."""
    text = _hit_source_pieces(hit, vault_root=vault_root)
    if text:
        return text
    return hit.node.name or hit.node.id


_EXCERPT_MARKER_TOKEN = "[EXCERPT"
"""Opening token of the excerpt marker. Single source of truth for the marker
format, the prompt-side sentence that explains it, and the tests that pin it."""


def _format_excerpt_marker(
    relative_path: str | None,
    spans: "Sequence[tuple[int, int]]",
    total_size: int | None,
) -> str:
    """Render the one-line EXCERPT marker prepended to a source block.

    STABLE FORMAT (pinned by tests) — one line, no newlines::

        [EXCERPT source=cnpj/guias/catalogo.md bytes=0-5959,14643-15070 of 32111]

    Every component degrades independently: an un-relativizable path drops the
    ``source=`` field entirely (NON-DISCLOSURE — an absolute path must never
    reach the LLM or the answer), an unreadable size drops ``of <n>``.

    WHY: without this the model cannot tell a whole file from two byte slices of
    it, so a value living outside the served slices reads as "absent from the
    record" and gets extrapolated (the R$ 470,58 incident). Kept to one short
    line because it is repeated per block and competes for the source budget."""
    parts = [_EXCERPT_MARKER_TOKEN]
    if relative_path:
        parts.append(f"source={relative_path}")
    if spans:
        parts.append("bytes=" + ",".join(f"{start}-{end}" for start, end in spans))
    if total_size is not None:
        parts.append(f"of {total_size}")
    return " ".join(parts) + "]"


def _hit_excerpt_marker(
    hit: QueryHit, *, vault_root: "str | PathLike[str] | None" = None
) -> "tuple[str, str, bool]":
    """``(marker, text, from_source)`` for one hit.

    ``marker`` is ``""`` whenever ``from_source`` is False: a node-NAME fallback
    is not an excerpt of anything, and labelling it with a byte range would be a
    fabricated provenance claim."""
    text, spans = _hit_source_spans(hit, vault_root=vault_root)
    if not text:
        snippet, from_source = _hit_context_snippet(hit, vault_root=vault_root)
        return "", snippet, from_source
    prov = hit.provenance
    path = prov.path if prov else ""
    marker = _format_excerpt_marker(
        _vault_relative(path, vault_root) if vault_root else None,
        spans,
        _source_total_size(path, vault_root),
    )
    return marker, text, True


def _hit_context_snippet(
    hit: QueryHit, *, vault_root: "str | PathLike[str] | None" = None
) -> "tuple[str, bool]":
    """Ask-trace-honest counterpart to ``_hit_text``.

    Distinguishes two cases that ``_hit_text`` treats identically:
    - **No anchor at all** (no ``provenance.path``): falls back to the node's
      name, same as ``_hit_text`` — a nameless hit never claimed block
      grounding, so this is not a regression.
    - **Anchor present but the read failed** (source file moved/deleted, a
      Document with a stale ``byte_end=0`` anchor, a blanked stored path):
      the anchor rotted. This does NOT fall back to the node's name — a name
      is not source text, and silently substituting one would let
      ``source_blocks_used`` claim block grounding that never happened.

    Returns ``(snippet, from_source)``; ``from_source`` is True only when
    real source bytes were read for this hit."""
    text = _hit_source_pieces(hit, vault_root=vault_root)
    if text:
        return text, True
    prov = hit.provenance
    if prov and prov.path:
        # Anchor present but every read off it came back empty — do not
        # fabricate a source block out of the node's name.
        return "", False
    return hit.node.name or hit.node.id, False


__all__ = [
    "Sensitivity",
    "ProgressCallback",
    "CorrelationKind",
    "OutcomeAction",
    "ReviewAction",
    "ReviewReason",
    "Correlation",
    "CandidateOutcome",
    "RememberResult",
    "Answer",
    "ReviewItem",
    "CompanionError",
    "LLMUnavailableError",
    "ReviewItemNotFoundError",
    "SourceOutsideVaultError",
    "RememberCancelled",
    "Companion",
    "_ASK_SYSTEM",
    "_ASK_SYSTEM_GRAPH",
    "_ASK_COVERAGE_GRAPH_DEFAULT",
]
