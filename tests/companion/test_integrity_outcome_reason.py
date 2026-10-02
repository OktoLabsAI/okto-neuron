"""The remember outcome and the server status explain a backend without a write fence the same way."""

from __future__ import annotations

from types import SimpleNamespace

from okto_neuron.companion import _current_integrity_outcome
from okto_neuron.server._integrity import NO_FENCE_UNAUDITED_REASON


def test_no_fence_outcome_keeps_its_status_and_explains_itself_like_the_server_status(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "okto_neuron.vault_registry.resolve_vault_backend", lambda _path: "grafx"
    )
    vault = SimpleNamespace(store=SimpleNamespace(), path=tmp_path)

    outcome = _current_integrity_outcome(vault)

    assert outcome["status"] == "not_applicable"
    assert outcome["audit_id"] is None and outcome["graph_generation"] is None
    assert outcome["reason"] == (
        "the grafx backend does not fence writes; an audit is optional "
        "(POST /api/v1/graph/integrity runs one)"
    )
    # one explanation on both surfaces: the same two facts and the same route as the server status text
    server_text = NO_FENCE_UNAUDITED_REASON.format(backend="grafx")
    for fragment in ("the grafx backend does not fence writes", "an audit is optional", "POST /api/v1/graph/integrity"):
        assert fragment in outcome["reason"]
        assert fragment in server_text
