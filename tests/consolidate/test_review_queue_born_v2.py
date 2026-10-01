"""A vault the product creates is born at yaml version 2, so its own daemon serves it (#14).

The conftest shim turns a hand-written version-1 yaml into a version-2 one for every
other test, which would hide a production scaffold path that still writes version 1
(the daemon would then refuse that vault in real use). These tests opt out of the shim
and check the real gate.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

from okto_neuron.config._vault import vault_yaml_version
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.review_queue import ReviewQueue, layout_refusal
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import ServerState, init_state, reset_state_for_tests
from okto_neuron.vault import Vault

REAL_QUEUE_GATE = True

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "okto_neuron"

# Every place under src/ that may spell a yaml version of 1, by enclosing function.
# Reads and the loader baseline accept 1 (old vaults exist); only the migration
# rollback WRITES it, deliberately (yaml 1 = review_queue.json is the source of truth).
_ALLOWED = {
    ("config/_vault.py", "vault_yaml_version"): 1,  # reader: a missing key is version 1
    ("config/_vault.py", "VaultConfig"): 1,  # field default of the load baseline
    ("config/_vault.py", "default"): 1,  # load() deep-merge baseline, never a scaffold
    ("config/_vault.py", "enable_application_inheritance"): 1,  # preserves the vault's own
    ("config/_vault.py", "_validate_data"): 1,  # reader: version check on load
    ("consolidate/review_queue.py", "layout_refusal"): 1,  # docstring
    ("consolidate/review_queue_migration.py", "rollback"): 2,  # the only writer of 1
}
_VERSION_ONE = re.compile(
    r"marginalia_yaml_version[^\n]*?[:=,]\s*1\b"
    r"|set_vault_yaml_version\([^)\n]*\b1\s*\)"
    r"|\b(?:yaml_)?version\s*=\s*1\b"
)


def _enclosing(tree: ast.AST, lineno: int) -> str:
    best = ("<module>", 0)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end and node.lineno >= best[1]:
                best = (node.name, node.lineno)
    return best[0]


def test_no_production_module_writes_a_version_one_vault_yaml() -> None:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "yaml_version" not in text and "version=1" not in text and "version = 1" not in text:
            continue
        tree = ast.parse(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if _VERSION_ONE.search(line):
                key = (str(path.relative_to(_SRC)), _enclosing(tree, lineno))
                found[key] = found.get(key, 0) + 1
    # config version reads elsewhere use the literal 1 in unrelated schemas
    # (toml/pack versions); only yaml-version spellings are in scope.
    unexpected = {key: n for key, n in found.items() if _ALLOWED.get(key) != n}
    assert not unexpected, (
        "a new (or moved) spelling of yaml version 1 under src/: decide whether it "
        f"writes a vault yaml, then fix it or allowlist it with a reason: {unexpected}"
    )
    missing = {key for key, n in _ALLOWED.items() if n and key not in found}
    assert not missing, f"allowlisted spelling no longer exists (moved?): {missing}"


def test_modules_testing_the_refusal_opt_out_of_the_conftest_shim() -> None:
    tokens = ("layout_refusal", "review_queue_migration_required", "ReviewQueueMigrationRequired")
    offenders = []
    for path in sorted((_REPO / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in tokens) and not re.search(
            r"^REAL_QUEUE_GATE\s*=\s*True\b", text, re.MULTILINE
        ):
            offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, f"set REAL_QUEUE_GATE = True at module level in: {offenders}"


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "OKTO_NEURON_MLFLOW_TRACKING_URI",
            "OKTO_NEURON_MLFLOW_EXPERIMENT",
            "MARGINALIA_MLFLOW_TRACKING_URI",
            "MARGINALIA_GLM_API_KEY",
            "MARGINALIA_TOKEN",
        }
    }
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-m", "okto_neuron.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _host(tmp_path: Path) -> Vault:
    return Vault.init(tmp_path / "host", packs=["core"])


def _assert_served_and_usable(tmp_path: Path, created: Path, host: Vault) -> None:
    """Open ``created`` exactly as the daemon does and use its review queue."""

    created = created.resolve(strict=False)
    assert vault_yaml_version(created) == 2, created
    assert layout_refusal(created) is None
    state = ServerState(
        vault=host, vault_path=Path(host.path).resolve(strict=False), multi_vault_runtime_enabled=True
    )
    try:
        runtime = state.runtime_for(created)  # the refusal point; must not raise
        with runtime.lease_vault() as vault:
            queue = ReviewQueue(created / ".marginalia", vault.store)
            queue.enqueue(NodeCandidate(type="Concept", title="born v2"), "low_confidence")
            assert [item.title for item in queue.list()] == ["born v2"]
    finally:
        state.close()


def test_python_scaffold_entry_points_are_born_v2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    host = _host(tmp_path)
    init = Vault.init(tmp_path / "via-init", packs=["core"])
    scaffold = Vault.scaffold(tmp_path / "via-scaffold", packs=["core"], embedder="stub")
    bare = tmp_path / "via-open"
    (bare / "notes").mkdir(parents=True)
    from okto_neuron.store.vault import _write_default_config_if_absent

    _write_default_config_if_absent(bare)  # what _open_vault writes for a yaml-less dir
    for created in (Path(init.path), Path(scaffold), bare):
        _assert_served_and_usable(tmp_path, created, host)
    reset_state_for_tests()


def test_daemon_vault_create_route_is_born_v2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    host = _host(tmp_path)
    state = init_state(host, host.path)
    try:
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as http:
            response = http.post(
                "/api/v1/vaults",
                json={"name": "via-route", "backend": "ladybug", "embedder": "stub", "packs": ["core"]},
            )
            assert response.status_code == 200, response.text
            created = Path(response.json()["created"]["path"])
        # The route already opened it through runtime_for; re-check the files and queue.
        assert vault_yaml_version(created) == 2
        assert layout_refusal(created) is None
        with state.runtime_for(created).lease_vault() as vault:
            queue = ReviewQueue(created / ".marginalia", vault.store)
            queue.enqueue(NodeCandidate(type="Concept", title="born v2"), "low_confidence")
            assert len(queue.list()) == 1
    finally:
        reset_state_for_tests()


def test_cli_scaffold_entry_points_are_born_v2(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    reset_state_for_tests()
    host = _host(tmp_path)
    made: list[Path] = []

    _cli(home, "init", str(tmp_path / "cli-init"), "--embedder", "stub", "--packs", "core",
         "--backend", "ladybug")
    made.append(tmp_path / "cli-init")
    _cli(home, "vault", "create", "cli-vault", "--embedder", "stub", "--packs", "core",
         "--backend", "ladybug")
    made.append(home / ".okto-neuron" / "vaults" / "cli-vault")
    _cli(home, "kg", "init", str(tmp_path / "cli-kg-init"), "--backend", "ladybug")
    made.append(tmp_path / "cli-kg-init")
    _cli(home, "onboard", "--vault", str(tmp_path / "cli-onboard"), "--non-interactive", "--yes",
         "--disable-llm", "--backend", "ladybug")
    made.append(tmp_path / "cli-onboard")
    _cli(home, "kg", "snapshot", "dump", str(tmp_path / "cli-init"), str(tmp_path / "snap"))
    _cli(home, "kg", "snapshot", "load", str(tmp_path / "snap"), str(tmp_path / "cli-snapshot"))
    made.append(tmp_path / "cli-snapshot")

    for created in made:
        assert created.is_dir(), created
        _assert_served_and_usable(tmp_path, created, host)
    reset_state_for_tests()
