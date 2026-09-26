"""Unit tests for ADR 0025 continuous folder monitoring.

Model-free, fast. Tests the pure eligibility function ``_should_ingest_changes``
with an injected clock, the manifest diff logic using a tmp dir + tmp manifest,
and the per-root tick seam ``_tick_root`` with a stub state. No mocks, no LLM,
no vault handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.config import FolderWatchConfig
from okto_neuron.server import _folder_watch as fw
from okto_neuron.server._folder_watch import (
    _iter_watch_files,
    _should_ingest_changes,
    _tick_root,
    _VaultWatchState,
    compute_manifest_diff,
    _manifest_path,
    _save_manifest,
    _load_manifest,
    _record_ingest,
    _write_status,
    get_watch_status,
    run_folder_watch,
)

# chmod-000 tests need real permission enforcement — root bypasses it entirely.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# ── _should_ingest_changes ────────────────────────────────────────────────────


def _cfg(**kwargs) -> FolderWatchConfig:
    """Build a FolderWatchConfig with test-friendly defaults."""
    defaults = dict(
        enabled=True,
        poll_interval_s=5.0,
        quiet_debounce_s=3.0,
        min_interval_s=10.0,
    )
    defaults.update(kwargs)
    return FolderWatchConfig(**defaults)


class TestShouldIngestChanges:
    """Pure eligibility decision — injected clock, no I/O."""

    def test_not_pending_returns_none(self) -> None:
        cfg = _cfg()
        now = time.time()
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 30,
            last_enqueue_at=0.0,
            pending=False,
            cfg=cfg,
        )
        assert result is None

    def test_not_yet_settled_returns_none(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0)
        now = 1000.0
        # File changed only 1 second ago — not quiet yet.
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 1.0,
            last_enqueue_at=0.0,
            pending=True,
            cfg=cfg,
        )
        assert result is None

    def test_settled_first_time_returns_reason(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=10.0)
        now = 1000.0
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 5.0,  # quiet for 5s > 3s
            last_enqueue_at=0.0,  # never enqueued → treated as 0 → passes min_interval
            pending=True,
            cfg=cfg,
        )
        assert result is not None
        assert "settled" in result

    def test_min_interval_throttle(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=10.0)
        now = 1000.0
        # File is quiet enough BUT was enqueued only 5s ago.
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 5.0,
            last_enqueue_at=now - 5.0,  # 5s < min_interval_s 10s
            pending=True,
            cfg=cfg,
        )
        assert result is None

    def test_min_interval_elapsed_returns_reason(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=10.0)
        now = 1000.0
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 5.0,
            last_enqueue_at=now - 15.0,  # 15s > 10s → ok
            pending=True,
            cfg=cfg,
        )
        assert result is not None

    def test_exactly_at_debounce_boundary_passes(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=0.0)
        now = 1000.0
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 3.0,  # exactly 3.0 — boundary (>=)
            last_enqueue_at=0.0,
            pending=True,
            cfg=cfg,
        )
        assert result is not None

    def test_just_under_debounce_boundary_fails(self) -> None:
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=0.0)
        now = 1000.0
        result = _should_ingest_changes(
            now=now,
            last_change_seen_at=now - 2.999,
            last_enqueue_at=0.0,
            pending=True,
            cfg=cfg,
        )
        assert result is None


# ── compute_manifest_diff ─────────────────────────────────────────────────────


class TestComputeManifestDiff:
    """Manifest diff/partition using a real tmp dir — no mocks."""

    def _write(self, path: Path, content: str = "hello") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_new_file_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        self._write(root / "note.md", "# New")

        diff, new_manifest = compute_manifest_diff(root, {})
        assert len(diff.new) == 1
        assert diff.new[0].name == "note.md"
        assert len(diff.changed) == 0
        assert len(diff.deleted) == 0
        assert "note.md" in new_manifest

    def test_unchanged_file_not_in_diff(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        p = root / "note.md"
        self._write(p, "hello")
        st = p.stat()

        # Build a manifest that matches current state exactly.
        import hashlib

        sha = hashlib.sha256(b"hello").hexdigest()
        manifest = {"note.md": {"mtime": st.st_mtime, "size": st.st_size, "sha256": sha}}

        diff, new_manifest = compute_manifest_diff(root, manifest)
        assert len(diff.new) == 0
        assert len(diff.changed) == 0
        assert len(diff.deleted) == 0

    def test_changed_file_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        p = root / "note.md"
        self._write(p, "new content")
        st = p.stat()

        # Manifest has stale mtime → triggers rehash; hash is different from stored.
        manifest = {
            "note.md": {
                "mtime": st.st_mtime - 10.0,  # stale mtime triggers rehash
                "size": st.st_size,
                "sha256": "0" * 64,  # wrong hash → real content change
            }
        }

        diff, new_manifest = compute_manifest_diff(root, manifest)
        # mtime differs, rehash runs, sha256 differs → changed
        assert len(diff.changed) == 1
        assert diff.changed[0].name == "note.md"

    def test_changed_mtime_triggers_rehash(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        p = root / "note.md"
        self._write(p, "hello world")
        st = p.stat()

        import hashlib

        sha = hashlib.sha256(b"hello world").hexdigest()
        manifest = {
            "note.md": {
                "mtime": st.st_mtime - 10.0,  # stale mtime → triggers rehash
                "size": st.st_size,
                "sha256": sha,  # same hash → content unchanged → NOT in changed
            }
        }

        diff, new_manifest = compute_manifest_diff(root, manifest)
        # Same hash → not changed (mtime touch, no real edit)
        assert len(diff.changed) == 0
        assert len(diff.new) == 0

    def test_deleted_file_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        # Manifest has a file that does not exist on disk.
        manifest = {"gone.md": {"mtime": 0.0, "size": 0, "sha256": "a" * 64}}

        diff, new_manifest = compute_manifest_diff(root, manifest)
        assert "gone.md" in diff.deleted
        assert "gone.md" not in new_manifest

    def test_ignore_globs(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        self._write(root / "note.md", "keep")
        self._write(root / "note.md.swp", "swap")
        self._write(root / "4913", "vim")

        diff, new_manifest = compute_manifest_diff(root, {}, ignore_globs=["*.swp", "4913"])
        assert len(diff.new) == 1
        assert diff.new[0].name == "note.md"

    def test_non_text_files_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        self._write(root / "image.png", "fake png")
        self._write(root / "note.md", "text")

        diff, _ = compute_manifest_diff(root, {})
        names = [p.name for p in diff.new]
        assert "image.png" not in names
        assert "note.md" in names

    def test_recursive_finds_nested_file(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        self._write(root / "subdir" / "deep.md", "deep")

        diff, _ = compute_manifest_diff(root, {}, recursive=True)
        names = [p.name for p in diff.new]
        assert "deep.md" in names

    def test_non_recursive_skips_nested_file(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        self._write(root / "subdir" / "deep.md", "deep")
        self._write(root / "top.md", "top")

        diff, _ = compute_manifest_diff(root, {}, recursive=False)
        names = [p.name for p in diff.new]
        assert "deep.md" not in names
        assert "top.md" in names


# ── _iter_watch_files: unified enumeration + directory pruning (F1) ──────────


class TestIterWatchFiles:
    """Directory-glob pruning shared by the watch loop and one-shot ingest."""

    def _write(self, path: Path, content: str = "x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _tree(self, root: Path) -> None:
        self._write(root / "real.md")
        self._write(root / "sub" / "nested.md")
        self._write(root / ".state" / "backups" / "junk.md")
        self._write(root / ".remember" / "now.md")
        self._write(root / ".git" / "config.md")
        self._write(root / "__pycache__" / "cache.md")
        self._write(root / "node_modules" / "pkg" / "readme.md")

    def test_default_dir_globs_prune_internal_dirs(self, tmp_path: Path) -> None:
        from okto_neuron.config import DEFAULT_IGNORE_DIR_GLOBS

        root = tmp_path / "root"
        self._tree(root)
        found = _iter_watch_files(root, ignore_dir_globs=list(DEFAULT_IGNORE_DIR_GLOBS))
        rels = [str(p.relative_to(root)) for p in found]
        assert rels == ["real.md", "sub/nested.md"]

    def test_no_dir_globs_walks_everything(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        self._tree(root)
        found = _iter_watch_files(root)
        rels = {str(p.relative_to(root)) for p in found}
        assert ".state/backups/junk.md" in rels
        assert ".remember/now.md" in rels

    def test_dir_glob_matches_component_at_any_depth(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        self._write(root / "a" / "b" / ".hidden" / "deep.md")
        self._write(root / "a" / "keep.md")
        found = _iter_watch_files(root, ignore_dir_globs=[".*"])
        rels = [str(p.relative_to(root)) for p in found]
        assert rels == ["a/keep.md"]

    def test_file_globs_and_suffixes_still_apply(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        self._write(root / "keep.md")
        self._write(root / "skip.md.swp")
        self._write(root / "image.png")
        found = _iter_watch_files(root, ignore_globs=["*.swp"])
        assert [p.name for p in found] == ["keep.md"]

    def test_non_recursive_top_level_only(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        self._write(root / "top.md")
        self._write(root / "sub" / "deep.md")
        found = _iter_watch_files(root, recursive=False)
        assert [p.name for p in found] == ["top.md"]

    def test_compute_manifest_diff_prunes_dirs(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        self._tree(root)
        diff, new_manifest = compute_manifest_diff(
            root, {}, ignore_dir_globs=[".*", "__pycache__", "node_modules"]
        )
        assert sorted(new_manifest) == ["real.md", "sub/nested.md"]
        assert len(diff.new) == 2

    def test_discover_folder_prunes_by_default(self, tmp_path: Path) -> None:
        from okto_neuron.server._ingest_queue import discover_folder

        root = tmp_path / "root"
        self._tree(root)
        found = discover_folder(root)
        rels = [str(p.relative_to(root)) for p in found]
        assert rels == ["real.md", "sub/nested.md"]

    def test_discover_folder_honors_recursive_false(self, tmp_path: Path) -> None:
        """POST /api/v1/ingest-folder passes recursive from the request body —
        discover_folder must actually honor it."""
        from okto_neuron.server._ingest_queue import discover_folder

        root = tmp_path / "root"
        self._tree(root)
        found = discover_folder(root, recursive=False)
        rels = [str(p.relative_to(root)) for p in found]
        assert rels == ["real.md"]

    def test_discover_folder_honors_custom_globs(self, tmp_path: Path) -> None:
        """A vault with customized folder_watch globs must exclude identically
        in one-shot ingest and the continuous watcher (review finding: the
        first cut hardcoded the defaults)."""
        from okto_neuron.server._ingest_queue import discover_folder

        root = tmp_path / "root"
        self._tree(root)
        self._write(root / "archive" / "old.md")

        custom = [".*", "__pycache__", "node_modules", "archive"]
        oneshot = discover_folder(root, ignore_dir_globs=custom)
        watcher = _iter_watch_files(root, ignore_dir_globs=custom)
        assert oneshot == watcher
        assert [str(p.relative_to(root)) for p in oneshot] == [
            "real.md",
            "sub/nested.md",
        ]

    def test_dot_files_are_denylisted(self, tmp_path: Path) -> None:
        """Leading-dot FILES are scaffolding/tooling and must be excluded — the
        default ``.*`` ignore_dir_glob only pruned dot-DIRS, so this closes the
        dot-FILE gap. A plain note beside it still enumerates."""
        from okto_neuron.config import DEFAULT_IGNORE_DIR_GLOBS

        root = tmp_path / "root"
        self._write(root / ".dotnote.md")
        self._write(root / "tracking" / ".migration-log.md")
        self._write(root / "plain.md")
        found = _iter_watch_files(root, ignore_dir_globs=list(DEFAULT_IGNORE_DIR_GLOBS))
        assert [str(p.relative_to(root)) for p in found] == ["plain.md"]

    def test_scaffolding_files_are_denylisted(self, tmp_path: Path) -> None:
        """CLAUDE.md and presales-style tracking scaffolding are excluded
        anywhere; a tracking/ index is excluded, but a bare index.md elsewhere
        is a legit note and stays."""
        root = tmp_path / "root"
        self._write(root / "CLAUDE.md")
        self._write(root / "tracking" / "sync-queue.md")
        self._write(root / "tracking" / "sweep-dates.md")
        self._write(root / "tracking" / "index.md")
        self._write(root / "docs" / "index.md")  # legit — kept
        self._write(root / "real.md")
        found = _iter_watch_files(root, ignore_dir_globs=[".*"])
        assert [str(p.relative_to(root)) for p in found] == [
            "docs/index.md",
            "real.md",
        ]

    def test_marginalia_dir_is_pruned_unconditionally(self, tmp_path: Path) -> None:
        """The .marginalia internal dir holds durable source COPIES; walking it
        re-ingests already-ingested files (a tracking/tracking double-descent).
        It is pruned even when the caller's ignore_dir_globs is empty."""
        root = tmp_path / "root"
        self._write(root / "tracking" / "notes.md")  # the real source
        # A durable copy of the SAME tracking file, re-nested under the internal
        # dir — this is what produced tracking/tracking on the live vault.
        self._write(root / ".marginalia" / "sources" / "abc123" / "tracking" / "notes.md")
        found = _iter_watch_files(root, ignore_dir_globs=[])  # no config prune
        assert [str(p.relative_to(root)) for p in found] == ["tracking/notes.md"]


class TestSourceSelectionPolicyEquivalence:
    """ONE source-selection policy for the folder walk AND the batch endpoint.

    ADR 0025 F1 exists because duplicated exclusion rules drift: the live run's
    worst outcome was a graph half-built from ``.state/backups/`` and
    ``.remember/``. ``POST /api/v1/ingest-batch`` takes a client-supplied LIST
    and so cannot reuse the WALK — it reuses the walk's per-path PREDICATE. If
    these ever disagree, the two ingest surfaces have drifted apart again and
    the Web UI's folder button is back to queueing dot-directory scaffolding
    (measured: 168 files queued where ``/ingest-folder`` had reduced the same
    folder to 76).
    """

    # A file glob that can actually reach rule 3: the packaged ignore_globs
    # (``*.swp``, ``*.tmp``, ``4913``, ``*~``, ``.#*``) all fail the suffix gate
    # first, so they would never prove the glob rule runs at all.
    IGNORE_GLOBS = ["scratch-*.md"]
    IGNORE_DIR_GLOBS = [".*", "__pycache__", "node_modules"]

    def _tree(self, root: Path) -> None:
        for rel in (
            "real.md",
            "sub/nested.md",
            "sub/deep/deeper.md",
            "docs/index.md",  # a bare index.md is a legit note — kept
            "notes.txt",
            "CLAUDE.md",  # agent tooling notes — denylisted anywhere
            "docs/CLAUDE.md",
            "tracking/index.md",  # tracking-folder index — denylisted
            "tracking/sync-queue.md",
            "tracking/.migration-log.md",  # leading-dot FILE
            ".dotnote.md",
            ".scratchpad/plan.md",  # dot-DIR
            ".remember/now.md",
            ".claude/settings.md",
            "__pycache__/cache.md",
            "node_modules/pkg/readme.md",
            "scratch-draft.md",  # ignore_globs hit, text suffix
            "sub/scratch-two.md",
            "image.png",  # non-text suffix
            "sub/report.pdf",
            ".marginalia/sources/abc123/tracking/notes.md",  # internal dir
        ):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("body\n", encoding="utf-8")

    def _every_relpath(self, root: Path) -> list[str]:
        """Every file in the tree, unfiltered — the batch endpoint's input is a
        list the CLIENT enumerated, so it can contain paths the walk would
        never have descended to."""
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                out.append(str((Path(dirpath) / name).relative_to(root)))
        return sorted(out)

    def test_walker_and_predicate_agree_on_every_path(self, tmp_path: Path) -> None:
        from okto_neuron.server._folder_watch import classify_source_relpath

        root = tmp_path / "root"
        self._tree(root)

        walked = {
            str(p.relative_to(root))
            for p in _iter_watch_files(
                root,
                ignore_globs=self.IGNORE_GLOBS,
                ignore_dir_globs=self.IGNORE_DIR_GLOBS,
            )
        }
        predicate_accepted = {
            rel
            for rel in self._every_relpath(root)
            if classify_source_relpath(
                Path(rel),
                ignore_globs=self.IGNORE_GLOBS,
                ignore_dir_globs=self.IGNORE_DIR_GLOBS,
            )
            is None
        }
        assert predicate_accepted == walked
        # Pin the expected verdict too, so an equivalence that agrees on the
        # WRONG answer (e.g. both accepting everything) still fails.
        assert walked == {
            "real.md",
            "sub/nested.md",
            "sub/deep/deeper.md",
            "docs/index.md",
            "notes.txt",
        }

    def test_batch_upload_names_match_the_walker(self, tmp_path: Path) -> None:
        """THE drift guard: the same input list, judged through the batch
        endpoint's entry point, yields the walker's decisions exactly."""
        from okto_neuron.server._ingest_queue import classify_upload_name

        root = tmp_path / "root"
        self._tree(root)

        walked = {
            str(p.relative_to(root))
            for p in _iter_watch_files(
                root,
                ignore_globs=self.IGNORE_GLOBS,
                ignore_dir_globs=self.IGNORE_DIR_GLOBS,
            )
        }
        batch_accepted = {
            rel
            for rel in self._every_relpath(root)
            if classify_upload_name(
                rel,
                ignore_globs=self.IGNORE_GLOBS,
                ignore_dir_globs=self.IGNORE_DIR_GLOBS,
            )
            is None
        }
        assert batch_accepted == walked

    def test_reason_codes_are_specific(self, tmp_path: Path) -> None:
        """Skips are reported with a reason, so each rule must be
        distinguishable — a single opaque "rejected" tells the operator
        nothing about a mis-selected folder."""
        from okto_neuron.server._ingest_queue import classify_upload_name

        cases = {
            ".scratchpad/plan.md": "ignored_dir",
            ".claude/settings.md": "ignored_dir",
            "CLAUDE.md": "denylisted",
            "docs/CLAUDE.md": "denylisted",
            ".dotnote.md": "denylisted",
            "tracking/index.md": "denylisted",
            ".marginalia/sources/k/a.md": "ignored_dir",
            "scratch-draft.md": "ignored_glob",
            "report.pdf": "non_text_suffix",
            "real.md": None,
            "sub/nested.md": None,
        }
        for name, expected in cases.items():
            assert (
                classify_upload_name(
                    name,
                    ignore_globs=self.IGNORE_GLOBS,
                    ignore_dir_globs=self.IGNORE_DIR_GLOBS,
                )
                == expected
            ), name

    def test_internal_dir_excluded_even_with_no_configured_globs(self, tmp_path: Path) -> None:
        """``.marginalia`` is not a config choice: an upload naming the vault's
        own durable source copies must be refused even when the caller cleared
        every glob."""
        from okto_neuron.server._ingest_queue import classify_upload_name

        assert (
            classify_upload_name(
                ".marginalia/sources/abc/notes.md", ignore_globs=[], ignore_dir_globs=[]
            )
            == "ignored_dir"
        )

    def test_missing_dir_globs_fall_back_to_packaged_defaults(self) -> None:
        """A vault whose config can't be read must still get dot-directory
        pruning — otherwise the fix silently disables itself on exactly the
        vault whose config is broken. Mirrors ``discover_folder``."""
        from okto_neuron.server._ingest_queue import classify_upload_name

        assert classify_upload_name(".scratchpad/plan.md") == "ignored_dir"
        assert classify_upload_name("__pycache__/x.md") == "ignored_dir"
        assert classify_upload_name("CLAUDE.md") == "denylisted"
        assert classify_upload_name("notes/real.md") is None

    def test_judged_path_is_the_written_path(self, tmp_path: Path) -> None:
        """``classify_upload_name`` and ``upload_target_path`` share one
        normalization, so a hostile name can never be judged as one path and
        written as another."""
        from okto_neuron.server import _ingest_queue as iq

        sources = tmp_path / "sources"
        sources.mkdir()
        # Traversal collapses to the bare basename in BOTH: judged clean,
        # written clean.
        assert iq.classify_upload_name("../../.scratchpad/notes.md") is None
        assert iq.upload_target_path(sources, "../../.scratchpad/notes.md", "x").name == "notes.md"
        # ...and a traversal that collapses onto a denylisted basename is still
        # refused by the same shared normalization.
        assert iq.classify_upload_name("../../CLAUDE.md") == "denylisted"


class TestDedupeRoots:
    """Overlapping/nested watched roots collapse so a subtree is walked once."""

    def test_nested_root_dropped(self, tmp_path: Path) -> None:
        from okto_neuron.server._folder_watch import _dedupe_roots

        outer = tmp_path / "vault"
        inner = outer / "tracking"
        # Configured inner-then-outer AND outer-then-inner both collapse to the
        # outer covering root, so tracking/ is descended exactly once.
        assert _dedupe_roots([inner, outer]) == [outer]
        assert _dedupe_roots([outer, inner]) == [outer]

    def test_disjoint_roots_kept(self, tmp_path: Path) -> None:
        from okto_neuron.server._folder_watch import _dedupe_roots

        a = tmp_path / "a"
        b = tmp_path / "b"
        assert _dedupe_roots([a, b]) == [a, b]

    def test_exact_duplicate_collapsed(self, tmp_path: Path) -> None:
        from okto_neuron.server._folder_watch import _dedupe_roots

        a = tmp_path / "a"
        assert _dedupe_roots([a, a]) == [a]


# ── manifest sidecar I/O ──────────────────────────────────────────────────────


class TestManifestSidecar:
    """Round-trip: _save_manifest / _load_manifest, path keying."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        vault_path = tmp_path / "vault"
        root = tmp_path / "watched"
        vault_path.mkdir()
        root.mkdir()

        data = {"note.md": {"mtime": 1000.0, "size": 42, "sha256": "ab" * 32}}
        path = _manifest_path(vault_path, root)
        _save_manifest(path, data)

        loaded = _load_manifest(path)
        assert loaded == data

    def test_missing_manifest_returns_empty(self, tmp_path: Path) -> None:
        vault_path = tmp_path / "vault"
        root = tmp_path / "watched"
        vault_path.mkdir()
        root.mkdir()
        path = _manifest_path(vault_path, root)

        loaded = _load_manifest(path)
        assert loaded == {}

    def test_different_roots_get_different_sidecar_paths(self, tmp_path: Path) -> None:
        vault_path = tmp_path / "vault"
        vault_path.mkdir()
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"

        assert _manifest_path(vault_path, root_a) != _manifest_path(vault_path, root_b)


# ── status snapshot (HTTP /api/v1/folder-watch/status backing store) ─────────


class TestWatchStatusSnapshot:
    """The in-memory status dict the watch loop writes each tick and the HTTP
    layer reads for /api/v1/folder-watch/status. Pure dict ops, no I/O."""

    def test_unknown_vault_returns_disabled_placeholder(self) -> None:
        status = get_watch_status("/nonexistent/vault")
        assert status["enabled"] is False
        assert status["roots"] == []
        assert status["pending"] == []

    def test_write_status_then_read_back(self) -> None:
        _write_status("/tmp/vaultX", enabled=True, roots=["/tmp/watched"], now=1000.0)
        status = get_watch_status("/tmp/vaultX")
        assert status["enabled"] is True
        assert status["roots"] == ["/tmp/watched"]
        assert status["last_poll_ts"] == 1000.0

        all_status = get_watch_status()
        assert "/tmp/vaultX" in all_status

    def test_record_ingest_appends_and_caps_recent(self) -> None:
        _write_status("/tmp/vaultY", enabled=True, roots=["/tmp/watched"], now=1.0)
        for i in range(25):
            _record_ingest("/tmp/vaultY", [f"file{i}.md"], float(i))
        status = get_watch_status("/tmp/vaultY")
        assert len(status["recent_ingests"]) == 20
        assert status["recent_ingests"][-1]["name"] == "file24.md"


# ── _tick_root seam (characterization, injected clock) ───────────────────────


def _stub_state(vault_path: Path) -> SimpleNamespace:
    """Minimal ServerState stand-in for the tick seam.

    ``ingest_worker_active=True`` keeps ``ensure_worker`` from spawning a real
    drain task; enqueued items still land in ``ingest_queue``.
    """
    return SimpleNamespace(
        vault_path=vault_path,
        ingest_queue=[],
        ingest_worker_active=True,
        ingest_cancel_requested=False,
    )


class TestTickRoot:
    """Characterize one full tick: manifest load → diff → debounce → fire →
    enqueue → manifest save, driven with an injected ``now``."""

    def _setup(self, tmp_path: Path) -> tuple[SimpleNamespace, Path, Path, _VaultWatchState]:
        vault = tmp_path / "vault"
        vault.mkdir()
        root = tmp_path / "watched"
        root.mkdir()
        return _stub_state(vault), vault, root, _VaultWatchState()

    def _tick(self, state, vault: Path, root: Path, cfg, vs, now: float) -> None:
        asyncio.run(_tick_root(state, vault, str(vault), root, cfg, vs, now))

    def test_new_file_registers_pending_without_enqueue(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        (root / "note.md").write_text("# hi\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)

        fs = vs.get_root(str(root))["note.md"]
        assert fs.pending is True
        assert fs.last_change_seen_at == 1000.0
        assert state.ingest_queue == []
        # F5: a pending NEW file is withheld from the sidecar until its
        # enqueue fires, so a restart re-detects it.
        manifest = _load_manifest(_manifest_path(vault, root))
        assert "note.md" not in manifest

    def test_settled_file_fires_and_enqueues(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        (root / "note.md").write_text("# hi\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=10.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)  # registers pending
        self._tick(state, vault, root, cfg, vs, now=1005.0)  # quiet 5s > 3s → fires

        assert len(state.ingest_queue) == 1
        item = state.ingest_queue[0]
        assert item.name == "note.md"
        assert item.status == "queued"
        # Durable copy landed under .marginalia/sources.
        assert (vault / ".marginalia" / "sources") in Path(item.path).parents
        fs = vs.get_root(str(root))["note.md"]
        assert fs.pending is False
        assert fs.last_enqueue_at == 1005.0

    def test_min_interval_throttles_second_fire(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0, min_interval_s=100.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1002.0)  # fires (first enqueue)
        assert len(state.ingest_queue) == 1

        p.write_text("v2\n", encoding="utf-8")
        self._tick(state, vault, root, cfg, vs, now=1004.0)  # re-pending
        self._tick(state, vault, root, cfg, vs, now=1010.0)  # quiet, but interval 8s < 100s
        assert len(state.ingest_queue) == 1  # throttled

    def test_deleted_file_dropped_from_debounce_and_manifest(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        p.unlink()
        self._tick(state, vault, root, cfg, vs, now=1005.0)

        assert "note.md" not in vs.get_root(str(root))
        manifest = _load_manifest(_manifest_path(vault, root))
        assert "note.md" not in manifest

    def test_pending_edit_keeps_old_sha_until_fire(self, tmp_path: Path) -> None:
        """F5 durability: a detected-but-not-yet-fired edit must keep the OLD
        entry in the persisted manifest — a daemon restart inside the debounce
        window then re-detects the edit instead of silently losing it. (This
        inverts the pre-fix characterization: the buggy watcher persisted the
        new sha immediately.)"""
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=0.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1005.0)  # v1 ingested
        assert len(state.ingest_queue) == 1

        p.write_text("v2 — edited\n", encoding="utf-8")
        self._tick(state, vault, root, cfg, vs, now=1010.0)  # pending, NOT fired
        assert vs.get_root(str(root))["note.md"].pending is True
        assert len(state.ingest_queue) == 1

        import hashlib

        manifest = _load_manifest(_manifest_path(vault, root))
        v1_sha = hashlib.sha256(b"v1\n").hexdigest()
        v2_sha = hashlib.sha256("v2 — edited\n".encode()).hexdigest()
        assert manifest["note.md"]["sha256"] == v1_sha  # old entry survives

        # After the fire, the new sha lands in the sidecar.
        self._tick(state, vault, root, cfg, vs, now=1015.0)
        assert len(state.ingest_queue) == 1  # dedup: same path still queued → refresh
        manifest = _load_manifest(_manifest_path(vault, root))
        assert manifest["note.md"]["sha256"] == v2_sha

    def test_restart_inside_debounce_window_still_ingests(self, tmp_path: Path) -> None:
        """F5 end-to-end: daemon dies between detection and fire → a fresh
        in-memory watch state (restart) re-detects the edit from the sidecar
        and fires it."""
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=0.0)
        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1005.0)  # v1 ingested
        p.write_text("v2 after restart\n", encoding="utf-8")
        self._tick(state, vault, root, cfg, vs, now=1010.0)  # pending — daemon "dies" here

        vs2 = _VaultWatchState()  # restart: empty debounce table
        self._tick(state, vault, root, cfg, vs2, now=2000.0)  # re-detects from sidecar
        assert vs2.get_root(str(root))["note.md"].pending is True
        self._tick(state, vault, root, cfg, vs2, now=2005.0)  # settles → fires
        fs = vs2.get_root(str(root))["note.md"]
        assert fs.pending is False
        assert fs.last_enqueue_at == 2005.0

    def test_edit_then_undo_clears_pending_without_ingest(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0, min_interval_s=0.0)
        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1005.0)  # v1 ingested
        assert len(state.ingest_queue) == 1

        p.write_text("v2 temporary\n", encoding="utf-8")
        self._tick(state, vault, root, cfg, vs, now=1010.0)  # pending
        p.write_text("v1\n", encoding="utf-8")  # undo
        self._tick(state, vault, root, cfg, vs, now=1011.0)

        assert vs.get_root(str(root))["note.md"].pending is False
        self._tick(state, vault, root, cfg, vs, now=1020.0)  # would have fired
        assert len(state.ingest_queue) == 1  # nothing new

    def test_pending_new_file_deleted_before_fire_is_dropped(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "ephemeral.md"
        p.write_text("here and gone\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=3.0)
        self._tick(state, vault, root, cfg, vs, now=1000.0)  # pending new file
        assert "ephemeral.md" in vs.get_root(str(root))
        p.unlink()
        self._tick(state, vault, root, cfg, vs, now=1001.0)
        assert "ephemeral.md" not in vs.get_root(str(root))
        manifest = _load_manifest(_manifest_path(vault, root))
        assert "ephemeral.md" not in manifest

    def test_enqueue_failure_keeps_file_pending_and_retries(self, tmp_path: Path) -> None:
        """A source that cannot be copied at fire time (permissions) must stay
        pending with its old manifest entry — retried when readable again —
        instead of being dropped forever (review finding on Phase 0/1)."""
        import os as _os

        state, vault, root, vs = self._setup(tmp_path)
        p = root / "locked.md"
        p.write_text("secret v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0, min_interval_s=0.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)  # pending
        _os.chmod(p, 0o000)  # unreadable at fire time
        try:
            self._tick(state, vault, root, cfg, vs, now=1002.0)  # fire → copy fails
            fs = vs.get_root(str(root))["locked.md"]
            assert fs.pending is True  # kept for retry
            assert state.ingest_queue == []
            manifest = _load_manifest(_manifest_path(vault, root))
            assert "locked.md" not in manifest  # new file: withheld
        finally:
            _os.chmod(p, 0o644)

        self._tick(state, vault, root, cfg, vs, now=1004.0)  # retry succeeds
        assert [i.name for i in state.ingest_queue] == ["locked.md"]
        assert vs.get_root(str(root))["locked.md"].pending is False

    def test_tick_root_survives_enqueue_exception(self, tmp_path: Path, monkeypatch) -> None:
        """T2 (review finding): an unforeseen exception raised inside the
        enqueue step must not propagate out of ``_tick_root`` — the file stays
        pending and the persisted manifest keeps its old (pre-edit) entry, so
        the next tick retries cleanly. Uses a previously-ingested file (fired
        once normally) so "manifest keeps old entry" is a meaningful
        assertion — a brand-new pending file is withheld from the manifest
        regardless (F5), which would make this test pass even if the
        try/except around the enqueue call were deleted."""
        state, vault, root, vs = self._setup(tmp_path)
        p = root / "note.md"
        p.write_text("v1\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0, min_interval_s=0.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)  # pending
        self._tick(state, vault, root, cfg, vs, now=1002.0)  # v1 ingested
        assert len(state.ingest_queue) == 1

        p.write_text("v2 edited\n", encoding="utf-8")
        self._tick(state, vault, root, cfg, vs, now=1004.0)  # re-pending, not fired (0s < 1s)
        v1_sha = hashlib.sha256(b"v1\n").hexdigest()
        manifest = _load_manifest(_manifest_path(vault, root))
        assert manifest["note.md"]["sha256"] == v1_sha

        async def _raise_enqueue(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(fw, "_enqueue_for_vault", _raise_enqueue)

        self._tick(state, vault, root, cfg, vs, now=1010.0)  # settled → would fire, raises

        fs = vs.get_root(str(root))["note.md"]
        assert fs.pending is True
        assert len(state.ingest_queue) == 1  # no new item queued
        manifest = _load_manifest(_manifest_path(vault, root))
        assert manifest["note.md"]["sha256"] == v1_sha  # old entry survives

    def test_per_path_acceptance_partial_batch(self, tmp_path: Path) -> None:
        """T3: a batch with two settled files, one made unreadable before the
        fire, must accept the readable one and keep only the unreadable one
        pending. A mutation collapsing the per-path ``in accepted`` check to a
        batch-level ``if accepted`` would flip BOTH files to fired here."""
        if _IS_ROOT:
            pytest.skip("chmod 000 has no effect for root")

        state, vault, root, vs = self._setup(tmp_path)
        readable = root / "readable.md"
        locked = root / "locked.md"
        readable.write_text("hello\n", encoding="utf-8")
        locked.write_text("secret\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0, min_interval_s=0.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)  # both pending, not fired
        os.chmod(locked, 0o000)
        try:
            self._tick(state, vault, root, cfg, vs, now=1002.0)  # settled → batch fire

            fs_readable = vs.get_root(str(root))["readable.md"]
            fs_locked = vs.get_root(str(root))["locked.md"]
            assert fs_readable.pending is False
            assert fs_locked.pending is True
            assert [i.name for i in state.ingest_queue] == ["readable.md"]
        finally:
            os.chmod(locked, 0o644)

    def test_cleanup_survives_permission_error_on_subdir(self, tmp_path: Path) -> None:
        """T4: ``pathlib.Path.is_file()`` raises (not returns False) when a
        PARENT directory loses search permission — the vanished-file cleanup
        must catch that and treat the entry as still present, not propagate."""
        if _IS_ROOT:
            pytest.skip("chmod 000 has no effect for root")

        state, vault, root, vs = self._setup(tmp_path)
        sub = root / "sub"
        sub.mkdir()
        p = sub / "note.md"
        p.write_text("hello\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=0.0, min_interval_s=0.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)  # discovered & fired
        assert len(state.ingest_queue) == 1
        assert "sub/note.md" in vs.get_root(str(root))

        os.chmod(sub, 0o000)
        try:
            # note.md is no longer enumerable (os.walk can't descend into
            # sub); the cleanup loop's is_file() check on "sub/note.md" now
            # hits a PermissionError from the unsearchable parent — must not
            # raise, and the entry must be kept (treated as still present).
            self._tick(state, vault, root, cfg, vs, now=1001.0)
            assert "sub/note.md" in vs.get_root(str(root))
        finally:
            # 0o755, not 0o644: a directory needs the execute (search) bit to
            # be usable at all — restoring only rw- would leave `sub`
            # permanently unenterable and break tmp_path teardown.
            os.chmod(sub, 0o755)

    def test_skipped_non_text_counted_and_surfaced(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        (root / "note.md").write_text("text\n", encoding="utf-8")
        (root / "image.png").write_text("fake\n", encoding="utf-8")
        (root / "data.csv").write_text("a,b\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        assert vs.skipped_non_text[str(root)] == 2

        _write_status(str(vault), enabled=True, roots=[str(root)], now=1000.0, vs=vs, cfg=cfg)
        assert get_watch_status(str(vault))["skipped_non_text"] == 2

    def test_paused_reason_in_status(self) -> None:
        _write_status(
            "/tmp/vault-paused",
            enabled=True,
            roots=["/tmp/watched"],
            now=1.0,
            paused_reason=(
                "not the legacy fallback — folder-watch ingest paused in direct "
                "ServerState compatibility mode"
            ),
        )
        status = get_watch_status("/tmp/vault-paused")
        assert "not the legacy fallback" in status["paused_reason"]
        # Placeholder for unknown vaults carries the key too (UI contract).
        assert get_watch_status("/nonexistent/x")["paused_reason"] is None

    def test_fired_files_recorded_in_watch_status(self, tmp_path: Path) -> None:
        state, vault, root, vs = self._setup(tmp_path)
        (root / "note.md").write_text("# hi\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0)

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1002.0)

        status = get_watch_status(str(vault))
        names = [e["name"] for e in status["recent_ingests"]]
        assert "note.md" in names

    def test_default_config_prunes_junk_dirs_end_to_end(self, tmp_path: Path) -> None:
        """Mutation-killer (review finding): with a PLAIN FolderWatchConfig —
        no explicit globs — a full tick must never register or enqueue files
        under .state/, .remember/, .git/ etc. Pins both the pydantic default
        AND the cfg→_tick_root→compute_manifest_diff plumbing; deleting either
        turns this red."""
        state, vault, root, vs = self._setup(tmp_path)
        (root / "real.md").write_text("keep\n", encoding="utf-8")
        for junk in (".state/backups/old.md", ".remember/now.md", ".git/x.md"):
            p = root / junk
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("junk\n", encoding="utf-8")
        cfg = _cfg(quiet_debounce_s=1.0)  # defaults for ignore_dir_globs

        self._tick(state, vault, root, cfg, vs, now=1000.0)
        self._tick(state, vault, root, cfg, vs, now=1002.0)

        assert list(vs.get_root(str(root))) == ["real.md"]
        assert [i.name for i in state.ingest_queue] == ["real.md"]


# ── run_folder_watch: the GLOBAL loop, F12b non-active pause end-to-end ──────


class TestRunFolderWatchGlobalLoop:
    """T1: drives the actual ``run_folder_watch`` background task (not the
    ``_tick_root`` seam) for a couple of ticks with two fake vaults, so the
    F12b active-vault gate, the per-vault status writes, and the warn-once
    behavior are pinned end-to-end rather than only unit-tested piecewise."""

    def _run_state(self, vault_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            vault_path=vault_path,
            draining=False,
            ingest_queue=[],
            ingest_worker_active=True,  # inert: no real drain worker spawned
            ingest_cancel_requested=False,
        )

    def test_f12b_gate_end_to_end(
        self, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import okto_neuron.vault_registry as vault_registry_mod

        vault_a = tmp_path / "vault_a"
        vault_b = tmp_path / "vault_b"
        vault_a.mkdir()
        vault_b.mkdir()
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "note.md").write_text("hello\n", encoding="utf-8")

        entries = [SimpleNamespace(path=vault_a), SimpleNamespace(path=vault_b)]
        monkeypatch.setattr(vault_registry_mod, "list_vaults", lambda **_kw: entries)

        cfg_a = _cfg(
            enabled=True,
            poll_interval_s=0.01,
            quiet_debounce_s=0.0,
            min_interval_s=0.0,
            roots=[str(root_a)],
        )
        cfg_b = _cfg(
            enabled=True,
            poll_interval_s=0.01,
            quiet_debounce_s=0.0,
            min_interval_s=0.0,
            roots=[str(root_b)],
        )

        def _fake_load_cfg(vault_path: Path) -> FolderWatchConfig:
            return cfg_a if Path(vault_path) == vault_a else cfg_b

        monkeypatch.setattr(fw, "_load_folder_watch_config", _fake_load_cfg)
        monkeypatch.setattr(fw, "WATCH_TICK_S", 0.01)

        state = self._run_state(vault_a)

        async def _drive() -> None:
            task = asyncio.ensure_future(run_folder_watch(state))
            try:
                await asyncio.sleep(0.3)  # comfortably more than 2 ticks
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        caplog.set_level(logging.WARNING, logger="okto_neuron.server.folder_watch")
        asyncio.run(_drive())

        # A (compatibility fallback): the file settled and was enqueued.
        assert [i.name for i in state.ingest_queue] == ["note.md"]

        # B (different vault in direct ServerState compatibility mode): paused,
        # never scanned or given a manifest sidecar.
        status_b = get_watch_status(str(vault_b))
        assert status_b["paused_reason"] is not None
        assert "not the legacy fallback" in status_b["paused_reason"]
        assert not (vault_b / ".marginalia" / "watches").exists()

        # The pause warning fires exactly once across every tick of the run.
        pause_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "does not match the legacy fallback" in r.getMessage()
        ]
        assert len(pause_warnings) == 1


# ── CLI ↔ endpoint contract: watch-folder add initial ingest ─────────────────


class TestInitialIngestContract:
    """The CLI's initial-ingest payload must satisfy api_ingest_folder's
    required key. The original implementation posted {"root": ...} against a
    handler requiring "path" — every `watch-folder add` initial ingest 400'd
    (review finding: the fix shipped with no test pinning the contract)."""

    def test_cli_payload_key_accepted_by_endpoint(self, tmp_path: Path) -> None:
        from starlette.testclient import TestClient

        from okto_neuron import Vault
        from okto_neuron.cli import _initial_ingest_payload
        from okto_neuron.server.http import build_rest_app
        from okto_neuron.server.state import init_state, reset_state_for_tests

        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v")
        try:
            init_state(vault, Path(vault.path))
            client = TestClient(build_rest_app(), base_url="http://127.0.0.1")
            # Nonexistent folder: a REJECTED key would 400 "bad_request"
            # before the path is ever looked at; an ACCEPTED key reaches the
            # folder check and 404s. That distinction pins the contract
            # without needing a drain worker or an LLM.
            missing = str(tmp_path / "does-not-exist")
            resp = client.post(
                "/api/v1/ingest-folder",
                json=_initial_ingest_payload(missing, True),
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"] == "not_found"
        finally:
            vault.close()
            reset_state_for_tests()


class TestWatchFolderCliServerContract:
    def test_add_registers_and_ingests_with_immutable_vault_selector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from okto_neuron import Vault
        from okto_neuron.cli import app

        # Isolate HOME: the `app` group callback unconditionally calls
        # load_user_env_file(), which would otherwise leak this developer
        # machine's real ~/.marginalia/env secrets into the pytest process
        # (see tests/cli/conftest.py's docstring for the reproduction history).
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        vault_path = tmp_path / "vault"
        vault = Vault.init(vault_path, embedding_provider="stub")
        vault.close()
        root = tmp_path / "source"
        root.mkdir()
        calls: list[tuple[str, str, str, dict | None, float, object]] = []

        def fake_request(
            endpoint,
            method,
            path,
            payload=None,
            *,
            timeout=30.0,
            transport=None,
            vault=None,
        ):
            del transport
            calls.append((endpoint, method, path, payload, timeout, vault))
            if path == "/api/v1/folder-watch/roots":
                return {"status": "ok", "folder_watch": {"recursive": False}}
            if path == "/api/v1/ingest-folder":
                return {"status": "ok", "enqueued": 2, "refreshed": 1}
            raise AssertionError(path)

        monkeypatch.setattr("okto_neuron.cli._client_request", fake_request)
        before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
        result = CliRunner().invoke(
            app,
            [
                "watch-folder",
                "add",
                str(root),
                "--vault",
                str(vault_path),
                "--endpoint",
                "http://127.0.0.1:7890",
                "--timeout",
                "4",
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls == [
            (
                "http://127.0.0.1:7890",
                "POST",
                "/api/v1/folder-watch/roots",
                {"path": str(root)},
                4.0,
                vault_path,
            ),
            (
                "http://127.0.0.1:7890",
                "POST",
                "/api/v1/ingest-folder",
                {"path": str(root), "recursive": False},
                4.0,
                vault_path,
            ),
        ]
        assert "initial ingest: 2 file(s) queued, 1 already queued" in result.output
        assert (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8") == before

    def test_add_without_vault_uses_configured_default_selector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from okto_neuron import Vault
        from okto_neuron.cli import app

        from okto_neuron.vault_registry import set_default_vault

        vault_path = tmp_path / "expected"
        vault = Vault.init(vault_path, embedding_provider="stub")
        vault.close()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        set_default_vault(vault_path)
        root = tmp_path / "source"
        root.mkdir()
        calls: list[tuple[str, object]] = []

        def fake_request(endpoint, method, path, payload=None, **kwargs):
            del endpoint, method, payload
            calls.append((path, kwargs.get("vault")))
            return {"status": "ok", "folder_watch": {"recursive": True}}

        monkeypatch.setattr("okto_neuron.cli._client_request", fake_request)
        before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
        result = CliRunner().invoke(
            app,
            ["watch-folder", "add", str(root), "--no-initial-ingest"],
        )

        assert result.exit_code == 0, result.output
        assert calls == [("/api/v1/folder-watch/roots", vault_path.resolve())]
        assert (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8") == before

    def test_add_reports_registration_as_partial_when_initial_ingest_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from okto_neuron import Vault
        from okto_neuron.cli import app
        from okto_neuron.cli._client import ServerError

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        vault_path = tmp_path / "vault"
        vault = Vault.init(vault_path, embedding_provider="stub")
        vault.close()
        root = tmp_path / "source"
        root.mkdir()

        def fake_request(endpoint, method, path, payload=None, **kwargs):
            del endpoint, method, payload, kwargs
            if path == "/api/v1/folder-watch/roots":
                return {"status": "ok", "folder_watch": {"recursive": True}}
            if path == "/api/v1/ingest-folder":
                raise ServerError(503, "server is draining")
            raise AssertionError(path)

        monkeypatch.setattr("okto_neuron.cli._client_request", fake_request)
        result = CliRunner().invoke(
            app,
            ["watch-folder", "add", str(root), "--vault", str(vault_path)],
        )

        assert result.exit_code == 1
        assert "registered:" in result.output
        assert "root was registered, but initial ingest failed" in result.output

    def test_list_and_remove_use_server_config_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from okto_neuron import Vault
        from okto_neuron.cli import app

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        vault_path = tmp_path / "vault"
        vault = Vault.init(vault_path, embedding_provider="stub")
        vault.close()
        root = tmp_path / "source"
        root.mkdir()
        calls: list[tuple[str, str, dict | None, object]] = []

        def fake_request(endpoint, method, path, payload=None, **kwargs):
            del endpoint
            calls.append((method, path, payload, kwargs.get("vault")))
            if path == "/api/v1/config":
                return {"status": "ok", "folder_watch": {"enabled": True, "roots": [str(root)]}}
            if path == "/api/v1/folder-watch/roots":
                return {"status": "ok", "folder_watch": {"enabled": True, "roots": []}}
            raise AssertionError(path)

        monkeypatch.setattr("okto_neuron.cli._client_request", fake_request)
        runner = CliRunner()
        listed = runner.invoke(
            app,
            ["watch-folder", "list", "--vault", str(vault_path)],
        )
        removed = runner.invoke(
            app,
            ["watch-folder", "remove", str(root), "--vault", str(vault_path)],
        )

        assert listed.exit_code == 0, listed.output
        assert str(root) in listed.output
        assert removed.exit_code == 0, removed.output
        assert calls[-1] == (
            "DELETE",
            "/api/v1/folder-watch/roots",
            {"path": str(root)},
            vault_path,
        )
        assert calls[0] == ("GET", "/api/v1/config", None, vault_path)


class TestInitialIngestEndpointBehavior:
    def test_endpoint_honors_vault_configured_dir_globs(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end pin of the config→discover_folder plumbing (review
        finding: deleting the VaultConfig.load block survived the suite). A
        vault with a CUSTOM ignore_dir_globs entry must see it applied by
        POST /api/v1/ingest-folder."""
        import yaml as _yaml
        from starlette.testclient import TestClient

        from okto_neuron import Vault
        from okto_neuron.server import _ingest_queue as iq_mod
        from okto_neuron.server.http import build_rest_app
        from okto_neuron.server.state import init_state, reset_state_for_tests

        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v")
        try:
            cfg_file = Path(vault.path) / "okto-neuron.yaml"
            raw = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            raw["folder_watch"] = {
                "ignore_dir_globs": [".*", "__pycache__", "node_modules", "archive"]
            }
            cfg_file.write_text(_yaml.dump(raw), encoding="utf-8")

            folder = tmp_path / "oneshot"
            (folder / "archive").mkdir(parents=True)
            (folder / "keep.md").write_text("keep\n", encoding="utf-8")
            (folder / "archive" / "old.md").write_text("old\n", encoding="utf-8")

            # Keep the drain worker inert (no LLM in this test).
            monkeypatch.setattr(iq_mod, "ensure_worker", lambda s, f: None)

            init_state(vault, Path(vault.path))
            client = TestClient(build_rest_app(), base_url="http://127.0.0.1")
            resp = client.post(
                "/api/v1/ingest-folder", json={"path": str(folder), "recursive": True}
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["enqueued"] == 1
            names = [i["name"] for i in body["items"]]
            assert names == ["keep.md"]  # archive/ pruned per vault config
        finally:
            vault.close()
            reset_state_for_tests()

    def test_endpoint_response_includes_skipped_non_text_and_refreshed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """T6: F12c added `skipped_non_text`/`refreshed` counters to the
        response body (see api_ingest_folder in server/http.py), but nothing
        pinned that the HTTP response actually carries them through — only
        that the underlying discover_folder/enqueue_paths stats plumbing
        exists. A non-text file (.png) must be counted and surfaced, and a
        first-time enqueue must report zero refreshes."""
        from starlette.testclient import TestClient

        from okto_neuron import Vault
        from okto_neuron.server import _ingest_queue as iq_mod
        from okto_neuron.server.http import build_rest_app
        from okto_neuron.server.state import init_state, reset_state_for_tests

        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v")
        try:
            folder = tmp_path / "oneshot-skipped"
            folder.mkdir()
            (folder / "keep.md").write_text("keep\n", encoding="utf-8")
            (folder / "image.png").write_text("fake png\n", encoding="utf-8")

            # Keep the drain worker inert (no LLM in this test).
            monkeypatch.setattr(iq_mod, "ensure_worker", lambda s, f: None)

            init_state(vault, Path(vault.path))
            client = TestClient(build_rest_app(), base_url="http://127.0.0.1")
            resp = client.post(
                "/api/v1/ingest-folder", json={"path": str(folder), "recursive": True}
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "skipped_non_text" in body
            assert "refreshed" in body
            assert body["skipped_non_text"] == 1  # image.png
            assert body["refreshed"] == 0
        finally:
            vault.close()
            reset_state_for_tests()
