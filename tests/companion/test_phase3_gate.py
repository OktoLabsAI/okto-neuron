"""Model-free unit tests for ADR 0011 Phase 3 — the Tier-1→Tier-2
coverage/abstain gate, the abstention-pattern detector, and the mandatory
vault-root path re-validation on Tier-2 byte reads.

No LLM and no embedder are loaded: the pure helpers are tested directly, and the
two-pass flow is driven by a fake provider that returns queued canned answers,
so this whole module is CI-safe and offline.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import (
    Answer,
    AskRetrievalPolicy,
    Companion,
    _is_abstention,
    _is_within_root,
    _read_slice,
    _subgraph_context_thin,
)
from okto_neuron.llm import LLMProviderError, Message, StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# ── _is_abstention — the post-hoc insufficiency signal ────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "That is not in the notes.",
        "The notes do not contain that information.",
        "I cannot find any mention of this topic.",
        "No information is available about that.",
        "That detail is not mentioned anywhere.",
        "The value is not specified in the provided notes.",
        "There is no explicit statement about the budget.",
    ],
)
def test_is_abstention_true(text: str) -> None:
    assert _is_abstention(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Luke Gray participated in the reference evaluation kickoff workshop.",
        "The project partner is Jordan Lee Carter.",
        "Alice founded Acme in 2019.",
    ],
)
def test_is_abstention_false(text: str) -> None:
    assert _is_abstention(text) is False


# ── _is_within_root / _read_slice — the security gate (load-bearing) ──────────
def test_is_within_root_dormant_when_no_root() -> None:
    # vault_root=None → guard is OFF (the default block-dump path is unchanged).
    assert _is_within_root("/etc/passwd", None) is True


def test_is_within_root_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    # A ../-escape path resolves outside the vault → rejected.
    traversal = str(root / ".." / "secret.txt")
    assert _is_within_root(traversal, root) is False
    assert _is_within_root("/etc/passwd", root) is False
    assert _is_within_root("", root) is False


def test_is_within_root_accepts_in_vault(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    inside = root / "note.md"
    inside.write_text("body", encoding="utf-8")
    assert _is_within_root(str(inside), root) is True


def test_read_slice_rejects_out_of_vault_path(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    secret = tmp_path / "passwd"
    secret.write_bytes(b"root:x:0:0:secret")
    # With vault_root set (Tier-2), an out-of-vault read returns "" — never bytes.
    assert _read_slice(str(secret), 0, 17, vault_root=root) == ""
    # The classic traversal form is rejected too.
    traversal = str(root / ".." / "passwd")
    assert _read_slice(traversal, 0, 17, vault_root=root) == ""


def test_read_slice_reads_in_vault_path(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "note.md"
    note.write_bytes(b"hello world")
    assert _read_slice(str(note), 0, 5, vault_root=root) == "hello"


def test_read_slice_without_root_is_unchanged(tmp_path: Path) -> None:
    # OFF path (vault_root=None): byte-identical to the legacy reader — reads any
    # readable path, no validation. This pins the default-path immutability.
    secret = tmp_path / "anywhere.txt"
    secret.write_bytes(b"plain bytes")
    assert _read_slice(str(secret), 0, 5) == "plain"


# ── _subgraph_context_thin — the cheap coverage pre-gate ──────────────────────
def test_thin_on_empty_context() -> None:
    assert _subgraph_context_thin("", 0.4) is True
    assert _subgraph_context_thin("   ", 0.4) is True


def test_thin_on_header_only_context() -> None:
    ctx = "=== NODES ===\nN1 [Agent] Alice\n=== RELATIONSHIPS ===\n=== CLAIMS ===\n"
    assert _subgraph_context_thin(ctx, 0.4) is True


def test_not_thin_with_substantive_rows() -> None:
    ctx = (
        "=== NODES ===\nN1 [Agent] Alice\n"
        "=== RELATIONSHIPS ===\nN1 -[founded]-> N2  (claim:c1 conf=0.9)\n"
        "=== CLAIMS ===\n- Alice founded Acme. [c1]\n"
    )
    assert _subgraph_context_thin(ctx, 0.4) is False


def test_threshold_zero_disables_pregate() -> None:
    # coverage_threshold=0 → pre-gate off; only the post-hoc abstention check runs.
    assert _subgraph_context_thin("=== NODES ===\nN1 [Agent] Alice\n", 0.0) is False


# ── two-pass integration — fake provider, real vault, no real LLM ─────────────
class _QueuedProvider:
    """A fake LLM provider that returns canned answers in order, recording how
    many ``complete`` calls it received. ``raise_after`` makes the Nth call raise
    ``LLMProviderError`` (to exercise the provider-down ≠ abstention path)."""

    model = "fake"
    api_base = "http://127.0.0.1:0/v1"

    def __init__(self, answers: list[str], *, raise_at: set[int] | None = None) -> None:
        self._answers = list(answers)
        self._raise_at = raise_at or set()
        self.calls = 0
        self.user_prompts: list[str] = []

    def complete(self, messages: Sequence[Message], **_: object) -> str:
        self.calls += 1
        self.user_prompts.append(messages[-1].content if messages else "")
        if self.calls in self._raise_at:
            raise LLMProviderError("simulated provider down")
        if not self._answers:
            return ""
        return self._answers.pop(0)


def _seeded_vault(tmp_path: Path) -> Vault:
    """A vault with one ingested note so ``ask`` retrieves real hits and the
    subgraph render has nodes to walk."""
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    # Wikilink + tag so the DETERMINISTIC ingest path mints Claims (StubLLM yields
    # no LLM candidates) and retrieval returns real hits → the gate actually fires.
    note.write_text(
        "# Alpha\n\nAlice founded [[Acme]] in 2019. The budget was 5 million. #funding\n",
        encoding="utf-8",
    )
    Companion(vault, provider=StubLLM()).remember(note)
    return vault


def _enable_subgraph(vault: Vault) -> None:
    (Path(vault.path) / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nllm:\n  ask:\n    enable_subgraph: true\n",
        encoding="utf-8",
    )


def test_tier1_answer_kept_when_not_abstaining(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert isinstance(answer, Answer)
        assert answer.hits  # retrieval is non-vacuous (else the gate never runs)
        # A confident Tier-1 answer → exactly one completion, no Tier-2 retry.
        assert provider.calls == 1
        assert answer.text == "Alice founded Acme in 2019."
    finally:
        vault.close()


def test_escalation_retry_on_tier1_abstention(tmp_path: Path) -> None:
    # Efficient-hybrid: Tier-1 (blend) abstains → ONE bounded escalation re-ask
    # with the excerpt budget doubled. A tiny base budget forces the blend
    # excerpt to truncate so the doubled budget genuinely adds source bytes.
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["That is not in the notes.", "The budget was 5 million."])
        answer = Companion(vault, provider=provider).ask(
            "what was the budget?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_budget_tokens=10),
        )
        assert answer.hits
        assert provider.calls == 2  # Tier-1 abstained → bounded escalation fired
        assert answer.text == "The budget was 5 million."
        assert answer.retrieval["path"] == "subgraph_blend_escalated"
        assert answer.retrieval["escalated"] is True
        # Objective bounded escalation: at most 2x the base budget, never None.
        assert answer.retrieval["source_block_budget_tokens"] == 20
    finally:
        vault.close()


def test_escalation_skipped_when_doubled_budget_adds_nothing(tmp_path: Path) -> None:
    # Efficient-hybrid: when every source byte already fits the base budget,
    # an abstaining Tier-1 does NOT trigger a duplicate re-ask over an
    # identical context — the escalation is objectively skipped.
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["That is not in the notes.", "unused"])
        answer = Companion(vault, provider=provider).ask("what was the budget?", k=5)
        assert answer.hits
        assert provider.calls == 1
        assert answer.retrieval["path"] == "subgraph_blend"
        assert answer.retrieval["escalation_skipped_no_new_source"] is True
        assert _is_abstention(answer.text) is True
    finally:
        vault.close()


def test_abstention_floor_when_escalation_also_abstains(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        # Both passes abstain → keep the abstention (never fabricate).
        provider = _QueuedProvider(["That is not in the notes.", "No information is available."])
        answer = Companion(vault, provider=provider).ask(
            "acme funding budget",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_budget_tokens=10),
        )
        assert answer.hits
        assert provider.calls == 2
        assert _is_abstention(answer.text) is True
    finally:
        vault.close()


def test_provider_error_at_tier1_is_not_abstention(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        # Provider down on the FIRST call → graceful "", and NO Tier-2 retry.
        provider = _QueuedProvider(["unused"], raise_at={1})
        answer = Companion(vault, provider=provider).ask("acme funding budget", k=5)
        assert answer.hits
        assert provider.calls == 1  # did not retry on provider error
        assert answer.text == ""
    finally:
        vault.close()


def test_default_path_makes_one_completion(tmp_path: Path) -> None:
    # enable_subgraph defaults False → block-dump path → exactly one completion,
    # never the two-pass gate.
    vault = _seeded_vault(tmp_path)
    try:
        provider = _QueuedProvider(["Alice founded Acme."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.hits
        assert provider.calls == 1
    finally:
        vault.close()


def test_policy_never_source_blocks_skips_tier2_on_abstention(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _QueuedProvider(["That is not in the notes.", "unused tier2"])
        answer = Companion(vault, provider=provider).ask(
            "what was the budget?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(
                enable_subgraph=True,
                source_block_policy="never",
            ),
        )
        assert answer.hits
        assert provider.calls == 1
        assert answer.retrieval["path"] == "subgraph_only"
        assert answer.retrieval["source_blocks_used"] is False
        assert _is_abstention(answer.text) is True
    finally:
        vault.close()


def test_policy_always_source_blocks_includes_sources_on_first_pass(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _QueuedProvider(["The budget was 5 million."])
        answer = Companion(vault, provider=provider).ask(
            "what was the budget?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(
                enable_subgraph=True,
                source_block_policy="always",
                source_block_budget_tokens=500,
            ),
        )
        assert answer.hits
        assert provider.calls == 1
        assert answer.retrieval["path"] == "subgraph_with_sources"
        assert answer.retrieval["source_blocks_used"] is True
        assert "=== SOURCE BLOCKS ===" in provider.user_prompts[0]
    finally:
        vault.close()


# ── 3.19: Answer.citations under-represents subgraph-mode grounding ───────────
# citations/hits are always the retrieval seeds, but in subgraph mode the model
# is asked to cite claim:<id> anchors from a wider 1-hop+ ego-graph render that
# is never in hits. subgraph_evidence_ids surfaces that full ego-graph pool.
def test_subgraph_mode_populates_evidence_ids_beyond_citations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic topology (mirrors test_explore.py): a single seed node
    (Alice) is 1-hop from a Claim and its object (Acme). The retrieval-seed
    citation is Alice alone; the ego-graph evidence pool must also carry the
    claim id and Acme — exactly the gap the finding describes."""
    from okto_neuron.core.schema import Edge, Node
    from okto_neuron.models import Provenance, QueryHit
    from okto_neuron.store.memory import InMemoryStore

    store = InMemoryStore()
    alice = Node(id="a" * 64, type="Agent", title="Alice", content="Alice")
    acme = Node(id="c" * 64, type="Agent", title="Acme", content="Acme")
    claim = Node(
        id="d" * 64,
        type="Claim",
        title="alice founded",
        content="alice founded",
        facets={
            "S_id": alice.id,
            "P": "founded",
            "O_id": acme.id,
            "confidence": 0.9,
            "block_id": "b" * 64,
        },
    )
    for n in (alice, acme, claim):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    store.add_edge(Edge(id="e2", type="rdf:object", src=claim.id, dst=acme.id))
    vault = Vault(tmp_path, store)
    try:
        from okto_neuron.vault import _public_node

        hit = QueryHit(
            node=_public_node(alice),
            score=1.0,
            provenance=Provenance(
                path="alice.md",
                byte_start=0,
                byte_end=5,
                content_hash="sha256:" + "0" * 64,
                extraction_activity_id="activity-1",
                agent_id="agent-1",
                document_id="doc-1",
                block_id="b" * 64,
            ),
        )
        monkeypatch.setattr(vault, "query", lambda *a, **k: [hit])
        monkeypatch.setattr(vault, "query_seeds", lambda *a, **k: [(alice.id, 1.0)])

        provider = _QueuedProvider(["Alice founded Acme."])
        answer = Companion(vault, provider=provider).ask(
            "who did alice found?",
            k=1,
            retrieval_policy=AskRetrievalPolicy(
                enable_subgraph=True,
                # never-read source blocks: this fixture's QueryHit provenance
                # doesn't point at a real file, and the ego-graph render alone
                # is exactly what's under test here.
                source_block_policy="never",
            ),
        )

        assert answer.retrieval["mode"] == "subgraph"
        assert answer.citations == (alice.id,)
        # The whole point of 3.19: the ego-graph pool the model could cite
        # from reaches beyond the retrieval-seed citations.
        assert set(answer.subgraph_evidence_ids) == {alice.id, acme.id, claim.id}
        assert claim.id not in answer.citations
        assert acme.id not in answer.citations
        # trace-level id parity: it's exactly what _ask_subgraph computed, not
        # a coincidental re-derivation.
        assert set(answer.subgraph_evidence_ids) == set(answer.retrieval["subgraph_evidence_ids"])
    finally:
        vault.close()


def test_block_mode_leaves_subgraph_evidence_ids_empty(tmp_path: Path) -> None:
    # enable_subgraph defaults False → block-dump path never touches the
    # ego-graph, so the new field must stay empty (citations already is the
    # complete grounding set on this path).
    vault = _seeded_vault(tmp_path)
    try:
        provider = _QueuedProvider(["Alice founded Acme."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["mode"] == "block"
        assert answer.subgraph_evidence_ids == ()
    finally:
        vault.close()
