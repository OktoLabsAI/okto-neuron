#!/usr/bin/env python3
"""Supervised runner for long Okto Neuron commands.

Runs exactly one child command in its own process group, streams a periodic
heartbeat plus the child's combined output to a log file, and enforces a hard
wall-clock timeout by terminating (then killing) the whole process tree. It
exists so that ingest, rebuild, and acceptance commands that outlive an
interactive tool call still produce durable, inspectable evidence instead of a
silently truncated transcript.

This is the supervised-run entry point for long live runs: every command
expected to exceed roughly 30 seconds is run through it with an explicit
``--timeout-s``, a ``--heartbeat-s`` no slower than the run requires, and a
``--log`` path.

Example::

    uv run python scripts/run_supervised.py \\
        --timeout-s 3600 --heartbeat-s 30 --log run.log \\
        -- uv run kg rebuild --vault demo
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import TextIO


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a command with visible heartbeats and a hard process-group timeout."
    )
    parser.add_argument("--timeout-s", type=float, required=True)
    parser.add_argument("--heartbeat-s", type=float, default=30.0)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _terminate_group(process: subprocess.Popen[str], *, grace_s: float = 3.0) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover - exercised on Windows hosts
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T"],
            check=False,
            capture_output=True,
        )
    try:
        process.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGKILL)
    else:  # pragma: no cover - exercised on Windows hosts
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    process.wait()


def _copy_output(stream: TextIO, messages: queue.SimpleQueue[str | None]) -> None:
    try:
        for line in stream:
            messages.put(line)
    finally:
        messages.put(None)


def main() -> int:
    args = _parser().parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        _parser().error("a command is required after --")
    if args.timeout_s <= 0:
        _parser().error("--timeout-s must be greater than zero")
    if not 1 <= args.heartbeat_s <= 30:
        _parser().error("--heartbeat-s must be between 1 and 30 seconds")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=os.name == "posix",
    )
    assert process.stdout is not None
    print(
        f"supervisor: started pid={process.pid} timeout_s={args.timeout_s:g} "
        f"log={args.log}",
        flush=True,
    )

    messages: queue.SimpleQueue[str | None] = queue.SimpleQueue()
    reader = threading.Thread(target=_copy_output, args=(process.stdout, messages), daemon=True)
    reader.start()
    next_heartbeat = started + args.heartbeat_s
    reader_done = False

    def stop(_signum: int, _frame: object) -> None:
        _terminate_group(process)
        raise SystemExit(128 + _signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with args.log.open("a", encoding="utf-8") as log:
        while True:
            now = time.monotonic()
            while True:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    break
                if message is None:
                    reader_done = True
                    continue
                log.write(message)
                log.flush()
                sys.stdout.write(message)
                sys.stdout.flush()

            return_code = process.poll()
            if return_code is not None and reader_done:
                elapsed = time.monotonic() - started
                print(
                    f"supervisor: completed pid={process.pid} status={return_code} "
                    f"elapsed_s={elapsed:.1f}",
                    flush=True,
                )
                return return_code

            if now - started >= args.timeout_s:
                _terminate_group(process)
                elapsed = time.monotonic() - started
                print(
                    f"supervisor: timeout pid={process.pid} elapsed_s={elapsed:.1f}",
                    file=sys.stderr,
                    flush=True,
                )
                return 124

            if now >= next_heartbeat:
                elapsed = now - started
                print(
                    f"supervisor: running pid={process.pid} elapsed_s={elapsed:.1f}",
                    flush=True,
                )
                next_heartbeat = now + args.heartbeat_s
            time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
