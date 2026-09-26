"""The closed schema type set, enforced at the store write boundary.

ADR 0040 locks the graph to 5 primitives + 6 support types.  Until now that was
a convention honoured by the writers and re-checked only on the read side
(``server/http.py`` validates ``?type=``), so a buggy or adversarial writer could
mint a node outside the closed set and only be noticed at query time.  This
module is the single definition, and both :class:`~okto_neuron.store.ladybug.LadybugStore`
and :class:`~okto_neuron.store.memory.InMemoryStore` refuse an out-of-set type in
``add_node``.

``SchemaMetadata`` is *writable but not part of the closed schema*.  The bootstrap
writes it with raw Cypher (``store/schema.py``), so it is a real row that
``list_nodes`` yields; the rebuild/reembed copy paths therefore hand it back to
``add_node`` and would break if it were refused.  It is internal bookkeeping and
is already excluded from every semantic surface (``semantic_quality.INTERNAL_NODE_TYPES``,
``server/http.py``'s browser filters).
"""

from __future__ import annotations

from typing import Final

from okto_neuron.core.schema import Edge

PRIMITIVE_NODE_TYPES: Final[tuple[str, ...]] = (
    "Agent",
    "Activity",
    "InformationObject",
    "Concept",
    "Place",
)

SUPPORT_NODE_TYPES: Final[tuple[str, ...]] = (
    "Document",
    "Identifier",
    "Annotation",
    "Claim",
    "Block",
    "Finding",
)

#: The locked semantic schema: adding a member requires a new RFC/ADR.
CLOSED_NODE_TYPES: Final[frozenset[str]] = frozenset(PRIMITIVE_NODE_TYPES + SUPPORT_NODE_TYPES)

#: Internal bookkeeping rows that are never part of the semantic graph.
INTERNAL_NODE_TYPES: Final[frozenset[str]] = frozenset({"SchemaMetadata"})

#: Everything a store is allowed to persist.
WRITABLE_NODE_TYPES: Final[frozenset[str]] = CLOSED_NODE_TYPES | INTERNAL_NODE_TYPES


def require_writable_node_type(node_type: object) -> None:
    """Fail closed on a node type outside the closed schema.

    Raises ``ValueError`` — the same failure mode both stores already use for
    the sibling ``add_edge`` endpoint invariant.
    """
    if node_type not in WRITABLE_NODE_TYPES:
        raise ValueError(
            f"node type outside the closed schema: {node_type!r}; "
            f"must be one of {sorted(WRITABLE_NODE_TYPES)}"
        )


def require_same_edge_identity(existing: Edge, proposed: Edge) -> None:
    """Fail closed when an existing edge id is reused for a different (type, src, dst)."""
    existing_identity = (existing.type, existing.src, existing.dst)
    proposed_identity = (proposed.type, proposed.src, proposed.dst)
    if existing_identity != proposed_identity:
        raise ValueError(
            f"edge identity collision for {proposed.id}: "
            f"existing={existing_identity!r}, proposed={proposed_identity!r}"
        )
