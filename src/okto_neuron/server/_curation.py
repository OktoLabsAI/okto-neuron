"""Curation domain glue for the daemon: reconcile/authority runners + the
read-time equivalence fold (ADR 0009 P2).

CRITICAL invariant (ADR 0007/0034): every runner receives one immutable runtime
and operates on its pool-owned leased handle. It NEVER opens a second ``Vault``
or ``VaultConnection`` for that path. The CLI path
(``cli/kg.py:_open_reconcile_context``) opens its own Vault, which is the
second-handle corruption path and is precisely why CLI reconcile forced the UI
down. Here we pass the runtime handle's ``store`` and ``embedder`` straight into
reconcile functions that never open a handle themselves.

The off-graph side-stores (``AuthorityIndex``, ``ReconcileQueue``) are pure JSON
under ``<vault>/.marginalia/`` and hold NO store reference, so they cannot write
the graph by construction. ``apply_reconciliation`` asserts off-graph by never
calling ``add_node``/``add_edge``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from okto_neuron.config._capacity import curation_effective_max_concurrent
from okto_neuron.errors import RebuildAuditFailed, VaultCorrupted

if TYPE_CHECKING:
    from okto_neuron.server.state import ServerState

_MARGINALIA_DIR = ".marginalia"
_REBUILD_RECOVERY_DIR = "rebuild-recovery"

# Bounded wait (seconds) for the last in-flight ingest item to leave the
# writer_lock/to_thread before the swap — mirrors api_embedding_reembed's 30s gate.
_INGEST_DRAIN_TIMEOUT = 30.0


async def _swap_under_runtime_fence(
    state: "ServerState",
    job: Any,
    swap_graph: Callable[[], None],
    *,
    validate_reopened: Callable[[Any], None] | None = None,
) -> None:
    """Drain readers, replace one graph, and publish its handle under a fence.

    The curation worker holds one full-job pool lease so long build/copy phases
    can safely read the live graph. At the final boundary this coroutine fences
    the immutable runtime, releases that worker lease, waits for every earlier
    request lease, and only then lets the path-wide Ladybug close/swap happen.
    """
    from okto_neuron.vault import Vault

    vault_path = Path(state.vault_path).resolve(strict=False)
    pool = state.vault_pool
    if pool.is_fenced(vault_path):
        raise RuntimeError(f"vault is already fenced for maintenance: {vault_path}")

    pool.fence(vault_path)
    state.mark_draining()
    job.release_vault_lease_for_swap()
    safe_to_unfence = True
    ownership = None
    try:
        deadline = time.monotonic() + _INGEST_DRAIN_TIMEOUT
        leases = pool.lease_count(vault_path)
        while leases and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            leases = pool.lease_count(vault_path)
        if leases:
            raise RuntimeError(
                f"vault still has {leases} active lease(s) at swap deadline: {vault_path}"
            )

        pool.release_path(vault_path, require_fenced=True)
        safe_to_unfence = False
        ownership = pool.claim_fenced_ownership(vault_path)
        ownership.require_held()
        swap_graph()
        reopened = Vault.open(vault_path)
        try:
            state.install_fenced_vault(reopened, ownership=ownership)
            ownership = None
        except Exception:
            reopened.close()
            raise
        if validate_reopened is not None:
            await asyncio.to_thread(validate_reopened, reopened)
        safe_to_unfence = True
    except Exception:
        # The graph swap helper rolls its filesystem rename back where possible.
        # Reinstall a pool owner before admitting any new request.
        if not safe_to_unfence and vault_path.exists() and pool.peek(vault_path) is None:
            try:
                if ownership is None:
                    ownership = pool.claim_fenced_ownership(vault_path)
                reopened = Vault.open(vault_path)
                try:
                    state.install_fenced_vault(reopened, ownership=ownership)
                    ownership = None
                except Exception:
                    reopened.close()
                    raise
                safe_to_unfence = True
            except Exception:  # noqa: BLE001
                # Leave the path fenced/draining. A clean restart is required;
                # exposing a known-closed handle would be worse than unavailability.
                pass
        raise
    finally:
        if ownership is not None:
            ownership.release()
        if safe_to_unfence:
            state.draining = False
            pool.unfence(vault_path)


class _FenceAlreadyGuardsSwap:
    """No-op ``RebuildLockHandle`` (``store/rebuild_lock.py``) for the daemon's
    runners (M2b spec §2.2/§2.3).

    ``finish_staged_swap`` calls ``lock.require_held()`` immediately before
    ``commit()`` — for the CLI's offline lease that IS the exclusivity
    guarantee (invariant 2). Online, the equivalent check already happens
    inside the unmodified ``_swap_under_runtime_fence`` above, as
    ``ownership.require_held()`` right before the physical swap; that
    ``ownership`` handle isn't available outside the fence to hand to
    ``finish_staged_swap`` directly, so this stands in as a documented no-op
    rather than a missing check.
    """

    def require_held(self) -> None:
        return None


# ── config-only construction (NO Vault open) ─────────────────────────────────--
def _load_config(state: "ServerState", *, vault_path: "Path | str | None" = None) -> Any:
    from okto_neuron.config import VaultConfig

    path = vault_path if vault_path is not None else state.vault_path
    try:
        return VaultConfig.load(path)
    except Exception:  # noqa: BLE001 — missing/partial config → defaults
        return VaultConfig()


def _judge_model(state: "ServerState", *, vault_path: "Path | str | None" = None) -> str:
    cfg = _load_config(state, vault_path=vault_path)
    resolved = cfg.llm.resolved("judge")
    return getattr(resolved, "model", "") or ""


def _build_judge(
    state: "ServerState",
    *,
    vault_path: "Path | str | None" = None,
    on_completion: Callable[[dict[str, Any] | None], None] | None = None,
):
    """Build the LLM merge judge from config alone (no vault handle needed).

    Mirrors ``cli/kg.py:_open_reconcile_context.build_judge`` but without the
    ``Vault.open`` it is nested inside — the judge only needs the resolved LLM
    provider, never the graph.

    ``vault_path`` keeps the direct-``ServerState`` compatibility branch scoped
    to its target. Application jobs already receive one immutable runtime."""
    from okto_neuron.companion import _StepLabelledProvider
    from okto_neuron.config import VaultConfig
    from okto_neuron.llm import get_provider, sampler_overrides
    from okto_neuron.resolve import LLMMergeJudge

    cfg: VaultConfig = _load_config(state, vault_path=vault_path)
    resolved = cfg.llm.resolved("judge")
    return LLMMergeJudge(
        _StepLabelledProvider(
            get_provider(resolved),
            "judge",
            on_completion=on_completion,
        ),
        **sampler_overrides(resolved),
        top_p=resolved.top_p,
        top_k=resolved.top_k,
        min_p=resolved.min_p,
        presence_penalty=resolved.presence_penalty,
        enable_thinking=resolved.enable_thinking,
        system_prompt=cfg.llm.judge.system_prompt,
    )


def authority_index(state: "ServerState", *, vault_path: "Path | str | None" = None):
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME, AuthorityIndex

    path = vault_path if vault_path is not None else state.vault_path
    return AuthorityIndex(Path(path) / _MARGINALIA_DIR / AUTHORITY_DIRNAME)


def identity_decision_index(state: "ServerState", *, vault_path: "Path | str | None" = None):
    from okto_neuron.reconcile.decisions import IdentityDecisionIndex

    return IdentityDecisionIndex(authority_index(state, vault_path=vault_path).dir)


def reconcile_queue(state: "ServerState", *, vault_path: "Path | str | None" = None):
    from okto_neuron.reconcile.queue import RECONCILE_DIRNAME, ReconcileQueue

    path = vault_path if vault_path is not None else state.vault_path
    return ReconcileQueue(
        Path(path) / _MARGINALIA_DIR / RECONCILE_DIRNAME,
        authority_index(state, vault_path=path),
    )


def predicate_alias_index(state: "ServerState", *, vault_path: "Path | str | None" = None):
    from okto_neuron.predicates import PredicateAliasIndex

    path = vault_path if vault_path is not None else state.vault_path
    return PredicateAliasIndex(Path(path))


def _publish_linked_reconcile_outcome(
    state: "ServerState",
    ingest_item_ids: object,
    outcome: dict[str, object],
    *,
    expected_job_id: str | None = None,
    persist_now: bool = True,
) -> bool:
    """Project one propose job's state onto the ingest items that requested it.

    The graph commit and the semantic follow-up have deliberately separate
    outcomes.  Reconciliation may fail after a technically verified commit; in
    that case the ingest item stays ``done`` and only this additive semantic
    outcome becomes ``failed``. Returns whether any item changed;
    ``persist_now=False`` leaves the sidecar write to an off-loop caller.
    """
    if not isinstance(ingest_item_ids, list):
        return False
    item_ids = {str(value) for value in ingest_item_ids if str(value)}
    if not item_ids:
        return False
    changed = False
    for item in getattr(state, "ingest_queue", ()):
        if str(getattr(item, "id", "")) not in item_ids:
            continue
        current = dict(getattr(item, "outcome", {}) or {})
        linked = current.get("cross_document_reconciliation")
        if expected_job_id and (
            not isinstance(linked, dict) or str(linked.get("job_id") or "") != expected_job_id
        ):
            # The item was retried and cleared/replaced this link while the old
            # proposal was running. Never attach an old run's result to the new one.
            continue
        current["cross_document_reconciliation"] = dict(outcome)
        item.outcome = current
        changed = True
    if changed and persist_now:
        from okto_neuron.server import _ingest_queue

        _ingest_queue.persist(state)
    return changed


# Find-or-submit below must stay atomic: callers now run it on store-executor
# threads (issue #13), where two verified commits could otherwise both miss the
# queued pass and submit two proposals instead of coalescing into one.
_SCHEDULE_LOCK = threading.RLock()


def schedule_cross_document_reconciliation(
    state: "ServerState",
    *,
    trigger: str,
    ingest_item_id: str | None = None,
    graph_generation: str | None = None,
) -> dict[str, object]:
    """Schedule one propose-only reconciliation pass (see
    :func:`_schedule_cross_document_reconciliation`), atomically."""
    with _SCHEDULE_LOCK:
        return _schedule_cross_document_reconciliation(
            state,
            trigger=trigger,
            ingest_item_id=ingest_item_id,
            graph_generation=graph_generation,
        )


def _schedule_cross_document_reconciliation(
    state: "ServerState",
    *,
    trigger: str,
    ingest_item_id: str | None = None,
    graph_generation: str | None = None,
) -> dict[str, object]:
    """Schedule one propose-only reconciliation pass after a verified boundary.

    A queued pass is coalesced so a large folder ingest does not create one
    expensive sweep per file.  A running pass is never reused: a later commit
    needs a new queued snapshot that starts after that commit.  Scheduling
    failure is returned as semantic outcome data and never raises across the
    already-completed graph commit boundary.
    """
    from okto_neuron.server import _jobs

    linked_ids = [ingest_item_id] if ingest_item_id else []
    params: dict[str, object] = {
        "trigger": trigger,
        "ingest_item_ids": linked_ids,
    }
    if graph_generation:
        params["graph_generation"] = graph_generation
    if not bool(getattr(_load_config(state).curation, "enabled", True)):
        outcome: dict[str, object] = {
            "state": "skipped",
            "stage": "schedule",
            "trigger": trigger,
            "reason": "continuous curation is disabled",
        }
        if ingest_item_id:
            _publish_linked_reconcile_outcome(state, [ingest_item_id], outcome)
        return outcome
    try:
        queued = next(
            (
                candidate
                for candidate in reversed(getattr(state, "curation_jobs", ()))
                if (
                    candidate.kind == "reconcile-propose"
                    and candidate.status == "queued"
                    and candidate.params.get("trigger") == trigger
                    and (
                        not graph_generation
                        or not candidate.params.get("graph_generation")
                        or candidate.params.get("graph_generation") == graph_generation
                    )
                )
            ),
            None,
        )
        coalesced = queued is not None
        if queued is None:
            queued = _jobs.submit(
                state,
                "reconcile-propose",
                label="cross-document reconciliation proposal",
                params=params,
            )
        elif ingest_item_id:
            existing = queued.params.get("ingest_item_ids")
            existing_ids = list(existing) if isinstance(existing, list) else []
            if ingest_item_id not in existing_ids:
                existing_ids.append(ingest_item_id)
                queued.params["ingest_item_ids"] = existing_ids
                _jobs.persist(state)
        _jobs.ensure_worker(state)
        outcome = {
            "state": "scheduled",
            "job_id": queued.id,
            "trigger": trigger,
            "coalesced": coalesced,
        }
    except Exception as exc:  # noqa: BLE001 — semantic follow-up cannot roll back commit
        if getattr(state, "draining", False):
            try:
                deferred = _jobs.enqueue_during_drain(
                    state,
                    "reconcile-propose",
                    label="cross-document reconciliation proposal",
                    params=params,
                )
                outcome = {
                    "state": "scheduled",
                    "job_id": deferred.id,
                    "trigger": trigger,
                    "coalesced": False,
                }
                if bool(getattr(state, "shutting_down", False)):
                    outcome["deferred_until_restart"] = True
                else:
                    outcome["deferred_until_maintenance_end"] = True
            except Exception as deferred_exc:  # noqa: BLE001
                outcome = {
                    "state": "failed",
                    "stage": "schedule",
                    "trigger": trigger,
                    "error_category": type(deferred_exc).__name__,
                    "error": str(deferred_exc)[:500],
                }
        else:
            outcome = {
                "state": "failed",
                "stage": "schedule",
                "trigger": trigger,
                "error_category": type(exc).__name__,
                "error": str(exc)[:500],
            }

    if ingest_item_id:
        _publish_linked_reconcile_outcome(state, [ingest_item_id], outcome)
    return outcome


def attach_verified_reconciliation_outcome(
    state: "ServerState",
    outcome: dict[str, object] | None,
    *,
    trigger: str,
    ingest_item_id: str | None = None,
) -> dict[str, object]:
    """Schedule D8 only when Companion proves the post-write generation safe."""
    enriched = dict(outcome or {})
    if str(enriched.get("quality") or "") in {"failed", "integrity_failed"}:
        return enriched
    integrity = enriched.get("integrity")
    if not isinstance(integrity, dict) or str(integrity.get("status") or "") not in {
        "verified",
        "not_applicable",
    }:
        return enriched
    reconciliation = schedule_cross_document_reconciliation(
        state,
        trigger=trigger,
        ingest_item_id=ingest_item_id,
        graph_generation=(str(integrity.get("graph_generation") or "") or None),
    )
    enriched["cross_document_reconciliation"] = reconciliation
    return enriched


# ── serialization helpers (mirror the CLI JSON shapes) ──────────────────────────
def cluster_verdict_row(cluster: Any, verdict: Any) -> dict:
    """One propose row — mirrors ``cli/kg.py:kg_reconcile_propose`` JSON."""
    return {
        "cluster_id": cluster.cluster_id,
        "type": cluster.type,
        # Candidate discovery is intentionally permissive, so one connected
        # component may contain both accepted aliases and members the judge
        # rejected as distinct. Keep those sets explicit: ``member_ids`` is the
        # adjudicated survivor set, while ``candidate_member_ids`` preserves the
        # complete discovery evidence for inspection.
        "candidate_member_ids": list(cluster.member_ids),
        "member_ids": list(verdict.member_ids),
        "corroborated_ids": list(verdict.corroborated_ids),
        "lanes": sorted(cluster.lane_evidence.keys()),
        "same": verdict.same,
        "confidence": verdict.confidence,
        "canonical_id": verdict.canonical_id,
        "corroboration": verdict.corroboration,
        "reason": verdict.reason,
    }


def queued_cluster_row(qc: Any) -> dict:
    """One reconcile-review-queue entry for the UI (cluster + verdict + titles)."""
    titles = dict(qc.correlations.get("titles", {}))
    return {
        "cluster_id": qc.cluster.cluster_id,
        "type": qc.cluster.type,
        "confidence": qc.verdict.confidence,
        "corroboration": qc.verdict.corroboration,
        "canonical_id": qc.verdict.canonical_id,
        "reason": qc.verdict.reason,
        "member_ids": list(qc.cluster.member_ids),
        "members": [{"id": m, "title": titles.get(m, m)} for m in qc.cluster.member_ids],
        "lanes": sorted(qc.cluster.lane_evidence.keys()),
    }


def authority_record_row(rec: Any) -> dict:
    return rec.to_json()


def predicate_record_row(rec: Any) -> dict:
    return rec.to_json()


def predicate_outcome_row(result: Any) -> dict:
    record = result.to_record()
    return {
        "pair": [result.predicate_a, result.predicate_b],
        "mapping": result.mapping,
        "status": result.status,
        "outcome": result.outcome,
        "canonical": result.canonical,
        "confidence": result.confidence,
        "reason": result.reason,
        "duration_s": result.duration_s,
        "usage": result.usage,
        "evidence": result.evidence,
        "judge_model": result.judge_model,
        "record": record.to_json(),
    }


TriageAction = Literal["commit", "discard", "keep"]
_TRIAGE_DISCARD_THRESHOLD = 0.85
_TRIAGE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class _TriageVerdict:
    action: TriageAction
    confidence: float = 0.0
    reason: str = ""
    duration_s: float | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class _TriageItem:
    candidate: Any
    prompt: str
    block_id: str | None
    excerpt: str

    @property
    def candidate_id(self) -> str:
        return str(self.candidate.candidate_id)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _load_json_object(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    match = _TRIAGE_JSON_RE.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (TypeError, ValueError):
        return None


def _triage_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "marginalia_companion_triage",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["commit", "discard", "keep"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reason": {"type": "string"},
                },
                "required": ["action", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    }


def _triage_batch_response_format(n: int) -> dict[str, object]:
    verdict = {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "action": {"type": "string", "enum": ["commit", "discard", "keep"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string"},
        },
        "required": ["candidate_id", "action", "confidence", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "marginalia_companion_triage_batch",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "verdicts": {
                        "type": "array",
                        "minItems": n,
                        "maxItems": n,
                        "items": verdict,
                    }
                },
                "required": ["verdicts"],
                "additionalProperties": False,
            },
        },
    }


def _parse_triage_action(value: object) -> TriageAction | None:
    raw = str(value or "").strip().casefold()
    if raw in {"commit", "discard", "keep"}:
        return raw  # type: ignore[return-value]
    if raw in {"queue", "abstain"}:
        return "keep"
    return None


def _triage_verdict_from_mapping(data: Any) -> _TriageVerdict | None:
    if not isinstance(data, dict):
        return None
    action = _parse_triage_action(data.get("action"))
    if action is None:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return _TriageVerdict(
        action=action,
        confidence=_clamp01(confidence),
        reason=str(data.get("reason", ""))[:300],
    )


def _parse_triage_verdict(text: str) -> _TriageVerdict:
    data = _load_json_object(text)
    verdict = _triage_verdict_from_mapping(data)
    if verdict is not None:
        return verdict
    return _TriageVerdict(action="keep", confidence=0.0, reason="unparseable")


def _parse_triage_batch_verdicts(text: str, expected_ids: list[str]) -> dict[str, _TriageVerdict]:
    data = _load_json_object(text)
    if not isinstance(data, dict):
        return {}
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return {}
    expected = set(expected_ids)
    out: dict[str, _TriageVerdict] = {}
    duplicated: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        candidate_id = entry.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in expected:
            continue
        if candidate_id in out or candidate_id in duplicated:
            out.pop(candidate_id, None)
            duplicated.add(candidate_id)
            continue
        verdict = _triage_verdict_from_mapping(entry)
        if verdict is not None:
            out[candidate_id] = verdict
    return out


def _same_type_established_context(candidate: Any, store: Any) -> dict[str, Any]:
    from okto_neuron.consolidate.prefilter import is_established_re_mention
    from okto_neuron.semantic_surface import discovery_surface_key

    title = discovery_surface_key(getattr(candidate, "title", ""))
    if not title:
        return {"exists": False}
    for node in store.list_nodes(type=getattr(candidate, "type", None)):
        if getattr(node, "id", "") == getattr(candidate, "candidate_id", ""):
            continue
        node_title = discovery_surface_key(getattr(node, "title", ""))
        if not node_title:
            continue
        similar = node_title == title
        if not similar and min(len(node_title), len(title)) >= 4:
            similar = title in node_title or node_title in title
        if not similar:
            continue
        return {
            "exists": True,
            "node_id": getattr(node, "id", ""),
            "node_title": getattr(node, "title", ""),
            "established_re_mention": is_established_re_mention(candidate, node),
        }
    return {"exists": False}


def _triage_proposed_action(
    candidate: Any,
    *,
    store: Any,
    gate_threshold: float,
) -> str:
    context = _same_type_established_context(candidate, store)
    lines = [
        "  resolver_proposal: triage this parked review-queue candidate",
        f"  gate_threshold: {gate_threshold:.3f}",
        f"  discard_threshold: {_TRIAGE_DISCARD_THRESHOLD:.3f}",
        "  triage_commit: return commit only when the candidate is grounded, useful, distinct, and ready for the graph",
        "  triage_discard: return discard only when it is clearly invalid, duplicate, ungrounded, or non-knowledge",
        "  triage_keep: return keep when a human should still review it",
        f"  same_type_similar_title_exists: {str(bool(context.get('exists'))).lower()}",
    ]
    if context.get("exists"):
        lines.extend(
            [
                f"  similar_title_node_id: {context.get('node_id')}",
                f"  similar_title_node_title: {context.get('node_title')}",
                "  similar_title_established_re_mention: "
                f"{str(bool(context.get('established_re_mention'))).lower()}",
            ]
        )
    return "\n".join(lines)


def _companion_triage_system_prompt(cfg: Any) -> str:
    from okto_neuron.curator import candidate_curator_system

    base = cfg.llm.curator.system_prompt or candidate_curator_system(cfg.packs)
    return (
        base + "\n\nReview-queue triage mode: choose exactly one action. "
        "commit means this queued node candidate should now be committed. "
        "discard means it should be removed from the queue without writing. "
        "keep means it should stay queued for human review. "
        'Reply with ONLY JSON: {"action":"commit|discard|keep",'
        '"confidence":<0..1>,"reason":"<short grounded reason>"}; '
        "in batch mode return the same fields plus candidate_id for each verdict."
    )


def _build_companion_triage_provider(state: "ServerState", resolved: Any) -> Any:
    from okto_neuron.llm import get_provider

    return get_provider(resolved)


def _single_triage_call(
    item: _TriageItem,
    *,
    provider: Any,
    system_prompt: str,
    resolved: Any,
) -> _TriageVerdict:
    from okto_neuron.llm import LLMProviderError, Message, last_call_stats

    started = time.perf_counter()
    try:
        reply = provider.complete(
            [Message("system", system_prompt), Message("user", item.prompt)],
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
            top_p=resolved.top_p,
            top_k=resolved.top_k,
            min_p=resolved.min_p,
            presence_penalty=resolved.presence_penalty,
            enable_thinking=resolved.enable_thinking,
            response_format=_triage_response_format(),
        )
    except LLMProviderError:
        return _TriageVerdict(
            action="keep",
            confidence=0.0,
            reason="llm-unavailable",
            duration_s=round(time.perf_counter() - started, 3),
        )
    verdict = _parse_triage_verdict(reply)
    return replace(
        verdict,
        duration_s=round(time.perf_counter() - started, 3),
        usage=last_call_stats(),
    )


def _run_companion_triage_batch(
    idxs: list[int],
    items: list[_TriageItem],
    *,
    provider: Any,
    system_prompt: str,
    resolved: Any,
) -> dict[int, _TriageVerdict]:
    from okto_neuron.curator_batch import (
        BATCH_MAX_TOKENS_CAP,
        batch_system_prompt,
        build_batch_user_prompt,
    )
    from okto_neuron.llm import LLMProviderError, Message, last_call_stats

    if len(idxs) == 1:
        idx = idxs[0]
        return {
            idx: _single_triage_call(
                items[idx],
                provider=provider,
                system_prompt=system_prompt,
                resolved=resolved,
            )
        }

    members = [items[i] for i in idxs]
    user = build_batch_user_prompt(
        members[0].excerpt,
        [(member.candidate_id, member.prompt) for member in members],
    )
    started = time.perf_counter()
    try:
        reply = provider.complete(
            [
                Message("system", batch_system_prompt(system_prompt, len(members))),
                Message("user", user),
            ],
            temperature=resolved.temperature,
            # Batching needs an explicit aggregate cap even when the user leaves
            # provider defaults untouched; this is task policy, not an LLM default.
            max_tokens=min(
                max(resolved.max_tokens or 1024, 1) * len(members),
                BATCH_MAX_TOKENS_CAP,
            ),
            top_p=resolved.top_p,
            top_k=resolved.top_k,
            min_p=resolved.min_p,
            presence_penalty=resolved.presence_penalty,
            enable_thinking=resolved.enable_thinking,
            response_format=_triage_batch_response_format(len(members)),
        )
    except LLMProviderError:
        return {}
    duration_s = round(time.perf_counter() - started, 3)
    usage = last_call_stats()
    parsed = _parse_triage_batch_verdicts(reply, [member.candidate_id for member in members])
    results: dict[int, _TriageVerdict] = {}
    first_usage = True
    for idx in idxs:
        verdict = parsed.get(items[idx].candidate_id)
        if verdict is None:
            continue
        results[idx] = replace(
            verdict,
            duration_s=duration_s,
            usage=usage if first_usage else None,
        )
        first_usage = False
    return results


def _run_companion_triage_verdicts(
    items: list[_TriageItem],
    *,
    provider: Any,
    system_prompt: str,
    resolved: Any,
    batch_size: int,
    max_concurrent: int,
    timeout_s: float | None,
    on_progress: "Any | None" = None,
) -> list[_TriageVerdict]:
    from okto_neuron.curator_batch import plan_batches

    batches = plan_batches(
        [
            # ``plan_batches`` only needs candidate_id/block_id/excerpt/prompt.
            type(
                "_BatchPlanItem",
                (),
                {"candidate_id": i.candidate_id, "block_id": i.block_id},
            )()
            for i in items
        ],
        batch_size,
    )
    results: dict[int, _TriageVerdict] = {}

    def complete_batch(idxs: list[int], batch_results: dict[int, _TriageVerdict]) -> None:
        results.update(batch_results)
        if on_progress is not None:
            on_progress(len(results), len(items))
        for idx in idxs:
            if idx not in results:
                results[idx] = _single_triage_call(
                    items[idx],
                    provider=provider,
                    system_prompt=system_prompt,
                    resolved=resolved,
                )

    if max_concurrent > 1 and len(batches) > 1:
        with ThreadPoolExecutor(max_workers=min(max_concurrent, len(batches))) as pool:
            futures = {
                id(batch): pool.submit(
                    _run_companion_triage_batch,
                    batch,
                    items,
                    provider=provider,
                    system_prompt=system_prompt,
                    resolved=resolved,
                )
                for batch in batches
            }
            for batch in batches:
                try:
                    batch_results = futures[id(batch)].result(timeout=timeout_s)
                except FuturesTimeoutError:
                    batch_results = {}
                except Exception:  # noqa: BLE001
                    batch_results = {}
                complete_batch(batch, batch_results)
    else:
        for batch in batches:
            try:
                batch_results = _run_companion_triage_batch(
                    batch,
                    items,
                    provider=provider,
                    system_prompt=system_prompt,
                    resolved=resolved,
                )
            except Exception:  # noqa: BLE001
                batch_results = {}
            complete_batch(batch, batch_results)

    return [results[i] for i in range(len(items))]


def run_companion_triage(state: "ServerState", job: Any) -> dict:
    """Judge parked companion node candidates and auto-resolve clear cases."""
    from okto_neuron.companion import Companion, ReviewItemNotFoundError
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.curator import LLMCandidateCurator, _source_excerpt
    from okto_neuron.llm import sampler_overrides
    from okto_neuron.resolve import resolve

    cfg = _load_config(state)
    consolidation = cfg.consolidation
    resolved = cfg.llm.resolved("curator")
    provider = _build_companion_triage_provider(state, resolved)
    curator = LLMCandidateCurator(
        provider,
        **sampler_overrides(resolved),
        top_p=resolved.top_p,
        top_k=resolved.top_k,
        min_p=resolved.min_p,
        presence_penalty=resolved.presence_penalty,
        enable_thinking=resolved.enable_thinking,
        system_prompt=cfg.llm.curator.system_prompt,
        packs=cfg.packs,
    )
    store = state.vault.store
    embedder = state.vault.embedder
    queue = ReviewQueue(Path(state.vault_path) / _MARGINALIA_DIR, store)
    candidates = queue.candidates()
    total = len(candidates)
    job.progress(f"triaging 0/{total}")
    if total == 0:
        return {"triaged": 0, "committed": 0, "discarded": 0, "kept": 0, "errors": []}

    items: list[_TriageItem] = []
    errors: list[dict[str, str]] = []
    gate_threshold = consolidation.auto_commit_threshold
    for candidate in candidates:
        try:
            outcome = resolve(candidate, store, embedder=embedder)
            prompt = curator.build_prompt(
                candidate,
                outcome,
                store=store,
                edges=[],
                proposed_action=_triage_proposed_action(
                    candidate,
                    store=store,
                    gate_threshold=gate_threshold,
                ),
            )
            raw_block_id = candidate.facets.get("block_id")
            items.append(
                _TriageItem(
                    candidate=candidate,
                    prompt=prompt,
                    block_id=str(raw_block_id) if raw_block_id else None,
                    excerpt=_source_excerpt(candidate, store),
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": str(candidate.candidate_id), "error": str(exc)})

    verdicts = _run_companion_triage_verdicts(
        items,
        provider=provider,
        system_prompt=_companion_triage_system_prompt(cfg),
        resolved=resolved,
        batch_size=consolidation.curation_batch_size,
        max_concurrent=curation_effective_max_concurrent(cfg),
        timeout_s=consolidation.curation_call_timeout_s,
        on_progress=lambda done, total_items: job.progress(f"judging {done}/{total_items}"),
    )

    companion = Companion(state.vault)
    committed = 0
    discarded = 0
    kept = 0
    triaged = 0
    for index, (item, verdict) in enumerate(zip(items, verdicts), start=1):
        triaged += 1
        job.progress(f"triaging {index}/{total}")
        candidate_id = item.candidate_id
        try:
            if verdict.action == "commit" and verdict.confidence >= gate_threshold:
                companion.resolve_review(candidate_id, "commit")
                committed += 1
            elif verdict.action == "discard" and verdict.confidence >= _TRIAGE_DISCARD_THRESHOLD:
                companion.resolve_review(candidate_id, "discard")
                discarded += 1
            else:
                kept += 1
        except ReviewItemNotFoundError:
            kept += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": candidate_id, "error": str(exc)})

    return {
        "triaged": triaged,
        "committed": committed,
        "discarded": discarded,
        "kept": kept,
        "errors": errors,
    }


def _predicate_propose_empty(*, vocabulary_size: int = 0, disabled: bool = False) -> dict:
    return {
        "pairs_considered": 0,
        "judged": 0,
        "outcomes": [],
        "auto_eligible": 0,
        "queued": 0,
        "rejected": 0,
        "vocabulary_size": vocabulary_size,
        "disabled": disabled,
    }


# ── job runners (run OFF the event loop, on state.vault) ─────────────────────────
def _job_vault(state: "ServerState", job: Any) -> tuple[Any, Path]:
    """Resolve the ``(Vault, path)`` a SWEEP-kind runner should operate against.

    Application jobs receive a ``VaultRuntime`` and cannot retarget. The tagged
    path handling below is retained only for direct-``ServerState`` compatibility
    jobs persisted before immutable runtimes shipped.

    Getting this wrong is the subtle failure mode flagged by the investigation:
    a sweep that reads ``state.vault``/``state.vault_path`` unconditionally would
    silently run against the WRONG vault's graph while writing its off-graph
    side-files into the fallback vault's ``.marginalia/`` — corrupting one
    vault's review state with another vault's candidates.

    Raises ``VaultPoolError`` (via ``VaultPool.get_or_open``) when a pooled
    vault named by a job has since vanished (evicted/closed) — callers catch
    this so ONE job fails, not the drain worker."""
    from okto_neuron.server.state import VaultRuntime

    raw = job.params.get("vault") if isinstance(getattr(job, "params", None), dict) else None
    active_path = Path(state.vault_path) if state.vault_path is not None else None
    if isinstance(state, VaultRuntime):
        if raw:
            requested = Path(raw).expanduser().resolve(strict=False)
            if requested != state.vault_path:
                raise RuntimeError(
                    "curation job vault does not match its immutable runtime: "
                    f"job={requested} runtime={state.vault_path}"
                )
        return state.vault, state.vault_path
    if not raw:
        return state.vault, active_path
    target = Path(raw).expanduser().resolve(strict=False)
    if active_path is not None and target == active_path.expanduser().resolve(strict=False):
        return state.vault, active_path
    vault = state.vault_pool.get_or_open(target)
    return vault, target


def run_propose(state: "ServerState", job: Any) -> dict:
    """Read-only propose: candidate clusters + per-cluster verdicts. Writes
    NOTHING. Runs against the immutable runtime target; tagged compatibility
    jobs resolve their recorded path.  A verified-boundary trigger also updates
    its linked ingest outcomes; failure remains semantic and cannot change an
    already-verified graph commit."""
    from okto_neuron.construction_cost import ConstructionCostTracker
    from okto_neuron.reconcile.candidates import generate_candidate_clusters
    from okto_neuron.reconcile.propose import adjudicate_cluster
    from okto_neuron.server._vault_pool import VaultPoolError

    type_filter = job.params.get("type") or None
    use_cluster_judge = bool(job.params.get("use_cluster_judge", False))
    try:
        vault, vault_path = _job_vault(state, job)
    except VaultPoolError as exc:
        raise RuntimeError(f"sweep target vault unavailable: {exc}") from exc
    expected_generation = str(job.params.get("graph_generation") or "")
    current_generation = str(
        getattr(getattr(vault.store, "_graph_handle", None), "graph_generation", "") or ""
    )
    if expected_generation and current_generation != expected_generation:
        raise RuntimeError("reconciliation target graph generation changed before proposal")
    store = vault.store
    embedder = vault.embedder
    construction_cost = ConstructionCostTracker()
    judge = _build_judge(
        state,
        vault_path=vault_path,
        on_completion=construction_cost.record_completion,
    )
    decisions = identity_decision_index(state, vault_path=vault_path)

    job.progress("clustering")
    clusters = generate_candidate_clusters(store, embedder=embedder, type=type_filter)
    rows: list[dict] = []
    for i, cluster in enumerate(clusters):
        job.progress(f"adjudicating {i + 1}/{len(clusters)}")
        verdict = adjudicate_cluster(
            cluster,
            store,
            judge=judge,
            embedder=embedder,
            use_cluster_judge=use_cluster_judge,
            merge_blocked=decisions.is_distinct,
        )
        rows.append(cluster_verdict_row(cluster, verdict))

    outcome = {
        "state": "complete",
        "stage": "propose",
        "job_id": job.id,
        "trigger": str(job.params.get("trigger") or "manual"),
        "clusters": len(rows),
        "graph_generation": current_generation or None,
    }
    return {
        "clusters": rows,
        "count": len(rows),
        "outcome": outcome,
        "construction_cost": construction_cost.snapshot(),
    }


def run_apply(state: "ServerState", job: Any) -> dict:
    """Off-graph apply: auto-merge high-confidence to the AuthorityIndex; queue the
    rest. NEVER touches the graph (apply_reconciliation asserts off-graph)."""
    from okto_neuron.reconcile.apply import apply_reconciliation

    type_filter = job.params.get("type") or None
    use_cluster_judge = bool(job.params.get("use_cluster_judge", False))
    store = state.vault.store
    embedder = state.vault.embedder
    judge = _build_judge(state)
    authority = authority_index(state)
    queue = reconcile_queue(state)
    decisions = identity_decision_index(state)

    job.progress("reconciling")
    report = apply_reconciliation(
        store,
        embedder=embedder,
        judge=judge,
        authority=authority,
        queue=queue,
        type=type_filter,
        use_cluster_judge=use_cluster_judge,
        judge_model=_judge_model(state),
        merge_blocked=decisions.is_distinct,
    )
    return {
        "auto_merged": list(report.auto_merged),
        "queued": list(report.queued),
        "skipped": list(report.skipped),
        "counts": {
            "auto_merged": len(report.auto_merged),
            "queued": len(report.queued),
            "skipped": len(report.skipped),
        },
    }


def _build_predicate_judge(state: "ServerState", *, vault_path: "Path | str | None" = None):
    from okto_neuron.companion import _StepLabelledProvider
    from okto_neuron.predicates import LLMPredicateJudge

    judge = LLMPredicateJudge.from_config(_load_config(state, vault_path=vault_path))
    # LLMPredicateJudge.from_config builds its own provider internally (no
    # constructor seam to inject a wrapped one), so the step label is applied
    # post-construction onto the judge's stored provider — same effect as
    # wrapping at build time, just applied one step later.
    judge._provider = _StepLabelledProvider(judge._provider, "predicate_judge")
    return judge


def _job_judged_pairs(job: Any) -> list[tuple[str, str]] | None:
    raw = job.params.get("judged_pairs")
    if not isinstance(raw, list):
        return None
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and all(isinstance(value, str) for value in item)
        ):
            pairs.append((item[0], item[1]))
    return pairs


def run_predicate_propose(state: "ServerState", job: Any) -> dict:
    """Read-only predicate canonicalization proposal run.

    ADR 0017 decision 6 is load-bearing here: this runner judges candidates and
    returns full proposal records in the job result, but writes nothing to the
    predicate alias index.

    Runs against the SWEEP TARGET's handle/config/alias-index (the active
    vault, or a POOLED vault when the scheduler tagged this job — issue #5)."""
    from okto_neuron.predicates import (
        collect_predicate_vocabulary,
        generate_predicate_candidates,
    )
    from okto_neuron.server._vault_pool import VaultPoolError

    try:
        vault, vault_path = _job_vault(state, job)
    except VaultPoolError as exc:
        raise RuntimeError(f"sweep target vault unavailable: {exc}") from exc
    cfg = _load_config(state, vault_path=vault_path)
    store = vault.store
    index = predicate_alias_index(state, vault_path=vault_path)

    job.progress("scanning predicate vocabulary")
    vocabulary = collect_predicate_vocabulary(store)
    vocabulary_size = len(vocabulary)
    if not cfg.upkeep.enabled:
        return _predicate_propose_empty(
            vocabulary_size=vocabulary_size,
            disabled=True,
        )

    job.progress("generating predicate candidates")
    candidates = generate_predicate_candidates(
        store,
        vault.embedder,
        alias_index=index,
        judged_pairs=_job_judged_pairs(job),
        cluster_threshold=cfg.upkeep.cluster_threshold,
        min_support=cfg.upkeep.min_support,
        max_pairs=cfg.upkeep.max_pairs_per_run,
    )
    if not candidates:
        return _predicate_propose_empty(vocabulary_size=vocabulary_size)

    judge = _build_predicate_judge(state, vault_path=vault_path)
    outcomes: list[dict] = []
    for i, candidate in enumerate(candidates):
        job.progress(f"judging predicate pair {i + 1}/{len(candidates)}")
        result = judge.judge(
            candidate,
            auto_fold_threshold=cfg.upkeep.auto_fold_threshold,
        )
        outcomes.append(predicate_outcome_row(result))

    auto_eligible = sum(1 for row in outcomes if row["status"] == "auto")
    queued = sum(1 for row in outcomes if row["status"] == "queued")
    rejected = sum(1 for row in outcomes if row["status"] == "rejected")
    return {
        "pairs_considered": len(candidates),
        "judged": len(outcomes),
        "outcomes": outcomes,
        "auto_eligible": auto_eligible,
        "queued": queued,
        "rejected": rejected,
        "vocabulary_size": vocabulary_size,
        "disabled": False,
    }


def _predicate_records_from_params(state: "ServerState", params: dict) -> list[Any] | None:
    from okto_neuron.predicates import PredicateAliasRecord

    raw = params.get("records")
    if raw is None:
        raw = params.get("proposals")
    if raw is None:
        raw = params.get("outcomes")

    if raw is None and params.get("job_id"):
        job_id = str(params["job_id"])
        source = next((j for j in state.curation_jobs if j.id == job_id), None)
        if source is None:
            raise ValueError(f"predicate propose job not found: {job_id}")
        result = source.result or {}
        raw = result.get("outcomes")

    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("predicate proposals must be a list")

    records: list[Any] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("predicate proposal entries must be objects")
        record = item.get("record", item)
        if not isinstance(record, dict):
            raise ValueError("predicate proposal record must be an object")
        records.append(PredicateAliasRecord.from_json(record))
    return records


def run_predicate_apply(state: "ServerState", job: Any) -> dict:
    """Persist predicate proposal records to the off-graph alias index.

    If the caller supplies proposal records (or a propose ``job_id``), those are
    applied. Otherwise the runner performs a fresh propose pass and applies that
    result. All writes are confined to ``.marginalia/predicates/aliases.json``.
    """
    records = _predicate_records_from_params(state, job.params)
    source = "params"
    proposed: dict | None = None
    if records is None:
        source = "rerun"
        proposed = run_predicate_propose(state, job)
        records = _predicate_records_from_params(state, {"outcomes": proposed["outcomes"]})

    index = predicate_alias_index(state)
    counts = {"auto": 0, "confirmed": 0, "queued": 0, "rejected": 0, "total": 0}
    written: list[dict] = []
    for record in records:
        index.upsert(record)
        counts[record.status] = counts.get(record.status, 0) + 1
        counts["total"] += 1
        written.append(predicate_record_row(record))

    return {
        "source": source,
        "counts": counts,
        "records": written,
        "propose": proposed,
    }


def run_detect_drift(state: "ServerState", job: Any) -> dict:
    """Read-only deterministic drift detection (ADR 0009 P4). Runs every closed-set
    detector against the job runtime's leased handle and aggregates per-detector Finding
    counts into the job RESULT. Writes NOTHING.

    Registered with ``writes=False`` — advisory background detection, an INTENTIONAL
    divergence from the ``/detect-drift`` HTTP endpoint (which takes ``writer_lock``):
    here reads/ingest must stay up while the continuous loop sweeps. Findings are
    deterministic, no LLM. This runner exists so the scheduler can ``_jobs.submit``
    a ``detect-drift`` job (the kind is otherwise HTTP-only and would KeyError).

    Runs against the immutable runtime target; tagged compatibility jobs resolve
    their recorded path."""
    from okto_neuron.detectors import DETECTOR_NAMES, run_detector
    from okto_neuron.server._vault_pool import VaultPoolError

    try:
        vault, _vault_path = _job_vault(state, job)
    except VaultPoolError as exc:
        raise RuntimeError(f"sweep target vault unavailable: {exc}") from exc

    counts: dict[str, int] = {}
    total = 0
    for i, name in enumerate(DETECTOR_NAMES):
        job.progress(f"detecting {i + 1}/{len(DETECTOR_NAMES)}: {name}")
        findings = run_detector(name, vault)
        counts[name] = len(findings)
        total += len(findings)
    return {"counts": counts, "total": total}


# ── in-process rebuild / heal / reembed (ADR 0009 P3) ───────────────────────────
# These runners build a FRESH graph at a TMP path while the daemon keeps serving
# from the live ``state.vault`` handle, then do a close→os.replace→reopen swap ONLY
# at the very end — the handle is unavailable for the swap instant only.
#
# Why these CANNOT call ``kg_rebuild`` wholesale (load-bearing):
#   1. ``kg_rebuild`` closes the live handle at the START (cli/kg.py) — wrong for
#      in-process, the daemon must serve from the live handle during the long build.
#   2. ``kg_rebuild`` installs SIGINT/SIGTERM handlers via ``signal.signal``, which
#      raises ``ValueError: signal only works in main thread`` here — the runner
#      executes via ``asyncio.to_thread`` (NOT the main thread). The daemon owns
#      process signals; the in-process path skips signal handlers entirely.
# So we re-sequence using cli/kg.py's HELPERS (``_build_fresh_graph`` +
# ``_swap_rebuilt_graph`` + ``_close_live_graph_handles``), not the wrapper.
#
# INGEST-DURING-REBUILD POLICY — two variants, both race-free:
#
# rebuild / reembed (writer_lock + WRITE draining for the WHOLE job). The runner registers
#   ``writes=True`` so ``_jobs._drain`` holds ``state.writer_lock`` for the whole job,
#   AND the runner sets ``state.mark_draining()`` for the same window (cleared only
#   after the post-swap reopen). BOTH gates are used because the codebase serializes
#   writes via two mechanisms: lock-gated writers (/add, /remember, config PATCH,
#   reconcile confirm, authority unmerge) AND draining-gated-then-lock writers
#   (api_ingest checks ``draining`` BEFORE acquiring the lock; the ingest-queue worker
#   drains off-loop). Draining for the job duration 503s every writer and stops the
#   queue worker starting new items. HTTP/MCP reads intentionally ignore maintenance
#   draining and keep leasing the old live generation throughout the slow build. Only
#   process shutdown stops reads; the final pool fence protects the atomic swap.
#
# heal (writer_lock ONLY for the job; draining ONLY around the ~1s swap). The heal is
#   a fast deterministic copy whose ONLY mutation is to a SEPARATE tmp graph — the live
#   graph is read once (``list_nodes``/``list_edges``) and never written. ADR 0009
#   requires the daemon to be down ONLY at the swap instant, so the heal must keep
#   reads at 200 for the whole copy. ``writer_lock`` ALONE makes that safe: the ingest
#   worker acquires the SAME ``writer_lock`` before every ``remember``, so while the
#   heal holds it for the job, no ingest write reaches the live graph — the snapshot
#   read and the swap both run under one continuously-held lock, so nothing interleaves
#   between them. (The pre-lock ``draining`` check on api_ingest is NOT needed as a
#   second gate here: an ingest item that passes its pre-lock draining check still
#   parks at ``async with writer_lock`` in the queue worker until the heal releases the
#   lock AFTER the swap, so it lands on the post-swap graph, not the discarded one.)
#   Draining is therefore set only inside ``_do_swap`` (right before
#   ``_close_live_graph_handles``, cleared after ``Vault.open``) as a belt-and-suspenders
#   guard. Because ``_do_swap`` is a single NO-AWAIT block, no read can interleave with
#   it: reads return 200 for the WHOLE copy, and the swap shows up as a one-time ~1s
#   latency blip on any in-flight read (served 200 against the reopened graph), NOT a
#   503 window. Belt-and-braces: before the copy we bounded-wait on
#   ``state.ingest_worker_active`` so an item already inside the lock/thread finishes.


def _rebuild_recovery_dir(vault_path: Path, job_id: str) -> Path:
    return vault_path / _MARGINALIA_DIR / _REBUILD_RECOVERY_DIR / job_id


def _clear_rebuild_recovery_dir(vault_path: Path, job_id: str) -> None:
    from okto_neuron.cli import kg as kg_cli

    recovery_dir = _rebuild_recovery_dir(vault_path, job_id)
    if not recovery_dir.exists():
        return
    shutil.rmtree(recovery_dir)
    kg_cli._fsync_parent_dir(recovery_dir)


def _recover_interrupted_rebuild(state: "ServerState", job: Any) -> str:
    """Restore a crash-interrupted rebuild only when the live graph proves no swap.

    The queue invokes this during startup before it makes the durable running job
    terminal. A job-bound checkpoint supplies the exact pre-job semantic sidefile
    bytes. The live graph generation is the phase fence: old generation means the
    process died before swap and restoration is safe; any other generation fails
    closed without applying old policy to potentially new graph bytes.
    """
    from okto_neuron.cli import kg as kg_cli

    vault_path = Path(state.vault_path)
    marginalia_dir = vault_path / _MARGINALIA_DIR
    graph_path = vault_path / kg_cli._GRAPH_FILE
    staging_path = graph_path.with_name("graph.rebuild.lbug")
    state_path = marginalia_dir / kg_cli._REBUILD_STATE
    recovery_dir = _rebuild_recovery_dir(vault_path, str(job.id))
    checkpoint_path = recovery_dir / kg_cli._PREVIOUS_SEMANTIC_POLICY
    try:
        checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing pre-job semantic checkpoint for {job.id}") from exc
    if not isinstance(checkpoint_payload, dict):
        raise RuntimeError("pre-job semantic checkpoint is not an object")
    previous_generation = str(checkpoint_payload.get("graph_generation") or "")
    previous_policy = str(checkpoint_payload.get("semantic_policy_fingerprint") or "")
    if not previous_policy:
        raise RuntimeError("pre-job semantic checkpoint has no policy fingerprint")

    live_generation = ""
    if graph_path.is_file():
        identity = kg_cli._graph_identity_at_path(graph_path)
        live_generation = str(identity.graph_generation or "")
    if live_generation != previous_generation:
        raise RuntimeError(
            "interrupted rebuild reached or passed the swap boundary; "
            f"live generation is {live_generation or 'unset'}, expected pre-job "
            f"generation {previous_generation or 'unset'}; semantic policy was not restored"
        )

    files = kg_cli._load_semantic_policy_checkpoint(
        checkpoint_path,
        expected_graph_generation=previous_generation,
        expected_semantic_policy_fingerprint=previous_policy,
    )
    kg_cli._restore_semantic_policy_sidefiles(vault_path, files)
    restored_policy = kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]
    if restored_policy != previous_policy:
        raise RuntimeError(
            "restored semantic policy fingerprint mismatch: "
            f"expected {previous_policy}, got {restored_policy}"
        )

    rebuild_state: dict[str, Any] = {}
    try:
        loaded_state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded_state, dict):
            rebuild_state = loaded_state
    except (OSError, TypeError, ValueError):
        pass
    staging_generation = str(rebuild_state.get("graph_generation") or f"interrupted-{job.id}")
    artifact_dir = kg_cli._rebuild_artifact_dir(vault_path, staging_generation)
    retained_staging = artifact_dir / "staging.interrupted.lbug"
    source_family_exists = staging_path.exists() or bool(
        kg_cli._active_graph_sidecars(staging_path)
    )
    target_family_exists = retained_staging.exists() or bool(
        kg_cli._active_graph_sidecars(retained_staging)
    )
    if source_family_exists and target_family_exists:
        raise RuntimeError(f"both active and retained interrupted staging exist for {job.id}")
    if source_family_exists:
        kg_cli._move_graph_family(staging_path, retained_staging)

    retained_checkpoint = artifact_dir / kg_cli._PREVIOUS_SEMANTIC_POLICY
    shutil.copy2(checkpoint_path, retained_checkpoint)
    with retained_checkpoint.open("rb") as handle:
        os.fsync(handle.fileno())
    kg_cli._fsync_parent_dir(retained_checkpoint)

    interrupted_at = kg_cli._utc_now()
    interrupted = {
        **rebuild_state,
        "phase": "process_interrupted",
        "interrupted_at": interrupted_at,
        "job_id": str(job.id),
        "previous_graph_generation": previous_generation,
        "restored_semantic_policy_fingerprint": restored_policy,
        "staging_graph": (
            str(retained_staging) if target_family_exists or source_family_exists else None
        ),
    }
    kg_cli._write_rebuild_state(state_path, interrupted)
    kg_cli._write_rebuild_state(artifact_dir / "validation.json", interrupted)
    _clear_rebuild_recovery_dir(vault_path, str(job.id))
    staging_note = (
        f"staging retained at {retained_staging}"
        if interrupted["staging_graph"]
        else "no staging graph existed"
    )
    return (
        f"pre-swap recovery completed; semantic policy {restored_policy} restored; {staging_note}"
    )


def run_rebuild(state: "ServerState", job: Any) -> dict:
    """In-process rebuild (``job.kind == 'rebuild'``) — the markdown reversibility
    path (re-extract every source file into a fresh graph + atomic swap). The heal
    (``job.kind == 'heal'``) is a SEPARATE, no-LLM deterministic copy — see
    :func:`run_heal`.

    Sequence (runs inside ``asyncio.to_thread`` with ``writer_lock`` held by
    ``_drain``; the runner sets write-maintenance draining for its own duration):
      1. mark_draining + bounded-wait for the last in-flight ingest item. Read-only
         HTTP/MCP requests continue against the old live generation.
      2. BUILD phase — ``_build_fresh_graph`` at the TMP path. The daemon's
         ``state.vault`` handle is UNTOUCHED here, so reads keep working against the
         live graph for the whole (long) build.
      3. SWAP phase — marshal a single no-await ``_do_swap`` onto the event loop via
         ``run_coroutine_threadsafe(...).result()``: close the live handle →
         _swap_rebuilt_graph → reopen. Because every read handler does its Ladybug
         access synchronously on the loop, a no-await block cannot be preempted, so
         no read observes a half-swapped graph — the swap is atomic to readers.
      4. clear ``draining`` after reopen (in ``finally``).
    On any failure BEFORE the swap, the tmp graph is discarded and the live handle
    stays open (it was never closed pre-swap), so the daemon keeps serving the
    unchanged live graph."""
    from okto_neuron.cli import kg as kg_cli

    vault_path = Path(state.vault_path)
    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    # M4 spec §2 ("one construction seam generalized"): resolved once, up
    # front, so both the build head below and the swap tail share one
    # registry-driven pin — mirrors ``cli/kg.py``'s ``kg_rebuild``. Ladybug
    # (the default, and every pre-M4 vault with no ``storage`` pin) gets
    # byte-identical values: ``graph_path``/``tmp_graph_path`` match today's
    # own literal computation exactly, and ``open_staged_store`` stays
    # ``None`` so the build head below keeps its unmodified Ladybug path.
    backend_name = kg_cli._resolve_pinned_backend(vault_path)
    staging_port, open_staged_store = kg_cli._swap_construction_for(vault_path, backend_name)
    graph_path = kg_cli._live_graph_path(vault_path, backend_name)
    # CRITICAL (ADR 0009 P3): the in-process build target must NOT be
    # ``graph.lbug.tmp`` — ladybug owns ``<db>.tmp`` as its OWN scratch file for the
    # LIVE ``graph.lbug`` handle, and closing the live handle (at swap time) deletes
    # it, taking our freshly-built graph with it. The CLI rebuild never collided
    # because it closes the live handle BEFORE creating its tmp; the daemon keeps the
    # live handle open during the whole build, so we use a distinct base name whose
    # scratch (``graph.rebuild.lbug.tmp``/``.wal``) cannot clash with the live one.
    # ``staging_port.stage_path("rebuild")`` gives Ladybug that exact literal
    # (``graph.rebuild.lbug``) and Grafx its own directory sibling
    # (``graph.rebuild.grafx``).
    tmp_graph_path = staging_port.stage_path("rebuild")
    state_path = marginalia_dir / kg_cli._REBUILD_STATE

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal swap")

    previous_generation = str(state.vault.store.generation())
    from okto_neuron.semantic_fingerprint import materialized_semantic_fingerprints

    previous_fingerprints = materialized_semantic_fingerprints(
        vault_path,
        graph_generation=previous_generation or None,
    )
    previous_policy_sidefiles = kg_cli._capture_semantic_policy_sidefiles(vault_path)
    ordered_source_files = kg_cli._validated_rebuild_source_files(vault_path, None)
    semantic_seed_base = kg_cli._effective_semantic_fingerprint_triplet(vault_path)
    source_manifest = kg_cli._semantic_source_manifest(vault_path, ordered_source_files)
    recovery_dir = _rebuild_recovery_dir(vault_path, str(job.id))
    if recovery_dir.exists():
        raise RuntimeError(f"rebuild recovery checkpoint already exists for {job.id}")
    recovery_dir.mkdir(parents=True)
    kg_cli._write_semantic_policy_checkpoint(
        recovery_dir,
        graph_generation=previous_generation,
        semantic_policy_fingerprint=semantic_seed_base["semantic_policy"],
        files=previous_policy_sidefiles,
    )

    job.progress("preparing")
    state.mark_draining()
    swapped = False
    try:
        # Wait for any ingest item already inside writer_lock/to_thread to finish so
        # the swap doesn't race a live-graph write that started before draining was
        # observed (the rebuild reads markdown, but we still close this window).
        deadline = time.monotonic() + _INGEST_DRAIN_TIMEOUT
        while state.ingest_worker_active and time.monotonic() < deadline:
            time.sleep(0.05)

        # Bootstrap a live graph if the vault was never built, so the swap below has
        # a target (mirrors kg_rebuild's live-bootstrap-if-missing). Done WITHOUT
        # closing the daemon handle — only create the on-disk file if absent.
        kg_cli._ensure_live_graph_exists(
            vault_path,
            backend_name,
            graph_path,
            storage_config=kg_cli._load_storage_config(vault_path),
        )

        staging_port.discard(tmp_graph_path)
        if backend_name != "neo4j":
            # Neo4j's staged "path" is a synthetic marker never written to
            # disk (Neo4jStaging docstring) -- nothing to fsync. Mirrors
            # ``kg_rebuild``'s own CLI-side guard (cli/kg.py).
            kg_cli._fsync_parent_dir(tmp_graph_path)
        started_at = kg_cli._utc_now()
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "in_progress",
                "files_done": [],
                "current_file": None,
                "started_at": started_at,
            },
        )

        def _progress(current_file: str, done: int, total: int) -> None:
            job.progress(f"ingesting {done}/{total}: {current_file}")

        # BUILD PHASE — live handle still serving. No signal handlers (we are off the
        # main thread; the daemon owns signals), no live-handle close, no swap.
        semantic_seed = kg_cli._apply_pending_semantic_policy(
            vault_path,
            expected_base=semantic_seed_base,
            expected_source_manifest=source_manifest,
        )
        built = kg_cli._build_fresh_graph(
            vault_path,
            tmp_graph_path,
            state_path,
            started_at,
            interrupt_check=None,
            progress=_progress,
            source_files=ordered_source_files,
            staging=staging_port,
            open_staged_store=open_staged_store,
        )
        if semantic_seed is not None:
            built["semantic_policy_seed"] = str(semantic_seed)
        checkpoint = kg_cli._checkpoint_semantic_discovery(
            vault_path,
            built,
            base_fingerprints=semantic_seed_base,
            source_manifest=source_manifest,
        )
        if checkpoint is not None:
            built["semantic_discovery_checkpoint"] = str(checkpoint)
        kg_cli._require_rebuild_candidate(
            vault_path, tmp_graph_path, state_path, built, backend_name=backend_name
        )
        graph_generation = str(built["graph_generation"])
        backup_graph_path, artifact_dir = kg_cli._prepare_rebuild_backup(
            vault_path,
            graph_generation,
        )
        kg_cli._write_previous_semantic_materialization(
            artifact_dir,
            graph_generation=previous_generation or None,
            fingerprints=previous_fingerprints,
        )
        if previous_generation and all(previous_fingerprints.values()):
            kg_cli._write_semantic_policy_checkpoint(
                artifact_dir,
                graph_generation=previous_generation,
                semantic_policy_fingerprint=str(previous_fingerprints["semantic_policy"]),
                files=previous_policy_sidefiles,
            )
        from okto_neuron.curation.orchestrate import finish_staged_swap
        from okto_neuron.store import schema as graph_schema

        post_swap: dict[str, Any] = {}
        # Populated by ``_fenced_commit`` once the swap actually lands, for a
        # non-Ladybug backend only. Grafx's real backup location
        # (``StagingPort.commit``'s own sibling-tag naming, e.g.
        # ``graph.grafx.rebuild``) never equals ``backup_graph_path`` (the
        # Ladybug-only generation-keyed guess ``_prepare_rebuild_backup``
        # computed above for the rollback endpoint's ``rebuild-artifacts/
        # <generation>/previous-graph.lbug`` layout) — every report below
        # reads THIS instead of the precomputed path directly; for Ladybug it
        # stays empty and every read below falls back to ``backup_graph_path``
        # unchanged (mirrors ``cli/kg.py``'s ``kg_rebuild``'s own
        # ``committed_backup``).
        committed_backup: dict[str, Path] = {}

        def _reported_backup_path() -> Path:
            return committed_backup.get("path", backup_graph_path)

        def _audit_graph_path_and_checkpoint(
            path: Path, *, dim: int, expected_identity: Any, stage: str
        ) -> tuple[Any, dict[str, object]]:
            if open_staged_store is None:
                result, payload = kg_cli._audit_rebuild_graph_path(
                    path, dim=dim, expected_identity=expected_identity, stage=stage
                )
            else:
                result, payload = kg_cli._audit_reopened_staged_store(
                    vault_path,
                    path,
                    dim=dim,
                    expected_identity=expected_identity,
                    stage=stage,
                    open_staged_store=open_staged_store,
                )
            post_swap.update({"result": result, "payload": payload})
            kg_cli._write_rebuild_state(artifact_dir / "post-swap-audit.json", payload)
            if not result.verified:
                failed_payload = {
                    "phase": "post_swap_validation_failed",
                    "failed_at": kg_cli._utc_now(),
                    "backup_path": str(_reported_backup_path()),
                    "post_swap_audit": payload,
                    **built,
                }
                kg_cli._write_rebuild_state(state_path, failed_payload)
            return result, payload

        def _publish_and_track(vp: Path, result: Any, *, audit_id: str | None = None) -> None:
            kg_cli._publish_integrity_result(vp, result, audit_id=audit_id)
            state.integrity_last_audit = result

        # SWAP PHASE — the only handle-unavailable instant. The rename and reopen
        # happen while the runtime is fenced. Marshaling a single no-await
        # ``_swap_graph`` onto the loop keeps it atomic to readers; the post-reopen
        # audit/publish now runs after this returns, on the worker thread, as
        # ``finish_staged_swap``'s shared tail (M2b spec §2.3).
        def _fenced_commit() -> Path:
            async def _do_swap() -> None:
                def _swap_graph() -> None:
                    kg_cli._close_live_graph_handles(vault_path)
                    if backend_name == "ladybug":
                        kg_cli._swap_rebuilt_graph(
                            vault_path, graph_path, tmp_graph_path, backup_graph_path
                        )
                    else:
                        committed_backup["path"] = staging_port.commit(tmp_graph_path, "rebuild")

                await _swap_under_runtime_fence(state, job, _swap_graph)

            asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
            return _reported_backup_path()

        job.progress("swapping")
        try:
            finish_staged_swap(
                vault_path,
                staging_port,
                tmp_graph_path,
                lock=_FenceAlreadyGuardsSwap(),
                backup_tag="rebuild",
                expected_identity=graph_schema.GraphIdentity(
                    graph_generation,
                    str(built["identity_contract_version"]),
                ),
                dim=int(built["embedding_dim"]),  # type: ignore[arg-type]
                stage_prefix="rebuild",
                audit_graph_path=_audit_graph_path_and_checkpoint,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=_publish_and_track,
                commit=_fenced_commit,
                live_graph_path=graph_path,
            )
            kg_cli._publish_built_semantic_materialization(vault_path, built)
        except RebuildAuditFailed as exc:
            # finish_staged_swap's generic message differs from this verb's legacy
            # text; preserve today's exact wording (attributes/cause untouched).
            exc.message = (
                "post-swap graph failed integrity verification; "
                f"previous graph retained at {_reported_backup_path()}"
            )
            exc.args = (exc.message,)
            raise
        swapped = True
        kg_cli._clear_pending_semantic_policy(vault_path)

        final_sha256 = kg_cli._sha256_of_graph(graph_path, backend_name)
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "complete",
                "completed_at": kg_cli._utc_now(),
                "sha256": final_sha256,
                "backup_path": str(_reported_backup_path()),
                "post_swap_audit": post_swap["payload"],
                **built,
            },
        )
        _clear_rebuild_recovery_dir(vault_path, str(job.id))

        async def _schedule_verified_rebuild_reconciliation() -> dict[str, object]:
            return schedule_cross_document_reconciliation(
                state,
                trigger="verified_rebuild",
                graph_generation=graph_generation,
            )

        # Curation runners execute in ``to_thread`` but queue ownership stays on
        # the event loop. Marshal this follow-up there instead of mutating the
        # runtime's job list from the rebuild worker thread.
        try:
            reconciliation = asyncio.run_coroutine_threadsafe(
                _schedule_verified_rebuild_reconciliation(),
                loop,
            ).result()
        except Exception as exc:  # noqa: BLE001 — verified rebuild stays successful
            reconciliation = {
                "state": "failed",
                "stage": "schedule",
                "trigger": "verified_rebuild",
                "error_category": type(exc).__name__,
                "error": str(exc)[:500],
            }
        return {
            "phase": "complete",
            "files_done": built["count"],
            "swapped": swapped,
            "sha256": final_sha256,
            "graph_generation": graph_generation,
            "backup_path": str(_reported_backup_path()),
            "audits": built["audits"],
            "final_audit": built["final_audit"],
            "post_swap_audit": post_swap["payload"],
            "cross_document_reconciliation": reconciliation,
        }
    except Exception as exc:
        if not swapped:
            kg_cli._restore_semantic_policy_sidefiles(
                vault_path,
                previous_policy_sidefiles,
            )
            # Pre-swap failure: discard tmp, leave the live handle untouched.
            staging_port.discard(tmp_graph_path)
            kg_cli._write_failed_rebuild_state(
                state_path,
                vault_path=vault_path,
                error=exc,
            )
        _clear_rebuild_recovery_dir(vault_path, str(job.id))
        raise
    finally:
        # Clear draining (and reopen happened in the swap) BEFORE returning to
        # ``_drain`` so its ``while not state.draining`` re-check passes and queued
        # jobs are not orphaned.
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


def run_rollback(state: "ServerState", job: Any) -> dict:
    """Restore the verified checkpoint immediately preceding the live rebuild.

    Rollback is graph-generation-bound and fail-closed. It never accepts a caller
    path: the live generation selects its own rebuild artifact. The checkpoint is
    copied to staging, physically and semantically audited, then swapped under the
    same runtime fence as rebuild. The displaced graph remains available for
    incident recovery, while the original previous-graph backup is never consumed.
    """

    from okto_neuron.cli import kg as kg_cli

    vault_path = Path(state.vault_path)
    backend_name = kg_cli._resolve_pinned_backend(vault_path)
    if backend_name == "neo4j":
        # M5 neo4j daemon parity: Neo4j's rollback is a metadata-singleton
        # pointer flip (``Neo4jStaging.commit``/``restore`` in
        # ``store/staging.py``), not a byte-copy swap — it needs neither the
        # Ladybug branch's generation-keyed rebuild artifacts nor the grafx
        # sibling's staged-copy/audit/discard sequence, so it gets its own
        # minimal dispatch (see :func:`_run_rollback_neo4j`).
        return _run_rollback_neo4j(state, job, vault_path=vault_path)
    if backend_name != "ladybug":
        # M4 grafx daemon parity: Ladybug's rollback contract below depends
        # end-to-end on generation-keyed rebuild artifacts
        # (``rebuild-artifacts/<generation>/previous-graph.lbug`` plus its
        # semantic-materialization/policy-checkpoint receipts) that only
        # ``kg_rebuild``'s Ladybug-only backup path writes — a non-Ladybug
        # backend has no such receipts, so it gets a self-contained sibling
        # implementation instead of trying to thread it through the branches
        # below (see :func:`_run_rollback_non_ladybug`).
        return _run_rollback_non_ladybug(state, job, vault_path=vault_path, backend_name=backend_name)

    from okto_neuron.companion import _incremental
    from okto_neuron.config import VaultConfig
    from okto_neuron.semantic_fingerprint import (
        load_semantic_materialization,
        materialized_semantic_fingerprints,
        publish_semantic_materialization,
        semantic_fingerprints,
    )

    marginalia_dir = vault_path / _MARGINALIA_DIR
    graph_path = vault_path / kg_cli._GRAPH_FILE
    staging_path = graph_path.with_name("graph.rollback.lbug")
    state_path = marginalia_dir / kg_cli._ROLLBACK_STATE
    current_generation = str(state.vault.store.generation())
    expected_generation = str(job.params.get("from_generation") or "").strip()
    if not expected_generation or expected_generation != current_generation:
        raise RuntimeError(
            "rollback target changed before execution: "
            f"expected={expected_generation or 'missing'} current={current_generation or 'missing'}"
        )

    artifact_dir = marginalia_dir / kg_cli._REBUILD_ARTIFACTS_DIR / current_generation
    backup_path = artifact_dir / "previous-graph.lbug"
    target_materialization_path = artifact_dir / kg_cli._PREVIOUS_SEMANTIC_MATERIALIZATION
    target_policy_path = artifact_dir / kg_cli._PREVIOUS_SEMANTIC_POLICY
    target_identity = kg_cli._graph_identity_at_path(backup_path)
    target_materialization = load_semantic_materialization(
        target_materialization_path,
        expected_graph_generation=target_identity.graph_generation,
    )
    if target_materialization is None:
        raise RuntimeError(
            "rollback checkpoint has no generation-bound semantic materialization receipt"
        )
    target_fingerprints = dict(target_materialization["fingerprints"])
    target_policy_sidefiles = kg_cli._load_semantic_policy_checkpoint(
        target_policy_path,
        expected_graph_generation=str(target_identity.graph_generation or ""),
        expected_semantic_policy_fingerprint=target_fingerprints["semantic_policy"],
    )

    cfg = VaultConfig.load(vault_path)
    effective = semantic_fingerprints(
        cfg,
        vault_path,
        ingest_config=cfg.ingest,
        effective_incremental=_incremental.incremental_enabled(cfg.ingest),
        effective_subchunk=_incremental.subchunk_enabled(cfg.ingest),
    )
    if (
        effective.config_fingerprint != target_fingerprints["config"]
        or effective.extraction_fingerprint != target_fingerprints["extraction"]
    ):
        raise RuntimeError(
            "current semantic configuration does not match the rollback checkpoint; "
            "restore the prior configuration before restoring its graph generation"
        )
    current_policy_sidefiles = kg_cli._capture_semantic_policy_sidefiles(vault_path)

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal rollback swap")

    job.progress("validating rollback checkpoint")
    state.mark_draining()
    swapped = False
    try:
        kg_cli._copy_closed_graph_checkpoint(backup_path, staging_path)
        dim = kg_cli._resolve_configured_dim(vault_path)
        audit_result, audit_payload = kg_cli._audit_rebuild_graph_path(
            staging_path,
            dim=dim,
            expected_identity=target_identity,
            stage="rollback_before_swap",
        )
        if not audit_result.verified:
            raise RebuildAuditFailed(
                vault_path,
                staging_path=staging_path,
                audit_status=audit_result.status.value,
                message="rollback checkpoint failed physical integrity verification",
            )
        semantic_report = kg_cli._semantic_rebuild_report(
            vault_path,
            staging_path,
            dim=dim,
            expected_identity=target_identity,
            integrity_audit=audit_payload,
            materialized_fingerprints=target_fingerprints,
        )
        semantic_gate = semantic_report.get("rebuild_gate")
        if not isinstance(semantic_gate, dict) or semantic_gate.get("swap_allowed") is not True:
            raise RebuildAuditFailed(
                vault_path,
                staging_path=staging_path,
                audit_status="semantic_failed",
                message="rollback checkpoint failed registered semantic invariants",
            )

        rollback_dir = artifact_dir / "rollbacks" / str(job.id)
        rollback_dir.mkdir(parents=True, exist_ok=False)
        displaced_graph = rollback_dir / "replaced-graph.lbug"
        current_fingerprints = materialized_semantic_fingerprints(
            vault_path,
            graph_generation=current_generation,
        )
        kg_cli._write_previous_semantic_materialization(
            rollback_dir,
            graph_generation=current_generation,
            fingerprints=current_fingerprints,
        )
        kg_cli._write_semantic_policy_checkpoint(
            rollback_dir,
            graph_generation=current_generation,
            semantic_policy_fingerprint=str(current_fingerprints["semantic_policy"]),
            files=current_policy_sidefiles,
        )
        current_integrity = kg_cli.integrity_state_path(vault_path)
        if current_integrity.is_file():
            shutil.copy2(current_integrity, rollback_dir / "replaced-graph-integrity.json")

        from okto_neuron.curation.orchestrate import finish_staged_swap

        # M4 spec §2 ("one construction seam generalized"): the staging port
        # is registry-driven even though this call site's own ``commit=``
        # closure (not ``staging.commit``) does the actual swap — a
        # ladybug-pinned (or unpinned, pre-M4) vault gets byte-identical
        # ``LadybugStaging(vault_path)``.
        staging_port, _ = kg_cli._swap_construction_for(
            vault_path, kg_cli._resolve_pinned_backend(vault_path)
        )

        def _publish_and_track(vp: Path, result: Any, *, audit_id: str | None = None) -> None:
            kg_cli._publish_integrity_result(vp, result, audit_id=audit_id)
            state.integrity_last_audit = result

        # ``_swap_graph``'s policy-sidefile install/verify/rollback is rollback-only
        # bookkeeping with no analogue in the shared tail, so it stays here as the
        # body of the fenced commit — only the swap itself becomes the primitive
        # ``finish_staged_swap`` calls.
        def _fenced_commit() -> Path:
            async def _do_swap() -> None:
                def _swap_graph() -> None:
                    policy_installed = False
                    graph_swapped = False
                    try:
                        kg_cli._restore_semantic_policy_sidefiles(
                            vault_path,
                            target_policy_sidefiles,
                        )
                        policy_installed = True
                        restored = semantic_fingerprints(
                            cfg,
                            vault_path,
                            ingest_config=cfg.ingest,
                            effective_incremental=_incremental.incremental_enabled(cfg.ingest),
                            effective_subchunk=_incremental.subchunk_enabled(cfg.ingest),
                        )
                        if (
                            restored.config_fingerprint != target_fingerprints["config"]
                            or restored.extraction_fingerprint
                            != target_fingerprints["extraction"]
                            or restored.semantic_policy_fingerprint
                            != target_fingerprints["semantic_policy"]
                        ):
                            raise RuntimeError(
                                "restored decision checkpoint does not reproduce the "
                                "rollback fingerprint"
                            )
                        kg_cli._close_live_graph_handles(vault_path)
                        kg_cli._swap_rebuilt_graph(
                            vault_path,
                            graph_path,
                            staging_path,
                            displaced_graph,
                        )
                        graph_swapped = True
                    except Exception:
                        if policy_installed and not graph_swapped:
                            kg_cli._restore_semantic_policy_sidefiles(
                                vault_path,
                                current_policy_sidefiles,
                            )
                        raise

                await _swap_under_runtime_fence(state, job, _swap_graph)

            asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
            return displaced_graph

        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "validated",
                "from_generation": current_generation,
                "to_generation": target_identity.graph_generation,
                "pre_swap_audit": audit_payload,
                "semantic_gate": semantic_gate,
            },
        )
        job.progress("swapping rollback checkpoint")
        try:
            swap_result = finish_staged_swap(
                vault_path,
                staging_port,
                staging_path,
                lock=_FenceAlreadyGuardsSwap(),
                backup_tag="rollback",
                expected_identity=target_identity,
                dim=dim,
                stage_prefix="rollback",
                audit_graph_path=kg_cli._audit_rebuild_graph_path,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=_publish_and_track,
                commit=_fenced_commit,
            )
            publish_semantic_materialization(
                vault_path,
                graph_generation=str(target_identity.graph_generation or ""),
                fingerprints=target_fingerprints,
                source="rollback",
            )
        except RebuildAuditFailed as exc:
            # finish_staged_swap's generic message differs from this verb's legacy
            # text; preserve today's exact wording (attributes/cause untouched).
            exc.message = "rolled-back graph failed post-swap integrity verification"
            exc.args = (exc.message,)
            raise
        swapped = True
        final_sha256 = kg_cli._sha256_file(graph_path)
        result = {
            "phase": "complete",
            "swapped": True,
            "from_generation": current_generation,
            "graph_generation": target_identity.graph_generation,
            "sha256": final_sha256,
            "displaced_graph": str(displaced_graph),
            "pre_swap_audit": audit_payload,
            "post_swap_audit": swap_result.post_swap_audit,
            "semantic_gate": semantic_gate,
        }
        kg_cli._write_rebuild_state(state_path, result)
        return result
    except Exception as exc:
        if not swapped:
            kg_cli._discard_graph_family(staging_path)
            kg_cli._write_rebuild_state(
                state_path,
                {
                    "phase": "failed",
                    "from_generation": current_generation,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        raise
    finally:
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


def _run_rollback_neo4j(
    state: "ServerState",
    job: Any,
    *,
    vault_path: Path,
) -> dict:
    """Rollback for a Neo4j-pinned vault.

    Unlike :func:`_run_rollback_non_ladybug` (grafx), Neo4j's
    ``Neo4jStaging.commit``/``restore`` (``store/staging.py``) are an atomic
    pointer flip on the per-vault metadata singleton's ``graph_generation``/
    ``backup_tag`` properties, not a byte copy — the previous live
    generation's nodes/edges never leave the graph, they stay tagged with
    their own ``_generation``. So this rollback has no staged copy, no
    ``staging_port.discard``, no ``shutil.copytree``, and no
    ``_fsync_parent_dir`` to run: it is the fenced pointer flip alone,
    reusing the same :func:`_swap_under_runtime_fence` marshaling every
    other swap-owning verb uses.
    """
    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.store.staging import Neo4jStaging

    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    state_path = marginalia_dir / kg_cli._ROLLBACK_STATE
    staging_port, _ = kg_cli._swap_construction_for(vault_path, "neo4j")
    assert isinstance(staging_port, Neo4jStaging)

    current_generation = str(state.vault.store.generation())
    expected_generation = str(job.params.get("from_generation") or "").strip()
    if not expected_generation or expected_generation != current_generation:
        raise RuntimeError(
            "rollback target changed before execution: "
            f"expected={expected_generation or 'missing'} current={current_generation or 'missing'}"
        )

    from okto_neuron.config import VaultConfig
    from okto_neuron.curation.orchestrate import rollback_candidate

    storage_config = VaultConfig.load(vault_path).storage
    candidate = rollback_candidate(vault_path, "neo4j", storage_config)
    if candidate is None:
        raise RuntimeError(
            "rollback_unavailable: no swap backup recorded on the Neo4j metadata "
            "singleton's backup_tag property"
        )
    backup_tag = candidate.to_generation

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal rollback swap")

    job.progress("restoring prior generation pointer")
    state.mark_draining()
    swapped = False
    try:
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "validated",
                "from_generation": current_generation,
                "to_generation": backup_tag,
            },
        )
        job.progress("swapping rollback pointer")

        def _swap_graph() -> None:
            kg_cli._close_live_graph_handles(vault_path)
            staging_port.restore(staging_port.stage_path(backup_tag))

        async def _do_swap() -> None:
            await _swap_under_runtime_fence(state, job, _swap_graph)

        asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
        swapped = True

        graph_path = kg_cli._live_graph_path(vault_path, "neo4j")
        final_sha256 = kg_cli._sha256_of_graph(graph_path, "neo4j")
        result = {
            "phase": "complete",
            "swapped": True,
            "from_generation": current_generation,
            "graph_generation": backup_tag,
            "sha256": final_sha256,
        }
        kg_cli._write_rebuild_state(state_path, result)
        return result
    except Exception as exc:
        if not swapped:
            kg_cli._write_rebuild_state(
                state_path,
                {
                    "phase": "failed",
                    "from_generation": current_generation,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        raise
    finally:
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


def _run_rollback_non_ladybug(
    state: "ServerState",
    job: Any,
    *,
    vault_path: Path,
    backend_name: str,
) -> dict:
    """Rollback for a non-Ladybug (M4: grafx) pinned vault.

    A deliberately simpler sibling of the Ladybug :func:`run_rollback` body
    above: it restores the backup directory the most recent rebuild/heal/
    reembed swap produced (see
    :func:`okto_neuron.curation.orchestrate.rollback_candidate`)
    rather than a semantic-materialization-bound, generation-keyed rebuild
    artifact — Ladybug's own decision-sidefile/semantic-policy matching has
    no equivalent receipt on this backend yet (out of scope for M4 daemon
    parity; only the physical restore is asserted here). Reuses the SAME
    staged-copy -> audit -> fenced-commit shape :func:`run_heal` does so the
    checkpoint is COPIED (never moved) onto a staged path first — the
    original backup this rollback restored from stays intact afterwards,
    exactly like the Ladybug path's ``previous-graph.lbug``. The swap's own
    backup (produced by ``StagingPort.commit``) becomes the new ``displaced
    graph`` for a later incident, mirroring Ladybug's own displaced-graph
    contract.
    """
    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.curation.orchestrate import finish_staged_swap, rollback_candidate

    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    state_path = marginalia_dir / kg_cli._ROLLBACK_STATE
    staging_port, open_staged_store = kg_cli._swap_construction_for(vault_path, backend_name)
    graph_path = kg_cli._live_graph_path(vault_path, backend_name)
    assert open_staged_store is not None  # every non-ladybug backend supplies one

    current_generation = str(state.vault.store.generation())
    expected_generation = str(job.params.get("from_generation") or "").strip()
    if not expected_generation or expected_generation != current_generation:
        raise RuntimeError(
            "rollback target changed before execution: "
            f"expected={expected_generation or 'missing'} current={current_generation or 'missing'}"
        )

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal rollback swap")

    candidate = rollback_candidate(vault_path, backend_name, None)
    if candidate is None:
        raise RuntimeError(f"rollback_unavailable: no swap backup found for {backend_name!r} vault")
    backup_source = Path(candidate.source)
    dim = kg_cli._resolve_configured_dim(vault_path)
    staged_path = staging_port.stage_path("rollback")

    job.progress("validating rollback checkpoint")
    state.mark_draining()
    swapped = False
    try:
        staging_port.discard(staged_path)
        shutil.copytree(backup_source, staged_path)
        kg_cli._fsync_parent_dir(staged_path)

        # Learn the checkpoint's own identity by opening the (durable, just
        # copied) staged bytes through the SAME opener the swap tail reopens
        # with — mirrors run_heal's "open once, then close+reopen audit".
        probe_store, target_identity = open_staged_store(vault_path, staged_path, dim)
        probe_store.close()

        audit_result, audit_payload = kg_cli._audit_reopened_staged_store(
            vault_path,
            staged_path,
            dim=dim,
            expected_identity=target_identity,
            stage="rollback_before_swap",
            open_staged_store=open_staged_store,
        )
        if not audit_result.verified:
            raise RebuildAuditFailed(
                vault_path,
                staging_path=staged_path,
                audit_status=audit_result.status.value,
                message="rollback checkpoint failed physical integrity verification",
            )

        def _publish_and_track(vp: Path, result: Any, *, audit_id: str | None = None) -> None:
            kg_cli._publish_integrity_result(vp, result, audit_id=audit_id)
            state.integrity_last_audit = result

        def _audit_graph_path(
            path: Path, *, dim: int, expected_identity: Any, stage: str
        ) -> tuple[Any, dict[str, object]]:
            return kg_cli._audit_reopened_staged_store(
                vault_path,
                path,
                dim=dim,
                expected_identity=expected_identity,
                stage=stage,
                open_staged_store=open_staged_store,
            )

        committed_backup: dict[str, Path] = {}

        def _fenced_commit() -> Path:
            async def _do_swap() -> None:
                def _swap_graph() -> None:
                    kg_cli._close_live_graph_handles(vault_path)
                    committed_backup["path"] = staging_port.commit(staged_path, "rollback")

                await _swap_under_runtime_fence(state, job, _swap_graph)

            asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
            return committed_backup["path"]

        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "validated",
                "from_generation": current_generation,
                "to_generation": target_identity.graph_generation,
                "pre_swap_audit": audit_payload,
                "rollback_source": str(backup_source),
            },
        )
        job.progress("swapping rollback checkpoint")
        try:
            swap_result = finish_staged_swap(
                vault_path,
                staging_port,
                staged_path,
                lock=_FenceAlreadyGuardsSwap(),
                backup_tag="rollback",
                expected_identity=target_identity,
                dim=dim,
                stage_prefix="rollback",
                audit_graph_path=_audit_graph_path,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=_publish_and_track,
                commit=_fenced_commit,
                live_graph_path=graph_path,
            )
        except RebuildAuditFailed as exc:
            exc.message = "rolled-back graph failed post-swap integrity verification"
            exc.args = (exc.message,)
            raise
        swapped = True
        final_sha256 = kg_cli._sha256_of_graph(graph_path, backend_name)
        result = {
            "phase": "complete",
            "swapped": True,
            "from_generation": current_generation,
            "graph_generation": target_identity.graph_generation,
            "sha256": final_sha256,
            "displaced_graph": str(committed_backup.get("path", "")),
            "rollback_source": str(backup_source),
            "pre_swap_audit": audit_payload,
            "post_swap_audit": swap_result.post_swap_audit,
        }
        kg_cli._write_rebuild_state(state_path, result)
        return result
    except Exception as exc:
        if not swapped:
            staging_port.discard(staged_path)
            kg_cli._write_rebuild_state(
                state_path,
                {
                    "phase": "failed",
                    "from_generation": current_generation,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        raise
    finally:
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


def run_heal(state: "ServerState", job: Any) -> dict:
    """In-process heal (``job.kind == 'heal'``) — DETERMINISTIC topology collapse,
    NO LLM. Materializes the confirmed off-graph equivalence fold into the graph
    topology via a graph→fresh-graph COPY (``copy_graph_canonicalizing``) + atomic
    swap. Mirrors :func:`run_reembed` (reads the live graph via the daemon's own
    handle, builds a fresh graph at a tmp path, marshals the swap), NOT
    :func:`run_rebuild` (markdown + LLM).

    UI-STAYS-UP POLICY (ADR 0009: daemon down ONLY at the swap instant). UNLIKE
    ``run_rebuild``/``run_reembed``, the heal does NOT ``mark_draining`` for the whole
    job. The long copy phase only READS the live graph (a single
    ``list_nodes``/``list_edges`` snapshot) and writes a SEPARATE tmp graph; it never
    mutates the live graph, so reads (query/recall/browse/graph/nodes/health) must keep
    returning 200 throughout the ~minutes-long copy. ``writes=True`` (see
    :func:`register_runners`) makes ``_jobs._drain`` hold ``state.writer_lock`` for the
    WHOLE job, and that lock ALONE serializes the heal vs ingest: the ingest worker
    (:mod:`_ingest_queue._drain`) acquires the same ``writer_lock`` before every
    ``remember`` and so cannot write the live graph while the heal holds it. The
    snapshot read AND the swap both run under that one continuously-held lock (the swap
    is marshaled onto the loop while ``_drain`` is suspended at ``await to_thread`` but
    still owns the lock), so no ingest write can interleave between the snapshot and the
    swap — race-free WITHOUT draining.

    OBSERVABLE EFFECT ON READS: reads return 200 for the WHOLE copy. ``draining`` is set
    only around the final swap (inside ``_do_swap``, right before
    ``_close_live_graph_handles``, cleared after ``Vault.open``) as a belt-and-suspenders
    guard — it would 503 a read ONLY if one could interleave with the swap. But
    ``_do_swap`` is a single NO-AWAIT block on the loop, so a read that arrives during the
    swap is not handled until the block returns (draining already cleared) and is then
    served 200 against the reopened graph. Net: no read ever observes a 503; the swap
    manifests as a brief one-time latency blip (close→os.replace→``Vault.open``, ~1s+
    depending on graph size), NOT a 503 window. (Verified E2E: 100% of reads returned
    200 across a ~6min copy; the swap was a single delayed-200, never a 503.)

    INTEGRITY (ADR 0039): this runner is fence-equivalent to the CLI heal. It refuses
    a fenced source generation (:func:`require_unfenced_generation`), audits the
    staging graph BEFORE the swap, and marks/publishes the integrity verdict for the
    generation it mints, so the sidecar never names the superseded generation.

    On any failure BEFORE the swap, the tmp graph is discarded and the live handle
    stays open."""
    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.predicates import PredicateAliasIndex
    from okto_neuron.store.integrity_state import require_unfenced_generation
    from okto_neuron.store.reembed import copy_graph_canonicalizing

    vault_path = Path(state.vault_path)
    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    # M4 spec §2 ("one construction seam generalized"): registry-driven
    # staging port + staged-store opener, resolved once up front (same
    # ordering as run_rebuild) — a ladybug-pinned (or unpinned, pre-M4) vault
    # gets byte-identical values below.
    backend_name = kg_cli._resolve_pinned_backend(vault_path)
    staging_port, open_staged_store = kg_cli._swap_construction_for(vault_path, backend_name)
    graph_path = kg_cli._live_graph_path(vault_path, backend_name)
    # Distinct build-target base name — same reason as run_rebuild/run_reembed:
    # ``graph.lbug.tmp`` is ladybug's own scratch for the live handle and would be
    # deleted at swap time. Distinct from graph.rebuild.lbug and graph.reembed.lbug.
    # ``staging_port.stage_path("heal")`` gives Ladybug that exact literal
    # (``graph.heal.lbug``) and Grafx its own directory sibling.
    tmp_graph_path = staging_port.stage_path("heal")
    backup_graph_path = graph_path.with_name(f"{graph_path.name}.bak")
    state_path = marginalia_dir / kg_cli._REBUILD_STATE

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal swap")

    job.progress("preparing")
    # ADR 0039 fence — SAME rule as the CLI heal: never canonicalize a generation
    # already proven damaged (or unattestable) into a fresh graph that then looks
    # clean. Raised before anything is written, so nothing to unwind.
    require_unfenced_generation(vault_path)
    # Deliberately NO mark_draining() here — reads must stay 200 during the copy. The
    # writer_lock held by _drain (writes=True) is what serializes ingest behind the
    # heal; draining is set only inside _do_swap, around the ~1s swap.
    swapped = False
    try:
        # Belt-and-braces: writer_lock already blocks any new ingest write, but wait
        # out an item that entered the lock/thread before the heal acquired the lock so
        # the snapshot below cannot race a write that is already in flight. (Under the
        # serialized worker this is normally already drained; the wait is a cheap
        # margin, not the safety mechanism — the lock is.)
        deadline = time.monotonic() + _INGEST_DRAIN_TIMEOUT
        while state.ingest_worker_active and time.monotonic() < deadline:
            time.sleep(0.05)

        # The equivalence fold (member_id -> canonical_id), the SAME map recall /
        # Browse / Graph already fold through (Vault._equivalence_map via the
        # AuthorityIndex). None/empty => the heal degenerates to a verbatim copy.
        equivalence = equivalence_map(state)
        records = len(authority_index(state).records())
        predicate_index = PredicateAliasIndex(vault_path)
        predicate_aliases = predicate_index.alias_map() or None
        inverse_aliases = predicate_index.inverse_map() or None

        # Read the LIVE graph through the daemon's own handle (still open — NO second
        # handle). This is the source the deterministic copy folds.
        job.progress("reading")
        nodes = list(state.vault.store.list_nodes())
        edges = list(state.vault.store.list_edges())
        # Bootstrap the tmp graph at the EXISTING vectors' width (the copy preserves
        # embeddings VERBATIM, so the fixed-width vector column must match the copied
        # vectors; config dimension may have drifted).
        live_dim = _stored_embedding_dim(nodes)

        staging_port.discard(tmp_graph_path)
        started_at = kg_cli._utc_now()
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "in_progress",
                "files_done": [],
                "current_file": None,
                "started_at": started_at,
            },
        )

        from okto_neuron.curation.orchestrate import _ladybug_open_staged_store

        # Key the tmp store on tmp_graph_path (NOT vault_path) so it gets its OWN
        # handle and does not bind to the daemon's live handle — same fix as
        # run_reembed. ADR-0007-safe: distinct db files.
        # M4 spec §2 ("one construction seam generalized"): resolves the dim
        # the SAME way orchestrate.heal() does (a concrete int up front) — a
        # ladybug-pinned (or unpinned, pre-M4) vault gets byte-identical
        # behaviour, reusing the exact same default closure orchestrate.py's
        # heal()/reembed() fall back to. ``staging_port``/``open_staged_store``
        # were already resolved once, up front.
        staging_dim = live_dim if live_dim is not None else kg_cli._resolve_configured_dim(vault_path)
        open_store = open_staged_store or _ladybug_open_staged_store(kg_cli._bootstrap_graph_at_path)
        store, identity = open_store(vault_path, tmp_graph_path, staging_dim)

        def _audit_graph_path(
            path: Path, *, dim: int, expected_identity: Any, stage: str
        ) -> tuple[Any, dict[str, object]]:
            if open_staged_store is None:
                return kg_cli._audit_rebuild_graph_path(
                    path, dim=dim, expected_identity=expected_identity, stage=stage
                )
            return kg_cli._audit_reopened_staged_store(
                vault_path,
                path,
                dim=dim,
                expected_identity=expected_identity,
                stage=stage,
                open_staged_store=open_staged_store,
            )

        try:
            job.progress("collapsing")
            stats = copy_graph_canonicalizing(
                nodes,
                edges,
                equivalence,
                store,
                predicate_aliases=predicate_aliases,
                inverse_aliases=inverse_aliases,
            )
            store.close()
            health = store.health()
            if not health.healthy:
                raise VaultCorrupted(tmp_graph_path, message=health.detail)
            # Same pre-swap gate as the CLI heal: nothing reaches live until the FULL
            # integrity audit passes on the durable staging bytes, reopened read-only.
            job.progress("auditing staging")
            staging_result, _ = _audit_graph_path(
                tmp_graph_path,
                dim=staging_dim,
                expected_identity=identity,
                stage="heal_after_close_reopen",
            )
            if not staging_result.verified:
                raise RebuildAuditFailed(
                    vault_path,
                    staging_path=tmp_graph_path,
                    audit_status=staging_result.status.value,
                    message=(
                        "healed staging graph failed integrity verification; "
                        "live graph left unchanged"
                    ),
                )
        except Exception:
            # ``tmp_handle`` no longer reaches this scope directly (it lives
            # inside the opener closure); ``_close_tmp_rebuild_handles`` only
            # ever touches it when ``store`` itself is ``None``, which cannot
            # be true here (the opener above already returned one), so this
            # is behaviourally identical to the old ``(store, tmp_handle)`` call.
            kg_cli._close_tmp_rebuild_handles(store, None)
            raise

        from okto_neuron.curation.orchestrate import finish_staged_swap

        def _publish_and_track(vp: Path, result: Any, *, audit_id: str | None = None) -> None:
            # Publish the verdict for the generation THIS heal minted. Skipping it
            # would leave the sidecar naming the superseded generation, so a healthy
            # heal would read back as stale and fence every later write.
            kg_cli._publish_integrity_result(vp, result, audit_id=audit_id)
            state.integrity_last_audit = result

        # Populated by ``_fenced_commit`` for a non-Ladybug backend only —
        # Grafx's real backup location (``StagingPort.commit``'s own
        # sibling-tag naming, e.g. ``graph.grafx.bak``) never equals the
        # Ladybug-shaped ``backup_graph_path`` literal above. For Ladybug it
        # stays empty and every read below falls back to that literal
        # unchanged.
        committed_backup: dict[str, Path] = {}

        def _reported_backup_path() -> Path:
            return committed_backup.get("path", backup_graph_path)

        def _fenced_commit() -> Path:
            async def _do_swap() -> None:
                def _swap_graph() -> None:
                    kg_cli._close_live_graph_handles(vault_path)
                    if backend_name == "ladybug":
                        kg_cli._swap_rebuilt_graph(
                            vault_path, graph_path, tmp_graph_path, backup_graph_path
                        )
                    else:
                        committed_backup["path"] = staging_port.commit(tmp_graph_path, "bak")

                await _swap_under_runtime_fence(state, job, _swap_graph)

            asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
            return _reported_backup_path()

        job.progress("swapping")
        try:
            finish_staged_swap(
                vault_path,
                staging_port,
                tmp_graph_path,
                lock=_FenceAlreadyGuardsSwap(),
                backup_tag="heal",
                expected_identity=identity,
                dim=staging_dim,
                stage_prefix="heal",
                audit_graph_path=_audit_graph_path,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=_publish_and_track,
                commit=_fenced_commit,
                live_graph_path=graph_path,
            )
        except RebuildAuditFailed as exc:
            # finish_staged_swap's generic message differs from this verb's legacy
            # text; preserve today's exact wording (attributes/cause untouched).
            exc.message = (
                "post-swap healed graph failed integrity verification; "
                f"previous graph retained at {_reported_backup_path()}"
            )
            exc.args = (exc.message,)
            raise
        swapped = True

        final_sha256 = kg_cli._sha256_of_graph(graph_path, backend_name)
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "complete",
                "completed_at": kg_cli._utc_now(),
                "sha256": final_sha256,
                "heal": {"records": records, **stats},
            },
        )
        return {
            "phase": "complete",
            "swapped": swapped,
            "sha256": final_sha256,
            "heal": {"records": records, **stats},
        }
    except Exception:
        if not swapped:
            staging_port.discard(tmp_graph_path)
            kg_cli._write_rebuild_state(
                state_path,
                {"phase": "failed", "failed_at": kg_cli._utc_now()},
            )
        raise
    finally:
        # Safety-net only: the heal does NOT drain for the job; draining is set and
        # cleared inside _do_swap. This clears it if the swap raised after
        # mark_draining() but before clearing, so reads never stay 503 past the job.
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


def _stored_embedding_dim(nodes: list[Any]) -> int | None:
    """Width of the first stored vector, or ``None`` when no node carries one (then
    bootstrap falls back to the configured dimension)."""
    for node in nodes:
        embedding = getattr(node, "embedding", None)
        if embedding:
            return len(embedding)
    return None


def run_reembed(state: "ServerState", job: Any) -> dict:
    """In-process vectors-only reembed via the same fresh-build + marshaled-swap
    pattern as :func:`run_rebuild`. Optional this round; kept minimal. Reads the
    live graph through a RAW handle, re-embeds into a fresh graph, swaps."""
    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.config import VaultConfig
    from okto_neuron.curation.orchestrate import _ladybug_open_staged_store
    from okto_neuron.embed import get_provider
    from okto_neuron.store.reembed import copy_graph_reembedding

    vault_path = Path(state.vault_path)
    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    # M4 spec §2 ("one construction seam generalized"): registry-driven
    # staging port + staged-store opener, resolved once up front (same
    # ordering as run_rebuild/run_heal) — a ladybug-pinned (or unpinned,
    # pre-M4) vault gets byte-identical values below.
    backend_name = kg_cli._resolve_pinned_backend(vault_path)
    staging_port, open_staged_store = kg_cli._swap_construction_for(vault_path, backend_name)
    graph_path = kg_cli._live_graph_path(vault_path, backend_name)
    # Distinct build-target base name — same reason as run_rebuild: ``graph.lbug.tmp``
    # is ladybug's own scratch for the live handle and would be deleted at swap time.
    # ``staging_port.stage_path("reembed")`` gives Ladybug that exact literal
    # (``graph.reembed.lbug``) and Grafx its own directory sibling.
    tmp_graph_path = staging_port.stage_path("reembed")
    backup_graph_path = graph_path.with_name(f"{graph_path.name}.bak")
    state_path = marginalia_dir / kg_cli._REEMBED_STATE

    loop = state.loop
    if loop is None:
        raise RuntimeError("server event loop not captured; cannot marshal swap")

    try:
        cfg = VaultConfig.load(vault_path).embedding
    except Exception:  # noqa: BLE001
        from okto_neuron.config import EmbeddingConfig

        cfg = EmbeddingConfig()
    embedder = get_provider(cfg)
    new_dim = int(cfg.dimension)

    job.progress("preparing")
    state.mark_draining()
    swapped = False
    try:
        deadline = time.monotonic() + _INGEST_DRAIN_TIMEOUT
        while state.ingest_worker_active and time.monotonic() < deadline:
            time.sleep(0.05)

        started_at = kg_cli._utc_now()
        # Read the LIVE graph through the daemon's own handle (still open — no second
        # handle): list nodes/edges to replay through the embedder.
        job.progress("reading")
        nodes = list(state.vault.store.list_nodes())
        edges = list(state.vault.store.list_edges())

        staging_port.discard(tmp_graph_path)
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "embedding",
                "nodes_done": 0,
                "nodes_total": len(nodes),
                "started_at": started_at,
            },
        )
        # Key the tmp store on tmp_graph_path (NOT vault_path) so it gets its OWN
        # handle and does not bind to the daemon's live handle — same fix as
        # _build_fresh_graph (see the note there). ADR-0007-safe: distinct db files.
        # ``staging_port``/``open_staged_store`` were already resolved once,
        # up front.
        open_store = open_staged_store or _ladybug_open_staged_store(kg_cli._bootstrap_graph_at_path)
        store, identity = open_store(vault_path, tmp_graph_path, new_dim)
        try:

            def _p(done: int, total: int) -> None:
                job.progress(f"embedding {done}/{total}")

            def _embedding_settings() -> tuple[int, int]:
                latest = VaultConfig.load(vault_path).embedding
                return latest.batch_size, latest.max_concurrent_batches

            stats = copy_graph_reembedding(
                nodes,
                edges,
                store,
                embedder,
                batch_size=cfg.batch_size,
                max_concurrent_batches=cfg.max_concurrent_batches,
                embedding_settings=_embedding_settings,
                progress=_p,
            )
            store.close()
            health = store.health()
            if not health.healthy:
                raise VaultCorrupted(tmp_graph_path, message=health.detail)
        except Exception:
            # ``tmp_handle`` no longer reaches this scope directly (it lives
            # inside the opener closure); ``_close_tmp_rebuild_handles`` only
            # ever touches it when ``store`` itself is ``None``, which cannot
            # be true here (the opener above already returned one), so this
            # is behaviourally identical to the old ``(store, tmp_handle)`` call.
            kg_cli._close_tmp_rebuild_handles(store, None)
            raise

        from okto_neuron.curation.orchestrate import finish_staged_swap

        # Populated by ``_fenced_commit`` for a non-Ladybug backend only —
        # Grafx's real backup location (``StagingPort.commit``'s own
        # sibling-tag naming, e.g. ``graph.grafx.bak``) never equals the
        # Ladybug-shaped ``backup_graph_path`` literal above. For Ladybug it
        # stays empty and every read below falls back to that literal
        # unchanged.
        committed_backup: dict[str, Path] = {}

        def _fenced_commit() -> Path:
            async def _do_swap() -> None:
                def _swap_graph() -> None:
                    kg_cli._close_live_graph_handles(vault_path)
                    if backend_name == "ladybug":
                        kg_cli._swap_rebuilt_graph(
                            vault_path, graph_path, tmp_graph_path, backup_graph_path
                        )
                    else:
                        committed_backup["path"] = staging_port.commit(tmp_graph_path, "bak")

                await _swap_under_runtime_fence(state, job, _swap_graph)

            asyncio.run_coroutine_threadsafe(_do_swap(), loop).result()
            return committed_backup.get("path", backup_graph_path)

        job.progress("swapping")
        # Preserves reembed's documented asymmetry (spec §1/§7 OQ2): vectors-only,
        # no topology change, so only a bare health check gates the swap — no
        # post-swap audit, no generation fenced/published (``publish_integrity=False``).
        finish_staged_swap(
            vault_path,
            staging_port,
            tmp_graph_path,
            lock=_FenceAlreadyGuardsSwap(),
            backup_tag="reembed",
            expected_identity=identity,
            dim=new_dim,
            stage_prefix="reembed",
            publish_integrity=False,
            commit=_fenced_commit,
            live_graph_path=graph_path,
        )
        swapped = True

        final_sha256 = kg_cli._sha256_of_graph(graph_path, backend_name)
        kg_cli._write_rebuild_state(
            state_path,
            {
                "phase": "complete",
                "completed_at": kg_cli._utc_now(),
                "sha256": final_sha256,
                "embedding_dim": new_dim,
                **stats,
            },
        )
        return {"phase": "complete", "swapped": swapped, "sha256": final_sha256, **stats}
    except Exception:
        if not swapped:
            staging_port.discard(tmp_graph_path)
            kg_cli._write_rebuild_state(
                state_path,
                {"phase": "failed", "failed_at": kg_cli._utc_now()},
            )
        raise
    finally:
        if not state.vault_pool.is_fenced(vault_path):
            state.draining = False


# ── read-time equivalence fold (Browse/Graph dedupe like search) ────────────────
# These are PURE transforms over already-fetched node/edge lists. They consult the
# same off-graph map ``vault._equivalence_map()`` that ``query.search_claims``
# folds through. The stored graph topology is never changed (off-graph, reversible
# — drop the authority record and the fold vanishes). ``equivalence`` is
# ``member_id -> canonical_id`` (canonical maps to itself); ``None`` => no-op.


def equivalence_map(state: "ServerState") -> dict[str, str] | None:
    """The daemon's current member->canonical fold, or None when nothing is
    reconciled. Delegates to the same ``Vault._equivalence_map`` that recall uses,
    so Browse/Graph and search stay in lockstep."""
    try:
        return state.vault._equivalence_map()  # noqa: SLF001 — intentional reuse
    except Exception:  # noqa: BLE001 — a bad index must not break a read
        return None


def fold_node_list(
    nodes: list[Any], equivalence: dict[str, str] | None
) -> tuple[list[Any], dict[str, int]]:
    """Collapse an equivalence class to its canonical representative for the node
    list. Returns ``(kept_nodes, variant_counts)`` where ``kept_nodes`` drops
    non-canonical variants whose canonical is present, and ``variant_counts`` maps
    a surviving canonical id -> number of folded-away variants (for a UI badge).

    A variant whose canonical is NOT in this page is kept as-is (so a filtered/
    paginated slice never silently loses a node). Order is preserved."""
    if not equivalence:
        return nodes, {}
    present = {str(getattr(n, "id", "")) for n in nodes}
    kept: list[Any] = []
    variant_counts: dict[str, int] = {}
    for n in nodes:
        nid = str(getattr(n, "id", ""))
        canonical = equivalence.get(nid, nid)
        if canonical == nid:
            kept.append(n)
        elif canonical in present:
            # Folded away — its canonical carries it; bump the badge count.
            variant_counts[canonical] = variant_counts.get(canonical, 0) + 1
        else:
            kept.append(n)  # canonical absent from this page → keep the variant
    return kept, variant_counts


def fold_graph(
    nodes: list[dict],
    edges: list[dict],
    equivalence: dict[str, str] | None,
) -> tuple[list[dict], list[dict]]:
    """Collapse variant nodes onto canonical for the graph view, then remap every
    edge through the map, drop self-loops the collapse creates, and de-dupe.

    THE correctness trap (per ADR/critique): "looks deduped" means collapse, and
    collapsing nodes WITHOUT remapping edges leaves edges pointing at nodes no
    longer in the set. We:
      1. keep only canonical-or-unmapped nodes (a variant whose canonical is
         present is dropped);
      2. remap each edge's src/dst to its canonical;
      3. drop edges whose endpoints don't both survive, and drop self-loops;
      4. de-dupe (src, dst, type).
    ``nodes``/``edges`` are the already-serialized dict rows from ``api_graph``."""
    if not equivalence:
        return nodes, edges

    node_ids = {str(n.get("id")) for n in nodes}

    def repr_in_view(nid: str) -> str:
        """The id a node collapses to WITHIN THIS VIEW. ``api_graph`` caps nodes by
        degree BEFORE the fold, so a variant can be in the view while its canonical
        was capped out. In that case the variant must represent ITSELF (else its
        real edges are dropped, the inverse of the dangling-edge bug). The canonical
        is used only when it is actually present in the fetched node set."""
        canonical = equivalence.get(nid, nid)
        return canonical if canonical in node_ids else nid

    # 1. Surviving node set: a node survives iff it represents itself in-view (its
    #    canonical is absent, OR it IS the canonical). A variant whose canonical is
    #    present collapses away.
    kept_nodes: list[dict] = []
    surviving: set[str] = set()
    for n in nodes:
        nid = str(n.get("id"))
        if repr_in_view(nid) == nid:
            kept_nodes.append(n)
            surviving.add(nid)
    # 2-4. Remap each edge endpoint through the in-view representative, drop edges
    #      whose endpoints don't both survive, drop self-loops, dedupe (src,dst,type).
    seen: set[tuple[str, str, str]] = set()
    kept_edges: list[dict] = []
    for e in edges:
        s = repr_in_view(str(e.get("src")))
        d = repr_in_view(str(e.get("dst")))
        if s not in surviving or d not in surviving:
            continue
        if s == d:
            continue  # self-loop created by the collapse
        key = (s, d, str(e.get("type")))
        if key in seen:
            continue
        seen.add(key)
        row = dict(e)
        row["src"] = s
        row["dst"] = d
        kept_edges.append(row)
    return kept_nodes, kept_edges


def canonical_for(node_id: str, equivalence: dict[str, str] | None) -> str | None:
    """Return the canonical id for a node if it is a FOLDED VARIANT, else None.
    Used by node-detail to annotate/redirect a variant to its canonical."""
    if not equivalence:
        return None
    canonical = equivalence.get(node_id)
    if canonical is None or canonical == node_id:
        return None
    return canonical


def register_runners() -> None:
    """Register reconcile runners with the generic job queue. Idempotent."""
    from okto_neuron.server import _jobs

    # Both write only off-graph JSON side-files; we still mark them ``writes`` so
    # the worker serializes them under writer_lock (two apply jobs must not
    # interleave their authority/queue writes, and propose's heavy reads stay
    # behind any in-flight write for a consistent snapshot).
    # Proposal is graph-read-only, but needs writer-lock serialization plus the
    # integrity fence so one pass observes a stable, verified generation.
    _jobs.register_runner(
        "reconcile-propose",
        run_propose,
        writes=False,
        verified_snapshot=True,
    )
    _jobs.register_runner("reconcile-apply", run_apply, writes=True)
    _jobs.register_runner("predicate-propose", run_predicate_propose, writes=False)
    _jobs.register_runner("predicate-apply", run_predicate_apply, writes=True)
    _jobs.register_runner("companion-triage", run_companion_triage, writes=True)
    # ADR 0009 P4: detect-drift as a queue job (read-only, writes=False) so the
    # continuous-curation scheduler can submit it. Deterministic Findings, no LLM,
    # no writer_lock — reads/ingest stay up during a background sweep.
    _jobs.register_runner("detect-drift", run_detect_drift, writes=False)
    # ADR 0009 P3: in-process rebuild/heal/reembed on the runtime's leased handle.
    # writes=True => _drain holds this runtime's writer_lock for the whole job (this
    # serializes its ingest behind the job for all three). rebuild/reembed also set draining for the
    # whole job; heal sets draining ONLY around its ~1s swap so reads stay 200 during
    # the copy (see the policy note above and run_heal's docstring).
    _jobs.register_runner(
        "rebuild",
        run_rebuild,
        writes=True,
        interrupted_recovery=_recover_interrupted_rebuild,
    )
    _jobs.register_runner("rollback", run_rollback, writes=True)
    # Heal is a SEPARATE deterministic no-LLM copy runner (ADR 0009 P3 redo), NOT
    # a heal-mode of run_rebuild — the entities are already extracted; the heal only
    # materializes the off-graph equivalence fold into the topology.
    _jobs.register_runner("heal", run_heal, writes=True)
    _jobs.register_runner("reembed", run_reembed, writes=True)


__all__ = [
    "authority_index",
    "reconcile_queue",
    "predicate_alias_index",
    "cluster_verdict_row",
    "queued_cluster_row",
    "authority_record_row",
    "predicate_record_row",
    "run_propose",
    "run_apply",
    "run_predicate_propose",
    "run_predicate_apply",
    "run_companion_triage",
    "run_detect_drift",
    "run_rebuild",
    "run_rollback",
    "run_heal",
    "run_reembed",
    "register_runners",
    "equivalence_map",
    "fold_node_list",
    "fold_graph",
    "canonical_for",
]
