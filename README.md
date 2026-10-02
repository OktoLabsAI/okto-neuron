# Okto Neuron

> Okto Neuron by Okto Labs. Local-first knowledge graph memory for agents, with cited sources and an MCP server.

Okto Neuron was developed under the working name **Marginalia**. Every release up to 0.2.0
was published as `marginalia` from the `OktoLabsAI/marginalia-dist` repository. From 0.3.0 the
product, the package, the command and this repository are named Okto Neuron. The 0.3.0 line
builds on an earlier Okto Neuron MVP by João Braga (2026-09), which is archived as
`OktoLabsAI/okto-neuron-mvp`; see [ADR 0044](docs/adr/0044-rename-to-okto-neuron.md).

## Upgrading from Marginalia (0.3.0)

0.3.0 is a rename release. Nothing about how a vault stores or answers changes, and existing
vaults are opened where they are.

| Before (up to 0.2.0) | From 0.3.0 | What still works |
|---|---|---|
| `marginalia` command | `okto-neuron` command (`kg` is unchanged) | `marginalia` keeps working and prints a rename warning until 0.5 |
| `MARGINALIA_*` environment variables | `OKTO_NEURON_*` | the old name is read with one warning until 0.5; when both are set the new one wins |
| `~/.marginalia` app home | `~/.okto-neuron` | app files (`marginalia.toml`, `env`, `providers.yaml`, `defaults.yaml`) are copied over once; `~/.marginalia/vaults` is **not** moved and stays a vault root, because document ids hash absolute paths |
| `marginalia.yaml` in a vault | `okto-neuron.yaml` for new vaults | an existing `marginalia.yaml` is read and written in place and never renamed; a vault's `.marginalia/` state directory keeps its name |
| `X-Marginalia-Vault` header, `marginalia_version` field | `X-Okto-Neuron-Vault`, `okto_neuron_version` | both old names are still accepted and sent |
| MCP server `marginalia` | MCP server `okto-neuron` (tools become `mcp__okto-neuron__*`) | the daemon keeps its port and credential; the installer replaces a user- or local-scope `marginalia` entry, and an entry in any other client keeps connecting until you rename it |
| import `marginalia` | import `okto_neuron` | none: there is no `marginalia` import shim, because that name belongs to an unrelated PyPI package |

The installer in this repository does the upgrade for you: it finds the `marginalia` uv tool,
stops its daemon with that tool's own command, installs `okto-neuron`, copies the app files,
and registers `okto-neuron` with Claude in the same scope (user or local) as your existing
`marginalia` entry. Once `okto-neuron` answers Claude's connection check, the installer removes
the old `marginalia` entry from that same scope; if the installer fails after that, it adds the
old entry back. A project-scope entry in a shared `.mcp.json` is left for you to rename. If
anything fails before the new version is verified, the exact previous `marginalia` tool is
restored.

To upgrade by hand instead, stop the old daemon with `marginalia stop`, run
`uv tool uninstall marginalia`, install with `uv tool install "okto-neuron[serve,litellm]"`,
then run `okto-neuron migrate-home` once to copy the app files. If the old daemon is still
running (for example after `uv tool install --force`), `okto-neuron serve` refuses to start
while it holds `~/.marginalia/runtime`, and `okto-neuron stop` stops it.

The ChatGPT-subscription provider stays opt-in and is marked experimental: it needs
`OKTO_NEURON_ENABLE_CHATGPT=1`, and OpenAI's terms for consumer subscriptions may not allow
access from third-party tools. Read them before enabling it
([docs/remote-providers.md](docs/remote-providers.md)).

## Known issues

- **Large documents on the Grafx backend**: ingesting a single document longer than 65,536
  characters fails on the default Okto Grafx backend. Split the file, or create the vault with
  `--backend ladybug` or `--backend neo4j`. It is an open issue in the
  [issue tracker](https://github.com/OktoLabsAI/okto-neuron/issues) ("grafx fails on documents
  over 65,536 characters").
- **Snapshot race on Ladybug**: `kg snapshot dump` can read `graph.lbug` between the two
  renames of a concurrent staged commit and see a mix of pre- and post-swap rows. It reproduces
  since v0.1.0 and is tracked by
  `tests/store/contract/test_snapshot_concurrency.py::TestLadybugSnapshotPinnedDumpVsSwap::test_dump_never_mixes_pre_and_post_swap_rows`.
  Do not dump a Ladybug vault while a rebuild, heal or reembed is committing.

## Release history (published as Marginalia)

Versions up to 0.2.0 were published as Marginalia, under Apache-2.0, from the
[marginalia-dist](https://github.com/OktoLabsAI/marginalia-dist) installer repo. Release notes
and wheels stay on its [releases page](https://github.com/OktoLabsAI/marginalia-dist/releases).

| Version | Published | Status |
|---|---|---|
| 0.2.0 | 2026-09-23 | Prerelease. Reliability release on the 0.1.0 MCP surface: `provider login` / `provider status chatgpt`, transient provider errors retried in `ask` and in every ingest judge and curator step, two sealed-plan wedges fixed, `remember` write failures logged on MCP and REST, Neo4j driver errors translated. |
| 0.1.0 | 2026-09-18 | Prerelease. The agent-facing MCP surface: five tools, `ask` widened to 17 parameters, `include_sources`, `synthesis_status` on every `ask` ([ADR 0043](./docs/adr/0043-agent-facing-mcp-surface.md)). |
| 0.0.50 | 2026-09-17 | Prerelease. Ingest-quality release. |
| 0.0.44 to 0.0.49 | 2026-07-29 to 2026-09-14 | Prereleases. |
| 0.0.43 | 2026-07-14 | The last release published as stable. |
| 0.0.42 | 2026-07-14 | Prerelease. Its release asset was replaced after publication, so it is not an immutable release. |
| 0.0.41 | 2026-07-14 | Prerelease, not eligible for promotion: a native Windows run failed. |
| 0.0.40 | 2026-07-13 | Not eligible for promotion. |

For 0.0.47 through 0.2.0 the release gates (model-free suite, acceptance, eval floor, wheel
checks) were run locally rather than on hosted CI. A Linux Docker+tmux install rehearsal from the
public raw installer URL passed for 0.0.47, 0.0.50, 0.1.0 and 0.2.0. No published version has passed a
real interactive Windows PowerShell 5.1 lifecycle, so Windows install is not yet a validated
path. The snapshot race listed under Known issues was present in 0.1.0 and 0.2.0.

Compatibility note from 0.0.41: two client-derived public spellings were replaced without
aliases, as privacy-neutralizing breaks. `budgets` exports `CORPUS_BUDGET_SECONDS` and
`budget_check()` takes the `corpus` suite key; callers using the old constant or key must migrate.
`pilot` reports use the `pilot-log.YYYY-MM-DD.json` basename.

Okto Neuron is a standalone, local-first knowledge graph
usable as a Python library, CLI (`okto-neuron` / `kg`), and authenticated MCP server. Your Markdown
vault is the trust root; the graph is derived from it and can always be rebuilt. See
[RFC.md](./RFC.md) for the full design and [docs/index.html](./docs/index.html) for the
documentation hub (Roadmap · Understanding · Knowledge Base · RFC).

## What it does

- **Ingest Markdown into a real graph.** Files are chunked, LLM-extracted into
  atomic claims and entities, embedded, and stored in a pluggable graph store —
  Okto Grafx by default, with Ladybug and Neo4j selectable via `--backend`
  (see [docs/backends](./docs/backends)).
- **Answer with graph retrieval and source grounding.** Hybrid retrieval (dense + BM25)
  selects graph hits, and `ask()` synthesizes over their byte-anchored source-block context by
  default. Graph-native subgraph assembly is opt-in; `explore()` walks the graph with no LLM.
- **Living memory.** `remember()` extracts, deduplicates, reconciles entities,
  and supersedes stale claims on every ingest. A folder watcher keeps a vault in
  sync as files change.
- **Provenance you can trust.** The atomic unit is a `Claim` — one subject-
  predicate-object assertion — anchored by PROV-O edges to its source `Block`,
  the extractor `Activity`, and the responsible `Agent`.
- **Surfaces over one core.** A sync CLI, an async FastMCP server (five tools:
  `ask`, `explore`, `remember`, `list_vaults`, `init_vault`), a Python SDK,
  and a built Web UI converge on the same `Vault`/`Companion` core. The small
  `okto_neuron.api` module is the compatibility export for library callers, not
  the implementation layer for every surface.

## Schema

The schema is closed at **5 primitives** — `Agent`, `Activity`,
`InformationObject`, `Concept`, `Place` — plus **6 support types** — `Document`,
`Identifier`, `Annotation`, `Claim`, `Block`, `Finding`. Vault configuration can
select the fixed built-in label/edge registries (`core`, `research`, `personal`,
`sdlc`); external manifest packs are not a production plug-in surface. Adding a
sixth primitive requires a new RFC and code change. Field and edge names follow
the canonical vocabulary (PROV-O, SKOS, Dublin Core, BIBFRAME, CiTO, W3C
`oa:Annotation`) rather than coined terms.

## Install

From 0.3.0 the installer and its SHA-256 release manifest live in this repository, and wheels
are GitHub Release assets here and on PyPI as `okto-neuron`. Until 0.3.0 is published, the
`marginalia-dist` one-liner below keeps installing 0.2.0; after that it forwards to this
installer. The installer starts the
local application, opens it in the default browser, and does not choose a vault on the user's
behalf; on a fresh interactive terminal with no existing vault or config (greenfield + TTY) it
additionally asks once, after installing the tool and before starting the app, whether to run
the terminal `okto-neuron onboard` flow (`Y`/Enter runs it, `n` keeps the application-first
path), and skips that prompt for piped/CI installs (no TTY), `OKTO_NEURON_NO_OPEN=1`,
`OKTO_NEURON_VAULT` preseeding, the `--no-onboard` flag, and every reinstall or upgrade. The
PowerShell installer resolves the same release on Windows; there is still no
retained native PowerShell 5.1 lifecycle evidence for any published version (the 0.0.41 Windows
daemon lifecycle is known broken), so treat that path as unverified. The raw manifest pins the
exact wheel and checksum.

```bash
# macOS / Linux (0.3.0 and later)
curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.sh | bash
```

```powershell
# Windows PowerShell (0.3.0 and later)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.ps1 | iex"
```

```bash
# PyPI (0.3.0 and later), as a uv tool
uv tool install --python 3.12 "okto-neuron[serve,litellm]"
```

```bash
# 0.2.0 (published as Marginalia), the current published prerelease
curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/marginalia-dist/main/install.sh | bash
```

A fresh vault's graph backend is **Okto Grafx by default** — no `--accept-experimental` flag or
extra install step needed, it is a base dependency of the package. Pass `--backend ladybug` or
`--backend neo4j` at creation time to opt into one of the other two fully supported, selectable
backends instead. See `docs/backends/grafx.md`, `docs/backends/ladybug.md`, and
`docs/backends/neo4j.md`.

Direct Python consumers install `okto-neuron` (0.3.0 and later) or the exact wheel file or
release URL. Never install the unrelated PyPI packages named `marginalia` or `neuron`. The direct-wheel contract below describes the
released `0.2.0` wheel; it does **not**
retroactively describe the immutable public predecessor wheels named in the release status
history above. The repaired base wheel supports package imports, `--version`, command help, and actionable
missing-feature errors; graph-backed library work adds `[ladybug]`. `[serve]` is the preferred
complete application-server boundary (Ladybug + FastMCP + embeddings), while `[mcp]` is an
identical compatibility alias for earlier install guidance. JSON-LD/JSON-LD-star export adds
`[jsonld]`, which supplies RDFLib. Clean-wheel tests execute these composed features separately so
a combined full-extras environment cannot mask an incomplete dependency contract.

The public distribution installer provisions `uv` and Python 3.12,
installs the `okto-neuron` / `kg` commands, starts the application daemon with zero or more
registered vaults, and opens `http://127.0.0.1:7777/` after verifying the installed version.
Create, select, delete, and configure vaults inside the application. Automation that deliberately
wants CLI preconfiguration may set `OKTO_NEURON_VAULT`; only then do `OKTO_NEURON_PACKS` and
`OKTO_NEURON_LLM_*` configure that preseed through `okto-neuron onboard`. API keys stay out of
`okto-neuron.yaml` and non-loopback LLM endpoints require explicit remote-egress confirmation.
`OKTO_NEURON_NO_OPEN=1` keeps installation headless; rollback and recovery restarts never open a
browser. The exact Linux lifecycle proves this contract from the public raw URL.

## First-time setup: `okto-neuron onboard`

`okto-neuron onboard` creates or selects a vault, asks for an LLM provider, asks
for the API key when the provider needs one, discovers models where the provider
exposes a model-list endpoint, and writes the chosen provider/base URL/model into
`okto-neuron.yaml`. Provider presets are provider-first: auto-detect, skip, LM
Studio, Ollama, LiteLLM Proxy, OpenRouter, OpenAI, Gemini, Anthropic, and custom
OpenAI-compatible endpoints. Existing explicit LLM config is kept unless you pass
`--reconfigure` or `--disable-llm`. For OpenAI-compatible endpoints the base URL is
canonicalized in one place regardless of the form typed: a server root
(`http://host:port`) and its versioned form (`http://host:port/v1`) both derive the
same endpoints — discovery probes `{root}/v1/models`, the pre-save verify completes
through `{root}/v1/chat/completions`, and the base persisted to `okto-neuron.yaml` is
always the canonical `{root}/v1`, so the saved config never depends on which form was
entered. Onboarding runs that verify before it writes anything: if the completion
fails, the run aborts with the exact attempted URL and saves nothing (no LLM config
block, no env secret). A non-interactive run without `--model` never silently defaults
to the first discovered model — it lists what it found and demands an explicit
`--model`, because on a multi-model server `models[0]` can be a non-chat model. For
scripted first-run setup:

```bash
okto-neuron onboard --non-interactive --provider local
```

The Web UI offers the same direct setup for both **LLM Defaults** and
**Embedding** single-key providers. Both use the generic loopback-only provider
credential endpoint: paste the key, store it in Okto Neuron's local secrets file,
then save the generated `OKTO_NEURON_PROVIDER_*_API_KEY` reference with the vault
settings. The key is never returned to the browser or placed in vault YAML.
Provider-specific credential chains continue to use the advanced environment
reference. POSIX protects the secrets file with owner-only permissions; Windows
stores each managed value with CurrentUser DPAPI so plaintext is absent at rest.

## Use it

```bash
okto-neuron serve                 # start the app and open its plain local URL in your browser
okto-neuron serve --no-open       # start headless; Ctrl-C drains, repeat Ctrl-C forces shutdown
okto-neuron serve --daemon        # background, open the UI, then print status/stop/log details
okto-neuron ui                    # reopen the same local app URL in your default browser
okto-neuron status                # daemon PID, version, runtime health, and recovery hints
okto-neuron stop                  # clean stop
okto-neuron add notes/one.md      # ingest via the daemon's configured CLI fallback vault
okto-neuron query "knowledge graph"
okto-neuron ask "what did I decide about provenance?"
okto-neuron watch-folder add ./notes  # register a continuously watched folder
```

Prefix any command with `uv run` (for example `uv run okto-neuron serve`) if you
did not install the global tool.

Named vaults live under `~/.okto-neuron/vaults/<name>` by default. Manage them
with `okto-neuron vault list`, `okto-neuron vault create <name>`, and
`okto-neuron vault use <name>`, or from the Web UI. The application itself does
not require a startup vault: each browser tab can create or select a vault, and
can delete an idle app-created vault after typing its exact name. External,
legacy, and symlink-backed vaults are never deleted by Okto Neuron.

### Web UI, REST, and MCP

A built React/Vite SPA ships with Okto Neuron and is served by `okto-neuron serve`
on the REST/UI port (default `:7777`); the MCP surface stays on `:8201` — one
process, two ports.

The SPA has seven primary views: **Query, Add, Logs, Browse, Graph, Curation, and Config**.
The Curation view is the review-first control plane for predicate folds, entity merges,
held node candidates, heal/rebuild, authority audit, drift, and manual reconciliation.

`okto-neuron serve` waits for readiness and opens the plain local application URL in the default
browser; `--no-open` is the explicit headless mode. `--daemon` performs the same readiness check
before opening the UI, then prints its PID, URL, status command, exact stop command, and log path.
If the daemon is already running, open `http://127.0.0.1:7777/` directly; `okto-neuron ui` remains
a compatibility alias for that URL. Browser profiles, private windows, and additional tabs do not
need a bootstrap command, credential, or session cookie.

Every `serve` (foreground or daemon) writes `~/.okto-neuron/logs/okto-neuron-serve.log`, even when
its stdout and stderr are discarded; the foreground command also echoes the records to the console.
The file is size-rotated (10 MiB, 3 backups: `.1`, `.2`, `.3`). `--log-file PATH` changes the path and
`--log-file /dev/null` silences the file. A file that cannot be opened gives one warning on stderr
and the server keeps running.

```
web UI : http://127.0.0.1:7777
REST   : http://127.0.0.1:7777/api/v1/...
MCP    : http://127.0.0.1:8201/mcp   (requires the daemon bearer credential)
```

`serve` binds loopback (`127.0.0.1`) by default. The local REST/UI application has no browser
credential or cookie gate; it instead enforces a strict loopback/Host boundary, rejects
cross-origin browser writes, requires JSON for JSON writes, exposes no permissive CORS policy,
and denies framing. MCP remains bearer-authenticated on its separate port with one
application-scoped daemon capability. Binding a non-loopback `--host` is restricted to loopback;
direct remote serving is withdrawn until its TLS and trusted-proxy contract is complete. Use an
SSH tunnel. If `frontend_dist/` is absent the server still boots API-only — the CLI and MCP are
unaffected and the UI route simply 404s.

#### MCP tools

The MCP surface is **five tools**: `ask`, `explore`, `remember`, `list_vaults`, and
`init_vault`. All five are bearer-authenticated on `:8201`.

`list_vaults` returns vault **names only** — no filesystem paths, no internal ids. That is
deliberate: an agent needs to name a vault, not to learn where it lives on disk, and the daemon
is the only thing that should be resolving a name to a path.

`ask`, `explore`, and `remember` each accept a per-call `vault=<name>`. It is a **name**, never
a path; a path is rejected. If the MCP connection itself was opened with `?vault=`, that
connection-scoped vault wins and the per-call override is discarded — the discard is reported
back on the response as `vault_override_ignored`, so a mis-scoped agent finds out rather than
silently reading the wrong graph.

A project can pin its vault with a `.okto-neuron-vault` file (a pre-0.3.0 `.marginalia-vault`
works the same way). That file is read by the **agent**,
which then passes the name as `vault=`. The server never reads it: one daemon serves every
project on the machine, so the server has no way to know which project a call came from.

##### `ask` retrieval parameters

`ask` no longer hides its retrieval policy behind vault config — the policy is exposed as
flattened parameters:

| Parameter | Notes |
| --- | --- |
| `vault` | vault name (see above) |
| `enable_subgraph` | opt-in graph-native assembly; default unchanged |
| `source_block_policy` | how source blocks are selected for synthesis |
| `seed_k` | number of retrieval seeds |
| `max_degree_per_seed` | edge fan-out cap per seed |
| `neighbour_budget_tokens` | token budget for expanded neighbours |
| `source_block_budget_tokens` | token budget for source-block context |
| `coverage_threshold` | coverage floor before assembly stops expanding |
| `min_claim_confidence` | drop claims below this confidence |
| `max_nodes` / `max_relationships` / `max_claims` | hard caps on assembled context |
| `relationship_types` | restrict traversal to these edge types |
| `include_sources` | return source anchors with the answer |

`explore` takes, in full: `topic`, `node_id`, `hops`, `k`, `vault`,
`relationship_types`, `min_claim_confidence`, and `max_degree_per_seed`.
`remember` takes, in full: `source`, `sensitivity` (`local_only` or `default`), and `vault`.

`enable_subgraph` is **opt-in and the default is deliberately unchanged**: measured on our
evaluation set, source-block synthesis scores 0.792 against subgraph assembly's 0.6. Turn it on
to experiment, not because it sounds better. On `ask`, `hops` only takes effect when
`enable_subgraph` is true — with the default path it is inert. (On `explore` it always applies.)
`MAX_QUERY_K` is 100.

`include_sources` returns, per source, the `block_id`, the byte range, the `content_hash`, and a
**vault-relative** path. Absolute paths are not returned.

`explore` now returns a `block_id` on both claims and relationships, plus its own retrieval
block describing what it walked.

##### Reading an `ask` response

Every `ask` response (REST and MCP) carries a top-level `status`: `ok` only when synthesis
produced a clean answer, `degraded` for anything else. `retrieval.synthesis_status` says why, and
is one of `ok`, `no_llm`, `empty`, `provider_error`, `truncated`, or `abnormal_stop`.

- `no_llm`: the vault has no usable LLM (the built-in defaults with an empty model, or
  `llm.enabled: false`). No model is called. The citations are still the retrieval hits, and
  `retrieval.no_llm_reason` names the setting to change.
- `provider_error`: `text: ""` here means the model was unreachable, not that the graph has no
  answer. Do not report "I couldn't find anything" on it.

`finish_reason` and `native_finish_reason` from the provider are surfaced as-is. Anything other
than `stop` should be treated as suspect output, even when `text` looks complete.

##### `remember` takes minutes, and most of it is curation

Budget for this before wiring `remember` into an agent loop. A measured ingest of a 15,882-byte,
3-block Markdown file took **1,536 s** end to end: 271 s of extraction (18%) against 1,266 s of
curation (82%), over 177 completion calls and 438,464 input / 28,495 output tokens. Three blocks,
twenty-five minutes. That run used the default local endpoint (`http://127.0.0.1:8123/v1`,
qwen3.8-27b on a Mac). The same file, same model, re-run on a separate LAN inference
server took **297 s** end to end (255 s curation, 163 calls, 407,279 / 26,837
tokens; same `units {scheduled:3, attempted:3, succeeded:3}`, `quality: complete`) — per-call
latency 6.0–9.3 s vs 1.14–2.26 s on an identical 2,139-token payload. The endpoint alone is a
~5× lever; the shape of the cost (serial curation dominating) is the same at both ends.

Curation dominates because it is **serial by default** — `curation_max_concurrent` and
`curation_batch_size` are both `1`, and `curation_call_timeout_s` is unbounded. Raising
`curation_max_concurrent` may still change nothing: the effective value is clamped back to `1`
unless *every* model used by a curation step is listed in `llm.parallel_capable_models`, so a vault
can be configured for four-way fan-out and run strictly sequentially with no error at all.

That duration is longer than a typical MCP client's idle timeout — Claude Code aborts a tool call
after 300 s with no response or progress. When that happens the daemon keeps working and finishes
the ingest; it is the *payload* that is lost, including the only trustworthy success signal
(`units.succeeded` against the block total). Do not read the abort as a failed ingest, and do not
re-send the same document on a timeout without checking the vault first.

Per-block and per-phase MCP progress notifications, which keep that idle timer alive so the real
payload survives, are in the working tree and not in any published artifact
(see `docs/adr/0043-agent-facing-mcp-surface.md`, D11).

## Start fresh (reset a vault)

A reset wipes the graph, vault-local derived state under `.marginalia/`, ingested
sources, and your `notes/`/`refs/`, then leaves a clean empty graph.
`okto-neuron.yaml` (embedder, packs) is preserved by default, so the same vault is
immediately reusable.

```bash
# CLI — wipe, then reinit an existing vault (refused if a server is live):
okto-neuron init ./vault --wipe

# Apply new config while wiping (without these flags, the existing config is kept):
okto-neuron init ./vault --wipe --packs core,research --embedder fastembed
```

In the Web UI, the **Config** tab has a **Danger zone** with a **Start fresh**
button. Reset refuses with `409 busy` while ingest or curation work is active,
and is loopback-only. Direct non-loopback server startup is disabled.

## Development

`uv` is the canonical entry point; there is no `pip install -e` path here.

```bash
uv sync                          # provision / sync the env
uv run pytest                    # default test run
uv run ruff check src tests      # lint
```

For an editable local install of the `okto-neuron` command, use the developer
scripts (developer-only — not the public install surface):

```bash
./scripts/install-dev.sh                           # macOS / Linux
.\scripts\install-dev.ps1                          # Windows PowerShell
okto-neuron dev                                     # build UI, serve, rebuild on change
```

Development mode prints the UI URL but does not open a browser automatically, including after
source-triggered server restarts.

Without `--vault`, development mode starts vault-neutral and the browser selects its own vault.
Use `okto-neuron dev --vault <name>` only when testing the legacy unscoped fallback explicitly.

`frontend_dist/` is committed, so the UI is live the moment the server boots;
`./build.sh` is only needed once, or after you change the frontend. The built UI
ships as package data in the wheel, so run `./build.sh` before `uv build` to
publish a fresh UI. Frontend builds require Node.js `^20.19` or `>=22.12`; release
CI uses Node.js 22.

## Where the old name came from

Marginalia, the working name, came from the notes a reader writes in the margins: the
original personal knowledge graph of annotations, connections, citations and dissent. The
project rebuilds that as a machine-queryable substrate, grounded in library science
(Ranganathan, FRBR, SKOS, authority control) and current ML (embeddings, LLM extraction,
hybrid retrieval). It ships as Okto Neuron, one of the Okto Labs products alongside Okto Pulse
and Okto Nexus, on the Okto Grafx graph store.

## License

From 0.3.0, Okto Neuron is licensed under the [Elastic License 2.0 with the Okto Labs
SaaS, Competing Service, Internal Use, and Branding addendum](LICENSE). The addendum asks
that the "Okto Neuron by Okto Labs" attribution stays visible in the UI and CLI; see
[TRADEMARKS.md](TRADEMARKS.md). Releases up to and including 0.2.0 were published as
`marginalia` under Apache 2.0, and that grant still applies to those releases. Third-party
material is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## ADRs

Architectural decisions live in [`docs/adr/`](docs/adr/). Highlights:

- [ADR 0001 — Rejected Models](docs/adr/0001-rejected-models.md)
- [ADR 0003 — Provenance Is a Span, Not a Node](docs/adr/0003-provenance-is-a-span-not-a-node.md)
- [ADR 0008 — Retroactive Entity Reconciliation](docs/adr/0008-retroactive-entity-reconciliation.md)
- [ADR 0009 — Curation Control Plane](docs/adr/0009-curation-control-plane.md)
- [ADR 0011 — Subgraph-First Answer Assembly](docs/adr/0011-subgraph-first-answer-assembly.md)
- [ADR 0018 — Differentiated Retrieval Defaults](docs/adr/0018-differentiated-retrieval-defaults.md)
- [ADR 0019 — Graph-Native Answer Assembly](docs/adr/0019-graph-native-answer-assembly.md)
- [ADR 0023 — Incremental Content-Hash Ingest](docs/adr/0023-incremental-content-hash-ingest.md)
- [ADR 0025 — Continuous Folder Monitoring](docs/adr/0025-continuous-folder-monitoring.md)
