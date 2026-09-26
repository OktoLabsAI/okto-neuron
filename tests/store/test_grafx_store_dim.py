"""Tests for ``GrafxStore``'s ``embedding_dim`` constructor override (M4 spec).

``embedding_dim`` is the explicit width override used by ``cli/kg.py``'s
``_open_grafx_staged_store`` (see ``store/grafx.py:GrafxStore.__init__`` docstring)
to bootstrap a fresh staged graph at a caller-resolved width instead of
whatever ``okto-neuron.yaml`` says. It affects only the construction that
creates the schema; a later reopen still compares the stored width against
the vault's *configured* width (``okto-neuron.yaml``, or the shared default
when no config exists) exactly as before, ignoring the override.

Skips cleanly when the ``[grafx]`` extra isn't installed (``okto-grafx``),
matching ``tests/store/contract/conftest.py``'s ``graph_store`` fixture's own
``pytest.importorskip``-style handling of optional backend packages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("okto_grafx")

from okto_neuron.errors import EmbeddingDimMismatch  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402


def test_fresh_grafx_store_bootstraps_at_explicit_embedding_dim(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    store = GrafxStore(vault_path, embedding_dim=8)
    try:
        assert store._embedding_dim == 8

        spaces = {
            space.name: space for space in store._db.catalog.catalog.space_definitions
        }
        assert spaces["node_embed"].dimension == 8
    finally:
        store.close()


def test_reopen_at_different_configured_dim_raises_embedding_dim_mismatch(
    tmp_path: Path,
) -> None:
    """A graph bootstrapped at an overridden width (8) stays 8 forever --
    reopening it without that override falls back to the vault's configured
    width (the shared default, 384, since no ``okto-neuron.yaml`` exists here),
    which disagrees with what's actually stored and must fail loud rather
    than silently truncate/pad vectors."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    store = GrafxStore(vault_path, embedding_dim=8)
    store.close()

    with pytest.raises(EmbeddingDimMismatch) as exc_info:
        GrafxStore(vault_path)

    assert exc_info.value.stored_dim == 8
    assert exc_info.value.configured_dim == 384


def test_reopen_at_matching_configured_dim_succeeds(tmp_path: Path) -> None:
    """The mismatch check compares the stored width against the vault's
    *configured* width (``okto-neuron.yaml``), not against whatever override a
    reopen call happens to pass -- so a config that actually says 8 must let
    an 8-wide graph reopen cleanly, both with and without repeating the
    override."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {"marginalia_yaml_version": 1, "embedding": {"dimension": 8}}
        ),
        encoding="utf-8",
    )

    store = GrafxStore(vault_path, embedding_dim=8)
    store.close()

    reopened_with_override = GrafxStore(vault_path, embedding_dim=8)
    try:
        assert reopened_with_override._embedding_dim == 8
    finally:
        reopened_with_override.close()

    reopened_without_override = GrafxStore(vault_path)
    try:
        assert reopened_without_override._embedding_dim == 8
    finally:
        reopened_without_override.close()
