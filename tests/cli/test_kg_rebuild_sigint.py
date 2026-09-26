from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.cli.kg import kg_init, kg_rebuild
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import LadybugStore, VaultConnection


_INTERRUPT_SCRIPT = """
from __future__ import annotations

from pathlib import Path
import sys
import time

from okto_neuron.cli.kg import kg_rebuild
from okto_neuron.errors import RebuildInterrupted

vault_path = Path(sys.argv[1])
marker_path = Path(sys.argv[2])
interrupt_on = sys.argv[3]


def ingest(path, store):
    del store
    relative = path.relative_to(vault_path).as_posix()
    marker_path.write_text(relative, encoding="utf-8")
    if relative == interrupt_on:
        for _ in range(30):
            time.sleep(0.05)


try:
    kg_rebuild(vault_path, ingest=ingest)
except RebuildInterrupted as exc:
    raise SystemExit(exc.EXIT_CODE)
"""


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        handle.close()
    bootstrap_module._bootstrap_cache.clear()


def test_sigint_during_rebuild_keeps_live_graph_and_next_run_starts_from_scratch(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    marker_path = tmp_path / "current-file.txt"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    _write_text(vault_path / "notes" / "b.md", "# B\n")
    graph_path = vault_path / "graph.lbug"
    tmp_graph_path = vault_path / "graph.rebuild.lbug"
    state_path = vault_path / ".marginalia" / "rebuild.state.json"
    original_sha256 = _sha256_file(graph_path)

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _INTERRUPT_SCRIPT,
            str(vault_path),
            str(marker_path),
            "notes/b.md",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=_subprocess_env(),
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(marker_path, "notes/b.md")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == 130, stderr or stdout
    assert _sha256_file(graph_path) == original_sha256
    assert not tmp_graph_path.exists()

    interrupted_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert interrupted_state["phase"] == "interrupted"
    assert interrupted_state["current_file"] == "notes/b.md"
    assert interrupted_state["last_file_done"] == "notes/a.md"
    assert "interrupted_at" in interrupted_state

    tmp_graph_path.write_bytes(b"stale interrupted temp graph")
    observed: list[str] = []

    def ingest(path: Path, store: LadybugStore) -> None:
        assert not store.is_closed
        observed.append(path.relative_to(vault_path).as_posix())

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    assert observed == ["notes/a.md", "notes/b.md"]
    assert not tmp_graph_path.exists()
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["phase"] == "complete"


def _wait_for_marker(marker_path: Path, expected: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker_path.exists() and marker_path.read_text(encoding="utf-8") == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"subprocess did not reach {expected}")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    source_path = str(Path(__file__).resolve().parents[2] / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        env["PYTHONPATH"] = os.pathsep.join([source_path, existing_pythonpath])
    else:
        env["PYTHONPATH"] = source_path
    return env


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
