"""M3 spec section 2.5/2.6 — the backend pin, exercised through the CLI.

Covers ``okto-neuron init``, ``okto-neuron kg init``, and ``vault create``:
a matching ``--backend`` on re-init/re-open is unchanged, a real backend
conflict raises ``VaultBackendMismatch`` (not a silent reopen), and a typed
but unregistered backend name (``store/registry.py``'s ``NoSuchBackendError``)
fails a different, earlier way than a pin conflict does. The only backend
besides ``ladybug`` actually resolvable through the registry in this dev
tree is ``stub`` (``tests/fixtures/stub_backend_pkg``, installed via the
``dev`` dependency group's ``[tool.uv.sources]`` entry, M3 spec section
2.11) — it is what exercises the real ``VaultBackendMismatch`` path here.
``neo4j``/``grafx``/``neptune`` are typed :class:`~okto_neuron.config._vault.StorageConfig`
variants (M3 spec section 2.1/D-45) but are not registry-reachable this
milestone (D-44), so a CLI test using one of them exercises
``NoSuchBackendError`` instead, never the pin-conflict branch — that
distinction is asserted explicitly below rather than conflated.

The last two tests cover ``--backend stub`` end to end (create, open, add,
list) and a same-process reopen, which used to be a real gap found while
writing this file: the *first* open of a fresh ``stub``-pinned vault works
(``config/_vault.py``'s ``StorageConfig`` now has a ``CustomStorageConfig``
fallback variant for any backend name outside the four typed literals, so
the round-trip through ``VaultConfig`` succeeds), and a *second* open of
that same vault in the same process — via ``kg init`` again, ``Vault.open``
again, or any other call into ``store/vault.py::_open_vault`` — used to
crash with a raw ``AttributeError`` rather than a clean reopen.
``_open_vault``'s in-process cache-freshness check (``store/vault.py`` line
~52) reads ``cached.is_closed`` unconditionally; that attribute was not
part of the ``GraphStore`` Protocol (``store/protocol.py``) and was only
ever defined on ``LadybugStore``, so any other conformant backend —
including the real ``stub_backend_pkg`` registry-contract fixture —
tripped it. D-49 fixed this at the root: ``is_closed`` is now a read-only
property on the ``GraphStore`` Protocol itself, implemented by every
backend (``LadybugStore``, ``InMemoryStore``, ``StubGraphStore``) and
passed through explicitly by ``IndexedStore`` (the ``vault_module``'s
in-process cache always stores an ``IndexedStore``, never a bare graph
store), so the registry's structural validation now fails closed on any
backend that omits it. The M3 spec's own acceptance-scenario text (section
4, ``tests/acceptance/scenarios/99_backend_selection.sh``) assumes
``--backend stub`` "opens, kg add/kg query work, survives restart" — the
second test below now proves the in-process reopen half of that claim
directly, rather than pinning the crash as a known gap.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest

from okto_neuron.cli import app
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.vault import VaultBackendMismatch


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    return home


# --- matching backend on re-init: unchanged -------------------------------


def test_marginalia_init_reinit_with_matching_backend_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    first = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])
    assert first.exit_code == 0, first.output
    before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    second = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])

    assert second.exit_code == 0, second.output
    assert "initialized vault at" in second.output
    after = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert after == before


def test_kg_init_reinit_with_matching_backend_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    created = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])
    assert created.exit_code == 0, created.output
    before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    explicit = runner.invoke(app, ["kg", "init", str(vault_path), "--backend", "ladybug"])
    implicit = runner.invoke(app, ["kg", "init", str(vault_path)])

    assert explicit.exit_code == 0, explicit.output
    assert implicit.exit_code == 0, implicit.output
    assert "okto-neuron.yaml" in explicit.output
    assert "okto-neuron.yaml" in implicit.output
    after = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert after == before


# --- real backend conflict: VaultBackendMismatch --------------------------


def test_marginalia_init_backend_conflict_raises_vault_backend_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    created = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])
    assert created.exit_code == 0, created.output
    before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    result = runner.invoke(app, ["init", str(vault_path), "--backend", "stub"])

    assert result.exit_code == VaultBackendMismatch.EXIT_CODE == 11
    assert "pinned to backend 'ladybug'" in result.output
    assert "as backend 'stub'" in result.output
    after = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert after == before, "a rejected create/re-init must not mutate the existing config"


def test_kg_init_backend_conflict_raises_vault_backend_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    created = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])
    assert created.exit_code == 0, created.output
    before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    result = runner.invoke(app, ["kg", "init", str(vault_path), "--backend", "stub"])

    assert result.exit_code == VaultBackendMismatch.EXIT_CODE == 11
    assert "pinned to backend 'ladybug'" in result.output
    assert "as backend 'stub'" in result.output
    after = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert after == before, "a rejected kg init must not mutate the existing config"


# --- typed-but-unregistered name: a *different* failure than a pin conflict


def test_unregistered_backend_name_fails_with_bad_parameter_not_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``neptune`` is a real :class:`StorageConfig` variant (M3 spec 2.1) but is
    not registry-reachable (M5 registers ``neo4j``, not ``neptune``).
    ``_resolve_and_pin_backend`` validates the name via the registry *before*
    it ever checks the existing pin, so this fails as ``NoSuchBackendError``
    (exit code 2, a ``click.BadParameter``) — never as ``VaultBackendMismatch``
    (exit code 11) — regardless of what the target vault is already pinned to.
    """
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    created = runner.invoke(app, ["init", str(vault_path), "--backend", "ladybug"])
    assert created.exit_code == 0, created.output
    before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    via_init = runner.invoke(app, ["init", str(vault_path), "--backend", "neptune"])
    via_kg_init = runner.invoke(app, ["kg", "init", str(vault_path), "--backend", "neptune"])

    for result in (via_init, via_kg_init):
        assert result.exit_code == 2, result.output
        assert result.exit_code != VaultBackendMismatch.EXIT_CODE
        assert "no such graph backend is registered" in result.output
        assert "'neptune'" in result.output
    after = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert after == before


def test_unregistered_backend_name_fails_before_touching_a_fresh_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same registry check, on a path with no vault yet: fails just as fast,
    and creates nothing — the name lookup does not depend on a pin existing.
    """
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "never-created"

    result = CliRunner().invoke(app, ["init", str(vault_path), "--backend", "neptune"])

    assert result.exit_code == 2, result.output
    assert "no such graph backend is registered" in result.output
    assert not vault_path.exists()


# --- ``stub`` end to end: first open works; a same-process reopen is a
# --- currently real ``store/vault.py`` bug, not a config-validation gap.


def test_backend_stub_creates_opens_and_round_trips_a_node_on_first_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``okto-neuron init --backend stub`` genuinely works today: the registry
    resolves ``stub`` (the real ``tests/fixtures/stub_backend_pkg`` entry
    point), ``config/_vault.py``'s ``CustomStorageConfig`` fallback lets the
    written ``storage.backend: stub`` round-trip through ``VaultConfig``, and
    the CLI's own first open (inside ``Vault.init``) succeeds. This confirms
    the pin persists to disk and that the resulting live store is a real,
    working ``GraphStore`` — add/list round-trip through it directly, since
    the CLI itself has no ``kg add`` in this repo.
    """
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"

    result = CliRunner().invoke(app, ["init", str(vault_path), "--backend", "stub"])

    assert result.exit_code == 0, result.output
    yaml_path = vault_path / "okto-neuron.yaml"
    assert yaml_path.is_file()
    assert "backend: stub" in yaml_path.read_text(encoding="utf-8")

    from okto_neuron.core.schema import Node

    cached = vault_module._STORE_CACHE[vault_path.resolve(strict=False)]
    assert type(cached.graph).__name__ == "StubGraphStore"
    cached.add_node(Node(id="n1", type="Concept", title="hello", content="world"))
    assert [node.id for node in cached.list_nodes()] == ["n1"]


def test_reopening_a_stub_backed_vault_in_the_same_process_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-49 fix, pinned as a regression test: ``_open_vault``'s in-process
    cache-freshness check (``store/vault.py`` ~line 52) reads
    ``cached.is_closed`` unconditionally. ``is_closed`` is now part of the
    ``GraphStore`` Protocol (``store/protocol.py``), implemented by every
    backend (``LadybugStore``, ``InMemoryStore``, ``StubGraphStore``) and
    passed through explicitly by ``IndexedStore`` — the type actually cached
    in ``_STORE_CACHE`` — so reopening ANY conformant backend, including
    this real ``stub_backend_pkg`` fixture, works instead of raising a raw
    ``AttributeError``. This proves the M3 spec's
    ``99_backend_selection.sh`` acceptance scenario's "survives restart"
    claim for ``stub`` (section 4) in the one place a real process restart
    can't be exercised (CLI integration tests run in one process): close the
    cached handle out from under the cache the way the CLI's own ``kg init``
    does when it finishes, then reopen through the CLI again and confirm the
    fresh handle reads back the SAME graph content from disk.
    """
    _home(tmp_path, monkeypatch)
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    created = runner.invoke(app, ["init", str(vault_path), "--backend", "stub"])
    assert created.exit_code == 0, created.output

    from okto_neuron.core.schema import Node

    resolved = vault_path.resolve(strict=False)
    first_open = vault_module._STORE_CACHE[resolved]
    first_open.add_node(Node(id="n1", type="Concept", title="hello", content="world"))
    first_open.checkpoint()
    # Mimic the CLI's own close-when-done (kg_init's finally block): the
    # cache keeps the entry, but it is now closed underneath it.
    first_open.close()

    reopened = runner.invoke(app, ["kg", "init", str(vault_path)])

    assert reopened.exit_code == 0, reopened.output

    second_open = vault_module._STORE_CACHE[resolved]
    assert second_open is not first_open, "a closed cache entry must be dropped, not reused"
    assert [node.id for node in second_open.list_nodes()] == ["n1"]
