"""Backend effective-capacity policy (ADR 0040 live-run capacity decision).

Only aliases declared in ``llm.parallel_capable_models`` may exceed one
in-flight completion. Everything else clamps to 1 for BOTH extraction and
curation. The clamp lives at the consumption sites, so these tests pin three
separate things: the helper's arithmetic, that stored config is never rewritten,
and that the two real fan-outs actually consume the helper.
"""

from __future__ import annotations

import pathlib

import pytest

from okto_neuron.config._capacity import (
    DEFAULT_PARALLEL_CAPABLE_MODELS,
    capacity_notice,
    capacity_report,
    curation_effective_max_concurrent,
    effective_max_concurrent,
    extraction_effective_max_concurrent,
    is_parallel_capable,
    step_model,
)
from okto_neuron.config._vault import VaultConfig

PARALLEL = DEFAULT_PARALLEL_CAPABLE_MODELS[0]


def _config(
    *,
    default_model: str = "some-serial-model",
    extraction_model: str | None = None,
    curator_model: str | None = None,
    relation_model: str | None = None,
    extraction_max_concurrent: int | None = None,
    curation_max_concurrent: int = 1,
    parallel_capable: list[str] | None = None,
) -> VaultConfig:
    llm: dict = {"defaults": {"model": default_model}}
    if parallel_capable is not None:
        llm["parallel_capable_models"] = parallel_capable
    llm["extraction"] = {}
    if extraction_model is not None:
        llm["extraction"]["model"] = extraction_model
    if extraction_max_concurrent is not None:
        llm["extraction"]["max_concurrent"] = extraction_max_concurrent
    if curator_model is not None:
        llm["curator"] = {"model": curator_model}
    if relation_model is not None:
        llm["relation_curator"] = {"model": relation_model}
    return VaultConfig.model_validate(
        {
            "llm": llm,
            "consolidation": {"curation_max_concurrent": curation_max_concurrent},
        }
    )


# ── helper arithmetic ─────────────────────────────────────────────────────────


def test_default_allowlist_is_the_single_parallel_alias() -> None:
    assert DEFAULT_PARALLEL_CAPABLE_MODELS == ("desktop/qwen3.6-35b-10-parallel",)


@pytest.mark.parametrize("requested", [2, 4, 8, 32])
def test_non_allowlisted_model_clamps_to_one(requested: int) -> None:
    assert (
        effective_max_concurrent(
            requested, models=["glm-5.2"], allowlist=DEFAULT_PARALLEL_CAPABLE_MODELS
        )
        == 1
    )


@pytest.mark.parametrize("requested", [2, 4, 10])
def test_allowlisted_model_keeps_configured_value(requested: int) -> None:
    assert (
        effective_max_concurrent(
            requested, models=[PARALLEL], allowlist=DEFAULT_PARALLEL_CAPABLE_MODELS
        )
        == requested
    )


@pytest.mark.parametrize("configured", [None, 0, -5, 1])
def test_values_at_or_below_one_normalize_to_one(configured: int | None) -> None:
    assert (
        effective_max_concurrent(
            configured, models=[PARALLEL], allowlist=DEFAULT_PARALLEL_CAPABLE_MODELS
        )
        == 1
    )


def test_empty_model_set_is_fail_closed() -> None:
    assert effective_max_concurrent(8, models=[], allowlist=DEFAULT_PARALLEL_CAPABLE_MODELS) == 1


def test_allowlist_match_is_case_and_whitespace_insensitive() -> None:
    assert is_parallel_capable(
        "  Desktop/Qwen3.6-35B-10-Parallel  ", DEFAULT_PARALLEL_CAPABLE_MODELS
    )


def test_blank_alias_is_never_parallel_capable() -> None:
    assert not is_parallel_capable("   ", DEFAULT_PARALLEL_CAPABLE_MODELS)
    assert not is_parallel_capable(None, DEFAULT_PARALLEL_CAPABLE_MODELS)


# ── multi-step fail-closed rule ───────────────────────────────────────────────


def test_curation_is_the_min_across_curator_and_relation_curator() -> None:
    """One serial step in the pair is enough to clamp the whole fan-out."""
    cfg = _config(
        default_model=PARALLEL,
        relation_model="glm-5.2",
        curation_max_concurrent=6,
    )
    assert curation_effective_max_concurrent(cfg) == 1


def test_curation_stays_parallel_when_both_steps_are_capable() -> None:
    cfg = _config(default_model=PARALLEL, curation_max_concurrent=6)
    assert curation_effective_max_concurrent(cfg) == 6


def test_extraction_gates_on_its_own_step_only() -> None:
    """A serial curator must not drag down extraction, and vice versa."""
    cfg = _config(
        default_model="glm-5.2",
        extraction_model=PARALLEL,
        extraction_max_concurrent=4,
        curation_max_concurrent=4,
    )
    assert extraction_effective_max_concurrent(cfg) == 4
    assert curation_effective_max_concurrent(cfg) == 1


def test_step_model_falls_back_to_defaults() -> None:
    cfg = _config(default_model="base-model")
    assert step_model(cfg, "extraction") == "base-model"
    assert step_model(cfg, "curator") == "base-model"


def test_step_model_prefers_the_step_override() -> None:
    cfg = _config(default_model="base-model", extraction_model="override-model")
    assert step_model(cfg, "extraction") == "override-model"
    assert step_model(cfg, "curator") == "base-model"


def test_allowlist_is_configuration_not_hardcoded() -> None:
    """A vault may declare its own parallel-capable backend."""
    cfg = _config(
        default_model="my-local-8-slot",
        curation_max_concurrent=8,
        parallel_capable=["my-local-8-slot"],
    )
    assert curation_effective_max_concurrent(cfg) == 8


def test_default_allowlist_alone_would_clamp_that_same_model() -> None:
    cfg = _config(default_model="my-local-8-slot", curation_max_concurrent=8)
    assert curation_effective_max_concurrent(cfg) == 1


# ── stored config is never rewritten ──────────────────────────────────────────


def test_clamping_does_not_mutate_stored_configuration() -> None:
    """Configured != effective must remain observable, and old YAML loadable."""
    cfg = _config(
        default_model="glm-5.2",
        extraction_max_concurrent=4,
        curation_max_concurrent=4,
    )
    assert cfg.consolidation.curation_max_concurrent == 4
    assert cfg.llm.extraction.max_concurrent == 4
    assert curation_effective_max_concurrent(cfg) == 1
    assert extraction_effective_max_concurrent(cfg) == 1


def test_capacity_report_shows_configured_and_effective_with_a_reason() -> None:
    cfg = _config(
        default_model="glm-5.2",
        extraction_max_concurrent=4,
        curation_max_concurrent=4,
    )
    report = capacity_report(cfg)
    assert report["curation"]["configured"] == 4
    assert report["curation"]["effective"] == 1
    assert "glm-5.2" in report["curation"]["notice"]
    assert "parallel_capable_models" in report["curation"]["notice"]
    assert report["extraction"]["configured"] == 4
    assert report["extraction"]["effective"] == 1


def test_capacity_notice_is_absent_when_nothing_was_clamped() -> None:
    cfg = _config(default_model=PARALLEL, curation_max_concurrent=4)
    report = capacity_report(cfg)
    assert report["curation"]["notice"] is None
    assert (
        capacity_notice(
            configured=1,
            effective=1,
            models=["glm-5.2"],
            allowlist=DEFAULT_PARALLEL_CAPABLE_MODELS,
        )
        is None
    )


def test_notice_blames_only_the_step_that_failed_the_allowlist() -> None:
    """A mixed pair must not name the parallel-capable alias as the cause."""
    cfg = _config(
        default_model=PARALLEL,
        relation_model="glm-5.2",
        curation_max_concurrent=6,
    )
    notice = capacity_report(cfg)["curation"]["notice"]
    assert "glm-5.2" in notice
    assert PARALLEL not in notice


def test_capacity_helper_performs_no_provider_registry_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion re-reads capacity in a hot loop; it must stay I/O-free."""
    import okto_neuron.providers as providers

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("capacity must not touch the provider registry")

    monkeypatch.setattr(providers.ProviderRegistry, "load", _boom)
    cfg = _config(default_model="glm-5.2", curation_max_concurrent=4)
    assert curation_effective_max_concurrent(cfg) == 1


# ── both fan-outs actually consume the helper ─────────────────────────────────


def test_extraction_fan_out_reads_the_capacity_helper() -> None:
    """Pins ADR 0036 extraction (including the mid-run config re-read)."""
    import okto_neuron.companion as companion_mod

    source = pathlib.Path(companion_mod.__file__).read_text()
    assert "extraction_effective_max_concurrent(cfg)" in source
    # the hot-reload path must not bypass the clamp
    assert "extraction_effective_max_concurrent(self._vault_config())" in source
    assert "llm.extraction.max_concurrent or 1" not in source


def test_curation_fan_outs_read_the_capacity_helper() -> None:
    """Pins ADR 0015 D1 curation across the companion and the server job."""
    import okto_neuron.companion as companion_mod
    from okto_neuron.server import _curation as curation_mod

    for module in (companion_mod, curation_mod):
        source = pathlib.Path(module.__file__).read_text()
        assert "curation_effective_max_concurrent(cfg)" in source, module.__name__
        assert "consolidation.curation_max_concurrent" not in source, module.__name__


def test_embedding_is_not_routed_through_the_capacity_helper() -> None:
    """Embedding keeps its own standing decision (batch 32, 1 concurrent batch)."""
    cfg = VaultConfig()
    assert cfg.embedding.max_concurrent_batches == 1
    report = capacity_report(cfg)
    assert set(report) == {"parallel_capable_models", "extraction", "curation", "curation_batch"}


def test_declared_allowlist_survives_an_unrelated_config_patch(tmp_path) -> None:
    """A UI save of some other LLM field must not reset declared capacity.

    The Config UI's draft has no `parallel_capable_models` field, so a PATCH of
    e.g. a step model omits it. `VaultConfig.apply_patch` deep merges, which is
    what keeps the omission harmless. If that ever became a block replace, a
    vault declaring its own multi-slot backend would silently drop from 8
    in-flight to 1 with no error — the exact silent divergence this feature
    exists to surface, caused by the feature's own UI.
    """
    from okto_neuron.vault import Vault

    vault = Vault.init(tmp_path / "v")
    path = pathlib.Path(vault.path)
    (path / "okto-neuron.yaml").write_text(
        "\n".join(
            [
                "marginalia_yaml_version: 1",
                "llm:",
                "  parallel_capable_models:",
                "    - my-8-slot-model",
                "  defaults:",
                "    model: my-8-slot-model",
                "consolidation:",
                "  curation_max_concurrent: 8",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert curation_effective_max_concurrent(VaultConfig.load(path)) == 8

    VaultConfig.apply_patch(path, {"llm": {"extraction": {"max_concurrent": 4}}})

    reloaded = VaultConfig.load(path)
    assert reloaded.llm.parallel_capable_models == ["my-8-slot-model"]
    assert curation_effective_max_concurrent(reloaded) == 8
    assert extraction_effective_max_concurrent(reloaded) == 4


def test_capacity_report_shows_the_relation_batch_cap() -> None:
    cfg = VaultConfig.model_validate({"consolidation": {"curation_batch_size": 32}})
    batch = capacity_report(cfg)["curation_batch"]
    assert batch["configured"] == 32
    assert batch["node_effective"] == 32
    assert batch["relation_effective"] == 4
    assert batch["notice"] is not None and "capped at 4" in batch["notice"]


def test_capacity_report_batch_notice_absent_when_nothing_is_capped() -> None:
    cfg = VaultConfig.model_validate({"consolidation": {"curation_batch_size": 4}})
    batch = capacity_report(cfg)["curation_batch"]
    assert batch["relation_effective"] == batch["node_effective"] == 4
    assert batch["notice"] is None
