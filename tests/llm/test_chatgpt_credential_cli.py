"""``okto-neuron provider login|status chatgpt`` and the helpers behind them.

The login itself cannot be completed in a test (it needs a human to authorize
a device code); what is pinned here is every guard in front of it and the
read-only status path. litellm's ``Authenticator`` is replaced so no test can
start a real flow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.llm import LLMProviderError, thinking_request_params
from okto_neuron.llm import _chatgpt


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


def _write_credential(token_dir: Path, *, expires_at: float = 4102444800) -> Path:
    token_dir.mkdir(parents=True, exist_ok=True)
    path = token_dir / "auth.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": expires_at,
                "id_token": _jwt(
                    {
                        "email": "someone@example.com",
                        "https://api.openai.com/auth": {
                            "chatgpt_plan_type": "pro",
                            "chatgpt_subscription_active_until": "2030-01-01T00:00:00+00:00",
                        },
                    }
                ),
            }
        )
    )
    return path


@pytest.fixture
def token_dir(monkeypatch, tmp_path) -> Path:
    target = tmp_path / "marginalia-chatgpt"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(target))
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    return target


class _FakeAuthenticator:
    calls = 0

    def get_access_token(self) -> str:
        type(self).calls += 1
        _write_credential(Path(os.environ["CHATGPT_TOKEN_DIR"]))
        return "a"


@pytest.fixture
def fake_login(monkeypatch):
    import litellm.llms.chatgpt.authenticator as authenticator

    _FakeAuthenticator.calls = 0
    monkeypatch.setattr(authenticator, "Authenticator", _FakeAuthenticator)
    return _FakeAuthenticator


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


# ── credential_summary: read-only identity report ──────────────────────


def test_summary_decodes_identity_and_expiry_without_writing(token_dir) -> None:
    path = _write_credential(token_dir, expires_at=4102444800000)  # milliseconds
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    summary = _chatgpt.credential_summary()
    assert summary["email"] == "someone@example.com"
    assert summary["plan"] == "pro"
    assert summary["subscription_active_until"] == "2030-01-01T00:00:00+00:00"
    assert summary["access_token_expires_at"] == "2100-01-01T00:00:00+00:00"
    assert summary["access_token_expired"] is False
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


def test_summary_flags_an_expired_access_token(token_dir) -> None:
    _write_credential(token_dir, expires_at=1_000_000)
    assert _chatgpt.credential_summary()["access_token_expired"] is True


def test_summary_refuses_a_missing_credential(token_dir) -> None:
    with pytest.raises(LLMProviderError, match="okto-neuron provider login chatgpt"):
        _chatgpt.credential_summary()


# ── foreign token dirs ─────────────────────────────────────────────────


@pytest.mark.parametrize("foreign", ["~/.codex", "~/.pi/agent", "~/.codex/sub"])
def test_codex_and_pi_login_dirs_are_refused(monkeypatch, tmp_path, foreign) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", foreign)
    with pytest.raises(LLMProviderError, match="own login directory"):
        _chatgpt.refuse_foreign_token_dir()


def test_a_symlink_into_codex_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / "sneaky").symlink_to(tmp_path / ".codex")
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "sneaky"))
    with pytest.raises(LLMProviderError):
        _chatgpt.refuse_foreign_token_dir()


def test_default_dir_is_accepted_and_resolved_against_this_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    _chatgpt.refuse_foreign_token_dir()
    assert _chatgpt.token_dir() == str(tmp_path / ".config" / "litellm" / "chatgpt")


# ── interactive_login guards ───────────────────────────────────────────


def test_login_refuses_without_a_terminal(token_dir, fake_login, monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(LLMProviderError, match="interactive"):
        _chatgpt.interactive_login()
    assert fake_login.calls == 0
    assert not token_dir.exists()


def test_login_refuses_codex_dir_before_anything_else(
    monkeypatch, tmp_path, fake_login, tty
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", "~/.codex")
    with pytest.raises(LLMProviderError, match="own login directory"):
        _chatgpt.interactive_login()
    assert fake_login.calls == 0


def test_login_runs_the_flow_when_nothing_is_stored(token_dir, fake_login, tty) -> None:
    summary = _chatgpt.interactive_login()
    assert fake_login.calls == 1
    assert summary["already_provisioned"] is False
    assert summary["plan"] == "pro"


def test_login_keeps_an_existing_credential_without_force(token_dir, fake_login, tty) -> None:
    path = _write_credential(token_dir)
    before = path.read_bytes()
    summary = _chatgpt.interactive_login()
    assert summary["already_provisioned"] is True
    assert fake_login.calls == 0
    assert path.read_bytes() == before


def test_force_keeps_the_old_credential_aside(token_dir, fake_login, tty) -> None:
    _write_credential(token_dir)
    _chatgpt.interactive_login(force=True)
    assert fake_login.calls == 1
    backups = list(token_dir.glob("auth.json.bak-*"))
    assert len(backups) == 1


def test_an_aborted_login_stub_is_not_mistaken_for_a_credential(token_dir, fake_login, tty) -> None:
    """Observed 2026-09-22: aborting the device flow leaves a small auth.json
    holding only litellm's cooldown record. The next login must run."""
    token_dir.mkdir(parents=True)
    (token_dir / "auth.json").write_text(json.dumps({"device_code_requested_at": 1}))
    summary = _chatgpt.interactive_login()
    assert fake_login.calls == 1
    assert summary["already_provisioned"] is False
    assert list(token_dir.glob("auth.json.bak-*")) == []


def test_a_login_inside_litellms_cooldown_is_refused_not_left_silent(
    token_dir, fake_login, tty
) -> None:
    """Observed 2026-09-22: a second attempt right after an aborted one
    printed nothing, because litellm polls quietly for the rest of its
    five-minute device-code window."""
    import time

    token_dir.mkdir(parents=True)
    stub = token_dir / "auth.json"
    stub.write_text(json.dumps({"device_code_requested_at": time.time() - 30}))
    before = stub.read_bytes()
    with pytest.raises(LLMProviderError, match=r"retry in \d+s"):
        _chatgpt.interactive_login()
    assert fake_login.calls == 0
    assert stub.read_bytes() == before


# ── CLI ────────────────────────────────────────────────────────────────


def test_status_reports_without_touching_the_file(token_dir, monkeypatch) -> None:
    path = _write_credential(token_dir)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.delenv("OKTO_NEURON_ENABLE_CHATGPT", raising=False)
    result = CliRunner().invoke(app, ["provider", "status", "chatgpt", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["plan"] == "pro"
    assert payload["opt_in_enabled"] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_status_exits_nonzero_on_a_missing_credential(token_dir) -> None:
    result = CliRunner().invoke(app, ["provider", "status", "chatgpt", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "okto-neuron provider login chatgpt" in payload["error"]
    assert not token_dir.exists()


def test_status_text_mode_says_whether_the_opt_in_is_set(token_dir, monkeypatch) -> None:
    _write_credential(token_dir)
    monkeypatch.setenv("OKTO_NEURON_ENABLE_CHATGPT", "1")
    result = CliRunner().invoke(app, ["provider", "status", "chatgpt"])
    assert result.exit_code == 0, result.output
    assert "OKTO_NEURON_ENABLE_CHATGPT: set" in result.output


def test_login_cli_refuses_under_a_non_terminal(token_dir, fake_login) -> None:
    # CliRunner's stdin is never a tty, which is exactly the unattended case.
    result = CliRunner().invoke(app, ["provider", "login", "chatgpt"])
    assert result.exit_code == 1
    assert "interactive" in result.output
    assert fake_login.calls == 0


# ── derived thinking params (the provenance source of truth) ───────────


def test_thinking_off_derives_reasoning_effort_none_where_advertised() -> None:
    params, dropped = thinking_request_params(
        provider="chatgpt",
        enable_thinking=False,
        supported_openai_params=frozenset({"reasoning_effort"}),
        provider_config_params=frozenset(),
    )
    assert (params, dropped) == ({"reasoning_effort": "none"}, None)


def test_thinking_on_sends_nothing_without_a_thinking_param() -> None:
    params, dropped = thinking_request_params(
        provider="chatgpt",
        enable_thinking=True,
        supported_openai_params=frozenset({"reasoning_effort"}),
        provider_config_params=frozenset(),
    )
    assert (params, dropped) == ({}, None)


def test_a_thinking_block_wins_where_supported() -> None:
    params, _ = thinking_request_params(
        provider="openai",
        enable_thinking=False,
        supported_openai_params=frozenset({"thinking", "reasoning_effort"}),
        provider_config_params=frozenset(),
    )
    assert set(params) == {"thinking"}


def test_anthropic_thinking_on_is_dropped_with_a_reason() -> None:
    params, dropped = thinking_request_params(
        provider="anthropic",
        enable_thinking=True,
        supported_openai_params=frozenset({"thinking"}),
        provider_config_params=frozenset(),
    )
    assert params == {}
    assert dropped == "provider-requires-budget-tokens-and-temperature-constraints"


def test_non_boolean_is_dropped() -> None:
    assert thinking_request_params(
        provider="openai",
        enable_thinking="false",
        supported_openai_params=frozenset({"reasoning_effort"}),
        provider_config_params=frozenset(),
    ) == ({}, "invalid-boolean-value")
