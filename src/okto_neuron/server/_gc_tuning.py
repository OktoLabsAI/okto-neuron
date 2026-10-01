"""Garbage-collector tuning and pause visibility for the daemon (refs #38).

Why: the daemon heap is ~2 GB of long-lived Python objects. With CPython's default
thresholds (700, 10, 10) a generation-2 collection walks all of it and pauses every
thread (the event loop included) for 100-168 ms, which is what remained of the
``/health`` and MCP stalls. In a scratch audit, ``gc.freeze()`` plus
``gc.set_threshold(50000, 20, 100)`` removed the stalls (max pause 87-111 ms, p99 6-42 ms,
audit 15-20% faster).

What :func:`apply_gc_tuning` does, ONCE per process, at one quiet point after the startup
vault opened: ``gc.freeze()`` (everything alive then moves to the permanent generation and
is never scanned again), then the new thresholds. It is deliberately NOT repeated after
later (lazy) vault opens: ``gc.freeze()`` also freezes cyclic garbage alive at that
instant and frozen objects are never collected, so re-freezing during operation would leak
in-flight garbage on every pass. If lazily opened vaults still show gen-2 pauses, the
counters from :func:`snapshot` (``/api/v1/status`` -> ``gc``) and the slow-pause WARNING
will say so.

:func:`install_gc_watch` is independent of the tuning. A ``gc.callbacks`` hook counts
collections and pauses with plain ints. Logging from inside the collector is unsafe (the
handler can take a non-reentrant stream lock the interrupted thread already holds), so the
callback only writes plain counters and a pending slot; a tiny daemon thread flushes the
slot to a WARNING at most once per :data:`LOG_INTERVAL_S`, folding every pause in between
into the count. No lock is taken inside the callback.

Knobs (environment wins over ``okto-neuron.toml`` ``[server]``, an invalid value warns and
falls back to the next source, then to the default; startup never fails here):

* ``OKTO_NEURON_GC_TUNING=off`` / ``gc_tuning = false``: no freeze, no threshold change.
* ``OKTO_NEURON_GC_THRESHOLDS=50000,20,100`` / ``gc_thresholds = [50000, 20, 100]``.
* ``OKTO_NEURON_GC_WATCH=off``: do not install the pause hook.

GIL switch interval (also #38, same startup point): with the collector tamed, the
remaining ``/health`` stalls during a back-to-back audit were pure-bytecode GIL sharing
between the audit thread and the event loop (default interval 5 ms). :func:`apply_switch_interval`
sets ``sys.setswitchinterval(0.0002)`` ONCE per process (job-window ``/health`` p99
121.7-124.4 ms -> 38.8 ms in the scratch audit at 0.001; job-active MCP connect p99
474 ms at 0.001 -> 124 ms at 0.0002, n=61 each, #40). Knobs: ``OKTO_NEURON_SWITCH_INTERVAL=<seconds>|off``
/ ``[server] switch_interval``; ``off`` leaves the interpreter default; an invalid value
(non-numeric, <= 0, > 1.0 s) warns and falls back to the next source, then 0.0002.
"""

from __future__ import annotations

import gc
import logging
import sys
import threading
import time
from typing import Any

from okto_neuron import _compat

_LOG = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = (50000, 20, 100)
SLOW_PAUSE_MS = 50
LOG_INTERVAL_S = 5.0
_FLUSH_POLL_S = 1.0
_OFF = {"off", "false", "0", "no"}
_ON = {"on", "true", "1", "yes"}
DEFAULT_SWITCH_INTERVAL_S = 0.0002
MAX_SWITCH_INTERVAL_S = 1.0
_SWITCH_OFF = "off"

_clock = time.perf_counter
_mono = time.monotonic

# --- state written inside the collector callback: plain ints/floats only -----------
_installed = False
_t0 = 0.0
_collections = [0, 0, 0]
_total_us = 0
_max_us = 0
_slow = 0
_last_slow_at = 0
_pending = 0
_pending_worst_us = 0
_pending_gen = 0
_pending_thread = ""
# --- state touched outside the collector --------------------------------------------
_last_log = float("-inf")
_applied = False
_switch_applied = False
_stop =threading.Event()
_thread: threading.Thread | None = None


def _parse_enabled(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _ON:
            return True
        if text in _OFF:
            return False
    return None


def _parse_thresholds(raw: object) -> tuple[int, int, int] | None:
    parts = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(parts, (list, tuple)) or len(parts) != 3:
        return None
    values: list[int] = []
    for part in parts:
        if isinstance(part, bool) or not isinstance(part, (int, str)):
            return None
        try:
            values.append(int(part))
        except ValueError:
            return None
    if any(v < 1 for v in values):
        return None
    return values[0], values[1], values[2]


def _parse_switch_interval(raw: object) -> float | str | None:
    """Seconds in (0, 1.0], or ``"off"``; anything else is invalid (``None``)."""
    if isinstance(raw, bool):
        return _SWITCH_OFF if raw is False else None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("off", "false", "no"):
            return _SWITCH_OFF
        try:
            raw = float(text)
        except ValueError:
            return None
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not 0 < value <= MAX_SWITCH_INTERVAL_S:  # also rejects nan
        return None
    return value


def _pick(parse: Any, default: Any, *candidates: tuple[str, object]) -> Any:
    """First candidate that parses; an invalid one warns and falls through."""
    for label, value in candidates:
        if value is None:
            continue
        parsed = parse(value)
        if parsed is not None:
            return parsed
        _LOG.warning("ignoring invalid %s=%r; using the next source or the default", label, value)
    return default


def _resolve(server: Any) -> tuple[bool, tuple[int, int, int]]:
    """env > ``[server]`` config > default for both knobs."""
    enabled = _pick(
        _parse_enabled,
        True,
        ("OKTO_NEURON_GC_TUNING", _compat.getenv("OKTO_NEURON_GC_TUNING")),
        ("[server] gc_tuning", getattr(server, "gc_tuning", None)),
    )
    thresholds = _pick(
        _parse_thresholds,
        DEFAULT_THRESHOLDS,
        ("OKTO_NEURON_GC_THRESHOLDS", _compat.getenv("OKTO_NEURON_GC_THRESHOLDS")),
        ("[server] gc_thresholds", getattr(server, "gc_thresholds", None)),
    )
    return enabled, thresholds


def _load_server_settings() -> Any:
    """The ``[server]`` settings, or ``None`` (defaults) when the config cannot be read."""
    try:
        from okto_neuron.config import OktoNeuronConfig

        return OktoNeuronConfig.load().server
    except Exception as exc:  # noqa: BLE001 - startup must not die on these knobs
        _LOG.warning("could not read [server] runtime settings (%s); using the defaults", exc)
        return None


def apply_switch_interval(server: Any = None) -> dict[str, Any]:
    """Shorten the GIL switch interval once per process; return what happened (INFO logged).

    ``off`` leaves the interpreter default untouched and logs nothing.
    """
    global _switch_applied
    if server is None:
        server = _load_server_settings()
    chosen = _pick(
        _parse_switch_interval,
        DEFAULT_SWITCH_INTERVAL_S,
        ("OKTO_NEURON_SWITCH_INTERVAL", _compat.getenv("OKTO_NEURON_SWITCH_INTERVAL")),
        ("[server] switch_interval", getattr(server, "switch_interval", None)),
    )
    previous = sys.getswitchinterval()
    report: dict[str, Any] = {
        "enabled": chosen != _SWITCH_OFF,
        "switch_interval_s": previous,
        "previous_switch_interval_s": previous,
        "skipped": None,
    }
    if chosen == _SWITCH_OFF:
        report["skipped"] = "disabled"
    elif _switch_applied:
        report["skipped"] = "already_applied"
        _LOG.info("switch interval already applied in this process; not setting it again")
    else:
        sys.setswitchinterval(chosen)
        _switch_applied = True
        # CPython keeps whole microseconds: 0.0002 reads back as 0.00019999999999999998.
        report["switch_interval_s"] = round(sys.getswitchinterval(), 9)
        _LOG.info("switch interval set to %s (was %s)", report["switch_interval_s"], previous)
    return report


def apply_gc_tuning(server: Any = None) -> dict[str, Any]:
    """Freeze once and set the thresholds; return what happened (also logged at INFO).

    ``server`` is the ``[server]`` settings object; ``None`` loads it from the app config
    (a broken config falls back to the defaults and warns).
    """
    global _applied
    if server is None:
        server = _load_server_settings()
    enabled, thresholds = _resolve(server)
    previous = gc.get_threshold()
    report: dict[str, Any] = {
        "enabled": enabled,
        "thresholds": list(thresholds),
        "previous_thresholds": list(previous),
        "frozen": False,
        "frozen_objects": gc.get_freeze_count(),
        "skipped": None,
    }
    if not enabled:
        report["skipped"] = "disabled"
        _LOG.info("gc tuning disabled (thresholds stay %s)", previous)
    elif _applied:
        report["skipped"] = "already_applied"
        _LOG.info("gc tuning already applied in this process; not freezing again")
    else:
        gc.freeze()
        gc.set_threshold(*thresholds)
        _applied = True
        report["frozen"] = True
        report["frozen_objects"] = gc.get_freeze_count()
        _LOG.info(
            "gc tuning applied: thresholds %s (was %s), froze %d objects",
            thresholds,
            previous,
            report["frozen_objects"],
        )
    return report


def _on_gc(phase: str, info: dict[str, int]) -> None:
    """Runs inside the collector: no logging, no locks, no allocation beyond ints."""
    global _t0, _total_us, _max_us, _slow, _last_slow_at
    global _pending, _pending_worst_us, _pending_gen, _pending_thread
    try:
        if phase == "start":
            _t0 = _clock()
            return
        gen = info["generation"]
        _collections[gen] += 1
        us = int((_clock() - _t0) * 1_000_000)
        _total_us += us
        if us > _max_us:
            _max_us = us
        if us >= SLOW_PAUSE_MS * 1000:
            _slow += 1
            _last_slow_at = int(time.time())
            _pending += 1
            if us > _pending_worst_us:
                _pending_worst_us = us
                _pending_gen = gen
                _pending_thread = threading.current_thread().name
    except Exception:  # noqa: BLE001 - an exception here is printed by the interpreter
        pass


def flush_pending(now: float | None = None) -> bool:
    """Log the pending slow pauses as one WARNING, at most once per ``LOG_INTERVAL_S``."""
    global _pending, _pending_worst_us, _last_log
    count = _pending
    if not count:
        return False
    now = _mono() if now is None else now
    if now - _last_log < LOG_INTERVAL_S:
        return False
    worst, gen, thread = _pending_worst_us, _pending_gen, _pending_thread
    _pending -= count  # pauses recorded while logging stay pending
    _pending_worst_us = 0
    _last_log = now
    _LOG.warning(
        "gc pause over %d ms: %d slow collection(s) since the last report; worst gen=%d "
        "%.1f ms on thread %s (process max %d ms, gen2 collections %d)",
        SLOW_PAUSE_MS,
        count,
        gen,
        worst / 1000,
        thread,
        _max_us // 1000,
        _collections[2],
    )
    return True


def _flush_loop() -> None:
    while not _stop.wait(_FLUSH_POLL_S):
        flush_pending()


def install_gc_watch() -> bool:
    """Install the pause hook (idempotent). ``OKTO_NEURON_GC_WATCH=off`` skips it."""
    global _installed, _thread
    if _parse_enabled(_compat.getenv("OKTO_NEURON_GC_WATCH")) is False:
        _LOG.info("gc pause watch disabled by OKTO_NEURON_GC_WATCH")
        return False
    if _installed:
        return True
    _stop.clear()
    gc.callbacks.append(_on_gc)
    _installed = True
    _thread = threading.Thread(target=_flush_loop, name="okto-neuron-gc-watch", daemon=True)
    _thread.start()
    return True


def snapshot() -> dict[str, Any]:
    """Plain-int view for ``/api/v1/status``; trivial cost."""
    return {
        "watch": _installed,
        "tuning_applied": _applied,
        "thresholds": list(gc.get_threshold()),
        "frozen_objects": gc.get_freeze_count(),
        "collections": {"gen0": _collections[0], "gen1": _collections[1], "gen2": _collections[2]},
        "max_pause_ms": _max_us // 1000,
        "total_pause_ms": _total_us // 1000,
        "slow_pauses": _slow,
        "slow_pause_threshold_ms": SLOW_PAUSE_MS,
        "last_slow_pause_at": _last_slow_at or None,
        "switch_interval_s": round(sys.getswitchinterval(), 9),
        "switch_interval_tuned": _switch_applied,
    }


def reset_for_tests() -> None:
    """Remove the hook, stop its thread, zero the counters, forget that tuning ran."""
    global _installed, _t0, _total_us, _max_us, _slow, _last_slow_at, _pending
    global _pending_worst_us, _pending_gen, _pending_thread, _last_log, _applied, _thread
    global _switch_applied
    _stop.set()
    if _on_gc in gc.callbacks:
        gc.callbacks.remove(_on_gc)
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
    _installed = False
    _t0 = 0.0
    _collections[:] = [0, 0, 0]
    _total_us = _max_us = _slow = _last_slow_at = _pending = _pending_worst_us = _pending_gen = 0
    _pending_thread = ""
    _last_log = float("-inf")
    _applied = False
    _switch_applied = False
