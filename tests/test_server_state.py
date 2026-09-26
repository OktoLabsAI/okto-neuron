"""Unit tests for the shared server-state module.

Covers the process-wide ``asyncio.Lock`` that the REST and MCP write
paths share to serialize all vault-mutating operations against the
single Ladybug vault handle. Per spec 9030718e business rule
``br_2f1a804f`` the lock is exercised against a *real* :class:`Vault`
instance (no mocks).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.server import state as server_state


@pytest.fixture(autouse=True)
def _reset_lock_singleton() -> None:
    """Each test gets a fresh lock — production callers must not rely on this."""
    server_state.reset_vault_write_lock()
    yield
    server_state.reset_vault_write_lock()


def test_get_vault_write_lock_returns_singleton() -> None:
    """REST and MCP must observe the same lock instance.

    The pivot's correctness hinges on this: if REST :7777 and MCP :8201
    each held their own lock, concurrent writes would race the single
    Ladybug handle and the global lock would not prevent WAL corruption.
    """
    first = server_state.get_vault_write_lock()
    second = server_state.get_vault_write_lock()
    assert first is second
    assert isinstance(first, asyncio.Lock)


def test_reset_creates_new_instance() -> None:
    """``reset_vault_write_lock`` is the only sanctioned way to drop the lock."""
    first = server_state.get_vault_write_lock()
    server_state.reset_vault_write_lock()
    second = server_state.get_vault_write_lock()
    assert first is not second


def test_server_state_without_vault_uses_compatibility_lock() -> None:
    state = server_state.ServerState(vault=None, vault_path=None)

    assert state.writer_lock is server_state.get_vault_write_lock()


@pytest.mark.asyncio
async def test_lock_is_mutually_exclusive() -> None:
    """A second ``async with`` blocks until the first releases."""
    lock = server_state.get_vault_write_lock()
    order: list[str] = []

    async def writer(name: str, hold: float) -> None:
        async with lock:
            order.append(f"{name}:acquired")
            await asyncio.sleep(hold)
            order.append(f"{name}:released")

    await asyncio.gather(writer("A", 0.05), writer("B", 0.0))

    # Interleaving is impossible: B cannot acquire before A releases.
    assert order in (
        ["A:acquired", "A:released", "B:acquired", "B:released"],
        ["B:acquired", "B:released", "A:acquired", "A:released"],
    )


@pytest.mark.asyncio
async def test_lock_serializes_real_vault_writes(tmp_path: Path) -> None:
    """Concurrent writers through the lock all succeed against a real Vault.

    This is the unit-test analogue of test scenario ``ts_eb40a76c``
    (N=8 concurrent kg add). It does NOT replace the acceptance
    scenario — it only proves the lock primitive itself serializes
    correctly when wired against a real Ladybug-backed Vault. The
    end-to-end HTTP scenario is owned by the REST card.
    """
    vault = Vault.init(
        tmp_path / "v",
        packs=["core", "research", "personal"],
        embedding_provider="stub",
    )
    try:
        lock = server_state.get_vault_write_lock()
        notes: list[Path] = []
        for i in range(8):
            note = tmp_path / f"n{i}.md"
            note.write_text(
                f"---\ntitle: Note {i}\ntags: [concurrent]\n---\n"
                f"# Note {i}\n\nContent body number {i} about provenance.\n",
                encoding="utf-8",
            )
            notes.append(note)

        in_critical_section = 0
        max_in_critical_section = 0

        async def add_one(path: Path) -> object:
            nonlocal in_critical_section, max_in_critical_section
            async with lock:
                in_critical_section += 1
                max_in_critical_section = max(max_in_critical_section, in_critical_section)
                # Vault.add is sync — run in default executor so we yield
                # control and would let a second writer in IF the lock
                # were not actually exclusive.
                loop = asyncio.get_running_loop()
                try:
                    return await loop.run_in_executor(None, vault.add, path)
                finally:
                    in_critical_section -= 1

        results = await asyncio.gather(*(add_one(p) for p in notes))

        assert len(results) == 8
        assert all(r is not None for r in results)
        assert max_in_critical_section == 1, (
            "Lock failed to serialize writers: "
            f"saw {max_in_critical_section} concurrent vault.add calls"
        )

        hits = vault.query("provenance")
        assert len(hits) >= 1
    finally:
        vault.close()


def test_switch_vault_keeps_old_handle_queryable(tmp_path: Path) -> None:
    """ADR 0014: switch_vault must NOT close the old vault.

    Because ``Vault.close()`` is path-wide and an MCP connection may still be
    using the old handle, switching the active vault leaves the old handle pooled
    and live; it is closed only at shutdown via ``vault_pool.close_all()``.
    """
    old = Vault.init(tmp_path / "old", packs=["core"])
    new = Vault.init(tmp_path / "new", packs=["core"])

    st = server_state.ServerState(vault=old, vault_path=(tmp_path / "old").resolve())
    st.vault_pool.adopt(old, st.vault_path)
    try:
        st.switch_vault(new, tmp_path / "new")
        # Active is now the new vault.
        assert st.vault is new
        # The OLD handle is still open and queryable (connection not closed).
        assert getattr(old, "_closed", False) is False
        assert old.query("anything", k=1) == []
    finally:
        st.close()
        assert getattr(old, "_closed", False) is True
        assert getattr(new, "_closed", False) is True
