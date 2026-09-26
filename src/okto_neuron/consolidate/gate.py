"""Pure confidence policy between resolution and the sealed write plan.

Phase E of ``docs/autonomous-architecture-plan.md``. Given a candidate and the
:class:`~okto_neuron.resolve.ResolveOutcome` the graph talked back with, the gate
decides one of two fates per candidate:

- **auto-commit** — confidence at/above the threshold and no contradiction;
- **review** — any contradiction (``contradiction``), or confidence below the
  threshold (``low_confidence``).

This module decides policy only. The companion turns that verdict into a sealed,
receipted plan before any graph or queue mutation.

Thresholds live in config (``consolidation`` block); a sensible default ships
here as :class:`GateConfig` so callers can run the gate without wiring full
config.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_neuron.resolve import ResolveOutcome

# ── tunables ──────────────────────────────────────────────────────────────────
AUTO_COMMIT_THRESHOLD = 0.75
"""Default minimum confidence for a candidate to auto-commit (config:
``consolidation.auto_commit_threshold``)."""

REVIEW_ON_CONTRADICTION = True
"""Default policy: a contradicted candidate always routes to review, regardless
of confidence (config: ``consolidation.review_on_contradiction``)."""


@dataclass(frozen=True)
class GateConfig:
    """Confidence-gate policy. Mirrors the ``consolidation`` config block."""

    auto_commit_threshold: float = AUTO_COMMIT_THRESHOLD
    review_on_contradiction: bool = REVIEW_ON_CONTRADICTION


def decide(
    outcome: ResolveOutcome,
    config: GateConfig | None = None,
) -> tuple[bool, str | None]:
    """Pure verdict for one candidate: ``(commit, review_reason)``.

    Contradiction takes precedence over confidence — a contradicted candidate
    routes to review even if its confidence is high.
    """
    config = config or GateConfig()
    if outcome.contradicted and config.review_on_contradiction:
        return (False, "contradiction")
    if outcome.confidence >= config.auto_commit_threshold:
        return (True, None)
    return (False, "low_confidence")


__all__ = [
    "AUTO_COMMIT_THRESHOLD",
    "REVIEW_ON_CONTRADICTION",
    "GateConfig",
    "decide",
]
