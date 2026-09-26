"""Runtime boundary that keeps ordinary recall free of LLM completions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


class CompletionForbiddenError(RuntimeError):
    """A completion provider was invoked inside a completion-free operation."""


@dataclass
class CompletionProbe:
    operation: str
    attempted_calls: int = 0


_ACTIVE_PROBE: ContextVar[CompletionProbe | None] = ContextVar(
    "marginalia_completion_probe",
    default=None,
)


@contextmanager
def prohibit_completions(operation: str) -> Iterator[CompletionProbe]:
    """Fail any built-in completion attempt while ``operation`` is running."""

    probe = CompletionProbe(operation=operation)
    token = _ACTIVE_PROBE.set(probe)
    try:
        yield probe
    finally:
        _ACTIVE_PROBE.reset(token)


def assert_completion_allowed() -> None:
    """Record and reject a completion attempt under an active prohibition."""

    probe = _ACTIVE_PROBE.get()
    if probe is None:
        return
    probe.attempted_calls += 1
    raise CompletionForbiddenError(f"LLM completion is forbidden during {probe.operation}")


__all__ = [
    "CompletionForbiddenError",
    "CompletionProbe",
    "assert_completion_allowed",
    "prohibit_completions",
]
