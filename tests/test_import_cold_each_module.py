"""Every ``okto_neuron`` module must import cleanly as the FIRST import (refs #40).

Import cycles are order dependent: a package can import fine when reached through
its ``__init__`` and still fail when a submodule is the first thing imported. Each
module is imported in its own fresh interpreter so a cycle that only bites on one
first-import order is found here instead of in a daemon thread.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PER_IMPORT_TIMEOUT_S = 120
_MISSING = re.compile(r"No module named '([^'.]+)")


def _module_names() -> list[str]:
    spec = importlib.util.find_spec("okto_neuron")
    assert spec is not None and spec.submodule_search_locations
    root = Path(next(iter(spec.submodule_search_locations)))
    names: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).with_suffix("")
        parts = ["okto_neuron", *rel.parts]
        if parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


def _import_first(module: str) -> tuple[str, str | None]:
    """Return (module, error) where error is None on success, or ``skip:<top-level>``
    when an OPTIONAL third-party extra is not installed."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=PER_IMPORT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return module, f"timed out after {PER_IMPORT_TIMEOUT_S}s"
    if proc.returncode == 0:
        return module, None
    err = proc.stderr.strip()
    last = err.splitlines()[-1] if err else ""
    match = _MISSING.search(last)
    if "ModuleNotFoundError" in last and match and match.group(1) != "okto_neuron":
        return module, f"skip:{match.group(1)}"
    return module, err


def test_every_module_imports_first_in_a_fresh_interpreter() -> None:
    modules = _module_names()
    assert len(modules) > 100, "module discovery found suspiciously few modules"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_import_first, modules))
    skipped = {m: e[5:] for m, e in results if e and e.startswith("skip:")}
    failures = {m: e for m, e in results if e and not e.startswith("skip:")}
    print(f"checked {len(modules)} modules; skipped (optional extra missing): {skipped}")
    assert not failures, "modules that fail as the first import:\n" + "\n\n".join(
        f"{m}:\n{e}" for m, e in failures.items()
    )
    if skipped and len(skipped) == len(modules):
        pytest.skip("every module needs an optional extra")
