"""Column projection: scans and batch reads do not select the vector column.

Query-shape tests (the statement text, not post-filtering) per backend, field
equivalence between the two projections, and the read-modify-write guard: a node
read through the helpers that feed ``add_node`` keeps its stored embedding.
"""

from __future__ import annotations

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


def test_a_light_read_written_back_would_erase_the_vector(store) -> None:
    """Documents why the default is unsafe for write-back paths."""
    _fill(store)
    light = store.get_nodes(["n1"])[0]
    store.add_node(light)
    assert store.get_node("n1").embedding is None


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
