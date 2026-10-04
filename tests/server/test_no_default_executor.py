"""No server code may offload to the default executor or a private pool (issue #13).

Every blocking call in ``okto_neuron.server`` goes through the two bounded pools
in ``server/_store_io.py`` (``store_io`` / ``job_io``). This walks the AST (not a
text grep, so comments and docstrings never count) and fails on:

* ``asyncio.to_thread(...)`` / a bare ``to_thread(...)``;
* ``<loop>.run_in_executor(None, ...)`` (the default executor);
* any ``ThreadPoolExecutor(...)`` construction outside ``_store_io.py`` that is
  not listed in ``_POOL_EXEMPTIONS`` with the reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import okto_neuron.server

SERVER_DIR = Path(okto_neuron.server.__file__).resolve().parent

# (file name, enclosing function) -> why a private pool is allowed there.
_POOL_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("_curation.py", "_run_companion_triage_verdicts"): (
        "LLM-internal fan-out of concurrent model calls inside one job runner that "
        "already occupies a job worker; never touches the store or the event loop"
    ),
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(SERVER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        enclosing: dict[ast.AST, str] = {}
        for parent in ast.walk(tree):
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(parent):
                    enclosing.setdefault(child, parent.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            where = f"{path.name}:{node.lineno}"
            if name == "to_thread":
                found.append(f"{where}: to_thread() uses the default executor")
            elif name == "run_in_executor":
                first = node.args[0] if node.args else None
                if first is None or (isinstance(first, ast.Constant) and first.value is None):
                    found.append(f"{where}: run_in_executor(None, ...) uses the default executor")
            elif name == "ThreadPoolExecutor" and path.name != "_store_io.py":
                key = (path.name, enclosing.get(node, "<module>"))
                if key not in _POOL_EXEMPTIONS:
                    found.append(f"{where}: private ThreadPoolExecutor in {key[1]}")
    return found


def test_server_never_uses_the_default_executor_or_private_pools() -> None:
    assert _violations() == []


def test_pool_exemptions_still_exist() -> None:
    """A stale exemption would silently allow a new pool under the same name."""
    for file_name, function in _POOL_EXEMPTIONS:
        tree = ast.parse((SERVER_DIR / file_name).read_text(encoding="utf-8"))
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function in functions, (file_name, function)
