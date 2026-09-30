"""``okto-neuron kg review-queue migrate`` CLI: dry-run, migrate, rollback, restore-backup (#14)."""

from __future__ import annotations

REAL_QUEUE_GATE = True  # these tests exercise the version-1 layout gate itself

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
from okto_neuron.config._vault import vault_yaml_version
from okto_neuron.consolidate.review_queue import (
    ReviewQueue,
    entry_digest,
    load_legacy_entries,
)
from okto_neuron.consolidate.review_queue_migration import rollback
from okto_neuron.consolidate.review_queue_sqlite import ReviewQueueMigrationRequired
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.writer_lease import release_all
from tests.perf._synthetic_vault import build_synthetic_vault, scaled

_REPO = Path(__file__).resolve().parents[2]

_HOLDER = textwrap.dedent(
    """
    import sys
    from okto_neuron.store.writer_lease import acquire_writer_lease
    acquire_writer_lease(sys.argv[1], role="daemon", operation="serve")
    print("ready", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture(autouse=True)
def _clean():
    release_all()
    yield
    release_all()
    VaultConnection.close_all()


def _tree(root: Path) -> dict[str, str]:
    """Every entry under ``root`` (dirs included) -> sha256 of its bytes."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if key == ".okto-neuron-writer.lock":
            continue  # lease metadata (pid, timestamp) is rewritten by any lease holder
        out[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return out


def _digests(queue_json: Path) -> dict[str, str]:
    return {cid: entry_digest(e.to_json()) for cid, e in load_legacy_entries(queue_json).items()}


@pytest.fixture
def v1_vault(tmp_path: Path) -> Path:
    """A synthetic vault (tests/perf helper) put back into the version-1 JSON layout."""
    root = tmp_path / "vault"
    build_synthetic_vault(root, shape=scaled(0.02))
    VaultConnection.close_all()
    rollback(root)
    for aside in (root / ".marginalia").glob("review_queue.sqlite*"):
        aside.unlink()
    assert vault_yaml_version(root) == 1
    assert (root / ".marginalia" / "review_queue.json").exists()
    return root


def _invoke(*args: str):
    return CliRunner().invoke(app, ["kg", "review-queue", "migrate", *args])


def _json(result) -> dict:
    # stdout only: anything the command printed to stderr must not break the parse
    return json.loads(result.stdout)


def test_dry_run_changes_nothing_and_reports(v1_vault: Path) -> None:
    before = _tree(v1_vault)
    result = _invoke("--vault", str(v1_vault), "--dry-run", "--json")
    assert result.exit_code == 0, (result.stdout, result.stderr)
    report = _json(result)
    assert report["outcome"] == "dry-run-ok"
    assert report["dry_run"] is True
    assert report["source_count"] == report["migrated_count"] == report["hash_equal"] > 0
    assert _tree(v1_vault) == before


def test_dry_run_text_output_says_nothing_was_written(v1_vault: Path) -> None:
    result = _invoke("--vault", str(v1_vault), "--dry-run")
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "dry-run-ok" in result.stdout and "nothing was written" in result.stdout


def test_migrate_is_idempotent(v1_vault: Path) -> None:
    marginalia = v1_vault / ".marginalia"
    original = (marginalia / "review_queue.json").read_bytes()
    first = _invoke("--vault", str(v1_vault), "--json")
    assert first.exit_code == 0, (first.stdout, first.stderr)
    assert _json(first)["outcome"] == "migrated"
    assert vault_yaml_version(v1_vault) == 2
    assert (marginalia / "review_queue.json.bak-v1").read_bytes() == original
    assert not (marginalia / "review_queue.json").exists()
    assert (marginalia / "review_queue.sqlite").exists()

    settled = _tree(v1_vault)
    second = _invoke("--vault", str(v1_vault), "--json")
    assert second.exit_code == 0, (second.stdout, second.stderr)
    assert _json(second)["outcome"] == "already-migrated"
    assert _tree(v1_vault) == settled


def test_rollback_roundtrip_keeps_every_entry_hash(v1_vault: Path) -> None:
    marginalia = v1_vault / ".marginalia"
    before = _digests(marginalia / "review_queue.json")
    original = (marginalia / "review_queue.json").read_bytes()
    assert _invoke("--vault", str(v1_vault)).exit_code == 0

    result = _invoke("--vault", str(v1_vault), "--rollback", "--json")
    assert result.exit_code == 0, (result.stdout, result.stderr)
    report = _json(result)
    assert report["outcome"] == "rolled-back"
    assert report["hash_equal"] == len(before)
    assert vault_yaml_version(v1_vault) == 1
    assert _digests(marginalia / "review_queue.json") == before
    assert (marginalia / "review_queue.json").read_bytes() == original
    asides = list(marginalia.glob("review_queue.sqlite.rolled-back-*"))
    assert len(asides) == 1 and asides[0].stat().st_size > 0
    assert (marginalia / "review_queue.json.bak-v1").exists()  # never deleted
    assert not (marginalia / "review_queue.sqlite").exists()

    # and forward again: the moved-aside file does not block a new migration
    again = _invoke("--vault", str(v1_vault), "--json")
    assert again.exit_code == 0, (again.stdout, again.stderr)
    assert _json(again)["outcome"] == "migrated"
    assert _json(again)["hash_equal"] == len(before)


def test_restore_backup_restores_the_literal_bak(v1_vault: Path) -> None:
    marginalia = v1_vault / ".marginalia"
    original = (marginalia / "review_queue.json").read_bytes()
    assert _invoke("--vault", str(v1_vault)).exit_code == 0
    result = _invoke("--vault", str(v1_vault), "--restore-backup", "--json")
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert _json(result)["action"] == "rollback-restore-backup"
    assert (marginalia / "review_queue.json").read_bytes() == original
    assert vault_yaml_version(v1_vault) == 1
    assert list(marginalia.glob("review_queue.sqlite.rolled-back-*"))


@pytest.mark.parametrize("flags", [["--dry-run", "--rollback"], ["--rollback", "--restore-backup"]])
def test_flags_are_mutually_exclusive(v1_vault: Path, flags: list[str]) -> None:
    before = _tree(v1_vault)
    result = _invoke("--vault", str(v1_vault), *flags)
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
    assert _tree(v1_vault) == before


def test_unknown_vault_fails_cleanly(tmp_path: Path) -> None:
    result = _invoke("--vault", str(tmp_path / "nope"))
    assert result.exit_code != 0
    assert "vault not found" in result.stderr
    assert not (tmp_path / "nope").exists()


def _spawn_holder(vault: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(vault)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")
@pytest.mark.parametrize("flags", [[], ["--rollback"], ["--restore-backup"]])
def test_a_daemon_holding_the_lease_refuses_writes_with_exit_5(v1_vault: Path, flags: list[str]) -> None:
    holder = _spawn_holder(v1_vault)
    try:
        before = _tree(v1_vault)
        result = _invoke("--vault", str(v1_vault), *flags)
        assert result.exit_code == 5, (result.stdout, result.stderr)
        assert f"daemon pid {holder.pid}" in result.stderr
        assert _tree(v1_vault) == before
        # a dry run writes nothing, so it does not need the lease
        assert _invoke("--vault", str(v1_vault), "--dry-run").exit_code == 0
        assert _tree(v1_vault) == before
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)
        holder.stdout.close()  # type: ignore[union-attr]


def test_a_read_only_opener_never_migrates(v1_vault: Path) -> None:
    marginalia = v1_vault / ".marginalia"
    before = _tree(v1_vault)
    # version 1: the queue refuses to open, and refusing writes nothing
    with pytest.raises(ReviewQueueMigrationRequired):
        len(ReviewQueue(marginalia, store=None))  # type: ignore[arg-type]
    assert _tree(v1_vault) == before

    assert _invoke("--vault", str(v1_vault)).exit_code == 0
    settled = _tree(v1_vault)
    # version 2: opening and reading the queue leaves the vault byte-identical
    queue = ReviewQueue(marginalia, store=None)  # type: ignore[arg-type]
    assert len(queue) > 0
    assert queue.list()
    assert _tree(v1_vault) == settled
    assert vault_yaml_version(v1_vault) == 2


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    drop = {
        "OKTO_NEURON_MLFLOW_TRACKING_URI",
        "OKTO_NEURON_MLFLOW_EXPERIMENT",
        "MARGINALIA_MLFLOW_TRACKING_URI",
        "MARGINALIA_GLM_API_KEY",
        "MARGINALIA_TOKEN",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["HOME"] = str(home)
    return subprocess.run(
        [str(_REPO / ".venv" / "bin" / "okto-neuron"), "kg", "review-queue", "migrate", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_subprocess_dry_run_is_byte_identical(v1_vault: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    before = _tree(v1_vault)
    result = _cli(home, "--vault", str(v1_vault), "--dry-run", "--json")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["outcome"] == "dry-run-ok"
    assert _tree(v1_vault) == before


def test_subprocess_migrate_and_rollback_roundtrip(v1_vault: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marginalia = v1_vault / ".marginalia"
    before = _digests(marginalia / "review_queue.json")
    migrated = _cli(home, "--vault", str(v1_vault), "--json")
    assert migrated.returncode == 0, (migrated.stdout, migrated.stderr)
    assert json.loads(migrated.stdout)["hash_equal"] == len(before)
    assert vault_yaml_version(v1_vault) == 2
    rolled = _cli(home, "--vault", str(v1_vault), "--rollback", "--json")
    assert rolled.returncode == 0, (rolled.stdout, rolled.stderr)
    assert json.loads(rolled.stdout)["outcome"] == "rolled-back"
    assert vault_yaml_version(v1_vault) == 1
    assert _digests(marginalia / "review_queue.json") == before
