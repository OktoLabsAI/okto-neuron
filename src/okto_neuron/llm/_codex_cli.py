"""Codex CLI provider — shells out to the headless ``codex`` binary.

Uses whatever auth the CLI already has (ChatGPT subscription / API key via
``~/.codex/config.toml``): no API key, no api_base. ``model`` is passed
verbatim to ``-m``. Sampling knobs (temperature, top_p, top_k, min_p,
presence_penalty, enable_thinking) cannot be sent — ``codex exec --help``
(codex-cli 0.144.6) exposes no generation parameter at all, only ``-c
key=value`` overrides of the user's own ``config.toml``. They are no longer
swallowed silently: every configured-but-unsendable value is reported through
``_notify_request_observer`` as an omitted parameter and warned about once per
(driver, model, parameter set). See ``CliShellProvider._sampling_accounting``.

Invocation notes (verified live against the installed CLI, codex-cli 0.142.5):

- ``codex exec -s read-only --skip-git-repo-check -c approval_policy=never
  --ephemeral --json -o <file> [--output-schema <file>] -m <model> -``
  runs non-interactively, reading the prompt from stdin (``-``).
- ``--json`` emits JSONL events to stdout: ``thread.started``,
  ``item.completed`` (``item.type in {"agent_message", "error"}`` — non-fatal
  ``error`` items, e.g. a plugin-hook parse warning, were observed live and
  must be skipped, not treated as failure), and ``turn.completed`` with a
  ``usage`` block (``input_tokens``, ``cached_input_tokens``,
  ``output_tokens``, ``reasoning_output_tokens``).
- ``-o``/``--output-last-message <file>`` writes the final response text to a
  file — live-verified clean and fence-free even with ``--output-schema``
  supplied (``{"greeting":"Hello","n":42}``, zero repair needed).
- There is no ``--system-prompt`` flag (confirmed via ``codex exec --help``),
  so the system prompt is folded into stdin ahead of the user text.

Latency is seconds-not-milliseconds per call (~10-30s) — accepted trade-off.

Set ``CODEX_OBSERVER_DIR`` to an existing (or creatable) directory to opt into
writing a per-call debug sidecar (command, system_prompt, user_text) next to
the ``-o`` output tempfile; unset (the default), nothing is written.
"""

from __future__ import annotations

import json
import os
import shutil  # noqa: F401 — kept as a monkeypatch point for tests, mirroring pi_cli.
import subprocess  # noqa: F401 — same as above.
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
)
from okto_neuron.llm._cli_provider import CliShellProvider
from okto_neuron.llm._structured_output import NativeSchemaStrategy

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

_STDERR_TAIL = 500


class CodexCliProvider(CliShellProvider):
    """LLM provider backed by the local Codex CLI (``codex exec``)."""

    _binary_name = "codex"
    _timeout_env_var = "OKTO_NEURON_CODEX_CLI_TIMEOUT"
    _default_timeout = 300.0
    _strategy = NativeSchemaStrategy()

    def __init__(self, resolved: "ResolvedLLM") -> None:
        super().__init__(resolved)
        # Thread-local so concurrent curation fan-out (ADR 0015 D1) never sees
        # another thread's in-flight tempfile paths.
        self._tmp = threading.local()

    def _stdin_payload(self, system_prompt: str, user_text: str) -> str:
        # codex has no --system-prompt flag; fold it into the piped text.
        if system_prompt:
            return f"{system_prompt}\n\n{user_text}"
        return user_text

    def _build_command(
        self, binary: str, model: str, system_prompt: str, schema: dict | None
    ) -> list[str]:
        output_fd, output_path = tempfile.mkstemp(prefix="okto-neuron-codex-out-", suffix=".txt")
        os.close(output_fd)
        self._tmp.output_path = output_path

        schema_path: str | None = None
        if schema is not None:
            schema_fd, schema_path = tempfile.mkstemp(
                prefix="okto-neuron-codex-schema-", suffix=".json"
            )
            os.close(schema_fd)
            Path(schema_path).write_text(json.dumps(schema))
        self._tmp.schema_path = schema_path

        cmd = [
            binary,
            "exec",
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "-c",
            "approval_policy=never",
            "--ephemeral",
            "--json",
            "-o",
            output_path,
        ]
        if schema_path is not None:
            cmd += ["--output-schema", schema_path]
        cmd += ["-m", model, "-"]
        return cmd

    def _write_observer_sidecar(
        self,
        *,
        cmd: list[str],
        system_prompt: str,
        user_text: str,
        stdin_text: str,
        schema: dict | None,
    ) -> None:
        directory = os.environ.get("CODEX_OBSERVER_DIR")
        output_path = getattr(self._tmp, "output_path", None)
        if not directory or not output_path:
            return
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            Path(directory, f"{Path(output_path).name}.json").write_text(
                json.dumps(
                    {
                        "created_at": time.time(),
                        "pid": os.getpid(),
                        "model": self.model,
                        "command": cmd,
                        "output_path": output_path,
                        "schema_path": getattr(self._tmp, "schema_path", None),
                        "system_prompt": system_prompt,
                        "user_text": user_text,
                        "stdin_bytes": len(stdin_text.encode()),
                    }
                )
            )
            self._tmp.observer_sidecar = Path(directory, f"{Path(output_path).name}.json")
        except OSError:
            logger.debug("unable to write Codex observer sidecar", exc_info=True)

    def _parse_output(self, stdout: str) -> tuple[str, dict]:
        output_path = getattr(self._tmp, "output_path", None)
        usage: dict = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            # Non-fatal item.completed error items (e.g. a plugin-hook
            # parse warning) are expected live traffic — skip, don't fail.
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage") or {}
                usage = {
                    k: v
                    for k, v in (
                        ("prompt_tokens", raw_usage.get("input_tokens")),
                        ("completion_tokens", raw_usage.get("output_tokens")),
                        ("cached_tokens", raw_usage.get("cached_input_tokens")),
                        ("reasoning_tokens", raw_usage.get("reasoning_output_tokens")),
                    )
                    if isinstance(v, int)
                }
                # DELIBERATELY no ``finish_reason``. A live ``--json`` run was
                # captured against codex-cli 0.144.6 and the terminal event is
                # exactly ``{"type": "turn.completed", "usage": {...}}`` — the
                # CLI reports no stop reason of any kind, mapped or native.
                # ``turn.completed`` describes the AGENT LOOP finishing, not
                # how the model's last message ended, so calling it ``"stop"``
                # would assert something codex never said. The event name is
                # carried as the native reason because it is the only terminal
                # signal that exists, and ``finish_reason_available`` therefore
                # stays False for this provider — an honest "unknown" instead
                # of a fabricated clean stop.
                usage["native_finish_reason"] = "turn.completed"
            elif event.get("type") == "turn.failed":
                error = event.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else str(error)
                classification = classify_provider_exception(RuntimeError(str(message)))
                raise LLMProviderError(
                    f"codex CLI reported turn.failed (model={self.model}): "
                    f"{str(message)[:_STDERR_TAIL]}",
                    category=classification.category,
                    retry_after_s=classification.retry_after_s,
                    retryable=classification.retryable,
                )

        raw_text = ""
        if output_path is not None:
            try:
                raw_text = Path(output_path).read_text().strip()
            except OSError as exc:
                raise LLMProviderError(
                    f"codex CLI produced no readable output file (model={self.model}): {exc}",
                    category="malformed_output",
                    retryable=False,
                ) from exc
        if not raw_text:
            raise LLMProviderError(
                f"codex CLI returned empty output (model={self.model})",
                category="malformed_output",
                retryable=False,
            )
        # Provider-specific detail only. The canonical ``litellm usage ...``
        # line every token accounter parses is emitted once, for all three CLI
        # providers, by ``CliShellProvider._log_canonical_usage``.
        logger.info(
            "codex_cli usage model=%s prompt_tokens=%s completion_tokens=%s "
            "cached_tokens=%s reasoning_tokens=%s native_finish_reason=%s step=%s",
            self.model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("cached_tokens"),
            usage.get("reasoning_tokens"),
            usage.get("native_finish_reason"),
            current_call_step(),
        )
        return raw_text, usage

    def _cleanup(self) -> None:
        for attr in ("output_path", "schema_path"):
            path = getattr(self._tmp, attr, None)
            if path is not None:
                Path(path).unlink(missing_ok=True)
                setattr(self._tmp, attr, None)
        sidecar = getattr(self._tmp, "observer_sidecar", None)
        if sidecar is not None:
            sidecar.unlink(missing_ok=True)
            self._tmp.observer_sidecar = None
