"""Acceptance-suite conftest: laptop-gate guard for acceptance_private_corpus tests (FR5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _private_corpus_path() -> Path:
    override = os.environ.get("OKTO_NEURON_PRIVATE_CORPUS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".marginalia-private-corpus"


def _laptop_gate_skip_reason() -> str | None:
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return "laptop-gate: CI environment detected (CI or GITHUB_ACTIONS set)"
    if os.environ.get("OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS") != "1":
        return "laptop-gate: OKTO_NEURON_ACCEPTANCE_ALLOW_PRIVATE_CORPUS != '1'"
    corpus_path = _private_corpus_path()
    if not corpus_path.is_dir():
        return f"laptop-gate: private corpus dir not present at {corpus_path}"
    return None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "acceptance_synthetic: MVP acceptance gate run against synthetic vault fixture (CI-safe)",
    )
    config.addinivalue_line(
        "markers",
        "acceptance_private_corpus: opt-in acceptance gate for an external private corpus "
        "(laptop-only, never CI)",
    )


def pytest_collection_modifyitems(config, items):
    reason = _laptop_gate_skip_reason()
    if reason is None:
        return

    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if "acceptance_private_corpus" in item.keywords:
            item.add_marker(skip_marker)
