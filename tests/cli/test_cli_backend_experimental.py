"""Grafx is the default, non-experimental graph backend (owner decision
retiring D-12) at every vault-creation CLI entry point: ``kg init``,
``okto-neuron init``, ``vault create``, ``onboard``.

D-12's consent gate (``_confirm_experimental_backend`` in ``cli/__init__.py``)
was removed entirely: no official backend (``ladybug``/``grafx``/``neo4j``) is
D-12-gated any more, so selecting ``--backend grafx`` at any of the four entry
points now succeeds without ``--accept-experimental`` -- and without any
vault write being blocked. The flag itself is still accepted (deprecated,
accepted-and-ignored) so an old script that still passes it keeps working
identically. ``okto-neuron init``/``vault create``/``onboard`` also default
``--backend`` to ``grafx`` outright, so a genuinely new vault with no
``--backend`` flag at all is pinned to ``grafx`` too, needing no consent flag
either.

Requires the ``[grafx]`` extra installed: ``_resolve_and_pin_backend`` (still
called at every entry point) resolves the backend name via
``resolve_graph_backend`` -- which imports ``okto_neuron.store.grafx`` (a hard
``import okto_grafx``). Module-level ``pytest.importorskip``, matching
``tests/store/test_grafx_store_dim.py``'s own convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("okto_grafx")

from click.testing import CliRunner  # noqa: E402

from okto_neuron.cli import app  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        try:
            store.close()
        except Exception:
            pass
    vault_module._STORE_CACHE.clear()


def _pinned_backend(vault_path: Path) -> str:
    config = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    return config["storage"]["backend"]


def _assert_reopens_empty(vault_path: Path) -> None:
    """Reopen through the same registry-driven path a real daemon/CLI open
    uses (``store.vault._open_vault``), and confirm it comes back empty and
    closes cleanly -- proving the freshly-bootstrapped Grafx graph is not
    just present on disk but actually usable."""
    from okto_neuron.store.vault import _open_vault

    store = _open_vault(vault_path)
    try:
        assert list(store.list_nodes()) == []
    finally:
        store.close()


# ── kg init ──────────────────────────────────────────────────────────────


def test_kg_init_grafx_without_accept_experimental_succeeds(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(app, ["kg", "init", str(vault_path), "--backend", "grafx"])

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    assert (vault_path / "graph.grafx").is_dir()
    _assert_reopens_empty(vault_path)


def test_kg_init_grafx_with_accept_experimental_still_accepted(tmp_path: Path) -> None:
    """The deprecated flag is accepted-and-ignored -- an old script that
    still passes it must not break."""
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(
        app, ["kg", "init", str(vault_path), "--backend", "grafx", "--accept-experimental"]
    )

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


# ── okto-neuron init ─────────────────────────────────────────────────────


def test_marginalia_init_grafx_without_accept_experimental_succeeds(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(app, ["init", str(vault_path), "--backend", "grafx"])

    assert result.exit_code == 0, result.output
    assert "initialized vault" in result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_marginalia_init_grafx_with_accept_experimental_still_accepted(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(
        app, ["init", str(vault_path), "--backend", "grafx", "--accept-experimental"]
    )

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_marginalia_init_no_backend_flag_defaults_to_grafx_with_no_consent(
    tmp_path: Path,
) -> None:
    """Fresh vault, no ``--backend`` flag at all: pinned ``grafx`` in
    ``okto-neuron.yaml``, no ``--accept-experimental`` needed."""
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(app, ["init", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


# ── vault create ────────────────────────────────────────────────────────


def test_vault_create_grafx_without_accept_experimental_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["vault", "create", "grafx-vault", "--backend", "grafx"])

    assert result.exit_code == 0, result.output
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "grafx-vault"
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_vault_create_grafx_with_accept_experimental_still_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        ["vault", "create", "grafx-vault", "--backend", "grafx", "--accept-experimental"],
    )

    assert result.exit_code == 0, result.output
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "grafx-vault"
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_vault_create_no_backend_flag_defaults_to_grafx_with_no_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["vault", "create", "default-vault"])

    assert result.exit_code == 0, result.output
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "default-vault"
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


# ── onboard ─────────────────────────────────────────────────────────────


def test_onboard_grafx_without_accept_experimental_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            str(vault_path),
            "--backend",
            "grafx",
            "--non-interactive",
            "--disable-llm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_onboard_grafx_with_accept_experimental_still_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            str(vault_path),
            "--backend",
            "grafx",
            "--accept-experimental",
            "--non-interactive",
            "--disable-llm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


def test_onboard_no_backend_flag_defaults_to_grafx_with_no_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            str(vault_path),
            "--non-interactive",
            "--disable-llm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _pinned_backend(vault_path) == "grafx"
    _assert_reopens_empty(vault_path)


# ── kg init: no --backend on a fresh vault keeps the pre-existing,
#    unrelated legacy behavior (ladybug), NOT the new grafx default ────────


def test_kg_init_no_backend_flag_on_a_fresh_vault_stays_ladybug(tmp_path: Path) -> None:
    """``kg init`` with no ``--backend`` bypasses ``Vault.scaffold`` entirely
    (M3 spec D-46) and always has: it defers straight to
    ``store.vault._open_vault``'s own scaffolding rather than pinning
    anything explicitly, so a genuinely fresh vault created this way still
    gets no ``storage`` key at all -- the pre-existing ``"ladybug"``
    absent-key fallback -- unaffected by ``DEFAULT_NEW_VAULT_BACKEND``
    becoming ``grafx`` for the other three entry points."""
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(app, ["kg", "init", str(vault_path)])

    assert result.exit_code == 0, result.output
    config = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    assert "storage" not in config
    assert (vault_path / "graph.lbug").exists()
