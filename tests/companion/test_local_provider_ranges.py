"""P1 (user decision): private-network LLM endpoints count as LOCAL.

``_is_local_provider`` gates ``sensitivity=local_only``: a source may only
stay on the machine (or the operator's own network). Loopback alone was too
narrow — a LAN ollama/litellm box (e.g. http://192.168.31.222/v1) is just as
local for this purpose. Local means: no ``api_base`` at all (stub), or its
host is ``localhost``/loopback, a private RFC1918 address (10/8, 172.16/12,
192.168/16), an IPv6 unique-local (fc00::/7), or link-local (169.254/16,
fe80::/10). Anything else — public IPs, and every NON-IP hostname (a DNS
name can point anywhere) — is NOT local.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_neuron.companion import _is_local_provider


def _provider(api_base: str | None) -> object:
    # _is_local_provider is a pure attribute check by design; a namespace
    # carries exactly the attribute it reads.
    return SimpleNamespace(api_base=api_base)


@pytest.mark.parametrize(
    ("api_base", "why"),
    [
        ("http://127.0.0.1:11434/v1", "IPv4 loopback"),
        ("http://localhost:11434/v1", "the literal localhost"),
        ("http://[::1]:11434/v1", "IPv6 loopback"),
        ("http://10.0.0.7:8080/v1", "RFC1918 10/8"),
        ("http://10.255.255.255:8080/v1", "RFC1918 10/8 upper edge"),
        ("http://172.16.0.1:8080/v1", "RFC1918 172.16/12 lower edge"),
        ("http://172.31.255.254:8080/v1", "RFC1918 172.16/12 upper edge"),
        ("http://192.168.31.222/v1", "RFC1918 192.168/16 (the LAN box)"),
        ("http://[fd12:3456:789a::1]:8080/v1", "IPv6 ULA fc00::/7"),
        ("http://[fe80::1]:8080/v1", "IPv6 link-local"),
        ("http://169.254.8.8:8080/v1", "IPv4 link-local"),
    ],
)
def test_private_or_loopback_hosts_are_local(api_base: str, why: str) -> None:
    assert _is_local_provider(_provider(api_base)) is True, why


@pytest.mark.parametrize(
    ("api_base", "why"),
    [
        ("http://8.8.8.8/v1", "public IPv4"),
        ("http://172.32.0.1/v1", "just OUTSIDE 172.16/12"),
        ("http://203.0.113.7/v1", "TEST-NET is not RFC1918 (and not is_private-blanket local)"),
        ("http://[2001:4860:4860::8888]/v1", "public IPv6"),
        ("http://api.openai.com/v1", "a public DNS hostname"),
        ("http://my-llama-box.example.com/v1", "a hostname that only LOOKS local"),
    ],
)
def test_public_hosts_and_hostnames_are_not_local(api_base: str, why: str) -> None:
    assert _is_local_provider(_provider(api_base)) is False, why


def test_no_api_base_at_all_is_local() -> None:
    # The stub shape: no hosted endpoint whatsoever.
    assert _is_local_provider(_provider(None)) is True
    assert _is_local_provider(object()) is True


def test_real_litellm_provider_with_lan_base_is_local() -> None:
    """The gate reads the REAL provider class's api_base attribute too."""
    pytest.importorskip("litellm")
    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.llm import LiteLLMProvider

    provider = LiteLLMProvider(
        ResolvedLLM(
            provider="openai",
            api_base="http://192.168.31.222/v1",
            model="qwen3",
            api_key_env=None,
        )
    )
    assert _is_local_provider(provider) is True


def test_lan_llm_vault_accepts_local_only_through_the_queue(tmp_path, monkeypatch) -> None:
    """End to end through the ingest queue: a vault whose LLM api_base is the
    LAN box no longer trips the local_only gate — the worker's remember runs
    the production gate check (companion/__init__.py, ``sensitivity ==
    "local_only" and not _is_local_provider(self._get_provider())``) against
    a provider configured exactly like that vault's, and the job completes
    done/ok instead of erroring with the refusal."""
    import asyncio

    from okto_neuron.companion import CompanionError, RememberResult, _is_local_provider
    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.llm import LiteLLMProvider
    from okto_neuron.server import _ingest_queue as iq
    from okto_neuron.server._ingest_queue import IngestItem
    from types import SimpleNamespace

    lan_provider = LiteLLMProvider(
        ResolvedLLM(
            provider="openai",
            api_base="http://192.168.31.222/v1",
            model="qwen3",
            api_key_env=None,
        )
    ) if _litellm_available() else SimpleNamespace(api_base="http://192.168.31.222/v1")

    class _GateCheckingCompanion:
        """Runs the production local_only gate verbatim, then succeeds."""

        def __init__(self) -> None:
            self.sensitivities: list[str] = []

        def remember(
            self, _path, *, sensitivity="default", on_progress=None, **_kw
        ):  # type: ignore[no-untyped-def]
            self.sensitivities.append(sensitivity)
            if sensitivity == "local_only" and not _is_local_provider(lan_provider):
                raise CompanionError(
                    "local_only requires a local provider; this vault's LLM is remote"
                )
            return RememberResult(document_id="doc-lan", committed=1, blocks_total=1)

    state = SimpleNamespace(
        vault_path=tmp_path / "vault",
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_worker_task=None,
        ingest_cancel_requested=False,
        draining=False,
        shutting_down=False,
        writer_lock=asyncio.Lock(),
        note_ingest=None,
    )
    source = tmp_path / "lan-note.md"
    source.write_text("written on the LAN box\n", encoding="utf-8")
    companion = _GateCheckingCompanion()

    async def _run() -> None:
        iq.enqueue_materialized(state, source, sensitivity="local_only")
        iq.ensure_worker(state, lambda _s: companion)
        for _ in range(500):
            if state.ingest_queue and state.ingest_queue[0].status in {
                "done",
                "error",
                "cancelled",
            }:
                break
            await asyncio.sleep(0.01)

    asyncio.run(_run())
    item: IngestItem = state.ingest_queue[0]
    assert item.status == "done", item.error
    assert companion.sensitivities == ["local_only"]


def _litellm_available() -> bool:
    try:
        import litellm  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True
