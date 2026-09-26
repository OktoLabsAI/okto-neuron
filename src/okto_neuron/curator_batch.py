"""ADR 0015 D4 — block-keyed batched curation with individual-call fallback.

A batch is ONE source excerpt plus K candidates from that same block, judged in
a single LLM call. The single-call path in :mod:`okto_neuron.curator` remains the
semantic ground truth: any candidate whose batch verdict fails the deterministic
validation gate (missing id, duplicated id, malformed fields, unparseable reply)
is re-run individually. Batching is purely an optimization layer that degrades
to today's behavior, never below it.

Prompt construction deliberately keeps the shared excerpt section as a
byte-identical prefix of the single-call prompt for the same block, preserving
the provider-side prefix-cache win from ADR 0015 P1.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

from okto_neuron.config._capacity import RELATION_BATCH_MAX as _RELATION_BATCH_MAX
from okto_neuron.curator import (
    CuratorVerdict,
    _clamp,
    _relation_curator_verdict_from_mapping,
    normalize_predicate,
    relation_registry_section,
)
from okto_neuron.llm import (
    LLMProviderError,
    Message,
    ResponseFormat,
    _scoped_call_timeout,
    bind_parent,
    last_call_stats,
)
from okto_neuron.llm._structured_output import extract_json_object

if TYPE_CHECKING:
    from okto_neuron.llm import LLMProvider

# Hard ceiling on the scaled completion budget for a batch call
# (per-candidate max_tokens × K, capped here so K=32 cannot demand an
# unreasonable decode budget from the server).
BATCH_MAX_TOKENS_CAP = 16384

# The relation-batch cap lives with the other effective-capacity policy in
# ``config/_capacity.py`` (applied where the size is consumed, so the Config UI
# can show configured vs effective); re-exported here beside the token cap it
# sits next to conceptually. See its definition for the measurements behind 4.
RELATION_BATCH_MAX = _RELATION_BATCH_MAX

_BATCH_ACTIONS = ("commit", "queue", "abstain")
_RELATION_BATCH_ACTIONS = ("commit", "queue")
_RELATION_BATCH_FIELDS = frozenset(
    {
        "candidate_id",
        "action",
        "confidence",
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
        "reason",
    }
)

# Appended verbatim to the existing single-call system prompt — the curator
# rules themselves are NOT rewritten for batch mode.
_BATCH_SYSTEM_SUFFIX = (
    "\n\nBatch mode: you will receive {k} independent candidates judged against "
    "ONE shared source excerpt. Judge each candidate STRICTLY independently, as "
    "if it were the only candidate in the request; do not let one verdict "
    "influence another. Return ONLY JSON: "
    '{{"verdicts": [...]}} with exactly one verdict object per candidate, '
    "echoing each candidate_id verbatim."
)


def batch_system_prompt(base_system: str, k: int) -> str:
    """The single-call system prompt verbatim + the batch instruction block."""
    return base_system + _BATCH_SYSTEM_SUFFIX.format(k=k)


def batch_verdict_response_format(n: int, *, relation: bool) -> ResponseFormat:
    """json_schema response format for an n-candidate batch reply.

    Field semantics mirror ``CURATOR_RESPONSE_FORMAT`` /
    ``RELATION_CURATOR_RESPONSE_FORMAT`` exactly, with ``candidate_id`` added so
    each verdict is keyed back to its candidate.
    """
    properties: dict[str, Any] = {
        "candidate_id": {"type": "string"},
        "action": {
            "type": "string",
            "enum": list(_RELATION_BATCH_ACTIONS if relation else _BATCH_ACTIONS),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
    }
    if relation:
        properties.update(
            {
                "canonical_predicate": {"type": "string"},
                "predicate_definition": {"type": "string"},
                "predicate_direction": {
                    "type": "string",
                    "enum": ["subject_to_object", "symmetric", "unknown"],
                },
                "inverse_direction_required": {"type": "boolean"},
                "subject_supported": {"type": "boolean"},
                "predicate_supported": {"type": "boolean"},
                "object_supported": {"type": "boolean"},
                "direction_supported": {"type": "boolean"},
                "unsupported_inference": {"type": "boolean"},
                "structural_noise": {"type": "boolean"},
                "redundant": {"type": "boolean"},
                "useful": {"type": "boolean"},
            }
        )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": (
                "marginalia_relation_curator_batch"
                if relation
                else "marginalia_candidate_curator_batch"
            ),
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "verdicts": {
                        "type": "array",
                        "minItems": n,
                        "maxItems": n,
                        "items": {
                            "type": "object",
                            "properties": properties,
                            "required": list(properties),
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["verdicts"],
                "additionalProperties": False,
            },
        },
    }


def shared_excerpt_section(excerpt: str) -> str:
    """The excerpt section exactly as the single-call prompt builders emit it.

    Must stay byte-identical to the ``f"Source excerpt:\\n{...}\\n\\n"`` prefix
    of ``_build_curator_prompt`` / ``_build_relation_prompt`` so the provider's
    prefix cache is reused across the batch and single-call paths.
    """
    return f"Source excerpt:\n{excerpt or '  (not available)'}\n\n"


def relation_shared_prefix(excerpt: str, registry_block: str) -> str:
    """The relation prompt's shared prefix: excerpt, then known predicates.

    Must stay byte-identical to the leading bytes of ``_build_relation_prompt``
    for the same block and run, or ``build_batch_user_prompt``'s strip below
    silently no-ops and the batch pays K copies of both sections.
    """
    return shared_excerpt_section(excerpt) + relation_registry_section(registry_block)


def build_batch_user_prompt(
    excerpt: str,
    members: Sequence[tuple[str, str]],
    *,
    prefix: str | None = None,
) -> str:
    """Batch user prompt: shared excerpt FIRST, then per-candidate sections.

    ``members`` pairs each ``candidate_id`` with its prebuilt single-call user
    prompt; the shared prefix is stripped from each so it is paid once per
    batch. ``prefix`` defaults to the excerpt section alone (the node-curator
    shape); the relation path passes :func:`relation_shared_prefix` so the
    registry block is inside the stripped prefix rather than repeated K times.
    """
    prefix = shared_excerpt_section(excerpt) if prefix is None else prefix
    parts = [prefix]
    parts.append(
        f"You are given {len(members)} independent candidates against the "
        "source excerpt above. Judge each one independently.\n\n"
    )
    for candidate_id, prompt in members:
        body = prompt[len(prefix) :] if prompt.startswith(prefix) else prompt
        parts.append(f"=== candidate_id: {candidate_id} ===\n{body.strip()}\n\n")
    parts.append('Return ONLY JSON: {"verdicts": [one verdict object per candidate_id above]}.')
    return "".join(parts)


def _load_json_object(text: str) -> Any:
    parsed, _note = extract_json_object(text)
    return parsed


def parse_batch_verdicts(
    reply: str, expected_ids: Sequence[str], *, relation: bool = False
) -> dict[str, CuratorVerdict]:
    """Deterministic validation gate over a batch reply.

    Strict by construction: only verdicts whose ``candidate_id`` is expected,
    appears exactly once, has an action in the enum, and a coercible 0..1
    confidence make it into the result. Duplicated ids drop ALL their entries
    (strictest interpretation). Everything absent from the returned mapping is
    the caller's signal to fall back to the single-call path. Extra/unknown
    fields on a verdict object are never fatal.
    """
    expected = set(expected_ids)
    data = _load_json_object(reply or "")
    if not isinstance(data, dict):
        return {}
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}
    id_counts: dict[str, int] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        candidate_id = entry.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id in expected:
            id_counts[candidate_id] = id_counts.get(candidate_id, 0) + 1

    out: dict[str, CuratorVerdict] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        candidate_id = entry.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            continue
        if id_counts.get(candidate_id) != 1:
            continue
        if relation and set(entry) != _RELATION_BATCH_FIELDS:
            continue
        action = entry.get("action")
        allowed_actions = _RELATION_BATCH_ACTIONS if relation else _BATCH_ACTIONS
        if action not in allowed_actions:
            continue
        try:
            confidence = float(entry.get("confidence"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if relation:
            verdict = _relation_curator_verdict_from_mapping(
                entry,
                allow_candidate_id=True,
            )
            if verdict.action == "abstain":
                continue
            out[candidate_id] = verdict
        else:
            out[candidate_id] = CuratorVerdict(
                action=action,
                confidence=_clamp(confidence),
                reason=str(entry.get("reason", ""))[:300],
                canonical_predicate=normalize_predicate(entry.get("canonical_predicate")),
            )
    return out


@dataclass(frozen=True)
class BatchCurationItem:
    """One pre-pass curation item, ready for either the batch or single path."""

    candidate_id: str
    block_id: str | None
    excerpt: str
    prompt: str
    trace_context: dict[str, Any]
    single_call: Callable[[], CuratorVerdict]


def plan_batches(items: Sequence[BatchCurationItem], batch_size: int) -> list[list[int]]:
    """Group item indices by block (batch key = block) and chunk to batch_size.

    Items with no block_id form singleton batches (they have no shared excerpt
    to amortize). Group order follows first appearance; within a group the
    original input order is preserved.
    """
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    for idx, item in enumerate(items):
        key = item.block_id if item.block_id else f"__noblock__{idx}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(idx)
    batches: list[list[int]] = []
    size = max(1, batch_size)
    for key in order:
        idxs = groups[key]
        for start in range(0, len(idxs), size):
            batches.append(idxs[start : start + size])
    return batches


def iter_batched_curation(
    items: Sequence[BatchCurationItem],
    *,
    provider: "LLMProvider",
    system_prompt: str,
    relation: bool,
    batch_size: int,
    max_concurrent: int,
    timeout_s: float | None,
    temperature: float,
    max_tokens: int | None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    enable_thinking: bool | None = None,
    # Relation path only: the known-predicate block that must sit inside the
    # stripped shared prefix rather than be repeated once per batch member.
    registry_block: str = "",
    set_trace_context: Callable[[dict[str, Any]], None] = lambda _ctx: None,
    fallback_runner: Callable[[Sequence[Callable[[], CuratorVerdict]]], list[CuratorVerdict]]
    | None = None,
) -> Iterator[tuple[CuratorVerdict, dict[str, Any] | None]]:
    """Curate ``items`` with block-keyed batch calls + single-call fallback,
    yielding ``(verdict, batch_meta)`` pairs in input order AS BATCHES RETURN.

    ADR 0015 D5a, batched flavor: this is a generator so the consuming
    post-pass can ledger each batch's verdicts as soon as that batch (and its
    per-member single-call fallback, if any) completes, instead of waiting for
    the whole phase — restoring live progress and the per-verdict durability
    window that mid-run resume (D5b) keys on. Yield order is the ordered
    prefix of the input: item *i* is yielded once every item ``0..i`` is
    resolved, so interleaved-block plans simply buffer briefly.

    For a verdict produced by a batch call, its meta entry is
    ``{"batch_id": <uuid hex>, "batch_size": K}``; for singleton batches and
    fallback re-curations the meta entry is ``None`` (single-call provenance,
    unchanged ledger shape). Any batch member failing the deterministic
    validation gate is re-curated through its single call immediately after
    its batch returns (same per-candidate fallback as before — only the WHEN
    moved from phase end to batch end).

    Usage attribution choice (telemetry, ADR 0015 D3.3): the batch call's
    wall-clock ``duration_s`` lands on EVERY member verdict (it is the same
    wall-clock the member waited for), but the provider-reported token
    ``usage`` is recorded on the FIRST valid member only and omitted on the
    rest — so the ledger's per-method token aggregation (which sums ``usage``
    across comparison payloads) counts each batch exactly once with no ledger
    changes.
    """
    if not items:
        return

    batches = plan_batches(items, batch_size)
    multi_batches = [b for b in batches if len(b) > 1]
    worker_count = max(1, min(max_concurrent, max(len(multi_batches), 1)))
    call_capacity = threading.BoundedSemaphore(max(1, max_concurrent))

    def _run_batch(
        idxs: list[int],
    ) -> dict[int, tuple[CuratorVerdict, dict[str, Any]]]:
        members = [items[i] for i in idxs]
        k = len(members)
        batch_id = uuid.uuid4().hex
        set_trace_context(
            {
                "batch_id": batch_id,
                "batch_size": k,
                "block_id": members[0].block_id,
                "candidate_ids": [m.candidate_id for m in members],
            }
        )
        # The relation prompt's shared prefix is excerpt + known-predicate
        # block; stripping only the excerpt would leave K copies of the block
        # in the batch body (the strip is a silent no-op on mismatch).
        user = build_batch_user_prompt(
            members[0].excerpt,
            [(m.candidate_id, m.prompt) for m in members],
            prefix=(
                relation_shared_prefix(members[0].excerpt, registry_block) if relation else None
            ),
        )
        started = time.perf_counter()
        # An absent limit means provider/model default; only scale an explicit budget.
        batch_max_tokens = (
            min(max(max_tokens, 1) * k, BATCH_MAX_TOKENS_CAP) if max_tokens is not None else None
        )
        try:
            with _scoped_call_timeout(timeout_s):
                with call_capacity:
                    reply = provider.complete(
                        [
                            Message("system", batch_system_prompt(system_prompt, k)),
                            Message("user", user),
                        ],
                        temperature=temperature,
                        max_tokens=batch_max_tokens,
                        top_p=top_p,
                        top_k=top_k,
                        min_p=min_p,
                        presence_penalty=presence_penalty,
                        enable_thinking=enable_thinking,
                        response_format=batch_verdict_response_format(k, relation=relation),
                    )
        except LLMProviderError:
            return {}
        elapsed_s = time.perf_counter() - started
        duration_s = round(elapsed_s, 3)
        if timeout_s is not None and elapsed_s >= timeout_s:
            return {}
        usage = last_call_stats()
        parsed = parse_batch_verdicts(
            reply,
            [m.candidate_id for m in members],
            relation=relation,
        )
        results: dict[int, tuple[CuratorVerdict, dict[str, Any]]] = {}
        first_valid = True
        for i in idxs:
            verdict = parsed.get(items[i].candidate_id)
            if verdict is None:
                continue
            verdict = replace(
                verdict,
                duration_s=duration_s,
                usage=usage if first_valid else None,
            )
            first_valid = False
            results[i] = (verdict, {"batch_id": batch_id, "batch_size": k})
        return results

    resolved: dict[int, tuple[CuratorVerdict, dict[str, Any] | None]] = {}
    emit_at = 0

    def _fallback(idxs: list[int]) -> None:
        # Singletons + anything the batch path could not validate take the
        # existing single-call path — the semantic ground truth.
        def _bounded_single(call: Callable[[], CuratorVerdict]) -> CuratorVerdict:
            with _scoped_call_timeout(timeout_s):
                with call_capacity:
                    return call()

        calls = [lambda call=items[i].single_call: _bounded_single(call) for i in idxs]
        if fallback_runner is not None:
            fallback_verdicts = fallback_runner(calls)
        elif max_concurrent > 1 and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(max_concurrent, len(calls))) as fallback_pool:
                fallback_verdicts = list(fallback_pool.map(lambda call: call(), calls))
        else:
            fallback_verdicts = [call() for call in calls]
        for i, verdict in zip(idxs, fallback_verdicts):
            resolved[i] = (verdict, None)

    def _complete(
        idxs: list[int],
        batch_results: dict[int, tuple[CuratorVerdict, dict[str, Any]]],
    ) -> None:
        for i, (verdict, meta) in batch_results.items():
            resolved[i] = (verdict, meta)
        missing = [i for i in idxs if i not in resolved]
        if missing:
            _fallback(missing)

    def _drain() -> Iterator[tuple[CuratorVerdict, dict[str, Any] | None]]:
        nonlocal emit_at
        while emit_at < len(items) and emit_at in resolved:
            yield resolved.pop(emit_at)
            emit_at += 1

    if max_concurrent > 1 and len(multi_batches) > 1:
        multi_indices = [index for index, batch in enumerate(batches) if len(batch) > 1]
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures: dict[int, Future[dict[int, tuple[CuratorVerdict, dict[str, Any]]]]] = {}
            next_multi = 0

            def _top_up() -> None:
                nonlocal next_multi
                while len(futures) < worker_count and next_multi < len(multi_indices):
                    batch_index = multi_indices[next_multi]
                    futures[batch_index] = pool.submit(
                        bind_parent(_run_batch), batches[batch_index]
                    )
                    next_multi += 1

            _top_up()
            for batch_index, batch in enumerate(batches):
                if len(batch) == 1:
                    _fallback(batch)
                else:
                    future = futures.pop(batch_index)
                    try:
                        batch_results = future.result(timeout=timeout_s)
                    except FuturesTimeoutError:
                        batch_results = {}  # fail closed: all members fall back
                    except Exception:  # noqa: BLE001 — fall back per candidate
                        batch_results = {}
                    _complete(batch, batch_results)
                    _top_up()
                yield from _drain()
    else:
        for batch in batches:
            if len(batch) == 1:
                _fallback(batch)
            else:
                try:
                    batch_results = _run_batch(batch)
                except Exception:  # noqa: BLE001 — fall back per candidate
                    batch_results = {}
                _complete(batch, batch_results)
            yield from _drain()


def run_batched_curation(
    items: Sequence[BatchCurationItem],
    **kwargs: Any,
) -> tuple[list[CuratorVerdict], list[dict[str, Any] | None]]:
    """List-collecting wrapper over :func:`iter_batched_curation`.

    Returns ``(verdicts, batch_meta)`` both aligned to the input order, with
    identical semantics — kept for callers/tests that need the whole verdict
    set at once.
    """
    pairs = list(iter_batched_curation(items, **kwargs))
    return [verdict for verdict, _meta in pairs], [meta for _verdict, meta in pairs]


__all__ = [
    "BATCH_MAX_TOKENS_CAP",
    "RELATION_BATCH_MAX",
    "BatchCurationItem",
    "batch_system_prompt",
    "batch_verdict_response_format",
    "build_batch_user_prompt",
    "iter_batched_curation",
    "relation_shared_prefix",
    "parse_batch_verdicts",
    "plan_batches",
    "run_batched_curation",
    "shared_excerpt_section",
]
