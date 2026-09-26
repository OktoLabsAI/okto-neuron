#!/usr/bin/env python3
"""FLOOR METRICS (laptop-only) — the two correctness-floor signals from
requirement (1) that the CI provenance gate (judge.py ``floor``) cannot cover
because they need the LIVE, LLM-ingested vault:

  (i)  CITATION byte-verification
       For every citation an /ask answer returns, re-slice the cited Block's
       bytes out of the vault source on disk and assert
       sha256(raw[byte_start:byte_end]) == content_hash. This proves an answer's
       citations point at REAL, unmodified source bytes — the answer is anchored,
       not hallucinated provenance. Pure function of the captured responses + the
       on-disk vault sources; deterministic and bit-reproducible.

  (ii) EXTRACTION-completeness
       For every bound gold target (questions.bound.yaml), assert the live graph
       actually CONTAINS the fact: a Claim node anchored to the gold target's
       Block. "Does the graph know this?" — independent of whether retrieval
       happens to surface it (so it isolates extraction recall from query recall).

WHY LAPTOP-ONLY (not the CI floor):
  The CI floor (judge.py ``floor``) is a pure function of the committed dataset
  files — no vault, no daemon, no LLM — so it is the bit-reproducible CI anchor.
  These two metrics need the real LLM-ingested graph: citations only exist after
  an /ask against a populated vault, and extraction-completeness asks what Claims
  the extractor actually wrote. Both are measured against the live reference-eval vault
  and are reported SEPARATELY from the CI gate, never folded into it.

THE JUDGE REMAINS NON-AUTHORITATIVE. These are DETERMINISTIC floor checks
(byte-hashing + graph-membership), not LLM judgements — they stand on their own
regardless of the pending human-kappa clearance for the LLM judge/panel.

CONTRACT: black-box. Drives Okto Neuron over its public HTTP surface exactly like
any other client and re-slices source bytes off disk. Imports ZERO project
source. PyYAML is the authoritative parser for bound evidence files.

SUBCOMMANDS / WIRING
--------------------
  floor_metrics.py citations    --responses R.jsonl [--vault-root DIR] --out cit.json
  floor_metrics.py extraction   --bound questions.bound.yaml [--endpoint URL] --out ext.json
  floor_metrics.py floor-laptop --responses R.jsonl --bound B.yaml ... --out floor_laptop.json
  floor_metrics.py selftest

Also wired into judge.py as ``judge.py floor-laptop ...`` (mirrors ``floor`` /
``manifest`` / ``scorecard``), so the one resumable orchestrator emits it next to
the CI floor report.

RESPONSES FILE
--------------
A JSONL of per-question records as produced by run-golden.sh / the calib runner.
Each record needs an ``id`` and an ``ask`` object. ``ask.citations`` is a list of
node-id strings; ``ask.hits`` is a list of {node, score, provenance, ...}. The
citation node-ids are matched back to their hit's provenance, and THAT block's
bytes are the ones re-sliced. (A record with no ``ask.hits`` cannot be byte-
verified — recorded as ``unverifiable`` with the reason, never silently passed.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from golden_yaml import GoldenYamlError, load_yaml, load_yaml_text

# ── responses.jsonl ────────────────────────────────────────────────────────────


def read_responses(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


# ════════════════════════════════════════════════════════════════════════════
# (i) CITATION byte-verification
# ════════════════════════════════════════════════════════════════════════════
#
# For each /ask response: every citation is a node-id. Match it to the hit whose
# node.id equals it (the hit carries the provenance), then re-slice
# raw[byte_start:byte_end] from the source on disk and assert
# sha256 == content_hash. A citation with no matching hit, or a hit with no byte
# range, is UNVERIFIABLE (recorded with a reason) — never counted as a pass.


def _resolve_source(path: str, vault_root: Path | None) -> Path | None:
    """Resolve a provenance ``path`` to an on-disk file.

    Provenance paths from the live daemon are absolute vault-source paths
    (…/.marginalia/sources/<copy>.md). Prefer the path as-is; if a --vault-root
    override is given (e.g. the source dir moved), remap by basename under it."""
    if not path:
        return None
    p = Path(path)
    if vault_root is not None:
        cand = vault_root / p.name
        if cand.exists():
            return cand
        matches = list(vault_root.rglob(p.name))
        if matches:
            return matches[0]
    if p.is_absolute() and p.exists():
        return p
    return None


def verify_citation_bytes(
    responses: list[dict[str, Any]], vault_root: Path | None
) -> dict[str, Any]:
    """Re-slice every /ask citation's block bytes and check sha256==content_hash.

    Returns a byte-stable report: per-question citation verdicts + a roll-up. The
    gate (``citation_floor_pass``) fails when no citations were captured or when
    any VERIFIABLE citation mismatches. Unverifiable citations (no hit / no byte
    range) are surfaced but do not by themselves flip the gate — they are a
    capture-shape warning, not a provenance corruption."""
    per_q: list[dict[str, Any]] = []
    n_cit = n_verified = n_pass = n_unverifiable = 0
    hard_failures: list[str] = []

    for rec in sorted(responses, key=lambda r: str(r.get("id"))):
        qid = str(rec.get("id"))
        ask = rec.get("ask") or {}
        cites = ask.get("citations") or []
        hits = ask.get("hits") or []
        # index hits by node id for citation -> provenance lookup
        hit_by_id: dict[str, dict[str, Any]] = {}
        for h in hits:
            nid = str((h.get("node") or {}).get("id") or "")
            if nid:
                hit_by_id.setdefault(nid, h)

        cit_results: list[dict[str, Any]] = []
        for c in cites:
            cid = c if isinstance(c, str) else str((c or {}).get("id") or "")
            n_cit += 1
            h = hit_by_id.get(cid)
            if h is None:
                n_unverifiable += 1
                cit_results.append(
                    {
                        "citation": cid,
                        "verifiable": False,
                        "reason": "no matching hit carries provenance for this citation id",
                    }
                )
                continue
            prov = h.get("provenance") or {}
            path = str(prov.get("path") or "")
            bs, be = prov.get("byte_start"), prov.get("byte_end")
            ch = str(prov.get("content_hash") or "")
            if bs is None or be is None or not ch:
                n_unverifiable += 1
                cit_results.append(
                    {
                        "citation": cid,
                        "verifiable": False,
                        "reason": "hit provenance has no byte range / content_hash",
                    }
                )
                continue
            try:
                bs_i, be_i = int(bs), int(be)
            except (TypeError, ValueError):
                n_unverifiable += 1
                cit_results.append(
                    {
                        "citation": cid,
                        "verifiable": False,
                        "reason": f"non-int byte range [{bs!r}:{be!r}]",
                    }
                )
                continue
            src = _resolve_source(path, vault_root)
            if src is None:
                n_unverifiable += 1
                cit_results.append(
                    {
                        "citation": cid,
                        "verifiable": False,
                        "reason": f"source not found on disk: {path!r}",
                    }
                )
                continue
            raw = src.read_bytes()[bs_i:be_i]
            actual = hashlib.sha256(raw).hexdigest()
            expected = ch.split(":", 1)[-1].lower()
            ok = actual == expected
            n_verified += 1
            n_pass += int(ok)
            row = {
                "citation": cid,
                "verifiable": True,
                "ok": ok,
                "source": src.name,
                "byte_start": bs_i,
                "byte_end": be_i,
            }
            if not ok:
                row["expected"] = expected[:16]
                row["actual"] = actual[:16]
                hard_failures.append(
                    f"{qid} citation {cid[:12]}: {src.name}[{bs_i}:{be_i}] "
                    f"sha {actual[:12]} != {expected[:12]}"
                )
            cit_results.append(row)

        per_q.append(
            {
                "id": qid,
                "n_citations": len(cites),
                "n_verifiable": sum(1 for r in cit_results if r["verifiable"]),
                "n_pass": sum(1 for r in cit_results if r.get("ok")),
                "citations": cit_results,
            }
        )

    if n_cit == 0:
        hard_failures.append("no citations captured; citation floor is unmeasured")

    return {
        "metric": "citation_byte_verification",
        "laptop_only": True,
        "questions": len(per_q),
        "citations_total": n_cit,
        "citations_verifiable": n_verified,
        "citations_pass": n_pass,
        "citations_unverifiable": n_unverifiable,
        "verifiable_pass_rate": round(n_pass / n_verified, 4) if n_verified else None,
        "citation_floor_pass": not hard_failures,
        "hard_failures": sorted(hard_failures),
        "per_question": per_q,
    }


# ════════════════════════════════════════════════════════════════════════════
# (ii) EXTRACTION-completeness
# ════════════════════════════════════════════════════════════════════════════
#
# For each bound gold target: does the live graph contain a Claim anchored to the
# gold Block? The bound file carries node_ids[] AND the block_id. Node ids drift
# as the vault is re-curated (reconcile / dedup / predicate-upkeep re-key them),
# so the check is at the STABLE block level: resolve the declared node_ids against
# /api/v1/nodes/{id}; if ANY resolves to a live Claim whose provenance.block_id ==
# the gold block_id, the fact's block was extracted into a Claim -> present.
#
# This is retrieval-INDEPENDENT (it never calls /recall or /ask), so it measures
# extraction recall alone, not query recall. Negative-control questions have no
# gold_targets and are excluded.


_NODE_CACHE: dict[tuple[str, str, str], dict[str, Any] | None] = {}


def _get_node(endpoint: str, nid: str, *, timeout: float = 10.0) -> dict[str, Any] | None:
    """GET /api/v1/nodes/{id} with a tiny per-run cache (gold node-id lists
    overlap across questions sharing a source block). None only on 404; transport
    and authorization failures are infrastructure errors and are never cached."""
    base = endpoint.rstrip("/")
    vault_path = os.environ.get("OKTO_NEURON_VAULT_PATH", "").strip()
    cache_key = (base, vault_path, nid)
    if cache_key in _NODE_CACHE:
        return _NODE_CACHE[cache_key]
    url = base + f"/api/v1/nodes/{nid}"
    headers: dict[str, str] = {}
    token = os.environ.get("OKTO_NEURON_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if vault_path:
        headers["X-Okto-Neuron-Vault"] = vault_path
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"node probe failed for {nid}: HTTP {exc.code}") from exc
        data = None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"node probe failed for {nid}: {exc}") from exc
    _NODE_CACHE[cache_key] = data
    return data


def measure_extraction_completeness(
    bound: dict[str, Any], endpoint: str, *, max_probe: int = 12
) -> dict[str, Any]:
    """For each bound gold target, confirm a live Claim is anchored to its Block.

    ``max_probe`` caps how many of a target's declared node_ids are probed before
    giving up (the lists can be long; a Claim on the gold block is found in the
    first few in practice). A target whose block is found -> present. Returns a
    byte-stable report + roll-up grouped by category."""
    questions = sorted((bound.get("questions") or []), key=lambda q: str(q.get("id")))
    per_q: list[dict[str, Any]] = []
    gt_total = gt_present = 0
    cat_total: dict[str, int] = {}
    cat_present: dict[str, int] = {}
    missing: list[str] = []

    for q in questions:
        qid = str(q.get("id"))
        category = str(q.get("category") or "")
        if q.get("negative_control"):
            # Negative controls assert ABSENCE; they carry no gold_targets and are
            # not part of extraction-completeness. Recorded for transparency.
            per_q.append(
                {
                    "id": qid,
                    "category": category,
                    "negative_control": True,
                    "gold_targets": [],
                }
            )
            continue
        targets = q.get("gold_targets") or []
        gt_rows: list[dict[str, Any]] = []
        for gt in targets:
            gt_total += 1
            cat_total[category] = cat_total.get(category, 0) + 1
            gold_block = str(gt.get("block_id") or "")
            # Probe in a DETERMINISTIC order (sorted node_ids) so the report is
            # byte-stable against a static vault regardless of the declared order,
            # and collect ALL claims that land on the gold block — the witness is
            # then the lexicographically smallest matching claim id (a stable
            # tie-break when a block carries several claims), not "first probed".
            node_ids = sorted(str(n) for n in (gt.get("node_ids") or []))
            probed = 0
            resolved_claims = 0
            on_block_ids: list[str] = []
            for nid in node_ids[:max_probe]:
                nd = _get_node(endpoint, nid)
                probed += 1
                if not nd or nd.get("status") != "ok":
                    continue
                node = nd.get("node") or {}
                if str(node.get("type")) != "Claim":
                    continue
                resolved_claims += 1
                blk = str((nd.get("provenance") or {}).get("block_id") or "")
                if gold_block and blk == gold_block:
                    on_block_ids.append(str(node.get("id")))
            claim_on_block = min(on_block_ids) if on_block_ids else None
            present = claim_on_block is not None
            gt_present += int(present)
            if present:
                cat_present[category] = cat_present.get(category, 0) + 1
            else:
                missing.append(
                    f"{qid} [{category}] block {gold_block[:12]}: "
                    f"no live Claim anchored (probed {probed}/{len(node_ids)} node_ids, "
                    f"{resolved_claims} resolved as claims)"
                )
            gt_rows.append(
                {
                    "source_path": gt.get("source_path"),
                    "block_id": gold_block,
                    "declared_node_ids": len(node_ids),
                    "probed": probed,
                    "resolved_claims": resolved_claims,
                    "claim_present_on_block": present,
                    "example_claim_id": claim_on_block,
                }
            )
        per_q.append(
            {
                "id": qid,
                "category": category,
                "negative_control": False,
                "gold_targets": gt_rows,
            }
        )

    by_category = {
        cat: {
            "gold_targets": cat_total[cat],
            "present": cat_present.get(cat, 0),
            "rate": round(cat_present.get(cat, 0) / cat_total[cat], 4) if cat_total[cat] else None,
        }
        for cat in sorted(cat_total)
    }
    return {
        "metric": "extraction_completeness",
        "laptop_only": True,
        "endpoint": endpoint,
        "method": "block-level: a declared gold node_id resolves to a live Claim "
        "whose provenance.block_id == the gold block_id (node ids drift "
        "under re-curation; the block anchor is stable)",
        "questions_with_gold": sum(1 for q in per_q if q["gold_targets"]),
        "gold_targets_total": gt_total,
        "gold_targets_present": gt_present,
        "extraction_recall": round(gt_present / gt_total, 4) if gt_total else None,
        "by_category": by_category,
        "missing": sorted(missing),
        "per_question": per_q,
    }


# ════════════════════════════════════════════════════════════════════════════
# subcommands
# ════════════════════════════════════════════════════════════════════════════


def _write_stable(report: dict[str, Any], out: Path) -> None:
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_citations(args: argparse.Namespace) -> int:
    responses = read_responses(Path(args.responses).expanduser())
    vault_root = Path(args.vault_root).expanduser() if args.vault_root else None
    report = verify_citation_bytes(responses, vault_root)
    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "metric": report["metric"],
                "citations_total": report["citations_total"],
                "citations_verifiable": report["citations_verifiable"],
                "citations_pass": report["citations_pass"],
                "citations_unverifiable": report["citations_unverifiable"],
                "verifiable_pass_rate": report["verifiable_pass_rate"],
                "citation_floor_pass": report["citation_floor_pass"],
            },
            indent=2,
        )
    )
    return 0 if report["citation_floor_pass"] else 1


def cmd_extraction(args: argparse.Namespace) -> int:
    bound = load_yaml(Path(args.bound).expanduser())
    report = measure_extraction_completeness(bound, args.endpoint, max_probe=args.max_probe)
    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "metric": report["metric"],
                "gold_targets_total": report["gold_targets_total"],
                "gold_targets_present": report["gold_targets_present"],
                "extraction_recall": report["extraction_recall"],
                "by_category": report["by_category"],
            },
            indent=2,
        )
    )
    # extraction-completeness is a measured FLOOR, not a hard gate (a thin graph is
    # a finding, not an infra error) — always exit 0; the number lives in the report.
    return 0


def cmd_floor_laptop(args: argparse.Namespace) -> int:
    """Both laptop floor metrics in one pass, emitted next to the CI floor report.

    Clearly separated from the deterministic CI provenance gate: this is
    LAPTOP-ONLY (needs the live LLM-ingested vault) and is a measured floor, not
    the CI trust anchor."""
    responses = read_responses(Path(args.responses).expanduser())
    vault_root = Path(args.vault_root).expanduser() if args.vault_root else None
    citations = verify_citation_bytes(responses, vault_root)

    extraction: dict[str, Any] | None = None
    if args.bound:
        bound = load_yaml(Path(args.bound).expanduser())
        extraction = measure_extraction_completeness(bound, args.endpoint, max_probe=args.max_probe)

    report = {
        "report": "floor_laptop",
        "laptop_only": True,
        "separated_from_ci_gate": True,
        "note": "LAPTOP-ONLY correctness-floor metrics (live LLM-ingested vault). "
        "The CI provenance gate is judge.py `floor` (no vault/daemon/LLM); "
        "these two metrics are reported separately and never fold into it. "
        "Deterministic byte-hash + graph-membership checks — independent of "
        "the LLM judge, which remains non-authoritative pending human kappa.",
        "endpoint": args.endpoint,
        "citation_byte_verification": citations,
        "extraction_completeness": extraction,
    }
    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "report": "floor_laptop",
                "citation_floor_pass": citations["citation_floor_pass"],
                "citations_pass": f"{citations['citations_pass']}/{citations['citations_verifiable']}",
                "extraction_recall": (extraction or {}).get("extraction_recall"),
                "gold_targets_present": (
                    f"{(extraction or {}).get('gold_targets_present')}/"
                    f"{(extraction or {}).get('gold_targets_total')}"
                    if extraction
                    else None
                ),
            },
            indent=2,
        )
    )
    # Citation evidence is a hard quality floor: corruption and an unmeasured
    # zero-citation run are both nonzero and cannot be resumed as complete.
    return 0 if citations["citation_floor_pass"] else 1


def floor_laptop_report_errors(report: Any, *, expected_response_ids: list[str]) -> list[str]:
    if not isinstance(report, dict):
        return ["floor-laptop report must be an object"]
    errors: list[str] = []
    if report.get("report") != "floor_laptop":
        errors.append("report type is not floor_laptop")
    citations = report.get("citation_byte_verification")
    if not isinstance(citations, dict):
        return [*errors, "citation_byte_verification is missing"]
    if citations.get("citation_floor_pass") is not True:
        errors.append("citation floor did not pass")
    rows = citations.get("per_question")
    if not isinstance(rows, list):
        errors.append("citation per_question must be a list")
    else:
        row_ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
        if row_ids != sorted(expected_response_ids):
            errors.append("citation question ids/order do not match responses")
    return errors


def cmd_validate_floor_laptop(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    responses = read_responses(Path(args.responses).expanduser())
    errors = floor_laptop_report_errors(
        report, expected_response_ids=[str(rec.get("id") or "") for rec in responses]
    )
    if errors:
        print(json.dumps({"valid": False, "errors": errors}), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "questions": len(responses)}))
    return 0


# ════════════════════════════════════════════════════════════════════════════
# selftest (no network) — pins the two metric computations on synthetic fixtures
# ════════════════════════════════════════════════════════════════════════════


def cmd_selftest(_args: argparse.Namespace) -> int:
    import tempfile

    print("SELFTEST — floor_metrics on synthetic fixtures (no network)")
    ok = True

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # ── citation byte-verification ──
        src = root / "doc.md"
        body = b"alpha beta gamma delta epsilon"
        src.write_bytes(body)
        good_hash = hashlib.sha256(body[6:14]).hexdigest()  # "beta gam"
        responses = [
            {  # one good citation, one tampered, one unverifiable (no hit)
                "id": "q1",
                "ask": {
                    "citations": ["nodeGOOD", "nodeBAD", "nodeMISSING"],
                    "hits": [
                        {
                            "node": {"id": "nodeGOOD", "type": "Claim"},
                            "provenance": {
                                "path": str(src),
                                "byte_start": 6,
                                "byte_end": 14,
                                "content_hash": f"sha256:{good_hash}",
                            },
                        },
                        {
                            "node": {"id": "nodeBAD", "type": "Claim"},
                            "provenance": {
                                "path": str(src),
                                "byte_start": 0,
                                "byte_end": 5,
                                "content_hash": "sha256:" + "0" * 64,
                            },
                        },
                    ],
                },
            },
        ]
        cit = verify_citation_bytes(responses, None)
        c_ok = (
            cit["citations_total"] == 3
            and cit["citations_verifiable"] == 2
            and cit["citations_pass"] == 1
            and cit["citations_unverifiable"] == 1
            and cit["citation_floor_pass"] is False  # the tampered one fails the gate
        )
        print(
            f"  {'ok  ' if c_ok else 'FAIL'} citations: total={cit['citations_total']} "
            f"verifiable={cit['citations_verifiable']} pass={cit['citations_pass']} "
            f"unverifiable={cit['citations_unverifiable']} gate_pass={cit['citation_floor_pass']}"
        )
        ok &= c_ok

        # all-good variant flips the gate to pass
        responses_good = [
            {
                "id": "q1",
                "ask": {
                    "citations": ["nodeGOOD"],
                    "hits": [
                        {
                            "node": {"id": "nodeGOOD", "type": "Claim"},
                            "provenance": {
                                "path": str(src),
                                "byte_start": 6,
                                "byte_end": 14,
                                "content_hash": f"sha256:{good_hash}",
                            },
                        }
                    ],
                },
            }
        ]
        cit_good = verify_citation_bytes(responses_good, None)
        g_ok = cit_good["citation_floor_pass"] is True and cit_good["verifiable_pass_rate"] == 1.0
        print(
            f"  {'ok  ' if g_ok else 'FAIL'} citations all-good -> gate pass, rate=1.0  "
            f"(got pass={cit_good['citation_floor_pass']}, rate={cit_good['verifiable_pass_rate']})"
        )
        ok &= g_ok

        # byte-stable output: two writes are identical bytes
        o1, o2 = root / "a.json", root / "b.json"
        _write_stable(cit, o1)
        _write_stable(cit, o2)
        s_ok = o1.read_bytes() == o2.read_bytes()
        print(f"  {'ok  ' if s_ok else 'FAIL'} citation report is byte-stable across writes")
        ok &= s_ok

    # ── extraction-completeness (YAML boundary + block-match logic, mocked HTTP) ──
    bound_text = (
        "questions:\n"
        "- id: x-001\n"
        "  category: simple_lookup\n"
        "  negative_control: false\n"
        "  gold_targets:\n"
        '  - source_path: "a/b.md"\n'
        "    block_id: BLOCK_A\n"
        "    node_ids: [n1, n2, n3]\n"
        "  distractors:\n"
        '  - source_path: "a/c.md"\n'
        "    block_id: BLOCK_C\n"
        "    node_ids: [n9]\n"
        "- id: nc-001\n"
        "  category: negative_control\n"
        "  negative_control: true\n"
    )
    bound = load_yaml_text(bound_text, source="floor_metrics.selftest")
    parse_ok = (
        len(bound["questions"]) == 2
        and bound["questions"][0]["id"] == "x-001"
        and bound["questions"][0]["gold_targets"][0]["block_id"] == "BLOCK_A"
        and bound["questions"][0]["gold_targets"][0]["node_ids"] == ["n1", "n2", "n3"]
        and bound["questions"][1]["negative_control"] is True
    )
    print(
        f"  {'ok  ' if parse_ok else 'FAIL'} YAML boundary: 2 questions, "
        f"gold block + node_ids parsed, negative_control flagged"
    )
    ok &= parse_ok

    # Mock the node endpoint: n2 is a live Claim on BLOCK_A -> present.
    saved = _NODE_CACHE.copy()
    _NODE_CACHE.clear()
    _NODE_CACHE[("http://mock", "n1")] = {"status": "ok", "node": {"id": "n1", "type": "Concept"}}
    _NODE_CACHE[("http://mock", "n2")] = {
        "status": "ok",
        "node": {"id": "n2", "type": "Claim"},
        "provenance": {"block_id": "BLOCK_A"},
    }
    _NODE_CACHE[("http://mock", "n3")] = None  # drifted / 404
    ext = measure_extraction_completeness(bound, "http://mock", max_probe=12)
    pq_by_id = {q["id"]: q for q in ext["per_question"]}
    e_ok = (
        ext["gold_targets_total"] == 1
        and ext["gold_targets_present"] == 1
        and ext["extraction_recall"] == 1.0
        and pq_by_id["x-001"]["gold_targets"][0]["example_claim_id"] == "n2"
    )
    print(
        f"  {'ok  ' if e_ok else 'FAIL'} extraction present: claim on gold block found  "
        f"(total={ext['gold_targets_total']} present={ext['gold_targets_present']} "
        f"recall={ext['extraction_recall']})"
    )
    ok &= e_ok

    # Absent case: no node resolves to a Claim on BLOCK_A.
    _NODE_CACHE.clear()
    _NODE_CACHE[("http://mock", "n1")] = {"status": "ok", "node": {"id": "n1", "type": "Concept"}}
    _NODE_CACHE[("http://mock", "n2")] = {
        "status": "ok",
        "node": {"id": "n2", "type": "Claim"},
        "provenance": {"block_id": "OTHER_BLOCK"},
    }
    _NODE_CACHE[("http://mock", "n3")] = None
    ext_absent = measure_extraction_completeness(bound, "http://mock", max_probe=12)
    a_ok = (
        ext_absent["gold_targets_present"] == 0
        and ext_absent["extraction_recall"] == 0.0
        and len(ext_absent["missing"]) == 1
    )
    print(
        f"  {'ok  ' if a_ok else 'FAIL'} extraction absent: no claim on gold block -> recall 0, "
        f"1 missing  (got present={ext_absent['gold_targets_present']}, "
        f"missing={len(ext_absent['missing'])})"
    )
    ok &= a_ok
    _NODE_CACHE.clear()
    _NODE_CACHE.update(saved)

    print("-" * 60)
    print("SELFTEST: PASS" if ok else "SELFTEST: FAIL")
    return 0 if ok else 1


# ════════════════════════════════════════════════════════════════════════════
# argparse / wiring
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_ENDPOINT = "http://127.0.0.1:7777"


def build_citations_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--responses", required=True, help="responses.jsonl with /ask citations+hits")
    p.add_argument(
        "--vault-root",
        dest="vault_root",
        default=None,
        help="optional vault sources dir to remap provenance paths by "
        "basename (default: use the absolute path in provenance)",
    )
    p.add_argument("--out", required=True)
    return p


def build_extraction_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument(
        "--bound", required=True, help="questions.bound.yaml (carries gold block_id + node_ids)"
    )
    p.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"live daemon base url (default {DEFAULT_ENDPOINT})",
    )
    p.add_argument(
        "--max-probe",
        dest="max_probe",
        type=int,
        default=12,
        help="max declared node_ids to probe per gold target (default 12)",
    )
    p.add_argument("--out", required=True)
    return p


def build_floor_laptop_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--responses", required=True, help="responses.jsonl with /ask citations+hits")
    p.add_argument(
        "--bound", default=None, help="questions.bound.yaml — enables extraction-completeness"
    )
    p.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"live daemon base url (default {DEFAULT_ENDPOINT})",
    )
    p.add_argument(
        "--vault-root",
        dest="vault_root",
        default=None,
        help="optional vault sources dir to remap provenance paths",
    )
    p.add_argument(
        "--max-probe",
        dest="max_probe",
        type=int,
        default=12,
        help="max declared node_ids to probe per gold target (default 12)",
    )
    p.add_argument("--out", required=True)
    return p


def main() -> int:
    p = argparse.ArgumentParser(
        description="FLOOR METRICS (laptop-only): citation byte-verification + "
        "extraction-completeness over the live vault (black-box)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("citations", help="re-slice /ask citation bytes vs content_hash")
    build_citations_parser(pc)
    pc.set_defaults(func=cmd_citations)

    pe = sub.add_parser("extraction", help="bound gold target -> live Claim on its block?")
    build_extraction_parser(pe)
    pe.set_defaults(func=cmd_extraction)

    pf = sub.add_parser("floor-laptop", help="both laptop floor metrics in one report")
    build_floor_laptop_parser(pf)
    pf.set_defaults(func=cmd_floor_laptop)

    pv = sub.add_parser("validate-floor-laptop", help="fail unless a laptop floor passed")
    pv.add_argument("--report", required=True)
    pv.add_argument("--responses", required=True)
    pv.set_defaults(func=cmd_validate_floor_laptop)

    pt = sub.add_parser("selftest", help="validate both metrics on fixtures (no network)")
    pt.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
