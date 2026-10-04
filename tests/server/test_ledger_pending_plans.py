"""REST surface of the ingest wedge (P0 slice 2): pending sealed plans.

A vault wedged by a sealed-but-unreceipted commit plan exposes it read-only:

* ``GET /api/v1/ledger/pending-plans`` lists each plan (run id, source,
  receipts/operations, sealed-at) so an operator can pick resume vs abandon;
* ``GET /api/v1/status`` carries ``pending_sealed_plans`` (top level and per
  vault row) plus a degraded reason, so the UI can show a banner later.

The vault is wedged for real: a grafx vault ingests through ``Companion`` with
the ledger's receipt write failing before any durable receipt (the mid-apply
crash shape), out-of-band from the server's own handles.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

pytest.importorskip("okto_grafx")

from okto_neuron import Vault  # noqa: E402
from okto_neuron.companion import Companion  # noqa: E402
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate  # noqa: E402
from okto_neuron.consolidate.ledger import CandidateLedger  # noqa: E402
from okto_neuron.core.schema import Provenance  # noqa: E402
from okto_neuron.extract import ExtractionResult  # noqa: E402
from okto_neuron.llm import StubLLM  # noqa: E402
from okto_neuron.server.http import build_rest_app  # noqa: E402
from okto_neuron.server.state import init_state, reset_state_for_tests  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402

_DIM = 16
_VAULT = "wedged"


class _TitleExtractor:
    """One ``ENTITY <title>`` line -> one Concept candidate plus one literal
    claim, so ``remember`` produces a real multi-operation plan."""

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        import re

        match = re.search(r"ENTITY\s+(.+)", text)
        if match is None:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        node = NodeCandidate(
            type="Concept",
            title=match.group(1).strip(),
            content="Pets owned by the narrator.",
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
    reset_state_for_tests()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OKTO_NEURON_HOME", "OKTO_NEURON_CONFIG", "OKTO_NEURON_VAULT", "MARGINALIA_HOME"):
        monkeypatch.delenv(name, raising=False)
    reset_state_for_tests()
    state = init_state(None, None)
    with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _vault_root() -> Path:
    return Path.home() / ".okto-neuron" / "vaults" / _VAULT


def _wedge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scaffold a discoverable grafx vault, then crash its first ingest mid-apply."""
    root = _vault_root()
    root.mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": _VAULT,
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": _DIM},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    vault = Vault.open(root)
    try:
        note = root / "session-1.md"
        note.write_text("ENTITY Turtles\n", encoding="utf-8")
        original = CandidateLedger.record_operation_receipt

        def _failing(self: CandidateLedger, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated crash before any receipt")

        monkeypatch.setattr(CandidateLedger, "record_operation_receipt", _failing)
        try:
            with pytest.raises(RuntimeError):
                Companion(
                    vault, provider=StubLLM(), extractor=_TitleExtractor()
                ).remember(note)
        finally:
            monkeypatch.setattr(CandidateLedger, "record_operation_receipt", original)
        assert len(CandidateLedger(root / ".marginalia").unreceipted_commit_plans()) == 1
    finally:
        vault.close()


def test_pending_plans_route_lists_the_wedge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wedge(monkeypatch)
    run_id = CandidateLedger(_vault_root() / ".marginalia").unreceipted_commit_plans()[0].run_id

    listed = client.get("/api/v1/ledger/pending-plans", headers={"X-Okto-Neuron-Vault": _VAULT})
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["count"] == 1
    (plan,) = payload["plans"]
    assert plan["run_id"] == run_id
    assert plan["source"].endswith("session-1.md")
    assert plan["receipts"] == 0
    assert plan["operations"] >= 1
    assert plan["sealed_at"]

    status = client.get("/api/v1/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["pending_sealed_plans"] == 1
    vault_rows = [row for row in body["vaults"] if row["path"] == str(_vault_root())]
    assert vault_rows and vault_rows[0]["pending_sealed_plans"] == 1
    assert body["status"] == "degraded"
    assert any(
        "pending_sealed_plans" in reason for reason in body["degraded_reasons"]
    )


def test_pending_plans_route_reports_zero_on_a_discovered_healthy_vault(client: TestClient) -> None:
    root = _vault_root()
    root.mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": _VAULT,
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": _DIM},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    listed = client.get("/api/v1/ledger/pending-plans", headers={"X-Okto-Neuron-Vault": _VAULT})
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"plans": [], "count": 0}

    status = client.get("/api/v1/status").json()
    assert status["pending_sealed_plans"] == 0
