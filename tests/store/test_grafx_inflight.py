"""GrafxStore counts in-flight grafx calls and never closes under one (#22)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_neuron.core.schema import Node  # noqa: E402
from okto_neuron.errors import GraphBackendError  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402


class _BlockingDb:
    def __init__(self, real: object) -> None:
        self._real = real
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, *args: object, **kwargs: object):  # noqa: ANN201
        self.entered.set()
        assert self.release.wait(30)
        return self._real.execute(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_counter_tracks_calls_and_close_waits_for_them(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store = GrafxStore(vault, embedding_dim=4)
    store.add_node(Node(id="n1", type="Concept", title="One", content=""))
    assert store.calls_in_flight == 0

    blocking = _BlockingDb(store._db)
    store._db = blocking  # type: ignore[assignment]
    reader = threading.Thread(target=lambda: store.get_node("n1"))
    reader.start()
    assert blocking.entered.wait(10)
    assert store.calls_in_flight == 1

    closer = threading.Thread(target=store.close)
    closer.start()
    closer.join(timeout=0.5)
    assert closer.is_alive(), "close must wait for the in-flight call"
    with pytest.raises(GraphBackendError):
        store._query("MATCH (n:Node) RETURN n.id AS id")  # refused once closing

    blocking.release.set()
    reader.join(timeout=10)
    closer.join(timeout=10)
    assert not closer.is_alive()
    assert store.calls_in_flight == 0
    assert store.is_closed
