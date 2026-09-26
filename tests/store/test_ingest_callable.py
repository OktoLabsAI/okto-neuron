from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

import okto_neuron
from okto_neuron.store import IngestCallable
from okto_neuron.store.ladybug import LadybugStore


def test_ingest_callable_protocol_exists_with_stable_signature() -> None:
    assert inspect.isclass(IngestCallable)

    signature = inspect.signature(IngestCallable.__call__)
    assert list(signature.parameters) == ["self", "path", "store"]

    hints = get_type_hints(IngestCallable.__call__)
    assert hints == {"path": Path, "store": LadybugStore, "return": type(None)}


def test_plain_function_is_structurally_compatible() -> None:
    def ingest(path: Path, store: LadybugStore) -> None:
        return None

    assigned: IngestCallable = ingest

    assert assigned is ingest
    assert isinstance(ingest, IngestCallable)


def test_ingest_callable_is_importable_from_store() -> None:
    from okto_neuron import store

    assert store.IngestCallable is IngestCallable


def test_ingest_callable_is_not_exported_from_top_level_package() -> None:
    assert not hasattr(okto_neuron, "IngestCallable")
    with pytest.raises(ImportError):
        exec("from okto_neuron import IngestCallable", {})
