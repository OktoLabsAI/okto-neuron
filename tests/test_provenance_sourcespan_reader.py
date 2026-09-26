"""Model-free test for ADR 0003 Road B brick C2: ``Vault._provenance_for_node``
prefers a Claim's ``source_span`` facet (vault-relative byte anchor) over the
``block_id`` -> Block-node lookup, resolving the span back to an absolute path
under the vault root. The span path must yield a Provenance byte-identical to the
legacy block path (additive, behavior-preserving) AND survive Block deletion.

No LLM, no provider, no model load — a bare InMemoryStore + Vault."""

from __future__ import annotations

from pathlib import Path

from okto_neuron.core.schema import Node, Provenance
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.vault import Vault

_HASH = "a" * 64
_BLOCK_ID = "b" * 64
_CLAIM_ID = "c" * 64
_BYTE_START = 12
_BYTE_END = 48


def _vault(root: Path) -> Vault:
    return Vault(root, InMemoryStore())


def _block_node(abs_source: str) -> Node:
    return Node(
        id=_BLOCK_ID,
        type="Block",
        title="block",
        content="block text",
        facets={
            "source_path": abs_source,
            "byte_start": _BYTE_START,
            "byte_end": _BYTE_END,
            "content_hash": _HASH,
        },
        provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
    )


def _claim_node(*, rel: str | None, abs_source: str) -> Node:
    """A Claim carrying the legacy block_id + absolute source_path, and (when
    ``rel`` is set) the ADR 0003 source_span facet with a vault-relative path."""
    facets: dict[str, object] = {
        "block_id": _BLOCK_ID,
        "source_path": abs_source,
        "extraction_activity_id": "act-1",
        "agent_id": "agent-1",
        "document_id": "doc-1",
    }
    if rel is not None:
        facets["source_span"] = {
            "source_path": rel,
            "byte_start": _BYTE_START,
            "byte_end": _BYTE_END,
            "content_hash": _HASH,
        }
    return Node(
        id=_CLAIM_ID,
        type="Claim",
        title="subj relates_to obj",
        content="subj relates_to obj",
        facets=facets,
        provenance=Provenance(source="llm", rule_id="companion-remember"),
    )


def test_span_path_equals_block_path(tmp_path: Path) -> None:
    """With both anchors present, resolving via source_span yields a Provenance
    byte-identical to resolving via the block_id -> Block lookup."""
    rel = "notes/topic.md"
    abs_source = str((tmp_path / rel).resolve())

    vault = _vault(tmp_path)
    vault.store.add_node(_block_node(abs_source))

    via_span = vault._provenance_for_node(_claim_node(rel=rel, abs_source=abs_source))
    via_block = vault._provenance_for_node(_claim_node(rel=None, abs_source=abs_source))

    assert via_span == via_block
    # The resolved span path is the absolute path under the vault root.
    assert via_span.path == abs_source
    assert via_span.byte_start == _BYTE_START
    assert via_span.byte_end == _BYTE_END
    assert via_span.content_hash == f"sha256:{_HASH}"
    assert via_span.block_id == _BLOCK_ID


def test_span_survives_block_deletion(tmp_path: Path) -> None:
    """The whole point of C2: with the Block node gone, the span still resolves
    full byte provenance (the block_id fallback would zero out)."""
    rel = "notes/topic.md"
    abs_source = str((tmp_path / rel).resolve())

    vault = _vault(tmp_path)  # NOTE: no Block node added to the store
    prov = vault._provenance_for_node(_claim_node(rel=rel, abs_source=abs_source))

    assert prov.path == abs_source
    assert prov.byte_start == _BYTE_START
    assert prov.byte_end == _BYTE_END
    assert prov.content_hash == f"sha256:{_HASH}"


def test_span_path_escape_rejected(tmp_path: Path) -> None:
    """A span path that escapes the vault root falls through to the block path
    rather than exposing an out-of-vault byte read (security re-validation)."""
    rel = "notes/topic.md"
    abs_source = str((tmp_path / rel).resolve())

    vault = _vault(tmp_path)
    vault.store.add_node(_block_node(abs_source))

    node = _claim_node(rel=rel, abs_source=abs_source)
    # Bypass SourceSpan's own validator by injecting a traversal directly into
    # the stored facet dict (defense-in-depth at the read boundary).
    node.facets["source_span"] = {  # type: ignore[index]
        "source_path": "../../etc/passwd",
        "byte_start": _BYTE_START,
        "byte_end": _BYTE_END,
        "content_hash": _HASH,
    }
    prov = vault._provenance_for_node(node)
    # Fell through to the block-id path -> absolute in-vault source.
    assert prov.path == abs_source
