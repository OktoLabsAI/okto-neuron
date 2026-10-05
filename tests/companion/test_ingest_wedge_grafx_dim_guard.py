"""P0 regression: a hot embedding-dimension edit on a GRAFX vault must fail
loud with ``EmbeddingDimMismatch`` BEFORE query/ingest — never after a
commit plan is sealed.

The vault's dimension guard (``Vault._ensure_embedding_compatible``) used to
read the stored width only from ``LadybugStore``'s ``_graph_handle``.
``GrafxStore`` (the default backend) keeps the width in
``self._embedding_dim`` and has no ``_graph_handle``, so on grafx the guard
was a silent no-op. Production consequence: the vault config was hot-edited
from a 1024-dim to a 4096-dim embedding model; the next ``remember`` sealed a
semantic plan, then failed mid-apply with the driver's
``GrafxVectorValidationError`` ("stores vectors of 1024 components; got
4096"), and the half-applied sealed plan wedged every later ingest (a sealed
but unreceipted plan must be resumed first).

The fix gives every backend a public read-only ``embedding_dim`` property
(``GraphStore`` member) and makes the guard read it (the ``_graph_handle``
read survives only as a fallback). This test exercises the grafx path end to
end with the model-free ``stub`` embedder: a vault bootstrapped at dim A, a
config hot-edit to dim B exactly the way the config API does it
(``VaultConfig.apply_patch`` + ``Vault.invalidate_runtime_caches``), then

- ``vault.embedder`` / ``vault.query`` raise ``EmbeddingDimMismatch``, and
- ``remember`` raises ``EmbeddingDimMismatch`` before any plan is sealed,
  leaving the candidate ledger with zero unreceipted commit plans.

Skips cleanly when the ``[grafx]`` extra isn't installed (``okto-grafx``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytest.importorskip("okto_grafx")

from okto_neuron import Vault  # noqa: E402
from okto_neuron.companion import Companion  # noqa: E402
from okto_neuron.config import VaultConfig  # noqa: E402
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate  # noqa: E402
from okto_neuron.core.schema import Provenance  # noqa: E402
from okto_neuron.errors import EmbeddingDimMismatch  # noqa: E402
from okto_neuron.extract import ExtractionResult  # noqa: E402
from okto_neuron.llm import StubLLM  # noqa: E402
from okto_neuron.store import vault as vault_module  # noqa: E402

_DIM_A = 16
_DIM_B = 32
_CONTENT = "Pets owned by the narrator."


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()


class _TitleExtractor:
    """One ``ENTITY <title>`` line -> one Concept candidate plus one literal
    claim, so ``remember`` has real vector work to do (same shape as the
    sealed-plan regression tests)."""

    _RE = re.compile(r"ENTITY\s+(.+)")

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        match = self._RE.search(text)
        if match is None:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        node = NodeCandidate(
            type="Concept",
            title=match.group(1).strip(),
            content=_CONTENT,
            provenance=provenance or Provenance(),
        )
        claim = EdgeCandidate(
            type="has_value",
            src_ref=node.candidate_id,
            dst_literal="kept as pets",
            provenance=provenance or Provenance(),
        )
        return ExtractionResult(node_candidates=[node], edge_candidates=[claim])


def _init_grafx_vault(tmp_path: Path, dim: int) -> Vault:
    """Scaffold a grafx-pinned vault whose config says ``dim`` BEFORE the
    first open, so the fresh graph bootstraps at exactly that width (the
    ``stub`` embedder keeps the whole test offline and deterministic)."""
    root = tmp_path / "v"
    root.mkdir()
    (root / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 2,
                "vault_id": "v",
                "federation_opt_in": False,
                "packs": ["core"],
                "embedding": {"provider": "stub", "dimension": dim},
                "storage": {"backend": "grafx"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return Vault.open(root)


def _hot_edit_embedding_dim(vault: Vault, dim: int) -> None:
    """The config API's own hot-edit path (server/http.py
    ``_apply_vault_config_patch``): persist the patch, drop the vault's
    resolved-embedder cache, leave the open store handle untouched."""
    _, changed = VaultConfig.apply_patch(
        vault.path, {"embedding": {"dimension": dim}}
    )
    assert "embedding.dimension" in changed
    vault.invalidate_runtime_caches()


def _note(vault: Vault, name: str, title: str) -> Path:
    path = Path(vault.path) / name
    path.write_text(f"ENTITY {title}\n", encoding="utf-8")
    return path


def _companion(vault: Vault) -> Companion:
    # No injected embedder on purpose: ``remember`` resolves it from the
    # vault, so the hot-edited config is what actually drives the guard and
    # the embedding — the production daemon's own path.
    return Companion(vault, provider=StubLLM(), extractor=_TitleExtractor())


def test_grafx_stored_width_is_exposed_as_embedding_dim(tmp_path: Path) -> None:
    vault = _init_grafx_vault(tmp_path, _DIM_A)
    try:
        assert vault.store.embedding_dim == _DIM_A
        # The real graph directory, for the guard's error message.
        assert Path(vault.store.graph_path).name == "graph.grafx"
    finally:
        vault.close()


def test_hot_dim_edit_raises_before_query_and_before_sealing(
    tmp_path: Path,
) -> None:
    vault = _init_grafx_vault(tmp_path, _DIM_A)
    try:
        companion = _companion(vault)
        # Baseline: at the configured width the vault works end to end.
        companion.remember(_note(vault, "session-1.md", "Turtles"))
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()

        # The production trigger: hot-edit the dimension while the store
        # stays open (the config API's invalidate_runtime_caches path).
        _hot_edit_embedding_dim(vault, _DIM_B)

        # (a) Query-side: resolving the embedder / querying fails loud.
        with pytest.raises(EmbeddingDimMismatch) as exc_info:
            _ = vault.embedder
        assert exc_info.value.stored_dim == _DIM_A
        assert exc_info.value.configured_dim == _DIM_B
        # The error names the store's real graph path, not the hard-coded
        # Ladybug file.
        assert Path(exc_info.value.file_path).name == "graph.grafx"
        with pytest.raises(EmbeddingDimMismatch):
            vault.query("turtles")

        # (b) Ingest-side: remember fails BEFORE any commit plan is sealed,
        # so nothing half-applied can wedge later ingests.
        with pytest.raises(EmbeddingDimMismatch):
            companion.remember(_note(vault, "session-2.md", "Snakes"))
        assert companion._candidate_ledger().unreceipted_commit_plans() == ()

        # And the remedy message tells the operator how to un-wedge.
        with pytest.raises(EmbeddingDimMismatch) as exc_info:
            _ = vault.embedder
        assert "reembed" in str(exc_info.value)
    finally:
        vault.close()
