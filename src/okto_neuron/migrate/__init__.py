"""Live, per-commit graph structure derivation.

This package no longer hosts bulk in-place migrations. Structural heals belong to
``kg rebuild``: derive the complete graph in a fresh generation, verify it, then
atomic-swap. The Ladybug store independently enforces immutable edge topology and
updates only mutable payload in place.

What survives here is ``ensure_source_mentions``, called per-commit from
``companion.remember`` against the just-committed node set on a freshly-built store.
"""

from __future__ import annotations

from okto_neuron.migrate.bridge_edges import ensure_source_mentions

__all__ = ["ensure_source_mentions"]
