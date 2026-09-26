# LLM observability — MLflow GenAI traces

Okto Neuron can export every LLM interaction to an [MLflow](https://mlflow.org)
tracking server as a GenAI trace: one span per `complete()` call, carrying the
prompt, the effective request parameters, the response, token counts, the
finish-reason forensics, and the latency. It is **off by default** and costs
nothing — not even an import — until you turn it on.

## Turning it on

Install the optional extra and point Okto Neuron at a tracking server:

```bash
uv sync --group telemetry                    # or: pip install "okto-neuron[telemetry]"
# installed tool: re-run the installer with OKTO_NEURON_TELEMETRY=1

export OKTO_NEURON_MLFLOW_TRACKING_URI=http://my-mlflow-host:5000
export OKTO_NEURON_MLFLOW_EXPERIMENT=my-experiment      # optional
uv run okto-neuron serve
```

| Variable | Meaning |
|---|---|
| `OKTO_NEURON_MLFLOW_TRACKING_URI` | **The gate.** Unset ⇒ no export, no `mlflow` import, no behaviour change. Set ⇒ export to this server. The pre-0.3.0 `MARGINALIA_MLFLOW_TRACKING_URI` is still read, with one warning, when the new name is unset; the same holds for the other two variables. |
| `OKTO_NEURON_MLFLOW_EXPERIMENT` | Experiment name. Defaults to `okto-neuron` (it was `marginalia` before 0.3.0; traces already in that experiment stay there). The LoCoMo harness uses `locomo`. The experiment is created if it does not exist. |

| `OKTO_NEURON_MLFLOW_TAGS` | Optional JSON object of identity tags stamped on every span this process exports. Set by a spawner that knows what the run is; see "Saying which run a span belongs to". |

There is deliberately no config-file key. Telemetry is a property of how a
process is *run*, not of a vault, and a vault config that silently shipped
prompts to a remote server would be the wrong default in a local-first product.

### Making it stick, without a config file

Because the gate is an environment variable and nothing else, a local install
turns tracing on the same way it turns on anything else about a process: in the
shell profile that starts it, in the `launchd`/`systemd` unit, or in the
terminal for one session. That is the whole mechanism. If `okto-neuron serve` is
started by a launcher, the variable has to be in *that* launcher's environment
— exporting it in an interactive shell afterwards does nothing for an already
running daemon, because `enabled()` is read per call but the process only sees
the environment it was started with.

### What actually leaves your machine

Everything a span carries goes to the tracking server, and that includes the
**prompt text and the response text** of every `complete()` call, alongside the
model, endpoint, token counts, sampling parameters and latency. For a personal
vault that means your notes' contents. Point it at a server you control. There
is no redaction layer and no partial mode — the trade is deliberate, because a
trace that omitted the prompt could not answer the questions traces exist to
answer.

Nothing is sent when the gate is unset: no export, no `mlflow` import, no
network call, and `get_provider()` hands back the provider object untouched.

### Checking that it is on

There is no startup banner, so verify from the server rather than from the
logs. After one `remember` or `ask`:

```bash
uv run --extra telemetry python - <<'EOF'
import mlflow

mlflow.set_tracking_uri("http://my-mlflow-host:5000")
client = mlflow.MlflowClient()
experiment = client.get_experiment_by_name("okto-neuron")
if experiment is None:
    # The experiment is created on first export, so its absence means nothing
    # has ever been exported to this server under this name.
    raise SystemExit("no 'okto-neuron' experiment on this server yet")
traces = client.search_traces(locations=[experiment.experiment_id], max_results=5)
print(len(traces), "traces")
for trace in traces:
    tags = {k: v for k, v in trace.info.tags.items() if k.startswith("marginalia")}
    print(trace.info.state, tags)
EOF
```

Seeing zero traces with the variable set usually means one of three things: the
daemon was started before the variable was exported, `mlflow` is not installed
in the environment that runs the daemon (the optional `telemetry` extra), or the
server is unreachable — the last one warns once and then stays quiet by design,
since telemetry must never take a request down with it.

A missing `mlflow` is reported at startup, not at the first call: every CLI
command prints one `warning: OKTO_NEURON_MLFLOW_TRACKING_URI is set (...) but
mlflow is not installed ...` line on stderr, and `okto-neuron serve` writes the
same text to the daemon log as a `telemetry.unavailable` event. The installer
adds the extra when `OKTO_NEURON_TELEMETRY=1` or the tracking URI is set.

## The shape of an operation: nested spans

A completion is a call, not an operation. One `ask` is a retrieval and a
synthesis — two on the subgraph path, one per tier — and exporting each call as
its own root trace loses that entirely: the UI shows a flat list of rows and
leaves the reader to reconstruct which belonged to which question from
timestamps.

`ask` is therefore one trace with its calls nested under it:

```
ask  [CHAIN]  11289.7 ms  OK
  └─ retrieval  [RETRIEVER]    935.7 ms  OK
  └─ llm.ask    [LLM]        10346.0 ms  OK
```

Measured on a real smoke arm, which makes the point of the tree: **retrieval is
8.3% of that answer's latency and synthesis 91.7%** — a split that is
unanswerable from a flat list.

`retrieval` gets a span even though it is not a completion and never passes the
provider seam. Without it, the trace would show only the LLM call and an
answer's time would look like it was all generation.

Ingest is one trace per document, covering every call it makes — extraction,
curation, type adjudication, the dedup and correction judges:

```
ingest [CHAIN] 51381.6 ms   spans=17
  └─ llm.extraction ×3        3338.8 / 9792.8 / 7636.8 ms
  └─ llm.curator ×4
  └─ llm.relation_curator ×8
  └─ llm.judge ×1
```

One document is the unit because it is what the HTTP handler offloads to a
single thread (`asyncio.to_thread(companion.remember, …)`); everything below
it is a loop that fans out.

### A retried call is a sibling span, on ingest and on ask

One transient-retry policy (ADR 0039 D5, `provider_retry_delay` /
`complete_with_retry` in `marginalia/llm/__init__.py`) covers extraction
units, ask synthesis, every ingest step that degrades on a provider error (the
merge judge, predicate resolution, the curator, the relation curator, type
adjudication and the correction judge) and the predicate-propose sweep judge.
An error the provider layer classified as retryable (`timeout`,
`rate_limited`, `unavailable`) gets one more attempt, after the provider's
`Retry-After` when it sent one (capped at 60 s, cut short by a Stop); anything
else fails on the first attempt, and a retry that also fails degrades exactly
as a single failure always did (`llm-unavailable` abstain / distinct). A
scoped task deadline bounds the whole step rather than each attempt: when
`consolidation.curation_call_timeout_s` (or any `_scoped_call_timeout`) has
already run out, or would run out during the `Retry-After` wait, the first
failure stands and no second attempt is made. Two LLM calls are deliberately
not retried because their failure falls back to calls that are: a batched
curation call (to the per-candidate curator) and the reconcile cluster judge
(to the pairwise merge judge). The provider seam itself never retries, so
each attempt is its own call span and a retry shows up as a failed sibling
followed by a clean one. Measured
2026-09-23 against the ChatGPT provider, with an HTTP 503 injected in front of
it:

```
ask  [CHAIN]  OK      synthesis_status=ok  synthesis_retries=1
  └─ retrieval  OK
  └─ llm.ask    ERROR  error_category=unavailable retryable=true
  └─ llm.ask    OK     finish_reason=stop
ingest [CHAIN]  OK
  └─ llm.extraction  ERROR  error_category=unavailable retryable=true
  └─ llm.extraction  OK
ingest [CHAIN]  OK    provider_retries={"curator": 1}
  └─ llm.extraction  OK
  └─ llm.curator     ERROR  error_category=unavailable retryable=true
  └─ llm.curator     OK     (the same candidate, second attempt)
  └─ llm.curator     OK     ... one span per remaining candidate
ingest [CHAIN]  OK    provider_retries={"predicate_resolution": 1}
  └─ llm.judge       ERROR  error_category=unavailable retryable=true
  └─ llm.judge       OK
```

With the 503 injected on both attempts of one curator call, that candidate got
exactly two `llm.curator` ERROR spans and no third, and its ledger row is the
old `abstain` / `llm-unavailable`.

Where the retry is recorded differs by path. Extraction writes
`retry_disposition` (`retried` / `exhausted` / `not_retryable`) to the unit
journal. The other ingest steps record it on the ledger comparison row that
already carries their outcome: `payload.provider_retries` on `curator`,
`relation_curator`, `predicate_resolution` and `identity_type_adjudicator`
rows, on the per-pair `judge_batch` / `judge_store` rows (which then also
carry the judge's `reason`), and on each vote of a predicate-sweep alias
record (`votes.forward` / `votes.reverse`). The correction judge returns a
bare index, so its retry is counted but has no row of its own. Each entry is
`attempt`, `category`, `retry_after_s`, `delay_s`, `error`. The rule is the
same everywhere: `provider_retries` is present only when a retry happened, and
absent means none (a row, outcome or span with no retry is unchanged). The
document's ADR 0039 D6 outcome adds `provider_retries`, a per-step count,
which the `ingest` span repeats as `marginalia.provider_retries`, and the
ingest inspector gets one `llm_retry` event per retry. The curation fan-out no
longer adds a retry of its own on top (it used to re-run any `llm-unavailable`
verdict once, including non-retryable ones, without recording it). Ask has no
journal, so the answer's `retrieval["synthesis_retries"]`
lists each retried failure and the `ask` span carries the count as
`marginalia.synthesis_retries`. A recovered answer is `synthesis_status=ok`;
one whose retry also failed stays `provider_error`, exactly as before.

### Fan-out, and why `bind_parent` exists

A thread-local parent is invisible inside a thread pool, and ingest uses three:
extraction whenever a document has more than one block, type adjudication
whenever it runs, and curation as soon as `curation_max_concurrent` or
`curation_batch_size` leaves 1. Without propagation, those calls would each
export as their own root trace and the `ingest` trace would be missing exactly
the work that did the ingesting — and at default config curation would appear
to work, then silently break the moment a concurrency knob was raised.

`bind_parent(fn)` wraps a callable at the `pool.submit` site: it captures the
parent on the submitting thread and reinstalls it for the duration of the call.
It returns `fn` unchanged when there is no parent, so an untraced run keeps the
exact callable it had.

The example above is a real 3-block document: three `llm.extraction` spans that
ran on `marginalia-extract` worker threads, all correctly parented, zero
orphans.

**The judge stays flat, deliberately.** In the LoCoMo harness, scoring is one
LLM call per question with no grouping in the code to nest under — a
per-conversation parent would be inventing structure rather than instrumenting
it. Judge calls made DURING ingest (dedup, correction) do nest, because there
the enclosing document is real.

### How it works, and the two things that constrain it

`trace_parent()` opens the span and puts its ids in a thread-local; `record()`
reads them ON THE CALLER'S THREAD and carries them in the queued payload,
because the drain thread that performs the export cannot see a thread-local set
by whoever made the call. `_emit` then branches to `start_span`/`end_span` when
a payload names a parent, and to `start_trace`/`end_trace` when it does not.

Thread-local rather than a contextvar: the daemon runs each ask on its own
`asyncio.to_thread` worker, so two concurrent questions must never adopt each
other's parent.

**Constraint 1 — the parent is opened synchronously.** A child needs its
parent's ids before it can be queued, so unlike everything else here this one
call happens on the caller's thread. Measured against a real server: the first
`start_trace` in a process pays MLflow's lazy client setup (~90-140 ms), every
later one ~0.04 ms, and `end_trace` ~0.06 ms. Neither blocks on the export
itself, which the background exporter still does off the critical path.

**Constraint 2 — children arrive late, and that is safe.** Export is
asynchronous, so an `llm.ask` span is almost always sent AFTER its parent has
been ended. Verified against a live server: a child created and ended after its
parent's `end_trace` still lands correctly parented.

One consequence worth knowing: `start_span` accepts no tags — tags belong to
the trace, which the parent owns — so a nested call's identity
(`marginalia.step`, `marginalia.degraded_reason`) becomes a span ATTRIBUTE, and
the parent carries the filterable copy. A question whose synthesis was degraded
is marked `ERROR` on the parent too, so the trace list cannot show a green
question containing a red call.

`trace_parent()` also sets the thread's pipeline-step label to its own name for
the duration of the block, restoring whatever was there on exit — the same
save/restore pattern `Companion`'s `_StepLabelledProvider` uses around one
`complete()` call. Without it, a completion made directly through
`get_provider()` outside `Companion` — the only other place that ever sets the
label — named its span `llm.-` forever: no step, no error, just an
indistinguishable row in the trace view. A step a `Companion` call sets for
itself still wins for the duration of its own call; this only fills the gap
between such calls and covers completions with no step labelling of their own.
Verified against a live server: the same smoke call that produced `llm.-`
before the fix produces `llm.<parent-name>` after it.

## What a span carries

Every trace is a single `LLM`-typed span named `llm.<step>`, where `<step>` is
the pipeline stage label Okto Neuron already stamps on its per-call log lines
(`extraction`, `curator`, `relation_curator`, `predicate_judge`, `ask`, …).

**MLflow-native attributes** (so the MLflow UI renders them in its own columns):

- `mlflow.llm.model`, `mlflow.llm.provider`
- `mlflow.chat.tokenUsage` — `input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `total_tokens`

**Okto Neuron attributes:**

| Attribute | Notes |
|---|---|
| `marginalia.step` | Pipeline stage, same label as the `step=` log field. Span attribute and tag keys keep the `marginalia.` prefix after the 0.3.0 rename, so traces from before and after it answer the same queries. |
| `marginalia.api_base` | The endpoint actually used |
| `marginalia.params`, `marginalia.extra_body`, `marginalia.omitted_params` | The **effective** request, as assembled by the provider — not the caller's arguments. A role configured with a raw `sampling_payload` has its per-call sampler arguments discarded, so the two differ. |
| `marginalia.params_source` | `effective` or `requested`, so a reader never has to guess which of the two they are looking at |
| `marginalia.sampling_payload_applied` | Whether an operator's raw payload won |
| `marginalia.finish_reason` | What litellm reported |
| `marginalia.native_finish_reason` | What the **provider** reported, before litellm rewrote it |
| `marginalia.finish_reason_unmapped` | True when that native reason is a hidden abnormal stop |
| `marginalia.finish_reason_available` | **False means we do not know how the call ended**, not that it ended cleanly. The CLI providers report no finish reason at all. |
| `marginalia.latency_ms` | Wall-clock duration of the provider call |
| `marginalia.reasoning_content_present`, `marginalia.reasoning_stripped_chars` | Reasoning-split forensics |
| `marginalia.total_cost_usd` | Reported by the `claude_cli` provider only |
| `marginalia.error`, `marginalia.error_type`, `marginalia.error_category`, `marginalia.retryable` | Present on a failed call; the span status is then `ERROR` |
| `marginalia.degraded` | Always present. `false` only for a clean, non-empty, normally-finished answer |
| `marginalia.degraded_reason` | Present when `marginalia.degraded` is true: `provider_error`, `truncated`, `abnormal_stop`, or `empty`. Also emitted as a **tag** of the same name, which is what the MLflow UI and `search_traces` filter on |

Span **inputs** are the messages and params; span **outputs** are the response
text and any tool calls.

### Why `finish_reason_available` exists

An absent finish reason is a fact worth recording. The three CLI
pseudo-providers (`claude_cli`, `codex_cli`, `pi_cli`) genuinely do not report
one. Writing `stop` for them would manufacture a clean ending out of missing
data — the same class of mistake as litellm's own `map_finish_reason`, which
rewrites an *unmapped* provider reason into a clean `"stop"` and keeps the
truth only in `native_finish_reason`. Okto Neuron records both, plus the
`finish_reason_unmapped` flag that says the two disagree in a way that matters.

### A degraded answer is never a clean success

`OK` is reserved for a clean answer: non-empty text, finished normally,
nothing rewritten. Every degraded call is `ERROR`, and
`marginalia.degraded_reason` says which kind:

| `degraded_reason` | Meaning |
|---|---|
| `provider_error` | The provider call **raised** |
| `truncated` | `finish_reason = length` — text exists but is cut off |
| `abnormal_stop` | Any other non-`stop` reason, or a `stop` litellm normalized from an unmapped/known-abnormal native reason |
| `empty` | Returned empty or whitespace-only content |

**Why not `UNSET` for the degraded-but-returned cases?** It is unreachable.
MLflow's vocabulary has three codes, but OpenTelemetry's SDK *silently ignores*
`set_status(UNSET)` (`opentelemetry/sdk/trace/__init__.py`, "Ignore calls to
set to StatusCode.UNSET"), so ending the span `UNSET` is a no-op and it keeps
the `OK` its root started with — verified against the live tracking server on
2026-09-20, where a real `finish_reason="length"` ask landed as `state=OK`.
Between a status that overstates the problem and one that hides it the choice
is not symmetric: an answer that came back truncated or empty is not a success,
and the reason attribute keeps a hard failure distinguishable from a degraded
one.

Before this, the status was `ERROR` on an exception and `OK` on everything
else, so a completion that came back truncated, abnormally stopped, or empty
was reported in the MLflow UI as a clean success. That is the same defect a
LoCoMo run hit on the harness side: 304 answers persisted with `status="ok"`
and `finish_reason="stop"` while the ask trace said
`synthesis_status="provider_error"`, and the summary would have reported
`ask_error: 0` beside a judge percentage computed from nothing. An empty or
degraded answer is never a success, in the ledger or in a span.

Precedence, and the vocabulary itself, deliberately mirror the ask path's
`retrieval["synthesis_status"]` (`companion/__init__.py`, `_mark_provider_error`
and `_mark_finish_reason`): `provider_error` > `truncated` > `abnormal_stop` >
`empty`. The telemetry layer does **not** read `synthesis_status` — that value
is stamped one level above this wrapper and only for `ask`, while the wrapper
instruments *every* `complete()` (curation, judging, the CLI providers). It
derives the same verdict from the same underlying signals, so one mental model
filters both.

Two things it deliberately does not call degraded:

- A **tool-call turn** with no prose. Empty content there is correct, and
  flagging it would invent a failure.
- `finish_reason_available = false` on its own. The CLI providers never report
  a finish reason; that is already stated by its own attribute, and marking
  every CLI call degraded would make everything look broken while saying
  nothing new. An empty answer from one still reports `empty`.

To find degraded calls in the MLflow UI, filter on the tag:

```
tags."marginalia.degraded_reason" = 'truncated'
```

or read them back over REST. MLflow 3's trace search lives under `/api/3.0/`;
the `2.0` path answers `405 Method Not Allowed`:

```
POST http://<tracking-host>:5000/api/3.0/mlflow/traces/search
{"locations": [{"type": "MLFLOW_EXPERIMENT",
                "mlflow_experiment": {"experiment_id": "<id>"}}],
 "max_results": 100}
```

Each returned trace carries `state` plus its `tags`, so `state == "ERROR"` and
`tags["marginalia.degraded_reason"]` together say what went wrong and how.

## Guarantees

1. **Env-gated.** With `OKTO_NEURON_MLFLOW_TRACKING_URI` unset, `get_provider()`
   returns the provider object unwrapped, `mlflow` is never imported, and every
   existing log line, stats field, and payload is byte-identical.
2. **Fail-open.** An unreachable tracking server, a missing `mlflow` install, or
   an MLflow internal that moved logs one warning per process and is then
   ignored. Telemetry never fails or retries an operator's LLM call.
3. **Off the critical path.** The capture step does no I/O. It hands a
   pre-serialized record to a bounded queue drained by one daemon thread.
   When that queue is full the record is **dropped on purpose** — blocking a
   completion to make room for its own telemetry would be the latency
   regression the design exists to avoid.

### Concurrent first use: one resolution, and "already exists" is success

Fail-open used to have a hole that turned a healthy server into a disabled
exporter. `_client()` resolves the MLflow client and experiment once per
process, and it is not only called from the drain thread: `trace_parent` and
`trace_child` resolve it synchronously on the caller's thread. When several
threads made a process's first traced call at the same moment, every one of
them looked up the experiment, found nothing, and called `create_experiment`.
One won; every loser got `RESOURCE_ALREADY_EXISTS`, which the error branch
treated like an unreachable server, so it set `_CLIENT_FAILED` and export was
off for the rest of the process. The single warning then said "could not reach"
a server that had answered fine. Measured against the live server before the
fix: 12 threads, first use at the same instant, **0 of 12 traces landed**, three
runs out of three.

Two changes close it. The one-time resolution is double-checked under a lock,
so only one thread ever looks up and creates; the warm path is still two global
reads. And `RESOURCE_ALREADY_EXISTS` from `create_experiment` now means "the
name is taken, which is what we wanted": the experiment is fetched and adopted.
The lock only serialises threads in one process, and a benchmark harness and the
daemons it spawns can still race on the same new experiment across processes, so
the second change is not redundant with the first. Only a genuine failure
(unreachable server, auth, `mlflow` not installed) still disables export, and
its warning now names the exception. Same test, same server, after the fix:
**12 of 12** and **24 of 24**.

Anything running concurrent LLM calls is exposed to this, which in practice
means any run with `consolidation.curation_max_concurrent` above 1.

## How it is wired

The wrapper is installed at `get_provider()` (`src/okto_neuron/llm/__init__.py`),
the single seam every provider passes through, so the CLI pseudo-providers are
covered too. It sits **innermost**, under the companion's
`_StepLabelledProvider` and `_TracingLLMProvider`, so the step label is already
set when it reads it and the latency it measures is the provider's.

It reads the effective request through the existing single-slot
`_set_request_observer` thread-local, and **forwards to whatever observer was
already installed** rather than replacing it, so the ingest inspector's
one-`llm_request`-event-per-call invariant is preserved.

## Tracing a call this layer did not make

`get_provider()` covers every completion that goes **through** Okto Neuron's
provider layer. Some legitimately do not. The LoCoMo benchmark judge (part
of a separate, private harness) runs offline against an already-written
`answers.jsonl` — no vault, no daemon — and POSTs to the judge endpoint
itself. Those calls hit the same models and the same endpoints as the daemon's
own, which makes them exactly the ones worth comparing against it, and they
were invisible.

`okto_neuron.llm.trace_external_completion` is the seam for that case. It is a
context manager over a call the caller makes itself:

```python
from okto_neuron.llm import trace_external_completion

with trace_external_completion(
    model=judge_model,
    step="judge",
    provider="zai",
    api_base=judge_api_base,
    messages=messages,
    params={"temperature": 0.0},
) as span:
    body = post_chat_completion(...)   # the caller's own request
    span.set_openai_response(body)
```

Report the outcome with whichever setter matches what you have:

| Setter | For |
| --- | --- |
| `set_openai_response(body)` | a raw OpenAI-shaped response (dict, or an SDK object with `model_dump()`) |
| `set_response(text=…, usage=…, finish_reason=…, …)` | pieces you already extracted |
| `set_error(exc)` | a failure you caught and handled yourself |

An exception that **escapes** the `with` block is recorded as
`provider_error` and re-raised unchanged — a call that raised is the one
outcome a caller cannot forget to report. `set_error` exists for the opposite
case, where the caller swallows the failure and turns it into a result of its
own (the judge returns a `judge_error` row rather than raising), and the span
would otherwise claim a clean success.

This is **not a second implementation**. It builds the same field dict the
provider wrapper builds and hands it to the same `record()`, so the queue, the
drain thread, the attributes, the tags and the `degradation_reason()`
precedence are all literally the same code. A judge span and a daemon span are
the same kind of object, and one `search_traces` filter finds both:

```python
client.search_traces(
    experiment_ids=[exp],
    filter_string="tags.\"marginalia.degraded_reason\" = 'truncated'",
)
```

The three rules hold unchanged. With `OKTO_NEURON_MLFLOW_TRACKING_URI` unset
the context manager yields a no-op handle whose setters do nothing, `mlflow`
is never imported, and nothing is timed or captured — so a caller writes the
same code either way and pays nothing when telemetry is off. `params_source`
is always `requested` on this path, never `effective`: the caller built the
request, so there is no wire-level observer behind it and claiming otherwise
would overstate what was captured.

### Saying which run a span belongs to

A span's own tags say what the **call** was — provider, model, step, degraded
reason. They cannot say what the **run** was: a daemon spawned by a benchmark
arm has no idea which arm it is. The spawner does, and says so in the same
place it already says where to export — the child's environment.

`OKTO_NEURON_MLFLOW_TAGS` is a JSON object of extra tags stamped on every span
the process exports:

```bash
OKTO_NEURON_MLFLOW_TRACKING_URI=http://mlflow.example:5000 \
OKTO_NEURON_MLFLOW_EXPERIMENT=locomo \
OKTO_NEURON_MLFLOW_TAGS='{"locomo.run_name":"glm-full-a","locomo.phase":"ask"}' \
  okto-neuron serve ...
```

A caller that makes its own call can also pass `tags={...}` to
`trace_external_completion`. Precedence runs one way and only one way: the
span's own `marginalia.provider` / `marginalia.model` / `marginalia.step` /
`marginalia.degraded_reason` always win, then per-call tags, then the
environment's. A spawner cannot make a span lie about the call it describes.

Malformed JSON, a JSON array, an unset variable — each yields no extra tags
and at most one warning. This is why **one** experiment can hold every arm:
run identity is a tag, so cross-arm queries stay possible, which an
experiment-per-arm layout would make impossible.

### Draining before a short process exits

The export queue is drained by a **daemon** thread. A long-running daemon
never notices, but a process that exits moments after its last completion —
`score.py` is a burst of judge calls and then an exit — can drop whatever is
still in flight. `okto_neuron.llm.flush_telemetry(timeout_s)` blocks until the
queue drains and returns whether it did. It is a shutdown call only; nothing
on a request path may use it, and it returns `True` immediately when
telemetry is off, so a caller can invoke it unconditionally.

### The subprocess boundary

Inside the daemon, every completion runs in the killable helper process
(`llm/_litellm_worker.py`), because both the ingest and ask paths install a
cancel predicate. The parent therefore never sees litellm's native response —
only the JSON the worker serializes. Fields the worker did not send simply do
not exist upstream, however carefully the parent reads for them.

The worker payload is versioned. **v1** carried `content`, `finish_reason`,
`prompt_tokens`, `completion_tokens` and `cached_tokens`. **v2** adds
`native_finish_reason`, `reasoning_content`, `tool_calls`, `total_tokens` and
`reasoning_tokens`, and stamps `"protocol": 2`. The change is backward
compatible in both directions: the parent reads every v2 field with a `.get`
default, so a v1 payload yields exactly the values it yielded before, and an
older parent ignores the extra keys.

This fixes a real gap independent of MLflow: `native_finish_reason` was read by
the parent but could never be populated in the daemon, because the worker
dropped it. Z.ai reports a mid-generation transport failure as `network_error`,
which litellm maps to a clean `"stop"` — the precise case
`_NATIVE_ABNORMAL_STOP_REASONS` exists to catch, and which was silently
unreachable in daemon traffic.

The MLflow client is **never** built inside the worker. It is a short-lived
per-call subprocess; a client init per call would be a serious latency
regression. The worker returns richer JSON and the parent builds the span.

---

## The CLI pseudo-providers stopped being a blind spot

`codex_cli`, `claude_cli` and `pi_cli` had the worst observability in the
codebase: no finish reason at all, no usage line any accounter could read, and
sampling parameters accepted and discarded in silence. All three are fixed at
the shared base class (`llm/_cli_provider.py`) so they cannot drift apart
again.

### One canonical usage line

The LoCoMo harness's usage parser is the only durable record of a run's token spend —
`/api/v1/ask` returns no usage — and its regex matches the literal prefix
`litellm usage `. The CLI providers logged `codex_cli usage ...`,
`claude_cli call ...` and `pi_cli usage ...` instead, so **a CLI-provider run
reported zero tokens.** Not an error; silence, which is worse.

`CliShellProvider._log_canonical_usage` now emits the canonical line for all
three, once per call:

```
litellm usage model=codex_cli/gpt-5.5 prompt_tokens=11824 completion_tokens=5 cached_tokens=11648 step=extraction
litellm usage model=claude_cli/glm-5.3-flash prompt_tokens=54 completion_tokens=3 cached_tokens=0 step=extraction
```

The `litellm usage ` prefix is a wire format shared with the harness, not a
claim about which library made the call. The `<driver>/<model>` label carries
that truth, mirroring `LiteLLMProvider.model`, so a ledger can tell a CLI row
from a hosted one. Each provider keeps its own richer line in addition —
claude's `total_cost_usd`, pi's cost, codex's reasoning tokens.

`tests/llm/test_cli_provider_accounting.py` replicates the harness regex
verbatim and asserts against it, so a drift on either side fails a test instead
of quietly zeroing a run.

### Finish reasons: real where real, absent where unknown

Each CLI is different, and the mapping follows what the binary actually
reports — verified against the installed versions, not documentation.

| Provider | Native signal | Mapped `finish_reason`? |
|---|---|---|
| `claude_cli` | Anthropic's own `stop_reason` in the result element (`end_turn`, `max_tokens`, `tool_use`, `refusal`, …), plus a separate `terminal_reason` for the CLI turn | **Yes** — a real mapping exists |
| `pi_cli` | message-level `stopReason` (`stop`, `length`, `toolUse`, `aborted`, `deferred`, `pending`, `error`) | **Partly** — the first three map; the rest do not |
| `codex_cli` | nothing. The terminal event is exactly `{"type": "turn.completed", "usage": {...}}` (codex-cli 0.144.6, captured live) | **No** |

Where no faithful mapping exists, none is invented. The raw string still
travels as `native_finish_reason`, `finish_reason_unmapped` is set, and
`finish_reason_available` stays `False` — an honest "we do not know how this
call ended" rather than a fabricated clean stop. codex's `turn.completed`
describes the *agent loop* finishing, not how the model's last message ended,
so calling it `"stop"` would assert something codex never said.

This has a consequence beyond tracing: `extract/` retries on
`finish_reason == "length"`. A truncated `claude_cli` or `pi_cli` extraction was
previously indistinguishable from a complete one and kept its half-written
JSON. Now it is caught.

### Sampling parameters are reported, not swallowed

`codex exec --help` (codex-cli 0.144.6) and `claude --help` expose no
generation parameter at all — only codex's `-c key=value` overrides of the
user's own `config.toml`. `pi --help` exposes exactly one, `--thinking <level>`.

So rather than a new mechanism, the CLI providers use the same seam
`LiteLLMProvider` uses. Every configured-but-unsendable value goes to
`_notify_request_observer` as an omitted parameter with a reason, reaching the
ingest inspector and the MLflow span alike, and
`_warn_cli_dropped_params` logs it once per `(driver, model, parameter set)` —
the same dedupe shape as `_warn_gateway_narrowed_drops`, because one run makes
thousands of calls:

```
codex_cli exposes no flag for parameter(s) max_tokens, temperature, top_p;
they were NOT sent and the model ran with the CLI's own defaults for them.
```

`pi` does carry `enable_thinking=False` as `--thinking off`, with one guard: if
the configured model string already pins a level via pi's own
`provider/id:<thinking>` syntax, that more specific instruction wins and the
conflict is reported instead of silently overridden. `enable_thinking=True`
names no particular level, and the CLI's default already is a level, so sending
one would be a guess — it is reported as not-sent.

### Endpoint

A CLI provider has no HTTP endpoint, and `resolved.api_base` is a required
field carrying the vault's unrelated loopback default. A span reporting *that*
would claim a hosted CLI call ran against a local server, so these providers
report `cli:codex` / `cli:claude` / `cli:pi` instead — the transport is a
subprocess, and which subprocess is the only part we actually know.
