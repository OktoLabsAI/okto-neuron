"""Backend effective-capacity policy — one owner for LLM in-flight concurrency.

Okto Neuron orchestrates two independent bounded fan-outs that put completion
requests in flight concurrently:

* **extraction** — ``llm.extraction.max_concurrent`` (ADR 0036), bounding how
  many chunks have an extractor call outstanding.
* **curation** — ``consolidation.curation_max_concurrent`` (ADR 0015 D1),
  bounding the candidate-curation fan-out.

Both are *Okto Neuron orchestration policy*, never model parameters, and both
are only safe above one when the backend serving that model actually has more
than one generation slot. The LiteLLM Gateway does not advertise this: the
``/model_group/info`` adapter in :mod:`okto_neuron.providers` exposes ``mode``
and ``supported_openai_params`` only, with no verified parallel-slot field. So
capacity is **declared configuration**, not inference, and it is declared once
here rather than scattered as alias comparisons through the companion.

Policy
------
An alias may exceed one in-flight request only if it appears in
``llm.parallel_capable_models``. Everything else clamps to 1. The clamp is
applied where the value is **consumed**, not at parse time: the stored config
keeps exactly what the user wrote so the Config UI can show configured vs
effective, an existing vault YAML carrying ``4`` stays loadable, and the
companion's mid-run config re-read cannot slip past the policy.

Multi-step fail-closed rule
---------------------------
Curation's fan-out drives the ``curator`` **and** ``relation_curator`` steps,
which may resolve to different models. Effective curation concurrency exceeds
one only when *every* step feeding the fan-out is parallel-capable — the
minimum across steps, not the maximum. Extraction resolves from the single
``extraction`` step.

Embedding is deliberately out of scope. ``embedding.batch_size`` /
``embedding.max_concurrent_batches`` are governed by a separate standing
decision (batch 32, 1 concurrent batch) and are not routed through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from okto_neuron.config._vault import VaultConfig

# The single default allowlist. This is a *default for configuration*, not a
# hardcoded policy: `llm.parallel_capable_models` overrides it per vault. As of
# the ADR 0040 live-run capacity decision this is the only approved alias with
# more than one generation slot (llama.cpp `--parallel 10` behind the alias).
DEFAULT_PARALLEL_CAPABLE_MODELS: tuple[str, ...] = ("desktop/qwen3.6-35b-10-parallel",)

# Steps whose models gate each fan-out. Curation is the pair, per the
# fail-closed multi-step rule above.
EXTRACTION_STEPS: tuple[str, ...] = ("extraction",)
CURATION_STEPS: tuple[str, ...] = ("curator", "relation_curator")

# ADR 0015 D4 batching, relation curation only: the largest number of relation
# candidates judged in ONE call, whatever ``consolidation.curation_batch_size``
# says. Measured 2026-09-22 against chatgpt/gpt-5.6-luna, reasoning off, by
# replaying real LoCoMo curator prompts rebuilt byte-exact with the product's
# own batch builders (docs/remote-providers.md, "Batched curation changes the
# verdicts"). Verdicts inside one relation batch move together: a 25-edge,
# single-block document at batch size 32 went out as ONE call, came back 0/25
# committed, and the relationship liveness gate then queued all 12 of its
# nodes. Across three documents the per-document commit rate swung by +/-5 to
# +/-10 points at batch 4 and by +/-11 to +/-45 at 8, 12, 16 and a full block,
# while agreement with single calls was flat (61-65%) at every size. So the
# cap buys stability, not fidelity: 4 is the only size measured without
# whole-document flips. Node curation showed no size-dependent cliff within
# the sizes LoCoMo produces (up to 12), so it is not capped.
RELATION_BATCH_MAX = 4


def effective_curation_batch_size(configured: object, *, relation: bool) -> int:
    """Candidates per batched curation call that are actually sent.

    ``configured`` is ``consolidation.curation_batch_size`` as written. Node
    curation uses it unchanged; relation curation is capped at
    :data:`RELATION_BATCH_MAX`. Like the concurrency clamp above, this applies
    where the value is consumed, so the stored config keeps what the user wrote
    and the Config UI can show configured vs effective.
    """
    try:
        size = int(configured) if configured is not None else 1  # type: ignore[call-overload]
    except (TypeError, ValueError):
        size = 1
    size = max(1, size)
    return min(size, RELATION_BATCH_MAX) if relation else size


def curation_batch_notice(configured: object) -> str | None:
    """A user-facing reason the relation batch size was clamped, or ``None``."""
    try:
        requested = max(1, int(configured)) if configured is not None else 1  # type: ignore[call-overload]
    except (TypeError, ValueError):
        requested = 1
    effective = effective_curation_batch_size(requested, relation=True)
    if requested <= effective:
        return None
    return (
        f"configured curation_batch_size {requested} applies to node curation; "
        f"relation curation is capped at {effective} per call (RELATION_BATCH_MAX), "
        "because larger relation batches were measured to flip whole documents "
        "to queue"
    )


def _normalize(alias: object) -> str:
    """Case- and whitespace-insensitive alias key.

    Aliases come from hand-written YAML, so ``Desktop/Qwen3.6-35B-10-Parallel``
    and a stray trailing space must not silently fall out of the allowlist and
    produce a *stricter* clamp than intended, nor a looser one.
    """
    return str(alias or "").strip().lower()


def is_parallel_capable(model: object, allowlist: Iterable[str]) -> bool:
    """Whether ``model`` is declared to serve more than one request at a time."""
    key = _normalize(model)
    if not key:
        return False
    return key in {_normalize(entry) for entry in allowlist}


def step_model(config: "VaultConfig", step: str) -> str:
    """The alias a step will actually call.

    Resolved WITHOUT :meth:`LLMConfig.resolved` and without touching the
    provider registry: ``provider_ref`` resolution only rewrites ``provider`` /
    ``api_base`` / ``api_key_env`` / ``allow_remote``, never ``model``. Keeping
    registry I/O out of this path means capacity can be computed cheaply on
    every consumption, including inside the companion's hot re-read loop.
    """
    step_config = getattr(config.llm, step, None)
    override = getattr(step_config, "model", None)
    if isinstance(override, str) and override.strip():
        return override
    return config.llm.defaults.model


def effective_max_concurrent(
    configured: int | None,
    *,
    models: Sequence[object],
    allowlist: Iterable[str],
) -> int:
    """Clamp a configured in-flight bound to what the backend can actually serve.

    Returns 1 unless every alias in ``models`` is parallel-capable. A configured
    value below one (or ``None``) also normalizes to 1, matching the historical
    ``... or 1`` read sites.
    """
    floor = 1
    try:
        requested = int(configured) if configured is not None else floor
    except (TypeError, ValueError):
        return floor
    if requested <= floor:
        return floor
    if not models:
        return floor
    if all(is_parallel_capable(model, allowlist) for model in models):
        return requested
    return floor


def _effective_for(config: "VaultConfig", configured: int | None, steps: Sequence[str]) -> int:
    return effective_max_concurrent(
        configured,
        models=[step_model(config, step) for step in steps],
        allowlist=config.llm.parallel_capable_models,
    )


def extraction_effective_max_concurrent(config: "VaultConfig") -> int:
    """Effective in-flight extractor calls for this vault."""
    return _effective_for(config, config.llm.extraction.max_concurrent, EXTRACTION_STEPS)


def curation_effective_max_concurrent(config: "VaultConfig") -> int:
    """Effective in-flight curation calls for this vault."""
    return _effective_for(config, config.consolidation.curation_max_concurrent, CURATION_STEPS)


def capacity_notice(
    *,
    configured: int | None,
    effective: int,
    models: Sequence[object],
    allowlist: Iterable[str],
) -> str | None:
    """A clear, user-facing reason a configured bound was clamped.

    ``None`` when nothing was clamped, so callers can treat presence as "there
    is something to tell the user".

    Only the models that actually FAILED the allowlist are named. Curation gates
    on the curator/relation_curator pair, so a mixed setup (one parallel-capable
    step, one not) must blame the serial step alone — naming the capable alias
    as a cause would send the operator to fix the wrong line of config.
    """
    try:
        requested = int(configured) if configured is not None else 1
    except (TypeError, ValueError):
        requested = 1
    if requested <= effective:
        return None
    allowed = list(allowlist)
    offenders = sorted(
        {
            str(model)
            for model in models
            if str(model or "").strip() and not is_parallel_capable(model, allowed)
        }
    )
    return (
        f"configured concurrency {requested} clamped to {effective}: "
        f"{', '.join(offenders) or 'the configured model'} is not listed in "
        "llm.parallel_capable_models, so the backend is assumed to serve one "
        "request at a time"
    )


def capacity_report(config: "VaultConfig") -> dict[str, object]:
    """Configured-vs-effective summary for the config API and the Config UI."""
    extraction_models = [step_model(config, step) for step in EXTRACTION_STEPS]
    curation_models = [step_model(config, step) for step in CURATION_STEPS]
    extraction_configured = config.llm.extraction.max_concurrent
    curation_configured = config.consolidation.curation_max_concurrent
    extraction_effective = extraction_effective_max_concurrent(config)
    curation_effective = curation_effective_max_concurrent(config)
    return {
        "parallel_capable_models": list(config.llm.parallel_capable_models),
        "extraction": {
            "configured": extraction_configured,
            "effective": extraction_effective,
            "models": extraction_models,
            "notice": capacity_notice(
                configured=extraction_configured,
                effective=extraction_effective,
                models=extraction_models,
                allowlist=config.llm.parallel_capable_models,
            ),
        },
        "curation": {
            "configured": curation_configured,
            "effective": curation_effective,
            "models": curation_models,
            "notice": capacity_notice(
                configured=curation_configured,
                effective=curation_effective,
                models=curation_models,
                allowlist=config.llm.parallel_capable_models,
            ),
        },
        "curation_batch": {
            "configured": config.consolidation.curation_batch_size,
            "node_effective": effective_curation_batch_size(
                config.consolidation.curation_batch_size, relation=False
            ),
            "relation_effective": effective_curation_batch_size(
                config.consolidation.curation_batch_size, relation=True
            ),
            "notice": curation_batch_notice(config.consolidation.curation_batch_size),
        },
    }
