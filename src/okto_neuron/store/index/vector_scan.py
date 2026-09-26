"""Untruncated vector scan over IndexRecord corpora.

Mirrors ``LadybugStore.nodes_with_embeddings`` (ladybug.py at the time of the
M1 split): every record carrying a non-None embedding, in ascending id order,
excluding the schema metadata node, optionally filtered by type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

from okto_neuron.store import schema

if TYPE_CHECKING:
    from okto_neuron.store.index.corpus import IndexRecord


def scan_vectors(
    records: Iterable["IndexRecord"], type: Optional[str] = None
) -> Iterable[tuple[str, list[float]]]:
    """Yield (id, embedding) for every record with an embedding, ascending id.

    ``records`` need not already be sorted; this function sorts by id itself
    so callers may pass a dict's ``.values()`` in any order.
    """
    candidates = [
        record
        for record in records
        if record.embedding is not None and record.id != schema.SCHEMA_METADATA_NODE_ID
    ]
    if type is not None:
        candidates = [record for record in candidates if record.type == type]
    candidates.sort(key=lambda record: record.id)
    for record in candidates:
        yield record.id, record.embedding
