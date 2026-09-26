# ADR 0025 — Continuous Folder Monitoring

**Status:** Accepted; active-vault limitation superseded by ADR 0034
**Date:** 2026-06-30
**Depends on:** ADR 0014 (multi-vault serve), ADR 0023/0024 (incremental ingest)

---

## Supersedence note — 2026-07-13

ADR 0034 retires the active-vault-only drain limitation documented below.
Folder-watch events now enqueue into the immutable runtime of the vault whose
configuration declared the watched root, whether or not a browser currently
selects that vault. The polling, debounce, exclusion, and durable-manifest
decisions remain in force. ADR 0034 also supersedes the global writer-lock and
unleased raw-handle assumptions: each vault runtime owns its worker and lock,
and background supervision uses lease-aware pool access.

## Context

Users place markdown notes and documents into local directories and want them
to appear in the graph without manual `marginalia add` or explicit ingest
calls. The existing `kg watch` / `marginalia watch` command handles the vault
*inbox* (`.marginalia/incoming`) in a separate process; it cannot watch
arbitrary directories, is not multi-vault-aware, and is invisible to the
server's ingest queue UI.

ADR 0023 and ADR 0024 make re-ingest cheap (content-hash gating +
sub-chunk diff), so re-processing an edited file costs only the changed
blocks rather than the full 12k window.

## Decision

Implement a **global, multi-vault polling watch loop** (`server/_folder_watch.py`)
as a single asyncio background task that starts at daemon boot alongside the
curation scheduler and runs for the daemon's lifetime.

### Polling with settle-detection

- Each tick (every `WATCH_TICK_S = 1.0s`) the loop iterates all known vaults.
- Per-vault: re-reads `folder_watch` config from `marginalia.yaml` live (so a
  config change takes effect without restart).
- Per watched root: stat-walk, hash-on-change (only when `(mtime, size)` differ
  from the stored entry), diff vs a per-vault manifest sidecar at
  `<vault>/.marginalia/watches/<root-hash>.manifest.json`.
- **Settle-detection**: a file is only enqueued after it has not changed for
  `quiet_debounce_s` seconds (default 3). A mid-save flurry keeps bumping
  `last_change_seen_at`; ingest fires once the file is quiet.
- **Min-interval floor**: `min_interval_s` (default 10) prevents the same file
  from being re-ingested more than once per interval even if it bounces.

### Change detection is graph-free

Stat-walk + hash-vs-sidecar opens NO vault graph, so the loop scales past
`max_open=8` (ADR 0014). Only settled changes open a vault handle via
`vault_pool.get_or_open(vault_path)`.

### Enqueue, not inline remember()

Settled files are passed to `_ingest_queue.enqueue_paths` + `ensure_worker`.
The drain worker runs `remember()` off-thread under the writer lock — the watch
loop never blocks on LLM calls.

### watchfiles rejected as load-bearing

`watchfiles` may be present transitively with the server stack but is unused. Native FS events are
unreliable on Docker/NFS mount points and introduce a C/Rust dependency whose
failure mode (missed events) is silent. Polling stat-walk is the load-bearing
path; `watchfiles` may serve as an opt-in accelerator in a future ADR.

## Configuration

New `folder_watch` block in `marginalia.yaml` (mirroring `CurationSchedulerConfig`):

```yaml
folder_watch:
  enabled: false          # off by default; set true to activate
  poll_interval_s: 5.0    # stat-walk frequency
  quiet_debounce_s: 3.0   # file must be quiet this long before ingest
  min_interval_s: 10.0    # minimum gap between two ingests of the same file
  recursive: true
  ignore_globs:
    - "*.swp"
    - "*.tmp"
    - "4913"
    - "*~"
    - ".#*"
  roots: []               # absolute paths; add via `marginalia watch-folder add`
```

Re-read live per tick; a toggle takes effect without restart.

## Multi-vault (ADR 0014)

- The watch loop is **global**: iterates `list_vaults()` (all vaults in
  `vault_roots` + the configured default), not just the active vault.
- Watch-state (debounce table + manifest sidecar) is keyed by vault path, not
  by `state.vault`.
- `state.folder_watch_task` is set once at boot and **not reset by
  `switch_vault`** (it is vault-global).

### Historical writer-lock note (superseded by ADR 0034)

`state.writer_lock` is a **global** asyncio.Lock (`get_vault_write_lock()`),
shared by all vaults in the process (per ADR 0014 v1 decision). This
serializes ingests across vaults — acceptable in v1 given the ingest queue's
drain-one-at-a-time invariant. Per-vault locks are a future promotion if
concurrent cross-vault ingest throughput becomes a bottleneck.

### Historical v1 limitation: active-vault drain (superseded by ADR 0034)

The `_ingest_queue._drain` worker runs `remember()` against `state.vault`
(the active vault). For non-active vault roots, files are enqueued to
`state.ingest_queue` but processed in the active vault context. This is safe
only when the active vault is the intended target; full per-vault drain workers
require a larger refactor (tracked as follow-up).

**Historical v1 recommendation (no longer applicable):** configure
`folder_watch.roots` only on the active vault. Current source drains every
configured vault through its own immutable runtime.

## Lifecycle

- Started at daemon boot in `runtime._run_async` alongside `scheduler_task`.
- Cancelled in `runtime._run_async`'s `finally` block before vault close (same
  pattern as `scheduler_task`).
- Flag-gated by `folder_watch.enabled` (default `False`); inert unless
  configured.

## Registration storm prevention

When `marginalia watch-folder add <path>` registers a new root, it:
1. Writes the root to `folder_watch.roots` in `marginalia.yaml`.
2. Triggers an initial ingest via `POST /api/v1/ingest-folder`.
3. The manifest sidecar is written with post-ingest hashes so the first watch
   tick sees no diff.

## Edge cases

| Case | Handling |
|------|----------|
| Deleted file | Dropped from in-memory debounce table and from next manifest save. No graph data deleted (memory accretes; Feature 1 concern). |
| Rename | Delete + new (two separate events). |
| Partial write / editor swap | `ignore_globs` covers `*.swp`, `*.tmp`, `4913`, `*~`, `.#*`; debounce absorbs mid-save flurries. |
| Daemon down | On first tick after restart, manifest vs disk diff catches all offline edits and queues them. |
| root not a directory | Skipped with a debug log; checked each tick (mount may appear later). |
| pool_full (> 8 live handles) | The lease-aware pool evicts an idle LRU handle first. If every handle is leased, fenced, or compatibility-pinned, `VaultPoolError` is caught and the root retries on a later tick. |

## Consequences

- Users can point `marginalia watch-folder add <path>` at any directory; files
  appear in the graph within `poll_interval_s + quiet_debounce_s` seconds.
- No process separation: the loop runs inside `marginalia serve`, so it stops
  when the daemon stops.
- Polling frequency is bounded by `poll_interval_s` (default 5s), not
  inotify-latency, but is sufficient for note-taking workflows.
- Change detection is O(files) per tick — linear stat-walk. Large directories
  (thousands of files) may impose measurable overhead; watchfiles opt-in is the
  scaling escape hatch.

## HTTP + UI surface

The watch loop exposes a loopback-only status/config surface so the web UI can
show what is being monitored (the original ADR scoped this as optional; it
shipped with the v0.0.29 UI):

- `GET /api/v1/folder-watch/status` — per-vault snapshot from the live loop:
  `enabled`, `roots`, `last_poll_ts`, `watched_file_count`, `pending`
  (files still settling), `recent_ingests`. Graph-free (stat only), so it never
  opens the pool.
- `POST` / `DELETE /api/v1/folder-watch/roots` `{path}` — add/remove a root on
  the request-bound vault runtime's `folder_watch.roots` (same yaml edit as `watch-folder add`),
  enabling folder-watch on first add.
- `folder_watch` round-trips through the generic `GET/PATCH /api/v1/config`
  (added to `VaultConfig.WRITABLE_BLOCKS`).

The frontend renders three things from this: a **Folder watch** config section
(Config tab), a live **Folder Watch** status panel + friendly per-ingest event
summaries (`incremental_partition` / `subchunk_partition` / `claims_reconciled`)
in the Logs tab, and **superseded / detached badges** on claims in the KG
Browser node-detail (recall hides superseded claims; Browse can still inspect
them, where the badge — e.g. `superseded until <valid_until>` — renders).

## Implementation notes — live-run remediation series (2026-07-02)

> This section preserves the 2026-07-02 remediation sequence. References below to mutable
> `state.vault`, active-vault pausing, reset-on-switch counters, and a global writer lock describe
> that historical implementation and are superseded by ADR 0034's immutable per-vault runtimes.

The first production folder-watch run (vault `demo-vault`) surfaced a set of
watcher/queue defects (24-agent audit, 2026-07-02). The fixes land as a phased
remediation series against this ADR's implementation:

- **Phase 0 — `_tick_root` seam.** The per-root tick body of
  `run_folder_watch` (manifest load → diff → debounce update → fire decision →
  enqueue → manifest save) is extracted verbatim into a private async
  `_tick_root(state, vault_path, vp_str, root, cfg, vs, now)`. Zero behavior
  change; the seam takes an injected `now`, so the full tick is now covered by
  characterization tests (`tests/server/test_folder_watch.py::TestTickRoot`)
  ahead of the behavior-changing fixes. The known debounce-durability bug
  (a pending edit's new sha is persisted to the manifest before its enqueue
  fires, so a restart inside the debounce window loses the edit) is pinned by
  a characterization test and fixed in a later phase of this series.

- **Phase 1 — directory exclusion + one enumeration (fixes the `.state/`
  junk-ingest defect).** The live run's worst outcome — 47% of graph
  Documents derived from `.state/backups/` and `.remember/` — came from the
  watcher walking *every* directory under a root. Changes:
  - New `folder_watch.ignore_dir_globs` config (default
    `[".*", "__pycache__", "node_modules"]`, exported as
    `DEFAULT_IGNORE_DIR_GLOBS`): directory-NAME patterns pruned before
    descent, fnmatch-tested per path component, so a junk dir's whole subtree
    is never stat-walked.
  - One shared enumerator, `_iter_watch_files(root, recursive, ignore_globs,
    ignore_dir_globs)` (`os.walk` + in-place `dirnames` pruning, sorted for
    determinism), used by BOTH `compute_manifest_diff` (the watch loop) and
    `_ingest_queue.discover_folder` (one-shot `POST /api/v1/ingest-folder`),
    so exclusion behaves identically everywhere.
  - The dead `initial_ingest_and_write_manifest` helper (zero callers) is
    deleted; `watch-folder add` reaches initial ingest through the daemon
    endpoint, whose request key mismatch is fixed (the CLI posted
    `{"root": ...}` while the endpoint requires `"path"` — every
    `watch-folder add` initial ingest 400'd before this).
  - **Downgrade note:** older builds parse `marginalia.yaml` with
    `extra='forbid'`; a config carrying `ignore_dir_globs` makes them fail
    config load and silently disable folder-watch. Acceptable pre-alpha;
    remove the key by hand if downgrading.
  - *Review amendment:* `POST /api/v1/ingest-folder` loads the active vault's
    `folder_watch.ignore_globs`/`ignore_dir_globs` and passes them to
    `discover_folder`, so a CUSTOMIZED exclusion config also keeps one-shot
    ingest and the watcher identical (the first cut hardcoded the defaults).

- **Phase 2 — queue integrity (F2 dedup + stable ids, F4 provider-error
  terminal status).** The live run queued one file 11× (each fire a fresh
  item) and recorded LLM-outage files as quiet `done` with zero yield.
  - *Enqueue dedup:* a path that already has a `queued` item gets its durable
    sources copy refreshed (freshest bytes win) plus a persisted `refreshed`
    event, instead of a duplicate entry. A `processing` path still appends one
    fresh item (the in-flight drain reads pre-edit bytes; the new item picks
    up the edit). Enqueue and drain status flips are both event-loop-side, so
    the check cannot race.
  - *Stable ids:* `IngestItem.id` comes from a ServerState-scoped monotonic
    counter (`ingest_seq`), seeded by `rehydrate_queue` past every persisted
    id and reset on vault switch. The old len()-based ids collided after
    deletes.
  - *Worker wake:* `_enqueue_for_vault` calls `ensure_worker` whenever ANY
    item is queued, not only when the enqueue minted new items — a
    dedup-refreshed enqueue must still revive a dead worker.
  - *F4:* a `provider_error` with zero yield (no commits, no claims, no
    gate-parked candidates) terminates as `status="error"` — visible in the
    UI and retryable — instead of a quiet `done`. Partial yield stays
    `done` + `provider_error`. `POST /api/v1/ingest-queue/{id}/retry` accepts
    both `error` items and done-with-provider-error items and clears
    `provider_error` for the fresh run. **Semantics change:** ingests that
    previously showed `done` with 0 claims during an LLM outage now count in
    the `error` summary bucket.
  - *Known interaction (documented, accepted):* `watch-folder add` triggers
    the endpoint's initial ingest AND the watcher's first tick enumerates the
    same files against an empty manifest; the overlap is absorbed by the
    enqueue dedup (still-queued items refresh) and content-hash-idempotent
    `remember()` for already-drained items.

- **Phase 3 — watcher durability & visibility (F5, F12b/F13, F12c).**
  - *F5 — restarts no longer lose debounced edits.* The persisted manifest
    sidecar now withholds a pending (detected-but-not-fired) edit: the file
    keeps its PRE-edit entry (a pending new file stays absent) until its
    enqueue is ACCEPTED, so a daemon restart inside the debounce window
    re-detects the edit from the sidecar. The in-memory debounce clock bumps
    only when the observed sha actually differs from the tracked one (a
    pending file re-appears in the diff every tick by construction), and an
    edit-then-undo clears `pending` without an ingest.
  - *Enqueue-failure durability (review amendment):* the fired-state flip
    happens per path AFTER `enqueue_paths` reports acceptance. A source whose
    durable copy fails (unreadable file, full disk) stays pending with its
    old manifest entry and retries next tick; an enqueue exception no longer
    kills the watcher task. Unreadable-but-present files are kept in the
    debounce table (only vanished files are dropped).
  - *F12b/F13 — non-active vaults pause visibly.* The v1 drain worker runs
    `remember()` against the ACTIVE vault only, so scanning a non-active
    vault's roots would ingest into the wrong graph. The loop now checks
    active-ness BEFORE any manifest load/scan (also removing the
    second-instance manifest-write race), publishes a `paused_reason` in the
    status snapshot, and warns once per vault (cleared when it becomes
    active). Per-vault drain workers remain the ADR follow-up.
  - *F12c — skipped files are visible.* The shared enumerator counts
    non-text files it passes over; the count surfaces as `skipped_non_text`
    in `folder-watch/status` (per vault), in the `POST /api/v1/ingest-folder`
    response, and in the `watch-folder add` CLI echo ("queued X, skipped Y
    non-text").

- **Phase 3 review hardening (adversarial workflow wf_9a0698aa-e33, 14
  confirmed findings).**
  - The vanished-file cleanup's `is_file()` probe is exception-guarded (a
    parent dir losing search permission raises `PermissionError`, which would
    have killed the GLOBAL watcher task), and `run_folder_watch` wraps each
    per-root tick in a belt-and-braces try/except so no single root can ever
    stop the loop.
  - Edit-then-undo detection requires a genuine re-hash (same sha AND a
    changed mtime vs the withheld entry) — a transiently-unreadable pending
    file is no longer misclassified as undone.
  - A file whose enqueue is never accepted (persistently failing durable
    copy) bumps `last_enqueue_at` on the failed ATTEMPT, so `min_interval_s`
    acts as retry backoff instead of full-speed per-tick retries.
  - Per-root debounce/skip state is pruned when a root is removed from
    `folder_watch.roots` live (no phantom pending/skipped counts in status).
  - Web UI: the folder-watch panel renders `paused_reason` (paused badge with
    tooltip) and `skipped_non_text`; the bulk-import result surfaces
    `refreshed` and `skipped_non_text`, and an all-refreshed response no
    longer reads as a failure.
  - *Known accepted tradeoffs (documented, not fixed):* pending files are
    re-hashed each tick while withheld (bounded by the debounce in the happy
    path); enqueue "acceptance" precedes the sidecar persist (persist
    swallows OS errors — a crash in that window relies on the manifest
    withhold for re-detection); `shutil.copy2` refreshes the durable copy
    even while a same-path item is processing (the in-flight read may see
    fresh bytes — remember() is content-hash idempotent either way); an item
    enqueued in the cancel→worker-exit window strands as `queued` until the
    next enqueue wakes a worker.

- **Phase 5 — F11 stable sources identity (graph-identity semantics
  change).** Durable copies of folder-ingested files now mirror the source
  tree: `sources/<sha256(root)[:16]>/<component-sanitized relpath>` (same
  root-key convention as the manifest sidecars) instead of the flat
  hash-suffixed `<stem>-<pathhash8>.md` name. A file's durable path — and
  therefore its graph Document identity and the provenance paths on every
  claim — is now stable AND human-readable across edits. Path components are
  slugified per component with `..`/empty neutralized (a hostile relpath
  cannot escape the sources dir); `safe_source_filename` is NOT reused
  wholesale (it force-appends `.md`). Uploads and root-less enqueues keep the
  legacy flat scheme. **One-time fork (review-confirmed,
  accepted):** sources ingested by OLDER builds keep their old flat-named
  Documents; the first re-ingest under this scheme creates a fresh Document
  at the new path, and the old Document's claims are stranded outside the
  orphan-detach machinery. No migration is shipped — acceptable pre-alpha;
  the demo-vault rebuild starts from zero, and any other vault can
  `kg rebuild` (whose source enumeration now includes all text suffixes and
  nested `index.md` files under the tree scheme). Lossy-sanitized components
  carry a hash of their original spelling so distinct relpaths can never
  collide onto one durable copy. *Rebuild-caught regression (v0.0.32):* the
  suffix-preserving copies exposed that `ingest_document` rejected `.txt` —
  the flat scheme had force-renamed everything to `.md`. The parser gate now
  matches the queue's `TEXT_SUFFIXES` contract (`.md`/`.markdown`/`.txt`);
  3 real demo-vault transcripts failed the rebuild-from-zero until this fix.

## Addendum — 2026-09-15: Phase 1's "one enumeration" is now one *predicate* (batch uploads joined the policy)

Phase 1 unified the two callers that could **walk a tree**
(`compute_manifest_diff` and `discover_folder`). A third ingest surface cannot
walk anything: `POST /api/v1/ingest-batch` receives a file list the **browser**
enumerated, and it is what the Web UI's "Choose a folder" button actually calls.
It applied none of this ADR's exclusion policy, and a real run queued 168 files
where `/ingest-folder` had reduced the same folder to 76 — 51% dot-directory
scaffolding and agent tooling notes, the same defect class as the 47%
`.state/backups/` outcome that motivated Phase 1.

`_iter_watch_files` therefore keeps the walk, but every per-file verdict now
comes from an extracted predicate, `classify_source_relpath`, with the
directory rule shared through `_dir_component_excluded` so walk-time pruning is
an optimization of the predicate rather than a second copy of the rules. The
batch endpoint calls the same predicate via `_ingest_queue.classify_upload_name`.
Full rationale, the reported-skip contract, and the equivalence guard are in the
2026-09-15 addendum to ADR 0026.
