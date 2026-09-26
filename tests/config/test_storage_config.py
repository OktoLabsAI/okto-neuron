"""``StorageConfig``/``IndexConfig`` discriminated-union contract (M3 spec §2.1/§4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from okto_neuron.config._vault import (
    GrafxStorageConfig,
    IndexConfig,
    LadybugStorageConfig,
    Neo4jStorageConfig,
    NeptuneStorageConfig,
    StorageConfig,
    VaultConfig,
)

GOLDEN_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "vaults" / "_smoke" / "okto-neuron.yaml"
)

_STORAGE_ADAPTER = TypeAdapter(StorageConfig)


def test_ladybug_storage_config_round_trips_the_pre_m3_shape() -> None:
    """Field shape matches what Vault._write_config has always emitted."""
    config = LadybugStorageConfig(backend="ladybug", reason=None)
    assert config.model_dump(mode="json") == {"backend": "ladybug", "reason": None}


def test_ladybug_storage_config_defaults_match_the_pre_m3_shape() -> None:
    assert LadybugStorageConfig().model_dump(mode="json") == {"backend": "ladybug", "reason": None}


@pytest.mark.skipif(
    not GOLDEN_FIXTURE.exists(),
    reason=f"golden vault fixture absent (tests/golden/vaults/ is gitignored): {GOLDEN_FIXTURE}",
)
def test_pre_m3_golden_fixture_loads_without_validation_error(tmp_path: Path) -> None:
    """A real, unmodified pre-M3 okto-neuron.yaml (storage: {backend: ladybug,
    reason: null}) must still load cleanly under the new discriminated union."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        GOLDEN_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    config = VaultConfig.load(vault_path)

    assert isinstance(config.storage, LadybugStorageConfig)
    assert config.storage.backend == "ladybug"
    assert config.storage.reason is None


def test_vault_config_default_omits_storage_and_index() -> None:
    """storage/index stay None-defaulted so _write_default_config_if_absent's
    model_dump(mode="json", exclude_none=True) omits them entirely."""
    dumped = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    assert "storage" not in dumped
    assert "index" not in dumped


def test_marginalia_yaml_with_no_storage_key_resolves_to_ladybug(tmp_path: Path) -> None:
    """A legacy vault whose ``okto-neuron.yaml`` predates any backend being
    pinnable (no ``storage`` key at all, not even an explicit
    ``backend: ladybug``) must still resolve to ladybug -- unaffected by
    ``DEFAULT_NEW_VAULT_BACKEND`` becoming ``grafx`` for a genuinely NEW
    vault (config/_vault.py's own comment on ``VaultConfig.storage`` and
    ``VaultConfig.default()``). ``config.storage`` itself stays ``None``
    here (matching ``VaultConfig.default()``'s own field default, deep-merge
    baseline included); every read site that resolves a pinned backend name
    (``vault.py::_check_backend_pin``, ``store/vault.py::
    _read_pinned_backends``, ``cli/kg.py::_resolve_pinned_backend``,
    ``reconcile/heal.py``) is what turns that ``None`` into the literal
    string ``"ladybug"`` -- exercised end-to-end by
    ``tests/cli/test_cli_backend_experimental.py::
    test_kg_init_no_backend_flag_on_a_fresh_vault_stays_ladybug``.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\n"
        "vault_id: vault\n"
        "packs: [core]\n"
        "embedding:\n"
        "  provider: fastembed\n",
        encoding="utf-8",
    )

    config = VaultConfig.load(vault_path)

    assert config.storage is None
    pinned_backend = config.storage.backend if config.storage is not None else "ladybug"
    assert pinned_backend == "ladybug"


def test_index_config_defaults_to_the_ladybug_engine() -> None:
    assert IndexConfig().backend == "ladybug_bm25+vector_scan"


def test_neo4j_storage_config_rejects_a_foreign_credential_env() -> None:
    with pytest.raises(ValidationError, match="OKTO_NEURON_"):
        Neo4jStorageConfig(
            backend="neo4j",
            uri="bolt://localhost:7687",
            credential_env="AWS_SECRET_ACCESS_KEY",
        )


def test_neo4j_storage_config_accepts_a_marginalia_namespaced_credential_env() -> None:
    config = Neo4jStorageConfig(
        backend="neo4j",
        uri="bolt://localhost:7687",
        credential_env="OKTO_NEURON_NEO4J_PASSWORD",
    )
    assert config.credential_env == "OKTO_NEURON_NEO4J_PASSWORD"


def test_neo4j_storage_config_defaults_a_retry_policy_like_grafx() -> None:
    """Mirrors ``GrafxStorageConfig.retry`` -- ``Neo4jStore`` already reads
    ``config.retry`` via ``getattr`` (see ``neo4j.py``'s ``_resolve_retry_policy``),
    so a Neo4j vault needs the same declared field for a non-default policy to
    ever reach it through ``okto-neuron.yaml``."""
    config = Neo4jStorageConfig(backend="neo4j", uri="bolt://localhost:7687")
    assert config.retry.max_attempts == 8

    with_override = Neo4jStorageConfig(
        backend="neo4j",
        uri="bolt://localhost:7687",
        retry={"max_attempts": 3},
    )
    assert with_override.retry.max_attempts == 3


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (LadybugStorageConfig, {"backend": "ladybug"}),
        (GrafxStorageConfig, {"backend": "grafx"}),
        (Neo4jStorageConfig, {"backend": "neo4j", "uri": "bolt://localhost:7687"}),
        (
            NeptuneStorageConfig,
            {"backend": "neptune", "endpoint": "neptune-db://example", "region": "us-east-1"},
        ),
    ],
)
def test_extra_forbid_rejects_an_unknown_key_per_variant(model_cls, payload) -> None:
    with pytest.raises(ValidationError):
        model_cls(**payload, unknown_field="surprise")


def test_storage_config_union_resolves_every_backend_by_discriminator() -> None:
    assert isinstance(
        _STORAGE_ADAPTER.validate_python({"backend": "ladybug"}), LadybugStorageConfig
    )
    assert isinstance(_STORAGE_ADAPTER.validate_python({"backend": "grafx"}), GrafxStorageConfig)
    assert isinstance(
        _STORAGE_ADAPTER.validate_python({"backend": "neo4j", "uri": "bolt://localhost:7687"}),
        Neo4jStorageConfig,
    )
    assert isinstance(
        _STORAGE_ADAPTER.validate_python(
            {"backend": "neptune", "endpoint": "neptune-db://example", "region": "us-east-1"}
        ),
        NeptuneStorageConfig,
    )


def test_storage_config_union_falls_back_to_custom_for_an_unknown_backend_name() -> None:
    """A ``backend`` name outside the four typed literals parses via the
    ``CustomStorageConfig`` fallback, not a ``ValidationError``.

    Backend-name *validity* is the registry's job (``resolve_graph_backend``,
    checked at every creation/open call site via ``_resolve_and_pin_backend``
    before a config is ever written), not the config layer's — a registry
    that can resolve a real out-of-tree backend (M3 spec §2.11's
    ``stub_backend_pkg``) but a config model that can never round-trip that
    backend's name would make the registry pointless. ``extra="forbid"``
    still applies to the fallback variant, so an unrelated unknown key is
    still rejected (covered by ``test_extra_forbid_rejects_an_unknown_key_per_variant``
    below, plus the dedicated case here).
    """
    from okto_neuron.config._vault import CustomStorageConfig

    resolved = _STORAGE_ADAPTER.validate_python({"backend": "sqlite"})
    assert isinstance(resolved, CustomStorageConfig)
    assert resolved.backend == "sqlite"
    assert resolved.reason is None

    with pytest.raises(ValidationError):
        _STORAGE_ADAPTER.validate_python({"backend": "sqlite", "unknown_field": "surprise"})
