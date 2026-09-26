"""Task #13 — ingest receipts must verify against the live graph, not the
sidecar.

``ingest-history.json`` is written by the drain worker as it goes: it
reflects what ``remember()`` reported committing *at that time*, not what
currently lives in the graph. The exact incident this fixes: the sidecar
claimed 17/17 ``done`` + ``receipts_complete`` + integrity ``verified``
with per-item node counts, while an unclean shutdown before the next
checkpoint (task #12) had left the recovered graph completely empty — pure
sidecar state that cannot, by construction, detect an empty graph.

``verify_receipt`` re-checks the LIVE graph (Document node + >=1 Block for
the source) at receipt-check time (``item_detail`` / ``retry_item``) and
durably corrects a stale claim so it is never surfaced as verified again,
and so the item becomes retryable through the normal API with no further
plumbing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.store.memory import InMemoryStore


def _state(root: Path, store: object) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        vault=SimpleNamespace(store=store),
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_cancel_requested=False,
        draining=False,
        last_ingest_at=None,
        last_ingest_at_by_vault={},
    )


def _stale_done_item(tmp_path: Path, name: str) -> IngestItem:
    """An item shaped exactly like the incident: sidecar says done, verified,
    receipts complete, with real-looking per-item counts — but nothing was
    actually written to (or survived in) the graph for it."""
    path = str((tmp_path / name).resolve())
    return IngestItem(
        id="1",
        name=name,
        path=path,
        status="done",
        stage="done",
        committed=3,
        nodes=5,
        edges=2,
        claims=4,
        outcome={
            "quality": "complete",
            "receipts_complete": True,
            "units": {"committed": 3},
        },
    )


def test_item_detail_reports_divergence_when_graph_is_missing(tmp_path: Path) -> None:
    item = _stale_done_item(tmp_path, "ghost.md")
    store = InMemoryStore()  # graph never actually received this document
    state = _state(tmp_path, store)
    state.ingest_queue = [item]

    detail = iq.item_detail(state, "1")

    outcome = detail["item"]["outcome"]
    assert outcome["quality"] == "graph_missing"
    assert outcome["receipts_complete"] is False
    assert outcome["graph_verification"]["status"] == "graph_missing"
    assert outcome["graph_verification"]["document_present"] is False


def test_stale_done_item_becomes_retryable_through_the_normal_api(
    tmp_path: Path,
) -> None:
    """The exact tonight-shape scenario: sidecar says done+verified, graph
    empty -> API reports the divergence AND the item is retryable — without
    the caller needing to call item_detail first (retry_item verifies too)."""
    item = _stale_done_item(tmp_path, "ghost.md")
    store = InMemoryStore()
    state = _state(tmp_path, store)
    state.ingest_queue = [item]

    retried, error = iq.retry_item(state, "1")

    assert error is None
    assert retried is item
    assert item.status == "queued"
    assert item.outcome == {}


def test_verify_receipt_leaves_a_genuinely_complete_item_alone(tmp_path: Path) -> None:
    """The common case must not regress: a done item whose Document + Block
    really are in the graph stays reported as complete, and a retry attempt
    on it still conflicts (nothing to retry)."""
    from okto_neuron.ingest import ingest_document

    path = tmp_path / "real.md"
    path.write_text("# Real\n\nActually ingested content here.\n", encoding="utf-8")
    store = InMemoryStore()
    ingest_document(store, path, vault_root=tmp_path)

    item = IngestItem(
        id="1",
        name="real.md",
        path=str(path.resolve()),
        status="done",
        outcome={"quality": "complete", "receipts_complete": True},
    )
    state = _state(tmp_path, store)
    state.ingest_queue = [item]

    detail = iq.item_detail(state, "1")
    outcome = detail["item"]["outcome"]
    assert outcome["quality"] == "complete"
    assert outcome["receipts_complete"] is True
    assert outcome["graph_verification"]["status"] == "verified"

    _, error = iq.retry_item(state, "1")
    assert error == "conflict"


def test_verify_receipt_leaves_a_bare_done_item_with_no_quality_claim_alone(
    tmp_path: Path,
) -> None:
    """A ``done`` item whose outcome never asserted a quality/receipts claim
    (e.g. a crash-recovery placeholder, or any caller that never populated
    ``outcome``) has nothing to cross-check: it must stay exactly as it was,
    and a retry attempt on it must still conflict — this is the existing
    "no evidence either way" contract (mirrors
    ``_item_retryable``'s own quality-empty branch) and must not regress just
    because the graph happens not to contain this item's source."""
    item = IngestItem(
        id="1",
        name="bare.md",
        path=str((tmp_path / "bare.md").resolve()),
        status="done",
        outcome={},
    )
    store = InMemoryStore()  # nothing ingested — deliberately graph-empty
    state = _state(tmp_path, store)
    state.ingest_queue = [item]

    result = iq.verify_receipt(state, item)
    assert result == {}
    assert item.outcome == {}

    _, error = iq.retry_item(state, "1")
    assert error == "conflict"
    assert item.status == "done"


def test_verify_receipt_recognizes_blocks_from_a_real_remember_run(
    tmp_path: Path,
) -> None:
    """End-to-end proof, not just code reading: the incident's items reached
    the graph through ``Companion.remember()`` (the LLM path), not the
    deterministic ``ingest_document`` helper this module calls directly for
    ``get_node``/``list_nodes``. Prove the Block facet shape
    ``_graph_receipt_state`` depends on (``facets["source_path"]``) actually
    matches what ``remember()`` produces, end to end, with a real Vault and
    a real (stubbed) Companion -- not an assumption from reading
    ``vault.add()``'s call chain."""
    import okto_neuron.store.vault as vault_module
    from okto_neuron import Vault
    from okto_neuron.companion import Companion
    from okto_neuron.llm import StubLLM
    from okto_neuron.store.ladybug import VaultConnection

    vault = Vault.init(tmp_path / "vault", embedding_provider="stub")
    try:
        path = Path(vault.path) / "note.md"
        path.write_text("# Title\n\nbody about knowledge graphs.\n", encoding="utf-8")
        companion = Companion(vault, provider=StubLLM())
        companion.remember(str(path))

        item = IngestItem(
            id="1",
            name="note.md",
            path=str(path.resolve()),
            status="done",
            outcome={"quality": "complete", "receipts_complete": True},
        )
        state = _state(tmp_path, vault.store)
        state.ingest_queue = [item]

        outcome = iq.verify_receipt(state, item)

        assert outcome["graph_verification"]["status"] == "verified"
        assert outcome["graph_verification"]["document_present"] is True
        assert outcome["graph_verification"]["block_present"] is True
        assert outcome["receipts_complete"] is True
    finally:
        vault.close()
        for store in list(vault_module._STORE_CACHE.values()):
            store.close()
        vault_module._STORE_CACHE.clear()
        VaultConnection.close_all()


def test_verify_receipt_is_a_noop_for_non_terminal_items(tmp_path: Path) -> None:
    """Only a 'done' item's claim can be stale; queued/processing items have
    no receipt to re-check yet."""
    item = IngestItem(id="1", name="x.md", path=str(tmp_path / "x.md"), status="queued")
    store = InMemoryStore()
    state = _state(tmp_path, store)
    state.ingest_queue = [item]

    result = iq.verify_receipt(state, item)

    assert result == {}
    assert item.outcome == {}
