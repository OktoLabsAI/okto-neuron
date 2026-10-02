# Grafx backend (default)

Okto Grafx is an embedded, Kuzu-dialect graph database with multi-process
MVCC (multi-version concurrency control) and native vector columns. It is
Okto Neuron's **default** `GraphStore` backend: `okto-neuron init`,
`vault create`, and `onboard` all pin a new vault to `grafx` unless a
different `--backend` is given explicitly. It was added in M4 alongside
Ladybug (the original default), and a later owner decision promoted it to
the default and retired its D-12 experimental-consent gate.

## Status: default, non-experimental

Grafx is no longer gated behind `--accept-experimental` on any surface. Two
disclosures still apply and are worth knowing even though they are no
longer consent-gated:

- **Pre-alpha on-disk format.** `okto-grafx` is pinned `>=0.0.7,<0.1`. The
  directory format (`graph.grafx/`) can change between patch releases inside
  that range. Do not treat a Grafx vault as a long-term archival format yet.
  (0.0.4 verified identical behavior to 0.0.3 on the contract suite and full
  acceptance run — D-91. The floor is 0.0.7, the release the verification
  and production daemon now run.)
- **License.** Okto Grafx ships under the Elastic License 2.0 plus an Okto
  Labs Addendum. It is not OSI-approved. Okto Neuron itself remains under its
  own license; using Grafx as a storage backend is the permitted "application
  built on top" case under that license (D-14).

`--accept-experimental` is still accepted on every creation surface
(deprecated, accepted-and-ignored) so a script written before this change
keeps working unmodified. `ladybug` and `neo4j` remain fully supported,
selectable backends — pass `--backend ladybug` (or `neo4j`) explicitly to
opt out of the new default.

## Install

```bash
pip install marginalia
```

`okto-grafx` (and `ladybug`, the default search index engine's dependency)
are now base dependencies — installed unconditionally, not behind the
`[grafx]` extra. `okto-neuron init` (no flags) creates a Grafx vault out of
the box. The `[grafx]` extra still exists as a compatibility alias for older
install guidance (`pip install "okto-neuron[grafx]"` also works, and pulls in
nothing beyond what the base install already includes).

In this repo, plain `uv run ...` is enough for any command that opens or
creates a Grafx vault (`uv run --extra grafx ...` still works too).

## Selecting it

Grafx is the default, so no flag is needed:

```bash
okto-neuron init
okto-neuron vault create myvault
okto-neuron onboard
```

In an interactive terminal, `okto-neuron onboard` with no `--backend` flag
now asks which graph backend to use before creating the vault: it prints a
one-line description of each of `grafx`, `ladybug`, and `neo4j`, then
prompts with `grafx` as the default (Enter keeps it). Picking `neo4j`
interactively also asks for the storage URI (default
`bolt://127.0.0.1:7687`), the credential env var name, and the database
(default `neo4j`), then runs the same remote-endpoint consent flow as
`--storage-uri` does today. Passing `--backend` explicitly — interactively
or not — skips the prompt entirely, and non-interactive onboarding
(`--non-interactive`, or stdin/stdout not a real terminal) never shows it,
so scripted installs are unaffected.

It can also be named explicitly, with or without the now-optional
`--accept-experimental`:

```bash
okto-neuron init --backend grafx
okto-neuron vault create --backend grafx
kg init --backend grafx
okto-neuron onboard --backend grafx
```

The REST vault-create endpoint (`POST /api/v1/vaults`) takes the same shape:
`{"backend": "grafx"}`; `accept_experimental` is still accepted in the
request body but has no effect for grafx (`BackendCapabilities.experimental`
is `False`).

The chosen backend is written to `okto-neuron.yaml`'s `storage.backend` and
pinned: every later re-open or re-init is checked against that pin and fails
closed (`VaultBackendMismatch`) on a mismatch. A vault created by `kg init`
with no `--backend` flag at all is the one exception: `kg init` bypasses
`Vault.scaffold` entirely (M3 spec D-46) and, with no explicit `--backend`,
defers straight to `store.vault._open_vault`'s own scaffolding rather than
pinning anything — so it still gets the pre-existing "no `storage` key"
legacy shape, which resolves to `ladybug`, not the new `grafx` default. Every
other creation surface pins `grafx` explicitly.

## Operational differences from Ladybug

- **On-disk shape.** A Grafx vault has `graph.grafx/` (a directory), not
  Ladybug's single `graph.lbug` file. Any tooling that hardcodes the
  `graph.lbug` filename (some acceptance-scenario assertions, the REST
  rollback checkpoint gate) does not yet apply to Grafx vaults; see the known
  gaps below.
- **Staging and rebuild.** Rebuild, heal, reembed, reindex, and rollback all
  work against a staged copy that is swapped onto the live path with an
  atomic rename chain over the whole `graph.grafx/` directory (`GrafxStaging`
  in `store/staging.py`), rather than Ladybug's single-file swap. A repeated
  commit under the same backup tag (e.g. heal running right after reembed)
  is supported and does not leave stray directories behind.
- **Concurrency model.** Grafx uses optimistic MVCC: two concurrent writers
  to the same row can conflict at commit time. Okto Neuron retries under
  `storage.retry` (`RetryConfig` in `config/_vault.py`): full-jitter
  exponential backoff from `backoff_base_ms` (default 50ms) up to
  `backoff_cap_ms` (default 2000ms), bounded by `max_attempts` (default 8)
  and a wall-clock `total_cap_s` ceiling (default 30s), whichever is hit
  first. A losing writer re-reads the winning row rather than overwriting it.
- **Buffer pool budget.** Grafx's own pool defaults to 64 MiB, which thrashes
  on a graph near 180 MB. Okto Neuron passes `buffer_budget_bytes` to
  `okto_grafx.connect` from `storage.buffer_budget` in the vault yaml (an
  integer byte count or a string such as `256MiB`; accepted range 16 MiB to
  8 GiB). A vault with `inherits_application_defaults: true` picks it up from
  `defaults.yaml` (`storage: {backend: grafx, buffer_budget: 512MiB}`) when it
  does not set its own. Unset, the budget is computed at open as
  max(256 MiB, 1.5 x current graph size), capped at 1 GiB. The open logs the
  vault name, graph size, chosen budget and its source (`default` or
  `config`), and `GET /api/v1/status` reports `grafx_buffer_budget_bytes` in
  each vault's block. Staged rebuild/heal/reembed copies read the same vault
  yaml. Grafx stays in exclusive mode.
- **Checkpoint.** `GrafxStore.checkpoint()` currently reports itself as a
  no-op in `BackendCapabilities` (`checkpoint_is_noop=True`). A direct probe
  against the underlying `okto_grafx.Database.checkpoint()` (bypassing that
  wrapper) shows it does real WAL-recycling work and returns immediately
  without blocking a concurrent open writer on a separate connection — see
  the M4 gate evidence (D-56) in the internal ADR 0041 plan for the full
  observation. Flipping the capability flag and wiring a real
  `checkpoint()` body is deliberately left for a later milestone.

- **Integrity status and the write fence.** Only Ladybug enforces the
  generation-scoped integrity fence on writes (`BackendCapabilities.write_fence`
  is true for Ladybug only): its first write audits the graph generation and a
  failed or incomplete audit blocks semantic writes. Grafx never audits or
  blocks a write on its own, so a grafx vault that was never audited reports
  `integrity.status: "unverified"` with `writer_fenced: false` and a reason
  saying that no audit has run, that grafx does not fence writes and that an
  audit is optional, instead of the Ladybug "integrity state is missing"
  fence. The on-demand audit is real on grafx (`POST /api/v1/graph/integrity`,
  `audit_supported` stays true) and records `verified` or a failure. A
  recorded `failed` or `incomplete` audit keeps its `writer_fenced: true` and
  its `integrity_fenced` degraded reason on grafx too, even though grafx does
  not block writes on it.

## Dialect gaps closed for Grafx

Grafx speaks a Kuzu-flavored Cypher dialect with a few differences from what
Ladybug's writers assumed. M4 closes these for the `Node`/`Edge` tables
Okto Neuron actually uses:

- **Vectors bind as `VectorValue`, not `list[float]`.** Kuzu's vector column
  type does not accept a bound Python list directly; `store/grafx.py` wraps
  embedding values in `VectorValue` before binding.
- **Vector space is `float64`, fixed dimension.** The embedding column is
  declared with a literal dimension at DDL time (matching the configured
  embedding model), not a variable-length array.
- **`kg reembed` can read past its own dim-guard.** `GrafxStore.__init__`
  raises `EmbeddingDimMismatch` on every non-fresh open whose stored width
  disagrees with the vault's configured width, mirroring Ladybug's guard.
  `kg reembed`'s live-graph read (`cli/kg.py`'s `_open_live_store`) needs to
  open the vault at its OLD width right after the configured width has
  changed, which is exactly what that guard exists to refuse — so it can't
  go through the ordinary guarded `from_vault`. `GrafxStore.open_for_live_read`
  is a raw-open counterpart (`_skip_dim_guard=True` on the constructor) used
  only by that read path, mirroring Ladybug's `_open_live_handle` bypass;
  ordinary opens still enforce the guard. `Neo4jStore` has the identical
  guard and the identical `open_for_live_read` counterpart for the same
  reason.
- **Writes are bare `MERGE ... SET` under the retry loop**, not a
  read-then-conditional-write pattern — the optimistic-conflict retry loop
  is what makes a plain `MERGE...SET` safe under concurrent writers.
- Timestamps and tag collections are carried through Grafx's own
  `Timestamp` type and a JSON-encoded envelope respectively, matching what
  Ladybug already stores logically.

Only the `Node` and `Edge` tables that Okto Neuron's semantic writers touch
are ported — Grafx is not a general-purpose Cypher target here.

## Throughput note

One run of `./bin/acceptance.sh 98_curation_verbs` on 2026-09-08 (not a gate,
just a throughput sample): `ladybug` 37.6s wall-clock vs `--backend grafx`
36.9s wall-clock — effectively the same, both GREEN. See the dated
measurement recorded alongside the M4 gate evidence in the internal ADR 0041
plan.

## Known gaps (tracked, not blocking)

- **REST rollback** (`POST /api/v1/curation/rollback`) hardcodes a
  Ladybug-only `previous-graph.lbug` checkpoint filename in
  `server/http.py`'s `api_curation_rollback`. Against a Grafx vault there is
  no satisfiable checkpoint, so rollback returns a documented gap rather than
  succeeding. CLI-level rebuild/heal/reembed/reindex/snapshot all work.
- Some acceptance scenarios assert directly against `graph.lbug` (a
  Ladybug-only file) and are skipped, not failed, when run with
  `--backend grafx`; see scenario headers for the exact skip conditions.
