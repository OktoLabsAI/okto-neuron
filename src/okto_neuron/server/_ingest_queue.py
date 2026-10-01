"""Background ingest queue for bulk "point at a folder and rip it" ingestion.

A single in-process worker drains the queue ONE item at a time, holding the
shared writer lock per file and running the synchronous companion in a worker
thread so HTTP polling stays responsive. Writes remain serialized by the lock.
Cancellation is cooperative: it stops at the next safe pre-commit checkpoint;
once an atomic graph commit begins, that short write tail is allowed to finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from okto_neuron.companion import LLMUnavailableError, RememberCancelled
from okto_neuron.server._integrity import IntegrityFenceError, require_write_allowed
from okto_neuron.server._persist_coalesce import PersistCoalescer
from okto_neuron.server._store_io import acquire_off_loop, call_soon_on_loop, job_io, store_io

if TYPE_CHECKING:
    from okto_neuron.server.state import ServerState

_LOG = logging.getLogger("okto_neuron.server.ingest_queue")

# Okto Neuron's trust root is markdown; only text-like sources are ingestible.
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
# Upper bound on a single enqueue request — a backstop against a pathological
# folder (or upload) flooding the in-memory queue.
MAX_ENQUEUE = 2000
# Sidecar JSON that makes the queue restart-durable. Lives under the vault's
# ``.marginalia/`` next to the other derived state.
HISTORY_FILENAME = "ingest-history.json"
HISTORY_VERSION = 1
# Bound the persisted file: keep ALL queued/processing items, but cap retained
# terminal (done/error) items to the most recent N so a long-lived server's
# history never grows without limit.
RETENTION_CAP = 500
# Throttle persistence during a single file's drain: write on stage change and
# every K blocks (plus always on a terminal status) so a 200-block file does not
# fsync the sidecar 200 times.
PERSIST_EVERY_BLOCKS = 10
# The stages whose ``on_progress`` pairs really ARE a blocks_done/blocks_total
# population. ``dedup``/``committing`` tick an item ordinal with an undeclared
# total instead (Companion._emit_substage), so they never write the blocks
# counters.
_BLOCK_POPULATION_STAGES = frozenset({"parsing", "extracting", "embedding"})
# Events are chatty (LLM request/response artifacts above all). Every retained event is
# kept in memory for the UI; the sidecar is written by a coalesced flush (see
# ``request_persist``), not once per event.
MAX_EVENTS_PER_ITEM = 80
MAX_EVENT_TEXT_CHARS = 12_000
MAX_EVENT_LIST_ITEMS = 80
# Persisted byte budget (#37). Body-carrying events (LLM request/response, chunk text,
# dedup/gate candidate lists) were ~88% of a 60 MB history file and the whole file is
# rewritten on every persist. The live inspector still gets the full body while the item
# is in flight; the sidecar, and a finished item, keep only a preview plus the original
# length and a sha256 of the full body.
EVENT_PREVIEW_CHARS = 2_048
MAX_PERSISTED_EVENT_BYTES_PER_ITEM = 64 * 1024
_KEEP_FIRST_EVENTS = 1
_KEEP_LAST_EVENTS = 5
_BODY_EVENT_KINDS = frozenset(
    {
        "llm_request",
        "llm_response",
        "chunks",
        "gate",
        "dedup_judge_batch",
        "dedup_store_exact",
        "dedup_store_judge",
    }
)
# Events other code reads structurally after the fact (retained extraction counts, the
# quality-check script): the byte cap drops these last.
_STRUCTURAL_EVENT_KINDS = frozenset({"extraction_result"})

# Terminal statuses, factored out so retention and rehydrate agree.
_TERMINAL = frozenset({"done", "error", "cancelled"})


@dataclass
class IngestItem:
    """One queued source on its way through the companion."""

    id: str
    name: str
    path: str  # absolute path the worker will remember()
    status: str = "queued"  # queued | processing | done | error | cancelled
    committed: int = 0
    queued: int = 0
    error: str | None = None
    provider_error: str | None = None
    # Within-file progress telemetry (Feature 2). ``stage`` tracks the phase of
    # ``remember`` (queued | parsing | extracting | embedding | dedup |
    # committing | done | error); ``blocks_total``/``blocks_done`` count the
    # per-block extraction loop. Defaults keep every existing constructor valid.
    stage: str = "queued"
    blocks_total: int = 0
    blocks_done: int = 0
    nodes: int = 0
    edges: int = 0
    claims: int = 0
    # Live extraction telemetry. ``nodes``/``edges``/``claims`` above are the
    # final remember() result; these fields advance per extraction_result event.
    extracted_nodes: int = 0
    extracted_edges: int = 0
    extracted_claims: int = 0
    # Fine-grained progress inside the current stage. Extraction already uses
    # block counts; embedding and semantic curation report their own natural
    # units so the UI does not appear frozen after all blocks are extracted.
    stage_progress_label: str | None = None
    stage_progress_done: int = 0
    stage_progress_total: int = 0
    # ADR 0039 T9: a reported ``done > total`` is a telemetry error, not a
    # percentage to clamp. Empty means no violation observed for the current
    # population; the reported counts above are always kept verbatim.
    progress_integrity_error: dict = field(default_factory=dict)
    # ADR 0039 D6: worker lifecycle and technical result quality are separate.
    # Missing on legacy sidecars means unknown, represented by an empty object.
    outcome: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)


def safe_source_filename(raw: str, content: str) -> str:
    """Derive a safe, durable ``.md`` filename under ``.marginalia/sources``.

    Strips path components (no traversal), slugifies, forces a markdown
    extension. With no usable name, mints ``note-<hash8>.md`` from the content
    so repeated pastes never clobber an earlier source's byte-range provenance.
    """
    base = Path(raw or "").name.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    if not stem:
        stem = "note-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    if not stem.lower().endswith((".md", ".markdown")):
        stem = f"{stem}.md"
    return stem


def discover_folder(
    root: Path,
    *,
    recursive: bool = True,
    ignore_globs: list[str] | None = None,
    ignore_dir_globs: list[str] | None = None,
    stats: dict | None = None,
) -> list[Path]:
    """All text-like source files under ``root``, sorted for stable ordering.

    Routed through the folder-watch enumerator so one-shot folder ingest
    (``POST /api/v1/ingest-folder``, ``watch-folder add`` initial ingest)
    prunes internal dirs exactly like the continuous watch loop does. Callers
    with a vault config in hand (the HTTP endpoint) pass its
    ``folder_watch.ignore_globs``/``ignore_dir_globs`` so a customized config
    keeps both enumeration paths identical; ``None`` dir-globs fall back to
    the packaged defaults. ``stats`` receives ``skipped_non_text`` (F12c)."""
    from okto_neuron.config import DEFAULT_IGNORE_DIR_GLOBS
    from okto_neuron.server._folder_watch import _iter_watch_files

    if ignore_dir_globs is None:
        ignore_dir_globs = list(DEFAULT_IGNORE_DIR_GLOBS)
    return _iter_watch_files(
        root,
        recursive=recursive,
        ignore_globs=ignore_globs,
        ignore_dir_globs=ignore_dir_globs,
        stats=stats,
    )


class SupportsRemember(Protocol):
    """The single companion capability the drain worker uses.

    Structural on purpose: production passes a real ``Companion`` while tests
    pass lightweight stubs, and the worker only ever calls ``remember`` with
    these keywords."""

    def remember(
        self,
        source: Any,
        *,
        on_progress: Any = None,
        on_event: Any = None,
        should_cancel: Any = None,
    ) -> Any: ...


def _next_id(state: "ServerState", path: str) -> str:
    """Mint a unique item id from the state-scoped monotonic counter.

    NOT len()-based: deleting terminal items shrinks the queue, and a len-based
    id then collides with a still-present item (the live-run audit observed the
    same id on distinct items). ``rehydrate_queue`` seeds the counter past every
    persisted id."""
    with _ID_LOCK:
        seq = int(getattr(state, "ingest_seq", 0) or 0)
        state.ingest_seq = seq + 1
    return f"{seq}-{hashlib.sha1(path.encode('utf-8')).hexdigest()[:8]}"


_ID_LOCK = threading.Lock()


# ── restart-durable persistence (Feature 1) ───────────────────────────────────
def history_path(state: "ServerState") -> Path:
    """Sidecar JSON for the ingest queue, under the vault's ``.marginalia/``.

    ``ServerState.vault_path`` is optional (no vault selected yet); the queue has
    no home in that state, so this fails loudly instead of building a path from
    ``None``."""
    vault_path = state.vault_path
    if vault_path is None:
        raise ValueError("ingest queue history requires a selected vault")
    return Path(vault_path) / ".marginalia" / HISTORY_FILENAME


def _persist_payload(items: list[IngestItem]) -> dict:
    """Serializable queue snapshot with retention applied.

    Keeps every queued/processing item; caps the most recent ``RETENTION_CAP``
    terminal (done/error) items, dropping the oldest while preserving the
    original interleaved ordering."""
    terminal_positions = [k for k, i in enumerate(items) if i.status in _TERMINAL]
    drop: set[int] = set()
    if len(terminal_positions) > RETENTION_CAP:
        drop = set(terminal_positions[:-RETENTION_CAP])
    kept = [i for k, i in enumerate(items) if k not in drop]
    dicts = []
    for item in kept:
        if item.status in _TERMINAL:
            compact_terminal_events(item)  # the full bodies leave memory with the item
        dicts.append(_persisted_item_dict(item))
    return {"version": HISTORY_VERSION, "items": dicts}


def _body_preview(payload: object) -> dict | None:
    """``{truncated, original_bytes, sha256, preview}`` for a payload above the budget."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(text) <= EVENT_PREVIEW_CHARS:
        return None
    body = text.encode("utf-8")
    return {
        "truncated": True,
        "original_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "preview": text[:EVENT_PREVIEW_CHARS],
    }


def _persisted_event(event: dict) -> dict:
    """The event as written to the sidecar: a preview in place of an over-budget body."""
    preview = event.get("body_preview")
    if preview is None:
        return event
    return {k: (preview if k == "payload" else v) for k, v in event.items() if k != "body_preview"}


def _event_bytes(event: dict) -> int:
    return len(json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str))


def _cap_event_bytes(events: list[dict]) -> list[dict]:
    """At most ``MAX_PERSISTED_EVENT_BYTES_PER_ITEM`` of events, oldest dropped first.

    The first event and the last five always stay; structural events
    (``extraction_result``) go only after every other kind.
    """
    sizes = [_event_bytes(e) for e in events]
    total = sum(sizes)
    if total <= MAX_PERSISTED_EVENT_BYTES_PER_ITEM:
        return events
    droppable = range(_KEEP_FIRST_EVENTS, max(_KEEP_FIRST_EVENTS, len(events) - _KEEP_LAST_EVENTS))
    order = [i for i in droppable if events[i].get("kind") not in _STRUCTURAL_EVENT_KINDS]
    order += [i for i in droppable if events[i].get("kind") in _STRUCTURAL_EVENT_KINDS]
    dropped: set[int] = set()
    for index in order:
        if total <= MAX_PERSISTED_EVENT_BYTES_PER_ITEM:
            break
        dropped.add(index)
        total -= sizes[index]
    return [e for i, e in enumerate(events) if i not in dropped]


def _persisted_events(events: list[dict]) -> list[dict]:
    return _cap_event_bytes([_persisted_event(e) for e in list(events)])


def compact_terminal_events(item: IngestItem) -> None:
    """Cut a finished item's events to the persisted form, in memory too."""
    if any("body_preview" in e for e in item.events) or _needs_cap(item.events):
        item.events[:] = _persisted_events(item.events)


def _needs_cap(events: list[dict]) -> bool:
    return len(events) > _KEEP_FIRST_EVENTS + _KEEP_LAST_EVENTS and (
        sum(_event_bytes(e) for e in events) > MAX_PERSISTED_EVENT_BYTES_PER_ITEM
    )


def _persisted_item_dict(item: IngestItem) -> dict:
    """``asdict`` without deep-copying the (large) live events."""
    data = {
        f.name: copy.deepcopy(getattr(item, f.name)) for f in fields(item) if f.name != "events"
    }
    data["events"] = _persisted_events(item.events)
    return data


def _event(kind: str, summary: str, payload: dict | None = None) -> dict:
    event = {
        "ts": time.time(),
        "kind": kind,
        "summary": summary,
        "payload": _sanitize_event_value(payload or {}),
    }
    if kind in _BODY_EVENT_KINDS:
        preview = _body_preview(event["payload"])
        if preview is not None:
            event["body_preview"] = preview
    return event


def record_event(
    state: "ServerState",
    item: IngestItem,
    kind: str,
    summary: str,
    payload: dict | None = None,
    *,
    persist_now: bool = True,
) -> None:
    """Append a bounded per-item artifact/event for the ingest inspector."""
    item.events.append(_event(kind, summary, payload))
    if len(item.events) > MAX_EVENTS_PER_ITEM:
        del item.events[: len(item.events) - MAX_EVENTS_PER_ITEM]
    if persist_now:
        persist(state)


def _sanitize_event_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_EVENT_TEXT_CHARS:
            return value
        omitted = len(value) - MAX_EVENT_TEXT_CHARS
        return value[:MAX_EVENT_TEXT_CHARS] + f"\n...[truncated {omitted} chars]"
    if isinstance(value, dict):
        return {str(k): _sanitize_event_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        kept = [_sanitize_event_value(v) for v in list(value)[:MAX_EVENT_LIST_ITEMS]]
        if len(value) > MAX_EVENT_LIST_ITEMS:
            kept.append({"truncated": len(value) - MAX_EVENT_LIST_ITEMS})
        return kept
    return _sanitize_event_value(str(value))


def _extraction_event_counts(payload: object) -> tuple[int, int, int]:
    """Count node, topology-edge, and literal-claim candidates in one event."""
    if not isinstance(payload, dict):
        return (0, 0, 0)
    nodes_raw = payload.get("nodes")
    edges_raw = payload.get("edges")
    claims_raw = payload.get("claims")
    nodes = len(nodes_raw) if isinstance(nodes_raw, list) else 0
    edges = 0
    claims = len(claims_raw) if isinstance(claims_raw, list) else 0
    if isinstance(edges_raw, list):
        for edge in edges_raw:
            if isinstance(edge, dict) and edge.get("dst_literal") is not None:
                claims += 1
            else:
                edges += 1
    return (nodes, edges, claims)


def _retained_extraction_counts(events: list[dict]) -> tuple[int, int, int]:
    nodes = edges = claims = 0
    for event in events:
        if event.get("kind") != "extraction_result":
            continue
        n, e, c = _extraction_event_counts(event.get("payload"))
        nodes += n
        edges += e
        claims += c
    return (nodes, edges, claims)


def persist(state: "ServerState") -> None:
    """Atomically write the queue to the sidecar (temp file + ``os.replace``).

    Local-only queue history: records status/progress plus bounded inspector
    events. Those events may include chunk text and LLM request/response bodies.
    Best-effort — a persistence failure must never abort an in-flight ingest, so
    OS errors are swallowed (the queue stays correct in memory). Writers are
    serialized and snapshot INSIDE the lock, so whichever write lands last
    carries the newest queue even when store-executor threads persist
    concurrently (issue #13)."""
    _coalescer(state).flushed()  # this write covers everything marked dirty so far
    with _PERSIST_LOCK:
        _persist_locked(state)


_PERSIST_LOCK = threading.Lock()
_COALESCER_LOCK = threading.Lock()


def _coalescer(state: "ServerState") -> PersistCoalescer:
    """The queue's coalescer (one per state/runtime), created on first use."""
    coalescer = getattr(state, "_ingest_persist_coalescer", None)
    if coalescer is None:
        with _COALESCER_LOCK:
            coalescer = getattr(state, "_ingest_persist_coalescer", None)
            if coalescer is None:
                coalescer = PersistCoalescer(lambda: persist(state), name="ingest-queue")
                try:
                    state._ingest_persist_coalescer = coalescer  # type: ignore[attr-defined]
                except AttributeError:
                    pass
    return coalescer


def request_persist(state: "ServerState") -> None:
    """Mark the queue dirty; ONE background flush writes it within a couple of seconds.

    For progress and event chatter only. A crash may lose up to the flush interval of
    progress EVENTS, never a state transition: enqueue, an item's start and terminal
    status, cancel, retry, delete and receipts still call :func:`persist` directly.
    Cheap and safe from any thread, including under the companion's event lock.
    """
    _coalescer(state).mark_dirty()


def shutdown_flush(state: "ServerState") -> bool:
    """Stop the background flush and write once more if anything is pending."""
    return _coalescer(state).close()


def _persist_locked(state: "ServerState") -> None:
    path = history_path(state)
    # Snapshot the list first: the drain worker runs ``remember`` off the event
    # loop and may append/mutate concurrently, so iterate a stable copy.
    payload = _persist_payload(list(state.ingest_queue))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp per writer: a throttled on_progress persist runs in the
        # to_thread drain worker while an HTTP enqueue persists on the event
        # loop — a shared temp name would let the two interleave their writes
        # and os.replace a half-written file into place. mkstemp gives each
        # caller its own fd+name in the same dir, so the replace stays atomic.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2))
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return
    except OSError:
        return


def _migrate_loaded_events(item: IngestItem) -> int:
    """Bring a sidecar written before the byte budget (#37) to the persisted form.

    Body-carrying events with a full payload get their preview computed; a finished
    item is then cut to previews and the per-item budget at once, so the next persist
    rewrites a small file.
    """
    events = item.events if isinstance(item.events, list) else []
    migrated = 0
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in _BODY_EVENT_KINDS:
            continue
        payload = event.get("payload")
        if "body_preview" in event or (
            isinstance(payload, dict) and payload.get("truncated") is True
        ):
            continue
        preview = _body_preview(payload)
        if preview is not None:
            event["body_preview"] = preview
            migrated += 1
    if item.status in _TERMINAL:
        compact_terminal_events(item)
    return migrated


def rehydrate_queue(state: "ServerState") -> None:
    """Restore ``state.ingest_queue`` from the sidecar on server startup.

    Items left ``processing`` at load are crash-interrupted. ``remember`` is
    content-hash idempotent (deterministic Block/Claim ids, ``get_node`` guards,
    and ``reconcile_against_store`` collapsing exact matches into already-
    committed nodes), so re-running it cannot duplicate — they are reset to
    ``queued`` for the drain worker to retry. Unknown/extra keys are ignored so
    an older sidecar still loads."""
    path = history_path(state)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    known = {f.name for f in fields(IngestItem)}
    restored: list[IngestItem] = []
    migrated_bodies = 0
    for entry in data.get("items", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        kwargs = {k: v for k, v in entry.items() if k in known}
        if not {"id", "name", "path"} <= set(kwargs):
            continue
        try:
            item = IngestItem(**kwargs)
        except TypeError:
            continue
        if item.status == "processing":
            item.status = "queued"
            item.stage = "queued"
            item.stage_progress_label = None
            item.stage_progress_done = 0
            item.stage_progress_total = 0
            item.outcome = {}
        migrated_bodies += _migrate_loaded_events(item)
        restored.append(item)
    state.ingest_queue = restored
    if migrated_bodies:
        # One-time and permanent: the full bodies of a legacy sidecar are cut to
        # previews here. Say so, with the sizes, so an operator can see it happen.
        before = len(raw.encode("utf-8"))
        persist(state)
        _LOG.info(
            "ingest history migrated to previews for %s: %d bytes -> %d bytes "
            "(%d event bodies cut; full bodies remain only in any earlier backup)",
            Path(state.vault_path).name,
            before,
            path.stat().st_size if path.exists() else 0,
            migrated_bodies,
        )
    # Seed the id counter past every persisted id so a restart (or vault
    # switch) never mints an id that collides with a rehydrated item.
    seq = int(getattr(state, "ingest_seq", 0) or 0)
    for item in restored:
        prefix = item.id.split("-", 1)[0]
        if prefix.isdigit():
            seq = max(seq, int(prefix) + 1)
    state.ingest_seq = seq


def _sanitize_rel_component(component: str) -> str:
    """One relpath component made durable-copy-safe: slugified, traversal
    neutralized, never empty. Unlike :func:`safe_source_filename` it does NOT
    force a ``.md`` suffix (directory components and non-md text files keep
    their shape).

    COLLISION-PROOF (review finding): sanitization is lossy ("notes 1.md" and
    "notes-1.md" both slug to "notes-1.md"), so any LOSSY component carries a
    short hash of its original spelling — distinct raw components can never
    share a durable path, while clean names stay readable verbatim."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", component).strip("-")
    if slug in ("", ".", ".."):
        return "_" + hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
    if slug != component:
        digest = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
        stem, dot, suffix = slug.rpartition(".")
        if dot and stem:
            return f"{stem}-{digest}.{suffix}"
        return f"{slug}-{digest}"
    return slug


def non_clobbering_target(target: Path, content: str) -> Path:
    """``target``, or a content-hash-suffixed sibling when it already holds
    DIFFERENT bytes. A Block's provenance anchors to the source path, so
    overwriting one source with another silently corrupts the first document's
    byte ranges. Identical content stays idempotent and reuses the same file.
    Mirrors ``runtime._persist_pasted_source``."""
    if not target.exists():
        return target
    try:
        existing: str | None = target.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing == content:
        return target
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return target.with_name(f"{target.stem}-{digest}{target.suffix or '.md'}")


def upload_rel_parts(raw_name: str) -> list[str]:
    """A client-supplied upload name → the relative path components it maps to.

    THE single normalization for an uploaded name, shared by
    :func:`upload_target_path` (where the bytes land) and
    :func:`classify_upload_name` (whether they may land at all). Sharing it is
    the invariant: the path that is JUDGED is always the path that is WRITTEN,
    so a hostile name can never be judged as one path and then written as
    another.

    A CLEAN relative name keeps its folder structure (the browser sends
    ``webkitRelativePath`` for a dropped folder). An absolute or traversal-shaped
    name is hostile input rather than folder structure, so it collapses to the
    bare basename — mirroring ``../../../etc/passwd`` into ``sources/etc/`` would
    be safe but nonsensical. An empty/unnamed upload yields no components at
    all; :func:`safe_source_filename` mints a content-hashed name for it."""
    raw = (raw_name or "").replace("\\", "/")
    parts = [c for c in raw.split("/") if c != ""]
    if raw.startswith("/") or any(c in (".", "..") for c in parts):
        parts = parts[-1:]
    return parts


def classify_upload_name(
    raw_name: str,
    *,
    ignore_globs: list[str] | None = None,
    ignore_dir_globs: list[str] | None = None,
) -> str | None:
    """Source-selection verdict for ONE uploaded name: ``None`` = ingestible,
    else a reason code from ``_folder_watch``.

    The batch/upload counterpart of :func:`discover_folder`: that one walks a
    root and applies the policy per file, this one applies the same policy to a
    name the client already enumerated. Both end in
    ``_folder_watch.classify_source_relpath`` — ADR 0025's one-policy rule, and
    ADR 0026's requirement that the two source selections agree.

    ``ignore_dir_globs=None`` falls back to the packaged defaults exactly as
    :func:`discover_folder` does, so a vault whose config cannot be read still
    gets dot-directory pruning instead of silently losing the whole policy.
    An unnamed upload has no path to judge and is accepted: it is materialized
    under a minted ``note-<hash8>.md``."""
    from okto_neuron.config import DEFAULT_IGNORE_DIR_GLOBS
    from okto_neuron.server._folder_watch import classify_source_relpath

    if ignore_dir_globs is None:
        ignore_dir_globs = list(DEFAULT_IGNORE_DIR_GLOBS)
    parts = upload_rel_parts(raw_name)
    if not parts:
        return None
    return classify_source_relpath(
        Path(*parts),
        ignore_globs=ignore_globs,
        ignore_dir_globs=ignore_dir_globs,
    )


def upload_target_path(sources_dir: Path, raw_name: str, content: str) -> Path:
    """Durable copy path for an UPLOADED file (drag-drop batch, paste/write).

    The browser sends ``webkitRelativePath`` for a dropped folder
    (``BulkImport.tsx``), so a multi-component name mirrors that tree under
    ``sources`` the way :func:`durable_copy_path` mirrors a watched root
    (ADR 0025 F11). Uploads used to bypass that entirely:
    :func:`safe_source_filename` keeps only ``Path(raw).name``, so
    ``cnpj/guias/catalogo.md`` and ``cnpj/husky/catalogo.md`` both resolved to
    ``sources/catalogo.md`` and the second overwrote the first — one real vault
    lost 23 of 166 documents that way, and a case-insensitive filesystem lost
    ``catalogo.md`` to ``CATALOGO.md`` on top of it.

    Components are sanitized with the collision-proof
    :func:`_sanitize_rel_component`; ``.``/``..`` are dropped so an upload can
    never escape ``sources``. A residual same-path/different-content clash falls
    through to :func:`non_clobbering_target`. Normalization is shared with
    :func:`classify_upload_name` via :func:`upload_rel_parts`, so what the
    source-selection policy judges is exactly what gets written."""
    parts = upload_rel_parts(raw_name)
    if len(parts) > 1:
        dirs = [_sanitize_rel_component(c) for c in parts[:-1]]
        target = sources_dir.joinpath(*dirs, safe_source_filename(parts[-1], content))
    else:
        target = sources_dir / safe_source_filename(raw_name, content)
    return non_clobbering_target(target, content)


def durable_copy_path(sources_dir: Path, src: Path, rel_root: Path | None) -> Path:
    """Where ``src``'s durable copy lives under ``.marginalia/sources`` (F11).

    With a watched/ingest ``rel_root``: ``<root-sha16>/<sanitized relpath>`` —
    the copy mirrors the source tree, so a file's durable path (and therefore
    its graph Document identity) is STABLE and readable in provenance; the
    root-key prefix matches the manifest-sidecar keying convention. Without a
    root (uploads, ad-hoc paths): the legacy flat ``<stem>-<pathhash8><suffix>``
    name."""
    src_abs = src.resolve()
    if rel_root is not None:
        try:
            rel = src_abs.relative_to(Path(rel_root).resolve())
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            root_key = hashlib.sha256(str(Path(rel_root).resolve()).encode("utf-8")).hexdigest()[
                :16
            ]
            parts = [_sanitize_rel_component(c) for c in rel.parts]
            return sources_dir.joinpath(root_key, *parts)
    digest = hashlib.sha256(str(src_abs).encode("utf-8")).hexdigest()[:8]
    return sources_dir / safe_source_filename(f"{src.stem}-{digest}{src.suffix}", "")


def enqueue_paths(
    state: "ServerState",
    paths: list[Path],
    sources_dir: Path,
    *,
    rel_root: Path | None = None,
    accepted_srcs: set[str] | None = None,
    stats: dict | None = None,
) -> list[IngestItem]:
    """Queue files discovered under a folder. Each is copied into the durable
    ``.marginalia/sources`` dir (the vault is the trust root, and ``remember``
    re-validates that every source path lives under the vault root) — the
    display name keeps the original filename, the on-disk copy is uniquified by
    a short hash of the source path so same-named files in different subfolders
    never clobber one another.

    DEDUP: a path that already has a ``queued`` item gets its durable copy
    refreshed (freshest bytes win — the eventual single drain reads the latest
    content) instead of a second queue entry; the live run showed one file
    queued 11×, starving everything behind it. A path currently ``processing``
    still appends a fresh item: the in-flight drain reads the pre-edit bytes,
    the new item picks up the edit after it. Enqueue and drain status flips
    both happen on the event loop, so the queued-check cannot race the worker.

    ``accepted_srcs`` (optional out-param) collects the resolved SOURCE path of
    every file the queue accepted — new item or dedup refresh. The folder
    watcher uses it to keep a failed copy pending for retry instead of
    silently dropping the edit. ``stats`` receives ``refreshed`` — the count
    of dedup refreshes — so callers can tell "everything was already queued"
    apart from "nothing enqueued".
    """
    if getattr(state, "draining", False):
        raise RuntimeError("vault runtime is draining; cannot enqueue ingest work")
    copied = _copy_sources(paths, sources_dir, rel_root)
    stats = stats if stats is not None else {}
    items = _register_copied(state, copied, accepted_srcs=accepted_srcs, stats=stats)
    if items or stats.get("refreshed"):
        persist(state)
    return items


async def enqueue_paths_async(
    state: "ServerState",
    paths: list[Path],
    sources_dir: Path,
    *,
    rel_root: Path | None = None,
    accepted_srcs: set[str] | None = None,
    stats: dict | None = None,
) -> list[IngestItem]:
    """:func:`enqueue_paths` for event-loop callers (issue #13).

    The durable copies and the sidecar write run on the store executor; the
    dedup check and the append stay on the loop, where the drain worker flips
    statuses, so the queued-check still cannot race the worker.
    """
    if getattr(state, "draining", False):
        raise RuntimeError("vault runtime is draining; cannot enqueue ingest work")
    copied = await store_io(_copy_sources, paths, sources_dir, rel_root)
    stats = stats if stats is not None else {}
    items = _register_copied(state, copied, accepted_srcs=accepted_srcs, stats=stats)
    if items or stats.get("refreshed"):
        await store_io(persist, state)
    return items


def _copy_sources(
    paths: list[Path], sources_dir: Path, rel_root: Path | None
) -> list[tuple[Path, str, str]]:
    """Copy each readable source into the durable sources dir.

    Returns ``(source, resolved source path, resolved durable copy path)`` for
    every file copied; an unreadable file is skipped rather than aborting the
    whole batch."""
    import shutil

    sources_dir.mkdir(parents=True, exist_ok=True)
    copied: list[tuple[Path, str, str]] = []
    for p in paths:
        src_abs = str(p.resolve())
        target = durable_copy_path(sources_dir, p, rel_root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        except OSError:
            continue  # unreadable file — skip rather than abort the whole batch
        copied.append((p, src_abs, str(target.resolve())))
    return copied


def _register_copied(
    state: "ServerState",
    copied: list[tuple[Path, str, str]],
    *,
    accepted_srcs: set[str] | None,
    stats: dict,
) -> list[IngestItem]:
    """Dedup-or-append already-copied sources; no I/O."""
    items: list[IngestItem] = []
    refreshed = 0
    for p, src_abs, abs in copied:
        existing = next(
            (i for i in state.ingest_queue if i.path == abs and i.status == "queued"),
            None,
        )
        if existing is not None:
            record_event(
                state,
                existing,
                "refreshed",
                "Source changed again before ingest; durable copy refreshed",
                {"source": src_abs},
                persist_now=False,
            )
            refreshed += 1
            if accepted_srcs is not None:
                accepted_srcs.add(src_abs)
            continue
        item = IngestItem(id=_next_id(state, abs), name=p.name, path=abs)
        state.ingest_queue.append(item)
        items.append(item)
        if accepted_srcs is not None:
            accepted_srcs.add(src_abs)
    stats["refreshed"] = refreshed
    return items


def enqueue_uploads(
    state: "ServerState", files: list[tuple[str, str]], sources_dir: Path
) -> list[IngestItem]:
    """Materialize uploaded ``(filename, content)`` pairs to the durable sources
    dir (trust root), then queue them."""
    if getattr(state, "draining", False):
        raise RuntimeError("vault runtime is draining; cannot enqueue ingest work")
    items = _register_uploads(state, _write_uploads(files, sources_dir))
    if items:
        persist(state)
    return items


async def enqueue_uploads_async(
    state: "ServerState", files: list[tuple[str, str]], sources_dir: Path
) -> list[IngestItem]:
    """:func:`enqueue_uploads` for event-loop callers: file writes and the
    sidecar write run on the store executor, the append stays on the loop."""
    if getattr(state, "draining", False):
        raise RuntimeError("vault runtime is draining; cannot enqueue ingest work")
    written = await store_io(_write_uploads, files, sources_dir)
    items = _register_uploads(state, written)
    if items:
        await store_io(persist, state)
    return items


def _write_uploads(files: list[tuple[str, str]], sources_dir: Path) -> list[tuple[str, str]]:
    """Materialize uploads; returns ``(display name, resolved path)`` per file."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, str]] = []
    for raw_name, content in files:
        target = upload_target_path(sources_dir, raw_name, content)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append((target.name, str(target.resolve())))
    return written


def _register_uploads(state: "ServerState", written: list[tuple[str, str]]) -> list[IngestItem]:
    items: list[IngestItem] = []
    for name, abs in written:
        item = IngestItem(id=_next_id(state, abs), name=name, path=abs)
        state.ingest_queue.append(item)
        items.append(item)
    return items


def record_completed(
    state: "ServerState",
    *,
    name: str,
    path: str,
    committed: int = 1,
    stage: str = "stored",
    persist_now: bool = True,
) -> IngestItem:
    """Record an already-completed deterministic store in the durable queue.

    REST ``/add`` writes a Document+Block directly (NO LLM extraction — the
    deliberate add-vs-remember split), bypassing the drain worker. This appends
    a terminal item AFTER the fact so the store is visible in
    ``/api/v1/ingest-queue`` and the UI. The retired MCP ``kg_add`` tool once
    used the same helper; the current MCP write surface uses ``remember``.
    ``stage`` is
    ``"stored"`` (not ``"done"``) so the UI can tell a deterministic store from a
    full extraction; ``status`` stays ``"done"`` so summary counts it as done.
    """
    item = IngestItem(
        id=_next_id(state, path),
        name=name,
        path=path,
        status="done",
        stage=stage,
        committed=committed,
        queued=0,
        blocks_total=0,
        blocks_done=0,
        outcome={"quality": "not_applicable"},
    )
    state.ingest_queue.append(item)
    # ADR 0009 P4: a deterministic REST /add store is in-process ingest activity
    # too; signal the continuous curation scheduler.
    _note_ingest(state)
    if persist_now:
        persist(state)
    return item


def _note_ingest(state: "ServerState", at: float | None = None) -> float:
    """Record activity on a VaultRuntime or the legacy ServerState shape."""
    note = getattr(state, "note_ingest", None)
    if callable(note):
        # Only VaultRuntime declares note_ingest, so the attribute is untyped
        # here; a runtime shape that returns a non-timestamp is a programming
        # error, not something to coerce silently.
        noted = note(at)
        if not isinstance(noted, (int, float)):
            raise TypeError("note_ingest() must return an epoch timestamp")
        return float(noted)
    observed = time.time() if at is None else float(at)
    state.last_ingest_at = observed
    by_vault = getattr(state, "last_ingest_at_by_vault", None)
    if isinstance(by_vault, dict) and state.vault_path is not None:
        by_vault[str(Path(state.vault_path).resolve(strict=False))] = observed
    return observed


def summary(state: "ServerState") -> dict:
    """Queue summary counts only: one pass, no per-item payloads (status hot path)."""
    items = state.ingest_queue
    counts = Counter(i.status for i in items)
    return {
        "total": len(items),
        "queued": counts["queued"],
        "processing": counts["processing"],
        "done": counts["done"],
        "error": counts["error"],
        "cancelled": counts["cancelled"],
        "active": state.ingest_worker_active,
        "cancel_requested": bool(
            state.ingest_worker_active and getattr(state, "ingest_cancel_requested", False)
        ),
    }


def snapshot(state: "ServerState") -> dict:
    """Queue + summary counts for the UI poll."""
    return {
        "status": "ok",
        "vault": _vault_payload(state),
        "summary": summary(state),
        "items": [_item_payload(i, include_events=False) for i in state.ingest_queue],
    }


def _graph_receipt_state(store: object, path: str) -> dict:
    """Live check: does the graph actually hold ``path``'s Document + a Block?

    Task #13: ``ingest-history.json`` is a sidecar written by the worker as it
    goes — it records what ``remember()`` reported at the time, not what
    currently lives in the graph. A crash between a semantic commit and its
    next checkpoint (task #12) can leave the sidecar saying ``done`` +
    ``receipts_complete`` while the recovered graph is missing that content
    entirely (the incident this fixes: 17/17 done + verified, empty graph).
    Document ids are deterministic (``sha256("document", resolved_path)``),
    so this needs no extra index — a direct ``get_node`` plus a bounded scan
    for one matching ``Block``. Returns ``{"checked": False}`` if the store
    doesn't support these lookups (defensive against lightweight test stubs).
    """
    get_node = getattr(store, "get_node", None)
    list_nodes = getattr(store, "list_nodes", None)
    if not callable(get_node) or not callable(list_nodes):
        return {"checked": False}
    from okto_neuron.ingest.markdown import sha256_hex

    resolved = str(Path(path).expanduser().resolve(strict=False))
    document_id = sha256_hex("document", resolved)
    document = get_node(document_id)
    document_present = document is not None and document.type == "Document"
    block_present = False
    if document_present:
        for node in list_nodes("Block"):
            facets = node.facets if isinstance(node.facets, dict) else {}
            if facets.get("source_path") == resolved:
                block_present = True
                break
    return {
        "checked": True,
        "status": "verified" if document_present and block_present else "graph_missing",
        "document_present": document_present,
        "block_present": block_present,
    }


def verify_receipt(state: "ServerState", item: IngestItem) -> dict:
    """Re-check a terminal ``done`` item's outcome against the LIVE graph.

    Read-only from the caller's point of view except for correcting a stale
    claim: when the graph disagrees with the sidecar (Document/Block absent),
    the item's ``outcome`` is durably corrected in place — ``quality`` becomes
    ``"graph_missing"``, ``receipts_complete`` is forced ``False``, and
    ``retryable`` is set explicitly ``True`` so ``retry_item`` (which already
    honors an explicit ``outcome["retryable"]``) can re-queue it through the
    normal API with no further plumbing.

    A no-op for anything not currently ``status == "done"``, for a vault/store
    that can't be reached yet, and — deliberately — for a ``done`` item whose
    outcome carries no ``quality`` claim at all: there is no "receipts
    complete" claim to cross-check for an item that never asserted one (e.g.
    a bare crash-recovery placeholder), so nothing here corrects it. This
    mirrors ``_item_retryable``'s own "no quality means legacy/unclassified"
    treatment of an empty outcome.
    """
    if not _receipt_check_needed(item):
        return {}
    verification = _graph_receipt_state(_receipt_store(state), item.path)
    return _apply_receipt(state, item, verification, persist_now=True)


async def verify_receipt_async(state: "ServerState", item: IngestItem) -> dict:
    """:func:`verify_receipt` for event-loop callers (issue #13): the graph
    lookups and the sidecar write run on the store executor; the item itself is
    only updated on the loop, where the drain worker also updates items."""
    if not _receipt_check_needed(item):
        return {}
    verification = await store_io(_graph_receipt_state, _receipt_store(state), item.path)
    before = item.outcome.get("graph_verification") if isinstance(item.outcome, dict) else None
    outcome = _apply_receipt(state, item, verification, persist_now=False)
    if outcome and outcome.get("graph_verification") != before:
        await store_io(persist, state)
    return outcome


def _receipt_check_needed(item: IngestItem) -> bool:
    if item.status != "done":
        return False
    outcome_in = item.outcome if isinstance(item.outcome, dict) else {}
    return bool(str(outcome_in.get("quality") or "").strip())


def _receipt_store(state: "ServerState") -> object:
    vault = getattr(state, "vault", None)
    return getattr(vault, "store", None)


def _apply_receipt(
    state: "ServerState", item: IngestItem, verification: dict, *, persist_now: bool
) -> dict:
    if not verification.get("checked"):
        return {}
    if not _receipt_check_needed(item):
        return {}
    outcome = item.outcome
    previous = outcome.get("graph_verification")
    changed = previous != verification
    outcome["graph_verification"] = verification
    if verification["status"] == "graph_missing":
        outcome["quality"] = "graph_missing"
        outcome["receipts_complete"] = False
        outcome["retryable"] = True
    item.outcome = outcome
    if changed:
        if verification["status"] == "graph_missing":
            record_event(
                state,
                item,
                "graph_verification_failed",
                (
                    "Sidecar reported this item done, but the live graph is "
                    "missing its Document/Block nodes"
                ),
                verification,
                persist_now=False,
            )
        if persist_now:
            persist(state)
    return outcome


def item_detail(state: "ServerState", item_id: str) -> dict | None:
    item = next((i for i in state.ingest_queue if i.id == item_id), None)
    if item is None:
        return None
    verify_receipt(state, item)
    return _item_detail_payload(state, item)


async def item_detail_async(state: "ServerState", item_id: str) -> dict | None:
    """:func:`item_detail` with the receipt check off the event loop."""
    item = next((i for i in state.ingest_queue if i.id == item_id), None)
    if item is None:
        return None
    await verify_receipt_async(state, item)
    return _item_detail_payload(state, item)


def _item_detail_payload(state: "ServerState", item: IngestItem) -> dict:
    return {
        "status": "ok",
        "vault": _vault_payload(state),
        "item": _item_payload(item, include_events=True),
    }


def _cancel_queued_items(state: "ServerState") -> int:
    """Mark every currently queued item cancelled and return the changed count."""
    cancelled = 0
    for item in state.ingest_queue:
        if item.status == "queued":
            item.status = "cancelled"
            item.stage = "cancelled"
            item.error = "cancelled by user"
            record_event(
                state,
                item,
                "cancelled",
                "Queued file cancelled before processing",
                persist_now=False,
            )
            cancelled += 1
    return cancelled


def cancel(state: "ServerState", *, persist_now: bool = True) -> dict:
    """Request a cooperative bulk-ingest stop.

    Queued files become terminal immediately. The processing file stops at the
    next safe pre-commit checkpoint, normally after its current LLM call; an
    atomic graph commit already in progress is allowed to finish.
    """
    already_requested = bool(getattr(state, "ingest_cancel_requested", False))
    state.ingest_cancel_requested = True
    cancelled = _cancel_queued_items(state)
    # The per-call predicate registries target only model calls owned by this
    # cancelled ingest. This makes Stop responsive during both a long CLI call
    # and a hosted LiteLLM request without disturbing unrelated ask/curation work.
    from okto_neuron.llm._cli_provider import cancel_requested_cli_processes
    from okto_neuron.llm._litellm_process import cancel_requested_litellm_calls

    cancel_requested_cli_processes()
    cancel_requested_litellm_calls()
    if not already_requested:
        for item in state.ingest_queue:
            if item.status != "processing":
                continue
            record_event(
                state,
                item,
                "cancel_requested",
                "Stop requested; winding down at the next safe checkpoint",
                persist_now=False,
            )
    if not state.ingest_worker_active:
        state.ingest_cancel_requested = False
    if persist_now:
        persist(state)
    snap = snapshot(state)
    snap["cancelled"] = cancelled
    return snap


def _find_item(state: "ServerState", item_id: str) -> IngestItem | None:
    return next((i for i in state.ingest_queue if i.id == item_id), None)


def _item_retryable(item: IngestItem) -> bool:
    """Mirror the public/UI retry contract from durable technical evidence."""

    outcome = item.outcome if isinstance(item.outcome, dict) else {}
    quality = str(outcome.get("quality") or "").strip()
    explicit_retryable = outcome.get("retryable")
    if isinstance(explicit_retryable, bool):
        return explicit_retryable
    failed_units = outcome.get("failed_units")
    if isinstance(failed_units, list):
        retry_flags = [
            unit.get("retryable")
            for unit in failed_units
            if isinstance(unit, dict) and isinstance(unit.get("retryable"), bool)
        ]
        if any(flag is True for flag in retry_flags):
            return True
        if retry_flags:
            return False
        # An explicit empty list is a classified, non-retryable result (for
        # example an integrity fence), not legacy/unclassified evidence.
        return False
    if not quality:
        return item.status == "error" or (item.status == "done" and bool(item.provider_error))
    # Legacy and pre-classification internal failures carried only
    # {"quality": "failed"}. Operator retry is safe and explicit: the source
    # path is deduplicated, graph writes are idempotent, and current policy
    # fingerprints decide which stage evidence may be reused.
    if quality == "failed" and item.status == "error":
        return True
    return quality == "partial" and bool(item.provider_error)


def retry_item(
    state: "ServerState",
    item_id: str,
    *,
    verify: bool = True,
    persist_now: bool = True,
) -> tuple[IngestItem | None, str | None]:
    """Re-enqueue a failed or provider-degraded item for the drain worker.

    New outcomes are retryable only when at least one failed unit says so;
    legacy items without outcome evidence retain the historical error/provider
    fallback. Returns ``(item, error_code)`` —
    ``(None, "not_found")`` for an unknown id, ``(item, "conflict")`` when the
    item is not retryable, and ``(item, None)`` on success. Resets the
    within-file telemetry so the retry reads like a fresh run; ``remember`` is
    content-hash idempotent, so re-processing the same source path is safe.
    """
    item = _find_item(state, item_id)
    if item is None:
        return None, "not_found"
    # Task #13: a sidecar-only "done" can be stale (graph content lost to an
    # unclean shutdown before the next checkpoint) — re-check the live graph
    # before trusting the sidecar's retryability verdict. ``verify=False`` means
    # the caller already ran :func:`verify_receipt_async` off the event loop.
    if verify:
        verify_receipt(state, item)
    if not _item_retryable(item):
        return item, "conflict"
    # Same-path dedup (mirrors enqueue_paths): if this durable path already has
    # a live queued/processing item — e.g. the watcher re-enqueued an edit
    # after the failure — a retry would drain identical bytes twice.
    live_dup = any(
        i is not item and i.path == item.path and i.status in ("queued", "processing")
        for i in state.ingest_queue
    )
    if live_dup:
        return item, "conflict"
    item.status = "queued"
    item.stage = "queued"
    item.error = None
    item.provider_error = None
    item.outcome = {}
    # Drop prior extraction telemetry events: _item_payload recomputes counts
    # from retained extraction_result events, so keeping the old run's events
    # would double-count the retry ("reads like a fresh run").
    item.events = [e for e in item.events if e.get("kind") != "extraction_result"]
    item.committed = 0
    item.queued = 0
    item.blocks_total = 0
    item.blocks_done = 0
    item.nodes = 0
    item.edges = 0
    item.claims = 0
    item.extracted_nodes = 0
    item.extracted_edges = 0
    item.extracted_claims = 0
    item.stage_progress_label = None
    item.stage_progress_done = 0
    item.stage_progress_total = 0
    record_event(state, item, "retried", "Re-queued after error", persist_now=False)
    if persist_now:
        persist(state)
    return item, None


def delete_item(
    state: "ServerState", item_id: str, *, persist_now: bool = True
) -> tuple[IngestItem | None, str | None]:
    """Remove a terminal (done/error/cancelled) item from the queue history.

    Returns ``(item, error_code)`` with the same shape as :func:`retry_item`;
    queued/processing items conflict — they belong to the live drain worker.
    """
    item = _find_item(state, item_id)
    if item is None:
        return None, "not_found"
    if item.status not in _TERMINAL:
        return item, "conflict"
    state.ingest_queue.remove(item)
    if persist_now:
        persist(state)
    return item, None


def ensure_worker(
    state: "ServerState", companion_factory: Callable[[object], SupportsRemember]
) -> None:
    """Start the drain worker if one isn't already running.

    Safe to call from a store-executor thread: the start is handed back to the
    event loop that dispatched that work."""
    if call_soon_on_loop(ensure_worker, state, companion_factory):
        return
    if getattr(state, "draining", False) or state.ingest_worker_active:
        return
    state.ingest_cancel_requested = False
    state.ingest_worker_active = True
    state.ingest_worker_task = asyncio.ensure_future(_drain(state, companion_factory))


def _progress_integrity_error(population: str, done: int, total: int | None) -> dict | None:
    """Structured telemetry record for a ``done > total`` progress report.

    ADR 0039 T9: ``done <= total`` is the invariant, and a violation is a
    telemetry error rather than a percentage to clamp and hide. This mirrors
    ``consolidate.ledger._progress``, including a zero declared total: work
    done against an empty population is a violation. ``total is None`` is the
    one exemption, and the reporter declares it explicitly (dedup counts
    judged pairs before any denominator exists) rather than it being inferred
    from a ``0``.
    """
    if total is None or done <= total:
        return None
    return {
        "code": "progress_done_exceeds_total",
        "population": population,
        "done": done,
        "total": total,
        "overflow": done - total,
    }


def _record_progress(
    state: "ServerState",
    item: IngestItem,
    population: str,
    done: int,
    total: int | None,
) -> None:
    """Store one reported progress pair and flag it if it violates T9.

    The reported numbers are kept verbatim — discarding or capping them to
    restore the invariant is the same clamp-and-hide the ADR forbids. The
    live ``progress_integrity_error`` field is a current-state indicator and
    clears once a consistent pair arrives, so entering the violating state
    also appends a durable, immediately persisted inspector event: a mid-run
    violation that later recovers must still leave evidence behind.
    """
    if population == "blocks":
        item.blocks_done = done
        item.blocks_total = total or 0
    else:
        item.stage_progress_done = done
        item.stage_progress_total = total or 0
    error = _progress_integrity_error(population, done, total)
    previous = item.progress_integrity_error or {}
    if error is not None:
        item.progress_integrity_error = error
        if previous.get("population") != population:
            record_event(
                state,
                item,
                "progress_integrity_error",
                f"progress reported {done}/{total} for {population}",
                error,
            )
    elif previous.get("population") == population:
        item.progress_integrity_error = {}


def _make_on_progress(state: "ServerState", item: IngestItem) -> Callable[[str, int, int], None]:
    """Build a throttled ``on_progress`` for one item's drain.

    Updates the live ``IngestItem`` telemetry on every call (so a status poll
    always sees fresh stage/block counts) but only marks the queue dirty on a
    stage change or every ``PERSIST_EVERY_BLOCKS`` blocks; the coalesced flush writes
    it (see :func:`request_persist`). Terminal status is persisted unconditionally by
    the caller. Runs inside the ``to_thread`` worker; marking is cheap and thread-safe."""
    last_stage = item.stage
    last_persist_blocks = 0

    def on_progress(stage: str, blocks_done: int, blocks_total: int) -> None:
        nonlocal last_stage, last_persist_blocks
        stage_changed = stage != last_stage
        if stage_changed:
            item.stage_progress_label = None
            item.stage_progress_done = 0
            item.stage_progress_total = 0
            # The previous stage's population is gone; its violation record
            # belongs to it, not to the incoming stage.
            if (item.progress_integrity_error or {}).get("population") != "blocks":
                item.progress_integrity_error = {}
        item.stage = stage
        # Only the block-counting stages report a blocks population. The
        # dedup/curation stages tick an item ORDINAL with an undeclared total
        # (see ``Companion._emit_substage``); folding those into
        # ``blocks_done``/``blocks_total`` would both clobber the displayed
        # block counts and trip the ADR 0039 T9 integrity check on every MCP
        # ingest (ordinal 212 > 3 blocks). Their fine-grained progress already
        # reaches the UI through the ``dedup_progress``/``curator_progress``/
        # ``relation_curator_progress`` events, so these ticks only keep the
        # live stage (and the client's idle timer) fresh.
        counts_blocks = stage in _BLOCK_POPULATION_STAGES
        if counts_blocks:
            _record_progress(state, item, "blocks", blocks_done, blocks_total)
        enough_blocks = counts_blocks and blocks_done - last_persist_blocks >= PERSIST_EVERY_BLOCKS
        if stage_changed or enough_blocks:
            last_stage = stage
            if counts_blocks:
                last_persist_blocks = blocks_done
            request_persist(state)

    return on_progress


def _declared_total(raw: object) -> int | None:
    """A reported denominator, or ``None`` when the reporter declared none."""
    if raw is None:
        return None
    try:
        return max(int(raw), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _make_on_event(state: "ServerState", item: IngestItem) -> Callable[[dict], None]:
    def on_event(event: dict) -> None:
        kind = str(event.get("kind") or "event")
        summary = str(event.get("summary") or kind)
        payload = event.get("payload")
        if kind == "extraction_result":
            nodes, edges, claims = _extraction_event_counts(payload)
            item.extracted_nodes += nodes
            item.extracted_edges += edges
            item.extracted_claims += claims
        if isinstance(payload, dict):
            progress_fields = {
                "embedding_progress": (
                    "Embedding candidates",
                    payload.get("nodes_done"),
                    payload.get("nodes_total"),
                ),
                "curator_progress": (
                    "Curating entities",
                    payload.get("reviewed"),
                    payload.get("total"),
                ),
                "relation_curator_progress": (
                    "Curating relationships",
                    payload.get("reviewed"),
                    payload.get("total"),
                ),
                # Dedup counts judged pairs before any denominator exists —
                # an explicitly undeclared population (ADR 0039 T9), not zero.
                "dedup_progress": (
                    "Dedup pairs judged",
                    payload.get("pairs_judged"),
                    None,
                ),
            }
            progress = progress_fields.get(kind)
            if progress is not None:
                label, done, total = progress
                item.stage_progress_label = label
                _record_progress(
                    state,
                    item,
                    kind,
                    max(int(done or 0), 0),
                    _declared_total(total),
                )
        # Runs under the companion's event lock: append in memory and mark dirty only;
        # the sidecar write happens in the coalesced flush, off this thread's lock.
        record_event(
            state,
            item,
            kind,
            summary,
            payload if isinstance(payload, dict) else {"value": payload},
            persist_now=False,
        )
        request_persist(state)

    return on_event


def _item_payload(item: IngestItem, *, include_events: bool) -> dict:
    payload = asdict(item)
    events = payload.pop("events", [])
    retained_nodes, retained_edges, retained_claims = _retained_extraction_counts(events)
    payload["extracted_nodes"] = max(int(payload.get("extracted_nodes") or 0), retained_nodes)
    payload["extracted_edges"] = max(int(payload.get("extracted_edges") or 0), retained_edges)
    payload["extracted_claims"] = max(int(payload.get("extracted_claims") or 0), retained_claims)
    payload["event_count"] = len(events)
    payload["last_event"] = events[-1] if events else None
    if include_events:
        payload["events"] = events
    return payload


async def _checkpoint_after_drain(state: "ServerState", vault: object) -> None:
    """Force the just-drained document's writes into the durable graph file.

    Ladybug (task #12) only merges its WAL into ``graph.lbug`` on a clean
    close; a long live ingest that never closes cleanly leaves everything
    written since the last merge stranded in the WAL, so a single unclean
    shutdown can void an entire run. Checkpointing here — once a document
    reaches a terminal ingest state, still inside the caller's
    ``writer_lock`` — caps that loss to whatever is ingested after this
    point, without a second writer ever touching the database.

    Best-effort: a checkpoint failure must never flip an otherwise-successful
    ingest to ``error`` (the semantic write already committed); it is logged
    and the next drain point gets another chance. Missing/incompatible store
    (tests pass lightweight stubs) is silently skipped.
    """
    store = getattr(vault, "store", None)
    checkpoint = getattr(store, "checkpoint", None)
    if not callable(checkpoint):
        return
    try:
        await store_io(checkpoint)
    except Exception:
        _LOG.warning(
            "post-drain checkpoint failed for vault %s; the graph stays "
            "correct in memory but writes since the last checkpoint remain "
            "WAL-only until the next successful checkpoint or a clean close",
            getattr(state, "vault_path", None),
            exc_info=True,
        )


def _vault_payload(state: "ServerState") -> dict[str, object] | None:
    vault_path = getattr(state, "vault_path", None)
    if vault_path is None:
        return None
    path = Path(vault_path).expanduser().resolve(strict=False)
    return {"name": path.name, "path": str(path), "current": True}


async def _drain(
    state: "ServerState", companion_factory: Callable[[object], SupportsRemember]
) -> None:
    try:
        while not state.draining:
            if getattr(state, "ingest_cancel_requested", False):
                # Catch files enqueued while the active item was winding down.
                if _cancel_queued_items(state):
                    await store_io(persist, state)
                break
            item = next((i for i in state.ingest_queue if i.status == "queued"), None)
            if item is None:
                break
            item.status = "processing"
            item.stage = "parsing"
            item.outcome = {}
            record_event(
                state,
                item,
                "started",
                "Started ingest",
                {"path": item.path},
                persist_now=False,
            )
            # transition is now durable before the slow LLM call
            await store_io(persist, state)
            on_progress = _make_on_progress(state, item)
            on_event = _make_on_event(state, item)
            try:

                def _should_cancel() -> bool:
                    return bool(getattr(state, "ingest_cancel_requested", False) or state.draining)

                lease_factory = getattr(state, "lease_vault", None)
                # ``lease_vault`` is duck-typed across the runtime shapes (and
                # absent on minimal test states), so the factory is untyped here.
                # Prove it produced a context manager before entering it.
                lease_context: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
                if callable(lease_factory):
                    # Leasing may open the vault under the pool lock: off-loop.
                    leased = await acquire_off_loop(lease_factory)
                    if not isinstance(leased, contextlib.AbstractContextManager):
                        raise TypeError("lease_vault() must return a context manager")
                    lease_context = leased
                # Hold the path lease for the entire model call + graph commit.
                # A managed deletion fence can therefore prove this worker is
                # finished before path-wide close.
                with lease_context as leased_vault:
                    async with state.writer_lock:
                        vault = leased_vault or getattr(state, "vault", None)
                        if vault is not None:
                            await store_io(require_write_allowed, state, vault)
                        companion = companion_factory(state)
                        # remember() is synchronous and blocks on the LLM HTTP call;
                        # run it off the event loop so /ingest-queue polls and reads
                        # stay responsive while a file is being extracted.
                        if _should_cancel():
                            raise RememberCancelled()
                        result = await job_io(
                            companion.remember,
                            item.path,
                            on_progress=on_progress,
                            on_event=on_event,
                            should_cancel=_should_cancel,
                        )
                        # Task #12: this document just reached a terminal
                        # ingest state — checkpoint now, still holding
                        # writer_lock, before the next (possibly much
                        # longer) document starts.
                        await _checkpoint_after_drain(state, vault)
                item.committed = int(getattr(result, "committed", 0))
                item.queued = int(getattr(result, "queued", 0))
                # Final reconciliation against the authoritative result, not a
                # revision of a live denominator: remember() reports the real
                # block population it processed, and done == total by
                # construction, so this pair can never violate T9.
                final_blocks = int(getattr(result, "blocks_total", item.blocks_total))
                _record_progress(state, item, "blocks", final_blocks, final_blocks)
                item.nodes = int(getattr(result, "nodes_extracted", 0))
                item.edges = int(getattr(result, "edges_extracted", 0))
                item.provider_error = getattr(result, "provider_error", None)
                raw_outcome = getattr(result, "outcome", None)
                item.outcome = dict(raw_outcome) if isinstance(raw_outcome, dict) else {}
                item.claims = int(getattr(result, "claims_minted", 0)) + sum(
                    1 for outcome in getattr(result, "outcomes", ()) if outcome.type == "Claim"
                )
                # F4: a provider error that yielded NOTHING (no commits, no
                # claims, no gate-parked candidates) is a failed ingest, not a
                # quiet "done" — mark it error so it is visible and retryable.
                # Partial yield keeps status "done" with provider_error set.
                zero_yield = (
                    bool(item.provider_error)
                    and item.committed == 0
                    and item.claims == 0
                    and item.queued == 0
                )
                outcome_quality = str(item.outcome.get("quality") or "").strip()
                outcome_failed = outcome_quality in {"failed", "integrity_failed"}
                if zero_yield or outcome_failed:
                    item.status = "error"
                    item.stage = "error"
                    item.error = (
                        f"ingest outcome: {outcome_quality}"
                        if outcome_failed
                        else f"provider error with zero yield: {item.provider_error}"
                    )
                    record_event(
                        state,
                        item,
                        "error",
                        (
                            "Ingest completed with a failed technical outcome"
                            if outcome_failed
                            else "Ingest yielded nothing (provider error)"
                        ),
                        {
                            "provider_error": item.provider_error,
                            "outcome_quality": outcome_quality or None,
                        },
                        persist_now=False,
                    )
                else:
                    item.status = "done"
                    item.stage = "done"
                    record_event(
                        state,
                        item,
                        "done",
                        "Ingest completed",
                        {
                            "committed": item.committed,
                            "queued": item.queued,
                            "nodes": item.nodes,
                            "edges": item.edges,
                            "claims": item.claims,
                            "provider_error": item.provider_error,
                            "outcome_quality": outcome_quality or None,
                        },
                        persist_now=False,
                    )
                    # ADR 0009 P4: signal in-process ingest activity so the
                    # continuous curation scheduler can debounce its sweeps off it.
                    _note_ingest(state)
                    from okto_neuron.server import _curation

                    previous_outcome = item.outcome
                    # Reads the vault config and persists job/queue sidecars.
                    item.outcome = await store_io(
                        _curation.attach_verified_reconciliation_outcome,
                        state,
                        item.outcome,
                        trigger="verified_file_commit",
                        ingest_item_id=item.id,
                    )
                    reconciliation = item.outcome.get("cross_document_reconciliation")
                    if isinstance(reconciliation, dict) and reconciliation != previous_outcome.get(
                        "cross_document_reconciliation"
                    ):
                        record_event(
                            state,
                            item,
                            "cross_document_reconciliation",
                            "Cross-document reconciliation follow-up recorded",
                            reconciliation,
                            persist_now=False,
                        )
            except RememberCancelled:
                if state.draining:
                    # A daemon stop pauses durable work; it must not silently
                    # discard the user's queue. Startup rehydrates this item and
                    # resumes it from the existing candidate ledger/checkpoint.
                    item.status = "queued"
                    item.stage = "queued"
                    item.error = None
                    record_event(
                        state,
                        item,
                        "paused",
                        "Server stopped; file queued for resume",
                        persist_now=False,
                    )
                else:
                    item.status = "cancelled"
                    item.stage = "cancelled"
                    item.error = "cancelled by user"
                    record_event(
                        state,
                        item,
                        "cancelled",
                        "Active file stopped at a safe checkpoint",
                        persist_now=False,
                    )
            except IntegrityFenceError as exc:
                item.status = "error"
                item.stage = "error"
                item.error = str(exc)
                item.outcome = {
                    "quality": "integrity_failed",
                    "units": {},
                    "failed_units": [],
                    "integrity": {
                        "status": exc.state.status.value,
                        "audit_id": exc.state.audit_id,
                        "graph_generation": exc.state.graph_generation,
                    },
                }
                record_event(
                    state,
                    item,
                    "error",
                    "Ingest blocked by graph integrity fence",
                    {
                        "error": item.error,
                        "code": "integrity_fenced",
                        "outcome": item.outcome,
                    },
                    persist_now=False,
                )
            except LLMUnavailableError as exc:
                item.status = "error"
                item.stage = "error"
                item.error = str(exc)
                item.provider_error = str(exc)
                item.outcome = dict(exc.outcome) if exc.outcome else {"quality": "failed"}
                record_event(
                    state,
                    item,
                    "error",
                    "Ingest failed because the configured LLM was unavailable",
                    {
                        "error": item.error,
                        "outcome_quality": item.outcome.get("quality"),
                    },
                    persist_now=False,
                )
            except Exception as exc:  # noqa: BLE001 — per-item isolation; one bad file never kills the queue
                item.status = "error"
                item.stage = "error"
                item.error = str(exc)
                item.outcome = {
                    "quality": "failed",
                    "error_class": "internal",
                    "retryable": True,
                }
                record_event(
                    state,
                    item,
                    "error",
                    "Ingest failed",
                    {"error": item.error},
                    persist_now=False,
                )
            await store_io(persist, state)  # always persist the terminal status
            await asyncio.sleep(0)  # yield so /ingest-queue polls stay responsive
    finally:
        state.ingest_worker_active = False
        state.ingest_cancel_requested = False
        if getattr(state, "ingest_worker_task", None) is asyncio.current_task():
            state.ingest_worker_task = None
        # A verified-file reconciliation job deliberately parks while this
        # durable batch is active so all file triggers coalesce into one stable
        # whole-graph proposal. Resume the curation worker as soon as the batch
        # becomes quiet.
        if any(job.status == "queued" for job in getattr(state, "curation_jobs", ())):
            from okto_neuron.server import _jobs

            _jobs.ensure_worker(state)


__all__ = [
    "IngestItem",
    "TEXT_SUFFIXES",
    "MAX_ENQUEUE",
    "HISTORY_FILENAME",
    "RETENTION_CAP",
    "PERSIST_EVERY_BLOCKS",
    "safe_source_filename",
    "upload_rel_parts",
    "classify_upload_name",
    "durable_copy_path",
    "discover_folder",
    "enqueue_paths",
    "enqueue_uploads",
    "record_completed",
    "record_event",
    "snapshot",
    "item_detail",
    "verify_receipt",
    "cancel",
    "retry_item",
    "delete_item",
    "ensure_worker",
    "history_path",
    "persist",
    "rehydrate_queue",
]
