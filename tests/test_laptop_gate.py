"""Tests for FR5 three-layer laptop-gate guard."""

from __future__ import annotations

import pytest

from tests.acceptance.conftest import _private_corpus_path, _laptop_gate_skip_reason


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "CI",
        "GITHUB_ACTIONS",
        "OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS",
        "OKTO_NEURON_PRIVATE_CORPUS",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_ci_env_blocks(clean_env):
    clean_env.setenv("CI", "true")
    assert (
        _laptop_gate_skip_reason()
        == "laptop-gate: CI environment detected (CI or GITHUB_ACTIONS set)"
    )


def test_github_actions_blocks(clean_env):
    clean_env.setenv("GITHUB_ACTIONS", "true")
    assert (
        _laptop_gate_skip_reason()
        == "laptop-gate: CI environment detected (CI or GITHUB_ACTIONS set)"
    )


def test_missing_allow_env_blocks(clean_env):
    assert (
        _laptop_gate_skip_reason()
        == "laptop-gate: OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS != '1'"
    )


def test_missing_corpus_blocks(clean_env, tmp_path):
    clean_env.setenv("OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS", "1")
    clean_env.setenv("OKTO_NEURON_PRIVATE_CORPUS", str(tmp_path / "nonexistent"))
    reason = _laptop_gate_skip_reason()
    assert reason is not None
    assert "private corpus dir not present" in reason


def test_corpus_override_allows_existing_private_corpus(clean_env, tmp_path):
    clean_env.setenv("OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS", "1")
    clean_env.setenv("OKTO_NEURON_PRIVATE_CORPUS", str(tmp_path))
    assert _private_corpus_path() == tmp_path
    assert _laptop_gate_skip_reason() is None


def test_default_corpus_is_derived_from_home(clean_env, tmp_path):
    clean_env.setenv("HOME", str(tmp_path))
    assert _private_corpus_path() == tmp_path / ".marginalia-private-corpus"
