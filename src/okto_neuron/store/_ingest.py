"""Ingest callable contract for rebuilding Okto Neuron vault graphs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from okto_neuron.store.ladybug import LadybugStore


@runtime_checkable
class IngestCallable(Protocol):
    """FR10 callable boundary used by kg_rebuild and Topic 03 ingestion."""

    def __call__(self, path: Path, store: LadybugStore) -> None: ...


__all__ = ["IngestCallable"]
