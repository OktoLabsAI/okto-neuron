"""Compatibility layer for the Marginalia -> Okto Neuron rename (0.3.0).

Every place that still has to understand a pre-0.3.0 name goes through this
module, so the full list of legacy names lives in one file. The grep gate in
``tests/test_rename_compat.py`` fails when a ``marginalia`` string literal
appears anywhere else in ``src/`` without being on :data:`KEPT_LEGACY_LITERALS`.

Rules (plan decisions D3 and D5):

* Environment: ``OKTO_NEURON_*`` is the name. The matching ``MARGINALIA_*``
  name is still read until 0.5, with one warning per variable per process.
  When both are set, the new name wins.
* App home: ``~/.okto-neuron``. A pre-0.3.0 ``~/.marginalia`` is read as a
  fallback file by file, and :func:`migrate_legacy_app_home` copies the
  app-level files over and leaves a ``MOVED_TO`` pointer. Vaults are never
  moved: document ids hash absolute paths, so ``~/.marginalia/vaults`` stays a
  vault root for as long as it exists.
* Config files: ``okto-neuron.toml`` / ``okto-neuron.yaml`` for new files; the
  old ``marginalia.toml`` / ``marginalia.yaml`` are read and, for vaults,
  written back under their existing name (never renamed).
* HTTP: ``X-Okto-Neuron-Vault`` and ``okto_neuron_version`` are added; the old
  header and field are accepted and still emitted.
* Process detection: ``okto-neuron`` and ``marginalia`` console scripts
  (plus ``.exe``) and both ``-m`` module spellings.
* Backend plugins: both entry-point groups are read; the new group wins on a
  name clash.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import warnings
from collections.abc import Iterator
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

PRODUCT_NAME = "Okto Neuron"
BRAND_LINE = "Okto Neuron by Okto Labs"
CLI_NAME = "okto-neuron"
DIST_NAME = "okto-neuron"
LEGACY_CLI_NAME = "marginalia"
LEGACY_DIST_NAME = "marginalia"
#: Release in which the legacy CLI alias and legacy env names stop working.
LEGACY_REMOVAL_VERSION = "0.5"

# --------------------------------------------------------------------------
# Environment variables
# --------------------------------------------------------------------------

ENV_PREFIX = "OKTO_NEURON_"
LEGACY_ENV_PREFIX = "MARGINALIA_"

_warned_env: set[str] = set()
_warn_lock = threading.Lock()


class LegacyNameWarning(FutureWarning):
    """A pre-0.3.0 ``marginalia`` name was used. Shown by default."""


def _warn_once(key: str, message: str) -> None:
    with _warn_lock:
        if key in _warned_env:
            return
        _warned_env.add(key)
    warnings.warn(message, LegacyNameWarning, stacklevel=3)


def install_cli_warning_format() -> None:
    """Show :class:`LegacyNameWarning` on the CLI as one plain ``warning:`` line.

    Python's default format prints the file, line and source line of the call
    site, which reads like a crash to someone who only set an old variable name.
    Other warning categories keep the default format. Idempotent.
    """
    original = warnings.showwarning
    if getattr(original, "_okto_neuron_cli_format", False):
        return

    def show(message, category, filename, lineno, file=None, line=None):  # type: ignore[no-untyped-def]
        if isinstance(category, type) and issubclass(category, LegacyNameWarning):
            print(f"warning: {message}", file=file or sys.stderr)
            return
        original(message, category, filename, lineno, file, line)

    show._okto_neuron_cli_format = True  # type: ignore[attr-defined]
    warnings.showwarning = show


def reset_legacy_warnings_for_tests() -> None:
    with _warn_lock:
        _warned_env.clear()


def legacy_env_name(name: str) -> str:
    """``OKTO_NEURON_X`` -> ``MARGINALIA_X``."""
    if not name.startswith(ENV_PREFIX):
        raise ValueError(f"{name!r} is not an {ENV_PREFIX}* variable")
    return LEGACY_ENV_PREFIX + name[len(ENV_PREFIX) :]


def getenv(name: str, default: str | None = None) -> str | None:
    """Read ``OKTO_NEURON_X``, falling back to ``MARGINALIA_X`` with one warning.

    ``name`` must be the new ``OKTO_NEURON_*`` spelling. An empty value counts as
    set, exactly like ``os.environ.get``.
    """
    legacy = legacy_env_name(name)
    if name in os.environ:
        if legacy in os.environ and os.environ[legacy] != os.environ[name]:
            _warn_once(
                legacy,
                f"both {name} and {legacy} are set; using {name}. "
                f"{legacy} is ignored and will stop being read in {LEGACY_REMOVAL_VERSION}.",
            )
        return os.environ[name]
    if legacy in os.environ:
        _warn_once(
            legacy,
            f"{legacy} is deprecated; set {name} instead. "
            f"The old name is read until {LEGACY_REMOVAL_VERSION}.",
        )
        return os.environ[legacy]
    return default


def env_is_set(name: str) -> bool:
    return getenv(name) is not None


def secret_env(name: str, default: str | None = None) -> str | None:
    """Read a credential env var whose *name* comes from config.

    A name stored in an existing config (``api_key_env``, ``credential_env``) is
    read verbatim, whatever its prefix: it is stored data. An ``OKTO_NEURON_*``
    name, which is what 0.3.0 presets and new credentials default to, falls back
    to the matching ``MARGINALIA_*`` variable with one warning, so an upgraded
    machine whose env file or shell still has the old name keeps working.
    """
    if name.startswith(ENV_PREFIX):
        return getenv(name, default)
    return os.environ.get(name, default)


# --------------------------------------------------------------------------
# App home (D5)
# --------------------------------------------------------------------------

APP_HOME_DIRNAME = ".okto-neuron"
LEGACY_APP_HOME_DIRNAME = ".marginalia"
APP_CONFIG_FILENAME = "okto-neuron.toml"
LEGACY_APP_CONFIG_FILENAME = "marginalia.toml"
#: Per-vault state directory. Stored data: kept permanently under this name.
VAULT_STATE_DIRNAME = ".marginalia"
VAULT_CONFIG_FILENAME = "okto-neuron.yaml"
LEGACY_VAULT_CONFIG_FILENAMES = ("marginalia.yaml", "marginalia.yml")
#: Project marker an agent reads to pick a vault (MCP tool instructions).
PROJECT_VAULT_MARKER = ".okto-neuron-vault"
LEGACY_PROJECT_VAULT_MARKER = ".marginalia-vault"
MOVED_TO_FILENAME = "MOVED_TO"
#: App-level files copied by :func:`migrate_legacy_app_home`, as
#: ``(legacy name, new name)``. Vaults, backups, runtime pids, daemon tokens and
#: logs are deliberately not copied.
MIGRATED_APP_FILES: tuple[tuple[str, str], ...] = (
    (LEGACY_APP_CONFIG_FILENAME, APP_CONFIG_FILENAME),
    ("env", "env"),
    ("providers.yaml", "providers.yaml"),
    ("defaults.yaml", "defaults.yaml"),
)


def app_home() -> Path:
    """The user-owned application home: ``~/.okto-neuron``."""
    return Path.home() / APP_HOME_DIRNAME


def legacy_app_home() -> Path:
    """The pre-0.3.0 application home: ``~/.marginalia``."""
    return Path.home() / LEGACY_APP_HOME_DIRNAME


def legacy_runtime_root() -> Path:
    """Where a pre-0.3.0 daemon keeps its lock: ``~/.marginalia/runtime``.

    A 0.2.0 daemon that is still running during an upgrade holds its PID lock
    here, not under ``~/.okto-neuron/runtime``. ``serve`` refuses to start a
    second daemon while one is live here, and ``stop`` finds and stops it.
    """
    return legacy_app_home() / "runtime"


def legacy_daemon_running_message(pid: int, legacy_root: Path) -> str:
    return (
        f"a Marginalia daemon from before the rename to {PRODUCT_NAME} is still running "
        f"(pid={pid}, lock {legacy_root}).\n"
        f"Starting {CLI_NAME} now would run two daemons over the same vaults.\n"
        f"Stop the old daemon first with `{CLI_NAME} stop`, then start again."
    )


def app_file(name: str, legacy_name: str | None = None) -> Path:
    """Where an app-level file lives, new home first.

    Returns the new-home path when that file exists, else the legacy-home path
    when *that* exists (so reads and writes keep using the one real file on an
    upgraded machine that has not migrated yet), else the new-home path.
    """
    new = app_home() / name
    if new.exists():
        return new
    legacy = legacy_app_home() / (legacy_name or name)
    if legacy.exists():
        return legacy
    return new


def default_vault_roots() -> list[Path]:
    """Vault roots used when the app config does not name any.

    ``~/.marginalia/vaults`` comes first whenever it exists, so an upgraded
    install keeps finding, and keeps creating, named vaults exactly where 0.2.0
    put them (D5). A fresh install only has ``~/.okto-neuron/vaults``.
    """
    roots: list[Path] = []
    legacy = legacy_app_home() / "vaults"
    if legacy.is_dir():
        roots.append(legacy.resolve(strict=False))
    roots.append((app_home() / "vaults").resolve(strict=False))
    return roots


def migrate_legacy_app_home() -> dict[str, object]:
    """Copy app-level files from ``~/.marginalia`` to ``~/.okto-neuron``.

    Idempotent. Never moves or copies vaults, and never touches anything else
    already under ``~/.okto-neuron`` (for example the MVP's ``grafx``
    directory): an existing destination file is kept as is. Leaves a
    ``MOVED_TO`` pointer in the legacy home. Returns a summary.
    """
    legacy = legacy_app_home()
    new = app_home()
    summary: dict[str, object] = {
        "legacy_home": str(legacy),
        "app_home": str(new),
        "copied": [],
        "kept": [],
        "vaults_left_in_place": (legacy / "vaults").is_dir(),
        "pointer": None,
    }
    if not legacy.is_dir():
        summary["status"] = "no_legacy_home"
        return summary
    new.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied: list[str] = []
    kept: list[str] = []
    for old_name, new_name in MIGRATED_APP_FILES:
        src = legacy / old_name
        dst = new / new_name
        if not src.is_file():
            continue
        if dst.exists():
            kept.append(new_name)
            continue
        shutil.copy2(src, dst)
        copied.append(new_name)
    pointer = legacy / MOVED_TO_FILENAME
    if not pointer.exists():
        pointer.write_text(
            f"{new}\n"
            "\n"
            f"Marginalia is now {PRODUCT_NAME}. App-level files ("
            + ", ".join(old for old, _ in MIGRATED_APP_FILES)
            + f") were copied to {new} by version 0.3.0.\n"
            "Vaults under this directory were NOT moved: document ids depend on\n"
            "their absolute paths, so they keep working from here.\n",
            encoding="utf-8",
        )
    summary["copied"] = copied
    summary["kept"] = kept
    summary["pointer"] = str(pointer)
    summary["status"] = "migrated"
    return summary


# --------------------------------------------------------------------------
# Vault config file
# --------------------------------------------------------------------------


def vault_config_path(vault_root: Path | str) -> Path:
    """The vault's config file: an existing one wins, new vaults get the new name.

    A vault created before 0.3.0 keeps its ``marginalia.yaml`` for good; it is
    read and written under that name and never renamed.
    """
    root = Path(vault_root)
    new = root / VAULT_CONFIG_FILENAME
    if new.exists():
        return new
    for legacy_name in LEGACY_VAULT_CONFIG_FILENAMES:
        legacy = root / legacy_name
        if legacy.exists():
            return legacy
    return new


def existing_vault_config(vault_root: Path | str) -> Path | None:
    candidate = vault_config_path(vault_root)
    return candidate if candidate.exists() else None


def is_vault_config_filename(name: str) -> bool:
    return name == VAULT_CONFIG_FILENAME or name in LEGACY_VAULT_CONFIG_FILENAMES


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

VAULT_HEADER = "X-Okto-Neuron-Vault"
LEGACY_VAULT_HEADER = "X-Marginalia-Vault"
VERSION_FIELD = "okto_neuron_version"
LEGACY_VERSION_FIELD = "marginalia_version"


def vault_header_value(headers: object) -> str | None:
    """Read the vault selector from a case-insensitive headers mapping."""
    getter = getattr(headers, "get")
    value = getter(VAULT_HEADER.lower())
    if value is None:
        value = getter(LEGACY_VAULT_HEADER.lower())
    return value


def version_payload(version: str) -> dict[str, str]:
    return {VERSION_FIELD: version, LEGACY_VERSION_FIELD: version}


def version_from_payload(payload: dict[str, object]) -> object:
    value = payload.get(VERSION_FIELD)
    return value if value is not None else payload.get(LEGACY_VERSION_FIELD)


# --------------------------------------------------------------------------
# Process detection
# --------------------------------------------------------------------------

CONSOLE_SCRIPT_NAMES = frozenset(
    {CLI_NAME, f"{CLI_NAME}.exe", LEGACY_CLI_NAME, f"{LEGACY_CLI_NAME}.exe"}
)
#: ``python -m <module>`` spellings of the CLI. ``marginalia.cli`` is how a
#: pre-0.3.0 daemon was spawned (``lifecycle.py`` builds ``-m <pkg>.cli serve``),
#: so it must stay here for a 0.3.0 process to recognise a 0.2.0 daemon.
LEGACY_CLI_MODULE_NAME = "marginalia.cli"
CLI_MODULE_NAMES = frozenset({"okto_neuron.cli", LEGACY_CLI_MODULE_NAME})


def is_console_script(argv0: str) -> bool:
    return argv0.replace("\\", "/").rsplit("/", 1)[-1].casefold() in CONSOLE_SCRIPT_NAMES


# --------------------------------------------------------------------------
# Backend plugin entry points
# --------------------------------------------------------------------------

GRAPH_BACKENDS_GROUP = "okto_neuron.graph_backends"
INDEX_BACKENDS_GROUP = "okto_neuron.index_backends"
LEGACY_GRAPH_BACKENDS_GROUP = "marginalia.graph_backends"
LEGACY_INDEX_BACKENDS_GROUP = "marginalia.index_backends"


def iter_entry_points(group: str, legacy_group: str, *, name: str | None = None) -> Iterator[EntryPoint]:
    """Entry points from the new group, then the legacy one; first name wins."""
    seen: set[str] = set()
    for selected in (group, legacy_group):
        eps = entry_points(group=selected)
        if name is not None:
            eps = eps.select(name=name)
        for entry_point in eps:
            if entry_point.name in seen:
                continue
            seen.add(entry_point.name)
            yield entry_point


# --------------------------------------------------------------------------
# Legacy CLI alias
# --------------------------------------------------------------------------


def warn_legacy_cli() -> None:
    """Printed once when the ``marginalia`` console script is used."""
    print(
        f"warning: the 'marginalia' command is now '{CLI_NAME}'. "
        f"The old name keeps working until {LEGACY_REMOVAL_VERSION}.",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# Kept legacy namespaces (stored or exported data; never renamed)
# --------------------------------------------------------------------------

#: MLflow span attribute / tag namespace. Unchanged so traces written before and
#: after the rename answer the same queries; only the default experiment moved.
TELEMETRY_ATTRIBUTE_PREFIX = "marginalia."
#: JSON-LD export vocabulary. Exported term IRIs are identities other tools
#: store, so they do not change with the product name.
JSONLD_VOCABULARY_IRI = "https://oktolabs.ai/marginalia#"
JSONLD_VOCABULARY_PREFIX = "marginalia"
#: ``originator`` the ChatGPT-subscription provider sends (llm/_chatgpt.py).
#: Unchanged: a new value could not be checked against the live endpoint for
#: this release, and the provider is opt-in and experimental.
CHATGPT_ORIGINATOR = "marginalia"


#: The only places a ``marginalia`` name may appear in a string literal under
#: ``src/`` outside this module, as ``{regex: reason}``. The grep gate in
#: ``tests/test_rename_compat.py`` fails on any occurrence not covered by one of
#: these patterns. Everything here is stored data, an exported identity, an LLM
#: wire contract, or a mention of a legacy name that is still read.
KEPT_LEGACY_LITERALS: dict[str, str] = {
    r"\.marginalia(?![\w-])": "per-vault state directory `.marginalia/` and the legacy app home "
    "`~/.marginalia` (stored data, D5)",
    r"__marginalia_schema__": "graph schema marker table (stored data)",
    r"marginalia_(toml|yaml)_version": "version keys inside existing config files (stored data)",
    r"marginalia_version": "snapshot manifest field and pre-0.3.0 HTTP field (stored data, still "
    "emitted next to okto_neuron_version)",
    r"marginalia\.(toml|ya?ml)": "pre-0.3.0 config file names, still read",
    r"\.marginalia-vault": "pre-0.3.0 project vault marker, still honoured by agents",
    r"X-Marginalia-Vault": "pre-0.3.0 HTTP header, still accepted and emitted",
    r"MARGINALIA_": "pre-0.3.0 env var prefix, still read with a warning; user-stored api_key_env "
    "values keep it",
    r"marginalia\.(graph|index)_backends": "pre-0.3.0 backend entry-point groups, still read",
    r"marginalia\.(step|degraded|degraded_reason|provider|model|document_id|committed|queued|"
    r"claims_minted|blocks_total|provider_retries|provider_error|synthesis_status|"
    r"synthesis_retries|citations|mode|hits)\b": "MLflow span attribute and tag keys "
    "(TELEMETRY_ATTRIBUTE_PREFIX; stored trace data)",
    r"marginalia_(predicate_resolution|predicate_judge|completion_probe|relation_curator_batch|"
    r"candidate_curator_batch|type_adjudication|cluster_verdict|merge_verdict|candidate_curator|"
    r"relation_curator|companion_triage_batch|companion_triage|extraction)\b": "structured-output "
    "schema names on the LLM wire; unchanged so recorded replays and ledgers keep matching",
    r"marginalia-reconcile": "reconcile decision agent id (stored in ledgers)",
    r"x-marginalia-standards": "JSON Schema vendor extension in exported primitive schemas",
    r"marginalia_(node|edge)_identity": "Neo4j constraint names already created in existing "
    "databases",
    r"marginalia_retrieval": "arm name inside stored semantic-acceptance reports",
    r"Marginalia (deterministic extraction system agent|LLM extraction agent|schema v)": "content "
    "of system nodes already stored in existing graphs",
    r"You are Marginalia's (candidate|relationship) curator|Marginalia will still require|"
    r"Marginalia structured response contract": "LLM "
    "prompt text; it is hashed into claim ids and semantic fingerprints, so it must not change",
    r"^marginalia_kg_query$": "name exported by the retired mcp_server compatibility module",
}
