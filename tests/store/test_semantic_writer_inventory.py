"""ADR 0039 Phase 3 exit guard — the semantic-writer inventory is complete.

Every ``add_node`` / ``add_edge`` call site in ``src/okto_neuron`` is either
planner-routed (it applies a sealed plan / durable ledger decision and is subject
to the lease + fence) or non-semantic infrastructure (deterministic ingest
scaffolding, or a whole-graph copy that mints nothing new).  A new, unclassified
writer is a release-blocking regression, so this test fails until the site is
added to :data:`CLASSIFIED_WRITERS` *and* to ``docs/semantic-writer-inventory.md``.

Sites are keyed by ``(module, enclosing qualname, method)`` — never line numbers,
which churn under unrelated edits.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "okto_neuron"
DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "semantic-writer-inventory.md"

PLANNER_ROUTED = "planner-routed"
INFRASTRUCTURE = "non-semantic-infrastructure"

#: (module path relative to src/okto_neuron, enclosing qualname, method) -> class.
CLASSIFIED_WRITERS: dict[tuple[str, str, str], str] = {
    # ── deterministic ingest scaffolding (no LLM, no planner) ────────────────
    ("ingest/__init__.py", "ingest_document", "add_node"): INFRASTRUCTURE,
    ("ingest/__init__.py", "_add_claim_provenance_edges", "add_edge"): INFRASTRUCTURE,
    ("ingest/__init__.py", "_ensure_system_nodes", "add_node"): INFRASTRUCTURE,
    # ── companion: sealed-plan overlay + applier ─────────────────────────────
    ("companion/__init__.py", "_seed_planning_overlay", "add_node"): PLANNER_ROUTED,
    ("companion/__init__.py", "_seed_planning_overlay", "add_edge"): PLANNER_ROUTED,
    ("companion/__init__.py", "_apply_sealed_semantic_plan", "add_node"): PLANNER_ROUTED,
    ("companion/__init__.py", "_apply_sealed_semantic_plan", "add_edge"): PLANNER_ROUTED,
    # ── incremental re-ingest (block-hash delta; deterministic) ──────────────
    ("companion/_incremental.py", "_apply_supersede", "add_node"): INFRASTRUCTURE,
    ("companion/_incremental.py", "_apply_supersede", "add_edge"): INFRASTRUCTURE,
    ("companion/_incremental.py", "_apply_detach", "add_node"): INFRASTRUCTURE,
    ("companion/_incremental.py", "resurrect_reverted_claims", "add_node"): INFRASTRUCTURE,
    ("ingest/__init__.py", "_retire_stale_deterministic_claims", "add_node"): INFRASTRUCTURE,
    # ── whole-graph copies: mint nothing, preserve identity ──────────────────
    ("store/reembed.py", "copy_graph_reembedding", "add_node"): INFRASTRUCTURE,
    ("store/reembed.py", "copy_graph_reembedding", "add_edge"): INFRASTRUCTURE,
    ("store/reembed.py", "copy_graph_canonicalizing", "add_node"): INFRASTRUCTURE,
    ("store/reembed.py", "copy_graph_canonicalizing", "add_edge"): INFRASTRUCTURE,
    # ── one-shot deterministic migration ─────────────────────────────────────
    ("migrate/bridge_edges.py", "ensure_source_mentions", "add_edge"): INFRASTRUCTURE,
    # ── M1: IndexedStore write-through wrapper (mints nothing, delegates) ────
    ("store/index/indexed.py", "IndexedStore.add_node", "add_node"): INFRASTRUCTURE,
    ("store/index/indexed.py", "IndexedStore.add_edge", "add_edge"): INFRASTRUCTURE,
    # ── M2a: snapshot load (verified whole-graph replay, mints nothing new) ──
    ("store/snapshot.py", "load", "add_node"): INFRASTRUCTURE,
    ("store/snapshot.py", "load", "add_edge"): INFRASTRUCTURE,
}


def _enclosing_qualname(tree: ast.Module) -> dict[int, str]:
    """Map every line to the qualname of the innermost enclosing def/class."""
    by_line: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                for line in range(child.lineno, (child.end_lineno or child.lineno) + 1):
                    by_line[line] = qualname
                walk(child, f"{qualname}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return by_line


def discover_writers() -> set[tuple[str, str, str]]:
    """AST-scan for ``<something>.add_node(...)`` / ``.add_edge(...)`` calls.

    ``store/protocol.py`` and the store implementations' own ``def add_node`` /
    ``def add_edge`` are definitions, not call sites, and are skipped.
    """
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in {"store/protocol.py", "store/memory.py", "store/ladybug.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = _enclosing_qualname(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {"add_node", "add_edge"}:
                continue
            found.add((rel, lines.get(node.lineno, "<module>"), func.attr))
    return found


def test_every_semantic_writer_is_classified() -> None:
    discovered = discover_writers()
    unclassified = sorted(discovered - set(CLASSIFIED_WRITERS))
    assert not unclassified, (
        "unclassified add_node/add_edge call site(s). Classify each in "
        "CLASSIFIED_WRITERS and document it in docs/semantic-writer-inventory.md: "
        f"{unclassified}"
    )


def test_inventory_has_no_stale_entries() -> None:
    discovered = discover_writers()
    stale = sorted(set(CLASSIFIED_WRITERS) - discovered)
    assert not stale, f"classified writers no longer present in src/okto_neuron: {stale}"


def test_every_classified_writer_appears_in_the_durable_doc() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    missing = sorted(
        {
            f"{module}::{qualname}"
            for module, qualname, _ in CLASSIFIED_WRITERS
            if f"{module}` · `{qualname}" not in text
        }
    )
    assert not missing, f"call sites absent from {DOC_PATH.name}: {missing}"
