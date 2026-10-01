"""Import every ``okto_neuron`` module in one single-threaded phase (issue #40).

WHY. importlib's ``_find_and_load`` holds the lock of the CHILD module while it
imports the PARENT package. A package with an eager ``__init__`` that imports its
own submodules (``reconcile``, ``predicates`` and about fifteen more) therefore
inverts the lock order when two threads cold-import different submodules at the
same time: thread A holds ``pkg.queue`` and waits on ``pkg``, thread B runs
``pkg.__init__`` (holding ``pkg``) and waits on ``pkg.queue``. Python resolves
the cycle by handing one thread a partially initialised module, which surfaces
as ``ImportError: cannot import name 'ReconcileQueue' from partially initialized
module 'okto_neuron.reconcile.queue'`` (a reconcile-propose job died that way
two seconds after submission; 50 of 50 fresh-interpreter race runs fail).

The daemon does its first imports on worker threads (store/job executors, the
asyncio default executor), so it is exposed. Instead of making every package's
``__init__`` lazy, the daemon imports everything on the main thread before any
thread that can run ``okto_neuron`` code exists. After that every import is a
``sys.modules`` hit and the race cannot happen.

Call :func:`preload_server_modules` before the executors, writer-lease
heartbeat, scheduler or prewarm are started.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
import time

_LOG = logging.getLogger(__name__)

_PRELOADED: int | None = None
# ``__main__`` runs the CLI on import; everything else is import-safe.
_SKIP_SUFFIXES = (".__main__",)


def preload_server_modules() -> int:
    """Import every importable ``okto_neuron`` module; return how many were loaded.

    Modules that need an optional third-party extra that is not installed
    (``ModuleNotFoundError`` for a non-``okto_neuron`` top-level name, for
    example ``neo4j``) are skipped and listed in one INFO line. Any other
    failure is logged as a warning and skipped: the caller is the daemon
    startup path, which must not die here (the lazy import would fail the same
    way later, in the code that really needs the module). Idempotent.
    """
    global _PRELOADED
    if _PRELOADED is not None:
        return _PRELOADED
    import okto_neuron

    started = time.perf_counter()
    loaded = 0
    missing_extras: set[str] = set()
    failed: dict[str, str] = {}

    def _on_error(name: str) -> None:
        # walk_packages imports each package to read its __path__; an error there
        # reaches this hook instead of raising.
        failed[name] = "package import failed during the walk"

    names = ["okto_neuron"]
    for info in pkgutil.walk_packages(okto_neuron.__path__, "okto_neuron.", onerror=_on_error):
        if info.name.endswith(_SKIP_SUFFIXES):
            continue
        names.append(info.name)
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            top = (exc.name or "").split(".", 1)[0]
            if top and top != "okto_neuron":
                missing_extras.add(top)
            else:
                failed[name] = repr(exc)
        except Exception as exc:  # noqa: BLE001 - startup must not die here
            failed[name] = repr(exc)
        else:
            loaded += 1
    elapsed = time.perf_counter() - started
    foreign = sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())
    _LOG.info(
        "preloaded %d modules in %.2fs; skipped (optional extra not installed): %s; "
        "other threads alive at preload: %s",
        loaded,
        elapsed,
        sorted(missing_extras) or "none",
        foreign or "none",
    )
    for name, error in sorted(failed.items()):
        _LOG.warning("preload could not import %s: %s", name, error)
    _PRELOADED = loaded
    return loaded


__all__ = ["preload_server_modules"]
