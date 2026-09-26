"""Tests for the ambient background runner (Phase G).

Drives :func:`process_once` over a real vault inbox with a :class:`StubLLM`-backed
:class:`Companion`. StubLLM yields zero candidates, so we assert the durable file
lifecycle (moved out of ``incoming/`` into ``processed/``, idempotent re-drain)
rather than commit/queue counts. NO network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion, LLMUnavailableError
from okto_neuron.errors import OktoNeuronError
from okto_neuron.llm import StubLLM
from okto_neuron.runner import FAILED_DIRNAME, Runner, inbox_dir, process_once
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


class _RaisingCompanion:
    """Companion stand-in whose remember() always fails — drives the poison-file
    path. Tracks call count to prove no reprocessing."""

    def __init__(self) -> None:
        self.calls = 0

    def remember(self, source: object, **_kw: object) -> object:
        self.calls += 1
        raise OktoNeuronError("simulated remember failure")


class _LLMUnavailableCompanion:
    """Companion stand-in whose remember() always raises LLMUnavailableError —
    drives the Defect C retryable-outage path (distinct from the generic
    poison-file path above)."""

    def __init__(self) -> None:
        self.calls = 0

    def remember(self, source: object, **_kw: object) -> object:
        self.calls += 1
        raise LLMUnavailableError("simulated total LLM outage")


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _companion(vault: Vault) -> Companion:
    return Companion(vault, provider=StubLLM())


def _drop(vault: Vault, name: str = "note.md", text: str = "# Note\n\nbody.\n") -> Path:
    incoming = inbox_dir(Path(vault.path))
    incoming.mkdir(parents=True, exist_ok=True)
    target = incoming / name
    target.write_text(text, encoding="utf-8")
    return target


def test_process_once_remembers_and_marks_done(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        dropped = _drop(vault)
        results = process_once(Path(vault.path), _companion(vault))

        assert len(results) == 1
        # remembered: a document id is always populated by remember()
        assert results[0].document_id
        # marked done: moved out of the inbox into processed/
        assert not dropped.exists()
        processed = Path(vault.path) / ".marginalia" / "processed" / "note.md"
        assert processed.is_file()
    finally:
        vault.close()


def test_processed_block_source_path_resolves_and_rehashes(tmp_path: Path) -> None:
    """After the runner processes an inbox file, the committed Blocks'
    ``source_path`` must resolve on disk (it lives in processed/, not the now-empty
    incoming/) and the stored byte range must re-hash to ``content_hash``. Guards
    the move-after-commit dangling-provenance defect."""
    vault = Vault.init(tmp_path / "v")
    try:
        text = "# Heading\n\nSome body paragraph about alpha and beta.\n"
        _drop(vault, name="anchored.md", text=text)
        results = process_once(Path(vault.path), _companion(vault))
        assert len(results) == 1

        blocks = list(vault.store.list_nodes(type="Block"))
        assert blocks, "deterministic ingest must mint at least one Block"
        for block in blocks:
            facets = block.facets
            source_path = Path(facets["source_path"])
            # anchored to processed/, never the drained incoming/ path
            assert source_path.is_file(), f"source_path dangles: {source_path}"
            assert ".marginalia/processed/" in str(source_path)
            raw = source_path.read_bytes()[facets["byte_start"] : facets["byte_end"]]
            stored = str(facets.get("content_hash") or facets.get("sha256") or "")
            assert hashlib.sha256(raw).hexdigest() == stored.removeprefix("sha256:")
    finally:
        vault.close()


def test_empty_inbox_is_noop(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        inbox_dir(Path(vault.path)).mkdir(parents=True, exist_ok=True)
        assert process_once(Path(vault.path), _companion(vault)) == []
    finally:
        vault.close()


def test_missing_inbox_is_noop(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        assert process_once(Path(vault.path), _companion(vault)) == []
    finally:
        vault.close()


def test_second_run_does_not_reprocess(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _drop(vault)
        first = process_once(Path(vault.path), _companion(vault))
        assert len(first) == 1
        second = process_once(Path(vault.path), _companion(vault))
        assert second == []
    finally:
        vault.close()


def test_only_markdown_is_drained(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        incoming = inbox_dir(Path(vault.path))
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / "data.txt").write_text("not markdown", encoding="utf-8")
        _drop(vault, name="real.md")

        results = process_once(Path(vault.path), _companion(vault))
        assert len(results) == 1
        # the non-markdown file is left untouched in the inbox
        assert (incoming / "data.txt").is_file()
    finally:
        vault.close()


def test_runner_process_once_delegates(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        _drop(vault)
        runner = Runner(Path(vault.path), _companion(vault))
        results = runner.process_once()
        assert len(results) == 1
    finally:
        vault.close()


def test_failing_file_moves_to_failed_and_is_not_reprocessed(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        target = _drop(vault, name="poison.md")
        incoming = inbox_dir(Path(vault.path))
        failed_dir = Path(vault.path) / ".marginalia" / FAILED_DIRNAME
        companion = _RaisingCompanion()

        results = process_once(Path(vault.path), companion)  # type: ignore[arg-type]

        # remember() raised → no results, file left incoming, moved to failed/
        assert results == []
        assert companion.calls == 1
        assert not target.exists()
        assert not (incoming / "poison.md").exists()
        assert (failed_dir / "poison.md").is_file()

        # second drain is a no-op — the poison file is NOT re-fed to remember()
        again = process_once(Path(vault.path), companion)  # type: ignore[arg-type]
        assert again == []
        assert companion.calls == 1
    finally:
        vault.close()


def test_llm_unavailable_file_returns_to_incoming_for_retry(tmp_path: Path) -> None:
    """Defect C: an ``LLMUnavailableError`` (transient/total LLM outage) is
    retryable — unlike a genuinely poisoned file, it must NOT be parked in
    failed/ permanently (which would silently drop it from every future
    drain). It goes back to incoming/ so the next poll retries it."""
    vault = Vault.init(tmp_path / "v")
    try:
        target = _drop(vault, name="outage.md")
        incoming = inbox_dir(Path(vault.path))
        failed_dir = Path(vault.path) / ".marginalia" / FAILED_DIRNAME
        companion = _LLMUnavailableCompanion()

        results = process_once(Path(vault.path), companion)  # type: ignore[arg-type]

        # remember() raised LLMUnavailableError → no results, file back in
        # incoming/ (round-tripped through processed/ and back to the SAME
        # path since nothing else claimed the name), NOT moved to failed/.
        assert results == []
        assert companion.calls == 1
        assert target.exists()
        assert (incoming / "outage.md").is_file()
        assert not (failed_dir / "outage.md").exists()
        processed_dir = Path(vault.path) / ".marginalia" / "processed"
        assert not (processed_dir / "outage.md").exists()

        # next drain retries it (the LLM is still down in this test, so it
        # fails again and returns to incoming/ once more — never dropped).
        again = process_once(Path(vault.path), companion)  # type: ignore[arg-type]
        assert again == []
        assert companion.calls == 2
        assert (incoming / "outage.md").is_file()
    finally:
        vault.close()
