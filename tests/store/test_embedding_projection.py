"""Column projection: scans and batch reads do not select the vector column.

Query-shape tests (the statement text, not post-filtering) per backend, field
equivalence between the two projections, and the read-modify-write guard: a node
read through the helpers that feed ``add_node`` keeps its stored embedding.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store.index.indexed import IndexedStore
from okto_neuron.store.memory import InMemoryStore

DIM = 4


_STORE_DIM: dict[str, int] = {}


def _vec(i: float) -> list[float]:
    head = [i, 1.0, 0.5, 0.25]
    return head + [0.0] * (_STORE_DIM.get("dim", DIM) - DIM)


def _nodes() -> list[Node]:
    return [
        Node(
            id=f"n{i}",
            type="Claim" if i % 2 else "Concept",
            title=f"title {i}",
            content=f"content {i}",
            tags=["t"],
            facets={"P": "likes", "k": i},
            embedding=_vec(float(i)),
        )
        for i in range(1, 5)
    ]


def _other_fields(node: Node) -> dict:
    return node.model_dump(exclude={"embedding"})


def _fill(store) -> list[Node]:
    nodes = _nodes()
    for node in nodes:
        store.add_node(node)
    return nodes


def _make(kind: str, tmp_path: Path):
    if kind == "memory":
        return InMemoryStore()
    if kind == "grafx":
        pytest.importorskip("okto_grafx")
        from okto_neuron.store.grafx import GrafxStore

        return GrafxStore(tmp_path / "vault", embedding_dim=DIM)
    from okto_neuron.store.ladybug import LadybugStore

    store = LadybugStore(tmp_path / "vault")
    _STORE_DIM["dim"] = _ladybug_dim(tmp_path / "vault")
    return store


def _ladybug_dim(path: Path) -> int:
    from okto_neuron.store.schema import DEFAULT_EMBEDDING_DIM

    return DEFAULT_EMBEDDING_DIM  # bare bootstrap, no config file


@pytest.fixture(params=["memory", "grafx", "ladybug"])
def store(request, tmp_path: Path):
    _STORE_DIM.clear()
    s = _make(request.param, tmp_path)
    yield s
    s.close()


def test_default_reads_drop_the_vector_and_keep_every_other_field(store) -> None:
    written = {n.id: n for n in _fill(store)}
    light_list = list(store.list_nodes())
    light_type = list(store.list_nodes(type="Claim"))
    light_batch = store.get_nodes(list(written))
    assert [n.id for n in light_list] == sorted(written)
    for node in light_list + light_type + light_batch:
        assert node.embedding is None
    full_list = {n.id: n for n in store.list_nodes(include_embedding=True)}
    full_batch = {n.id: n for n in store.get_nodes(list(written), include_embedding=True)}
    for node_id, original in written.items():
        assert full_list[node_id].embedding == pytest.approx(original.embedding)
        assert full_batch[node_id].embedding == pytest.approx(original.embedding)
    for node in light_list:
        assert _other_fields(node) == _other_fields(full_list[node.id])
    assert {n.id for n in light_type} == {i for i, n in written.items() if n.type == "Claim"}


def test_single_read_keeps_the_vector_by_default_and_can_drop_it(store) -> None:
    _fill(store)
    assert store.get_node("n1").embedding == pytest.approx(_vec(1.0))
    light = store.get_node("n1", include_embedding=False)
    assert light is not None and light.embedding is None and light.title == "title 1"


def test_read_modify_write_helper_keeps_the_embedding(store) -> None:
    """The companion's batch helper feeds add_node (supersede/detach/revert)."""
    from okto_neuron.companion._incremental import _nodes_by_id

    _fill(store)
    claim = _nodes_by_id(store, ["n1"])["n1"]
    store.add_node(claim.model_copy(update={"facets": {**claim.facets, "_detached": True}}))
    after = store.get_node("n1")
    assert after.facets["_detached"] is True
    assert after.embedding == pytest.approx(_vec(1.0))


def test_a_light_read_written_back_keeps_the_vector(store) -> None:
    """Upserting a node with embedding=None preserves the stored vector."""
    _fill(store)
    light = store.get_nodes(["n1"])[0]
    assert light.embedding is None
    store.add_node(light.model_copy(update={"facets": {**light.facets, "touched": True}}))
    after = store.get_node("n1", include_embedding=True)
    assert after.facets["touched"] is True
    assert after.embedding == pytest.approx(_vec(1.0))
    # an otherwise identical light write is a no-op and leaves the vector too
    store.add_node(store.get_nodes(["n2"])[0])
    assert store.get_node("n2").embedding == pytest.approx(_vec(2.0))


def test_explicit_clear_erases_the_vector_and_a_new_node_stays_none(store) -> None:
    _fill(store)
    light = store.get_nodes(["n3"])[0]
    store.add_node(light, clear_embedding=True)
    assert store.get_node("n3").embedding is None
    store.add_node(Node(id="fresh", type="Concept", title="fresh"))
    assert store.get_node("fresh").embedding is None
    # a provided vector always replaces
    store.add_node(light.model_copy(update={"embedding": _vec(9.0)}))
    assert store.get_node("n3").embedding == pytest.approx(_vec(9.0))


def test_preserving_the_vector_does_not_change_the_generation(store) -> None:
    from okto_neuron.store.index import compute_graph_generation

    _fill(store)
    before = compute_graph_generation(store)
    light = store.get_nodes(["n1"])[0]
    store.add_node(light)  # no-op payload
    assert compute_graph_generation(store) == before


def test_grafx_keep_vector_statement_omits_the_embedding_set(tmp_path: Path) -> None:
    pytest.importorskip("okto_grafx")
    from okto_neuron.store.grafx import GrafxStore

    _STORE_DIM.clear()
    s = GrafxStore(tmp_path / "vault", embedding_dim=DIM)
    try:
        _fill(s)
        seen: list[str] = []
        original = s._execute_write

        def spy(statement, params):
            seen.append(statement)
            return original(statement, params)

        s._execute_write = spy  # type: ignore[method-assign]
        light = s.get_nodes(["n1"])[0]
        s.add_node(light.model_copy(update={"title": "changed"}))
        s.add_node(light.model_copy(update={"title": "changed again"}), clear_embedding=True)
        assert len(seen) == 2
        assert "embedding" not in seen[0]
        assert "n.embedding = $embedding" in seen[1]
    finally:
        s.close()


def _import_neo4j_module(monkeypatch: pytest.MonkeyPatch):
    """The real driver is optional; stub just enough of it to import the module."""
    import importlib
    import sys
    import types

    if importlib.util.find_spec("neo4j") is None:
        driver = types.ModuleType("neo4j")
        driver.GraphDatabase = object  # type: ignore[attr-defined]
        errors = types.ModuleType("neo4j.exceptions")
        for name in ("ClientError", "DriverError", "Neo4jError", "ServiceUnavailable"):
            setattr(errors, name, type(name, (Exception,), {}))
        monkeypatch.setitem(sys.modules, "neo4j", driver)
        monkeypatch.setitem(sys.modules, "neo4j.exceptions", errors)
        monkeypatch.delitem(sys.modules, "okto_neuron.store.neo4j", raising=False)
    return importlib.import_module("okto_neuron.store.neo4j")


def test_neo4j_keep_vector_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    neo = _import_neo4j_module(monkeypatch)
    _NODE_MERGE_CYPHER = neo._NODE_MERGE_CYPHER
    _NODE_MERGE_CYPHER_KEEP_VECTOR = neo._NODE_MERGE_CYPHER_KEEP_VECTOR

    assert "n.embedding = $embedding" in _NODE_MERGE_CYPHER
    assert "embedding" not in _NODE_MERGE_CYPHER_KEEP_VECTOR
    assert "n.schema_version = $schema_version" in _NODE_MERGE_CYPHER_KEEP_VECTOR


def test_index_keeps_the_indexed_vector_on_a_light_upsert() -> None:
    from okto_neuron.store.index import InMemoryIndexStore

    inner = InMemoryStore()
    index = InMemoryIndexStore()
    wrapped = IndexedStore(inner, index)
    _fill(wrapped)
    assert len(list(index.scan_vectors())) == 4
    light = wrapped.get_nodes(["n1"])[0]
    wrapped.add_node(light.model_copy(update={"title": "renamed"}))
    assert len(list(index.scan_vectors())) == 4, "index dropped a vector on a light upsert"
    assert wrapped.get_node("n1").embedding is not None
    wrapped.add_node(light, clear_embedding=True)
    assert len(list(index.scan_vectors())) == 3
    assert wrapped.get_node("n1").embedding is None


def test_planning_overlay_keeps_the_base_vector() -> None:
    from okto_neuron.companion import _PlanningGraphOverlay

    base = InMemoryStore()
    _fill(base)
    overlay = _PlanningGraphOverlay(base)
    light = base.get_nodes(["n1"])[0]
    overlay.add_node(light.model_copy(update={"title": "planned"}))
    planned = overlay.get_node("n1")
    assert planned.title == "planned" and planned.embedding == pytest.approx(_vec(1.0))
    overlay.add_node(light, clear_embedding=True)
    assert overlay.get_node("n1").embedding is None


def test_indexed_store_passes_the_switch_through() -> None:
    inner = InMemoryStore()
    _fill(inner)

    class _Index:
        def upsert(self, node): ...

    wrapped = IndexedStore(inner, _Index())  # type: ignore[arg-type]
    assert all(n.embedding is None for n in wrapped.list_nodes())
    assert all(n.embedding is not None for n in wrapped.list_nodes(include_embedding=True))
    assert wrapped.get_nodes(["n1"])[0].embedding is None
    assert wrapped.get_nodes(["n1"], include_embedding=True)[0].embedding is not None
    assert wrapped.get_node("n1").embedding is not None


def test_grafx_statement_shape(tmp_path: Path) -> None:
    pytest.importorskip("okto_grafx")
    from okto_neuron.store.grafx import GrafxStore

    _STORE_DIM.clear()
    s = GrafxStore(tmp_path / "vault", embedding_dim=DIM)
    try:
        _fill(s)
        seen: list[str] = []
        original = s._query

        def spy(statement, params=None):
            seen.append(statement)
            return original(statement, params)

        s._query = spy  # type: ignore[method-assign]
        list(s.list_nodes())
        list(s.list_nodes(type="Claim"))
        s.get_nodes(["n1", "n2"])
        s.get_node("n1", include_embedding=False)
        assert len(seen) == 4
        assert all("embedding" not in stmt for stmt in seen), seen
        seen.clear()
        list(s.list_nodes(include_embedding=True))
        list(s.list_nodes(type="Claim", include_embedding=True))
        s.get_nodes(["n1"], include_embedding=True)
        s.get_node("n1")
        assert len(seen) == 4
        assert all("n.embedding" in stmt for stmt in seen), seen
    finally:
        s.close()


def test_ladybug_statement_shape(tmp_path: Path) -> None:
    from okto_neuron.store.ladybug import LadybugStore

    _STORE_DIM.clear()
    s = LadybugStore(tmp_path / "vault")
    _STORE_DIM["dim"] = _ladybug_dim(tmp_path / "vault")
    try:
        _fill(s)
        seen: list[str] = []
        original = s._fetch_rows

        def spy(statement, params=None):
            seen.append(statement)
            return original(statement, params)

        s._fetch_rows = spy  # type: ignore[method-assign]
        list(s.list_nodes())
        list(s.list_nodes(type="Claim"))
        s.get_nodes(["n1"])
        s.get_node("n1", include_embedding=False)
        assert len(seen) == 4 and all("embedding" not in q for q in seen), seen
        seen.clear()
        list(s.list_nodes(include_embedding=True))
        list(s.list_nodes(type="Claim", include_embedding=True))
        s.get_nodes(["n1"], include_embedding=True)
        s.get_node("n1")
        assert len(seen) == 4 and all("n.embedding" in q for q in seen), seen
    finally:
        s.close()


def test_neo4j_mixin_statement_shape() -> None:
    from okto_neuron.store._generation_tag_base import GenerationScopedBackendMixin

    class _Fake(GenerationScopedBackendMixin):
        vault_id = "v"
        _generation_tag = "live"

        def __init__(self) -> None:
            self.seen: list[str] = []

        def _run_read(self, cypher, params):
            self.seen.append(cypher)
            return []

    fake = _Fake()
    fake.list_nodes()
    fake.list_nodes(type="Claim")
    fake.get_nodes(["a"])
    fake.get_node("a", include_embedding=False)
    assert len(fake.seen) == 4
    assert all("embedding" not in q and "AS n" in q for q in fake.seen), fake.seen
    fake.seen.clear()
    fake.list_nodes(include_embedding=True)
    fake.list_nodes(type="Claim", include_embedding=True)
    fake.get_nodes(["a"], include_embedding=True)
    fake.get_node("a")
    assert len(fake.seen) == 4
    assert all(" RETURN n" in q and "AS n" not in q for q in fake.seen), fake.seen


def test_companion_supersede_and_detach_keep_vectors_even_from_a_light_read(store) -> None:
    from okto_neuron.companion._incremental import _apply_detach, _apply_supersede

    _fill(store)
    light = {n.id: n for n in store.get_nodes(["n1", "n3"])}
    assert all(n.embedding is None for n in light.values())
    _apply_supersede(store, light["n1"], "n2", "2026-01-01")
    _apply_detach(store, light["n3"], "2026-01-01")
    for node_id, number in (("n1", 1.0), ("n3", 3.0)):
        kept = store.get_node(node_id)
        assert kept.embedding == pytest.approx(_vec(number))
    assert store.get_node("n1").facets["_superseded"] is True


def test_real_grafx_4096_dim_light_read_modify_write(tmp_path: Path) -> None:
    pytest.importorskip("okto_grafx")
    from okto_neuron.store.grafx import GrafxStore

    s = GrafxStore(tmp_path / "vault", embedding_dim=4096)
    try:
        vector = [((i * 7) % 13) / 13.0 for i in range(4096)]
        s.add_node(Node(id="big", type="Claim", title="t", facets={"a": 1}, embedding=vector))
        light = s.get_nodes(["big"])[0]
        assert light.embedding is None
        s.add_node(light.model_copy(update={"facets": {"a": 2}}))
        after = s.get_node("big", include_embedding=True)
        assert after.facets == {"a": 2}
        assert after.embedding == pytest.approx(vector, abs=1e-3)
        s.add_node(light, clear_embedding=True)
        assert s.get_node("big").embedding is None
    finally:
        s.close()
