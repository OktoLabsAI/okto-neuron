# ADR 0035: Dynamic LiteLLM Parameter Editor

- **Status:** Superseded (the `parameters` map, its `MANAGED_LLM_PARAMETERS` allowlist, and the
  Advanced Parameters dropdown UI this ADR describes were removed outright 2026-09-15 — no
  migration, no deprecation period; see the Addendum below and `docs/web-ui.md`'s
  2026-09-15 addendum). The underlying LiteLLM capability-introspection machinery this ADR also
  describes (`parameter_capabilities()`/`LLMParameterCapabilities` in `marginalia.llm`) is
  unaffected and still backs request-shaping in `LiteLLMProvider.complete()`.
- **Date:** 2026-07-15
- **Deciders:** Marginalia maintainers
- **Relates to:** ADR 0034

## Context

The LLM configuration UI currently asks LiteLLM which parameters a provider and
model support, but then intersects that answer with seven fields hard-coded in
both Python and TypeScript. The result looks dynamic while remaining a static
form. It also materializes Marginalia sampling defaults even when the user only
wants to select a provider connection and model, so optional values are sent
without an explicit user choice.

LiteLLM exposes two relevant capability sources. Direct providers use the
installed Python adapter (`get_supported_openai_params` plus the provider config
registry); LiteLLM Proxy aliases use the gateway model-group metadata. Those
sources reliably identify parameter names, but they do not consistently expose a
complete UI schema with type, range, choices, and description.

Some supported request parameters are owned by the application rather than by a
user preference. Marginalia owns messages, model routing, credentials, timeouts,
streaming mode, tools, and structured-output schemas. Allowing an advanced form
to override those fields would break request invariants or expose secrets.

## Decision

### 1. Optional parameters are absent by default

Provider connection, credential, and model are sufficient configuration. New
LLM defaults and per-step blocks contain no optional generation parameter unless
the user explicitly adds one. Absence means omission from the LiteLLM call so the
selected provider/model applies its own default.

Parameters are stored in a `parameters` mapping. Defaults contain concrete JSON
values. Per-step mappings contain values or `null` tombstones: a value overrides
an inherited default and `null` removes that default for the step. Existing typed
fields (`max_tokens`, `temperature`, `top_p`, `top_k`, `min_p`,
`presence_penalty`, and `enable_thinking`) remain readable as a compatibility
boundary, but have no code defaults and are not the primary UI contract.

### 2. The backend owns one capability schema

The parameter-capabilities endpoint returns descriptors derived from the selected
provider/model's LiteLLM capability data. Each descriptor includes its name,
source, editable status, input kind, and any known constraints or choices.
Canonical OpenAI parameters receive stable UI metadata maintained at the adapter
boundary. Unknown provider-specific parameters remain available through a JSON
editor instead of requiring provider branches in the frontend.

Request-owned or security-sensitive parameters are returned as non-editable and
cannot be persisted in `parameters`. The same backend capability object is used
for the UI and request shaping so display and runtime cannot drift.

### 3. The frontend is a generic editor

The normal LLM card shows only connection and model. An initially collapsed
**Advanced parameters** section lets the user add one supported editable
parameter at a time. Controls are selected from descriptor metadata; the
frontend contains no provider-specific parameter list. Removing an override
omits it from requests. Changing provider or model immediately refreshes the
available set and flags any now-unsupported persisted override so it can be
removed without silently deleting user data.

### 4. Runtime validation remains authoritative

Configuration loading validates JSON shape, parameter names, and application-
owned exclusions without requiring network access. At call time the LiteLLM
adapter rechecks support for the selected provider/model, sends only advertised
parameters, and records omitted parameters in the existing parameter plan.
`drop_params=True` remains a final compatibility guard, not the primary design.

Direct private/loopback providers with the explicit `local_extended` policy keep
their bounded raw-body escape hatch for local sampler parameters. Hosted and
gateway connections never receive unknown raw fields.

## Consequences

- New configurations use provider defaults unless the user opts into an
  override.
- Capability discovery, persistence, rendering, and request shaping share one
  model instead of parallel hard-coded lists.
- Older YAML remains loadable and its explicit legacy values appear as advanced
  parameters that can be edited or removed.
- LiteLLM capability metadata can be incomplete. The UI reports that state and
  does not invent support; the runtime continues to fail safe by omitting an
  unadvertised optional parameter.

## Addendum 2026-07-29 — an unset config value must not silence a client default

A judged golden run showed two completions ending at the model host's own 32768-token
ceiling with `max_tokens=None` in the warning line, and the captured upstream request
bodies carried only `model`, `messages`, and `response_format` — no cap, no
`temperature`. Both responses were degenerate decode-repetition loops (a complete,
valid object at the head, then thousands of repeated fragments), not reasoning burn.
Nothing bounded them.

Two independent causes stacked, and both are now fixed.

**1. `None` was passed through as an explicit override.** ADR 0035 states that the typed
compatibility fields "have no code defaults". That is true of the *configuration* layer
and stays true. It was never true of the pipeline clients: `LLMExtractor` (16000),
`LLMRelationCurator` / `LLMCandidateCurator` / `LLMMergeJudge` / `LLMPredicateJudge`
(2000), and `LLMTypeAdjudicator` (4000) each carry an empirically-tuned constructor
default. The daemon call sites passed `resolved.max_tokens` and `resolved.temperature`
*unconditionally*, so an unconfigured vault's `None` overrode those defaults and the
adapter then correctly omitted a `None` cap — yielding no cap at all.

Call sites now build those two kwargs through `marginalia.llm.sampler_overrides()`,
which omits a value the vault never set. A configured value still passes through
unchanged, so this is a no-op for any vault that pinned one. Configuration absence now
means "the client's documented default applies", not "no bound at all".

The caps keep their documented values (16000 / 2000 / 4000). They are not raised toward
the host's 32768 ceiling: in both observed truncations the useful output was complete
long before the loop began, so the lower cap bounds waste rather than losing content.

**2. The unknown-gateway narrowing dropped `temperature`.** When a LiteLLM gateway alias
has no per-alias capability metadata, the adapter narrows the advertised parameter set,
because the proxy adapter advertises an OpenAI-wide superset that is not evidence of
per-alias support. That narrowing previously retained only `max_tokens` /
`max_completion_tokens`, so a configured `temperature=0.0` never reached the model and
extraction ran at the host's default temperature.

`temperature` is now retained as well. This does not rest on the proxy's superset claim:
it is part of the baseline OpenAI chat-completions body that any OpenAI-compatible
gateway accepts. Vendor-specific parameters — including the thinking control — stay
narrowed out; delivering those over a gateway alias is a separate, egress-adjacent
change that is deliberately deferred.

**Divergence is no longer silent.** When the narrowing discards a parameter the caller
actually configured, the adapter emits one WARNING naming the dropped parameters,
deduplicated per `(model, dropped names)` so a run making thousands of calls logs it
once. Silent configured-versus-actual divergence is what made months of comparison runs
uninterpretable.

## Addendum 2026-09-15 — the `parameters` map and this ADR's UI/endpoint were removed

The owner decided the constrained "advanced parameters" surface this ADR designed — the
`parameters` field on `LLMDefaults`/`StepLLM`, its `MANAGED_LLM_PARAMETERS` allowlist-and-range
validation, the `/api/v1/llm/parameter-capabilities` endpoint, and the Web UI's "Select a
parameter" / "Add" dropdown — should be deleted outright rather than deprecated: no real user
vaults existed to keep loadable, so there was nothing to migrate. Removed: the `parameters` field
itself (and its field validators) on `LLMDefaults`/`StepLLM`; the `/api/v1/llm/parameter-capabilities`
endpoint and its `/api/v1/config/defaults/...` alias, plus their route registrations; the
dropdown/list editor in `ConfigPanel.tsx` and its capability-fetch state; the now-dead tests for
all of the above.

Kept, because each still guards or backs something real: `ResolvedLLM.parameters` (the fold
target for the three parameters-only typed fields — `repeat_penalty`, `reasoning_effort`,
`preserve_thinking` — that have no dedicated `ResolvedLLM` field of their own, and still directly
settable via the `/llm/test-completion` REST probe's request body for an ad hoc, non-persisted
test); `_check_llm_parameters`/`MANAGED_LLM_PARAMETERS` (guarding both of those); and
`parameter_capabilities()`/`LLMParameterCapabilities`/`parameter_descriptor()` in `marginalia.llm`
(still used by `LiteLLMProvider.complete()` itself to decide which typed sampler fields a
model/provider actually supports — request-shaping, independent of the deleted UI — and still
independently unit tested in `tests/llm/test_provider.py`).

In its place: `sampling_payload`, a deliberately unconstrained raw JSON override per LLM role
(defaults or a step), with its own much narrower reserved-key list (connection-owned plus
response-shape keys — see `docs/web-ui.md`'s 2026-09-15 addendum for the full account) instead of
this ADR's whitelist-and-range-validated `MANAGED_LLM_PARAMETERS` approach. The Web UI gained a
matching raw JSON editor (`SamplingPayloadEditor` in `ConfigPanel.tsx`) in the same change, so the
"generic editor, one capability schema" shape this ADR designed continues, just for an
unconstrained escape hatch instead of a whitelisted one.

**The observability surface had to move with it.** "Show what is actually sent" was an explicit
requirement of the raw-payload feature, and the first live run showed the ingest inspector failing
it: the `llm_request` trace event was built from the tracing wrapper's own method arguments, which
are the values BEFORE the payload override, so a configured role's extraction trace reported
`temperature: 0` / `max_tokens: 16000` — `LLMExtractor`'s class defaults, discarded inside
`LiteLLMProvider.complete()` and never sent. This is the same failure mode this ADR's own earlier
addendum names ("silent configured-versus-actual divergence is what made months of comparison runs
uninterpretable"), arriving from the opposite direction: there the configured value never reached
the model; here it did, and the surface built to confirm that said otherwise.

The fix keeps the constraint this ADR has repeatedly paid for — **one implementation of the
merge**. Rather than a second copy of the override rules in the tracer, or a preview method that
re-runs the whole build (which would also re-probe `parameter_capabilities()`, a network call on
the LiteLLM-gateway path, and re-log its narrowing warning), the provider reports the request it
has already assembled through a thread-local observer at the one point where the effective values
exist: after `_param_accounting_summary`, before the call is issued. Reporting there — not after
the call returns — keeps the trace live during a slow call and keeps a failed call traced. The
reported view is filtered through the same `_LITELLM_CONTROL_PARAMS` set the accounting already
uses, so the `api_key` and `api_base` sitting in that request dict cannot reach the ingest history
or the UI, and the two filters cannot drift apart. `docs/web-ui.md`'s 2026-09-15 addendum carries
the event's field-by-field shape.
