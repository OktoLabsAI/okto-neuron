"""Golden eval set + transport-agnostic scorers for okto-neuron recall quality.

This module is the single source of truth for the recall/ask eval, shared by:
  - tests/test_eval_recall_quality.py    (CI-safe, deterministic, no LLM — fastembed only)
  - tests/acceptance/scenarios/92_eval_recall_provenance.sh  (real server + /api/v1/recall)

Metrics scored (no mocks, grounded against the FIXED synthetic-vault fixture):
  - expected-entity-present@k : for each golden query, is the expected source file
    present among the top-k hits? Reported as a fraction; gated by ENTITY_AT_K_MIN.
  - provenance-valid          : every Claim hit's byte range, sliced out of the source
    file on disk, must sha256 back to the stored content_hash. ANY mismatch fails.
  - partner-recall            : a designated hard-gate query MUST surface its target
    in top-k. This is the "known-good query never regresses" gate.

A "hit" is the transport-neutral dict: {"path", "byte_start", "byte_end",
"content_hash", "type"(optional)}. Both the in-process QueryHit and the
/api/v1/recall JSON shape normalize into this via `normalize_hit`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fixed eval vault. Same fixture FR3 golden tests use — stable, fabricated, offline.
FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic-vault"

# top-k used for every eval query.
K = 10

# Gate: at least this fraction of golden queries must surface their expected file.
ENTITY_AT_K_MIN = 5 / 6  # >= 5 of 6


@dataclass(frozen=True)
class GoldenCase:
    id: str
    query: str
    # The expected source file, vault-relative posix. Matched by basename OR suffix
    # (provenance.path may be a bare filename or a relative path depending on store).
    expect_path: str
    partner_recall: bool = False  # hard gate: MUST be in top-k


GOLDEN: list[GoldenCase] = [
    GoldenCase(
        id="handoff_owner_invoice",
        query="handoff owner invoice",
        expect_path="handoff.md",
        partner_recall=True,  # the partner-recall hard gate
    ),
    GoldenCase(
        id="ladybug_kuzu_decision",
        query="ladybug kuzu decision json fallback store",
        expect_path="decisions/ladybug-kuzu.md",
    ),
    GoldenCase(
        id="acme_proposal_author",
        query="ACME proposal markdown-first knowledge workflow",
        expect_path="proposals/proposal-acme-2026.md",
    ),
    GoldenCase(
        id="sow_q1_signed",
        query="statement of work signed Q1 2026 scope workstreams",
        expect_path="sows/sow-2026-q1.md",
    ),
    GoldenCase(
        id="overdue_intake_commitment",
        query="overdue intake commitment due date stale promise",
        expect_path="drift_commitment.md",
    ),
    GoldenCase(
        id="provenance_concept",
        query="provenance visible history of a note who created it when",
        expect_path="concepts/concept-provenance.md",
    ),
]


# ── hit normalization ─────────────────────────────────────────────────────────


def normalize_hit(hit: Any) -> dict[str, Any]:
    """Normalize an in-process QueryHit OR an /api/v1/recall JSON hit into a flat dict."""
    if isinstance(hit, dict):
        prov = hit.get("provenance") or {}
        node = hit.get("node") or {}
        return {
            "path": prov.get("path") or "",
            "byte_start": prov.get("byte_start"),
            "byte_end": prov.get("byte_end"),
            "content_hash": prov.get("content_hash") or "",
            "type": node.get("type") or hit.get("type") or "",
        }
    # in-process QueryHit
    prov = getattr(hit, "provenance", None)
    node = getattr(hit, "node", None)
    return {
        "path": getattr(prov, "path", "") or "",
        "byte_start": getattr(prov, "byte_start", None),
        "byte_end": getattr(prov, "byte_end", None),
        "content_hash": getattr(prov, "content_hash", "") or "",
        "type": str(getattr(node, "type", "")) if node is not None else "",
    }


def _path_matches(hit_path: str, expect_path: str) -> bool:
    if not hit_path:
        return False
    nx = hit_path.replace("\\", "/")
    exp = expect_path.replace("\\", "/")
    return nx.endswith(exp) or Path(nx).name == Path(exp).name


def entity_present(hits: list[dict[str, Any]], expect_path: str) -> bool:
    return any(_path_matches(h["path"], expect_path) for h in hits)


# ── provenance byte-range validation ──────────────────────────────────────────


def _resolve_source(vault_root: Path, rel_or_abs: str) -> Path | None:
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    if p.is_absolute() and p.exists():
        return p
    cand = vault_root / rel_or_abs
    if cand.exists():
        return cand
    # provenance.path may be a bare filename — locate it under the vault.
    matches = list(vault_root.rglob(Path(rel_or_abs).name))
    return matches[0] if len(matches) == 1 else (matches[0] if matches else None)


def validate_provenance(vault_root: Path, hit: dict[str, Any]) -> tuple[bool, str]:
    """Re-derive the block bytes from disk and confirm they sha256 to content_hash.

    Returns (ok, reason). Only byte-anchored node types (Claim, Block) carry a real
    byte range; Activity/Agent/Concept/etc. expose a node-id content-hash fallback
    with a zero-length range and are skipped (nothing to validate against source).
    """
    ch = hit.get("content_hash") or ""
    bs, be = hit.get("byte_start"), hit.get("byte_end")
    node_type = hit.get("type") or ""
    if node_type and node_type not in {"Claim", "Block"}:
        return True, f"not byte-anchored ({node_type}, skipped)"
    if not ch or bs is None or be is None or int(be) <= int(bs):
        return True, "no byte range (skipped)"
    expected_hex = ch.split(":", 1)[-1].lower()
    if not expected_hex:
        return True, "empty-hash (skipped)"
    src = _resolve_source(vault_root, hit.get("path") or "")
    if src is None:
        return False, f"source file not found for path={hit.get('path')!r}"
    try:
        raw = src.read_bytes()[int(bs) : int(be)]
    except OSError as exc:
        return False, f"read failed: {exc}"
    actual_hex = hashlib.sha256(raw).hexdigest()
    if actual_hex != expected_hex:
        return (
            False,
            f"hash mismatch path={src.name} bytes[{bs}:{be}] "
            f"expected={expected_hex[:12]}… actual={actual_hex[:12]}…",
        )
    return True, "ok"


# ── full scorer ────────────────────────────────────────────────────────────────


def score(
    vault_root: Path,
    query_fn,
    *,
    k: int = K,
) -> dict[str, Any]:
    """Run every golden query through `query_fn(query, k) -> list[hit]` and score.

    `query_fn` returns either in-process QueryHit objects or /api/v1/recall JSON
    hit dicts; both are normalized. Returns a structured report dict.
    """
    per_query: list[dict[str, Any]] = []
    prov_failures: list[str] = []
    partner_failures: list[str] = []
    entity_hits = 0

    for case in GOLDEN:
        raw_hits = query_fn(case.query, k)
        hits = [normalize_hit(h) for h in raw_hits]
        present = entity_present(hits, case.expect_path)
        if present:
            entity_hits += 1
        if case.partner_recall and not present:
            partner_failures.append(f"{case.id}: expected {case.expect_path} not in top-{k}")

        # provenance validity for every claim-bearing hit
        bad = []
        for h in hits:
            ok, reason = validate_provenance(vault_root, h)
            if not ok:
                bad.append(reason)
                prov_failures.append(f"{case.id}: {reason}")
        per_query.append(
            {
                "id": case.id,
                "query": case.query,
                "expect_path": case.expect_path,
                "entity_present": present,
                "n_hits": len(hits),
                "prov_failures": bad,
            }
        )

    total = len(GOLDEN)
    entity_at_k = entity_hits / total if total else 0.0
    return {
        "k": k,
        "total_queries": total,
        "entity_hits": entity_hits,
        "entity_at_k": entity_at_k,
        "entity_at_k_min": ENTITY_AT_K_MIN,
        "entity_gate_pass": entity_at_k >= ENTITY_AT_K_MIN,
        "provenance_failures": prov_failures,
        "provenance_gate_pass": not prov_failures,
        "partner_recall_failures": partner_failures,
        "partner_recall_gate_pass": not partner_failures,
        "per_query": per_query,
        "overall_pass": (
            entity_at_k >= ENTITY_AT_K_MIN and not prov_failures and not partner_failures
        ),
    }
