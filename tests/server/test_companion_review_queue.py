from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from starlette.testclient import TestClient

from okto_neuron.consolidate._candidates import NodeCandidate
from okto_neuron.consolidate.review_queue import ReviewQueue
from okto_neuron.config import VaultConfig
from okto_neuron.core.schema import Node
from okto_neuron.llm import Message
from okto_neuron.server import _curation
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.vault import Vault


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def _vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    root.mkdir()
    return Vault(root, InMemoryStore(), embedder=_Embedder())


def test_review_queue_batch_mixed_found_and_missing_ids(tmp_path: Path) -> None:
    reset_state_for_tests()
    vault = _vault(tmp_path)
    state = init_state(vault, vault.path)
    queue = ReviewQueue(Path(vault.path) / ".marginalia", vault.store)
    first = NodeCandidate(type="Concept", title="Alpha", content="Alpha content")
    second = NodeCandidate(type="Concept", title="Beta", content="Beta content")
    queue.enqueue(first, "low_confidence")
    queue.enqueue(second, "low_confidence")

    app = build_rest_app(state)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.post(
                "/review-queue/batch",
                json={
                    "candidate_ids": [
                        first.candidate_id,
                        "missing-candidate",
                        second.candidate_id,
                    ],
                    "action": "discard",
                },
            )
    finally:
        reset_state_for_tests()

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "resolved": 2,
        "skipped": 1,
        "errors": [],
    }
    assert ReviewQueue(Path(vault.path) / ".marginalia", vault.store).list() == []
    assert vault.store.get_node(first.candidate_id) is None
    assert vault.store.get_node(second.candidate_id) is None


_BATCH_ID_RE = re.compile(r"=== candidate_id: (\S+) ===")
_SINGLE_ID_RE = re.compile(r"id: ([0-9a-f]{64})")


class _TriageProvider:
    model = "fake-triage"

    def __init__(self, verdicts: dict[str, tuple[str, float]]) -> None:
        self.verdicts = verdicts
        self.calls: list[list[str]] = []

    def complete(self, messages: Sequence[Message], **kwargs: object) -> str:
        schema = kwargs.get("response_format")
        schema_name = None
        if isinstance(schema, dict):
            raw_schema = schema.get("json_schema")
            if isinstance(raw_schema, dict):
                schema_name = raw_schema.get("name")
        user = messages[-1].content
        if schema_name == "marginalia_companion_triage_batch":
            ids = _BATCH_ID_RE.findall(user)
            self.calls.append(ids)
            return json.dumps(
                {
                    "verdicts": [
                        {
                            "candidate_id": candidate_id,
                            "action": self.verdicts[candidate_id][0],
                            "confidence": self.verdicts[candidate_id][1],
                            "reason": f"{self.verdicts[candidate_id][0]} {candidate_id}",
                        }
                        for candidate_id in ids
                    ]
                }
            )
        match = _SINGLE_ID_RE.search(user)
        candidate_id = match.group(1) if match else ""
        self.calls.append([candidate_id])
        action, confidence = self.verdicts[candidate_id]
        return json.dumps(
            {"action": action, "confidence": confidence, "reason": f"{action} {candidate_id}"}
        )


class _Job:
    params: dict = {}

    def __init__(self) -> None:
        self.progress_updates: list[str] = []

    def progress(self, stage: str) -> None:
        self.progress_updates.append(stage)


def test_companion_triage_runner_maps_commit_discard_keep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = _vault(tmp_path)
    store = vault.store
    block = Node(id="block:1", type="Block", title="Block", content="Source text")
    store.add_node(block)
    commit = NodeCandidate(
        type="Concept",
        title="Commit Me",
        content="Commit me.",
        facets={"block_id": "block:1"},
        embedding=(1.0, 0.0, 0.0),
    )
    discard = NodeCandidate(
        type="Concept",
        title="Discard Me",
        content="Discard me.",
        facets={"block_id": "block:1"},
        embedding=(0.0, 1.0, 0.0),
    )
    keep = NodeCandidate(
        type="Concept",
        title="Keep Me",
        content="Keep me.",
        facets={"block_id": "block:1"},
        embedding=(0.0, 0.0, 1.0),
    )
    queue = ReviewQueue(Path(vault.path) / ".marginalia", store)
    for candidate in (commit, discard, keep):
        queue.enqueue(candidate, "low_confidence")

    provider = _TriageProvider(
        {
            commit.candidate_id: ("commit", 0.8),
            discard.candidate_id: ("discard", 0.9),
            keep.candidate_id: ("keep", 0.95),
        }
    )
    cfg = VaultConfig()
    cfg.consolidation.curation_batch_size = 3
    monkeypatch.setattr(_curation, "_load_config", lambda _state: cfg)
    monkeypatch.setattr(
        _curation,
        "_build_companion_triage_provider",
        lambda _state, _resolved: provider,
    )

    state = SimpleNamespace(vault=vault, vault_path=vault.path)
    job = _Job()
    result = _curation.run_companion_triage(state, job)

    assert result == {
        "triaged": 3,
        "committed": 1,
        "discarded": 1,
        "kept": 1,
        "errors": [],
    }
    assert provider.calls == [[commit.candidate_id, discard.candidate_id, keep.candidate_id]]
    assert job.progress_updates[0] == "triaging 0/3"
    assert job.progress_updates[-1] == "triaging 3/3"
    remaining = ReviewQueue(Path(vault.path) / ".marginalia", store).list()
    assert [item.candidate_id for item in remaining] == [keep.candidate_id]
    assert store.get_node(commit.candidate_id) is not None
    assert store.get_node(discard.candidate_id) is None
