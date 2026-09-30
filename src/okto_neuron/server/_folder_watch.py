"""Global continuous folder-monitoring loop (ADR 0025).

A single asyncio background task (started at daemon boot alongside the
curation scheduler) polls ALL registered vaults for file changes in their
``folder_watch.roots`` directories and auto-ingests settled files via the
existing ingest queue.

Design principles:

* GLOBAL, not selection-scoped. The loop iterates every vault that declares
  ``folder_watch.roots`` in its okto-neuron.yaml and sends settled changes to
  that vault's immutable ``VaultRuntime``. Browser selection never pauses or
  retargets background ingest (ADR 0034).
* CHANGE DETECTION is graph-free: stat + hash vs a per-vault manifest sidecar.
  No vault is opened until a settled change triggers an ingest. Scales past
  ``max_open=8``.
* SETTLE DETECTION: each file tracks ``last_change_seen_at``; a mid-save flurry
  keeps bumping it; ingest fires once the file has been quiet for
  ``quiet_debounce_s``.
* ENQUEUE, not inline remember(). Settled changes go to
  ``_ingest_queue.enqueue_paths`` + ``ensure_worker``. The drain worker runs
  ``remember()`` off-thread under the writer lock — the watch loop never blocks
  on LLM calls.
* POLLING ONLY. ``watchfiles`` is declared but unused; native FS events are
  unreliable on Docker/NFS. stat-walk is the load-bearing path. watchfiles can
  serve as an opt-in accelerator in a future ADR.

Each ``VaultRuntime`` owns its writer lock, so unrelated vaults can drain in
parallel while work within one vault stays serialized.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.server._store_io import store_io

if TYPE_CHECKING:
    from okto_neuron.config import FolderWatchConfig
    from okto_neuron.server.state import ServerState

_LOG = logging.getLogger("okto_neuron.server.folder_watch")

# Short tick so a long config poll_interval never delays SIGTERM shutdown.
WATCH_TICK_S = 1.0

# Sidecar manifest lives under the vault's .marginalia/watches/
_WATCHES_DIR = ".marginalia/watches"


# ── per-file debounce state ────────────────────────────────────────────────────


@dataclass
class _FileState:
    """Tracking entry for one watched file."""

    mtime: float
    size: int
    sha256: str
    last_change_seen_at: float = field(default_factory=time.time)
    last_enqueue_at: float = 0.0
    pending: bool = False  # True = change seen, not yet ingested


# ── pure eligibility decision ──────────────────────────────────────────────────


def _should_ingest_changes(
    now: float,
    last_change_seen_at: float,
    last_enqueue_at: float,
    pending: bool,
    cfg: "FolderWatchConfig",
) -> str | None:
    """Pure eligibility test for one file. Returns a reason string when the
    file should be enqueued for ingest, else ``None``. Mirrors
    ``_scheduler._should_sweep`` for injected-clock testability.

    Conditions (ALL must hold):
    1. ``pending`` — a change has been detected but not yet ingested.
    2. QUIET / debounce: ``now - last_change_seen_at >= quiet_debounce_s``.
    3. MIN-INTERVAL FLOOR: ``now - last_enqueue_at >= min_interval_s``
       (anti-thrash; first enqueue has ``last_enqueue_at`` 0 → passes).
    """
    if not pending:
        return None
    if now - last_change_seen_at < cfg.quiet_debounce_s:
        return None  # file still changing — keep waiting
    if now - last_enqueue_at < cfg.min_interval_s:
        return None  # min-interval anti-thrash floor
    return (
        f"settled after {now - last_change_seen_at:.1f}s quiet, "
        f"interval {now - last_enqueue_at:.0f}s since last enqueue"
    )


# ── per-vault manifest sidecar ─────────────────────────────────────────────────


def _manifest_path(vault_path: Path, root: Path) -> Path:
    """Sidecar manifest for one watched root, keyed by the root's hash."""
    root_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return vault_path / _WATCHES_DIR / f"{root_key}.manifest.json"


def _load_manifest(path: Path) -> dict[str, dict]:
    """Load a sidecar manifest; returns {} on any read/parse error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_manifest(path: Path, data: dict[str, dict]) -> None:
    """Atomically write the manifest sidecar (temp + os.replace)."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches_any_glob(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


# ── hard scaffolding/dot-file denylist (non-configurable) ────────────────────
# ALWAYS excluded from ingest enumeration, regardless of a vault's ignore_globs
# — this is tooling/scaffolding the graph must never ingest as user knowledge:
#   * the ``.marginalia/`` internal dir (durable source copies, graph, state).
#     Re-walking it re-ingests copies of already-ingested files — a
#     ``tracking/`` folder reappearing under ``.marginalia/sources/<key>/
#     tracking/`` is the "tracking/tracking" double-descent. Pruned at BOTH the
#     directory (never descend) and file (belt) level, and unconditionally so a
#     user who clears ``ignore_dir_globs`` still can't self-ingest.
#   * any leading-dot FILE. Dot-DIRS are already pruned by the default ``.*``
#     ignore_dir_glob; dot-FILES were not (the gap this closes).
#   * agent tooling notes (``CLAUDE.md``) and presales-style tracking
#     scaffolding (``sync-queue.md``, ``sweep-dates.md``) anywhere.
#   * a tracking-folder index (``index.md`` ONLY under a ``tracking/`` path
#     segment — a bare ``index.md`` elsewhere is a legitimate note, so it is
#     NOT denylisted by basename).
INTERNAL_DIR_NAME = ".marginalia"
_DENY_FILE_NAMES: frozenset[str] = frozenset({"CLAUDE.md", "sync-queue.md", "sweep-dates.md"})


def _is_denylisted_relpath(rel: Path) -> bool:
    """Basename-level denylist shared by watch enumeration AND trust-root rebuild.

    ``rel`` is a file path RELATIVE to the tree being enumerated (the watch root,
    or a durable source-key root under ``.marginalia/sources/``). It applies only
    NAME-level scaffolding rules plus the ``tracking/tracking`` double-descent
    exclusion, and deliberately does NOT apply the ``.marginalia`` internal-dir
    rule: rebuild computes ``rel`` relative to a source-key root that already
    lives under ``.marginalia/sources/``, so an ``.marginalia in parts`` test
    there would wrongly reject every durable source. The one caller that must
    also guard the internal dir (``_is_denylisted_file`` for the live watch) adds
    that rule itself. Factored out so watch and rebuild can never drift.

    Excludes:
      * any leading-dot FILE (``tracking/.migration-log.md``).
      * agent tooling / presales scaffolding by name (``CLAUDE.md``,
        ``sync-queue.md``, ``sweep-dates.md``).
      * a tracking-folder index (``index.md`` ONLY under a ``tracking/`` segment).
      * the ``tracking/tracking`` double-descent: the nested duplicate the
        companion mirrors when a ``tracking`` subroot is watched inside its own
        parent — every file beneath the inner ``tracking`` is a dupe.
    """
    parts = rel.parts
    if not parts:
        return False
    name = parts[-1]
    parents = parts[:-1]
    if name.startswith("."):
        return True
    if name in _DENY_FILE_NAMES:
        return True
    if name == "index.md" and "tracking" in parents:
        return True
    if any(a == "tracking" and b == "tracking" for a, b in zip(parts, parts[1:])):
        return True
    return False


def _is_denylisted_file(rel_parts: tuple[str, ...]) -> bool:
    """True when a file is scaffolding/tooling that must never enter the graph.

    ``rel_parts`` is the file's path components relative to the walk root
    (``("tracking", "index.md")``). Adds the ``.marginalia`` internal-dir guard
    on top of the shared basename rules; the live watch root is a vault (not a
    source-key root), so that guard is safe and wanted here."""
    if not rel_parts:
        return False
    if INTERNAL_DIR_NAME in rel_parts:
        return True
    return _is_denylisted_relpath(Path(*rel_parts))


# ── the shared source-selection policy (ADR 0025 F1, ADR 0026) ───────────────
# ADR 0025's whole point is ONE enumerator so exclusion behaves identically
# everywhere. A second ingest surface that takes a client-supplied LIST of names
# (``POST /api/v1/ingest-batch``, the Web UI's "Choose a folder" button and its
# drag-drop) cannot walk the disk, so it cannot reuse the WALK — it needs the
# PREDICATE the walk applies per file. These are that predicate: the walk below
# and the batch endpoint both route every decision through them, so a rule can
# never be enforced on one path and missing on the other. The live regression
# this closes: a folder ``/ingest-folder`` reduced to 76 files was queued as 168
# through the batch endpoint — 51% of that queue was dot-directory scaffolding
# and agent tooling notes, filtered by nothing but a browser-side regex.
#
# Reason codes are part of the operator-facing contract (the batch endpoint
# reports them per rejected file); keep them few and stable.
SKIP_IGNORED_DIR = "ignored_dir"
SKIP_NON_TEXT = "non_text_suffix"
SKIP_IGNORED_GLOB = "ignored_glob"
SKIP_DENYLISTED = "denylisted"


def _dir_component_excluded(name: str, dir_globs: list[str]) -> bool:
    """One directory component's exclusion rule.

    Shared by the walk's in-place ``dirnames`` pruning (which must decide
    incrementally, so it never stat-walks an excluded subtree) and by
    :func:`classify_source_relpath` (which decides after the fact, from a path
    it was handed). Same rule, one implementation — the pruning is an
    optimization of this predicate, not a second copy of it.

    The ``.marginalia`` internal dir is excluded unconditionally, never as a
    config choice, so a user who clears ``ignore_dir_globs`` still cannot
    re-ingest the vault's own durable source copies."""
    return name == INTERNAL_DIR_NAME or _matches_any_glob(name, dir_globs)


def classify_source_relpath(
    rel: Path,
    *,
    ignore_globs: list[str] | None = None,
    ignore_dir_globs: list[str] | None = None,
) -> str | None:
    """Should this relative path be ingested? ``None`` = yes, else a reason code.

    ``rel`` is a file path RELATIVE to the tree being ingested (the watch root,
    or the folder the browser enumerated). Rules are applied in the same order
    the walk applies them, which is load-bearing for equivalence: a file inside
    an excluded directory reports ``ignored_dir`` and is NOT counted as
    non-text, because the walk prunes that directory before ever seeing the
    file.

      1. ``ignore_dir_globs`` (plus the unconditional ``.marginalia`` prune)
         against every PARENT component.
      2. the ``TEXT_SUFFIXES`` gate — Okto Neuron's trust root is markdown.
      3. ``ignore_globs`` against the BASENAME (``4913`` must still match
         ``sub/4913``, so this is deliberately not a relpath match).
      4. the non-configurable scaffolding/dot-file denylist
         (:func:`_is_denylisted_file`, internal-dir guard included).

    Both glob kinds treat ``None`` as "no globs", matching
    :func:`_iter_watch_files`; a caller that wants the packaged dir-glob
    defaults passes them explicitly (``discover_folder`` and the batch endpoint
    both do).
    """
    from okto_neuron.server._ingest_queue import TEXT_SUFFIXES

    parts = rel.parts
    if not parts:
        return None
    fglobs = ignore_globs or []
    dglobs = ignore_dir_globs or []
    name = parts[-1]
    if any(_dir_component_excluded(component, dglobs) for component in parts[:-1]):
        return SKIP_IGNORED_DIR
    if Path(name).suffix.lower() not in TEXT_SUFFIXES:
        return SKIP_NON_TEXT
    if _matches_any_glob(name, fglobs):
        return SKIP_IGNORED_GLOB
    if _is_denylisted_file(parts):
        return SKIP_DENYLISTED
    return None


def _dedupe_roots(roots: list[Path]) -> list[Path]:
    """Drop nested/duplicate watched roots so a subtree is descended once.

    Two configured roots where one is a subpath of the other (``<X>`` and
    ``<X>/tracking``) both walk the ``tracking/`` subtree — the same source is
    then enqueued twice under two different durable-copy prefixes, defeating the
    queue's same-path dedup (the "tracking/tracking" double-descent). Keep only
    the outermost covering roots. Inputs must already be resolved; order is
    preserved for the first occurrence of each kept root."""
    kept: list[Path] = []
    for root in roots:
        if any(root == k or root.is_relative_to(k) for k in kept):
            continue
        # A newly-seen OUTER root supersedes any already-kept inner root.
        kept = [k for k in kept if not k.is_relative_to(root)]
        kept.append(root)
    return kept


def _iter_watch_files(
    root: Path,
    *,
    recursive: bool = True,
    ignore_globs: list[str] | None = None,
    ignore_dir_globs: list[str] | None = None,
    stats: dict | None = None,
) -> list[Path]:
    """Deterministic enumeration of ingestible files under ``root``.

    The single enumeration used by BOTH the continuous watch loop and one-shot
    folder ingest, so exclusion behaves identically everywhere. ``os.walk``
    with in-place ``dirnames`` pruning: any directory whose name matches an
    entry in ``ignore_dir_globs`` is dropped before descent, so internal state
    dirs (``.state/``, ``.remember/``, ``.git/``, caches) are never stat-walked
    at all — each path component is fnmatch-tested as the walk descends.
    Files are filtered by ``TEXT_SUFFIXES`` and ``ignore_globs``; output is
    sorted for stable ordering.

    ``stats`` (optional dict) receives ``skipped_non_text`` — the count of
    files passed over because their suffix is not ingestible. Surfaced so the
    watcher status and one-shot ingest can SAY they skipped (the live run
    silently ignored 21 non-md files and the operator had no signal).

    Every per-file decision is delegated to :func:`classify_source_relpath` so
    the walk and the list-taking batch endpoint enforce one policy.
    """
    fglobs = ignore_globs or []
    dglobs = ignore_dir_globs or []
    skipped_non_text = 0
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if recursive:
            # Prune before descent so an excluded subtree is never stat-walked;
            # same rule the predicate applies to a path's parent components.
            dirnames[:] = sorted(d for d in dirnames if not _dir_component_excluded(d, dglobs))
        else:
            dirnames[:] = []
        for name in sorted(filenames):
            abs_path = Path(dirpath) / name
            reason = classify_source_relpath(
                abs_path.relative_to(root),
                ignore_globs=fglobs,
                ignore_dir_globs=dglobs,
            )
            if reason is not None:
                if reason == SKIP_NON_TEXT:
                    skipped_non_text += 1
                continue
            if not abs_path.is_file():
                continue  # broken symlink etc.
            out.append(abs_path)
    if stats is not None:
        stats["skipped_non_text"] = skipped_non_text
    return sorted(out)


# ── manifest diff ──────────────────────────────────────────────────────────────


@dataclass
class _ManifestDiff:
    changed: list[Path] = field(default_factory=list)
    new: list[Path] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # relpath strings
    skipped_non_text: int = 0  # files passed over for non-ingestible suffix


def compute_manifest_diff(
    root: Path,
    manifest: dict[str, dict],
    *,
    recursive: bool = True,
    ignore_globs: list[str] | None = None,
    ignore_dir_globs: list[str] | None = None,
) -> tuple[_ManifestDiff, dict[str, dict]]:
    """Stat-walk ``root``, diff vs ``manifest``.

    Returns (diff, new_manifest_snapshot) where new_manifest_snapshot reflects
    current on-disk state (mtime/size/sha256) for all present files.

    Hash is computed ONLY when (mtime, size) differs from the stored entry —
    cheap for unchanged files. Deleted files (in manifest but not on disk) are
    included in diff.deleted; their entries are dropped from new_manifest.
    """
    diff = _ManifestDiff()
    new_manifest: dict[str, dict] = {}

    # Enumerate on-disk files (shared exclusion rules — see _iter_watch_files).
    walk_stats: dict = {}
    for abs_path in _iter_watch_files(
        root,
        recursive=recursive,
        ignore_globs=ignore_globs,
        ignore_dir_globs=ignore_dir_globs,
        stats=walk_stats,
    ):
        try:
            st = abs_path.stat()
        except OSError:
            continue
        relpath = str(abs_path.relative_to(root))
        prev = manifest.get(relpath)

        mtime = st.st_mtime
        size = st.st_size

        if prev is None:
            # New file: hash it.
            try:
                sha = _sha256_file(abs_path)
            except OSError:
                continue
            diff.new.append(abs_path)
            new_manifest[relpath] = {"mtime": mtime, "size": size, "sha256": sha}
        elif prev.get("mtime") != mtime or prev.get("size") != size:
            # Mtime or size changed: hash to confirm actual content change.
            try:
                sha = _sha256_file(abs_path)
            except OSError:
                new_manifest[relpath] = prev  # keep old entry, skip
                continue
            if sha != prev.get("sha256"):
                diff.changed.append(abs_path)
                new_manifest[relpath] = {"mtime": mtime, "size": size, "sha256": sha}
            else:
                # Same content, just mtime touch (e.g. editor save with no change).
                new_manifest[relpath] = {"mtime": mtime, "size": size, "sha256": sha}
        else:
            # Unchanged.
            new_manifest[relpath] = prev

    # Deleted files (in manifest but not found on disk).
    for relpath in manifest:
        if relpath not in new_manifest:
            diff.deleted.append(relpath)

    diff.skipped_non_text = int(walk_stats.get("skipped_non_text", 0))
    return diff, new_manifest


# ── live status snapshot (read by the HTTP status endpoint) ──────────────────
#
# Stat-only: the loop writes a per-vault snapshot here each tick. The HTTP layer
# reads it directly — it never opens a graph or touches the filesystem itself
# (ADR 0025: status reads must stay cheap and non-blocking).
_WATCH_STATUS: dict[str, dict] = {}


def get_watch_status(vault_path_str: str | None = None) -> dict:
    """Return the live folder-watch status snapshot.

    With no argument, returns the full ``{vault_path: snapshot}`` map (every
    vault that is either enabled or has roots configured). With a vault path,
    returns just that vault's snapshot (or an empty/disabled placeholder if the
    loop hasn't observed it yet)."""
    if vault_path_str is None:
        return dict(_WATCH_STATUS)
    return _WATCH_STATUS.get(
        vault_path_str,
        {
            "enabled": False,
            "roots": [],
            "last_poll_ts": None,
            "watched_file_count": 0,
            "skipped_non_text": 0,
            "pending": [],
            "recent_ingests": [],
            "paused_reason": None,
        },
    )


def _write_status(
    vp_str: str,
    *,
    enabled: bool,
    roots: list[str],
    now: float,
    vs: "_VaultWatchState | None" = None,
    cfg: "FolderWatchConfig | None" = None,
    paused_reason: str | None = None,
) -> None:
    """Build and store one vault's status entry. Pure-ish: only touches the
    module-level snapshot dict, no I/O."""
    pending: list[dict] = []
    watched_file_count = 0
    skipped_non_text = 0
    if vs is not None:
        for file_table in vs.file_states.values():
            watched_file_count += len(file_table)
            for relpath, fs in file_table.items():
                if fs.pending:
                    settling_for_s = max(0.0, now - fs.last_change_seen_at)
                    pending.append({"name": relpath, "settling_for_s": round(settling_for_s, 1)})
        skipped_non_text = sum(vs.skipped_non_text.values())

    prev = _WATCH_STATUS.get(vp_str, {})
    recent_ingests = list(prev.get("recent_ingests", []))

    _WATCH_STATUS[vp_str] = {
        "enabled": enabled,
        "roots": roots,
        "last_poll_ts": now,
        "watched_file_count": watched_file_count,
        "skipped_non_text": skipped_non_text,
        "pending": pending,
        "recent_ingests": recent_ingests[-20:],
        "poll_interval_s": cfg.poll_interval_s if cfg is not None else None,
        "quiet_debounce_s": cfg.quiet_debounce_s if cfg is not None else None,
        "paused_reason": paused_reason,
    }


def _record_ingest(vp_str: str, names: list[str], now: float) -> None:
    entry = _WATCH_STATUS.setdefault(
        vp_str,
        {
            "enabled": True,
            "roots": [],
            "last_poll_ts": now,
            "watched_file_count": 0,
            "pending": [],
            "recent_ingests": [],
        },
    )
    recent = entry.setdefault("recent_ingests", [])
    for name in names:
        recent.append({"name": name, "ts": now})
    entry["recent_ingests"] = recent[-20:]


# ── per-vault watch state (in-memory debounce table) ─────────────────────────


class _VaultWatchState:
    """In-process debounce table for one vault (keyed by vault path)."""

    def __init__(self) -> None:
        # file_states[root_str][relpath_str] = _FileState
        self.file_states: dict[str, dict[str, _FileState]] = {}
        # skipped_non_text[root_str] = count from the latest walk (F12c).
        self.skipped_non_text: dict[str, int] = {}

    def get_root(self, root: str) -> dict[str, _FileState]:
        if root not in self.file_states:
            self.file_states[root] = {}
        return self.file_states[root]


# ── config helpers ────────────────────────────────────────────────────────────


def _load_folder_watch_config(vault_path: Path) -> "FolderWatchConfig":
    from okto_neuron.config import FolderWatchConfig, VaultConfig

    try:
        return VaultConfig.load(vault_path).folder_watch
    except Exception:  # noqa: BLE001
        return FolderWatchConfig()


# ── main watch loop ───────────────────────────────────────────────────────────


def _manifest_for_persist(
    manifest: dict[str, dict],
    new_manifest: dict[str, dict],
    file_table: dict[str, _FileState],
) -> dict[str, dict]:
    """The manifest snapshot that may be persisted RIGHT NOW.

    F5 durability rule: a file whose change is still pending (detected but not
    yet successfully enqueued) keeps its PRE-EDIT manifest entry — a brand-new
    pending file stays absent entirely. A daemon restart inside the debounce
    window then re-detects the edit from the sidecar instead of silently
    losing it. The new sha reaches the sidecar only after its enqueue fires.
    """
    out = dict(new_manifest)
    for relpath, fs in file_table.items():
        if not fs.pending:
            continue
        if relpath in manifest:
            out[relpath] = manifest[relpath]
        else:
            out.pop(relpath, None)
    return out


async def _tick_root(
    state: "ServerState",
    vault_path: Path,
    vp_str: str,
    root: Path,
    cfg: "FolderWatchConfig",
    vs: "_VaultWatchState",
    now: float,
) -> None:
    """One poll tick for one watched root: load the manifest sidecar, diff it
    against disk, update the debounce table, fire settled files into the ingest
    queue, and save the manifest (withholding still-pending edits — see
    :func:`_manifest_for_persist`)."""
    r_str = str(root)
    file_table = vs.get_root(r_str)
    mf_path = _manifest_path(vault_path, root)
    # Every filesystem touch below (manifest read, stat-walk + hashing, the
    # is_file probes, manifest save) runs on the store executor (issue #13);
    # only the in-memory debounce table is updated on the event loop.
    manifest = await store_io(_load_manifest, mf_path)

    # Stat-walk and compute diff.
    try:
        diff, new_manifest = await store_io(
            compute_manifest_diff,
            root,
            manifest,
            recursive=cfg.recursive,
            ignore_globs=cfg.ignore_globs,
            ignore_dir_globs=cfg.ignore_dir_globs,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("folder-watch: diff error for %s", root)
        return
    vs.skipped_non_text[r_str] = diff.skipped_non_text

    # Update debounce table with changed/new files. While an edit is pending
    # the persisted manifest keeps the OLD sha, so the same file re-appears in
    # diff.changed every tick — the debounce clock only bumps when the content
    # actually changed vs what the table already tracks (otherwise a pending
    # file would never settle).
    changed_or_new: set[str] = set()
    for abs_path in diff.changed + diff.new:
        relpath = str(abs_path.relative_to(root))
        changed_or_new.add(relpath)
        entry_st = new_manifest.get(relpath, {})
        new_sha = entry_st.get("sha256", "")
        fs = file_table.get(relpath)
        if fs is None:
            fs = _FileState(
                mtime=entry_st.get("mtime", 0.0),
                size=entry_st.get("size", 0),
                sha256=new_sha,
                last_change_seen_at=now,
                pending=True,
            )
            file_table[relpath] = fs
        elif new_sha != fs.sha256:
            fs.mtime = entry_st.get("mtime", 0.0)
            fs.size = entry_st.get("size", 0)
            fs.sha256 = new_sha
            fs.last_change_seen_at = now
            fs.pending = True

    # Edit-then-undo: a pending file that no longer differs from the persisted
    # manifest (disk sha back to the pre-edit sha) has nothing to ingest.
    #
    # A same-sha match alone is NOT enough: on a hash failure (file
    # transiently unreadable this tick), compute_manifest_diff withholds the
    # OLD pre-edit entry unchanged in new_manifest (see the mtime/size-changed
    # branch above), so entry_now IS manifest[relpath] and their sha trivially
    # matches — that is a stalled re-hash, not a genuine revert. A real undo
    # REWRITES the file, so its on-disk mtime differs from the withheld
    # entry's; require that too, so a transiently-unreadable pending file
    # stays pending instead of being misclassified as undone.
    for relpath, fs in file_table.items():
        if not fs.pending or relpath in changed_or_new:
            continue
        entry_now = new_manifest.get(relpath)
        prev_entry = manifest.get(relpath, {})
        if (
            entry_now is not None
            and entry_now.get("sha256") == prev_entry.get("sha256")
            and entry_now.get("mtime") != prev_entry.get("mtime")
        ):
            fs.pending = False
            fs.sha256 = entry_now.get("sha256", "")
            fs.mtime = entry_now.get("mtime", 0.0)
            fs.size = entry_now.get("size", 0)

    # Drop debounce entries whose file vanished (v1: no graph data deletion).
    # Keyed off new_manifest, which also covers pending NEW files that were
    # deleted before firing — those are withheld from the persisted manifest,
    # so diff.deleted never lists them. A file merely UNREADABLE right now
    # (permissions — absent from new_manifest because hashing failed) is NOT
    # vanished: keep tracking it so it retries when readable again.
    #
    # is_file() raises (not just returns False) when a PARENT directory loses
    # search permission (e.g. chmod 000 on a watched subdir) — that exception
    # must not propagate: it would kill the caller's tick, and (absent the
    # belt-and-braces guard in run_folder_watch) the GLOBAL watch loop task
    # forever. Treat an unreadable-parent error the same as "still present":
    # keep the entry so it retries once access is restored.
    missing = [r for r in file_table if r not in new_manifest]
    if missing:
        for relpath in await store_io(_vanished_relpaths, root, missing):
            file_table.pop(relpath, None)

    # Fire settled files. State flips AFTER the enqueue reports acceptance —
    # a file whose durable copy failed (unreadable source, full disk) stays
    # pending, keeps its old manifest entry, and is retried next tick.
    settled: list[tuple[str, str]] = []
    for relpath, fs in file_table.items():
        reason = _should_ingest_changes(
            now,
            fs.last_change_seen_at,
            fs.last_enqueue_at,
            fs.pending,
            cfg,
        )
        if reason is not None:
            settled.append((relpath, reason))
    present = (
        await store_io(_present_relpaths, root, [relpath for relpath, _ in settled])
        if settled
        else set()
    )
    to_ingest: list[tuple[str, Path]] = []
    for relpath, reason in settled:
        if relpath not in present:
            continue
        abs_path = root / relpath
        _LOG.info("folder-watch: queuing %s (%s)", abs_path.name, reason)
        to_ingest.append((relpath, abs_path))

    if not to_ingest:
        await store_io(
            _save_manifest, mf_path, _manifest_for_persist(manifest, new_manifest, file_table)
        )
        return

    # Enqueue via the ingest queue for this vault; per-path acceptance drives
    # the pending flip. An enqueue exception leaves everything pending (the
    # watcher must survive a bad tick — the next one retries).
    try:
        accepted = await _enqueue_for_vault(
            state, vault_path, [p for _, p in to_ingest], rel_root=root
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("folder-watch: enqueue failed for %s; will retry", root)
        accepted = set()

    resolved = await store_io(_resolved_paths, [abs_path for _, abs_path in to_ingest])
    fired_names: list[str] = []
    for (relpath, abs_path), resolved_path in zip(to_ingest, resolved, strict=True):
        fs = file_table[relpath]
        if resolved_path in accepted:
            fs.last_enqueue_at = now
            fs.pending = False
            fired_names.append(abs_path.name)
        else:
            # A file whose enqueue is never accepted (durable copy keeps
            # failing) must still respect min_interval_s as a retry backoff —
            # otherwise `_should_ingest_changes` sees an unbumped
            # last_enqueue_at forever and re-fires the FULL enqueue attempt
            # every single poll tick (log spam + unbounded copy attempts).
            # Bumping it here on rejection (not just on acceptance) makes the
            # min-interval floor engage for retries too, while `pending`
            # stays True so the file is never dropped.
            fs.last_enqueue_at = now
            _LOG.warning(
                "folder-watch: enqueue not accepted for %s; keeping pending",
                abs_path,
            )
    if fired_names:
        _record_ingest(vp_str, fired_names, now)

    # Persist post-enqueue: fired files carry their new sha; anything still
    # pending keeps the pre-edit entry so a restart re-detects it.
    await store_io(
        _save_manifest, mf_path, _manifest_for_persist(manifest, new_manifest, file_table)
    )


def _vanished_relpaths(root: Path, relpaths: list[str]) -> list[str]:
    """Relpaths that are really gone. ``is_file()`` raises (not just returns
    False) when a PARENT directory loses search permission; that counts as
    "still present" so the entry retries once access is restored."""
    gone: list[str] = []
    for relpath in relpaths:
        try:
            still_present = (root / relpath).is_file()
        except OSError:
            still_present = True
        if not still_present:
            gone.append(relpath)
    return gone


def _present_relpaths(root: Path, relpaths: list[str]) -> set[str]:
    return {relpath for relpath in relpaths if (root / relpath).is_file()}


def _resolved_paths(paths: list[Path]) -> list[str]:
    return [str(path.resolve()) for path in paths]


async def run_folder_watch(state: "ServerState") -> None:
    """Global folder-watch task. Runs for the lifetime of the daemon.

    Polls all registered vaults every ``poll_interval_s``, detects settled
    changes, and enqueues them. Reads config live per tick so changes to
    okto-neuron.yaml take effect without a restart.
    """
    # In-process debounce tables, keyed by vault path string.
    vault_states: dict[str, _VaultWatchState] = {}
    # Per-vault, per-root last-tick time so we respect poll_interval_s.
    last_poll: dict[str, dict[str, float]] = {}  # vault_path -> root -> float
    # Compatibility-only warning state. Direct legacy ``ServerState`` callers
    # still have one active queue; the application daemon uses VaultRuntime and
    # never takes this pause branch.
    inactive_warned: set[str] = set()

    _LOG.debug("folder-watch global task started")

    while True:
        try:
            await asyncio.sleep(WATCH_TICK_S)
        except asyncio.CancelledError:
            _LOG.debug("folder-watch task cancelled")
            return

        if state.draining:
            continue

        # Task 4: guard the ENTIRE poll-tick body. Per-vault/per-root steps are
        # already isolated, but the scaffolding between them (list()/dict pruning,
        # _dedupe_roots, resolved_roots, _write_status) was not — an unguarded
        # exception there would escape the ``while`` body and KILL the global watch
        # task, silently stopping ALL auto-ingest forever. A crashing tick now logs
        # and the loop retries next tick. A cancel mid-tick still propagates (the
        # task is genuinely cancelled — shutdown), so the done-callback in runtime
        # can tell a crash (restart) from a clean stop (leave dead → /health degraded).
        try:
            await _poll_tick(state, vault_states, last_poll, inactive_warned)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOG.exception("folder-watch: unhandled error in poll tick; loop continues")


async def _poll_tick(
    state: "ServerState",
    vault_states: dict[str, "_VaultWatchState"],
    last_poll: dict[str, dict[str, float]],
    inactive_warned: set[str],
) -> None:
    """One full poll tick over every registered vault. Extracted from
    :func:`run_folder_watch` so the loop can wrap it in a single crash guard
    (Task 4). The persistent per-tick tables are owned by the loop and threaded
    in so they survive across ticks."""
    from okto_neuron.vault_registry import list_vaults

    now = time.time()

    # Enumerate all known vaults. Registry scan, config reads, runtime
    # resolution and root probes run on the store executor (issue #13).
    try:
        entries = await store_io(
            list_vaults, current=state.vault_path if state.vault_path else None
        )
    except Exception:  # noqa: BLE001
        return

    for entry in entries:
        vault_path = entry.path
        vp_str = str(vault_path)

        try:
            cfg = await store_io(_load_folder_watch_config, vault_path)
        except Exception:  # noqa: BLE001
            continue

        if not cfg.enabled or not cfg.roots:
            # Still record status for a configured-but-disabled/rootless vault
            # so the UI can show it (it would otherwise vanish from the
            # endpoint entirely) — but only if there's something to show.
            if cfg.roots or cfg.enabled:
                _write_status(
                    vp_str,
                    enabled=cfg.enabled,
                    roots=list(cfg.roots),
                    now=now,
                    vs=vault_states.get(vp_str),
                    cfg=cfg,
                )
            else:
                # Fully off (no roots AND not enabled): drop any stale status entry
                # so its frozen last_poll_ts can't later false-trip the /health
                # ``folder_watch_stalled`` check (Task 4) after an enable→disable.
                _WATCH_STATUS.pop(vp_str, None)
            continue

        multi_runtime = bool(getattr(state, "multi_vault_runtime_enabled", False))
        target_state = (
            await store_io(state.runtime_for, vault_path) if multi_runtime else state
        )
        if multi_runtime and target_state.draining:
            _write_status(
                vp_str,
                enabled=cfg.enabled,
                roots=list(cfg.roots),
                now=now,
                vs=vault_states.get(vp_str),
                cfg=cfg,
                paused_reason="vault runtime is draining for maintenance or deletion",
            )
            continue

        # Preserve the old single-fallback behavior only for direct ServerState
        # test fixtures and compatibility callers. Production resolves an
        # immutable target above, so every registered watch continues.
        is_active = state.vault_path is not None and await store_io(
            _same_resolved_path, vault_path, Path(state.vault_path)
        )
        if not multi_runtime and not is_active:
            if vp_str not in inactive_warned:
                _LOG.warning(
                    "folder-watch: vault %s does not match the legacy fallback; "
                    "its watched roots are paused in direct ServerState mode",
                    vault_path,
                )
                inactive_warned.add(vp_str)
            _write_status(
                vp_str,
                enabled=cfg.enabled,
                roots=list(cfg.roots),
                now=now,
                vs=vault_states.get(vp_str),
                cfg=cfg,
                paused_reason=(
                    "not the legacy fallback — folder-watch ingest paused in "
                    "direct ServerState compatibility mode"
                ),
            )
            continue
        inactive_warned.discard(vp_str)

        if vp_str not in vault_states:
            vault_states[vp_str] = _VaultWatchState()
        if vp_str not in last_poll:
            last_poll[vp_str] = {}

        vs = vault_states[vp_str]
        lp = last_poll[vp_str]

        # Config is re-read live every tick (okto-neuron.yaml can change
        # without a restart), so a root removed from folder_watch.roots
        # must have its per-root debounce/skipped-count entries pruned
        # here — otherwise get_watch_status keeps reporting phantom
        # pending/skipped counts for a root that is no longer watched.
        # Build the keep-set from every CURRENTLY configured root
        # (resolved, matching the keying _tick_root/get_root use) before
        # the poll_interval_s throttle below skips any of them — a root
        # merely due for a later poll this tick must still be kept.
        # Collapse nested/duplicate roots so a subtree is descended once
        # (the "tracking/tracking" double-descent) — the per-root debounce
        # keep-set and the tick loop both work off the deduped list.
        resolved_roots = await store_io(_resolved_roots, list(cfg.roots))
        current_roots = {str(root) for root in resolved_roots}
        for stale in [r for r in vs.file_states if r not in current_roots]:
            vs.file_states.pop(stale, None)
        for stale in [r for r in vs.skipped_non_text if r not in current_roots]:
            vs.skipped_non_text.pop(stale, None)

        for root in resolved_roots:
            r_str = str(root)

            # Respect poll_interval_s.
            if now - lp.get(r_str, 0.0) < cfg.poll_interval_s:
                continue
            lp[r_str] = now

            if not await store_io(root.is_dir):
                _LOG.debug("folder-watch: root not found, skipping: %s", root)
                continue

            # Belt-and-braces: _tick_root already isolates the diff and
            # enqueue steps internally, but an unforeseen exception here
            # (e.g. an OSError from a filesystem edge case not already
            # guarded) must not kill the GLOBAL watch loop task for every
            # vault forever — isolate per-root so one bad root never takes
            # down the others.
            try:
                await _tick_root(target_state, vault_path, vp_str, root, cfg, vs, now)
            except Exception:  # noqa: BLE001
                _LOG.exception("folder-watch: unhandled error ticking root %s; will retry", root)

        _write_status(vp_str, enabled=cfg.enabled, roots=list(cfg.roots), now=now, vs=vs, cfg=cfg)


def _same_resolved_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _resolved_roots(roots: list[str]) -> list[Path]:
    return _dedupe_roots([Path(root_str).expanduser().resolve() for root_str in roots])


async def _enqueue_for_vault(
    state: "ServerState",
    vault_path: Path,
    paths: list[Path],
    *,
    rel_root: Path | None = None,
) -> set[str]:
    """Enqueue ``paths`` to this target runtime's ingest worker.

    In the application daemon ``state`` is the immutable ``VaultRuntime`` for
    ``vault_path``. Direct legacy callers retain their single-active shape.

    Returns the set of resolved source paths the queue ACCEPTED (either a new
    item or a dedup refresh of a queued item). A path missing from the result
    failed its durable copy — the caller keeps it pending and retries.
    """
    from okto_neuron.server import _ingest_queue
    from okto_neuron.server.http import _companion

    if getattr(state, "draining", False):
        raise RuntimeError(f"folder-watch target is draining: {vault_path}")
    target_path = Path(state.vault_path or vault_path).resolve(strict=False)
    requested_path = Path(vault_path).resolve(strict=False)
    if target_path != requested_path:
        raise RuntimeError(
            f"folder-watch target mismatch: runtime={target_path} requested={requested_path}"
        )
    sources = target_path / ".marginalia" / "sources"
    accepted: set[str] = set()
    queued = await _ingest_queue.enqueue_paths_async(
        state, paths, sources, rel_root=rel_root, accepted_srcs=accepted
    )
    # Wake the worker whenever ANYTHING is queued — not only when this enqueue
    # minted new items. A dedup-refreshed enqueue returns [], but the original
    # queued item (or an earlier backlog) still needs a live drain worker.
    if any(i.status == "queued" for i in state.ingest_queue):
        _ingest_queue.ensure_worker(state, _companion)
    if queued:
        _LOG.info(
            "folder-watch: enqueued %d file(s) from %s",
            len(queued),
            vault_path.name,
        )
    return accepted


__all__ = [
    "WATCH_TICK_S",
    "_iter_watch_files",
    "_should_ingest_changes",
    "_tick_root",
    "compute_manifest_diff",
    "run_folder_watch",
    "get_watch_status",
]
