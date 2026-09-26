"""Claude Code CLI provider — shells out to the headless ``claude`` binary.

Uses whatever auth the CLI already has (subscription / keychain): no API key,
no api_base. ``model`` is passed verbatim to ``--model`` (sonnet/haiku/opus/
fable or full model ids). Sampling knobs (temperature, top_p, top_k, min_p,
presence_penalty, enable_thinking) are accepted for protocol compatibility and
IGNORED — the CLI exposes no generation parameters.

Invocation notes (verified live against the installed CLI):

- ``--output-format json`` emits a JSON **list** of events; the answer is the
  element with ``type == "result"`` (the docs imply a single object — reality
  is a list).
- ``--json-schema <schema>`` constrains output; the parsed result lands in the
  ``structured_output`` field of the result element.
- ``--bare`` skips hooks/plugins/CLAUDE.md; ``--strict-mcp-config`` with no
  ``--mcp-config`` loads zero MCP servers; ``--tools ""`` removes built-in
  tools; ``cwd`` is a neutral temp dir so no project context leaks.
- The user prompt is piped via stdin (avoids ARG_MAX).
- ``--no-session-persistence`` prevents session-file accumulation across the
  hundreds of calls an extraction run makes.
- The system prompt is written to a private (0600, ``tempfile.mkstemp``
  default) tempfile and passed via ``--system-prompt-file <path>``, not
  ``--system-prompt <text>`` in argv. Verified live: the installed CLI accepts
  ``--system-prompt-file`` and errors clearly (``System prompt file not
  found: ...``) when the path doesn't exist, confirming it reads the file
  rather than treating the argument as literal text. A bare ``--system-prompt
  <text>`` argument is visible to any local user via ``ps -ef``/
  ``/proc/<pid>/cmdline``; ``system_prompt`` here is vault/extraction content,
  not a constant, so it can carry vault-specific instructions worth keeping
  off the process table (finding 3.20).

Latency is seconds-not-milliseconds per call (~10-30s) — accepted trade-off.
"""

from __future__ import annotations

import json
import os
import shutil  # noqa: F401 - retained as the provider test monkeypatch seam
import subprocess  # noqa: F401 - shared module object; tests patch Popen here
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.llm import (
    LLMProviderError,
    classify_provider_exception,
    current_call_step,
    logger,
    strip_reasoning,
)
from okto_neuron.llm._cli_provider import CliShellProvider
from okto_neuron.llm._structured_output import NativeSchemaStrategy

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

_STDERR_TAIL = 500

# The claude CLI's result element carries Anthropic's OWN ``stop_reason``,
# verified live against the installed CLI (see the module docstring's
# invocation notes): a successful call returned ``"stop_reason": "end_turn"``
# alongside ``"terminal_reason": "completed"``, and an auth failure returned
# ``"stop_reason": "stop_sequence"`` with ``"terminal_reason": "api_error"``.
#
# Anthropic's documented values map cleanly onto the OpenAI-shaped
# ``finish_reason`` the rest of Okto Neuron reads. Values NOT listed here get no
# ``finish_reason`` at all — the raw string still travels as
# ``native_finish_reason`` and ``finish_reason_available`` stays False.
# Inventing ``"stop"`` for an unknown reason is precisely the failure
# ``native_finish_reason`` was added to prevent (litellm's ``map_finish_reason``
# rewriting an unmapped reason into a clean stop).
_STOP_REASON_TO_FINISH_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "tool_calls",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}


class ClaudeCliProvider(CliShellProvider):
    """LLM provider backed by the local Claude Code CLI (``claude -p``)."""

    _binary_name = "claude"
    _timeout_env_var = "OKTO_NEURON_CLAUDE_CLI_TIMEOUT"
    _default_timeout = 300.0
    _strategy = NativeSchemaStrategy()

    def __init__(self, resolved: "ResolvedLLM") -> None:
        super().__init__(resolved)
        self._call = threading.local()
        # Thread-local so concurrent curation fan-out (ADR 0015 D1) never sees
        # another thread's in-flight system-prompt tempfile path.
        self._tmp = threading.local()

    def _build_command(
        self, binary: str, model: str, system_prompt: str, schema: dict | None
    ) -> list[str]:
        self._call.started = time.monotonic()
        # mkstemp creates the file with mode 0600 by default (POSIX) — no
        # other local user can read the system prompt off disk either.
        prompt_fd, prompt_path = tempfile.mkstemp(
            prefix="okto-neuron-claude-sysprompt-", suffix=".txt"
        )
        try:
            with os.fdopen(prompt_fd, "w", encoding="utf-8") as fh:
                fh.write(system_prompt)
        except BaseException:
            Path(prompt_path).unlink(missing_ok=True)
            raise
        self._tmp.prompt_path = prompt_path

        cmd = [
            binary,
            "--bare",
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--model",
            model,
            "--system-prompt-file",
            prompt_path,
        ]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        return cmd

    def _cleanup(self) -> None:
        prompt_path = getattr(self._tmp, "prompt_path", None)
        if prompt_path is not None:
            Path(prompt_path).unlink(missing_ok=True)
            self._tmp.prompt_path = None

    def _set_parse_context(self, *, stderr_tail: str) -> None:
        self._call.stderr_tail = stderr_tail

    def _parse_output(self, stdout: str) -> tuple[str, dict]:
        try:
            events = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(
                f"claude CLI returned unparseable output (model={self.model}): "
                f"{getattr(self._call, 'stderr_tail', '')}",
                category="malformed_output",
                retryable=False,
            ) from exc

        result_el = None
        if isinstance(events, list):
            for el in events:
                if isinstance(el, dict) and el.get("type") == "result":
                    result_el = el
        elif isinstance(events, dict) and events.get("type") == "result":
            result_el = events
        if result_el is None:
            raise LLMProviderError(
                f"claude CLI output had no result element (model={self.model})",
                category="malformed_output",
                retryable=False,
            )
        if result_el.get("is_error"):
            detail = str(result_el.get("result", ""))[:_STDERR_TAIL]
            classification = classify_provider_exception(RuntimeError(detail))
            raise LLMProviderError(
                f"claude CLI reported is_error (model={self.model}): {detail}",
                category=classification.category,
                retry_after_s=classification.retry_after_s,
                retryable=classification.retryable,
            )

        usage = result_el.get("usage") or {}
        stats = {
            k: v
            for k, v in (
                ("prompt_tokens", usage.get("input_tokens")),
                ("completion_tokens", usage.get("output_tokens")),
                ("cached_tokens", usage.get("cache_read_input_tokens")),
            )
            if isinstance(v, int)
        }
        # The claude CLI is the ONE provider that reports what the call
        # actually cost. That number was previously only logged, so it could
        # not be aggregated per step or per run. Carried on the stats channel
        # so the telemetry span (and any ledger reader) can read it.
        total_cost_usd = result_el.get("total_cost_usd")
        if isinstance(total_cost_usd, (int, float)) and not isinstance(total_cost_usd, bool):
            stats["total_cost_usd"] = float(total_cost_usd)
        # How the model ACTUALLY stopped. Previously this provider reported no
        # finish reason at all, so a truncated extraction was indistinguishable
        # from a complete one. The raw string is always carried; the mapped
        # value only when a real mapping exists.
        stop_reason = result_el.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            stats["native_finish_reason"] = stop_reason
            mapped = _STOP_REASON_TO_FINISH_REASON.get(stop_reason)
            if mapped is not None:
                stats["finish_reason"] = mapped
            else:
                # Same signal LiteLLMProvider records for a reason litellm
                # rewrote: we saw a reason, we just do not know what it means.
                stats["finish_reason_unmapped"] = True
        # The CLI's own loop-level outcome, which is NOT the model's finish
        # reason ("completed" can accompany any stop_reason) but is the only
        # thing that distinguishes a clean turn from one the harness aborted.
        terminal_reason = result_el.get("terminal_reason")
        if isinstance(terminal_reason, str) and terminal_reason:
            stats["cli_terminal_reason"] = terminal_reason
        logger.info(
            "claude_cli call model=%s duration=%.1fs cost_usd=%s "
            "cache_read_input_tokens=%s cache_creation_input_tokens=%s step=%s",
            self.model,
            time.monotonic() - getattr(self._call, "started", time.monotonic()),
            result_el.get("total_cost_usd"),
            usage.get("cache_read_input_tokens"),
            usage.get("cache_creation_input_tokens"),
            current_call_step(),
        )

        structured = result_el.get("structured_output")
        if structured is not None:
            return json.dumps(structured), stats
        return strip_reasoning(str(result_el.get("result") or "")), stats
