#!/usr/bin/env python3
"""Cross-platform editable installer for an Okto Neuron checkout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_WITH = (
    "ladybug>=0.16,<0.17",
    "fastmcp>=0.4",
    "fastembed>=0.4",
    "litellm",
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    uv = shutil.which("uv")
    if uv is None:
        _error("uv not found on PATH. Install uv first: https://docs.astral.sh/uv/")
        return 1

    install_cmd = [uv, "tool", "install", "--editable", str(REPO_ROOT)]
    for dep in RUNTIME_WITH:
        install_cmd.extend(["--with", dep])
    if args.force:
        install_cmd.append("--force")

    _say(f"==> Installing editable Okto Neuron CLI from {REPO_ROOT}")
    code = _run(install_cmd)
    if code != 0:
        return code

    cli = _find_marginalia(uv)
    if cli is None:
        _error("uv installed Okto Neuron, but the uv tool bin directory is not on PATH.")
        _say("Run this once, restart your terminal, then rerun this installer:")
        _say("  uv tool update-shell")
        return 1

    code = _run([str(cli), "--help"], quiet=True)
    if code != 0:
        _error(f"installed Okto Neuron command failed its smoke check: {cli}")
        return code

    _say(f"==> OK: {cli}")

    should_onboard = _should_onboard(args)
    if should_onboard:
        onboard_cmd = [str(cli), "onboard"]
        if args.vault:
            onboard_cmd.extend(["--vault", args.vault])
        if args.provider:
            onboard_cmd.extend(["--provider", args.provider])
        if args.api_base:
            onboard_cmd.extend(["--api-base", args.api_base])
        if args.api_key_env:
            onboard_cmd.extend(["--api-key-env", args.api_key_env])
        if args.model:
            onboard_cmd.extend(["--model", args.model])
        if args.skip_model_discovery:
            onboard_cmd.append("--skip-model-discovery")
        if args.reconfigure:
            onboard_cmd.append("--reconfigure")
        if args.yes:
            onboard_cmd.append("--yes")
        if args.allow_remote_llm:
            onboard_cmd.append("--allow-remote-llm")
        if args.disable_llm:
            onboard_cmd.append("--disable-llm")
        if args.dry_run:
            onboard_cmd.append("--dry-run")
        if args.print_summary_json:
            onboard_cmd.append("--print-summary-json")
        if args.non_interactive or not _is_interactive():
            onboard_cmd.append("--non-interactive")
        return _run(onboard_cmd)

    _say("    Next: okto-neuron onboard")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install this checkout as an editable local marginalia command.",
    )
    parser.add_argument("--no-force", dest="force", action="store_false", help="Do not pass --force to uv tool install.")
    parser.set_defaults(force=True)

    onboard = parser.add_mutually_exclusive_group()
    onboard.add_argument("--onboard", action="store_true", help="Run first-time onboarding after install.")
    onboard.add_argument("--no-onboard", action="store_true", help="Skip first-time onboarding.")

    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; use defaults and explicit options.")
    parser.add_argument("--vault", help="Vault name or path to pass to okto-neuron onboard.")
    parser.add_argument("--provider", help="Provider preset to pass to okto-neuron onboard.")
    parser.add_argument("--api-base", help="Provider base URL to pass to okto-neuron onboard.")
    parser.add_argument("--api-key-env", help="OKTO_NEURON_* env var name to pass to okto-neuron onboard.")
    parser.add_argument("--model", help="Model id to pass to okto-neuron onboard.")
    parser.add_argument("--skip-model-discovery", action="store_true", help="Skip provider model discovery during onboarding.")
    parser.add_argument("--reconfigure", action="store_true", help="Reconfigure an existing explicit LLM block.")
    parser.add_argument("--yes", action="store_true", help="Accept onboarding confirmations.")
    parser.add_argument("--allow-remote-llm", action="store_true", help="Allow a validated non-loopback LLM endpoint.")
    parser.add_argument("--disable-llm", action="store_true", help="Disable LLM-backed features for the vault.")
    parser.add_argument("--dry-run", action="store_true", help="Print the onboarding patch without writing it.")
    parser.add_argument("--print-summary-json", action="store_true", help="Print a machine-readable onboarding summary.")
    return parser


def _should_onboard(args: argparse.Namespace) -> bool:
    if args.no_onboard:
        return False
    if args.onboard:
        return True
    if args.non_interactive or not _is_interactive():
        return False
    reply = input("Run first-time Okto Neuron onboarding now? [Y/n] ").strip().lower()
    return not reply.startswith("n")


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _find_marginalia(uv: str) -> Path | None:
    names = _marginalia_command_names()
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    proc = subprocess.run(
        [uv, "tool", "dir", "--bin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    bin_dir = Path(proc.stdout.strip())
    for name in names:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _marginalia_command_names() -> list[str]:
    return ["marginalia.exe", "marginalia.cmd", "marginalia"] if _is_windows() else ["marginalia"]


def _is_windows() -> bool:
    return os.name == "nt"


def _run(cmd: list[str], *, quiet: bool = False) -> int:
    if not quiet:
        _say("+ " + " ".join(_quote(part) for part in cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=False,
    )
    return int(proc.returncode)


def _quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value):
        return repr(value)
    return value


def _say(message: str) -> None:
    print(message, flush=True)


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
