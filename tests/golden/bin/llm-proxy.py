#!/usr/bin/env python3
"""llm-proxy.py — black-box logging proxy between Okto Neuron and its LLM endpoint.

The golden harness must SEE the full inference chain without touching app code.
This proxy is just another HTTP client to the LLM — exactly how any application
connects to an IP — so it captures everything from the outside.

Flow:
    okto-neuron serve  ──OKTO_NEURON_LLM_BASE_URL──▶  llm-proxy (here)
                                                        │ forwards verbatim
                                                        ▼
                                          explicitly configured real upstream

For every OpenAI-compatible call it forwards the request unchanged, returns the
upstream response unchanged, and writes ONE JSON trace file capturing the chain:

    source file → ACTUAL request sent to inference → ACTUAL response →
    provider/model/endpoint → usage/finish_reason/timing → action classification

"action" here is the LLM-shape verdict the proxy can see from outside
(HAS-JSON / NO-JSON-prose / EMPTY); the harness layers graph-level "what was
ingested" on top. Source file is read best-effort from a sentinel file the
harness rewrites before each document (loose coupling, no shared process state).

Pure stdlib. Imports nothing from okto_neuron. Loopback only.

Env:
    OKTO_NEURON_PROXY_PORT          port to listen on (required)
    OKTO_NEURON_PROXY_UPSTREAM      upstream base url (required)
    OKTO_NEURON_PROXY_TRACE_DIR     dir for per-call JSON traces (required)
    OKTO_NEURON_PROXY_CURRENT_FILE  path to a 1-line file naming the doc being ingested
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM = os.environ.get("OKTO_NEURON_PROXY_UPSTREAM", "").strip().rstrip("/")
if not UPSTREAM:
    raise SystemExit("OKTO_NEURON_PROXY_UPSTREAM is required")
# The client (marginalia) already includes the full path (e.g. /v1/chat/completions),
# so forward against the upstream HOST only — appending UPSTREAM's own /v1 would
# double it. Keep scheme+netloc; the incoming request path is authoritative.
_UP = urlsplit(UPSTREAM)
_UP_ROOT = f"{_UP.scheme}://{_UP.netloc}"
TRACE_DIR = os.environ.get("OKTO_NEURON_PROXY_TRACE_DIR", "")
CURRENT_FILE = os.environ.get("OKTO_NEURON_PROXY_CURRENT_FILE", "")
PORT = int(os.environ.get("OKTO_NEURON_PROXY_PORT", "0"))

_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _current_source() -> str:
    """Best-effort: which document is the harness ingesting right now."""
    if CURRENT_FILE and os.path.exists(CURRENT_FILE):
        try:
            with open(CURRENT_FILE, encoding="utf-8") as fh:
                return fh.read().strip() or "unknown"
        except OSError:
            return "unknown"
    return "unknown"


def _classify(content: str | None) -> str:
    """The LLM-shape verdict visible from outside the model."""
    text = (content or "").strip()
    if not text:
        return "EMPTY: no content returned"
    low = text
    if '"nodes"' in low or '"edges"' in low or low.startswith("{"):
        return "HAS-JSON: structured extraction returned"
    return "NO-JSON: model returned prose/other — likely nothing ingested"


def _write_trace(rec: dict, seq: int) -> None:
    if not TRACE_DIR:
        return
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        path = os.path.join(TRACE_DIR, f"{seq:04d}_chat.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
    except OSError as exc:  # tracing must never break the run
        sys.stderr.write(f"[llm-proxy] trace write failed: {exc}\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:  # noqa: A002 — silence access log
        pass

    # -- harness health probe -------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._forward(b"")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self._forward(body)

    # -- verbatim forward + trace --------------------------------------------
    def _forward(self, body: bytes) -> None:
        seq = _next_seq()
        url = f"{_UP_ROOT}{self.path}"
        is_chat = self.path.endswith("/chat/completions") and self.command == "POST"

        req_json: dict | None = None
        if is_chat and body:
            try:
                req_json = json.loads(body)
            except ValueError:
                req_json = None

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in {"host", "content-length", "connection"}
        }
        started = time.monotonic()
        ts = time.time()
        status = 0
        resp_bytes = b""
        err: str | None = None
        try:
            req = urllib.request.Request(
                url, data=body or None, headers=headers, method=self.command
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                status = resp.getcode()
                resp_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_bytes = exc.read()
            err = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            status = 502
            resp_bytes = json.dumps({"error": str(exc)}).encode()
            err = str(exc)
        elapsed = round(time.monotonic() - started, 2)

        # return upstream response to marginalia unchanged
        self.send_response(status or 502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

        if not is_chat:
            return  # only trace inference calls, not /models or health

        # -- decode the actual response for the trace -------------------------
        content = None
        usage = None
        finish = None
        resp_json: dict | None = None
        try:
            resp_json = json.loads(resp_bytes)
            choices = resp_json.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content")
            finish = choices[0].get("finish_reason")
            usage = resp_json.get("usage")
        except ValueError:
            pass

        model = (req_json or {}).get("model")
        messages = (req_json or {}).get("messages") or []
        system = user = None
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                system = m.get("content")
            elif m.get("role") == "user":
                user = m.get("content")

        _write_trace(
            {
                "seq": seq,
                "ts": ts,
                "source_file": _current_source(),
                "endpoint": url,
                "upstream": UPSTREAM,
                "model": model,
                "status": status,
                "error": err,
                "elapsed_s": elapsed,
                "finish_reason": finish,
                "usage": usage,
                "action": _classify(content),
                "prompt_sent": {"system": system, "user": user},
                "actual_request": req_json,
                "actual_response_content": content,
                "actual_response_raw": resp_bytes.decode("utf-8", "replace"),
            },
            seq,
        )


def main() -> int:
    if not PORT:
        sys.stderr.write("[llm-proxy] OKTO_NEURON_PROXY_PORT is required\n")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(
        f"[llm-proxy] listening on 127.0.0.1:{PORT} -> {UPSTREAM} (trace -> {TRACE_DIR or 'off'})\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
