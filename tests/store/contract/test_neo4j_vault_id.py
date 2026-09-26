"""Neo4j ``storage.vault_id`` persistence + legacy-adoption contract (D-83).

Companion to `test_neo4j_generation.py`: same live-container guard shape,
same "no mocks" convention. Covers the internal ADR 0041 plan
D-83 -- `Neo4jStore` now scopes reads/writes by a `storage.vault_id`
recorded in `okto-neuron.yaml` instead of re-deriving a hash from the
resolved vault path on every open, so a vault directory can be copied or
moved without disconnecting it from its graph. Every vault this module
creates is torn down (`wipe_vault`-style manual delete via `Neo4jStore`)
regardless of pass/fail, so no test data survives against the shared CE
instance.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

pytest.importorskip("neo4j")

_URI = os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
if not _URI:
    pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set", allow_module_level=True)
_CREDENTIAL_ENV = os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")

from okto_neuron.core.schema import Node, Provenance  # noqa: E402
from okto_neuron.store.neo4j import Neo4jStore, vault_id_for  # noqa: E402


class _Cfg:
    backend = "neo4j"
    database = "neo4j"

    def __init__(self, *, vault_id: str | None = None) -> None:
        self.uri = _URI
        self.credential_env = _CREDENTIAL_ENV
        self.allow_remote = False
        self.vault_id = vault_id


def _node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type="Concept",
        title=node_id,
        content=f"content for {node_id}",
        tags=["m5-vault-id"],
        facets={},
        provenance=Provenance(source="ingest", layer="deterministic"),
    )


def _fresh_vault_path(tmp_path: Path) -> Path:
    return tmp_path / f"vault-{uuid.uuid4().hex[:8]}"


def _wipe(vault_path: Path, config: _Cfg) -> None:
    store = Neo4jStore(vault_path, config=config)
    try:
        with store._driver.session(database=store._database) as session:  # noqa: SLF001
            session.run(
                "MATCH (n:Node {vault_id: $vault_id}) DETACH DELETE n",
                vault_id=store.vault_id,
            )
    finally:
        store.close()


def _write_marginalia_yaml(vault_path: Path, *, storage: dict[str, object]) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    config = {
        "marginalia_yaml_version": 1,
        "vault_id": vault_path.name,
        "federation_opt_in": False,
        "packs": [],
        "embedding": {"provider": "stub", "model": "stub", "dimension": 8},
        "storage": storage,
    }
    (vault_path / "okto-neuron.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# A new vault (configured storage.vault_id) is scoped by that id.
# ---------------------------------------------------------------------------


def test_new_vault_uses_configured_vault_id(tmp_path: Path) -> None:
    vault_path = _fresh_vault_path(tmp_path)
    configured_id = uuid.uuid4().hex
    cfg = _Cfg(vault_id=configured_id)
    try:
        store = Neo4jStore(vault_path, config=cfg)
        try:
            assert store.vault_id == configured_id
            store.add_node(_node("n1"))
            assert store.get_node("n1") is not None
        finally:
            store.close()
    finally:
        _wipe(vault_path, cfg)


# ---------------------------------------------------------------------------
# A legacy vault (no storage.vault_id on disk) opens against its existing
# path-hash-scoped nodes, reads them, and gains the field.
# ---------------------------------------------------------------------------


def test_legacy_vault_adopts_path_hash_vault_id(tmp_path: Path) -> None:
    vault_path = _fresh_vault_path(tmp_path)
    _write_marginalia_yaml(
        vault_path,
        storage={"backend": "neo4j", "uri": _URI, "credential_env": _CREDENTIAL_ENV},
    )
    legacy_id = vault_id_for(vault_path.expanduser().resolve(strict=False))
    cfg = _Cfg(vault_id=None)
    try:
        # First open: no vault_id in the config object (as a pre-D-83 caller
        # would build it from a okto-neuron.yaml with no storage.vault_id
        # key), so the store must fall back to the legacy hash.
        store = Neo4jStore(vault_path, config=cfg)
        try:
            assert store.vault_id == legacy_id
            store.add_node(_node("legacy-n1"))
        finally:
            store.close()

        on_disk = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
        assert on_disk["storage"]["vault_id"] == legacy_id

        # Second open with a config that (like a real VaultConfig.load()
        # reload) now carries the adopted id -- must read back the same node.
        cfg2 = _Cfg(vault_id=legacy_id)
        store2 = Neo4jStore(vault_path, config=cfg2)
        try:
            assert store2.get_node("legacy-n1") is not None
        finally:
            store2.close()
    finally:
        _wipe(vault_path, _Cfg(vault_id=legacy_id))


# ---------------------------------------------------------------------------
# cp -r portability: a configured vault_id survives a directory copy to a
# new path, and the copy sees the same graph.
# ---------------------------------------------------------------------------


def test_cp_r_portability_with_configured_vault_id(tmp_path: Path) -> None:
    original = _fresh_vault_path(tmp_path)
    configured_id = uuid.uuid4().hex
    cfg = _Cfg(vault_id=configured_id)
    try:
        store = Neo4jStore(original, config=cfg)
        try:
            store.add_node(_node("port-n1"))
            store.add_node(_node("port-n2"))
        finally:
            store.close()

        original.mkdir(parents=True, exist_ok=True)
        copy_path = tmp_path / f"vault-copy-{uuid.uuid4().hex[:8]}"
        shutil.copytree(original, copy_path)

        # Same config (same configured vault_id) reopened at the new path.
        copy_store = Neo4jStore(copy_path, config=cfg)
        try:
            assert copy_store.vault_id == configured_id
            assert len(list(copy_store.list_nodes())) == 2
            assert copy_store.get_node("port-n1") is not None
            assert copy_store.get_node("port-n2") is not None
        finally:
            copy_store.close()
    finally:
        _wipe(original, cfg)
