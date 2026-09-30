"""``shutdown.phase`` logging and the store-close -> lease-release order (#22)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from okto_neuron.server.lifecycle import set_shutdown_hard_deadline, shutdown_phase
from okto_neuron.server.state import ServerState
from okto_neuron.vault import Vault

_PHASE = re.compile(
    r"shutdown\.phase name=(?P<name>\w+) duration_ms=(?P<ms>\d+) "
    r"remaining_s=(?P<rem>-?[\d.]+) status=(?P<status>\w+)(?P<rest>.*)"
)


def _phases(caplog) -> list[re.Match[str]]:
    return [m for r in caplog.records if (m := _PHASE.search(r.getMessage()))]


def test_shutdown_phase_logs_name_duration_remaining_and_error(caplog) -> None:
    set_shutdown_hard_deadline(None)
    with caplog.at_level(logging.INFO, logger="okto_neuron.server.shutdown"):
        with shutdown_phase("demo", vault="scratch") as detail:
            detail["extra"] = 3
        try:
            with shutdown_phase("boom"):
                raise RuntimeError("x")
        except RuntimeError:
            pass
    demo, boom = _phases(caplog)
    assert demo["name"] == "demo" and demo["status"] == "ok"
    assert "vault=scratch" in demo["rest"] and "extra=3" in demo["rest"]
    assert boom["name"] == "boom" and boom["status"] == "error"


def test_state_close_logs_each_vault_then_releases_leases_in_order(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    from okto_neuron.server import state as state_module

    path = tmp_path / "scratch-vault"
    Vault.init(path, packs=["core"]).close()
    path = path.resolve(strict=False)
    state = ServerState(vault=None, vault_path=None)
    state.vault_pool.get_or_open(path)

    order: list[str] = []
    real_close = state.vault_pool._close
    monkeypatch.setattr(
        state.vault_pool,
        "_close",
        lambda vault: (order.append("store_close"), real_close(vault))[1],
    )
    real_release = state_module.release_all_writer_leases
    monkeypatch.setattr(
        state_module,
        "release_all_writer_leases",
        lambda: (order.append("writer_lease_release"), real_release())[1],
    )
    set_shutdown_hard_deadline(None)
    with caplog.at_level(logging.INFO, logger="okto_neuron.server.shutdown"):
        state.close()

    assert order == ["store_close", "writer_lease_release"]
    names = [m["name"] for m in _phases(caplog)]
    assert names == [
        "vault_close",
        "handle_lease_release",
        "store_close",
        "writer_lease_release",
    ]
    vault_line = next(m for m in _phases(caplog) if m["name"] == "vault_close")
    assert "vault=scratch-vault" in vault_line["rest"]
