"""Reads retry a retryable grafx error inside the store adapter (issue #26).

A foreign commit between grafx's view snapshot and its exact read raises a
``GrafxIndexError`` flagged retryable (``index_view_changed``). The adapter must
absorb it with a small bounded backoff; a non-retryable error must surface at
once, and a persistent retryable error must exhaust a bounded budget and reach
the caller typed as retryable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_grafx.domain import errors as grafx_errors  # noqa: E402

from okto_neuron.core.schema import Node  # noqa: E402
from okto_neuron.errors import GraphBackendError  # noqa: E402
from okto_neuron.store import grafx as grafx_module  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402


def _view_changed() -> grafx_errors.GrafxIndexError:
    return grafx_errors.GrafxIndexError("view changed", retryable=True)


class _FlakyDb:
    """Delegates to the real database, failing the first ``failures`` executes."""

    def __init__(self, real: object, failures: int, error: Exception) -> None:
        self._real = real
        self.remaining = failures
        self.calls = 0
        self._error = error

    def execute(self, *args: object, **kwargs: object):  # noqa: ANN201
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self._error
        return self._real.execute(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    monkeypatch.setattr(grafx_module, "_read_retry_sleep", sleeps.append)
    vault = tmp_path / "vault"
    vault.mkdir()
    opened = GrafxStore(vault, embedding_dim=4)
    opened.add_node(Node(id="n1", type="Concept", title="One", content=""))
    opened.sleeps = sleeps  # type: ignore[attr-defined]
    try:
        yield opened
    finally:
        opened.close()


def _inject(store: GrafxStore, failures: int, error: Exception) -> _FlakyDb:
    flaky = _FlakyDb(store._db, failures, error)
    store._db = flaky  # type: ignore[assignment]
    return flaky


@pytest.mark.parametrize("read", ["get_node", "get_nodes", "list_nodes"])
def test_reads_survive_transient_retryable_errors(store: GrafxStore, read: str) -> None:
    flaky = _inject(store, 3, _view_changed())
    if read == "get_node":
        assert store.get_node("n1") is not None
    elif read == "get_nodes":
        assert [n.id for n in store.get_nodes(["n1"])] == ["n1"]
    else:
        assert [n.id for n in store.list_nodes()] == ["n1"]
    assert flaky.calls == 4  # three failures, then the read that landed
    sleeps = store.sleeps  # type: ignore[attr-defined]
    assert len(sleeps) == 3
    assert all(0 <= s <= grafx_module._READ_RETRY_POLICY.backoff_cap_ms / 1000 for s in sleeps)


def test_non_retryable_error_surfaces_immediately(store: GrafxStore) -> None:
    flaky = _inject(store, 5, grafx_errors.GrafxIndexError("broken index"))
    with pytest.raises(GraphBackendError) as excinfo:
        store.get_node("n1")
    assert excinfo.value.retryable is False
    assert flaky.calls == 1
    assert store.sleeps == []  # type: ignore[attr-defined]


def test_persistent_retryable_error_is_bounded_and_typed_retryable(store: GrafxStore) -> None:
    flaky = _inject(store, 10_000, _view_changed())
    with pytest.raises(GraphBackendError) as excinfo:
        store.get_node("n1")
    assert excinfo.value.retryable is True
    policy = grafx_module._READ_RETRY_POLICY
    assert flaky.calls == policy.max_attempts
    assert len(store.sleeps) == policy.max_attempts - 1  # type: ignore[attr-defined]
