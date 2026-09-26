# ADR 0044: Rename Marginalia to Okto Neuron, Relicense, and Keep Every Stored Name

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** Okto Labs maintainers, with João Braga (author of the Okto Neuron MVP)
- **Supersedes:** the 2026-09-15 working decision that Marginalia would absorb the Okto
  Neuron MVP while keeping its own name. The two converged; the product keeps the Okto Neuron
  name and this codebase.
- **Relates to:** ADR 0034 (application, daemon, browser and multi-vault contract), ADR 0041
  (pluggable graph backend and its entry-point groups), ADR 0043 (agent-facing MCP surface)
- **Scope:** the package, command, environment, app-home, config-file, HTTP, MCP, telemetry
  and installer names; the license from 0.3.0 on; and the list of names that never change

## Context

Marginalia was always a working name. Okto Labs ships products as `okto-<product>`: Okto
Pulse, Okto Nexus, and Okto Grafx, the graph store this codebase already uses by default. In
2026-09 João Braga built an Okto Neuron MVP, contextual graph memory for agents on Okto Grafx,
in `OktoLabsAI/okto-neuron`. Its direction and this codebase's were the same product, so the
owners decided that the product is Okto Neuron, the implementation is this codebase, and
João's MVP is preserved and archived as `OktoLabsAI/okto-neuron-mvp` rather than deleted or
overwritten. The MVP's design work, its `~/.okto-neuron/grafx` memory layout and its release
history stay available there.

Two facts make a rename risky here:

1. **Stored data carries names.** A vault has a `.marginalia/` state directory, a
   `marginalia.yaml` config and a `__marginalia_schema__` marker; document ids hash absolute
   file paths, so moving a vault changes its ids; claim ids hash the extraction prompt; users
   store `MARGINALIA_*` env-var *names* in `api_key_env`.
2. **The names `marginalia` and `neuron` on PyPI belong to other projects**, so an import shim
   or a bare `neuron` package would collide with them.

## Decision

1. **Names.** Product "Okto Neuron". PyPI distribution, command and MCP server `okto-neuron`;
   import package `okto_neuron`; environment `OKTO_NEURON_*`; app home `~/.okto-neuron`; app
   config `okto-neuron.toml`; vault config `okto-neuron.yaml` for new vaults; HTTP header
   `X-Okto-Neuron-Vault` and field `okto_neuron_version`; MLflow default experiment
   `okto-neuron`; backend entry-point groups `okto_neuron.graph_backends` and
   `okto_neuron.index_backends`. `kg` stays. Version 0.3.0.
2. **Compatibility until 0.5.** Every legacy name that a user or another program may still
   send is read in one place, `src/okto_neuron/_compat.py`:
   - `marginalia` stays a console script that prints a rename warning and runs the same app.
   - `MARGINALIA_*` is read when the matching `OKTO_NEURON_*` is unset, with one warning per
     variable per process. When both are set the new name wins.
   - `~/.marginalia` app files are read until `okto-neuron migrate-home` (run by the
     installer) copies them to `~/.okto-neuron` and leaves a `MOVED_TO` note.
   - Both HTTP names are accepted and both are emitted; both entry-point groups are read, the
     new one first.
   There is **no** top-level `marginalia` import shim, because of the unrelated PyPI package.
3. **Vaults never move.** `~/.marginalia/vaults` stays a vault root for as long as it exists,
   and new named vaults on an upgraded machine are created there too. A vault's existing
   `marginalia.yaml` is read and written in place and never renamed.
4. **Kept permanently.** These are stored data, exported identities or LLM wire contracts,
   and do not change with the product name: the per-vault `.marginalia/` directory,
   `__marginalia_schema__`, `marginalia_toml_version` / `marginalia_yaml_version` keys,
   the snapshot `marginalia_version` field, user `api_key_env` values, the JSON-LD vocabulary
   IRI, MLflow span attribute keys (`marginalia.*`), structured-output schema names,
   the `marginalia-reconcile` ledger agent id, Neo4j constraint names, the content of stored
   system nodes, and LLM prompt text (it is hashed into claim ids and semantic fingerprints).
   `KEPT_LEGACY_LITERALS` in `_compat.py` lists each with its reason, and a test fails on any
   other `marginalia` string literal under `src/`.
5. **License.** From 0.3.0 the code is under the Elastic License 2.0 with the Okto Labs SaaS,
   Competing Service, Internal Use, and Branding addendum, the same terms as Okto Pulse, with
   "Okto Pulse" replaced by "Okto Neuron". The addendum's attribution requirement is met by
   "Okto Neuron by Okto Labs" in the web UI footer and in `okto-neuron --version` and help.
   Releases up to 0.2.0 were published as `marginalia` under Apache 2.0; that grant is
   irrevocable and still covers them.
6. **Distribution.** The public repository carries the source and the distribution:
   `install.sh` and `install.ps1` at the repository root (one-liner
   `curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.sh | bash`),
   their testers under `bin/`, and the SHA-256 release manifest, with
   wheels attached to GitHub Releases and published on PyPI as `okto-neuron`. The installer
   upgrades a Marginalia install in place and can roll back to the exact previous
   `marginalia` tool. `OktoLabsAI/marginalia-dist` is frozen at 0.2.0 and forwards to it.
7. **The ChatGPT-subscription provider** stays behind `OKTO_NEURON_ENABLE_CHATGPT=1`,
   documented as experimental, with a warning that OpenAI's consumer-subscription terms may
   not allow third-party access.

## Consequences

- An upgraded machine keeps its vaults, document ids, claim ids, daemon credential and MCP
  connection. Re-ingesting an unchanged file creates no duplicate document.
- Library callers must change `import marginalia` to `import okto_neuron`; there is no shim.
- Records written before 0.3.0 (ADRs, dated audits and ledgers, published benchmark
  artifacts, release evidence in `marginalia-dist`) keep the name they were written under.
  Benchmark numbers are labelled with the code version they ran on.
- The legacy names are removed in 0.5, after at least one release that warns on each.
