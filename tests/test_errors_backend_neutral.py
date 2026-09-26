"""Backend-neutral REST error vocabulary (M3 spec section 2.6, section 7 D-41).

Covers the additive ``GRAPH_LOCK_HELD_MESSAGE``/``GRAPH_WRITE_FAILED_CODE``
constants in ``errors.py`` and the shared ``_graph_write_failed`` REST helper
in ``server/http.py``. Both sit *alongside* the existing Ladybug-specific
``VaultLockHeld``/``VaultCorrupted`` exception classes and the legacy
``"ladybug_write_failed"`` wire code — this suite is a regression guard that
the additive vocabulary never mutates or replaces either, matching D-41's
"additive, not a replacement" design and its own reversal note (route
non-Ladybug failures through it once a second backend ships).

Companion to ``tests/test_errors.py`` (the general error-taxonomy suite);
kept separate per the M3 spec's own test-plan naming (section 4).
"""

from __future__ import annotations

import json

from okto_neuron import errors
from okto_neuron.server import http as http_mod


def test_graph_backend_neutral_constants_have_exact_strings() -> None:
    assert errors.GRAPH_LOCK_HELD_MESSAGE == "graph store lock is held"
    assert errors.GRAPH_WRITE_FAILED_CODE == "graph_write_failed"


def test_graph_backend_neutral_constants_are_importable_and_exported() -> None:
    # Importable directly off the module (not just as attributes) ...
    from okto_neuron.errors import GRAPH_LOCK_HELD_MESSAGE, GRAPH_WRITE_FAILED_CODE

    assert GRAPH_LOCK_HELD_MESSAGE == errors.GRAPH_LOCK_HELD_MESSAGE
    assert GRAPH_WRITE_FAILED_CODE == errors.GRAPH_WRITE_FAILED_CODE
    # ... and declared in __all__, matching every other public error symbol.
    assert "GRAPH_LOCK_HELD_MESSAGE" in errors.__all__
    assert "GRAPH_WRITE_FAILED_CODE" in errors.__all__


def test_vault_lock_held_default_message_unchanged() -> None:
    assert errors.VaultLockHeld.default_message == "ladybug file lock is held"
    assert errors.VaultLockHeld("/tmp/vault").message == "ladybug file lock is held"


def test_vault_corrupted_default_message_unchanged() -> None:
    """Sibling exception cited alongside VaultLockHeld in D-41 (errors.py:177,248)
    — same "unchanged" guarantee applies to it."""
    assert errors.VaultCorrupted.default_message == "ladybug graph is corrupted"


def test_graph_write_failed_helper_emits_legacy_and_new_codes() -> None:
    response = http_mod._graph_write_failed(RuntimeError("disk full"))

    assert response.status_code == 500
    body = json.loads(response.body)
    # "error" stays the legacy string every existing caller/test matches on.
    assert body["error"] == "ladybug_write_failed"
    assert body["detail"] == "ladybug write failed: disk full"
    # "codes" is the new, purely additive field: legacy code first, then the
    # backend-neutral constant — verbatim, not just "contains".
    assert body["codes"] == ["ladybug_write_failed", errors.GRAPH_WRITE_FAILED_CODE]
    assert body["codes"] == ["ladybug_write_failed", "graph_write_failed"]


def test_graph_write_failed_helper_logs_every_failure_with_its_cause(caplog) -> None:
    """Each call site returns this helper without logging first, so the helper
    is where a failed write reaches the operator log. A wedged vault fails
    every later ingest through it; each one must log at error level with the
    underlying cause, not only the first failure."""
    cause = ValueError("node artifact differs from sealed plan")
    exc = errors.IngestError(
        "/vault/session-20.md",
        vault_path="/vault",
        message="a different sealed semantic plan must be resumed before new ingest work",
        cause=cause,
    )
    with caplog.at_level("ERROR", logger="okto_neuron.server.http"):
        http_mod._graph_write_failed(exc)
        http_mod._graph_write_failed(exc)

    records = [r for r in caplog.records if r.name == "okto_neuron.server.http"]
    assert [r.levelname for r in records] == ["ERROR", "ERROR"]
    message = records[0].getMessage()
    assert "a different sealed semantic plan must be resumed" in message
    assert "cause: node artifact differs from sealed plan" in message


def test_backend_write_exhausted_types_share_the_neutral_base() -> None:
    """``server/http.py`` recognizes an exhausted-retry graph write through the
    shared base without importing an optional backend driver."""
    import pytest

    from okto_neuron.store.grafx import GrafxWriteExhausted

    assert issubclass(GrafxWriteExhausted, errors.GraphWriteExhausted)
    assert "GraphWriteExhausted" in errors.__all__
    pytest.importorskip("neo4j")
    from okto_neuron.store.neo4j import Neo4jWriteExhausted

    assert issubclass(Neo4jWriteExhausted, errors.GraphWriteExhausted)
