# Marginalia Onboarding Plan

Original date: 2026-06-13
Status: implemented and reconciled 2026-07-14

This plan covers first-run setup after `scripts/install-dev.sh`, plus the longer
path to a polished onboarding experience across CLI, Web UI, and server config.

## Current implementation — 2026-07-14

Release `0.0.42` is now explicitly authorized as the immutable successor candidate. It contains
the native-Windows daemon repairs and keyless private-LAN provider fix described below. The earlier
development-wheel run remains diagnostic evidence; publication requires the complete
assigned-version source, exact-wheel, atomic-distribution, Linux Docker+tmux, and real interactive
Windows PowerShell 5.1 matrix, including recovery or upgrade from current public `0.0.41`.

The first-run contract is present in public prerelease `0.0.41`. The raw installer starts and opens the application
without inventing a vault; the Web UI owns default vault creation, selection, deletion,
and provider setup. Automation may explicitly set `MARGINALIA_VAULT` to preseed through
`marginalia onboard`, which supports interactive and non-interactive operation, validates
remote-egress consent, discovers models where the provider has a real catalogue, writes
only `MARGINALIA_*` environment-variable references to YAML, stores supplied secrets in
the owner-restricted POSIX env file or as CurrentUser-DPAPI envelopes on Windows, and saves configuration through
`VaultConfig.apply_patch`. Preseed-only packs and LLM variables are rejected without an
explicit vault. Provider presets include auto-detected local runtimes, LM Studio, Ollama,
LiteLLM Proxy, OpenRouter, OpenAI, Gemini, Anthropic, pi CLI, Codex CLI, and custom
OpenAI-compatible endpoints.

The Web UI Config surface now provides provider selection, the same generic loopback-only
managed credential flow for LLM and embedding providers, model validation/discovery, and a
separate real completion test. There is no
dedicated first-run wizard or `--print-shell` mode; those remain optional product UX
backlog, not corrective-release gates. Public installation is owned by `install.sh` and
`install.ps1` at the repository root (from 0.3.0; `marginalia-dist` is frozen at 0.2.0); `scripts/install-dev.*`
remain developer-only wrappers.

The exact public Linux lifecycle is retained on dist evidence commit
`6d9c1d1c604331773d06fadbe8770e04432bfb4c`, but the subsequent native Windows rehearsal failed
and made immutable `0.0.41` non-promotable. The three owning defects were a missing `os.fchmod`
attribute while writing the PID record, a locked PID payload byte that status and stop could not
read, and a Windows venv launcher PID that differed from the runtime PID owning the lifecycle lock.
Source repairs guard the optional chmod, move the lock byte beyond the bounded payload, and bind
readiness to the lock-owning runtime PID. A local development-wheel candidate passed native
Windows VM install plus daemon start/status/stop with `FIXED_SOURCE_WINDOWS_DAEMON_OK`; it has no
immutable tag or published-wheel identity and is not release evidence. Assigned version `0.0.42`
must pass its complete source, exact-wheel, distribution, Linux Docker+tmux, and real interactive
Windows PowerShell 5.1 gates. Optional wizard and
`--print-shell` work remains product backlog, not a release gate.

## Goals

1. A new user can install Marginalia, have the application start and open, then create/select a
   vault and configure its LLM provider, API key, and model inside the application without reading
   config internals.
2. Local users get the shortest path: assume an OpenAI-compatible endpoint by
   default, but expose Ollama and LM Studio as first-class choices.
3. Hosted users get OpenAI, Anthropic, Gemini, OpenRouter, and a custom LiteLLM
   route without needing to know the exact `marginalia.yaml` shape.
4. Raw API keys are never written to `marginalia.yaml`, never echoed, and never
   returned by HTTP APIs. The vault config stores only a namespaced
   `MARGINALIA_*` env var name.
5. Whatever onboarding writes must be visible through the same
   `GET /api/v1/config` payload that the Config tab renders. No side-channel
   config that the UI cannot show.
6. Model discovery must be best effort: call the provider model-list endpoint
   after the user supplies a key or local endpoint, show the returned ids, and
   always fall back to manual model entry.
7. Non-interactive installs must never hang. CI and scripted installs should get
   a clear follow-up command instead of prompts.

## Historical evidence at plan creation

- `scripts/install-dev.sh` previously installed the editable CLI and stopped at
  `marginalia vault list`; it did not create a vault or configure LLM settings.
- `marginalia init` and `marginalia vault create` only accept packs and embedder
  options; neither asks for LLM provider, key, base URL, or model.
- The Web UI config reads from `GET /api/v1/config`, which is backed by
  `VaultConfig.load` and `VaultConfig.apply_patch`.
- The existing security model is correct: `marginalia.yaml` stores
  `api_key_env`, and validators reject non-`MARGINALIA_*` env var names.
- The existing `POST /api/v1/llm/test` endpoint already probes
  OpenAI-compatible `/models` endpoints and returns model ids without secrets.
- The config schema already supports per-step LLM configuration, but onboarding
  should configure only `llm.defaults` at first. Advanced per-step routing stays
  in the Config tab.

## Historical proposal retained for decision provenance

The sections through **Validation Plan** below are the original 2026-06-13 proposal. They
explain why the implementation was chosen but do not describe current installer defaults,
browser credential behavior, or remaining work. The **Current implementation** section above
and **Future UX backlog** below are authoritative.

### Established Installer Patterns To Copy

The onboarding should follow patterns that users already recognize from mature
CLI projects:

1. **Inspectable installer before execution.** uv documents separate shell and
   PowerShell installers and explicitly tells users how to inspect the script
   before running it.
2. **Platform-specific wrappers, shared core behavior.** uv exposes shell and
   PowerShell entrypoints; Poetry uses a Python installer; Supabase has a
   standalone install script with clear flags. Marginalia should use a Python
   core installer with thin shell/PowerShell wrappers, because this repo already
   requires Python and needs identical behavior on macOS, Linux, and Windows.
3. **Non-interactive mode is first-class.** CI and automation must never hang on
   a prompt. Supabase-style flags and env overrides are the right model.
4. **Custom locations via environment or flags.** Rustup documents install-home
   env vars; Supabase supports install-dir env/flags. Marginalia should keep
   `MARGINALIA_CONFIG`, `MARGINALIA_ENV_FILE`, and future `MARGINALIA_HOME`
   style overrides explicit.
5. **PATH remediation, not mystery failure.** If the installed command is not on
   `PATH`, print the exact repair command (`uv tool update-shell`) and still try
   the uv tool bin path for immediate onboarding.
6. **A real first command after install.** Supabase leads users into
   `supabase init` / `supabase start`; Marginalia should lead users into
   `marginalia onboard` / `marginalia serve`.

References used for these patterns:

- uv installation docs: `https://docs.astral.sh/uv/getting-started/installation/`
- Poetry installer repository: `https://github.com/python-poetry/install.python-poetry.org`
- Supabase CLI installer: `https://github.com/supabase/cli/blob/develop/install`
- Supabase local CLI docs: `https://supabase.com/docs/guides/local-development/cli/getting-started`
- rustup installation docs: `https://rust-lang.github.io/rustup/installation/index.html`

### First-Run User Journey (historical proposal)

1. User runs the platform entrypoint:
   - macOS/Linux: `./scripts/install-dev.sh`
   - Windows PowerShell: `.\scripts\install-dev.ps1`
   - Direct/core: `python scripts/install-dev.py`
2. The Python installer installs the editable CLI with runtime extras through
   `uv tool install`.
3. If stdin/stdout are interactive, it asks whether to run first-time onboarding.
4. Onboarding chooses or creates a vault:
   - If there is a current vault, use it.
   - Otherwise prompt for a vault name, default `default`.
   - Create `~/.marginalia/vaults/<name>` with the normal scaffold.
   - Set it as the default vault.
5. Onboarding asks for the provider:
   - Local OpenAI-compatible
   - Ollama
   - LM Studio
   - OpenAI
   - Anthropic
   - Gemini
   - OpenRouter
   - Custom LiteLLM provider
6. Onboarding asks for or confirms the base URL:
   - Local OpenAI-compatible default: `http://127.0.0.1:8123/v1`
   - Ollama default: `http://127.0.0.1:11434/v1`
   - LM Studio default: `http://127.0.0.1:1234/v1`
   - Hosted defaults use the provider's public API base where model discovery
     needs one.
7. Onboarding asks for the API key:
   - Hosted providers treat the key as required, but allow the user to continue
     if the env var is already exported.
   - Local providers ask too, but make blank acceptable for keyless endpoints.
   - If a key is entered, write it to `~/.marginalia/env` with mode `0600`.
   - Write only `api_key_env` into `marginalia.yaml`.
8. Onboarding discovers models:
   - OpenAI-compatible: `GET <api_base>/models`
   - Anthropic: `GET https://api.anthropic.com/v1/models`
   - Gemini: `GET https://generativelanguage.googleapis.com/v1beta/models`
   - OpenRouter: `GET https://openrouter.ai/api/v1/models`
   - If discovery fails, print the short reason and ask for a model manually.
9. Onboarding writes config through `VaultConfig.apply_patch`:
   - `llm.allow_remote`
   - `llm.defaults.provider`
   - `llm.defaults.api_base`
   - `llm.defaults.model`
   - `llm.defaults.api_key_env`
10. Onboarding prints a redacted summary:
   - vault path
   - provider
   - model
   - base URL
   - env var name and env file path, never the key
11. User runs `marginalia serve`.
12. The CLI auto-loads `~/.marginalia/env` into the process, so provider calls can
    read the configured `MARGINALIA_*` key without requiring shell-profile edits.

### Cross-Platform Installer Shape (historical proposal)

The canonical implementation is `scripts/install-dev.py`.

- `scripts/install-dev.sh` is a POSIX wrapper only.
- `scripts/install-dev.ps1` is a PowerShell wrapper only.
- All wrappers pass arguments through to the Python installer.
- The Python installer discovers `uv`, runs `uv tool install --editable`, locates
  the installed `marginalia` command either through `PATH` or `uv tool dir --bin`,
  smoke-tests `marginalia --help`, and optionally runs `marginalia onboard`.
- Flags:
  - `--onboard` / `--no-onboard`
  - `--non-interactive`
  - `--vault`
  - `--provider`
  - `--litellm-provider`
  - `--api-base`
  - `--api-key-env`
  - `--model`
  - `--skip-model-discovery`

### Provider Matrix (historical proposal)

| Choice | Config provider | Default base URL | API key behavior | Discovery |
| --- | --- | --- | --- | --- |
| Local OpenAI-compatible | `openai` | `http://127.0.0.1:8123/v1` | Optional | `/models` |
| Ollama | `ollama` | `http://127.0.0.1:11434/v1` | Optional | `/models` |
| LM Studio | `lm_studio` | `http://127.0.0.1:1234/v1` | Optional | `/models` |
| OpenAI | `openai` | `https://api.openai.com/v1` | Required | `/models` |
| Anthropic | `anthropic` | `https://api.anthropic.com/v1` | Required | provider model API |
| Gemini | `gemini` | `https://generativelanguage.googleapis.com/v1beta` | Required | provider model API |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` | Required | `/models` |
| Custom LiteLLM | user-chosen | user-chosen | Optional by default | `/models` |

### Secrets Strategy (historical proposal)

- `marginalia.yaml` remains key-free.
- The CLI-generated env file is `~/.marginalia/env`.
- Env file permissions are `0600`; parent directory permissions are `0700`.
- Existing process env wins over file values, so users can override keys per
  shell/session without editing files.
- Only `MARGINALIA_*` variable names are loaded from the file.
- The Web UI still only sees and edits env var names, not raw secrets.

### Config Round-Trip Guarantee (historical proposal)

The onboarding command must use `VaultConfig.apply_patch`, not hand-written YAML
mutation, for the final save. This preserves the same validation, deep-merge
semantics, and `GET /api/v1/config` payload the UI relies on. The acceptance
check is:

1. Run onboarding.
2. Load `<vault>/marginalia.yaml` with `VaultConfig.load`.
3. Start or use the server against that vault.
4. `GET /api/v1/config`.
5. Confirm `llm.defaults.provider`, `api_base`, `model`, and `api_key_env` match.

### UI Follow-Up (historical proposal; subsequently shipped)

The CLI solves first-run setup. The Web UI should later gain a matching
"First-run" or "Provider setup" flow that reuses the same backend primitives:

1. If no vault exists, show vault creation first.
2. If a vault exists but `llm.defaults` is still the stock local default, show
   the provider setup panel before the main Config surface.
3. Reuse `POST /api/v1/llm/test` for model discovery.
4. For raw hosted keys, avoid sending them through the existing config PATCH.
   Add a separate loopback-only secret write endpoint if the UI is allowed to
   manage `~/.marginalia/env`; otherwise show exact export/env-file guidance.
5. Keep the current advanced Config tab for per-step overrides.

### Validation Plan (historical proposal)

1. CLI unit tests:
   - non-interactive local onboarding creates a vault and writes visible
     `llm.defaults` config.
   - hosted onboarding writes the raw key to `~/.marginalia/env`, writes only
     `api_key_env` to YAML, and redacts command output.
   - env-file loader does not override existing process env.
2. Model discovery tests:
   - OpenAI-compatible `/models` JSON parses ids.
   - Gemini discovery strips `models/` and filters to generation-capable models.
   - HTTP/network failures return a recoverable error.
3. Provider tests:
   - placeholder API keys are still injected for keyless OpenAI-compatible
     endpoint providers.
   - placeholder API keys are not injected for explicit managed-provider
     endpoints when no hosted key is present.
4. Script behavior:
   - interactive shell offers onboarding.
   - non-interactive shell prints `marginalia onboard` and exits cleanly.
5. Manual smoke:
   - run `marginalia onboard --provider local` against a real local endpoint.
   - choose a discovered model.
   - run `marginalia serve`.
   - verify the Config tab shows the selected provider/base/model.
   - run a small ingest and confirm provider errors are loud if the endpoint is
     wrong.

## Future UX backlog (non-release)

1. Add a dedicated first-run Web UI wizard on top of the already-shipped Config
   provider/credential flow.
2. Extend provider-specific model filters where providers return mixed catalogues.
3. Add a `marginalia onboard --print-shell` mode for users who do not want the
   CLI-managed env file.
4. Add documentation screenshots if a dedicated first-run wizard ships.

Two original decisions are closed: the Web UI may write the user env file only through
the loopback-restricted, cross-origin-protected JSON credential endpoint, and Config exposes a separate
real completion test in addition to model-list validation.
