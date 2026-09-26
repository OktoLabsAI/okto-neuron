from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading

from click.testing import CliRunner
from click.testing import _NamedTextIOWrapper
import pytest

from okto_neuron.cli import app
from okto_neuron.cli import _inspect_existing_llm_config
from okto_neuron.cli import _prompt_existing_llm_action
from okto_neuron.cli import _select_onboarding_model
from okto_neuron.config import VaultConfig
from okto_neuron import Vault
from okto_neuron.onboarding import ModelDiscoveryResult, VerifyCompletionResult
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


@pytest.fixture(autouse=True)
def stub_pre_save_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the orchestration tests hermetic: they exercise config
    persistence/prompting against fake endpoints that cannot answer a real
    completion. Tests that need the REAL pre-save verify re-patch it back —
    the test's monkeypatch applies after this fixture and wins for the test.
    """
    monkeypatch.setattr(
        "okto_neuron.cli.verify_onboarding_completion",
        lambda *args, **kwargs: VerifyCompletionResult(
            ok=True, url_attempted="http://127.0.0.1:9/v1/chat/completions"
        ),
    )


def test_onboard_noninteractive_creates_vault_and_persists_visible_llm_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    assert result.exit_code == 0, result.output
    assert "Okto Neuron onboarding complete" in result.output
    cfg = VaultConfig.load(vault_path)
    assert cfg.llm.defaults.provider == "openai"
    assert cfg.llm.defaults.api_base == "http://127.0.0.1:9999/v1"
    assert cfg.llm.defaults.model == "qwen-local"
    assert cfg.llm.defaults.api_key_env is None
    assert cfg.llm.allow_remote is False


def test_onboard_hosted_provider_stores_secret_in_env_file_not_yaml_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "openai",
            "--api-key",
            "sk-test-secret",
            "--model",
            "gpt-test",
            "--skip-model-discovery",
            "--allow-remote-llm",
            "--yes",
            "--non-interactive",
        ],
    )

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    env_path = home / ".okto-neuron" / "env"
    cfg = VaultConfig.load(vault_path)
    raw_yaml = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")

    assert result.exit_code == 0, result.output
    assert cfg.llm.allow_remote is True
    assert cfg.llm.defaults.provider == "openai"
    assert cfg.llm.defaults.api_base == "https://api.openai.com/v1"
    assert cfg.llm.defaults.model == "gpt-test"
    assert cfg.llm.defaults.api_key_env == "OKTO_NEURON_OPENAI_API_KEY"
    assert env_path.is_file()
    raw_env = env_path.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "OKTO_NEURON_OPENAI_API_KEY=dpapi-v1:" in raw_env
        assert "sk-test-secret" not in raw_env
    else:
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        assert "OKTO_NEURON_OPENAI_API_KEY=sk-test-secret" in raw_env
    assert "sk-test-secret" not in raw_yaml
    assert "sk-test-secret" not in result.output


def test_onboard_hosted_provider_requires_remote_confirmation_noninteractive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 1
    assert "--allow-remote-llm --yes" in result.output


def test_onboard_skip_leaves_no_explicit_llm_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "skip",
            "--non-interactive",
        ],
    )

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    raw_yaml = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert result.exit_code == 0, result.output
    assert "\nllm:" not in raw_yaml
    assert "state:    not_configured" in result.output


def test_onboard_existing_config_is_kept_without_reconfigure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--non-interactive",
        ],
    )

    cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
    assert second.exit_code == 0, second.output
    assert "state:    configured" in second.output
    assert cfg.llm.defaults.api_base == "http://127.0.0.1:9999/v1"


def test_onboard_existing_config_action_two_inspects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_prompt(*_args, **_kwargs):
        return 2

    monkeypatch.setattr("okto_neuron.cli.click.prompt", fake_prompt)
    assert _prompt_existing_llm_action("configured") == "inspect"


def test_onboard_existing_config_inspection_validates_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 0, result.output

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    yaml_path = vault_path / "okto-neuron.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "okto_neuron.cli.discover_models",
        lambda *_args, **_kwargs: ModelDiscoveryResult(["other", "qwen-local"]),
    )

    _inspect_existing_llm_config(vault_path)

    output = capsys.readouterr().out
    after = yaml_path.read_text(encoding="utf-8")
    assert after == before
    assert "Testing existing LLM config" in output
    assert "validation: config schema ok" in output
    assert "provider: openai" in output
    assert "base URL: http://127.0.0.1:9999/v1" in output
    assert "model discovery: ok (2 models; configured model listed: yes)" in output
    assert "config unchanged" in output


def test_onboard_existing_config_rejects_conflicting_flags_without_reconfigure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "ollama",
            "--model",
            "llama-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )

    cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
    assert second.exit_code == 1
    assert "--reconfigure" in second.output
    assert cfg.llm.defaults.provider == "openai"
    assert cfg.llm.defaults.api_base == "http://127.0.0.1:9999/v1"
    assert cfg.llm.defaults.model == "qwen-local"


def test_onboard_reconfigure_replaces_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "ollama",
            "--model",
            "llama-local",
            "--skip-model-discovery",
            "--reconfigure",
            "--non-interactive",
        ],
    )

    cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
    assert second.exit_code == 0, second.output
    assert cfg.llm.defaults.provider == "ollama"
    assert cfg.llm.defaults.api_base == "http://127.0.0.1:11434/v1"
    assert cfg.llm.defaults.model == "llama-local"


def test_onboard_disable_llm_marks_existing_config_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    first = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--disable-llm",
            "--non-interactive",
        ],
    )

    cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
    assert second.exit_code == 0, second.output
    assert "state:    disabled" in second.output
    assert cfg.llm.enabled is False


def test_onboard_dry_run_prints_json_and_writes_no_llm_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "ollama",
            "--model",
            "llama-local",
            "--skip-model-discovery",
            "--dry-run",
            "--print-summary-json",
            "--non-interactive",
        ],
    )

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["provider"] == "ollama"
    assert not (vault_path / "okto-neuron.yaml").exists()


def test_onboard_discovered_models_accepts_manual_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "okto_neuron.cli.click.prompt",
        lambda *_args, **_kwargs: "manual-model-id",
    )

    selected = _select_onboarding_model(
        ["listed-a", "listed-b"],
        default_model="listed-a",
        interactive=True,
    )

    assert selected == "manual-model-id"


def test_onboard_legacy_local_without_model_runs_discovery_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.0.48 companion item: the hidden legacy-local preset carries no
    hardcoded model, so an onboard run without --model must drive the existing
    discovery step against the base URL — never preselect a model the
    endpoint may not serve. Since the base-URL fix, a NON-INTERACTIVE run
    then demands an explicit --model (it must not silently default to the
    first discovered model, which on a multi-model server can be a
    non-chat model); the interactive flow still offers the discovered list."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    calls: list[dict] = []

    def fake_discover_models(preset, *, api_base=None, api_key=None, **_kwargs):
        calls.append({"key": preset.key, "api_base": api_base, "api_key": api_key})
        return ModelDiscoveryResult(
            models=["discovered-first", "discovered-second"],
            endpoint=f"{api_base}/models",
        )

    monkeypatch.setattr("okto_neuron.cli.discover_models", fake_discover_models)

    # Non-interactive without --model drives the discovery step first, then
    # REFUSES to silently pick a discovered model (the hidden legacy-local
    # preset has no preset default of its own) and saves nothing.
    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--non-interactive",
        ],
    )

    assert len(calls) == 1
    assert calls[0]["key"] == "local"
    assert calls[0]["api_base"] == "http://127.0.0.1:9999/v1"
    assert calls[0]["api_key"] is None
    assert result.exit_code != 0, result.output
    assert "discovered-first" in result.output
    assert "discovered-second" in result.output
    assert "--model" in result.output
    assert "Traceback" not in result.output
    vault_yaml = (home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml").read_text(
        encoding="utf-8"
    )
    assert "llm" not in vault_yaml
    assert "Qwen3.6-35B-A3B-oQ4-fp16-mtp" not in result.output


def test_onboard_explicit_backend_grafx_matches_implicit_default_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--backend grafx`` is a no-op vs. omitting the flag: ``grafx`` is the
    default (owner decision retiring D-12 -- Okto Grafx is the default,
    non-experimental graph backend), so an explicit ``--backend grafx`` must
    scaffold a byte-identical ``okto-neuron.yaml`` (storage block included)
    and print the same summary, modulo the vault path itself. No
    ``--accept-experimental`` is needed on either invocation.
    """
    home_implicit = tmp_path / "home-implicit"
    home_explicit = tmp_path / "home-explicit"
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    base_args = [
        "onboard",
        "--vault",
        "alpha",
        "--provider",
        "local",
        "--api-base",
        "http://127.0.0.1:9999/v1",
        "--model",
        "qwen-local",
        "--skip-model-discovery",
        "--non-interactive",
    ]

    monkeypatch.setenv("HOME", str(home_implicit))
    implicit = CliRunner().invoke(app, base_args)
    monkeypatch.setenv("HOME", str(home_explicit))
    explicit = CliRunner().invoke(app, [*base_args, "--backend", "grafx"])

    assert implicit.exit_code == 0, implicit.output
    assert explicit.exit_code == 0, explicit.output
    implicit_yaml = (home_implicit / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml").read_text(
        encoding="utf-8"
    )
    explicit_yaml = (home_explicit / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml").read_text(
        encoding="utf-8"
    )
    assert implicit_yaml == explicit_yaml
    assert "storage:\n  backend: grafx\n  reason: null\n" in implicit_yaml
    normalized_implicit = implicit.output.replace(str(home_implicit), "<HOME>")
    normalized_explicit = explicit.output.replace(str(home_explicit), "<HOME>")
    assert normalized_implicit == normalized_explicit


def test_onboard_explicit_backend_ladybug_is_still_fully_selectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ladybug remains a fully supported, selectable backend -- it is simply
    no longer the *default* one. No ``--accept-experimental`` needed here
    either; ladybug was never D-12-gated."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
            "--backend",
            "ladybug",
        ],
    )

    assert result.exit_code == 0, result.output
    vault_yaml = (
        tmp_path / "home" / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "storage:\n  backend: ladybug\n  reason: null\n" in vault_yaml


def test_onboard_remote_storage_endpoint_without_consent_requires_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3 spec section 4: a non-loopback ``--storage-uri`` needs explicit consent.

    ``_confirm_remote_storage_endpoint`` runs before backend/pin validation
    (``cli/__init__.py`` onboard body), so this fails on the missing consent
    flags regardless of whether ``neo4j`` is itself a registered backend.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--backend",
            "neo4j",
            "--storage-uri",
            "bolt://remote.example:7687",
            "--provider",
            "skip",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "bolt://remote.example:7687 is not loopback" in result.output
    assert "--allow-remote-db --yes" in result.output
    assert not (home / ".okto-neuron" / "vaults" / "alpha").exists()


def test_onboard_remote_storage_consent_then_unregistered_backend_fails_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3 spec section 4: consenting to the endpoint does not register the backend.

    With ``--allow-remote-db --yes`` the endpoint consent step passes, but
    ``neptune`` is only a typed :class:`~okto_neuron.config._vault.StorageConfig`
    variant (M3 spec section 2.1/D-45) — it is not registry-reachable (M5
    registers ``neo4j``, not ``neptune``; ``store/registry.py``'s ``_OFFICIAL``
    map has no ``neptune`` entry), so ``_resolve_and_pin_backend`` ->
    ``_validate_backend_name`` rejects it with ``NoSuchBackendError`` before
    ``_onboarding_vault`` ever calls ``Vault.scaffold``. No vault directory is
    created, so the credential env var's value can never reach a written
    ``okto-neuron.yaml`` or the CLI output — a stronger guarantee than "the yaml
    doesn't hold it".
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.setenv("OKTO_NEURON_NEPTUNE_PASSWORD", "super-secret-value")

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--backend",
            "neptune",
            "--storage-uri",
            "bolt://remote.example:7687",
            "--allow-remote-db",
            "--yes",
            "--provider",
            "skip",
            "--non-interactive",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "no such graph backend is registered" in result.output
    assert "neptune" in result.output
    assert "super-secret-value" not in result.output
    assert list(home.glob("**/okto-neuron.yaml")) == []


def test_onboard_cli_written_base_url_is_visible_in_config_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "local",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "qwen-local",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    reset_state_for_tests()
    vault = Vault.open(vault_path)
    state = init_state(vault, vault.path)
    try:
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
            response = client.get("/api/v1/config")
    finally:
        reset_state_for_tests()
        vault.close()

    assert response.status_code == 200, response.text
    defaults = response.json()["llm"]["defaults"]
    assert defaults["provider"] == "openai"
    assert defaults["api_base"] == "http://127.0.0.1:9999/v1"
    assert defaults["model"] == "qwen-local"


def _force_interactive_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``CliRunner``'s captured stdin/stdout report ``isatty() is True``.

    ``onboard()``'s ``interactive`` flag is ``(not non_interactive) and
    sys.stdin.isatty() and sys.stdout.isatty()`` -- real terminal detection,
    not just "``--non-interactive`` was omitted". ``CliRunner.isolation``
    always swaps ``sys.stdin``/``sys.stdout`` for a fresh
    ``click.testing._NamedTextIOWrapper`` per invocation (a plain
    ``io.TextIOWrapper`` subclass whose ``isatty()`` is False), so patching
    an instance ahead of time doesn't reach the object actually used during
    ``invoke()``. Patching the class method here does, for every instance
    created for the life of this monkeypatch.
    """
    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


def test_onboard_interactive_backend_prompt_empty_input_keeps_grafx_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-onboard-C: interactive onboarding with no ``--backend`` flag prompts for a
    backend, printing the three one-line descriptions; pressing Enter on the
    prompt keeps the ``grafx`` default and needs no further backend-specific
    input before the vault-creation confirm (also answered with Enter).
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    _force_interactive_tty(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["onboard", "--vault", "alpha", "--provider", "skip"],
        input="\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Which graph backend should this vault use?" in result.output
    assert "grafx: default, embedded, by Okto Labs" in result.output
    assert "ladybug: embedded single file, the legacy default" in result.output
    assert "neo4j: external server, needs a bolt URI and a credential env var" in result.output
    vault_yaml = (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "storage:\n  backend: grafx\n  reason: null\n" in vault_yaml


def test_onboard_interactive_backend_prompt_ladybug_pins_ladybug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    _force_interactive_tty(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["onboard", "--vault", "alpha", "--provider", "skip"],
        input="ladybug\n\n",
    )

    assert result.exit_code == 0, result.output
    vault_yaml = (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "storage:\n  backend: ladybug\n  reason: null\n" in vault_yaml


def test_onboard_interactive_backend_prompt_neo4j_asks_uri_env_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picking ``neo4j`` at the interactive backend prompt asks for the storage
    URI (defaulting to the local bolt port), the credential env var name, and
    the database name (defaulting to ``neo4j``); a loopback URI needs no
    further remote-egress confirmation, so ``Vault.scaffold`` (which only
    writes ``okto-neuron.yaml`` -- no live Neo4j connection at creation time)
    completes and pins the entered values.
    """
    pytest.importorskip("neo4j")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    _force_interactive_tty(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["onboard", "--vault", "alpha", "--provider", "skip"],
        input="neo4j\nbolt://127.0.0.1:7688\nMARGINALIA_TEST_NEO4J_PASSWORD\nneo4jtest\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Neo4j storage URI" in result.output
    assert "Env var holding the Neo4j credential" in result.output
    assert "Neo4j database" in result.output
    vault_yaml = (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "backend: neo4j" in vault_yaml
    assert "uri: bolt://127.0.0.1:7688" in vault_yaml
    assert "credential_env: MARGINALIA_TEST_NEO4J_PASSWORD" in vault_yaml
    assert "database: neo4jtest" in vault_yaml


def test_onboard_noninteractive_backend_prompt_never_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactive onboarding must stay byte-identical to before D-onboard-C:
    no backend prompt, regardless of tty state, when ``--non-interactive`` is
    passed (or stdin/stdout are not a real terminal, the CliRunner default).
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "skip",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Which graph backend should this vault use?" not in result.output
    vault_yaml = (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "storage:\n  backend: grafx\n  reason: null\n" in vault_yaml


def test_onboard_explicit_backend_skips_interactive_prompt_even_when_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``--backend`` on an interactive session skips the new prompt
    entirely (``ctx.get_parameter_source`` detects explicitness, not a
    default-value comparison) -- no backend-choice input is needed, only the
    vault-creation confirm.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    _force_interactive_tty(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["onboard", "--vault", "alpha", "--provider", "skip", "--backend", "ladybug"],
        input="\n",
    )

    assert result.exit_code == 0, result.output
    assert "Which graph backend should this vault use?" not in result.output
    vault_yaml = (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")
    assert "storage:\n  backend: ladybug\n  reason: null\n" in vault_yaml


class _FakeOpenAIServer:
    """Loopback server that serves the OpenAI API ONLY under /v1 — the shape
    of llama.cpp / llama-swap / oMLX: ``/v1/models`` and
    ``/v1/chat/completions`` answer, while root-level paths (including
    ``/chat/completions``) are 404s. Used to replay the user's exact
    root-form-base repro hermetically."""

    def __init__(self, models: tuple[str, ...] = ("tiny-model",)) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            server_version = "FakeOpenAI/1.0"

            def log_message(self, *args: object) -> None:  # silence
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                if self.path == "/v1/models":
                    self._send(
                        200,
                        {"data": [{"id": m} for m in models]},
                    )
                else:
                    self._send(404, {"error": "404 page not found"})

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                if self.path == "/v1/chat/completions":
                    length = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(length)
                    self._send(
                        200,
                        {
                            "id": "chatcmpl-fake",
                            "object": "chat.completion",
                            "created": 0,
                            "model": models[0],
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "pong"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        },
                    )
                else:
                    self._send(404, {"error": "404 page not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_onboard_root_form_base_discovery_and_verify_derive_working_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's exact repro, hermetic: against a server that serves the
    OpenAI API only under /v1, the ROOT-form base (no /v1 — what a user
    naturally types) must work end to end: discovery derives
    ``{root}/v1/models``, the pre-save verify completes through
    ``{root}/v1/chat/completions``, and the persisted base is the canonical
    ``{root}/v1``. The /v1-form input must derive the identical values
    (idempotent normalization)."""
    pytest.importorskip("litellm")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    # The REAL pre-save verify — this test is its regression net.
    from okto_neuron.onboarding import (
        discover_models,
        get_provider_preset,
        verify_onboarding_completion as real_verify,
    )

    monkeypatch.setattr("okto_neuron.cli.verify_onboarding_completion", real_verify)

    server = _FakeOpenAIServer()
    server.start()
    try:
        root_base = f"http://127.0.0.1:{server.port}"

        # Discovery from the ROOT form derives the working /v1/models URL.
        discovered = discover_models(
            get_provider_preset("custom"),
            api_base=root_base,
        )
        assert discovered.error is None, discovered.error
        assert discovered.models == ["tiny-model"]
        assert discovered.endpoint == f"{root_base}/v1/models"
        # Same derivation from the /v1 form (idempotent).
        assert discover_models(
            get_provider_preset("custom"), api_base=f"{root_base}/v1"
        ).models == ["tiny-model"]

        # ROOT-form onboard: verify runs a real completion, then saves.
        result = CliRunner().invoke(
            app,
            [
                "onboard",
                "--vault",
                "alpha",
                "--provider",
                "custom",
                "--api-base",
                root_base,
                "--model",
                "tiny-model",
                "--non-interactive",
            ],
        )
        assert result.exit_code == 0, result.output
        cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
        assert cfg.llm.defaults.api_base == f"{root_base}/v1"
        assert cfg.llm.defaults.model == "tiny-model"

        # /v1-form onboard: identical persisted config (idempotent).
        result_v1 = CliRunner().invoke(
            app,
            [
                "onboard",
                "--vault",
                "gamma",
                "--provider",
                "custom",
                "--api-base",
                f"{root_base}/v1",
                "--model",
                "tiny-model",
                "--non-interactive",
            ],
        )
        assert result_v1.exit_code == 0, result_v1.output
        cfg_v1 = VaultConfig.load(home / ".okto-neuron" / "vaults" / "gamma")
        assert cfg_v1.llm.defaults.api_base == cfg.llm.defaults.api_base
    finally:
        server.stop()


def test_onboard_verify_failure_saves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead endpoint must abort onboarding with the exact attempted URL
    visible and persist NOTHING — no llm config block, no env secret."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    from okto_neuron.onboarding import verify_onboarding_completion as real_verify

    monkeypatch.setattr("okto_neuron.cli.verify_onboarding_completion", real_verify)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "custom",
            "--api-base",
            "http://127.0.0.1:1",  # dead endpoint
            "--model",
            "tiny-model",
            "--skip-model-discovery",
            "--non-interactive",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "verify failed" in result.output
    assert "nothing was saved" in result.output
    assert "http://127.0.0.1:1/v1/chat/completions" in result.output
    assert "Traceback" not in result.output
    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    # The vault itself may be created before verify; the LLM config must not
    # be.
    assert vault_path.is_dir()
    assert "llm" not in (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    assert not (home / ".okto-neuron" / "env").exists()


def test_onboard_noninteractive_without_model_does_not_pick_first_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oMLX repro guard: models[0] on a multi-model server can be a
    non-chat model (MarkItDown, first in sort order). A non-interactive run
    without --model must refuse — listing what it found — instead of silently
    making the first discovered model the default chat model."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    def fake_discover_models(preset, *, api_base=None, api_key=None, **_kwargs):
        return ModelDiscoveryResult(
            models=["MarkItDown", "Qwen3.8-27B"],
            endpoint=f"{api_base}/models",
        )

    monkeypatch.setattr("okto_neuron.cli.discover_models", fake_discover_models)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "custom",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--non-interactive",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "MarkItDown" in result.output
    assert "Qwen3.8-27B" in result.output
    assert "--model" in result.output
    assert "Traceback" not in result.output
    assert "llm" not in (
        home / ".okto-neuron" / "vaults" / "alpha" / "okto-neuron.yaml"
    ).read_text(encoding="utf-8")


def test_onboard_preset_default_model_still_used_noninteractively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preset's OWN declared default (an explicit, documented choice) is
    still usable non-interactively when it is actually served by the
    endpoint — only the silent models[0] fallback is removed."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    def fake_discover_models(preset, *, api_base=None, api_key=None, **_kwargs):
        return ModelDiscoveryResult(
            models=["MarkItDown", "local-model"],
            endpoint=f"{api_base}/models",
        )

    monkeypatch.setattr("okto_neuron.cli.discover_models", fake_discover_models)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "custom",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = VaultConfig.load(home / ".okto-neuron" / "vaults" / "alpha")
    assert cfg.llm.defaults.model == "local-model"


def test_onboard_unknown_openai_like_driver_points_at_custom_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_like is a LiteLLM driver, not an onboarding preset — the error
    must redirect to the custom OpenAI-compatible preset instead of leaving
    the user to guess from the preset list."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "onboard",
            "--vault",
            "alpha",
            "--provider",
            "openai_like",
            "--api-base",
            "http://127.0.0.1:9999/v1",
            "--model",
            "m",
            "--non-interactive",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "unknown provider preset 'openai_like'" in result.output
    assert "--provider custom" in result.output
    assert "LiteLLM gateway" in result.output
