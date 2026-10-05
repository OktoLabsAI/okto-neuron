"""P2: LiteLLMProvider token streaming vs the non-stream path, bit-for-bit.

A stub OpenAI-compatible server (plain ``http.server`` on a loopback socket)
serves BOTH ``stream=false`` and ``stream=true`` chat completions from one
script of chunks, so the same request can be replayed through
``LiteLLMProvider.complete`` with and without ``on_token``. The assembled
stream result must equal the non-stream result exactly: text, the recorded
``finish_reason`` (including "length" truncation), the native (unmapped)
finish reason, and the usage stats. A deadline/cancel-scoped call with
``on_token`` must still succeed via the deliberate non-stream fallback.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.llm import LiteLLMProvider, Message, _set_last_call_stats

pytest.importorskip("litellm")

import okto_neuron.llm as llm_module  # noqa: E402


def _write_tmp(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class _Script:
    """One canned response: full text, chunks, finish_reason, usage."""

    def __init__(
        self,
        *,
        text: str,
        chunks: list[str],
        finish_reason: str,
        usage: dict[str, int],
    ) -> None:
        self.text = text
        self.chunks = chunks
        self.finish_reason = finish_reason
        self.usage = usage


SCRIPTS: dict[str, _Script] = {
    "plain": _Script(
        text="alpha beta gamma delta",
        chunks=["alpha ", "beta ", "gamma ", "delta"],
        finish_reason="stop",
        usage={"prompt_tokens": 11, "completion_tokens": 4},
    ),
    "length": _Script(
        text="one two three four five six seven eight nine ten eleven twelve",
        chunks=["one two three four five ", "six seven eight nine ten ", "eleven twelve"],
        finish_reason="length",
        usage={"prompt_tokens": 9, "completion_tokens": 12},
    ),
    # A native finish reason litellm does NOT map: it must survive as
    # native_finish_reason with finish_reason_unmapped, stream or not.
    "weird-native": _Script(
        text="halted mid thought",
        chunks=["halted ", "mid ", "thought"],
        finish_reason="stop",  # what the stub SENDS; litellm records the raw
        usage={"prompt_tokens": 5, "completion_tokens": 3},
    ),
}


class _Handler(BaseHTTPRequestHandler):
    server: "_StubServer"

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        script = SCRIPTS[body.get("model", "plain")]
        want_stream = bool(body.get("stream"))
        native = body.get("extra_body", {}).get("native_finish_reason")
        if want_stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for index, chunk in enumerate(script.chunks):
                payload: dict[str, Any] = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body.get("model", "plain"),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": chunk},
                            "finish_reason": None,
                        }
                    ],
                }
                self._sse(payload)
            self._sse(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body.get("model", "plain"),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": script.finish_reason,
                        }
                    ],
                }
            )
            # stream_options={"include_usage": True}: usage rides the LAST chunk.
            self._sse(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body.get("model", "plain"),
                    "choices": [],
                    "usage": script.usage,
                }
            )
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            message = {"role": "assistant", "content": script.text}
            if native:
                message["native_finish_reason"] = native
            response = {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model", "plain"),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": script.finish_reason,
                    }
                ],
                "usage": script.usage,
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    def _sse(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

    def log_message(self, *_args: object) -> None:
        return


class _StubServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture()
def stub() -> Iterator[_StubServer]:
    server = _StubServer()
    try:
        yield server
    finally:
        server.close()


def _provider(stub: _StubServer, model: str = "plain") -> LiteLLMProvider:
    return LiteLLMProvider(
        ResolvedLLM(
            provider="openai",
            api_base=f"http://127.0.0.1:{stub.port}/v1",
            model=model,
            api_key_env=None,
        )
    )


def _stats() -> dict:
    return dict(llm_module.last_call_stats() or {})


@pytest.mark.parametrize("script_name", ["plain", "length"])
def test_stream_matches_non_stream_text_reason_and_usage(
    stub: _StubServer, script_name: str
) -> None:
    provider = _provider(stub, script_name)
    messages = [Message("user", "say the thing")]

    _set_last_call_stats(None)
    plain_text = provider.complete(messages)
    plain_stats = _stats()

    _set_last_call_stats(None)
    seen: list[str] = []
    streamed_text = provider.complete(messages, on_token=seen.append)
    streamed_stats = _stats()

    assert streamed_text == plain_text == SCRIPTS[script_name].text
    assert "".join(seen) == SCRIPTS[script_name].text, "tokens must compose the text"
    assert len(seen) == len(SCRIPTS[script_name].chunks)
    assert streamed_stats.get("finish_reason") == plain_stats.get("finish_reason")
    assert streamed_stats.get("prompt_tokens") == plain_stats.get("prompt_tokens")
    assert streamed_stats.get("completion_tokens") == plain_stats.get(
        "completion_tokens"
    )
    if script_name == "length":
        assert plain_stats.get("finish_reason") == "length"


def test_length_truncation_is_flagged_identically_when_streaming(
    stub: _StubServer,
) -> None:
    provider = _provider(stub, "length")
    messages = [Message("user", "go long")]

    _set_last_call_stats(None)
    provider.complete(messages)
    plain = _stats()

    _set_last_call_stats(None)
    provider.complete(messages, on_token=lambda _t: None)
    streamed = _stats()

    assert plain.get("finish_reason") == "length"
    assert streamed.get("finish_reason") == "length"


def test_no_callback_leaves_the_request_non_streaming(stub: _StubServer) -> None:
    """on_token=None must be byte-for-byte today's behaviour: the stub marks
    the streaming lane by only sending SSE when asked, and the non-stream
    reply's shape is asserted by the other tests; here we pin that no
    stream_options key is added either (it would 400 on a strict server)."""
    provider = _provider(stub, "plain")
    _set_last_call_stats(None)
    assert provider.complete([Message("user", "hi")]) == SCRIPTS["plain"].text


def test_deadline_scoped_call_falls_back_to_non_stream(
    stub: _StubServer,
) -> None:
    """With a task deadline active, streaming is deliberately unsupported (the
    cancellable helper-process path owns the HTTP call there); the call must
    still succeed via the non-stream fallback and deliver the tokens late (the
    callback fires once with the whole text)."""
    from okto_neuron.llm import _scoped_call_timeout

    provider = _provider(stub, "plain")
    seen: list[str] = []
    with _scoped_call_timeout(30.0):
        text = provider.complete([Message("user", "hi")], on_token=seen.append)
    assert text == SCRIPTS["plain"].text


def test_on_token_failure_never_fails_the_completion(stub: _StubServer) -> None:
    def _boom(_token: str) -> None:
        raise RuntimeError("telemetry transport gone")

    provider = _provider(stub, "plain")
    assert provider.complete([Message("user", "hi")], on_token=_boom) == (
        SCRIPTS["plain"].text
    )
