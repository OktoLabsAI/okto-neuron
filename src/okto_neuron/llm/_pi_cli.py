"""Pi CLI provider — shells out to the headless ``pi`` binary (pi.dev).

Uses whatever auth the CLI already has (~/.pi/agent/settings.json / env vars):
no API key, no api_base. ``model`` is passed verbatim to ``--model``
(provider/id with optional :thinking suffix). Sampling knobs (temperature,
top_p, top_k, min_p, presence_penalty, enable_thinking) are accepted for
protocol compatibility and IGNORED — the CLI exposes no generation parameters.

Invocation notes (verified live against the installed CLI):

- ``--mode json`` emits NDJSON (one JSON object per line) to stdout.
- ``-p`` is non-interactive mode (process prompt and exit).
- ``--no-session`` prevents session-file accumulation.
- ``--no-tools`` disables all tools; ``--no-context-files`` skips AGENTS.md/
  CLAUDE.md; ``--no-extensions`` / ``--no-skills`` / ``--no-prompt-templates``
  disable their respective discovery. ``cwd`` is a neutral temp dir so no
  project context leaks.
- The user prompt is piped via stdin (avoids ARG_MAX).
- There is NO ``--json-schema`` flag; when ``response_format`` is passed, the
  schema is embedded into the prompt via ``BestEffortJsonStrategy`` and the
  response is repaired (fence-stripping / balanced-brace extraction) and
  schema-validated by the shared :class:`~okto_neuron.llm._cli_provider.CliShellProvider`
  reask loop — live-verified this required fence-stripping on 100% of calls
  (``.scratchpad/pi_cli_schema_spike.py``, 28/28 convergence).
- ``pi`` exits 0 even on internal model errors; failure is detected via
  ``stopReason == "error"`` in the NDJSON stream. Invalid CLI-level args
  (bad ``--provider``) do exit nonzero with stderr.
- ``--system-prompt`` receives a **path** to a private (0600,
  ``tempfile.mkstemp`` default) tempfile, not the literal system-prompt text.
  There is no separate ``--system-prompt-file`` flag, but ``pi``'s own arg
  resolver (``resolvePromptInput`` in
  ``core/resource-loader.js``) treats the ``--system-prompt``/
  ``--append-system-prompt`` value as a file path whenever ``existsSync()``
  is true for it, reading the file's contents instead — confirmed by reading
  the installed CLI's bundled source and by a live call (``--system-prompt
  <tmpfile>`` with marker text produced a response quoting the marker). A
  bare ``--system-prompt <text>`` argument is visible to any local user via
  ``ps -ef``/``/proc/<pid>/cmdline``; ``system_prompt`` here is
  vault/extraction content, not a constant, so it can carry vault-specific
  instructions worth keeping off the process table (finding 3.20).

Latency is seconds-not-milliseconds per call (~10-30s) — accepted trade-off.
"""

from __future__ import annotations

import json
import os
import shutil  # noqa: F401 — tests/llm/test_pi_cli_provider.py monkeypatches
import subprocess  # noqa: F401 — mod.shutil.which / mod.subprocess.run; shutil and
import tempfile  # subprocess are shared module singletons, so the patch
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.llm import (
    LLMProviderError,
    classify_provider_exception,
    current_call_step,
    logger,
)
from okto_neuron.llm._cli_provider import CliShellProvider
from okto_neuron.llm._structured_output import BestEffortJsonStrategy

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

_STDERR_TAIL = 500

# pi's own message-level stop reasons, read out of the installed CLI bundle
# (``@earendil-works/pi-coding-agent/dist``): aborted, deferred, error,
# length, pending, stop, toolUse. ``error`` is raised before this map is
# consulted. Anything absent here carries as ``native_finish_reason`` only —
# ``aborted``/``deferred``/``pending`` have no OpenAI-shaped equivalent and
# inventing one would misreport a cancelled or still-running turn as a clean
# completion.
_PI_STOP_REASON_TO_FINISH_REASON = {
    "stop": "stop",
    "length": "length",
    "toolUse": "tool_calls",
}

# ``pi --thinking <level>`` (verified in ``pi --help``) is the ONE generation
# knob any of the three CLIs exposes. Only the OFF direction is a faithful
# mapping of Okto Neuron's boolean: ``enable_thinking=False`` is unambiguously
# ``off``, while ``True`` names no particular level and the CLI's own default
# already is a level, so sending one would be a guess.
_PI_THINKING_OFF = "off"


class PiCliProvider(CliShellProvider):
    """LLM provider backed by the local Pi CLI (``pi -p``)."""

    _binary_name = "pi"
    _timeout_env_var = "OKTO_NEURON_PI_CLI_TIMEOUT"
    _default_timeout = 300.0
    _strategy = BestEffortJsonStrategy()

    def __init__(self, resolved: "ResolvedLLM") -> None:
        super().__init__(resolved)
        # Thread-local so concurrent curation fan-out (ADR 0015 D1) never sees
        # another thread's in-flight system-prompt tempfile path.
        self._tmp = threading.local()

    def _build_command(
        self, binary: str, model: str, system_prompt: str, schema: dict | None
    ) -> list[str]:
        # pi has no native schema slot — schema instructions are embedded in
        # stdin by BestEffortJsonStrategy, so `schema` is unused here.
        # mkstemp creates the file with mode 0600 by default (POSIX) — no
        # other local user can read the system prompt off disk either.
        prompt_fd, prompt_path = tempfile.mkstemp(prefix="okto-neuron-pi-sysprompt-", suffix=".txt")
        try:
            with os.fdopen(prompt_fd, "w", encoding="utf-8") as fh:
                fh.write(system_prompt)
        except BaseException:
            Path(prompt_path).unlink(missing_ok=True)
            raise
        self._tmp.prompt_path = prompt_path

        cmd = [
            binary,
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--no-tools",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--model",
            model,
            "--system-prompt",
            prompt_path,
        ]
        if self._thinking_off_applies(model):
            cmd += ["--thinking", _PI_THINKING_OFF]
        return cmd

    def _thinking_off_applies(self, model: str) -> bool:
        """Whether this call should append ``--thinking off``.

        Two conditions, both required. The caller must have asked for
        ``enable_thinking=False`` (``True`` names no level — see
        ``_PI_THINKING_OFF``), and the configured model string must not already
        carry pi's own ``provider/id:<thinking>`` suffix. A model spec that
        pins a thinking level is a deliberate, more specific instruction than
        a vault-wide boolean; overriding it from here would silently beat the
        operator's explicit choice, and ``_sampling_accounting`` reports the
        conflict instead.
        """

        requested = getattr(self._sampling, "requested", None) or {}
        return requested.get("enable_thinking") is False and ":" not in model

    def _sampling_accounting(self, requested: dict[str, object]) -> dict[str, str]:
        omitted = dict.fromkeys(requested, "cli-exposes-no-flag-for-this-parameter")
        if "enable_thinking" not in omitted:
            return omitted
        if requested.get("enable_thinking") is False:
            if ":" in self.model:
                omitted["enable_thinking"] = (
                    "model-string-pins-its-own-thinking-level; --thinking not overridden"
                )
            else:
                # Actually sent, as ``--thinking off``.
                del omitted["enable_thinking"]
        else:
            omitted["enable_thinking"] = (
                "enable_thinking=True names no pi thinking level; CLI default retained"
            )
        return omitted

    def _cleanup(self) -> None:
        prompt_path = getattr(self._tmp, "prompt_path", None)
        if prompt_path is not None:
            Path(prompt_path).unlink(missing_ok=True)
            self._tmp.prompt_path = None

    def _parse_output(self, stdout: str) -> tuple[str, dict]:
        events: list[dict] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip non-JSON lines (e.g. progress markers)
                continue

        agent_end = None
        for event in events:
            if isinstance(event, dict) and event.get("type") == "agent_end":
                agent_end = event

        if agent_end is None:
            raise LLMProviderError(
                f"pi CLI output had no agent_end event (model={self.model})",
                category="malformed_output",
                retryable=False,
            )

        messages_list = agent_end.get("messages", [])
        if not messages_list:
            raise LLMProviderError(
                f"pi CLI agent_end had no messages (model={self.model})",
                category="malformed_output",
                retryable=False,
            )

        last_msg = messages_list[-1]

        if last_msg.get("stopReason") == "error":
            error_content = ""
            for item in last_msg.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    error_content += item.get("text", "")
            detail = error_content[:_STDERR_TAIL] or "unknown error"
            classification = classify_provider_exception(RuntimeError(detail))
            raise LLMProviderError(
                f"pi CLI reported error (model={self.model}): {detail}",
                category=classification.category,
                retry_after_s=classification.retry_after_s,
                retryable=classification.retryable,
            )

        usage = last_msg.get("usage") or {}
        stats = {
            k: v
            for k, v in (
                ("prompt_tokens", usage.get("input")),
                ("completion_tokens", usage.get("output")),
                ("cached_tokens", usage.get("cacheRead")),
            )
            if isinstance(v, int)
        }
        stop_reason = last_msg.get("stopReason")
        if isinstance(stop_reason, str) and stop_reason:
            stats["native_finish_reason"] = stop_reason
            mapped = _PI_STOP_REASON_TO_FINISH_REASON.get(stop_reason)
            if mapped is not None:
                stats["finish_reason"] = mapped
            else:
                stats["finish_reason_unmapped"] = True
        # Provider-specific detail only; the canonical ``litellm usage ...``
        # line is emitted by ``CliShellProvider._log_canonical_usage``.
        logger.info(
            "pi_cli usage model=%s cost=%s input_tokens=%s output_tokens=%s "
            "cache_read=%s native_finish_reason=%s step=%s",
            self.model,
            usage.get("cost"),
            usage.get("input"),
            usage.get("output"),
            usage.get("cacheRead"),
            stats.get("native_finish_reason"),
            current_call_step(),
        )

        result_parts: list[str] = []
        for item in last_msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                result_parts.append(item.get("text", ""))
        return "".join(result_parts).strip(), stats
