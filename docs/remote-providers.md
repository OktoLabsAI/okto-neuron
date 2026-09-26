# Remote providers: the egress gate, and the ChatGPT-subscription provider

Two related things live here. The first is a security fix: four provider
drivers were reaching hosted APIs from vaults configured with
`allow_remote: false`. The second is a new, deliberately restricted provider —
`chatgpt` — which is one of those four, and which is documented here in full
because almost everything about it is narrower than it looks.

---

## 1. `allow_remote: false` did not stop every remote driver

### What was wrong

`allow_remote` is enforced by a URL check. `_check_api_base` classifies the
host of a configured `api_base` and refuses a non-loopback one unless the flag
is set. That works for every driver that has an `api_base`.

Four do not:

| Driver | Where the prompt actually goes | Why no `api_base` |
|---|---|---|
| `chatgpt` | `chatgpt.com/backend-api/codex` | litellm's `ChatGPTConfig` injects the base URL itself |
| `codex_cli` | OpenAI | shells out to the local `codex` binary, which holds its own auth |
| `claude_cli` | Anthropic | shells out to the local `claude` binary |
| `pi_cli` | whatever `~/.pi/agent/settings.json` selects | shells out to the local `pi` binary |

For these, the gate was not failing open on a bad URL — it was never reached at
all. Two separate paths were affected, and both had to be closed:

* `ProviderRegistry._validate_profile` called `_check_api_base` **inside**
  `if profile.api_base is not None:`. A profile with no base skipped it.
* `LLMConfig._validate_api_bases` did call the check unconditionally, but a
  driver with no base of its own inherits the vault's *default* `api_base`,
  which is loopback. The check passed vacuously.

A "local-only" vault could therefore be configured with `provider: codex_cli`
and ship every extracted block to a hosted model, with nothing anywhere saying
so.

### What it does now

`config/_vault.py` declares `UNCONDITIONALLY_REMOTE_DRIVERS` — the four above,
each with a sentence explaining why it is remote — and `_check_remote_driver`
refuses them when remote egress is not allowed. Both validation paths call it,
outside any `api_base` branch. The error names the driver, the reason, and the
specific flag to change (a provider profile's own `allow_remote`, or
`llm.allow_remote` — they are different fields, and naming the wrong one sends
people editing something that will not help):

```
provider driver 'codex_cli' always sends prompts to a remote service: the
local `codex` binary is only a shell — it forwards every prompt to OpenAI
under the ChatGPT subscription or API key it already holds. This vault sets
llm.allow_remote: false, which forbids that. It has no api_base to inspect,
so the refusal is by driver name rather than by URL. Set llm.allow_remote:
true to accept that the prompts leave this machine, or pick a driver that
talks to an endpoint you control (e.g. `openai` against a local inference
server).
```

**This is a behaviour change.** A vault that was running a CLI provider under
`allow_remote: false` now fails at config validation instead of silently
egressing. That is the point, but it will break such a setup on upgrade. The
fix is one line: set `allow_remote: true`, which is an accurate description of
what that vault was already doing.

The gate is by driver name only, and only for drivers with no inspectable URL.
Everything else still goes through the existing URL check, unchanged — there
is no second, divergent copy of the loopback logic.

---

## 2. The `chatgpt` provider — experimental, exploration only

> **Experimental.** This provider signs in with a ChatGPT consumer subscription.
> OpenAI's terms for those subscriptions may not allow access from third-party
> tools such as Okto Neuron. Read them before you enable it; you use it at your
> own risk. The same warning is printed by `okto-neuron provider login chatgpt`,
> logged when the provider is enabled, and included in the error when it is
> refused (`CHATGPT_TERMS_WARNING` in `llm/_chatgpt.py`).

> **Results from this provider are published only under the owner exception
> below.** It is a flat-rate consumer subscription, it silently drops most of
> the request, and it has no per-token price. A number measured through it is
> never comparable with another arm without those caveats beside it.

**Amended 2026-09-24 (owner decision D13).** The rule above used to read
"Never publish results from this provider." For the Okto Neuron benchmark
page the owner decided to publish the existing published arms plus one arm
from this provider, the full-tier LoCoMo arm
`gpt-5.6-luna` (macro 65.39), now in the published benchmark bundle
([`docs/benchmarks/locomo.md`](benchmarks/locomo.md)). It is published with these conditions, and
any later arm from this provider needs its own owner decision; this is not a
general lifting of the rule:

- It is labelled with the code it ran on, v0.1.0 + 44 commits (a
  development build between v0.1.0 and v0.2.0), which is not the code the
  other published arms ran on.
- It carries no dollar cost. The subscription is flat-rate and not metered per
  call, so it is shown as "flat-rate subscription, cost not per-call metered",
  never as a price point next to a metered arm.
- The sampling-preset confound is stated beside every comparison: this arm is
  `vendor_default` because the transport drops the sampling fields, while the
  qwen baselines run `instruct`.
- No paid re-score, no external-system panel, and no fresh regression run on
  later code were added for it.

This is the page every error message and docstring in that path cites by name
— `ChatGPTProvider.__init__`'s opt-in refusal, `assert_credential_present`'s
three failure messages, `_chatgpt.py`'s module docstring, and the `chatgpt`
`ProviderPreset` note in `onboarding.py`. If any of them ever again say
`See docs/<something-else>.md`, that reference is stale; this file, not a new
one, is where the fix belongs — see the credentials section below for why
duplicating it across two files is actively worse than a single page going
temporarily out of date.

### Turning it on

Opt-in by environment variable, with no config key and no default, so it can
never be reached by a run that did not ask for it by name:

```bash
export OKTO_NEURON_ENABLE_CHATGPT=1   # the pre-0.3.0 MARGINALIA_ENABLE_CHATGPT is still read, with a warning
```

Without it, constructing the provider raises immediately and the error explains
why rather than just naming the flag. The preset is also kept off the first-run
menu, so nobody arrives here by wandering through onboarding.

The LoCoMo harness has a run-scoped form of the same opt-in, `--enable-chatgpt`,
which sets the variable for that run's daemons only and leaves the operator's
shell alone. Either way `methodology.json` records which one was used (see
"Running a LoCoMo arm on it" below).

### What actually reaches the model

This is the part that matters most, and it is not what litellm advertises.

litellm's ChatGPT transport filters the assembled request body down to eleven
keys (`llms/chatgpt/responses/transformation.py:94-108`):

```python
allowed_keys = {"model", "input", "instructions", "stream", "store",
                "include", "tools", "tool_choice", "reasoning",
                "previous_response_id", "truncation"}
return {k: v for k, v in request.items() if k in allowed_keys}
```

Anything else is dropped by that dict comprehension — no error, no warning. Two
of the drops are easy to miss because they happen one layer below where the
parameter was named:

* `response_format` is mapped to `request["text"]` first
  (`completion_extras/litellm_responses_transformation/transformation.py:327-330`),
  and `text` is not whitelisted. **Structured output does not work here.** A
  curation step that assumes JSON mode gets prose.
* `max_tokens` becomes `max_output_tokens`, also not whitelisted. The token cap
  is discarded too.

Meanwhile `ChatGPTConfig` subclasses `OpenAIConfig` and does not override
`get_supported_openai_params`, so litellm reports **26 supported parameters**
for a transport that carries at most eight. Okto Neuron believing that
advertisement was the actual risk: curation would have reported itself as
running in JSON mode, at temperature 0, with a token cap, while sending none of
the three.

So `parameter_capabilities()` returns a narrowed set for this provider
(`litellm_chatgpt_narrowed`), listing only what survives:

```
previous_response_id, reasoning_effort, tool_choice, tools, truncation
```

Everything downstream — request shaping, the omitted-parameter accounting, the
Config UI's parameter list — reads that one corrected answer. Configure
`temperature` here and it is reported as omitted, which is the truth.

### Originator

litellm defaults the `originator` header and user-agent to `codex_cli_rs`
(`llms/chatgpt/common_utils.py:23`), which tells OpenAI's servers the traffic is
OpenAI's own first-party Codex CLI. It is not. The provider pins
`CHATGPT_ORIGINATOR=marginalia` (the pre-0.3.0 product name, kept because a new value could not be checked against the live endpoint for 0.3.0) before the first call, overriding an inherited
value rather than deferring to it.

### Prompt accounting: ~1.6K tokens that are not yours

Unless `CHATGPT_DEFAULT_INSTRUCTIONS` is set, litellm prepends the entire Codex
CLI system prompt — roughly eighty lines, hardcoded at
`common_utils.py:25-105` — to every request. Measured against this account: a
six-word prompt billed **1638 prompt tokens**.

Left alone, `prompt_tokens` would be mostly somebody else's prompt while
telemetry reported it as ours. The provider therefore sets the variable to a
short Okto Neuron instruction (~101 characters) so the operator's own prompt
dominates the count, records the length it set on the per-call stats, and
leaves an operator-supplied value alone if there is one.

### Cost: there is none, and it is not zero

Every one of the fourteen `chatgpt/*` entries in `litellm.model_cost` has
`input_cost_per_token: None` and `output_cost_per_token: None`. It is a
subscription; there is no per-token price to report.

A consumer that treats `None` as zero reports a free run. So the provider
attaches an explicit `cost_unavailable_reason` to the per-call stats instead of
a number, and the telemetry span carries it. A trace from this provider says
"cost unknown, and here is why", never "$0.00".

### Credentials — give Okto Neuron its own directory

litellm reads a **flat** JSON object from `$CHATGPT_TOKEN_DIR` (default
`~/.config/litellm/chatgpt/auth.json`):

```json
{"access_token": "...", "refresh_token": "...", "id_token": "...",
 "account_id": "...", "expires_at": 1234567890}
```

**Do not point this at `~/.codex` or `~/.pi/agent`.** Two facts make sharing a
credential file actively destructive:

* litellm **rewrites the file even on a plain read**. `_is_token_expired`
  (`authenticator.py:110-119`) and `get_account_id` (`:74-87`) both call
  `_write_auth_file` when a field is missing, and `_write_auth_file`
  (`:103-108`) is a bare `open(..., "w")` — no atomic rename, no `0600`, no
  locking, and it writes the whole dict back.
* OpenAI rotates the refresh token on use.

Codex also stores its tokens *nested* under a `tokens` key, not flat, so a
copied `~/.codex/auth.json` would not work even setting the hazard aside. The
provider checks for `access_token` and refuses a Codex-shaped file with an
error that says so.

#### Provisioning, once, in a terminal

```bash
okto-neuron provider login chatgpt
```

It prints a URL (`https://auth.openai.com/codex/device`) and a device code,
waits while you enter the code in a browser, then writes `auth.json` and
reports the account, plan, subscription end and access-token expiry it just
stored. The credential goes to `$CHATGPT_TOKEN_DIR` when that is set and
otherwise to litellm's default `~/.config/litellm/chatgpt`, which is fine to
use as Okto Neuron's own directory because nothing else writes there.

The command refuses four things before it starts a flow:

* a `CHATGPT_TOKEN_DIR` that resolves (symlinks included) to `~/.codex` or
  `~/.pi/agent`, for the reasons above;
* a stdin or stdout that is not a terminal. The flow blocks polling until a
  human authorizes it, so it must never be started by a daemon, a benchmark
  or an agent;
* an existing usable credential. It reports that one and stops; `--force`
  moves it aside to `auth.json.bak-<timestamp>` and logs in again. A leftover
  file with no `access_token` (what an aborted login leaves behind) is not
  treated as a credential;
* a login started less than five minutes ago and not finished. litellm allows
  one device code per five minutes and, inside that window, waits silently
  instead of printing a new one. The command says so and tells you how many
  seconds are left.

To check the stored credential without touching it:

```bash
okto-neuron provider status chatgpt          # human-readable
okto-neuron provider status chatgpt --json   # exit 1 if unusable
```

`status` opens the file directly and never goes through litellm's
`Authenticator`, whose reads rewrite it; the file's bytes and mtime are
unchanged afterwards. The account, plan and subscription date are decoded from
the stored `id_token` for display only and are never trusted for access; the
backend alone decides whether a call succeeds. It also says whether
`OKTO_NEURON_ENABLE_CHATGPT` is set in the current shell.

#### The provider refuses rather than starting a login

Observed live, and the reason a preflight exists: **with no credential file, a
plain completion does not raise.** litellm's authenticator begins an
interactive device-code flow — it prints a URL and an eight-digit code to
stdout, then blocks, polling. Inside `okto-neuron serve` that is a completion
that never returns and a device code written into a log nobody is reading; in a
benchmark it is a wedged run.

An unattended process must never be the thing that starts an OAuth flow, so
`ChatGPTProvider.complete` checks for a readable, flat, `access_token`-bearing
file first and refuses with instructions if it is absent. The check is a
read-only `os.path.exists` plus a JSON parse, deliberately: constructing
litellm's own `Authenticator` to ask it would itself `makedirs` the directory
and, on several paths, rewrite the file.

### Model discovery

`chatgpt` has no `/v1/models` endpoint — nothing under `litellm/llms/chatgpt/`
lists models, and `get_complete_url` only ever builds `/responses`. Probing
`{base}/v1/models` the way the `openai` branch does would 404.

Models therefore come from `litellm.model_cost`, a local dict read with no
network call and no credential. Two entries are filtered out because this
account was observed to reject them with *"not supported when using Codex with a
ChatGPT account"*: `gpt-5.3-codex` and `gpt-5.1-codex-mini`. `gpt-5.5` and
`gpt-5.6-terra` are verified working end to end through Okto Neuron's provider
layer, with `response.model` confirming correct routing (self-reported model
name in the response TEXT is not reliable — models get their own name wrong).
`gpt-6*` is absent from `litellm.model_cost` entirely, at every litellm
version tested 1.87.0 through 1.102.0 (see below) — it is reachable through
Codex CLI's own `openai` provider, so the account can use it, but litellm's
static `chatgpt/*` catalog has not added it yet, at any version.

### Dependency version: pinned to 1.89.7, not litellm's latest

`litellm` is pinned `>=1.89.7,<1.90` in `pyproject.toml` (the `litellm` and
`bedrock` extras). This is deliberately **not** latest — PyPI's latest at
time of writing is 1.102.0, and 1.90.0 through 1.102.0 all fail every
`chatgpt` completion with `APIConnectionError: ChatgptException - Unknown
items in responses API response: []`. Reproduced 3/3 against the real
backend, with the same credential that works cleanly one version down.

Bisected 2026-09-22 against the real backend, one full round trip per
version: 1.87.0 through 1.89.7 all succeed; 1.90.0 through 1.102.0 (checked
at 1.90.0 and 1.90.7, and confirmed still broken at 1.102.0) all fail
identically. The break is not in `llms/chatgpt/` at all — every citation in
this document was re-checked against 1.89.7 and is unchanged from what was
already written, confirming the `chatgpt`-specific code has not moved. It
lives in `completion_extras/litellm_responses_transformation/`, the generic
Responses-API SSE-reconstruction bridge that the `chatgpt` backend's
always-`stream=True` transport depends on to reassemble a full response —
63 commits touched that directory between our old floor (1.87.0) and
1.102.0, many titled `fix(responses_bridge)`, which reads as an area in
active flux upstream rather than one clean regression to patch around.
1.89.7 is the newest release confirmed working end to end; revisit the pin
when a later litellm release fixes this (watch
`completion_extras/litellm_responses_transformation/transformation.py`'s
`transform_response`, which is what raises the error).

### What is recorded

Every span from this provider carries `api_base:
https://chatgpt.com/backend-api/codex` rather than the vault's unrelated
loopback default, so a run that used it stays identifiable afterwards. That
matters more than usual here, because the whole point of the gate is that such
runs must never be mistaken for measurable ones.

Spans also carry the request params the product actually sent, under
`marginalia.params`. With `enable_thinking: false` that includes
`reasoning_effort: "none"`, which no sampling field configures: the product
derives it (`thinking_request_params` in `src/okto_neuron/llm/__init__.py`) for
any provider that advertises `reasoning_effort`.

### Running a LoCoMo arm on it

The published `gpt-5.6-luna` LoCoMo arm was produced with the project's benchmark harness,
which is kept private because its scorer ports CC BY-NC 4.0 code. The methodology and every
published number are in [`docs/benchmarks/locomo.md`](benchmarks/locomo.md). For ingest with
this provider, keep `consolidation.curation_batch_size: 1` and take throughput from
`consolidation.curation_max_concurrent`; section 3 below is the measurement behind that.

---

## 3. Batched curation: relation batches are capped at 4

`consolidation.curation_batch_size` packs several curation candidates into one
call (ADR 0015 D4). The ADR set a gate before anyone should rely on it: batch
verdicts must agree with single-call verdicts on the same candidates. This
section is the measurement of that gate, taken through the `chatgpt` provider
above (`gpt-5.6-luna`, reasoning off), and the cap it produced. The provider
caveat applies: these numbers describe that model on that transport, and say
nothing about any other backend.

### How it was measured (2026-09-22)

The curator prompts from a real conv-49 ingest were exported from MLflow and
replayed, rebuilt byte-exact from the product's own prompt builders
(`build_batch_user_prompt`, `batch_system_prompt`, the single-call curator
prompts). Three documents per curator, three repeats per arm, the same
candidates in every arm. Two numbers per arm:

- **commit rate**, mean over documents with a 95% CI across repeats;
- **pairwise agreement with single calls**: for each candidate, how often a
  batch verdict matches a single-call verdict from an independent repeat.
  Single-vs-single (excluding a repeat against itself) gives the noise floor.
  An arm is "indistinguishable from single calls" only if it sits at that
  floor.

### Relation curator (sessions 08, 09, 14; ~27 candidates per document)

| Batch size | session-08 | session-09 | session-14 | Mean ± CI95 | Agreement vs single |
|---|---|---|---|---|---|
| 1 (single) | 63.1 ± 13.7 | 46.5 ± 15.3 | 61.5 ± 25.2 | 57.0 ± 7.9 | **86.1 ± 3.6** (floor) |
| 4 | 56.0 ± 10.2 | 59.5 ± 5.2 | 67.9 ± 5.4 | 61.1 ± 4.5 | 63.1 ± 3.9 |
| 8 | 51.2 ± 42.0 | 53.6 ± 35.5 | 62.8 ± 11.0 | 55.9 ± 9.6 | 64.7 ± 3.3 |
| 12 | 31.0 ± 31.1 | 66.6 ± 41.0 | 67.9 ± 5.4 | 55.2 ± 16.1 | 61.5 ± 3.6 |
| 16 | 69.1 ± 41.0 | 82.2 ± 38.7 | 71.8 ± 30.7 | 74.3 ± 11.0 | 63.0 ± 3.1 |
| whole document | 36.9 ± 45.5 | 65.5 ± 36.8 | 73.1 ± 19.1 | 58.5 ± 15.9 | 60.2 ± 3.9 |

Commit rate is the percentage of relation candidates committed rather than
queued.

### Node curator (sessions 01, 08, 11; ~12 candidates per document)

| Batch size | session-01 | session-08 | session-11 | Mean ± CI95 | Agreement vs single |
|---|---|---|---|---|---|
| 1 (single) | 77.8 ± 11.9 | 90.9 ± 0.0 | 69.5 ± 11.9 | 79.4 ± 7.7 | **96.3 ± 2.1** (floor) |
| 4 | 86.1 ± 12.0 | 100.0 ± 0.0 | 80.5 ± 11.9 | 88.9 ± 7.2 | 84.9 ± 3.4 |
| 8 | 80.6 ± 31.6 | 100.0 ± 0.0 | 80.5 ± 11.9 | 87.0 ± 9.1 | 84.9 ± 3.4 |
| whole document | 100.0 ± 0.0 | 100.0 ± 0.0 | 75.0 ± 0.0 | 91.7 ± 9.6 | 87.7 ± 3.2 |

### What the numbers say

- **No batch size reaches the noise floor, for either curator.** Relation
  batches agree with single calls 60–65% of the time against an 86% floor;
  node batches 85–88% against 96%. The ADR's ≥97% gate is not met at any size.
- **The batch format itself causes most of the loss, not batch size.** A batch
  of exactly one candidate, sent through the batch prompt, already drops to
  68.1% relation agreement (39.1% commit) and 90.6% node agreement. Neighbours
  in the batch add little on top.
- **Relation batches above 4 flip whole documents.** Look at the per-document
  spread: at 4 it is ±5–10 points, at 8 and up it is ±11–45. A single repeat
  of a large batch commits almost everything or almost nothing for a document,
  which is what drives the wide CIs and the misleading 74.3% mean at 16.
  When that lands at zero committed edges, `relationship_liveness_gate` queues
  every node too, so the failure cascades.
- **Node batching over-commits at every size** (+8 to +12 points over single
  calls), with no cliff up to a whole document (~12 candidates). It is a steady
  bias, not an instability, so a size cap would not fix it.

### Prompt variants tried (one change at a time)

None closed the gap. Measured against the same single-call baseline:

| Variant | What changed | Result |
|---|---|---|
| `evid` | ask for the evidence quote before the verdict | relation b8 commit 50.0, whole-doc 35.8; node agreement fell to 81–83% |
| `percand` | restate the single-call rules per candidate | relation b8 57.3; node agreement 82–84% |
| `tmpl` | render each candidate with the single-call user template | relation b8 58.0; node b4 agreement 82.0% |
| `nosuffix` | drop the batch-format system suffix | model returned single objects; 48 unparseable replies at b1 |
| `minimal` | shortest batch framing | best at b1 (71.1% relation agreement) but 64.1% at b4, no better than baseline |

The `chatgpt` transport drops `response_format`, so the batch schema never
reaches this model; a backend that enforces it may behave differently. That was
not tested.

### What shipped

- **`RELATION_BATCH_MAX = 4`** (`src/okto_neuron/config/_capacity.py`,
  re-exported from `curator_batch.py`). Relation curation uses
  `min(curation_batch_size, 4)`. Four is the only measured size with no
  whole-document flips, and it still cuts relation calls roughly 4x (63 calls
  against 246 for the same candidates). It does **not** make batched verdicts
  match single calls; it keeps the damage bounded and repeatable.
- **No node cap.** There is no cliff to cap, and the over-commit is the same at
  4 as at 12.
- **The clamp is visible, never silent.** `capacity_report()` returns a
  `curation_batch` block (`configured`, `node_effective`, `relation_effective`,
  `notice`), and a companion ingest that clamps logs and emits a
  `relation_batch_capped` event naming the configured and effective sizes. The
  LoCoMo harness records `ingest.curation_batch_effective` in
  `methodology.json` (schema 7) and folds the effective relation size into the
  vault-cache fingerprint (version 5), so an old cached vault built with
  32-candidate relation batches is never reused as if it were built with 4.
- **Capped batches still parallelize.** A 25-edge single-block document with
  `curation_batch_size: 8` now plans as six batches of 4 plus one singleton
  fallback, and with `curation_max_concurrent > 1` those six go through the
  thread pool together (pinned by
  `test_capped_relation_batches_actually_run_concurrently`, which asserts more
  than one batch in flight at once).

### Recommendation

For any run where curation fidelity matters (a benchmark arm, a baseline, a
gate), keep `curation_batch_size: 1` and get throughput from
`curation_max_concurrent` on a backend listed in `parallel_capable_models`.
Single calls in parallel give the same verdicts as single calls in series;
batches of any size do not. Use batching only where call count matters more
than verdict fidelity, and expect relation batches to be clamped to 4.

Not established: whether reasoning effort changes any of this (all runs had
reasoning off), node batch sizes above ~12, and any backend other than
`gpt-5.6-luna` over `chatgpt`.
