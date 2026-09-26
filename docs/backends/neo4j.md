# Neo4j backend

Neo4j is Okto Neuron's third `GraphStore` backend, added in M5 alongside
Ladybug and Grafx (the default backend as of the owner decision retiring
Grafx's D-12 experimental gate; see `docs/backends/grafx.md`). It talks Bolt
to a Neo4j 5 Community Edition (CE) server (self-hosted or containerized)
using Cypher 5.

## Status: not experimental

Like Grafx, Neo4j is not gated behind `--accept-experimental` -- no official
backend is any more. Selecting `--backend neo4j` on any creation surface
works without a consent flag.

Non-loopback endpoints still go through the existing egress-consent flow
(built in M3, unchanged by M5): any `bolt://`, `neo4j://`, or `neo4j+s://`
URI that does not resolve to loopback requires `--allow-remote-db --yes` on
the CLI, or the equivalent consent on the web UI, before a vault is created
against it. A loopback URI (e.g. a local Docker container) needs no consent.

## Install

```bash
pip install "okto-neuron[neo4j]"
```

The `neo4j` extra is self-sufficient: it pins `neo4j>=6,<7` (the Python Bolt
driver; tested against a `neo4j:5-community` server) for the graph store,
plus `ladybug` for the default search index engine (index selection is
independent of graph backend, so every backend extra also carries the
default index engine's dependency; D-88). No separate `pip install` is
needed to run `okto-neuron init --backend neo4j`.

In this repo, `uv run --extra neo4j ...` works the same way for any command
that opens or creates a Neo4j vault.

## Selecting it

Neo4j can be selected on every vault-creation surface, each of which also
takes `--storage-uri`, `--storage-credential-env`, and
`--storage-database`:

```bash
okto-neuron init --backend neo4j --storage-uri bolt://127.0.0.1:7687 \
  --storage-credential-env OKTO_NEURON_NEO4J_PASSWORD
okto-neuron vault create --backend neo4j --storage-uri bolt://127.0.0.1:7687 \
  --storage-credential-env OKTO_NEURON_NEO4J_PASSWORD
kg init --backend neo4j --storage-uri bolt://127.0.0.1:7687 \
  --storage-credential-env OKTO_NEURON_NEO4J_PASSWORD
okto-neuron onboard --backend neo4j --storage-uri bolt://127.0.0.1:7687 \
  --storage-credential-env OKTO_NEURON_NEO4J_PASSWORD
```

`--storage-database` defaults to `neo4j` (CE's single user database; CE has
no `CREATE DATABASE`, so a mismatched name fails fast at open rather than
silently targeting a nonexistent one). Add `--allow-remote-db --yes` when
the URI is not loopback.

The REST vault-create endpoint (`POST /api/v1/vaults`), the MCP `init_vault`
tool, and the web UI's create-vault form take the same `storage_uri` /
`storage_credential_env` / `storage_database` fields, plus the matching
`allow_remote_db` boolean for the CLI's `--allow-remote-db` consent: a
non-loopback `storage_uri` without it is rejected with a 400 (REST) or a
tool error (MCP) naming the field, and the web UI shows an "Allow remote
database endpoint" checkbox once a non-loopback URI is entered. Both REST's
`api_vault_create` and the MCP tool default `backend` to the same product
default as the CLI (`config._vault.DEFAULT_NEW_VAULT_BACKEND`, `"grafx"`)
when omitted -- they no longer silently create a Ladybug vault.

The chosen backend is written to `okto-neuron.yaml`'s `storage.backend` and
pinned: every later re-open or re-init is checked against that pin and fails
closed (`VaultBackendMismatch`) on a mismatch. The credential itself is
never written to `okto-neuron.yaml` — only the *name* of the environment
variable that holds it (`storage.credential_env`), read at open time.

## Credentials

`credential_env` names an environment variable holding the Neo4j password.
The username is fixed to CE's default account, `neo4j` — CE has no
multi-user roles, so there is no separate username field.

## Schema

Every node is a single `:Node` label and every relationship a single
`[:EDGE]` type, mirroring the `Node`/`Edge` shape the other two backends use
— `(:Node {id, type, title, content, tags, facets, provenance, created_at,
schema_version, embedding, graph_generation, identity_contract_version})`
and `(s:Node)-[e:EDGE {id, type, src, dst, weight, provenance,
created_at}]->(d:Node)` as a genuine relationship (not edge-as-node), so
`startNode(e)`/`endNode(e)` give adjacency lookups natively. Application
`id` is an ordinary indexed property, never Neo4j's internal `elementId()`.

Every node and relationship also carries a `vault_id` property, and every
read, write, constraint, and wipe issued by `Neo4jStore` is scoped by it.
CE has exactly one database shared across every vault that points at the
same server — without this scoping, an acceptance-test vault and a LoCoMo
benchmark vault pointed at the same container would silently collide.

### `storage.vault_id` (D-83)

`okto-neuron.yaml`'s `storage` block for a `neo4j`-backed vault carries a
`vault_id` field: an opaque `uuid4` hex, written once at vault creation by
every writer of that block (`okto-neuron init`/`vault create`/`kg init`/
`onboard`, and `kg snapshot load` when restoring into a fresh vault). This
is the id `Neo4jStore` scopes its Cypher by — it is **not** derived from
the vault's filesystem path, so copying or moving the vault directory
(`cp -r`, a renamed mount point, restoring from a backup at a new path)
keeps it attached to the same graph.

A vault created before D-83 has no `storage.vault_id` on disk. Opening it
falls back to the legacy behavior — a SHA-256 hash of the vault's resolved
absolute path — for that open, and then adopts the id by writing
`storage.vault_id` into `okto-neuron.yaml` (an atomic replace; every other
key is preserved). From then on the vault reads its id back explicitly
instead of re-deriving it, and becomes portable the same way a freshly
created vault is. This adoption is why a legacy vault directory should not
be copied *before* it has been opened once under the fixed code — the copy
would each independently re-derive the same legacy hash from their
(different) paths only until the first open, after which each copy adopts
its own frozen id and the two stop sharing a path-derived identity (they
were never meant to be edited concurrently as the same vault anyway).

## Generation tagging (staging and rollback)

The live `GraphStore` protocol has no `generation=` kwarg and no
`snapshot()` method, so Neo4j reuses the same staging seam Grafx uses
(`StagingPort`), adapted for a server backend with no filesystem path to
stage:

- Every node/relationship carries an internal `_generation` tag. Normal
  reads and writes use the vault's current live tag, read once at open from
  a metadata singleton row and cached.
- A staged/build-mode open (driven by `Neo4jStaging`) opens a second
  `Neo4jStore` under a different tag — a fully isolated read/write view of
  the same database, using the same class.
- `GenerationScopedBackendMixin` (`store/_generation_tag_base.py`)
  implements `get_node`/`get_nodes`/`list_nodes`/`list_edges` exactly once,
  each filtering on `_generation` — there is no code path for a subclass to
  bypass the filter. `Neo4jStore` supplies only the driver/session/Cypher
  layer on top.
- Commit is an atomic pointer flip: the metadata singleton's
  `graph_generation` property is overwritten to the build tag in one
  transaction, after stashing the previous tag under a `backup_tag`
  property. Rollback (`restore`) reverses that write from the stashed tag —
  no filesystem copy, no rename chain, no no-live-window gap.
- `StagingPort.stage_path(tag)` returns a synthetic marker path (never
  written to disk) that `Neo4jStore.__init__` recognizes and unpacks into a
  build-mode open, so the rest of the codebase's `Path`-typed staging seam
  needs no change. The tag embedded in that path is not the caller's bare
  verb literal (e.g. `"heal"`) — `stage_path` mints a unique suffix per call
  (`f"{tag}-{uuid.uuid4().hex[:12]}"`), so two runs of the same verb against
  one vault (two `heal`s, two `reembed`s) never collide on one `_generation`
  tag. An earlier version returned the bare literal, which let a second
  run's commit/discard drop the live generation before the rebuild
  finished writing (see decision log D-80).
- Curation rollback (`server/_curation.py`) reads the stored `backup_tag`
  directly instead of globbing sibling backup directories by mtime (the
  Grafx/Ladybug filesystem approach) — there is nothing on disk to glob.
- `discard` reads the metadata singleton's current `graph_generation`
  before deleting anything and refuses (no-op, logged warning) when the tag
  to discard equals the live generation — a backstop against a caller
  passing back the live tag by accident, independent of the uniquify fix
  above.
- `commit`'s pointer flip also garbage-collects whichever generation
  becomes neither live nor backup as a result of the flip, bounding
  retained generations to live + one backup per `vault_id` rather than
  accumulating one row set per call.

GC of superseded `_generation` rows otherwise stays explicit-only
(`kg gc-generations` — not built in this milestone), matching the other
backends.

## Constraints and the CE gap

Bootstrap creates a composite node uniqueness constraint,
`CREATE CONSTRAINT ... FOR (n:Node) REQUIRE (n.id, n.vault_id, n._generation)
IS UNIQUE` — composite node uniqueness constraints are CE-available (only
`IS NODE KEY`, existence, and type constraints are Enterprise-only).

**Neo4j 5 Community Edition has no relationship uniqueness constraints at
all.** A best-effort composite constraint is attempted on `[:EDGE]` too and
silently skipped if the server rejects it (Enterprise-gated in some Neo4j
builds); either way, edge id-collision safety comes from the same
read-then-branch guard the other backends use in `add_edge` (`MATCH`
existing, `CREATE` if absent, else `SET`), not from DDL. The contract
suite's adversarial-write battery is the trust boundary here, matching the
same gap the plan already accepts for every backend's schema enforcement.

## Concurrency and retry

Every `add_node`/`add_edge` runs inside `session.execute_write(...)` (the
driver's own managed-transaction retry), wrapped again by
`storage.retry`'s `RetryConfig` (D-10 defaults: 8 attempts, 50ms base,
2000ms cap, 30s total wall-clock cap, full-jitter exponential backoff) via
`store/_retry.py`'s `retry_with_backoff`. The outer loop is what retries a
compound-constraint violation from two writers racing the same new id — not
one of the driver's own retryable exception classes.

## Error contract

No `neo4j` driver exception escapes the store. The driver has two sibling
exception roots: `Neo4jError` (the server reported a failure: syntax,
constraint, transient) and `DriverError` (the client side raised one on its
own: `Driver closed`, session expired, service unavailable, result already
consumed). The adapter translates both into
`okto_neuron.errors.GraphBackendError` (message prefixed `neo4j backend:`,
driver exception kept as `__cause__`) at every place it touches the driver:
driver construction, schema bootstrap, reads, writes, `wipe`, `close`, the
`Neo4jStaging` discard/commit/restore sessions, and the rollback-candidate
probe in `curation/orchestrate.py`. Failing to reach the server at open is
still reported as `MarginaliaError("failed to connect to Neo4j at ...")`.
A read on a closed store therefore raises `GraphBackendError`, which callers
catch as a `MarginaliaError` (the REST read handlers answer
`graph_read_failed` instead of `internal server error`).

Translation does not change what a write retries. Writes are translated only
after `_run_with_retry` has classified the raw exception, and a read that runs
inside a write attempt (the `get_node` probe in `add_node`) is judged by the
driver exception it wraps: a `ServiceUnavailable` or `TransientError` there is
still retried, and still ends as `Neo4jWriteExhausted` (a
`GraphWriteExhausted`) when the budget runs out. A non-retryable driver
failure on the write path becomes `GraphBackendError`. Caller-input errors
(`ValueError` for a missing edge endpoint or an off-schema node type) are not
driver exceptions and pass through unchanged. The contract test is
`tests/store/contract/test_error_contract.py`.

## Rebuild lock

Neo4j reuses the existing plain file lock keyed on the vault's local path
(`store/rebuild_lock.py`), the same primitive Grafx uses. This protects
against two rebuilds racing inside one process/host, but not two different
hosts racing a rebuild against the same remote Neo4j instance — a
fenced-lease primitive inside Neo4j itself is deferred until a real
multi-host deployment needs it.

## Operational differences from Ladybug

- **No on-disk graph file.** A Neo4j vault has no `graph.lbug` or
  `graph.grafx/` — graph data lives entirely on the Neo4j server. Locally,
  `okto-neuron.yaml` gets a synthetic `.neo4j-generation/` marker path used
  only to route staging calls; nothing under it is ever read or written as
  real file content.
- **`checkpoint()` is a capability-gated no-op**
  (`checkpoint_is_noop=True`), same posture as Grafx — Neo4j manages its own
  durability.
- **`recovery_status()` and `detect_drift()` are hardcoded** to
  `RecoveryStatus(recovered=False)` and `None` respectively — neither is
  observable over Bolt, matching the same precedent already documented for
  Grafx.
- **REST rollback** (`POST /api/v1/curation/rollback`) previously hardcoded
  a Ladybug-only checkpoint filename; Neo4j's rollback path was added
  alongside Grafx's, dispatching on backend name.

## CI recipe

```bash
docker run --rm -d --name marginalia-test-neo4j -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword neo4j:5-community
export OKTO_NEURON_TEST_NEO4J_URI=bolt://127.0.0.1:7687
export OKTO_NEURON_TEST_NEO4J_PASSWORD=testpassword   # or your chosen credential-env value
export OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV=OKTO_NEURON_TEST_NEO4J_PASSWORD
uv run --extra neo4j pytest -q tests/store tests/curation tests/cli tests/config \
  tests/test_server_http.py tests/test_server_api_v1.py tests/reconcile tests/server \
  tests/test_dependency_contract.py
docker rm -f marginalia-test-neo4j
```

The Neo4j contract suite skips cleanly (not failed) when
`OKTO_NEURON_TEST_NEO4J_URI` is unset, so the full model-free test suite
collects and passes with no container running.

The targeted `[neo4j]`-extra suite above measured ~6.5 minutes against a
local container — over the milestone's 6-minute bar for adding a Neo4j
service container to every-PR CI. As a result, `model-free-tests.yml` is
untouched; a Neo4j-backed gate (`neo4j-backend-gate`) runs instead in
`release-artifact-gate.yml`, scoped to tag pushes (`refs/tags/v*`), against
a `neo4j:5-community` GitHub Actions service container with a Bolt-readiness
poll pre-step.

**Release gate.** The release process treats
`release-artifact-gate / neo4j-backend-gate` as a required workflow on the
source SHA before tagging, and its clean-wheel loop resolves
`okto-neuron[neo4j]` in its own environment and proves `--backend neo4j`
does *not* require `--accept-experimental`.

## Known gaps (tracked, not blocking)

- The `84_neo4j_backend_selection.sh` acceptance scenario, and the
  `kg rebuild`/`kg reconcile heal`/`kg reembed` defects it had surfaced
  (a live-graph read gate that was always false for Neo4j's synthetic
  marker path, a missing `storage_config` on the heal code path, and an
  unconditional byte-copy on a failed rebuild candidate), are fixed. The
  scenario is green both standalone and in the full acceptance sweep, and
  is now wired into the tag-triggered `neo4j-backend-gate` CI job alongside
  the contract suite.
- `daemon`-side rollback (`server/_curation.py`'s `_run_rollback_neo4j`)
  is now wired and exercised end-to-end for Neo4j (a pointer-flip only,
  no filesystem copy, mirroring the commit/restore model above), covered
  by a dedicated test against a live container. Daemon-side rebuild also
  now threads the real `storage_config` through, fixing a prior crash on
  every daemon rebuild against a Neo4j vault.
- No dedicated `docs/build_knowledge_base.py` `THEMES` entry beyond
  registering this file. The release process's clean-wheel loop entry and
  required-workflow listing are done (D-87); this doc's
  registration in `THEMES` remains plan-scoped to M6.
