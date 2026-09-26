from __future__ import annotations

import importlib.metadata as md
from pathlib import Path

import click

from okto_neuron._compat import DIST_NAME


@click.group()
def main() -> None:
    """Manage local model artifacts and caches."""


app = main


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _fmt(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


@app.command("size")
def size_cmd() -> None:
    sections = []
    try:
        dist = md.distribution(DIST_NAME)
        site_packages_size = sum(
            Path(f.locate()).stat().st_size for f in (dist.files or []) if Path(f.locate()).exists()
        )
    except Exception:
        site_packages_size = 0

    hf_cache = Path.home() / ".cache" / "huggingface"
    fastembed_cache = Path.home() / ".cache" / "fastembed"
    gliner_cache = Path.home() / ".cache" / "gliner"

    rows = [
        (f"{DIST_NAME} site-packages", site_packages_size),
        ("HuggingFace cache", _dir_size(hf_cache)),
        ("fastembed cache", _dir_size(fastembed_cache)),
        ("legacy GLiNER cache (unused)", _dir_size(gliner_cache)),
    ]
    sections.extend(rows)
    total = sum(s for _, s in rows)

    click.echo("okto-neuron models size")
    for name, s in rows:
        click.echo(f"  {name}: {_fmt(s)}")
    click.echo(f"  total: {_fmt(total)}")
