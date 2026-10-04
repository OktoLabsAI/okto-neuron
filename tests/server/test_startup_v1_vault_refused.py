"""`serve --vault <v1 vault>` refuses that vault untouched and still serves (#14)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from okto_neuron.server import runtime
from okto_neuron.vault import Vault

REAL_QUEUE_GATE = True


def _listing(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_v1_vault(path: Path) -> Path:
    vault = Vault.init(path, packs=["core"])
    root = Path(vault.path)
    vault.close()
    config = root / "okto-neuron.yaml"
    config.write_text(
        config.read_text().replace("marginalia_yaml_version: 2", "marginalia_yaml_version: 1"),
        encoding="utf-8",
    )
    (root / ".marginalia").mkdir(exist_ok=True)
    (root / ".marginalia" / "review_queue.json").write_text('{"entries": []}\n', encoding="utf-8")
    return root.resolve(strict=False)


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_open_startup_vault_refuses_v1_before_lease_or_graph(tmp_path: Path) -> None:
    old = _make_v1_vault(tmp_path / "old")
    before = _listing(old)

    vault, active, warning = runtime._open_startup_vault(old)

    assert (vault, active) == (None, None)
    assert warning is not None and warning["code"] == "review_queue_migration_required"
    assert warning["remedy"] == "okto-neuron kg review-queue migrate --vault old"
    assert not (old / ".okto-neuron-writer.lock").exists()
    assert _listing(old) == before


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof is needed to prove no graph handle")
def test_serve_with_a_v1_fallback_vault_stays_up_and_leaves_it_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    roots = home / "roots"
    old = _make_v1_vault(roots / "old")
    Vault.init(roots / "served", packs=["core"]).close()
    config_path = home / ".okto-neuron" / "okto-neuron.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f"marginalia_toml_version = 1\nvault_roots = [{json.dumps(str(roots))}]\n",
        encoding="utf-8",
    )
    before = _listing(old)
    rest_port, mcp_port = _free_port(), _free_port()
    log_path = tmp_path / "serve.log"
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                str(Path(sys.executable).parent / "okto-neuron"), "serve", "--vault", str(old),
                "--port", str(rest_port), "--mcp-port", str(mcp_port), "--no-open",
            ],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                health_code, _ = _get(rest_port, "/health")
                break
            except (urllib.error.URLError, ConnectionError):
                assert proc.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.5)
        assert health_code == 200

        _, status = _get(rest_port, "/api/v1/status")
        by_name = {Path(v["path"]).name: v for v in status["vaults"]}
        assert by_name["old"]["review_queue"]["state"] == "migration_required"
        assert by_name["old"]["review_queue"]["code"] == "review_queue_migration_required"
        assert by_name["served"]["review_queue"]["state"] == "ok"
        assert status["vault_warning"]["code"] == "review_queue_migration_required"
        assert status["status"] == "degraded"
        assert any("review_queue_migration_required: old" in r for r in status["degraded_reasons"])

        code, body = _get(rest_port, "/api/v1/review-queue", {"X-Okto-Neuron-Vault": "old"})
        assert code == 409 and body["error"] == "review_queue_migration_required"
        code, _ = _get(rest_port, "/api/v1/review-queue", {"X-Okto-Neuron-Vault": "served"})
        assert code == 200

        # No graph handle and no lease on the refused vault, proven from the process.
        held = subprocess.run(
            ["lsof", "-p", str(proc.pid), "-Fn"], capture_output=True, text=True
        ).stdout
        assert str(old) not in held, held
        assert not (old / ".okto-neuron-writer.lock").exists()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    assert _listing(old) == before
    assert log_path.read_text().count("refusing vault old") == 1, log_path.read_text()
