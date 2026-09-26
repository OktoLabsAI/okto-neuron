"""``GrafxStore.get_nodes`` must batch its ``IN $ids`` list under Grafx's cap.

Grafx rejects a query list holding more than ``MAX_LIST_ELEMENTS`` (1024)
elements. ``query._vector_seeds`` deliberately passes EVERY embedded node id to
``get_nodes`` in one call — its untruncated scan is the documented recall floor
— so an unbatched implementation made ``ask`` fail outright on any vault
holding more than 1024 embedded nodes:

    okto_grafx.domain.errors.GrafxConfigurationError:
        A query list may hold at most 1024 elements.

Observed on a real 1,477-embedded-node vault against 0.0.50, where every
existing gate passed because the acceptance vaults all stay under the cap.
That is exactly why this test writes MORE than 1024 nodes rather than a token
handful: a fixture below the limit cannot catch the regression.

Skips cleanly when the ``[grafx]`` extra isn't installed, matching
``tests/store/test_grafx_store_dim.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_neuron.core.schema import Node  # noqa: E402
from okto_neuron.store.grafx import _ID_QUERY_BATCH, GrafxStore  # noqa: E402

# Comfortably over Grafx's 1024-element ceiling, and over the batch size, so the
# read spans at least three batches.
_NODE_COUNT = 2100


def test_batch_size_stays_under_the_grafx_query_list_cap() -> None:
    # The whole fix rests on this: a batch at or above the cap would still raise.
    assert _ID_QUERY_BATCH <= 1024


def test_get_nodes_reads_more_ids_than_the_grafx_query_list_cap(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    store = GrafxStore(vault_path, embedding_dim=4)
    try:
        expected = [
            Node(id=f"n{i:05d}", type="Concept", title=f"Node {i}", content="")
            for i in range(_NODE_COUNT)
        ]
        for node in expected:
            store.add_node(node)

        ids = [node.id for node in expected]
        assert len(ids) > 1024, "fixture must exceed the cap or it cannot regress"

        got = store.get_nodes(ids)

        # Every node comes back, exactly once, in the requested order.
        assert [node.id for node in got] == ids
    finally:
        store.close()


def test_get_nodes_preserves_order_and_dedups_across_batch_boundaries(
    tmp_path: Path,
) -> None:
    # Batching must not change the documented contract: caller order is kept and
    # duplicate ids collapse — including when duplicates straddle two batches.
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    store = GrafxStore(vault_path, embedding_dim=4)
    try:
        for i in range(_NODE_COUNT):
            store.add_node(
                Node(id=f"n{i:05d}", type="Concept", title=f"Node {i}", content="")
            )

        first = "n00000"
        straddling = f"n{_ID_QUERY_BATCH:05d}"  # first id of the second batch
        requested = [first, straddling, first, "n02099", straddling]

        got = store.get_nodes(requested)

        assert [node.id for node in got] == [first, straddling, "n02099"]
    finally:
        store.close()


def test_get_nodes_skips_unknown_ids_across_batches(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    store = GrafxStore(vault_path, embedding_dim=4)
    try:
        for i in range(_NODE_COUNT):
            store.add_node(
                Node(id=f"n{i:05d}", type="Concept", title=f"Node {i}", content="")
            )

        requested = [f"n{i:05d}" for i in range(_NODE_COUNT)] + ["absent-1", "absent-2"]

        got = store.get_nodes(requested)

        assert len(got) == _NODE_COUNT
        assert {node.id for node in got}.isdisjoint({"absent-1", "absent-2"})
    finally:
        store.close()
