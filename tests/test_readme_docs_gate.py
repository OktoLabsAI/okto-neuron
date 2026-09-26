"""Guards that keep the README wired into the docs gate so it can't rot.

README.md is a first-class doc source: it is rendered inline into the knowledge
base (registered in THEMES in docs/build_knowledge_base.py) and recognised by both
docs-gate layers (the local .githooks/pre-commit hook and .github/workflows/
docs-gate.yml). If any of those wirings is dropped, the README can drift from the
code without the gate noticing — which is the exact rot this file prevents.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_readme_registered_in_knowledge_base_generator() -> None:
    generator = (REPO_ROOT / "docs" / "build_knowledge_base.py").read_text(encoding="utf-8")
    assert '("README.md"' in generator, "README.md must stay registered in THEMES"


def test_readme_recognised_by_local_precommit_docs_gate() -> None:
    hook = (REPO_ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "README.md" in hook, "local pre-commit docs gate must recognise README.md"


def test_readme_recognised_by_ci_docs_gate() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "docs-gate.yml").read_text(encoding="utf-8")
    assert "README.md" in workflow, "CI docs gate must accept README.md as a docs change"
