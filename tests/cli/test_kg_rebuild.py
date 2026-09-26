from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.cli.kg import _deterministic_rebuild_files, kg_init, kg_rebuild, kg_reembed
from okto_neuron.core.schema.legacy import Edge, Node
from okto_neuron.errors import IngestError, RebuildAuditFailed, VaultLockHeld
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.semantic_fingerprint import load_semantic_materialization
from okto_neuron.store import vault as vault_module
from okto_neuron.store._bootstrap import reset_bootstrap_cache_for_tests
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import load_integrity_state
from okto_neuron.store.handle_lease import handle_lease_path
from okto_neuron.store.ladybug import LadybugStore, VaultConnection


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


def test_kg_rebuild_happy_path_swaps_and_finalizes_checkpoint(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)
    observed: list[str] = []

    def ingest(path: Path, store: LadybugStore) -> None:
        assert not store.is_closed
        observed.append(path.relative_to(vault_path).as_posix())

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    graph_path = vault_path / "graph.lbug"
    state_path = vault_path / ".marginalia" / "rebuild.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    backup_path = Path(state["backup_path"])

    assert graph_path.is_file()
    assert backup_path.is_file()
    assert backup_path.parent.name == state["graph_generation"]
    assert backup_path.name == "previous-graph.lbug"
    assert not (vault_path / "graph.rebuild.lbug").exists()
    assert state["phase"] == "complete"
    assert state["sha256"] == _sha256_file(graph_path)
    assert all(audit["status"] == "verified" for audit in state["audits"])
    assert state["final_audit"]["stage"] == "after_close_reopen"
    assert state["semantic_gate"]["status"] == "passed"
    assert state["semantic_gate"]["swap_allowed"] is True
    assert state["semantic_quality"]["schema_version"] == "semantic_quality.v1"
    assert state["post_swap_audit"]["stage"] == "after_swap_reopen"
    materialization = load_semantic_materialization(
        vault_path / ".marginalia" / "semantic-materialization.json",
        expected_graph_generation=state["graph_generation"],
    )
    assert materialization is not None
    assert materialization["source"] == "rebuild"
    assert materialization["fingerprints"]["semantic_policy"].startswith("sha256:")
    integrity = load_integrity_state(
        vault_path,
        expected_graph_generation=state["graph_generation"],
    )
    assert integrity.status is AuditStatus.VERIFIED
    assert integrity.writer_fenced is False
    assert integrity.audit_id
    assert "completed_at" in state
    assert observed == [
        "notes/a.md",
        "notes/nested/c.md",
        "notes/z.md",
    ]


def test_rebuild_links_verified_after_source_audit_to_ingest_run(tmp_path: Path) -> None:
    from okto_neuron.companion import RememberResult
    from okto_neuron.consolidate.ledger import CandidateLedger

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    ledger = CandidateLedger(vault_path / ".marginalia")
    run_ids: list[str] = []

    def ingest(path: Path, _store: LadybugStore) -> RememberResult:
        run_id = ledger.start_run(
            document_id="doc-a",
            source=path.as_posix(),
            blocks_total=1,
            model="fixture",
        )
        ledger.finish_run(
            run_id,
            state="completed",
            summary={
                "outcome": {
                    "quality": "complete",
                    "integrity": {"status": "unverified", "audit_id": None},
                }
            },
        )
        run_ids.append(run_id)
        return RememberResult(
            document_id="doc-a",
            committed=1,
            blocks_total=1,
            ledger_run_id=run_id,
        )

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert len(run_ids) == 1
    expected = {
        "status": "verified",
        "audit_id": state["audits"][0]["audit_id"],
        "graph_generation": state["graph_generation"],
    }
    detail = ledger.run_detail(run_ids[0])
    assert detail is not None
    assert detail["integrity_outcomes"][-1]["integrity"] == expected
    summary = ledger.run_summaries(limit=1)[0]
    assert summary["integrity"] == expected
    assert summary["summary"]["outcome"]["integrity"] == expected


def test_rebuild_rematerializes_after_semantic_policy_changes_mid_pass(
    tmp_path: Path,
) -> None:
    """A discovery pass cannot be labelled as one-policy materialization."""

    from okto_neuron.predicates import (
        PredicateDecisionProvenance,
        PredicateRecord,
        PredicateRegistry,
    )

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    _write_text(vault_path / "notes" / "b.md", "# B\n")
    observed: list[tuple[int, str]] = []
    pass_number = 0

    def ingest(path: Path, store: LadybugStore) -> None:
        nonlocal pass_number
        relative = path.relative_to(vault_path).as_posix()
        if relative == "notes/a.md":
            pass_number += 1
        observed.append((pass_number, relative))
        store.add_node(
            Node(
                id=relative,
                type="Concept",
                title=f"materialized-pass-{pass_number}",
            )
        )
        if pass_number == 1 and relative == "notes/a.md":
            PredicateRegistry(vault_path).upsert(
                PredicateRecord(
                    label="lives_in",
                    lifecycle="canonical",
                    definition="The subject lives in the object place.",
                    direction="subject_to_object",
                    symmetric=False,
                    signatures=(),
                    support_count=0,
                    samples=(),
                    confidence=1.0,
                    provenance=PredicateDecisionProvenance(
                        source="human",
                        decision_id="decision-lives-in",
                        judge_model="",
                        prompt_version="",
                        semantic_policy_fingerprint="fixture-policy",
                        created_at="2026-07-18T00:00:00+00:00",
                    ),
                )
            )

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    passes = state["fingerprint_passes"]
    assert [row["stable"] for row in passes] == [False, True]
    assert passes[0]["changed_after_files"] == ["notes/a.md", "notes/b.md"]
    assert passes[1]["changed_after_files"] == []
    assert observed == [
        (1, "notes/a.md"),
        (1, "notes/b.md"),
        (2, "notes/a.md"),
        (2, "notes/b.md"),
    ]
    assert state["semantic_quality"]["semantic_snapshot"]["fingerprints"] == {
        "status": "measured",
        **passes[1]["start"],
    }
    materialization = load_semantic_materialization(
        vault_path / ".marginalia" / "semantic-materialization.json",
        expected_graph_generation=state["graph_generation"],
    )
    assert materialization is not None
    assert materialization["fingerprints"] == passes[1]["start"]

    rebuilt = LadybugStore(vault_path)
    try:
        assert {node.title for node in rebuilt.list_nodes()} == {"materialized-pass-2"}
    finally:
        rebuilt.close()


def test_rebuild_reserves_validation_pass_after_three_discovery_passes(
    tmp_path: Path,
) -> None:
    """The discovery budget must not consume the required stable pass."""

    from okto_neuron.predicates import (
        PredicateDecisionProvenance,
        PredicateRecord,
        PredicateRegistry,
    )

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    pass_number = 0

    def ingest(_path: Path, store: LadybugStore) -> None:
        nonlocal pass_number
        pass_number += 1
        store.add_node(Node(id="a", type="Concept", title=f"pass-{pass_number}"))
        if pass_number > 3:
            return
        label = f"discovered_in_pass_{pass_number}"
        PredicateRegistry(vault_path).upsert(
            PredicateRecord(
                label=label,
                lifecycle="provisional",
                definition=f"Predicate discovered in pass {pass_number}.",
                direction="subject_to_object",
                symmetric=False,
                signatures=(),
                support_count=1,
                samples=(),
                confidence=0.9,
                provenance=PredicateDecisionProvenance(
                    source="model",
                    decision_id=f"decision-{pass_number}",
                    judge_model="fixture",
                    prompt_version="fixture.v1",
                    semantic_policy_fingerprint=f"fixture-policy-{pass_number}",
                    created_at="2026-07-19T00:00:00+00:00",
                ),
            )
        )

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert [row["stable"] for row in state["fingerprint_passes"]] == [
        False,
        False,
        False,
        True,
    ]
    assert pass_number == 4
    assert state["fingerprint_passes"][-1]["pass"] == 4


def test_explicit_rebuild_reuses_failed_discovery_without_changing_live_policy(
    tmp_path: Path,
) -> None:
    """A bounded failure may seed a later explicit attempt, never the live graph."""

    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.predicates import (
        PredicateDecisionProvenance,
        PredicateRecord,
        PredicateRegistry,
    )

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    original_policy = kg_cli._capture_semantic_policy_sidefiles(vault_path)
    pass_number = 0

    def ingest(_path: Path, store: LadybugStore) -> None:
        nonlocal pass_number
        pass_number += 1
        store.add_node(Node(id="a", type="Concept", title=f"pass-{pass_number}"))
        if pass_number > 4:
            return
        label = f"discovered_in_pass_{pass_number}"
        PredicateRegistry(vault_path).upsert(
            PredicateRecord(
                label=label,
                lifecycle="provisional",
                definition=f"Predicate discovered in pass {pass_number}.",
                direction="subject_to_object",
                symmetric=False,
                signatures=(),
                support_count=1,
                samples=(),
                confidence=0.9,
                provenance=PredicateDecisionProvenance(
                    source="model",
                    decision_id=f"decision-{pass_number}",
                    judge_model="fixture",
                    prompt_version="fixture.v1",
                    semantic_policy_fingerprint=f"fixture-policy-{pass_number}",
                    created_at="2026-07-19T00:00:00+00:00",
                ),
            )
        )

    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=ingest)

    assert exc_info.value.audit_status == "semantic_failed"
    assert kg_cli._capture_semantic_policy_sidefiles(vault_path) == original_policy
    pending = kg_cli._pending_semantic_policy_path(vault_path)
    assert pending.is_file()
    failed = json.loads(pending.read_text(encoding="utf-8"))
    assert (
        failed["base_fingerprints"]["semantic_policy"]
        != failed["discovered_fingerprints"]["semantic_policy"]
    )

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    assert pass_number == 5
    assert not pending.exists()
    assert {f"discovered_in_pass_{index}" for index in range(1, 5)} <= PredicateRegistry(
        vault_path
    ).labels()
    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "complete"
    assert state["fingerprint_passes"][0]["stable"] is True
    assert state["semantic_policy_seed"].endswith("pending-semantic-policy.json")


def test_rebuild_fails_closed_when_semantic_policy_never_stabilizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.cli import kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    _write_text(vault_path / "notes" / "b.md", "# B\n")
    observed: list[str] = []
    fingerprint_call = 0

    def changing_triplet(_vault_path: Path) -> dict[str, str]:
        nonlocal fingerprint_call
        fingerprint_call += 1
        value = f"sha256:{fingerprint_call:064x}"
        return {"config": value, "extraction": value, "semantic_policy": value}

    monkeypatch.setattr(
        kg_cli,
        "_effective_semantic_fingerprint_triplet",
        changing_triplet,
    )

    tmp_graph_path = vault_path / "graph.rebuild.lbug"
    state_path = vault_path / ".marginalia" / "rebuild.state.json"
    result = kg_cli._build_fresh_graph(
        vault_path,
        tmp_graph_path,
        state_path,
        kg_cli._utc_now(),
        ingest=lambda path, _store: observed.append(path.name),
    )

    assert result["swap_allowed"] is False
    assert result["semantic_gate"]["status"] == "failed"
    assert len(result["fingerprint_passes"]) == 4
    assert all(row["stable"] is False for row in result["fingerprint_passes"])
    # Discovery passes still inspect the whole corpus. Once the final bounded
    # pass drifts, its first verified source proves the staging can never swap,
    # so the remaining source is deliberately not processed.
    assert observed == [
        "a.md",
        "b.md",
        "a.md",
        "b.md",
        "a.md",
        "b.md",
        "a.md",
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "semantic_policy_unstable"
    assert len(state["fingerprint_passes"]) == 4
    assert state["fingerprint_passes"][3]["stopped_early_for_policy_drift"] is True
    assert state["fingerprint_passes"][3]["files_materialized"] == 1
    assert state["fingerprint_passes"][3]["files_total"] == 2


def test_kg_rebuild_lock_contention_raises_vault_lock_held(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    lock_path = vault_path / ".marginalia" / ".bootstrap.lock"
    lock_path.parent.mkdir(parents=True)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        os.fsync(lock_file.fileno())
        try:
            with pytest.raises(VaultLockHeld) as exc_info:
                kg_rebuild(vault_path)
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            os.fsync(lock_file.fileno())
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    assert exc_info.value.EXIT_CODE == 5
    assert exc_info.value.holding_pid == os.getpid()


@pytest.mark.parametrize("operation", [kg_rebuild, kg_reembed])
def test_standalone_graph_swap_refuses_live_vault_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: object,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setattr("okto_neuron.server.lifecycle.active_server_pid", lambda _vault: 4242)

    with pytest.raises(VaultLockHeld, match="stop the server first") as exc_info:
        operation(vault_path)  # type: ignore[operator]

    assert exc_info.value.holding_pid == 4242


@pytest.mark.parametrize("operation", [kg_rebuild, kg_reembed])
def test_standalone_graph_swap_refuses_real_cross_process_pool_handle(
    tmp_path: Path,
    operation: object,
) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _close_test_handles()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from okto_neuron.server._vault_pool import VaultPool; "
                "pool=VaultPool(); lease=pool.lease(Path(sys.argv[1])); "
                "print('READY', flush=True); sys.stdin.readline(); "
                "lease.release(); pool.close_all()"
            ),
            str(vault_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(VaultLockHeld, match="owns a live graph handle") as exc_info:
            operation(vault_path)  # type: ignore[operator]
        assert exc_info.value.holding_pid == child.pid
        assert not (vault_path / ".marginalia" / "rebuild.state.json").exists()
    finally:
        if child.stdin is not None:
            child.stdin.write("stop\n")
            child.stdin.flush()
        child.communicate(timeout=10)


def test_standalone_rebuild_rechecks_lease_immediately_before_swap(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)
    _close_test_handles()
    graph_path = vault_path / "graph.lbug"
    original_sha = _sha256_file(graph_path)

    def tamper_ownership(_path: Path, _store: LadybugStore) -> None:
        handle_lease_path(vault_path).write_text("different-owner", encoding="utf-8")

    with pytest.raises(VaultLockHeld, match="ownership changed before graph swap"):
        kg_rebuild(vault_path, ingest=tamper_ownership)

    assert _sha256_file(graph_path) == original_sha


def _close_test_handles() -> None:
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        handle.close()
    bootstrap_module._bootstrap_cache.clear()


def test_kg_rebuild_calls_ingest_once_per_file_in_posix_order(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "refs" / "z.ref", "z")
    _write_text(vault_path / "refs" / "a.ref", "a")
    _write_text(vault_path / "notes" / "b.md", "b")
    _write_text(vault_path / "notes" / "a.md", "a")
    observed: list[str] = []

    def ingest(path: Path, store: LadybugStore) -> None:
        assert not store.is_closed
        observed.append(path.relative_to(vault_path).as_posix())

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    assert observed == [
        "notes/a.md",
        "notes/b.md",
        "refs/a.ref",
        "refs/z.ref",
    ]


def test_fresh_build_accepts_only_an_exact_source_order_permutation(tmp_path: Path) -> None:
    from okto_neuron.cli import kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "a")
    _write_text(vault_path / "notes" / "b.md", "b")
    canonical = _deterministic_rebuild_files(vault_path)
    reversed_sources = list(reversed(canonical))
    observed: list[str] = []

    result = kg_cli._build_fresh_graph(
        vault_path,
        vault_path / "graph.source-order.lbug",
        vault_path / ".marginalia" / "source-order.state.json",
        kg_cli._utc_now(),
        ingest=lambda path, _store: observed.append(path.name),
        source_files=reversed_sources,
    )

    assert observed == ["b.md", "a.md"]
    assert result["files_done"] == ["notes/b.md", "notes/a.md"]

    with pytest.raises(ValueError, match="exact permutation"):
        kg_cli._build_fresh_graph(
            vault_path,
            vault_path / "graph.invalid-source-order.lbug",
            vault_path / ".marginalia" / "invalid-source-order.state.json",
            kg_cli._utc_now(),
            ingest=lambda _path, _store: None,
            source_files=[canonical[0]],
        )

    assert not (vault_path / "graph.invalid-source-order.lbug").exists()


def test_kg_rebuild_installs_private_exact_source_order_permutation(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "a")
    _write_text(vault_path / "notes" / "b.md", "b")
    reversed_sources = list(reversed(_deterministic_rebuild_files(vault_path)))
    observed: list[str] = []

    assert (
        kg_rebuild(
            vault_path,
            ingest=lambda path, _store: observed.append(path.name),
            source_files=reversed_sources,
        )
        == 0
    )

    assert observed == ["b.md", "a.md"]


def test_rebuild_enumerates_marginalia_sources_excluding_index(tmp_path: Path) -> None:
    """The canonical source set includes ``.marginalia/sources/*.md`` (where the
    companion saves ingested sources) but EXCLUDES the generated ``index.md``
    manifest. notes/ and refs/ are still covered."""
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    sources = vault_path / ".marginalia" / "sources"
    _write_text(sources / "alpha.md", "# Alpha\n")
    _write_text(sources / "beta.md", "# Beta\n")
    _write_text(sources / "index.md", "# generated tracking index\n")
    _write_text(vault_path / "notes" / "note.md", "# Note\n")

    enumerated = [
        path.relative_to(vault_path).as_posix() for path in _deterministic_rebuild_files(vault_path)
    ]

    assert ".marginalia/sources/alpha.md" in enumerated
    assert ".marginalia/sources/beta.md" in enumerated
    assert "notes/note.md" in enumerated
    # The generated manifest is never re-ingested.
    assert ".marginalia/sources/index.md" not in enumerated


def test_rebuild_applies_folder_watch_denylist_under_source_key(tmp_path: Path) -> None:
    """Rebuild must exclude the SAME scaffolding/junk the live folder watch drops,
    computed relative to each source-key root under ``.marginalia/sources/``.

    This mirrors the durable demo-vault tree: a source-key hash dir holding real
    corpus plus tooling files (``CLAUDE.md``), presales scaffolding
    (``sync-queue.md``/``sweep-dates.md``/``.migration-log.md``), a tracking
    index page, and a ``tracking/tracking`` double-descent of duplicates. The
    denylist is applied per source-key root, so legit content under the hash dir
    survives while every junk basename is dropped."""
    from okto_neuron.server._folder_watch import _is_denylisted_relpath
    from okto_neuron.server._ingest_queue import TEXT_SUFFIXES

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    key = vault_path / ".marginalia" / "sources" / "deadbeefcafef00d"

    keep = [
        "artifacts/report.md",
        "transcripts/call-01.md",
        "notes/summary.md",
        "tracking/action-items.md",  # the non-nested, legitimate tracking file
        "tracking/decision-log.md",
        "docs/readme.markdown",
        "research/finding.txt",
    ]
    drop = [
        "CLAUDE.md",  # deny-name
        "tracking/.migration-log.md",  # leading-dot file
        "tracking/sync-queue.md",  # deny-name
        "tracking/sweep-dates.md",  # deny-name
        "tracking/index.md",  # index.md under a tracking/ segment
        "tracking/tracking/action-items.md",  # double-descent duplicate
        "tracking/tracking/decision-log.md",
        "tracking/tracking/stakeholder-map.md",
    ]
    for rel in keep + drop:
        _write_text(key / rel, f"# {rel}\n")

    enumerated = {
        path.relative_to(vault_path).as_posix() for path in _deterministic_rebuild_files(vault_path)
    }

    key_prefix = ".marginalia/sources/deadbeefcafef00d/"
    for rel in keep:
        assert key_prefix + rel in enumerated, f"legit source dropped: {rel}"
    for rel in drop:
        assert key_prefix + rel not in enumerated, f"junk enumerated: {rel}"

    # PARITY GUARD: every durable text file the rebuild drops must be one the
    # shared folder-watch denylist rejects (relative to its source-key root), or
    # the top-level generated manifest. This is what stops the two paths drifting.
    sources_path = vault_path / ".marginalia" / "sources"
    manifest = sources_path / "index.md"
    for path in sources_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel_parts = path.relative_to(sources_path).parts
        rel_under_key = Path(*rel_parts[1:]) if len(rel_parts) > 1 else Path(*rel_parts)
        dropped = path.relative_to(vault_path).as_posix() not in enumerated
        should_drop = path == manifest or _is_denylisted_relpath(rel_under_key)
        assert dropped == should_drop, f"drift for {path.relative_to(vault_path)}"


# PROV-O + bridge + mention edges are content-addressed
# (``id == sha256_hex("edge", src, type, dst)``). Consolidation topology edges
# (LLM relationships) carry a random ``Edge.id`` default, so the hash check applies
# ONLY to these types.
_CONTENT_ADDRESSED_EDGE_TYPES = frozenset(
    {
        "prov:wasDerivedFrom",
        "prov:wasGeneratedBy",
        "prov:wasAttributedTo",
        "rdf:subject",
        "rdf:object",
        "schema:mentions",
    }
)


def test_rebuild_builds_fresh_graph_with_no_inconsistent_edges(tmp_path: Path) -> None:
    """Model-free end-to-end: rebuild into a FRESH graph via the deterministic
    ingest path, then assert no edge is corrupt.

    The PROVEN corruption signature (the bug this whole change fixes) is an
    adjacency-vs-stored-property DESYNC: the graph topology ``(s)-[e]->(d)`` no
    longer matches the edge's own ``src``/``dst`` columns. A clean fresh build has
    zero such desyncs. Content-addressed edges additionally have a stable
    ``id == sha256_hex("edge", src, type, dst)``; that hash check is scoped to those
    types only (topology edges carry a random id by design)."""
    from okto_neuron.ingest import ingest_document

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    # Frontmatter tags + headings mint deterministic ``has_tag`` / ``has_heading``
    # Claims, each with PROV-O + content-addressed ``rdf:subject`` edges — so both
    # the adjacency and the hash branches below have edges to chew on.
    _write_text(
        vault_path / "notes" / "note.md",
        "---\ntags: [alpha, beta]\n---\n\n# Title\n\n## Section\n\nbody text\n",
    )

    def ingest(path: Path, store: LadybugStore) -> None:
        ingest_document(store, path, vault_root=vault_path)

    assert kg_rebuild(vault_path, ingest=ingest) == 0

    reset_bootstrap_cache_for_tests(vault_path)
    store = LadybugStore(vault_path)
    try:
        nodes = list(store.list_nodes())
        # Topological adjacency joined against each edge's stored src/dst columns.
        rows = store._fetch_rows(
            """
            MATCH (s:Node)-[e:Edge]->(d:Node)
            RETURN s.id, d.id, e.src, e.dst, e.id, e.type
            """,
            {},
        )
    finally:
        store.close()

    assert nodes, "rebuilt graph must contain nodes"
    # Guard against a vacuous check: the corruption signature is meaningless if the
    # rebuilt graph has no edges at all.
    assert rows, "fixture produced no edges — corruption check would be vacuous"

    adjacency_desync = [
        (etype, eid)
        for adj_src, adj_dst, prop_src, prop_dst, eid, etype in rows
        if adj_src != prop_src or adj_dst != prop_dst
    ]
    assert adjacency_desync == [], (
        f"{len(adjacency_desync)} adjacency-vs-stored-property desyncs (corruption "
        f"signature) in rebuilt graph: {adjacency_desync}"
    )

    hash_mismatch = [
        (etype, eid)
        for _adj_src, _adj_dst, prop_src, prop_dst, eid, etype in rows
        if etype in _CONTENT_ADDRESSED_EDGE_TYPES
        and str(eid) != sha256_hex("edge", str(prop_src), str(etype), str(prop_dst))
    ]
    assert hash_mismatch == [], (
        f"{len(hash_mismatch)} content-addressed edges with a mismatched id: {hash_mismatch}"
    )


def test_llm_unavailable_on_one_file_marks_staging_unswappable(
    tmp_path: Path,
) -> None:
    """Defect C: a total LLM failure ingesting ONE file (e.g. a single
    transient timeout on a one-block file) must not blast-radius the WHOLE
    rebuild. Before the fix, ``_build_fresh_graph``'s bare ``except
    Exception`` wrapped ANY ingest failure into ``IngestError`` and re-raised,
    which the CALLER's own ``except Exception`` catches by discarding the
    entire tmp graph — so one flaky file aborted a rebuild that would
    otherwise have succeeded for every other file. The fix carves out
    ``LLMUnavailableError`` specifically: record it in ``failed_files`` and
    keep going."""
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    _write_text(vault_path / "notes" / "b.md", "# B\n")
    _write_text(vault_path / "notes" / "c.md", "# C\n")

    from okto_neuron.cli.kg import _build_fresh_graph, _utc_now
    from okto_neuron.companion import LLMUnavailableError

    observed: list[str] = []

    def ingest(path: Path, store: LadybugStore) -> None:
        rel = path.relative_to(vault_path).as_posix()
        observed.append(rel)
        if rel == "notes/b.md":
            raise LLMUnavailableError("simulated total LLM outage for this file")

    tmp_graph_path = vault_path / "graph.rebuild.lbug"
    state_path = vault_path / ".marginalia" / "rebuild.state.json"
    result = _build_fresh_graph(vault_path, tmp_graph_path, state_path, _utc_now(), ingest=ingest)

    # every file was attempted, including the one after the failure...
    assert observed == ["notes/a.md", "notes/b.md", "notes/c.md"]
    # ...but only the two healthy files count as done.
    assert result["files_done"] == ["notes/a.md", "notes/c.md"]
    assert result["count"] == 2
    assert result["failed_files"] == [
        {"file": "notes/b.md", "error": "simulated total LLM outage for this file"}
    ]
    # The complete staging graph remains available to the caller, but it is not a
    # valid swap candidate because one source lacks its LLM-derived artifacts.
    assert tmp_graph_path.exists()
    assert result["swap_allowed"] is False
    assert result["final_audit"]["status"] == "verified"


def test_rebuild_refuses_corrupt_staging_and_preserves_live_graph(tmp_path: Path) -> None:
    from okto_neuron.companion import RememberResult
    from okto_neuron.consolidate.ledger import CandidateLedger

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)
    live = LadybugStore(vault_path)
    live.add_node(Node(id="live-sentinel", type="Concept", title="live"))
    live.close()
    ledger = CandidateLedger(vault_path / ".marginalia")
    run_ids: dict[str, str] = {}

    def ingest(path: Path, store: LadybugStore) -> RememberResult:
        rel = path.relative_to(vault_path).as_posix()
        if rel == "notes/a.md":
            for node_id in ("actual", "property", "destination"):
                store.add_node(Node(id=node_id, type="Concept", title=node_id))
            store.add_edge(
                Edge(
                    id="corruptible-edge",
                    type="relates_to",
                    src="actual",
                    dst="destination",
                )
            )
        elif rel == "notes/nested/c.md":
            store._execute(  # noqa: SLF001 - deliberate corruption fixture
                "MATCH (:Node)-[e:Edge {id: $id}]->(:Node) SET e.src = $src",
                {"id": "corruptible-edge", "src": "property"},
            )
        run_id = ledger.start_run(
            document_id=f"doc:{rel}",
            source=rel,
            blocks_total=1,
            model="fixture",
        )
        ledger.finish_run(
            run_id,
            state="completed",
            summary={"outcome": {"quality": "complete"}},
        )
        run_ids[rel] = run_id
        return RememberResult(
            document_id=f"doc:{rel}",
            committed=1,
            blocks_total=1,
            ledger_run_id=run_id,
        )

    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=ingest)

    assert exc_info.value.failing_file == "notes/nested/c.md"
    assert exc_info.value.audit_status == "failed"
    retained = exc_info.value.staging_path
    assert retained is not None and retained.is_file()
    assert not (vault_path / "graph.rebuild.lbug").exists()

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "validation_failed"
    assert state["staging_graph"] == str(retained)
    assert [audit["status"] for audit in state["audits"]] == [
        "verified",
        "failed",
        "failed",
    ]
    failed_detail = ledger.run_detail(run_ids["notes/nested/c.md"])
    assert failed_detail is not None
    failed_outcome = failed_detail["integrity_outcomes"][-1]
    assert failed_outcome["quality"] == "integrity_failed"
    assert failed_outcome["integrity"] == {
        "status": "failed",
        "audit_id": state["audits"][1]["audit_id"],
        "graph_generation": state["graph_generation"],
    }

    reopened = LadybugStore(vault_path)
    try:
        assert {node.id for node in reopened.list_nodes()} == {"live-sentinel"}
    finally:
        reopened.close()


def test_rebuild_refuses_provider_partial_staging(tmp_path: Path) -> None:
    from okto_neuron.companion import LLMUnavailableError

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)

    def ingest(path: Path, store: LadybugStore) -> None:
        if path.name == "c.md":
            raise LLMUnavailableError("provider timed out")

    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=ingest)

    assert exc_info.value.failing_file == "notes/nested/c.md"
    assert exc_info.value.audit_status == "provider_failed"
    assert exc_info.value.staging_path is not None
    assert exc_info.value.staging_path.is_file()
    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "validation_failed"
    assert state["failed_files"] == [{"file": "notes/nested/c.md", "error": "provider timed out"}]


def test_rebuild_preserves_generic_ingest_failure_boundary(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)

    def ingest(path: Path, store: LadybugStore) -> None:
        del store
        if path.name == "c.md":
            raise RuntimeError("azure relation gate exploded")

    with pytest.raises(
        IngestError,
        match="RuntimeError: azure relation gate exploded",
    ):
        kg_rebuild(vault_path, ingest=ingest)

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "validation_failed"
    assert state["failing_file"] == "notes/nested/c.md"
    assert state["build_error"] == "IngestError: RuntimeError: azure relation gate exploded"
    assert Path(state["staging_graph"]).is_file()


def test_offline_rebuild_failure_restores_decision_sidefiles(tmp_path: Path) -> None:
    from okto_neuron.companion import LLMUnavailableError
    from okto_neuron.predicates import PredicateRegistry

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)
    registry = PredicateRegistry(vault_path)
    registry.seed_builtins()
    original = registry.path.read_bytes()

    def ingest(path: Path, store: LadybugStore) -> None:
        del store
        if path.name == "a.md":
            registry.path.write_bytes(original + b" ")
            raise LLMUnavailableError("fixture provider failure")

    with pytest.raises(RebuildAuditFailed):
        kg_rebuild(vault_path, ingest=ingest)

    assert registry.path.read_bytes() == original


def test_rebuild_refuses_failed_registered_semantic_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.cli.kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_fixture_files(vault_path)
    live_sha = _sha256_file(vault_path / "graph.lbug")

    def failed_report(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "schema_version": "semantic_quality.v1",
            "rebuild_gate": {
                "schema_version": "semantic_rebuild_gate.v1",
                "status": "failed",
                "swap_allowed": False,
                "failed_codes": ["registered_predicates"],
                "checks": [],
            },
        }

    monkeypatch.setattr(kg_cli, "_semantic_rebuild_report", failed_report)

    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=lambda path, store: None)

    assert exc_info.value.audit_status == "semantic_failed"
    assert _sha256_file(vault_path / "graph.lbug") == live_sha
    assert exc_info.value.staging_path is not None
    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "validation_failed"
    assert state["semantic_gate"]["failed_codes"] == ["registered_predicates"]


def test_rebuild_refuses_incomplete_close_reopen_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.cli.kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    original = kg_cli._audit_rebuild_graph_path

    def incomplete(*args, **kwargs):
        result, payload = original(*args, **kwargs)
        if kwargs["stage"] == "after_close_reopen":
            result = replace(
                result,
                status=AuditStatus.INCOMPLETE,
                edges_complete=False,
                incomplete_reasons=("simulated truncated edge scan",),
            )
            payload = kg_cli._rebuild_audit_payload(
                result,
                stage=kwargs["stage"],
                file=None,
            )
        return result, payload

    monkeypatch.setattr(kg_cli, "_audit_rebuild_graph_path", incomplete)
    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=lambda _path, _store: None)

    assert exc_info.value.audit_status == "incomplete"
    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "validation_failed"
    assert state["final_audit"]["status"] == "incomplete"


def test_rebuild_post_swap_failure_fences_new_generation_and_keeps_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.cli.kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    original = kg_cli._audit_rebuild_graph_path

    def fail_after_swap(*args, **kwargs):
        result, payload = original(*args, **kwargs)
        if kwargs["stage"] == "after_swap_reopen":
            result = replace(
                result,
                status=AuditStatus.FAILED,
                issue_count=1,
            )
            payload = kg_cli._rebuild_audit_payload(
                result,
                stage=kwargs["stage"],
                file=None,
            )
        return result, payload

    monkeypatch.setattr(kg_cli, "_audit_rebuild_graph_path", fail_after_swap)
    with pytest.raises(RebuildAuditFailed) as exc_info:
        kg_rebuild(vault_path, ingest=lambda _path, _store: None)

    assert exc_info.value.audit_status == "failed"
    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    assert state["phase"] == "post_swap_validation_failed"
    assert Path(state["backup_path"]).is_file()
    integrity = load_integrity_state(
        vault_path,
        expected_graph_generation=state["graph_generation"],
    )
    assert integrity.status is AuditStatus.FAILED
    assert integrity.writer_fenced is True


def test_rebuild_uses_distinct_backups_and_moves_live_sidecars(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    VaultConnection.close_all()
    reset_bootstrap_cache_for_tests(vault_path)
    stale_wal = vault_path / "graph.lbug.wal"
    stale_wal.write_bytes(b"old-live-sidecar")

    assert kg_rebuild(vault_path, ingest=lambda _path, _store: None) == 0
    first = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    first_backup = Path(first["backup_path"])
    assert first_backup.with_name(f"{first_backup.name}.wal").read_bytes() == b"old-live-sidecar"
    assert not stale_wal.exists()

    assert kg_rebuild(vault_path, ingest=lambda _path, _store: None) == 0
    second = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    second_backup = Path(second["backup_path"])
    assert first["graph_generation"] != second["graph_generation"]
    assert first_backup != second_backup
    assert first_backup.is_file()
    assert second_backup.is_file()


def test_rebuild_retains_staging_when_close_reopen_audit_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.cli.kg as kg_cli

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _write_text(vault_path / "notes" / "a.md", "# A\n")

    def crash_reopen(*_args, **_kwargs):
        raise RuntimeError("simulated durable reopen failure")

    monkeypatch.setattr(kg_cli, "_audit_rebuild_graph_path", crash_reopen)
    with pytest.raises(RuntimeError, match="simulated durable reopen failure"):
        kg_rebuild(vault_path, ingest=lambda _path, _store: None)

    state = json.loads(
        (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
    )
    retained = Path(state["staging_graph"])
    assert state["phase"] == "validation_failed"
    assert "simulated durable reopen failure" in state["build_error"]
    assert retained.is_file()
    assert (retained.parent / "validation.json").is_file()


def test_graph_family_move_rolls_back_main_when_sidecar_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.cli.kg as kg_cli

    source = tmp_path / "graph.lbug"
    wal = tmp_path / "graph.lbug.wal"
    target = tmp_path / "artifacts" / "previous-graph.lbug"
    source.write_bytes(b"main")
    wal.write_bytes(b"wal")
    real_replace = kg_cli.os.replace
    calls = 0

    def fail_sidecar(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("simulated locked WAL")
        return real_replace(src, dst)

    monkeypatch.setattr(kg_cli.os, "replace", fail_sidecar)
    with pytest.raises(PermissionError, match="simulated locked WAL"):
        kg_cli._move_graph_family(source, target)

    assert source.read_bytes() == b"main"
    assert wal.read_bytes() == b"wal"
    assert not target.exists()


def test_live_sidecar_scan_excludes_legacy_backup_family(tmp_path: Path) -> None:
    import okto_neuron.cli.kg as kg_cli

    graph = tmp_path / "graph.lbug"
    live_wal = tmp_path / "graph.lbug.wal"
    legacy_backup = tmp_path / "graph.lbug.bak"
    legacy_backup_wal = tmp_path / "graph.lbug.bak.wal"
    for path in (graph, live_wal, legacy_backup, legacy_backup_wal):
        path.write_bytes(path.name.encode())

    assert kg_cli._active_graph_sidecars(graph) == [live_wal]


def test_migrate_bridge_edges_command_is_removed() -> None:
    """TASK 2: the corrupting ``kg migrate bridge-edges`` command no longer exists,
    while the safe per-commit ``ensure_source_mentions(store, node_ids)`` still
    works (covered fully in tests/migrate/test_source_mentions.py)."""
    from click.testing import CliRunner

    from okto_neuron.cli import app
    from okto_neuron.migrate import ensure_source_mentions

    result = CliRunner().invoke(app, ["migrate", "bridge-edges", "--help"])
    assert result.exit_code != 0  # no such command

    # The safe helper survives and is importable.
    assert callable(ensure_source_mentions)


def _write_fixture_files(vault_path: Path) -> None:
    _write_text(vault_path / "notes" / "z.md", "# Z\n")
    _write_text(vault_path / "notes" / "a.md", "# A\n")
    _write_text(vault_path / "notes" / "nested" / "c.md", "# C\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
