"""The remedy for the embedding-dimension wedge, proven end to end (P0).

The guard from the ingest-wedge fix makes ``EmbeddingDimMismatch`` fire loud
on grafx — this file proves the OPERATOR RUNBOOK still works against that
guard, on real grafx vaults with the model-free stub embedder:

1. CLI:  vault at width A, config hot-edited to width B -> remember/query
   raise ``EmbeddingDimMismatch`` -> the real ``okto-neuron kg reembed``
   succeeds -> the graph is width B and query works.
2. REST: the same wedge healed through a throwaway daemon's
   ``POST /api/v1/vaults/reembed`` + status poll -> query works.
3. Incident: a vault wedged exactly like production (a sealed, half-applied
   plan whose pinned vectors are width B while the graph is width A) ->
   ``kg reembed`` -> ``kg plans resume <run>`` completes the plan ->
   no pending plans remain.

The reembed pipeline resolves its embedder through ``get_provider`` and
opens the graph via ``open_for_live_read`` (deliberate guard bypass), and
apply-resume never resolves an embedder at all, so neither remedy path is
blocked by the new guard — these tests pin that.
"""

from __future__ import annotations

import json
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
from okto_neuron.config import VaultConfig  # noqa: E402
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate  # noqa: E402
from okto_neuron.core.schema import Provenance  # noqa: E402
from okto_neuron.errors import EmbeddingDimMismatch  # noqa: E402
from okto_neuron.extract import ExtractionResult  # noqa: E402
from okto_neuron.llm import StubLLM  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402

_DIM_A = 16
_DIM_B = 32


class _TitleExtractor:
    """One ``ENTITY <title>`` line -> one Concept + one literal claim."""

    _RE = re.compile(r"ENTITY\s+(.+)")

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        match = self._RE.search(text)
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


def _init_vault(tmp_path: Path, name: str, dim: int) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": name,
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": dim},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def _hot_edit_dim(vault: Vault, dim: int) -> None:
    """The config API's own hot-edit path: persist + drop the resolved-embedder cache."""
    _, changed = VaultConfig.apply_patch(Path(vault.path), {"embedding": {"dimension": dim}})
    assert "embedding.dimension" in changed
    vault.invalidate_runtime_caches()


def _note(root: Path, name: str, title: str) -> Path:
    path = root / name
    path.write_text(f"ENTITY {title}\n", encoding="utf-8")
    return path


def _companion(vault: Vault) -> Companion:
    return Companion(vault, provider=StubLLM(), extractor=_TitleExtractor())


def _ingest(vault: Vault, name: str, title: str) -> None:
    result = _companion(vault).remember(_note(Path(vault.path), name, title))
    assert result.committed + result.queued >= 1


def test_cli_reembed_is_the_remedy_after_a_hot_dim_edit(tmp_path: Path) -> None:
    root = _init_vault(tmp_path, "cli", _DIM_A)
    vault = Vault.open(root)
    try:
        _ingest(vault, "session-1.md", "Turtles")
        assert vault.store.embedding_dim == _DIM_A
        _hot_edit_dim(vault, _DIM_B)
        with pytest.raises(EmbeddingDimMismatch):
            _companion(vault).remember(_note(root, "session-2.md", "Snakes"))
        with pytest.raises(EmbeddingDimMismatch):
            vault.query("turtles")
    finally:
        vault.close()

    # The operator runbook, exactly: kg reembed with the daemon stopped.
    runner = CliRunner()
    reembedded = runner.invoke(app, ["kg", "reembed", str(root)])
    assert reembedded.exit_code == 0, reembedded.output

    vault = Vault.open(root)
    try:
        assert vault.store.embedding_dim == _DIM_B
        hits = vault.query("turtles")
        assert hits, "query must work at the new width after reembed"
        result = _companion(vault).remember(_note(root, "session-2.md", "Snakes"))
        assert result.committed + result.queued >= 1
    finally:
        vault.close()


def test_daemon_reembed_route_is_the_remedy(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from okto_neuron.server.http import build_rest_app
    from okto_neuron.server.state import init_state, reset_state_for_tests

    home = tmp_path / "home"
    root = home / ".okto-neuron" / "vaults" / "wedged"
    root.mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": "wedged",
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": _DIM_A},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    vault = Vault.open(root)
    try:
        _ingest(vault, "session-1.md", "Turtles")
    finally:
        vault.close()
    # The daemon refuses a graph swap while any process (including THIS test
    # process) holds a live graph handle; release ours the way kg reindex does.
    from okto_neuron.cli.kg import _close_live_graph_handles

    _close_live_graph_handles(root)
    vault = Vault.open(root)
    try:
        _hot_edit_dim(vault, _DIM_B)
    finally:
        vault.close()
    _close_live_graph_handles(root)

    reset_state_for_tests()
    monkeyhome = pytest.MonkeyPatch()
    monkeyhome.setenv("HOME", str(home))
    for name in ("OKTO_NEURON_HOME", "OKTO_NEURON_CONFIG", "OKTO_NEURON_VAULT", "MARGINALIA_HOME"):
        monkeyhome.delenv(name, raising=False)
    try:
        state = init_state(None, None)
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
            started = client.post("/api/v1/vaults/reembed", json={"vault": str(root)})
            assert started.status_code in {200, 202}, started.text
            import time as _time

            for _ in range(240):
                status = client.get("/api/v1/vaults/reembed/status", params={"vault": str(root)})
                assert status.status_code == 200, status.text
                payload = status.json()
                phase = payload.get("phase")
                if phase == "complete":
                    break
                assert phase != "failed", json.dumps(payload)
                _time.sleep(0.05)
            else:
                pytest.fail(f"daemon reembed never finished: {json.dumps(payload)}")

        vault = Vault.open(root)
        try:
            assert vault.store.embedding_dim == _DIM_B
            assert vault.query("turtles"), "query must work after the daemon-side reembed"
        finally:
            vault.close()
    finally:
        monkeyhome.undo()
        reset_state_for_tests()


def test_incident_reembed_then_plans_resume_unwedges(tmp_path: Path, monkeypatch) -> None:
    root = _init_vault(tmp_path, "incident", _DIM_A)
    vault = Vault.open(root)
    try:
        _ingest(vault, "session-1.md", "Turtles")
        _hot_edit_dim(vault, _DIM_B)

        # Recreate the production incident: with the PRE-FIX code (guard a
        # no-op on grafx) the hot edit sailed through, sealed a plan whose
        # pinned vectors were width B, and apply died on the A-width column.
        # Simulate the pre-fix guard so the wedge can still be manufactured.
        monkeypatch.setattr(
            Vault, "_ensure_embedding_compatible", lambda self, embedder: None
        )
        with pytest.raises(Exception) as wedge:
            _companion(vault).remember(_note(root, "session-2.md", "Snakes"))
        assert "vector" in str(wedge.value).lower() or "dimension" in str(wedge.value).lower()
        monkeypatch.undo()

        from okto_neuron.consolidate.ledger import CandidateLedger

        ledger = CandidateLedger(root / ".marginalia")
        plans = ledger.unreceipted_commit_plans()
        assert len(plans) == 1
        run_id = plans[0].run_id
        assert ledger.operation_receipts(plans[0]), "the wedge must be half-applied"
    finally:
        vault.close()

    # Operator runbook, exactly: reembed to the configured width B ...
    runner = CliRunner()
    reembedded = runner.invoke(app, ["kg", "reembed", str(root)])
    assert reembedded.exit_code == 0, reembedded.output

    # ... then resume the sealed plan: pinned width-B vectors now fit.
    resumed = runner.invoke(app, ["kg", "plans", "resume", run_id, str(root)])
    assert resumed.exit_code == 0, resumed.output
    assert f"resumed run {run_id}" in resumed.output

    from okto_neuron.consolidate.ledger import CandidateLedger

    assert CandidateLedger(root / ".marginalia").unreceipted_commit_plans() == ()

    vault = Vault.open(root)
    try:
        assert vault.store.embedding_dim == _DIM_B
        result = _companion(vault).remember(_note(root, "session-3.md", "Frogs"))
        assert result.committed + result.queued >= 1
        assert _companion(vault)._candidate_ledger().unreceipted_commit_plans() == ()
    finally:
        vault.close()
