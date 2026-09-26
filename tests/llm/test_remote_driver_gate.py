"""``allow_remote: false`` must refuse drivers that are remote by construction.

The loopback gate is a URL check. Four drivers never set an ``api_base`` — the
three CLI pseudo-providers and ``chatgpt`` — so the check was simply never
reached for them and they egressed under ``allow_remote: false``. These tests
pin both halves of the closure: the provider registry (which reads a profile's
own ``allow_remote``) and the vault's ``llm`` block (which reads
``llm.allow_remote``).
"""

from __future__ import annotations

import pytest

from okto_neuron.config._vault import (
    UNCONDITIONALLY_REMOTE_DRIVERS,
    LLMConfig,
    _check_remote_driver,
)
from okto_neuron.providers import ProviderRegistry

REMOTE_DRIVERS = sorted(UNCONDITIONALLY_REMOTE_DRIVERS)


def test_the_gated_set_is_exactly_the_four_baseless_remote_drivers() -> None:
    """A driver joins this set only when it egresses with no api_base to check."""

    assert set(UNCONDITIONALLY_REMOTE_DRIVERS) == {
        "chatgpt",
        "codex_cli",
        "claude_cli",
        "pi_cli",
    }


@pytest.mark.parametrize("driver", REMOTE_DRIVERS)
def test_vault_llm_block_refuses_a_remote_driver(driver: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        LLMConfig(
            enabled=True,
            allow_remote=False,
            defaults={"provider": driver, "model": "m"},
        )
    message = str(excinfo.value)
    # The error has to name the driver, say WHY it is remote, and name the
    # exact knob to flip — an operator reading only this line should not have
    # to go find out which of two ``allow_remote`` fields applies.
    assert driver in message
    assert "llm.allow_remote" in message
    assert UNCONDITIONALLY_REMOTE_DRIVERS[driver].split(";")[0][:30] in message


@pytest.mark.parametrize("driver", REMOTE_DRIVERS)
def test_vault_llm_block_allows_it_once_remote_egress_is_accepted(driver: str) -> None:
    config = LLMConfig(
        enabled=True,
        allow_remote=True,
        defaults={"provider": driver, "model": "m"},
    )
    assert config.resolved_defaults().provider == driver


@pytest.mark.parametrize("driver", REMOTE_DRIVERS)
def test_a_step_override_cannot_smuggle_a_remote_driver_past_the_gate(driver: str) -> None:
    """Defaults may be local while one step quietly points somewhere hosted."""

    with pytest.raises(ValueError):
        LLMConfig(
            enabled=True,
            allow_remote=False,
            defaults={"provider": "openai", "model": "m"},
            curator={"provider": driver, "model": "m"},
        )


@pytest.mark.parametrize("driver", REMOTE_DRIVERS)
def test_provider_registry_refuses_a_remote_driver(monkeypatch, tmp_path, driver: str) -> None:
    monkeypatch.setenv("OKTO_NEURON_HOME", str(tmp_path))
    registry = ProviderRegistry.load()
    with pytest.raises(ValueError) as excinfo:
        registry.add_provider(
            name=f"probe-{driver}",
            driver=driver,
            api_base=None,
            allow_remote=False,
            credential_id=None,
            parameter_mode="safe",
        )
    assert "allow_remote" in str(excinfo.value)


@pytest.mark.parametrize("driver", REMOTE_DRIVERS)
def test_provider_registry_accepts_it_with_allow_remote(monkeypatch, tmp_path, driver: str) -> None:
    monkeypatch.setenv("OKTO_NEURON_HOME", str(tmp_path))
    registry = ProviderRegistry.load()
    profile = registry.add_provider(
        name=f"probe-{driver}",
        driver=driver,
        api_base=None,
        allow_remote=True,
        credential_id=None,
        parameter_mode="safe",
    )
    assert profile.driver == driver


def test_a_driver_outside_the_set_is_untouched() -> None:
    """The gate must not become a second, divergent copy of the URL check."""

    _check_remote_driver("openai", False, where="llm.allow_remote")
    _check_remote_driver("ollama", False, where="llm.allow_remote")


def test_local_drivers_still_load_under_allow_remote_false() -> None:
    config = LLMConfig(
        enabled=True,
        allow_remote=False,
        defaults={"provider": "openai", "api_base": "http://127.0.0.1:8123/v1", "model": "m"},
    )
    assert config.resolved_defaults().api_base == "http://127.0.0.1:8123/v1"
