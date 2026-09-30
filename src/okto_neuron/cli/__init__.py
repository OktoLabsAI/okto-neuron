"""Click CLI entry point."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

import click
from click.core import ParameterSource
import yaml

from okto_neuron._compat import (
    BRAND_LINE,
    CLI_NAME,
    install_cli_warning_format,
    legacy_daemon_running_message,
    legacy_runtime_root,
    migrate_legacy_app_home,
    warn_legacy_cli,
)
from okto_neuron._compat import version_from_payload as _compat_version_from_payload
from okto_neuron._compat import version_payload as _compat_version_payload
from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._compat import secret_env as _secret_env
from okto_neuron._compat import vault_config_path
from okto_neuron import __version__
from okto_neuron.cli._client import (
    ClientUnreachable,
    DEFAULT_TIMEOUT_SECONDS,
    EXIT_ASK_DEGRADED,
    EXIT_UNREACHABLE,
    ServerError,
    UNREACHABLE_MESSAGE_TEMPLATE,
    post as _client_post,
    request as _client_request,
    resolve_endpoint,
)
from okto_neuron.cli.models import app as models_app
from okto_neuron.config._app_config import default_app_home
from okto_neuron.config._vault import DEFAULT_NEW_VAULT_BACKEND
from okto_neuron.detectors import DETECTOR_NAMES, run_detector
from okto_neuron.errors import OktoNeuronError, OptionalDependencyError
from okto_neuron.onboarding import (
    MENU_PRESETS,
    PROVIDER_PRESETS,
    auto_detect_loopback,
    disabled_llm_patch,
    discover_models,
    get_provider_preset,
    llm_config_patch,
    load_user_env_file,
    verify_onboarding_completion,
    write_user_env_secret,
)
from okto_neuron.schema.support.finding import Finding
from okto_neuron.store.vault_writer import vault_writer
from okto_neuron.vault import Vault
from okto_neuron.vault_registry import (
    AmbiguousVaultNameError,
    ensure_global_layout,
    is_vault,
    list_vaults,
    mark_managed_vault,
    resolve_vault_reference,
    set_default_vault,
    vault_path_for_name,
)


_SERVER_STARTUP_TIMEOUT_SECONDS = 60.0
# First line stays "<command> <version>" so scripts can compare it; the second
# is the attribution the license addendum asks for.
_VERSION_MESSAGE = "%(prog)s %(version)s\n" + BRAND_LINE


def _warn_if_telemetry_unavailable() -> None:
    """One stderr line when MLflow export is requested but cannot happen."""
    from okto_neuron.llm._telemetry import missing_mlflow_warning

    message = missing_mlflow_warning()
    if message:
        click.echo(f"warning: {message}", err=True)


class OktoNeuronGroup(click.Group):
    """Top-level Click group with the RFC failure taxonomy dispatcher."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        debug = _debug_requested(kwargs.get("args"))
        standalone_mode = kwargs.get("standalone_mode", True)
        try:
            return super().main(*args, **kwargs)
        except (click.ClickException, click.exceptions.Exit):
            raise
        except AmbiguousVaultNameError as exc:
            click.echo(f"Error: {exc}", err=True)
            if standalone_mode:
                raise SystemExit(2) from None
            raise click.exceptions.Exit(2) from None
        except OktoNeuronError as exc:
            click.echo(exc.user_message(), err=True)
            if debug:
                _print_marginalia_debug(exc)
            if standalone_mode:
                raise SystemExit(exc.EXIT_CODE) from exc
            raise click.exceptions.Exit(exc.EXIT_CODE) from exc
        except Exception as exc:
            traceback.print_exception(
                type(exc),
                exc,
                exc.__traceback__,
                file=sys.stderr,
                chain=debug,
            )
            if standalone_mode:
                raise SystemExit(1) from exc
            raise click.exceptions.Exit(1) from exc


@click.group(
    cls=OktoNeuronGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, message=_VERSION_MESSAGE)
@click.option("--debug", is_flag=True, is_eager=True, help="Print chained tracebacks.")
@click.pass_context
def app(ctx: click.Context, debug: bool) -> None:
    """Okto Neuron by Okto Labs - local-first knowledge graph memory for agents."""
    install_cli_warning_format()
    load_user_env_file()
    if ctx.invoked_subcommand != "serve":  # serve reports it itself, once per stream
        _warn_if_telemetry_unavailable()
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


@click.group(
    cls=OktoNeuronGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, message=_VERSION_MESSAGE)
@click.option("--debug", is_flag=True, is_eager=True, help="Print chained tracebacks.")
@click.pass_context
def kg_cli(ctx: click.Context, debug: bool) -> None:
    """Compatibility CLI with graph commands flattened at the top level."""
    install_cli_warning_format()
    load_user_env_file()
    if ctx.invoked_subcommand != "serve":  # serve reports it itself, once per stream
        _warn_if_telemetry_unavailable()
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


app.add_command(models_app, name="models")


def legacy_app() -> None:
    """The pre-0.3.0 command name: same commands, plus a rename warning (removed in 0.5)."""
    warn_legacy_cli()
    app()


@app.command("migrate-home", hidden=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def migrate_home_command(as_json: bool) -> None:
    """Copy app-level files from a pre-0.3.0 ~/.marginalia into ~/.okto-neuron.

    Vaults are never moved or copied: document ids depend on absolute paths, so
    vaults under ~/.marginalia/vaults keep working from where they are. Safe to
    run more than once. The installer runs it on upgrade.
    """
    summary = migrate_legacy_app_home()
    if as_json:
        click.echo(json.dumps(summary, sort_keys=True))
        return
    if summary["status"] == "no_legacy_home":
        click.echo(f"nothing to migrate: {summary['legacy_home']} does not exist")
        return
    copied = ", ".join(summary["copied"]) or "none"
    kept = ", ".join(summary["kept"]) or "none"
    click.echo(f"copied to {summary['app_home']}: {copied}; already present: {kept}")
    if summary["vaults_left_in_place"]:
        click.echo(f"vaults left in place under {summary['legacy_home']}/vaults")


@app.command("version")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def version_command(as_json: bool) -> None:
    """Print the installed Okto Neuron version."""
    if as_json:
        click.echo(json.dumps(_compat_version_payload(__version__), sort_keys=True))
        return
    click.echo(f"{CLI_NAME} {__version__}")
    click.echo(BRAND_LINE)


@app.group("provider")
def provider_group() -> None:
    """Sign in to subscription-backed LLM providers.

    For providers that authenticate with an account login rather than an API
    key. The credential is per user, not per vault, which is why this is not
    part of `onboard` (that configures one vault's LLM block).
    """


def _print_chatgpt_summary(summary: dict[str, object]) -> None:
    for label, key in (
        ("credential", "path"),
        ("account", "email"),
        ("plan", "plan"),
        ("subscription active until", "subscription_active_until"),
        ("access token expires", "access_token_expires_at"),
    ):
        click.echo(f"{label}: {summary.get(key) if summary.get(key) is not None else 'unknown'}")
    if summary.get("access_token_expired"):
        click.echo(
            "note: the access token has expired; litellm refreshes it on the next call "
            "using the stored refresh token."
        )


@provider_group.command("login")
@click.argument("provider", type=click.Choice(["chatgpt"]))
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Replace an existing credential (the old file is kept as a .bak copy).",
)
def provider_login(provider: str, force: bool) -> None:
    """Log in with a ChatGPT subscription via a device code (interactive).

    Writes Okto Neuron's own credential file (default
    ~/.config/litellm/chatgpt/auth.json, or $CHATGPT_TOKEN_DIR). Never shares
    a directory with the codex or pi CLIs. Using the provider afterwards still
    needs OKTO_NEURON_ENABLE_CHATGPT=1 — see docs/remote-providers.md.
    Experimental: OpenAI's terms for ChatGPT subscriptions may not allow
    access from third-party tools; read them before using it.
    """
    from okto_neuron.llm import LLMProviderError
    from okto_neuron.llm._chatgpt import CHATGPT_TERMS_WARNING, interactive_login

    click.echo(f"warning: {CHATGPT_TERMS_WARNING}", err=True)

    try:
        summary = interactive_login(force=force)
    except LLMProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    if summary.get("already_provisioned"):
        click.echo("already logged in (use --force to replace this credential):")
    else:
        click.echo("logged in:")
    _print_chatgpt_summary(summary)


@provider_group.command("status")
@click.argument("provider", type=click.Choice(["chatgpt"]))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def provider_status(provider: str, as_json: bool) -> None:
    """Show the stored credential without touching it. Exit 1 if unusable."""
    from okto_neuron.llm import LLMProviderError
    from okto_neuron.llm._chatgpt import (
        credential_path,
        credential_summary,
        opt_in_enabled,
        refuse_foreign_token_dir,
    )

    try:
        refuse_foreign_token_dir()
        summary = credential_summary()
    except LLMProviderError as exc:
        if as_json:
            click.echo(
                json.dumps(
                    {"ok": False, "path": credential_path(), "error": str(exc)}, sort_keys=True
                )
            )
            raise SystemExit(1) from exc
        raise click.ClickException(str(exc)) from exc
    summary["opt_in_enabled"] = opt_in_enabled()
    if as_json:
        click.echo(json.dumps({"ok": True, **summary}, sort_keys=True))
        return
    _print_chatgpt_summary(summary)
    click.echo(
        "OKTO_NEURON_ENABLE_CHATGPT: "
        + ("set" if summary["opt_in_enabled"] else "NOT set (the provider refuses to run)")
    )


@app.group("vault")
def vault_group() -> None:
    """Manage named vaults under the configured Okto Neuron home."""


@vault_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def vault_list(as_json: bool) -> None:
    """List discovered vaults."""
    ensure_global_layout()
    current = resolve_vault_reference(None)
    entries = list_vaults(current=current if is_vault(current) else None)
    payload = {
        "status": "ok",
        "current": next((entry.to_json() for entry in entries if entry.current), None),
        "vaults": [entry.to_json() for entry in entries],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not entries:
        click.echo("(no vaults)")
        return
    for entry in entries:
        marker = "*" if entry.current else " "
        click.echo(f"{marker} {entry.name}\t[{entry.backend}]\t{entry.path}")


@vault_group.command("create")
@click.argument("name")
@click.option("--packs", default="core,research,personal", help="Comma-separated packs.")
@click.option("--embedder", default="fastembed", help="fastembed | stub.")
@click.option(
    "--backend",
    default=DEFAULT_NEW_VAULT_BACKEND,
    show_default=True,
    help="Graph backend to pin the vault to.",
)
@click.option("--use/--no-use", "make_current", default=True, help="Set as default vault.")
@click.option(
    "--accept-experimental",
    is_flag=True,
    default=False,
    help="Deprecated, no longer required; kept for compatibility.",
)
@click.option(
    "--storage-uri",
    default=None,
    help="Remote storage endpoint URI for a non-Ladybug --backend (e.g. bolt://host:7687).",
)
@click.option(
    "--storage-credential-env",
    default=None,
    help="Name of the env var holding the storage backend's credential.",
)
@click.option(
    "--storage-database",
    default=None,
    help="Database/keyspace name for a server-side --backend (e.g. Neo4j database).",
)
@click.option(
    "--allow-remote-db",
    is_flag=True,
    default=False,
    help="Confirm a non-loopback --storage-uri.",
)
def vault_create(
    name: str,
    packs: str,
    embedder: str,
    backend: str,
    make_current: bool,
    accept_experimental: bool,
    storage_uri: str | None,
    storage_credential_env: str | None,
    storage_database: str | None,
    allow_remote_db: bool,
) -> None:
    """Create a named vault in the default vault root (~/.okto-neuron/vaults)."""
    ensure_global_layout()
    try:
        path = vault_path_for_name(name)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="name") from exc
    if is_vault(path):
        click.echo(f"vault already exists: {path}", err=True)
        raise click.exceptions.Exit(1)
    _resolve_and_pin_backend(path, backend)
    # accept_experimental (deprecated) is intentionally unused below: no
    # backend is D-12-gated any more, so the flag is accepted-and-ignored
    # for CLI compatibility with old scripts.
    if storage_uri is not None:
        _confirm_remote_storage_endpoint(
            storage_uri, allow_remote_db=allow_remote_db, yes=allow_remote_db, interactive=False
        )
    pack_list = [pack.strip() for pack in packs.split(",") if pack.strip()]
    # A new path: nobody else holds it, so this takes the lease instead of refusing.
    with vault_writer(path, "vault create"):
        Vault.scaffold(
            path,
            packs=pack_list,
            embedder=embedder,
            allow_external_sources=True,
            backend=backend,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=allow_remote_db,
        )
    mark_managed_vault(path, name=name)
    if make_current:
        set_default_vault(path)
    click.echo(f"created vault {name} at {path}")


@vault_group.command("use")
@click.argument("vault")
def vault_use(vault: str) -> None:
    """Set the default vault by name or path."""
    target = resolve_vault_reference(vault)
    if not is_vault(target):
        click.echo(f"vault not found: {target}", err=True)
        raise click.exceptions.Exit(2)
    set_default_vault(target)
    click.echo(f"current vault: {target}")


@vault_group.command("current")
def vault_current() -> None:
    """Print the configured default vault."""
    target = resolve_vault_reference(None)
    if not is_vault(target):
        click.echo("no current vault configured", err=True)
        raise click.exceptions.Exit(1)
    click.echo(str(target))


@app.group("kg")
def kg_group() -> None:
    """Knowledge graph operations."""


@app.group("quality")
def quality_group() -> None:
    """Read-only quality evidence and diagnostics."""


@quality_group.command("ledger")
@click.argument("vault", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--run-id",
    "run_ids",
    multiple=True,
    required=True,
    help="Explicit candidate-ledger run id; repeat to select multiple runs.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write deterministic JSON to this path instead of stdout.",
)
@click.option(
    "--adjudication",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="semantic_adjudication.v1 JSON document to bind and measure.",
)
@click.option(
    "--registered-predicates",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="JSON list of predicate labels supplied to the registry-coverage measurement.",
)
@click.option(
    "--recall-samples",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="JSON list of recall_cost.v1 samples to bind and aggregate.",
)
@click.option("--force", is_flag=True, help="Replace an existing output file.")
def quality_ledger_command(
    vault: Path,
    run_ids: tuple[str, ...],
    output: Path | None,
    adjudication: Path | None,
    registered_predicates: Path | None,
    recall_samples: Path | None,
    force: bool,
) -> None:
    """Measure explicit pre-commit runs from a complete ledger snapshot.

    This command never opens or mutates the graph. Its report is Phase 1a
    evidence and remains non-authoritative for stored graph semantics.
    """

    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.semantic_quality import evaluate_ledger_scan

    selected = tuple(run_id.strip() for run_id in run_ids)
    if any(not run_id for run_id in selected):
        raise click.BadParameter("run ids must not be blank", param_hint="--run-id")
    vault_path = vault.expanduser().resolve(strict=False)
    ledger = CandidateLedger(vault_path / ".marginalia")
    if not ledger.path.is_file():
        raise click.ClickException(f"candidate ledger not found: {ledger.path}")
    adjudication_payload = _read_json_evidence(adjudication, expected="object")
    registered_payload = _read_json_evidence(registered_predicates, expected="list")
    recall_payload = _read_json_evidence(recall_samples, expected="list")
    if registered_payload is not None and any(
        not isinstance(value, str) or not value.strip() for value in registered_payload
    ):
        raise click.ClickException("registered-predicates must contain only non-empty strings")
    try:
        report = evaluate_ledger_scan(
            ledger.scan(),
            selected,
            adjudication=adjudication_payload,
            registered_predicates=registered_payload,
            recall_samples=recall_payload,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_quality_json(report, output=output, force=force)


@quality_group.command("churn")
@click.argument(
    "before",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.argument(
    "after",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write deterministic JSON to this path instead of stdout.",
)
@click.option("--sample-limit", type=click.IntRange(min=0), default=20, show_default=True)
@click.option(
    "--require-stable",
    is_flag=True,
    help="Exit non-zero when any identity, predicate, or relation member changed.",
)
@click.option("--force", is_flag=True, help="Replace an existing output file.")
def quality_churn_command(
    before: Path,
    after: Path,
    output: Path | None,
    sample_limit: int,
    require_stable: bool,
    force: bool,
) -> None:
    """Compare two semantic-quality snapshots without opening either graph."""

    from okto_neuron.semantic_quality import compare_semantic_snapshots

    before_payload = _read_json_evidence(before, expected="object")
    after_payload = _read_json_evidence(after, expected="object")
    before_snapshot = _quality_semantic_snapshot(before_payload, source=before)
    after_snapshot = _quality_semantic_snapshot(after_payload, source=after)
    try:
        result = compare_semantic_snapshots(
            before_snapshot,
            after_snapshot,
            sample_limit=sample_limit,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_quality_json(result, output=output, force=force)
    if require_stable and not result["stable"]:
        raise click.exceptions.Exit(1)


@quality_group.command("acceptance")
@click.argument(
    "bundle",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write deterministic JSON to this path instead of stdout.",
)
@click.option(
    "--require-ready",
    is_flag=True,
    help="Exit non-zero unless every ADR 0040 gate and the public diagnostic are complete.",
)
@click.option("--force", is_flag=True, help="Replace an existing output file.")
def quality_acceptance_command(
    bundle: Path,
    output: Path | None,
    require_ready: bool,
    force: bool,
) -> None:
    """Evaluate an immutable ADR 0040 multi-corpus evidence bundle."""

    from okto_neuron.semantic_acceptance import evaluate_semantic_acceptance

    payload = _read_json_evidence(bundle, expected="object")
    try:
        result = evaluate_semantic_acceptance(payload)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_quality_json(result, output=output, force=force)
    if require_ready and not result["acceptance_ready"]:
        raise click.exceptions.Exit(1)


@quality_group.command("acceptance-status")
@click.argument(
    "collection",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write deterministic JSON to this path instead of stdout.",
)
@click.option(
    "--require-ready",
    is_flag=True,
    help="Exit non-zero unless every pinned collection artifact is ready.",
)
@click.option("--force", is_flag=True, help="Replace an existing output file.")
def quality_acceptance_status_command(
    collection: Path,
    output: Path | None,
    require_ready: bool,
    force: bool,
) -> None:
    """Inspect a pinned ADR 0040 collection without materializing its bundle."""

    from okto_neuron.semantic_acceptance import inspect_semantic_acceptance_collection

    payload = _read_json_evidence(collection, expected="object")
    try:
        result = inspect_semantic_acceptance_collection(
            payload,
            base_dir=collection.resolve().parent,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_quality_json(result, output=output, force=force)
    if require_ready and result["status"] != "ready":
        raise click.exceptions.Exit(1)


@quality_group.command("acceptance-collect")
@click.argument(
    "collection",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Write the immutable materialized bundle to this path.",
)
@click.option("--force", is_flag=True, help="Replace an existing output file.")
def quality_acceptance_collect_command(
    collection: Path,
    output: Path,
    force: bool,
) -> None:
    """Materialize a complete pinned ADR 0040 acceptance collection."""

    from okto_neuron.semantic_acceptance import materialize_semantic_acceptance_collection

    payload = _read_json_evidence(collection, expected="object")
    try:
        result = materialize_semantic_acceptance_collection(
            payload,
            base_dir=collection.resolve().parent,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_quality_json(result, output=output, force=force)


def _read_json_evidence(path: Path | None, *, expected: str) -> Any:
    """Load one explicit quality-evidence file and fail closed on its outer shape."""

    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"cannot read quality evidence {path}: {exc}") from exc
    valid = isinstance(value, dict) if expected == "object" else isinstance(value, list)
    if not valid:
        raise click.ClickException(f"quality evidence {path} must contain a JSON {expected}")
    return value


def _emit_quality_json(
    value: dict[str, Any],
    *,
    output: Path | None,
    force: bool,
) -> None:
    """Emit one deterministic quality artifact without partial replacement."""

    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        click.echo(rendered, nl=False)
        return
    output_path = output.expanduser().resolve(strict=False)
    if output_path.exists() and not force:
        raise click.ClickException(
            f"output already exists: {output_path}; pass --force to replace it"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    click.echo(str(output_path))


def _quality_semantic_snapshot(value: dict[str, Any], *, source: Path) -> dict[str, Any]:
    """Extract the snapshot from one of the three product-owned report envelopes."""

    if value.get("schema_version") == "semantic_snapshot.v1":
        snapshot: object = value
    elif isinstance(value.get("semantic_snapshot"), dict):
        snapshot = value["semantic_snapshot"]
    elif isinstance(value.get("semantic_quality"), dict):
        snapshot = value["semantic_quality"].get("semantic_snapshot")
    else:
        snapshot = None
    if not isinstance(snapshot, dict):
        raise click.ClickException(f"quality evidence {source} has no semantic_snapshot object")
    return snapshot


def _run_kg_command(name: str, *args: Any, **kwargs: Any) -> int:
    """Load the optional graph implementation only when a graph command runs."""
    try:
        from okto_neuron.cli import kg as kg_commands
    except ModuleNotFoundError as exc:
        if exc.name != "ladybug":
            raise
        raise OktoNeuronError(
            "Ladybug storage is not installed; install okto-neuron[ladybug] to run graph commands",
            cause=exc,
        ) from exc
    command = getattr(kg_commands, name)
    return command(*args, **kwargs)


@kg_group.command("init")
@click.argument("vault", type=click.Path(path_type=Path))
@click.option(
    "--backend",
    default=DEFAULT_NEW_VAULT_BACKEND,
    show_default=True,
    help="Graph backend to pin a new vault to (ignored if the vault already exists).",
)
@click.option(
    "--accept-experimental",
    is_flag=True,
    default=False,
    help="Deprecated, no longer required; kept for compatibility.",
)
@click.option(
    "--storage-uri",
    default=None,
    help="Remote storage endpoint URI for a non-Ladybug --backend (e.g. bolt://host:7687).",
)
@click.option(
    "--storage-credential-env",
    default=None,
    help="Name of the env var holding the storage backend's credential.",
)
@click.option(
    "--storage-database",
    default=None,
    help="Database/keyspace name for a server-side --backend (e.g. Neo4j database).",
)
@click.option(
    "--allow-remote-db",
    is_flag=True,
    default=False,
    help="Confirm a non-loopback --storage-uri.",
)
@click.option("--debug", is_flag=True, is_eager=True, help="Print chained tracebacks.")
@click.pass_context
def kg_init_command(
    ctx: click.Context,
    vault: Path,
    backend: str,
    accept_experimental: bool,
    storage_uri: str | None,
    storage_credential_env: str | None,
    storage_database: str | None,
    allow_remote_db: bool,
    debug: bool,
) -> None:
    """Initialize a vault graph."""
    if debug:
        ctx.find_root().ensure_object(dict)
        ctx.find_root().obj["debug"] = True
    from click.core import ParameterSource

    explicit_backend = ctx.get_parameter_source("backend") == ParameterSource.COMMANDLINE
    if storage_uri is not None:
        _confirm_remote_storage_endpoint(
            storage_uri, allow_remote_db=allow_remote_db, yes=allow_remote_db, interactive=False
        )
    ctx.exit(
        _run_kg_command(
            "kg_init",
            vault,
            backend=backend if explicit_backend else None,
            accept_experimental=accept_experimental,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=allow_remote_db,
        )
    )


@kg_group.command("rebuild")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.pass_context
def kg_rebuild_command(ctx: click.Context, vault: Path | None) -> None:
    """Rebuild a vault graph."""
    ctx.exit(_run_kg_command("kg_rebuild", vault))


@kg_group.command("reembed")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.pass_context
def kg_reembed_command(ctx: click.Context, vault: Path | None) -> None:
    """Recompute every vector at the configured embedding width (no re-extraction).

    Vectors-only: replays the existing graph's stored text through the configured
    embedder and rebuilds the graph at the configured width. Use this after changing
    embedding.provider / model / dimension. ``kg rebuild`` (re-extracts from
    markdown) is unchanged.
    """
    ctx.exit(_run_kg_command("kg_reembed", vault))


@kg_group.command("reindex")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Rebuild even if the index is already current.")
@click.pass_context
def kg_reindex_command(ctx: click.Context, vault: Path | None, force: bool) -> None:
    """Rebuild the search index from the graph, or check it is current.

    Without ``--force``, rebuilds only when the index's generation stamp does
    not match the graph's current content (the same staleness check ``kg
    init``/serve run on open). Makes no LLM calls: text and vectors are read
    straight off each node.
    """
    ctx.exit(_run_kg_command("kg_reindex", vault, force=force))


@kg_group.group("reconcile")
def kg_reconcile_group() -> None:
    """Retroactive entity reconciliation (v0.0.5, ADR 0008).

    Consolidate already-committed look-alike entities. Option A (propose/apply) is
    off-graph + reversible (writes only side-data, NEVER the graph); Option B
    (heal) folds aliases into a FRESH graph via ``kg rebuild``.
    """


@kg_reconcile_group.command("propose")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.option("--type", "node_type", default=None, help="Scope to one node type.")
@click.option(
    "--cluster-judge",
    "cluster_judge",
    is_flag=True,
    default=False,
    help="Use the compare/select prompt (falls back to pairwise).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def kg_reconcile_propose_command(
    ctx: click.Context,
    vault: Path | None,
    node_type: str | None,
    cluster_judge: bool,
    as_json: bool,
) -> None:
    """Read-only: emit candidate clusters + verdicts. Writes NOTHING."""
    ctx.exit(
        _run_kg_command(
            "kg_reconcile_propose",
            vault,
            type=node_type,
            use_cluster_judge=cluster_judge,
            as_json=as_json,
        )
    )


@kg_reconcile_group.command("apply")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.option("--type", "node_type", default=None, help="Scope to one node type.")
@click.option(
    "--cluster-judge",
    "cluster_judge",
    is_flag=True,
    default=False,
    help="Use the compare/select prompt (falls back to pairwise).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def kg_reconcile_apply_command(
    ctx: click.Context,
    vault: Path | None,
    node_type: str | None,
    cluster_judge: bool,
    as_json: bool,
) -> None:
    """Auto-merge high-confidence clusters off-graph; queue the rest."""
    ctx.exit(
        _run_kg_command(
            "kg_reconcile_apply",
            vault,
            type=node_type,
            use_cluster_judge=cluster_judge,
            as_json=as_json,
        )
    )


@kg_reconcile_group.group("review")
def kg_reconcile_review_group() -> None:
    """List queued clusters, or confirm/reject one.

    Explicit subcommands (``list`` / ``confirm`` / ``reject``) avoid Click's
    positional-vault-vs-subcommand collision — a group-level optional VAULT
    argument would greedily swallow the subcommand name.
    """


@kg_reconcile_review_group.command("list")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def kg_reconcile_review_list_command(ctx: click.Context, vault: Path | None, as_json: bool) -> None:
    """List queued clusters awaiting confirmation."""
    ctx.exit(_run_kg_command("kg_reconcile_review_list", vault, as_json=as_json))


@kg_reconcile_review_group.command("confirm")
@click.argument("cluster_id")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.pass_context
def kg_reconcile_review_confirm_command(
    ctx: click.Context, cluster_id: str, vault: Path | None
) -> None:
    """Confirm a queued cluster → off-graph AuthorityIndex; dequeue."""
    ctx.exit(_run_kg_command("kg_reconcile_review_confirm", cluster_id, vault))


@kg_reconcile_review_group.command("reject")
@click.argument("cluster_id")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.pass_context
def kg_reconcile_review_reject_command(
    ctx: click.Context, cluster_id: str, vault: Path | None
) -> None:
    """Reject (drop) a queued cluster."""
    ctx.exit(_run_kg_command("kg_reconcile_review_reject", cluster_id, vault))


@kg_reconcile_group.command("heal")
@click.argument("vault", required=False, type=click.Path(path_type=Path))
@click.pass_context
def kg_reconcile_heal_command(ctx: click.Context, vault: Path | None) -> None:
    """Option B: fold aliases into a FRESH graph via kg rebuild + atomic swap."""
    ctx.exit(_run_kg_command("kg_reconcile_heal", vault))


@kg_group.group("snapshot")
def kg_snapshot_group() -> None:
    """Dump / verify / load a backend-agnostic logical graph snapshot (M2a).

    See the internal ADR 0041 plan, decisions D-29, D-30, D-31.
    A snapshot directory (``manifest.json``, ``nodes.jsonl``, ``edges.jsonl``,
    ``embeddings.jsonl``, ``sources/``, ``CHECKSUMS.sha256``) round-trips a
    vault's durable knowledge into a fresh vault of any GraphStore backend.
    """


@kg_snapshot_group.command("dump")
@click.argument("vault", type=click.Path(path_type=Path))
@click.argument("dest", type=click.Path(path_type=Path))
@click.pass_context
def kg_snapshot_dump_command(ctx: click.Context, vault: Path, dest: Path) -> None:
    """Dump VAULT's graph into a fresh logical snapshot directory at DEST."""
    ctx.exit(_run_kg_command("kg_snapshot_dump", vault, dest))


@kg_snapshot_group.command("verify")
@click.argument("src", type=click.Path(path_type=Path))
@click.pass_context
def kg_snapshot_verify_command(ctx: click.Context, src: Path) -> None:
    """Recompute every checksum/count in the snapshot at SRC and report problems."""
    ctx.exit(_run_kg_command("kg_snapshot_verify", src))


@kg_snapshot_group.command("load")
@click.argument("src", type=click.Path(path_type=Path))
@click.argument("vault", type=click.Path(path_type=Path))
@click.option(
    "--skip-embeddings",
    is_flag=True,
    default=False,
    help="Load nodes without their vectors (index rebuild recomputes them).",
)
@click.option(
    "--storage-uri",
    default=None,
    help=(
        "Remote storage endpoint URI for a snapshot whose origin backend needs "
        "one to open at all (e.g. neo4j's bolt://host:7687) -- a snapshot never "
        "carries live connection secrets, so restoring one into the same server "
        "it was dumped from must re-supply this."
    ),
)
@click.option(
    "--storage-credential-env",
    default=None,
    help="Name of an env var holding the storage credential (paired with --storage-uri).",
)
@click.option(
    "--storage-database",
    default=None,
    help="Storage database/namespace name (paired with --storage-uri).",
)
@click.option(
    "--allow-remote-db",
    is_flag=True,
    default=False,
    help="Consent to a non-loopback --storage-uri host.",
)
@click.pass_context
def kg_snapshot_load_command(
    ctx: click.Context,
    src: Path,
    vault: Path,
    skip_embeddings: bool,
    storage_uri: str | None,
    storage_credential_env: str | None,
    storage_database: str | None,
    allow_remote_db: bool,
) -> None:
    """Load the snapshot at SRC into a fresh vault at VAULT."""
    ctx.exit(
        _run_kg_command(
            "kg_snapshot_load",
            src,
            vault,
            skip_embeddings=skip_embeddings,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=allow_remote_db,
        )
    )


# NOTE: the ``kg migrate bridge-edges`` command was REMOVED. Structural heals route
# through ``kg rebuild`` so the complete derived topology is verified in a fresh
# generation before one atomic swap. ``LadybugStore.add_edge`` now rejects topology
# identity reuse and updates mutable payload in place.


@app.command("init")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--packs", default="core,research,personal", help="Comma-separated packs.")
@click.option("--embedder", default="fastembed", help="fastembed | stub.")
@click.option(
    "--backend",
    default=DEFAULT_NEW_VAULT_BACKEND,
    show_default=True,
    help="Graph backend to pin a new vault to.",
)
@click.option(
    "--wipe",
    is_flag=True,
    default=False,
    help="Wipe an existing vault (delete notes, sources, derived state, and graph) then re-init.",
)
@click.option(
    "--accept-experimental",
    is_flag=True,
    default=False,
    help="Deprecated, no longer required; kept for compatibility.",
)
@click.option(
    "--storage-uri",
    default=None,
    help="Remote storage endpoint URI for a non-Ladybug --backend (e.g. bolt://host:7687).",
)
@click.option(
    "--storage-credential-env",
    default=None,
    help="Name of the env var holding the storage backend's credential.",
)
@click.option(
    "--storage-database",
    default=None,
    help="Database/keyspace name for a server-side --backend (e.g. Neo4j database).",
)
@click.option(
    "--allow-remote-db",
    is_flag=True,
    default=False,
    help="Confirm a non-loopback --storage-uri.",
)
@click.pass_context
def init(
    ctx: click.Context,
    path: Path,
    packs: str,
    embedder: str,
    backend: str,
    wipe: bool,
    accept_experimental: bool,
    storage_uri: str | None,
    storage_credential_env: str | None,
    storage_database: str | None,
    allow_remote_db: bool,
) -> None:
    """Create a vault."""
    pack_list = [pack.strip() for pack in packs.split(",") if pack.strip()]
    if storage_uri is not None:
        _confirm_remote_storage_endpoint(
            storage_uri, allow_remote_db=allow_remote_db, yes=allow_remote_db, interactive=False
        )

    if wipe and (vault_config_path(path)).exists():
        from click.core import ParameterSource

        from okto_neuron.store.vault import wipe_vault

        # A wipe deliberately resets the vault, so only the backend *name* is
        # validated here (registry lookup) — the existing pin on disk is about
        # to be discarded, not something a new --backend should be checked
        # against.
        _validate_backend_name(backend)

        explicit_flags = (
            ctx.get_parameter_source("packs") == ParameterSource.COMMANDLINE
            or ctx.get_parameter_source("embedder") == ParameterSource.COMMANDLINE
            or ctx.get_parameter_source("backend") == ParameterSource.COMMANDLINE
        )
        # Refused (exit 5) while the daemon writes this vault: it names the
        # daemon pid and points at POST /api/v1/reset.
        with vault_writer(path, "init --wipe"):
            wipe_vault(path, keep_config=not explicit_flags)
            if explicit_flags:
                Vault.init(
                    path,
                    packs=pack_list,
                    embedding_provider=embedder,
                    backend=backend,
                    storage_uri=storage_uri,
                    storage_credential_env=storage_credential_env,
                    storage_database=storage_database,
                    storage_allow_remote=allow_remote_db,
                )
        click.echo(f"wiped vault at {path}")
        return

    _resolve_and_pin_backend(path, backend)
    # accept_experimental (deprecated) is intentionally unused below: no
    # backend is D-12-gated any more, so the flag is accepted-and-ignored
    # for CLI compatibility with old scripts.
    with vault_writer(path, "init"):
        Vault.init(
            path,
            packs=pack_list,
            embedding_provider=embedder,
            backend=backend,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=allow_remote_db,
        )
    click.echo(f"initialized vault at {path}")


_ONBOARDING_PROVIDER_KEYS = ", ".join(preset.key for preset in PROVIDER_PRESETS)


@app.command("onboard")
@click.option("--vault", "vault_ref", default=None, help="Vault name or path to create/configure.")
@click.option(
    "--provider",
    "provider_key",
    default=None,
    help=f"Provider preset ({_ONBOARDING_PROVIDER_KEYS}).",
)
@click.option(
    "--litellm-provider",
    default=None,
    help="Deprecated. Custom OpenAI-compatible endpoints use provider=openai.",
)
@click.option("--api-base", default=None, help="Provider base URL.")
@click.option("--api-key-env", default=None, help="OKTO_NEURON_* env var name to store in config.")
@click.option(
    "--api-key",
    default=None,
    help="API key to save into ~/.okto-neuron/env. Prefer the interactive prompt.",
)
@click.option("--model", default=None, help="Model id to write into okto-neuron.yaml.")
@click.option(
    "--skip-model-discovery",
    is_flag=True,
    default=False,
    help="Do not call the provider model-list endpoint.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Use defaults/options and never prompt.",
)
@click.option(
    "--reconfigure",
    is_flag=True,
    default=False,
    help="Overwrite an existing explicit LLM config.",
)
@click.option("--yes", is_flag=True, default=False, help="Accept remote-egress confirmations.")
@click.option(
    "--allow-remote-llm",
    is_flag=True,
    default=False,
    help="Allow a validated non-loopback LLM endpoint.",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would change; write nothing."
)
@click.option(
    "--print-summary-json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable summary after the human output.",
)
@click.option(
    "--disable-llm",
    is_flag=True,
    default=False,
    help="Explicitly disable LLM-backed ask/remember for this vault.",
)
@click.option(
    "--backend",
    default=DEFAULT_NEW_VAULT_BACKEND,
    show_default=True,
    help="Graph backend to pin a new vault to.",
)
@click.option(
    "--accept-experimental",
    is_flag=True,
    default=False,
    help="Deprecated, no longer required; kept for compatibility.",
)
@click.option(
    "--storage-uri",
    default=None,
    help="Remote storage endpoint URI for a non-Ladybug --backend (e.g. bolt://host:7687).",
)
@click.option(
    "--storage-endpoint",
    "storage_endpoint",
    default=None,
    help="Alias of --storage-uri for endpoint-style backends (e.g. Neptune).",
)
@click.option(
    "--allow-remote-db",
    is_flag=True,
    default=False,
    help="Allow a validated non-loopback storage endpoint.",
)
@click.option(
    "--storage-credential-env",
    default=None,
    help="Name of the env var holding the storage backend's credential.",
)
@click.option(
    "--storage-database",
    default=None,
    help="Database/keyspace name for a server-side --backend (e.g. Neo4j database).",
)
@click.pass_context
def onboard(
    ctx: click.Context,
    vault_ref: str | None,
    provider_key: str | None,
    litellm_provider: str | None,
    api_base: str | None,
    api_key_env: str | None,
    api_key: str | None,
    model: str | None,
    skip_model_discovery: bool,
    non_interactive: bool,
    reconfigure: bool,
    yes: bool,
    allow_remote_llm: bool,
    dry_run: bool,
    print_summary_json: bool,
    disable_llm: bool,
    backend: str,
    accept_experimental: bool,
    storage_uri: str | None,
    storage_endpoint: str | None,
    allow_remote_db: bool,
    storage_credential_env: str | None,
    storage_database: str | None,
) -> None:
    """Guide first-run vault and LLM provider setup."""

    interactive = (not non_interactive) and sys.stdin.isatty() and sys.stdout.isatty()
    ensure_global_layout()

    if litellm_provider:
        raise click.BadParameter(
            "--litellm-provider is no longer used by onboarding; choose --provider custom "
            "for generic OpenAI-compatible endpoints or --provider litellm_proxy for a proxy.",
            param_hint="litellm-provider",
        )
    if (
        skip_model_discovery
        and model is None
        and provider_key not in {"skip", None}
        and not disable_llm
    ):
        raise click.BadParameter(
            "--skip-model-discovery requires --model unless provider is skip",
            param_hint="model",
        )

    backend_explicit = ctx.get_parameter_source("backend") == ParameterSource.COMMANDLINE
    if interactive and not backend_explicit:
        backend = _prompt_onboarding_backend(backend)
        if backend == "neo4j":
            storage_uri, storage_credential_env, storage_database = (
                _prompt_onboarding_neo4j_storage(
                    storage_uri=storage_uri,
                    storage_credential_env=storage_credential_env,
                    storage_database=storage_database,
                )
            )

    storage_target = storage_uri if storage_uri is not None else storage_endpoint
    if storage_target is not None:
        _confirm_remote_storage_endpoint(
            storage_target,
            allow_remote_db=allow_remote_db,
            yes=yes,
            interactive=interactive,
        )

    target = _onboarding_target(vault_ref, interactive=interactive)
    if not dry_run:
        # The writer lease is the first vault-touching step: refused with exit 5
        # while the daemon holds it, before the backend-pin check, the default-vault
        # switch, scaffolding or any okto-neuron.yaml patch (PATCH /api/v1/config is
        # the running-daemon equivalent). Released when the command's context closes.
        ctx.with_resource(vault_writer(target.resolve(strict=False), "onboard"))

    vault_path, created = _onboarding_vault(
        target,
        interactive=interactive,
        dry_run=dry_run,
        backend=backend,
        accept_experimental=accept_experimental,
        storage_uri=storage_target,
        storage_credential_env=storage_credential_env,
        storage_database=storage_database,
        storage_allow_remote=allow_remote_db,
    )
    raw_state = _llm_config_state(vault_path)
    if raw_state in {"configured", "disabled"} and not reconfigure and not disable_llm:
        if _onboarding_config_flags_supplied(
            provider_key=provider_key,
            api_base=api_base,
            api_key_env=api_key_env,
            api_key=api_key,
            model=model,
        ):
            raise click.ClickException(
                "this vault already has explicit LLM config; pass --reconfigure to change it"
            )
        if interactive:
            action = _prompt_existing_llm_action(raw_state)
            if action == "keep":
                _print_onboarding_summary(
                    vault_path=vault_path,
                    created=created,
                    state=raw_state,
                    dry_run=dry_run,
                    summary_json=print_summary_json,
                    changed=[],
                )
                return
            if action == "inspect":
                _inspect_existing_llm_config(vault_path)
                _print_onboarding_summary(
                    vault_path=vault_path,
                    created=created,
                    state=raw_state,
                    dry_run=dry_run,
                    summary_json=print_summary_json,
                    changed=[],
                )
                return
            if action == "disable":
                disable_llm = True
            else:
                reconfigure = True
        else:
            _print_onboarding_summary(
                vault_path=vault_path,
                created=created,
                state=raw_state,
                dry_run=dry_run,
                summary_json=print_summary_json,
                changed=[],
            )
            return

    if disable_llm:
        patch = disabled_llm_patch()
        if dry_run:
            changed: list[str] = []
        else:
            from okto_neuron.config import VaultConfig

            _, changed = VaultConfig.apply_patch(vault_path, patch)
        _print_onboarding_summary(
            vault_path=vault_path,
            created=created,
            state="disabled",
            dry_run=dry_run,
            summary_json=print_summary_json,
            changed=changed,
            patch=patch if dry_run else None,
        )
        return

    try:
        preset = _onboarding_preset(provider_key, interactive=interactive)
    except ValueError as exc:
        # A raw LiteLLM driver name (e.g. openai_like) is not an onboarding
        # preset — say so, and point at the preset that maps to it, instead of
        # leaving the user to guess from the key list alone.
        message = str(exc)
        if provider_key:
            from okto_neuron.config._vault import _LLM_PROVIDERS
            from okto_neuron.providers import (
                OPENAI_V1_DISCOVERY_ONLY_DRIVERS,
                OPENAI_V1_DRIVERS,
            )

            if provider_key in _LLM_PROVIDERS:
                if provider_key in OPENAI_V1_DRIVERS | OPENAI_V1_DISCOVERY_ONLY_DRIVERS:
                    message += (
                        f"\n  {provider_key!r} is a LiteLLM driver, not an onboarding preset — "
                        "for a generic OpenAI-compatible endpoint use --provider custom"
                        " (or --provider litellm_proxy for a LiteLLM gateway)."
                    )
                else:
                    message += (
                        f"\n  {provider_key!r} is a LiteLLM driver, not an onboarding preset — "
                        "configure it in the Config UI or as a named provider"
                        " (okto-neuron providers), or re-run with a preset above."
                    )
        raise click.BadParameter(message, param_hint="provider") from exc

    if preset.key == "auto":
        preset, model = _resolve_auto_provider(
            interactive=interactive,
            non_interactive=non_interactive,
            provided_model=model,
        )
    if preset.key == "skip":
        if dry_run:
            changed = []
            patch = {"llm": None}
        else:
            changed = _remove_explicit_llm_config(vault_path)
            patch = None
        _print_onboarding_summary(
            vault_path=vault_path,
            created=created,
            state="not_configured",
            dry_run=dry_run,
            summary_json=print_summary_json,
            changed=changed,
            patch=patch,
        )
        click.echo(
            "explore works without an LLM; run 'okto-neuron onboard --reconfigure' to enable ask/remember."
        )
        return

    # Presets like pi_cli have no api_base concept — skip URL prompting and
    # remote-egress gating for them.
    has_api_base = bool(preset.api_base)
    if has_api_base:
        api_base = api_base or _prompt_api_base(preset.api_base, interactive=interactive)
    else:
        api_base = None

    api_key_env = api_key_env or preset.api_key_env

    # Presets like pi_cli have no api_key_env — skip normalization for them.
    if api_key_env:
        try:
            api_key_env = _normalize_onboarding_api_key_env(api_key_env)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="api-key-env") from exc

    if has_api_base:
        remote_confirmed = _confirm_remote_endpoint(
            preset,
            api_base=api_base,  # type: ignore[arg-type]
            allow_remote_llm=allow_remote_llm,
            yes=yes,
            interactive=interactive,
        )
    else:
        remote_confirmed = False

    api_key = _prompt_api_key(
        preset,
        api_key_env=api_key_env,
        provided=api_key,
        interactive=interactive,
    )

    api_key_in_process = api_key or _secret_env(api_key_env)
    api_key_env_for_config = (
        api_key_env if api_key_env and (api_key_in_process or preset.api_key_required) else None
    )

    if model is None and not skip_model_discovery:
        result = discover_models(
            preset,
            api_base=api_base,
            api_key=api_key_in_process,
            allow_remote=allow_remote_llm or remote_confirmed,
            remote_confirmed=remote_confirmed,
        )
        if result.models:
            model = _select_onboarding_model(
                result.models,
                default_model=preset.default_model,
                interactive=interactive,
            )
        elif result.error:
            click.echo(f"model discovery failed ({result.error}); enter a model manually")

    if model is None:
        model = (
            click.prompt("Model", default=preset.default_model)
            if interactive
            else preset.default_model
        )

    # Pre-save verify: one real minimal completion through the canonical
    # endpoint BEFORE anything is written. Discovery (a model listing) proved
    # the base URL was close; only a completion proves the base the runtime
    # will use actually works. On failure nothing is saved — no config patch,
    # no env secret — and the exact URL attempted is shown with a one-line
    # hint. Skipped for dry runs (nothing would be saved anyway) and for
    # presets with no HTTP endpoint (pi_cli/codex_cli).
    if has_api_base and model and not dry_run:
        verified = verify_onboarding_completion(
            preset,
            model=model,
            api_base=api_base,  # type: ignore[arg-type]
            api_key=api_key_in_process,
            api_key_env=api_key_env,
        )
        if not verified.ok:
            click.echo("")
            click.echo("verify failed — nothing was saved:")
            click.echo(f"  {verified.error}")
            raise click.ClickException("onboarding aborted; re-check the base URL and model")

    # Persisted only after the pre-save verify above succeeded, so a failed
    # verify leaves nothing behind.
    env_path: Path | None = None
    if api_key:
        env_path = write_user_env_secret(api_key_env, api_key)

    try:
        patch = llm_config_patch(
            preset,
            model=model,
            api_base=api_base,
            api_key_env=api_key_env_for_config,
            allow_remote=allow_remote_llm or remote_confirmed,
        )
        if dry_run:
            changed = []
            cfg = None
        else:
            from okto_neuron.config import VaultConfig

            cfg, changed = VaultConfig.apply_patch(vault_path, patch)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    _print_onboarding_summary(
        vault_path=vault_path,
        created=created,
        state="configured",
        dry_run=dry_run,
        summary_json=print_summary_json,
        changed=changed,
        provider=preset.provider if cfg is None else cfg.llm.defaults.provider,
        model=model if cfg is None else cfg.llm.defaults.model,
        api_base=api_base if cfg is None else cfg.llm.defaults.api_base,
        api_key_env=api_key_env_for_config if cfg is None else cfg.llm.defaults.api_key_env,
        env_path=env_path,
        patch=patch if dry_run else None,
    )


def _onboarding_preset(provider_key: str | None, *, interactive: bool):
    if provider_key:
        return get_provider_preset(provider_key)
    if not interactive:
        raise click.UsageError(
            "--provider is required in --non-interactive mode unless existing config is kept"
        )

    click.echo("Choose a provider:")
    click.echo("  0. Skip LLM setup")
    menu = [preset for preset in MENU_PRESETS if preset.key != "skip"]
    for idx, preset in enumerate(menu, start=1):
        suffix = f" - {preset.note}" if preset.note else ""
        click.echo(f"  {idx}. {preset.label} ({preset.key}){suffix}")
    raw = click.prompt("Provider", default="1")
    if raw in {"0", "skip"}:
        return get_provider_preset("skip")
    if raw in _ONBOARDING_PROVIDER_KEYS.split(", "):
        return get_provider_preset(raw)
    try:
        choice = int(raw)
    except ValueError as exc:
        raise click.BadParameter("provider choice must be a number or provider id") from exc
    if choice < 1 or choice > len(menu):
        raise click.BadParameter("provider choice is out of range")
    return menu[choice - 1]


# One-line descriptions shown by ``_prompt_onboarding_backend`` (D-onboard-C).
# ``neptune`` is deliberately absent: it parses and validates but is not yet
# registry-reachable (see ``_validate_backend_name``'s docstring), so it is
# not offered as an interactive choice.
_ONBOARDING_BACKEND_DESCRIPTIONS: dict[str, str] = {
    "grafx": "default, embedded, by Okto Labs",
    "ladybug": "embedded single file, the legacy default",
    "neo4j": "external server, needs a bolt URI and a credential env var",
}


def _prompt_onboarding_backend(default: str) -> str:
    """Ask which graph backend a new vault should use.

    Only called from ``onboard()`` when onboarding is interactive and
    ``--backend`` was not passed explicitly on the command line (checked via
    ``ctx.get_parameter_source``, not by comparing to the default value, so
    an explicit ``--backend grafx`` still skips this prompt). Non-interactive
    onboarding never reaches this function, so its behavior stays
    byte-identical.
    """
    click.echo("")
    click.echo("Which graph backend should this vault use?")
    for name in ("grafx", "ladybug", "neo4j"):
        click.echo(f"  {name}: {_ONBOARDING_BACKEND_DESCRIPTIONS[name]}")
    return click.prompt(
        "Graph backend",
        type=click.Choice(["grafx", "ladybug", "neo4j"]),
        default=default,
    )


def _prompt_onboarding_neo4j_storage(
    *,
    storage_uri: str | None,
    storage_credential_env: str | None,
    storage_database: str | None,
) -> tuple[str, str | None, str]:
    """Ask for the Neo4j connection triple after the backend prompt picks ``neo4j``.

    Returns the ``(storage_uri, storage_credential_env, storage_database)``
    onboarding already threads through to ``_confirm_remote_storage_endpoint``
    and ``_onboarding_vault`` unchanged -- this only fills in defaults for an
    interactive session, it does not touch the consent flow itself.
    """
    uri = click.prompt("Neo4j storage URI", default=storage_uri or "bolt://127.0.0.1:7687")
    credential_env = click.prompt(
        "Env var holding the Neo4j credential",
        default=storage_credential_env or "OKTO_NEURON_NEO4J_PASSWORD",
    )
    database = click.prompt("Neo4j database", default=storage_database or "neo4j")
    return uri, credential_env, database


def _onboarding_target(vault_ref: str | None, *, interactive: bool) -> Path:
    """Resolve which vault path onboarding targets, with no side effects on it."""
    if vault_ref:
        return resolve_vault_reference(vault_ref)
    current = resolve_vault_reference(None)
    if is_vault(current):
        return current
    name = click.prompt("Vault name", default="mynotes") if interactive else "mynotes"
    return vault_path_for_name(name)


def _onboarding_vault(
    target: Path,
    *,
    interactive: bool,
    dry_run: bool = False,
    # ``onboard()`` (the only caller) always passes its own click-resolved
    # ``--backend`` value explicitly, so this default is inert on the CLI
    # path; unlike Vault.init/scaffold it has no direct library/test
    # caller, so it tracks the product default rather than staying pinned
    # to ladybug.
    backend: str = DEFAULT_NEW_VAULT_BACKEND,
    # Deprecated (kept for compatibility): no backend is D-12-gated any
    # more, so this no longer affects behavior either way.
    accept_experimental: bool = False,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
) -> tuple[Path, bool]:
    # Validates the backend name unconditionally, and (only when `target`
    # already has a okto-neuron.yaml) enforces that `backend` matches its
    # existing pin — covers both branches below, including dry-run.
    _resolve_and_pin_backend(target, backend)
    # accept_experimental (deprecated) is intentionally unused below: no
    # backend is D-12-gated any more, so the flag is accepted-and-ignored
    # for CLI compatibility with old scripts.

    if is_vault(target):
        set_default_vault(target)
        return target.resolve(strict=False), False

    if dry_run:
        return target.resolve(strict=False), True
    if interactive and not click.confirm(f"Create vault at {target}?", default=True):
        raise click.exceptions.Abort()

    Vault.scaffold(
        target,
        packs=["core", "research", "personal"],
        embedder="fastembed",
        allow_external_sources=True,
        backend=backend,
        storage_uri=storage_uri,
        storage_credential_env=storage_credential_env,
        storage_database=storage_database,
        storage_allow_remote=storage_allow_remote,
    )
    set_default_vault(target)
    return target.resolve(strict=False), True


def _llm_config_state(vault_path: Path) -> str:
    from okto_neuron.config import VaultConfig

    raw = VaultConfig.load_raw(vault_path)
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        return "not_configured"
    if llm.get("enabled") is False:
        return "disabled"
    return "configured"


def _remove_explicit_llm_config(vault_path: Path) -> list[str]:
    from okto_neuron.config import VaultConfig

    config_path = vault_config_path(vault_path)
    raw = VaultConfig.load_raw(vault_path)
    if "llm" not in raw:
        return []
    raw.pop("llm", None)
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return ["llm"]


def _onboarding_config_flags_supplied(
    *,
    provider_key: str | None,
    api_base: str | None,
    api_key_env: str | None,
    api_key: str | None,
    model: str | None,
) -> bool:
    return any(value is not None for value in (provider_key, api_base, api_key_env, api_key, model))


def _prompt_existing_llm_action(state: str) -> str:
    click.echo(f"This vault already has explicit LLM config ({state}).")
    click.echo("  1. Keep existing config")
    click.echo("  2. Test current config")
    click.echo("  3. Reconfigure provider/model")
    click.echo("  4. Disable LLM setup")
    choice = click.prompt("Action", default=1, type=int)
    if choice == 1:
        return "keep"
    if choice == 2:
        return "inspect"
    if choice == 3:
        return "reconfigure"
    if choice == 4:
        return "disable"
    raise click.BadParameter("action choice is out of range")


def _inspect_existing_llm_config(vault_path: Path) -> None:
    """Print a no-write validation summary for an existing explicit LLM block."""

    from okto_neuron.config import VaultConfig
    from okto_neuron.config._vault import classify_api_base

    click.echo("")
    click.echo("Testing existing LLM config")
    try:
        cfg = VaultConfig.load(vault_path)
    except Exception as exc:  # noqa: BLE001 - validation summary should be actionable.
        click.echo(f"  validation: failed ({exc})")
        click.echo("  config unchanged.")
        return

    llm = cfg.llm
    click.echo("  validation: config schema ok")
    if not llm.enabled:
        click.echo("  state: disabled")
        click.echo("  model discovery: skipped (LLM disabled)")
        click.echo("  config unchanged.")
        return

    defaults = llm.defaults
    click.echo(f"  provider: {defaults.provider}")
    click.echo(f"  base URL: {defaults.api_base}")
    click.echo(f"  model: {defaults.model}")
    if defaults.api_key_env:
        status = "set" if _secret_env(defaults.api_key_env) else "not set"
        click.echo(f"  key env: {defaults.api_key_env} ({status}; value hidden)")
    else:
        click.echo("  key env: none")

    try:
        endpoint_class = classify_api_base(defaults.api_base, resolve=False)
    except ValueError as exc:
        click.echo(f"  endpoint: invalid ({exc})")
        click.echo("  config unchanged.")
        return
    click.echo(f"  endpoint: {endpoint_class}")

    try:
        preset = get_provider_preset(defaults.provider)
    except ValueError:
        click.echo(
            f"  model discovery: skipped (provider {defaults.provider!r} has no onboarding preset)"
        )
        click.echo("  config unchanged.")
        return

    api_key = _secret_env(defaults.api_key_env) if defaults.api_key_env else None
    result = discover_models(
        preset,
        api_base=defaults.api_base,
        api_key=api_key,
        timeout=3.0,
        allow_remote=llm.allow_remote,
        remote_confirmed=llm.allow_remote,
    )
    if result.models:
        listed = "yes" if defaults.model in result.models else "no"
        click.echo(
            f"  model discovery: ok ({len(result.models)} models; configured model listed: {listed})"
        )
    elif result.error:
        click.echo(f"  model discovery: failed ({result.error})")
    else:
        click.echo("  model discovery: no models returned")
    click.echo("  config unchanged.")


def _resolve_auto_provider(
    *,
    interactive: bool,
    non_interactive: bool,
    provided_model: str | None,
):
    candidates = auto_detect_loopback()
    if len(candidates) == 1:
        candidate = candidates[0]
        model = provided_model or (candidate.models[0] if candidate.models else None)
        click.echo(f"Found {candidate.preset.label} at {candidate.preset.api_base}.")
        return candidate.preset, model
    if non_interactive:
        if not candidates:
            raise click.ClickException("auto-detect found no local LLM runtime")
        names = ", ".join(candidate.preset.key for candidate in candidates)
        raise click.ClickException(f"auto-detect found multiple local runtimes: {names}")
    if candidates:
        click.echo("Found multiple local runtimes:")
        for idx, candidate in enumerate(candidates, start=1):
            detail = (
                f"{len(candidate.models)} models"
                if candidate.models
                else candidate.error or "present"
            )
            click.echo(f"  {idx}. {candidate.preset.label} - {detail}")
        click.echo(f"  {len(candidates) + 1}. Choose another provider")
        choice = click.prompt("Runtime", default=1, type=int)
        if 1 <= choice <= len(candidates):
            candidate = candidates[choice - 1]
            model = provided_model or (candidate.models[0] if candidate.models else None)
            return candidate.preset, model
    else:
        click.echo("No local LM Studio, Ollama, or LiteLLM Proxy runtime was detected.")
    return _onboarding_preset(None, interactive=interactive), provided_model


def _prompt_api_base(default: str, *, interactive: bool) -> str:
    if not interactive:
        return default
    return click.prompt("Base URL", default=default)


def _normalize_onboarding_api_key_env(value: str | None) -> str:
    from okto_neuron.config._vault import _check_api_key_env

    checked = _check_api_key_env(value)
    return checked or ""


def _validate_backend_name(requested: str) -> str:
    """Resolve ``requested`` against the graph-backend registry, or fail loud.

    An unregistered/typo'd backend name fails now with a clear
    ``click.BadParameter`` instead of surfacing later as an opaque import
    error at open time (M3 spec section 2.2).
    """
    from okto_neuron.store.registry import NoSuchBackendError, resolve_graph_backend

    try:
        resolve_graph_backend(requested)
    except NoSuchBackendError as exc:
        raise click.BadParameter(str(exc), param_hint="backend") from exc
    return requested


def _resolve_and_pin_backend(path: Path, requested: str) -> str:
    """Validate ``requested`` and, when ``path`` already has a vault, enforce its pin.

    Shared by every vault-creation entry point (``onboard``, ``okto-neuron
    init``, ``vault create``, ``kg init``). Registry validation
    (:func:`_validate_backend_name`) is not something ``Vault.scaffold``/
    ``Vault.init`` do themselves, so every caller needs this regardless of
    whether it also goes through them. The pin-conflict check reuses
    ``vault._check_backend_pin`` — the exact function ``Vault.scaffold``/
    ``Vault.init`` already call internally (M3 spec section 2.5) — so a
    caller that bypasses them entirely (``kg init``, D-46) still raises the
    identical ``VaultBackendMismatch`` a scaffold-based caller would, instead
    of silently reopening under the old backend. Calling this again right
    before a ``Vault.scaffold``/``Vault.init`` that will re-check the same
    pin is intentional, not redundant busywork: it fails before any of a
    caller's earlier side effects (e.g. ``mark_managed_vault``).
    """
    _validate_backend_name(requested)

    from okto_neuron.vault import _check_backend_pin

    _check_backend_pin(Path(path), requested)
    return requested


# ``_confirm_experimental_backend`` (D-12's experimental-backend consent
# gate) was removed here: the owner decision retiring D-12 made Okto Grafx
# the default, non-experimental graph backend, so no official backend
# (ladybug/grafx/neo4j) is ever gated at vault creation any more. Every
# former call site (`vault_create`, `init`, `_onboarding_vault` above,
# `kg_init` in `cli/kg.py`) still accepts `--accept-experimental` /
# `accept_experimental` -- deprecated, accepted-and-ignored, kept only so an
# old script that passes the flag does not break.


def _confirm_remote_storage_endpoint(
    uri: str,
    *,
    allow_remote_db: bool,
    yes: bool,
    interactive: bool,
) -> bool:
    """Sibling of ``_confirm_remote_endpoint`` for a storage-backend endpoint.

    Copied rather than called (M3 spec section 2.7): same loopback/consent
    shape, but classifies via ``_classify_storage_endpoint`` (the wider
    bolt/neo4j-aware scheme allowlist) and gates on ``--allow-remote-db``/
    ``--yes`` instead of the LLM flags. Unreachable in M3 today — no
    non-Ladybug backend is registry-resolvable yet (``_validate_backend_name``
    fails first) — but present so M5 needs no new consent code.
    """
    from okto_neuron.config._vault import _classify_storage_endpoint

    try:
        endpoint_class = _classify_storage_endpoint(uri, resolve=False)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if endpoint_class == "loopback":
        return False
    if not allow_remote_db and not interactive:
        raise click.ClickException(
            f"{uri} is not loopback; pass --allow-remote-db --yes to confirm remote egress"
        )
    if not allow_remote_db and interactive:
        click.echo("")
        click.echo("This storage endpoint is not loopback:")
        click.echo(f"  endpoint: {uri}")
        click.echo("Vault-derived context may be sent to this endpoint.")
        if not click.confirm("Allow this endpoint?", default=False):
            raise click.exceptions.Abort()
        return True
    if not yes and not interactive:
        raise click.ClickException(
            f"{uri} is {endpoint_class}; pass --yes with --allow-remote-db to confirm"
        )
    if allow_remote_db and not yes and interactive:
        click.echo("")
        click.echo(f"storage endpoint is not loopback: {uri}")
        click.echo("Vault-derived context may leave this machine.")
        if not click.confirm("Allow remote storage egress?", default=False):
            raise click.exceptions.Abort()
        return True
    return True


def _confirm_remote_endpoint(
    preset,
    *,
    api_base: str,
    allow_remote_llm: bool,
    yes: bool,
    interactive: bool,
) -> bool:
    from okto_neuron.config._vault import api_base_is_loopback, classify_api_base

    try:
        if api_base_is_loopback(api_base):
            return False
        endpoint_class = classify_api_base(api_base, resolve=False)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not allow_remote_llm and not interactive:
        raise click.ClickException(
            f"{api_base} is not loopback; pass --allow-remote-llm --yes to confirm remote egress"
        )
    if not allow_remote_llm and interactive:
        click.echo("")
        click.echo("This endpoint is not loopback:")
        click.echo(f"  provider: {preset.label}")
        click.echo(f"  endpoint: {api_base}")
        click.echo("Vault-derived context may be sent to this endpoint.")
        if not click.confirm("Allow this endpoint?", default=False):
            raise click.exceptions.Abort()
        return True
    if not yes and not interactive:
        raise click.ClickException(
            f"{api_base} is {endpoint_class}; pass --yes with --allow-remote-llm to confirm"
        )
    if allow_remote_llm and not yes and interactive:
        click.echo("")
        click.echo(f"{preset.label} is not loopback: {api_base}")
        click.echo("Vault-derived context may leave this machine.")
        if not click.confirm("Allow remote LLM egress?", default=False):
            raise click.exceptions.Abort()
        return True
    if preset.hosted and interactive:
        click.echo(f"{preset.label} is hosted; provider terms and privacy policy apply.")
    return True


def _prompt_api_key(
    preset,
    *,
    api_key_env: str,
    provided: str | None,
    interactive: bool,
) -> str | None:
    if provided is not None:
        return provided
    if not interactive:
        return _secret_env(api_key_env) if api_key_env else None

    suffix = "press Enter if this local endpoint has no key"
    if preset.api_key_required:
        suffix = f"saved as {api_key_env}; leave blank only if already exported"
    value = click.prompt(f"API key ({suffix})", default="", show_default=False, hide_input=True)
    if value:
        return value
    if preset.api_key_required and not _secret_env(api_key_env):
        click.echo(f"no key saved; set {api_key_env} before using this hosted provider")
    return None


def _select_onboarding_model(
    models: list[str],
    *,
    default_model: str,
    interactive: bool,
) -> str:
    if not models:
        return default_model
    if not interactive:
        # Non-interactive runs must never silently default to models[0] — on a
        # multi-model server that can be a non-chat model (e.g. a document
        # parser first in sort order) becoming the default chat model. Only
        # the preset's own declared default is explicit enough to use without
        # an interactive choice; otherwise require --model.
        if default_model and default_model in models:
            return default_model
        shown = ", ".join(models[:8]) + (" …" if len(models) > 8 else "")
        raise click.UsageError(
            f"no --model given and no preset default among the discovered models "
            f"({shown}); re-run with --model <id>"
        )

    default_index = models.index(default_model) + 1 if default_model in models else 1
    click.echo("Available models:")
    for idx, model in enumerate(models, start=1):
        click.echo(f"  {idx}. {model}")
    click.echo("Type a number from the list or paste a model ID.")
    raw = click.prompt("Model", default=str(default_index)).strip()
    try:
        choice = int(raw)
    except ValueError:
        return raw
    if choice < 1 or choice > len(models):
        raise click.BadParameter("model choice is out of range")
    return models[choice - 1]


def _print_onboarding_summary(
    *,
    vault_path: Path,
    created: bool,
    state: str,
    dry_run: bool,
    summary_json: bool,
    changed: list[str],
    provider: str | None = None,
    model: str | None = None,
    api_base: str | None = None,
    api_key_env: str | None = None,
    env_path: Path | None = None,
    patch: dict[str, Any] | None = None,
) -> None:
    click.echo("")
    click.echo("Okto Neuron onboarding complete" if not dry_run else "Okto Neuron onboarding dry run")
    click.echo(f"  vault:    {vault_path}")
    click.echo(f"  created:  {'yes' if created else 'no'}")
    click.echo(f"  state:    {state}")
    if provider:
        click.echo(f"  provider: {provider}")
    if model:
        click.echo(f"  model:    {model}")
    if api_base:
        click.echo(f"  base URL: {api_base}")
    if api_key_env:
        source = (
            f" ({env_path})" if env_path is not None else " (existing environment or later export)"
        )
        click.echo(f"  key env:  {api_key_env}{source}")
    elif provider:
        click.echo("  key env:  none")
    click.echo(f"  changed:  {', '.join(changed) if changed else 'none'}")
    if patch is not None:
        click.echo("  patch:")
        click.echo(json.dumps(patch, indent=2, sort_keys=True))
    if summary_json:
        payload = {
            "status": "ok",
            "vault": str(vault_path),
            "created": created,
            "state": state,
            "dry_run": dry_run,
            "changed": changed,
            "provider": provider,
            "model": model,
            "api_base": api_base,
            "api_key_env": api_key_env,
        }
        click.echo(json.dumps(payload, sort_keys=True))


def _endpoint_option(func):
    return click.option(
        "--endpoint",
        "endpoint",
        default=None,
        help="Server endpoint URL. Precedence: --endpoint > OKTO_NEURON_ENDPOINT env > "
        "http://127.0.0.1:7777.",
    )(func)


def _timeout_option(func):
    return click.option(
        "--timeout",
        "timeout",
        default=DEFAULT_TIMEOUT_SECONDS,
        type=float,
        help="HTTP request timeout in seconds (default 30).",
    )(func)


def _server_endpoint(host: str, port: int) -> str:
    """Build a browser-usable HTTP endpoint from a bind host and port."""
    browser_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _daemon_stop_command(*, explicit_vault: bool, vault_path: Path | None) -> str:
    del explicit_vault, vault_path  # compatibility signature for existing callers
    return "okto-neuron stop"


def _daemon_ui_url(endpoint: str) -> str:
    """Return the direct browser URL for the local application."""
    return f"{endpoint.rstrip('/')}/"


def _daemon_status_command(endpoint: str, vault_path: Path | None = None) -> str:
    del vault_path  # status describes the application daemon, not one vault lock
    command = f"okto-neuron status --endpoint {endpoint}"
    return command


def _discover_stop_root() -> Path:
    """Find the only verified daemon owner when ``stop`` has no --vault."""
    from okto_neuron.server.lifecycle import active_server_pid

    candidates: list[Path] = [_server_lock_root(), legacy_runtime_root()]
    current = resolve_vault_reference(None)
    if is_vault(current):
        candidates.append(current)
    for entry in list_vaults(current=current if is_vault(current) else None):
        candidates.append(entry.path)

    live_roots: list[tuple[Path, int]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = Path(candidate).expanduser().resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        pid = active_server_pid(resolved)
        if pid is not None:
            live_roots.append((resolved, pid))
    if len(live_roots) == 1:
        return live_roots[0][0]
    if len(live_roots) > 1:
        commands = "\n".join(
            f'  okto-neuron stop --vault "{root}"  # pid {pid}' for root, pid in live_roots
        )
        raise click.UsageError(
            "multiple Okto Neuron daemons were found; choose one explicitly:\n" + commands
        )
    return _server_lock_root()


def _wait_for_server_health(
    endpoint: str,
    *,
    timeout: float,
    expected_pid: int | None = None,
    vault_path: Path | None = None,
) -> bool:
    """Wait for liveness and, when requested, exact daemon ownership."""
    import time

    import httpx

    del vault_path  # compatibility-only; readiness is application-scoped
    deadline = time.monotonic() + timeout
    health_url = endpoint.rstrip("/") + "/health"
    status_url = endpoint.rstrip("/") + "/api/v1/status"
    with httpx.Client(timeout=0.5) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(health_url)
                payload = response.json()
                if response.status_code == 200 and payload == {"status": "ok"}:
                    if expected_pid is None:
                        return True
                    status_response = client.get(status_url)
                    status_payload = status_response.json()
                    if (
                        status_response.status_code == 200
                        and status_payload.get("pid") == expected_pid
                    ):
                        return True
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            time.sleep(0.05)
    return False


def _wait_for_daemon_owner(
    endpoint: str,
    lock_root: Path,
    spawned_pid: int,
    *,
    timeout: float,
    vault_path: Path | None,
) -> int | None:
    """Return the exact runtime PID after a detached daemon becomes ready."""
    if os.name != "nt":
        return (
            spawned_pid
            if _wait_for_server_health(
                endpoint,
                timeout=timeout,
                expected_pid=spawned_pid,
                vault_path=vault_path,
            )
            else None
        )

    # A Windows venv python.exe can be a redirector whose Popen PID differs
    # from the interpreter that owns the lifecycle lock. First wait for the
    # endpoint, then bind it to the lock-owning runtime PID.
    if not _wait_for_server_health(
        endpoint,
        timeout=timeout,
        vault_path=vault_path,
    ):
        return None
    from okto_neuron.server.lifecycle import active_server_pid

    owner_pid = active_server_pid(lock_root)
    if owner_pid is None:
        return None
    if not _wait_for_server_health(
        endpoint,
        timeout=2.0,
        expected_pid=owner_pid,
        vault_path=vault_path,
    ):
        return None
    return owner_pid


def _open_ui_in_browser(endpoint: str) -> bool:
    """Best-effort browser launch for the plain loopback UI URL."""
    import webbrowser

    url = endpoint.rstrip("/") + "/"
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if opened:
        click.echo(f"Opened Okto Neuron at {url}")
    else:
        click.echo(f"Browser did not open automatically; open {url}", err=True)
    return opened


def _open_ui_when_ready(
    endpoint: str,
    *,
    expected_pid: int,
    vault_path: Path | None,
) -> None:
    if _wait_for_server_health(
        endpoint,
        timeout=_SERVER_STARTUP_TIMEOUT_SECONDS,
        expected_pid=expected_pid,
        vault_path=vault_path,
    ):
        _open_ui_in_browser(endpoint)


def _start_ui_browser_thread(
    endpoint: str,
    *,
    expected_pid: int,
    vault_path: Path | None,
) -> None:
    """Open after foreground Uvicorn is ready without blocking startup."""
    import threading

    threading.Thread(
        target=_open_ui_when_ready,
        kwargs={
            "endpoint": endpoint,
            "expected_pid": expected_pid,
            "vault_path": vault_path,
        },
        name="okto-neuron-browser-launch",
        daemon=True,
    ).start()


@app.command("ui")
@_endpoint_option
@click.option(
    "--vault",
    "vault_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Deprecated compatibility option; browser access is application-scoped.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the local UI in the default browser (default: open).",
)
def ui(endpoint: str | None, vault_path: Path | None, open_browser: bool) -> None:
    """Open the running daemon's local web UI."""
    del vault_path
    resolved = resolve_endpoint(endpoint)
    if not _wait_for_server_health(resolved, timeout=2.0):
        click.echo(UNREACHABLE_MESSAGE_TEMPLATE.format(url=resolved), err=True)
        raise click.exceptions.Exit(EXIT_UNREACHABLE)
    if not open_browser:
        click.echo(f"Okto Neuron UI is ready at {resolved.rstrip('/')}/; browser launch skipped")
        return
    _open_ui_in_browser(resolved)


def _run_thin_client(
    endpoint: str | None,
    timeout: float,
    path: str,
    payload: dict[str, Any],
    *,
    vault: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve endpoint, POST payload, translate transport errors to CLI exits."""
    resolved = resolve_endpoint(endpoint)
    try:
        if vault is not None:
            return _client_post(resolved, path, payload, timeout=timeout, vault=vault)
        return _client_post(resolved, path, payload, timeout=timeout)
    except ClientUnreachable as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(EXIT_UNREACHABLE) from exc
    except ServerError as exc:
        click.echo(f"server error {exc.status_code}: {exc.detail}", err=True)
        raise click.exceptions.Exit(1) from exc


def _run_client_request(
    endpoint: str,
    timeout: float,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    auth_token: str | None = None,
    vault: str | Path | None = None,
) -> dict[str, Any]:
    """Run one request through the shared CLI client."""
    try:
        if auth_token is None:
            return _client_request(
                endpoint,
                method,
                path,
                payload,
                timeout=timeout,
                vault=vault,
            )
        return _client_request(
            endpoint,
            method,
            path,
            payload,
            timeout=timeout,
            auth_token=auth_token,
            vault=vault,
        )
    except ClientUnreachable as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(EXIT_UNREACHABLE) from exc
    except ServerError as exc:
        click.echo(f"server error {exc.status_code}: {exc.detail}", err=True)
        raise click.exceptions.Exit(1) from exc


@app.command("status")
@_endpoint_option
@click.option(
    "--vault",
    "vault_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional vault status context; omit for the application aggregate.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@_timeout_option
def status(
    endpoint: str | None,
    vault_path: Path | None,
    as_json: bool,
    timeout: float,
) -> None:
    """Show the application daemon, version, and operational state."""
    resolved = resolve_endpoint(endpoint)
    health = _run_client_request(
        resolved,
        timeout,
        "GET",
        "/api/v1/status",
        vault=vault_path,
    )
    version_payload = _run_client_request(
        resolved,
        timeout,
        "GET",
        "/version",
    )
    scope = health.get("scope")
    if scope not in {"application", "vault"}:
        scope = "vault" if health.get("active_vault") else "application"
    payload = {
        "status": health.get("status", "unknown"),
        "scope": scope,
        "endpoint": resolved,
        "pid": health.get("pid"),
        "vault_path": health.get("vault_path"),
        "backend": health.get("backend"),
        "active_vault": health.get("active_vault", False),
        "vault_count": health.get("vault_count"),
        "vaults": health.get("vaults", []),
        "uptime_s": health.get("uptime_s"),
        "queue_error_count": health.get("queue_error_count", 0),
        "ingest": health.get("ingest", {}),
        "degraded_reasons": health.get("degraded_reasons", []),
        **_compat_version_payload(_compat_version_from_payload(version_payload) or "unknown"),
        "api_version": version_payload.get("api_version", "unknown"),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    state = str(payload["status"])
    click.echo(f"Okto Neuron {payload['okto_neuron_version']} — {state}")
    click.echo(f"  Endpoint: {resolved}")
    click.echo(f"  PID:      {payload['pid'] or 'unknown'}")
    click.echo(f"  Scope:    {payload['scope']}")
    if payload["scope"] == "application":
        vault_count = payload["vault_count"]
        click.echo(f"  Vaults:   {vault_count if isinstance(vault_count, int) else 'unknown'}")
        if payload["vault_path"]:
            click.echo(f"  Fallback: {payload['vault_path']}")
            if payload["backend"]:
                click.echo(f"  Backend:  {payload['backend']}")
    else:
        click.echo(f"  Vault:    {payload['vault_path'] or '(unknown)'}")
        if payload["backend"]:
            click.echo(f"  Backend:  {payload['backend']}")
    uptime = payload["uptime_s"]
    if isinstance(uptime, (int, float)):
        click.echo(f"  Uptime:   {uptime:.1f}s")
    ingest = payload["ingest"]
    if isinstance(ingest, dict):
        total = int(ingest.get("total") or 0)
        terminal = sum(int(ingest.get(key) or 0) for key in ("done", "error", "cancelled"))
        phase = (
            "stopping"
            if ingest.get("cancel_requested")
            else "active"
            if ingest.get("active")
            else "idle"
        )
        click.echo(
            f"  Ingest:   {phase} ({terminal}/{total} terminal, "
            f"{int(ingest.get('processing') or 0)} processing, "
            f"{int(ingest.get('queued') or 0)} queued)"
        )
    click.echo(f"  UI:       {_daemon_ui_url(resolved)}")
    click.echo("  Stop:     okto-neuron stop")
    for reason in payload["degraded_reasons"]:
        click.echo(f"  Warning:  {reason}")


@app.command("add")
@click.argument("file", type=click.Path(path_type=Path))
@click.option("--vault", type=click.Path(path_type=Path), help="Vault name or path to target.")
@_endpoint_option
@_timeout_option
def add(file: Path, vault: Path | None, endpoint: str | None, timeout: float) -> None:
    """Ingest one markdown file via the running server."""
    file_path = Path(file)
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        click.echo(f"cannot read {file_path}: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    payload = {"path": str(file_path), "content": content}
    body = _run_thin_client(endpoint, timeout, "/add", payload, vault=vault)
    document_id = body.get("document_id", "")
    chunks = body.get("chunks_ingested", "?")
    click.echo(f"added {document_id} ({chunks} chunks)")


@app.command("query")
@click.argument("text")
@click.option("--vault", type=click.Path(path_type=Path), help="Vault name or path to target.")
@click.option(
    "--k",
    default=10,
    type=int,
    help="Notes to retrieve (default 10). query/recall feeds an agent that can "
    "re-query, so it stays tight and cheap. For a one-shot grounded answer, use "
    "`ask` (seeds wider, default 20).",
)
@click.option("--format", "output_format", default="text", help="text | json.")
@_endpoint_option
@_timeout_option
def query(
    text: str,
    vault: Path | None,
    k: int,
    output_format: str,
    endpoint: str | None,
    timeout: float,
) -> None:
    """Query the running server and return provenance-bearing hits."""
    payload = {"query": text, "k": k}
    body = _run_thin_client(endpoint, timeout, "/query", payload, vault=vault)
    results = body.get("results", []) or []
    if output_format == "json":
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("(no hits)")
        return
    for hit in results:
        score = hit.get("score", 0.0)
        document_id = hit.get("document_id", "")
        path = hit.get("path", "")
        snippet = hit.get("snippet", "")
        click.echo(f"{score:5.2f} {document_id} {path} {snippet}".rstrip())


@app.command("ask")
@click.argument("text")
@click.option("--vault", type=click.Path(path_type=Path), help="Vault name or path to target.")
@click.option(
    "--k",
    default=20,
    type=int,
    help="Notes to retrieve (default 20). ask is one-shot, so it seeds wide — "
    "raise for broad/multi-part questions, lower for cheap narrow lookups. "
    "recall (query) defaults to 10 because it feeds an agent that can re-query.",
)
@click.option(
    "--hops",
    default=1,
    type=int,
    help="Graph neighbourhood depth (1-5) when subgraph retrieval is enabled. "
    "Raise when an answer needs more connected context.",
)
@click.option("--format", "output_format", default="text", help="text | json.")
@_endpoint_option
@_timeout_option
def ask(
    text: str,
    vault: Path | None,
    k: int,
    hops: int,
    output_format: str,
    endpoint: str | None,
    timeout: float,
) -> None:
    """Ask a question and get a grounded, citation-bearing answer.

    Unlike `query` (which returns raw hits for an agent to re-query), `ask`
    synthesises a single answer in one shot, so it seeds a wider candidate set.

    Exits 17 when the answer is degraded (no LLM configured, provider failure,
    truncated, abnormal stop or empty answer). The output is still printed.
    """
    payload: dict[str, Any] = {"question": text, "k": k}
    if hops != 1:
        payload["retrieval_policy"] = {"hops": hops}
    body = _run_thin_client(endpoint, timeout, "/api/v1/ask", payload, vault=vault)
    degraded = body.get("status") == "degraded"
    if output_format == "json":
        click.echo(json.dumps(body, indent=2))
        if degraded:
            raise click.exceptions.Exit(EXIT_ASK_DEGRADED)
        return
    click.echo(body.get("text", "") or "(no answer)")
    if degraded:
        retrieval = body.get("retrieval") or {}
        why = (
            retrieval.get("no_llm_reason")
            or retrieval.get("provider_error")
            or retrieval.get("finish_reason")
            or ""
        )
        click.echo(
            f"answer degraded: {retrieval.get('synthesis_status', 'unknown')}"
            + (f" ({why})" if why else ""),
            err=True,
        )
    citations = body.get("citations", []) or []
    if citations:
        click.echo("\nCitations:")
        for c in citations:
            path = c.get("path", "") if isinstance(c, dict) else str(c)
            click.echo(f"  - {path}")
    if degraded:
        raise click.exceptions.Exit(EXIT_ASK_DEGRADED)


@app.command("detect-drift")
@click.option(
    "--vault",
    type=click.Path(path_type=Path),
    help="Vault name or path to target (also the legacy corpus root fallback).",
)
@click.option(
    "--corpus-root",
    "corpus_root",
    type=click.Path(path_type=Path),
    help="Corpus root to diff against.",
)
@click.option("--mode", default="on-query", help="on-query | on-ingest | background.")
@click.option(
    "--algos",
    default=",".join(DETECTOR_NAMES),
    help="Comma-separated detector names.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False, help="Detect but do not remediate."
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope.")
@_endpoint_option
@_timeout_option
def detect_drift(
    vault: Path | None,
    corpus_root: Path | None,
    mode: str,
    algos: str,
    dry_run: bool,
    json_output: bool,
    endpoint: str | None,
    timeout: float,
) -> None:
    """Detect drift via the running server."""
    del algos  # algos kept for CLI back-compat; server runs its configured detectors
    if mode not in {"on-query", "on-ingest", "background"}:
        raise click.BadParameter("mode must be on-query, on-ingest, or background")
    root = corpus_root if corpus_root is not None else vault
    if root is None:
        raise click.BadParameter("--corpus-root (or back-compat --vault) is required")
    payload = {"corpus_root": str(Path(root).expanduser().resolve()), "dry_run": dry_run}
    body = _run_thin_client(endpoint, timeout, "/detect-drift", payload, vault=vault)
    if json_output:
        click.echo(json.dumps(body, indent=2, sort_keys=True))
        return
    added = body.get("added", 0)
    removed = body.get("removed", 0)
    changed = body.get("changed", 0)
    click.echo(f"drift: +{added} -{removed} ~{changed} ({mode})")
    for action in body.get("actions", []) or []:
        click.echo(f"- {action.get('op')} {action.get('path')}")


@app.command("pilot")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--report-dir", type=click.Path(path_type=Path), default=Path("docs"))
def kg_pilot(vault_path: Path, report_dir: Path) -> None:
    """Run the generic corpus pilot gate over one configured markdown vault."""
    rejection = _laptop_gate_rejection()
    if rejection:
        click.echo(rejection, err=True)
        raise click.exceptions.Exit(2)
    root = vault_path.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        click.echo(f"vault not found: {root}", err=True)
        raise click.exceptions.Exit(2)
    if not (vault_config_path(root)).exists():
        click.echo(f"vault is missing okto-neuron.yaml: {root}", err=True)
        raise click.exceptions.Exit(2)

    with vault_writer(root, "pilot"):
        opened = Vault.open(root)
        ingest_count = _ingest_markdown(opened)
        drift_envelope = _drift_envelope(
            opened,
            mode="on-query",
            detector_names=list(DETECTOR_NAMES),
        )
    passed = int(drift_envelope["total"]) >= 1
    timestamp = datetime.now(timezone.utc)
    payload = {
        "timestamp": timestamp.isoformat(),
        "vault_root": str(root),
        "ingest_count": ingest_count,
        "drift_envelope": drift_envelope,
        "pass": passed,
        "fail": not passed,
    }
    report_root = report_dir.expanduser()
    if not report_root.is_absolute():
        report_root = Path.cwd() / report_root
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"pilot-log.{timestamp.date().isoformat()}.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"wrote {report_path}")
    raise click.exceptions.Exit(0 if passed else 1)


@app.command("serve")
@click.option(
    "--vault",
    type=click.Path(path_type=Path),
    help="Optional compatibility initial vault; selection normally happens in the app.",
)
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1, loopback-only).")
@click.option(
    "--allow-remote",
    is_flag=True,
    default=False,
    help="Reserved compatibility flag. Direct remote serving is currently disabled; "
    "use an SSH tunnel to the loopback listener.",
)
@click.option("--port", "rest_port", default=7777, type=int, help="REST bind port (default 7777).")
@click.option("--mcp-port", default=8201, type=int, help="FastMCP bind port (default 8201).")
@click.option("--daemon", "daemon_mode", is_flag=True, help="Detach into the background.")
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the local UI after the server is ready (default: open).",
)
@click.option(
    "--log-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Write server logs to this file (size-rotated). With --daemon the default is "
    "~/.okto-neuron/logs/okto-neuron-serve.log; pass /dev/null to silence. "
    "Without --daemon logs go to stdout unless this is set.",
)
@click.option(
    "--foreground/--no-foreground",
    default=True,
    help="Run in the foreground (default). Ignored when --daemon is set.",
)
@click.pass_context
def serve(
    ctx: click.Context,
    vault: Path | None,
    host: str,
    allow_remote: bool,
    rest_port: int,
    mcp_port: int,
    daemon_mode: bool,
    open_browser: bool,
    log_file: Path | None,
    foreground: bool,
) -> None:
    """Start the Okto Neuron app server (REST/UI :7777 + FastMCP :8201)."""
    import logging as _logging

    # Remote serving is intentionally withdrawn until TLS, trusted-proxy identity,
    # and remote write/read capabilities have one tested contract. Keep the flag
    # only to produce a useful migration message for existing scripts.
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    if host not in _LOOPBACK_HOSTS or allow_remote:
        refusal = (
            f"refusing to bind to non-loopback host {host!r}."
            if host not in _LOOPBACK_HOSTS
            else "refusing --allow-remote because remote mode is disabled."
        )
        click.echo(
            f"{refusal}\n"
            "Direct remote serving, including --allow-remote, is currently disabled. "
            "Bind to 127.0.0.1 and use an SSH tunnel instead.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    try:
        from okto_neuron.server.runtime import run as run_server
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".", 1)[0] not in {"ladybug", "starlette", "uvicorn"}:
            raise
        raise OptionalDependencyError(
            "server dependencies are not installed; install okto-neuron[serve] "
            "to run `okto-neuron serve`",
            cause=exc,
        ) from exc
    from okto_neuron.server.lifecycle import (
        LifecycleError,
        PidFile,
        StaleLockError,
        configure_logging,
        daemonize,
        default_daemon_log_path,
        pid_file_path,
        send_stop,
    )

    explicit_vault = ctx.get_parameter_source("vault") == ParameterSource.COMMANDLINE
    try:
        resolved = _resolve_serve_vault(vault, explicit=explicit_vault)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(2) from exc
    if explicit_vault and resolved is None:
        click.echo(f"vault not found: {_resolve_vault(vault)}", err=True)
        raise click.exceptions.Exit(2)
    lock_root = _server_lock_root(explicit_vault=explicit_vault, vault_path=resolved)
    _refuse_if_legacy_daemon_running()

    if daemon_mode and log_file is None:
        log_file = default_daemon_log_path()
    if log_file is not None:
        log_file = log_file.expanduser()
        if not log_file.exists() or log_file.is_file():
            log_file.parent.mkdir(parents=True, exist_ok=True)

    if daemon_mode:
        # Pre-flight the lock in the parent so we can surface
        # StaleLockError before forking.
        try:
            with PidFile(lock_root, pid=os.getpid()) as preflight:
                preflight.release()  # release immediately; we only validated.
        except StaleLockError as exc:
            click.echo(str(exc), err=True)
            endpoint = _server_endpoint(host, rest_port)
            if open_browser and _wait_for_server_health(endpoint, timeout=2.0):
                _open_ui_in_browser(endpoint)
            click.echo(f"UI: {_daemon_ui_url(endpoint)}", err=True)
            click.echo(
                f"Stop: {_daemon_stop_command(explicit_vault=explicit_vault, vault_path=resolved)}",
                err=True,
            )
            raise click.exceptions.Exit(1) from exc
        _warn_if_telemetry_unavailable()  # the re-exec'd child logs it to the file
        # Raw stdout/stderr go to the same file so uncaught tracebacks and
        # third-party prints survive; structured logs use the rotating handler.
        spawned_pid = daemonize(stdout=log_file, stderr=log_file)
        endpoint = _server_endpoint(host, rest_port)
        new_pid = _wait_for_daemon_owner(
            endpoint,
            lock_root,
            spawned_pid,
            timeout=_SERVER_STARTUP_TIMEOUT_SECONDS,
            vault_path=resolved,
        )
        if new_pid is None:
            import signal

            try:
                send_stop(
                    lock_root,
                    sig=signal.SIGTERM,
                    expected_pid=spawned_pid,
                )
            except LifecycleError:
                pass
            raise click.ClickException(
                f"daemon spawned from pid {spawned_pid} did not become ready; inspect {log_file}"
            )
        if open_browser:
            _open_ui_in_browser(endpoint)
        stop_command = _daemon_stop_command(
            explicit_vault=explicit_vault,
            vault_path=resolved,
        )
        click.echo(f"Okto Neuron is running in the background (pid={new_pid})")
        click.echo(f"  UI: {endpoint.rstrip('/')}/")
        click.echo(f"  Status: {_daemon_status_command(endpoint, resolved)}")
        click.echo(f"  Stop: {stop_command}")
        click.echo(f"  Logs: {log_file}")
        return

    logger = configure_logging(resolved, log_file=log_file)
    from okto_neuron.llm._telemetry import missing_mlflow_warning

    telemetry_warning = missing_mlflow_warning()
    if telemetry_warning:
        logger.warning(
            "%s",
            telemetry_warning,
            extra={"component": "server", "event": "telemetry.unavailable"},
        )
    try:
        with PidFile(lock_root) as pid_file:
            logger.info(
                "okto-neuron server starting",
                extra={
                    "component": "server",
                    "event": "server.start",
                    "vault": str(resolved) if resolved is not None else None,
                },
            )
            if open_browser:
                _start_ui_browser_thread(
                    _server_endpoint(host, rest_port),
                    expected_pid=pid_file.pid,
                    vault_path=resolved,
                )
            try:
                run_server(
                    vault_path=resolved,
                    host=host,
                    allow_remote=allow_remote,
                    rest_port=rest_port,
                    mcp_port=mcp_port,
                )
            finally:
                logger.info(
                    "okto-neuron server stopped",
                    extra={
                        "component": "server",
                        "event": "server.stop",
                        "vault": str(resolved) if resolved is not None else None,
                    },
                )
        from okto_neuron.server._store_io import exit_if_workers_abandoned

        # The stores are closed and the PID file released; a worker still parked
        # in an LLM/network wait must not keep the process alive.
        exit_if_workers_abandoned()
    except StaleLockError as exc:
        # Idempotent start: surface the running server and exit 1.
        logger.error(
            str(exc),
            extra={
                "component": "server",
                "event": "server.start_refused",
                "vault": str(resolved) if resolved is not None else None,
            },
        )
        endpoint = _server_endpoint(host, rest_port)
        if open_browser and _wait_for_server_health(endpoint, timeout=2.0):
            _open_ui_in_browser(endpoint)
        click.echo(f"UI: {_daemon_ui_url(endpoint)}", err=True)
        raise click.exceptions.Exit(1) from exc
    finally:
        # Drop handlers so subsequent invocations (tests) don't duplicate.
        for h in list(_logging.getLogger("okto_neuron").handlers):
            if getattr(h, "_okto_neuron_json", False):
                _logging.getLogger("okto_neuron").removeHandler(h)
    # Reference for static analysis: emit pid_file_path so import is exercised.
    _ = pid_file_path


@app.command("dev")
@click.option("--vault", type=click.Path(path_type=Path), help="Vault name or root to open.")
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option(
    "--allow-remote",
    is_flag=True,
    default=False,
    help="Reserved compatibility flag; direct remote serving is disabled.",
)
@click.option("--port", "rest_port", default=7777, type=int, help="REST/UI port.")
@click.option("--mcp-port", default=8201, type=int, help="FastMCP port.")
@click.option(
    "--poll",
    "poll_seconds",
    default=1.0,
    type=click.FloatRange(min=0.1),
    help="File polling interval in seconds.",
)
@click.option(
    "--build/--no-build",
    "build_frontend",
    default=True,
    help="Build frontend_dist before start and after frontend source changes.",
)
def dev(
    vault: Path | None,
    host: str,
    allow_remote: bool,
    rest_port: int,
    mcp_port: int,
    poll_seconds: float,
    build_frontend: bool,
) -> None:
    """Run a local dev server; rebuild UI and restart when source files change."""
    if allow_remote or host not in {"127.0.0.1", "localhost", "::1"}:
        raise click.ClickException(
            "direct remote serving is disabled; bind to 127.0.0.1 and use an SSH tunnel"
        )
    from okto_neuron.dev import DevOptions, run_dev

    try:
        code = run_dev(
            DevOptions(
                vault=vault,
                host=host,
                rest_port=rest_port,
                mcp_port=mcp_port,
                allow_remote=allow_remote,
                poll_seconds=poll_seconds,
                build_frontend=build_frontend,
            ),
            echo=click.echo,
        )
    except RuntimeError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1) from exc
    raise click.exceptions.Exit(code)


@app.command("stop")
@click.option(
    "--vault",
    type=click.Path(path_type=Path),
    help="Legacy daemon lock root; new application daemons do not require it.",
)
@click.option(
    "--timeout",
    default=30.0,
    type=float,
    help="Drain budget in seconds. The daemon gets a further close budget "
    "(max(5 s, 25%)) to close its stores; stop waits for both and never escalates.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Send the force request: skip the drain and exit now (stores are not closed).",
)
def stop(vault: Path | None, timeout: float, force: bool) -> None:
    """Stop the application daemon."""
    from okto_neuron.server.lifecycle import (
        LifecycleError,
        consume_stop_outcome,
        read_pid,
        stop_server,
    )

    application_root = _server_lock_root()
    if read_pid(application_root) is not None:
        resolved = application_root
    else:
        # Upgrade compatibility for pre-ADR-0034 vault-scoped daemon locks.
        resolved = _resolve_vault(vault) if vault is not None else _discover_stop_root()
    running_pid = read_pid(resolved)
    if running_pid is not None:
        if force:
            click.echo(f"forcing okto-neuron server (pid={running_pid}) to exit")
        else:
            click.echo(
                f"stopping okto-neuron server (pid={running_pid}); waiting up to "
                f"{timeout:.1f}s for drain plus the store-close budget (no automatic "
                "escalation; use --force to skip the drain)"
            )
    try:
        pid = stop_server(resolved, timeout=timeout, force=force)
    except LifecycleError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1) from exc
    skipped = consume_stop_outcome(resolved, pid)
    if skipped is not None:
        click.echo(
            f"stopped, but the store close was skipped ({skipped['calls_in_flight']} grafx "
            "calls in flight); the next start recovers from the WAL",
            err=True,
        )
        raise click.exceptions.Exit(3)
    click.echo(f"stopped okto-neuron server (pid={pid})")


@app.command("watch")
@click.argument("vault_path", type=click.Path(path_type=Path))
@click.option("--once", "run_once", is_flag=True, help="Drain the inbox once and exit.")
@click.option(
    "--poll",
    "poll_seconds",
    default=None,
    type=float,
    help="Poll interval in seconds (default 5).",
)
def watch(vault_path: Path, run_once: bool, poll_seconds: float | None) -> None:
    """Run the ambient companion over a vault's inbox (``.marginalia/incoming``).

    Drops a markdown note into the inbox and the companion remembers it: the
    confidence gate auto-commits high-confidence candidates and parks the rest on
    the review queue. ``--once`` drains the inbox a single time and exits;
    otherwise it polls until interrupted.
    """
    from okto_neuron.companion import Companion
    from okto_neuron.runner import DEFAULT_POLL_SECONDS, process_once, watch as watch_loop

    root = vault_path.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        click.echo(f"vault not found: {root}", err=True)
        raise click.exceptions.Exit(2)
    if not (vault_config_path(root)).exists():
        click.echo(f"vault is missing okto-neuron.yaml: {root}", err=True)
        raise click.exceptions.Exit(2)

    with vault_writer(root, "watch"):
        vault = Vault.open(root)
        companion = Companion(vault)
        try:
            if run_once:
                results = process_once(root, companion)
                click.echo(f"processed {len(results)} file(s)")
                return
            interval = DEFAULT_POLL_SECONDS if poll_seconds is None else poll_seconds
            inbox = root / ".marginalia" / "incoming"
            click.echo(f"watching {inbox} (poll {interval}s); ctrl-c to stop")
            try:
                for results in watch_loop(root, companion, poll_seconds=interval):
                    if results:
                        click.echo(f"processed {len(results)} file(s)")
            except KeyboardInterrupt:
                click.echo("stopped")
        finally:
            vault.close()


# ── watch-folder CLI group (ADR 0025) ─────────────────────────────────────────


@app.group("watch-folder")
def watch_folder_group() -> None:
    """Manage continuous folder monitoring (ADR 0025).

    The running daemon polls registered roots every ``folder_watch.poll_interval_s``
    seconds and auto-ingests new or changed files once they settle. Roots are stored
    in each selected vault's ``okto-neuron.yaml`` under ``folder_watch.roots``.

    Use ``add`` to register a root (and trigger an initial ingest), ``remove`` to
    stop watching a root, and ``list`` to see what is currently registered.
    """


@watch_folder_group.command("list")
@click.option("--vault", "vault_path", default=None, type=click.Path(path_type=Path))
@_endpoint_option
@_timeout_option
def watch_folder_list(
    vault_path: Path | None,
    endpoint: str | None,
    timeout: float,
) -> None:
    """List all roots registered for continuous monitoring."""
    resolved, vp = _watch_folder_server(endpoint, vault_path, timeout)
    body = _run_client_request(resolved, timeout, "GET", "/api/v1/config", vault=vp)
    fw = body.get("folder_watch") or {}
    click.echo(f"vault: {vp}")
    click.echo(f"folder_watch.enabled: {bool(fw.get('enabled', False))}")
    roots = fw.get("roots") or []
    if not roots:
        click.echo("no watched roots configured")
        return
    for root in roots:
        click.echo(f"  {root}")


def _initial_ingest_payload(root_str: str, recursive: bool) -> dict:
    """Request body for ``POST /api/v1/ingest-folder`` — the endpoint requires
    the folder under the key ``path`` (pinned by a contract test; the original
    implementation posted ``root`` and every initial ingest 400'd)."""
    return {"path": root_str, "recursive": recursive}


def _watch_folder_server(
    endpoint: str | None,
    vault_path: Path | None,
    timeout: float,
) -> tuple[str, Path]:
    """Resolve one explicit/default/sole vault without mutating daemon selection."""
    resolved = resolve_endpoint(endpoint)
    expected = resolve_vault_reference(vault_path).resolve(strict=False)
    if not is_vault(expected):
        click.echo(
            f"vault not found: {expected}; pass --vault when multiple vaults "
            "exist and no default is configured",
            err=True,
        )
        raise click.exceptions.Exit(2)
    return resolved, expected


@watch_folder_group.command("add")
@click.argument("root_path", type=click.Path(path_type=Path))
@click.option("--vault", "vault_path", default=None, type=click.Path(path_type=Path))
@click.option(
    "--no-initial-ingest",
    is_flag=True,
    default=False,
    help="Register the root without triggering an initial ingest.",
)
@_endpoint_option
@_timeout_option
def watch_folder_add(
    root_path: Path,
    vault_path: Path | None,
    no_initial_ingest: bool,
    endpoint: str | None,
    timeout: float,
) -> None:
    """Register ROOT_PATH for continuous monitoring and trigger an initial ingest.

    The running daemon updates only the selected vault and enqueues existing files.

    Requires the daemon to be running (``okto-neuron serve``). The initial ingest is
    skipped when ``--no-initial-ingest`` is given.
    """
    root = root_path.expanduser().resolve()

    if not root.exists() or not root.is_dir():
        click.echo(f"path not found or not a directory: {root}", err=True)
        raise click.exceptions.Exit(1)

    resolved, _vp = _watch_folder_server(endpoint, vault_path, timeout)
    root_str = str(root)
    registration = _run_client_request(
        resolved,
        timeout,
        "POST",
        "/api/v1/folder-watch/roots",
        {"path": root_str},
        vault=_vp,
    )
    click.echo(f"registered: {root}")

    if no_initial_ingest:
        return

    folder_watch = registration.get("folder_watch") or {}
    recursive = bool(folder_watch.get("recursive", True))
    try:
        body = _client_request(
            resolved,
            "POST",
            "/api/v1/ingest-folder",
            _initial_ingest_payload(root_str, recursive),
            timeout=timeout,
            vault=_vp,
        )
    except ClientUnreachable as exc:
        click.echo(
            f"root was registered, but initial ingest failed: {exc}",
            err=True,
        )
        raise click.exceptions.Exit(EXIT_UNREACHABLE) from exc
    except ServerError as exc:
        click.echo(
            "root was registered, but initial ingest failed: "
            f"server error {exc.status_code}: {exc.detail}",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc

    enqueued = body.get("enqueued", 0)
    refreshed = body.get("refreshed", 0)
    skipped = body.get("skipped_non_text", 0)
    msg = f"initial ingest: {enqueued} file(s) queued"
    if refreshed:
        msg += f", {refreshed} already queued (refreshed)"
    if skipped:
        msg += f", {skipped} non-text skipped"
    click.echo(msg)


@watch_folder_group.command("remove")
@click.argument("root_path", type=click.Path(path_type=Path))
@click.option("--vault", "vault_path", default=None, type=click.Path(path_type=Path))
@_endpoint_option
@_timeout_option
def watch_folder_remove(
    root_path: Path,
    vault_path: Path | None,
    endpoint: str | None,
    timeout: float,
) -> None:
    """Remove ROOT_PATH from continuous monitoring.

    The daemon updates only the selected vault. The manifest sidecar is left in place
    because its historical hashes are harmless.
    """
    root = root_path.expanduser().resolve()
    root_str = str(root)
    resolved, _vp = _watch_folder_server(endpoint, vault_path, timeout)
    _run_client_request(
        resolved,
        timeout,
        "DELETE",
        "/api/v1/folder-watch/roots",
        {"path": root_str},
        vault=_vp,
    )
    click.echo(f"removed: {root}")


def handle_marginalia_error(error: OktoNeuronError, *, debug: bool = False) -> None:
    """Render a typed Okto Neuron failure and exit with its documented code."""
    click.echo(error.user_message(), err=True)
    if debug:
        _print_marginalia_debug(error)
    raise click.exceptions.Exit(error.EXIT_CODE)


def _resolve_serve_vault(path: Path | None, *, explicit: bool) -> Path | None:
    if not explicit:
        return None
    resolved = resolve_vault_reference(path)
    if is_vault(resolved):
        return resolved
    if explicit:
        return None
    return None


def _refuse_if_legacy_daemon_running() -> None:
    """Exit 1 when a pre-0.3.0 daemon still holds ``~/.marginalia/runtime``.

    0.3.0 keeps its daemon lock under ``~/.okto-neuron/runtime``, so a 0.2.0
    daemon left running by a manual or PyPI upgrade would not block the new
    lock and two daemons would serve the same vaults.
    """
    from okto_neuron.server.lifecycle import active_server_pid

    legacy_root = legacy_runtime_root()
    pid = active_server_pid(legacy_root)
    if pid is None:
        return
    click.echo(legacy_daemon_running_message(pid, legacy_root), err=True)
    raise click.exceptions.Exit(1)


def _server_lock_root(*, explicit_vault: bool = False, vault_path: Path | None = None) -> Path:
    del explicit_vault, vault_path  # application lifecycle is never vault-scoped
    return default_app_home() / "runtime"


def _resolve_vault(path: Path | None) -> Path:
    return resolve_vault_reference(path)


def _hit_json(hit: object) -> dict[str, object]:
    return {
        "claim_id": getattr(hit, "claim_id"),
        "score": getattr(hit, "score"),
        "path": getattr(hit, "path"),
        "byte_start": getattr(hit, "byte_start"),
        "byte_end": getattr(hit, "byte_end"),
        "content_hash": getattr(hit, "content_hash"),
        "title": getattr(hit, "node").title,
    }


def _parse_algos(raw: str) -> list[str]:
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        raise click.BadParameter("at least one detector is required")
    invalid = [name for name in names if name not in DETECTOR_NAMES]
    if invalid:
        raise click.BadParameter(
            f"unknown detector(s): {', '.join(invalid)}; expected {', '.join(DETECTOR_NAMES)}"
        )
    return names


def _drift_envelope(
    vault: Vault,
    *,
    mode: str,
    detector_names: list[str],
) -> dict[str, object]:
    ran_at = datetime.now(timezone.utc)
    by_detector = {
        detector_name: run_detector(detector_name, vault) for detector_name in detector_names
    }
    findings = [
        _finding_json(vault, finding)
        for detector_findings in by_detector.values()
        for finding in detector_findings
    ]
    return {
        "schema_version": "drift.v1",
        "vault_name": vault.root.name,
        "vault_root": str(vault.root.resolve()),
        "ran_at": ran_at.isoformat(),
        "mode": mode,
        "findings": findings,
        "counts": {
            detector_name: len(detector_findings)
            for detector_name, detector_findings in by_detector.items()
        },
        "total": len(findings),
    }


def _finding_json(vault: Vault, finding: Finding) -> dict[str, object]:
    subject = _finding_subject(vault, finding)
    return {
        "finding_id": finding.id,
        "detector": finding.kind,
        "severity": finding.severity.value,
        "subject": subject,
        "message": finding.message,
        "evidence_claim_ids": finding.evidence_claim_ids,
    }


def _finding_subject(vault: Vault, finding: Finding) -> dict[str, object]:
    evidence_id = finding.evidence_claim_ids[0]
    node = vault.store.get_node(evidence_id)
    if node and node.type == "Block":
        return {
            "doc_path": node.facets.get("source_path") or node.facets.get("path"),
            "block_id": node.id,
        }
    if node and node.type == "Document":
        return {
            "doc_path": node.facets.get("path") or node.facets.get("uri"),
            "block_id": None,
        }
    return {"doc_path": None, "block_id": evidence_id}


def _ingest_markdown(vault: Vault) -> int:
    count = 0
    for path in sorted(vault.root.rglob("*.md")):
        if ".marginalia" in path.parts:
            continue
        vault.add(path)
        count += 1
    return count


def _laptop_gate_rejection() -> str | None:
    value = _compat_getenv("OKTO_NEURON_LAPTOP_GATE", "").strip().lower()
    if value in {
        "0",
        "false",
        "no",
        "reject",
        "rejected",
        "deny",
        "denied",
        "block",
        "blocked",
        "fail",
        "failed",
    }:
        return f"laptop-gate: OKTO_NEURON_LAPTOP_GATE={value}"
    return None


def _debug_requested(args: object) -> bool:
    if args is None:
        tokens = sys.argv[1:]
    elif isinstance(args, str):
        tokens = args.split()
    else:
        tokens = [str(arg) for arg in args]
    return "--debug" in tokens


def _print_marginalia_debug(exc: OktoNeuronError) -> None:
    cause = exc.__cause__
    if cause is None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        return
    click.echo("Chained cause traceback:", err=True)
    traceback.print_exception(type(cause), cause, cause.__traceback__, file=sys.stderr)


def _install_kg_compat_commands() -> None:
    """Keep historical ``kg add/query`` while making ``kg rebuild`` truthful."""
    for name, command in app.commands.items():
        if name != "kg":
            kg_cli.add_command(command, name=name)
    for name, command in kg_group.commands.items():
        if name not in kg_cli.commands:
            kg_cli.add_command(command, name=name)


_install_kg_compat_commands()


if __name__ == "__main__":
    app()
