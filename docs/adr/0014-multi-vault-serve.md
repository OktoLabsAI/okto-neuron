# ADR 0014: Multi-Vault Serve — Per-Connection MCP Vault Selection

- **Status:** Superseded in part by ADR 0034
- **Date:** 2026-06-10
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0008, ADR 0013
- **Relates to:** ADR 0009

---

## Supersedence note — 2026-07-13

ADR 0034 retains the single pool owner, path-wide-close constraint, and explicit
per-request vault resolution seam. It supersedes this ADR's “human sees one
active vault”, active-vault UI queue, process-global writer lock, raw-handle
lifetime, and no-eviction decisions. The application now binds UI, REST, MCP,
ingest, and curation work to immutable per-vault runtimes.

## Context

`marginalia serve` was single-vault-per-process. One daemon held ONE Ladybug
vault handle and bound it to both surfaces: the UI/REST API (`:7777`) and the
FastMCP server (`:8201`). All three MCP tools (`ask`/`explore`/`remember`) hit
that single global active vault, so an agent connected over MCP could only ever
work the vault the UI happened to have selected.

This is the Pulse pattern inverted. In Pulse, one server hosts many boards and
each connection works the board it cares about; here we want one Marginalia
daemon hosting many vaults, where a project folder's `.mcp.json` pins that
project's vault by selecting it on the connection URL. The daemon keeps its UI
(the human still sees one active vault), but agents route themselves.

Two further forces:

- **A cloud seam.** A future hosted Marginalia is multi-tenant: a request's
  token maps to a tenant, which maps to an allowed set of vaults. We want that
  resolution to flow through ONE place now, even though no auth code ships yet,
  so the cloud build is a change at the seam and not a rewrite of every tool.
- **Agent-created vaults.** Agents should be able to create a vault over MCP
  (`init_vault`), not only through the REST/UI vault manager.

### The path-wide close constraint (verified in code)

`Vault.close()` → `VaultConnection.close_vault(path)` is **path-wide**: it closes
EVERY connection for that path and evicts the shared cached handle
(`store/ladybug.py`). Two `Vault` objects opened on the same path are therefore
NOT independent — closing one closes the other. This shapes the whole design:
a single owner must hold at most one live handle per path, hand it to everyone
who asks for that path, and close it exactly once.

Several existing flows (curation rebuild/reembed, vault wipe) deliberately close
the ACTIVE vault mid-lifetime — a path-wide close — and let the next access
reopen. Any pooling layer must tolerate a handle being closed out from under it.

## Decision (historical; superseded clauses retained for evidence)

1. **Per-connection selection via `?vault=`.** The MCP URL carries an optional
   `vault` query param. FastMCP exposes the in-flight request on every tool call
   (`get_http_request()`), so each tool resolves its vault per-call. A name (e.g.
   `?vault=myproject`) resolves through the registry; an absolute path resolves
   directly. No param → the active UI/REST vault (back-compat).

2. **One resolution seam: `resolve_vault_selector`.** All selection flows through
   a single pure function (testable without FastMCP). This IS the future
   multi-tenant auth seam — token → tenant → allowed-vaults maps here later, and
   every caller stays unchanged. Absolute-path selectors are loopback-only
   (mirroring the loopback-only posture of the sensitive REST routes); name
   selectors are allowed from any caller (subject to future tenancy checks).

3. **The pool owns ALL handles (`VaultPool`).** Because close is path-wide, the
   active vault and every per-connection vault are owned by one
   `VaultPool` keyed by resolved path. `get_or_open` liveness-checks each entry
   and transparently reopens a stale one, so the curation/reembed/wipe flows that
   close the active vault need no changes — the next fetch self-heals.
   `switch_vault` no longer closes the old vault; it stays pooled (an MCP
   connection may still be using it) and is closed only at shutdown via
   `close_all()`.

4. **`init_vault` MCP tool.** Loopback-only. Creates a named vault under the
   global vault root, adopts it into the pool, and returns `{name, path}`. It
   does NOT switch the active vault and does NOT set the default.

5. **Single global writer lock kept (v1).** All writes — active and pooled —
   still serialize through the one process `asyncio.Lock`. Per-vault locks are
   deferred.

## Consequences (historical; superseded clauses retained for evidence)

- **Cross-vault writes serialize.** The global writer lock means a `remember`
  against vault A blocks a `remember` against vault B. Acceptable at single-user
  daemon scale; per-vault locks are a later optimization.
- **UI ingest queue + curation scheduler are active-vault-scoped.** A pooled
  (non-active) `remember` commits to its vault but does NOT enqueue into the UI
  bulk queue and does NOT bump the continuous-curation debounce signal. Only
  active-vault ingest drives the UI surfaces.
- **Handles accumulate, bounded at `MAX_OPEN_VAULTS = 8`.** No eviction in v1 —
  eviction needs refcounting (a pooled handle may be mid-use by a connection).
  The 9th distinct path raises a typed `pool_full` error. Handles are released
  only at shutdown.
- **`switch_vault` no longer closes the old vault.** The old handle lingers in
  the pool until shutdown. This is required by the path-wide close constraint,
  not a leak: closing it would also close any connection that selected it.
- **Rebuild/reembed flows rely on pool self-healing.** They keep closing the
  active vault deliberately; the pool's liveness-check + reopen on next fetch is
  what makes that safe under pooling.
