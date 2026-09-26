"""Deterministic drift detectors for RFC §5 Finding output.

The detectors operate over the RFC §4.2 support spine (`Document`, `Block`,
`Claim`, `Finding`) and intentionally use the existing vault store API rather
than introducing a second graph abstraction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron.core.schema import Node
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.schema.support.finding import Finding

DETECTOR_NAMES = (
    "commitment_temporal_shacl",
    "supersedence_stale_head",
    "authority_alias_collision",
)


def run_detector(name: str, vault: object) -> list[Finding]:
    """Run one closed-set detector and return RFC §5 `Finding` values.

    Findings cite RFC §4.2 `Block` IDs when available, falling back to the
    offending `Document.id` only when a document has no ingested block.
    """
    if name == "commitment_temporal_shacl":
        return _detect_commitment_temporal_shacl(vault)
    if name == "supersedence_stale_head":
        return _detect_supersedence_stale_head(vault)
    if name == "authority_alias_collision":
        return _detect_authority_alias_collision(vault)
    raise ValueError(f"unknown detector {name!r}; expected one of {', '.join(DETECTOR_NAMES)}")


def _detect_commitment_temporal_shacl(vault: object) -> list[Finding]:
    reference_date = _reference_date()
    docs = _documents(vault)
    findings: list[Finding] = []
    for doc in docs:
        metadata = _frontmatter(doc)
        if metadata.get("type") != "commitment":
            continue
        due = _parse_date(metadata.get("due"))
        if due is None or due >= reference_date:
            continue
        if _has_closure_evidence(doc, docs):
            continue
        findings.append(
            _finding(
                name="commitment_temporal_shacl",
                doc=doc,
                vault=vault,
                message=f"Commitment is overdue with no closure evidence: {_doc_label(doc)}",
            )
        )
    return findings


def _detect_supersedence_stale_head(vault: object) -> list[Finding]:
    docs = _documents(vault)
    by_superseded = _supersedence_index(docs)
    findings: list[Finding] = []
    for doc in docs:
        metadata = _frontmatter(doc)
        if not metadata.get("supersedes") or metadata.get("head") is not False:
            continue
        decision_id = str(metadata.get("decision_id") or "")
        if not decision_id or decision_id not in by_superseded:
            continue
        findings.append(
            _finding(
                name="supersedence_stale_head",
                doc=doc,
                vault=vault,
                message=f"Decision {decision_id} is stale and superseded by a later head",
            )
        )
    return findings


def _detect_authority_alias_collision(vault: object) -> list[Finding]:
    docs = _documents(vault)
    primary_agents = {
        str(metadata["agent"])
        for metadata in (_frontmatter(doc) for doc in docs)
        if metadata.get("type") == "authority"
        and metadata.get("agent")
        and not metadata.get("alias_of")
    }
    findings: list[Finding] = []
    for doc in docs:
        metadata = _frontmatter(doc)
        agent = str(metadata.get("agent") or "")
        if (
            metadata.get("type") == "authority"
            and metadata.get("alias_of")
            and agent
            and agent in primary_agents
        ):
            findings.append(
                _finding(
                    name="authority_alias_collision",
                    doc=doc,
                    vault=vault,
                    message=f"Authority alias collides with primary authority: {agent}",
                )
            )
    return findings


def _documents(vault: object) -> list[Node]:
    return list(getattr(vault, "store").list_nodes(type="Document"))


def _frontmatter(doc: Node) -> dict[str, Any]:
    path = _doc_path(doc)
    metadata = _read_frontmatter(path) if path else {}
    if metadata:
        return metadata
    facet_metadata = doc.facets.get("metadata") or {}
    return facet_metadata if isinstance(facet_metadata, dict) else {}


def _read_frontmatter(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}
    try:
        metadata = yaml.safe_load(raw[4:end]) or {}
    except yaml.YAMLError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _doc_path(doc: Node) -> Path | None:
    raw_path = doc.facets.get("path") or doc.facets.get("uri")
    return Path(str(raw_path)) if raw_path else None


def _doc_label(doc: Node) -> str:
    path = _doc_path(doc)
    return path.name if path else doc.id


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00").date()
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _reference_date() -> date:
    raw = _compat_getenv("OKTO_NEURON_REFERENCE_DATE", "2026-05-19")
    parsed = _parse_date(raw)
    if parsed is None:
        raise ValueError(f"invalid OKTO_NEURON_REFERENCE_DATE: {raw!r}")
    return parsed


def _has_closure_evidence(doc: Node, docs: Iterable[Node]) -> bool:
    targets = _reference_tokens(doc)
    closure_keys = {
        "evidence",
        "evidence_for",
        "closure",
        "closure_for",
        "closed_for",
        "closes",
        "resolved_for",
        "resolves",
        "resolves_commitment",
    }
    for other in docs:
        if other.id == doc.id:
            continue
        metadata = _frontmatter(other)
        for key in closure_keys:
            if _contains_reference(metadata.get(key), targets):
                return True
    return False


def _reference_tokens(doc: Node) -> set[str]:
    path = _doc_path(doc)
    metadata = _frontmatter(doc)
    tokens = {doc.id}
    for key in ("id", "commitment_id", "title"):
        if metadata.get(key):
            tokens.add(str(metadata[key]))
    if path:
        tokens.add(path.name)
        tokens.add(path.as_posix())
        try:
            root = Path.cwd().resolve()
            tokens.add(path.resolve().relative_to(root).as_posix())
        except (OSError, ValueError):
            pass
    return tokens


def _contains_reference(value: object, targets: set[str]) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, int, float, bool)):
        return str(value) in targets
    if isinstance(value, dict):
        return any(_contains_reference(item, targets) for item in value.values())
    if isinstance(value, Iterable):
        return any(_contains_reference(item, targets) for item in value)
    return False


def _supersedence_index(docs: Iterable[Node]) -> dict[str, list[Node]]:
    index: dict[str, list[Node]] = {}
    for doc in docs:
        supersedes = _frontmatter(doc).get("supersedes")
        for decision_id in _as_list(supersedes):
            index.setdefault(str(decision_id), []).append(doc)
    return index


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _finding(name: str, doc: Node, vault: object, message: str) -> Finding:
    return Finding(
        id=f"f_{sha256_hex(name, doc.id)[:16]}",
        kind=name,
        severity="warn",
        status="open",
        evidence_claim_ids=_evidence_ids(doc, vault),
        message=message,
        detected_at=datetime.now(timezone.utc),
    )


def _evidence_ids(doc: Node, vault: object) -> list[str]:
    path = _doc_path(doc)
    source_path = str(path.resolve()) if path else None
    block_ids = [
        block.id
        for block in getattr(vault, "store").list_nodes(type="Block")
        if str(block.facets.get("source_path") or "") == source_path
    ]
    return block_ids or [doc.id]
