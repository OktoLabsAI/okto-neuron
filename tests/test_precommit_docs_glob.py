"""Regression test for the pre-commit docs-gate glob bug.

``path_is_docs_source`` in ``.githooks/pre-commit`` used bare bash ``case``
globs (``docs/*.md`` etc.) to classify paths as "docs source". Bash ``case``
globs match "/", so that pattern also matched files nested under a docs
subdirectory (e.g. ``docs/benchmarks/locomo.md``). As a result, an untracked
file living in such a subdirectory made the hook's "validation snapshot is
only partially staged" refusal fire for commits that have nothing to do with
that file.

This test drives the real hook script (unmodified, via ``bash``) inside a
disposable sandbox git repository so it exercises the actual classification
logic rather than a re-implementation of it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-commit"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)

    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir()
    shutil.copy2(HOOK, hooks_dir / "pre-commit")
    (hooks_dir / "pre-commit").chmod(0o755)

    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "unrelated.py").write_text("print(1)\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=repo)
    result = _run(["git", "commit", "-q", "-m", "init"], cwd=repo)
    assert result.returncode == 0, result.stderr

    return repo


def _stage_unrelated_change(repo: Path) -> None:
    """Stage a docs-source change so the hook proceeds past its early exit.

    The hook exits 0 immediately (before ever inspecting untracked files)
    unless something in the staged set matches docs/generated/instruction/
    lock inputs. README.md staged as "docs source" is enough to reach the
    partially-staged-validation-snapshot check the bug lives in.
    """
    with (repo / "README.md").open("a", encoding="utf-8") as fh:
        fh.write("more\n")
    _run(["git", "add", "README.md"], cwd=repo)


def test_untracked_nested_docs_file_does_not_block_unrelated_commit(
    tmp_path: Path,
) -> None:
    """An untracked file under a docs/ subdirectory must not trip the gate.

    Before the fix: docs/*.md matched docs/sub/x.md too (case globs match
    "/"), so this untracked file was misclassified as an unstaged docs
    "validation input" and the hook refused the commit.
    """
    repo = _make_sandbox_repo(tmp_path)
    _stage_unrelated_change(repo)

    nested = repo / "docs" / "sub"
    nested.mkdir(parents=True)
    (nested / "x.md").write_text("# nested doc\n", encoding="utf-8")

    result = _run(["bash", ".githooks/pre-commit"], cwd=repo)

    assert "partially staged" not in result.stderr, result.stderr
    assert "docs/sub/x.md" not in result.stderr, result.stderr


def test_untracked_top_level_docs_file_still_blocks(tmp_path: Path) -> None:
    """A genuine top-level docs/*.md untracked file must still be caught.

    Guards against an overcorrection that stops classifying any docs/*.md
    file as docs source.
    """
    repo = _make_sandbox_repo(tmp_path)
    _stage_unrelated_change(repo)

    (repo / "docs" / "toplevel.md").parent.mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "toplevel.md").write_text("# top level doc\n", encoding="utf-8")

    result = _run(["bash", ".githooks/pre-commit"], cwd=repo)

    assert result.returncode == 1
    assert "partially staged" in result.stderr
    assert "docs/toplevel.md" in result.stderr
