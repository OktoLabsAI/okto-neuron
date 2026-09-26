from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron import Document, Vault
from okto_neuron.errors import FileNotUnderVaultError, IngestError
from okto_neuron.store import vault as vault_module
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    IntegrityFenceError,
    load_integrity_state,
    write_integrity_state,
)
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_ts_ac817a90_add_rejects_file_outside_vault(tmp_path: Path) -> None:
    pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    try:
        with pytest.raises(FileNotUnderVaultError):
            vault.add(outside)
    finally:
        vault.close()


def test_vault_add_honors_durable_integrity_fence(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    note = vault.path / "notes" / "blocked.md"
    note.write_text("# Blocked\n", encoding="utf-8")
    generation = vault.store._graph_handle.graph_generation  # noqa: SLF001
    write_integrity_state(
        vault.path,
        GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation=generation,
            writer_fenced=True,
            reason="adjacency mismatch",
        ),
    )

    try:
        with pytest.raises(IntegrityFenceError, match="integrity_fenced"):
            vault.add(note)
        assert list(vault.store.list_nodes()) == []
    finally:
        vault.close()


def test_vault_add_reaudits_generation_after_write(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    note = vault.path / "notes" / "audited.md"
    note.write_text("# Audited\n\nbody\n", encoding="utf-8")
    generation = vault.store._graph_handle.graph_generation  # noqa: SLF001

    try:
        vault.add(note)
        state = load_integrity_state(
            vault.path,
            expected_graph_generation=generation,
        )
        assert state.status is AuditStatus.VERIFIED
        assert state.writer_fenced is False
    finally:
        vault.close()


def test_ts_ac817a90_add_accepts_http_scheme_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest = pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")

    def fake_ingest_document(store: object, source: str, *, vault_root: Path) -> Document:
        assert source == "http://example.com/x"
        assert vault_root == vault.path
        return Document(id="remote-doc", path=source)

    monkeypatch.setattr(ingest, "ingest_document", fake_ingest_document)

    try:
        document = vault.add("http://example.com/x")
        assert document.id == "remote-doc"
        assert document.path == "http://example.com/x"
    finally:
        vault.close()


def test_ts_ac817a90_add_rejects_unsupported_url_scheme(tmp_path: Path) -> None:
    pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")

    try:
        with pytest.raises(IngestError, match="unsupported source scheme: ftp"):
            vault.add("ftp://example.com/x")
    finally:
        vault.close()


def test_windows_drive_letter_path_not_misparsed_as_url_scheme(tmp_path: Path) -> None:
    """A Windows drive-letter source (``C:\\Users\\me\\note.md``) is misparsed by
    ``urlparse`` as a URI with scheme 'c'. It must not be rejected at the scheme
    allowlist stage (issue #1) — it may still fail later on POSIX (no such path
    under the vault), but never with the 'unsupported source scheme' message."""
    pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")

    try:
        with pytest.raises(IngestError) as excinfo:
            vault.add("C:\\Users\\me\\note.md")
        assert "unsupported source scheme" not in str(excinfo.value)
    finally:
        vault.close()

    # ftp:// (a genuine multi-letter unsupported scheme) must still be rejected.
    vault2 = Vault.init(tmp_path / "v2")
    try:
        with pytest.raises(IngestError, match="unsupported source scheme: ftp"):
            vault2.add("ftp://example.com/x")
    finally:
        vault2.close()


def test_add_unsupported_suffix_preserves_underlying_exception_message(tmp_path: Path) -> None:
    """Vault.add must not swallow the underlying exception's message (issue #3):
    str(IngestError) should surface the wrapped exception's type and message,
    not the generic 'ingest callable raised an error' default."""
    pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")
    source = vault.path / "unsupported.py"
    source.write_text("print('not markdown')\n", encoding="utf-8")

    try:
        with pytest.raises(IngestError) as excinfo:
            vault.add(source)
        assert "NotImplementedError" in str(excinfo.value)
    finally:
        vault.close()


def test_ts_ac817a90_add_same_file_is_content_hash_idempotent(tmp_path: Path) -> None:
    pytest.importorskip("okto_neuron.ingest")
    vault = Vault.init(tmp_path / "v")
    note = vault.path / "notes" / "same.md"
    note.write_text(
        "---\ntitle: Same\n---\n# Same\n\nThis note carries repeatable provenance.\n",
        encoding="utf-8",
    )

    try:
        first = vault.add(note)
        stored_first = vault.store.get_node(first.id)
        assert stored_first is not None
        epoch = vault.store.semantic_write_epoch
        second = vault.add(note)
        stored_second = vault.store.get_node(second.id)

        assert first.content_hash is not None
        assert second.content_hash == first.content_hash
        assert second.id == first.id
        assert stored_second == stored_first
        assert vault.store.semantic_write_epoch == epoch
    finally:
        vault.close()
