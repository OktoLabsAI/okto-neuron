"""Ambient background runner — Phase G of the autonomous-companion build.

The companion working while you work. The runner watches the vault inbox
(``.marginalia/incoming``) for dropped markdown notes and feeds each one through
:meth:`~okto_neuron.companion.Companion.remember`. The confidence gate inside
``remember`` keeps autonomy safe: high-confidence candidates auto-commit, the
rest are parked on the review queue — the runner never bypasses that brake.

The unit of work is :func:`process_once`: a single, testable drain of the inbox.
A processed file is **moved** out of ``incoming/`` into ``processed/`` (or
``failed/`` when ``remember`` raises), so idempotency is structural — a second
drain finds an empty inbox and is a no-op, with no separate state file to keep
in sync. :class:`Runner` is a thin poll loop on top (``start``/``stop`` via a
background thread); it owns scheduling, not the per-file logic.

Scope is deliberately the inbox only. A vault-wide mtime watcher (re-resolving on
every edit) is a possible future extension, but the plan leaves "re-resolve on
every edit vs. new files only" open (``docs/autonomous-architecture-plan.md``
open questions), so the runner sticks to explicit drops into the inbox.

No filesystem-watcher dependency is required; a poll loop is sufficient.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

from okto_neuron.companion import Companion, LLMUnavailableError, RememberResult
from okto_neuron.errors import OktoNeuronError

_LOG = logging.getLogger("okto_neuron.runner")

INBOX_DIRNAME = "incoming"
PROCESSED_DIRNAME = "processed"
FAILED_DIRNAME = "failed"

DEFAULT_POLL_SECONDS = 5.0


def inbox_dir(vault_path: Path) -> Path:
    """The watched inbox under a vault's ``.marginalia/``."""
    return Path(vault_path) / ".marginalia" / INBOX_DIRNAME


def _pending(vault_path: Path) -> list[Path]:
    """Markdown files currently waiting in the inbox, oldest first.

    Markdown is the trust root, so only ``*.md`` is drained; other dropped files
    are left untouched."""
    incoming = inbox_dir(vault_path)
    if not incoming.is_dir():
        return []
    files = [p for p in incoming.iterdir() if p.is_file() and p.suffix.lower() == ".md"]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def _move_into(file: Path, dest_dir: Path) -> Path:
    """Move ``file`` into ``dest_dir`` (created on demand), avoiding clobber."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / file.name
    if target.exists():
        stem, suffix = file.stem, file.suffix
        n = 1
        while target.exists():
            target = dest_dir / f"{stem}.{n}{suffix}"
            n += 1
    file.replace(target)
    return target


def process_once(vault_path: Path, companion: Companion) -> list[RememberResult]:
    """Drain the inbox once, calling ``remember`` on each pending markdown file.

    Each file is processed independently. It is moved into
    ``.marginalia/processed/`` **before** ``remember`` runs, so the committed
    Document/Block/Claim ``source_path`` anchors to the file's PERMANENT location
    — re-reading + re-hashing those byte ranges off disk keeps working
    ("markdown/vault is canonical"). Ingesting from ``incoming/`` and moving
    after would leave ``source_path`` dangling at the now-empty inbox path. On
    failure the file is relocated to ``.marginalia/failed/`` so the drain makes
    forward progress instead of looping on a poison file. Moving the file out of
    the inbox up front also preserves idempotency: a second drain finds an empty
    inbox and is a no-op. An empty inbox returns ``[]``; one result per
    successfully remembered file otherwise.
    """
    marg = Path(vault_path) / ".marginalia"
    results: list[RememberResult] = []
    for file in _pending(vault_path):
        resting = _move_into(file, marg / PROCESSED_DIRNAME)
        try:
            result = companion.remember(resting)
        except LLMUnavailableError as exc:
            # Defect C exception: a transient/total LLM outage is retryable,
            # not a poison file — parking it in failed/ permanently would
            # silently drop it from every future drain. Leave it in
            # incoming/ so the next poll retries it once the LLM recovers.
            _LOG.warning(
                "runner: LLM unavailable for %s — leaving in incoming/ for retry: %s",
                file.name,
                exc,
            )
            _move_into(resting, marg / INBOX_DIRNAME)
            continue
        except (OktoNeuronError, OSError) as exc:
            _LOG.warning("runner: remember failed for %s: %s", file.name, exc)
            _move_into(resting, marg / FAILED_DIRNAME)
            continue
        results.append(result)
    return results


class Runner:
    """A poll loop over :func:`process_once`.

    ``start`` spins a daemon thread that drains the inbox every ``poll_seconds``;
    ``stop`` signals it to finish the current cycle and join. All real work lives
    in :func:`process_once`; this class only schedules it.
    """

    def __init__(
        self,
        vault_path: Path,
        companion: Companion,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._vault_path = Path(vault_path)
        self._companion = companion
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_once(self) -> list[RememberResult]:
        """Drain the inbox once (delegates to the module-level function)."""
        return process_once(self._vault_path, self._companion)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any single drain
                _LOG.exception("runner: drain cycle failed")
            self._stop.wait(self._poll_seconds)

    def start(self) -> None:
        """Start the background poll loop (idempotent while running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="okto-neuron-runner", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """Signal the loop to stop and join the thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> "Runner":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def watch(
    vault_path: Path, companion: Companion, *, poll_seconds: float = DEFAULT_POLL_SECONDS
) -> Iterator[list[RememberResult]]:
    """Blocking generator: yield each drain's results forever, until interrupted.

    Convenience for a foreground ``kg watch`` — callers iterate and stop on
    :class:`KeyboardInterrupt`. For background use, prefer :class:`Runner`.
    """
    stop = threading.Event()
    while not stop.is_set():
        yield process_once(vault_path, companion)
        stop.wait(poll_seconds)


__all__ = [
    "INBOX_DIRNAME",
    "PROCESSED_DIRNAME",
    "FAILED_DIRNAME",
    "DEFAULT_POLL_SECONDS",
    "inbox_dir",
    "process_once",
    "Runner",
    "watch",
]
