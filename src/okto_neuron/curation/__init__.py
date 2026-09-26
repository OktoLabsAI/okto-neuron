"""Curation domain: the one shared graph-swap tail, consolidated (M2b).

``rebuild``/``heal``/``reembed`` each stage a fresh graph via their own
build head, then hand off to :func:`orchestrate.finish_staged_swap` for the
identical sequence every offline/online owner used to hand-copy: pre-swap
audit (when the verb has one) -> require the caller's exclusivity lock still
held -> commit the staged graph onto live -> (unless the verb opts out) fence
the new generation, audit it post-swap, and publish the verdict.

This package intentionally never imports ``okto_neuron.cli`` or
``okto_neuron.server`` — see :mod:`okto_neuron.curation.orchestrate`'s module
docstring for why, and for exactly which callables each caller must supply.
"""

from __future__ import annotations

from okto_neuron.curation.orchestrate import (
    StagedSwapResult,
    finish_staged_swap,
    heal,
    rebuild,
    reembed,
)

__all__ = [
    "StagedSwapResult",
    "finish_staged_swap",
    "heal",
    "reembed",
    "rebuild",
]
