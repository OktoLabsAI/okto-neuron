"""The quality bar, regression-locked as a pytest — REAL models, NO mocks.

This is the pytest twin of acceptance scenario 56. It drives the real
Companion.remember pipeline (an explicitly configured OpenAI-compatible model +
real fastembed) over a fresh single-note vault and asserts the team's full
definition of done:

  1. remember() a partner note into a fresh vault.
  2. recall("who is the partner on Okto Neuron") TOP-1 is the relationship Claim
     naming Jordan Lee Carter, WITH byte-range provenance (path + bs<be + hash).
  3. ZERO infra/provenance nodes in the recall top-k.
  4. ZERO raw Block nodes in the untyped recall top-k.
  5. Single-entity note -> exactly one node per entity (no duplicates).

Tagged ``realmodel`` AND ``slow`` so the default suite
(``-m "not slow and not acceptance_private_corpus"``) deselects it. Set
``OKTO_NEURON_LLM_BASE_URL`` and run explicitly with ``-m realmodel``. The model
defaults to ``unsloth/Qwen3.6-27B-NVFP4``; override it only through
``OKTO_NEURON_REALMODEL_MODEL``. The test skips when the explicit endpoint is
missing or unreachable.
"""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest

from okto_neuron._internal.infra import is_infra
from okto_neuron.companion import Companion
from okto_neuron.config._vault import VaultConfig
from okto_neuron.embed import get_provider as embed_provider
from okto_neuron.vault import Vault

pytestmark = [pytest.mark.realmodel, pytest.mark.slow]

API_BASE = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
MODEL = os.environ.get("OKTO_NEURON_REALMODEL_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()
PARTNER = "Jordan Lee Carter"
QUESTION = "who is the partner on Okto Neuron"
NOTE = """# Okto Neuron project note

Okto Neuron is a local-first knowledge graph spun out of okto-pulse-core.
Alex is the lead author of Okto Neuron. Jordan Lee Carter is the project
partner on Okto Neuron. The schema is locked to five primitives and uses
PROV-O for provenance.
"""

_ENTITY_TYPES = {"Concept", "Agent", "InformationObject", "Place", "Activity"}


def _server_reachable(base: str) -> bool:
    try:
        httpx.get(base.rstrip("/") + "/models", timeout=3.0).raise_for_status()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def recall_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not API_BASE:
        pytest.skip("OKTO_NEURON_LLM_BASE_URL is required for realmodel tests")
    if not _server_reachable(API_BASE):
        pytest.skip(f"configured model server not reachable at {API_BASE}")

    root = tmp_path_factory.mktemp("quality-bar") / "vault"
    vault = Vault.init(root, embedding_provider="fastembed")
    VaultConfig.apply_patch(
        root,
        {
            "llm": {
                "allow_remote": True,
                "defaults": {
                    "provider": "openai",
                    "api_base": API_BASE,
                    "model": MODEL,
                },
                "extraction": {"samples": 2},
            }
        },
    )
    vault.close()
    vault = Vault.open(root)
    note = root / "notes" / "partner.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(NOTE, encoding="utf-8")

    # Let Companion build the explicitly configured provider from the vault config.
    companion = Companion(vault, embedder=embed_provider("fastembed"))
    companion.remember(note)

    hits = companion.recall(QUESTION, k=10)
    all_nodes = list(vault.store.list_nodes())
    return {
        "note": note,
        "raw": note.read_bytes(),
        "hits": hits,
        "all_nodes": all_nodes,
    }


def _fmt(hits) -> str:
    return "\n".join(
        f"  [{i}] type={h.node.type} infra={is_infra(h.node)} title={h.node.title!r}"
        for i, h in enumerate(hits)
    )


def test_recall_returns_hits(recall_report: dict) -> None:
    assert recall_report["hits"], "recall returned no hits"


def test_top1_is_partner_claim_with_provenance(recall_report: dict) -> None:
    hits = recall_report["hits"]
    raw = recall_report["raw"]
    top = hits[0]
    assert top.node.type == "Claim", f"top-1 must be a Claim, got {top.node.type}\n{_fmt(hits)}"
    assert PARTNER in top.node.title, (
        f"top-1 must name {PARTNER}, got {top.node.title!r}\n{_fmt(hits)}"
    )
    assert top.provenance.path, "top-1 has no provenance path"
    assert top.byte_end > top.byte_start, f"invalid byte range [{top.byte_start}:{top.byte_end}]"
    digest = hashlib.sha256(raw[top.byte_start : top.byte_end]).hexdigest()
    assert digest == top.content_hash, "top-1 byte range does not hash back to source"


def test_zero_infra_nodes_in_topk(recall_report: dict) -> None:
    infra = [h.node.title for h in recall_report["hits"] if is_infra(h.node)]
    assert not infra, f"infra/provenance nodes leaked into recall top-k: {infra}"


def test_zero_raw_block_nodes_in_topk(recall_report: dict) -> None:
    blocks = [h.node.title for h in recall_report["hits"] if h.node.type == "Block"]
    assert not blocks, f"raw Block nodes leaked into untyped recall top-k: {blocks}"


def test_single_entity_no_duplicates(recall_report: dict) -> None:
    counts: dict[tuple[str, str], int] = {}
    for n in recall_report["all_nodes"]:
        if is_infra(n) or n.type not in _ENTITY_TYPES:
            continue
        key = (n.type, (n.title or "").strip().lower())
        counts[key] = counts.get(key, 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    assert not dupes, f"single-entity note minted duplicate entity nodes: {dupes}"
