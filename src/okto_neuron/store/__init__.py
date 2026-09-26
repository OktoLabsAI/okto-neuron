"""Graph-store package.

``okto_neuron.store``'s own top level stays importable without the optional
``[ladybug]`` extra installed — a real requirement for ``okto-neuron[grafx]``:
``store/grafx.py`` does ``from okto_neuron.store import schema``, which (since
``schema`` is a submodule, not an attribute this package defines) still runs
this file's top level first. ``store/ladybug.py`` and ``store/_bootstrap.py``
both do a hard, module-level ``import ladybug``; ``store/_ingest.py`` and
``store/vault.py`` each import ``LadybugStore`` unconditionally too, so the
five names below that trace back to one of those four modules
(``LadybugStore``, ``_bootstrap_cache``, ``bootstrap_vault_graph``,
``IngestCallable``, ``_open_vault``) are resolved lazily through
``__getattr__`` (PEP 562) instead of being imported eagerly here. ``GraphStore``
and ``InMemoryStore`` have no such dependency and stay eager.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.protocol import GraphStore

if TYPE_CHECKING:
    from okto_neuron.store._bootstrap import _bootstrap_cache, bootstrap_vault_graph
    from okto_neuron.store._ingest import IngestCallable
    from okto_neuron.store.ladybug import LadybugStore
    from okto_neuron.store.neo4j import Neo4jStore
    from okto_neuron.store.vault import _open_vault

__all__ = [
    "GraphStore",
    "InMemoryStore",
    "IngestCallable",
    "LadybugStore",
    "Neo4jStore",
    "_bootstrap_cache",
    "_open_vault",
    "bootstrap_vault_graph",
]

# name -> (module to import lazily, attribute on that module).
_LAZY: dict[str, tuple[str, str]] = {
    "LadybugStore": ("okto_neuron.store.ladybug", "LadybugStore"),
    "Neo4jStore": ("okto_neuron.store.neo4j", "Neo4jStore"),
    "_bootstrap_cache": ("okto_neuron.store._bootstrap", "_bootstrap_cache"),
    "bootstrap_vault_graph": ("okto_neuron.store._bootstrap", "bootstrap_vault_graph"),
    "IngestCallable": ("okto_neuron.store._ingest", "IngestCallable"),
    "_open_vault": ("okto_neuron.store.vault", "_open_vault"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == "ladybug":
            raise ModuleNotFoundError(
                f"okto_neuron.store.{name} requires the optional 'ladybug' package; "
                "install it with the 'okto-neuron[ladybug]' extra (or 'okto-neuron[serve]', "
                "which includes it).",
                name="ladybug",
            ) from exc
        raise
    value = getattr(module, attr_name)
    globals()[name] = value  # cache on the package so repeat access skips __getattr__
    return value
