"""Review queue JSON -> SQLite migration, rollback, guard and pagination (#14)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from okto_neuron.config._vault import vault_yaml_version
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate import review_queue_migration as mig
from okto_neuron.consolidate.review_queue import (
    ReviewQueue,
    _NodeEntry,
    _RelationEntry,
    entry_digest,
    load_legacy_entries,
)
from okto_neuron.consolidate.review_queue_migration import (
    migrate,
    rollback,
)
from okto_neuron.consolidate.review_queue_sqlite import (
    ReviewQueueMigrationRequired,
    SqliteQueueStore,
)
from okto_neuron.store import InMemoryStore

REAL_QUEUE_GATE = True  # these tests exercise the version-1 layout gate itself

_HELPERS_PATH = Path(__file__).with_name("test_review_queue.py")
_spec = importlib.util.spec_from_file_location("_rq_helpers", _HELPERS_PATH)
_helpers = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules.setdefault("_rq_helpers", _helpers)
_spec.loader.exec_module(_helpers)  # type: ignore[union-attr]

_NASTY = [
    -0.0,
    0.0,
    5e-324,
    2.2250738585072014e-308,
    1e-7,
    0.1,
    1e22,
    123456789.123456789,
    1.7976931348623157e308,
    -1.7976931348623157e308,
]


def _floats(rng: random.Random, dim: int) -> tuple[float, ...]:
    values = list(_NASTY[: min(dim, len(_NASTY))])
    while len(values) < dim:
        bits = struct.unpack("<d", rng.randbytes(8))[0]
        if bits == bits and abs(bits) != float("inf"):
            values.append(bits)
    return tuple(values)


def _entries(nodes: int, relations: int, dim: int, seed: int = 7) -> list:
    rng = random.Random(seed)
    entries: list = []
    for index in range(nodes):
        candidate = NodeCandidate(
            type="Claim",
            title=f"synthetic node {index}",
            content=f"body {index} é中\U0001f600",
            facets={"predicate": "p", "n": index},
            embedding=_floats(rng, dim),
        )
        entries.append(
            _NodeEntry(
                candidate=candidate,
                reason="low_confidence" if index % 2 else "contradiction",
                correlations=(),
            )
        )
    for index in range(relations):
        candidate = _helpers._relation_candidate().model_copy(update={"byte_start": index, "model_id": f"m{index}"})
        entries.append(
            _RelationEntry(
                candidate=candidate,
                reason="queue_grounding",
                pinned_proposal=_helpers._pinned_relation_proposal("queue_grounding"),
            )
        )
    rng.shuffle(entries)  # kinds interleaved, as the JSON era stored them
    return entries


def _legacy_vault(tmp_path: Path, entries: list, *, version: int = 1) -> Path:
    root = tmp_path / "vault"
    (root / ".marginalia").mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        f"# keep me\nmarginalia_yaml_version: {version}\nvault_id: scratch\n", encoding="utf-8"
    )
    payload = {"entries": [entry.to_json() for entry in entries]}
    (root / ".marginalia" / "review_queue.json").write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return root


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digests(entries: list) -> dict[str, str]:
    from okto_neuron.consolidate.review_queue import _entry_id

    return {_entry_id(entry): entry_digest(entry.to_json()) for entry in entries}


def _listing(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_production_shape_roundtrip_keeps_every_digest(tmp_path: Path) -> None:
    entries = _entries(nodes=1087, relations=835, dim=4096)
    root = _legacy_vault(tmp_path, entries)
    expected = _digests(entries)

    report = migrate(root)

    assert report.outcome == "migrated"
    assert (report.source_count, report.migrated_count, report.hash_equal) == (1922, 1922, 1922)
    assert (report.nodes, report.relations) == (1087, 835)
    assert report.normalized_rows == 0
    queue = ReviewQueue(root / ".marginalia", InMemoryStore())
    rows = SqliteQueueStore(root / ".marginalia").rows(with_embedding=True)
    from okto_neuron.consolidate.review_queue import join_record

    assert {row.candidate_id: entry_digest(join_record(row)) for row in rows} == expected
    node = next(row for row in rows if row.kind == "node")
    assert queue.resolution_scope(node.candidate_id)["entry_sha256"] == expected[node.candidate_id]
    assert vault_yaml_version(root) == 2
    assert (root / "okto-neuron.yaml").read_text().startswith("# keep me\n")
    # the embeddings left the payload column
    assert '"embedding": null' in node.payload or '"embedding":null' in node.payload


def test_float_text_is_identical_after_the_blob_roundtrip(tmp_path: Path) -> None:
    entries = _entries(nodes=3, relations=0, dim=64)
    root = _legacy_vault(tmp_path, entries)
    migrate(root)
    restored = {c.candidate_id: c for c in ReviewQueue(root / ".marginalia", None).candidates()}  # type: ignore[arg-type]
    for entry in entries:
        original = entry.candidate
        assert json.dumps(list(restored[original.candidate_id].embedding)) == json.dumps(
            list(original.embedding)
        )


@pytest.mark.parametrize(
    "step",
    ["after_backup", "after_yaml", "after_build", "after_verify", "after_replace", "after_rename"],
)
def test_crash_at_every_step_leaves_a_rerunnable_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    entries = _entries(nodes=12, relations=8, dim=16)
    root = _legacy_vault(tmp_path, entries)
    marginalia = root / ".marginalia"
    source = marginalia / "review_queue.json"
    original = _sha(source)
    expected = _digests(entries)

    def boom(name: str) -> None:
        if name == step:
            raise RuntimeError(f"injected crash at {name}")

    monkeypatch.setattr(mig, "_FAULT", boom)
    with pytest.raises(RuntimeError, match="injected crash"):
        migrate(root)
    monkeypatch.setattr(mig, "_FAULT", None)

    # the source is intact at every crash point before its retirement
    if step != "after_rename":
        assert _sha(source) == original
    # 0.3.1 safety invariant: never yaml 1 next to a SQLite queue of any kind
    if vault_yaml_version(root) == 1:
        assert not (marginalia / "review_queue.sqlite").exists()
        assert step == "after_backup"

    report = migrate(root)

    assert report.outcome in {"migrated", "finished-source-retirement", "already-migrated"}
    assert vault_yaml_version(root) == 2
    assert not list(marginalia.glob("review_queue.sqlite.tmp-*"))
    rows = SqliteQueueStore(marginalia).rows(with_embedding=True)
    from okto_neuron.consolidate.review_queue import join_record

    assert {row.candidate_id: entry_digest(join_record(row)) for row in rows} == expected
    assert _sha(marginalia / "review_queue.json.bak-v1") == original
    assert not source.exists()
    assert migrate(root).outcome == "already-migrated"


def test_dry_run_writes_nothing_and_reports(tmp_path: Path) -> None:
    root = _legacy_vault(tmp_path, _entries(10, 5, 8))
    before = _listing(root)

    report = migrate(root, dry_run=True)

    assert report.outcome == "dry-run-ok"
    assert (report.source_count, report.migrated_count, report.hash_equal) == (15, 15, 15)
    assert report.sqlite_bytes > 0 and report.source_bytes > 0
    assert _listing(root) == before


def test_refusals_leave_the_source_and_yaml_untouched(tmp_path: Path) -> None:
    root = _legacy_vault(tmp_path, _entries(3, 0, 4))
    source = root / ".marginalia" / "review_queue.json"
    data = json.loads(source.read_text())
    data["entries"][0]["candidate"]["embedding"][0] = float("nan")
    source.write_text(json.dumps(data), encoding="utf-8")  # writes the NaN token
    before = _listing(root)

    with pytest.raises(ValueError):
        migrate(root)

    assert _listing(root) == before


def test_rollback_regenerates_json_from_sqlite_and_keeps_the_sqlite(tmp_path: Path) -> None:
    entries = _entries(9, 6, 12)
    root = _legacy_vault(tmp_path, entries)
    marginalia = root / ".marginalia"
    migrate(root)
    queue = ReviewQueue(marginalia, InMemoryStore())
    extra = NodeCandidate(type="Concept", title="added after migration", embedding=(0.5, 0.25))
    queue.enqueue(extra, "low_confidence")
    queue.acknowledge(entries[0].candidate.candidate_id if isinstance(entries[0], _NodeEntry) else queue.list()[0].candidate_id)

    report = rollback(root)

    assert report.outcome == "rolled-back"
    assert vault_yaml_version(root) == 1
    assert not (marginalia / "review_queue.sqlite").exists()
    assert list(marginalia.glob("review_queue.sqlite.rolled-back-*"))
    regenerated = load_legacy_entries(marginalia / "review_queue.json")
    assert len(regenerated) == 9 + 6  # +1 added, -1 acknowledged
    assert extra.candidate_id in regenerated
    with pytest.raises(ReviewQueueMigrationRequired):
        ReviewQueue(marginalia, InMemoryStore())


def test_rollback_restore_backup_is_the_literal_copy(tmp_path: Path) -> None:
    root = _legacy_vault(tmp_path, _entries(5, 3, 8))
    marginalia = root / ".marginalia"
    original = _sha(marginalia / "review_queue.json")
    migrate(root)

    report = rollback(root, restore_backup=True)

    assert report.outcome == "rolled-back"
    assert _sha(marginalia / "review_queue.json") == original
    assert vault_yaml_version(root) == 1
    assert (marginalia / "review_queue.json.bak-v1").exists()


@pytest.mark.parametrize(
    "step", ["rollback_before_replace", "rollback_after_replace", "rollback_after_move"]
)
def test_rollback_crash_points_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    entries = _entries(6, 4, 8)
    root = _legacy_vault(tmp_path, entries)
    marginalia = root / ".marginalia"
    migrate(root)

    def boom(name: str) -> None:
        if name == step:
            raise RuntimeError("injected")

    monkeypatch.setattr(mig, "_FAULT", boom)
    with pytest.raises(RuntimeError):
        rollback(root)
    monkeypatch.setattr(mig, "_FAULT", None)
    # yaml is only lowered LAST, so a crash never yields yaml 1 with a live SQLite
    assert vault_yaml_version(root) == 2

    rollback(root)

    assert vault_yaml_version(root) == 1
    assert set(load_legacy_entries(marginalia / "review_queue.json")) == set(_digests(entries))


def test_yaml_1_with_a_stale_sqlite_treats_the_json_as_the_truth(tmp_path: Path) -> None:
    entries = _entries(4, 2, 8)
    root = _legacy_vault(tmp_path, entries)
    marginalia = root / ".marginalia"
    stale = NodeCandidate(type="Concept", title="stale")
    SqliteQueueStore(marginalia).put(
        candidate_id=stale.candidate_id,
        kind="node",
        reason="low_confidence",
        payload="{}",
        entry_sha256="sha256:0",
        embedding=None,
    )

    migrate(root)

    assert list(marginalia.glob("review_queue.sqlite.stale-*"))
    ids = {row.candidate_id for row in SqliteQueueStore(marginalia).rows()}
    assert ids == set(_digests(entries)) and stale.candidate_id not in ids


def test_readers_never_migrate(tmp_path: Path) -> None:
    root = _legacy_vault(tmp_path, _entries(3, 1, 4))
    before = _listing(root)
    with pytest.raises(ReviewQueueMigrationRequired):
        ReviewQueue(root / ".marginalia", InMemoryStore()).list()
    assert _listing(root) == before  # no sqlite, no backup, yaml untouched

    migrate(root)
    after = _listing(root)
    queue = ReviewQueue(root / ".marginalia", InMemoryStore())
    assert len(queue.list()) + len(queue.list_relations()) == 4
    queue.page(2)
    assert {k: v for k, v in _listing(root).items() if not k.endswith(("-wal", "-shm"))} == {
        k: v for k, v in after.items() if not k.endswith(("-wal", "-shm"))
    }


def test_pagination_walks_the_whole_queue_once(tmp_path: Path) -> None:
    entries = _entries(23, 14, 4)
    root = _legacy_vault(tmp_path, entries)
    migrate(root)
    queue = ReviewQueue(root / ".marginalia", InMemoryStore())

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        items, cursor, total = queue.page(10, cursor)
        seen.extend(item.candidate_id for item in items)
        pages += 1
        assert total == 37
        if cursor is None:
            break
    assert pages == 4
    assert len(seen) == len(set(seen)) == 37
    assert seen == [i.candidate_id for i in queue.list()] + [
        i.candidate_id for i in queue.list_relations()
    ]
    assert queue.page(0) == ([], None, 37)
    with pytest.raises(ValueError):
        queue.page(5, "garbage")


# -- the REAL 0.3.1 binary ---------------------------------------------------------
_MANIFEST = json.loads((Path(__file__).parents[2] / "release-manifest.json").read_text())


@pytest.fixture(scope="module")
def v031(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A scratch venv holding exactly the released 0.3.1 wheel (sha256-pinned)."""

    base = tmp_path_factory.mktemp("v031")
    wheel = Path(os.environ.get("OKTO_NEURON_031_WHEEL", base / _MANIFEST["wheel"]))
    if not wheel.exists():
        with urllib.request.urlopen(_MANIFEST["wheel_url"], timeout=120) as response:  # noqa: S310
            wheel.write_bytes(response.read())
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == _MANIFEST["sha256"]
    venv = base / "venv"
    subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(venv)], check=True, timeout=300)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheel)],
        check=True,
        timeout=900,
    )
    home = base / "home"
    home.mkdir()
    # PYTHONPATH & co. would make the 0.3.1 venv import the tree under test instead of
    # the released wheel (it then scaffolds yaml 2 and the guard test proves nothing).
    env = {
        k: v
        for k, v in os.environ.items()
        if "MLFLOW" not in k and k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"}
    }
    env["HOME"] = str(home)
    version = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import importlib.metadata as m;print(m.version('okto-neuron'))"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()
    assert version == "0.3.1"
    origin = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import okto_neuron;print(okto_neuron.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()
    assert origin.startswith(str(venv)), f"0.3.1 venv imports okto_neuron from {origin}, not its wheel"
    return {"bin": str(venv / "bin" / "okto-neuron"), "env": env, "base": base}


def _run031(v031: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [v031["bin"], *args], capture_output=True, text=True, env=v031["env"], timeout=180
    )


def test_real_031_binary_refuses_a_migrated_vault_and_rollback_reopens_it(v031: dict) -> None:
    work = v031["base"] / "guard"
    work.mkdir()
    vault = work / "vault"
    init = _run031(v031, "kg", "init", str(vault))
    assert init.returncode == 0, init.stderr
    assert vault_yaml_version(vault) == 1
    entries = _entries(7, 4, 8)
    (vault / ".marginalia").mkdir(exist_ok=True)
    (vault / ".marginalia" / "review_queue.json").write_text(
        json.dumps({"entries": [e.to_json() for e in entries]}, indent=2) + "\n", encoding="utf-8"
    )
    expected = _digests(entries)
    assert _run031(v031, "kg", "reindex", str(vault)).returncode == 0

    migrate(vault)
    refused = _run031(v031, "kg", "reindex", str(vault))
    assert refused.returncode == 4, (refused.returncode, refused.stderr)
    assert "unsupported" in refused.stderr and "supported: 1" in refused.stderr

    rollback(vault)
    reopened = _run031(v031, "kg", "reindex", str(vault))
    assert reopened.returncode == 0, reopened.stderr

    migrate(vault)
    rows = SqliteQueueStore(vault / ".marginalia").rows(with_embedding=True)
    from okto_neuron.consolidate.review_queue import join_record

    assert {row.candidate_id: entry_digest(join_record(row)) for row in rows} == expected
    assert _run031(v031, "kg", "reindex", str(vault)).returncode == 4


def test_a_failed_031_reembed_leftovers_are_tolerated(v031: dict) -> None:
    """Decision (6): stale lock/state files from a failed 0.3.1 reembed never block us."""

    work = v031["base"] / "leftovers"
    work.mkdir()
    entries = _entries(3, 1, 4)
    root = _legacy_vault(work, entries)
    marginalia = root / ".marginalia"
    for name in (".graph-handle.lock", ".bootstrap.lock"):
        (marginalia / name).write_text("", encoding="utf-8")
    (marginalia / "reembed.state.json").write_text('{"state": "failed"}', encoding="utf-8")

    assert migrate(root).outcome == "migrated"
    assert len(ReviewQueue(marginalia, InMemoryStore())) == 4
    assert (marginalia / ".graph-handle.lock").exists()
