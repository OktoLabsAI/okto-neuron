"""CLI side of the per-vault writer lease (#21).

A bare interpreter stands in for the daemon (it holds the lease with
``role="daemon"``); every guarded command is driven through its real click entry
point. A refused command must exit 5, name the holder's pid and the remedy, and
leave every file in the vault untouched. Readers must keep working, and commands
that create a new vault path take the lease instead of refusing.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.core.schema import Node
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _open_vault
from okto_neuron.store.writer_lease import (
    LEASE_FILENAME,
    WriterLeaseHeld,
    acquire_writer_lease,
    held_writer_lease,
    release_all,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")

_HOLDER = textwrap.dedent(
    """
    import sys
    from okto_neuron.store.writer_lease import acquire_writer_lease
    acquire_writer_lease(sys.argv[1], role=sys.argv[2], operation="serve")
    print("ready", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OKTO_NEURON_LAPTOP_GATE", raising=False)
    release_all()
    yield
    release_all()
    VaultConnection.close_all()


def spawn_holder(vault: Path, role: str = "daemon") -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(vault), role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def stop_holder(proc: subprocess.Popen) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="n1", type="Concept", title="n1", content="fixture"))
    store.checkpoint()
    store.close()
    VaultConnection.close_all()
    return vault


def invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def guarded_cases(vault: Path, tmp_path: Path) -> list[tuple[str, list[str], str]]:
    """(id, argv, remedy fragment) for every command the daemon's lease refuses."""
    v = str(vault)
    stop = "okto-neuron stop"
    return [
        ("watch", ["watch", v, "--once"], stop),
        ("pilot", ["pilot", v, "--report-dir", str(tmp_path / "pilot")], stop),
        ("init-wipe", ["init", v, "--wipe"], "POST /api/v1/reset"),
        ("kg-init", ["kg", "init", v], stop),
        ("kg-rebuild", ["kg", "rebuild", v], "POST /api/v1/curation/rebuild"),
        ("kg-reembed", ["kg", "reembed", v], "POST /api/v1/curation/reembed"),
        ("kg-reindex", ["kg", "reindex", v], stop),
        ("reconcile-propose", ["kg", "reconcile", "propose", v], "/api/v1/reconcile/propose"),
        ("reconcile-apply", ["kg", "reconcile", "apply", v], "/api/v1/reconcile/apply"),
        (
            "review-confirm",
            ["kg", "reconcile", "review", "confirm", "cid", v],
            "/api/v1/reconcile/review/confirm",
        ),
        (
            "review-reject",
            ["kg", "reconcile", "review", "reject", "cid", v],
            "/api/v1/reconcile/review/reject",
        ),
        ("reconcile-heal", ["kg", "reconcile", "heal", v], "POST /api/v1/curation/heal"),
        (
            "snapshot-dump",
            ["kg", "snapshot", "dump", v, str(tmp_path / "snap")],
            stop,
        ),
        (
            "onboard",
            ["onboard", "--vault", v, "--backend", "ladybug", "--disable-llm", "--non-interactive"],
            "PATCH /api/v1/config",
        ),
    ]


_IDS = [
    "watch",
    "pilot",
    "init-wipe",
    "kg-init",
    "kg-rebuild",
    "kg-reembed",
    "kg-reindex",
    "reconcile-propose",
    "reconcile-apply",
    "review-confirm",
    "review-reject",
    "reconcile-heal",
    "snapshot-dump",
    "onboard",
]


@pytest.mark.parametrize("case_id", _IDS)
def test_guarded_command_is_refused_while_the_daemon_holds_the_lease(
    case_id: str, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    holder = spawn_holder(vault)
    try:
        before = tree_hashes(vault)
        _, argv, remedy = next(c for c in guarded_cases(vault, tmp_path) if c[0] == case_id)
        result = invoke(argv)

        assert result.exit_code == 5, (result.output, result.stderr)
        assert f"daemon pid {holder.pid}" in result.stderr, result.stderr
        assert remedy in result.stderr, result.stderr
        assert tree_hashes(vault) == before
        assert not (tmp_path / "snap").exists()
        assert not (tmp_path / "pilot").exists()
    finally:
        stop_holder(holder)


def test_refusal_names_another_cli_writer_without_daemon_advice(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    holder = spawn_holder(vault, role="cli")
    try:
        result = invoke(["kg", "reindex", str(vault)])
        assert result.exit_code == 5
        assert f"cli pid {holder.pid}" in result.stderr
        assert "wait for it to finish" in result.stderr
        assert "okto-neuron stop" not in result.stderr
    finally:
        stop_holder(holder)


def test_guarded_command_runs_once_the_holder_is_gone(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    holder = spawn_holder(vault)
    assert invoke(["kg", "reindex", str(vault), "--force"]).exit_code == 5
    holder.kill()
    holder.wait(timeout=10)
    result = invoke(["kg", "reindex", str(vault), "--force"])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert held_writer_lease(vault) is None  # released on exit


def test_readers_succeed_while_the_daemon_holds_the_lease(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    snap = tmp_path / "snap"
    assert invoke(["kg", "snapshot", "dump", str(vault), str(snap)]).exit_code == 0
    holder = spawn_holder(vault)
    try:
        before = tree_hashes(vault)
        listed = invoke(["kg", "reconcile", "review", "list", str(vault)])
        assert listed.exit_code == 0, (listed.output, listed.stderr)
        assert "review queue empty" in listed.output
        as_json = invoke(["kg", "reconcile", "review", "list", str(vault), "--json"])
        assert as_json.exit_code == 0 and json.loads(as_json.output) == []
        verified = invoke(["kg", "snapshot", "verify", str(snap)])
        assert verified.exit_code == 0, (verified.output, verified.stderr)
        assert tree_hashes(vault) == before
    finally:
        stop_holder(holder)


def test_review_list_opens_no_graph_store(tmp_path: Path, monkeypatch) -> None:
    vault = make_vault(tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("review list must not open the vault")

    monkeypatch.setattr("okto_neuron.vault.Vault.open", boom)
    assert invoke(["kg", "reconcile", "review", "list", str(vault)]).exit_code == 0


def test_review_list_on_a_missing_vault_fails_like_the_writable_open(tmp_path: Path) -> None:
    result = invoke(["kg", "reconcile", "review", "list", str(tmp_path / "nope")])
    assert result.exit_code != 0


def test_init_on_a_new_path_takes_the_lease_then_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    real = acquire_writer_lease

    def spy(path, *, role, operation, endpoint=None):
        seen.append((role, operation))
        return real(path, role=role, operation=operation, endpoint=endpoint)

    monkeypatch.setattr("okto_neuron.store.vault_writer.acquire_writer_lease", spy)
    new = tmp_path / "fresh"
    result = invoke(["init", str(new), "--embedder", "stub"])
    assert result.exit_code == 0, (result.output, result.stderr)
    assert ("cli", "init") in seen
    assert (new / LEASE_FILENAME).exists()
    assert held_writer_lease(new) is None
    acquire_writer_lease(new, role="cli", operation="probe").release()


def test_new_path_commands_succeed_and_take_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = acquire_writer_lease

    def spy(path, *, role, operation, endpoint=None):
        seen.append(operation)
        return real(path, role=role, operation=operation, endpoint=endpoint)

    monkeypatch.setattr("okto_neuron.store.vault_writer.acquire_writer_lease", spy)

    created = invoke(["vault", "create", "alpha", "--embedder", "stub", "--no-use"])
    assert created.exit_code == 0, (created.output, created.stderr)
    assert "vault create" in seen

    source = make_vault(tmp_path)
    snap = tmp_path / "snap"
    assert invoke(["kg", "snapshot", "dump", str(source), str(snap)]).exit_code == 0
    loaded = invoke(["kg", "snapshot", "load", str(snap), str(tmp_path / "restored")])
    assert loaded.exit_code == 0, (loaded.output, loaded.stderr)
    assert "snapshot load" in seen

    initd = invoke(["kg", "init", str(tmp_path / "kgnew"), "--backend", "ladybug"])
    assert initd.exit_code == 0, (initd.output, initd.stderr)
    assert "kg init" in seen


def test_refused_onboard_changes_nothing_not_even_the_default_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is onboard's first step: it beats the backend-pin check and set_default_vault.

    The argv deliberately asks for a backend the vault is not pinned to; if the
    pin check ran first this would exit 11 (VaultBackendMismatch), not 5.
    """
    from okto_neuron.vault_registry import set_default_vault

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OKTO_NEURON_CONFIG", "OKTO_NEURON_VAULT", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    vault = make_vault(tmp_path)
    other = tmp_path / "other-default"
    other.mkdir()
    set_default_vault(other)
    config_files = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert any(b"default_vault" in raw for raw in config_files.values())

    holder = spawn_holder(vault)
    try:
        before = tree_hashes(vault)
        result = invoke(
            ["onboard", "--vault", str(vault), "--backend", "grafx", "--disable-llm",
             "--non-interactive"]
        )
        assert result.exit_code == 5, (result.output, result.stderr)
        assert f"daemon pid {holder.pid}" in result.stderr, result.stderr
        assert tree_hashes(vault) == before
        assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == config_files
        assert str(other.resolve()) in next(
            raw for raw in config_files.values() if b"default_vault" in raw
        ).decode()
    finally:
        stop_holder(holder)


def test_onboard_dry_run_takes_no_lease_and_is_not_refused(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    holder = spawn_holder(vault)
    try:
        result = invoke(
            [
                "onboard",
                "--vault",
                str(vault),
                "--backend",
                "ladybug",
                "--disable-llm",
                "--non-interactive",
                "--dry-run",
            ]
        )
        assert result.exit_code == 0, (result.output, result.stderr)
    finally:
        stop_holder(holder)


def test_in_process_holder_passes_through_the_guard(tmp_path: Path) -> None:
    """The daemon's own in-process ``kg_reembed`` must not contend with itself."""
    from okto_neuron.store.vault_writer import vault_writer

    vault = make_vault(tmp_path)
    with acquire_writer_lease(vault, role="daemon", operation="serve") as lease:
        with vault_writer(vault, "reembed") as inner:
            assert inner is lease
        assert lease.held  # the nested guard did not release the daemon's lease


def seed_031_leftovers(vault: Path) -> None:
    """What a failed 0.3.1 ``kg reembed`` leaves behind."""
    (vault / ".graph-handle.lock").write_text("999999\n", encoding="utf-8")
    marginalia = vault / ".marginalia"
    marginalia.mkdir(exist_ok=True)
    (marginalia / ".bootstrap.lock").write_text("", encoding="utf-8")
    (marginalia / "reembed.state.json").write_text(
        json.dumps({"phase": "failed", "started_at": "2026-09-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def test_031_leftovers_do_not_break_the_guard(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    seed_031_leftovers(vault)

    # No holder: a writer command works despite the leftovers ...
    assert invoke(["kg", "reindex", str(vault), "--force"]).exit_code == 0

    # ... and with a holder the refusal is still the lease's, leaving them alone.
    holder = spawn_holder(vault)
    try:
        before = tree_hashes(vault)
        result = invoke(["kg", "reembed", str(vault)])
        assert result.exit_code == 5, (result.output, result.stderr)
        assert f"daemon pid {holder.pid}" in result.stderr
        assert tree_hashes(vault) == before
    finally:
        stop_holder(holder)


def test_refusal_is_a_writer_lease_held_error_with_the_remedy(tmp_path: Path) -> None:
    from okto_neuron.store.vault_writer import vault_writer

    vault = make_vault(tmp_path)
    holder = spawn_holder(vault)
    try:
        with pytest.raises(WriterLeaseHeld) as info:
            with vault_writer(vault, "kg rebuild"):
                pytest.fail("must not enter")
        assert info.value.EXIT_CODE == 5
        assert info.value.holder.pid == holder.pid
        assert "okto-neuron stop" in info.value.user_message()
    finally:
        stop_holder(holder)
