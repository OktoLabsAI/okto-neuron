"""M4 spec section 4, bullet 4 — Okto Grafx parity for the offline curation
verbs and CLI plumbing that route through the M4 "one construction seam
generalized" (``curation/orchestrate.py``'s ``open_staged_store``).

Two things are covered, both against the REAL production entry points (not
hand-rolled wiring) so this proves the seam, not a reimplementation of it:

* **Content parity.** ``reconcile.heal.heal_via_copy`` and ``cli.kg.
  kg_reembed`` each internally resolve ``_swap_construction_for(vault_path,
  backend_name)`` (``cli/kg.py``) to pick a backend's ``StagingPort`` +
  ``open_staged_store`` opener. Run each against a Ladybug vault and a Grafx
  vault seeded with the IDENTICAL fixture (same node/edge content, same
  embedding width, same deterministic ``StubEmbedder`` for reembed) and
  assert the two backends' resulting live graph content — read back through
  the same registry-driven ``_open_graph_store`` helper ``kg reindex``/``kg
  snapshot dump`` themselves use — is identical modulo nothing (every field,
  including the fixture's fixed ``created_at``, must round-trip losslessly
  through both Cypher dialects).
* **CLI smoke.** ``kg rebuild`` (via a stub ``ingest`` monkeypatched into
  ``cli.kg._build_fresh_graph``, the same seam
  ``tests/server/test_curation_rebuild.py``'s grafx daemon tests already
  prove works end-to-end for this backend), ``kg reindex``, and ``kg
  snapshot dump`` each exit 0 through :class:`click.testing.CliRunner`
  against a grafx-pinned vault, with no LLM call anywhere in the run.

Requires the ``[grafx]`` extra installed (``resolve_graph_backend("grafx")``
imports ``okto_neuron.store.grafx``, which does a hard ``import okto_grafx``)
— module-level ``pytest.importorskip``, matching
``tests/store/test_grafx_store_dim.py``'s own convention.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

pytest.importorskip("okto_grafx")

import okto_neuron.store._bootstrap as bootstrap_module  # noqa: E402
from click.testing import CliRunner  # noqa: E402
import okto_neuron.cli.kg as kg_cli  # noqa: E402
from okto_neuron.cli import app  # noqa: E402
from okto_neuron.cli.kg import kg_init, kg_reembed  # noqa: E402
from okto_neuron.core.schema import Edge, Node, Provenance  # noqa: E402
from okto_neuron.reconcile.heal import heal_via_copy  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402
from okto_neuron.store.ladybug import VaultConnection  # noqa: E402


# ── global-cache hygiene (mirrors tests/curation/test_orchestrate.py's own) ──
@pytest.fixture(autouse=True)
def _close_vault_handles():
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        try:
            store.close()
        except Exception:
            pass
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        try:
            handle.close()
        except Exception:
            pass
    bootstrap_module._bootstrap_cache.clear()


_CREATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PROVENANCE = Provenance(source="test", rule_id="orchestrate-grafx-fixture")
_DIM = 8


def _fixture_nodes(dim: int = _DIM) -> list[Node]:
    return [
        Node(
            id="doc:alpha",
            type="Document",
            title="Alpha",
            content="Alpha content",
            embedding=[0.1] * dim,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
        Node(
            id="doc:beta",
            type="Document",
            title="Beta",
            content="Beta content",
            embedding=[0.2] * dim,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
        Node(
            # No stored vector: exercises copy_graph_reembedding's
            # "copied, not recomputed" branch on both backends identically.
            id="doc:gamma",
            type="Document",
            title="Gamma",
            content="Gamma content",
            embedding=None,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
    ]


def _fixture_edges() -> list[Edge]:
    return [
        Edge(
            id="edge:alpha-beta",
            type="related_to",
            src="doc:alpha",
            dst="doc:beta",
            provenance=_PROVENANCE,
        ),
    ]


def _write_vault_config(
    vault_path: Path,
    *,
    backend: str | None,
    dim: int,
    embed_provider: str | None = None,
) -> None:
    """A minimal, schema-valid ``okto-neuron.yaml`` — same shape
    ``cli.kg._write_kg_init_vault_config`` writes, plus an explicit
    ``embedding.dimension`` override so both backends bootstrap at the
    fixture's width instead of the shared 384 default.
    """
    from okto_neuron.config import VaultConfig

    config = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    embedding = dict(config.get("embedding") or {})
    embedding["dimension"] = dim
    if embed_provider is not None:
        embedding["provider"] = embed_provider
    config["embedding"] = embedding
    if backend is not None:
        config["storage"] = {"backend": backend, "reason": None}
    vault_path.mkdir(parents=True, exist_ok=True)
    (vault_path / "okto-neuron.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


def _set_stub_embedding_provider(vault_path: Path) -> None:
    """Point the vault's embedding config at the deterministic, network-free
    ``StubEmbedder`` so ``kg_reembed`` never touches a real model — same
    provider for both backends, so their recomputed vectors are directly
    comparable (``StubEmbedder.embed`` is a pure sha256-of-text function,
    independent of which graph backend called it)."""
    config_path = vault_path / "okto-neuron.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    embedding = dict(data.get("embedding") or {})
    embedding["provider"] = "stub"
    data["embedding"] = embedding
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _populated_vault(tmp_path: Path, name: str, *, backend: str | None, dim: int = _DIM) -> Path:
    """A vault whose LIVE graph already carries the fixture nodes/edges,
    populated through the real registry-driven ``_open_graph_store`` (the
    same helper ``kg reindex``/``kg snapshot dump`` themselves use) rather
    than any backend-specific bootstrap trick — so a Ladybug vault
    (``backend=None``, matching the "absence means ladybug" pin default) and
    a Grafx vault (``backend="grafx"``) are populated through equally-real
    production code.
    """
    from okto_neuron.store.vault import _open_graph_store, _read_pinned_backends

    vault_path = tmp_path / name
    _write_vault_config(vault_path, backend=backend, dim=dim)
    graph_backend, _index_backend, storage_config = _read_pinned_backends(vault_path)
    store = _open_graph_store(vault_path, graph_backend, storage_config)
    for node in _fixture_nodes(dim):
        store.add_node(node)
    for edge in _fixture_edges():
        store.add_edge(edge)
    store.checkpoint()
    store.close()
    kg_cli._close_live_graph_handles(vault_path)
    return vault_path


def _read_back(vault_path: Path) -> tuple[list[Node], list[Edge]]:
    """Reopen the live graph (whichever backend it's pinned to) and return
    its content, sorted for comparison."""
    from okto_neuron.store.vault import _open_graph_store, _read_pinned_backends

    graph_backend, _index_backend, storage_config = _read_pinned_backends(vault_path)
    store = _open_graph_store(vault_path, graph_backend, storage_config)
    try:
        nodes = sorted(store.list_nodes(include_embedding=True), key=lambda n: n.id)
        edges = sorted(store.list_edges(), key=lambda e: e.id)
    finally:
        store.close()
    kg_cli._close_live_graph_handles(vault_path)
    return nodes, edges


def _dump(models: list[Node] | list[Edge]) -> list[dict[str, object]]:
    """``model_dump(mode="json")`` for the whole list — a datetime-safe,
    readable-diff-on-failure comparison basis (plain ``==`` on ``Node``/
    ``Edge`` objects works too, but a failed assertion's pytest diff is far
    less useful without this)."""
    return [m.model_dump(mode="json") for m in models]


# =============================================================================
# Group A — content parity: heal/reembed via the seam, Grafx vs. Ladybug.
# =============================================================================


def test_heal_via_copy_grafx_matches_ladybug_content(tmp_path: Path) -> None:
    """``reconcile.heal.heal_via_copy`` (the real offline CLI entry point,
    which resolves ``_swap_construction_for`` internally) against a Grafx
    vault produces the exact same post-heal node/edge content as the
    identical run against a Ladybug vault. No authority equivalence map
    exists in either vault, so both heals degenerate to a verbatim copy
    (matches ``tests/curation/test_orchestrate.py``'s own Ladybug-only heal
    test: 3 nodes kept, 0 dropped, 1 edge kept)."""
    ladybug_vault = _populated_vault(tmp_path, "ladybug-vault", backend=None)
    grafx_vault = _populated_vault(tmp_path, "grafx-vault", backend="grafx")

    _, ladybug_stats = heal_via_copy(ladybug_vault, return_stats=True)
    _, grafx_stats = heal_via_copy(grafx_vault, return_stats=True)

    # Full stats dicts match between backends (StagedSwapResult/copy_graph_
    # canonicalizing's own return value carries more than these 3 fields --
    # see the assertion below for the exact set this vault touches).
    assert ladybug_stats == grafx_stats
    assert ladybug_stats["nodes_kept"] == 3
    assert ladybug_stats["nodes_dropped"] == 0
    assert ladybug_stats["edges_kept"] == 1

    ladybug_nodes, ladybug_edges = _read_back(ladybug_vault)
    grafx_nodes, grafx_edges = _read_back(grafx_vault)

    assert _dump(ladybug_nodes) == _dump(grafx_nodes)
    assert _dump(ladybug_edges) == _dump(grafx_edges)


def test_kg_reembed_grafx_matches_ladybug_content(tmp_path: Path) -> None:
    """``cli.kg.kg_reembed`` (the real standalone reembed entry point)
    against a Grafx vault recomputes the exact same vectors as the identical
    run against a Ladybug vault, via the deterministic ``StubEmbedder`` —
    no network, no real model."""
    ladybug_vault = _populated_vault(tmp_path, "ladybug-vault", backend=None)
    grafx_vault = _populated_vault(tmp_path, "grafx-vault", backend="grafx")
    _set_stub_embedding_provider(ladybug_vault)
    _set_stub_embedding_provider(grafx_vault)

    assert kg_reembed(ladybug_vault) == 0
    assert kg_reembed(grafx_vault) == 0

    ladybug_stats = _reembed_stats(ladybug_vault)
    grafx_stats = _reembed_stats(grafx_vault)
    assert ladybug_stats == grafx_stats == {"nodes": 3, "edges": 1, "recomputed": 2, "copied": 1}

    ladybug_nodes, ladybug_edges = _read_back(ladybug_vault)
    grafx_nodes, grafx_edges = _read_back(grafx_vault)

    assert _dump(ladybug_nodes) == _dump(grafx_nodes)
    assert _dump(ladybug_edges) == _dump(grafx_edges)
    # doc:gamma never carried a vector -- copy_graph_reembedding copies it
    # verbatim (never fabricates one) on either backend.
    gamma = next(n for n in grafx_nodes if n.id == "doc:gamma")
    assert gamma.embedding is None


def test_kg_reembed_grafx_survives_embedder_dimension_change(tmp_path: Path) -> None:
    """Regression test for the "fenced forever" defect: a Grafx vault whose
    configured embedder DIMENSION changes must still be re-embeddable via
    ``kg reembed``.

    Before the fix, ``GrafxStore.__init__``'s dim-guard (``not fresh and
    self._embedding_dim != configured_dim`` -> ``EmbeddingDimMismatch``)
    fired on every open of an existing graph, including the live-graph
    read ``_kg_reembed_owned``/``_open_live_store`` itself performs to copy
    old-width vectors into the new width -- so the one operation that is
    supposed to *resolve* a dimension change instead raised the same error
    it exists to fix, permanently fencing the vault. This reproduces that
    exact sequence: ingest at ``_DIM``, change the configured width, then
    run the real ``kg reembed`` entry point end to end.
    """
    grafx_vault = _populated_vault(tmp_path, "grafx-vault", backend="grafx", dim=_DIM)
    _set_stub_embedding_provider(grafx_vault)

    new_dim = _DIM * 2
    _set_configured_embedding_dim(grafx_vault, new_dim)

    # Pre-fix, this raised EmbeddingDimMismatch(stored_dim=_DIM,
    # configured_dim=new_dim) out of GrafxStore.__init__ via
    # _open_live_store -- the vault never got a chance to actually reembed.
    assert kg_reembed(grafx_vault) == 0

    stats = _reembed_stats(grafx_vault)
    assert stats == {"nodes": 3, "edges": 1, "recomputed": 2, "copied": 1}

    nodes, _edges = _read_back(grafx_vault)
    alpha = next(n for n in nodes if n.id == "doc:alpha")
    assert alpha.embedding is not None
    assert len(alpha.embedding) == new_dim

    # The live graph's own stored width metadata now agrees with the new
    # configured width -- confirms this is a real re-width, not just
    # differently-sized vectors sitting in an unchanged-width column, and
    # that an ordinary (guarded) reopen no longer trips the dim-guard.
    from okto_neuron.store.grafx import GrafxStore

    reopened = GrafxStore(grafx_vault)
    try:
        assert reopened._embedding_dim == new_dim
    finally:
        reopened.close()
    kg_cli._close_live_graph_handles(grafx_vault)


def _set_configured_embedding_dim(vault_path: Path, dim: int) -> None:
    """Rewrite ``okto-neuron.yaml``'s configured embedding width in place --
    exactly what changing the embedder/model would do to a live vault's
    config, and the mutation ``kg reembed`` exists to read past."""
    config_path = vault_path / "okto-neuron.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    embedding = dict(data.get("embedding") or {})
    embedding["dimension"] = dim
    data["embedding"] = embedding
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _reembed_stats(vault_path: Path) -> dict[str, int]:
    state_path = vault_path / ".marginalia" / "reembed.state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    return {key: data[key] for key in ("nodes", "edges", "recomputed", "copied")}


# =============================================================================
# Group B — CLI smoke: kg rebuild/reindex/snapshot dump on a grafx vault via
# CliRunner, all model-free (stub ingest / no embedder call needed).
# =============================================================================


def test_kg_rebuild_grafx_via_cli_exits_zero_with_stub_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate HOME: the `app` group callback unconditionally calls
    # load_user_env_file(), which would otherwise leak this developer
    # machine's real ~/.marginalia/env secrets into the pytest process
    # (see tests/cli/conftest.py's docstring for the reproduction history).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path, backend="grafx") == 0
    kg_cli._close_live_graph_handles(vault_path)
    (vault_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (vault_path / "notes" / "b.md").write_text("# B\n", encoding="utf-8")

    def stub_ingest(path: Path, store: object) -> None:
        rel = path.relative_to(vault_path).as_posix()
        store.add_node(Node(id=f"n-{rel}", type="Concept", title=rel))

    orig_build = kg_cli._build_fresh_graph

    def patched_build(*args: object, **kwargs: object) -> object:
        kwargs["ingest"] = stub_ingest
        kwargs.pop("extractor", None)
        return orig_build(*args, **kwargs)

    monkeypatch.setattr(kg_cli, "_build_fresh_graph", patched_build)

    result = CliRunner().invoke(app, ["kg", "rebuild", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert (vault_path / "graph.grafx").is_dir()
    nodes, _edges = _read_back(vault_path)
    assert {n.id for n in nodes} == {"n-notes/a.md", "n-notes/b.md"}
    state = json.loads((vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "complete"


def test_kg_reindex_grafx_via_cli_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault_path = _populated_vault(tmp_path, "vault", backend="grafx")

    result = CliRunner().invoke(app, ["kg", "reindex", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert "doc_count=3" in result.output


def test_kg_snapshot_dump_grafx_via_cli_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault_path = _populated_vault(tmp_path, "vault", backend="grafx")
    dest = tmp_path / "snapshot"

    result = CliRunner().invoke(app, ["kg", "snapshot", "dump", str(vault_path), str(dest)])

    assert result.exit_code == 0, result.output
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["origin_backend"] == "grafx"
    assert manifest["node_count"] == 3
    assert manifest["edge_count"] == 1
