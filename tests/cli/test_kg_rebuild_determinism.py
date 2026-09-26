from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


_REBUILD_SCRIPT = """
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys

from okto_neuron.cli.kg import kg_rebuild
from okto_neuron.core.schema import Node, Provenance


vault_path = Path(sys.argv[1])
created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
provenance = Provenance(source="ingest", rule_id="tr8-stub")


def stub_ingest(path, store):
    content = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    store.add_node(
        Node(
            id=f"note:{digest}",
            type="Document",
            title=path.stem,
            content=content,
            created_at=created_at,
            provenance=provenance,
        )
    )


raise SystemExit(kg_rebuild(vault_path, ingest=stub_ingest))
"""


@pytest.mark.xfail(
    strict=True,
    reason="Content-hash-derived stable node IDs ship in Topic 03 — TR8",
)
def test_kg_rebuild_graph_lbug_is_byte_identical_across_fresh_processes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_fixture_vault(vault_path)

    first_sha256 = _run_rebuild_and_hash_graph(vault_path)
    second_sha256 = _run_rebuild_and_hash_graph(vault_path)

    assert second_sha256 == first_sha256


def _write_fixture_vault(vault_path: Path) -> None:
    _write_text(vault_path / "notes" / "alpha.md", "# Alpha\n\nFirst note.\n")
    _write_text(vault_path / "notes" / "beta.md", "# Beta\n\nSecond note.\n")
    _write_text(vault_path / "notes" / "nested" / "gamma.md", "# Gamma\n\nThird note.\n")


def _run_rebuild_and_hash_graph(vault_path: Path) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", _REBUILD_SCRIPT, str(vault_path)],
        cwd=Path(__file__).resolve().parents[2],
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return _sha256_file(vault_path / "graph.lbug")


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
