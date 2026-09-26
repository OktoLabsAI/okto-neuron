#!/usr/bin/env python3
"""DETERMINISTIC RECALL + EXTRACTION FLOOR — CI trust anchors (2) and (3).

This is the second half of the deterministic eval gate. The provenance byte-hash
gate (``judge.py floor``) is a pure function of the committed dataset *files* — no
graph at all. This module gates the two signals that DO need a graph but are still
bit-reproducible because they run over a FROZEN, committed Ladybug vault with NO
daemon, NO LLM, and NO ANN randomness:

  (2) HARD-RECALL@k + MRR@k + PRECISION@k
      For each question, run the SAME retrieval the daemon ``/recall`` runs
      (``Vault.query`` -> ``Vault._search`` -> ``okto_neuron.query.search_claims``)
      over the frozen ``graph.lbug``. HARD-RECALL@k asks: does each gold target's
      SOURCE FILE appear in the top-k provenance? MRR@k and PRECISION@k add the
      RANKING-QUALITY floor Requirement 1 demands so the floor cannot saturate the
      way coarse membership can: MRR@k = mean over answerable questions of 1/(rank
      of the first gold-relevant hit in top-k) (0 if none); PRECISION@k = mean of
      (#gold-relevant hits in top-k)/k. All three share ONE relevance definition
      (a hit's source file is a gold source file), and the near-duplicate
      ``distractors`` in each question compete for top-k slots so the ranking
      metrics stay < 1.0. Bypasses only the HTTP/JSON layer (irrelevant to
      ranking), so it is a faithful, daemon-free reproduction of ``/recall``.

  (3) EXTRACTION-COMPLETENESS
      For each gold target, does a Claim node exist anchored (via its Block) to the
      gold source file, with the Claim's byte span covering the gold quote's bytes?
      Retrieval-INDEPENDENT (never calls query): it isolates extraction recall from
      query recall — "does the graph KNOW this fact?".

WHY THIS IS BIT-REPRODUCIBLE (proven, not assumed)
--------------------------------------------------
The determinism spike ran the underlying probe 5x in independent processes over
the frozen ``graph.lbug`` and got byte-identical reports (sha256 stable, per-
question ranks stable incl. explicit ``None`` misses). The sources of order were
each pinned:
  * NO ANN index — ``query._vector_seeds`` is a deliberate full cosine scan.
  * fastembed is deterministic (CPU, no sampling): same text -> same 384-d vector.
  * ``LadybugStore.nodes_with_embeddings`` ends in ``ORDER BY n.id`` and Python's
    ``list.sort`` is stable, so the score-only sorts resolve cosine ties in a
    fixed input order. (A latent fragility — the two score-only sorts have no
    explicit ``id`` tie-break — is documented in the spike; it is currently safe
    because the storage iteration order is stable. Cannot be fixed here: that is a
    ``src/okto_neuron/query.py`` change, out of scope for the eval gate.)

WHY A FROZEN .lbug (not a serialized JSON + InMemoryStore)
----------------------------------------------------------
The faithful ``/recall`` ranking is ``LadybugStore``'s fused RRF (BM25 lexical leg
+ full-scan cosine + graph expansion). ``InMemoryStore.search_text`` is a totally
different token-overlap scorer, so a JSON->InMemoryStore probe would gate a
DIFFERENT algorithm — not the shipped retrieval. The frozen ``.lbug`` is small
(~5 MB) and proven stable across re-opens, so committing it is the only form that
keeps the gate faithful. The 14 source copies are committed alongside it because
extraction-completeness re-slices gold-quote bytes off disk.

THE GATE
--------
``recall_floor.py gate`` computes all metrics over the frozen vault and compares
them to a committed baseline (``recall_floor_baseline.json`` in the dataset dir):
  * exit 0 — every metric >= baseline (counts within ``--tolerance`` (default 0);
             MRR/precision rates within ``--rate-tolerance`` (default 0.0)).
  * exit 1 — REGRESSION: hard-recall hits, extraction present-count, MRR@k rate, or
             precision@k rate dropped below baseline (a real quality regression —
             the gate's whole point).
  * exit 2 — infra error (vault won't open, questions missing, ladybug extra
             absent). Distinguished from a quality regression so CI can tell a
             broken runner from a real drop.

The committed frozen vault is opened on a THROWAWAY COPY, never in place —
``Vault.open`` is read/write and dirties the ``.lbug``, so opening the committed
file directly would leave the working tree dirty after a gate run. The copy keeps
``git status`` clean (a deterministic-floor invariant).

This is a DETERMINISTIC floor (graph-membership + byte-hash + the shipped ranking),
NOT an LLM judgement — it stands on its own regardless of the LLM judge's pending
human-kappa clearance.

CONTRACT NOTE: unlike ``judge.py floor`` (which imports zero project source), this
module DOES import ``marginalia`` — it has to, to run the real retrieval code path.
That is the point. The provenance floor stays import-free; this recall/extraction
floor is the one place the gate touches live code, over a frozen graph.

USAGE
-----
  recall_floor.py gate --vault DIR --questions Q.yaml [--baseline B.json] \
                       [--k 10] [--tolerance 0] [--rate-tolerance 0.0] \
                       --out recall_floor.json
  recall_floor.py probe --vault DIR --questions Q.yaml [--k 10] --out report.json
  recall_floor.py selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from golden_yaml import GoldenYamlError, load_yaml, question_validation_errors


def _canonical_quote(q: str) -> str:
    """Return the PyYAML-parsed source text without decoding it a second time."""

    return q


def _basename_no_hash(path: str) -> str:
    """A source copy is '<orig-stem>-<8hex>.md'; recover '<orig>.md' so it matches
    a gold target's source_path (e.g. '00-charter.md')."""
    name = Path(path).name
    m = re.match(r"^(.*)-[0-9a-f]{8}(\.[A-Za-z0-9]+)$", name)
    if m:
        return m.group(1) + m.group(2)
    return name


def _load_questions(path: Path, requested_k: int | None = None) -> tuple[list[dict[str, Any]], int]:
    doc = load_yaml(path)
    errors = question_validation_errors(doc)
    if errors:
        raise ValueError("invalid questions: " + "; ".join(errors))
    configured_k = int((doc.get("settings") or {}).get("k", 10))
    if requested_k is not None and requested_k != configured_k:
        raise ValueError(f"--k={requested_k} differs from questions settings.k={configured_k}")
    return (
        sorted(doc["questions"], key=lambda q: str(q.get("id"))),
        configured_k,
    )


# ════════════════════════════════════════════════════════════════════════════
# frozen-vault hygiene — open WITHOUT mutating the committed .lbug
# ════════════════════════════════════════════════════════════════════════════
#
# ``Vault.open`` is read/WRITE: opening a LadybugStore touches/rewrites internal
# bookkeeping in the .lbug (WAL checkpoint, header timestamps), so opening the
# committed frozen vault IN PLACE dirties ``git status`` (verified: sha256 of
# graph.lbug changes after a single open, even though the logical graph is
# unchanged). That makes a CI gate that "must not touch the working tree" leave
# the tree dirty. The fix is to open a TEMP COPY of the whole vault dir and let
# the OS reclaim it — the committed bytes are never opened, so they never change.


def _ranking_metrics(
    hit_files: list[str], gold_files: set[str], k: int
) -> tuple[float, float, int | None]:
    """Pure ranking math for ONE answerable question — shared by ``run_probe`` and
    ``selftest`` so the gated formula is testable with no vault.

    ``hit_files`` is the top-k retrieved source basenames in rank order; a position
    is gold-relevant iff its file is in ``gold_files`` (the SAME relevance
    hard-recall@k uses). Returns ``(reciprocal_rank, precision_at_k, first_rank)``:
      * reciprocal_rank = 1/(1-based rank of first gold-relevant hit), 0 if none.
      * precision_at_k  = (#gold-relevant hits in top-k) / k  (fixed-k denominator).
      * first_rank      = that 1-based rank, or None."""
    first_rank: int | None = None
    relevant_in_topk = 0
    for pos, hf in enumerate(hit_files):
        if hf in gold_files:
            relevant_in_topk += 1
            if first_rank is None:
                first_rank = pos + 1
    rr = (1.0 / first_rank) if first_rank else 0.0
    prec = (relevant_in_topk / k) if k else 0.0
    return rr, prec, first_rank


def _copy_vault_to_temp(vault_dir: Path) -> Path:
    """Copy the entire frozen vault into a fresh temp dir and return the copy.

    Copies the whole tree (graph.lbug + .marginalia/sources/* + okto-neuron.yaml)
    because extraction-completeness re-slices gold bytes off ``.marginalia/sources``.
    Caller owns cleanup (use via ``_opened_vault_copy``)."""
    import shutil
    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix="recall_floor_vault_"))
    dest = tmp_root / vault_dir.name
    shutil.copytree(vault_dir, dest)
    return dest


# ════════════════════════════════════════════════════════════════════════════
# the probe — faithful, daemon-free reproduction of /recall + extraction
# ════════════════════════════════════════════════════════════════════════════


def run_probe(vault_dir: Path, questions: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """Compute HARD-RECALL@k + MRR@k + PRECISION@k + EXTRACTION-COMPLETENESS over
    the frozen vault.

    Imports marginalia lazily so ``selftest`` (which mocks this) and ``--help`` do
    not require the ladybug extra. Returns a LOCATION-INDEPENDENT, byte-stable
    report (no absolute vault path), so two runs from different checkout dirs
    produce identical bytes.

    The committed frozen vault is NEVER opened in place: ``Vault.open`` is
    read/write and dirties the .lbug, so we open a throwaway COPY and discard it.
    That keeps ``git status`` clean after a gate run (cleanup gap #6)."""
    import shutil

    from okto_neuron.vault import Vault  # lazy: needs the [ladybug] extra

    work_dir = _copy_vault_to_temp(vault_dir)
    # ``run_probe`` re-slices gold bytes off ``<vault>/.marginalia/sources`` — point
    # at the COPY's sources so nothing reads through to the committed tree.
    vault = Vault.open(str(work_dir))
    try:
        store = vault.store

        # EXTRACTION index: for every Claim, resolve its anchored source basename +
        # byte range from the provenance the daemon would expose. Built once,
        # retrieval-independent. claim_anchors[basename] -> [(bs, be, claim_id), …]
        claim_anchors: dict[str, list[tuple[int, int, str]]] = {}
        for node in store.list_nodes(type="Claim"):
            prov = vault._provenance_for_node(node)
            if not prov or not prov.path:
                continue
            base = _basename_no_hash(prov.path)
            claim_anchors.setdefault(base, []).append(
                (int(prov.byte_start), int(prov.byte_end), str(node.id))
            )
        for base in claim_anchors:
            claim_anchors[base].sort()

        sources_dir = work_dir / ".marginalia" / "sources"
        source_files = sorted(sources_dir.glob("*")) if sources_dir.is_dir() else []

        def quote_bytes(base: str, quote: str) -> tuple[int, int] | None:
            for f in source_files:
                if _basename_no_hash(f.name) == base:
                    raw = f.read_bytes()
                    qb = _canonical_quote(quote).encode("utf-8")
                    idx = raw.find(qb)
                    return (idx, idx + len(qb)) if idx >= 0 else None
            return None

        per_q: list[dict[str, Any]] = []
        recall_hit = recall_total = 0
        ext_present = ext_total = 0
        # Ranking metrics are averaged over ANSWERABLE questions (those carrying
        # gold_targets), mirroring the laptop floor_runner. Negative controls have
        # no gold rank, so they are excluded from MRR/precision denominators.
        mrr_sum = 0.0
        precision_sum = 0.0
        ranked_q_total = 0
        # GN-1: claim_coverage counts — distinct from extraction_completeness.
        # extraction_completeness: "any Claim on the Block" (file-level).
        # claim_coverage: "a Claim whose byte_span covers THIS quote" (quote-level).
        claim_present = claim_total = 0

        for q in questions:
            qid = str(q.get("id"))
            question = str(q.get("question") or "")
            negctl = bool(q.get("negative_control"))
            golds = q.get("gold_targets") or []

            hits = vault.query(question, k=k)  # the SAME path /recall uses
            hit_files = [
                _basename_no_hash(h.provenance.path) if (h.provenance and h.provenance.path) else ""
                for h in hits
            ]
            hit_file_set = set(hit_files)

            # RANKING METRICS (MRR@k + precision@k). A hit position is
            # "gold-relevant" iff its source file is one of THIS question's gold
            # source files — the SAME gold-target -> hit relevance hard-recall@k
            # uses (sp in hit_file_set), no second relevance definition.
            gold_files = {str(gt.get("source_path") or "") for gt in golds}
            gold_files.discard("")
            q_mrr = 0.0
            q_precision = 0.0
            q_first_relevant_rank = None  # 1-based rank of first gold-relevant hit
            if gold_files:  # answerable question
                q_mrr, q_precision, q_first_relevant_rank = _ranking_metrics(
                    hit_files, gold_files, k
                )
                mrr_sum += q_mrr
                precision_sum += q_precision
                ranked_q_total += 1

            gold_rows: list[dict[str, Any]] = []
            for gt in golds:
                sp = str(gt.get("source_path") or "")
                quote = str(gt.get("quote") or "")

                recall_total += 1
                r_ok = sp in hit_file_set
                recall_hit += int(r_ok)

                ext_total += 1
                qspan = quote_bytes(sp, quote)
                anchors = claim_anchors.get(sp, [])
                claim_on_file = len(anchors) > 0
                claim_on_quote = None
                if qspan is not None:
                    qs, qe = qspan
                    covering = sorted(cid for (bs, be, cid) in anchors if bs <= qs and be >= qe)
                    if not covering:  # 12k single-block files cover the whole file
                        covering = sorted(
                            cid for (bs, be, cid) in anchors if not (be <= qs or bs >= qe)
                        )
                    claim_on_quote = covering[0] if covering else None
                e_ok = claim_on_quote is not None or (qspan is None and claim_on_file)
                ext_present += int(e_ok)

                # GN-1: claim_coverage — stricter than extraction_completeness.
                # Only counts when a Claim's byte_span directly covers the gold
                # quote span (or overlaps it when the whole file is one block).
                # claim_on_file (any Claim on the file) >= e_ok >= claim_on_quote.
                claim_total += 1
                claim_present += int(claim_on_quote is not None)

                gold_rows.append(
                    {
                        "source_path": sp,
                        "recall_hit": r_ok,
                        "rank": (hit_files.index(sp) if sp in hit_files else None),
                        "extraction_present": e_ok,
                        "example_claim_id": claim_on_quote,
                        "n_claims_on_file": len(anchors),
                    }
                )

            per_q.append(
                {
                    "id": qid,
                    "category": q.get("category"),
                    "negative_control": negctl,
                    "k": k,
                    "n_hits": len(hits),
                    "hit_files_topk": hit_files,
                    "gold_targets": gold_rows,
                    "reciprocal_rank": round(q_mrr, 6),
                    "precision_at_k": round(q_precision, 6),
                    "first_relevant_rank": q_first_relevant_rank,
                }
            )
    finally:
        try:
            vault.close()
        finally:
            # Drop the throwaway vault copy + its temp root. The committed frozen
            # vault was never opened, so it stays byte-identical (cleanup gap #6).
            shutil.rmtree(work_dir.parent, ignore_errors=True)

    report = {
        "probe": "direct_library_frozen_vault",
        "deterministic": True,
        "daemon": False,
        "llm": False,
        "k": k,
        "questions": len(questions),
        "hard_recall_at_k": {
            "gold_targets_total": recall_total,
            "gold_targets_hit": recall_hit,
            "rate": round(recall_hit / recall_total, 4) if recall_total else None,
        },
        # MRR@k + PRECISION@k — ranking-quality floor (Requirement 1). Averaged
        # over ANSWERABLE questions (gold_targets present); negative controls have
        # no gold rank and are excluded from the denominator. ``mrr`` is the mean
        # 1/(rank of first gold-relevant hit), ``precision`` the mean of
        # (#gold-relevant hits in top-k)/k. Both are < 1.0 on a non-trivial corpus
        # with near-duplicate distractors competing for top-k slots, so they cannot
        # saturate the way a coarse block-membership recall can.
        "mrr_at_k": {
            "answerable_questions": ranked_q_total,
            "reciprocal_rank_sum": round(mrr_sum, 6),
            "rate": round(mrr_sum / ranked_q_total, 4) if ranked_q_total else None,
        },
        "precision_at_k": {
            "answerable_questions": ranked_q_total,
            "precision_sum": round(precision_sum, 6),
            "rate": round(precision_sum / ranked_q_total, 4) if ranked_q_total else None,
        },
        "extraction_completeness": {
            "gold_targets_total": ext_total,
            "gold_targets_present": ext_present,
            "rate": round(ext_present / ext_total, 4) if ext_total else None,
        },
        # GN-1: claim_coverage — does THIS specific answering Claim exist and
        # cover the gold quote span? Stricter than extraction_completeness (which
        # only checks "any Claim on the Block"). claim_present <= ext_present.
        "claim_coverage": {
            "claim_total": claim_total,
            "claim_present": claim_present,
            "rate": round(claim_present / claim_total, 4) if claim_total else None,
        },
        "per_question": per_q,
    }
    # A single sha256 over the GATED scalar payload (recall + MRR + precision +
    # extraction), so the bit-reproducibility proof covers every metric the gate
    # acts on — not just hard-recall. Computed last so it reflects the final dict.
    report["metric_payload_sha256"] = _metric_payload_sha256(report)
    return report


def _metric_payload_sha256(report: dict[str, Any]) -> str:
    """sha256 of the canonical GATED-metric scalars. Independent of per-question
    detail ordering and of the absolute checkout path, so two runs in separate
    processes over the same frozen vault hash identically. The rates are the
    rounded values the gate compares, so the hash moves iff a gated number moves."""
    import hashlib

    hr = report.get("hard_recall_at_k", {})
    mr = report.get("mrr_at_k", {})
    pr = report.get("precision_at_k", {})
    ex = report.get("extraction_completeness", {})
    cc = report.get("claim_coverage", {})
    payload = {
        "k": report.get("k"),
        "hard_recall_at_k": {
            "gold_targets_total": hr.get("gold_targets_total"),
            "gold_targets_hit": hr.get("gold_targets_hit"),
            "rate": hr.get("rate"),
        },
        "mrr_at_k": {
            "answerable_questions": mr.get("answerable_questions"),
            "reciprocal_rank_sum": mr.get("reciprocal_rank_sum"),
            "rate": mr.get("rate"),
        },
        "precision_at_k": {
            "answerable_questions": pr.get("answerable_questions"),
            "precision_sum": pr.get("precision_sum"),
            "rate": pr.get("rate"),
        },
        "extraction_completeness": {
            "gold_targets_total": ex.get("gold_targets_total"),
            "gold_targets_present": ex.get("gold_targets_present"),
            "rate": ex.get("rate"),
        },
        "claim_coverage": {
            "claim_total": cc.get("claim_total"),
            "claim_present": cc.get("claim_present"),
            "rate": cc.get("rate"),
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ════════════════════════════════════════════════════════════════════════════
# baseline regression check
# ════════════════════════════════════════════════════════════════════════════


def baseline_schema_errors(baseline: Any, *, expected_k: Any) -> list[str]:
    """Reject missing, mismatched, or self-inconsistent regression baselines."""

    if not isinstance(baseline, dict):
        return ["baseline must be an object"]
    errors: list[str] = []
    k = baseline.get("k")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        errors.append("baseline.k must be a positive integer")
    elif k != expected_k:
        errors.append(f"baseline.k={k} differs from current k={expected_k}")

    metric_fields = {
        "hard_recall_at_k": ("gold_targets_total", "gold_targets_hit", "rate"),
        "mrr_at_k": ("answerable_questions", "reciprocal_rank_sum", "rate"),
        "precision_at_k": ("answerable_questions", "precision_sum", "rate"),
        "extraction_completeness": (
            "gold_targets_total",
            "gold_targets_present",
            "rate",
        ),
        "claim_coverage": ("claim_total", "claim_present", "rate"),
    }
    for metric, fields in metric_fields.items():
        payload = baseline.get(metric)
        if not isinstance(payload, dict):
            errors.append(f"baseline.{metric} must be an object")
            continue
        for field in fields:
            value = payload.get(field)
            if field == "rate" or field.endswith("_sum"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"baseline.{metric}.{field} must be numeric")
                elif field == "rate" and not 0.0 <= float(value) <= 1.0:
                    errors.append(f"baseline.{metric}.rate must be within [0, 1]")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"baseline.{metric}.{field} must be a non-negative integer")

    count_pairs = (
        ("hard_recall_at_k", "gold_targets_hit", "gold_targets_total"),
        ("extraction_completeness", "gold_targets_present", "gold_targets_total"),
        ("claim_coverage", "claim_present", "claim_total"),
    )
    for metric, present_key, total_key in count_pairs:
        payload = baseline.get(metric)
        if not isinstance(payload, dict):
            continue
        present, total = payload.get(present_key), payload.get(total_key)
        if isinstance(present, int) and isinstance(total, int):
            if total <= 0:
                errors.append(f"baseline.{metric}.{total_key} must be greater than zero")
            elif present > total:
                errors.append(f"baseline.{metric}.{present_key} cannot exceed {total_key}")
    for metric in ("mrr_at_k", "precision_at_k"):
        payload = baseline.get(metric)
        if isinstance(payload, dict) and payload.get("answerable_questions") == 0:
            errors.append(f"baseline.{metric}.answerable_questions must be greater than zero")

    digest = baseline.get("metric_payload_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        errors.append("baseline.metric_payload_sha256 must be a lowercase sha256")
    elif not errors and digest != _metric_payload_sha256(baseline):
        errors.append("baseline.metric_payload_sha256 does not match its metrics")
    return errors


def compare_to_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    tolerance: int,
    rate_tolerance: float = 0.0,
) -> dict[str, Any]:
    """A regression is a drop below baseline beyond tolerance. Going UP is never a
    regression. A change in the gold-target TOTAL (dataset edit) is reported but
    does not gate; re-baseline deliberately in that case.

    TWO tolerance kinds, by metric shape:
      * COUNT metrics (hard-recall hits, extraction present) use the integer
        ``tolerance``: a clean integer count drop, no float-rounding ambiguity.
      * RATE metrics (MRR@k, precision@k) are ranking-quality means with no clean
        integer count, so they gate on the rounded rate with ``rate_tolerance``.
        The ranking is fully deterministic (no ANN randomness, stable sort) and the
        rates are rounded to 4 dp before comparison, so the DEFAULT ``rate_tolerance
        = 0.0`` gates EXACTLY — any real ranking regression that moves a gold hit to
        a worse rank lowers MRR/precision and trips the gate, while a bit-identical
        re-run reproduces the same rounded rate and passes. Tolerance is kept at 0.0
        on purpose: there is no float noise to absorb on a frozen, deterministic
        vault, so loosening it would only hide regressions."""
    schema_errors = baseline_schema_errors(baseline, expected_k=current.get("k"))
    if schema_errors:
        raise ValueError("invalid recall baseline: " + "; ".join(schema_errors))
    regressions: list[str] = []

    cr, br = current["hard_recall_at_k"], baseline.get("hard_recall_at_k", {})
    ce, be = current["extraction_completeness"], baseline.get("extraction_completeness", {})
    cm, bm = current.get("mrr_at_k", {}), baseline.get("mrr_at_k", {})
    cp, bp = current.get("precision_at_k", {}), baseline.get("precision_at_k", {})
    cc, bc = current.get("claim_coverage", {}), baseline.get("claim_coverage", {})

    cur_recall = int(cr.get("gold_targets_hit") or 0)
    base_recall = int(br.get("gold_targets_hit") or 0)
    if cur_recall < base_recall - tolerance:
        regressions.append(
            f"hard-recall@{current['k']} hits dropped: {base_recall} -> {cur_recall} "
            f"(tolerance {tolerance})"
        )

    cur_ext = int(ce.get("gold_targets_present") or 0)
    base_ext = int(be.get("gold_targets_present") or 0)
    if cur_ext < base_ext - tolerance:
        regressions.append(
            f"extraction-completeness present dropped: {base_ext} -> {cur_ext} "
            f"(tolerance {tolerance})"
        )

    cur_mrr = float(cm.get("rate") or 0.0)
    base_mrr = float(bm.get("rate") or 0.0)
    if cur_mrr < base_mrr - rate_tolerance:
        regressions.append(
            f"mrr@{current['k']} rate dropped: {base_mrr} -> {cur_mrr} "
            f"(rate_tolerance {rate_tolerance})"
        )

    cur_prec = float(cp.get("rate") or 0.0)
    base_prec = float(bp.get("rate") or 0.0)
    if cur_prec < base_prec - rate_tolerance:
        regressions.append(
            f"precision@{current['k']} rate dropped: {base_prec} -> {cur_prec} "
            f"(rate_tolerance {rate_tolerance})"
        )

    # GN-1: claim_coverage gate (zero tolerance — count regression).
    cur_cc = int(cc.get("claim_present") or 0)
    base_cc = int(bc.get("claim_present") or 0)
    if cur_cc < base_cc - tolerance:
        regressions.append(
            f"claim-coverage present dropped: {base_cc} -> {cur_cc} (tolerance {tolerance})"
        )

    total_changed: list[str] = []
    if int(cr.get("gold_targets_total") or 0) != int(br.get("gold_targets_total") or 0):
        total_changed.append(
            f"hard-recall total {br.get('gold_targets_total')} -> {cr.get('gold_targets_total')}"
        )
    if int(ce.get("gold_targets_total") or 0) != int(be.get("gold_targets_total") or 0):
        total_changed.append(
            f"extraction total {be.get('gold_targets_total')} -> {ce.get('gold_targets_total')}"
        )
    if int(cm.get("answerable_questions") or 0) != int(bm.get("answerable_questions") or 0):
        total_changed.append(
            f"mrr answerable {bm.get('answerable_questions')} -> {cm.get('answerable_questions')}"
        )

    return {
        "tolerance": tolerance,
        "rate_tolerance": rate_tolerance,
        "baseline_recall_hits": base_recall,
        "current_recall_hits": cur_recall,
        "baseline_extraction_present": base_ext,
        "current_extraction_present": cur_ext,
        "baseline_mrr": base_mrr,
        "current_mrr": cur_mrr,
        "baseline_precision": base_prec,
        "current_precision": cur_prec,
        "baseline_claim_present": base_cc,
        "current_claim_present": cur_cc,
        "total_changed": sorted(total_changed),
        "regressions": sorted(regressions),
    }


# ════════════════════════════════════════════════════════════════════════════
# byte-stable I/O
# ════════════════════════════════════════════════════════════════════════════


def _write_stable(report: dict[str, Any], out: Path) -> None:
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════════
# subcommands
# ════════════════════════════════════════════════════════════════════════════


def cmd_probe(args: argparse.Namespace) -> int:
    vault_dir = Path(args.vault).expanduser()
    questions, actual_k = _load_questions(Path(args.questions).expanduser(), args.k)
    try:
        report = run_probe(vault_dir, questions, actual_k)
    except ImportError as exc:  # ladybug extra missing
        print(json.dumps({"error": f"marginalia/ladybug import failed: {exc}"}), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — probe/storage failures are infrastructure
        print(json.dumps({"error": f"recall probe failed: {exc}"}), file=sys.stderr)
        return 2
    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "hard_recall_at_k": report["hard_recall_at_k"],
                "mrr_at_k": report["mrr_at_k"],
                "precision_at_k": report["precision_at_k"],
                "extraction_completeness": report["extraction_completeness"],
                "claim_coverage": report["claim_coverage"],
                "metric_payload_sha256": report["metric_payload_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """Run the probe over the frozen vault and gate vs a committed baseline.

    exit 0 = no regression; 1 = recall/extraction regression; 2 = infra error."""
    vault_dir = Path(args.vault).expanduser()
    if not (vault_dir / "graph.lbug").is_file():
        print(
            json.dumps({"error": f"no graph.lbug under frozen vault: {vault_dir}"}), file=sys.stderr
        )
        return 2
    qpath = Path(args.questions).expanduser()
    if not qpath.is_file():
        print(json.dumps({"error": f"no questions.yaml: {qpath}"}), file=sys.stderr)
        return 2
    questions, actual_k = _load_questions(qpath, args.k)
    try:
        report = run_probe(vault_dir, questions, actual_k)
    except ImportError as exc:
        print(json.dumps({"error": f"marginalia/ladybug import failed: {exc}"}), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — probe/storage failures are infrastructure
        print(json.dumps({"error": f"recall probe failed: {exc}"}), file=sys.stderr)
        return 2

    comparison: dict[str, Any] | None = None
    if args.baseline:
        bpath = Path(args.baseline).expanduser()
        if not bpath.exists():
            print(json.dumps({"error": f"baseline not found: {bpath}"}), file=sys.stderr)
            return 2
        try:
            baseline = json.loads(bpath.read_text(encoding="utf-8"))
            comparison = compare_to_baseline(report, baseline, args.tolerance, args.rate_tolerance)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            print(json.dumps({"error": f"invalid recall baseline: {exc}"}), file=sys.stderr)
            return 2
        report["baseline_comparison"] = comparison

    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "hard_recall_at_k": report["hard_recall_at_k"],
                "mrr_at_k": report["mrr_at_k"],
                "precision_at_k": report["precision_at_k"],
                "extraction_completeness": report["extraction_completeness"],
                "claim_coverage": report["claim_coverage"],
                "metric_payload_sha256": report["metric_payload_sha256"],
                "regressions": (comparison or {}).get("regressions") if comparison else None,
            },
            indent=2,
            sort_keys=True,
        )
    )

    if comparison and comparison.get("regressions"):
        return 1
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Validate the deterministic logic (no vault, no network): quote
    canonicalization, basename recovery, byte-stable output, and the
    baseline-regression verdict (the only logic that gates)."""
    import tempfile

    print("SELFTEST — recall_floor (no vault, no network)")
    ok = True

    # basename recovery
    b_ok = (
        _basename_no_hash("/x/.marginalia/sources/00-charter-903aa8f3.md") == "00-charter.md"
        and _basename_no_hash("plain.md") == "plain.md"
        and _basename_no_hash("a-b-c-deadbeef.md") == "a-b-c.md"
    )
    print(f"  {'ok  ' if b_ok else 'FAIL'} basename-no-hash recovery")
    ok &= b_ok

    # quote canonicalization preserves already-parsed source text verbatim.
    q_ok = _canonical_quote("a\\nb") == "a\\nb" and _canonical_quote("plain") == "plain"
    print(f"  {'ok  ' if q_ok else 'FAIL'} quote canonicalization")
    ok &= q_ok

    # ── ranking-metric formulas (MRR@k + precision@k), pure, no vault ──────────
    # first gold hit at rank 2 (1-based) -> rr=0.5; 1 relevant in top-10 -> 0.1
    rr1, pr1, fr1 = _ranking_metrics(["x.md", "gold.md", "y.md"], {"gold.md"}, 10)
    m_ok = rr1 == 0.5 and abs(pr1 - 0.1) < 1e-12 and fr1 == 2
    # two relevant in top-5, first at rank 1 -> rr=1.0, precision=2/5=0.4
    rr2, pr2, fr2 = _ranking_metrics(["g.md", "x.md", "g.md", "y.md", "z.md"], {"g.md"}, 5)
    m_ok &= rr2 == 1.0 and abs(pr2 - 0.4) < 1e-12 and fr2 == 1
    # no gold hit in top-k -> rr=0, precision=0, no rank
    rr3, pr3, fr3 = _ranking_metrics(["a.md", "b.md"], {"gold.md"}, 10)
    m_ok &= rr3 == 0.0 and pr3 == 0.0 and fr3 is None
    print(f"  {'ok  ' if m_ok else 'FAIL'} MRR@k + precision@k formula")
    ok &= m_ok

    base = {
        "k": 10,
        "hard_recall_at_k": {"gold_targets_total": 32, "gold_targets_hit": 15, "rate": 0.4688},
        "mrr_at_k": {"answerable_questions": 21, "reciprocal_rank_sum": 10.5, "rate": 0.5},
        "precision_at_k": {"answerable_questions": 21, "precision_sum": 4.2, "rate": 0.2},
        "extraction_completeness": {
            "gold_targets_total": 32,
            "gold_targets_present": 18,
            "rate": 0.5625,
        },
        "claim_coverage": {"claim_total": 32, "claim_present": 10, "rate": 0.3125},
    }

    # the gated-metric payload hash is present + bounded (rates in [0,1), < 1.0)
    h1 = _metric_payload_sha256(base)
    h2 = _metric_payload_sha256(json.loads(json.dumps(base)))
    base["metric_payload_sha256"] = h1
    bounds_ok = (
        isinstance(h1, str)
        and len(h1) == 64
        and h1 == h2
        and 0.0 <= base["mrr_at_k"]["rate"] < 1.0
        and 0.0 <= base["precision_at_k"]["rate"] < 1.0
    )
    print(f"  {'ok  ' if bounds_ok else 'FAIL'} metric_payload_sha256 stable + rates in [0,1)")
    ok &= bounds_ok

    # the hash MOVES when a gated metric moves (so a regression cannot hide)
    moved = json.loads(json.dumps(base))
    moved["mrr_at_k"]["rate"] = 0.4
    sens_ok = _metric_payload_sha256(moved) != h1
    print(f"  {'ok  ' if sens_ok else 'FAIL'} payload hash is sensitive to a metric change")
    ok &= sens_ok

    # equal -> no regression
    same = compare_to_baseline(base, base, tolerance=0)
    s_ok = same["regressions"] == []
    print(f"  {'ok  ' if s_ok else 'FAIL'} equal counts -> no regression")
    ok &= s_ok

    # recall drop -> regression
    worse = json.loads(json.dumps(base))
    worse["hard_recall_at_k"]["gold_targets_hit"] = 13
    r = compare_to_baseline(worse, base, tolerance=0)
    r_ok = len(r["regressions"]) == 1 and "hard-recall" in r["regressions"][0]
    print(f"  {'ok  ' if r_ok else 'FAIL'} recall hit drop -> 1 regression")
    ok &= r_ok

    # extraction drop -> regression
    worse2 = json.loads(json.dumps(base))
    worse2["extraction_completeness"]["gold_targets_present"] = 16
    r2 = compare_to_baseline(worse2, base, tolerance=0)
    r2_ok = len(r2["regressions"]) == 1 and "extraction" in r2["regressions"][0]
    print(f"  {'ok  ' if r2_ok else 'FAIL'} extraction present drop -> 1 regression")
    ok &= r2_ok

    # MRR rate drop -> regression (rate_tolerance defaults to 0.0)
    worse3 = json.loads(json.dumps(base))
    worse3["mrr_at_k"]["rate"] = 0.4938
    r3 = compare_to_baseline(worse3, base, tolerance=0)
    r3_ok = len(r3["regressions"]) == 1 and "mrr@" in r3["regressions"][0]
    print(f"  {'ok  ' if r3_ok else 'FAIL'} MRR@k rate drop -> 1 regression")
    ok &= r3_ok

    # precision rate drop -> regression
    worse4 = json.loads(json.dumps(base))
    worse4["precision_at_k"]["rate"] = 0.19
    r4 = compare_to_baseline(worse4, base, tolerance=0)
    r4_ok = len(r4["regressions"]) == 1 and "precision@" in r4["regressions"][0]
    print(f"  {'ok  ' if r4_ok else 'FAIL'} precision@k rate drop -> 1 regression")
    ok &= r4_ok

    # improvement -> NOT a regression (all four metrics up)
    better = json.loads(json.dumps(base))
    better["hard_recall_at_k"]["gold_targets_hit"] = 20
    better["extraction_completeness"]["gold_targets_present"] = 25
    better["mrr_at_k"]["rate"] = 0.6
    better["precision_at_k"]["rate"] = 0.3
    rb = compare_to_baseline(better, base, tolerance=0)
    b2_ok = rb["regressions"] == []
    print(f"  {'ok  ' if b2_ok else 'FAIL'} improvement is not a regression")
    ok &= b2_ok

    # tolerance absorbs a small dip
    dip = json.loads(json.dumps(base))
    dip["hard_recall_at_k"]["gold_targets_hit"] = 14
    rt = compare_to_baseline(dip, base, tolerance=1)
    t_ok = rt["regressions"] == []
    print(f"  {'ok  ' if t_ok else 'FAIL'} 1-target dip within tolerance=1 -> pass")
    ok &= t_ok

    # ── GN-1: claim_coverage gate fixtures ──────────────────────────────────
    # claim_coverage drop -> regression
    cc_worse = json.loads(json.dumps(base))
    cc_worse["claim_coverage"]["claim_present"] = 8
    cc_worse["claim_coverage"]["rate"] = round(8 / 32, 4)
    r_cc = compare_to_baseline(cc_worse, base, tolerance=0)
    r_cc_ok = len(r_cc["regressions"]) == 1 and "claim-coverage" in r_cc["regressions"][0]
    print(
        f"  {'ok  ' if r_cc_ok else 'FAIL'} GN-1: claim_coverage drop -> 1 regression (gate trips)"
    )
    ok &= r_cc_ok

    # mangled byte span: claim_present goes to 0 (below baseline 10) -> must trip gate
    cc_mangled = json.loads(json.dumps(base))
    cc_mangled["claim_coverage"]["claim_present"] = 0
    cc_mangled["claim_coverage"]["rate"] = 0.0
    r_cc_m = compare_to_baseline(cc_mangled, base, tolerance=0)
    r_cc_m_ok = any("claim-coverage" in reg for reg in r_cc_m["regressions"])
    print(
        f"  {'ok  ' if r_cc_m_ok else 'FAIL'} GN-1: mangled-span fixture (0 present) trips gate (exit 1)"
    )
    ok &= r_cc_m_ok

    # claim_coverage improvement -> NOT a regression
    cc_better = json.loads(json.dumps(base))
    cc_better["claim_coverage"]["claim_present"] = 15
    cc_better["claim_coverage"]["rate"] = round(15 / 32, 4)
    r_cc_b = compare_to_baseline(cc_better, base, tolerance=0)
    r_cc_b_ok = not any("claim-coverage" in reg for reg in r_cc_b["regressions"])
    print(f"  {'ok  ' if r_cc_b_ok else 'FAIL'} GN-1: claim_coverage improvement -> no regression")
    ok &= r_cc_b_ok

    # invariant: claim_present <= extraction_present (per-q: claim_on_quote is
    # a stricter subset of e_ok; cannot have more covered quotes than present ones)
    inv_ok = (
        base["claim_coverage"]["claim_present"]
        <= base["extraction_completeness"]["gold_targets_present"]
    )
    print(
        f"  {'ok  ' if inv_ok else 'FAIL'} GN-1: claim_present <= extraction_present invariant (fixture)"
    )
    ok &= inv_ok

    # ── payload sha is sensitive to GN-1 claim_coverage metric ─────────────
    h_base = _metric_payload_sha256(base)
    cc_shift = json.loads(json.dumps(base))
    cc_shift["claim_coverage"]["claim_present"] = 12
    cc_shift["claim_coverage"]["rate"] = round(12 / 32, 4)
    sha_cc_ok = _metric_payload_sha256(cc_shift) != h_base
    print(f"  {'ok  ' if sha_cc_ok else 'FAIL'} GN-1: sha256 moves when claim_present changes")
    ok &= sha_cc_ok

    # byte-stable writes
    with tempfile.TemporaryDirectory() as td:
        o1, o2 = Path(td) / "a.json", Path(td) / "b.json"
        _write_stable(base, o1)
        _write_stable(base, o2)
        w_ok = o1.read_bytes() == o2.read_bytes()
    print(f"  {'ok  ' if w_ok else 'FAIL'} report is byte-stable across writes")
    ok &= w_ok

    print("-" * 60)
    print("SELFTEST: PASS" if ok else "SELFTEST: FAIL")
    return 0 if ok else 1


# ════════════════════════════════════════════════════════════════════════════
# argparse
# ════════════════════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(
        description="Deterministic HARD-RECALL@k + MRR@k + PRECISION@k + "
        "EXTRACTION-COMPLETENESS floor over a FROZEN okto-neuron vault "
        "(no daemon, no LLM). The CI companion to judge.py's provenance "
        "floor."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", help="emit the recall+extraction report (no gating)")
    pp.add_argument("--vault", required=True, help="frozen vault dir (contains graph.lbug)")
    pp.add_argument("--questions", required=True, help="questions.yaml")
    pp.add_argument(
        "--k",
        type=int,
        default=None,
        help="must match questions.settings.k (defaults to that value, or 10)",
    )
    pp.add_argument("--out", required=True)
    pp.set_defaults(func=cmd_probe)

    pg = sub.add_parser("gate", help="probe + regression-gate vs a committed baseline (exit 0/1/2)")
    pg.add_argument("--vault", required=True, help="frozen vault dir (contains graph.lbug)")
    pg.add_argument("--questions", required=True, help="questions.yaml")
    pg.add_argument(
        "--baseline",
        default=None,
        help="committed recall_floor_baseline.json; regression => exit 1",
    )
    pg.add_argument(
        "--k",
        type=int,
        default=None,
        help="must match questions.settings.k (defaults to that value, or 10)",
    )
    pg.add_argument(
        "--tolerance",
        type=int,
        default=0,
        help="COUNT targets (recall/extraction) may drop before gating (default 0)",
    )
    pg.add_argument(
        "--rate-tolerance",
        dest="rate_tolerance",
        type=float,
        default=0.0,
        help="RATE drop (MRR@k/precision@k) allowed before gating; the "
        "ranking is deterministic so the default 0.0 gates exactly "
        "(default 0.0)",
    )
    pg.add_argument("--out", required=True)
    pg.set_defaults(func=cmd_gate)

    pt = sub.add_parser("selftest", help="validate the deterministic logic (no vault/network)")
    pt.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(json.dumps({"error": "invalid_golden_input", "detail": str(exc)}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": "golden_io_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
