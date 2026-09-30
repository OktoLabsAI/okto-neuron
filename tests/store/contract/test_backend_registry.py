"""Entry-point registry contract (M3 spec §2.2/§4): ``store/registry.py``
and its ``store/index/registry.py`` mirror.

Both registries resolve a backend name to a class from an in-tree official
map first, then the matching entry-point group, and fail closed at
*registration* time -- a missing method or an incompatible method signature
raises before the class is ever constructed, not on first write.
"""

from __future__ import annotations

import pytest

from okto_neuron.store import registry
from okto_neuron.store.index import registry as index_registry
from okto_neuron.store.index.default import DefaultIndexStore
from okto_neuron.store.ladybug import LadybugStore

# --- fixture classes for the "fails at registration" tests ---------------
#
# Defined at module scope (not inside the test function) so `_import_target`
# -- `importlib.import_module(module_name)` + `getattr(module, attr)` -- can
# actually find them by name; registration is exercised through the public
# `resolve_graph_backend`/`resolve_index_backend` API via a monkeypatched
# `_OFFICIAL` entry, never by calling the private validator directly.


class _GraphStoreMissingAddEdge:
    """Structurally almost-GraphStore: every member except add_edge (is_closed
    included, D-49 — otherwise this would fail on "is_closed" too and no
    longer isolate the one member this fixture means to omit)."""

    def add_node(self, node): ...
    def get_node(self, node_id, include_embedding=True): ...
    def get_nodes(self, node_ids, include_embedding=False):
        return []

    def list_nodes(self, type=None, include_embedding=False):
        return []

    def list_edges(self, src=None, dst=None, type=None):
        return []

    def checkpoint(self): ...
    def close(self): ...

    @property
    def is_closed(self):
        return False

    def generation(self):
        return ""

    def health(self): ...
    def recovery_status(self): ...
    def detect_drift(self, expected_generation):
        return None


class _GraphStoreIncompatibleAddEdge:
    """Has every GraphStore member, but add_edge takes an extra required arg."""

    def add_node(self, node): ...
    def add_edge(self, edge, extra_required_arg): ...
    def get_node(self, node_id, include_embedding=True): ...
    def get_nodes(self, node_ids, include_embedding=False):
        return []

    def list_nodes(self, type=None, include_embedding=False):
        return []

    def list_edges(self, src=None, dst=None, type=None):
        return []

    def checkpoint(self): ...
    def close(self): ...

    @property
    def is_closed(self):
        return False

    def generation(self):
        return ""

    def health(self): ...
    def recovery_status(self): ...
    def detect_drift(self, expected_generation):
        return None


class _GraphStoreMissingIsClosed:
    """Has every GraphStore method, but omits the ``is_closed`` property
    (D-49) -- the fixture that pins the new member is actually enforced,
    not silently exempted from registration."""

    def add_node(self, node): ...
    def add_edge(self, edge): ...
    def get_node(self, node_id, include_embedding=True): ...
    def get_nodes(self, node_ids, include_embedding=False):
        return []

    def list_nodes(self, type=None, include_embedding=False):
        return []

    def list_edges(self, src=None, dst=None, type=None):
        return []

    def checkpoint(self): ...
    def close(self): ...
    def generation(self):
        return ""

    def health(self): ...
    def recovery_status(self): ...
    def detect_drift(self, expected_generation):
        return None


class _GraphStoreIsClosedAsMethod:
    """Has every GraphStore member by name, but ``is_closed`` is a plain
    method (``def is_closed(self)``) instead of a ``@property`` (D-49).
    ``hasattr`` alone can't tell those apart -- every caller reads
    ``is_closed`` as a value (``store/vault.py``'s ``not cached.is_closed``),
    and a bound method is always truthy, so this must fail at registration
    rather than silently defeating that check at runtime."""

    def add_node(self, node): ...
    def add_edge(self, edge): ...
    def get_node(self, node_id, include_embedding=True): ...
    def get_nodes(self, node_ids, include_embedding=False):
        return []

    def list_nodes(self, type=None, include_embedding=False):
        return []

    def list_edges(self, src=None, dst=None, type=None):
        return []

    def checkpoint(self): ...
    def close(self): ...
    def is_closed(self):
        return False

    def generation(self):
        return ""

    def health(self): ...
    def recovery_status(self): ...
    def detect_drift(self, expected_generation):
        return None


class _IndexStoreMissingDelete:
    """Structurally almost-IndexStore: every method except delete."""

    def upsert(self, node): ...
    def search_text(self, query, k=10, type=None):
        return []

    def scan_vectors(self, type=None):
        return []

    def search_vector(self, embedding, k, type=None):
        return None

    def invalidate_by_facet(self, predicate):
        return 0

    def stats(self): ...
    def generation(self):
        return ""

    def clear(self): ...
    def checkpoint(self): ...
    def close(self): ...


# --- store/registry.py -----------------------------------------------------


def test_resolve_graph_backend_ladybug_returns_ladybug_store() -> None:
    assert registry.resolve_graph_backend("ladybug") is LadybugStore


def test_resolve_graph_backend_unknown_name_raises() -> None:
    with pytest.raises(registry.NoSuchBackendError):
        registry.resolve_graph_backend("nope")


def test_resolve_graph_backend_stub_resolves_via_entry_point_with_no_in_tree_import() -> None:
    """The stub package (tests/fixtures/stub_backend_pkg) is never imported by
    src/okto_neuron -- it must resolve purely through the
    marginalia.graph_backends entry point (M3 spec §2.11)."""
    stub_backend_pkg = pytest.importorskip("stub_backend_pkg")

    resolved = registry.resolve_graph_backend("stub")

    assert resolved is stub_backend_pkg.StubGraphStore


def test_list_graph_backends_names_grafx_first_without_importing_or_validating() -> None:
    """Grafx is the default, non-experimental graph backend (owner decision
    retiring D-12), so ``_OFFICIAL``'s declaration order -- and this
    enumeration -- puts it first; ladybug and neo4j remain fully supported,
    selectable backends right behind it."""
    names = registry.list_graph_backends()
    assert names[0] == "grafx"
    assert "ladybug" in names
    assert "neo4j" in names
    assert len(names) == len(set(names))  # deduplicated


def test_resolve_graph_backend_missing_method_fails_at_registration(monkeypatch) -> None:
    monkeypatch.setitem(
        registry._OFFICIAL,
        "_test_missing_add_edge",
        f"{__name__}:_GraphStoreMissingAddEdge",
    )

    with pytest.raises(registry.NoSuchBackendError, match="add_edge"):
        registry.resolve_graph_backend("_test_missing_add_edge")


def test_resolve_graph_backend_incompatible_signature_fails_at_registration(monkeypatch) -> None:
    """An override with an extra required parameter must fail at resolution,
    not silently pass through and blow up on the backend's first write."""
    monkeypatch.setitem(
        registry._OFFICIAL,
        "_test_incompatible_add_edge",
        f"{__name__}:_GraphStoreIncompatibleAddEdge",
    )

    with pytest.raises(registry.NoSuchBackendError, match="add_edge"):
        registry.resolve_graph_backend("_test_incompatible_add_edge")


def test_resolve_graph_backend_missing_is_closed_fails_at_registration(monkeypatch) -> None:
    """D-49: ``is_closed`` is now a required ``GraphStore`` Protocol member (a
    read-only property, not a method) -- a class that omits it must fail
    closed at registration, the same as omitting any other required member.
    Also pins that a non-callable Protocol member does not break
    registration itself (``issubclass()`` against a ``@runtime_checkable``
    Protocol with a non-method member raises ``TypeError``, which
    ``_validate_backend_class`` must not let escape as an unrelated crash)."""
    monkeypatch.setitem(
        registry._OFFICIAL,
        "_test_missing_is_closed",
        f"{__name__}:_GraphStoreMissingIsClosed",
    )

    with pytest.raises(registry.NoSuchBackendError, match="is_closed"):
        registry.resolve_graph_backend("_test_missing_is_closed")


def test_resolve_graph_backend_is_closed_as_a_method_fails_at_registration(monkeypatch) -> None:
    """D-49: ``is_closed`` must be a property, not merely present under that
    name -- a plain method of the same name passes a bare ``hasattr`` check
    but is always truthy when read as a value (never called), which would
    silently defeat ``store/vault.py``'s ``not cached.is_closed`` freshness
    check instead of failing loudly here."""
    monkeypatch.setitem(
        registry._OFFICIAL,
        "_test_is_closed_as_method",
        f"{__name__}:_GraphStoreIsClosedAsMethod",
    )

    with pytest.raises(registry.NoSuchBackendError, match="is_closed"):
        registry.resolve_graph_backend("_test_is_closed_as_method")


# --- store/index/registry.py (mirrors the above) ----------------------------


def test_resolve_index_backend_default_returns_default_index_store() -> None:
    assert index_registry.resolve_index_backend("ladybug_bm25+vector_scan") is DefaultIndexStore


def test_resolve_index_backend_unknown_name_raises() -> None:
    with pytest.raises(index_registry.NoSuchIndexBackendError):
        index_registry.resolve_index_backend("nope")


def test_list_index_backends_names_default_first() -> None:
    names = index_registry.list_index_backends()
    assert names[0] == "ladybug_bm25+vector_scan"
    assert len(names) == len(set(names))


def test_resolve_index_backend_missing_method_fails_at_registration(monkeypatch) -> None:
    monkeypatch.setitem(
        index_registry._OFFICIAL,
        "_test_missing_delete",
        f"{__name__}:_IndexStoreMissingDelete",
    )

    with pytest.raises(index_registry.NoSuchIndexBackendError, match="delete"):
        index_registry.resolve_index_backend("_test_missing_delete")
