"""Development supervisor for the local Okto Neuron server."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from okto_neuron.vault_registry import is_vault, resolve_vault_reference

Echo = Callable[[str], None]
Snapshot = dict[Path, tuple[int, int]]

_IGNORED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "frontend_dist",
    "node_modules",
}
_FRONTEND_REL_FILES = (
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/postcss.config.js",
    "frontend/tailwind.config.cjs",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
)
_PYTHON_REL_FILES = (
    "pyproject.toml",
    "uv.lock",
)


@dataclass(frozen=True)
class DevOptions:
    vault: Path | str | None = None
    host: str = "127.0.0.1"
    rest_port: int = 7777
    mcp_port: int = 8201
    allow_remote: bool = False
    poll_seconds: float = 1.0
    build_frontend: bool = True


def run_dev(options: DevOptions, *, echo: Echo = print) -> int:
    """Run a rebuild/restart loop until interrupted."""
    repo_root = find_repo_root()
    vault_path = _resolve_dev_vault(options.vault)
    if options.vault is not None and vault_path is None:
        target = resolve_vault_reference(options.vault)
        raise RuntimeError(
            f"vault not found: {target}\n"
            "Create one with: okto-neuron vault create <name> --embedder stub"
        )
    if options.poll_seconds <= 0:
        raise RuntimeError("--poll must be greater than 0")

    if options.build_frontend:
        try:
            build_web_ui(repo_root, echo=echo)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"frontend build failed with status {exc.returncode}") from exc

    process: subprocess.Popen[bytes] | None = None
    snapshot = collect_snapshot(repo_root, include_frontend=options.build_frontend)
    try:
        process = start_server(options, vault_path, echo=echo)
        while True:
            time.sleep(options.poll_seconds)
            if process is not None:
                returncode = process.poll()
                if returncode is not None:
                    if should_restart_after_exit(returncode):
                        echo(f"dev: server exited with status {returncode}; restarting")
                        process = start_server(options, vault_path, echo=echo)
                        snapshot = collect_snapshot(
                            repo_root, include_frontend=options.build_frontend
                        )
                        continue
                    echo(
                        f"dev: server exited with status {returncode}; "
                        "waiting for a source change before restarting"
                    )
                    process = None

            next_snapshot = collect_snapshot(repo_root, include_frontend=options.build_frontend)
            changed = changed_paths(snapshot, next_snapshot)
            if not changed:
                snapshot = next_snapshot
                continue

            kinds = classify_changed_paths(changed, repo_root)
            if not kinds:
                snapshot = next_snapshot
                continue

            echo("dev: changed " + ", ".join(_format_changed_paths(changed, repo_root)))
            if "frontend" in kinds and options.build_frontend:
                try:
                    build_web_ui(repo_root, echo=echo)
                except subprocess.CalledProcessError as exc:
                    echo(
                        f"dev: frontend build failed with status {exc.returncode}; server left as-is"
                    )
                    snapshot = next_snapshot
                    continue

            if process is not None and not should_restart_server(kinds, server_running=True):
                echo("dev: frontend rebuilt; server continues without restart")
                snapshot = collect_snapshot(repo_root, include_frontend=options.build_frontend)
                continue

            if process is not None:
                stop_server(process, echo=echo)
            process = start_server(options, vault_path, echo=echo)
            snapshot = collect_snapshot(repo_root, include_frontend=options.build_frontend)
    except KeyboardInterrupt:
        echo("dev: stopping")
        return 0
    finally:
        if process is not None and process.poll() is None:
            stop_server(process, echo=echo)


def find_repo_root() -> Path:
    """Find the editable source checkout that contains the frontend."""
    origins = [Path.cwd().resolve(), Path(__file__).resolve()]
    seen: set[Path] = set()
    for origin in origins:
        current = origin if origin.is_dir() else origin.parent
        for candidate in (current, *current.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "pyproject.toml").is_file() and (candidate / "frontend").is_dir():
                return candidate
    raise RuntimeError("dev mode requires the Okto Neuron source checkout")


def build_web_ui(repo_root: Path, *, echo: Echo = print) -> None:
    frontend_dir = repo_root / "frontend"
    if shutil.which("npm") is None:
        raise RuntimeError("npm not found on PATH; install Node.js before running dev mode")
    if not (frontend_dir / "node_modules").is_dir():
        echo("dev: installing frontend dependencies (npm ci)")
        subprocess.run(["npm", "ci"], cwd=frontend_dir, check=True)
    echo("dev: building frontend_dist (npm run build)")
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)


def start_server(
    options: DevOptions, vault_path: Path | None, *, echo: Echo = print
) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "okto_neuron.cli",
        "serve",
        "--host",
        options.host,
        "--port",
        str(options.rest_port),
        "--mcp-port",
        str(options.mcp_port),
        "--foreground",
        "--no-open",
    ]
    if vault_path is not None:
        cmd[4:4] = ["--vault", str(vault_path)]
    if options.allow_remote:
        cmd.append("--allow-remote")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if vault_path is None:
        echo("dev: starting application with no compatibility fallback vault")
    else:
        echo(f"dev: starting server for {vault_path}")
    echo(f"dev: UI  http://{options.host}:{options.rest_port}")
    echo(f"dev: MCP http://{options.host}:{options.mcp_port}")
    return subprocess.Popen(cmd, cwd=find_repo_root(), env=env)


def stop_server(process: subprocess.Popen[bytes], *, echo: Echo = print) -> None:
    if process.poll() is not None:
        return
    echo("dev: stopping server")
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        echo("dev: server did not stop after 20s; killing it")
        process.kill()
        process.wait(timeout=5)


def _resolve_dev_vault(ref: Path | str | None) -> Path | None:
    # Development mode mirrors `serve`: vault selection belongs to each browser
    # tab unless the developer explicitly pins a compatibility fallback.
    if ref is None:
        return None
    try:
        candidate = resolve_vault_reference(ref)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if is_vault(candidate):
        return candidate
    if ref is None:
        return None
    return None


def collect_snapshot(repo_root: Path, *, include_frontend: bool = True) -> Snapshot:
    snapshot: Snapshot = {}
    for path in iter_watched_files(repo_root, include_frontend=include_frontend):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[path] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def changed_paths(before: Snapshot, after: Snapshot) -> list[Path]:
    paths = set(before) | set(after)
    return sorted(path for path in paths if before.get(path) != after.get(path))


def classify_changed_paths(paths: Iterable[Path], repo_root: Path) -> set[str]:
    kinds: set[str] = set()
    python_root = repo_root / "src" / "okto_neuron"
    frontend_root = repo_root / "frontend"
    for path in paths:
        if _is_relative_to(path, python_root) and path.suffix == ".py":
            kinds.add("python")
        elif path in (repo_root / rel for rel in _PYTHON_REL_FILES):
            kinds.add("python")
        elif _is_relative_to(path, frontend_root):
            kinds.add("frontend")
    return kinds


def should_restart_server(change_kinds: set[str], *, server_running: bool) -> bool:
    """Restart for Python changes, or recover a server that is not running."""

    return not server_running or "python" in change_kinds


def should_restart_after_exit(returncode: int) -> bool:
    """Keep dev alive after a clean or signal-driven child exit, without crash-looping."""

    return returncode <= 0


def iter_watched_files(repo_root: Path, *, include_frontend: bool = True) -> Iterable[Path]:
    python_root = repo_root / "src" / "okto_neuron"
    if python_root.is_dir():
        yield from _walk_files(python_root, suffixes={".py"})
    for rel in _PYTHON_REL_FILES:
        path = repo_root / rel
        if path.is_file():
            yield path

    if not include_frontend:
        return
    frontend_src = repo_root / "frontend" / "src"
    if frontend_src.is_dir():
        yield from _walk_files(
            frontend_src,
            suffixes={".css", ".html", ".js", ".json", ".jsx", ".ts", ".tsx"},
        )
    for rel in _FRONTEND_REL_FILES:
        path = repo_root / rel
        if path.is_file():
            yield path


def _walk_files(root: Path, *, suffixes: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in _IGNORED_DIR_NAMES and not dirname.startswith(".")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in suffixes:
                yield path


def _format_changed_paths(paths: Iterable[Path], repo_root: Path) -> list[str]:
    formatted: list[str] = []
    for path in sorted(paths):
        try:
            formatted.append(str(path.relative_to(repo_root)))
        except ValueError:
            formatted.append(str(path))
    return formatted[:8]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = [
    "DevOptions",
    "changed_paths",
    "classify_changed_paths",
    "collect_snapshot",
    "find_repo_root",
    "iter_watched_files",
    "run_dev",
    "should_restart_server",
    "should_restart_after_exit",
]
