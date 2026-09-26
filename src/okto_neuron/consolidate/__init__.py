"""Candidate primitives and deterministic deduplication for curation.

Public surface:
    NodeCandidate / EdgeCandidate
    collapse_duplicates(...)

Graph and queue writes belong to sealed, receipted companion plans.
"""

from __future__ import annotations

from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate._dedup import collapse_duplicates

__all__ = [
    "NodeCandidate",
    "EdgeCandidate",
    "collapse_duplicates",
]
