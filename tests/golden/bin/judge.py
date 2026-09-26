#!/usr/bin/env python3
"""Golden-harness grading helper — black-box, zero Okto Neuron source imports.

Two jobs, both driven from JSON the shell harness already captured:

  1. provenance  — re-slice every recalled/cited hit's (path, byte_start, byte_end)
                   out of the dataset's inputs/ on disk and assert
                   sha256(bytes) == content_hash. This is the deterministic,
                   no-LLM correctness gate. Mirrors tests/eval/golden.py::
                   validate_provenance but reimplemented here so the harness
                   imports NO marginalia source (black-box contract).

  2. byte-grounding — materialize acceptance evidence for cited Claim spans only,
                   hashing both the complete source and the exact byte excerpt.

  3. judge       — for each question, send {expected answer, system /ask text,
                   retrieved snippets} to a local OpenAI-compatible LLM with a
                   strict rubric and record a verdict
                   {correct|partial|wrong|missed} + 1-line rationale.
                   Fails as infrastructure-incomplete if no judge LLM is reachable.

Usage:
  judge.py provenance --inputs <dir> --responses <responses.jsonl> --out <deterministic.json>
  judge.py byte-grounding --corpus-id <id> --inputs <dir> --responses <responses.jsonl>
                           --manifest <run-manifest.json>
                           --semantic-quality <semantic-quality.json> --out <evidence.json>
  judge.py semantic-quality --endpoint <url> --responses <responses.jsonl> --out <semantic.json>
  judge.py judge      --questions <questions.yaml> --responses <responses.jsonl> --out <judge.json>
                      [--base-url URL] [--model NAME]

PyYAML is a core Okto Neuron dependency and is the sole YAML authority.  The
harness fails closed with a structured error if it is invoked outside the
project environment instead of approximating YAML with a partial parser; exact
question and evidence strings participate in byte-level quality gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Make the sibling modules (manifest / scorecard / sweep / panel / floor_metrics)
# importable at MODULE level regardless of cwd: judge.py is run as a script
# (`python3 judge.py …`), so its own directory is the import anchor. Inserting it
# explicitly means the imports below resolve the same whether the harness invokes
# judge.py from the repo root, from tests/golden/bin, or under a stricter type
# checker — no reliance on the interpreter happening to add the script dir, and no
# `# pyright: ignore` papering over an unresolved import.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
# `archive/` holds retired-but-retained apparatus (panel.py — the kappa=0.14
# N-judge panel). It is kept importable for the historical `panel` subcommand;
# it gates nothing.
_ARCHIVE_DIR = str(Path(__file__).resolve().parent / "archive")
if _ARCHIVE_DIR not in sys.path:
    sys.path.insert(0, _ARCHIVE_DIR)

import floor_metrics as _floor_metrics  # noqa: E402  sibling module (laptop floor)
import manifest as _manifest  # noqa: E402  sibling module, tests/golden/bin/manifest.py
import panel as _panel  # noqa: E402  ARCHIVED module, tests/golden/bin/archive/panel.py
import scorecard as _scorecard  # noqa: E402  sibling module, tests/golden/bin/scorecard.py
import sweep as _sweep  # noqa: E402  sibling module, tests/golden/bin/sweep.py
from golden_yaml import (  # noqa: E402
    GoldenYamlError,
    load_yaml,
    question_validation_errors,
)

_LIVE_LLM_BASE_URL = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
_LIVE_JUDGE_MODEL = os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()


def _openai_headers(*, json_body: bool = False) -> dict[str, str]:
    """Build standard OpenAI-compatible headers without logging the secret."""

    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ── YAML loading ───────────────────────────────────────────────────────────────


def cmd_validate_questions(args: argparse.Namespace) -> int:
    questions_path = Path(args.questions)
    if not questions_path.is_file():
        print(json.dumps({"error": f"questions file not found: {questions_path}"}), file=sys.stderr)
        return 1
    errors = question_validation_errors(load_yaml(questions_path))
    result = {"valid": not errors, "errors": errors}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


def input_basename_collisions(inputs_root: Path) -> list[list[str]]:
    """Find source paths that the HTTP ingest boundary would flatten together."""

    by_basename: dict[str, list[str]] = {}
    for path in sorted(inputs_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        relative = path.relative_to(inputs_root).as_posix()
        by_basename.setdefault(path.name.casefold(), []).append(relative)
    return sorted(paths for paths in by_basename.values() if len(paths) > 1)


def cmd_validate_inputs(args: argparse.Namespace) -> int:
    inputs_root = Path(args.inputs)
    if not inputs_root.is_dir():
        print(json.dumps({"error": f"inputs directory not found: {inputs_root}"}), file=sys.stderr)
        return 1
    collisions = input_basename_collisions(inputs_root)
    result = {
        "valid": not collisions,
        "basename_collisions": collisions,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not collisions else 1


# ── responses.jsonl ────────────────────────────────────────────────────────────


def read_responses_with_sha256(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    out: list[dict[str, Any]] = []
    for line_number, ln in enumerate(raw.decode("utf-8").splitlines(), start=1):
        ln = ln.strip()
        if ln:
            record = json.loads(ln)
            if not isinstance(record, dict):
                raise ValueError(f"response at line {line_number} is not an object")
            out.append(record)
    return out, hashlib.sha256(raw).hexdigest()


def read_responses(path: Path) -> list[dict[str, Any]]:
    return read_responses_with_sha256(path)[0]


def recall_cost_samples(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the complete per-question recall-cost evidence or fail closed."""

    if not responses:
        raise ValueError("responses.jsonl contains no recall samples")
    samples: list[dict[str, Any]] = []
    for index, record in enumerate(responses):
        recall = record.get("recall")
        sample = recall.get("recall_cost") if isinstance(recall, dict) else None
        if not isinstance(sample, dict):
            qid = record.get("id", index)
            raise ValueError(f"response {qid!r} has no recall.recall_cost object")
        samples.append(sample)
    return samples


def _post_semantic_quality(
    endpoint: str,
    samples: list[dict[str, Any]],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    body = json.dumps({"recall_samples": samples}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("OKTO_NEURON_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    vault_path = os.environ.get("OKTO_NEURON_VAULT_PATH", "").strip()
    if vault_path:
        headers["X-Okto-Neuron-Vault"] = vault_path
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/v1/quality/semantic",
        data=body,
        headers=headers,
        method="POST",
    )
    audit_busy_backoff_s = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
    for attempt in range(len(audit_busy_backoff_s) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            retryable = (
                exc.code == 409
                and isinstance(error_payload, dict)
                and error_payload.get("error") == "audit_busy"
            )
            if not retryable or attempt >= len(audit_busy_backoff_s):
                raise
            delay = audit_busy_backoff_s[attempt]
            print(
                "semantic-quality audit busy; "
                f"retry {attempt + 2}/{len(audit_busy_backoff_s) + 1} in {delay:g}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    if not isinstance(payload, dict):
        raise ValueError("semantic-quality endpoint returned a non-object response")
    return payload


def semantic_quality_capture_errors(
    payload: dict[str, Any],
    *,
    expected_samples: int,
    expected_responses_sha256: str,
) -> list[str]:
    """Validate capture completeness, leaving metric definitions to Okto Neuron."""

    errors: list[str] = []
    capture = payload.get("capture")
    if not isinstance(capture, dict):
        errors.append("capture metadata is missing")
    else:
        if capture.get("schema_version") != "golden.semantic_quality_capture.v1":
            errors.append("capture schema_version is not golden.semantic_quality_capture.v1")
        if capture.get("responses_sha256") != expected_responses_sha256:
            errors.append("capture responses_sha256 does not match responses.jsonl")
        if capture.get("sample_count") != expected_samples:
            errors.append("capture sample_count does not match responses.jsonl")
    if payload.get("status") != "ok":
        errors.append("endpoint status is not ok")
    report = payload.get("semantic_quality")
    if not isinstance(report, dict):
        return [*errors, "semantic_quality report is missing"]
    if report.get("schema_version") != "semantic_quality.v1":
        errors.append("semantic_quality schema_version is not semantic_quality.v1")
    if report.get("evaluator_version") != "semantic_quality.v1":
        errors.append("semantic_quality evaluator_version is not semantic_quality.v1")
    for section in ("evidence", "population", "hard_invariants", "verdict"):
        if not isinstance(report.get(section), dict):
            errors.append(f"semantic_quality.{section} is missing")
    if not isinstance(report.get("limitations"), list):
        errors.append("semantic_quality.limitations is missing")
    layers = report.get("layers")
    if isinstance(layers, dict):
        for layer in ("surface", "type", "identity", "predicate", "relation", "recall"):
            if not isinstance(layers.get(layer), dict):
                errors.append(f"semantic_quality.layers.{layer} is missing")
    recall = layers.get("recall") if isinstance(layers, dict) else None
    if not isinstance(recall, dict):
        return [*errors, "semantic_quality.layers.recall is missing"]
    if recall.get("schema_version") != "recall_cost.aggregate.v1":
        errors.append("recall aggregate schema_version is not recall_cost.aggregate.v1")
    if recall.get("status") != "measured":
        errors.append("recall cost status is not measured")
    if recall.get("provided_samples") != expected_samples:
        errors.append("recall provided_samples does not match responses.jsonl")
    if recall.get("measured_samples") != expected_samples:
        errors.append("recall measured_samples does not match responses.jsonl")
    rejected = recall.get("rejected_samples")
    if not isinstance(rejected, dict) or rejected.get("count") != 0:
        errors.append("recall aggregate rejected one or more samples")
    for section in (
        "completion",
        "query_embedding",
        "deterministic_retrieval",
        "deterministic_projection",
        "total_latency_ms",
        "answer_generation",
    ):
        if not isinstance(recall.get(section), dict):
            errors.append(f"recall aggregate {section} is missing")
    return errors


def capture_semantic_quality(
    endpoint: str,
    responses_path: Path,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    responses, responses_sha256 = read_responses_with_sha256(responses_path)
    samples = recall_cost_samples(responses)
    payload = _post_semantic_quality(endpoint, samples, timeout_s=timeout_s)
    payload["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": responses_sha256,
        "sample_count": len(samples),
    }
    errors = semantic_quality_capture_errors(
        payload,
        expected_samples=len(samples),
        expected_responses_sha256=responses_sha256,
    )
    if errors:
        raise ValueError("incomplete semantic-quality capture: " + "; ".join(errors))
    return payload


def cmd_semantic_quality(args: argparse.Namespace) -> int:
    """Capture the server-owned semantic report for every recorded recall."""

    try:
        payload = capture_semantic_quality(
            args.endpoint,
            Path(args.responses),
            timeout_s=args.timeout_s,
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"semantic-quality capture failed: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    recall = payload["semantic_quality"]["layers"]["recall"]
    print(
        json.dumps(
            {
                "status": payload["status"],
                "provided_samples": recall["provided_samples"],
                "measured_samples": recall["measured_samples"],
            },
            indent=2,
        )
    )
    return 0


def cmd_validate_semantic_quality(args: argparse.Namespace) -> int:
    """Check that a retained sidecar accounts for every retained response."""

    try:
        responses, responses_sha256 = read_responses_with_sha256(Path(args.responses))
        samples = recall_cost_samples(responses)
        payload = json.loads(Path(args.semantic_quality).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("semantic-quality sidecar is not an object")
        errors = semantic_quality_capture_errors(
            payload,
            expected_samples=len(samples),
            expected_responses_sha256=responses_sha256,
        )
        if errors:
            raise ValueError("; ".join(errors))
    except (OSError, ValueError) as exc:
        print(f"invalid semantic-quality sidecar: {exc}", file=sys.stderr)
        return 1
    return 0


# ── provenance byte-hash validation ─────────────────────────────────────────────

_BYTE_ANCHORED = {"Claim", "Block"}


def _resolve_source(inputs_root: Path, rel_or_abs: str) -> Path | None:
    # Gold spans and byte re-slicing are defined against the inputs/ trust copy,
    # so always resolve to that copy first — even when the hit carries an absolute
    # vault path (e.g. <vault>/.marginalia/sources/<file>). The vault source is a
    # verbatim byte-for-byte copy of inputs/, so the byte offsets are identical;
    # returning the absolute vault path instead would make every gold span fail to
    # match (different parent dir) and silently report all-zeros.
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    # Relative path: resolve directly under inputs_root.
    if not p.is_absolute():
        cand = inputs_root / rel_or_abs
        if cand.exists():
            return cand
    # Absolute (e.g. a vault source path) or unresolved relative: map by
    # basename into inputs_root. (`inputs_root / <abspath>` would yield the
    # absolute path itself, so basename matching is required here.)
    matches = list(inputs_root.rglob(p.name))
    if matches:
        return matches[0]
    if p.is_absolute() and p.exists():
        return p
    return None


def validate_hit(inputs_root: Path, hit: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason). Skips (ok=True) non-byte-anchored or empty-range hits."""
    prov = hit.get("provenance") or {}
    node = hit.get("node") or {}
    node_type = node.get("type") or hit.get("type") or ""
    ch = prov.get("content_hash") or ""
    bs, be = prov.get("byte_start"), prov.get("byte_end")
    path = prov.get("path") or ""

    if node_type and node_type not in _BYTE_ANCHORED:
        return True, f"not byte-anchored ({node_type}, skipped)"
    if not ch or bs is None or be is None:
        return True, "no byte range (skipped)"
    try:
        bs_i, be_i = int(bs), int(be)
    except (TypeError, ValueError):
        return True, "non-int byte range (skipped)"
    if be_i <= bs_i:
        return True, "empty byte range (skipped)"
    expected_hex = ch.split(":", 1)[-1].lower()
    if not expected_hex:
        return True, "empty-hash (skipped)"
    src = _resolve_source(inputs_root, path)
    if src is None:
        return False, f"source not found for path={path!r}"
    try:
        raw = src.read_bytes()[bs_i:be_i]
    except OSError as exc:
        return False, f"read failed: {exc}"
    actual_hex = hashlib.sha256(raw).hexdigest()
    if actual_hex != expected_hex:
        return False, (
            f"hash mismatch path={src.name} bytes[{bs_i}:{be_i}] "
            f"expected={expected_hex[:12]} actual={actual_hex[:12]}"
        )
    return True, "ok"


def _strict_input_source(inputs_root: Path, provenance_path: str) -> Path:
    """Resolve provenance into the immutable dataset trust copy, never the vault."""

    if not provenance_path:
        raise ValueError("cited Claim has no provenance.path")
    candidate = Path(provenance_path)
    if not candidate.is_absolute():
        direct = inputs_root / candidate
        if direct.is_file():
            return direct.resolve()
    matches = sorted(path.resolve() for path in inputs_root.rglob(candidate.name) if path.is_file())
    if not matches:
        raise ValueError(f"source not found in inputs trust copy: {provenance_path!r}")
    if len(matches) > 1:
        relative = [path.relative_to(inputs_root).as_posix() for path in matches]
        raise ValueError(
            f"ambiguous source basename {candidate.name!r} in inputs trust copy: {relative}"
        )
    return matches[0]


def _sha256_prefixed(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def build_byte_grounding_evidence(
    *,
    corpus_id: str,
    inputs_root: Path,
    responses_path: Path,
    manifest_path: Path,
    semantic_quality_path: Path,
) -> dict[str, Any]:
    """Build byte-grounding evidence from cited, live Claim hits.

    The response capture chooses the sample population. This function only
    materializes a sample after independently re-reading the dataset trust copy
    and proving that the captured Claim's exact byte range still hashes to the
    provenance content hash.
    """

    corpus_id = corpus_id.strip()
    if not corpus_id:
        raise ValueError("corpus_id must be non-empty")
    inputs_root = inputs_root.resolve()
    if not inputs_root.is_dir():
        raise ValueError(f"inputs directory not found: {inputs_root}")
    collisions = input_basename_collisions(inputs_root)
    if collisions:
        raise ValueError(f"inputs contain ambiguous basenames: {collisions}")

    manifest_raw = manifest_path.read_bytes()
    json.loads(manifest_raw.decode("utf-8"))
    manifest_sha256 = _sha256_prefixed(manifest_raw)

    semantic_capture = json.loads(semantic_quality_path.read_text(encoding="utf-8"))
    if not isinstance(semantic_capture, dict):
        raise ValueError("semantic-quality sidecar is not an object")
    semantic_report = semantic_capture.get("semantic_quality")
    if not isinstance(semantic_report, dict):
        raise ValueError("semantic-quality sidecar has no semantic_quality report")
    evidence = semantic_report.get("evidence")
    snapshot = semantic_report.get("semantic_snapshot")
    fingerprints = snapshot.get("fingerprints") if isinstance(snapshot, dict) else None
    graph_generation = evidence.get("graph_generation") if isinstance(evidence, dict) else None
    semantic_policy = (
        fingerprints.get("semantic_policy") if isinstance(fingerprints, dict) else None
    )
    if not isinstance(graph_generation, str) or not graph_generation.strip():
        raise ValueError("semantic-quality report has no graph generation")
    if not isinstance(semantic_policy, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", semantic_policy
    ):
        raise ValueError("semantic-quality report has no measured semantic policy fingerprint")

    samples_by_key: dict[tuple[str, str, int, int, str], dict[str, Any]] = {}
    for response_index, response in enumerate(read_responses(responses_path), start=1):
        recall = response.get("recall")
        ask = response.get("ask")
        hits = recall.get("hits") if isinstance(recall, dict) else None
        citations = ask.get("citations") if isinstance(ask, dict) else None
        if not isinstance(hits, list) or not isinstance(citations, list):
            continue
        cited_ids: set[str] = set()
        for citation in citations:
            if isinstance(citation, str) and citation.strip():
                cited_ids.add(citation)
            elif isinstance(citation, dict):
                node = citation.get("node")
                node_id = node.get("id") if isinstance(node, dict) else citation.get("id")
                if isinstance(node_id, str) and node_id.strip():
                    cited_ids.add(node_id)
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            node = hit.get("node")
            if not isinstance(node, dict) or node.get("type") != "Claim":
                continue
            claim_id = node.get("id")
            if not isinstance(claim_id, str) or claim_id not in cited_ids:
                continue
            provenance = hit.get("provenance")
            if not isinstance(provenance, dict):
                raise ValueError(f"cited Claim {claim_id!r} has no provenance object")
            try:
                start = int(provenance["byte_start"])
                end = int(provenance["byte_end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"cited Claim {claim_id!r} has an invalid byte range") from exc
            if start < 0 or end <= start:
                raise ValueError(f"cited Claim {claim_id!r} has an empty byte range")
            source = _strict_input_source(inputs_root, str(provenance.get("path") or ""))
            source_raw = source.read_bytes()
            if end > len(source_raw):
                raise ValueError(
                    f"cited Claim {claim_id!r} range [{start}:{end}] exceeds {source.name}"
                )
            excerpt_sha256 = _sha256_prefixed(source_raw[start:end])
            captured_sha256 = str(provenance.get("content_hash") or "").lower()
            if excerpt_sha256 != captured_sha256:
                raise ValueError(
                    f"cited Claim {claim_id!r} excerpt hash does not match captured provenance"
                )
            source_id = f"source:{source.relative_to(inputs_root).as_posix()}"
            key = (claim_id, source_id, start, end, excerpt_sha256)
            sample_seed = json.dumps(
                [corpus_id, *key], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            samples_by_key[key] = {
                "sample_id": _sha256_prefixed(sample_seed),
                "relation_claim_id": claim_id,
                "source_id": source_id,
                "source_sha256": _sha256_prefixed(source_raw),
                "start_byte": start,
                "end_byte": end,
                "excerpt_sha256": excerpt_sha256,
                "verified": True,
            }

    if not samples_by_key:
        raise ValueError("responses contain no cited Claim with verifiable byte provenance")
    samples = [samples_by_key[key] for key in sorted(samples_by_key)]
    return {
        "schema_version": "byte_grounding_evidence.v1",
        "corpus_id": corpus_id,
        "manifest_sha256": manifest_sha256,
        "graph_generation": graph_generation,
        "semantic_policy_fingerprint": semantic_policy,
        "samples": samples,
    }


def cmd_byte_grounding(args: argparse.Namespace) -> int:
    """Materialize deterministic ADR 0040 byte-grounding evidence."""

    try:
        payload = build_byte_grounding_evidence(
            corpus_id=args.corpus_id,
            inputs_root=Path(args.inputs),
            responses_path=Path(args.responses),
            manifest_path=Path(args.manifest),
            semantic_quality_path=Path(args.semantic_quality),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"byte-grounding capture failed: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "verified",
                "corpus_id": payload["corpus_id"],
                "samples": len(payload["samples"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


# ── gold-span metrics (deterministic; chunker-quality signal) ──────────────────
# A gold span is the source byte range that actually contains a question's answer.
# Holding the retriever fixed, these measure whether the chunker keeps that span
# usable: intact (survives inside ONE retrieved block) and retrieved@k (any
# retrieved block overlaps it), plus byte-IoU and word-token recall. Method follows
# R04's gold-span/token-recall evaluation (Chroma chunking-eval methodology).


def _tokens(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _union_len(spans: list[tuple[int, int]]) -> int:
    total = 0
    last_end = -1
    for s, e in sorted(spans):
        s = max(s, last_end)
        if e > s:
            total += e - s
            last_end = e
        elif e > last_end:
            last_end = e
    return total


def _hit_spans_for_path(
    inputs_root: Path, gold_src: Path, hits: list[dict[str, Any]], *, include_context: bool = True
) -> list[tuple[int, int]]:
    """Byte ranges of retrieved hits that resolve to the same file as the gold span.

    With ``include_context`` (default), block-neighbor expansion spans
    (``hit.context_spans``, D5) are folded in alongside each hit's primary
    provenance — this is what query-time expansion moves on retrieved@k / IoU /
    token-recall. Pass ``include_context=False`` for ``gold_span_intact``, which
    must stay a per-BLOCK measure (did one *retrieved seed* block keep the answer
    whole) so it isolates chunker quality for later attribution — unioning in
    neighbors would let a fragmented seed falsely read as intact."""
    spans: list[tuple[int, int]] = []
    for h in hits:
        ranges = [h.get("provenance") or {}]
        if include_context:
            ranges += list(h.get("context_spans") or [])
        for r in ranges:
            bs, be, path = r.get("byte_start"), r.get("byte_end"), r.get("path") or ""
            if bs is None or be is None:
                continue
            src = _resolve_source(inputs_root, path)
            if src is None or src.resolve() != gold_src.resolve():
                continue
            try:
                bs_i, be_i = int(bs), int(be)
            except (TypeError, ValueError):
                continue
            if be_i > bs_i:
                spans.append((bs_i, be_i))
    return spans


def gold_target_spans(
    inputs_root: Path,
    gold_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve quote-based Golden targets to the byte spans used by recall metrics."""

    spans: list[dict[str, Any]] = []
    for target in gold_targets:
        path = str(target.get("source_path") or "")
        quote = str(target.get("quote") or "")
        source = _resolve_source(inputs_root, path)
        if source is None or not quote:
            spans.append({"path": path, "byte_start": -1, "byte_end": -1})
            continue
        raw = source.read_bytes()
        quote_bytes = quote.encode("utf-8")
        if raw.count(quote_bytes) != 1:
            spans.append({"path": path, "byte_start": -1, "byte_end": -1})
            continue
        byte_start = raw.find(quote_bytes)
        spans.append(
            {
                "path": path,
                "byte_start": byte_start,
                "byte_end": byte_start + len(quote_bytes),
                "quote_hash": f"sha256:{hashlib.sha256(quote_bytes).hexdigest()}",
            }
        )
    return spans


def gold_span_metrics(
    inputs_root: Path, gold_spans: list[dict[str, Any]], hits: list[dict[str, Any]], k: int
) -> dict[str, Any] | None:
    """Per-question gold-span metrics, or None when no gold spans are declared."""
    if not gold_spans:
        return None
    topk = hits[:k]
    per_span: list[dict[str, Any]] = []
    for gs in gold_spans:
        src = _resolve_source(inputs_root, gs.get("path") or "")
        try:
            bs, be = int(gs["byte_start"]), int(gs["byte_end"])
        except (KeyError, TypeError, ValueError):
            per_span.append({"path": gs.get("path"), "error": "bad gold span range"})
            continue
        if src is None or be <= bs:
            per_span.append({"path": gs.get("path"), "error": "gold source not found / empty"})
            continue
        raw = src.read_bytes()
        gold_bytes = raw[bs:be]
        # Self-check: the declared gold span must hash to what's in the file.
        qh = (gs.get("quote_hash") or "").split(":", 1)[-1].lower()
        anchor_ok = (not qh) or hashlib.sha256(gold_bytes).hexdigest() == qh
        # retrieved@k / IoU / recall fold in neighbor-expansion context spans;
        # intact stays primary-block-only (chunker-quality isolation, per D5/D8).
        spans = _hit_spans_for_path(inputs_root, src, topk)
        primary_spans = _hit_spans_for_path(inputs_root, src, topk, include_context=False)
        # Retrieved hits and neighbor context may overlap or repeat the same
        # bytes. Count the covered gold bytes once; summing pairwise overlaps
        # can produce an impossible IoU greater than 1.
        intersection = _union_len(
            [(max(bs, s), min(be, e)) for s, e in spans if _overlap(bs, be, s, e)]
        )
        # clip retrieved spans to compute true union vs gold for IoU
        union = _union_len([(bs, be), *spans])
        iou = intersection / union if union else 0.0
        # `iou` (and its summary alias below) is a dilution ratio, not an
        # anchoring-quality score: `spans` include whole retrieved blocks plus
        # neighbor-expansion context, so union approx |retrieved| and iou approx
        # |gold| / |retrieved|. It reports how much extra context surrounds the
        # gold quote, not whether the gold quote was found (see anchor_ok /
        # retrieved / intact for that). Kept under the historical `byte_iou`
        # key as a deprecated alias; `byte_dilution_ratio` is the honest name.
        retrieved_bytes = _union_len(spans)
        gold_byte_count = len(gold_bytes)
        dilution_ratio = (
            round(retrieved_bytes / gold_byte_count, 2)
            if gold_byte_count and retrieved_bytes
            else None
        )
        intact = any(s <= bs and e >= be for s, e in primary_spans)
        gold_tok = _tokens(gold_bytes.decode("utf-8", "replace"))
        retr_text = " ".join(raw[s:e].decode("utf-8", "replace") for s, e in spans)
        retr_tok = set(_tokens(retr_text))
        token_recall = (
            sum(1 for t in gold_tok if t in retr_tok) / len(gold_tok) if gold_tok else 0.0
        )
        per_span.append(
            {
                "path": gs.get("path"),
                "anchor_ok": anchor_ok,
                "retrieved": intersection > 0,
                "intact": intact,
                "byte_iou": round(iou, 4),  # deprecated alias, see comment above
                "gold_bytes": gold_byte_count,
                "retrieved_bytes": retrieved_bytes,
                "byte_dilution_ratio": dilution_ratio,
                "token_recall": round(token_recall, 4),
            }
        )
    scored = [s for s in per_span if "error" not in s]
    dilutions = [s["byte_dilution_ratio"] for s in scored if s["byte_dilution_ratio"] is not None]
    return {
        "spans": len(gold_spans),
        "anchors_ok": all(s.get("anchor_ok", False) for s in scored) if scored else False,
        "gold_span_retrieved_at_k": all(s["retrieved"] for s in scored) if scored else False,
        "any_retrieved": any(s["retrieved"] for s in scored),
        "gold_span_intact": all(s["intact"] for s in scored) if scored else False,
        # Deprecated alias, retained for one release for downstream comparisons
        # (frozen synthetic-CI reports read this key). Do not treat as a
        # quality headline; see `mean_gold_byte_dilution` and the per-span
        # comment above.
        "mean_byte_iou": round(sum(s["byte_iou"] for s in scored) / len(scored), 4)
        if scored
        else 0.0,
        "mean_gold_byte_dilution": round(sum(dilutions) / len(dilutions), 2) if dilutions else None,
        "mean_token_recall": round(sum(s["token_recall"] for s in scored) / len(scored), 4)
        if scored
        else 0.0,
        "per_span": per_span,
    }


def session_reference_metrics(
    expected_source_paths: list[str],
    hits: list[dict[str, Any]],
    *,
    k_values: tuple[int, ...] = (1, 3, 5, 10, 30),
) -> dict[str, Any]:
    """Session-granularity retrieval metrics compatible with LongMemEval.

    Okto Neuron recalls nodes, so several hits may originate from one session.
    The upstream reference retrieves sessions. Collapse each source to its first
    observed rank before computing binary recall and NDCG; otherwise a session
    producing many Claims would unfairly consume multiple ranks.
    """

    expected = {Path(path).name for path in expected_source_paths if str(path).strip()}
    if not expected:
        return {
            "status": "not_applicable",
            "reason": "question has no reference answer session",
            "expected_sessions": 0,
            "metrics": {},
        }

    ranked_sources: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        provenance = hit.get("provenance") if isinstance(hit, dict) else None
        raw_path = provenance.get("path") if isinstance(provenance, dict) else None
        source = Path(str(raw_path)).name if raw_path else ""
        if not source or source in seen:
            continue
        seen.add(source)
        ranked_sources.append(source)

    metrics: dict[str, dict[str, float | int]] = {}
    for k in k_values:
        top = ranked_sources[:k]
        relevant = [source in expected for source in top]
        found = sum(relevant)
        dcg = sum(
            1.0 / math.log2(rank + 2) for rank, is_relevant in enumerate(relevant) if is_relevant
        )
        ideal_relevant = min(len(expected), k)
        idcg = sum(1.0 / math.log2(rank + 2) for rank in range(ideal_relevant))
        metrics[str(k)] = {
            "recall_any_at_k": int(found > 0),
            "recall_all_at_k": int(found == len(expected)),
            "ndcg_at_k": round(dcg / idcg, 6) if idcg else 0.0,
            "relevant_sessions_retrieved": found,
        }
    return {
        "status": "measured",
        "expected_sessions": len(expected),
        "ranked_unique_sessions": len(ranked_sources),
        "metrics": metrics,
    }


def cmd_provenance(args: argparse.Namespace) -> int:
    inputs_root = Path(args.inputs).resolve()
    responses = read_responses(Path(args.responses))
    questions: dict[str, Any] = {}
    default_k = 10
    if getattr(args, "questions", None):
        q_doc = load_yaml(Path(args.questions))
        validation_errors = question_validation_errors(q_doc)
        if validation_errors:
            print(
                json.dumps({"error": "invalid questions", "details": validation_errors}),
                file=sys.stderr,
            )
            return 1
        questions = {q["id"]: q for q in (q_doc.get("questions") or [])}
        default_k = (q_doc.get("settings") or {}).get("k", 10)
        invalid_k_ids = [
            str(rec.get("id") or "?") for rec in responses if rec.get("k") != default_k
        ]
        if invalid_k_ids:
            print(
                json.dumps(
                    {
                        "error": f"responses must all use pinned k={default_k}",
                        "ids": invalid_k_ids,
                    }
                ),
                file=sys.stderr,
            )
            return 1
    per_q: list[dict[str, Any]] = []
    all_failures: list[str] = []

    for rec in responses:
        qid = rec.get("id", "?")
        hits = (rec.get("recall") or {}).get("hits") or []
        cites = (rec.get("ask") or {}).get("citations") or []
        checked = 0
        failures: list[str] = []
        for h in list(hits) + [c for c in cites if isinstance(c, dict)]:
            ok, reason = validate_hit(inputs_root, h)
            if reason.endswith("(skipped)"):
                continue
            checked += 1
            if not ok:
                failures.append(reason)
        if failures:
            all_failures.extend(f"{qid}: {r}" for r in failures)
        q = questions.get(qid, {})
        declared_spans = q.get("gold_spans") or []
        span_source = "gold_spans" if declared_spans else None
        if not declared_spans and q.get("gold_targets"):
            declared_spans = gold_target_spans(inputs_root, q["gold_targets"])
            span_source = "gold_targets"
        gold = gold_span_metrics(inputs_root, declared_spans, hits, q.get("k", default_k))
        session_reference = session_reference_metrics(q.get("expected_source_paths") or [], hits)
        per_q.append(
            {
                "id": qid,
                "tier": rec.get("tier"),
                "hits_checked": checked,
                "provenance_ok": not failures,
                "failures": failures,
                "gold_spans": gold,
                "gold_span_source": span_source,
                "session_reference": session_reference,
            }
        )

    scored_rows = [p for p in per_q if p.get("gold_spans")]
    scored = [p["gold_spans"] for p in scored_rows]
    gold_summary = None
    if scored:
        dilutions = [
            g["mean_gold_byte_dilution"]
            for g in scored
            if g.get("mean_gold_byte_dilution") is not None
        ]
        gold_summary = {
            "questions_with_gold_spans": len(scored),
            # Headline health signals: a gold span is found (retrieved_at_k),
            # wholly contained in a primary block (intact), and its declared
            # bytes still hash correctly (anchors_ok). Gate/compare on these,
            # not on the dilution ratio below.
            "retrieved_at_k": sum(1 for g in scored if g["gold_span_retrieved_at_k"]),
            "intact": sum(1 for g in scored if g["gold_span_intact"]),
            "anchors_ok": sum(1 for g in scored if g["anchors_ok"]),
            "mean_token_recall": round(
                sum(g["mean_token_recall"] for g in scored) / len(scored), 4
            ),
            # Deprecated alias; see mean_gold_byte_dilution and the per-span
            # comment in gold_span_metrics(). This is a retrieved/gold byte
            # ratio, not a retrieval-quality score.
            "mean_byte_iou": round(sum(g["mean_byte_iou"] for g in scored) / len(scored), 4),
            "mean_gold_byte_dilution": round(sum(dilutions) / len(dilutions), 2)
            if dilutions
            else None,
            # C2: name the questions that are not (yet) headline-clean, so a
            # sub-100% retrieved_at_k/intact count is actionable rather than a
            # bare number.
            "not_retrieved_at_k_ids": [
                r["id"] for r in scored_rows if not r["gold_spans"]["gold_span_retrieved_at_k"]
            ],
            "not_intact_ids": [
                r["id"] for r in scored_rows if not r["gold_spans"]["gold_span_intact"]
            ],
        }
    measured_sessions = [
        row["session_reference"]
        for row in per_q
        if row["session_reference"]["status"] == "measured"
    ]
    session_summary = None
    if measured_sessions:
        session_summary = {
            "questions_evaluated": len(measured_sessions),
            "metrics": {
                k: {
                    metric: round(
                        sum(row["metrics"][k][metric] for row in measured_sessions)
                        / len(measured_sessions),
                        6,
                    )
                    for metric in (
                        "recall_any_at_k",
                        "recall_all_at_k",
                        "ndcg_at_k",
                    )
                }
                for k in ("1", "3", "5", "10", "30")
            },
        }
    report = {
        "inputs_root": str(inputs_root),
        "questions": len(responses),
        "k": default_k,
        "provenance_gate_pass": not all_failures,
        "total_failures": len(all_failures),
        "failures": all_failures,
        "gold_span_summary": gold_summary,
        "session_reference_summary": session_summary,
        "per_question": per_q,
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "provenance_gate_pass": report["provenance_gate_pass"],
                "total_failures": report["total_failures"],
                "questions": report["questions"],
            },
            indent=2,
        )
    )
    return 0  # provenance result lives in the report; harness decides gating


# ── scripted LLM judge ──────────────────────────────────────────────────────────

# ── rubric v3 — structured verdict (ADR 0040 / plan-5-judge-fabrication) ────────
#
# v2 asked the model to resolve, in prose, a precedence between hedge-tolerance
# and fabrication-strictness inside ONE scalar `verdict` field. Measured result:
# the embellishment probe moved bidirectionally across v1->v2 edits (one row
# stricter, two more lenient) because the precedence is a two-line decision the
# model was re-deriving per call. v3 asks for the two judgements the model can
# make independently and composes the scalar verdict in Python (`compose_verdict`
# below) so a rubric edit cannot silently flip precedence again.
#
# Owner ruling (Q1, session S4): fabrication CAPS the verdict at "partial" — a
# fabricated-detail answer can never be "correct", but fabrication alone never
# auto-downgrades all the way to "wrong". The one case that still reaches
# "wrong" through fabrication is a NEGATIVE/ABSENT question answered with
# invented specifics: the expected answer asserts the information is absent, so
# a substantive invented answer directly CONTRADICTS it (ground truth says "not
# present", answer says "present, and here are the details") — that is scored
# via key_facts="contradicted", not via the fabrication flag, and is therefore
# unaffected by the Q1 cap.
_RUBRIC = """You are a strict grader for a knowledge-graph QA system.
Compare the SYSTEM ANSWER against the EXPECTED ANSWER for the QUESTION. You may
also be given SOURCE EXCERPTS: bounded quotes of the underlying source text the
system's retrieval actually surfaced. Use them ONLY to decide whether a detail
the system stated is supported anywhere in the source, not just in the expected
answer text.

Judge two things SEPARATELY. Do not attempt to combine them into a single
verdict — that composition is done for you afterward.

1. `key_facts` — how well the system covers the key fact(s) the question asks for:
   - "conveyed"     : every key fact is present (see hedge/wrong-slot notes below).
   - "hedged"       : a key fact is present but only inside a quoted example, code
                        block, or parenthetical aside, while the surrounding prose
                        denies having it (e.g. "the notes do not provide the exact
                        text") or hedges around it. Read the WHOLE answer before
                        deciding it declined — a fact anywhere in the text counts.
   - "partial"      : the system is on-topic and partly right but misses or garbles
                        a key fact, OR fills the right slot with the wrong value while
                        naming an entity/figure that DOES appear in the EXPECTED ANSWER
                        or the SOURCE EXCERPTS (wrong-slot fill — the name is real, the
                        role assigned to it is not).
   - "missing"      : the system produced no usable answer (empty / declined / "I
                        don't know") where the question is NOT a negative/absent one.
   - "contradicted" : the system asserts something the EXPECTED ANSWER states to be
                        false, OR — for a NEGATIVE/ABSENT question (expected answer
                        says the information is NOT in the notes) — the system
                        supplies a substantive, specific answer instead of declining.
                        A substantive answer to a negative question always earns
                        "contradicted": the expected answer asserts absence, the
                        system asserts presence, and those cannot both be true.
                        Offering a different, plausible facet of a (non-negative)
                        question is NOT a contradiction — that is "partial" instead.
                        IMPORTANT: a system answer that DENIES having a fact in its
                        prose ("the notes do not provide the exact text") while
                        ACTUALLY including that exact fact elsewhere in the same
                        answer (a quote, a code block, an aside) is "hedged", NOT
                        "contradicted" — the denial is about the system's own
                        awareness, not a false assertion about the world. Check the
                        WHOLE answer for the fact BEFORE reading the denial as a
                        contradiction; "contradicted" requires the system to assert
                        something actually false, not merely disclaim knowledge of a
                        fact it goes on to state.

   Key vs secondary facts: a fact is KEY when it is the thing the question actually
   asks for. Descriptive attributes of an entity the answer already identifies
   correctly (role, title, sponsor, affiliation, date-of-joining and similar colour)
   are SECONDARY. Omitting a secondary attribute does not affect `key_facts` at all.

   SOURCE EXCERPTS are a bounded sample used ONLY for the `fabrication` check
   below. Never let their absence override a `key_facts` reading that the
   SYSTEM ANSWER and EXPECTED ANSWER already settle between themselves — e.g. if
   the SYSTEM ANSWER contains the EXPECTED ANSWER's exact key fact verbatim,
   that is "conveyed" or "hedged" even when the SOURCE EXCERPTS window shown to
   you doesn't happen to contain it.

2. `fabrication` — true/false: did the system state an added name, body, date,
   venue, figure, or citation that appears NOWHERE in the EXPECTED ANSWER and
   NOWHERE in the SOURCE EXCERPTS? This is about INVENTED SECONDARY detail
   layered onto an otherwise identifiable answer (e.g. correct score + invented
   awarding body). It is independent of `key_facts` — a hedged or partial answer
   can still fabricate, and a fully-conveyed answer can still fabricate extra
   colour around it. When true, also return `fabricated_items`: a short list of
   the specific invented name/figure/venue/date strings (empty list if false).
   Do NOT set `fabrication=true` merely because a detail is absent from the
   SOURCE EXCERPTS window shown to you if it is corroborated by the EXPECTED
   ANSWER — the excerpts are a bounded sample, not the complete source.

Respond with ONLY a compact JSON object, no prose:
{"key_facts":"conveyed|hedged|partial|missing|contradicted",
 "fabrication":true|false,
 "fabricated_items":["<invented item>", "..."],
 "rationale":"<=20 words"}"""


_VALID_KEY_FACTS = ("conveyed", "hedged", "partial", "missing", "contradicted")


def compose_verdict(key_facts: str, fabrication: bool, *, negative: bool = False) -> str:
    """Pure composition of the v3 structured judgement into the legacy scalar
    verdict, per the §3A truth table with the owner's Q1 ruling applied:
    fabrication caps a verdict at "partial" (never auto-downgrades to "wrong"
    on its own). The one path to "wrong" via fabrication-adjacent behavior is
    `key_facts="contradicted"`, which the rubric routes a negative question's
    substantive-and-fabricated answer through directly (see rubric docstring),
    not through the `fabrication` flag.

    `negative` is the question's `negative: true` flag. It only matters for the
    "missing" key_facts branch: on a negative/absent question, a decline (no
    usable answer) asserts nothing false and is exactly the wanted behavior, so
    it scores "correct" rather than "missed" — unless the judge also flagged
    fabrication, which is a self-contradictory reading we still cap at
    "partial" rather than trust blindly.
    """
    kf = str(key_facts).lower().strip()
    if kf not in _VALID_KEY_FACTS:
        raise ValueError(f"unknown key_facts value: {key_facts!r}")
    if kf == "contradicted":
        return "wrong"
    if kf == "conveyed":
        return "partial" if fabrication else "correct"
    if kf == "hedged":
        return "partial"
    if kf == "partial":
        return "partial"
    # kf == "missing"
    if negative:
        return "partial" if fabrication else "correct"
    return "partial" if fabrication else "missed"


def _llm_models_reachable(base_url: str) -> bool:
    url = base_url.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(url, headers=_openai_headers())
        with urllib.request.urlopen(request, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# Substrings that mark a model id as NOT chat-capable. The golden judge once
# auto-picked a TTS model (data[0]) and every request 400'd; filter those out so
# "auto" lands on a real chat model.
_NON_CHAT_MODEL_MARKERS = (
    "tts",
    "voice",
    "embed",
    "whisper",
    "parakeet",
    "stt",
    "rerank",
    "clip",
)


def _is_chat_model(model_id: str) -> bool:
    mid = model_id.lower()
    return not any(marker in mid for marker in _NON_CHAT_MODEL_MARKERS)


def _model_params_billions(model_id: str) -> float:
    """Parse the parameter count (in billions) advertised in a model id.

    ``Qwen3.6-27B`` -> 27.0, ``Qwen3.5-0.8B`` -> 0.8, ``35B-A3B`` -> 35.0
    (total params, not the MoE active count). Returns 0.0 when no size token
    is present so unmarked ids sort last."""
    best = 0.0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[bB]\b", model_id):
        best = max(best, float(m.group(1)))
    return best


def _select_chat_model(base_url: str) -> str:
    """Pick the largest chat-capable model id the server advertises.

    ``data[0]`` is unsafe twice over: a server hosting a TTS/embedding model
    first would route every judge call to it and 400, and a tiny chat model
    listed first (e.g. a 0.8B) cannot reliably emit verdict JSON and produces
    only ``unparseable`` results. Filter to chat-capable ids, then prefer the
    one with the most parameters — the most capable judge available."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers=_openai_headers(),
    )
    with urllib.request.urlopen(request, timeout=5) as r:
        ids = [m["id"] for m in json.load(r).get("data", [])]
    chat = [mid for mid in ids if _is_chat_model(mid)]
    if not chat:
        raise RuntimeError(f"no chat-capable model at {base_url}/models; advertised: {ids}")
    return max(chat, key=_model_params_billions)


def _llm_chat(base_url: str, model: str, system: str, user: str, *, max_tokens: int) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    # Some Qwen3 endpoints serve a *thinking* variant. Left to itself it can spend
    # the whole token budget
    # emitting chain-of-thought *prose* (no <think> tags) and the JSON verdict
    # never appears within max_tokens -> ~84% "unparseable". Passing
    # chat_template_kwargs.enable_thinking=false through the template suppresses
    # the reasoning so it answers with the bare verdict JSON in ~29 tokens. The
    # flag MUST live under chat_template_kwargs — a top-level enable_thinking is
    # ignored by this endpoint (verified live). Harmless on non-thinking servers
    # that ignore the kwarg.
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers=_openai_headers(json_body=True),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


_VALID_VERDICTS = ("correct", "partial", "wrong", "missed")
_UNSUPPORTED_VERDICT = "unsupported"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_VERDICT_KV_RE = re.compile(
    r"""["']?verdict["']?\s*[:=]\s*["']?(correct|partial|wrong|missed)\b""",
    re.IGNORECASE,
)


def _parse_verdict(text: str) -> dict[str, Any]:
    """Extract a judge reply, robust to reasoning models.

    Rubric v3 asks for a structured object ``{key_facts, fabrication,
    fabricated_items, rationale}``; this returns that shape verbatim when
    present. For back-compat with rubric v1/v2 replies (and any judge output
    that degrades to the legacy shape), it also recognizes a bare
    ``{verdict, rationale}`` object and returns ``{"verdict": ..., "rationale":
    ...}`` unchanged — callers must check for "key_facts" vs "verdict" in the
    returned dict.

    A Qwen3 thinking judge can, even with reasoning suppressed, return a
    reply that carries stray prose, <think> blocks, or markdown fences.
    Strategy, most-precise-first:
      1. strip <think>…</think> reasoning blocks,
      2. prefer JSON inside a ```json fence,
      3. scan EVERY balanced {...} object and take the LAST one that parses to
         a recognized shape (the model's final answer, not a schema it echoed
         mid-thought), preferring the v3 structured shape over the legacy one
         when both appear,
      4. fall back to a tolerant `verdict: <label>` regex over the whole text.
    Only genuinely label-free replies become "unparseable"."""
    raw = (text or "").strip()
    if not raw:
        return {"verdict": "unparseable", "rationale": ""}
    cleaned = _THINK_RE.sub(" ", raw).strip()

    candidates: list[str] = []
    fenced = _FENCE_RE.findall(cleaned)
    candidates.extend(f for f in fenced if "{" in f)
    candidates.append(cleaned)

    last_structured: dict[str, Any] | None = None
    last_legacy: dict[str, Any] | None = None
    for blob in candidates:
        for m in _JSON_OBJ_RE.finditer(blob):
            try:
                obj = json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            kf = str(obj.get("key_facts", "")).lower().strip()
            if kf in _VALID_KEY_FACTS:
                items = obj.get("fabricated_items") or []
                if not isinstance(items, list):
                    items = [items]
                last_structured = {
                    "key_facts": kf,
                    "fabrication": bool(obj.get("fabrication", False)),
                    "fabricated_items": [str(x)[:200] for x in items][:20],
                    "rationale": str(obj.get("rationale", ""))[:200],
                }
                continue
            v = str(obj.get("verdict", "")).lower().strip()
            if v in _VALID_VERDICTS:
                last_legacy = {"verdict": v, "rationale": str(obj.get("rationale", ""))[:200]}
    if last_structured is not None:
        return last_structured
    if last_legacy is not None:
        return last_legacy

    # Tolerant fallback: a bare `verdict: wrong` anywhere (last match wins).
    matches = _VERDICT_KV_RE.findall(cleaned)
    if matches:
        return {"verdict": matches[-1].lower(), "rationale": cleaned[:200]}

    return {"verdict": "unparseable", "rationale": raw[:200]}


def _judge_batch_errors(q_doc: Any, responses: list[dict[str, Any]]) -> list[str]:
    errors = question_validation_errors(q_doc)
    questions = q_doc.get("questions") if isinstance(q_doc, dict) else []
    question_ids: list[str] = []
    if isinstance(questions, list):
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            qid = str(question.get("id") or "").strip()
            if qid:
                question_ids.append(qid)
            if not isinstance(question.get("question"), str) or not question["question"].strip():
                errors.append(f"{qid or f'index-{index}'}: question text is required")

    response_ids = [str(rec.get("id") or "").strip() for rec in responses]
    if not responses:
        errors.append("responses must contain at least one item")
    if any(not qid for qid in response_ids):
        errors.append("every response requires a non-empty id")
    if len(set(response_ids)) != len(response_ids):
        errors.append("response ids must be unique")
    unknown = sorted(set(response_ids) - set(question_ids))
    missing = sorted(set(question_ids) - set(response_ids))
    if unknown:
        errors.append("response ids missing from questions: " + ", ".join(unknown))
    if missing:
        errors.append("question ids missing from responses: " + ", ".join(missing))
    return errors


def judge_sidecar_errors(
    report: Any, *, expected_response_ids: list[str] | None = None
) -> list[str]:
    if not isinstance(report, dict):
        return ["judge report must be an object"]
    errors: list[str] = []
    if report.get("skipped") is not False:
        errors.append("judge report is skipped")
    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        errors.append("judge report requires verdicts")
        return errors
    verdict_ids: list[str] = []
    for index, verdict in enumerate(verdicts):
        if not isinstance(verdict, dict):
            errors.append(f"verdict at index {index} must be an object")
            continue
        qid = str(verdict.get("id") or "").strip()
        if not qid:
            errors.append(f"verdict at index {index} requires a non-empty id")
        else:
            verdict_ids.append(qid)
        value = str(verdict.get("verdict") or "").lower()
        if value == _UNSUPPORTED_VERDICT:
            if (
                verdict.get("unsupported_capability") != "valid_time_queries"
                or verdict.get("score_eligible") is not False
            ):
                errors.append(
                    f"{qid or f'index-{index}'}: unsupported verdict lacks its "
                    "valid_time_queries exclusion contract"
                )
        elif value not in _VALID_VERDICTS:
            errors.append(f"{qid or f'index-{index}'}: incomplete verdict {value or '<empty>'}")
        if "fabrication" in verdict and not isinstance(verdict.get("fabrication"), bool):
            errors.append(f"{qid or f'index-{index}'}: fabrication must be a bool when present")
        if "fabricated_items" in verdict and not isinstance(verdict.get("fabricated_items"), list):
            errors.append(
                f"{qid or f'index-{index}'}: fabricated_items must be a list when present"
            )
        if "key_facts" in verdict and verdict.get("key_facts") not in (*_VALID_KEY_FACTS, None):
            errors.append(
                f"{qid or f'index-{index}'}: unknown key_facts {verdict.get('key_facts')!r}"
            )
    if len(set(verdict_ids)) != len(verdict_ids):
        errors.append("verdict ids must be unique")
    if expected_response_ids is not None and verdict_ids != expected_response_ids:
        errors.append("verdict ids/order do not match responses")
    return errors


_EXCERPT_CAP_BYTES = 1200


def build_excerpt_block(
    inputs_root: Path | None, hits: list[dict[str, Any]], *, cap_bytes: int = _EXCERPT_CAP_BYTES
) -> str:
    """Build a bounded SOURCE EXCERPTS block for the judge's fabrication check.

    Gives the fabrication rule the evidence its own prompt references (plan-5
    §3B) instead of asking the model to test membership against node names it
    was never shown. Dedups by ``provenance.block_id`` before slicing so many
    Claim hits anchored to the same block cost one excerpt, not five, then caps
    each block's excerpt at ``cap_bytes`` (owner ruling Q2: 1200 bytes),
    centred on the cited byte span. Resolution goes through
    `_strict_input_source` (fails loud on a basename collision) rather than the
    permissive `_resolve_source` used by the provenance-hash gate. Tolerates
    hits with no byte range or an empty `context_spans` — those are simply
    skipped rather than raising, since not every hit is byte-anchored."""
    if inputs_root is None or not hits:
        return ""
    seen_blocks: set[str] = set()
    excerpts: list[str] = []
    for h in hits:
        prov = h.get("provenance") or {}
        block_id = str(prov.get("block_id") or "") or None
        path = prov.get("path") or ""
        bs, be = prov.get("byte_start"), prov.get("byte_end")
        if not path or bs is None or be is None:
            continue
        dedup_key = block_id or f"{path}:{bs}:{be}"
        if dedup_key in seen_blocks:
            continue
        seen_blocks.add(dedup_key)
        try:
            bs_i, be_i = int(bs), int(be)
        except (TypeError, ValueError):
            continue
        if be_i <= bs_i:
            continue
        try:
            src = _strict_input_source(inputs_root, path)
            raw = src.read_bytes()
        except (ValueError, OSError):
            continue
        span_len = be_i - bs_i
        if span_len >= cap_bytes:
            excerpt = raw[bs_i : bs_i + cap_bytes]
        else:
            # centre the cap on the cited span
            slack = cap_bytes - span_len
            pad_before = min(bs_i, slack // 2)
            start = max(0, bs_i - pad_before)
            end = min(len(raw), start + cap_bytes)
            excerpt = raw[start:end]
        text = excerpt.decode("utf-8", errors="replace")
        excerpts.append(f"- [{src.name}] …{text}…")
    if not excerpts:
        return ""
    return "\n".join(excerpts)


def cmd_judge(args: argparse.Namespace) -> int:
    base_url = args.base_url
    model = args.model
    inputs_root = Path(args.inputs) if getattr(args, "inputs", None) else None
    q_doc = load_yaml(Path(args.questions))
    responses = read_responses(Path(args.responses))
    validation_errors = _judge_batch_errors(q_doc, responses)
    if validation_errors:
        print(
            json.dumps({"error": "invalid questions", "details": validation_errors}),
            file=sys.stderr,
        )
        return 1
    questions = {q["id"]: q for q in (q_doc.get("questions") or [])}
    requires_judge = any(
        not questions.get(str(rec.get("id") or ""), {}).get("unsupported_capability")
        for rec in responses
    )
    reachable = _llm_models_reachable(base_url) if requires_judge else True
    if not reachable:
        report = {
            "skipped": True,
            "reason": f"no OpenAI-compatible LLM at {base_url}/models",
            "base_url": base_url,
            "verdicts": [],
        }
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"skipped": True, "reason": report["reason"]}, indent=2))
        return 75

    # discover a chat-capable model name if caller passed "auto". Fail loud rather
    # than guess "local-model": a wrong/absent model 400s every call and the run
    # silently produces 14× unparseable verdicts (the original golden-run bug).
    if requires_judge and model == "auto":
        model = _select_chat_model(base_url)

    verdicts = []
    tally = {
        "correct": 0,
        "partial": 0,
        "wrong": 0,
        "missed": 0,
        "unsupported": 0,
        "unparseable": 0,
    }
    for rec in responses:
        qid = rec.get("id", "?")
        q = questions.get(qid, {})
        unsupported_capability = q.get("unsupported_capability")
        if unsupported_capability:
            v = {
                "verdict": _UNSUPPORTED_VERDICT,
                "rationale": (
                    "ADR 0040 preserves temporal evidence but does not support "
                    "valid-time query semantics"
                ),
            }
            tally[_UNSUPPORTED_VERDICT] += 1
            verdicts.append(
                {
                    "id": qid,
                    "tier": rec.get("tier") or q.get("tier"),
                    "verdict": v["verdict"],
                    "rationale": v["rationale"],
                    "score_eligible": False,
                    "unsupported_capability": unsupported_capability,
                }
            )
            continue
        question = q.get("question", rec.get("question", ""))
        expected = q.get("expected_answer", "")
        negative = bool(q.get("negative"))
        sys_text = (rec.get("ask") or {}).get("text", "") or ""
        hits = ((rec.get("recall") or {}).get("hits") or [])[:5]
        snippets = []
        for h in hits:
            node = h.get("node") or {}
            snippets.append(f"- [{node.get('type', '')}] {node.get('name', '')}")
        snip_txt = "\n".join(snippets) if snippets else "(none)"
        excerpt_txt = build_excerpt_block(inputs_root, hits)

        user = (
            f"QUESTION:\n{question}\n\n"
            f"EXPECTED ANSWER:\n{expected}\n\n"
            f"SYSTEM ANSWER:\n{sys_text or '(empty)'}\n\n"
            f"TOP RETRIEVED NODES:\n{snip_txt}\n\n"
            f"SOURCE EXCERPTS:\n{excerpt_txt or '(none available)'}\n"
        )
        try:
            raw = _llm_chat(base_url, model, _RUBRIC, user, max_tokens=args.max_tokens)
            parsed = _parse_verdict(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
            parsed = {"verdict": "unparseable", "rationale": f"llm error: {exc}"}

        if "key_facts" in parsed:
            verdict_value = compose_verdict(
                parsed["key_facts"], parsed["fabrication"], negative=negative
            )
            entry = {
                "id": qid,
                "tier": rec.get("tier") or q.get("tier"),
                "verdict": verdict_value,
                "key_facts": parsed["key_facts"],
                "fabrication": parsed["fabrication"],
                "fabricated_items": parsed["fabricated_items"],
                "rationale": parsed["rationale"],
                "score_eligible": True,
            }
        else:
            # legacy single-field reply (v1/v2-shaped) or unparseable — no
            # structured fabrication data to compose from.
            verdict_value = parsed["verdict"]
            entry = {
                "id": qid,
                "tier": rec.get("tier") or q.get("tier"),
                "verdict": verdict_value,
                "rationale": parsed.get("rationale", ""),
                "score_eligible": True,
            }
        tally[verdict_value] = tally.get(verdict_value, 0) + 1
        verdicts.append(entry)

    report = {
        "skipped": False,
        "base_url": base_url,
        "model": model,
        "tally": tally,
        "verdicts": verdicts,
    }
    report_errors = judge_sidecar_errors(
        report, expected_response_ids=[str(rec["id"]) for rec in responses]
    )
    report["complete"] = not report_errors
    report["errors"] = report_errors
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"skipped": False, "model": model, "tally": tally}, indent=2))
    return 0 if not report_errors else 2


def cmd_validate_judge(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.judge).read_text(encoding="utf-8"))
    responses = read_responses(Path(args.responses))
    errors = judge_sidecar_errors(
        report, expected_response_ids=[str(rec.get("id") or "") for rec in responses]
    )
    if errors:
        print(json.dumps({"valid": False, "errors": errors}), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "verdicts": len(report["verdicts"])}))
    return 0


# ── deterministic floor (the P0 CI trust anchor) ───────────────────────────────
#
# A NO-LLM, NO-DAEMON, NO-VAULT correctness gate built ONLY from signals the
# determinism spike proved bit-reproducible: provenance byte-hashing of source
# files. For every question's gold_targets/distractors quote we re-slice the quote
# out of inputs/<source_path>, assert it is present and UNIQUE, and record its
# byte offsets + sha256. This is a pure function of the committed dataset files,
# so two runs from scratch are byte-identical (proven by diffing two outputs).
#
# WHY recall is NOT gated here (measured, not assumed):
#   * The no-LLM ingest path (server POST /add -> Vault.add -> ingest_document)
#     creates Blocks with exact byte anchors + deterministic structural Claims
#     (has_tag/has_heading/links_to) but writes ZERO embeddings (embeddings come
#     only from the LLM remember()/reembed path). Verified: a fresh no-LLM vault of
#     synthetic-ci has 0 nodes carrying an embedding -> vector recall is dead.
#   * The shipped /api/v1/recall (untyped) DROPS Block nodes, so a daemon-routed
#     recall on a no-LLM vault returns only structural Claims, not answer content.
#   * type=Block recall does work (BM25 lexical leg, and it IS deterministic), but
#     synthetic-ci's 14 tiny files each fit one 12k Block, so block-recall ==
#     file-recall and hard-recall@10 over 14 files saturates at 100% trivially
#     (non-discriminating -> useless as a regression gate).
# => Meaningful claim/block recall needs the LLM-ingested vault and is therefore
#    LAPTOP-ONLY (run-golden.sh with an explicit live model). The CI floor is
#    provenance-only.
#
def _canonical_quote(quote: str) -> str:
    """Return already-parsed quote text without a destructive second decode."""

    return quote


def _locate_quote(raw: bytes, quote: str) -> tuple[int, int, int]:
    """Return (byte_start, byte_end, occurrences) for ``quote`` in ``raw``.

    occurrences == 0 -> absent; > 1 -> ambiguous offset (dataset defect). The
    offsets are for the FIRST occurrence (only meaningful when occurrences == 1)."""
    qb = _canonical_quote(quote).encode("utf-8")
    occurrences = raw.count(qb)
    idx = raw.find(qb)
    if idx < 0:
        return -1, -1, 0
    return idx, idx + len(qb), occurrences


def _floor_targets(inputs_root: Path, q: dict[str, Any], field: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in q.get(field) or []:
        sp = str(t.get("source_path") or "")
        quote = str(t.get("quote") or "")
        src = inputs_root / sp
        rec: dict[str, Any] = {"source_path": sp, "quote_len": len(_canonical_quote(quote))}
        if not src.exists():
            rec.update(present=False, unique=False, reason=f"source missing: {sp}")
            out.append(rec)
            continue
        bs, be, occ = _locate_quote(src.read_bytes(), quote)
        if occ == 0:
            rec.update(present=False, unique=False, reason="quote not found verbatim")
        elif occ > 1:
            rec.update(
                present=True,
                unique=False,
                occurrences=occ,
                reason=f"quote occurs {occ}x (ambiguous offset)",
            )
        else:
            digest = hashlib.sha256(src.read_bytes()[bs:be]).hexdigest()
            rec.update(
                present=True,
                unique=True,
                byte_start=bs,
                byte_end=be,
                content_hash=f"sha256:{digest}",
            )
        out.append(rec)
    return out


def cmd_floor(args: argparse.Namespace) -> int:
    """DETERMINISTIC FLOOR — provenance byte-hash gate over the dataset's quotes.

    Pure function of inputs/ + questions.yaml; no vault, no daemon, no LLM. Emits a
    byte-stable floor_report.json. Exit 0 = pass, 1 = regression vs --baseline or a
    hard gate failure (a gold quote that does not resolve uniquely)."""
    dataset_dir = Path(args.dataset).resolve()
    inputs_root = dataset_dir / "inputs"
    selected_questions = getattr(args, "questions", None)
    questions_path = (
        Path(selected_questions).resolve() if selected_questions else dataset_dir / "questions.yaml"
    )
    if not inputs_root.is_dir():
        print(json.dumps({"error": f"no inputs/: {inputs_root}"}), file=sys.stderr)
        return 1
    if not questions_path.is_file():
        print(json.dumps({"error": f"no questions.yaml: {questions_path}"}), file=sys.stderr)
        return 1

    q_doc = load_yaml(questions_path)
    validation_errors = question_validation_errors(q_doc)
    raw_questions = q_doc.get("questions") if isinstance(q_doc, dict) else []
    questions = (
        sorted(
            (q for q in raw_questions if isinstance(q, dict)),
            key=lambda q: str(q.get("id")),
        )
        if isinstance(raw_questions, list)
        else []
    )

    per_q: list[dict[str, Any]] = []
    gold_total = gold_ok = dist_total = dist_ok = 0
    failures: list[str] = list(validation_errors)
    for q in questions:
        qid = str(q.get("id"))
        gold = _floor_targets(inputs_root, q, "gold_targets")
        dist = _floor_targets(inputs_root, q, "distractors")
        gold_total += len(gold)
        dist_total += len(dist)
        for g in gold:
            if g.get("present") and g.get("unique"):
                gold_ok += 1
            else:
                failures.append(f"{qid} gold {g['source_path']}: {g.get('reason')}")
        for d in dist:
            if d.get("present") and d.get("unique"):
                dist_ok += 1
            else:
                # A distractor that fails to resolve is a dataset-hygiene warning,
                # not a correctness regression — record it but don't fail the gate.
                failures.append(f"[warn] {qid} distractor {d['source_path']}: {d.get('reason')}")
        per_q.append(
            {
                "id": qid,
                "category": q.get("category"),
                "tier": q.get("tier"),
                "negative_control": bool(q.get("negative_control")),
                "gold_targets": gold,
                "distractors": dist,
            }
        )

    # The HARD gate: every gold quote must resolve uniquely with a verifiable hash.
    # (Negative-control questions legitimately have no gold targets -> they neither
    # add to gold_total nor can they fail.) Distractor warnings never gate.
    hard_failures = [f for f in failures if not f.startswith("[warn]")]
    provenance_gate_pass = not hard_failures and gold_ok == gold_total

    report = {
        "dataset": dataset_dir.name,
        "inputs_root": str(inputs_root),
        "questions": len(questions),
        "gold_targets_total": gold_total,
        "gold_targets_resolved": gold_ok,
        "distractors_total": dist_total,
        "distractors_resolved": dist_ok,
        "provenance_gate_pass": provenance_gate_pass,
        "question_validation_pass": not validation_errors,
        "failures": sorted(failures),
        "recall_gated": False,
        "recall_note": (
            "claim/block recall is LAPTOP-ONLY (needs the LLM-ingested vault). The "
            "no-LLM ingest path writes zero embeddings and synthetic-ci's 14 "
            "single-block files make block-recall@k saturate trivially, so it is "
            "not a valid CI regression gate. See run-golden.sh for the LLM path."
        ),
        "per_question": per_q,
    }

    regression = None
    if getattr(args, "baseline", None):
        bpath = Path(args.baseline)
        if not bpath.exists():
            print(json.dumps({"error": f"baseline not found: {bpath}"}), file=sys.stderr)
            return 1
        baseline = json.loads(bpath.read_text(encoding="utf-8"))
        regression = _floor_regressions(baseline, report)
        report["baseline_comparison"] = regression

    # Byte-stable output: sort_keys + trailing newline. Two runs from scratch over
    # the same committed files produce identical bytes (the determinism proof).
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "dataset": report["dataset"],
                "provenance_gate_pass": report["provenance_gate_pass"],
                "gold_targets_resolved": f"{gold_ok}/{gold_total}",
                "distractors_resolved": f"{dist_ok}/{dist_total}",
                "regressions": (regression or {}).get("regressions") if regression else None,
            },
            indent=2,
            sort_keys=True,
        )
    )

    if not provenance_gate_pass:
        return 1
    if regression and regression.get("regressions"):
        return 1
    return 0


def _floor_regressions(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare two floor reports; a regression is a gold quote that USED to resolve
    to a hash and now resolves to a different hash or no longer resolves. New gold
    targets resolving cleanly are not regressions; they're additions."""

    def gold_hashes(report: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for q in report.get("per_question", []):
            for g in q.get("gold_targets", []):
                if g.get("present") and g.get("unique"):
                    out[f"{q['id']}::{g['source_path']}::{g.get('byte_start')}"] = g["content_hash"]
        return out

    base = gold_hashes(baseline)
    cur = gold_hashes(current)
    regressions: list[str] = []
    for key, h in base.items():
        if key not in cur:
            regressions.append(f"gold no longer resolves: {key}")
        elif cur[key] != h:
            regressions.append(f"gold hash changed: {key} {h[:18]} -> {cur[key][:18]}")
    if not current.get("provenance_gate_pass") and baseline.get("provenance_gate_pass"):
        regressions.append("provenance_gate flipped pass -> fail")
    return {
        "baseline_gold_resolved": len(base),
        "current_gold_resolved": len(cur),
        "added": sorted(set(cur) - set(base)),
        "regressions": sorted(regressions),
    }


def cmd_yaml2json(args: argparse.Namespace) -> int:
    """Emit a YAML file as JSON on stdout (so the shell harness can read it)."""
    doc = load_yaml(Path(args.file))
    print(json.dumps(doc))
    return 0


def deterministic_sidecar_errors(
    payload: Any, *, expected_response_ids: list[str], expected_k: int | None
) -> list[str]:
    """Require complete provenance evidence before a live report can exist."""

    if not isinstance(payload, dict):
        return ["deterministic sidecar must be an object"]
    errors: list[str] = []
    if not expected_response_ids:
        errors.append("responses must contain at least one question")
    if not isinstance(payload.get("provenance_gate_pass"), bool):
        errors.append("provenance_gate_pass must be boolean")
    total_failures = payload.get("total_failures")
    if (
        isinstance(total_failures, bool)
        or not isinstance(total_failures, int)
        or total_failures < 0
    ):
        errors.append("total_failures must be a non-negative integer")
    if not isinstance(payload.get("failures"), list):
        errors.append("failures must be a list")
    if payload.get("questions") != len(expected_response_ids):
        errors.append(
            "questions count does not match responses: "
            f"{payload.get('questions')} != {len(expected_response_ids)}"
        )
    if expected_k is None:
        errors.append("responses must all declare the same positive integer k")
    elif payload.get("k") != expected_k:
        errors.append(
            f"deterministic k does not match responses: {payload.get('k')} != {expected_k}"
        )

    rows = payload.get("per_question")
    if not isinstance(rows, list):
        errors.append("per_question must be a list")
        return errors
    sidecar_ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
    if len(sidecar_ids) != len(rows) or any(not qid for qid in sidecar_ids):
        errors.append("every per_question row requires a non-empty id")
    if len(set(sidecar_ids)) != len(sidecar_ids):
        errors.append("per_question ids must be unique")
    if sidecar_ids != expected_response_ids:
        errors.append("per_question ids/order do not match responses")
    return errors


def cmd_report(args: argparse.Namespace) -> int:
    """Merge dataset meta + deterministic + judge + graph snapshot into report.json."""

    def _load(path: str | None) -> Any:
        if not path:
            return None
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    if not args.semantic_quality:
        print("report refused missing semantic-quality sidecar", file=sys.stderr)
        return 1
    deterministic = _load(args.deterministic)
    judge = _load(args.judge)
    if args.judge and judge is None:
        print("report refused missing judge sidecar", file=sys.stderr)
        return 1
    judge = judge or {"skipped": True, "reason": "judge not run"}
    node_types = _load(args.node_types) or {}
    if Path(args.responses).exists():
        responses, responses_sha256 = read_responses_with_sha256(Path(args.responses))
    else:
        responses, responses_sha256 = [], hashlib.sha256(b"").hexdigest()
    response_ids = [str(rec.get("id") or "") for rec in responses]
    if args.judge:
        judge_errors = judge_sidecar_errors(judge, expected_response_ids=response_ids)
        if judge_errors:
            print(
                "report refused incomplete judge sidecar: " + "; ".join(judge_errors),
                file=sys.stderr,
            )
            return 1
    response_ks = [rec.get("k") for rec in responses]
    expected_k = None
    if (
        response_ks
        and all(isinstance(k, int) and not isinstance(k, bool) and k > 0 for k in response_ks)
        and len(set(response_ks)) == 1
    ):
        expected_k = response_ks[0]
    deterministic_errors = deterministic_sidecar_errors(
        deterministic, expected_response_ids=response_ids, expected_k=expected_k
    )
    if deterministic_errors:
        print(
            "report refused incomplete deterministic sidecar: " + "; ".join(deterministic_errors),
            file=sys.stderr,
        )
        return 1
    semantic_capture = _load(args.semantic_quality) or {}
    errors = semantic_quality_capture_errors(
        semantic_capture,
        expected_samples=len(responses),
        expected_responses_sha256=responses_sha256,
    )
    if errors:
        print(
            "report refused incomplete semantic-quality sidecar: " + "; ".join(errors),
            file=sys.stderr,
        )
        return 1
    semantic_quality = semantic_capture.get("semantic_quality")
    floor_laptop = _load(getattr(args, "floor_laptop", None))
    if getattr(args, "floor_laptop", None):
        floor_errors = _floor_metrics.floor_laptop_report_errors(
            floor_laptop, expected_response_ids=response_ids
        )
        if floor_errors:
            print(
                "report refused incomplete floor-laptop sidecar: " + "; ".join(floor_errors),
                file=sys.stderr,
            )
            return 1

    # per-question merge: tier, provenance_ok, verdict
    prov_by_id = {q["id"]: q for q in deterministic["per_question"]}
    verdict_by_id = {v["id"]: v for v in judge.get("verdicts", [])}
    merged = []
    for rec in responses:
        qid = rec.get("id")
        prov = prov_by_id.get(qid, {})
        verd = verdict_by_id.get(qid, {})
        merged.append(
            {
                "id": qid,
                "tier": rec.get("tier"),
                "question": rec.get("question"),
                "provenance_ok": prov.get("provenance_ok"),
                "hits_checked": prov.get("hits_checked"),
                "verdict": verd.get("verdict", "skipped"),
                "rationale": verd.get("rationale", ""),
            }
        )

    report = {
        "dataset": args.dataset,
        "run_timestamp": args.timestamp,
        "questions": len(responses),
        "k": deterministic["k"],
        "provenance_gate_pass": deterministic.get("provenance_gate_pass"),
        "provenance_failures": deterministic.get("total_failures"),
        "judge_skipped": judge.get("skipped", True),
        "judge_tally": judge.get("tally", {}),
        "judge_model": judge.get("model"),
        "graph_node_types": node_types.get("types", []),
        "semantic_quality": semantic_quality,
        "semantic_quality_capture": semantic_capture.get("capture"),
        "floor_laptop": floor_laptop,
        "per_question": merged,
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "dataset": report["dataset"],
                "questions": report["questions"],
                "provenance_gate_pass": report["provenance_gate_pass"],
                "judge_tally": report["judge_tally"],
                "judge_skipped": report["judge_skipped"],
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Golden harness grading helper (black-box; no Okto Neuron imports)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    py = sub.add_parser("yaml2json")
    py.add_argument("--file", required=True)
    py.set_defaults(func=cmd_yaml2json)

    pv = sub.add_parser(
        "validate-questions",
        help="reject positive questions without at least one gold target",
    )
    pv.add_argument("--questions", required=True)
    pv.set_defaults(func=cmd_validate_questions)

    pvi = sub.add_parser(
        "validate-inputs",
        help="reject paths that flatten to the same source filename during HTTP ingest",
    )
    pvi.add_argument("--inputs", required=True)
    pvi.set_defaults(func=cmd_validate_inputs)

    pr = sub.add_parser("report")
    pr.add_argument("--dataset", required=True)
    pr.add_argument("--timestamp", required=True)
    pr.add_argument("--responses", required=True)
    pr.add_argument("--deterministic", required=True)
    pr.add_argument("--judge", default=None)
    pr.add_argument("--node-types", dest="node_types", default=None)
    pr.add_argument("--semantic-quality", dest="semantic_quality", required=True)
    pr.add_argument("--floor-laptop", dest="floor_laptop", default=None)
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_report)

    psq = sub.add_parser(
        "semantic-quality",
        help="capture server-owned semantic quality with every recall_cost.v1 sample",
    )
    psq.add_argument("--endpoint", required=True)
    psq.add_argument("--responses", required=True)
    psq.add_argument("--out", required=True)
    psq.add_argument("--timeout-s", dest="timeout_s", type=float, default=600.0)
    psq.set_defaults(func=cmd_semantic_quality)

    pvsq = sub.add_parser(
        "validate-semantic-quality",
        help="verify a retained semantic sidecar against retained responses",
    )
    pvsq.add_argument("--responses", required=True)
    pvsq.add_argument("--semantic-quality", dest="semantic_quality", required=True)
    pvsq.set_defaults(func=cmd_validate_semantic_quality)

    pp = sub.add_parser("provenance")
    pp.add_argument("--inputs", required=True)
    pp.add_argument("--responses", required=True)
    pp.add_argument("--out", required=True)
    pp.add_argument(
        "--questions", default=None, help="questions.yaml — enables gold-span metrics when present"
    )
    pp.set_defaults(func=cmd_provenance)

    pbg = sub.add_parser(
        "byte-grounding",
        help="materialize ADR 0040 evidence from cited Claim spans re-sliced from inputs",
    )
    pbg.add_argument("--corpus-id", dest="corpus_id", required=True)
    pbg.add_argument("--inputs", required=True)
    pbg.add_argument("--responses", required=True)
    pbg.add_argument("--manifest", required=True)
    pbg.add_argument("--semantic-quality", dest="semantic_quality", required=True)
    pbg.add_argument("--out", required=True)
    pbg.set_defaults(func=cmd_byte_grounding)

    pj = sub.add_parser("judge")
    pj.add_argument("--questions", required=True)
    pj.add_argument("--responses", required=True)
    pj.add_argument("--out", required=True)
    pj.add_argument(
        "--base-url",
        default=_LIVE_LLM_BASE_URL,
        required=not bool(_LIVE_LLM_BASE_URL),
        help="OpenAI-compatible endpoint (or set OKTO_NEURON_LLM_BASE_URL)",
    )
    pj.add_argument("--model", default=_LIVE_JUDGE_MODEL)
    pj.add_argument("--max-tokens", dest="max_tokens", type=int, default=2000)
    pj.add_argument(
        "--inputs",
        default=None,
        help="dataset inputs/ trust copy root, for bounded SOURCE EXCERPTS injection "
        "(fabrication evidence); omitted -> judge stays text-blind as before",
    )
    pj.set_defaults(func=cmd_judge)

    pvj = sub.add_parser("validate-judge", help="fail unless a scripted judge report is complete")
    pvj.add_argument("--judge", required=True)
    pvj.add_argument("--responses", required=True)
    pvj.set_defaults(func=cmd_validate_judge)

    pf = sub.add_parser(
        "floor",
        help="deterministic provenance byte-hash gate (no LLM/daemon/vault) — the P0 CI anchor",
    )
    pf.add_argument("dataset", help="dataset dir, e.g. tests/golden/datasets/synthetic-ci")
    pf.add_argument(
        "--questions",
        default=None,
        help="explicit questions.yaml to validate against the dataset inputs",
    )
    pf.add_argument("--out", default="floor_report.json")
    pf.add_argument(
        "--baseline",
        default=None,
        help="optional frozen floor_report.json; exit 1 on any gold-hash regression",
    )
    pf.set_defaults(func=cmd_floor)

    # RUN MANIFEST — the reproducibility pin (logic lives in manifest.py; wired
    # here so the eval-gate flow can emit it next to floor_report.json via the
    # same judge.py entry point that produces the floor). Module imported at top.
    pm = sub.add_parser(
        "manifest",
        help="emit a run-manifest.json pinning corpus-hash + embedder + git rev + "
        "model/retrieval knobs for reproducible, comparable runs",
    )
    _manifest.build_emit_parser(pm)
    pm.set_defaults(func=_manifest.cmd_emit)

    pma = sub.add_parser(
        "manifest-assert-arms",
        help="assert two run-manifest.json arms are comparable (all pinned fields "
        "equal except the declared free_variable; corpus hashes MUST match)",
    )
    _manifest.build_assert_arms_parser(pma)
    pma.set_defaults(func=_manifest.cmd_assert_arms)

    pid = sub.add_parser(
        "assert-endpoint-dataset",
        help="fail unless a populated endpoint matches the selected dataset bytes",
    )
    _manifest.build_assert_endpoint_dataset_parser(pid)
    pid.set_defaults(func=_manifest.cmd_assert_endpoint_dataset)

    # SCORECARD — paired A/B significance + power (McNemar exact + bootstrap CI +
    # MDE/required-N + the 5-condition REAL rule). Logic lives in scorecard.py;
    # wired here so two arms can be judged via the same judge.py entry point that
    # produces the floor/manifest (mirrors `floor` and `manifest`). Imported at top.
    psc = sub.add_parser(
        "scorecard",
        help="paired A/B significance + power scorecard for two arms' per-question "
        "verdicts (McNemar exact, bootstrap 95%% CI, MDE, required-N, 5-condition rule)",
    )
    _scorecard.build_scorecard_parser(psc)
    psc.set_defaults(func=_scorecard.cmd_scorecard)

    # SCORECARD-MULTISEED — Deliverable C: run the comparison N times (N grader
    # runs via --runs, or N bootstrap seeds via --a/--b/--seeds) and report the
    # per-metric variance band as median[IQR], plus an escalate-vote tally. Same
    # scorecard stats core, just aggregated across seeds. Imported at top.
    pmss = sub.add_parser(
        "scorecard-multiseed",
        help="multi-seed scorecard: per-metric median[IQR] variance band across N "
        "grader runs / bootstrap seeds, with an escalate-vote tally",
    )
    _scorecard.build_multiseed_parser(pmss)
    pmss.set_defaults(func=_scorecard.cmd_multiseed)

    # AB-RUNSET — GN-3 multi-run A/B significance harness.  LAPTOP-ONLY fleet
    # instrument (NOT wired into eval-gate CI / recall_floor).  Runs the same
    # block-vs-subgraph pair N≥2 times, grades each with compare_arms, and
    # aggregates via multiseed_bands into median[IQR] + escalate/real votes.
    # Logic lives in scorecard.py (mirrors scorecard / scorecard-multiseed).
    pab = sub.add_parser(
        "ab-runset",
        help="GN-3: multi-run A/B significance harness (laptop-only, NOT a CI gate) — "
        "N pre-captured block/subgraph arm pairs, graded and aggregated "
        "into median[IQR] + escalate_votes/real_votes",
    )
    _scorecard.build_ab_runset_parser(pab)
    pab.set_defaults(func=_scorecard.cmd_ab_runset)

    # SWEEP — model x knob comparative grid (P3). Generalizes the two-arm runner
    # into an N-cell matrix and emits a comparative table reusing scorecard's
    # stats vs a baseline cell. LAPTOP-ONLY when cells execute (hits the LLM);
    # logic lives in sweep.py (mirrors scorecard / manifest). Imported at top.
    psw = sub.add_parser(
        "sweep",
        help="run/grade a model x knob matrix (>=2 cells) and emit a comparative "
        "scorecard table vs a baseline cell (laptop-only when cells execute)",
    )
    _sweep.build_sweep_parser(psw)
    psw.set_defaults(func=_sweep.cmd_run)

    # PANEL — N-judge, disjoint-family, 2/3-vote grading with bias controls, 0-2
    # axes collapsed to the 6-class taxonomy, Fleiss inter-judge kappa, and the
    # ASK×RECALL matrix. LAPTOP-ONLY (hits the judges); the panel is NOT yet
    # authoritative — it's gated on the human kappa (kappa/). Logic lives in
    # panel.py (mirrors scorecard / manifest / sweep). Imported at top.
    ppn = sub.add_parser(
        "panel",
        help="run an N-judge disjoint-family 2/3-vote panel (0-2 axes -> 6-class, "
        "Fleiss kappa, no-self-grading + verbosity + position-swap controls, "
        "ASK×RECALL matrix); laptop-only, NOT yet authoritative (human-kappa gated)",
    )
    _panel.build_panel_parser(ppn)

    def _panel_entry(a: argparse.Namespace) -> int:
        _panel._resolve_qwen_auto(a)
        return _panel.cmd_panel(a)

    ppn.set_defaults(func=_panel_entry)

    # FLOOR-LAPTOP — the two requirement-(1) correctness-floor metrics that need
    # the live LLM-ingested vault and so cannot live in the deterministic CI floor:
    # (i) citation byte-verification (re-slice /ask citation Block bytes vs
    # content_hash) and (ii) extraction-completeness (a bound gold target has a
    # live Claim anchored to its Block). LAPTOP-ONLY, reported SEPARATELY from the
    # CI provenance gate; deterministic byte-hash + graph-membership, independent
    # of the LLM judge (which stays non-authoritative). Module imported at top.
    pfl = sub.add_parser(
        "floor-laptop",
        help="laptop-only correctness floor: /ask citation byte-verification + "
        "extraction-completeness vs the live vault (separate from the CI "
        "provenance gate; deterministic, not an LLM judgement)",
    )
    _floor_metrics.build_floor_laptop_parser(pfl)
    pfl.set_defaults(func=_floor_metrics.cmd_floor_laptop)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
