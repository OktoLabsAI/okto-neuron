"""Unit tests for the pure vault-selection seam (ADR 0014).

``resolve_vault_selector`` is the multi-tenant auth seam and is deliberately
testable without FastMCP. It maps a ``?vault=`` selector (None / name / absolute
path) + loopback flag + server state to a live vault handle, or a typed
``VaultResolutionError``.
"""

from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from okto_neuron.server.runtime import VaultResolutionError, resolve_vault_selector
from okto_neuron.server.state import ServerState
from okto_neuron.vault import Vault


@pytest.fixture
def registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the global vault root to a tmp HOME so named lookups are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    return home / ".okto-neuron" / "vaults"


def _state(vault: Vault | None, path: Path | None) -> ServerState:
    resolved = path.resolve(strict=False) if path is not None else None
    return ServerState(vault=vault, vault_path=resolved)


def _configure_roots(registry_home: Path, *roots: Path) -> None:
    config_path = registry_home.parent / "okto-neuron.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    encoded_roots = ", ".join(json.dumps(str(root)) for root in roots)
    config_path.write_text(
        f"marginalia_toml_version = 1\nvault_roots = [{encoded_roots}]\n",
        encoding="utf-8",
    )


def test_none_selector_returns_active(tmp_path: Path) -> None:
    active = Vault.init(tmp_path / "active", packs=["core"])
    state = _state(active, tmp_path / "active")
    state.vault_pool.adopt(active, state.vault_path)
    try:
        assert resolve_vault_selector(None, is_loopback=True, state=state) is active
    finally:
        state.close()


def test_none_selector_no_registered_vault_raises(registry_home: Path) -> None:
    assert not registry_home.exists()
    state = _state(None, None)
    with pytest.raises(VaultResolutionError) as excinfo:
        resolve_vault_selector(None, is_loopback=True, state=state)
    assert excinfo.value.code == "no_vault"


def test_none_selector_uses_sole_registered_vault(registry_home: Path) -> None:
    target = registry_home / "only"
    Vault.init(target, packs=["core"]).close()
    state = _state(None, None)
    try:
        resolved = resolve_vault_selector(None, is_loopback=True, state=state)
        assert resolved.path.resolve(strict=False) == target.resolve(strict=False)
        assert state.vault_path is None
    finally:
        state.close()


def test_none_selector_uses_configured_default(registry_home: Path) -> None:
    from okto_neuron.vault_registry import set_default_vault

    targets = [registry_home / "alpha", registry_home / "beta"]
    for target in targets:
        Vault.init(target, packs=["core"]).close()
    set_default_vault(targets[1])
    state = _state(None, None)
    try:
        resolved = resolve_vault_selector(None, is_loopback=True, state=state)
        assert resolved.path.resolve(strict=False) == targets[1].resolve(strict=False)
        assert state.vault_path is None
    finally:
        state.close()


def test_none_selector_requires_choice_when_multiple_have_no_default(
    registry_home: Path,
) -> None:
    for name in ("alpha", "beta"):
        Vault.init(registry_home / name, packs=["core"]).close()
    state = _state(None, None)
    with pytest.raises(VaultResolutionError) as excinfo:
        resolve_vault_selector(None, is_loopback=True, state=state)
    assert excinfo.value.code == "vault_selector_required"


def test_registered_name_resolves_via_pool(registry_home: Path) -> None:
    target = registry_home / "myproject"
    Vault.init(target, packs=["core"]).close()
    state = _state(None, None)
    try:
        resolved = resolve_vault_selector("myproject", is_loopback=True, state=state)
        assert resolved.path.resolve(strict=False) == target.resolve(strict=False)
        # Came from the pool.
        assert target.resolve(strict=False) in state.vault_pool.paths()
    finally:
        state.close()


def test_registered_name_resolves_from_secondary_configured_root(
    registry_home: Path,
    tmp_path: Path,
) -> None:
    secondary_root = tmp_path / "secondary-vaults"
    target = secondary_root / "elsewhere"
    Vault.init(target, packs=["core"]).close()
    _configure_roots(registry_home, registry_home, secondary_root)
    state = _state(None, None)
    try:
        resolved = resolve_vault_selector("elsewhere", is_loopback=True, state=state)
        assert resolved.path.resolve(strict=False) == target.resolve(strict=False)
    finally:
        state.close()


def test_duplicate_registered_name_across_roots_is_ambiguous(
    registry_home: Path,
    tmp_path: Path,
) -> None:
    secondary_root = tmp_path / "secondary-vaults"
    targets = [registry_home / "duplicate", secondary_root / "duplicate"]
    for target in targets:
        Vault.init(target, packs=["core"]).close()
    _configure_roots(registry_home, registry_home, secondary_root)
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_vault_selector("duplicate", is_loopback=True, state=state)
        assert excinfo.value.code == "ambiguous_vault"
        assert "absolute path" in str(excinfo.value)
        assert state.vault_pool.paths() == []
    finally:
        state.close()


def test_absolute_path_loopback_opens(tmp_path: Path) -> None:
    target = tmp_path / "byabs"
    Vault.init(target, packs=["core"]).close()
    state = _state(None, None)
    try:
        resolved = resolve_vault_selector(str(target), is_loopback=True, state=state)
        assert resolved.path.resolve(strict=False) == target.resolve(strict=False)
    finally:
        state.close()


def test_absolute_path_non_loopback_forbidden(tmp_path: Path) -> None:
    target = tmp_path / "byabs"
    Vault.init(target, packs=["core"]).close()
    state = _state(None, None)
    with pytest.raises(VaultResolutionError) as excinfo:
        resolve_vault_selector(str(target), is_loopback=False, state=state)
    assert excinfo.value.code == "forbidden_path_selector"


def test_missing_named_vault_hints_init(registry_home: Path) -> None:
    state = _state(None, None)
    with pytest.raises(VaultResolutionError) as excinfo:
        resolve_vault_selector("ghost", is_loopback=True, state=state)
    assert excinfo.value.code == "unknown_vault"
    assert "init_vault" in str(excinfo.value)


def test_missing_absolute_path_unknown(tmp_path: Path) -> None:
    state = _state(None, None)
    with pytest.raises(VaultResolutionError) as excinfo:
        resolve_vault_selector(str(tmp_path / "nope"), is_loopback=True, state=state)
    assert excinfo.value.code == "unknown_vault"


def test_selector_equal_to_active_path_returns_active_identity(tmp_path: Path) -> None:
    active = Vault.init(tmp_path / "active", packs=["core"])
    state = _state(active, tmp_path / "active")
    state.vault_pool.adopt(active, state.vault_path)
    try:
        resolved = resolve_vault_selector(
            str((tmp_path / "active").resolve(strict=False)),
            is_loopback=True,
            state=state,
        )
        assert resolved is active
    finally:
        state.close()


# --------------------------------------------------------------------------
# Per-call ``vault=`` override precedence: the connection's ?vault= selector
# always wins, because it is a deliberate hand edit of the client config and
# must never be silently overridden by an agent-supplied tool argument.
# --------------------------------------------------------------------------


def test_connection_selector_wins_over_per_call_override(registry_home: Path) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    beta = registry_home / "beta"
    Vault.init(alpha, packs=["core"]).close()
    Vault.init(beta, packs=["core"]).close()
    state = _state(None, None)
    try:
        runtime = resolve_runtime_selector("alpha", is_loopback=True, state=state, vault="beta")
        assert runtime.vault_path.resolve(strict=False) == alpha.resolve(strict=False)
    finally:
        state.close()


def test_per_call_override_applies_when_connection_has_no_selector(
    registry_home: Path,
) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    beta = registry_home / "beta"
    Vault.init(alpha, packs=["core"]).close()
    Vault.init(beta, packs=["core"]).close()
    state = _state(None, None)
    try:
        runtime = resolve_runtime_selector(None, is_loopback=True, state=state, vault="beta")
        assert runtime.vault_path.resolve(strict=False) == beta.resolve(strict=False)
    finally:
        state.close()


def test_per_call_override_rejects_absolute_path_even_on_loopback(
    registry_home: Path,
    tmp_path: Path,
) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    outside = tmp_path / "outside"
    Vault.init(outside, packs=["core"]).close()
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(None, is_loopback=True, state=state, vault=str(outside))
        assert excinfo.value.code == "forbidden_path_selector"
    finally:
        state.close()


# --------------------------------------------------------------------------
# A per-call ``vault=`` is ALWAYS validated, even when the connection selector
# wins and the value is discarded. Proven necessary by a negative control on a
# running server: with a connection pinned via ``?vault=personal-assistant``,
# ``ask(vault="nao-existe-xyz")`` and ``ask(vault="../../../etc")`` both returned
# a normal answer — the guard was never reached.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../../../etc", "~/", "/Users/someone", "C:\\Windows", "personal-assistant/../outro"],
)
def test_path_shaped_override_rejected_even_when_connection_selector_wins(
    registry_home: Path, bad: str
) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("alpha", is_loopback=True, state=state, vault=bad)
        assert excinfo.value.code == "forbidden_path_selector"
    finally:
        state.close()


def test_unknown_override_name_rejected_even_when_connection_selector_wins(
    registry_home: Path,
) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(
                "alpha", is_loopback=True, state=state, vault="nao-existe-xyz"
            )
        assert excinfo.value.code == "unknown_vault"
    finally:
        state.close()


def test_ignored_override_is_reported_and_routing_is_unchanged(registry_home: Path) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    beta = registry_home / "beta"
    Vault.init(alpha, packs=["core"]).close()
    Vault.init(beta, packs=["core"]).close()
    state = _state(None, None)
    ignored: list[str] = []
    try:
        runtime = resolve_runtime_selector(
            "alpha", is_loopback=True, state=state, vault="beta", override_ignored=ignored
        )
        # Precedence is UNCHANGED: the connection selector still routes.
        assert runtime.vault_path.resolve(strict=False) == alpha.resolve(strict=False)
        assert ignored == ["beta"]
    finally:
        state.close()


def test_invalid_override_errors_rather_than_being_echoed_as_ignored(
    registry_home: Path,
) -> None:
    """Order matters: validate FIRST, echo second."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    state = _state(None, None)
    ignored: list[str] = []
    try:
        with pytest.raises(VaultResolutionError):
            resolve_runtime_selector(
                "alpha", is_loopback=True, state=state, vault="/etc", override_ignored=ignored
            )
        assert ignored == []
    finally:
        state.close()


def test_whitespace_only_override_is_treated_as_no_override(registry_home: Path) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    state = _state(None, None)
    ignored: list[str] = []
    try:
        runtime = resolve_runtime_selector(
            "alpha", is_loopback=True, state=state, vault="   ", override_ignored=ignored
        )
        assert runtime.vault_path.resolve(strict=False) == alpha.resolve(strict=False)
        assert ignored == []
    finally:
        state.close()


# --------------------------------------------------------------------------
# Information disclosure on the EXCEPTION path (reproduced live, 2026-09-17).
# ``explore(vault="a"*300)`` on a pinned connection returned
# ``[Errno 63] File name too long: '/Users/<user>/.marginalia/vaults/aaa…'`` —
# the home directory and the internal vault layout, on a surface with no
# loopback gate. Two independent fixes: a length rule that fires before any
# filesystem call, and a generic envelope for ANY OSError on this seam.
# --------------------------------------------------------------------------

_LEAK_MARKERS = ("/Users", "/home/", ".marginalia", "vaults", "\\Users", str(Path.home()))


def _assert_no_path_leak(message: str) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in message, f"client message leaked {marker!r}: {message}"
    assert "Errno" not in message


def test_overlong_vault_name_is_rejected_without_leaking_a_path(registry_home: Path) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(
                "alpha", is_loopback=True, state=state, vault="a" * 300
            )
        assert excinfo.value.code == "bad_vault_name"
        _assert_no_path_leak(str(excinfo.value))
    finally:
        state.close()


def test_overlong_connection_selector_is_rejected_without_leaking_a_path(
    registry_home: Path,
) -> None:
    """The same trigger arrives via ``?vault=`` on the connection, not only per call."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("a" * 300, is_loopback=True, state=state)
        assert excinfo.value.code == "bad_vault_name"
        _assert_no_path_leak(str(excinfo.value))
    finally:
        state.close()


def test_length_check_runs_before_any_filesystem_access(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is a VALIDATION rule: nothing may stat the candidate path first."""
    from okto_neuron import vault_registry
    from okto_neuron.server.runtime import resolve_runtime_selector

    def _explode(_path):  # pragma: no cover - must never run
        raise AssertionError("filesystem was touched before the length check")

    monkeypatch.setattr(vault_registry, "_is_vault", _explode)
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("a" * 300, is_loopback=True, state=state)
        assert excinfo.value.code == "bad_vault_name"
    finally:
        state.close()


def test_name_just_under_the_limit_is_still_accepted(registry_home: Path) -> None:
    """255 is NAME_MAX, not a stylistic preference — a legitimate long name works."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    name = "a" * 255
    Vault.init(registry_home / name, packs=["core"]).close()
    state = _state(None, None)
    try:
        runtime = resolve_runtime_selector(name, is_loopback=True, state=state)
        assert runtime.vault_path.name == name
    finally:
        state.close()


def test_validator_rejects_nul_byte_and_non_ascii_on_the_charset_rule() -> None:
    """Direct validator test: a NUL byte cannot be sent through an MCP client.

    The non-ASCII case is the evidence for measuring LENGTH IN CHARACTERS: the
    charset allowlist is ASCII-only, so every name that reaches the length rule
    satisfies ``len(name) == len(name.encode("utf-8"))``.
    """
    from okto_neuron.vault_registry import _validate_name

    for bad in ("a\x00b", "\x00", "ä" * 300, "ä"):
        with pytest.raises(ValueError) as excinfo:
            _validate_name(bad)
        assert "must start with a letter or number" in str(excinfo.value)
        _assert_no_path_leak(str(excinfo.value))


def test_oserror_on_the_resolution_seam_is_sanitised_but_logged(
    registry_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Length is one trigger; ANY OSError here must lose its path for the client."""
    import logging

    from okto_neuron import vault_registry
    from okto_neuron.server.runtime import resolve_runtime_selector

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    secret = registry_home / "alpha"

    def _boom(_path):
        raise OSError(13, "Permission denied", str(secret))

    monkeypatch.setattr(vault_registry, "_is_vault", _boom)
    state = _state(None, None)
    try:
        with caplog.at_level(logging.ERROR, logger="okto_neuron.server.runtime"):
            with pytest.raises(VaultResolutionError) as excinfo:
                resolve_runtime_selector("alpha", is_loopback=True, state=state)
        assert excinfo.value.code == "vault_unavailable"
        _assert_no_path_leak(str(excinfo.value))
        # The NAME is the caller's own input and stays.
        assert "alpha" in str(excinfo.value)
        # The operator still gets the full detail.
        assert any(str(secret) in record.getMessage() for record in caplog.records)
    finally:
        state.close()


def test_oserror_from_a_path_selector_does_not_echo_the_path(
    registry_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-shaped selector must not be echoed back: ``resolve`` expands symlinks."""
    from okto_neuron import vault_registry
    from okto_neuron.server.runtime import resolve_runtime_selector

    outside = tmp_path / "outside"
    Vault.init(outside, packs=["core"]).close()

    def _boom(_path):
        raise OSError(5, "Input/output error", str(outside))

    monkeypatch.setattr(vault_registry, "_is_vault", _boom)
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(str(outside), is_loopback=True, state=state)
        assert excinfo.value.code == "vault_unavailable"
        assert str(outside) not in str(excinfo.value)
        assert str(tmp_path) not in str(excinfo.value)
    finally:
        state.close()


# --------------------------------------------------------------------------
# ``vault_override_ignored`` echoes ONE normalisation: the canonical registered
# name. Previously whitespace was stripped but casing was not, so the echo
# normalised inconsistently.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("supplied", ["beta", "BETA", "  beta  ", "  BeTa  "])
def test_ignored_override_echo_is_canonical_for_case_and_whitespace(
    registry_home: Path, supplied: str
) -> None:
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    Vault.init(registry_home / "beta", packs=["core"]).close()
    state = _state(None, None)
    ignored: list[str] = []
    try:
        runtime = resolve_runtime_selector(
            "alpha", is_loopback=True, state=state, vault=supplied, override_ignored=ignored
        )
        assert runtime.vault_path.resolve(strict=False) == alpha.resolve(strict=False)
        assert ignored == ["beta"]
    finally:
        state.close()


def test_empty_string_override_is_a_no_op(registry_home: Path) -> None:
    """``vault=""`` is NO override (deliberate), not a name to resolve."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    alpha = registry_home / "alpha"
    Vault.init(alpha, packs=["core"]).close()
    state = _state(None, None)
    ignored: list[str] = []
    try:
        runtime = resolve_runtime_selector(
            "alpha", is_loopback=True, state=state, vault="", override_ignored=ignored
        )
        assert runtime.vault_path.resolve(strict=False) == alpha.resolve(strict=False)
        assert ignored == []
    finally:
        state.close()


# --------------------------------------------------------------------------
# REAL-FILESYSTEM path-disclosure tests (complementary to the monkeypatched
# ones above). Those prove the envelope's LOGIC; these prove the envelope
# actually sits on the code path a genuine ``OSError`` travels. Each test owns
# its own ``registry_home`` tmp dir, which matters: ``list_vaults`` scans every
# child of every root, so one unreadable vault directory makes EVERY name
# resolution fail. Nothing is monkeypatched here — the kernel raises.
# --------------------------------------------------------------------------

_FS_LEAK_MARKERS = ("Permission denied", "Not a directory", "Too many levels")


def _assert_no_fs_leak(message: str, *, tmp_path: Path) -> None:
    """Stricter than ``_assert_no_path_leak``: also bans the tmp root itself.

    On macOS ``tmp_path`` lives under ``/private/var/folders/...``, which matches
    none of ``_LEAK_MARKERS`` — so a real-FS test that only reused that helper
    could pass while leaking the whole absolute path.
    """
    _assert_no_path_leak(message)
    assert str(tmp_path) not in message, f"client message leaked tmp_path: {message}"
    for marker in _FS_LEAK_MARKERS:
        assert marker not in message, f"client message leaked {marker!r}: {message}"


def test_real_enotdir_a_file_where_a_vault_name_resolves(
    registry_home: Path, tmp_path: Path
) -> None:
    """A regular FILE under the vault name: ``is_dir()`` is False, so this lands in
    the ``unknown_vault`` branch rather than the OSError envelope. It pins the
    absence of a leak on that branch; it is NOT evidence of envelope coverage.
    """
    from okto_neuron.server.runtime import resolve_runtime_selector

    registry_home.mkdir(parents=True, exist_ok=True)
    (registry_home / "target").write_text("not a directory", encoding="utf-8")
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("target", is_loopback=True, state=state)
        assert excinfo.value.code == "unknown_vault"
        _assert_no_fs_leak(str(excinfo.value), tmp_path=tmp_path)
        assert "target" in str(excinfo.value)
    finally:
        state.close()


@pytest.fixture
def unreadable_vault(registry_home: Path):
    """A real chmod-000 vault directory, restored unconditionally on teardown.

    Teardown must not be a ``try/finally`` in the test body: a failing assert
    would leave a ``0o000`` directory behind, and pytest keeps the last few
    ``tmp_path`` bases, so it would break a LATER run's cleanup, not this one.
    """
    registry_home.mkdir(parents=True, exist_ok=True)
    target = registry_home / "target"
    target.mkdir()
    (target / "okto-neuron.yaml").write_text("marginalia_version: 1\n", encoding="utf-8")
    import os

    os.chmod(target, 0o000)
    try:
        yield target
    finally:
        try:
            os.chmod(target, 0o700)
        except OSError:  # pragma: no cover - best-effort teardown
            pass


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits and a non-root euid (root bypasses them)",
)
def test_real_eacces_unreadable_vault_directory_is_enveloped(
    unreadable_vault: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only test here that genuinely exercises the OSError envelope.

    ``is_dir()`` on the mode-000 directory succeeds (its PARENT is readable), so
    resolution proceeds to ``(path / "okto-neuron.yaml").is_file()``, which the
    kernel answers with EACCES. No monkeypatching: this is a real ``OSError``
    travelling the real code path.
    """
    import logging

    from okto_neuron.server.runtime import resolve_runtime_selector

    state = _state(None, None)
    try:
        with caplog.at_level(logging.ERROR, logger="okto_neuron.server.runtime"):
            with pytest.raises(VaultResolutionError) as excinfo:
                resolve_runtime_selector("target", is_loopback=True, state=state)
        assert excinfo.value.code == "vault_unavailable"
        _assert_no_fs_leak(str(excinfo.value), tmp_path=tmp_path)
        # The NAME is the caller's own input and stays.
        assert "target" in str(excinfo.value)
        # The half that must KEEP working: the operator log has everything.
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert str(unreadable_vault) in logged
        assert "Permission denied" in logged
    finally:
        state.close()


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
def test_real_eloop_symlink_loop_under_a_vault_name(
    registry_home: Path, tmp_path: Path
) -> None:
    """A self-referencing symlink is a deterministic ELOOP on macOS and Linux.

    ``pathlib.Path.resolve()`` converts the kernel's ELOOP ``OSError`` into a
    ``RuntimeError("Symlink loop from '<abs path>'")``, which used to escape the
    OSError-only guard seam raw. The seam now unwraps that RuntimeError's
    ``__context__`` and routes it through the same sanitiser.
    """
    from okto_neuron.server.runtime import resolve_runtime_selector

    registry_home.mkdir(parents=True, exist_ok=True)
    loop = registry_home / "target"
    os.symlink(str(loop), str(loop))
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("target", is_loopback=True, state=state)
        # Same code on 3.12 (resolve() raises) and 3.13 (resolve() no longer
        # raises on a loop; the stat() behind the failed is_vault does).
        assert excinfo.value.code == "vault_unavailable"
        _assert_no_fs_leak(str(excinfo.value), tmp_path=tmp_path)
    finally:
        state.close()


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
def test_real_eloop_absolute_path_selector_is_vault_unavailable(
    tmp_path: Path,
) -> None:
    """The absolute-path branch gets the same treatment as the name branch."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    loop = tmp_path / "loop"
    os.symlink(str(loop), str(loop))
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(str(loop), is_loopback=True, state=state)
        assert excinfo.value.code == "vault_unavailable"
    finally:
        state.close()


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
def test_real_eloop_operator_log_keeps_the_full_detail(
    registry_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the split: the client gets nothing, the log gets it all."""
    import logging

    from okto_neuron.server.runtime import resolve_runtime_selector

    registry_home.mkdir(parents=True, exist_ok=True)
    loop = registry_home / "target"
    os.symlink(str(loop), str(loop))
    state = _state(None, None)
    try:
        with caplog.at_level(logging.ERROR, logger="okto_neuron.server.runtime"):
            with pytest.raises(VaultResolutionError) as excinfo:
                resolve_runtime_selector("target", is_loopback=True, state=state)
        assert excinfo.value.code == "vault_unavailable"
        _assert_no_fs_leak(str(excinfo.value), tmp_path=tmp_path)
        logged = "\n".join(
            record.getMessage() + (record.exc_text or "") for record in caplog.records
        )
        assert str(loop) in logged
    finally:
        state.close()


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
def test_real_absolute_symlink_selector_does_not_echo_the_resolved_target(
    registry_home: Path, tmp_path: Path
) -> None:
    """LEAK 2: the absolute-path branch resolved the selector and echoed it back.

    ``resolve()`` expands symlinks, so ``no vault at {target}`` disclosed the
    symlink's TARGET - a path the caller never supplied. The branch now names no
    path at all; the caller supplied one and already knows which.
    """
    from okto_neuron.server.runtime import resolve_runtime_selector

    hidden = tmp_path / "hidden_real_dir"
    hidden.mkdir()
    link = tmp_path / "link"
    os.symlink(str(hidden), str(link))
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(str(link), is_loopback=True, state=state)
        message = str(excinfo.value)
        assert excinfo.value.code == "unknown_vault"
        assert "hidden_real_dir" not in message
        assert "init_vault" in message
        _assert_no_fs_leak(message, tmp_path=tmp_path)
    finally:
        state.close()


@pytest.mark.parametrize("length", [254, 255])
def test_real_fs_name_length_at_and_below_the_cap_resolves_cleanly(
    registry_home: Path, tmp_path: Path, length: int
) -> None:
    """254/255 pass validation and genuinely reach the filesystem (NAME_MAX is 255)."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    registry_home.mkdir(parents=True, exist_ok=True)
    name = "a" * length
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector(name, is_loopback=True, state=state)
        assert excinfo.value.code == "unknown_vault"
        _assert_no_fs_leak(str(excinfo.value), tmp_path=tmp_path)
    finally:
        state.close()


def test_real_fs_name_length_above_the_cap_never_reaches_the_filesystem(
    registry_home: Path, tmp_path: Path
) -> None:
    """256 is stopped by the validation cap, before any ``os.stat``."""
    from okto_neuron.server.runtime import resolve_runtime_selector

    registry_home.mkdir(parents=True, exist_ok=True)
    state = _state(None, None)
    try:
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_runtime_selector("a" * 256, is_loopback=True, state=state)
        assert excinfo.value.code == "bad_vault_name"
        message = str(excinfo.value)
        _assert_no_fs_leak(message, tmp_path=tmp_path)
        assert "at most 255 characters" in message
        assert "got 256" in message
    finally:
        state.close()
