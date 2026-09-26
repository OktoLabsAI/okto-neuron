"""Template Method base class for CLI-shell LLM providers.

``claude``, ``pi``, and ``codex`` are shelled out as
non-interactive subprocesses: split messages into system/user text, run the
binary with a timeout, parse its stdout, record usage stats. This class owns
that shared mechanics once; subclasses supply only the CLI-specific argv
shape and output format via the abstract hooks below.

Structured output is a Strategy (:mod:`okto_neuron.llm._structured_output`):
a provider with a native schema flag (``codex exec --output-schema``) uses
``NativeSchemaStrategy`` — the schema travels out-of-prompt and the response
is trusted as-is. A provider with no such flag (``pi``) uses
``BestEffortJsonStrategy`` — the schema is embedded in the prompt and the
response is repaired + validated, reasking up to ``max_attempts`` on failure.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Sequence

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron.llm import (
    LLMProviderError,
    LLMCallCancelled,
    Message,
    ResponseFormat,
    _current_call_timeout_s,
    _notify_request_observer,
    _set_last_call_stats,
    classify_provider_exception,
    _current_call_cancel_predicate,
    current_call_step,
    logger,
)
from okto_neuron._internal.completion_guard import assert_completion_allowed
from okto_neuron.llm._structured_output import (
    StructuredOutputStrategy,
    _JsonValidationFailure,
)

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

_STDERR_TAIL = 500

# The sampling knobs ``complete()`` accepts, in the order they are reported.
# Named once so the accounting below cannot drift from the signature.
_SAMPLING_PARAM_NAMES: tuple[str, ...] = (
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "enable_thinking",
)

# Same dedupe shape as ``_warn_gateway_narrowed_drops`` in ``llm/__init__``:
# one run makes thousands of calls, so the divergence warning is emitted once
# per (provider, model, dropped names) rather than once per call.
_CLI_DROP_WARNED: set[tuple[str, str, tuple[str, ...]]] = set()


def _warn_cli_dropped_params(*, driver: str, model: str, omitted: dict[str, str]) -> None:
    """Say out loud that a configured sampler never reached the model.

    A CLI pseudo-provider used to accept ``temperature=0.0`` and drop it in
    silence, so a vault could be configured for greedy decoding and run at the
    CLI's own default for months with nothing in the log to show for it. That
    silent divergence is the same failure ``_warn_gateway_narrowed_drops``
    exists for, so it gets the same treatment rather than a new mechanism.
    """

    names = tuple(sorted(omitted))
    if not names:
        return
    key = (driver, model, names)
    if key in _CLI_DROP_WARNED:
        return
    _CLI_DROP_WARNED.add(key)
    logger.warning(
        "%s exposes no flag for parameter(s) %s; they were NOT sent and the "
        "model ran with the CLI's own defaults for them. Configure them on the "
        "CLI itself, or use a provider whose transport carries them.",
        driver,
        ", ".join(names),
    )


_ACTIVE_PROCESSES_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[subprocess.Popen, Callable[[], bool] | None] = {}
_FORCE_CANCEL_ALL = threading.Event()


def _register_process(proc: subprocess.Popen) -> Callable[[], bool] | None:
    predicate = _current_call_cancel_predicate()
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES[proc] = predicate
    return predicate


def _unregister_process(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.pop(proc, None)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-stop one CLI process and its descendants without waiting."""
    if sys.platform == "win32":
        # ``loop.add_signal_handler`` is unavailable on Windows, but keep the
        # registry helper safe for direct callers and tests.
        with contextlib.suppress(OSError):
            proc.kill()
    elif hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            proc.kill()


def cancel_requested_cli_processes() -> int:
    """Kill active CLI calls whose owning operation now requests cancellation."""
    with _ACTIVE_PROCESSES_LOCK:
        active = tuple(_ACTIVE_PROCESSES.items())
    cancelled = 0
    for proc, predicate in active:
        if predicate is None:
            continue
        try:
            should_cancel = predicate()
        except Exception:  # noqa: BLE001 — a broken predicate must not kill a process
            continue
        if should_cancel:
            _kill_process_tree(proc)
            cancelled += 1
    return cancelled


def kill_active_cli_processes() -> int:
    """Force-stop every active CLI provider process (repeat-signal escalation)."""
    # Keep the gate raised for the remainder of shutdown so a fail-open caller
    # cannot launch a replacement CLI process just after this snapshot.
    _FORCE_CANCEL_ALL.set()
    with _ACTIVE_PROCESSES_LOCK:
        active = tuple(_ACTIVE_PROCESSES)
    for proc in active:
        _kill_process_tree(proc)
    return len(active)


class CliShellProvider(ABC):
    """Abstract base for LLM providers that shell out to a local CLI binary.

    Subclasses declare these class attributes:

    - ``_binary_name``: the executable to look up on ``PATH``.
    - ``_timeout_env_var``: env var overriding the per-call timeout.
    - ``_default_timeout``: fallback timeout (seconds) when the env var is unset.
    - ``_strategy``: a :class:`~okto_neuron.llm._structured_output.StructuredOutputStrategy`.

    And implement these hooks:

    - ``_build_command(binary, model, system_prompt, schema) -> list[str]``
    - ``_parse_output(stdout) -> tuple[str, dict]`` — ``(raw_text, usage_stats)``.
    - ``_stdin_payload(system_prompt, user_text) -> str`` — default returns
      ``user_text`` unchanged; override only when the CLI has no flag for the
      system prompt and it must be folded into stdin.
    """

    _binary_name: str
    _timeout_env_var: str
    _default_timeout: float
    _strategy: StructuredOutputStrategy

    # The vault driver name (``codex_cli`` / ``claude_cli`` / ``pi_cli``),
    # kept so the canonical usage line can name ``<driver>/<model>`` exactly
    # the way ``LiteLLMProvider.model`` does. Without the prefix a benchmark
    # ledger cannot tell a CLI run's rows from a hosted run's.
    _driver: str

    def __init__(self, resolved: "ResolvedLLM") -> None:
        self.model: str = resolved.model
        self._driver = str(getattr(resolved, "provider", "") or self._binary_name)
        # There is no HTTP endpoint here, and ``resolved.api_base`` is a
        # required field carrying the vault's unrelated loopback default. A
        # telemetry span that reported THAT would claim a hosted CLI call ran
        # against a local server. A ``cli:`` pseudo-URL is the truthful answer:
        # the transport is a subprocess, and which subprocess is the only part
        # we actually know.
        self.api_base: str = f"cli:{self._binary_name}"
        # Per-thread copy of the sampler arguments of the call in flight, so a
        # subclass that CAN map one onto a CLI flag (``pi --thinking``) reads
        # it from ``_build_command`` without widening that hook's signature
        # for the two subclasses that cannot map anything.
        self._sampling = threading.local()
        self._timeout: float | None = resolved.request_timeout_s
        if self._timeout is None and resolved.provider_ref is None:
            timeout_env = _compat_getenv(self._timeout_env_var)
            try:
                self._timeout = float(timeout_env) if timeout_env else self._default_timeout
            except ValueError:
                self._timeout = self._default_timeout

    @abstractmethod
    def _build_command(
        self, binary: str, model: str, system_prompt: str, schema: dict | None
    ) -> list[str]: ...

    @abstractmethod
    def _parse_output(self, stdout: str) -> tuple[str, dict]: ...

    def _stdin_payload(self, system_prompt: str, user_text: str) -> str:
        return user_text

    def _sampling_accounting(self, requested: dict[str, object]) -> dict[str, str]:
        """Report which of ``requested`` this CLI cannot carry, and why.

        ``requested`` holds only the values the caller actually set (a ``None``
        argument is not a configured value and is not reported either way).
        The return value is the same ``{name: reason}`` shape
        ``LiteLLMProvider`` feeds to ``_notify_request_observer`` as
        ``omitted``, so a CLI call's trace answers the same question a litellm
        call's does — what did we ask for, and what actually went out.

        Default: nothing is carried. ``codex exec --help`` (codex-cli 0.144.6)
        and ``claude --help`` were both read against the installed binaries and
        neither exposes a single generation parameter, so for those two this
        default is the accurate answer rather than a placeholder. ``pi``
        overrides it.
        """

        return dict.fromkeys(requested, "cli-exposes-no-flag-for-this-parameter")

    def _usage_log_model(self) -> str:
        """``<driver>/<model>`` — the model label on the canonical usage line."""

        return f"{self._driver}/{self.model}"

    def _log_canonical_usage(self, usage: dict) -> None:
        """Emit the ONE usage line every downstream token accounter parses.

        The LoCoMo harness's usage parser is the only durable record of a run's
        token spend (``/api/v1/ask`` returns no usage), and its regex matches
        the literal prefix ``litellm usage ``. The CLI providers logged
        ``codex_cli usage ...`` / ``claude_cli call ...`` / ``pi_cli usage
        ...`` instead, so every CLI-provider run reported ZERO tokens — not an
        error, just silence, which is worse.

        The prefix is a wire format shared with the harness, not a description
        of which library made the call; the ``<driver>/<model>`` label carries
        the truth about which provider it was. Each provider keeps its own
        richer line (claude's ``total_cost_usd``, pi's cost, codex's reasoning
        tokens) in addition to this one.
        """

        logger.info(
            "litellm usage model=%s prompt_tokens=%s completion_tokens=%s "
            "cached_tokens=%s step=%s",
            self._usage_log_model(),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("cached_tokens"),
            current_call_step(),
        )

    def _cleanup(self) -> None:
        """Release any per-attempt resources created by ``_build_command``.

        Called after every attempt (success, failure, or reask), so a
        provider that writes ephemeral files (e.g. codex's ``-o``/
        ``--output-schema`` tempfiles) never leaks them on an early-exit path.
        No-op by default — providers with nothing to release don't override it.
        """
        return None

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: ResponseFormat | None = None,
    ) -> str:
        assert_completion_allowed()
        # Sampling knobs are no longer swallowed in silence. Whatever this CLI
        # cannot carry is reported through the SAME seam LiteLLMProvider uses
        # (``_notify_request_observer``), so the ingest inspector, the MLflow
        # span and a plain log line all see the divergence between what was
        # configured and what was sent.
        requested: dict[str, object] = {
            name: value
            for name, value in (
                ("temperature", temperature),
                ("max_tokens", max_tokens),
                ("top_p", top_p),
                ("top_k", top_k),
                ("min_p", min_p),
                ("presence_penalty", presence_penalty),
                ("enable_thinking", enable_thinking),
            )
            if value is not None
        }
        self._sampling.requested = dict(requested)
        omitted = self._sampling_accounting(requested)
        sent = {
            "model": self._usage_log_model(),
            **{name: value for name, value in requested.items() if name not in omitted},
        }
        if response_format is not None:
            # Structured output IS carried, by argv (codex/claude) or by prompt
            # embedding (pi) — so it belongs in ``sent``, not in ``omitted``.
            sent["response_format"] = response_format
        _notify_request_observer(
            kwargs=sent,
            extra_body={},
            omitted=omitted,
            sampling_payload_applied=False,
        )
        _warn_cli_dropped_params(driver=self._driver, model=self.model, omitted=omitted)

        binary = shutil.which(self._binary_name)
        if binary is None:
            raise LLMProviderError(
                f"{self._binary_name} CLI not found on PATH",
                category="unavailable",
                retryable=False,
            )

        system_prompt = "\n\n".join(m.content for m in messages if m.role == "system")
        turns: list[str] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "assistant":
                turns.append(f"Assistant: {m.content}")
            else:
                turns.append(m.content)
        user_text = "\n\n".join(turns)

        schema: dict | None = None
        if response_format is not None:
            js = response_format.get("json_schema")
            if isinstance(js, dict):
                schema = js.get("schema")

        last_error: Exception | None = None
        for attempt in range(self._strategy.max_attempts):
            if _FORCE_CANCEL_ALL.is_set():
                raise LLMCallCancelled()
            proc: subprocess.Popen | None = None
            stdin_text = self._stdin_payload(system_prompt, user_text) + (
                self._strategy.embed_prompt_suffix(schema)
            )
            if attempt > 0 and last_error is not None:
                stdin_text += (
                    f"\n\nYour previous response was invalid ({last_error}). "
                    "Respond again with ONLY corrected valid JSON."
                )
            try:
                # Honor an outer scoped deadline (ADR 0015's
                # ``curation_call_timeout_s``/``_scoped_call_timeout``) the same
                # way ``_run_litellm_completion`` does: the process timeout is
                # the tighter of the provider's own configured timeout and
                # whatever remains of the outer task deadline. Without this,
                # CLI-shell providers (claude_cli/codex_cli/pi_cli) silently
                # ignored any scheduler-level deadline and were bounded only by
                # ``self._timeout`` (finding 3.6).
                task_timeout = _current_call_timeout_s()
                if task_timeout is not None and task_timeout <= 0.0:
                    raise LLMProviderError(
                        "LLM task deadline expired before provider execution",
                        category="timeout",
                        retryable=True,
                    )
                if task_timeout is None:
                    effective_timeout = self._timeout
                elif self._timeout is None:
                    effective_timeout = task_timeout
                else:
                    effective_timeout = min(self._timeout, task_timeout)

                # Inside the try (not before it): _build_command is where a
                # provider like codex creates its per-attempt tempfiles (``-o``/
                # ``--output-schema``). If it raises partway through — e.g. the
                # output tempfile is created but the schema tempfile's mkstemp
                # fails (ENOSPC) — the finally below still runs _cleanup() and
                # releases whatever got recorded, instead of leaking the file.
                cmd = self._build_command(binary, self.model, system_prompt, schema)
                observer_sidecar = getattr(self, "_write_observer_sidecar", None)
                if observer_sidecar is not None:
                    observer_sidecar(
                        cmd=cmd,
                        system_prompt=system_prompt,
                        user_text=user_text,
                        stdin_text=stdin_text,
                        schema=schema,
                    )

                started = time.monotonic()
                # Own process group so a timeout can reap the WHOLE tree, not
                # just the direct child: the CLI's node wrapper reparents its
                # grandchild binary to launchd/the OS on a plain kill, leaking
                # it as an orphan (observed live: 8 orphaned codex binaries,
                # PPID=1, up to 5.2h old). POSIX uses start_new_session (own
                # session -> killpg reaps it); Windows has no process groups
                # in that sense, so it gets its own process group via
                # CREATE_NEW_PROCESS_GROUP instead, which taskkill /T can walk
                # on timeout below.
                popen_kwargs: dict = dict(
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=tempfile.gettempdir(),
                    env=os.environ,
                )
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True

                try:
                    proc = subprocess.Popen(cmd, **popen_kwargs)
                except FileNotFoundError as exc:
                    raise LLMProviderError(
                        f"{self._binary_name} CLI not found on PATH",
                        category="unavailable",
                        retryable=False,
                    ) from exc
                cancel_predicate = _register_process(proc)
                if _FORCE_CANCEL_ALL.is_set() or (
                    cancel_predicate is not None and cancel_predicate()
                ):
                    _kill_process_tree(proc)
                    proc.wait()
                    raise LLMCallCancelled()

                try:
                    stdout, stderr = proc.communicate(input=stdin_text, timeout=effective_timeout)
                except subprocess.TimeoutExpired as exc:
                    if sys.platform == "win32":
                        # killpg has no Windows equivalent; taskkill /T walks
                        # the whole process tree rooted at proc.pid the same
                        # way killpg reaps the POSIX process group above.
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            capture_output=True,
                        )
                    else:
                        _kill_process_tree(proc)
                    proc.wait()
                    raise LLMProviderError(
                        f"{self._binary_name} CLI timed out (model={self.model})",
                        category="timeout",
                        retryable=True,
                    ) from exc

                stderr_tail = (stderr or "")[-_STDERR_TAIL:]
                # A scoped Stop may have killed the process while communicate()
                # was blocked. Preserve cancellation as control flow before a
                # curator can mistake the non-zero exit for an ordinary provider
                # error and continue spawning fallback calls.
                if _FORCE_CANCEL_ALL.is_set() or (
                    cancel_predicate is not None and cancel_predicate()
                ):
                    raise LLMCallCancelled()
                if proc.returncode != 0:
                    classification = classify_provider_exception(RuntimeError(stderr_tail))
                    raise LLMProviderError(
                        f"{self._binary_name} CLI exited {proc.returncode} "
                        f"(model={self.model}): {stderr_tail}",
                        category=classification.category,
                        retry_after_s=classification.retry_after_s,
                        retryable=classification.retryable,
                    )

                parse_context = getattr(self, "_set_parse_context", None)
                if parse_context is not None:
                    parse_context(stderr_tail=stderr_tail)
                raw_text, usage = self._parse_output(stdout)
                _set_last_call_stats(usage or None)
                self._log_canonical_usage(usage or {})
                logger.info(
                    "%s call model=%s duration=%.1fs attempt=%d/%d step=%s",
                    self._binary_name,
                    self.model,
                    time.monotonic() - started,
                    attempt + 1,
                    self._strategy.max_attempts,
                    current_call_step(),
                )

                if schema is None:
                    return raw_text
                try:
                    return self._strategy.finalize(raw_text, schema)
                except _JsonValidationFailure as exc:
                    last_error = exc
                    continue
            finally:
                if proc is not None:
                    _unregister_process(proc)
                # Providers that create per-attempt resources in
                # _build_command (e.g. codex's tempfiles) release them here
                # on every exit path — return, raise, or reask continue.
                self._cleanup()

        raise LLMProviderError(
            f"{self._binary_name} CLI did not return schema-valid JSON after "
            f"{self._strategy.max_attempts} attempt(s) (model={self.model}): {last_error}",
            category="malformed_output",
            retryable=False,
        )


__all__ = [
    "CliShellProvider",
    "cancel_requested_cli_processes",
    "kill_active_cli_processes",
]
