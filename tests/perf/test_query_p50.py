from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.core.schema import Node
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


@pytest.mark.slow
@pytest.mark.perf
def test_query_p50_under_100ms_on_10k_node_fixture(tmp_path: Path) -> None:
    if os.environ.get("OKTO_NEURON_RUN_PERF") != "1":
        pytest.skip("set OKTO_NEURON_RUN_PERF=1 to run the 10K-node perf gate")

    vault = Vault.init(tmp_path / "v")
    try:
        for index in range(10_000):
            vault.store.add_node(
                Node(
                    id=f"perf-{index:05d}",
                    type="Claim",
                    title=f"Perf node {index}",
                    content="test retrieval latency stable corpus",
                )
            )

        vault.query("test", k=5)
        durations_ms: list[float] = []
        for _ in range(50):
            start = time.perf_counter()
            vault.query("test", k=5)
            durations_ms.append((time.perf_counter() - start) * 1000)

        assert statistics.median(durations_ms) < 100
    finally:
        vault.close()
