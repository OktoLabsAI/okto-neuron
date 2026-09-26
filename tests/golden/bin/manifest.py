#!/usr/bin/env python3
"""RUN MANIFEST — the reproducibility pin for the Okto Neuron golden eval framework.

Zero marginalia-source imports (black-box contract, same rule as
judge.py). Endpoint-mode runs read the effective provider configuration through
the public API; sealed deterministic runs retain their local embedder fallback.

A run-manifest.json pins everything that determines what a golden eval run
*measured*, so that:

  (a) a single run is reproducible (you can recreate the exact corpus + embedder
      + code revision + model/retrieval settings), and
  (b) two arms of a comparison are provably apples-to-apples — they must agree on
      every pinned field EXCEPT one declared `free_variable`, and they must share
      an identical corpus hash. This is the parity-leak guard: in a prior field
      eval one arm's vault silently carried 2 extra docs, so the arms were never
      comparable. `assert-arms` makes that class of drift a hard, named failure.

Pinned (the comparable surface):
  * corpus.content_hash  — sha256 over the dataset's inputs/ files, sorted by
    POSIX relpath; the running hash absorbs (relpath-bytes, NUL, file-bytes, NUL)
    for each file so a rename OR a content edit OR an add/remove changes it.
  * embedder — the effective endpoint provider/model/dimension for live runs, or
    the sealed local embedder id/version when no endpoint is involved.
  * code.{git_rev,dirty,patch_sha256} — `git rev-parse HEAD` + a dirty flag from
    `git status --porcelain` (non-empty => dirty) + sha256 of the working-tree diff
    (`git diff HEAD`) so the ACTUAL patch on top of the rev is pinned, not just a
    bool. Clean tree => the stable sentinel "clean".
  * model.{answerer_model,judge_model,answerer_quant,judge_quant,answerer_temp,
    judge_temp,answerer_seed,judge_seed} — for laptop ask/judge runs; accepted via
    args/env, default null. quant is parsed from each model id (e.g. q4_K_M, fp16),
    "unknown" when the id encodes none.
  * retrieval.{k,query_neighbors} — k from questions.settings.k;
    query_neighbors from OKTO_NEURON_QUERY_NEIGHBORS (default 0).
  * consolidation — the effective semantic-stage and curation policy used to
    materialize a live graph.
  * execution — extraction and embedding concurrency/batching knobs. They are
    not semantic variables, but unequal values invalidate construction-cost
    comparisons.
  * ingest — the effective chunking policy used for graph construction.
  * free_variable — exactly one string naming the field allowed to differ.

Separated (NOT compared by assert-arms):
  * run_meta.{generated_at, tool} — wall-clock timestamp + tool tag. Lives in its
    own block precisely so it can be excluded from the parity assertion.

Usage:
  manifest.py emit <dataset> [--free-variable FIELD] [--out run-manifest.json]
                  [--answerer-model M] [--judge-model M]
                  [--answerer-temp T] [--judge-temp T]
                  [--answerer-seed S] [--judge-seed S]
  manifest.py assert-arms <manifestA.json> <manifestB.json>

Also wired into judge.py as `judge.py manifest <dataset> ...` (mirrors `floor`)
and `judge.py manifest-assert-arms <A> <B>`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from golden_yaml import GoldenYamlError, load_yaml

# Manifest format version — bump if the pinned-field set changes shape so stale
# manifests can be detected rather than silently mis-compared. Version 5 pins
# construction policy/execution and supports explicitly isolated construction
# arms without treating their necessarily different vault selectors as leakage.
MANIFEST_VERSION = 5

# Sealed deterministic runs use the local embedder. Live endpoint runs never use
# this fallback; they pin the public /api/v1/config response instead.
EMBEDDER_ID = "fastembed"


# ── corpus content hash ─────────────────────────────────────────────────────────


def corpus_content_hash(inputs_root: Path) -> tuple[str, int, int]:
    """sha256 over every file under inputs_root, sorted by POSIX relpath.

    The digest absorbs, per file in sorted order:
        relpath-bytes  +  b"\\x00"  +  file-bytes  +  b"\\x00"
    so a rename, a content edit, or an add/remove all move the hash. Returns
    (``sha256:<hex>``, file_count, total_bytes). Symlinks are followed via the
    default Path.read_bytes; the dataset inputs/ are plain regular files.
    """
    files = sorted(
        (p for p in inputs_root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(inputs_root).as_posix(),
    )
    h = hashlib.sha256()
    total_bytes = 0
    for p in files:
        rel = p.relative_to(inputs_root).as_posix()
        data = p.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(data)
        h.update(b"\x00")
        total_bytes += len(data)
    return f"sha256:{h.hexdigest()}", len(files), total_bytes


_INGESTIBLE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})


def dataset_content_identity(inputs_root: Path) -> dict[str, Any]:
    """Path-independent identity for the files the golden harness ingests.

    Endpoint uploads may normalize source filenames, so the endpoint reuse guard
    compares the multiset of source byte hashes and lengths rather than paths.
    Duplicate files remain visible because every entry participates in the
    sorted digest.
    """

    files = sorted(
        (
            p
            for p in inputs_root.rglob("*")
            if p.is_file() and p.suffix.lower() in _INGESTIBLE_SUFFIXES
        ),
        key=lambda p: p.relative_to(inputs_root).as_posix(),
    )
    entries = []
    total_bytes = 0
    for path in files:
        data = path.read_bytes()
        entries.append({"sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)})
        total_bytes += len(data)
    entries.sort(key=lambda item: (item["sha256"], item["byte_length"]))
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\x00")
        digest.update(str(entry["byte_length"]).encode("ascii"))
        digest.update(b"\x00")
    return {
        "schema_version": "golden-dataset-identity.v1",
        "content_fingerprint": f"sha256:{digest.hexdigest()}",
        "document_count": len(entries),
        "total_bytes": total_bytes,
        "documents": entries,
    }


def _endpoint_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("OKTO_NEURON_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    vault_path = os.environ.get("OKTO_NEURON_VAULT_PATH", "").strip()
    if vault_path:
        headers["X-Okto-Neuron-Vault"] = vault_path
    return headers


def _endpoint_json(endpoint: str, path: str) -> dict[str, Any]:
    url = endpoint.rstrip("/") + path
    request = urllib.request.Request(url, headers=_endpoint_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"could not read endpoint identity from {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid endpoint identity response from {url}")
    return payload


def endpoint_content_identity(endpoint: str) -> dict[str, Any]:
    """Read the live endpoint's complete Document content identity, fail closed."""

    summaries: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _endpoint_json(endpoint, f"/api/v1/nodes?type=Document&limit=500&offset={offset}")
        nodes = page.get("nodes")
        total = page.get("total")
        if not isinstance(nodes, list) or not isinstance(total, int):
            raise RuntimeError("endpoint Document listing lacks nodes/total identity fields")
        summaries.extend(node for node in nodes if isinstance(node, dict))
        offset += len(nodes)
        if offset >= total:
            break
        if not nodes:
            raise RuntimeError("endpoint Document listing stopped before total was reached")

    entries = []
    total_bytes = 0
    for summary in summaries:
        node_id = summary.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise RuntimeError("endpoint Document listing contains an item without an id")
        detail = _endpoint_json(
            endpoint,
            "/api/v1/nodes/" + urllib.parse.quote(node_id, safe=""),
        )
        node = detail.get("node")
        facets = node.get("facets") if isinstance(node, dict) else None
        if not isinstance(facets, dict):
            raise RuntimeError(f"endpoint Document {node_id} lacks facets")
        raw_hash = facets.get("sha256") or facets.get("content_hash")
        raw_length = facets.get("byte_length")
        digest = str(raw_hash or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"endpoint Document {node_id} lacks a valid content hash")
        if not isinstance(raw_length, int) or raw_length < 0:
            raise RuntimeError(f"endpoint Document {node_id} lacks a valid byte length")
        entries.append({"sha256": digest, "byte_length": raw_length})
        total_bytes += raw_length

    entries.sort(key=lambda item: (item["sha256"], item["byte_length"]))
    fingerprint = hashlib.sha256()
    for entry in entries:
        fingerprint.update(entry["sha256"].encode("ascii"))
        fingerprint.update(b"\x00")
        fingerprint.update(str(entry["byte_length"]).encode("ascii"))
        fingerprint.update(b"\x00")
    return {
        "schema_version": "golden-dataset-identity.v1",
        "content_fingerprint": f"sha256:{fingerprint.hexdigest()}",
        "document_count": len(entries),
        "total_bytes": total_bytes,
        "documents": entries,
    }


def compare_endpoint_dataset(dataset_dir: Path, endpoint: str) -> dict[str, Any]:
    expected = dataset_content_identity(dataset_dir / "inputs")
    actual = endpoint_content_identity(endpoint)
    return {
        "schema_version": "golden-endpoint-dataset-check.v1",
        "dataset": dataset_dir.name,
        "endpoint": endpoint,
        "matches": actual == expected,
        "expected": expected,
        "actual": actual,
    }


# ── pinned-field collectors ──────────────────────────────────────────────────────


def fastembed_version() -> str | None:
    """The EXACT installed fastembed version, or None if not importable."""
    try:
        return importlib.metadata.version(EMBEDDER_ID)
    except importlib.metadata.PackageNotFoundError:
        return None


def endpoint_runtime_config(endpoint: str) -> dict[str, Any]:
    """Read the effective black-box config measured by an endpoint-mode run."""

    url = endpoint.rstrip("/") + "/api/v1/config"
    request = urllib.request.Request(url, headers=_endpoint_headers())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"could not read effective runtime config from {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid effective runtime config from {url}")
    return payload


# Quantization tokens this ecosystem encodes in model ids (verified against the
# real ids in-tree, e.g. ``qwen3.6-35b-instruct-q4_K_M``, ``...-mlx-fp16``,
# ``...-oQ4-fp16-mtp``). The model id STRING is the only model metadata a manifest
# receives (there is no separate model-config source on the eval path — the
# answerer/judge model is passed verbatim as a name via args/env), so the quant is
# parsed out of that name. Ordered most-specific first so e.g. ``q4_k_m`` wins over
# a bare ``q4`` (``re.search`` would otherwise stop at the shorter token).
_QUANT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bq\d+_k_\w+\b", re.I),  # q4_K_M, q5_K_S
    re.compile(r"\bq\d+_\d+\b", re.I),  # q4_0, q8_0
    re.compile(r"\b(?:mxfp4|fp4)\b", re.I),
    re.compile(r"\b(?:fp16|bf16|fp8|fp32|f16)\b", re.I),
    re.compile(r"\b(?:int4|int8)\b", re.I),
    re.compile(r"\b(?:awq|gptq|gguf)\b", re.I),
    re.compile(r"\bq\d+\b", re.I),  # bare q4, q8 (last resort)
]


def parse_quant(model_id: str | None) -> str | None:
    """Extract the quantization label from a model id, or ``"unknown"`` if absent.

    The eval path only ever knows a model by its NAME (no separate config blob), so
    the quant must come from the id string when the publisher encodes it — which
    these local builds do (``q4_K_M``, ``fp16``, ``mxfp4``, ``awq`` …). Returns the
    matched token normalized to lower-case (e.g. ``q4_k_m``, ``fp16``). When the id
    carries NO recognizable quant token (e.g. the short ``qwen3.6-35b``), returns
    the EXPLICIT sentinel ``"unknown"`` rather than ``None``/omitting the field, so
    a manifest never silently drops the quant dimension. ``None`` is returned only
    when there is no model id at all (the field stays null alongside the null
    model), keeping parity-compare clean for laptop-less emits."""
    if model_id is None or str(model_id).strip() == "":
        return None
    mid = str(model_id)
    # Search in declared precedence order; first hit wins, normalized to the exact
    # matched substring lower-cased (preserves q4_K_M vs fp16 distinctions).
    for pat in _QUANT_PATTERNS:
        m = pat.search(mid)
        if m:
            return m.group(0).lower()
    return "unknown"


def _git(*args: str, repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_state(repo: Path) -> dict[str, Any]:
    """{'git_rev': <full sha or None>, 'dirty': <bool>, 'patch_sha256': <str or None>}.

    * git_rev      — `git rev-parse HEAD`, the committed revision.
    * dirty        — True when `git status --porcelain` has any output (kept as the
                     quick yes/no it always was).
    * patch_sha256 — sha256 of the working-tree diff (`git diff HEAD`), so the
                     ACTUAL patch on top of the pinned revision is captured, not
                     merely whether one exists (Req 3: pin "git rev + patches").
                     A clean tree (empty diff) pins the stable sentinel ``"clean"``
                     so a clean run is itself a fixed, comparable value rather than
                     a hash of "". Note `git diff HEAD` covers tracked-file edits
                     (staged + unstaged) but NOT untracked files; `dirty` (from
                     --porcelain) still flips on untracked files, so the two fields
                     together distinguish "edited tracked code" from "only new
                     untracked files present".

    All git fields degrade to None when the path is not a git repo / git is
    unavailable, so a manifest is still emittable off-tree."""
    rev = _git("rev-parse", "HEAD", repo=repo)
    porcelain = _git("status", "--porcelain", repo=repo)
    diff = _git("diff", "HEAD", repo=repo)
    if diff is None:
        patch_sha = None
    else:
        # The diff text is what `git diff HEAD` prints; hash its bytes verbatim.
        # Empty diff (no tracked-file changes) -> stable "clean" sentinel.
        patch_sha = (
            "clean" if diff == "" else "sha256:" + hashlib.sha256(diff.encode("utf-8")).hexdigest()
        )
    return {
        "git_rev": rev.strip() if rev is not None else None,
        "dirty": (porcelain.strip() != "") if porcelain is not None else None,
        "patch_sha256": patch_sha,
    }


def _read_k(questions_path: Path) -> int | None:
    """Retrieval k comes only from questions.yaml settings.k.

    Returns None if it is absent or not an integer."""
    if not questions_path.is_file():
        return None
    document = load_yaml(questions_path)
    settings = document.get("settings") if isinstance(document, dict) else None
    value = settings.get("k") if isinstance(settings, dict) else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _read_tier_cost(dataset_dir: Path) -> dict[str, Any]:
    """GN-8 Gate 3 cost-budget tag for this dataset/run.

    Reads an OPTIONAL ``tier_cost.json`` in the dataset dir, e.g.
    ``{"tier": "tier-1", "needs_reingest": false, "note": "read-path only"}``.
    Absent file -> the safe default (tier-1, no re-ingest). Only the two known
    keys are honoured; anything else is passed through under ``extra`` so a
    dataset can annotate without schema churn. See docs/eval-gates.md."""
    default = {"tier": "tier-1", "needs_reingest": False, "source": "default"}
    cfg_path = dataset_dir / "tier_cost.json"
    if not cfg_path.is_file():
        return default
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(raw, dict):
        return default
    tier = raw.get("tier")
    out: dict[str, Any] = {
        "tier": tier if tier in ("tier-1", "tier-2") else "tier-1",
        "needs_reingest": bool(raw.get("needs_reingest", False)),
        "source": "tier_cost.json",
    }
    if "note" in raw:
        out["note"] = str(raw["note"])
    return out


def _opt_float(v: str | None) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _opt_int(v: str | None) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


# The complete set of pinned (assert-arms-compared) top-level keys. run_meta is
# intentionally NOT here — it is excluded from parity comparison by construction.
PINNED_KEYS = (
    "manifest_version",
    "dataset",
    "free_variable",
    "corpus",
    "questions",
    "target",
    "embedder",
    "code",
    "model",
    "retrieval",
    "consolidation",
    "execution",
    "ingest",
)

_ISOLATED_CONSTRUCTION_VARIABLES = frozenset(
    {
        "consolidation.type_adjudication_enabled",
        "consolidation.relation_curator_enabled",
    }
)


def build_manifest(
    dataset_dir: Path,
    *,
    free_variable: str | None,
    repo: Path,
    answerer_model: str | None = None,
    judge_model: str | None = None,
    answerer_temp: float | None = None,
    judge_temp: float | None = None,
    answerer_seed: int | None = None,
    judge_seed: int | None = None,
    runtime_config: dict[str, Any] | None = None,
    questions_path: Path | None = None,
) -> dict[str, Any]:
    inputs_root = dataset_dir / "inputs"
    if not inputs_root.is_dir():
        raise FileNotFoundError(f"no inputs/: {inputs_root}")

    chash, fcount, fbytes = corpus_content_hash(inputs_root)
    selected_questions = (questions_path or dataset_dir / "questions.yaml").resolve()
    if not selected_questions.is_file():
        raise FileNotFoundError(f"no questions.yaml: {selected_questions}")
    question_bytes = selected_questions.read_bytes()
    question_document = load_yaml(selected_questions)
    raw_questions = (
        question_document.get("questions") if isinstance(question_document, dict) else None
    )
    question_count = len(raw_questions) if isinstance(raw_questions, list) else 0
    vault_selector = os.environ.get("OKTO_NEURON_VAULT_PATH", "").strip()
    qn = _opt_int(os.environ.get("OKTO_NEURON_QUERY_NEIGHBORS"))

    embedding = runtime_config.get("embedding", {}) if runtime_config else {}
    llm = runtime_config.get("llm", {}) if runtime_config else {}
    llm_defaults = llm.get("defaults", {}) if isinstance(llm, dict) else {}
    extraction = llm.get("extraction", {}) if isinstance(llm, dict) else {}
    consolidation = runtime_config.get("consolidation", {}) if runtime_config else {}
    ingest = runtime_config.get("ingest", {}) if runtime_config else {}
    if runtime_config:
        embedder = {
            "id": embedding.get("provider_ref") or embedding.get("provider"),
            "provider": embedding.get("provider"),
            "model": embedding.get("model"),
            "dimension": embedding.get("dimension"),
            "version": None,
        }
        if answerer_model is None:
            answerer_model = llm_defaults.get("model")
    else:
        embedder = {
            "id": EMBEDDER_ID,
            "version": fastembed_version(),
        }

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "dataset": dataset_dir.name,
        "free_variable": free_variable,
        "corpus": {
            "content_hash": chash,
            "file_count": fcount,
            "total_bytes": fbytes,
        },
        "questions": {
            "sha256": f"sha256:{hashlib.sha256(question_bytes).hexdigest()}",
            "bytes": len(question_bytes),
            "count": question_count,
        },
        "target": {
            "explicit_vault": bool(vault_selector),
            "vault_selector_sha256": (
                f"sha256:{hashlib.sha256(vault_selector.encode('utf-8')).hexdigest()}"
                if vault_selector
                else None
            ),
        },
        "embedder": embedder,
        "code": git_state(repo),
        "model": {
            "answerer_model": answerer_model,
            "answerer_provider_ref": (llm_defaults.get("provider_ref") if runtime_config else None),
            "answerer_provider": llm_defaults.get("provider") if runtime_config else None,
            "judge_model": judge_model,
            # Quantization parsed from each model id (the only model metadata the
            # eval path receives). "unknown" when the id carries no quant token;
            # null only when there is no model id at all. (Req 3: pin model quant.)
            "answerer_quant": parse_quant(answerer_model),
            "judge_quant": parse_quant(judge_model),
            "answerer_temp": answerer_temp,
            "judge_temp": judge_temp,
            "answerer_seed": answerer_seed,
            "judge_seed": judge_seed,
        },
        "retrieval": {
            "k": _read_k(selected_questions),
            "query_neighbors": qn if qn is not None else 0,
        },
        "consolidation": dict(consolidation) if isinstance(consolidation, dict) else {},
        "execution": {
            "extraction_max_concurrent": (
                extraction.get("max_concurrent") if isinstance(extraction, dict) else None
            ),
            "embedding_batch_size": embedding.get("batch_size"),
            "embedding_max_concurrent_batches": embedding.get("max_concurrent_batches"),
        },
        "ingest": dict(ingest) if isinstance(ingest, dict) else {},
        # Separated block — wall-clock metadata, EXCLUDED from assert-arms parity.
        "run_meta": {
            "generated_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "tool": "tests/golden/bin/manifest.py",
            # GN-8 Gate 3 (cost budget): how expensive a change in this arc is.
            # tier-1 = read-path only, hours, NO re-ingest; tier-2 = days,
            # re-ingest REQUIRED (reference-eval only, per ADR 0019's locked decision).
            # Read from an optional tier_cost.json in the dataset dir; defaults to
            # the safe read-path tier. EXCLUDED from assert-arms parity (cost tier
            # is metadata, not a comparability axis). See docs/eval-gates.md.
            "tier_cost": _read_tier_cost(dataset_dir),
        },
    }
    return manifest


def write_manifest(manifest: dict[str, Any], out: Path) -> None:
    """Byte-stable serialization (sort_keys + trailing newline). Two emits in the
    same git state differ ONLY in run_meta.generated_at."""
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── multi-arm parity assertion ───────────────────────────────────────────────────

# Fields DERIVED from another pinned field, by the leaf name of their source. When
# the free_variable IS a source field, its derived companions MUST also be excused
# from parity, or they register as spurious diffs: ``answerer_quant`` is a pure
# function of ``answerer_model`` (parse_quant), so freeing the model necessarily
# frees its quant. Keyed by source-leaf -> derived-leaf set; consulted by
# ``_pinned_view`` for both bare-leaf and dotted ``model.<leaf>`` free variables.
_DERIVED_OF: dict[str, set[str]] = {
    "answerer_model": {"answerer_quant"},
    "judge_model": {"judge_quant"},
}


def _deep_drop(obj: Any, field: str) -> Any:
    """Return a copy of ``obj`` with key ``field`` removed wherever it appears.

    Lets the free_variable be named by its bare leaf (e.g. ``answerer_model``,
    which lives at ``model.answerer_model``) or by a top-level section name (e.g.
    ``retrieval`` to free the whole block). Dotted paths are handled by the caller
    before this is reached."""
    if isinstance(obj, dict):
        return {k: _deep_drop(v, field) for k, v in obj.items() if k != field}
    if isinstance(obj, list):
        return [_deep_drop(v, field) for v in obj]
    return obj


def _pinned_view(manifest: dict[str, Any], drop_field: str | None) -> dict[str, Any]:
    """The comparable projection of a manifest: pinned keys only (run_meta
    dropped), with the declared free_variable field removed wherever it lives so
    a legitimate difference there does not register as a diff.

    ``drop_field`` may be:
      * a top-level pinned section (``retrieval``, ``model``) — the whole block is
        excused from comparison;
      * a dotted path (``retrieval.k``, ``model.answerer_model``) — only that leaf;
      * a bare leaf name (``answerer_model``, ``k``) — dropped wherever it nests.

    When the freed field has DERIVED companions (``answerer_model`` ->
    ``answerer_quant``), those are dropped too: a derived field is a pure function
    of its source, so it co-varies legitimately and must not register as a diff.
    """
    view = {k: manifest.get(k) for k in PINNED_KEYS}
    if not drop_field:
        return view
    if drop_field in _ISOLATED_CONSTRUCTION_VARIABLES:
        target = view.get("target")
        if isinstance(target, dict) and target.get("explicit_vault") is True:
            target = dict(target)
            target["vault_selector_sha256"] = "<isolated-construction-arm>"
            view["target"] = target
    head, sep, tail = drop_field.partition(".")
    if sep:
        # Dotted path: drop exactly view[head][tail] + any fields derived from tail.
        section = view.get(head)
        if isinstance(section, dict):
            section = dict(section)
            for leaf in (tail, *_DERIVED_OF.get(tail, set())):
                section.pop(leaf, None)
            view[head] = section
    elif head in view:
        # Bare name that IS a top-level pinned section -> excuse the whole block.
        view[head] = "<free_variable>"
    else:
        # Bare leaf name -> drop it (and its derived companions) wherever they nest.
        for leaf in (head, *_DERIVED_OF.get(head, set())):
            view = {k: _deep_drop(v, leaf) for k, v in view.items()}
    return view


def _diff(a: Any, b: Any, path: str, diffs: list[str]) -> None:
    """Collect named, path-qualified differences between two JSON-ish values."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}.{key}" if path else key
            if key not in a:
                diffs.append(f"{sub}: <missing in A> != {b[key]!r}")
            elif key not in b:
                diffs.append(f"{sub}: {a[key]!r} != <missing in B>")
            else:
                _diff(a[key], b[key], sub, diffs)
    elif a != b:
        diffs.append(f"{path}: {a!r} != {b!r}")


def assert_arms(man_a: dict[str, Any], man_b: dict[str, Any]) -> dict[str, Any]:
    """Assert two arms are comparable.

    Rules:
      * Both must declare the SAME free_variable (you cannot compare arms that
        disagree on what's allowed to vary).
      * Corpus hashes MUST be equal (the parity-leak guard — different corpus =>
        not comparable, full stop, even if it is the declared free variable).
      * Every other pinned field must be equal EXCEPT the declared free_variable.

    Returns {'ok': bool, 'free_variable': str|None, 'diffs': [str], 'reason': str}.
    """
    diffs: list[str] = []

    fv_a = man_a.get("free_variable")
    fv_b = man_b.get("free_variable")
    if fv_a != fv_b:
        diffs.append(f"free_variable: A declares {fv_a!r}, B declares {fv_b!r}")
        return {
            "ok": False,
            "free_variable": None,
            "diffs": diffs,
            "reason": "arms declare different free_variable",
        }
    free_variable = fv_a

    # Corpus hash equality is non-negotiable and is checked even if the caller
    # (mis)declared "corpus" as the free variable.
    ha = (man_a.get("corpus") or {}).get("content_hash")
    hb = (man_b.get("corpus") or {}).get("content_hash")
    corpus_ok = ha == hb
    if not corpus_ok:
        diffs.append(f"corpus.content_hash: {ha} != {hb}")

    # corpus.content_hash is reported explicitly above; drop it from the structural
    # _diff so it is named exactly once (file_count/total_bytes still compared).
    view_a = _deep_drop(_pinned_view(man_a, free_variable), "content_hash")
    view_b = _deep_drop(_pinned_view(man_b, free_variable), "content_hash")
    _diff(view_a, view_b, "", diffs)
    # De-dup the corpus.content_hash line if _diff also surfaced it. Reaching here
    # means both arms declared the same free_variable (the mismatch path returned
    # early), so OK reduces to: corpus hashes match AND no pinned diffs remain.
    diffs = sorted(set(diffs))

    ok = corpus_ok and not diffs
    if ok:
        reason = (
            f"arms comparable — all pinned fields equal except free_variable "
            f"'{free_variable}', corpus hashes match"
        )
    elif not corpus_ok:
        reason = "PARITY LEAK: corpus content hashes differ (arms not comparable)"
    else:
        reason = "arms differ in pinned field(s) other than the declared free_variable"
    return {
        "ok": ok,
        "free_variable": free_variable,
        "diffs": diffs,
        "reason": reason,
    }


# ── subcommands ──────────────────────────────────────────────────────────────────


def cmd_emit(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset).resolve()
    if not dataset_dir.is_dir():
        print(json.dumps({"error": f"dataset dir not found: {dataset_dir}"}), file=sys.stderr)
        return 1
    repo = Path(getattr(args, "repo", None) or _default_repo(dataset_dir))
    try:
        runtime_config = endpoint_runtime_config(args.endpoint) if args.endpoint else None
        manifest = build_manifest(
            dataset_dir,
            free_variable=args.free_variable,
            repo=repo,
            answerer_model=args.answerer_model,
            judge_model=args.judge_model,
            answerer_temp=_opt_float(args.answerer_temp),
            judge_temp=_opt_float(args.judge_temp),
            answerer_seed=_opt_int(args.answerer_seed),
            judge_seed=_opt_int(args.judge_seed),
            runtime_config=runtime_config,
            questions_path=Path(args.questions).resolve() if args.questions else None,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    write_manifest(manifest, Path(args.out))
    # Console summary (stable, sorted) — the full pinned surface minus run_meta.
    summary = {k: manifest[k] for k in PINNED_KEYS}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_assert_arms(args: argparse.Namespace) -> int:
    pa, pb = Path(args.manifest_a), Path(args.manifest_b)
    for p in (pa, pb):
        if not p.is_file():
            print(json.dumps({"error": f"manifest not found: {p}"}), file=sys.stderr)
            return 1
    man_a = json.loads(pa.read_text(encoding="utf-8"))
    man_b = json.loads(pb.read_text(encoding="utf-8"))
    result = assert_arms(man_a, man_b)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def cmd_assert_endpoint_dataset(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset).resolve()
    if not (dataset_dir / "inputs").is_dir():
        print(json.dumps({"error": f"no inputs/: {dataset_dir / 'inputs'}"}), file=sys.stderr)
        return 1
    try:
        result = compare_endpoint_dataset(dataset_dir, args.endpoint)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["matches"] else 1


def _default_repo(dataset_dir: Path) -> Path:
    """Walk up from the dataset dir to the nearest enclosing git repo root; fall
    back to the dataset dir itself (git_state then yields nulls cleanly)."""
    for parent in [dataset_dir, *dataset_dir.parents]:
        if (parent / ".git").exists():
            return parent
    return dataset_dir


def build_emit_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach the `emit` arguments to a parser (shared with judge.py's wiring)."""
    p.add_argument("dataset", help="dataset dir, e.g. tests/golden/datasets/synthetic-ci")
    p.add_argument(
        "--free-variable",
        dest="free_variable",
        default=None,
        help="name of the ONE field allowed to differ across arms "
        "(e.g. 'answerer_model', 'retrieval.k', 'model')",
    )
    p.add_argument("--out", default="run-manifest.json")
    p.add_argument(
        "--repo", default=None, help="git repo root to pin (default: nearest enclosing .git)"
    )
    p.add_argument(
        "--questions",
        default=None,
        help="explicit questions.yaml to hash and use for retrieval settings",
    )
    p.add_argument(
        "--endpoint",
        default=None,
        help="running Okto Neuron endpoint whose effective provider/model config is measured",
    )
    # Model fields — args override env; both default to null in the manifest.
    p.add_argument(
        "--answerer-model",
        dest="answerer_model",
        default=os.environ.get("OKTO_NEURON_ANSWERER_MODEL"),
    )
    p.add_argument(
        "--judge-model", dest="judge_model", default=os.environ.get("OKTO_NEURON_JUDGE_MODEL")
    )
    p.add_argument(
        "--answerer-temp", dest="answerer_temp", default=os.environ.get("OKTO_NEURON_ANSWERER_TEMP")
    )
    p.add_argument(
        "--judge-temp", dest="judge_temp", default=os.environ.get("OKTO_NEURON_JUDGE_TEMP")
    )
    p.add_argument(
        "--answerer-seed", dest="answerer_seed", default=os.environ.get("OKTO_NEURON_ANSWERER_SEED")
    )
    p.add_argument(
        "--judge-seed", dest="judge_seed", default=os.environ.get("OKTO_NEURON_JUDGE_SEED")
    )
    return p


def build_assert_arms_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("manifest_a", help="first arm's run-manifest.json")
    p.add_argument("manifest_b", help="second arm's run-manifest.json")
    return p


def build_assert_endpoint_dataset_parser(
    p: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    p.add_argument("dataset", help="dataset directory containing inputs/")
    p.add_argument("--endpoint", required=True, help="populated Okto Neuron endpoint")
    p.add_argument("--out", default=None, help="optional JSON evidence path")
    return p


def main() -> int:
    p = argparse.ArgumentParser(
        description="RUN MANIFEST — reproducibility pin + multi-arm parity guard "
        "for the black-box golden eval framework."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("emit", help="emit a run-manifest.json for a dataset")
    build_emit_parser(pe)
    pe.set_defaults(func=cmd_emit)

    pa = sub.add_parser(
        "assert-arms",
        help="assert two manifests are comparable (all pinned fields equal except "
        "the declared free_variable; corpus hashes MUST match)",
    )
    build_assert_arms_parser(pa)
    pa.set_defaults(func=cmd_assert_arms)

    pid = sub.add_parser(
        "assert-endpoint-dataset",
        help="fail unless a populated endpoint contains exactly this dataset's source bytes",
    )
    build_assert_endpoint_dataset_parser(pid)
    pid.set_defaults(func=cmd_assert_endpoint_dataset)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
