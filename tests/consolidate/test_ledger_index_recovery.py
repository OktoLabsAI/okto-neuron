"""Apply-resume recovery end to end with the ledger index present, missing and corrupt.

A process dies after sealing a plan and before applying it. The restarted
companion must find the same plan, apply it once without any model call, and
leave the ledger and its index consistent, whatever state the sidecar is in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion, _source_binding
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger, _sidecar_cache_drop
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from tests.consolidate.test_ledger_index import assert_index_is_truth
from tests.support._ledger_synth import close_plan, dead_letter_operation


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _NeverCalled:
    model = "index-recovery-test"
    api_base = "http://127.0.0.1:8123/v1"

    def complete(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("a sealed plan must not call the LLM")

    def extract(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("a sealed plan must not rerun extraction")


@pytest.mark.parametrize("index_state", ["present", "missing", "corrupt"])
def test_sealed_plan_recovers_with_any_index_state(tmp_path: Path, index_state: str) -> None:
    _sidecar_cache_drop()
    vault = Vault.init(tmp_path / "vault")
    source = Path(vault.path) / "note.md"
    source.write_text("# Note\n\nA durable semantic fact.\n", encoding="utf-8")
    document = vault.add(source)
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")

    # History: a finished document whose plan is closed.
    old_run = ledger.start_run(document_id="old-doc", source="old.md", blocks_total=1, model="m")
    close_plan(
        ledger,
        old_run,
        ledger.record_commit_plan(
            old_run, operations=[dead_letter_operation("old-1")], context={"document_id": "old-doc"}
        ),
    )

    candidate = NodeCandidate(
        type="Concept", title="Durable plan", content="Sealed before semantic graph application."
    )
    plan_id = ledger.record_commit_plan(
        "crashed-after-seal",
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": candidate.candidate_id,
                "candidate": candidate.model_dump(mode="json"),
                "confidence": 0.98,
                "correlations": [],
                "reason": None,
            }
        ],
        context={
            "document_id": document.id,
            "source": str(source),
            "source_binding": _source_binding(vault.store, source, document.id),
            "blocks_total": 1,
            "nodes_extracted": 1,
            "edges_extracted": 0,
            "semantic_policy_fingerprint": "sha256:sealed-policy",
        },
    )
    before = [p.plan_id for p in ledger.unreceipted_commit_plans()]
    assert before == [plan_id]

    ledger.write_index_checkpoint()
    if index_state == "missing":
        ledger.index_path.unlink()
    elif index_state == "corrupt":
        ledger.index_path.write_bytes(b'{"index_version":1}\n{"truncat')
    _sidecar_cache_drop()  # the process restarted

    never = _NeverCalled()
    companion = Companion(vault, provider=never, extractor=never)  # type: ignore[arg-type]
    try:
        result = companion.remember(source)
        assert result.document_id == document.id
        assert result.committed == 1
        assert vault.store.get_node(candidate.candidate_id) is not None

        recovered = CandidateLedger(Path(vault.path) / ".marginalia")
        assert recovered.unreceipted_commit_plans() == ()
        receipts = [
            r
            for r in recovered.iter_records()
            if r.get("kind") == "operation_receipt" and r.get("plan_id") == plan_id
        ]
        assert [r["status"] for r in receipts] == ["applied"]
        live = assert_index_is_truth(recovered)
        assert live["closed"][plan_id] == "c" and not live["open_plans"]
        assert len(live["closed"]) == 2
    finally:
        vault.close()
