# ADR 0026 — Rebuild Honors the Source-Selection Denylist

**Status:** Accepted
**Date:** 2026-07-04
**Depends on:** ADR 0007 (rebuild from trust root), ADR 0025 (continuous folder monitoring)

---

## Context

Two code paths enumerate the files that become graph Documents, and they had
drifted apart:

- The **live folder-watch / ingest path** (`_iter_watch_files` in
  `src/marginalia/server/_folder_watch.py`) applies a hard, non-configurable
  denylist so scaffolding never enters the graph: leading-dot files, agent
  tooling notes (`CLAUDE.md`), presales tracking scaffolding
  (`sync-queue.md`, `sweep-dates.md`), a `tracking/`-folder `index.md`, and the
  `tracking/tracking` double-descent duplicates the companion mirrors when a
  `tracking` subroot is watched inside its own parent.

- The **trust-root rebuild path** (`_deterministic_rebuild_files` in
  `src/marginalia/cli/kg.py`), invoked by `kg rebuild` and the curation rebuild,
  re-enumerates the durable source tree under `.marginalia/sources/` to
  re-derive the graph from the vault (ADR 0007). It excluded only the top-level
  generated `sources/index.md` manifest — nothing else.

Because rebuild ignored the folder-watch denylist, a rebuild **re-ingested junk
the incremental path already excludes**: `CLAUDE.md`, dot-files, the tracking
index pages, sync-queue/sweep-dates scaffolding, and the `tracking/tracking`
duplicates. A rebuild therefore produced a *different, dirtier* graph than the
incremental ingest it was supposed to reproduce — a violation of ADR 0007's
"graph is re-derived from the trust root" invariant, where the two source
selections must agree.

## Decision

Factor a single **shared basename-level predicate**,
`_is_denylisted_relpath(rel: Path)` in
`src/marginalia/server/_folder_watch.py`, and have **both** enumeration paths
call it so their source selection can never drift:

- `_is_denylisted_file(rel_parts)` (the live watch) now delegates to
  `_is_denylisted_relpath` after adding its own `.marginalia` internal-dir
  guard on top.
- `_deterministic_rebuild_files` imports and calls `_is_denylisted_relpath`
  for every candidate under `.marginalia/sources/`.

The predicate applies only **name-level** rules (leading-dot files, the
`_DENY_FILE_NAMES` set `{CLAUDE.md, sync-queue.md, sweep-dates.md}`, a
`tracking/` `index.md`, and the `tracking/tracking` double-descent).

### Denylist is evaluated relative to each source root, not the vault root

Rebuild evaluates the predicate on a path **relative to each source-key root**
under `.marginalia/sources/<sha256(root)[:16]>/`, not relative to the vault
root. The leading source-key hash segment is stripped so the predicate sees the
mirrored tree (e.g. `tracking/…`) exactly as the watch saw it at ingest.

The `.marginalia` internal-dir rule is **deliberately excluded** from the shared
predicate. Every durable source lives *under* `.marginalia/sources/`, so a
vault-relative `.marginalia in parts` test would reject the entire durable
corpus. The one caller that needs that guard — `_is_denylisted_file` for the
live watch, whose root is a vault, not a source-key root — adds it itself.

## Consequences

- `kg rebuild` and the curation rebuild now produce the **same clean document
  set** as incremental ingest; the two paths share one denylist and cannot
  drift.
- Verified end-to-end on the live `demo-vault` vault: the rebuild dropped exactly
  13 junk Documents (**64 → 51**) — the CLAUDE.md, dot-files, tracking index
  pages, sync-queue/sweep-dates scaffolding, and `tracking/tracking`
  duplicates — while preserving all legitimate content.
- A denylist + watch/rebuild parity test guards the shared predicate
  (`tests/cli/test_kg_rebuild.py`).
- Legitimate `index.md` notes outside a `tracking/` segment, and all real text
  suffixes (`.md`/`.markdown`/`.txt`) preserved by the F11 tree scheme, remain
  eligible sources.

## Addendum — 2026-09-15: the batch upload path was a third source selection, and it agreed with neither

The decision above made **rebuild** agree with **watch**. A third ingest surface
was never brought into that agreement: `POST /api/v1/ingest-batch`
(`api_ingest_batch`, `src/marginalia/server/http.py`), which the Web UI's
"Choose a folder" button and its drag-drop handler actually call
(`frontend/src/components/ingest/BulkImport.tsx` — the button drives a hidden
`<input type="file" multiple webkitdirectory>`, enumerates the files **in the
browser**, and POSTs their contents). It is not the `/ingest-folder` endpoint,
and it applied **zero** server-side source selection. The only filter standing
was a one-line browser regex, `TEXT_RE = /\.(md|markdown|txt)$/i`.

Measured on a real run: a folder `/ingest-folder` correctly reduced to 76 files
was queued as **168** through the UI. The 92 extras were 49 from `.scratchpad/`,
27 from `.remember/`, 7 `CLAUDE.md`, 4 from `.claude/`, 3 from
`.documentation/`, and 2 from `.pytest_cache/` — **51% of the queue was
dot-directory scaffolding and agent tooling notes**. This is ADR 0025's
`.state/`-junk-ingest defect (47% of Documents) returning through a different
door, and the normative text has always been path-independent: the hard denylist
is "ALWAYS excluded from ingest enumeration … anywhere".

A second exposure on the same endpoint: it had **no server-side suffix check**
at all (only non-empty and a size cap), while `safe_source_filename`
force-appends `.md` to any stem that lacks it. A direct POST of
`{"filename": "x.pdf", …}` therefore landed as `x.pdf.md` and sailed past the
downstream `ingest_document` suffix gate. The browser regex was the whole
markdown trust root, and every non-browser caller bypassed it.

### What changed

The batch endpoint receives a client-supplied **list**, so it cannot reuse the
**walk**. Duplicating the rules into a second implementation is exactly the
failure mode ADR 0025 F1 and this ADR exist to prevent, so the **predicate** was
factored out of the walker instead:

- `classify_source_relpath(rel, *, ignore_globs, ignore_dir_globs)` in
  `src/marginalia/server/_folder_watch.py` is now THE source-selection policy:
  `None` when a relative path may be ingested, else a stable reason code
  (`ignored_dir`, `non_text_suffix`, `ignored_glob`, `denylisted`). It applies
  the dir-globs (plus the unconditional `.marginalia` prune) to every parent
  component, the `TEXT_SUFFIXES` gate, the `ignore_globs` **basename** match,
  and `_is_denylisted_file` — in the order the walk applied them, which is
  load-bearing: a file under an excluded directory reports `ignored_dir` and is
  not counted as non-text, because the walk prunes that directory before ever
  seeing the file.
- `_dir_component_excluded(name, dir_globs)` is the one directory rule, shared
  by the walk's in-place `dirnames` pruning (which must decide incrementally so
  an excluded subtree is never stat-walked) and by the predicate. The pruning is
  now an optimization *of* the predicate rather than a second copy of it.
- `_iter_watch_files` keeps its walk and its `skipped_non_text` accounting but
  delegates every per-file verdict to `classify_source_relpath`. `discover_folder`
  and `/ingest-folder` are unchanged and inherit the same policy as before.
- `classify_upload_name(raw_name, …)` in
  `src/marginalia/server/_ingest_queue.py` is the batch counterpart: it
  normalizes a client-supplied name through the new `upload_rel_parts` and hands
  the result to `classify_source_relpath`. `ignore_dir_globs=None` falls back to
  `DEFAULT_IGNORE_DIR_GLOBS` exactly as `discover_folder` does, so a vault whose
  config cannot be read still gets dot-directory pruning instead of silently
  losing the entire policy.
- `upload_rel_parts` is shared with `upload_target_path`, so the path that is
  **judged** is always the path that is **written**; a hostile name cannot be
  judged as one path and materialized as another.
- `/ingest-batch` reads the selected vault's `folder_watch` globs through the
  same `_folder_watch_globs(state)` helper `/ingest-folder` now uses, so a
  customized config applies to both surfaces.

### Skips are reported, not silently dropped

The owner had just lost a run to **silent over-inclusion**, so the fix does not
answer it with silent under-inclusion. `/ingest-batch` returns exact counts
(`skipped_non_text`, `skipped_excluded`, `skipped_empty` — the first mirroring
`/ingest-folder`'s existing field) plus a per-file `skipped` list of
`{filename, reason}`, capped at 200 entries while the counts stay exact. When
the policy rejects *everything*, the 400 names the breakdown in its `detail`
rather than a generic "nothing to ingest". Empty-content uploads, previously
dropped with no signal at all, are now reported as `empty`. `BulkImport.tsx`
surfaces the counts in its scan-summary line and the per-file list behind a
"Show skipped files" disclosure.

`POST /api/v1/ingest` (the single paste/write surface) is deliberately
untouched: it is one operator-authored note, not a bulk source selection.

### Guards

- `tests/server/test_folder_watch.py::TestSourceSelectionPolicyEquivalence` —
  the drift guard. The same input list must yield the same accept/reject set
  through the walker, through `classify_source_relpath`, and through
  `classify_upload_name`, with the expected verdict pinned so an equivalence
  that agrees on the *wrong* answer still fails.
- `tests/test_server_api_v1.py::test_ingest_batch_applies_folder_source_selection_policy`,
  `…_rejects_non_markdown_suffix`, `…_accepts_legitimate_nested_markdown`,
  `…_all_rejected_400_names_the_reasons`, `…_honors_vault_configured_globs`.
