"""Tests for the raw sampling-payload override (``sampling_payload``) on
``LLMDefaults``/``StepLLM``/``ResolvedLLM``.

Covers: reserved-key rejection, arbitrary-key acceptance (no whitelist, no
range validation), FROZEN per-role resolution (no inheritance/merge from
``defaults`` once a role sets its own payload), the config-PATCH
replace-not-merge fix in ``_deep_merge``, a full save/load round trip, the
promoted ``SAMPLING_PRESETS`` drift guard, and backward compatibility with
vaults that predate this field entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from okto_neuron.config._vault import (
    RESERVED_SAMPLING_PAYLOAD_KEYS,
    SAMPLING_PRESETS,
    LLMConfig,
    LLMDefaults,
    StepLLM,
    VaultConfig,
)

GOLDEN_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "vaults" / "adr0040-cop-hinge2" / "okto-neuron.yaml"
)


def _vault(tmp_path: Path, yaml_text: str = "marginalia_yaml_version: 1\n") -> Path:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(yaml_text, encoding="utf-8")
    return vault_path


class TestReservedKeys:
    """Decision 2 (six connection-owned keys) plus the 2026-09-15 owner
    expansion (four response-shape keys): exactly ten keys are reserved,
    split across two categories with distinct rejection wording; every other
    key passes."""

    @pytest.mark.parametrize("key", sorted(RESERVED_SAMPLING_PAYLOAD_KEYS))
    def test_reserved_key_rejected_on_defaults(self, key: str) -> None:
        with pytest.raises(ValidationError, match=key):
            LLMDefaults(sampling_payload={key: "nope"})

    @pytest.mark.parametrize("key", sorted(RESERVED_SAMPLING_PAYLOAD_KEYS))
    def test_reserved_key_rejected_on_step(self, key: str) -> None:
        with pytest.raises(ValidationError, match=key):
            StepLLM(sampling_payload={key: "nope"})

    def test_reserved_key_set_is_exactly_ten(self) -> None:
        assert RESERVED_SAMPLING_PAYLOAD_KEYS == {
            # connection-owned (decision 2)
            "api_base",
            "api_key",
            "drop_params",
            "messages",
            "model",
            "timeout",
            # response-shape (owner expansion, 2026-09-15): these change the
            # SHAPE of the response Okto Neuron has to parse, not the sampling
            # of it — the same category as blocking ``messages``.
            "extra_body",
            "n",
            "stream",
            "tools",
        }

    @pytest.mark.parametrize("key", ["stream", "n", "tools", "extra_body"])
    def test_response_shape_keys_rejected_with_distinct_wording(self, key: str) -> None:
        """The response-shape rejection names the category and is worded
        differently from the connection-owned rejection, since the reason
        differs (parse-shape ownership, not connection/transport ownership)."""
        with pytest.raises(ValidationError, match="response shape"):
            StepLLM(sampling_payload={key: True})

    @pytest.mark.parametrize(
        "key", ["api_base", "api_key", "drop_params", "messages", "model", "timeout"]
    )
    def test_connection_owned_keys_rejected_with_connection_wording(self, key: str) -> None:
        with pytest.raises(ValidationError, match="owned by the connection config"):
            StepLLM(sampling_payload={key: "nope"})


class TestArbitraryKeysAccepted:
    """Decision 2: no whitelist, no range validation — anything else passes."""

    def test_typical_p_stop_token_ids_and_nested_grammar_pass_through(self) -> None:
        payload = {
            "typical_p": 0.92,
            "stop_token_ids": [151643, 151644],
            "grammar": {"type": "json_object", "schema": {"type": "object", "properties": {}}},
        }
        step = StepLLM(sampling_payload=payload)
        assert step.sampling_payload == payload

    def test_out_of_range_style_value_is_not_rejected(self) -> None:
        """A value that WOULD have failed a typed field's range validator
        (e.g. temperature > 2.0) must pass here — the backend is the
        validator, deliberately (decision 2)."""
        step = StepLLM(sampling_payload={"temperature": 9.9})
        assert step.sampling_payload == {"temperature": 9.9}

    def test_ordinary_sampling_keys_unaffected_by_response_shape_expansion(self) -> None:
        """None of the newly reserved response-shape keys (stream/n/tools/
        extra_body) collide with genuine sampling parameters — top_k, min_p,
        a nested ``chat_template_kwargs``, and an arbitrary vendor key all
        still pass through untouched."""
        payload = {
            "typical_p": 0.9,
            "top_k": 20,
            "min_p": 0.05,
            "chat_template_kwargs": {"enable_thinking": False},
            "some_vendor_specific_knob": "value",
        }
        step = StepLLM(sampling_payload=payload)
        assert step.sampling_payload == payload


class TestFrozenResolution:
    """Decision 3: a role with its own payload never merges with defaults,
    and changing the default payload must never alter a role that already
    has its own."""

    def test_step_with_own_payload_ignores_default_payload_entirely(self) -> None:
        cfg = LLMConfig(
            defaults=LLMDefaults(
                api_base="http://127.0.0.1:8123/v1",
                sampling_payload={"stop_token_ids": [1, 2, 3]},
            ),
            extraction=StepLLM(sampling_payload={"typical_p": 0.9}),
        )
        resolved = cfg.resolved("extraction")
        assert resolved.sampling_payload == {"typical_p": 0.9}

    def test_changing_default_payload_does_not_alter_a_role_with_its_own(self) -> None:
        cfg = LLMConfig(
            defaults=LLMDefaults(api_base="http://127.0.0.1:8123/v1"),
            extraction=StepLLM(sampling_payload={"typical_p": 0.9}),
        )
        before = cfg.resolved("extraction").sampling_payload

        cfg.defaults.sampling_payload = {"stop_token_ids": [9, 9, 9]}
        after = cfg.resolved("extraction").sampling_payload

        assert before == after == {"typical_p": 0.9}

    def test_step_without_its_own_payload_inherits_default_whole(self) -> None:
        cfg = LLMConfig(
            defaults=LLMDefaults(
                api_base="http://127.0.0.1:8123/v1",
                sampling_payload={"stop_token_ids": [1, 2, 3]},
            ),
            judge=StepLLM(),
        )
        resolved = cfg.resolved("judge")
        assert resolved.sampling_payload == {"stop_token_ids": [1, 2, 3]}

    def test_step_with_explicit_empty_payload_owns_an_empty_payload(self) -> None:
        """An explicit ``{}`` on a step is itself a frozen (empty) payload —
        distinct from ``None`` (never set, inherits default)."""
        cfg = LLMConfig(
            defaults=LLMDefaults(
                api_base="http://127.0.0.1:8123/v1",
                sampling_payload={"stop_token_ids": [1, 2, 3]},
            ),
            ask=StepLLM(sampling_payload={}),
        )
        resolved = cfg.resolved("ask")
        assert resolved.sampling_payload == {}

    def test_resolved_defaults_reports_the_default_payload(self) -> None:
        cfg = LLMConfig(
            defaults=LLMDefaults(
                api_base="http://127.0.0.1:8123/v1",
                sampling_payload={"typical_p": 0.5},
            )
        )
        assert cfg.resolved_defaults().sampling_payload == {"typical_p": 0.5}


class TestPatchReplacesNotMerges:
    """The write-path (`_deep_merge`) must never fold a new sampling_payload
    PATCH into what was stored — same "no deep merge anywhere" rule as
    resolution."""

    def test_second_patch_replaces_first_wholesale(self, tmp_path: Path) -> None:
        vault_path = _vault(tmp_path)
        VaultConfig.apply_patch(
            vault_path,
            {
                "llm": {
                    "extraction": {
                        "sampling_payload": {"typical_p": 0.9, "stop_token_ids": [1, 2]}
                    }
                }
            },
        )
        cfg, _ = VaultConfig.apply_patch(
            vault_path,
            {"llm": {"extraction": {"sampling_payload": {"grammar": {"type": "json"}}}}},
        )
        assert cfg.llm.extraction.sampling_payload == {"grammar": {"type": "json"}}


class TestRoundTrip:
    def test_save_and_reload_preserves_sampling_payload_exactly(self, tmp_path: Path) -> None:
        vault_path = _vault(tmp_path)
        payload = {
            "typical_p": 0.9,
            "stop_token_ids": [1, 2, 3],
            "grammar": {"type": "json_object", "schema": {"x": 1}},
        }
        VaultConfig.apply_patch(
            vault_path, {"llm": {"extraction": {"sampling_payload": payload}}}
        )

        reloaded = VaultConfig.load(vault_path)

        assert reloaded.llm.extraction.sampling_payload == payload
        assert reloaded.llm.resolved("extraction").sampling_payload == payload


class TestSamplingPresetsDriftGuard:
    """Independently pins the product's SAMPLING_PRESETS values (mirrors the
    literal assertions in tests/benchmarks/locomo/test_config.py, which now
    imports this exact object) so a product-side edit cannot silently
    invalidate recorded LoCoMo runs without a test failing here too."""

    def test_instruct_preset_matches_recorded_values(self) -> None:
        preset = SAMPLING_PRESETS["instruct"]
        assert preset["temperature"] == 0.7
        assert preset["max_tokens"] == 32768
        assert preset["top_p"] == 0.80
        assert preset["top_k"] == 20
        assert preset["min_p"] == 0.0
        assert preset["presence_penalty"] == 1.5
        assert preset["repeat_penalty"] == 1.0
        assert preset["enable_thinking"] is False
        assert preset["reasoning_effort"] == "none"
        assert preset["preserve_thinking"] is False
        assert preset["chat_template_kwargs"] == {
            "enable_thinking": False,
            "preserve_thinking": False,
            "reasoning_effort": "none",
        }

    def test_thinking_preset_matches_recorded_values(self) -> None:
        preset = SAMPLING_PRESETS["thinking"]
        assert preset["temperature"] == 1.0
        assert preset["max_tokens"] == 32768
        assert preset["top_p"] == 0.95
        assert preset["top_k"] == 20
        assert preset["min_p"] == 0.0
        assert preset["presence_penalty"] == 0.0
        assert preset["repeat_penalty"] == 1.0
        assert preset["enable_thinking"] is True
        assert preset["reasoning_effort"] == "xhigh"
        assert preset["preserve_thinking"] is False
        assert preset["chat_template_kwargs"] == {
            "enable_thinking": True,
            "preserve_thinking": False,
            "reasoning_effort": "xhigh",
        }

    def test_preset_keys_never_collide_with_reserved_sampling_payload_keys(self) -> None:
        """Every preset key is a valid sampling_payload entry — none of them
        happen to be reserved."""
        for name, preset in SAMPLING_PRESETS.items():
            collision = set(preset) & RESERVED_SAMPLING_PAYLOAD_KEYS
            assert not collision, f"preset {name!r} uses reserved key(s) {collision}"


class TestBackwardCompatibility:
    """Existing vaults using only the old typed fields / ``parameters`` map
    (no ``sampling_payload`` key at all) must keep loading and resolving
    exactly as before this field existed."""

    @pytest.mark.skipif(
        not GOLDEN_FIXTURE.exists(),
        reason=f"golden vault fixture absent (tests/golden/vaults/ is gitignored): {GOLDEN_FIXTURE}",
    )
    def test_pre_existing_golden_vault_loads_with_empty_sampling_payload(self) -> None:
        cfg = VaultConfig.load(GOLDEN_FIXTURE.parent)
        assert cfg.llm.defaults.sampling_payload == {}
        assert cfg.llm.resolved("extraction").sampling_payload == {}

    def test_default_llm_config_has_empty_sampling_payload_everywhere(self) -> None:
        cfg = LLMConfig()
        assert cfg.defaults.sampling_payload == {}
        for step_name in ("extraction", "judge", "curator", "relation_curator", "ask"):
            assert cfg.resolved(step_name).sampling_payload == {}

    def test_typed_fields_still_resolve_when_sampling_payload_unset(self) -> None:
        """A vault that only ever used the old typed sampler fields must
        resolve identically — sampling_payload being empty must not
        interfere with them. (The old generic ``parameters`` map this test
        used to also cover was removed outright, decision A, 2026-09-15 — no
        migration, no deprecation period.)"""
        cfg = LLMConfig(
            defaults=LLMDefaults(
                api_base="http://127.0.0.1:8123/v1",
                temperature=0.3,
            )
        )
        resolved = cfg.resolved("extraction")
        assert resolved.temperature == 0.3
        assert resolved.sampling_payload == {}
