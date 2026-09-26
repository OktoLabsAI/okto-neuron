# ADR 0034: Application Daemon, Direct Loopback UI, and Client-Scoped Vaults

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** Marginalia maintainers
- **Supersedes in part:** ADR 0014, ADR 0025
- **Relates to:** ADR 0007, ADR 0009, ADR 0013

---

## Context

Marginalia currently presents an application shell but still behaves like a
single-vault service in three load-bearing places.

First, browser access is a command-line ceremony. The REST/UI port requires a
daemon capability, `marginalia ui` mints a short-lived URL, and the server
exchanges it for an eight-hour browser cookie. A direct visit, a second browser
profile, a private window, a cookie clear, or a `localhost`/`127.0.0.1` origin
change returns an HTML page instructing the user to run another command. Neither
foreground nor background `marginalia serve` opens a usable application URL by
default.

Second, the UI's selected vault is process-global. `ServerState.switch_vault()`
replaces `state.vault` and `state.vault_path`, clears the in-memory ingest and
curation queues, and rehydrates sidecars from the new path. Queue persistence,
workers, runners, and maintenance helpers repeatedly dereference those mutable
fields. The switch endpoint therefore blocks while ingest or curation is active:
without that block, an operation can finish against, or persist into, the wrong
vault. The continuous scheduler is process-lifetime and repeatedly submits
finite curation jobs, so the safety block can feel permanent.

ADR 0014 deliberately kept “the human sees one active vault”, active-vault UI
queues, one global writer lock, and a non-evicting raw-handle pool. ADR 0025
later documented that non-active folder-watch roots must be paused because the
drain worker targets the mutable active vault. Those constraints no longer match
the product requirement: one application must create, select, use, monitor, and
delete multiple vaults while work continues safely in the others.

Third, the absence of pool leases means a vault handle cannot be released or
deleted safely. `Vault.close()` is path-wide, so deleting a directory while a
request or background job still holds that handle can invalidate unrelated
work. There is also no ownership distinction between an application-created
vault and an externally discovered folder.

## Decision

### 1. `serve` starts an application, not a vault

`marginalia serve` requires no vault argument and must boot successfully with no
configured or open vault. Vault selection and creation are first-class UI
operations. A configured default remains only a compatibility fallback for
unscoped CLI/API/MCP callers; a sole registered vault is the final unambiguous
fallback. Resolving either is request-scoped and never mutates the process
fallback. With multiple vaults and no configured default, callers must select
one explicitly rather than letting the daemon guess.

The local application opens its plain REST/UI URL in the default browser after
readiness. `--no-open` is the explicit headless/CI/installer escape hatch.
`marginalia ui` remains a compatibility convenience that opens the same plain
URL; it does not mint credentials or create a browser session. Reopening the URL
from any browser profile must work.

`marginalia dev` is a keep-alive development supervisor. It always passes
`--no-open`; source changes replace the child process, and a clean or
signal-driven child exit is relaunched automatically. A positive failure exit
waits for a source change instead of entering a crash loop. Stopping development
therefore means stopping the supervisor, not terminating only its server child.

### 2. The loopback UI has no browser credential

The REST/UI surface adopts a local-application trust model:

- bind only to literal loopback and continue refusing remote mode;
- keep the loopback `Host` allowlist to resist DNS rebinding;
- reject cross-origin browser writes using `Origin` and Fetch Metadata;
- require `application/json` on JSON write routes, so HTML forms and `no-cors`
  requests cannot mutate state;
- emit no permissive CORS headers and deny framing;
- keep the existing path-confinement, SSRF, secret-redaction, and loopback-only
  sensitive-operation checks.

The REST/UI port intentionally trusts local OS processes that can reach the
user's loopback socket. A zero-step browser UI cannot simultaneously distinguish
an “authorized” browser without reintroducing a browser credential or native
wrapper. FastMCP remains capability-token authenticated on its separate port.
The surviving daemon credential is application-scoped, never vault-scoped.
Managed provider-key saves are also application-scoped. UI/API writes perform
protected file persistence off the event loop under one synchronous application
mutation lock acquired by the worker itself. Request cancellation therefore
cannot release serialization while the worker still holds a stale read/modify
snapshot, so concurrent browser tabs cannot lose one another's keys. Separate
CLI onboarding automation must not race a live UI save; portable cross-process
file locking is outside this application-daemon contract.

### 3. Every vault has one immutable runtime context

The application state owns process lifecycle, the pool, and global supervisors.
For each resolved vault path it lazily owns one `VaultRuntime` whose path never
changes. That runtime owns:

- the pooled vault lease/handle;
- its ingest queue, id sequence, cancellation state, and worker;
- its curation jobs and worker;
- its writer, config, and maintenance state;
- its scheduler debounce and surfaced status;
- its durable sidecar locations.

Queue items and curation jobs are born inside one runtime and never consult a
later UI selection. Persistence always uses the runtime's immutable path.
Ingest, curation, rebuild/reembed/heal, scheduler, and folder-watch callbacks
capture that runtime for their full operation. Separate vaults may progress
independently; each vault still serializes its own graph writes.

The process-lifetime scheduler and folder watcher iterate vault runtimes. A
watched file is enqueued into the declaring vault's runtime even when no browser
currently displays it. The ADR 0025 active-only pause is retired.

### 4. Browser selection is client-scoped

The frontend owns the selected vault for the browser tab and sends an explicit
vault selector on every vault-scoped request. One server middleware/resolution
seam validates the selector, leases the matching runtime, and attaches it to the
request. Handlers use only that bound runtime. An async response captured for an
older selection must not overwrite the new vault's UI state.

Selecting a vault does not reset server queues, wait for workers, mutate another
browser tab, or rewrite the global default. The old switch endpoint remains only
as a documented compatibility path for setting the fallback default; the SPA
does not use it. CLI clients send `X-Marginalia-Vault`, MCP keeps its per-request
selector, and both resolve through the same immutable-runtime seam. Unscoped
application status aggregates all runtimes; a selected status request reports
only that exact runtime.

Named selectors and manager references search every configured vault root. A
unique registered name resolves to that path; duplicate names fail ambiguous
rather than silently choosing the first root. Named vault creation through REST
and MCP shares the same worker-owned synchronous application mutation lock and
repeats the existence check while holding it, so cancellation cannot let another
request scaffold the same path or replace its deletion ownership marker while
the first worker still runs. The init handle is closed inside that worker; only
a successfully awaiting caller registers the lightweight runtime, whose graph
handle opens lazily on first use.

### 4a. Application defaults are available without a vault

The Config view is an application surface and remains available when no browser
vault is selected. In that state it edits `~/.marginalia/defaults.yaml`, which
uses the same typed configuration blocks as a vault. Effective configuration is
resolved in this order:

1. code defaults;
2. application defaults;
3. sparse per-vault overrides in `marginalia.yaml`.

Only application-created vaults created after this contract opt into the middle
layer, using `inherits_application_defaults: true`. Existing vaults keep their
current standalone behavior so adding application defaults cannot silently
change an established graph. A new inheriting vault stores identity, storage,
and explicit overrides only; it does not copy the full application defaults.

Vault-local controls such as folder-watch roots, reset, and immediate re-embed
remain unavailable without a selected vault. Provider probes and managed
credential storage are application operations and remain usable in the defaults
view. If an application-default provider, model, or dimension change alters the
effective embedding space of inheriting vaults, the response identifies exactly
those vaults, invalidates any cached embedders, and the UI starts their explicit
vectors-only re-embed jobs after confirmation. Non-inheriting vaults and vaults
with effective values unchanged are untouched.

### 4b. Credentials and provider connections are application resources

Secrets, provider connections, and model usage are separate concerns. Managed
API keys are application-scoped named credentials. Their metadata and stable
references live in `~/.marginalia/providers.yaml`; their values continue to use
the existing owner-only `~/.marginalia/env` store (and CurrentUser DPAPI on
Windows). Secret values are write-only: list/read APIs expose presence and
update metadata, never the value. Rotation keeps the same reference, so the
next provider call observes the new value without a daemon restart. A
credential cannot be deleted while a provider references it.

A named provider connection owns the LiteLLM driver, optional base URL,
endpoint-egress permission (`allow_remote`, default true), credential reference,
and request-compatibility mode. LLM and embedding config
may refer to it by immutable `provider_ref`; model choice, vector dimension, and
generation preferences remain at their existing call-site configuration. The
legacy inline `provider` / `api_base` / `api_key_env` shape remains readable and
writable, with the old LLM-level egress flag applying only to those inline
connections; no automatic migration rewrites established vaults. Provider or
credential deletion is rejected while an application default or vault config
still references it.

Request shaping stays centralized in the shared provider boundary. LiteLLM's
provider/model capability data determines standard and mapped parameters. Direct
connections use the installed Python adapter's `get_supported_openai_params` and
provider config. LiteLLM Proxy connections use LiteLLM's Python proxy client and
the selected model group's gateway metadata; the proxy provider's generic OpenAI
superset is not treated as per-alias evidence. The Config UI reads the same plan
and only renders applicable generation controls. When capabilities are unavailable,
a gateway alias fails closed by omitting optional sampler fields; direct providers
remain protected by LiteLLM's `drop_params`. Raw `extra_body` fields such as `top_k`,
`min_p`, and `chat_template_kwargs` are permitted only for an explicitly
extended, direct local inference connection. A loopback/private LiteLLM Proxy is
still a proxy and defaults to safe mode because it may route to Gemini, OpenAI,
or another hosted backend. Provider/model tests use the same request planner as
ingest and ask, and may expose sent/mapped/omitted parameter names but never
secret values.

### 5. The vault pool leases handles and fences destructive maintenance

The pool owns exactly one handle per resolved path and returns scoped leases,
not untracked raw lifetime ownership. It tracks active lease counts and a
maintenance fence. New leases fail once reset, reembed, rebuild, heal, or
deletion reaches its destructive boundary. Only an idle path may be
released or evicted; path-wide close happens exactly once after its final lease
ends. Bounded idle LRU eviction replaces the permanent eight-handle accumulation.

Every path-wide close follows one ownership protocol: fence the immutable
runtime, stop new work, wait for existing leases, release the pool-owned handle,
perform the wipe or atomic graph swap, install the replacement while the fence
is still held, then admit requests again. Maintenance control/status routes bind
the runtime without borrowing a graph lease so the initiating request cannot
self-deadlock and progress remains pollable. A vault A fence never blocks a
vault B lease or writer lock.

Deletion is an explicit trust-root operation, not graph mutation:

1. Every regular vault that is a direct, non-symlink child of a configured vault
   root is deletable in the app. Newly created vaults keep a persisted identity
   marker; legacy children without one receive a deterministic path identity.
   Symlinked vaults and vaults outside configured roots remain visible but are
   not deletable.
2. The request must repeat the exact vault name and the UI must show the resolved
   path and irreversible warning.
3. The server revalidates the vault identity, direct membership in a configured
   vault root, non-symlink path, regular `marginalia.yaml`, and ancestor/root/home
   exclusions immediately before deletion.
4. Queued/running ingest, curation, or maintenance work blocks deletion for that
   vault only. The deletion fence rejects new work and waits for short in-flight
   leases to drain before closing the handle.
5. Only the managed vault directory is removed. External folder-watch roots are
   never removed. Other vaults must remain byte-for-byte untouched.
6. Deleting the compatibility default clears that reversible configuration
   before the irreversible directory removal. Rollback restores the default,
   runtime, and fence only after the ownership guard still proves the directory
   intact and a released handle reopens successfully. If removal fails after
   partially changing the directory, the default stays clear, stale active and
   runtime state is detached, and the path stays fenced and unavailable; the
   request reports failure rather than claiming deletion. A successful removal
   never reports a false failure because of later in-memory cleanup. Directory
   removal runs inside the worker-owned application mutation boundary. If the
   request is cancelled, the server keeps the deletion fence and drain in place
   until that worker's real outcome is known, then either completes successful
   runtime cleanup or performs the validated rollback before propagating the
   cancellation. Repeated cancellation cannot reopen or unfence a path while
   its removal thread is still running.
7. Browser selection moves locally to another vault or the no-vault manager.

## Consequences

- Switching vaults becomes immediate while background work continues against
  its original vault. The previous curator/ingest switch warning disappears.
- Two browser tabs and MCP clients can use different vaults concurrently without
  changing one another's selection.
- Direct browser access is intentionally simpler and browser-independent. The
  security boundary is the local OS user plus loopback/cross-site protections,
  not a per-browser cookie.
- Multi-vault correctness becomes an ownership property: every request, item,
  job, sidecar, lock, watcher event, and maintenance operation has one immutable
  vault runtime.
- External vault deletion remains deliberately unavailable until a separate,
  explicit adoption contract is approved.
- Existing unscoped local API clients keep a temporary configured-default
  fallback, but new clients should always select a vault explicitly.
- The application can be configured before its first vault exists; newly
  created managed vaults inherit those defaults without duplicating them.

## Required proof

Release evidence must include:

1. clean-wheel `serve` starts without a vault, opens one secret-free URL after
   readiness, and `--no-open` never invokes a browser;
2. independent cookie jars and browser profiles load the same URL with no
   bootstrap, cookie, or `Set-Cookie` response;
3. spoofed hosts, cross-site unsafe requests, non-JSON writes, and non-loopback
   binds fail while same-origin UI writes and bearer-authenticated MCP succeed;
4. a long ingest/curation job in vault A continues while two clients select,
   query, and write vault B, with sidecars and graph provenance isolated;
5. both vaults' folder watchers drain, and a restart restores both durable
   queues to their owning runtimes;
6. managed deletion rejects wrong confirmation, symlinks, external/legacy
   vaults, active work, and active leases, then removes only the confirmed vault;
   cancellation during a blocked removal keeps the path fenced, excludes other
   application mutations, and never restores a partial path/default/runtime;
   a removal that raises after partial filesystem damage reports failure while
   keeping the broken path out of the default, runtime registry, and open pool;
7. exact-wheel CLI help, source, generated UI, documentation, workflows, tests,
   fixtures, and reports contain no private project, person, machine path, host,
   or external-corpus residue.

---

## Addendum — 2026-07-28 deletion-eligibility revision was made in place

Contract items 1 and 3 under "Deletion is an explicit trust-root operation" were
revised in place rather than superseded by a dated note, so the original text is not
recoverable from this file. Recording the change explicitly here.

The original contract limited deletion to vaults carrying an ownership marker minted
by Marginalia's own application or MCP create flow; discovered, legacy, symlinked, or
external vaults were listed but never deletable, and item 3 revalidated that *marker*
identity before removal.

The current contract broadens eligibility to any regular vault that is a direct,
non-symlink child of a configured vault root. Newly created vaults still persist an
identity marker, but legacy children without one now receive a deterministic path
identity and become deletable; item 3 revalidates that vault identity rather than a
minted marker. Symlinked vaults and vaults outside configured roots remain visible
and non-deletable, and every other guard (exact name confirmation, resolved-path
warning, regular `marginalia.yaml`, ancestor/root/home exclusion, work and lease
fences) is unchanged.

This is a deliberate widening of a destructive operation's blast radius: the
pre-revision rule was "we only delete what we created," and the current rule is "we
delete what sits directly inside a root you configured." Read the current items 1 and
3 as authoritative; this note exists so the change is not mistaken for the original
decision.
