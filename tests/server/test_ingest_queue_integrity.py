"""Queue-integrity fixes from the 2026-07-02 live-run remediation (Phase 2).

F2 — enqueue dedup (a queued path refreshes its durable copy instead of
duplicating; the live run queued one file 11×) and stable state-scoped item
ids (the len()-based scheme minted colliding ids after deletes).
F4 — a provider error with zero yield terminates as ``error`` (visible,
retryable), and ``retry_item`` also accepts done-with-provider-error items.

Model-free: stub state (per tests/support conftest style) + a stub companion
for the drain worker. No LLM, no vault handle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem


def _state(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_cancel_requested=False,
        ingest_seq=0,
        draining=False,
        writer_lock=asyncio.Lock(),
        last_ingest_at=None,
        # issue #5: multi-vault scheduler signal, bumped alongside the scalar.
        last_ingest_at_by_vault={},
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── F2: enqueue dedup ─────────────────────────────────────────────────────────


class TestEnqueueDedup:
    def test_second_enqueue_of_queued_path_refreshes_not_duplicates(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        src = tmp_path / "ext" / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"

        first = iq.enqueue_paths(state, [src], sources)
        assert len(first) == 1
        item = first[0]

        _write(src, "v2 fresher bytes")
        second = iq.enqueue_paths(state, [src], sources)

        assert second == []  # no duplicate item
        assert len(state.ingest_queue) == 1
        # Freshest bytes won: the durable copy now holds v2.
        assert Path(item.path).read_text(encoding="utf-8") == "v2 fresher bytes"
        # The refresh left an event on the surviving item.
        assert any(e["kind"] == "refreshed" for e in item.events)

    def test_refresh_is_persisted_even_when_no_new_items(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        src = tmp_path / "ext" / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"
        iq.enqueue_paths(state, [src], sources)

        _write(src, "v2")
        iq.enqueue_paths(state, [src], sources)

        # Rehydrating a fresh state sees the refreshed event on disk.
        fresh = _state(state.vault_path)
        iq.rehydrate_queue(fresh)
        assert len(fresh.ingest_queue) == 1
        assert any(e["kind"] == "refreshed" for e in fresh.ingest_queue[0].events)

    def test_processing_path_still_appends_fresh_item(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        src = tmp_path / "ext" / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"
        first = iq.enqueue_paths(state, [src], sources)
        first[0].status = "processing"  # drain worker picked it up

        _write(src, "v2")
        second = iq.enqueue_paths(state, [src], sources)

        assert len(second) == 1
        assert len(state.ingest_queue) == 2
        assert second[0].id != first[0].id

    def test_terminal_path_appends_fresh_item(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        src = tmp_path / "ext" / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"
        first = iq.enqueue_paths(state, [src], sources)
        first[0].status = "done"

        _write(src, "v2")
        second = iq.enqueue_paths(state, [src], sources)
        assert len(second) == 1
        assert len(state.ingest_queue) == 2


# ── F2: stable ids ────────────────────────────────────────────────────────────


class TestStableIds:
    def test_ids_unique_after_delete(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        sources = state.vault_path / ".marginalia" / "sources"
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        c = tmp_path / "c.md"
        for p in (a, b, c):
            _write(p, p.name)

        items = iq.enqueue_paths(state, [a, b], sources)
        items[0].status = "done"
        iq.delete_item(state, items[0].id)  # queue shrinks

        (new,) = iq.enqueue_paths(state, [c], sources)
        ids = [i.id for i in state.ingest_queue]
        assert len(ids) == len(set(ids))
        # Counter is monotonic: the new id's seq is past both earlier items.
        assert int(new.id.split("-", 1)[0]) == 2

    def test_switch_vault_reseeds_counter_from_new_vault(self, tmp_path: Path) -> None:
        """The counter reset in switch_vault must happen BEFORE rehydrate — a
        reset placed after rehydrate wipes the seed and resurrects the
        id-collision bug (caught live as a planted mutation)."""
        from okto_neuron import Vault
        from okto_neuron.server.state import init_state, reset_state_for_tests

        reset_state_for_tests()
        vault_a = Vault.init(tmp_path / "a")
        vault_b = Vault.init(tmp_path / "b")
        try:
            # Seed vault B's sidecar with a high persisted id.
            seed = _state(Path(vault_b.path))
            seed.ingest_queue = [
                IngestItem(id="41-cafecafe", name="x.md", path="/x.md", status="done"),
            ]
            iq.persist(seed)

            state = init_state(vault_a, Path(vault_a.path))
            state.ingest_seq = 7  # pretend vault A minted some ids
            state.switch_vault(vault_b, Path(vault_b.path))
            assert state.ingest_seq == 42
        finally:
            vault_a.close()
            vault_b.close()
            reset_state_for_tests()

    def test_rehydrate_seeds_counter_past_persisted_ids(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        state.ingest_queue = [
            IngestItem(id="7-aaaaaaaa", name="a.md", path="/x/a.md", status="done"),
            IngestItem(id="3-bbbbbbbb", name="b.md", path="/x/b.md", status="done"),
        ]
        iq.persist(state)

        fresh = _state(state.vault_path)
        iq.rehydrate_queue(fresh)
        assert fresh.ingest_seq == 8

        src = tmp_path / "new.md"
        _write(src, "x")
        (item,) = iq.enqueue_paths(fresh, [src], fresh.vault_path / ".marginalia" / "sources")
        assert item.id.startswith("8-")


# ── F4: zero-yield provider error → error status; broadened retry ────────────


def _drain_result(**overrides) -> SimpleNamespace:
    base = dict(
        committed=0,
        queued=0,
        blocks_total=1,
        nodes_extracted=0,
        edges_extracted=0,
        provider_error=None,
        claims_minted=0,
        outcomes=(),
        outcome={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_drain(state: SimpleNamespace, result: SimpleNamespace) -> None:
    class _Companion:
        def remember(self, path, sensitivity="default", on_progress=None, on_event=None, should_cancel=None):
            return result

    state.ingest_worker_active = True
    asyncio.run(iq._drain(state, lambda s: _Companion()))


def _run_drain_exception(state: SimpleNamespace, error: BaseException) -> None:
    class _Companion:
        def remember(self, path, sensitivity="default", on_progress=None, on_event=None, should_cancel=None):
            raise error

    state.ingest_worker_active = True
    asyncio.run(iq._drain(state, lambda s: _Companion()))


class TestProviderErrorTerminal:
    def _queued_item(self, state: SimpleNamespace, tmp_path: Path) -> IngestItem:
        src = tmp_path / "note.md"
        _write(src, "body")
        (item,) = iq.enqueue_paths(state, [src], state.vault_path / ".marginalia" / "sources")
        return item

    def test_zero_yield_provider_error_becomes_error(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(provider_error="LLM unreachable"))

        assert item.status == "error"
        assert item.stage == "error"
        assert "zero yield" in (item.error or "")
        assert state.last_ingest_at is None  # not a successful ingest

    def test_partial_yield_stays_done_with_provider_error(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(provider_error="degraded mid-file", committed=3))
        assert item.status == "done"
        assert item.provider_error == "degraded mid-file"

    def test_partial_outcome_is_persisted_without_changing_done_lifecycle(
        self, tmp_path: Path
    ) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        outcome = {
            "quality": "partial",
            "units": {"scheduled": 2, "succeeded": 1, "failed": 1},
            "failed_units": [{"unit_id": "unit-2", "error_class": "timeout", "retryable": True}],
        }

        _run_drain(
            state,
            _drain_result(
                provider_error="degraded mid-file",
                committed=3,
                outcome=outcome,
            ),
        )

        assert item.status == "done"
        assert item.stage == "done"
        assert item.outcome == outcome

    def test_failed_outcome_sets_error_lifecycle(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        outcome = {
            "quality": "failed",
            "units": {"scheduled": 1, "succeeded": 0, "failed": 1},
            "failed_units": [{"unit_id": "unit-1", "error_class": "auth", "retryable": False}],
        }

        _run_drain(state, _drain_result(outcome=outcome))

        assert item.status == "error"
        assert item.stage == "error"
        assert item.error == "ingest outcome: failed"
        assert item.outcome == outcome
        assert state.last_ingest_at is None

    def test_total_provider_failure_preserves_structured_outcome(self, tmp_path: Path) -> None:
        from okto_neuron.companion import LLMUnavailableError

        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        outcome = {
            "quality": "failed",
            "units": {"scheduled": 1, "succeeded": 0, "failed": 1},
            "failed_units": [
                {
                    "unit_id": "unit-1",
                    "error_class": "authentication",
                    "retryable": False,
                }
            ],
        }

        _run_drain_exception(
            state,
            LLMUnavailableError("invalid key", outcome=outcome),
        )

        assert item.status == "error"
        assert item.outcome == outcome
        assert item.provider_error == "invalid key"
        _, retry_error = iq.retry_item(state, item.id)
        assert retry_error == "conflict"

    def test_integrity_fence_produces_non_retryable_integrity_outcome(self, tmp_path: Path) -> None:
        from okto_neuron.server._integrity import IntegrityFenceError
        from okto_neuron.store.integrity import AuditStatus
        from okto_neuron.store.integrity_state import GraphIntegrityState

        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        fence = GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation="generation-a",
            writer_fenced=True,
            reason="adjacency mismatch",
        )

        _run_drain_exception(state, IntegrityFenceError(fence))

        assert item.status == "error"
        assert item.outcome == {
            "quality": "integrity_failed",
            "units": {},
            "failed_units": [],
            "integrity": {
                "status": "failed",
                "audit_id": None,
                "graph_generation": "generation-a",
            },
        }
        _, retry_error = iq.retry_item(state, item.id)
        assert retry_error == "conflict"

    def test_unclassified_exception_is_explicitly_operator_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)

        _run_drain_exception(state, RuntimeError("unexpected"))

        assert item.status == "error"
        assert item.outcome == {
            "quality": "failed",
            "error_class": "internal",
            "retryable": True,
        }
        retried, retry_error = iq.retry_item(state, item.id)
        assert retry_error is None
        assert retried is item
        assert item.status == "queued"

    def test_gate_parked_candidates_count_as_yield(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(provider_error="late failure", queued=5))
        assert item.status == "done"  # parked yield is not zero-yield

    def test_claims_only_yield_stays_done(self, tmp_path: Path) -> None:
        """Pins the `claims == 0` conjunct of the zero-yield gate: a provider
        error after claims were minted (committed=0, queued=0) is partial
        yield, not a failure."""
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(provider_error="late failure", claims_minted=4))
        assert item.status == "done"
        assert item.claims == 4

    def test_clean_zero_yield_without_provider_error_stays_done(self, tmp_path: Path) -> None:
        """An empty-but-healthy ingest (nothing extractable, no provider
        error) is NOT an error — pins the `bool(provider_error)` conjunct."""
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result())
        assert item.status == "done"

    def test_clean_success_still_done(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(committed=2, claims_minted=4))
        assert item.status == "done"
        assert state.last_ingest_at is not None

    def test_verified_success_schedules_cross_document_proposal(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        from okto_neuron.server import _jobs

        state = _state(tmp_path / "vault")
        state.curation_jobs = []
        state.curation_worker_active = True  # keep proposal queued and model-free
        monkeypatch.setitem(
            _jobs._REGISTRY,
            "reconcile-propose",
            (lambda _s, _j: {}, False),
        )
        monkeypatch.setattr(
            _jobs,
            "_VERIFIED_SNAPSHOT_KINDS",
            {*_jobs._VERIFIED_SNAPSHOT_KINDS, "reconcile-propose"},
        )
        item = self._queued_item(state, tmp_path)

        _run_drain(
            state,
            _drain_result(
                committed=2,
                outcome={
                    "quality": "complete",
                    "integrity": {
                        "status": "verified",
                        "graph_generation": "generation-a",
                    },
                },
            ),
        )

        assert item.status == "done"
        assert len(state.curation_jobs) == 1
        assert state.curation_jobs[0].kind == "reconcile-propose"
        semantic = item.outcome["cross_document_reconciliation"]
        assert semantic["state"] == "scheduled"
        assert any(event["kind"] == "cross_document_reconciliation" for event in item.events)

    def test_llm_disabled_shape_stays_done_not_error(self, tmp_path: Path) -> None:
        """Defect A regression: llm.enabled=false used to ride ``provider_error``
        (a truthy message), which THIS F4 rule then read as "every ingest on
        this vault failed" — marking every disabled-vault item "error" and
        retry-looping forever. The fixed ``RememberResult`` shape for a
        disabled vault carries ``llm_disabled=True`` with ``provider_error``
        left ``None`` — the drain worker must land it as a normal, non-error
        done item."""
        state = _state(tmp_path / "vault")
        item = self._queued_item(state, tmp_path)
        _run_drain(state, _drain_result(llm_disabled=True))
        assert item.status == "done"
        assert item.stage == "done"
        assert item.provider_error is None


class TestRetryBroadened:
    def _item(self, state, status="error", provider_error=None) -> IngestItem:
        item = IngestItem(
            id="0-deadbeef",
            name="a.md",
            path="/x/a.md",
            status=status,
            provider_error=provider_error,
        )
        state.ingest_queue.append(item)
        return item

    def test_error_item_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="error")
        got, err = iq.retry_item(state, item.id)
        assert err is None
        assert got.status == "queued"

    def test_legacy_quality_only_internal_failure_is_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="error")
        item.outcome = {"quality": "failed"}

        got, err = iq.retry_item(state, item.id)

        assert err is None
        assert got is item
        assert item.status == "queued"

    def test_done_with_provider_error_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="done", provider_error="degraded")
        got, err = iq.retry_item(state, item.id)
        assert err is None
        assert got.status == "queued"
        assert got.provider_error is None  # reset for the fresh run

    def test_retry_resets_terminal_outcome(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="done", provider_error="degraded")
        item.outcome = {
            "quality": "partial",
            "failed_units": [{"unit_id": "unit-1", "error_class": "timeout", "retryable": True}],
        }

        got, err = iq.retry_item(state, item.id)

        assert err is None
        assert got is item
        assert got.outcome == {}

    def test_failed_auth_outcome_is_not_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="error", provider_error="authentication")
        item.outcome = {
            "quality": "failed",
            "failed_units": [
                {
                    "unit_id": "unit-1",
                    "error_class": "authentication",
                    "retryable": False,
                }
            ],
        }

        got, err = iq.retry_item(state, item.id)

        assert got is item
        assert err == "conflict"
        assert item.status == "error"

    def test_failed_timeout_outcome_is_retryable(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="error", provider_error="timeout")
        item.outcome = {
            "quality": "failed",
            "failed_units": [{"unit_id": "unit-1", "error_class": "timeout", "retryable": True}],
        }

        got, err = iq.retry_item(state, item.id)

        assert err is None
        assert got is item
        assert item.status == "queued"

    def test_clean_done_conflicts(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="done")
        _, err = iq.retry_item(state, item.id)
        assert err == "conflict"

    def test_queued_conflicts(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = self._item(state, status="queued")
        _, err = iq.retry_item(state, item.id)
        assert err == "conflict"

    def test_retry_conflicts_when_same_path_already_queued(self, tmp_path: Path) -> None:
        """Review finding: the watcher may have re-enqueued the path after the
        failure — retrying the errored item would drain identical bytes twice."""
        state = _state(tmp_path / "vault")
        errored = self._item(state, status="error")
        fresh = IngestItem(id="1-deadbeef", name="a.md", path=errored.path)
        state.ingest_queue.append(fresh)

        _, err = iq.retry_item(state, errored.id)
        assert err == "conflict"
        assert errored.status == "error"

    def test_retry_conflicts_when_same_path_already_processing(self, tmp_path: Path) -> None:
        """Same-path conflict guard must also cover a PROCESSING duplicate, not
        just a queued one — the drain worker may already be mid-flight on this
        path (e.g. the watcher re-fired it) when a retry is requested; the
        existing test only exercised the queued branch of the `in ("queued",
        "processing")` check, so a mutation dropping "processing" from that
        tuple would survive undetected."""
        state = _state(tmp_path / "vault")
        errored = self._item(state, status="error")
        in_flight = IngestItem(id="1-deadbeef", name="a.md", path=errored.path, status="processing")
        state.ingest_queue.append(in_flight)

        _, err = iq.retry_item(state, errored.id)
        assert err == "conflict"
        assert errored.status == "error"

    def test_retry_drops_stale_extraction_events(self, tmp_path: Path) -> None:
        """Review finding: _item_payload recomputes counts from retained
        extraction_result events — a retry keeping the old run's events
        double-counts the telemetry."""
        state = _state(tmp_path / "vault")
        item = self._item(state, status="done", provider_error="degraded")
        item.extracted_claims = 10
        for _ in range(3):
            item.events.append(
                {
                    "ts": 0.0,
                    "kind": "extraction_result",
                    "summary": "x",
                    "payload": {"claims": [1, 2]},
                }
            )
        got, err = iq.retry_item(state, item.id)
        assert err is None
        assert got.extracted_claims == 0
        assert not any(e["kind"] == "extraction_result" for e in got.events)
        payload = iq._item_payload(got, include_events=False)
        assert payload["extracted_claims"] == 0  # reads like a fresh run


class TestEnqueueForVaultWake:
    """Mutation-killer (review finding): _enqueue_for_vault must wake the
    drain worker whenever ANY item is queued — a dedup-refreshed enqueue
    returns [] but the original queued item still needs a live worker."""

    def test_refresh_only_enqueue_still_wakes_worker(self, tmp_path: Path, monkeypatch) -> None:
        from okto_neuron.server import _folder_watch as fw

        state = _state(tmp_path / "vault")
        src = tmp_path / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"
        iq.enqueue_paths(state, [src], sources)  # one queued item exists
        state.ingest_worker_active = False  # worker died

        calls: list = []
        monkeypatch.setattr(iq, "ensure_worker", lambda s, f: calls.append(True))

        _write(src, "v2")
        asyncio.run(fw._enqueue_for_vault(state, state.vault_path, [src]))
        assert calls, "worker not woken on a refresh-only enqueue"


# ── F11: stable durable-copy identity (sources mirror the watched tree) ──────


class TestDurableCopyIdentity:
    def test_watched_root_file_maps_to_stable_tree_path(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        root = tmp_path / "watched"
        src = root / "sub dir" / "note.md"
        _write(src, "v1")
        sources = state.vault_path / ".marginalia" / "sources"

        (item,) = iq.enqueue_paths(state, [src], sources, rel_root=root)
        rel = Path(item.path).relative_to(sources)
        # <root-sha16>/<sanitized components>. Clean components stay verbatim;
        # a LOSSY one ("sub dir") carries a hash of its original spelling so
        # distinct raw names can never collide (review finding).
        assert len(rel.parts) == 3
        assert len(rel.parts[0]) == 16
        assert rel.parts[1].startswith("sub-dir-") and len(rel.parts[1]) == len("sub-dir-") + 8
        assert rel.parts[2] == "note.md"

        # An edit re-enqueues to the SAME durable path (stable identity):
        # the queued item dedup-refreshes instead of forking a copy.
        _write(src, "v2")
        again = iq.enqueue_paths(state, [src], sources, rel_root=root)
        assert again == []
        assert len(state.ingest_queue) == 1
        assert Path(item.path).read_text(encoding="utf-8") == "v2"
        copies = [p for p in sources.rglob("*") if p.is_file()]
        assert len(copies) == 1

    def test_same_name_different_subfolders_do_not_clobber(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        root = tmp_path / "watched"
        a = root / "alpha" / "note.md"
        b = root / "beta" / "note.md"
        _write(a, "A")
        _write(b, "B")
        sources = state.vault_path / ".marginalia" / "sources"
        items = iq.enqueue_paths(state, [a, b], sources, rel_root=root)
        assert len({i.path for i in items}) == 2
        assert Path(items[0].path).read_text(encoding="utf-8") == "A"
        assert Path(items[1].path).read_text(encoding="utf-8") == "B"

    def test_no_rel_root_falls_back_to_legacy_flat_name(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        src = tmp_path / "elsewhere" / "note.md"
        _write(src, "x")
        sources = state.vault_path / ".marginalia" / "sources"
        (item,) = iq.enqueue_paths(state, [src], sources)
        rel = Path(item.path).relative_to(sources)
        assert len(rel.parts) == 1
        assert rel.parts[0].startswith("note-")

    def test_lossy_sanitization_cannot_collide(self, tmp_path: Path) -> None:
        """'notes 1.md' vs 'notes-1.md' must map to DISTINCT durable paths
        (review finding: the slug alone collided them, silently dropping one
        file's knowledge)."""
        sources = tmp_path / "vault" / ".marginalia" / "sources"
        root = tmp_path / "watched"
        a = root / "notes 1.md"
        b = root / "notes-1.md"
        _write(a, "A")
        _write(b, "B")
        pa = iq.durable_copy_path(sources, a, root)
        pb = iq.durable_copy_path(sources, b, root)
        assert pa != pb

    def test_hostile_components_cannot_escape_sources_dir(self, tmp_path: Path) -> None:
        sources = tmp_path / "vault" / ".marginalia" / "sources"
        root = tmp_path / "watched"
        # Components that sanitize toward traversal/empty must be neutralized.
        for raw in ("..", ".", "híd den", "a..b"):
            out = iq._sanitize_rel_component(raw)
            assert out not in ("", ".", "..")
            assert "/" not in out
        weird = root / "..b" / "no te.md"
        _write(weird, "x")
        target = iq.durable_copy_path(sources, weird, root)
        assert sources.resolve() in target.resolve().parents


# ── ADR 0039 T9: progress integrity ───────────────────────────────────────────


class TestProgressIntegrity:
    def test_blocks_done_over_total_is_flagged_not_clamped(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = IngestItem(id="1", name="a.md", path=str(tmp_path / "a.md"))
        state.ingest_queue.append(item)
        on_progress = iq._make_on_progress(state, item)

        on_progress("extracting", 12, 10)

        # Reported numbers survive verbatim — no cap, no discard.
        assert (item.blocks_done, item.blocks_total) == (12, 10)
        assert item.progress_integrity_error == {
            "code": "progress_done_exceeds_total",
            "population": "blocks",
            "done": 12,
            "total": 10,
            "overflow": 2,
        }

    def test_consistent_progress_clears_the_violation(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = IngestItem(id="1", name="a.md", path=str(tmp_path / "a.md"))
        state.ingest_queue.append(item)
        on_progress = iq._make_on_progress(state, item)

        on_progress("extracting", 12, 10)
        on_progress("extracting", 12, 14)

        assert item.progress_integrity_error == {}
        # ...but the violation still left durable evidence behind.
        assert [e["kind"] for e in item.events] == ["progress_integrity_error"]
        assert item.events[0]["payload"]["overflow"] == 2

    def test_unknown_denominator_is_not_a_violation(self, tmp_path: Path) -> None:
        """Dedup reports judged pairs against a not-yet-declared population."""
        assert iq._progress_integrity_error("dedup_progress", 7, None) is None
        # A declared, empty population IS a violation (matches the ledger rule).
        assert iq._progress_integrity_error("blocks", 7, 0) is not None

    def test_stage_progress_event_flags_overrun(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = IngestItem(id="1", name="a.md", path=str(tmp_path / "a.md"))
        state.ingest_queue.append(item)
        on_event = iq._make_on_event(state, item)

        on_event(
            {
                "kind": "curator_progress",
                "summary": "curating",
                "payload": {"reviewed": 9, "total": 4},
            }
        )

        assert (item.stage_progress_done, item.stage_progress_total) == (9, 4)
        assert item.progress_integrity_error["population"] == "curator_progress"

        # A new stage owns a new population; the old violation does not carry.
        iq._make_on_progress(state, item)("embedding", 0, 0)
        assert item.progress_integrity_error == {}

    def test_substage_ordinals_never_touch_the_blocks_population(self, tmp_path: Path) -> None:
        """The dedup/curation keep-alive ticks an item ORDINAL with an
        undeclared total. Folding it into blocks_done would both clobber the
        displayed block counts and report a T9 violation (ordinal 212 > 3
        blocks) on every MCP ingest."""
        state = _state(tmp_path / "vault")
        item = IngestItem(id="1", name="a.md", path=str(tmp_path / "a.md"))
        state.ingest_queue.append(item)
        on_progress = iq._make_on_progress(state, item)

        on_progress("extracting", 3, 3)
        on_progress("embedding", 3, 3)
        on_progress("dedup", 212, 0)
        on_progress("committing", 280, 0)

        assert item.stage == "committing"
        # Block counters keep the last real block pair.
        assert (item.blocks_done, item.blocks_total) == (3, 3)
        assert item.progress_integrity_error == {}

    def test_violation_survives_persist_and_rehydrate(self, tmp_path: Path) -> None:
        state = _state(tmp_path / "vault")
        item = IngestItem(id="1", name="a.md", path=str(tmp_path / "a.md"), status="done")
        state.ingest_queue.append(item)
        iq._make_on_progress(state, item)("extracting", 12, 10)
        iq.persist(state)

        restored = _state(tmp_path / "vault")
        iq.rehydrate_queue(restored)
        assert restored.ingest_queue[0].progress_integrity_error["overflow"] == 2
