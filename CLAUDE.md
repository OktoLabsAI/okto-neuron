# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Okto Neuron is a local-first knowledge graph memory for agents. It ingests Markdown, extracts claims with an LLM, stores them in a graph with byte-exact source citations, and answers questions over REST, MCP, a CLI and a web UI. The earlier name was Marginalia. This repo is public: do not put private vault names, vault content or private network addresses in it.

## Commands

`uv` owns the environment through `uv.lock`. There is no pip path. Python is 3.12. Always use `uv run` and `uv sync`.

```bash
uv sync                                   # install (default groups: serve + dev)
uv run okto-neuron serve                  # REST + web UI on :7777, MCP on :8201
uv run ruff check src tests               # lint (ruff only; there is no type checker for Python)

# The model-free suite (same selection as CI). Runs on StubLLM + fastembed.
uv run pytest -m "not slow and not realmodel and not perf and not footprint and not acceptance_private_corpus and not acceptance_judge" -q

uv run pytest tests/test_smoke.py::test_name -xvs   # one test
./bin/acceptance.sh [filter]                        # end-to-end scenarios, no mocks (tests/acceptance/scenarios/)
./tests/golden/bin/quality-gate.sh --tier0-only     # CI-parity eval floor, no model, no daemon
uv run python docs/build_knowledge_base.py          # regenerate the generated docs pages
```

Frontend (React 18, Vite, TypeScript, Tailwind; Node `^20.19` or `>=22.12`):

```bash
cd frontend && npm ci
npm run dev          # Vite on :5180, proxies /api to http://127.0.0.1:7777
npm run typecheck
npm run build        # writes ../frontend_dist (or run ./build.sh from the repo root)
```

Notes on these commands:

- A plain `uv run pytest` also selects `slow` and `realmodel` tests. Use the `-m` selection above unless you want them.
- `realmodel`, `acceptance_judge`, `./bin/acceptance.sh --realmodel`, and tiers 1 and 2 of `quality-gate.sh` need a real LLM endpoint. Tiers 1 and 2 read `OKTO_NEURON_QUALITY_GATE_API_BASE` (no default).
- `tests/conftest.py` gives every test a scratch `HOME`, clears the `OKTO_NEURON_*` and `MARGINALIA_*` directory variables, and blocks writes to the real `~/.okto-neuron` and `~/.marginalia`. Do not weaken these fixtures.
- `scripts/install-dev.py` pins a Ladybug version that conflicts with `pyproject.toml`. Use `uv sync`.
- `[tool.uv] default-groups` must keep `dev`, or `uv run pytest` and `uv run ruff` lose their dependencies.
- The `litellm` extra is pinned below 1.90 on purpose (see the comment in `pyproject.toml`).

## Gates a change must pass

CI runs on push and pull request to `main`. Work on a branch; do not commit to `main`.

- **Docs gate** (`.githooks/pre-commit` and `docs-gate.yml`): a commit that changes `src/okto_neuron/` must also change a docs source (`README.md`, `RFC.md`, `docs/*.md` and others listed in the hook). The four generated pages (`docs/index.html`, `docs/knowledge-base/index.html`, `docs/rfc.html`, `docs/roadmap.html`) must match the output of `docs/build_knowledge_base.py`. Never edit them by hand. Run `uv run pytest -q tests/test_docs_integrity.py tests/test_readme_docs_gate.py`.
- **Version gate**: `version` in `pyproject.toml` must equal `__version__` in `src/okto_neuron/__init__.py`.
- **Lock gate**: `uv lock --check`.
- **Model-free tests** (`model-free-tests.yml`): ruff, the pytest selection above with `--extra ladybug --extra litellm --extra grafx`, then `./bin/acceptance.sh`.
- **Eval gate** (`eval-gate.yml`): a deterministic floor (provenance byte hashes, hard recall@k, extraction completeness) over the frozen vault in `tests/golden/datasets/synthetic-ci/`. It is the only quality metric that CI gates. Do not relax `recall_floor_baseline.json`.
- **Rename gate**: `tests/test_rename_compat.py` fails on a `marginalia` string literal in `src/` that is not in `KEPT_LEGACY_LITERALS` (`_compat.py`).
- **UI**: `frontend_dist/` is committed and ships in the wheel as `okto_neuron/_webui`. Commit the rebuilt `frontend_dist/` with every UI source change.
- **Schema**: a schema change needs an ADR in `docs/adr/` first (`CONTRIBUTING.md`).

`release-manifest.json` describes the last published wheel, not the working version. `install.sh` and `install.ps1` verify the wheel against it.

## Architecture

One package, `src/okto_neuron/`. Console scripts: `okto-neuron` (main CLI), `kg` (graph maintenance), `marginalia` (legacy alias, removed in 0.5).

### Process model

- `okto-neuron serve` is one process. It hosts the REST app and UI (`server/http.py`, port 7777) and the FastMCP streamable-http server (`server/runtime.py`, port 8201). It binds loopback only.
- `serve` is the only entry point that opens a vault. The CLI is a pure HTTP client (`cli/_client.py`). It has no direct-open fallback and does not start a daemon by itself.
- `VaultPool` (`server/_vault_pool.py`) owns exactly one handle per vault path. Each vault has an immutable `VaultRuntime` (`server/state.py`) with its own ingest queue, curation job queue and writer lock.
- In-process background tasks: the folder watcher (stat and hash polling, `server/_folder_watch.py`), the curation scheduler (`server/_scheduler.py`), one ingest worker per vault (`server/_ingest_queue.py`).
- MCP tools: `ask`, `explore`, `recall`, `remember`, `ingest_status`, `list_vaults`, `init_vault`. They use a bearer token from `~/.okto-neuron/daemon-<port>.token`.

### State on disk

- App home `~/.okto-neuron/`: `okto-neuron.toml` (app config), `defaults.yaml`, `providers.yaml`, `env`, `logs/`, `vaults/`.
- A vault: `notes/`, `refs/`, `okto-neuron.yaml` (vault config, Pydantic models in `config/_vault.py`), and the state directory `.marginalia/` (the name is kept forever).
- `.marginalia/` holds the graph (`graph.grafx` or `graph.lbug` plus WAL), `candidate-ledger.jsonl`, `review_queue.sqlite`, the rebuildable index cache `index/`, the authority index, the predicate aliases and the job state files.
- Vault config merge order: code defaults, then `~/.okto-neuron/defaults.yaml` (only with `inherits_application_defaults: true`), then the vault file.

### Write path (ingest)

`Companion.remember` in `companion/__init__.py` is the single write path. The folder watcher, REST, MCP and CLI all reach it through the per-vault ingest queue. That file has about 11,500 lines; search it by symbol, do not read it whole.

1. Parse Markdown to CommonMark blocks (`ingest/markdown.py`), then split into extraction units by `ingest.chunk_size_bytes`.
2. Skip unchanged content by content hash and sub-chunk diff (`companion/_incremental.py`).
3. Extract candidates with the LLM (`extract/`, `LLMExtractor`). Each unit is recorded in the candidate ledger.
4. Embed (`embed/`, default fastembed `BAAI/bge-small-en-v1.5`, 384 dimensions).
5. Adjudicate types (LLM), reconcile against the store, and run the merge judge (LLM, biased to "distinct when unsure") (`resolve/`, `reconcile/`).
6. Prefilter, then curate nodes, relations and predicates (LLM; `curator.py`, `curator_batch.py`, `predicates/`).
7. The consolidation gate (`consolidate/gate.py`) sends each candidate to auto-commit or to the review queue.
8. Record a commit plan in the ledger, then write. A plan without receipts can be resumed (`kg plans resume`).

### Read path

- `recall(k=10)` → `Vault.query` → `query.search_claims`: RRF fusion of a vector leg, a BM25 leg and an exact-title leg, then a one-hop graph expansion.
- `ask(k=20)`: the default answers from source blocks (`source_block_policy="blend"`). The subgraph answer path (`subgraph.py`) is opt-in through `enable_subgraph`.
- `explore`: an ego-graph walk with no LLM call.

### Storage

- `GraphStore` (`store/protocol.py`) has three official backends in `store/registry.py`: Grafx (default for new vaults), Ladybug (a Kuzu dialect) and Neo4j. `IndexStore` (`store/index/`) holds BM25 and vectors.
- The backend is pinned per vault in `storage.backend`. A vault config with no `storage` key means Ladybug, so `VaultConfig.storage` must stay `None` by default.
- The type system is closed: 5 primitives plus 6 support types (`primitives/`, `schema/support/`), enforced at write time (`store/closed_set.py`). Vocabulary comes from packs (`packs/`).
- Provenance is a `SourceSpan` value (vault-relative path, byte range, content hash), not a node.
- Maintenance operations all build a fresh graph and swap it in through one shared tail (`curation/orchestrate.py`): `kg rebuild` (re-extracts the Markdown sources, uses the LLM), `reconcile heal` (deterministic copy), `kg reembed` (vectors only), `kg reindex` (index cache).

### LLM layer

- `llm/__init__.py` defines the `LLMProvider` protocol and `get_provider`. Special providers: `stub`, `claude_cli`, `pi_cli`, `codex_cli`, `chatgpt` (opt-in). All other names go to `LiteLLMProvider`.
- Vault config selects the provider in `llm.defaults`, with per-step overrides for `extraction`, `judge`, `curator`, `relation_curator` and `ask`. A non-loopback `api_base` needs `allow_remote`.
- Tracing: `trace_parent` / `trace_child` (`llm/__init__.py`, `llm/_telemetry.py`). It is on only when `OKTO_NEURON_MLFLOW_TRACKING_URI` is set, and it fails open.

### Decisions

`docs/adr/` holds 44 ADRs. The ones that define the present design: 0007 (rebuild into a fresh graph), 0013 (candidate ledger), 0018 (retrieval defaults), 0023 (incremental ingest), 0029 (WAL recovery), 0034 (application daemon and vault pool), 0039 (verified commits, resumable extraction), 0041 (pluggable backends), 0043 (MCP surface), 0044 (rename). `RFC.md` is the v1.0 design baseline.

## Invariants

- Do not bulk-mutate edges in place on a populated Ladybug graph. Build a fresh graph and swap (ADR 0007).
- Do not open a second `Vault` handle on a path inside the daemon. `Vault.close()` is path-wide; use the pool-owned handle.
- One writer process per vault (`store/writer_lease.py`).
- The daemon uses fork then exec (`server/lifecycle.py`). A fork without exec crashes on macOS.
- Source and span paths are vault-relative. REST and MCP never return absolute paths.
- Document ids hash absolute paths. Vaults are never moved.
- LLM prompt text is hashed into claim ids and fingerprints (ADR 0044). Do not edit a prompt as a cosmetic change.
- Curator prompts put the source excerpt first so the prefix stays byte-identical for server-side prompt caching. Keep that order.
- The embedding dimension is fixed in the graph. A change of embedding model needs `kg reembed`.
- The scheduler submits only propose and detect sweeps. It never applies, heals, rebuilds or reembeds.

## Naming (rename to Okto Neuron, 0.3.0)

All legacy-name rules are in `src/okto_neuron/_compat.py`.

- New names: `okto_neuron`, `OKTO_NEURON_*`, `~/.okto-neuron`, `okto-neuron.toml`, `okto-neuron.yaml`.
- Read until 0.5: `MARGINALIA_*` variables (the new name wins; read them through `_compat.getenv`), `~/.marginalia` app files, the `marginalia` command.
- Kept forever: the per-vault `.marginalia/` directory, `__marginalia_schema__`, the `marginalia_*_version` config keys, `marginalia.*` trace attributes, an existing vault's `marginalia.yaml`.
- There is no `marginalia` import shim.
