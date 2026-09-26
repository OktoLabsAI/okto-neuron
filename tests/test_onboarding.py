from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import threading
from urllib.error import HTTPError

import pytest

from okto_neuron import onboarding as onboarding_module
from okto_neuron.onboarding import (
    default_api_key_env,
    discover_models,
    get_provider_preset,
    load_user_env_file,
    llm_config_patch,
    write_user_env_secret,
    _discover_codex_cli_models,
    _discover_pi_cli_models,
)
from okto_neuron.config._vault import LLMConfig, LLMDefaults, _check_api_base


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


def test_discover_openai_compatible_models_parses_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(req, *, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["timeout"] = timeout
        return _Response({"data": [{"id": "z-model"}, {"id": "a-model"}]})

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)

    result = discover_models(
        get_provider_preset("local"),
        api_base="http://127.0.0.1:9999/v1",
        api_key="sk-test",
        timeout=1.5,
    )

    assert result.error is None
    assert result.models == ["a-model", "z-model"]
    assert seen == {
        "url": "http://127.0.0.1:9999/v1/models",
        "auth": "Bearer sk-test",
        "timeout": 1.5,
    }


def test_legacy_local_preset_carries_no_hardcoded_model() -> None:
    """0.0.48 companion item: the hidden legacy-local preset must not
    preselect a model. The flow's existing discovery step (pick-list from the
    provider's model endpoint, manual entry fallback) owns model selection, so
    defaults can never claim a model the endpoint may not serve."""
    preset = get_provider_preset("local")

    assert preset.menu is False
    assert preset.default_model == ""
    assert "Qwen3.6-35B-A3B-oQ4-fp16-mtp" not in (
        " ".join(str(part) for part in (preset.key, preset.label, preset.default_model, preset.note))
    )


def test_discover_gemini_models_filters_to_generate_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "okto_neuron.config._vault._resolve_host_addresses",
        lambda _host, _port: [ipaddress.ip_address("8.8.8.8")],
    )

    def fake_urlopen(req, *, timeout):
        del timeout
        assert "key=gemini-key" in req.full_url
        return _Response(
            {
                "models": [
                    {
                        "name": "models/gemini-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embed-only",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            }
        )

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)

    result = discover_models(
        get_provider_preset("gemini"),
        api_key="gemini-key",
        allow_remote=True,
        remote_confirmed=True,
    )

    assert result.error is None
    assert result.models == ["gemini-pro"]


def test_discover_remote_endpoint_is_gated_before_key_is_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    resolved = False

    def fake_urlopen(req, *, timeout):
        del req, timeout
        nonlocal called
        called = True
        return _Response({"data": [{"id": "leaked"}]})

    def fake_resolve(_host, _port):
        nonlocal resolved
        resolved = True
        return [ipaddress.ip_address("8.8.8.8")]

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)
    monkeypatch.setattr("okto_neuron.config._vault._resolve_host_addresses", fake_resolve)

    result = discover_models(
        get_provider_preset("custom"),
        api_base="https://example.com/v1",
        api_key="sk-real",
        allow_remote=False,
        remote_confirmed=False,
    )

    assert result.models == []
    assert "remote endpoint requires explicit confirmation" in (result.error or "")
    assert called is False
    assert resolved is False


def test_discover_rechecks_dns_and_rejects_dangerous_resolution_before_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_urlopen(req, *, timeout):
        del req, timeout
        nonlocal called
        called = True
        return _Response({"data": [{"id": "leaked"}]})

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)
    monkeypatch.setattr(
        "okto_neuron.config._vault._resolve_host_addresses",
        lambda _host, _port: [ipaddress.ip_address("169.254.169.254")],
    )

    result = discover_models(
        get_provider_preset("custom"),
        api_base="https://models.example.test/v1",
        api_key="sk-real",
        allow_remote=True,
        remote_confirmed=True,
    )

    assert result.models == []
    assert "disallowed address" in (result.error or "")
    assert called is False


def test_discovery_redirects_are_not_followed() -> None:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/v1/models":
                self.send_response(302)
                self.send_header("Location", "/v1/redirected-models")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "redirected"}]}).encode())

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        result = discover_models(
            get_provider_preset("custom"),
            api_base=f"http://{host}:{port}/v1",
            timeout=1.0,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.models == []
    assert result.error == "HTTP 302"
    assert seen == ["/v1/models"]


def test_litellm_proxy_model_info_discovery_uses_bare_proxy_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_urlopen(req, *, timeout):
        del timeout
        seen.append(req.full_url)
        return _Response({"data": [{"model_name": "proxy/model-a"}]})

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)

    result = discover_models(get_provider_preset("litellm_proxy"), timeout=1.0)

    assert result.error is None
    assert result.models == ["proxy/model-a"]
    assert seen == ["http://127.0.0.1:4000/v1/model/info"]


def test_litellm_proxy_discovery_falls_back_to_model_info_without_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_urlopen(req, *, timeout):
        del timeout
        seen.append(req.full_url)
        if req.full_url.endswith("/v1/model/info"):
            raise HTTPError(req.full_url, 404, "not found", {}, None)
        return _Response({"data": [{"model_name": "proxy/fallback-model"}]})

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)

    result = discover_models(get_provider_preset("litellm_proxy"), timeout=1.0)

    assert result.error is None
    assert result.models == ["proxy/fallback-model"]
    assert seen == [
        "http://127.0.0.1:4000/v1/model/info",
        "http://127.0.0.1:4000/model/info",
    ]


def test_litellm_proxy_discovery_falls_back_to_openai_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_urlopen(req, *, timeout):
        del timeout
        seen.append(req.full_url)
        if not req.full_url.endswith("/v1/models"):
            raise HTTPError(req.full_url, 404, "not found", {}, None)
        return _Response({"data": [{"id": "proxy/openai-style"}]})

    monkeypatch.setattr("okto_neuron.onboarding._urlopen_no_redirect", fake_urlopen)

    result = discover_models(get_provider_preset("litellm_proxy"), timeout=1.0)

    assert result.error is None
    assert result.models == ["proxy/openai-style"]
    assert seen == [
        "http://127.0.0.1:4000/v1/model/info",
        "http://127.0.0.1:4000/model/info",
        "http://127.0.0.1:4000/v1/models",
    ]


def test_api_base_rejects_metadata_and_encoded_ip_even_when_remote_allowed() -> None:
    with pytest.raises(ValueError, match="disallowed address"):
        _check_api_base("http://169.254.169.254/v1", allow_remote=True)
    with pytest.raises(ValueError, match="disallowed address"):
        _check_api_base("http://169.254.0.1/v1", allow_remote=True)
    with pytest.raises(ValueError, match="disallowed address"):
        _check_api_base("http://0.0.0.0/v1", allow_remote=True)
    with pytest.raises(ValueError, match="disallowed address"):
        _check_api_base("http://[::]/v1", allow_remote=True)
    with pytest.raises(ValueError, match="disallowed address"):
        _check_api_base("http://[::ffff:169.254.169.254]/v1", allow_remote=True)
    with pytest.raises(ValueError, match="encoded IP"):
        _check_api_base("http://0x7f000001/v1", allow_remote=True)
    with pytest.raises(ValueError, match="encoded IP"):
        _check_api_base("http://2130706433/v1", allow_remote=True)
    with pytest.raises(ValueError, match="encoded IP"):
        _check_api_base("https://0177.0.0.1/v1", allow_remote=True)
    with pytest.raises(ValueError, match="encoded IP"):
        _check_api_base("http://0300.0250.0000.0001/v1", allow_remote=True)


def test_api_base_rejects_userinfo_even_for_loopback() -> None:
    with pytest.raises(ValueError, match="username or password"):
        _check_api_base("http://user:pass@127.0.0.1:1234/v1", allow_remote=True)


def test_api_base_rejects_public_http_but_allows_private_http_after_opt_in() -> None:
    with pytest.raises(ValueError, match="public LLM endpoints must use https"):
        _check_api_base("http://8.8.8.8/v1", allow_remote=True)
    _check_api_base("http://10.0.0.9:4000/v1", allow_remote=True)


def test_api_base_allows_private_http_hostname_only_after_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "okto_neuron.config._vault._resolve_host_addresses",
        lambda _host, _port: [ipaddress.ip_address("192.168.1.10")],
    )

    with pytest.raises(ValueError, match="not loopback"):
        _check_api_base("http://llm.internal.example:8080/v1", allow_remote=False)
    _check_api_base("http://llm.internal.example:8080/v1", allow_remote=True)


def test_api_base_ipv4_mapped_private_address_requires_remote_opt_in() -> None:
    with pytest.raises(ValueError, match="not loopback"):
        _check_api_base("http://[::ffff:192.168.1.10]:4000/v1", allow_remote=False)
    _check_api_base("http://[::ffff:192.168.1.10]:4000/v1", allow_remote=True)


def test_llm_disabled_skips_api_base_validation() -> None:
    cfg = LLMConfig(
        enabled=False,
        defaults=LLMDefaults(api_base="http://169.254.169.254/v1"),
    )
    assert cfg.enabled is False


def test_load_user_env_file_does_not_override_existing_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "env"
    write_user_env_secret("OKTO_NEURON_TEST_KEY", "from-file", path=env_path)
    monkeypatch.setenv("OKTO_NEURON_TEST_KEY", "from-process")

    load_user_env_file(env_path)

    assert os.environ["OKTO_NEURON_TEST_KEY"] == "from-process"


def test_write_user_env_secret_replaces_duplicate_key_and_locks_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OKTO_NEURON_TEST_KEY", raising=False)
    env_path = tmp_path / "marginalia" / "env"
    env_path.parent.mkdir()
    env_path.write_text(
        "\n".join(
            [
                "# keep comment",
                "OKTO_NEURON_TEST_KEY=old-one",
                "OKTO_NEURON_OTHER_KEY=other",
                "export OKTO_NEURON_TEST_KEY=old-two",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    write_user_env_secret("OKTO_NEURON_TEST_KEY", "new secret", path=env_path)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    if os.name == "nt":
        assignment = next(line for line in lines if line.startswith("OKTO_NEURON_TEST_KEY="))
        assert assignment.startswith("OKTO_NEURON_TEST_KEY=dpapi-v1:")
        assert "new secret" not in assignment
    else:
        assert lines.count("OKTO_NEURON_TEST_KEY='new secret'") == 1
    assert not any("old-one" in line or "old-two" in line for line in lines)
    assert "OKTO_NEURON_OTHER_KEY=other" in lines
    if os.name != "nt":
        assert (env_path.stat().st_mode & 0o777) == 0o600
        assert (env_path.parent.stat().st_mode & 0o777) == 0o700


def _install_fake_windows_secret_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onboarding_module,
        "_windows_secret_protection_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        onboarding_module,
        "_protect_windows_secret",
        lambda payload: bytes(character ^ 0xA5 for character in payload),
    )
    monkeypatch.setattr(
        onboarding_module,
        "_unprotect_windows_secret",
        lambda payload: bytes(character ^ 0xA5 for character in payload),
    )


def test_windows_managed_secret_codec_protects_rotates_and_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_windows_secret_codec(monkeypatch)
    env_name = "OKTO_NEURON_PROVIDER_TEST_API_KEY"
    env_path = tmp_path / "windows-env"
    first = "first managed secret"
    second = "second managed secret"
    monkeypatch.delenv(env_name, raising=False)

    write_user_env_secret(env_name, first, path=env_path)

    stored = env_path.read_text(encoding="utf-8")
    assert stored.count(f"{env_name}=") == 1
    assert f"{env_name}=dpapi-v1:" in stored
    assert first not in stored
    assert os.environ[env_name] == first

    monkeypatch.delenv(env_name, raising=False)
    load_user_env_file(env_path)
    assert os.environ[env_name] == first

    write_user_env_secret(env_name, second, path=env_path)
    stored = env_path.read_text(encoding="utf-8")
    assert stored.count(f"{env_name}=") == 1
    assert first not in stored
    assert second not in stored

    monkeypatch.delenv(env_name, raising=False)
    load_user_env_file(env_path)
    assert os.environ[env_name] == second


@pytest.mark.parametrize("secret", ["line-one\nline-two", "bad\rvalue", "bad\x00value"])
def test_windows_managed_secret_codec_rejects_injection_before_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
) -> None:
    _install_fake_windows_secret_codec(monkeypatch)
    monkeypatch.setattr(
        onboarding_module,
        "_protect_windows_secret",
        lambda _payload: pytest.fail("unsafe input reached the credential protector"),
    )
    env_path = tmp_path / "windows-env"

    with pytest.raises(ValueError):
        write_user_env_secret(
            "OKTO_NEURON_PROVIDER_TEST_API_KEY",
            secret,
            path=env_path,
        )

    assert not env_path.exists()


def test_default_api_key_env_separates_standard_and_custom_endpoints() -> None:
    standard = default_api_key_env("openai", "https://api.openai.com/v1")
    custom_a = default_api_key_env("openai", "https://api.example.test/v1/tenant-a")
    custom_b = default_api_key_env("openai", "https://api.example.test/v1/tenant-b")

    assert standard == "OKTO_NEURON_PROVIDER_OPENAI_API_KEY"
    assert custom_a.startswith("OKTO_NEURON_PROVIDER_OPENAI_API_EXAMPLE_TEST_")
    assert custom_a.endswith("_API_KEY")
    assert len({standard, custom_a, custom_b}) == 3


@pytest.mark.parametrize("secret", ["", "line-one\nline-two", "bad\rvalue", "bad\x00value"])
def test_write_user_env_secret_rejects_unsafe_values(tmp_path: Path, secret: str) -> None:
    env_path = tmp_path / "env"

    with pytest.raises(ValueError):
        write_user_env_secret("OKTO_NEURON_PROVIDER_TEST_API_KEY", secret, path=env_path)

    assert not env_path.exists()


# ---------------------------------------------------------------------------
# pi_cli onboarding tests
# ---------------------------------------------------------------------------


def test_discover_pi_cli_models_parses_normal_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal well-formed table with all columns present."""

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = (
                "provider      model                                context  max-out  thinking  images\n"
                "anthropic     claude-3-5-sonnet-20240620           200K     8.2K     no        yes   \n"
                "anthropic     claude-3-sonnet-20240229             4.1K     no        yes   \n"
                "google        gemini-2.5-pro                       1.0M     65.5K    yes       yes   \n"
            )
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.error is None
    assert result.models == [
        "anthropic/claude-3-5-sonnet-20240620",
        "anthropic/claude-3-sonnet-20240229",
        "google/gemini-2.5-pro",
    ]
    assert result.endpoint == "pi --list-models"


def test_discover_pi_cli_models_parses_ragged_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ragged-row example from the spec: claude-3-sonnet missing a column."""

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = (
                "provider      model                                context  max-out  thinking  images\n"
                "anthropic     claude-3-5-sonnet-20240620           200K     8.2K     no        yes   \n"
                "anthropic     claude-3-5-sonnet-20241022           200K     8.2K     no        yes   \n"
                "anthropic     claude-3-sonnet-20240229             4.1K     no        yes   \n"
                "Desktop       qwen3.6-27b-high                     131.1K   32.8K    yes       no    \n"
                "google        gemini-2.5-pro                       1.0M     65.5K    yes       yes   \n"
            )
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.error is None
    assert "anthropic/claude-3-sonnet-20240229" in result.models
    assert "Desktop/qwen3.6-27b-high" in result.models
    assert "google/gemini-2.5-pro" in result.models
    assert len(result.models) == 5


def test_discover_pi_cli_models_skips_truncation_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines with '...' as provider or model should be skipped."""

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = (
                "provider      model                                context  max-out  thinking  images\n"
                "anthropic     claude-3-5-sonnet-20240620           200K     8.2K     no        yes   \n"
                "...           ...                                    ...      ...      ...       ...   \n"
                "google        gemini-2.5-pro                       1.0M     65.5K    yes       yes   \n"
            )
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.error is None
    assert result.models == [
        "anthropic/claude-3-5-sonnet-20240620",
        "google/gemini-2.5-pro",
    ]


def test_discover_pi_cli_models_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When 'pi' is not on PATH, return an error result."""

    def fake_which(name: str) -> str | None:
        return None

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.models == []
    assert result.error == "pi CLI not found on PATH"


def test_discover_pi_cli_models_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pi --list-models times out, return an error result."""
    import subprocess as sub

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        raise sub.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.models == []
    assert result.error == "pi --list-models timed out"


def test_discover_pi_cli_models_subprocess_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pi --list-models exits non-zero, return stderr as error."""

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "pi: command failed\n"

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_pi_cli_models(timeout=10.0)

    assert result.models == []
    assert "command failed" in (result.error or "")


def test_discover_models_bypasses_http_path_for_pi_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover_models() should call _discover_pi_cli_models directly for pi_cli,
    bypassing _discovery_base_for / _validate_discovery_target."""

    called_http = False

    def fake_discovery_base(*args, **kwargs):
        nonlocal called_http
        called_http = True
        return "http://should-not-be-called"

    def fake_validate(*args, **kwargs):
        nonlocal called_http
        called_http = True
        return "should not be called"

    def fake_which(name: str) -> str | None:
        if name == "pi":
            return "/usr/local/bin/pi"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = (
                "provider      model                                context  max-out  thinking  images\n"
                "anthropic     claude-haiku-4-5                     200K     64K      yes       yes   \n"
            )
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding._discovery_base_for", fake_discovery_base)
    monkeypatch.setattr("okto_neuron.onboarding._validate_discovery_target", fake_validate)
    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = discover_models(get_provider_preset("pi_cli"))

    assert called_http is False
    assert result.error is None
    assert result.models == ["anthropic/claude-haiku-4-5"]


def test_discover_codex_cli_models_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When 'codex' is not on PATH, return an error result."""

    def fake_which(name: str) -> str | None:
        return None

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)

    result = _discover_codex_cli_models()

    assert result.models == []
    assert result.error == "codex CLI not found on PATH"


def test_discover_codex_cli_models_never_returns_provider_endpoints_as_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: [model_providers.*] config.toml keys (e.g. 'omlx') are provider
    *endpoint* configs, not model ids — live-verified passing one as -m fails.
    A working binary must always report an empty model list, never those keys."""

    def fake_which(name: str) -> str | None:
        if name == "codex":
            return "/usr/local/bin/codex"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = "codex-cli 0.142.5\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_codex_cli_models()

    assert result.models == []
    assert result.error is None


def test_discover_codex_cli_models_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sub

    def fake_which(name: str) -> str | None:
        if name == "codex":
            return "/usr/local/bin/codex"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        raise sub.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_codex_cli_models()

    assert result.models == []
    assert result.error == "codex --version timed out"


def test_discover_codex_cli_models_subprocess_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        if name == "codex":
            return "/usr/local/bin/codex"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "codex: command failed\n"

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = _discover_codex_cli_models()

    assert result.models == []
    assert "command failed" in (result.error or "")


def test_discover_models_bypasses_http_path_for_codex_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover_models() should call _discover_codex_cli_models directly for
    codex_cli, bypassing _discovery_base_for / _validate_discovery_target."""

    called_http = False

    def fake_discovery_base(*args, **kwargs):
        nonlocal called_http
        called_http = True
        return "http://should-not-be-called"

    def fake_validate(*args, **kwargs):
        nonlocal called_http
        called_http = True
        return "should not be called"

    def fake_which(name: str) -> str | None:
        if name == "codex":
            return "/usr/local/bin/codex"
        return None

    def fake_run(cmd, *, capture_output, text, timeout):
        class Proc:
            returncode = 0
            stdout = "codex-cli 0.142.5\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr("okto_neuron.onboarding._discovery_base_for", fake_discovery_base)
    monkeypatch.setattr("okto_neuron.onboarding._validate_discovery_target", fake_validate)
    monkeypatch.setattr("okto_neuron.onboarding.shutil.which", fake_which)
    monkeypatch.setattr("okto_neuron.onboarding.subprocess.run", fake_run)

    result = discover_models(get_provider_preset("codex_cli"))

    assert called_http is False
    assert result.error is None
    assert result.models == []


def test_llm_config_patch_pi_cli_no_api_base() -> None:
    """llm_config_patch() should produce a valid patch with no api_base for pi_cli."""

    preset = get_provider_preset("pi_cli")
    patch = llm_config_patch(
        preset,
        model="anthropic/claude-haiku-4-5",
        api_base=None,
        api_key_env=None,
    )

    assert patch["llm"]["enabled"] is True
    assert patch["llm"]["allow_remote"] is False
    defaults = patch["llm"]["defaults"]
    assert defaults["provider"] == "pi_cli"
    assert defaults["model"] == "anthropic/claude-haiku-4-5"
    assert "api_base" not in defaults
    assert defaults["api_key_env"] is None


def test_llm_config_patch_existing_presets_unchanged() -> None:
    """Verify existing presets still produce api_base in the patch."""

    # lm_studio (local, no key required)
    preset = get_provider_preset("lm_studio")
    patch = llm_config_patch(
        preset,
        model="local-model",
        api_base=None,
        api_key_env=None,
    )
    assert patch["llm"]["defaults"]["api_base"] == "http://127.0.0.1:1234/v1"
    assert patch["llm"]["allow_remote"] is False

    # openai (hosted)
    preset = get_provider_preset("openai")
    patch = llm_config_patch(
        preset,
        model="gpt-4.1-mini",
        api_base=None,
        api_key_env="OKTO_NEURON_OPENAI_API_KEY",
        allow_remote=True,
    )
    assert patch["llm"]["defaults"]["api_base"] == "https://api.openai.com/v1"
    assert patch["llm"]["allow_remote"] is True


def test_pi_cli_preset_in_menu() -> None:
    """The pi_cli preset should appear in MENU_PRESETS."""
    from okto_neuron.onboarding import MENU_PRESETS

    keys = {p.key for p in MENU_PRESETS}
    assert "pi_cli" in keys


def test_pi_cli_preset_properties() -> None:
    """Verify pi_cli preset has the expected properties."""
    preset = get_provider_preset("pi_cli")
    assert preset.key == "pi_cli"
    assert preset.provider == "pi_cli"
    assert preset.api_base == ""
    assert preset.api_key_env == ""
    assert preset.hosted is False
    assert preset.discovery == "pi_cli"
    assert preset.api_key_required is False
    assert preset.menu is True


def test_onboard_cli_reports_unresolvable_api_base_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for issue #6.

    ``okto-neuron onboard --api-base <unresolvable-host>`` must surface a clean
    click.ClickException (exit code != 0, message mentions the endpoint) instead
    of letting the error bubble up as a raw traceback. Since the base-URL fix,
    the failure surfaces EARLIER and stricter than before: the pre-save verify
    step cannot reach the host, so onboarding aborts with the attempted URL
    visible — and nothing is saved (previously the same host only failed at
    config-write time, deep in llm_config_patch -> _check_api_base).
    """

    from click.testing import CliRunner

    from okto_neuron.cli import app

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    def fake_resolve(_host, _port):
        raise ValueError("llm api_base host 'no-such-host.internal' did not resolve")

    monkeypatch.setattr("okto_neuron.config._vault._resolve_host_addresses", fake_resolve)

    vault_path = tmp_path / "vault"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            str(vault_path),
            "--provider",
            "custom",
            "--api-base",
            "http://no-such-host.internal:1234/v1",
            "--allow-remote-llm",
            "--yes",
            "--skip-model-discovery",
            "--model",
            "m",
            "--non-interactive",
        ],
    )

    assert result.exit_code != 0
    assert "verify failed" in result.output
    assert "nothing was saved" in result.output
    # The attempted URL makes the unresolvable host visible to the user.
    assert "no-such-host.internal" in result.output
    assert "Traceback" not in result.output
    # Nothing was persisted: the vault exists (created before verify) but
    # carries no llm block.
    vault_yaml = vault_path / "okto-neuron.yaml"
    if vault_yaml.exists():
        assert "llm" not in vault_yaml.read_text(encoding="utf-8")
