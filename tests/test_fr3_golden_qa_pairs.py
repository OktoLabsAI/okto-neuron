"""FR3 golden Q/A provenance-shape tests against the synthetic vault."""

from __future__ import annotations

from datetime import date, datetime
import fnmatch
from pathlib import Path
import re
import shutil
from typing import Any

import pytest
import yaml

from okto_neuron.vault import Vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "synthetic-vault"

GOLDEN_CASES = [
    {
        "id": "qa1_handoff_owner",
        "question": "handoff owner invoice",
        "expected_path_glob": "*.md",
        "expected_agent_regex": r"^agent:[A-Za-z0-9_.-]+$",
    },
    {
        "id": "qa2_decision_date",
        "question": "ladybug kuzu decision",
        "expected_path_glob": "decisions/*.md",
        "expected_date_regex": r"^\d{4}-\d{2}-\d{2}",
    },
    {
        "id": "qa3_commitment_status",
        "question": "commitment overdue",
        "expected_path_glob": "*.md",
        "expected_agent_regex": r"^agent:[A-Za-z0-9_.-]+$",
    },
]


@pytest.fixture(scope="module")
def synthetic_vault(tmp_path_factory: pytest.TempPathFactory) -> Vault:
    vault_root = tmp_path_factory.mktemp("fr3") / "synthetic-vault"
    shutil.copytree(FIXTURE_ROOT, vault_root)
    vault = Vault.open(vault_root)
    for path in sorted(vault_root.rglob("*.md")):
        vault.add(path)
    return vault


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["id"])
def test_golden_qa_provenance_shape(synthetic_vault: Vault, case: dict[str, str]) -> None:
    hits = synthetic_vault.query(case["question"], k=5)
    assert hits
    # Claims-first short-circuit was removed: a non-Claim node can now legitimately
    # outrank Claims. The contract is that a Claim is PRESENT in the top-k with
    # byte-anchored provenance — not that it sits at rank 0.
    hit = next((h for h in hits if h.claim_id), None)
    assert hit is not None

    relative = Path(hit.path).resolve().relative_to(synthetic_vault.root).as_posix()
    assert fnmatch.fnmatch(relative, case["expected_path_glob"]) or fnmatch.fnmatch(
        Path(hit.path).name,
        case["expected_path_glob"],
    )

    provenance = synthetic_vault.get_provenance(hit.claim_id)
    assert provenance is not None
    claim = provenance["claim"]
    block = provenance["block"]
    assert isinstance(claim, dict)
    facets = claim["facets"]
    frontmatter = _frontmatter(Path(facets["source_path"]))

    if block is not None:
        assert isinstance(block, dict)
        block_facets = block["facets"]
        assert block_facets["byte_end"] >= block_facets["byte_start"]
        assert block_facets["content_hash"]

    agent_regex = case.get("expected_agent_regex")
    if agent_regex:
        agent_value = facets.get("agent") or frontmatter.get("agent")
        assert agent_value is not None
        assert re.match(agent_regex, str(agent_value))

    date_regex = case.get("expected_date_regex")
    if date_regex:
        date_value = facets.get("created_at") or frontmatter.get("created_at")
        assert date_value is not None
        assert re.match(date_regex, _date_text(date_value))


def _frontmatter(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}
    data = yaml.safe_load(raw[4:end]) or {}
    return data if isinstance(data, dict) else {}


def _date_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
