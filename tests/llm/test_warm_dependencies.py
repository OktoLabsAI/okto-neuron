"""``warm_provider_dependencies`` — daemon-boot eager import of LLM deps.

Guards the 2026-07-07 failure mode: a running daemon whose shared ``.venv``
loses ``litellm`` to an exact ``uv run`` re-sync 500'd every ask with a
per-request ``ModuleNotFoundError`` (a full 142-question A/B recorded 284/284
empty answers). The warmup imports at boot — pinned in ``sys.modules`` — and
converts a missing dependency into ONE typed, loud :class:`LLMProviderError`.
"""

import sys

import pytest

import okto_neuron.llm as llm_mod
from okto_neuron.llm import LLMProviderError, warm_provider_dependencies


def test_stub_claude_cli_and_pi_cli_are_noops() -> None:
    # None of these providers have optional imports; warmup must not raise or import.
    warm_provider_dependencies("stub")
    warm_provider_dependencies("claude_cli")
    warm_provider_dependencies("pi_cli")


def test_missing_litellm_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ``None`` sys.modules entry makes ``import litellm`` fail regardless of
    # whether the package is installed — deterministic in every environment.
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(LLMProviderError, match="litellm"):
        warm_provider_dependencies("openai")


def test_missing_provider_optional_dependency_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider-specific optional deps (the bedrock/boto3 pattern) go through the
    # same preflight; a fake unimportable module must surface the typed error.
    monkeypatch.setitem(
        llm_mod._PROVIDER_OPTIONAL_DEPENDENCIES, "bedrock", ("definitely_not_a_module_xyz",)
    )
    with pytest.raises(LLMProviderError, match="definitely_not_a_module_xyz"):
        warm_provider_dependencies("bedrock")


def test_warm_succeeds_when_dependencies_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With litellm resolvable (stubbed via sys.modules — no real install needed),
    # warmup completes silently for a litellm-backed provider.
    import types

    monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
    warm_provider_dependencies("openai")
