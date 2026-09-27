# Web UI

Okto Neuron ships a built single-page web app from the REST Starlette application
(default `:7777`). It is a thin client over the locked `/api/v1/*` REST contract and
never reaches into the store directly. A sibling FastMCP ASGI application listens on
the MCP port (default `:8201`); both applications are started by one daemon, but they are
separate servers with deliberately different public operations and authentication. The
application lazily owns one immutable `VaultRuntime` per resolved vault path. Each runtime
owns that vault's handle lease, queues, jobs, locks, scheduler state, and sidecars, so work in
one vault cannot be retargeted by a selection change in another browser tab. REST/UI binds a
runtime from the tab's selector on every vault-scoped request. MCP selects a runtime per
connection and exposes only the five agent-memory tools documented below.

**MCP surface.** `serve` exposes a bearer-authenticated FastMCP streamable-http endpoint
(default `:8201`, `--mcp-port`). Each MCP connection selects its own vault via the `?vault=`
query param (ADR 0014/0034), resolves the same immutable runtime used by REST, and coordinates
writes through that runtime's per-vault lock. One daemon therefore serves many vaults without
one vault's work blocking writes to another. It is a deliberately small, graph-native
agent-memory surface of five tools:

- `ask(question, k, hops)` — a one-shot grounded answer with `k=20` by default. It honors the
  selected vault's retrieval configuration: the production default synthesizes over retrieved,
  byte-anchored source blocks; the relevance-capped efficient-hybrid subgraph path remains
  opt-in through `llm.ask.enable_subgraph`. `hops` is clamped to 1–5 and takes effect when that
  graph path is enabled. Returns `{text, citations, retrieval}`, including the effective mode.
- `explore(topic | node_id, hops, k)` — drill into the graph: seed by topic text or expand
  directly from a node id, returning the structured ego-graph (`nodes`/`relationships`/`claims`)
  so an agent can walk outward by re-calling `explore` on any returned node id.
- `remember(source, sensitivity)` — ingest + autonomously curate a source into the graph.
  `source` accepts either a path or RAW TEXT: a multi-line string, or a single-line string
  with no path shape, is materialized to a new file under `.marginalia/sources/` before
  ingest, so the byte-range provenance the graph anchors to always has a real file behind it.
- `list_vaults()` — discovery: the vault NAMES this server can reach, returned as
  `{vaults: [{name, current, backend}, ...]}`. `current` marks the vault THIS CONNECTION
  resolves to; `backend` is the pinned graph backend. Names only — no `path` and no `id`,
  so the MCP port never discloses filesystem layout (ADR 0043 D2). The server does not read
  a project's `.marginalia-vault` pin; that is the caller's job (ADR 0043 D3).
- `init_vault(name, packs)` — create a new application-managed named vault under the global
  vault root and register it (loopback-only; does not select it in any browser or set the
  configured CLI fallback). Returns `{name, path}`.

**`?vault=` selection.** The selector accepts either a registered vault NAME (resolved through
the registry, e.g. `?vault=myproject`) or an absolute PATH (loopback-only — path selectors are
refused over non-loopback connections, mirroring the sensitive-route posture). With no `?vault=`
parameter, tools use the explicit compatibility fallback, configured default, or sole
registered vault, in that order — never any browser tab's selection. Multiple vaults with no
default require an explicit selector. A project pins its vault in `.mcp.json`:

```json
{"mcpServers": {"marginalia": {"type": "http", "url": "http://127.0.0.1:8201/mcp?vault=myproject"}}}
```

Resolution flows through a single seam (`resolve_vault_selector`) — the point where a future
multi-tenant build maps token → tenant → allowed vaults. All open handles are owned by one
lease-aware pool. It keeps at most eight live handles, evicts the least-recently used idle handle
when necessary, and reports `pool_full` only when every candidate is leased, fenced, or
compatibility-pinned.

The legacy `kg_add` / `kg_query_natural` / `kg_get_provenance` tools, the flat `recall`, and
the review-queue pair were retired from MCP to avoid tool-selection ambiguity — `explore`
subsumes flat recall. `remember` serializes through the selected runtime's writer lock;
`init_vault` stays loopback-only, matching the REST write posture.

The sidebar includes a vault manager backed by `GET/POST /api/v1/vaults` and a tab-local
selector. The SPA sends `X-Okto-Neuron-Vault` on every vault-scoped request and discards an
asynchronous response if selection changed while it was in flight. Selection is immediate:
it does not close a handle, clear server work, wait for ingest/curation, mutate another tab,
or rewrite the configured fallback. The old `POST /api/v1/vaults/switch` endpoint is a
compatibility path only; the SPA does not use it.

Opening or reloading the app starts in the vault manager with no browser selection, even when
the daemon reports a configured default or only one registered vault. Those fallbacks belong to
unscoped CLI/API/MCP compatibility and are never imported into a browser tab. Creating a vault
selects the new vault in that tab; deleting the selected vault returns that tab to the manager.
A slow list refresh reads the tab's latest selection when its response arrives, so it cannot
undo a newer local selection.

The same manager can delete any idle vault that is a regular direct child of a configured
vault root through
`DELETE /api/v1/vaults/{vault_id}` after the user types its exact name. The server fences new
leases, revalidates the stable vault identity and safe configured-root path, and removes only that
vault directory after its requests and jobs drain. The configured default is cleared before
the filesystem commit point and restored with a usable pooled handle if removal fails. Busy
work blocks deletion only for that vault. Legacy children inside configured roots are deletable
without migration; symlink-backed vaults and vaults outside configured roots remain visible but
non-deletable, and external folder-watch roots are never removed. If the configured/default
vault cannot open because its graph vectors were written at an old
embedding width, `serve` still boots with no configured fallback and the vault manager shows a
repair warning instead of killing the server. The warning includes a **Re-embed vault**
button that runs the explicit vectors-only repair for that vault and then re-opens it when
the graph width matches the configured embedder again.
On narrow viewports the fixed desktop sidebar becomes a compact top shell with a horizontally
scrollable primary navigation; the content region keeps the full viewport width rather than
compressing Query or Bulk Import beside the sidebar.

## The seven views

The primary navigation is **Query · Add · Logs · Browse · Graph · Curation · Config**. The
sections below group related read, write, and operational surfaces rather than repeating the
navigation order.

**Query.** A chat-style box over the REST `/api/v1/recall` and `/api/v1/ask` routes.
`ask` synthesises an answer with citations; `recall` returns ranked content hits with scores.
MCP deliberately exposes `ask` plus graph-native `explore`, not this flat REST recall route.
Every hit renders its source byte-range provenance (`path[start:end]`), and clicking a hit
jumps straight to that node in the browser. This is the "query the vault the way Claude
does here" surface — answer text plus the grounded spans it came from.

Query history is browser-local and keyed by resolved vault path, so returning to a vault
restores only that vault's thread. The old browser-global history key is discarded because its
answers cannot be attributed safely to one vault. Selecting another vault (or resetting the
selected vault) remounts the visible view and reloads its separately keyed state; component-local
filters, drafts, and in-flight results therefore cannot bleed into the newly selected vault.
Clearing a query thread removes only the selected vault's browser-local history.

**Browse.** The KG / database browser. A type sidebar lists the closed schema — the five
primitives (Agent, Activity, InformationObject, Concept, Place) and six support types
(Document, Identifier, Annotation, Claim, Block, Finding) — with live facet counts pulled
from `/api/v1/node-types`. Every census entry carries `minted_by_ingest`: `Identifier`,
`Annotation`, and `Finding` are declared by the closed schema but have no ingest write path
(`Finding` is computed on demand by the detectors and returned as a value, never stored), so
their count is permanently 0 and the flag is `false`. That distinguishes a never-minted type
from a live type that simply has no instances yet, which is what made a zero count read as a
regression in golden-run reports. Filtering by type or name pages through `/api/v1/nodes`.
Deterministic document-structure Claims — the `has_heading`, `has_tag`, and `links_to` Claims
that ingest mints so a markdown heading, tag, or wikilink has a byte-anchored provenance
target — are hidden by default in both the list and the sidebar counts, because on a real
vault they are about a third of every Claim page. A **structural anchors** checkbox opts them
back in; it sets `include_structural=1` on `/api/v1/nodes` and `/api/v1/node-types`.
Selecting a node opens a detail panel: its incoming and outgoing edges (each clickable),
its claim provenance block (source path, byte range, content hash, document / block /
activity / agent ids), and a one-hop neighborhood graph rendered with `@xyflow/react`. The
browser is strictly read-only; the closed type set is never user-editable.

**Graph.** The committed graph overview, rendered from `GET /api/v1/graph` and
`GET /api/v1/graph/stats`. During a live ADR 0013 ingest, this view remains honest about
the file-level commit boundary: the canvas shows only graph data already committed to the
store, while a banner surfaces the active file/stage plus compact ledger counts from
`GET /api/v1/ledger/summary` (accepted pending nodes, accepted pending relationships, and
relation-curator progress). During long commits, the banner also previews the pending
semantic shape with accepted node types and top accepted predicates, so a structural-only
canvas is not confused for the final KG. The ledger summary reports raw `candidate_kinds`
separately from post-dedup `active_candidate_kinds`, and curator progress uses the active
counts, so a large extract that collapses many duplicate mentions does not look stalled
against its pre-dedup candidate total. While the file is still in extraction and has not reached the
ledger/commit boundary, the same banner also pulls the selected queue item's bounded event
log from `GET /api/v1/ingest-queue/{item_id}` and shows recent extracted candidate counts
(node mentions, topology relations, and literal claims). Newer daemon snapshots expose the
same counts cumulatively as `extracted_nodes`, `extracted_edges`, and `extracted_claims` on
the queue item. That prevents structural anchors (`Document` / `Block`) from being mistaken
for the final semantic graph while a file is still in `extracting` or `committing`.
When a bulk ingest item reaches a terminal state, the Graph view refreshes its committed
overview and stats automatically unless the user is inspecting a focused neighborhood.

**Config.** Config remains available when no vault is selected. In that state the view edits
application defaults at `~/.okto-neuron/defaults.yaml` through
`GET/PATCH /api/v1/config/defaults`; only the Providers, LLM, Embedding, and Curation tabs are
shown because folder-watch roots and destructive actions belong to a concrete vault. New vaults created by the
application store `inherits_application_defaults: true` plus explicit overrides instead of copying
the full baseline. Their effective configuration resolves in this order: code defaults,
application defaults, then sparse per-vault overrides. Existing vaults without the marker remain
standalone and unchanged.

With a vault selected, the same view controls that vault's runtime knobs, persisted as sparse
overrides to `okto-neuron.yaml` via `PATCH /api/v1/config`:

The view is divided into **Providers**, **LLM**, **Embedding**, **Curation**, **Ingestion**, and **Vault**
tabs. They share one draft and one persistent save bar, so switching sections does not discard
unsaved changes.

Changing the application-default embedding provider, model, or dimension reports exactly which
inheriting vaults have a different effective embedding space. After explicit confirmation, the UI
starts a vectors-only re-embed for those vaults; a vault that overrides the changed field is not
included. The backend and `Vault` runtime both resolve the same effective embedding configuration,
so graph bootstrap width and query/ingest embedder cannot diverge.

- **Providers** — application-wide named credentials and reusable provider connections.
  A credential value is write-only: the browser can create, rename, rotate, or delete it, but
  the API returns only its name and configured status. Secret values remain in the existing
  owner-only `~/.okto-neuron/env` store; connection metadata lives in
  `~/.okto-neuron/providers.yaml`. A provider connection owns the driver, Base URL, remote-endpoint
  permission, credential reference, parameter-compatibility policy, and an optional
  completion request timeout. The timeout is empty by default: Okto Neuron then adds no
  deadline and delegates transport policy to LiteLLM/the provider (LiteLLM may still apply its
  own default), while explicit Stop and shutdown still terminate ingest-owned request processes. A positive timeout is forwarded
  through LiteLLM and also bounds the cancellable helper. The connection can be selected by id from LLM or Embedding
  config. Connections referenced by embedding config cannot have their driver or endpoint mutated
  underneath stored vectors; create a new connection and select it in Embedding so the normal
  re-embed warning and confirmation run at the point of change. Legacy inline connection fields
  remain readable and editable for existing vaults.
- **LLM (per-step)** — the LLM config is a `defaults` baseline plus five independent
  per-step override cards: **extraction**, **merge judge**, **candidate curator**,
  **relationship curator**, and **ask**. Extraction proposes nodes, topology edges, and
  literal claims. The deterministic resolver records correlations as evidence, not as a
  final semantic decision. The merge judge adjudicates likely duplicates. The candidate
  curator evaluates each unique proposed node before graph write. The relationship curator
  evaluates each final proposed topology edge and literal claim before graph write. Candidates
  removed or remapped before that final review receive deterministic audit records by default;
  the consolidation gate can opt back into exhaustive LLM audit for those superseded node or
  relation candidates. Curator abstention is fail-closed for graph-writing candidates:
  malformed output, unavailable providers, or any non-commit verdict queue the candidate
  instead of letting resolver evidence write the graph. The Config screen starts with a compact
  workflow map that shows the
  effective provider/model for those LLM-backed steps.
  The Extraction card also owns **Concurrent chunks** (`llm.extraction.max_concurrent`):
  an execution-policy value from 1 through 32, with 1 as the effective default. It overlaps only
  independent per-chunk extractor calls. Each chunk's internal retries/enumeration/samples remain
  sequential, and candidate folding, embedding, ledgering, deduplication, curation, and graph
  writes remain ordered on the ingest thread. Saving this one execution-policy value is also
  hot for the file already being extracted: its sliding scheduler re-reads the effective limit
  within 250 ms while waiting on a provider and at every completed-chunk boundary. Increasing it
  opens new slots; decreasing it stops new submissions without killing calls already in flight.
  Model, prompt, and sampling remain fixed for that file. This value is never sent to LiteLLM or
  the model.
  Each card can set any of provider, model, base URL, the API-key *environment variable name*
  (`api_key_env`), the sampling knobs (max tokens, temperature, top_p, top_k, min_p,
  presence_penalty), an `enable_thinking` toggle, and — step cards only — a `system_prompt`.
  Any field left blank inherits from `defaults`, so a step card is a minimal diff over the
  baseline. The seeded defaults preserve historical behavior: extraction runs with thinking
  off; the judge and both curator cards run at temperature 0.2. The provider field mirrors the
  installed LiteLLM chat
  provider registry and stores canonical LiteLLM prefixes (`openai`, `anthropic`,
  `fireworks_ai`, `vertex_ai`, `together_ai`, `lm_studio`, etc.). Local OpenAI-compatible
  servers such as oMLX/vLLM are configured as `provider: openai` plus their `api_base`; Ollama
  uses `provider: ollama`; LM Studio uses `provider: lm_studio`. Legacy aliases
  (`openai-compat`, `omlx`, `local`, `together`) are accepted only as load-time migrations.
  The legacy inline Defaults card also accepts a raw API key for the single-key provider presets. The
  loopback-only `PUT /api/v1/credentials/provider` route writes it atomically to the
  same local secrets file used by `okto-neuron onboard`, immediately installs it in the running
  daemon, and returns only a server-generated `OKTO_NEURON_PROVIDER_*_API_KEY` reference. The
  browser clears its password field after success; the secret is never returned or written to
  `okto-neuron.yaml`. Providers with credential chains or multi-field authentication keep the
  advanced env-reference flow. POSIX protects the secrets file with owner-only permissions;
  Windows stores managed values as CurrentUser-DPAPI envelopes so plaintext is absent at rest.
  At daemon boot, Okto Neuron reads every discovered vault config without opening its graph,
  deduplicates the configured LLM providers, and eagerly warms their optional dependencies. A
  malformed config or missing provider dependency is isolated and logged without preventing
  other vaults' providers from warming.
  New configuration should use the named credential and provider catalog in the Providers tab.
  - **Keyless self-hosted endpoints work without any key.** Many self-hosted servers (vLLM,
    llama.cpp, LM Studio, Ollama) accept any credential, but LiteLLM's OpenAI path still
    *requires* one or it raises `Missing credentials` — which previously surfaced as silent
    zero-extraction. When a step targets a loopback or explicitly approved private-LAN
    `api_base` and no key is configured, Okto Neuron sends a harmless placeholder so keyless
    setups just work. Public and known hosted endpoints never receive the placeholder, so a
    genuinely missing hosted key still fails loud. A provider error during
    ingest is now logged and emitted as an `extraction_provider_error` trace event rather than
    swallowed.
  - Each card has two probe buttons. **Validate** (`POST /api/v1/llm/test`, loopback-only)
    is the cheap check and does not require a model to be entered first. Known onboarding
    presets use their provider-specific discovery protocol; custom OpenAI-compatible endpoints
    use a bounded `GET {api_base}/models`. Both return `{ok, models, error}` and never return
    the API key, so a discovered model can be selected before saving. Providers without a
    reliable discovery protocol disable Validate and use **Test**
    (`POST /api/v1/llm/test-completion`, loopback-only) is the expensive, honest check: it
    round-trips one real prompt through the exact `get_provider(...).complete(...)` path every
    real ask/ingest call uses and shows the actual reply (or the actual failure) — catching a
    wrong model id or a broken end-to-end call that reachability alone can't. Neither ever
    echoes the API key. The three CLI-shell providers below (`claude_cli`, `pi_cli`,
    `codex_cli`) skip Validate's HTTP probe entirely and instead check the local binary / run
    subprocess-based model discovery; Test still runs a real completion for all providers.
  - **CLI-shell providers — local binaries used as providers, no API key or `api_base`.**
    Okto Neuron ships three: `claude_cli` (the headless `claude` binary), `pi_cli` (the
    headless `pi` binary, pi.dev's multi-provider CLI), and `codex_cli` (the headless
    `codex` binary). All three share one internal `CliShellProvider` Template Method base
    class (`src/okto_neuron/llm/_cli_provider.py`) for the shell-out mechanics
    (message splitting, subprocess exec, timeout/error handling, usage-stat capture) — on
    timeout it reaps the whole process GROUP (POSIX `os.killpg`, Windows `taskkill /T /F`),
    not just the parent process, after a plain single-process kill was observed to leak
    orphaned children. The same registry lets a scoped Stop Bulk Ingest request cancel the
    active provider process tree without waiting for the CLI timeout. All three use whatever
    login the CLI already has (subscription /
    keychain / its own config) — no API key, no `api_base`. Sampling knobs (temperature,
    top_p, top_k, min_p, presence penalty, thinking) are ignored by all three — none of the
    CLIs expose generation parameters. Per-call
    latency is seconds, not milliseconds (~10–30s), so they pair best per-step (e.g. a fast
    local extractor paired with a CLI provider for ask/judge), though every step supports
    them. Each is selectable directly from the Config view's provider dropdown; selecting one
    shows an inline notice that Base URL, API key, and sampling are ignored, and the Model
    field's placeholder shows that provider's id syntax (none of the three expose a catalog
    worth defaulting to).
    - **`claude_cli`** (aliases `claude-code` / `claude_code`): `model` is passed verbatim to
      `--model` (`sonnet`, `haiku`, `opus`, `fable`, or full model ids). Schema-constrained
      calls use the CLI's own `--json-schema` flag natively. Validate checks `claude --version`
      and returns a hint list of model aliases.
    - **`pi_cli`** (alias `pi`): `model` is passed verbatim to `--model` (`provider/id`, e.g.
      `anthropic/claude-haiku-4-5`). `pi` has no native schema flag, so schema-constrained
      calls use a best-effort strategy: the schema is embedded as a prompt instruction and the
      response is repaired (fence-stripping, then balanced-brace extraction) and
      jsonschema-validated, reasking up to 3 attempts total on failure. Validate runs
      `pi --list-models` and returns the real provider/model catalog.
  - **Hosted/local HTTP-provider cancellation.** LiteLLM's generic async entry point can place a
    synchronous provider adapter in an executor, where cancelling the asyncio task leaves the
    HTTP thread alive. For an ingest-owned call, Okto Neuron instead isolates only the pure
    `litellm.completion()` request in a private helper process. Request data and credentials travel
    over stdin, never process arguments. Stop Bulk Ingest terminates that owned process tree and
    closes its connection, then the existing pre-commit checkpoint converts the active file to
    `cancelled`. Parsing, gating, and graph writes remain in the server's remember thread; once the
    atomic commit tail begins it is allowed to finish intact. Non-ingest calls retain the normal
    in-process provider path.
    - **`codex_cli`** (alias `codex`): `model` is passed verbatim to `-m` (plain OpenAI-style
      id, e.g. `gpt-5.5` — no provider prefix). Like `claude_cli`,
      `codex exec --output-schema <file>` enforces the schema natively, so schema-constrained
      responses are trusted as-is with no repair pass. Codex has no `models` subcommand and no
      model-catalog API, so Validate never returns a model list for it — an earlier version
      surfaced the CLI's own `[model_providers.*]` config.toml keys (e.g. `omlx`) as if they
      were selectable models, but those are provider *endpoint* configs
      (`base_url`/`env_key`), not model ids: live-testing proved `codex exec -m omlx` fails
      ("Model metadata for `omlx` not found ... model is not supported"). Validate now only
      confirms `codex --version` runs; the model id must be entered manually.
- **Embedding** — provider and model. Its own block, separate from `llm`. Local non-LiteLLM
  choices are `fastembed`, `sentence-transformers`, and `stub`; external embedding choices use
  canonical LiteLLM prefixes from installed embedding model metadata (`openai`, `cohere`,
  `voyage`, `bedrock`, `vertex_ai`, `together_ai`, etc.). A named `litellm_proxy` connection is
  reusable here as well as in LLM configuration; the embedding model field holds the proxy's
  embedding alias. Selecting that connection reads the gateway model catalog through LiteLLM's
  Python client, filters entries advertised with `mode: embedding`, and presents those aliases as
  a model selector. Discovery and testing are separate: the catalog populates the selector, while
  **Test** performs one real two-input batch and verifies the configured credential, response
  cardinality/order, and vector width.
  Local production providers require
  their published extras (`okto-neuron[embeddings]` or
  `okto-neuron[sentence-transformers]`), while external providers require
  `okto-neuron[litellm]`; a missing provider dependency fails explicitly and never substitutes
  the deterministic test embedder.
  Single-key embedding providers use the same managed credential pattern as LLM defaults:
  Config sends `kind: "embedding"` to `PUT /api/v1/credentials/provider`, stores only the
  generated environment reference in `embedding.api_key_env`, clears the password field, and
  never returns or writes the raw key to vault YAML. Provider/endpoint changes clear a stale
  reference instead of silently reusing another provider's key. Windows uses the same browser
  flow and recovers its DPAPI-protected value only in the owning user's process; native
  credential chains continue to use the advanced external environment-reference flow.
  **Advanced execution** owns two vector-space-neutral scheduling settings:
  `embedding.batch_size` (default 32, range 1–256) is the maximum number of texts sent in one
  provider request, and `embedding.max_concurrent_batches` (default 1, range 1–32) bounds the
  requests in flight. The pipeline collects unique extracted nodes before a real embedding stage,
  plans new relationship Claims before embedding them, and uses the same bulk boundary for
  vectors-only re-embedding. LiteLLM receives list-valued `input`; FastEmbed and Sentence
  Transformers use their native list APIs. Results are attached in source order only after the
  complete batch passes cardinality, index, and dimension checks. Saving either execution setting
  never requires a re-embed and a running embedding scheduler adopts it within 250 ms or at the
  next completed-batch boundary. Query embeddings remain single-input.
- **Ingestion / Text chunking** — `ingest.chunk_size_bytes` controls the target maximum
  extraction-window size (default 6,000; range 256–1,000,000) and
  `ingest.chunk_overlap_bytes` repeats up to that many bytes of whole trailing lines in the next
  window (default 0; always smaller than the chunk size). Windows never split a line, so an
  individual oversized line remains whole and the actual overlap can be smaller than the target.
  Every Block records the effective policy alongside its exact byte anchors; a reingest selects
  only Blocks from that policy rather than mixing old and new partitions. Both values are available
  in application defaults and per-vault overrides. Saving is non-destructive and takes effect on the
  next ingest; already-ingested sources must be reingested before their Blocks and extraction
  results reflect the new values.
- **Consolidation gate** — auto-commit threshold, review-on-contradiction, and advanced
  superseded-candidate audit toggles. The audit toggles control whether candidates already
  removed or remapped before final graph-write review also receive an extra LLM audit verdict;
  they default off so audit-only bookkeeping cannot hold a commit plan hostage.

Every save reports what it took to apply. The server returns `applied: "live"` for changes
that take effect on the next request or ingest (any LLM step — `defaults` or per-step —, the gate,
and chunking)
or `applied: "reembed"` for an embedding change. Changing the embedder provider, model, or
dimension is the one heavy change: stored vectors were written in the old model's space and
width, so switching invalidates them until the vault is re-embedded — and all three fields are
listed in `reembed_required_fields`. The UI surfaces a stark warning
before that save lands. The live embedder cache is invalidated immediately, so no daemon restart
is required. The already-open graph remains fail-closed: its fixed stored width is checked
against the freshly resolved embedder before query or ingest (and before structural ingest
writes), producing a typed re-embed-required error instead of allowing a late Ladybug commit
failure. In the Config UI, confirming a provider/model/dimension change saves the config and
immediately starts the vectors-only re-embed as one user action.

**Save is never blocked behind an in-flight ingest (2026-07-10).** `PATCH /api/v1/config`
(and the folder-watch roots add/remove routes) now serialize under a dedicated
`state.config_lock` instead of the graph `writer_lock`. An ingest item can hold
`writer_lock` for its entire run — minutes to over an hour on dense files — and Save used
to queue behind it, so clicking Save mid-ingest looked like a hung spinner. Config writes
only touch `okto-neuron.yaml` and runtime caches, which already tolerate in-flight work on
the old settings, so they no longer need to wait on the graph lock at all.

A **Danger zone** at the bottom of Config holds **Start fresh** — wipe the vault and begin
empty. It calls `POST /api/v1/reset`, which refuses while ingest or curation work is active,
then deletes the graph, vault-local derived state under `.marginalia/`, ingested sources, and
user content before re-opening an empty vault. `okto-neuron.yaml` is preserved, so the
LLM/embedder/gate config survives the wipe. The action is destructive and irreversible; the
UI gates it behind a confirm. The same wipe is reachable from the CLI as
`okto-neuron init <vault> --wipe`, sharing one `wipe_vault` helper.

Reset, reembed, rebuild, and heal share the pool's fenced replacement contract: reject new
leases for that runtime, wait for existing requests, release the one pool-owned handle, perform
the wipe or atomic swap, install the replacement while still fenced, and only then resume the
vault. Maintenance control/status routes do not borrow a graph lease, so the initiating request
cannot wait on itself and reembed progress stays pollable. Other vault runtimes remain available.

**Add.** The write surface (implemented by `IngestView`): turn raw text into queryable knowledge without leaving the
browser. Three ways in, all routing through the same companion `remember()` loop the CLI and
MCP use:

- **Paste / write** a note (`POST /api/v1/ingest`) — the content is materialized as a durable
  `.md` under `.marginalia/sources/` *first*, so the byte-range provenance the graph anchors
  to always has a real file behind it. The posted `filename` follows the same rule as batch
  upload: a clean relative path is mirrored, and an existing file with different bytes is never
  overwritten. The companion then extracts, resolves, gates, and
  commits; the response reports what committed versus what parked in the review queue.
- **Attach / drag-drop files** (`POST /api/v1/ingest-batch`) — each `{filename, content}` is
  written to the sources dir under a slugified, traversal-safe name, then queued. When the
  browser supplies a folder-relative name (`webkitRelativePath`, which `BulkImport.tsx` sends
  for a dropped directory), that relative tree is mirrored under `.marginalia/sources/` the way
  folder import mirrors a watched root. This matters: the name used to be flattened to its bare
  basename, so four `catalogo.md` files from four sibling directories all resolved to
  `sources/catalogo.md` and each silently overwrote the last. A remaining same-path clash whose
  bytes differ is re-minted under the full content hash rather than overwritten — a Block
  anchors its byte ranges to the source path, so clobbering one source corrupts another
  document's provenance. Identical content re-uploaded stays idempotent.
- **Point at a folder** (`POST /api/v1/ingest-folder`) — the server walks its own disk
  (loopback-only, like `detect-drift`), discovers every `.md` / `.markdown` / `.txt` file,
  and queues them. Each is copied into the vault's `.marginalia/sources/` (uniquified by a
  short hash of the source path), so the trust root owns every byte the graph anchors to.

Folder and batch ingest hand off to a background queue and return immediately; the view polls
`GET /api/v1/ingest-queue` for live per-file status (`queued · processing · done · error ·
cancelled`). Selecting a row fetches `GET /api/v1/ingest-queue/{item_id}` for bounded per-file
inspector events (chunks, LLM request/response, extraction, dedup, gate, commit). The
`POST /api/v1/ingest-cancel` endpoint cooperatively stops a bulk run: queued files are cancelled
immediately, and the active file stops at its next safe pre-commit checkpoint, normally after the
current synchronous model call. Cancellable CLI providers such as Codex are terminated immediately;
other providers wind down when their current call returns. Queue snapshots expose
`summary.cancel_requested` throughout that wind-down. If the atomic graph commit has already
started, its short write tail and ledger finalization finish safely while remaining best-effort
correction checks are skipped.
Like every other write, ingest is **loopback-only — even under `--allow-remote`** (matching
config-write); a remote operator tunnels in over SSH. The mechanics of the queue are in the
addendum below.

**Logs.** A read-only ingest inspection screen with two modes:

- **Queue** — backed by the ingest queue endpoints. It shows the durable queue snapshot,
  status/search filters, file-level progress and counts, a per-file event timeline, and the
  selected event payload as formatted JSON. This is the live worker view for what the Add
  screen and background queue are doing right now.
- **Ledger** — backed by ADR 0013's durable candidate ledger endpoints,
  `GET /api/v1/ledger/runs`, `GET /api/v1/ledger/runs/{run_id}`, and the compact
  `GET /api/v1/ledger/summary`. It lists ingest runs, raw candidate counts, active
  post-dedup candidate counts, comparison/plan/commit counts, extracted candidate
  summaries, pending accepted node/relation samples, the decision timeline, and a
  selected sanitized JSON record. Heavy vector
  payloads are stripped from the HTTP response and replaced with `embedding_dim`, so the
  browser can inspect process state without dumping every embedding. While ingest is
  active, or while the selected ledger run is still `started`, the Ledger mode polls
  automatically and shows a small live marker. High-volume LLM request/response events are
  retained in memory for the live inspector but queue-history persistence is batched, while
  coarse `relation_curator_progress` events are still persisted as durable heartbeats during
  long relationship review runs. Dedup passes write their own per-pair ledger comparison row
  for every judged candidate and emit a `dedup_progress` event every 25 pairs on the same
  cadence as node/relation curator progress; every LLM call log line also carries a
  `step=<extraction|judge|curator|relation_curator|ask>` tag, so a mixed-provider vault's
  logs are attributable to the pipeline stage that made the call. Stage transitions in the
  per-file event timeline are now guaranteed to both log and emit an event on every actual
  transition, so a long-running file no longer shows frozen block counts.

The Logs view does not start ingestion; it is for inspecting what the Add screen, background
worker, and curation path already produced. The Bulk Import panel also exposes an **Inspect
candidate ledger** action that opens Logs directly in Ledger mode.

**Curation.** The review-first control plane has three tabs:

- **Overview** leads with the combined attention count, graph health, recent automatic sweeps,
  and unapplied confirmed decisions; its expandable details show stats, scheduler history, and
  the in-process curation job queue.
- **Review** combines the three human-decision queues without conflating them: predicate folds,
  entity merges, and companion node candidates. Confirm/reject actions update their off-graph
  authority, predicate-alias, or review ledgers; they do not bypass the one-writer graph path.
- **Maintenance** exposes Apply/Heal, authority audit and unmerge, drift detection, and manual
  reconcile. Blocking topology work runs through the daemon-owned curation job queue, and
  confirmed off-graph decisions reach the graph through heal/rebuild rather than direct UI writes.

Continuous curation is proposal-only: the scheduler may run reconcile-propose and drift sweeps,
but never auto-applies an entity merge or rebuild. This is the UI surface for the ADR 0009/0017
control plane, not an additional ingestion pipeline.

## Running it

The app is a built artifact (React 18 + Vite + TypeScript + Tailwind + Zustand, graph viz
via `@xyflow/react`), not the no-build self-contained HTML the `docs/` pages use. The
`@xyflow/react` dependency needs a bundler, so the app surface is a built SPA while the
documentation surface stays no-build.

```bash
# install the editable CLI once
./scripts/install-dev.sh

# from the repo, build the SPA into frontend_dist/
cd frontend && npm ci && npm run build      # or: ./build.sh

# serve it (UI + REST on :7777, MCP on its own port)
okto-neuron serve
```

`build.sh` runs the npm build and emits to `frontend_dist/` at the repo root; `marginalia
serve` mounts that directory last (after the API routes) with an SPA fallback, so a missing
build simply means an API-only server — the CLI and MCP are unaffected. The Python package
ships `frontend_dist/` as package data, so a `pip install` user gets the UI without a Node
toolchain. Starting without a configured vault is valid: the UI boots to a vault manager where
you create or select a vault under `~/.okto-neuron/vaults/`, while Config remains available for
editing the defaults those new vaults will inherit.

For development, run:

```bash
okto-neuron dev
```

It builds `frontend_dist/`, starts the real `okto-neuron serve` child process, and rebuilds after
frontend source changes without restarting that child; refresh the browser to load the new asset
hashes. Python source changes restart the child immediately because a running interpreter cannot
safely replace arbitrary loaded code. Dev mode prints the UI URL but starts every child with
`--no-open`, so a rebuild or Python restart never opens another browser tab. With no `--vault`, it
always starts vault-neutral rather than pinning the only discovered vault; browser tabs select
vaults locally. The optional `--vault <name>` argument selects only the compatibility fallback
for unscoped callers. `npm run dev` remains useful when you
want Vite's own dev server on `:5180`; it proxies `/api` to the local server. The client
always uses a relative `/api/v1` base, so the built app works on whatever host serves it.
Vite 8 requires Node.js `^20.19` or `>=22.12`. Mock responses are hidden in normal production
sessions; developers can expose the persisted mock-data control with an explicit `?mock=1`
query parameter.

## Security posture

The web surface inherits the server's trust boundary rather than inventing its own:

- **REST/UI is a direct loopback application, not a browser-authentication ceremony.**
  `okto-neuron serve` opens the plain URL after readiness; `--no-open` is the headless mode, and
  `okto-neuron ui` reopens the same URL. There is no browser bootstrap route, query credential,
  session cookie, or `Set-Cookie` dependency. Any local browser profile can open the app. Server
  startup and the foreground browser launcher share a 60-second readiness budget so a cold
  dependency import on a slower Windows host does not cause a false startup failure. The
  server still refuses non-loopback binds, validates the `Host` header against literal loopback,
  emits no permissive CORS policy, denies framing, and keeps operational responses
  `Cache-Control: no-store` where appropriate.
- **Credential-free browser writes have an explicit cross-site boundary.** Mutating requests
  reject a foreign `Origin` or Fetch Metadata context, and JSON write routes require
  `application/json`. This blocks HTML-form and `no-cors` mutations while preserving ordinary
  same-origin UI writes and trusted local non-browser clients. Path confinement, SSRF checks,
  secret redaction, exact delete confirmation, and loopback-only sensitive-operation gates
  remain defense in depth.
- **MCP remains capability authenticated on its separate port.** The surviving bearer
credential belongs to the application daemon, never to a browser session or selected vault.
  MCP callers send `Authorization: Bearer <token>`; the token is never accepted as a URL query
  credential or exposed by the UI. Normal REST thin-client requests are credential-free and
  never discover or copy the MCP bearer; only an explicitly supplied compatibility token is
  forwarded by the low-level client.
- **Folder-watch commands are daemon-owned.** `watch-folder add`, `list`, and `remove` use the
  local thin HTTP client and an explicit, configured-default, or sole vault selector on every
  config and ingest request. Once registered, each root is polled and drained through its
  declaring vault's immutable runtime whether or not any browser currently displays that vault.
  If registration succeeds but the initial enqueue fails, the command identifies the partial
  result and exits nonzero.
- **Direct remote serving is disabled.** The compatibility `--allow-remote` flag remains
  accepted only to return an actionable refusal; use an SSH tunnel to the loopback listener.
  The lower-level write gates remain loopback-only as defense in depth. Config writes
  (`PATCH /api/v1/config`), graph ingest (MCP `remember`), and the vault wipe
  (`POST /api/v1/reset`) are never reachable from a remote caller, regardless of flags. The
  reset route is gated by the same loopback check as config-write (`remote_config_allowed`),
  so a spoofed `Host` or a non-loopback peer gets a 403. An operator who needs to change
  config, ingest, or wipe from another
  machine does so over an SSH tunnel, so the request still arrives on loopback. There is no
  flag that exposes these routes remotely.
- **Curation review actions fail fast instead of hanging (2026-07-10).** Reconcile-review
  confirm/reject, predicate-upkeep confirm/reject, and authority unmerge are single
  off-graph JSON writes (`AuthorityIndex` / `PredicateAliasIndex` / `ReconcileQueue` side
  files) that still serialize under the graph `writer_lock` — a curation apply job writes
  those same side files under that lock, so a second lock here would race it. But
  `writer_lock` can also be held for minutes by an in-flight ingest/rebuild/heal/reembed,
  and unlike those background jobs, a review-action click is a synchronous UI button. It now
  waits up to 5s (`_writer_lock_fast` in `server/http.py`) and returns `503 busy` if the
  lock isn't free in time, instead of leaving the button spinning behind the queue.
- **Endpoint permission belongs to a provider connection.** Named providers expose an
  `allow_remote` checkbox, on by default, which permits private-LAN and HTTPS endpoints. Test,
  LLM, and embedding calls through `provider_ref` use that provider-owned value. The older
  `llm.allow_remote` flag remains only for legacy inline LLM Base URL fields. This governs where
  the server calls out, not who may call the config route — config writes stay loopback-only.
- **Scheme and range validation.** Only `http`/`https` base URLs are accepted; numeric knobs
  (threshold ∈ [0,1], `max_tokens` > 0, temperature ∈ [0,2], `k` ≥ 1) are range-checked, and
  out-of-range values return a 400 with a human-readable `detail` shown inline on the form.
- **No raw secrets in config.** API keys are referenced by env-var name only; the yaml never
  holds a literal secret. POSIX writes managed values to an owner-only file; Windows persists
  CurrentUser-DPAPI envelopes rather than plaintext. Config PATCH proactively rejects literal-key
  fields without reflecting their value. Provider errors redact the configured key before logging
  or propagation.
- **Credentials follow provider endpoints, not independent field inheritance.** A per-step
  provider or Base URL override with no explicit `api_key_env` resolves to no key instead of
  inheriting the Defaults credential. This is a security migration for mixed-endpoint vaults:
  attach the proxy/provider key explicitly on the step when intentional.
- **The LLM Test probe is loopback-gated and key-safe.** `POST /api/v1/llm/test` is
  restricted to loopback callers (the same gate as config-write), validates `api_key_env`
  against the `OKTO_NEURON_*` namespace *before* touching the environment, runs the same SSRF
  check on the probed `api_base`, and resolves the key from the environment at call time — it
  never echoes the secret in the response, the error string, or the logs. The `stub` provider
  short-circuits without any network call.
- **Read-only graph, closed schema.** Browse cannot create or edit nodes; `type` filters are
  validated against the closed set. Writing the graph stays on the existing
  `remember` / `add` paths (loopback-only).
- **Recall/ask source spans are gated** by the same byte-read path re-validation the MCP and
  HTTP surfaces use: stored provenance paths are re-checked against the vault root before any
  source span is exposed.

## Testing

The UI contract is held in three layers. Recall quality/provenance and the HTTP security boundary
remain fast browser-free gates. A repository-owned Playwright smoke additionally drives the real
built SPA against a foreground daemon:

```bash
pytest -m eval                       # direct-Vault recall quality: entity presence at k,
                                     #   byte-range provenance, partner recall
./bin/acceptance.sh 92_eval_recall_provenance   # eval scenario end-to-end (no mocks)
./bin/acceptance.sh 93_security_hardening       # config-write + query security gate:
                                     #   SSRF allowlist, scheme/range rejection, env-only keys
npm --prefix frontend run build
npx --prefix frontend playwright install chromium   # once per machine/browser cache
npm --prefix frontend run test:release-browser
```

The marked eval directly exercises `Vault.query()` against a fixed synthetic vault. Scenario 92
drives the direct-loopback `/api/v1/recall` contract end to end and deliberately never probes an ambient LLM
endpoint. Live extraction and answer quality belong to explicit real-model scenario 56. Ordinary
server API tests separately cover the JSON response shapes the frontend uses,
pagination, config round-trips, and closed-schema type-filter validation. The security scenario
exercises the SSRF allowlist, the loopback-vs-remote policy, and the no-raw-secret rule on the
config-write surface.

The browser smoke owns a temporary HOME, vault root, REST/MCP/provider ports, foreground daemon,
browser context, and loopback OpenAI-compatible embedding stub, then removes all of them. It proves
that the built app opens directly at the zero-vault manager with no cookie; two tabs keep independent
selections and send their own `X-Okto-Neuron-Vault` headers; a tab can switch after a real durable
curation job is persisted without calling the legacy blocking switch endpoint; a fictional managed
embedding key is stored owner-only, used by a real loopback provider probe, and never written to
vault YAML; and exact-name deletion of the selected managed vault returns that tab to the manager.
`OKTO_NEURON_BROWSER_SMOKE_CLI` selects the executable under test. The release-artifact workflow
installs the exact wheel in a clean venv, installs Chromium, and points this same smoke at that wheel,
so a source-tree browser pass cannot substitute for artifact evidence.

The shell acceptance harness itself is a CPython 3.12 contract. It provisions a suite-owned
environment below `OKTO_NEURON_ACCEPTANCE_DIR`, isolates HOME plus Okto Neuron config/env/vault
state for every scenario, rejects the advertised user ports, and accepts a started server only
after its status response matches both the owned PID and the suite-owned compatibility vault.
This keeps acceptance from changing the repository environment, a running user daemon, or
`~/.okto-neuron`.

Live ingest quality is covered by `scripts/ingest_quality_check.py`, which talks to the
running server instead of importing internals. The current guardrail uses two explicit vault
checks: a CoP reset/reingest that expects SDLC concepts and clean canonical predicates, and a
LOTR reset/reingest that expects Tolkien/publication-history knowledge while forbidding SDLC
taxonomy leakage. The script also distinguishes raw provenance `Document` / `Block` anchors
from extracted knowledge nodes, so parser labels such as `paragraph 1` only fail the check if
they appear as knowledge candidates rather than source anchors. It also supports
`--domain-profile lotr`, which reports grouped LOTR coverage (characters, places, artifacts,
works/contributors) and keeps SDLC taxonomy leakage as a named forbidden-title set. During a
long-running ingest, the same report inspects retained `extraction_result` queue events and
can fail on `--min-extracted-node-mentions`, `--min-extracted-relation-candidates`,
`--min-extracted-claim-candidates`, and `--min-extracted-domain-profile-coverage`, so a
committed-only structural graph is not mistaken for an empty extractor. Its ledger-detail
section also reports raw `candidate_kinds` beside post-dedup `active_candidate_kinds`, so
progress checks use the real curator workload after exact/judged dedup passes. The node-review
summary also reports same-title/multiple-type conflicts, which catches generic extraction
inconsistencies such as a named artifact being proposed both as an `Agent` and a `Concept`.
For long file-level commits, `--min-pending-nodes` and `--min-pending-relations` assert that
the accepted pre-commit KG is growing even when `/graph` still shows only structural anchors;
the report includes sample accepted node candidates and relationship triples so the pending
KG can be inspected without waiting for the commit plan to land. With `--domain-profile`,
the ledger detail also reports pending-domain-profile coverage over accepted pending nodes,
and explains missing expected entities when matching candidates existed but were queued or
superseded. That keeps "not extracted" separate from "extracted but lost at dedup/curation."
The LOTR check exposed that exact duplicate collapse can hide better later mentions behind a
weak first mention; within-file duplicate selection now uses source-grounding evidence while
leaving non-duplicates and edge-connected same-title endpoints stable.
The JSON report also compacts the ingest queue by keeping item status, counts, stage, and
last-event metadata while omitting heavy LLM request/response payloads; detailed prompts remain
available through the Logs screen and per-item API, but the guardrail output stays reviewable.
The June 9 reports were ephemeral local evidence under `/tmp`; they are not durable project
artifacts. Current release evidence belongs in the tracked state ledger and CI runs.

## Addendum · 2026-05-29 — graph quality on the content surfaces

The Query and Browse views are content surfaces, and they now show asserted knowledge rather
than the machinery that produced it:

- **Infrastructure is filtered; Blocks stay inspectable.** System and LLM extraction
  Agent/Activity nodes are graph provenance, not entities, and are excluded from `recall`, `ask`,
  and Browse through their durable mint-time marker. Raw `Block` chunks are anchoring substrate,
  not answers, so untyped `recall` and `ask` exclude them. Browse's default All listing and
  support-type counts include Blocks, and an explicit `type=Block` filter isolates them, keeping
  the provenance substrate directly inspectable.
- **Document structure is hidden, not dropped.** `has_heading` / `has_tag` / `links_to` Claims
  restate a document's own structure and are section labels rather than knowledge, so Browse,
  the graph views, and their counts omit them unless `include_structural=1` asks for them. They
  remain first-class, provenance-bearing graph nodes: `GET /api/v1/nodes/{id}` still resolves
  one, and it is still valid as a neighbors seed, so a direct link never 404s. The filter keys
  on the Claim predicate rather than the `_salience` facet — that facet also marks relation
  endpoints the companion auto-promotes, which are genuine entities.
- **Titles keep their source spelling.** Entity titles are normalized in SHAPE only —
  whitespace, spacing around `/`, a trailing `/`, a leading generic "The". Casing is never
  rewritten: source spelling is provenance (ADR 0040 rejects "normalize names by overwriting
  titles"), and the extraction pipeline carries no language signal, so a per-token recasing
  pass could only ever encode one language's conventions. It previously encoded English, which
  rendered every Portuguese title wrong ("Serviços Do Contribuinte" for "do"). Matching does not
  depend on display casing — every dedup layer keys on `semantic_surface.exact_surface_key`,
  which casefolds — and node identity now folds the title too, so the casing an entity happened
  to arrive with first is no longer baked into its id.
- **A container is not an entity.** File names (`bookkeeping.md`, `orchestrate.py`) are dropped
  at the extraction gate, and a directory label's trailing `/` is stripped so it collapses onto
  the bare name instead of forking a second Concept ("Guias/" vs "Guias"). The filter keys on a
  known file extension and nothing else, because real entities do contain `/` and `.`
  (`PIS/COFINS`, `CNAE 6201-5/01`, an `N.V.`-style company suffix).
- **The extraction prompt carries no genre-specific typing rules.** A rule was briefly added to
  steer how software work items are typed, because one corpus mixed project-tracking files with
  other material. It was removed, and the episode is worth recording because both of its
  failure modes are general. First, it illustrated itself with concrete identifier-shaped codes,
  and a local model read those examples as a naming template rather than as illustrations:
  against documents that name the same kind of item using another language's own noun and a
  number, it rewrote them into the example's prefix form and left the correctly-named nodes in
  place, manufacturing a duplicate per item — in a change whose purpose was removing duplicates.
  An example in a prompt is a pattern to copy, so a rule about preserving the source's own
  wording must never display a competing wording. Second, and more fundamentally, the rule was
  fitted to one corpus's document genre and validated against that same corpus; it corrected a
  cosmetic inconsistency in one genre while corrupting another genre in the same vault. A prompt
  shared by every vault should encode what the closed schema means, not how any one corpus
  happens to label its documents.
- **Entity resolution costs a bounded number of LLM calls per candidate.** Which pairs are
  offered to the merge judge is deliberately generous — a low embedding score never vetoes a
  pair, because blocking optimizes recall while the judge optimizes precision (ADR 0042). But
  the lexical lane scans committed nodes of the same type, so without a bound the number of
  judge calls would grow with the size of the graph rather than with the document being
  ingested: on a live 76-document run, judge calls for the same first three documents rose
  4.7x as the store filled, and one document spent over seventeen minutes committing. Matches
  are now ranked strongest-first — a deterministic name-containment match outranks a merely
  similar spelling — and only the top few are judged, alongside the embedding lane's own cap.
  The budget is per candidate and independent of graph size, so ingest speed stays steady as a
  vault grows. Recall itself is not capped; ranking and truncation happen after it.
- **Dedup is authority control.** Two mentions of one entity in a note collapse to a single
  node before staging, with edges remapped to the survivor — the FRBR/LRM "manifestations of
  one work" reconciliation step, so Browse shows one node per entity, not duplicates.
- **Recall is question-aware.** For interrogative queries (`who is the partner on
  Okto Neuron?`), the Claim that *answers* the question is ranked above the bare topic entity
  it is *about*, the canonical KGQA move of ranking the incident triple rather than the focus
  entity. The ranking is deterministic — same query, same order. The grounding is established
  IR/library science: KGQA triple-ranking, coordination-level matching for term coverage, and
  FRBR/LRM for entity reconciliation.

## Addendum · 2026-05-29 — bulk & folder ingest, async queue

The original Add view introduced one in-process background worker for the then-active vault.
ADR 0034 supersedes that process-global limitation: every immutable vault runtime now owns the
same queue/worker contract described below, so separate vaults can drain independently while
each vault still honors its one-writer invariant.

- **One worker per vault runtime, one file at a time within that vault.** The
  blocking part of `remember()` is the synchronous LLM extraction HTTP call, so the worker runs
  it off the event loop via `asyncio.to_thread` — the `/api/v1/ingest-queue` poll and ordinary
  reads stay responsive while a file is being extracted. The runtime's `writer_lock` still
  serializes that vault's writes one at a time, and each store op opens its own Ladybug connection, so an
  off-loop read never shares a connection with the worker. Granularity is per-file, matching
  `remember()` being atomic per source.
- **Per-file isolation.** One bad file flips to `error` with its message and the queue keeps
  draining — a single failure never kills the run.
- **Trust root preserved.** Pasted notes and uploaded files are written to
  `.marginalia/sources/` under a slugified, traversal-safe name (a content-hashed
  `note-<hash8>.md` when no usable name is given, so repeated pastes never clobber an earlier
  source's provenance). Folder ingest copies each discovered file into the same sources dir,
  uniquified by a short hash of its origin path so same-named files in different subfolders
  never clobber one another. Everything the graph anchors to therefore lives under the vault
  root — which is exactly what `remember()` re-validates before it commits — so the byte-range
  provenance always points at a real `.md` the vault owns. Markdown stays canonical.
- **Backstops.** A single enqueue is capped at `MAX_ENQUEUE = 2000` files (folders past that are
  truncated, flagged `truncated: true` in the response); a single uploaded file is capped at 2M
  chars. Only `.md` / `.markdown` / `.txt` are ingestible — the trust root is text.
- **Loopback-only, always.** All three ingest routes (`/api/v1/ingest`, `ingest-folder`,
  `ingest-batch`) reject non-loopback callers even under `--allow-remote`, the same posture as
  config-write. `ingest-queue` is read-only status.

## Addendum · 2026-06-01 — durable queue + within-file progress

The async queue gained two properties:

- **Restart-durable history.** The queue persists a sidecar at
  `.marginalia/ingest-history.json` with file names, statuses, counts, timestamps, and bounded
  per-item inspector events. Inspector events can include local chunk text and LLM
  request/response bodies, capped by the server before persistence. `persist()` is best-effort and
  writes through a temp file + atomic rename on every state change, so a partial write can't corrupt
  the history. On startup `rehydrate_queue()` reads it back, so a server restart no longer drops the
  batch record. Items caught mid-flight (status
  `processing` at crash time) are treated as crash-interrupted; re-running them is safe because
  `remember()` re-anchors against the same byte-addressed `.md` under the vault root rather than
  appending blindly.
- **UI re-hydrates on mount.** The Add view fetches `/api/v1/ingest-queue` when it mounts, so a
  browser refresh during a long run shows the live queue instead of an empty board.
- **Within-file progress.** Each `processing` item now reports a `stage` label
  (Parsing → Extracting → Embedding → Dedup → Committing) plus `blocks_done` / `blocks_total`, so a
  large file shows real motion in the BulkImport progress bar instead of a frozen spinner. These are
  additive status fields on the queue item; the poll contract is unchanged.

## Addendum · 2026-06-01 — global ingest awareness + warm-recovery

Two follow-ups widened the queue's reach and hardened boot:

- **Every deterministic store path was made visible in the queue.** At the date of this addendum,
  `/add` (REST) and MCP `kg_add` recorded `status=done`, `stage=stored` entries. The legacy
  `kg_add` tool was later retired from the current five-tool MCP surface in favor of `remember`;
  `/add` remains visible, while MCP `remember` uses the autonomous ingest path.
- **Cross-session, cross-tab awareness.** Queue state is lifted into the app-level store with one
  shared `useIngestPoller` (1.2 s while active, 5 s idle). Every poll carries the tab's selected
  vault, so a job started through that vault's REST queue or another tab is discovered without
  conflating another vault's queue. MCP `remember` writes through the selected runtime's shared
  Companion/writer path but does not enter its browser bulk-import queue. A sidebar
  **"Ingesting N/M"** indicator is visible from *any* view for the selected vault.
- **Warm-recovery from a corrupt graph.** `bootstrap_vault_graph` no longer crashes on a corrupt
  Ladybug graph (the `kill -9` / torn-WAL case). The first implementation quarantined the whole
  graph and booted empty. ADR 0029 superseded that fallback: current code quarantines only a torn
  WAL/sidecars and reopens the intact checkpoint when possible; it quarantines the whole graph and
  boots empty only when the checkpoint itself cannot be recovered. Either path flags the recovery
  mode and never touches the Markdown trust root.

## Addendum · 2026-06-03 — per-step LLM config + LiteLLM substrate

The single flat `llm` block in the Config view became per-step, and the provider substrate
moved to LiteLLM:

- **Per-step cards.** Config now shows an `llm.defaults` baseline plus five override cards —
  **extraction**, **merge judge**, **candidate curator**, **relationship curator**, and
  **ask** — each a `StepLLM` that inherits any unset field from defaults
  (`LLMConfig.resolved(step)` does the merge). Editing a step writes only the fields that
  differ, so a permutation swap is a tiny `okto-neuron.yaml` diff. The config-write path
  deep-merges a nested `llm` patch, so a one-field step edit never clobbers the rest of the
  block, and the seeded step defaults (extraction `enable_thinking=False`; judge and curator
  `temperature=0.2`) survive a patch instead of reverting to the inherited baseline.
- **LiteLLM substrate.** `OpenAICompatProvider` was removed in favor of `LiteLLMProvider`
  (lazy `import litellm`, optional `[litellm]` extra, not core); the model string now uses
  canonical LiteLLM prefixes directly (`openai/<model>`, `anthropic/<model>`,
  `together_ai/<model>`, `lm_studio/<model>`, etc.). Hosted and proxy providers receive only
  capability-supported parameters; `top_k` / `min_p` / `enable_thinking` ride in
  `extra_body` only for explicitly extended direct local inference connections. Hosted providers are called without
  Okto Neuron's default local `api_base`; endpoint-backed providers and any provider with an
  explicitly changed `api_base` receive that URL. `StubLLM` remains an explicit deterministic
  CI/offline-test provider; production defaults to the configured local OpenAI-compatible
  endpoint and never silently substitutes the stub. Existing configs that still say
  `openai-compat`, `omlx`, `local`, or `together` are canonicalized during config load.
- **Structured JSON calls.** Extraction, merge-judge, reconciliation cluster-judge, candidate
  curator, and relationship curator calls now pass LiteLLM `response_format` JSON schemas
  instead of relying only on prompt wording. The old tolerant parsers remain as a fallback: if
  a local OpenAI-compatible server rejects structured output parameters, Okto Neuron retries
  that call without `response_format` and still parses the returned text defensively. Missing
  or malformed curator output queues the candidate rather than committing from resolver
  evidence alone. Committed relations also canonicalize a blank curator predicate from the
  original extracted predicate, so common CoP variants such as `uses_analogy`,
  `failure_mode`, and `red_flag` still land on the preferred graph vocabulary.
- **Test affordance.** Each card's **Test** button calls `POST /api/v1/llm/test` — a
  loopback-gated probe that does a bounded `GET {api_base}/models`, SSRF-checks the base URL,
  validates `api_key_env` against the `OKTO_NEURON_*` namespace, and returns `{ok, models,
  error}` without ever echoing the key. Per-step LLM changes apply `live`; an embedding
  provider/model change requires a vectors-only re-embed but no daemon restart. The completion
  test also reports parameter names sent, mapped to a local raw body, or omitted. Hosted and proxy
  connections use capability-gated parameters; raw `top_k`, `min_p`, and similar sampler fields
  are only enabled by an explicit `local_extended` policy on a direct private/loopback inference
  driver. LLM cards now start with no optional parameters, so selecting a provider connection and
  model sends no hidden sampler defaults. The collapsed **Advanced parameters** editor reads typed
  descriptors from one backend capability contract and adds only values the user selects. Direct
  providers are described by the installed LiteLLM Python adapter; a LiteLLM Proxy is described by
  the selected model group's gateway metadata. Per-step maps inherit defaults, may override them,
  or use a `null` tombstone to omit one inherited parameter. Request-owned fields such as messages,
  credentials, tools, streaming, and structured-output schemas remain visible to the backend but
  are not editable. Existing typed YAML sampler fields remain readable and appear in the same
  advanced editor until removed. Runtime request shaping rechecks the same capabilities and records
  every sent or omitted value in the completion-test parameter plan.

## Addendum · 2026-06-03 — Graph view (full-screen WebGL explorer)

A fifth view joins the four above: a top-level **Graph** tab. The Browse view's one-hop
mini-graph (rendered with `@xyflow/react`) was good for inspecting a single node's
neighborhood, but it could not show the shape of the whole vault. The Graph view is a
full-screen, zoomable WebGL explorer (`sigma.js` + `graphology`) with a ForceAtlas2 layout
run on a worker, so the force simulation never blocks the UI thread.

It is a pure read/visualization surface. It adds no write path, and it does not touch the
ingest / resolution / `remember()` pipeline, the closed 5-primitive schema, the provenance
model, or the confidence gates — it only renders what the store already holds, through three
new read-only routes under the locked `/api/v1` contract:

- **`GET /api/v1/graph`** — a capped, filtered *overview* subgraph, never a raw dump. Params:
  `types`, `relations`, `limit` (default 1500, hard cap 5000), `min_degree`. It picks the
  top-`limit` nodes by in-filter degree, returns only edges whose **both** endpoints survive
  the cap, and sets `truncated` when the cap dropped nodes the filters kept — so a large vault
  renders its densest core instead of erroring on size. Unlike `/nodes` (which 400s on
  `limit` over its max), the graph routes *clamp* to the hard cap.
- **`GET /api/v1/nodes/{id}/neighbors`** — incremental click-to-expand from a seed node.
  Params: `hops` (1–3), `limit`, `types`, `relations`. Clicking a node in the canvas pulls in
  its neighbors so you grow the picture outward instead of loading everything at once.
- **`GET /api/v1/graph/stats`** — closed-schema node-type counts plus open-vocabulary
  edge-type counts (and totals), feeding the type-filter controls.

The same content-vs-plumbing rule that governs recall, ask, and Browse holds here:
closed-schema node types only, `is_infra` nodes excluded, and deterministic
document-structure Claims omitted unless `include_structural=1` is passed — so internal
infrastructure machinery never appears in the graph. A neighbors *seed* is exempt from the
structural filter, so expanding outward from a heading anchor you linked to still works. The overview defaults to the semantic layer
(`Agent`, `Activity`, `InformationObject`, `Concept`, `Place`, `Claim`) so source-carrier
`Document` and chunk-anchor `Block` nodes do not dominate the visual layout. The Graph
filter rail has a **Provenance anchors** toggle for opting those nodes back in when debugging
source attachment. Unknown node-type filters return `400`; edge `relations` are an open,
pack-defined vocabulary and pass through unvalidated. Type filters and a click-to-inspect
detail panel sit over the canvas. An app-level `ErrorBoundary` and a WebGL-availability
fallback in `GraphView.tsx` degrade gracefully when WebGL is unavailable rather than blanking
the app.

## Addendum · 2026-06-08 — ingest inspector + cooperative stop

- **Per-file inspector.** Queue snapshots stay lightweight: `GET /api/v1/ingest-queue` returns
  `event_count` and `last_event`, while `GET /api/v1/ingest-queue/{item_id}` returns the selected
  item's bounded `events` list. The companion emits chunks, LLM request/response, extraction
  results, embedding counts, dedup passes, confidence-gate decisions, and commit summaries.
- **ADR 0013 boundary.** Queue events remain the lightweight live inspector for the current
  `remember()` worker. The durable pre-commit workbench is now the separate Ledger mode: it
  reads persisted candidate records, comparison records, commit plans, commit records, and
  review-action records from `<vault>/.marginalia/candidate-ledger.jsonl`.
- **Cooperative stop.** `POST /api/v1/ingest-cancel` marks queued files `cancelled` immediately and
  records one idempotent stop request on the processing file. `summary.cancel_requested` keeps the
  UI in an explicit **Stopping...** state during wind-down. An active CLI process tree or isolated
  LiteLLM HTTP helper is stopped; the active file then becomes `cancelled` at the next
  stage/model-call boundary, and no further model calls or files start. Once the confidence gate
  begins its atomic graph-write tail, that tail finishes before the worker exits so graph writes are
  not left half-applied. A cancelled pre-commit ledger run remains resumable on a later ingest.
- **Server shutdown.** The first Ctrl-C/SIGTERM enters drain, stops all active model calls, and
  pauses the processing file back to `queued` so a later server start can resume it; it does not
  discard the durable bulk queue. One absolute deadline bounds tracked REST/MCP requests, both
  transports, scheduler, folder watch, ingest and curation workers, writer-lock acquisition, and
  vault close. `okto-neuron stop` automatically repeats the signal after the cooperative grace
  interval; a manual repeated signal is the same explicit escalation and force-stops remaining
  CLI provider process groups and LiteLLM helpers. Use `okto-neuron status` to see the PID, version,
  application/runtime health, recovery warnings, and exact open/stop commands. REST owns the human Uvicorn
  lifecycle stream; the MCP transport still logs warnings/errors but suppresses duplicate INFO
  startup and shutdown lines for the same process.
- **Daemon identity and PID safety.** `<runtime-root>/.marginalia/server.pid` is a versioned owner
  record held under an OS file lock for the daemon's full lifetime. It records the PID, a
  process-birth fingerprint, and a random owner identity. The kernel lock makes concurrent starts
  mutually exclusive and is released automatically after a crash, so stale records are reclaimed
  without trusting a recycled numeric PID. Bare `okto-neuron stop` considers only verified owners,
  so a dead legacy PID file cannot create a false "multiple daemons" error. POSIX birth tokens force
  the C locale; a bounded migration accepts older localized tokens only for a still-running
  Okto Neuron serve command, and still uses the owner-targeted request rather than a numeric signal.
  `okto-neuron stop` validates the lock, birth fingerprint, and owner identity, then writes that
  request; the lock-owning daemon validates it and signals itself. Escalation remains tied to the
  same owner, so a replacement or unrelated process that later receives the old PID is never
  signalled. If the owner disappears during a shutdown process-table read, polling rechecks the
  exact PID-file path and lock: only a disappeared path or released lock counts as stopped, while a
  still-locked unverifiable owner or a concrete conflicting birth token remains fail-closed. The
  transactional installer matches the verified daemon PID to its actual lock
  root, invokes the staged candidate's stop implementation, and surfaces a stop failure immediately
  instead of suppressing it and reporting a fictitious long drain. A bounded upgrade bridge
  handles legacy integer-only PID files started through either the module or installed console
  entry point: before its first signal it requires a matching process-birth fingerprint, the
  Okto Neuron `serve` command identity, matching health PID, exact vault identity
  (or the canonical global runtime root), and a valid version response. Every escalation rechecks
  the captured birth identity; any missing or contradictory evidence refuses the legacy signal.
- **PID record removal on a clean stop.** On POSIX the daemon unlinks `server.pid` while it still
  holds the lock, so no newcomer can claim the path in between. Windows refuses to delete a file
  that any handle (including the daemon's own) still has open, so there the daemon closes the record
  first and then deletes it only if it is still unlocked and byte-identical to the record it owned; a
  newcomer keeps its handle open, so its record is never removed. Short sharing violations from
  readers such as the `stop` poller are retried. A removal that still fails is logged as
  `lifecycle.remove_failed` rather than swallowed, `stop` removes a dead owner's record the same
  way, and a start that finds one logs `lifecycle.stale_pid_reclaimed` and takes it over. Before
  0.3.1 every clean stop on Windows left the record behind.

## Addendum · 2026-09-14 — distinct Document titles, and an `empty` outcome that isn't an error

Two ingest correctness fixes, both from a live-vault audit:

- **Document titles no longer collide across directories.** A Document's title used to be the bare
  file stem (`p.stem`), so every same-named file anywhere in the ingest tree rendered identically —
  one live vault had 69 Document nodes but only 56 distinct titles (`SPRINT` x7, `sha256` x4,
  `catalogo` x4, `README` x2), making same-named files from different projects indistinguishable in
  any list, search result, or graph view. The Document *id* was always unique (a hash of the
  resolved absolute path), so this was a display-only bug. The title is now the document's path
  relative to the ingest root it was added under, extension dropped — e.g.
  `projects/alpha/notes/sprint-06/SPRINT` instead of the bare `SPRINT` — with an explicit
  frontmatter `title:` still taking precedence, and the bare stem as the fallback when no ingest
  root is known (a direct `parse_markdown()` call outside a vault, or `allow_external_sources`).
  Folder/CLI ingest mirrors a file's original tree under
  `.marginalia/sources/<root-key>/<relpath>` before parsing it (F11); the fix recovers that
  original relative tree by stripping the fixed `.marginalia/sources/` prefix and the 16-hex
  `root-key` directory `durable_copy_path` writes, so the displayed title matches how the user
  actually organized the source, not the durable-copy scaffolding. See
  `marginalia/ingest/markdown.py::_document_title`.
- **An ingest that legitimately found nothing to extract is no longer reported as `failed`.** Four
  documents in the same audit — three checksum manifests, one short task ticket — ended
  `status=error`, `outcome.quality=failed`, `error_class=empty_after_retry`, `provider_failures: 0`.
  Nothing broke; the extractor ran its full retry and correctly found no entity-grade content. That
  shape (every unresolved unit is `empty_after_retry`, zero provider/transport/parse failures) now
  gets its own result-quality value, `empty`, distinct from `failed` — see ADR 0039's 2026-09-14
  addendum for the exact classification rule and why the queue/health checks needed no change. The
  Add view's outcome badge renders `empty` with a neutral (non-alarming) "Empty" label — the
  document is still visibly stored, just flagged as having contributed no extracted knowledge,
  instead of vanishing into the same bucket as a broken LLM endpoint.

## Addendum · 2026-09-15 — raw sampling-payload override, and the old "advanced parameters" dropdown removed

The old "advanced parameters" dropdown — a `parameters`-map field on `LLMDefaults`/`StepLLM`
with a `MANAGED_LLM_PARAMETERS` allowlist and a `/llm/parameter-capabilities` REST endpoint that
populated a "Select a parameter" / "Add" control from LiteLLM's own capability introspection —
was deleted outright (no migration, no deprecation period: no real user vaults depend on it).
Removed: the `parameters` field itself on `LLMDefaults` and `StepLLM` (and their `_v_parameters`
validators), the `/api/v1/llm/parameter-capabilities` endpoint and its
`/api/v1/config/defaults/...` alias, and the Web UI's dropdown/list editor and its capability
fetch. Kept, because something real still depends on each: `ResolvedLLM.parameters` (still the
fold target for the three parameters-only typed fields — `repeat_penalty`, `reasoning_effort`,
`preserve_thinking` — and still settable directly in the `/llm/test-completion` probe's request
body for an ad hoc, non-persisted test), `_check_llm_parameters`/`MANAGED_LLM_PARAMETERS`
(guarding both of those), and `parameter_capabilities()`/`LLMParameterCapabilities`/
`parameter_descriptor()` in `okto_neuron.llm` (still used by `LiteLLMProvider.complete()` itself
to decide which typed sampler fields a model/provider actually supports, and independently unit
tested — this is capability introspection for request-shaping, not the removed dropdown).

In its place, `LLMDefaults`/`StepLLM` gained a second, deliberately unconstrained field:
`sampling_payload: dict[str, JsonValue]`. It is a raw sampling-payload override, per LLM role, for
an operator running a self-hosted model who needs to send a parameter Okto Neuron does not know
about (`typical_p`, `stop_token_ids`, a nested `grammar` object, provider-specific decode knobs,
…) without the application whitelisting it first. The Web UI's LLM config now has a matching raw
JSON editor for it — see the UI bullet below; it replaces the deleted dropdown one-for-one as the
place to set anything the typed sampler fields don't cover.

- **Reserved keys only — no whitelist otherwise.** `sampling_payload` refuses exactly ten keys,
  in two categories, each already owned by Okto Neuron's own request contract, connection
  routing, transport policy, or response shape: six **connection-owned** keys — `model`,
  `messages`, `api_base`, `api_key`, `drop_params`, `timeout` — plus four **response-shape** keys
  added the same day — `stream`, `n`, `tools`, `extra_body` — which change the SHAPE of the
  response Okto Neuron has to parse, not the sampling of it (the same category as blocking
  `messages`, not a whitelist on tuning: `stream: true` or `n: 3` would not fail cleanly, they'd
  produce a downstream parse failure — exactly the opaque failure this feature exists to
  eliminate; `extra_body` collides with the container `LiteLLMProvider.complete()` itself builds
  by routing every non-standard payload key into it). Every genuine sampling parameter stays
  unrestricted. The rejection message names which category the key belongs to (and why) — the
  Web UI's editor repeats that distinction client-side, but the backend validator is still the
  authority. Setting any of
  them raises a validation error naming the key. Every other key passes through untouched — no
  name pattern, no range check, no capability probe. The backend is the validator, deliberately:
  an unsupported key now surfaces the backend's own rejection instead of being silently dropped or
  clamped.
- **FROZEN per-role resolution — no deep merge, ever.** A step that sets its own `sampling_payload`
  (including an explicit `{}`) owns a complete, standalone payload: it is swapped in wholesale and
  never merged with `defaults.sampling_payload`, key by key or otherwise. A step that leaves the
  field unset (`null`, the default — distinct from `{}`) inherits the default's payload whole, not
  per-key. Changing `defaults.sampling_payload` later can never alter a step that already has its
  own — this holds both at read time (`LLMConfig.resolved(step)`) and at write time (a config PATCH
  to one role's `sampling_payload` replaces the stored dict outright; it does not fold new keys
  into whatever was already saved for that role).
- **Storage repeats itself on purpose.** The full JSON is stored per customized role in
  `okto-neuron.yaml`. The file literally contains what will be sent to the model host. Two roles
  that both need the same handful of keys each carry their own full copy — a deliberate tradeoff
  for a file that never lies about what a given role's request looks like.
  `LiteLLMProvider.complete()` routes the payload's OpenAI-standard keys (`temperature`,
  `top_p`, `max_tokens`, `presence_penalty`, `response_format`) onto the request top-level and
  everything else through `extra_body` — the same split the self-hosted `extra_body` escape hatch
  (`top_k`/`min_p`/`chat_template_kwargs`) already used, just applied unconditionally instead of
  gated to a loopback/private endpoint, since configuring a raw payload is itself the operator's
  explicit choice of backend. A non-empty payload is authoritative over that call's own per-call
  sampler arguments (an extractor's class-default temperature, its empty-result cold retry, …) —
  a role that opted into a raw payload owns the whole request, so those adaptive nudges have no
  effect on it. `response_format` is the one exception: it is Okto Neuron's structured-output
  contract with the parser, not a sampling preference, so the caller's schema still applies
  whenever the payload itself does not already set one.
- **`drop_params` flips with the payload.** When a role's effective raw payload is empty, behavior
  is exactly as before this field existed: `drop_params=True`, so a param the provider/model
  doesn't support is silently dropped, same as always. When it is non-empty, that request is sent
  with `drop_params=False`, so an unsupported key raises the backend's own error instead of
  vanishing — a hand-assembled request should fail the way a hand-written request would.
- **Backward compatible for the typed fields, not for the deleted map.** The old typed sampler
  fields (`temperature`, `top_p`, `top_k`, `min_p`, …) are untouched; an existing vault that only
  ever used them keeps resolving identically, with `sampling_payload` simply empty at every role.
  The generic `parameters` map is the one exception — it was deleted outright (see above), so a
  vault YAML that still has a `parameters:` key under `llm.defaults` or a step now fails to load
  (`extra="forbid"`). The owner's call: no real user vaults exist yet, so there is nothing to keep
  loadable.
- **No default preset (decision B).** `sampling_payload` defaults to `{}` everywhere — empty means
  "use provider defaults", exactly today's behaviour. `SAMPLING_PRESETS` (see below) stays
  exported from the product because the benchmark imports it, but nothing in the default
  resolution path (`LLMDefaults`, `StepLLM`, `LLMConfig._resolved_connection`) references it:
  baking one locally-hosted model's tuned values into the product default would tune the whole
  product to that one model.
- **Presets promoted to the product.** The two named LoCoMo sampling presets (`instruct`,
  `thinking`) moved from `benchmarks/locomo/config.py` into `okto_neuron.config.SAMPLING_PRESETS`
  as the single source of truth; the benchmark now imports that exact object instead of keeping
  its own copy, so a product-side edit can no longer silently invalidate the LoCoMo run history
  those two presets were measured under. The benchmark harness deliberately keeps threading these
  values through the older typed `llm.defaults` fields rather than through `sampling_payload` —
  changing that is a separate task, out of scope here.
- **Web UI: raw JSON editor replaces the deleted dropdown.** The vault's LLM config (`ConfigPanel`,
  `frontend/src/components/config/ConfigPanel.tsx`) has one `SamplingPayloadEditor` — a
  `JsonParameterInput`-style textarea with live client-side JSON validation — for the defaults
  card and each of the five step cards. Invalid JSON and a reserved-key violation (named, with
  wording that distinguishes connection-owned from response-shape) are flagged inline and never
  reach `onChange`, so they cannot be saved; an empty box is a normal, legible state ("uses
  provider defaults"), not an error. A step card additionally shows whether it is inheriting the
  default payload whole (read-only preview plus an "Override for this step" button) or owns a
  standalone payload (editor plus a "Reset to inherit default" button) — the FROZEN semantics
  from above made visible. The client-side reserved-key check is a UX aid only; the server
  validator is still the authority, and a rejected save surfaces the backend's error message
  verbatim.
- **The ingest inspector's `llm_request` event now reports the EFFECTIVE request** (added later the
  same day, after the first live run). It used to report the tracing wrapper's own method
  arguments, which are the values BEFORE the raw payload is applied — and a role with a
  `sampling_payload` has those arguments discarded inside `LiteLLMProvider.complete()`. So a vault
  configured with `{"temperature": 0.2, "max_tokens": 32768, …}` showed an extraction trace reading
  `temperature: 0, max_tokens: 16000, top_p: null, top_k: null` — `LLMExtractor`'s class defaults,
  which were never sent. Nothing was wrong with the feature; the one surface built to verify it was
  reporting the wrong end of the merge, which is worse than reporting nothing: it cost real
  debugging time and made a live run unverifiable. The provider now reports its assembled request
  through a thread-local observer (`okto_neuron.llm._set_request_observer`, the same install/restore
  shape as the existing per-call cancel predicate) at the single moment that request exists — fully
  built, not yet issued — and the tracing wrapper emits THAT. The merge keeps exactly one
  implementation; nothing re-derives it for the trace.
  - The event carries `params` (what is actually sent), `extra_body` (the non-OpenAI-standard keys,
    in full), `omitted_params` (a configured key the provider dropped, with its reason — the exact
    dual of this defect), `requested_params` (the caller's own arguments, kept so an operator can
    SEE that their payload won rather than trusting a merged blob), and `sampling_payload_applied`.
    `params_source` is `effective` or `requested` so a reader never has to guess which they have.
    When the requested and effective `response_format` are identical — the normal case, since it is
    Okto Neuron's own parser contract rather than an operator-tunable preference — the requested copy
    collapses to a `<same as params.response_format>` marker instead of repeating the whole schema
    in the chattiest event kind on a thousand-call ingest.
  - Credentials cannot reach the event. The assembled request carries `api_key` (the environment
    credential, or the placeholder injected for a keyless self-hosted endpoint) and `api_base`
    (which can embed userinfo) beside the sampler params; the reported view is filtered by the same
    control-key set the parameter accounting already uses, rather than a second hand-maintained
    list — applied to BOTH the top-level params and `extra_body`, since the config validator that
    refuses a credential in a raw payload lives in another module and inspects only top-level names.
    The event's own values are bounded by the existing generic queue-event sanitizer. Values are copied and JSON-normalized on the way out, so a later mutation cannot rewrite
    an emitted event and an exotic value cannot break the history sidecar.
  - Exactly one `llm_request` per call, on every exit path — success, provider error, cancellation.
    A provider that cannot report its assembled request (the CLI providers, `StubLLM`) is traced up
    front from the caller's arguments exactly as before, and says so via `params_source`. One known
    divergence is accepted rather than papered over: the structured-output fallback may reissue
    without `response_format` after the event fired, so the trace then names a `response_format` the
    retry did not send; that retry logs its own warning.

## Addendum · 2026-09-15 — "Choose a folder" now obeys the same source-selection policy as folder import

The **Choose a folder** button and the drag-drop zone in `BulkImport.tsx` do not
call `/api/v1/ingest-folder`. They enumerate files **in the browser** and POST
them to `/api/v1/ingest-batch`, which applied no server-side source selection at
all — the only filter was `TEXT_RE = /\.(md|markdown|txt)$/i` in the component.
A real run queued 168 files through that button where `/ingest-folder` had
reduced the same folder to 76: 51% of the queue was `.scratchpad/`,
`.remember/`, `CLAUDE.md`, `.claude/`, `.documentation/`, and `.pytest_cache/`
scaffolding.

`/ingest-batch` now applies the same policy `/ingest-folder` applies, through
the predicate extracted from the folder walker (`classify_source_relpath`, see
the 2026-09-15 addenda to ADR 0025 and ADR 0026) — dot-directory pruning, the
vault's `folder_watch` ignore globs, the non-configurable scaffolding denylist,
the `.marginalia` internal-dir guard, and a real server-side `TEXT_SUFFIXES`
gate. That last one matters on its own: `safe_source_filename` force-appends
`.md`, so before this a posted `x.pdf` landed as `x.pdf.md`, and the markdown
trust root depended entirely on a browser regex that any non-browser caller
simply skipped.

Rejections are **reported**, not silently dropped. The response carries
`skipped_non_text` (same field `/ingest-folder` already returned),
`skipped_excluded`, `skipped_empty`, and a per-file `skipped` list of
`{filename, reason}` with stable reason codes (`ignored_dir`, `denylisted`,
`ignored_glob`, `non_text_suffix`, `empty`); the itemized list is capped at 200
entries while the counts stay exact. When nothing survives the policy, the 400
names the breakdown in its `detail`. The scan-summary line under the drop zone
shows the counts, and a **Show skipped files** disclosure lists the individual
files and the rule each one hit. `POST /api/v1/ingest` (single paste/write) is
unchanged — one operator-authored note is not a bulk source selection.
