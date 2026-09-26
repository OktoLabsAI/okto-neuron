from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from okto_neuron._internal.infra import INFRA_FACET, LOW_SALIENCE_FACET
from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.ingest.markdown import (
    MarkdownIngest,
    ParsedBlock,
    ParsedClaim,
    load_markdown,
    parse_markdown,
    sha256_hex,
)

SYSTEM_AGENT_ID = sha256_hex("agent", "system")
EXTRACTION_ACTIVITY_ID = sha256_hex("activity", "deterministic-v1")


def ingest_document(store: object, source: str | Path, *, vault_root: str | Path):
    path = Path(source).expanduser().resolve(strict=False)
    # Must match the ingest queue's TEXT_SUFFIXES contract: .txt is enqueued
    # as ingestible, and the configurable byte-window chunker handles it exactly
    # like markdown. The old flat durable-copy scheme force-renamed .txt to
    # .md, masking this gate; F11's suffix-preserving tree copies exposed it
    # (three private-corpus transcripts failed the rebuild until this fix).
    if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise NotImplementedError(
            f"text ingest supports .md/.markdown/.txt files; got {path.suffix}"
        )

    from okto_neuron.config import IngestConfig, VaultConfig
    from okto_neuron.errors import ConfigNotFound

    try:
        ingest_config = VaultConfig.load(vault_root).ingest
    except (ConfigNotFound, FileNotFoundError):
        ingest_config = IngestConfig()

    _ensure_system_nodes(store)
    ingest = parse_markdown(
        path,
        extraction_activity_id=EXTRACTION_ACTIVITY_ID,
        agent_id=SYSTEM_AGENT_ID,
        chunk_size_bytes=ingest_config.chunk_size_bytes,
        chunk_overlap_bytes=ingest_config.chunk_overlap_bytes,
        vault_root=vault_root,
    )
    document_facets = {
        **ingest.document.model_dump(mode="json"),
        "path": str(path),
        "metadata": _json_safe(ingest.metadata),
    }
    existing_document = store.get_node(ingest.document.id)
    if existing_document is not None and existing_document.type == "Document":
        discovered_at = existing_document.facets.get("discovered_at")
        if discovered_at is not None:
            document_facets["discovered_at"] = discovered_at
    document_node = Node(
        id=ingest.document.id,
        type="Document",
        title=ingest.item.title,
        content=ingest.item.content,
        tags=ingest.item.tags,
        facets=document_facets,
        provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
    )
    store.add_node(document_node)

    for parsed in ingest.blocks:
        facets = {
            **parsed.block.model_dump(mode="json"),
            "chunk_size_bytes": ingest.chunk_size_bytes,
            "chunk_overlap_bytes": ingest.chunk_overlap_bytes,
        }
        block_node = Node(
            id=parsed.block.id,
            type="Block",
            title=f"{parsed.block.block_kind.value} {parsed.block.block_index}",
            content=parsed.text,
            facets={**facets, "source_path": str(path)},
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
        store.add_node(block_node)

    for parsed in ingest.claims:
        claim_facets = parsed.claim.model_dump(mode="json")
        extra_facets: dict[str, object] = {"source_path": str(path)}
        # has_heading claims are mostly structural anchors (a document's table of
        # contents), so tag them low-salience: the graph-native ask/subgraph path
        # (subgraph.py's is_low_salience gate) curates them out while the Claim
        # stays a first-class, provenance-anchored graph node. NB: this does NOT
        # hard-drop them from lexical/vector recall — a primary heading is often
        # the document's subject and is the byte-anchored provenance target for a
        # topical recall query (FR3 golden qa2/qa3); recall separates genuine
        # section-label noise by score, not by this facet.
        if parsed.claim.P == "has_heading":
            extra_facets.update(LOW_SALIENCE_FACET)
        claim_node = Node(
            id=parsed.claim.id,
            type="Claim",
            title=parsed.title,
            content=parsed.text,
            tags=parsed.tags,
            facets={**claim_facets, **extra_facets},
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
        store.add_node(claim_node)
        _add_claim_provenance_edges(
            store,
            parsed.claim.id,
            parsed.claim.block_id,
            subject_id=parsed.claim.S_id,
            object_id=parsed.claim.O_id,
        )

    _retire_stale_deterministic_claims(
        store,
        document_id=ingest.document.id,
        fresh_claim_ids={parsed.claim.id for parsed in ingest.claims},
    )

    flush = getattr(store, "flush", None)
    if callable(flush):
        flush()
    return ingest.item


_DETERMINISTIC_CLAIM_PREDICATES = frozenset({"has_tag", "has_heading", "links_to"})


def _retire_stale_deterministic_claims(
    store: object, *, document_id: str, fresh_claim_ids: set[str]
) -> None:
    """Supersede a document's previously-minted has_tag/has_heading/links_to
    Claims that the current ingest no longer produced (finding 3.5).

    Block/Claim ids are content-hash-derived, so an edited/removed tag,
    heading, or wikilink never overwrites its OLD Claim id in place — the old
    id is simply absent from the fresh set. Left alone, that stale Claim stays
    live forever: BM25 (``_ensure_bm25_stats``) has no staleness filter, and
    only the ``is_superseded`` recall gate (``query.py``) excludes a Claim from
    search — so an un-superseded stale Claim keeps surfacing a fact the trust
    root (the markdown file) no longer asserts.

    Supersede, not detach: once a document no longer carries a tag/heading/
    link, ``has_tag: X`` is now definitively FALSE about it, not merely
    unconfirmed (contrast ADR 0024's LLM-claim detach, which keeps a claim
    live because a deleted source line doesn't necessarily make the fact
    false). No ``supersedes`` edge is written — this is a pure removal, not a
    correction with a new claim replacing it.

    Runs unconditionally on every ``ingest_document`` call (every
    ``vault.add()``), independent of the ADR 0023/0024 incremental-ingest flags
    — so it also closes the gap ``companion/_incremental.py``'s
    ``detach_orphan_removals`` deliberately leaves for deterministic claims
    (see the comment at its ``model_id`` gate).

    Every deterministic Claim's ``S_id`` is always ``document_id`` (see
    ``_make_claim`` call sites in ``markdown.py``), and every such Claim gets
    an ``rdf:subject`` edge to the Document (``_add_claim_provenance_edges``,
    unconditional because the Document node is written before the claims
    loop). Walking that edge index instead of scanning every Claim in the
    vault keeps this O(claims for this document), not O(claims in the vault) —
    load-bearing for ``kg rebuild``, which calls ``ingest_document`` once per
    file across the whole vault.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    for edge in list(store.list_edges(dst=document_id, type="rdf:subject")):
        claim = store.get_node(edge.src)
        if claim is None or claim.type != "Claim":
            continue
        facets = claim.facets or {}
        if facets.get("P") not in _DETERMINISTIC_CLAIM_PREDICATES:
            continue
        if claim.provenance is None or claim.provenance.rule_id != "deterministic-v1":
            continue
        if claim.id in fresh_claim_ids:
            continue
        if facets.get("_superseded"):
            continue
        store.add_node(
            claim.model_copy(
                update={
                    "facets": {
                        **facets,
                        "_superseded": True,
                        "valid_until": today,
                    }
                }
            )
        )


def _ensure_system_nodes(store: object) -> None:
    if not store.get_node(SYSTEM_AGENT_ID):
        store.add_node(
            Node(
                id=SYSTEM_AGENT_ID,
                type="Agent",
                title="system",
                content="Marginalia deterministic extraction system agent",
                facets=dict(INFRA_FACET),
                provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
            )
        )
    if not store.get_node(EXTRACTION_ACTIVITY_ID):
        store.add_node(
            Node(
                id=EXTRACTION_ACTIVITY_ID,
                type="Activity",
                title="ExtractionActivity deterministic-v1",
                content="Deterministic frontmatter, tag, wikilink, and heading extraction.",
                facets=dict(INFRA_FACET),
                provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
            )
        )


def _add_claim_provenance_edges(
    store: object,
    claim_id: str,
    block_id: str,
    *,
    subject_id: str | None = None,
    object_id: str | None = None,
) -> None:
    """Wire the three PROV-O edges plus the ADR 0005 bridge edges. ``rdf:subject``
    (Claim → subject entity) is minted for every Claim including literal-object
    ones; ``rdf:object`` (Claim → object entity) only when the object is an entity
    (``object_id`` set). Bridge edges are skipped when the referent node is absent
    so a dangling id never crashes ingest — ``add_edge`` raises on a missing
    endpoint (ADR 0005 F6 idempotency falls out of the (claim_id, type, dst) id)."""
    edges: dict[str, str] = {
        "prov:wasDerivedFrom": block_id,
        "prov:wasGeneratedBy": EXTRACTION_ACTIVITY_ID,
        "prov:wasAttributedTo": SYSTEM_AGENT_ID,
    }
    if subject_id and store.get_node(subject_id) is not None:
        edges["rdf:subject"] = subject_id
    if object_id and store.get_node(object_id) is not None:
        edges["rdf:object"] = object_id
    for edge_type, dst in edges.items():
        store.add_edge(
            Edge(
                id=sha256_hex("edge", claim_id, edge_type, dst),
                type=edge_type,
                src=claim_id,
                dst=dst,
                provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
            )
        )


def _json_safe(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


__all__ = [
    "MarkdownIngest",
    "ParsedBlock",
    "ParsedClaim",
    "ingest_document",
    "load_markdown",
    "parse_markdown",
]
