"""``okto-neuron kg plans`` — operator recovery for a wedged vault (P0 slice 2).

A sealed-but-unreceipted commit plan in ``candidate-ledger.jsonl`` blocks every
later ``remember`` ("a different sealed semantic plan must be resumed") and,
before this fix, NO route, CLI or MCP tool could resume or abandon it. These
tests wedge a real grafx vault exactly the way production did — an apply that
fails mid-plan after some operation receipts exist (here: a ledger receipt
write that raises after N receipts, backend-independent) — and then drive the
whole recovery flow:

* the blocking ``IngestError`` names the plan's run id and the exact CLI;
* ``kg plans list`` shows the pending plan (source, receipts, sealed-at);
* ``kg plans resume <run>`` finishes the plan once the fault is gone, and new
  ingest work proceeds;
* ``kg plans abandon <run> --reason`` refuses (exit 2) while receipts exist,
  printing exactly which graph operations were already applied, and succeeds
  with ``--force-partial`` — leaving a ledger that still validates.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

pytest.importorskip("okto_grafx")

from okto_neuron import Vault  # noqa: E402
from okto_neuron.cli import app  # noqa: E402
from okto_neuron.companion import Companion  # noqa: E402
from okto_neuron.consolidate.ledger import CandidateLedger  # noqa: E402
from okto_neuron.core.schema import Provenance  # noqa: E402
from okto_neuron.errors import IngestError  # noqa: E402
from okto_neuron.extract import ExtractionResult  # noqa: E402
from okto_neuron.llm import StubLLM  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402

_DIM = 16
_CONTENT = "Pets owned by the narrator."


class _TitleExtractor:
    """One ``ENTITY <title>`` line -> one Concept candidate plus one literal
    claim (same shape as the sealed-plan regression tests), so ``remember``
    produces a multi-operation plan with real vector work."""

    _RE = re.compile(r"ENTITY\s+(.+)")

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        match = self._RE.search(text)
        if match is None:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate

        node = NodeCandidate(
            type="Concept",
            title=match.group(1).strip(),
            content=_CONTENT,
            provenance=provenance or Provenance(),
        )
        claim = EdgeCandidate(
            type="has_value",
            src_ref=node.candidate_id,
            dst_literal="kept as pets",
            provenance=provenance or Provenance(),
        )
        return ExtractionResult(node_candidates=[node], edge_candidates=[claim])


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()


def _init_grafx_vault(tmp_path: Path) -> Vault:
    root = tmp_path / "v"
    root.mkdir()
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": "v",
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": _DIM},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return Vault.open(root)


def _note(vault: Vault, name: str, title: str) -> Path:
    path = Path(vault.path) / name
    path.write_text(f"ENTITY {title}\n", encoding="utf-8")
    return path


def _companion(vault: Vault) -> Companion:
    return Companion(vault, provider=StubLLM(), extractor=_TitleExtractor())


@pytest.fixture
def wedge_after_one_receipt(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Make the next ``remember`` fail mid-apply after one durable receipt.

    The armed flag lets a test disarm the fault before resuming, the way the
    production fault (a driver failure) disappears before an operator retries.
    """
    original = CandidateLedger.record_operation_receipt
    calls = {"n": 0}
    armed = {"yes": True}

    def _flaky(self: CandidateLedger, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if armed["yes"] and calls["n"] > 1:
            raise RuntimeError("simulated crash after one durable receipt")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CandidateLedger, "record_operation_receipt", _flaky)
    return armed


def _pending_plan(vault: Vault):
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")
    plans = ledger.unreceipted_commit_plans()
    assert len(plans) == 1
    return ledger, plans[0]


def test_wedge_lists_resumes_and_unblocks(tmp_path: Path, wedge_after_one_receipt) -> None:
    vault = _init_grafx_vault(tmp_path)
    try:
        # Wedge: the first remember seals a plan, writes one receipt, then dies.
        with pytest.raises(RuntimeError, match="simulated crash"):
            _companion(vault).remember(_note(vault, "session-1.md", "Turtles"))
        ledger, plan = _pending_plan(vault)
        assert len(plan.operations) >= 2
        receipts = ledger.operation_receipts(plan)
        assert 1 <= len(receipts) < len(plan.operations)
        run_id = plan.run_id

        # The wedge: a different source is refused, naming the run id + CLI.
        with pytest.raises(IngestError) as blocked:
            _companion(vault).remember(_note(vault, "session-2.md", "Snakes"))
        message = str(blocked.value)
        assert f"run {run_id}" in message
        assert f"okto-neuron kg plans resume {run_id}" in message
        assert f"okto-neuron kg plans abandon {run_id}" in message
    finally:
        vault.close()

    runner = CliRunner()
    listed = runner.invoke(app, ["kg", "plans", "list", str(vault.path)])
    assert listed.exit_code == 0, listed.output
    assert run_id in listed.output
    assert "session-1.md" in listed.output
    assert "--force-partial" in listed.output

    # Fault gone: resume finishes the plan, and the vault accepts new work.
    wedge_after_one_receipt["yes"] = False
    resumed = runner.invoke(app, ["kg", "plans", "resume", run_id, str(vault.path)])
    assert resumed.exit_code == 0, resumed.output
    assert f"resumed run {run_id}" in resumed.output

    listed_again = runner.invoke(app, ["kg", "plans", "list", str(vault.path)])
    assert listed_again.exit_code == 0, listed_again.output
    assert "no pending sealed plans" in listed_again.output
    assert CandidateLedger(Path(vault.path) / ".marginalia").unreceipted_commit_plans() == ()

    vault = Vault.open(vault.path)
    try:
        result = _companion(vault).remember(_note(vault, "session-2.md", "Snakes"))
        assert result.committed + result.queued >= 1
        assert _companion(vault)._candidate_ledger().unreceipted_commit_plans() == ()
    finally:
        vault.close()


def test_abandon_refuses_with_receipts_then_force_partial_unblocks(
    tmp_path: Path, wedge_after_one_receipt
) -> None:
    vault = _init_grafx_vault(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            _companion(vault).remember(_note(vault, "session-1.md", "Turtles"))
        ledger, plan = _pending_plan(vault)
        receipts = ledger.operation_receipts(plan)
        assert receipts
        receipt_ids = sorted(receipts)
        run_id = plan.run_id
    finally:
        vault.close()

    runner = CliRunner()
    refused = runner.invoke(
        app,
        ["kg", "plans", "abandon", run_id, str(vault.path), "--reason", "operator test"],
    )
    assert refused.exit_code == 2, refused.output
    assert "refusing to abandon" in refused.output
    for receipt_id in receipt_ids:
        assert receipt_id in refused.output
    assert "already applied to the graph" in refused.output
    assert "--force-partial" in refused.output

    # Still wedged: the refusal wrote nothing.
    assert len(CandidateLedger(Path(vault.path) / ".marginalia").unreceipted_commit_plans()) == 1

    forced = runner.invoke(
        app,
        [
            "kg",
            "plans",
            "abandon",
            run_id,
            str(vault.path),
            "--reason",
            "operator test",
            "--force-partial",
        ],
    )
    assert forced.exit_code == 0, forced.output
    assert f"abandoned run {run_id}" in forced.output

    # The partially-applied plan is durably closed and the ledger still
    # validates (the force-partial abandon names its applied receipts).
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")
    assert ledger.unreceipted_commit_plans() == ()
    assert ledger.pending_plan_summaries() == ()

    wedge_after_one_receipt["yes"] = False
    vault = Vault.open(vault.path)
    try:
        result = _companion(vault).remember(_note(vault, "session-2.md", "Snakes"))
        assert result.committed + result.queued >= 1
    finally:
        vault.close()


def test_abandon_untouched_plan_needs_no_force(tmp_path: Path, monkeypatch) -> None:
    vault = _init_grafx_vault(tmp_path)
    try:
        # Fail before ANY receipt: the plan is sealed but untouched.
        original = CandidateLedger.record_operation_receipt

        def _first_receipt_fails(self: CandidateLedger, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated crash before any receipt")

        monkeypatch.setattr(CandidateLedger, "record_operation_receipt", _first_receipt_fails)
        with pytest.raises(RuntimeError, match="before any receipt"):
            _companion(vault).remember(_note(vault, "session-1.md", "Turtles"))
        monkeypatch.setattr(
            CandidateLedger, "record_operation_receipt", original
        )
        _ledger, plan = _pending_plan(vault)
        assert _ledger.operation_receipts(plan) == {}
        run_id = plan.run_id
    finally:
        vault.close()

    runner = CliRunner()
    abandoned = runner.invoke(
        app, ["kg", "plans", "abandon", run_id, str(vault.path), "--reason", "gone"]
    )
    assert abandoned.exit_code == 0, abandoned.output
    assert f"abandoned run {run_id}" in abandoned.output
    assert CandidateLedger(Path(vault.path) / ".marginalia").unreceipted_commit_plans() == ()
