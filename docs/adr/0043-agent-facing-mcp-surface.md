# ADR 0043: The Agent-Facing MCP Surface — Per-Call Vault Selection, Exposed Retrieval Policy, and Failures That Announce Themselves

- **Status:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Marginalia maintainers
- **Builds on:** ADR 0014 (multi-vault serve — `?vault=` per-connection selection,
  `resolve_vault_selector` as the single resolution seam), ADR 0034 (application, daemon,
  browser and multi-vault contract — immutable per-vault runtime context, pool leasing and
  fencing), ADR 0041 (pluggable graph backend — the backend a vault resolves to is pinned at
  creation via `storage.backend`)
- **Amends:** ADR 0014, ADR 0034
- **Relates to:** ADR 0009 (curation control plane), ADR 0013 (durable candidate ledger),
  ADR 0024 (validity subset carried on hits), ADR 0035 (dynamic litellm parameter editor — the
  precedent for an agent-facing parameter surface, added and then removed outright 2026-09-15)
- **Scope:** the five MCP tools exposed by `src/marginalia/server/runtime.py` (`ask`, `explore`,
  `remember`, `init_vault`, and the new `list_vaults`), the vault-resolution and pool-lease
  seams they call, the MCP progress-notification bridge and `init_vault`'s hint (D11/D12,
  added 2026-09-18), the `synthesis_status` / `finish_reason` fields stamped by
  `src/marginalia/companion/__init__.py`, the excerpt markers in the assembled source context,
  and the name validation in `src/marginalia/vault_registry.py`
- **Out of scope:** the REST and web-UI surfaces (ADR 0034 §4 owns client-scoped browser
  selection; `docs/web-ui.md` owns the query controls), the retrieval algorithm itself
  (ADR 0028, ADRs 0030–0031), and the entity-resolution path (ADR 0042)
- **Implementation status:** shipped in the working tree — `server/runtime.py`,
  `server/http.py`, `companion/__init__.py`, `llm/__init__.py`, `vault_registry.py`, with new
  tests in `tests/test_ask_synthesis_status.py`, `tests/companion/test_excerpt_markers.py`,
  `tests/server/test_resolve_vault_selector.py`, and `tests/llm/test_provider.py`.
  **Amended 2026-09-18** with D11 (MCP progress notifications during `remember`) and D12
  (`init_vault`'s optional `hint`); both are in the working tree and not in any published
  artifact. Amendment was chosen over a new ADR because this ADR is the record of the
  agent-facing surface, and a surface change belongs in it

---

## Purpose

An agent talking to Marginalia over MCP had four tools, two knobs, and no way to tell a broken
answer from an empty graph. It could not discover which vaults existed, could not name one per
call, could not reproduce what the web UI was doing, and could not distinguish a provider
outage from a vault that genuinely knew nothing. This ADR records the decisions that closed
that gap, and the disclosure rules that constrain how far the surface was allowed to open.

## Evidence that triggered this ADR

- An `ask` against a live vault returned `text=""` with a full citation list while llama.cpp
  was answering HTTP 500 with an empty body. `except LLMProviderError: text = ""` turned an
  outage into a success-shaped answer; the consuming agent concluded the graph had nothing and
  abandoned the line of inquiry.
- A retrieval served bytes `0-5959` and `14643-15070` of a 32,111-byte file. The answer lived
  at roughly byte 25,443. The model, unable to tell a whole file from two slices of one,
  extrapolated and returned R$ 470,58 where the record said R$ 502,51 — a fabricated number
  indistinguishable in shape from a correct one.
- A charset-valid but over-long vault name reached `os.stat` and the raw
  `[Errno 63] File name too long: '/Users/<user>/.marginalia/vaults/…'` propagated to the MCP
  caller, disclosing the operator's home directory and the internal vault layout on a surface
  with no loopback gate.
- A connection pinned with `?vault=beta` silently swallowed both `vault="../../../etc"` and
  `vault="nao-existe-xyz"` and answered normally, giving a traversal attempt and a typo the
  same non-signal.
- A client whose `${MARGINALIA_TOKEN}` placeholder expanded to an empty string got the same
  `bearer authentication required` 401 as a wrong token, so operators rotated credentials
  instead of fixing the placeholder.

## Decision

### D1. Vault selection gains a per-call argument; the connection still wins.

ADR 0014 made vault selection per connection via `?vault=`. That stays true and is amended,
not replaced: `ask`, `explore` and `remember` accept an optional `vault: str | None = None`.

- **The connection's `?vault=` takes precedence over the argument.** `?vault=` is a deliberate
  hand edit of a client config; an agent-supplied value must never silently override it. The
  override only substitutes into the selector position when the connection supplied none, so
  every downstream rule — configured default, sole-vault fallback, typed errors — is untouched.
- **The argument accepts registry NAMES only.** Path-shaped values (leading `~`, absolute,
  containing `/` or `\`, a drive letter) are rejected with `forbidden_path_selector`.
  `_resolve_vault_path_selector` accepts an absolute path whenever `is_loopback` is true, and
  `_mcp_request_selector` reports `is_loopback=True` whenever there is no HTTP context at all —
  so a per-call path argument would have been an unfenced read of arbitrary on-disk vaults.
- **The override is always validated, even when it loses.** Shape and resolvability are checked
  regardless of precedence, so an unknown name or a traversal attempt errors instead of being
  discarded in silence. On the losing path the code deliberately does not call
  `state.runtime_for`: validating must not open or adopt a pool entry for a vault that is not
  serving the call.
- **A discarded override is echoed as `retrieval["vault_override_ignored"]`,** carrying the
  canonical registry spelling via `_canonical_vault_name`. Case-insensitivity applies to the
  string, never to the resolved path — on a case-insensitive filesystem `root/"BETA"` is a real
  directory whose `Path` differs from registered `root/"beta"`.
- **`remember` resolves after its loopback write gate,** not before.

### D2. `list_vaults` exposes names, and nothing else.

A fifth MCP tool (`runtime.py:1523+`) returns `{vaults: [{name, current, backend}, …]}`. No
`path`, no `id` — the id is derived from the path. An in-code comment forbids switching to
`VaultEntry.to_json()` or `http._vaults_payload`, both of which leak filesystem layout to any
agent that reaches the MCP port. `current` is computed from the *connection's* selector
(`_mcp_request_selector` + `_resolve_vault_path_selector`), falling back to
`state.vault_path`; computing it from `state.vault_path` alone reported `current=false` for the
very vault a `?vault=<name>` connection was pinned to, because the daemon starts with
`vault=(none)`. A `VaultResolutionError` is swallowed so discovery keeps working: on a
multi-vault server with no default, `vault_selector_required` is precisely when the list is
most needed.

### D3. The `.marginalia-vault` project pin is the agent's job to read.

The `list_vaults` docstring tells the agent to read the project's `.marginalia-vault` file
(`{"vault": "<name>"}`) itself. Neither the server nor the MCP client reads it. One daemon
serves every project on the machine and its cwd is not the caller's, so any server-side
"current vault" derived from a project pin would be global by construction.

### D4. The retrieval policy is exposed to agents as flattened scalars.

`AskRetrievalPolicy` has 18 fields: the web UI already shipped 13 as controls
(`frontend/src/types/index.ts:525-537`) and the five `seed_*` diversification knobs
(`seed_diversity`, `seed_subject_cap`, `seed_entity_min`, `seed_rel_min`, `seed_scalar_max`) are
exposed on neither surface. MCP filled exactly one (`hops`). `ask` now takes
`enable_subgraph`, `source_block_policy`
(`never`/`on_coverage_miss`/`always`/`blend`), `seed_k`, `max_degree_per_seed`,
`neighbour_budget_tokens`, `source_block_budget_tokens`, `coverage_threshold`,
`min_claim_confidence`, `max_nodes`, `max_relationships`, `max_claims`, `relationship_types`,
plus `include_sources` (`runtime.py:1176-1300`).

- **Flattened scalars, not a nested dict,** so the tool schema is self-describing to the model.
- **`None` means inherit.** Only keys the caller actually set are passed to
  `AskRetrievalPolicy`, so an unset parameter can never clobber a vault default with a
  hardcoded one. That rule is what makes it safe to add the 12 policy knobs without changing the
  behaviour of any existing call. `include_sources` is not covered by it — it is not an
  `AskRetrievalPolicy` field and defaults to `False`, which is what keeps the existing payload
  byte-identical (D5).
- **`enable_subgraph` is exposed but its default is unchanged (off).** A grounded eval measured
  block-dump answers at 0.792 against the subgraph path at 0.6.
- **`seed_k` is clamped to `MAX_QUERY_K` (100)** on MCP, like `k`. This is a documented
  divergence from REST. `hops` is clamped 1..5 and takes effect only under subgraph retrieval.
- **A pydantic `ValidationError` becomes `invalid retrieval policy: <loc>: <msg>`,** not a raw
  traceback.

`explore` gains `relationship_types`, `min_claim_confidence` and `max_degree_per_seed` — all
already accepted by `build_ego_graph`, never exposed. `max_degree_per_seed`
(`companion/__init__.py:7763`) and `min_claim_confidence` (`:7767`) resolve caller-override >
vault `llm.ask` config > code default via `_first_int`/`_first_float`, so an explicit `0`/`0.0`
from the caller is honoured rather than falling through. `relationship_types` has **no config
fallback**: it goes to `build_ego_graph` as caller-or-`None` (`:7794`, with the reasoning in the
comment at `:7816-7818`), so the echoed value is simply what the caller passed, `[]` meaning
unrestricted. `explore` also gains a `retrieval` block reporting the *effective* values (`mode`, `seed_k` — `null` in node mode where `k` is unused,
`hops`, `max_degree_per_seed`, `min_claim_confidence`, `relationship_types` with `[]` meaning
unrestricted), and its serialized relationships and claims now carry `block_id`. `RelRecord`
and `ClaimRecord` always had it; only the serializer dropped it, leaving relationships and
claims unanchorable while `nodes` entries were anchorable. The top-level `hops` key stays
duplicated because it is an existing contract.

### D5. Provenance is emitted vault-relative or not at all.

`include_sources=true` adds a `sources` list to the ask payload: `id`, `block_id`,
`byte_start`, `byte_end`, `content_hash`, an optional vault-**relative** `path`, and the
ADR 0024 validity subset. Default false, so the existing payload is byte-identical.
`QueryHit.provenance.path` is absolute and `ask` has no loopback gate (unlike `remember` and
`init_vault`), so it is reachable remotely under `--allow-remote`; when relativization fails
the `path` key is omitted entirely rather than falling back to the absolute form.
`validity_subset` is factored out of the REST `_serialize_hit` (`http.py:4808`), preserving key
order for REST byte-parity, so the two surfaces share one implementation instead of drifting.

`retrieval["vault"]` on `ask` and `explore` carries the registry NAME of the vault that
actually served the call, matched from `runtime.vault_path` against the registry
(`_serving_vault_name`, `runtime.py:1125-1136`). The basename fallback is reachable only for a
vault the caller selected by path over a loopback `?vault=<path>` connection, so it discloses
nothing they did not supply. Both fields are injected in the MCP layer, leaving the companion's
trace and every REST regression pin unchanged.

### D6. A degraded answer must never look like a successful one.

`retrieval["synthesis_status"]` is now always present on every ask path, one of `ok`, `empty`,
`provider_error`, `truncated`, `abnormal_stop`, with precedence
`provider_error` > `truncated` > `abnormal_stop` > `empty` > `ok`; `provider_error` is never
overwritten. A truncated answer that stripped to empty reports `truncated`, because the cause is
known and actionable whereas `empty` would imply the model chose to say nothing.

A status field was chosen over raising so the documented graceful-degradation contract for
REST and the UI stays true: on `LLMProviderError` the answer still degrades to `text=""`, but
stamps `provider_error` plus a bounded, whitespace-collapsed `retrieval["provider_error"]`
summary capped at 300 characters. Credentials were already redacted upstream by
`_redact_api_key`; the summary never re-expands the message and never adds prompts or paths. A
field that appears only on failure is one clients forget to check, hence the always-present
`setdefault` backstop. `explore` deliberately has no `synthesis_status` — it makes no LLM call.

`retrieval["finish_reason"]` is stamped always and `retrieval["native_finish_reason"]` only
when litellm normalized the raw value away. **Any `finish_reason` other than `stop` is
suspect.** litellm's `map_finish_reason` silently defaults any *unmapped* provider reason to
`"stop"`, so an unknown abnormal stop reached callers disguised as a clean one — but the same
map also contains clean-stop aliases (`end_turn`, `stop_sequence`, `eos_token`, `COMPLETE`,
`STOP`), so flagging is based on genuine absence from the map
(`_finish_reason_is_unmapped`, `llm/__init__.py:385-400`), never on
`native != normalized`. Addendum 2026-09-19: the map also contains the inverse trap, a
provider reason it rewrites *to* `"stop"` although the provider means an abnormal end (Z.ai's
`network_error`); those are listed in `_NATIVE_ABNORMAL_STOP_REASONS` next to the unmapped
test and flagged the same way. That distinction is computed in the LLM layer, where litellm's own map
is in scope, so callers cannot re-derive it wrongly. `_complete_ask` clears the per-thread
stats channel with `_set_last_call_stats(None)` first, so providers that report nothing
(`StubLLM`, the CLI provider) cannot leak a neighbouring call's reason; in the tier-1/tier-2
escalation paths the tier-1 state is captured immediately and re-stamped on every branch that
returns the tier-1 answer.

Related, same channel: `strip_reasoning` now handles a completion that *begins* inside a
reasoning block because the chat template prefilled the opening `<think>` into the prompt (the
standard Qwen-style pattern), so only the closing tag is ever emitted. The dangling case is
decided on the original text before paired substitution, so a paired `</think>` is not re-read
as an unopened one. `reasoning_content` is read with `getattr(..., None)` — mandatory, because
litellm deletes the attribute when the provider sent none — and when the provider populated
`reasoning_content` *and* characters still had to be stripped from `content`, a
`litellm reasoning split MISBOUNDED` warning fires with counts and model name only; never the
reasoning or the stripped text, both of which can carry vault data. litellm's own
`merge_reasoning_content_in_choices` is rejected in a comment: it is streaming-only and injects
`<think>` tags into content.

### D7. Every source block declares which bytes of which file it is.

The assembled context prefixes each block with one line:
`[EXCERPT source=cnpj/guias/catalogo.md bytes=0-5959,14643-15070 of 32111]`, so the model can
say the excerpts do not cover a period instead of extrapolating across the gap.

- `_hit_source_pieces` is refactored into `_hit_source_spans`, returning only the spans that
  actually read non-empty — a marker never claims bytes the model was not shown.
- Every component degrades independently: an un-relativizable path drops `source=` entirely (an
  absolute path must never reach the LLM or the answer), an unreadable size drops `of <n>`.
- `_source_total_size` goes through the same `_is_within_root` gate as `_read_slice`: stat-ing a
  path the read guard would refuse discloses the existence and size of a file outside the vault.
- Markers are attached **after** ranking (`_reattach_excerpt_markers`, re-pairing by value in
  FIFO order, exact because the re-rank is stable). Letting marker tokens into
  `_query_term_ranked_snippets` would reorder source blocks, i.e. change retrieval behaviour.
- A node-NAME fallback gets no marker; labelling it with a byte range would be fabricated
  provenance.
- The prompt-side explanatory sentence is conditional on a marker actually being present, so
  marker-free contexts keep their prompt byte-for-byte, and it lives in `_complete_ask` rather
  than `_ASK_SYSTEM` because that prompt is overridable per vault (`llm.ask.system_prompt`) and
  a configured vault would silently lose it.

### D8. No filesystem path appears in any client-facing error.

- **A 255-character vault name cap, validated before any filesystem access**
  (`vault_registry.py:24-28`, `420-424`). 255 is exact, not a guess: the name is used verbatim
  as one path component under the vault root and is never embedded in a longer filename, so the
  binding constraint is `NAME_MAX` for a single component on ext4/btrfs/xfs and APFS/HFS+.
  `_VAULT_NAME` is ASCII-only, so `len(name) == len(name.encode("utf-8"))` and checking
  characters is exactly checking bytes, which sidesteps the HFS+ (255 bytes) versus APFS
  (255 UTF-8 characters) divergence.
- **One sanitiser at both seams.** `_resolve_vault_path_selector` becomes a thin guard over
  `_resolve_vault_path_selector_unguarded`; any escaping `OSError` becomes a typed, path-free
  `vault_unavailable`, with the full original (including `.filename`) going to the server log
  only. `VaultPoolError` messages are built as `"…: {key}"` where key is the resolved absolute
  vault path, and both the resolver and `_lease` used to pass `str(exc)` through verbatim —
  `_pool_error` is now the single sanitiser used at both seams so they cannot drift, preserving
  the stable machine `code` and replacing only the human text.
- **ELOOP is handled specially.** `pathlib.Path.resolve()` converts an ELOOP `OSError` into
  `RuntimeError("Symlink loop from %r")` carrying the offending absolute path, and since
  `vault_registry.resolve_vault_reference` resolves every name, a symlink loop under a vault
  NAME escaped this seam raw. The catch discriminates on **shape, not type**: `check_eloop`
  raises from inside `except OSError`, so the ELOOP `OSError` is always the `RuntimeError`'s
  immediate `__context__`; anything else is re-raised untouched.
- **The absolute-path branch no longer echoes a resolved path.** `_safe_selector_label` echoes a
  plain name (the caller's own input) but collapses any path-shaped selector to "the selected
  vault", because `resolve()` expands symlinks and `expanduser()` leaks `HOME` for a `~`
  selector. The `unknown_vault` message for a path selector names no path at all; the caller
  already knows which path it asked about.
- **`vault_unavailable` is a new code, deliberately not a reuse of `unknown_vault`,** because
  `unknown_vault` tells the agent to run `init_vault` — false remediation for an EACCES or EIO
  on a vault that exists.

### D9. Diagnosable 401s without a client-visible contract change.

`_presented_bearer_token` now returns `(token, reason)` and `_BEARER_DETAIL`
(`http.py:542-609`) maps it to one of four human details: `missing` (no `Authorization`
header), `empty` (header present, bearer credential blank — the `${MARGINALIA_TOKEN}`
placeholder expanded to nothing), `scheme` (non-Bearer scheme), `invalid` (well-formed token
that does not match). The machine `error` code stays `unauthorized` in all four cases, so
client branching is unchanged, and no detail echoes any part of the presented credential
(asserted by `tests/server/test_task6_write_gates_and_auth.py:343`).

### D10. `serverInfo` reports the real Marginalia version.

`FastMCP("marginalia")` becomes `FastMCP("marginalia", version=MARGINALIA_VERSION)`
(`runtime.py:1110`). The initialize handshake previously advertised FastMCP's own package
version, so no client could tell which Marginalia build it was talking to.

### D11. A long `remember` emits MCP progress notifications, so the client stops aborting it.

`remember` used to return nothing at all until it was finished. A measured live ingest of a
15,882-byte, 3-block Markdown file ran **1,536 s** end to end — 271 s of extraction against
1,266 s of curation (default local endpoint `127.0.0.1:8123/v1`, qwen3.8-27b on a Mac; the same
file on a LAN inference server took 297 s — see ADR 0039's measured-cost addendum) — and the calling
client aborted at roughly 300 s of silence ("sent no response or progress for 300s"). The daemon finished the work; the abort discarded the payload,
and the only trustworthy success signal (`units.succeeded == blocks_total`) lives in that payload.
A successful ingest was indistinguishable, to the agent, from a hung one.

`_mcp_progress_bridge` (`server/runtime.py`) builds the `on_progress` callback that `remember`
already accepted but the MCP tool never passed, and wires it at the `asyncio.to_thread` call site.

- **The loop and the `Context` are captured on the event loop, not in the callback.** `remember`
  runs under `asyncio.to_thread`, so `on_progress` fires on a worker thread, while
  `Context.report_progress` is a coroutine that must run on the loop; `get_context()` reads
  context-local state a worker thread does not have.
- **The future is discarded.** The worker hands the coroutine to `run_coroutine_threadsafe` and
  never joins it, so ingest speed never depends on notification delivery and a delivery failure
  can never fail the ingest. Failures are swallowed and logged at debug: the ingest is the
  product, the notification is telemetry.
- **Coalescing is on the exact `(stage, blocks_done)` pair,** because several alternate paths in
  the per-block loop emit the same pair two or three times for one block. The result is one
  notification per block completion plus one per stage change — deliberately no throttle, which
  could drop the very notification the idle timer needs.
- **Returns `None` when there is no MCP context at all** (in-memory and REST callers), so the
  caller passes `on_progress=None` rather than a callback that cannot do anything.
  `report_progress` itself no-ops when the client sent no `progressToken`.
- `blocks_total` is `0` during `parsing`; the bridge sends `total=None` so a client computing a
  percentage does not divide by zero.

Per-block notifications alone were not enough. Curation is serial by default and dominates the
wall clock, so the phases *after* extraction still went dark for many minutes. The companion now
emits sub-stage keep-alive ticks during dedup and curation, gated on "every 5 items OR 20 s,
whichever comes first" — item count alone cannot bound the silence when N items are N unbounded
serial LLM calls. Those ticks carry an item ordinal with an explicitly undeclared total and are
routed away from the block counters server-side; ADR 0039's 2026-09-18 addendum owns that contract.

### D12. `init_vault` may return a third key.

**This changes a documented return shape.** `init_vault` returned `{name, path}`. It now returns
`{name, path}` plus an optional `hint` — present only when the freshly created vault has no usable
LLM model.

A vault created with the application defaults inherits `provider` and `api_base` from
`LLMDefaults` but an **empty** `model` (discovery-first: the defaults never claim a model the
endpoint may not serve). Creation genuinely succeeds, so this is a hint on the result rather than
an exception — but without it `init_vault` reported a clean creation for a vault whose very first
`remember` is refused, and the agent learned that only from the refusal. `_vault_llm_model_hint`
(`server/runtime.py`) mirrors the pre-flight guard now in `Companion.remember`, and returns `None`
whenever the vault has a usable model or the config cannot be read — a hint must never break
creation, so every exception inside it is swallowed.

The key is **additive and absent on the healthy path**, so a client that reads only `name` and
`path` is unaffected; this is a widening of the shape, not a replacement.

## Consequences

- **The tool-surface count moves from four to five,** which is asserted in five acceptance
  scenarios (`tests/acceptance/scenarios/{50,52,80,81,82}*.sh`) and two pytest files
  (`tests/server/test_runtime_startup.py:166-180`, `tests/test_dependency_contract.py:1045`).
- **Supersession is a graph concept still handled as text.** Nothing writes the supersession
  facet outside the incremental-edit correction pass, and write-time contradiction detection
  only ever sees ENTITY candidates, never relation-derived claims. Three distinct symptoms were
  traced to this in a single day. Not fixed here.
- **Seeding has no per-source or per-block quota,** so a document split into N blocks fields N×
  as many competing claim nodes as a single-block newer note. Recency and correctness lose to
  block count.
- **`k` bounds claim NODES, not distinct source blocks,** so raising `k` cannot reach material
  outside the top-k blocks. The knob an agent will reach for first does not do what its name
  suggests.
- **`min_claim_confidence` is inert on the default block path** — accepted on the tool, threaded
  into the policy, never read, and not echoed. It is exposed anyway for parity with the UI and
  the subgraph path; a caller cannot currently tell it did nothing.
- **Assembled context can repeat the same block bytes.** The anchor dedup exists but is gated
  off on the block path, deferred pending the gold/distractor eval gate.
- Two tests that indexed `splitlines()[0]` moved to `[1]`, because the first line of a source
  block is now the excerpt marker (`tests/test_efficient_hybrid.py:112`,
  `tests/test_seed_diversity.py:454`).
- The docs-integrity test that pinned the one-line `def ask(question: str, k: int = 20,
  hops: int = 1)` signature now flattens whitespace and pins the defaults rather than the
  formatter's wrapping (`tests/test_docs_integrity.py:311-312`).

## Known gaps deliberately not taken now

- `explore` has no `synthesis_status` and no `include_sources`.
- `remember` takes `vault=` but gains none of the retrieval parameters; it has no retrieval.
- `seed_k`'s MCP-only `MAX_QUERY_K` clamp is a knowing divergence from REST rather than a
  unified bound; unifying it would change an existing REST contract.

## Rejected alternatives

- **Raising on `LLMProviderError` instead of stamping a status.** It would break the documented
  graceful-degradation contract that REST and the UI depend on, to fix a problem that only
  agents have.
- **Flagging an abnormal stop whenever `native_finish_reason != finish_reason`.** litellm's map
  contains clean-stop aliases, so this would flag every `end_turn` from Anthropic-shaped
  providers as abnormal.
- **A nested `policy` dict argument on `ask`.** Cheaper to add, but the tool schema stops being
  self-describing and the model loses per-field types and ranges.
- **Returning `VaultEntry.to_json()` from `list_vaults`.** It exists, it is already serialized,
  and it leaks `path` and the path-derived `id` to any agent that reaches the MCP port.
- **Letting the per-call `vault=` argument win over the connection's `?vault=`.** The connection
  selector is a human's deliberate hand edit; an agent-supplied string must not override it.
- **Accepting paths in the per-call argument for loopback callers.** `is_loopback` defaults to
  `True` when there is no HTTP context, so the gate that makes this safe for `?vault=` does not
  exist here.
- **Reusing `unknown_vault` for filesystem failures.** Its remediation (`init_vault`) is wrong
  for a vault that exists but cannot be opened.
- **litellm's `merge_reasoning_content_in_choices`.** Streaming-only, and it injects `<think>`
  tags into content.

## Relationship to ADR 0014, 0034, and 0035

ADR 0014 established `?vault=` per-connection selection and `resolve_vault_selector` as the
single resolution seam; this ADR amends it by adding a per-call argument that feeds the same
seam, with the connection retaining precedence. ADR 0034 remains the owner of the multi-vault
contract — immutable per-vault runtime context (§3), client-scoped browser selection (§4), pool
leasing and fencing (§5) — and is amended here only in that `_pool_error` now sanitises the
human text of `VaultPoolError` at the lease seam; the codes and the leasing semantics are
unchanged. ADR 0035 is the cautionary precedent: an agent- and user-facing parameter surface
(the `parameters` map, `MANAGED_LLM_PARAMETERS`, the Advanced Parameters dropdown) that was
added and then removed outright on 2026-09-15 with no migration. The 12 policy parameters added
here avoid its failure mode by being a projection of an existing validated model
(`AskRetrievalPolicy`) with `None`-means-inherit semantics, rather than a second parallel
store of tunables.
