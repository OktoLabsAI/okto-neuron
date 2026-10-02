"""REST surface for the Okto Neuron server (POST /add, /query, /detect-drift; GET /health, /version).

Application-wide endpoints read the process :class:`~okto_neuron.server.state.ServerState`.
Vault-scoped endpoints bind one immutable :class:`~okto_neuron.server.state.VaultRuntime`
for the request lifetime; its write paths serialize through that runtime's lock,
while reads and work in other vaults can progress concurrently.
Errors are emitted as structured JSON bodies — 4xx for client faults,
5xx for server faults — per spec 9030718e API contracts.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Final

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from okto_neuron._compat import (
    LEGACY_VAULT_HEADER,
    VAULT_HEADER,
    vault_header_value,
    version_payload,
)
from okto_neuron._compat import secret_env as _secret_env
from okto_neuron import __version__ as OKTO_NEURON_VERSION
from okto_neuron._internal.infra import is_infra
from okto_neuron.config._capacity import capacity_report
from okto_neuron.config._vault import DEFAULT_NEW_VAULT_BACKEND
from okto_neuron.detectors import DETECTOR_NAMES, run_detector
from okto_neuron.errors import (
    GRAPH_WRITE_FAILED_CODE,
    EmbeddingDimMismatch,
    FileNotUnderVaultError,
    GraphBackendError,
    GraphWriteExhausted,
    IngestError,
    OktoNeuronError,
    QueryError,
    VaultClosedError,
)
from okto_neuron.predicates import PredicateRegistry, PredicateRegistryError
from okto_neuron.semantic_quality import (
    evaluate_ledger_scan as evaluate_semantic_ledger_scan,
)
from okto_neuron.semantic_quality import evaluate_store as evaluate_semantic_quality
from okto_neuron.server import _curation, _gc_tuning, _jobs, _projection, _scheduler
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server._integrity import IntegrityFenceError
from okto_neuron.server._store_io import (
    acquire_off_loop,
    encode_json,
    encode_op,
    job_io,
    json_bytes_response,
    single_flight,
    store_io,
)
from okto_neuron.consolidate.review_queue_sqlite import ReviewQueueMigrationRequired
from okto_neuron.server._open_failure import client_open_failure
from okto_neuron.server._vault_pool import VaultPoolError, acquire_daemon_writer_lease
from okto_neuron.store.writer_lease import degraded_leases, held_writer_lease
from okto_neuron.server.lifecycle import request_id as bind_request_id
from okto_neuron.server.state import (
    ServerState,
    VaultRuntime,
    bind_vault_runtime,
    get_server_state,
    get_state,
)
from okto_neuron.store.capabilities import capabilities_for
from okto_neuron.store.closed_set import (
    CLOSED_NODE_TYPES,
    PRIMITIVE_NODE_TYPES,
    SUPPORT_NODE_TYPES,
)
from okto_neuron.store.registry import (
    NoSuchBackendError,
    list_graph_backends,
    resolve_graph_backend,
)
from okto_neuron.store.vault import wipe_vault
from okto_neuron.vault import Vault
from okto_neuron.vault_registry import (
    AmbiguousVaultNameError,
    VaultEntry,
    clear_default_vault,
    ensure_global_layout,
    is_vault,
    list_vaults,
    managed_vault_delete_guard,
    mark_managed_vault,
    resolve_vault_backend,
    resolve_vault_reference,
    set_default_vault,
    vault_path_for_name,
)

_LOG = logging.getLogger("okto_neuron.server.http")

API_VERSION = "v1"
EMBEDDING_MODEL = "bge-small-en-v1.5"

# Closed schema (locked): 5 primitives + 6 support types. The KG browser is
# strictly read-only and the ``?type=`` filter is validated against this set —
# empirically the only node types a populated vault mints are a subset of these
# (Agent/Activity/Concept/Place + Claim/Block/Document), so no real node is ever
# rejected. Internal bookkeeping nodes (e.g. SchemaMetadata) are hidden.
# Single definition lives in ``okto_neuron.store.closed_set``, which also fails
# closed on writes; this surface only re-uses it for read-side filtering.
_PRIMITIVE_TYPES: tuple[str, ...] = PRIMITIVE_NODE_TYPES
_SUPPORT_TYPES: tuple[str, ...] = SUPPORT_NODE_TYPES
_TYPE_KIND: dict[str, str] = {
    **{t: "primitive" for t in _PRIMITIVE_TYPES},
    **{t: "support" for t in _SUPPORT_TYPES},
}

# Closed-schema types that NO ingest path ever writes to the store, so a count of
# 0 is the expected steady state rather than a regression signal. ``Finding`` is
# computed on demand by the detectors and returned as a value; ``Identifier`` and
# ``Annotation`` are declared models with no writer. The census reports them so
# the closed schema stays visible, and flags them so a reader (or a golden
# report) can tell "never minted" apart from "live type that happens to be
# empty". Adding a write path for one of these means removing it from this set.
_NEVER_MINTED_TYPES: frozenset[str] = frozenset({"Identifier", "Annotation", "Finding"})

# Loopback Host-header names accepted by the DNS-rebind guard (L1). The empty
# Host (HTTP/1.0, or a client that omits it) is treated as local — DNS-rebind
# requires the browser to send an attacker-controlled *name*.
_LOOPBACK_HOST_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", ""})

# Upper bound on query fan-out (recall/ask ``k``) — caps work per request so a
# single call can't ask the vector scan for an unbounded result set.
MAX_QUERY_K = 100


# --------------------------- error helpers ---------------------------


def _err(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body = {"error": code, "detail": detail, "status": status}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status)


def log_graph_write_failure(exc: Exception, logger: logging.Logger = _LOG) -> None:
    """Log a failed graph write at error level with its cause and traceback.

    The one message shape for this failure on every surface: the REST routes
    reach it through :func:`_graph_write_failed`, and ``remember`` on REST and
    MCP through :func:`log_remember_failure`, so a failure seen by an MCP
    client leaves the same server-side record a REST failure does.
    """
    detail = exc.user_message() if isinstance(exc, OktoNeuronError) else str(exc)
    logger.error("graph write failed: %s", detail, exc_info=exc)


#: What a failed graph write reaches ``remember`` as: an ``IngestError`` the
#: vault minted around a store failure, a store driver's ``GraphBackendError``,
#: or a backend's exhausted-retry type.
_GRAPH_WRITE_ERRORS = (IngestError, GraphBackendError, GraphWriteExhausted)

#: OS errors that mean the caller named a source path that is not a file.
_SOURCE_PATH_OS_ERRORS = (FileNotFoundError, IsADirectoryError, NotADirectoryError)


def _same_path(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    try:
        return Path(os.fsdecode(a)).expanduser().resolve(strict=False) == Path(
            os.fsdecode(b)
        ).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def _remember_caller_error(exc: BaseException, source: str | None) -> str | None:
    """Name the caller's mistake behind a failed ``remember``, or ``None``.

    A caller error is a source the request itself got wrong: a path outside
    the vault, or a path that names no readable file. It is recognized only
    when the failing path IS the requested source, so an ``OSError`` about some
    internal file (a ledger, the graph directory) is never mistaken for one.
    """
    from okto_neuron.companion import SourceOutsideVaultError

    if isinstance(exc, (FileNotUnderVaultError, SourceOutsideVaultError)):
        return "source path is outside the vault"
    os_error = exc.cause if isinstance(exc, IngestError) else exc
    if isinstance(os_error, _SOURCE_PATH_OS_ERRORS):
        wrapped_path = exc.file_path if isinstance(exc, IngestError) else None
        if _same_path(os_error.filename, source) or _same_path(os_error.filename, wrapped_path):
            if isinstance(os_error, FileNotFoundError):
                return "source path not found"
            return "source path is not a file"
    return None


def log_remember_failure(
    exc: BaseException, source: str | None, logger: logging.Logger = _LOG
) -> None:
    """Log a failed ``remember`` the same way on REST and MCP.

    - A caller error (see :func:`_remember_caller_error`) is a WARNING naming
      the mistake, with no traceback: nothing on the server is broken.
    - A failed graph write goes through :func:`log_graph_write_failure`
      (ERROR, cause, traceback).
    - Any other ``OktoNeuronError`` is an expected, typed outcome the client
      already receives in full, so it is not logged.
    - Anything else is an unexpected failure: ERROR with the traceback.
    """
    reason = _remember_caller_error(exc, source)
    if reason is not None:
        detail = exc.user_message() if isinstance(exc, OktoNeuronError) else str(exc)
        logger.warning("remember rejected: %s: %s", reason, detail)
    elif isinstance(exc, _GRAPH_WRITE_ERRORS):
        log_graph_write_failure(exc, logger)
    elif not isinstance(exc, OktoNeuronError):
        logger.error("unexpected remember failure", exc_info=exc)


def _graph_write_failed(exc: Exception, *, logged: bool = False) -> JSONResponse:
    """The shared 500 body for a graph write that failed at any of the four sites.

    ``"error"`` stays the legacy ``"ladybug_write_failed"`` string every existing
    caller/test matches on (M3 spec section 7, D-41: additive, not a replacement).
    ``"codes"`` is a new, purely additive field carrying the backend-neutral
    :data:`okto_neuron.errors.GRAPH_WRITE_FAILED_CODE` alongside it, so a caller
    that wants a stable code once a second graph backend exists can start
    reading ``codes[-1]`` today without breaking on the legacy-only field.
    """
    # Every caller returns this without logging first, so this is the one place
    # a failed graph write reaches the operator log. A wedged vault (an
    # unreceipted sealed plan that every later ingest must resume and cannot)
    # fails each request here; without this line only the first of those
    # failures was visible, so an N-request outage read as one incident.
    if not logged:
        log_graph_write_failure(exc)
    return _err(
        500,
        "ladybug_write_failed",
        f"ladybug write failed: {exc}",
        codes=["ladybug_write_failed", GRAPH_WRITE_FAILED_CODE],
    )


def _k_cap_error(k: int) -> JSONResponse | None:
    """Reject a caller-supplied ``k`` above ``MAX_QUERY_K``.

    Shared by every recall/ask-shaped route — versioned (``/api/v1/recall``,
    ``/api/v1/ask``) and legacy (``/query``, ``/recall``, ``/ask``) alike — so
    the two surfaces can't drift: an oversized ``k`` doesn't multiply compute
    (both retrieval legs scan the full corpus regardless of ``k``), but it does
    uncap response size and, for ``ask``, the LLM seed-set size.
    """
    if k > MAX_QUERY_K:
        return _err(400, "bad_request", f"k must be <= {MAX_QUERY_K}")
    return None


def _integrity_fenced_response(exc: IntegrityFenceError) -> JSONResponse:
    guidance = (
        graph_integrity.INCOMPLETE_GUIDANCE
        if exc.state.status.value == "incomplete"
        else graph_integrity.RECOVERY_GUIDANCE
    )
    return _err(
        409,
        graph_integrity.INTEGRITY_FENCED_CODE,
        str(exc),
        integrity={
            "status": exc.state.status.value,
            "graph_generation": exc.state.graph_generation,
            "writer_fenced": exc.state.writer_fenced,
            "reason": exc.state.reason,
            "recovery_guidance": guidance,
        },
    )


async def _integrity_fence_handler(_request: Request, exc: IntegrityFenceError) -> JSONResponse:
    return _integrity_fenced_response(exc)


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
    except Exception as exc:  # noqa: BLE001
        raise _BadRequest("could not read request body: " + str(exc)) from exc
    if not raw:
        raise _BadRequest("empty request body; expected JSON object")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _BadRequest(f"malformed JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise _BadRequest("request body must be a JSON object")
    return payload


class _ApiError(Exception):
    """A named store operation's client-facing failure, raised off the event loop
    and turned into the same structured JSON error body by its handler."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra

    def response(self) -> JSONResponse:
        return _err(self.status, self.code, self.detail, **self.extra)


class _BadRequest(Exception):
    """Internal sentinel for 400 responses raised inside handlers."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _require(payload: dict[str, Any], key: str, expected: type) -> Any:
    if key not in payload:
        raise _BadRequest(f"missing or invalid field: {key}")
    value = payload[key]
    if not isinstance(value, expected):
        raise _BadRequest(f"missing or invalid field: {key}")
    if expected is str and not value:
        raise _BadRequest(f"missing or invalid field: {key}")
    return value


_VALID_SENSITIVITY_VALUES = ("local_only", "default")


def _parse_sensitivity(payload: dict[str, Any]) -> str:
    """Validate ``sensitivity`` the same way the MCP tool's ``Literal`` does.

    Missing/``None`` defaults to ``"default"``; any other value that isn't
    exactly ``"local_only"`` or ``"default"`` (a typo like ``"local-only"``,
    wrong casing, or a non-string) is a 400, not a silent downgrade to
    ``"default"`` — a rejected note is safer than one committed under the
    wrong sensitivity.
    """
    if "sensitivity" not in payload or payload["sensitivity"] is None:
        return "default"
    raw = payload["sensitivity"]
    if raw not in _VALID_SENSITIVITY_VALUES:
        raise _BadRequest(
            f"invalid sensitivity: {raw!r}; expected one of {list(_VALID_SENSITIVITY_VALUES)}"
        )
    return raw


def _parse_ask_retrieval_policy(payload: dict[str, Any]) -> Any:
    raw = payload.get("retrieval_policy")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _BadRequest("retrieval_policy must be an object")
    from pydantic import ValidationError

    from okto_neuron.companion import AskRetrievalPolicy

    try:
        return AskRetrievalPolicy.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "retrieval_policy"
        msg = first.get("msg", "invalid value")
        raise _BadRequest(f"invalid retrieval_policy.{loc}: {msg}") from exc


def _draining_response() -> JSONResponse:
    state = get_state()
    if getattr(state, "shutting_down", False):
        return _err(503, "shutting_down", "server is shutting down")
    return _err(
        503,
        "maintenance",
        "vault maintenance is in progress; reads remain available but writes are paused",
        retryable=True,
    )


def _internal_error() -> JSONResponse:
    """Generic 500 — the cause is logged server-side via ``_LOG.exception`` and
    never echoed to the client (avoids leaking paths/stack/internal state)."""
    return _err(500, "internal", "internal server error")


# --------------------------- fail-fast writer_lock (curation UI actions) ---------------------------

_CURATION_LOCK_TIMEOUT_S = 5.0


class _LockBusy(Exception):
    """Raised by :func:`_writer_lock_fast` when writer_lock isn't free in time."""


@contextlib.asynccontextmanager
async def _writer_lock_fast(
    state: ServerState,
    *,
    timeout: float = _CURATION_LOCK_TIMEOUT_S,
    verify_write_allowed: bool = True,
):
    """Acquire ``state.writer_lock`` but fail fast (503) instead of hanging.

    Reconcile/predicate/authority confirm-reject are single off-graph JSON writes
    (AuthorityIndex / PredicateAliasIndex / ReconcileQueue side-files) — they never
    touch the graph. They still need ``writer_lock`` (not a dedicated lock) because
    the ``reconcile-apply``/``predicate-apply`` curation JOBS write the SAME side
    files under ``writer_lock`` (``_jobs.py`` worker) — a second, independent lock
    here would let a UI confirm/reject race an in-flight apply job's write to the
    same JSON file (lost-update). But an ingest item, or a rebuild/heal/reembed job,
    can legitimately hold ``writer_lock`` for minutes to over an hour, and unlike
    those background jobs this is a synchronous button click in the UI — it must
    not hang forever waiting behind them (the same class of bug fixed for
    config-PATCH via ``state.config_lock``, which doesn't apply here because THESE
    writes really do need serialization against writer_lock, just not an unbounded
    wait). Raises :class:`_LockBusy` when the lock isn't free within ``timeout``
    seconds so the caller can return a clear 503 instead of blocking the request.

    ``acquired`` is only flipped True once ``wait_for`` returns a real, successful
    acquisition — never on the timeout path — so a timeout never leaves the lock
    held (no leak): the ``finally`` only releases what this context manager itself
    acquired.
    """
    writer_lock = state.writer_lock
    acquired = False
    try:
        await asyncio.wait_for(writer_lock.acquire(), timeout=timeout)
        acquired = True
    except asyncio.TimeoutError:
        raise _LockBusy from None
    try:
        if verify_write_allowed:
            await store_io(graph_integrity.require_write_allowed, state, state.vault)
        yield
    finally:
        if acquired:
            writer_lock.release()


def _vault_open_warning(vault_path: Path, exc: EmbeddingDimMismatch) -> dict[str, object]:
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    return {
        "code": "embedding_dim_mismatch",
        "path": str(resolved),
        "detail": exc.user_message(),
        "remedy": (
            "Rebuild vectors at the configured embedding width through the running "
            "daemon (POST /api/v1/vaults/reembed with {\"vault\": \"<vault name>\"}, which "
            "works while the vault cannot be opened, or the vault manager's Re-embed "
            "button); `okto-neuron kg reembed` is refused while the daemon holds the "
            "vault. Or switch to another vault."
        ),
    }


# --------------------------- locality helpers (L1 + M1) ---------------------------


def _allow_remote() -> bool:
    """True when the server was bound with ``--allow-remote`` (devops-wired)."""
    try:
        return bool(getattr(get_state(), "allow_remote", False))
    except Exception:  # noqa: BLE001 — no state yet → fail safe (enforce loopback)
        return False


def _host_header_is_loopback(request: Request) -> bool:
    """True when the request's ``Host`` header names a loopback target.

    The DNS-rebind defence (L1): a browser tricked into hitting 127.0.0.1 carries
    the *attacker's* domain in ``Host`` — so we accept only loopback names.
    """
    host = request.headers.get("host", "")
    hostname = host.rsplit(":", 1)[0] if host else ""
    hostname = hostname.strip("[]")  # normalise [::1] → ::1
    return hostname in _LOOPBACK_HOST_NAMES


def request_is_loopback(request: Request) -> bool:
    """True when the connecting peer is a loopback address.

    Helper for devops (M1): gate sensitive write routes (config PATCH and MCP
    remember/init_vault) to loopback callers. A real remote peer always presents a valid
    non-loopback IP. The in-process ASGI test transport presents a non-IP host
    (e.g. ``testclient``) which is not a routable remote, so it is treated local.
    """
    client = request.client
    if client is None:
        return True
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return True


def remote_config_allowed(request: Request) -> bool:
    """True only when the caller is on loopback. Sensitive writes (config PATCH
    and MCP remember/init_vault) are always restricted to loopback callers. Direct remote serving
    is disabled; an operator uses a loopback-preserving tunnel such as SSH."""
    return request_is_loopback(request)


class RequestIdMiddleware:
    """Bind a per-request ``request_id`` so every log line emitted while handling
    the request carries it (Task 4 — the field was ``null`` everywhere in the live
    log). Raw ASGI (not ``BaseHTTPMiddleware``) on purpose: it sets the contextvar
    in the SAME task that runs the whole downstream app, so the id propagates across
    the ``BaseHTTPMiddleware`` task hops into the endpoint and its logging (a
    contextvar set inside ``BaseHTTPMiddleware.dispatch`` before ``call_next`` does
    not reliably reach the endpoint). Honors an inbound ``X-Request-ID`` for
    client-side correlation, else mints a short uuid; echoes it as a response header.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rid = _request_id_from_scope(scope) or uuid.uuid4().hex[:12]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = rid
            await send(message)

        with bind_request_id(rid):
            await self.app(scope, receive, _send)


def _request_id_from_scope(scope: dict) -> str | None:
    """Read an inbound ``X-Request-ID`` header from the raw ASGI scope, if present."""
    for name, value in scope.get("headers") or []:
        if name == b"x-request-id":
            text = value.decode("latin-1").strip()
            if text:
                return text[:128]
    return None


class LoopbackHostMiddleware(BaseHTTPMiddleware):
    """L1: reject requests whose ``Host`` header is not a loopback name.

    Direct remote serving is fail-closed, so the compatibility state flag is not
    operator-reachable. Keeping the guard conditional only supports isolated
    policy tests while production startup always leaves it disabled.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not _allow_remote() and not _host_header_is_loopback(request):
            return _err(
                403,
                "forbidden_host",
                "request Host is not a loopback name; this server is bound to "
                "localhost only (use an SSH tunnel for remote access)",
            )
        return await call_next(request)


_UNSAFE_HTTP_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REST_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
)


def _same_origin(request: Request, origin: str) -> bool:
    """Return whether a browser ``Origin`` exactly matches this REST origin."""
    if not origin or origin == "null":
        return False
    return origin.rstrip("/") == f"{request.url.scheme}://{request.url.netloc}"


class LocalAppSecurityMiddleware(BaseHTTPMiddleware):
    """Protect the credential-free loopback UI from browser cross-site writes.

    ADR 0034 intentionally lets any local browser profile open the REST/UI
    origin. Host validation blocks DNS rebinding; this layer rejects unsafe
    cross-origin browser requests and simple ``no-cors``/HTML-form payloads.
    Non-browser local clients may omit Origin/Fetch Metadata, but every write
    still uses the JSON-only contract.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method.upper() in _UNSAFE_HTTP_METHODS:
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                response = _err(
                    403,
                    "forbidden_origin",
                    "cross-origin browser writes are not allowed",
                )
                self._secure(response)
                return response

            origin = request.headers.get("origin")
            if origin is not None and not _same_origin(request, origin):
                response = _err(
                    403,
                    "forbidden_origin",
                    "cross-origin browser writes are not allowed",
                )
                self._secure(response)
                return response

            content_type = request.headers.get("content-type", "")
            media_type = content_type.partition(";")[0].strip().lower()
            if media_type != "application/json":
                response = _err(
                    415,
                    "unsupported_media_type",
                    "write requests require Content-Type: application/json",
                )
                self._secure(response)
                return response

        response = await call_next(request)
        self._secure(response)
        return response

    @staticmethod
    def _secure(response: Any) -> None:
        for name, value in _REST_SECURITY_HEADERS:
            response.headers[name] = value


# Distinguishable 401 details. The ``error`` code stays ``unauthorized`` for
# every case (clients match on it); only the human-facing detail differs, and
# it never echoes any part of the presented credential.
_BEARER_DETAIL = {
    "missing": "missing Authorization header; send `Authorization: Bearer <token>`",
    "empty": (
        "Authorization header carried an empty bearer credential; "
        "the token placeholder likely expanded to nothing"
    ),
    "scheme": "unsupported Authorization scheme; only `Bearer` is accepted",
    "invalid": "bearer token is not valid for this server",
}


def _presented_bearer_token(scope: dict) -> tuple[str | None, str]:
    """Extract the MCP bearer credential and why it is unusable, if it is.

    Returns ``(token, reason)`` where ``reason`` is a key of
    :data:`_BEARER_DETAIL`; it is meaningless when ``token`` is not ``None``.
    """
    headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if not auth.strip():
        return None, "missing"
    if auth[:7].lower() != "bearer ":
        return (None, "empty") if auth.strip().lower() == "bearer" else (None, "scheme")
    token = auth[7:].strip()
    if not token:
        return None, "empty"
    return token, "invalid"


class AuthTokenMiddleware:
    """Require the application capability as an MCP Authorization bearer.

    REST/UI deliberately does not install this middleware. Query parameters,
    browser cookies, and legacy alternate headers are never credentials.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            expected = get_state().auth_token
        except Exception:  # noqa: BLE001 — fail closed if runtime wiring is incomplete
            await _send_json(
                send,
                503,
                {"error": "unavailable", "detail": "server state unavailable", "status": 503},
            )
            return

        presented, reason = _presented_bearer_token(scope)
        if presented is not None:
            if secrets.compare_digest(presented, expected):
                await self.app(scope, receive, send)
                return
            reason = "invalid"

        await _send_json(
            send,
            401,
            {
                "error": "unauthorized",
                "detail": _BEARER_DETAIL[reason],
                "status": 401,
            },
            extra_headers=[
                (b"cache-control", b"no-store"),
                (b"www-authenticate", b'Bearer realm="okto-neuron-mcp"'),
            ],
        )


async def _send_json(
    send: Any,
    status: int,
    body: dict,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Emit a JSON response from raw-ASGI middleware (no Request/Response objects)."""
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin-1")),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


_NO_VAULT_BLOCKED_BARE_PATHS: frozenset[str] = frozenset(
    {
        "/add",
        "/query",
        "/detect-drift",
        "/remember",
        "/recall",
        "/ask",
        "/review-queue",
        "/review-queue/batch",
        "/resolve-review",
    }
)

_GLOBAL_APP_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/version",
        "/api/v1/backends",
        "/api/v1/config/defaults",
        "/api/v1/config/defaults/llm/test",
        "/api/v1/config/defaults/llm/test-completion",
        "/api/v1/config/defaults/embedding/models",
        "/api/v1/config/defaults/embedding/test",
        "/api/v1/credentials/provider",
        "/api/v1/llm/credential",
    }
)

# These routes need an immutable vault context but deliberately do not borrow a
# graph handle in middleware.  Reset and reembed own the stronger
# fence -> zero leases -> release -> replace protocol themselves; holding the
# initiating request lease would deadlock that protocol.  Reembed status reads
# only its sidecar/runtime flags and must stay pollable while the graph is fenced.
_VAULT_LEASE_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/reset",
        "/api/v1/embedding/reembed",
        "/api/v1/embedding/reembed/status",
    }
)


def _pool_open_error_response(exc: VaultPoolError) -> JSONResponse:
    """REST answer for a failed vault open/lease.

    A width mismatch gets the stable ``embedding_dim_mismatch`` code, the real reason and
    the working remedy, path-free; every other pool error keeps its code and text.
    """
    mismatch = client_open_failure(exc)
    if mismatch is not None:
        _LOG.warning("vault open refused (embedding width mismatch): %s", exc)
        return _err(409, mismatch[0], mismatch[1])
    status = 503 if exc.code == "pool_full" else 409
    return _err(status, exc.code, str(exc))


def _request_is_vault_scoped(path: str) -> bool:
    """Return whether one REST path operates against a selected vault."""

    if (
        path in _GLOBAL_APP_PATHS
        or path.startswith("/api/v1/vaults")
        or path.startswith("/api/v1/credentials")
        or path.startswith("/api/v1/providers")
        or path == "/api/v1/provider-types"
    ):
        return False
    return path.startswith("/api/") or path in _NO_VAULT_BLOCKED_BARE_PATHS


def _resolve_rest_runtime(
    state: ServerState,
    selector: str | None,
) -> VaultRuntime | None:
    """Resolve a browser-tab selector only through the registered vault list."""

    entries = list_vaults(current=state.vault_path if state.vault_path is not None else None)
    if not selector:
        # Explicit startup ``--vault`` remains a compatibility fallback, but the
        # default application daemon intentionally does not mutate that process
        # pointer. Resolve an unscoped CLI operation to the configured/default or
        # sole registered vault for this request only.
        active = state.active_runtime
        if active is not None:
            return active
        try:
            preferred = resolve_vault_reference(None).resolve(strict=False)
        except AmbiguousVaultNameError:
            raise
        except (OSError, ValueError):
            preferred = None
        if preferred is not None:
            match = next((entry for entry in entries if entry.path == preferred), None)
            if match is not None:
                return state.runtime_for(match.path, rehydrate=True)
        if len(entries) == 1:
            return state.runtime_for(entries[0].path, rehydrate=True)
        return None
    if len(selector) > 4096 or any(character in selector for character in ("\r", "\n", "\x00")):
        raise ValueError("invalid vault selector")
    matches = [entry for entry in entries if selector in {entry.id, entry.name, str(entry.path)}]
    if len(matches) > 1:
        raise AmbiguousVaultNameError(selector, [entry.path for entry in matches])
    if not matches:
        raise FileNotFoundError(selector)
    return state.runtime_for(matches[0].path, rehydrate=True)


class ActiveVaultMiddleware:
    """Bind each vault-scoped request to one immutable runtime and pool lease.

    The historical class name is retained for import compatibility. Unlike the
    old process-global active-vault gate, this raw ASGI middleware holds the
    lease and ContextVar for the complete response lifetime. Browser tabs select
    independently with ``X-Okto-Neuron-Vault`` (or the pre-0.3.0 ``X-Marginalia-Vault``); unscoped CLI callers temporarily
    fall back to the configured compatibility runtime.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not _request_is_vault_scoped(path):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        selector = vault_header_value(request.headers)
        # Diagnostics without a selector describe the application as a whole.
        # A header-scoped status request below binds one exact runtime instead.
        if path == "/api/v1/status" and selector is None:
            await self.app(scope, receive, send)
            return
        try:
            state = get_server_state()
            # Registry scan + runtime rehydrate read YAML/sidecars: off-loop.
            runtime = await store_io(_resolve_rest_runtime, state, selector)
        except AmbiguousVaultNameError as exc:
            response = _err(409, "ambiguous_vault", str(exc))
            await response(scope, receive, send)
            return
        except ValueError as exc:
            response = _err(400, "bad_vault_selector", str(exc))
            await response(scope, receive, send)
            return
        except FileNotFoundError:
            response = _err(404, "vault_not_found", "selected vault is not registered")
            await response(scope, receive, send)
            return
        except VaultPoolError as exc:
            response = _pool_open_error_response(exc)
            await response(scope, receive, send)
            return
        except Exception:  # noqa: BLE001
            _LOG.exception("vault runtime resolution failed")
            response = _internal_error()
            await response(scope, receive, send)
            return

        if runtime is None:
            response = _err(
                409,
                "no_active_vault",
                "no vault selected; choose or create one in the application",
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["vault_runtime"] = runtime

        async def _send_with_vault(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[VAULT_HEADER.lower()] = str(runtime.vault_path)
                headers[LEGACY_VAULT_HEADER.lower()] = str(runtime.vault_path)
            await send(message)

        if path in _VAULT_LEASE_EXEMPT_PATHS:
            with bind_vault_runtime(runtime):
                await self.app(scope, receive, _send_with_vault)
            return

        try:
            # A lease may open the vault under the pool lock (seconds for a cold
            # or large graph): never on the loop that also serves /health.
            lease = await acquire_off_loop(runtime.lease_vault)
        except VaultPoolError as exc:
            response = _pool_open_error_response(exc)
            await response(scope, receive, send)
            return

        with lease, bind_vault_runtime(runtime):
            await self.app(scope, receive, _send_with_vault)


def _ladybug_version() -> str:
    try:
        import ladybug  # type: ignore

        return getattr(ladybug, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "unavailable"


# --------------------------- handlers ---------------------------

# Task 4: how long the global folder-watch loop may go without polling ANY
# configured root before operational status calls it stalled. Generous vs the default
# poll_interval_s so a slow-polling but healthy watcher never false-trips.
_FOLDER_WATCH_STALL_S = 300.0
_VAULT_DELETE_LEASE_WAIT_S = 2.0
_VAULT_MAINTENANCE_LEASE_WAIT_S = 30.0


def _folder_watch_poll_age(now: float) -> float | None:
    """Seconds since the most-recent folder-watch poll across ALL vaults, or None
    when no vault has ever been polled (folder-watch inert/unconfigured). Derived
    from the cheap in-memory status snapshot — never opens a graph or hits disk."""
    from okto_neuron.server._folder_watch import get_watch_status

    latest: float | None = None
    for snap in get_watch_status().values():
        ts = snap.get("last_poll_ts")
        if isinstance(ts, (int, float)) and (latest is None or ts > latest):
            latest = float(ts)
    if latest is None:
        return None
    return max(0.0, now - latest)


async def health(request: Request) -> JSONResponse:
    """Unauthenticated process liveness with no operational metadata."""
    state = get_state()
    if state.shutting_down:
        return JSONResponse(
            {"status": "shutting_down"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


def _aggregate_ingest_summaries(summaries: list[dict]) -> dict[str, object]:
    return {
        key: sum(int(summary.get(key) or 0) for summary in summaries)
        for key in ("total", "queued", "processing", "done", "error", "cancelled")
    } | {
        "active": any(bool(summary.get("active")) for summary in summaries),
        "cancel_requested": any(bool(summary.get("cancel_requested")) for summary in summaries),
    }


async def api_status(request: Request) -> JSONResponse:
    """Credential-free operational diagnostics for the CLI and local UI."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    # Concurrent pollers share one execution (the payload walks every vault's
    # sidecars). Nothing is cached after it completes: the next call recomputes,
    # and the last_degraded_reasons transition log runs once per execution. The
    # key carries the scope so a vault-scoped call never shares an application
    # result or another vault's. JSONResponse renders the dict immediately and
    # nobody mutates it, so sharing the same object across waiters is safe.
    scope = "application" if isinstance(state, ServerState) else str(state.vault_path)
    payload = await single_flight(("status_payload", scope), _status_payload, state)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _queue_layout_refusal(vault_path: Path) -> dict[str, str] | None:
    """Per-vault refusal state for a vault whose review queue is still JSON (#14)."""

    from okto_neuron.consolidate.review_queue import (
        clear_layout_refusal_log,
        layout_refusal,
        log_layout_refusal_once,
    )

    refusal = layout_refusal(vault_path)
    if refusal is None:
        clear_layout_refusal_log(vault_path)
        return None
    log_layout_refusal_once(vault_path, refusal)
    return {"state": "migration_required", **refusal}


def _grafx_buffer_budget(state: ServerState | VaultRuntime, vault_path: Path) -> int | None:
    """Buffer budget of the vault's open grafx store, None when not open / not grafx."""
    handle = (
        state.vault_pool.peek(vault_path) if isinstance(state, ServerState) else state.vault
    )
    value = getattr(getattr(handle, "store", None), "buffer_budget_bytes", None)
    return value if isinstance(value, int) else None


def _status_payload(state: ServerState | VaultRuntime) -> dict[str, Any]:
    """Store op: discover runtimes, read integrity verdicts and backend pins."""
    now = time.time()
    reasons: list[str] = []
    application_scope = isinstance(state, ServerState)
    runtimes = state.runtimes(discover=True) if application_scope else (state,)

    # 1. kill -9 boots an empty graph: the vault silently re-created its graph
    #    after on-disk corruption. status: ok would hide a wiped graph.
    handles = (
        [state.vault_pool.peek(runtime.vault_path) for runtime in runtimes]
        if application_scope
        else [state.vault]
    )
    recovered_handles = [
        handle
        for handle in handles
        if handle is not None and getattr(handle, "recovered_from_corruption", False)
    ]
    recovered = bool(recovered_handles)
    recovery_modes = {getattr(handle, "recovery_mode", None) for handle in recovered_handles}
    recovery_mode = (
        None
        if not recovery_modes
        else next(iter(recovery_modes))
        if len(recovery_modes) == 1
        else "mixed"
    )
    if recovered:
        if recovery_mode == "checkpoint":
            reasons.append(
                "recovered_from_corruption: a torn WAL was quarantined and the graph "
                "was RECOVERED from the last checkpoint after on-disk corruption; "
                "writes since that checkpoint were lost — re-ingest recent sources or "
                "run `kg rebuild`"
            )
        else:
            reasons.append(
                "recovered_from_corruption: the graph was quarantined and re-created "
                "EMPTY after on-disk corruption; run `kg rebuild` to reconstruct from sources"
            )

    integrity_summaries = [
        graph_integrity.summary(runtime, vault)
        for runtime, vault in zip(runtimes, handles, strict=True)
    ]
    terminal_integrity_fences = sum(
        1
        for integrity in integrity_summaries
        if integrity["status"] in {"failed", "incomplete"} and integrity["writer_fenced"]
    )
    if terminal_integrity_fences:
        reasons.append(
            "integrity_fenced: "
            f"{terminal_integrity_fences} vault graph(s) failed or could not complete "
            "integrity verification; reads remain available and a fresh-graph rebuild is required"
        )

    # 2. Ingest recency — SURFACED, never a degraded trigger on its own: an idle
    #    vault legitimately has a large value (making it degrade would false-trip
    #    every quiet daemon).
    observed_ingests = [
        runtime.last_ingest_at for runtime in runtimes if runtime.last_ingest_at is not None
    ]
    last_ingest = max(observed_ingests) if observed_ingests else None
    seconds_since_last_ingest = (now - last_ingest) if last_ingest is not None else None

    # 3. Folder-watch task died / stalled: its exception used to kill the loop
    #    with no signal, stopping ALL auto-ingest forever.
    folder_watch_last_poll_age = _folder_watch_poll_age(now)
    watch_task = getattr(state, "folder_watch_task", None)
    folder_watch_running = watch_task is None or not watch_task.done()
    if watch_task is not None and watch_task.done():
        reasons.append(
            "folder_watch_stopped: the folder-watch task is no longer running; "
            "filesystem auto-ingest is stalled (restarts="
            f"{getattr(state, 'folder_watch_restart_count', 0)})"
        )
    elif (
        folder_watch_last_poll_age is not None
        and folder_watch_last_poll_age > _FOLDER_WATCH_STALL_S
    ):
        reasons.append(
            f"folder_watch_stalled: no poll for {folder_watch_last_poll_age:.0f}s "
            f"(> {_FOLDER_WATCH_STALL_S:.0f}s stall threshold)"
        )

    # 4. Queue errors: items that failed ingest or degraded to a partial yield.
    queue_error_count = sum(
        1
        for runtime in runtimes
        for item in runtime.ingest_queue
        if item.status == "error"
        or (item.status == "done" and getattr(item, "provider_error", None))
    )
    if queue_error_count > 0:
        reasons.append(f"queue_errors: {queue_error_count} ingest item(s) failed or degraded")

    # 5. Curation job watchdog (issue #24): a running job with no progress past its
    #    limit holds the vault's writer lock (snapshot jobs) and is otherwise
    #    invisible: the vault just looks busy forever.
    stalled = [
        (runtime, job) for runtime in runtimes for job in _jobs.stalled_jobs(runtime, now)
    ]
    if stalled:
        worst = max(job.stalled_for_s(now) or 0.0 for _runtime, job in stalled)
        kinds = ", ".join(sorted({job.kind for _runtime, job in stalled}))
        reasons.append(
            f"curation_job_stalled: {len(stalled)} running curation job(s) ({kinds}) made no "
            f"progress for up to {worst:.0f}s; a stuck model call may be holding the vault's "
            "writer lock"
        )
    # 6. Writer lease degraded: the filesystem has no working flock, so nothing
    #    stops a CLI from writing this vault while the daemon serves it.
    for lease_vault, lease_reason in sorted(degraded_leases().items()):
        reasons.append(f"writer_lease_degraded: {lease_vault.name}: {lease_reason}")
    # One summary-only pass per vault, reused for the aggregate and per-vault rows.
    runtime_ingest = {runtime.vault_path: iq.summary(runtime) for runtime in runtimes}
    ingest_summary = (
        _aggregate_ingest_summaries(list(runtime_ingest.values()))
        if application_scope
        else iq.summary(state)
    )
    queue_refusals = {
        runtime.vault_path: _queue_layout_refusal(runtime.vault_path) for runtime in runtimes
    }
    for refused_path, refusal in sorted(queue_refusals.items()):
        if refusal is not None:
            reasons.append(
                f"review_queue_migration_required: {refused_path.name}: {refusal['remedy']}"
            )
    vault_summaries = [
        {
            "path": str(runtime.vault_path),
            "backend": resolve_vault_backend(runtime.vault_path),
            "grafx_buffer_budget_bytes": _grafx_buffer_budget(state, runtime.vault_path),
            "draining": runtime.draining,
            "ingest": runtime_ingest[runtime.vault_path],
            "curation": _jobs.summary(runtime),
            "maintenance": bool(runtime.maintenance_tasks),
            "integrity": integrity,
            # A v1 vault is refused (no open, no writes) until explicitly migrated.
            "review_queue": queue_refusals[runtime.vault_path]
            or {"state": "ok", "code": None, "remedy": None},
        }
        for runtime, integrity in zip(runtimes, integrity_summaries, strict=True)
    ]

    if application_scope:
        for refused_path, refusal in sorted(state.queue_refusals().items()):
            if refused_path in queue_refusals:
                continue
            reasons.append(
                f"review_queue_migration_required: {refused_path.name}: {refusal['remedy']}"
            )
            vault_summaries.append(
                {
                    "path": str(refused_path),
                    "refused": True,
                    "review_queue": {"state": "migration_required", **refusal},
                }
            )

    status = "degraded" if reasons else "ok"
    payload: dict[str, Any] = {
        "status": status,
        "scope": "application" if application_scope else "vault",
        "vault_path": str(state.vault_path) if state.vault_path is not None else None,
        "backend": resolve_vault_backend(state.vault_path) if state.vault_path is not None else None,
        "active_vault": state.has_vault,
        "vault_count": len(runtimes),
        "vaults": vault_summaries,
        "vault_warning": state.vault_open_error,
        "uptime_s": state.uptime_seconds(),
        "pid": state.pid,
        "gc": _gc_tuning.snapshot(),
        "recovered_from_corruption": recovered,
        "recovery_mode": recovery_mode,
        "seconds_since_last_ingest": seconds_since_last_ingest,
        "folder_watch_running": folder_watch_running,
        "folder_watch_last_poll_age": folder_watch_last_poll_age,
        "folder_watch_restart_count": getattr(state, "folder_watch_restart_count", 0),
        "queue_error_count": queue_error_count,
        "ingest": ingest_summary,
        "integrity": (
            {
                "vaults": len(integrity_summaries),
                "writer_fenced": sum(
                    1 for integrity in integrity_summaries if integrity["writer_fenced"]
                ),
                "failed_or_incomplete": terminal_integrity_fences,
            }
            if application_scope
            else integrity_summaries[0]
        ),
        **version_payload(OKTO_NEURON_VERSION),
        "ladybug_version": _ladybug_version(),
        "embedding_model": EMBEDDING_MODEL,
        "api_version": API_VERSION,
    }
    # Dedupe on STABLE reason keys (the part before ":"), not the full rendered
    # strings: some reasons embed live values (e.g. folder_watch_stalled's poll
    # age) that change on every poll, which would make every poll look like a
    # "transition" and defeat the log-once behavior below exactly when it
    # matters (a watcher stuck stalled for hours).
    current_reason_keys = tuple(r.split(":", 1)[0] for r in reasons)
    if reasons:
        payload["degraded_reasons"] = reasons
        # ERROR (not warning): a degraded daemon needs an operator log line, and it
        # carries the bound request_id so the health probe is correlatable. Logged
        # only on a TRANSITION (first degraded poll, or the reason KEY set
        # changing) — not on every poll, else a daemon that stays degraded for
        # hours floods the log with identical lines (live incident: hundreds of
        # repeats), and not on every value-only mutation of an already-known
        # reason (e.g. a stall-age counter ticking up).
        if current_reason_keys != state.last_degraded_reasons:
            _LOG.error(
                "health degraded: %s",
                "; ".join(reasons),
                extra={"event": "health_degraded"},
            )
    elif state.last_degraded_reasons:
        # Reasons cleared after being non-empty: log the recovery once.
        _LOG.info(
            "health recovered",
            extra={"event": "health_recovered"},
        )
    state.last_degraded_reasons = current_reason_keys
    return payload


async def version(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            **version_payload(OKTO_NEURON_VERSION),
            "api_version": API_VERSION,
        },
        headers={"Cache-Control": "no-store"},
    )


def _vault_entries(state: ServerState) -> list[VaultEntry]:
    entries = list_vaults(current=state.vault_path if state.vault_path is not None else None)
    if state.vault_path is not None and not any(
        entry.path == state.vault_path for entry in entries
    ):
        entries.append(
            VaultEntry(
                name=state.vault_path.name,
                path=state.vault_path,
                current=True,
                backend=resolve_vault_backend(state.vault_path),
            )
        )
    return sorted(entries, key=lambda entry: (not entry.current, entry.name.lower()))


def _vaults_payload(state: ServerState) -> dict[str, object]:
    entries = _vault_entries(state)
    current = next((entry for entry in entries if entry.current), None)
    warning = state.vault_open_error
    vaults = []
    for entry in entries:
        payload = entry.to_json()
        if warning and payload.get("path") == warning.get("path"):
            payload["issue"] = warning
        vaults.append(payload)
    payload = {
        "status": "ok",
        "current": current.to_json() if current is not None else None,
        "vaults": vaults,
    }
    if warning is not None:
        payload["warning"] = warning
    return payload


async def api_vaults(request: Request) -> JSONResponse:
    return JSONResponse(await store_io(_vaults_payload, get_state()))


async def api_backends(request: Request) -> JSONResponse:
    """List every graph backend resolvable in this process (M3 spec section 2.6).

    Names only, official first — ``list_graph_backends`` enumerates without
    importing or validating a backend, so an unregistered/broken third-party
    backend still shows up here rather than raising. ``capabilities`` is the
    declarative :class:`~okto_neuron.store.capabilities.BackendCapabilities`
    for that name, or ``None`` for a resolvable backend that has not
    registered a capabilities entry (a valid, if unusual, state — see
    ``capabilities_for``'s own docstring).
    """
    # Entry-point discovery reads installed package metadata from disk.
    return JSONResponse(await store_io(_backends_payload))


def _backends_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "capabilities": (
                asdict(capabilities) if (capabilities := capabilities_for(name)) is not None else None
            ),
        }
        for name in list_graph_backends()
    ]


def _parse_packs(raw: Any) -> list[str]:
    if raw is None:
        return ["core", "research", "personal"]
    if isinstance(raw, str):
        packs = [pack.strip() for pack in raw.split(",") if pack.strip()]
        if packs:
            return packs
    if isinstance(raw, list) and all(isinstance(pack, str) and pack.strip() for pack in raw):
        return [pack.strip() for pack in raw]
    raise _BadRequest("packs must be a comma-separated string or string array")


def _vault_scaffold_accepts_backend() -> bool:
    """Whether ``Vault.scaffold`` has grown the M3 ``backend`` pin parameter yet.

    This REST route's optional ``backend`` field (spec section 2.6) and the
    vault-level pin it selects (``Vault.scaffold``/``_write_config``, spec
    section 2.5) are separate pieces of M3 work. Feature-detect instead of
    assuming either lands first, so this route works whether it runs before
    or after ``Vault.scaffold`` gains the parameter — and so a caller asking
    for a real non-default backend gets a clear 400 today instead of silently
    getting a Ladybug vault back.
    """
    return "backend" in inspect.signature(Vault.scaffold).parameters


def _initialize_managed_vault(
    state: ServerState,
    target_path: Path,
    *,
    name: str,
    packs: list[str] | None,
    embedding_provider: str | None,
    backend: str = DEFAULT_NEW_VAULT_BACKEND,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    allow_remote_db: bool = False,
    embedding_spec: dict[str, Any] | None = None,
) -> None:
    """Create and mark one named vault under the worker-owned mutation lock.

    ``embedding_spec`` is the validated sparse ``embedding`` block the vault is created
    with (see ``validate_new_vault_embedding_spec``); it reaches the vault config before
    the graph is first opened, so the graph is born at the spec's width.

    ``backend`` defaults to ``DEFAULT_NEW_VAULT_BACKEND`` (grafx, D-94) so the
    MCP ``init_vault`` tool (``server/runtime.py``) keeps calling this without
    naming it and still gets the product default, not a stale Ladybug default.

    The initialized handle is always closed in this worker. A non-cancelled
    caller registers the lightweight runtime on the event-loop side; a cancelled
    caller therefore leaves a complete durable vault but no leaked raw handle.
    """

    def _mutate() -> None:
        if state.vault_pool.is_fenced(target_path):
            raise VaultPoolError(
                "vault_fenced",
                f"vault path is unavailable after deletion or maintenance: {target_path}",
            )
        if is_vault(target_path):
            raise FileExistsError(f"vault already exists: {target_path}")
        # Only widen the call when a non-default backend was actually requested,
        # so this stays the exact 2-keyword call every existing test double for
        # _create_inheriting_vault (a well-established DI seam) already expects.
        # Compared against DEFAULT_NEW_VAULT_BACKEND, not a literal "ladybug",
        # so the narrow (default) path tracks whatever the product default is.
        create_kwargs: dict[str, Any] = dict(
            packs=packs, embedding_provider=embedding_provider
        )
        if embedding_spec:
            # Only widened when a spec was given: the no-spec call stays exactly as before.
            create_kwargs["embedding_spec"] = embedding_spec
        if backend != DEFAULT_NEW_VAULT_BACKEND:
            create_kwargs["backend"] = backend
            create_kwargs["storage_uri"] = storage_uri
            create_kwargs["storage_credential_env"] = storage_credential_env
            create_kwargs["storage_database"] = storage_database
            create_kwargs["storage_allow_remote"] = allow_remote_db
        new_vault = _create_inheriting_vault(target_path, **create_kwargs)
        try:
            mark_managed_vault(target_path, name=name)
        finally:
            new_vault.close()

    state.run_application_mutation(_mutate)


def _create_inheriting_vault(
    target_path: Path,
    *,
    packs: list[str] | None,
    embedding_provider: str | None,
    backend: str = DEFAULT_NEW_VAULT_BACKEND,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
    embedding_spec: dict[str, Any] | None = None,
) -> Vault:
    """Create one vault whose sparse config extends the application defaults.

    ``embedding_spec`` (provider/model/dimension/api_base/api_key_env/allow_remote, already
    validated) is merged into the sparse ``embedding`` override BEFORE the first open of the
    graph, which is created at the configured width; without it the width is the
    application default.

    ``backend`` defaults to ``DEFAULT_NEW_VAULT_BACKEND``, NOT ``Vault.scaffold``'s
    own bare "ladybug" default (see the comment on ``Vault.scaffold``): every
    product entry point, including this one, must pass the product default
    explicitly rather than inherit the library-level one.
    """

    from okto_neuron.config import VaultConfig

    defaults = VaultConfig.load_application_defaults()
    scaffold_kwargs: dict[str, Any] = dict(
        packs=packs or defaults.packs,
        embedder=embedding_provider or defaults.embedding.provider,
        allow_external_sources=True,
    )
    if _vault_scaffold_accepts_backend():
        scaffold_kwargs["backend"] = backend
        scaffold_kwargs["storage_uri"] = storage_uri
        scaffold_kwargs["storage_credential_env"] = storage_credential_env
        scaffold_kwargs["storage_database"] = storage_database
        scaffold_kwargs["storage_allow_remote"] = storage_allow_remote
    Vault.scaffold(target_path, **scaffold_kwargs)
    VaultConfig.enable_application_inheritance(target_path)
    overrides: dict[str, Any] = {}
    if packs is not None:
        overrides["packs"] = packs
    if embedding_provider is not None:
        overrides["embedding"] = {"provider": embedding_provider}
    if embedding_spec:
        overrides["embedding"] = {**overrides.get("embedding", {}), **embedding_spec}
    if overrides:
        VaultConfig.apply_patch(target_path, overrides)
    return Vault.open(target_path)


async def api_vault_create(request: Request) -> JSONResponse:
    state = get_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "vault creation is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        name = _require(payload, "name", str)
        packs = _parse_packs(payload["packs"]) if "packs" in payload else None
        embedder = payload.get("embedder")
        if embedder is not None and (not isinstance(embedder, str) or not embedder.strip()):
            raise _BadRequest("embedder must be a non-empty string")
        embedding_spec = _parse_embedding_spec(payload.get("embedding"), embedder)
        backend_raw = payload.get("backend")
        if backend_raw is not None and (
            not isinstance(backend_raw, str) or not backend_raw.strip()
        ):
            raise _BadRequest("backend must be a non-empty string")
        backend = backend_raw.strip() if backend_raw else DEFAULT_NEW_VAULT_BACKEND
        accept_experimental_raw = payload.get("accept_experimental", False)
        if not isinstance(accept_experimental_raw, bool):
            raise _BadRequest("accept_experimental must be a boolean")
        accept_experimental = accept_experimental_raw
        storage_uri = payload.get("storage_uri")
        if storage_uri is not None and (
            not isinstance(storage_uri, str) or not storage_uri.strip()
        ):
            raise _BadRequest("storage_uri must be a non-empty string")
        storage_credential_env = payload.get("storage_credential_env")
        if storage_credential_env is not None and (
            not isinstance(storage_credential_env, str) or not storage_credential_env.strip()
        ):
            raise _BadRequest("storage_credential_env must be a non-empty string")
        storage_database = payload.get("storage_database")
        if storage_database is not None and (
            not isinstance(storage_database, str) or not storage_database.strip()
        ):
            raise _BadRequest("storage_database must be a non-empty string")
        allow_remote_db_raw = payload.get("allow_remote_db", False)
        if not isinstance(allow_remote_db_raw, bool):
            raise _BadRequest("allow_remote_db must be a boolean")
        allow_remote_db = allow_remote_db_raw
        if storage_uri:
            from okto_neuron.config._vault import _classify_storage_endpoint

            try:
                endpoint_class = _classify_storage_endpoint(storage_uri.strip(), resolve=False)
            except ValueError as exc:
                raise _BadRequest(str(exc)) from exc
            if endpoint_class != "loopback" and not allow_remote_db:
                raise _BadRequest(
                    f"storage_uri {storage_uri.strip()!r} is not loopback; set "
                    '"allow_remote_db": true to confirm remote storage egress'
                )
        try:
            resolve_graph_backend(backend)
        except NoSuchBackendError as exc:
            raise _BadRequest(str(exc)) from exc
        if backend != "ladybug" and not _vault_scaffold_accepts_backend():
            raise _BadRequest(
                f"backend {backend!r} does not support vault creation yet; only "
                "'ladybug' can be created today"
            )
        backend_capabilities = capabilities_for(backend)
        if (
            backend_capabilities is not None
            and backend_capabilities.experimental
            and not accept_experimental
        ):
            raise _BadRequest(
                f"backend {backend!r} is experimental (D-12): its on-disk format is "
                "pre-alpha and may change without a migration path, and it ships "
                "under the Elastic License 2.0 plus an Okto Labs Addendum, which is "
                'not OSI-approved. Pass "accept_experimental": true to create a '
                "vault on it."
            )
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    try:
        target_path, exists = await store_io(_vault_create_target, name.strip())
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("vault layout initialization failed")
        return _err(500, "vault_create_failed", f"vault layout initialization failed: {exc}")

    if exists:
        return _err(409, "vault_exists", f"vault already exists: {target_path}")

    # Creation is application-level and touches a brand-new path. Serialize only
    # against another registry/config mutation; work in every existing runtime
    # continues. The browser selects the result locally, so this route never
    # rewrites the process fallback or waits for another vault's workers.
    async with state.config_lock:
        try:
            await store_io(
                _initialize_managed_vault,
                state,
                target_path,
                name=name.strip(),
                packs=packs,
                embedding_provider=embedder.strip() if embedder is not None else None,
                backend=backend,
                storage_uri=storage_uri.strip() if storage_uri else None,
                storage_credential_env=(
                    storage_credential_env.strip() if storage_credential_env else None
                ),
                storage_database=storage_database.strip() if storage_database else None,
                allow_remote_db=allow_remote_db,
                embedding_spec=embedding_spec,
            )
            # Its graph handle opens lazily on the first scoped request rather
            # than retaining the init handle from a worker whose caller may have
            # been cancelled. Registration rehydrates sidecars, so it is a store op.
            await store_io(state.runtime_for, target_path, rehydrate=True)
        except FileExistsError:
            return _err(409, "vault_exists", f"vault already exists: {target_path}")
        except VaultPoolError as exc:
            return _err(409, exc.code, str(exc))
        except OktoNeuronError as exc:
            return _err(500, "vault_create_failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("vault create failed")
            return _err(500, "vault_create_failed", f"vault create failed: {exc}")

    created_payload = await store_io(_vault_created_payload, state, target_path)
    if embedding_spec:
        # Echo what the graph was created with: the width is fixed from here on.
        created_payload["embedding"] = dict(embedding_spec)
    return JSONResponse(created_payload)


def _parse_embedding_spec(raw: object, embedder: object = None) -> dict[str, Any] | None:
    """Validate the optional ``embedding`` object of a vault-create request (REST and MCP).

    ``None`` means "no spec": the vault is created exactly as before, at the application
    default width. A spec that names a provider different from the legacy ``embedder``
    string is refused rather than silently preferring one of them.
    """
    if raw is None:
        return None
    from okto_neuron.config._vault import validate_new_vault_embedding_spec

    try:
        spec = validate_new_vault_embedding_spec(raw)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from exc
    if (
        isinstance(embedder, str)
        and embedder.strip()
        and "provider" in spec
        and spec["provider"] != embedder.strip()
    ):
        raise _BadRequest(
            f"embedder {embedder.strip()!r} and embedding.provider {spec['provider']!r} disagree; "
            "send only embedding.provider"
        )
    return spec or None


def _vault_create_target(name: str) -> tuple[Path, bool]:
    """Store op: ensure the app layout, map ``name`` to its path, probe it."""
    ensure_global_layout()
    target_path = vault_path_for_name(name)
    return target_path, is_vault(target_path)


def _vault_created_payload(state: ServerState, target_path: Path) -> dict[str, object]:
    payload = _vaults_payload(state)
    payload["created"] = next(
        (
            entry.to_json()
            for entry in _vault_entries(state)
            if entry.path == target_path.resolve(strict=False)
        ),
        None,
    )
    return payload


def _runtime_delete_busy(runtime: VaultRuntime) -> dict[str, int | bool]:
    """Work that must finish before a configured-root vault can be removed."""

    return {
        "ingest_queued": sum(
            1
            for item in runtime.ingest_queue
            if getattr(item, "status", None) in {"queued", "processing"}
        ),
        "curation_queued": sum(
            1
            for job in runtime.curation_jobs
            if getattr(job, "status", None) in {"queued", "running"}
        ),
        "ingest_worker": runtime.ingest_worker_active,
        "curation_worker": runtime.curation_worker_active,
        "maintenance": bool(runtime.maintenance_tasks),
    }


async def _run_blocking_to_completion(
    operation: Callable[[], Any],
    deferred_cancellation: asyncio.CancelledError | None = None,
) -> tuple[Any | None, Exception | None, asyncio.CancelledError | None]:
    """Wait for one worker operation even if its request task is cancelled.

    A store-executor worker cannot be stopped when the awaiting task is
    cancelled. Shield the worker and remember cancellation instead, so callers
    can finish state cleanup that depends on the worker's actual outcome before
    propagating cancellation. Repeated cancellation is handled by the same loop.
    """

    worker = asyncio.ensure_future(store_io(operation))
    while True:
        try:
            return await asyncio.shield(worker), None, deferred_cancellation
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                return None, exc, deferred_cancellation
            if deferred_cancellation is None:
                deferred_cancellation = exc
        except Exception as exc:  # noqa: BLE001 - return the worker outcome to the caller
            return None, exc, deferred_cancellation


def _release_writer_lease(path: Path) -> bool:
    lease = held_writer_lease(path)
    if lease is None:
        return False
    lease.release()
    return True


async def api_vault_delete(request: Request) -> JSONResponse:
    """Delete one idle configured-root vault after exact confirmation."""

    state = get_server_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "vault deletion is restricted to loopback callers")
    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    if set(payload) != {"confirm_name"}:
        return _err(
            400,
            "bad_request",
            "vault deletion requires only the exact confirm_name field",
        )
    confirm_name = payload.get("confirm_name")
    if not isinstance(confirm_name, str):
        return _err(400, "bad_request", "missing or invalid field: confirm_name")

    vault_id = request.path_params.get("vault_id")
    entry = next(
        (item for item in await store_io(_vault_entries, state) if item.id == vault_id), None
    )
    if entry is None:
        return _err(404, "vault_not_found", "vault is not registered")
    if confirm_name != entry.name:
        return _err(409, "confirmation_mismatch", "vault name confirmation did not match")
    if not entry.deletable:
        return _err(
            409,
            "vault_protected",
            entry.delete_reason or "this vault cannot be deleted by Okto Neuron",
        )

    identity, guard_error = await store_io(managed_vault_delete_guard, entry.path)
    if identity is None or identity.id != entry.id:
        return _err(
            409,
            "vault_protected",
            guard_error or "vault identity does not match the registry entry",
        )

    runtime = await store_io(state.runtime_for, entry.path, rehydrate=True)
    busy = _runtime_delete_busy(runtime)
    if any(bool(value) for value in busy.values()):
        return _err(409, "vault_busy", "vault has queued or running work", busy=busy)

    pool = state.vault_pool
    # Workers observe the drain immediately (no await since the busy check);
    # the pool fence, which takes the pool lock, then rejects new requests.
    # Anything that leased in between is covered by the lease wait below.
    runtime.mark_draining()
    await store_io(pool.fence, entry.path)
    active_fallback = state.vault_path == entry.path
    released = False
    default_cleared = False
    filesystem_deleted = False
    lease_released = False
    deferred_cancellation: asyncio.CancelledError | None = None
    failure_response: JSONResponse | None = None
    try:
        # Let short read-only requests complete under the fence, while long or
        # newly-busy work fails safely and leaves the vault intact.
        deadline = time.monotonic() + _VAULT_DELETE_LEASE_WAIT_S
        while pool.lease_count(entry.path) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        busy = _runtime_delete_busy(runtime)
        leases = pool.lease_count(entry.path)
        if any(bool(value) for value in busy.values()) or leases:
            return _err(
                409,
                "vault_busy",
                "vault still has active work or requests",
                busy=busy,
                leases=leases,
            )

        async with runtime.writer_lock, runtime.config_lock, state.config_lock:
            busy = _runtime_delete_busy(runtime)
            leases = pool.lease_count(entry.path)
            if any(bool(value) for value in busy.values()) or leases:
                return _err(
                    409,
                    "vault_busy",
                    "vault became busy during deletion",
                    busy=busy,
                    leases=leases,
                )

            # Configuration is the only reversible state outside the vault
            # directory. Clear it before the filesystem commit point; the
            # ``finally`` block restores it if release/guard/rmtree fails.
            default_cleared = await store_io(clear_default_vault, entry.path)
            await store_io(pool.release_path, entry.path, require_fenced=True)
            released = True
            identity_now, guard_error = await store_io(managed_vault_delete_guard, entry.path)
            if identity_now is None or identity_now.id != entry.id:
                raise RuntimeError(
                    guard_error or "vault identity changed immediately before deletion"
                )
            # The store is closed; free the writer lease before the rmtree.
            lease_released = _release_writer_lease(entry.path)
            _, deletion_error, deferred_cancellation = await _run_blocking_to_completion(
                lambda: state.run_application_mutation(lambda: shutil.rmtree(entry.path)),
                deferred_cancellation,
            )
            if deletion_error is not None:
                raise deletion_error
            filesystem_deleted = True

        if active_fallback:
            state.vault = None
            state.vault_path = None
            state.vault_open_error = None
        runtime.draining = False
        try:
            await store_io(state.drop_runtime, entry.path)
        except Exception:  # noqa: BLE001 - deletion already committed
            # Do not report an ambiguous failure after the irreversible rmtree.
            # The fenced handle is gone; a stale in-memory runtime can only be
            # observed until restart and is safer than claiming the delete failed.
            _LOG.exception("could not drop deleted vault runtime for %s", entry.path)
    except VaultPoolError as exc:
        failure_response = _err(409, exc.code, str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("managed vault deletion failed for %s", entry.path)
        failure_response = _internal_error()
    finally:
        rollback_usable = False
        reopened = None
        if not filesystem_deleted:
            try:
                identity_after, rollback_error = await store_io(
                    managed_vault_delete_guard, entry.path
                )
            except Exception as exc:  # noqa: BLE001 - failed validation must quarantine
                identity_after = None
                rollback_error = f"rollback validation failed: {exc}"
            intact = identity_after is not None and identity_after.id == entry.id
            if intact and lease_released:
                try:
                    await store_io(acquire_daemon_writer_lease, entry.path)
                except Exception:  # noqa: BLE001
                    intact = False
                    rollback_error = "could not re-acquire the writer lease after failed deletion"
                    _LOG.exception("could not re-acquire writer lease for %s", entry.path)
            if intact and not released:
                rollback_usable = True
            elif intact:
                reopened, reopen_error, deferred_cancellation = await _run_blocking_to_completion(
                    lambda: _open_and_install_fenced_sync(runtime),
                    deferred_cancellation,
                )
                if reopen_error is None:
                    rollback_usable = True
                else:
                    _LOG.error(
                        "could not reopen vault after failed deletion for %s: %s",
                        entry.path,
                        reopen_error,
                    )
            elif rollback_error:
                _LOG.error(
                    "managed vault deletion left an unusable path fenced at %s: %s",
                    entry.path,
                    rollback_error,
                )

            if rollback_usable and default_cleared:
                try:
                    await store_io(set_default_vault, entry.path)
                except Exception:  # noqa: BLE001
                    rollback_usable = False
                    _LOG.exception("could not restore default after failed vault deletion")

            if rollback_usable:
                runtime.draining = False
            else:
                # A failed rmtree may leave an existing but incomplete directory.
                # Never advertise that path through the default, compatibility
                # fallback, runtime registry, or pool after rollback validation
                # or reopen failed. The retained fence makes explicit path use
                # fail closed for the rest of this process.
                if pool.peek(entry.path) is not None:
                    try:
                        await store_io(pool.release_path, entry.path, require_fenced=True)
                    except Exception:  # noqa: BLE001
                        _LOG.exception("could not release unusable vault handle for %s", entry.path)
                if active_fallback:
                    state.vault = None
                    state.vault_path = None
                    state.vault_open_error = None
                runtime.draining = False
                try:
                    await store_io(state.drop_runtime, entry.path)
                except Exception:  # noqa: BLE001
                    _LOG.exception("could not drop unusable vault runtime for %s", entry.path)
                finally:
                    runtime.mark_draining()

        if (filesystem_deleted or rollback_usable) and pool.is_fenced(entry.path):
            await store_io(pool.unfence, entry.path)

    if deferred_cancellation is not None:
        raise deferred_cancellation
    if failure_response is not None:
        return failure_response

    response = await store_io(_vaults_payload, state)
    response["deleted"] = {
        "id": entry.id,
        "name": entry.name,
        "path": str(entry.path),
    }
    return JSONResponse(response)


async def api_vault_current(request: Request) -> JSONResponse:
    state = get_state()
    return JSONResponse(await store_io(_vault_current_payload, state))


def _vault_current_payload(state: ServerState) -> dict[str, object]:
    current = next((entry for entry in _vault_entries(state) if entry.current), None)
    return {"status": "ok", "current": current.to_json() if current else None}


def _reembed_status_payload(
    state: ServerState | VaultRuntime, target_path: Path
) -> dict[str, object]:
    state_path = target_path / ".marginalia" / _REEMBED_STATE_FILE
    running = state.vault_reembed_active and state.vault_reembed_path == str(
        target_path.resolve(strict=False)
    )
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "status": "ok",
            "vault": str(target_path),
            "phase": "idle",
            "running": running,
        }
    except Exception:  # noqa: BLE001
        return {
            "status": "ok",
            "vault": str(target_path),
            "phase": "unknown",
            "running": running,
        }
    if not isinstance(data, dict):
        return {
            "status": "ok",
            "vault": str(target_path),
            "phase": "unknown",
            "running": running,
        }
    return {"status": "ok", "vault": str(target_path), "running": running, **data}


def _resolve_repair_vault(raw: str) -> Path:
    target_path = resolve_vault_reference(raw)
    if not is_vault(target_path):
        raise FileNotFoundError(str(target_path))
    return target_path.resolve(strict=False)


def _start_owned_maintenance(
    state: ServerState | VaultRuntime,
    worker: Callable[[], Awaitable[None]],
    *,
    name: str,
) -> asyncio.Task:
    """Retain one async maintenance coordinator for bounded shutdown."""
    task = asyncio.create_task(worker(), name=name)
    state.maintenance_tasks.add(task)
    task.add_done_callback(state.maintenance_tasks.discard)
    return task


async def _wait_for_runtime_leases(runtime: VaultRuntime, *, timeout: float) -> None:
    """Wait for pre-fence borrowers; a fence guarantees the count only falls."""
    deadline = time.monotonic() + timeout
    count = runtime.vault_pool.lease_count(runtime.vault_path)
    while count and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        count = runtime.vault_pool.lease_count(runtime.vault_path)
    if count:
        raise VaultPoolError(
            "vault_in_use",
            f"vault still has {count} active lease(s): {runtime.vault_path}",
        )


def _open_and_install_fenced_sync(runtime: VaultRuntime) -> Vault:
    """Claim ownership, open, and publish without an unowned-handle window."""
    ownership = runtime.vault_pool.claim_fenced_ownership(runtime.vault_path)
    try:
        reopened = Vault.open(runtime.vault_path)
        try:
            installed = runtime.install_fenced_vault(
                reopened,
                ownership=ownership,
            )
            ownership = None
            return installed
        except Exception:
            # There is no other handle for this fenced path after release, so
            # closing this rejected replacement cannot invalidate a pool owner.
            reopened.close()
            raise
    finally:
        if ownership is not None:
            ownership.release()


async def _open_and_install_fenced(runtime: VaultRuntime) -> Vault:
    """Async wrapper whose worker owns the full claim/open/install transaction."""
    return await store_io(_open_and_install_fenced_sync, runtime)


def _set_runtime_open_warning(runtime: VaultRuntime, warning: dict[str, object] | None) -> None:
    """Store a repair result on its runtime and the compatible manager warning."""
    runtime.vault_open_error = warning
    server = runtime.server
    current = server.vault_open_error
    current_path = current.get("path") if isinstance(current, dict) else None
    if (
        server.vault_path is None
        or server.vault_path == runtime.vault_path
        or current_path == str(runtime.vault_path)
    ):
        server.vault_open_error = warning


def _kg_reembed(vault_path: Path) -> None:
    # Imported here, on the worker: the CLI module is heavy to import on the loop.
    from okto_neuron.cli.kg import kg_reembed

    kg_reembed(vault_path)


async def _run_runtime_reembed(runtime: VaultRuntime) -> None:
    """Reembed exactly one fenced runtime, replacing its pooled handle safely."""
    pool = runtime.vault_pool
    released = False
    try:
        await _wait_for_runtime_leases(runtime, timeout=_VAULT_MAINTENANCE_LEASE_WAIT_S)
        async with runtime.writer_lock, runtime.config_lock:
            # The fence makes this a stable zero: no graph user can enter between
            # release and the replacement install.
            if pool.lease_count(runtime.vault_path):
                raise VaultPoolError(
                    "vault_in_use",
                    f"vault became leased during maintenance: {runtime.vault_path}",
                )
            await store_io(pool.release_path, runtime.vault_path, require_fenced=True)
            released = True

            # Embedding-bound (minutes): job executor, never a store worker.
            await job_io(_kg_reembed, runtime.vault_path)
            await _open_and_install_fenced(runtime)
            released = False
        _set_runtime_open_warning(runtime, None)
    except EmbeddingDimMismatch as exc:
        warning = _vault_open_warning(runtime.vault_path, exc)
        _set_runtime_open_warning(runtime, warning)
        _LOG.exception("vault repair reembed left embedding dimension mismatch")
    except Exception:  # noqa: BLE001
        _LOG.exception("vault reembed failed for %s", runtime.vault_path)
    finally:
        # The graph swap is fail-safe, so a failed worker normally leaves the
        # original graph available. Restore a pool owner before lifting the
        # fence. If open itself fails, the next request receives a clean open
        # error instead of borrowing a known-closed handle.
        if (
            released
            and await store_io(runtime.vault_path.exists)
            and pool.peek(runtime.vault_path) is None
        ):
            try:
                async with runtime.writer_lock, runtime.config_lock:
                    await _open_and_install_fenced(runtime)
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "could not restore vault handle after failed reembed for %s",
                    runtime.vault_path,
                )
        runtime.vault_reembed_active = False
        runtime.draining = False
        await store_io(pool.unfence, runtime.vault_path)


async def api_vault_reembed(request: Request) -> JSONResponse:
    """Start a vectors-only reembed for a selected vault from the vault manager.

    A configured startup fallback may fail to open after an embedding
    model/dimension change; the vault manager still needs a loopback-only button
    to run the explicit heavy reembed without changing application selection.
    """
    state = get_server_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "vault reembed is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        raw = _require(payload, "vault", str)
        target_path = await store_io(_resolve_repair_vault, raw)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    except FileNotFoundError as exc:
        return _err(404, "vault_not_found", f"vault not found: {exc}")
    except AmbiguousVaultNameError as exc:
        return _err(409, "ambiguous_vault", str(exc))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))

    runtime = await store_io(state.runtime_for, target_path, rehydrate=True)
    if runtime.draining or runtime.vault_reembed_active:
        return _err(409, "busy", "this vault already has maintenance in progress")
    busy = _runtime_delete_busy(runtime)
    if any(bool(value) for value in busy.values()):
        return _err(409, "busy", "this vault has active work", busy=busy)
    if state.vault_pool.is_fenced(target_path):
        return _err(409, "vault_fenced", "this vault is already fenced for maintenance")

    # Claim and drain with no await since the checks above, so a second request
    # or background worker cannot enter the target; the pool fence (which takes
    # the pool lock) follows off-loop before 202 is returned.
    runtime.mark_draining()
    runtime.vault_reembed_active = True
    runtime.vault_reembed_path = str(target_path)
    await store_io(state.vault_pool.fence, target_path)
    try:
        _start_owned_maintenance(
            runtime,
            lambda: _run_runtime_reembed(runtime),
            name="okto-neuron-vault-reembed",
        )
    except Exception:  # noqa: BLE001
        runtime.vault_reembed_active = False
        runtime.draining = False
        await store_io(state.vault_pool.unfence, target_path)
        raise
    return JSONResponse(
        {
            "status": "started",
            "vault": str(target_path),
            "reembed": await store_io(_reembed_status_payload, runtime, target_path),
        },
        status_code=202,
    )


async def api_vault_reembed_status(request: Request) -> JSONResponse:
    state = get_server_state()
    raw = request.query_params.get("vault")
    if not raw:
        return _err(400, "bad_request", "missing vault")
    try:
        return JSONResponse(await store_io(_vault_reembed_status_op, state, raw))
    except FileNotFoundError as exc:
        return _err(404, "vault_not_found", f"vault not found: {exc}")
    except AmbiguousVaultNameError as exc:
        return _err(409, "ambiguous_vault", str(exc))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))


def _vault_reembed_status_op(state: ServerState, raw: str) -> dict[str, object]:
    target_path = _resolve_repair_vault(raw)
    runtime = state.runtime_for(target_path, rehydrate=True)
    return _reembed_status_payload(runtime, target_path)


def _maintenance_blocker(state: ServerState) -> dict[str, object] | None:
    """Report work that blocks destructive maintenance on this runtime.

    Returns ``None`` when nothing blocks. Otherwise a structured payload so the UI
    can surface *what* is running and *how far along* it is instead of an opaque
    "wait" string. Always carries a backwards-compatible ``reason`` string. The
    priority order (shutdown > maintenance > reembed > ingest > curation) and the reported
    ``kind``/``progress`` correspond to the single condition being blocked on.
    """
    if state.shutting_down:
        return {
            "kind": "shutdown",
            "reason": "server is shutting down",
            "progress": "shutting down",
        }
    if state.maintenance_draining:
        active_job = next(
            (job for job in reversed(state.curation_jobs) if job.status in {"queued", "running"}),
            None,
        )
        progress = getattr(active_job, "progress", None) or "maintenance in progress"
        return {
            "kind": "maintenance",
            "reason": "vault maintenance is in progress; writes are paused",
            "progress": progress,
        }
    if state.vault_reembed_active:
        return {
            "kind": "reembed",
            "reason": ("vault re-embed is active; wait for repair to finish before maintenance"),
            "progress": "re-embedding vectors",
            "vault": state.vault_reembed_path,
        }
    ingest_active = [item for item in state.ingest_queue if item.status in {"queued", "processing"}]
    if state.ingest_worker_active or ingest_active:
        processing = next(
            (item for item in state.ingest_queue if item.status == "processing"),
            None,
        )
        queued = sum(1 for item in state.ingest_queue if item.status == "queued")
        active_name = getattr(processing, "name", None) if processing is not None else None
        progress = f"ingesting {active_name}" if active_name else "ingest in progress"
        return {
            "kind": "ingest",
            "reason": ("ingest is active; wait for the ingest queue to finish before maintenance"),
            "progress": progress,
            "active": active_name,
            "queued": queued,
            "total": len(state.ingest_queue),
        }
    running = next((job for job in state.curation_jobs if job.status == "running"), None)
    queued_jobs = [job for job in state.curation_jobs if job.status == "queued"]
    if state.curation_worker_active or running is not None or queued_jobs:
        job_kind = getattr(running, "kind", None) if running is not None else None
        job_progress = getattr(running, "progress", "") if running is not None else ""
        if job_kind:
            progress = f"{job_kind}: {job_progress}" if job_progress else job_kind
        else:
            progress = "curation in progress"
        return {
            "kind": "curation",
            "reason": ("curation is active; wait for curation jobs to finish before maintenance"),
            "progress": progress,
            "job_kind": job_kind,
            "running": 1 if running is not None else 0,
            "queued": len(queued_jobs),
        }
    return None


async def api_vault_switch(request: Request) -> JSONResponse:
    state = get_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "vault switching is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        raw = _require(payload, "vault", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    try:
        target_path, target_is_vault = await store_io(_resolve_switch_target, raw)
    except AmbiguousVaultNameError as exc:
        return _err(409, "ambiguous_vault", str(exc))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))

    if not target_is_vault:
        return _err(
            404,
            "vault_not_found",
            f"vault is missing okto-neuron.yaml: {target_path}",
        )

    # Deprecated compatibility path: update only the unscoped CLI fallback.
    # Browser tabs never call it, and immutable runtimes mean work in either
    # vault can continue without queue resets or cross-vault retargeting.
    async with state.config_lock:
        if state.vault_path is not None and target_path == state.vault_path:
            try:
                await store_io(set_default_vault, target_path)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("vault default update failed")
                return _err(
                    500,
                    "vault_switch_failed",
                    f"vault default update failed: {exc}",
                )
            return JSONResponse(await store_io(_vaults_payload, state))

        lease = None
        try:
            runtime = await store_io(state.runtime_for, target_path, rehydrate=True)
            lease = await acquire_off_loop(runtime.lease_vault)
            new_vault = lease.vault
            await store_io(set_default_vault, target_path)
            await store_io(state.switch_vault, new_vault, target_path)
            state.vault_open_error = None
        except EmbeddingDimMismatch as exc:
            state.vault_open_error = _vault_open_warning(target_path, exc)
            return JSONResponse(await store_io(_vaults_payload, state))
        except OktoNeuronError as exc:
            return _err(500, "vault_switch_failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("vault switch failed")
            return _err(500, "vault_switch_failed", f"vault switch failed: {exc}")
        finally:
            if lease is not None:
                lease.release()

        if any(item.status == "queued" for item in runtime.ingest_queue):
            iq.ensure_worker(runtime, _companion)
        if any(job.status == "queued" for job in runtime.curation_jobs):
            _jobs.ensure_worker(runtime)

    return JSONResponse(await store_io(_vaults_payload, state))


def _resolve_switch_target(raw: str) -> tuple[Path, bool]:
    target_path = resolve_vault_reference(raw)
    return target_path, is_vault(target_path)


def _safe_add_target(sources: Path, client_path: str) -> Path:
    """Directory-aware, collision-safe target under ``.marginalia/sources/``
    for the client-posted ``path`` string in ``POST /add``.

    ``client_path`` is untrusted (it comes straight off the request body, not
    a real path the server ever reads from), so this never trusts it beyond
    picking a destination filename: the file is always written under
    ``sources`` from the already-posted ``content``, never read from
    ``client_path`` itself.

    A well-formed *relative* client path with no ``..`` traversal keeps its
    full subpath under ``sources/`` (``notes/a/README.md`` and
    ``notes/b/README.md`` land at distinct targets, so two same-basename
    files from different directories no longer collapse onto one file and
    one document id). An absolute path, a drive-letter path, a path with any
    ``..`` segment, or one that resolves outside ``sources`` after joining
    instead falls back to a deterministic subdirectory keyed by a hash of the
    full ``client_path`` string plus the bare filename — still collision-free
    across distinct client paths, and idempotent (the same outside/absolute
    path re-added later reuses the same target) rather than content-derived
    (re-adding an *edited* file at the same path must keep updating the same
    document, not mint a new one — see ADR 0023 on Block/Claim identity).
    """
    sources_resolved = sources.resolve(strict=False)
    name = Path(client_path).name or "source.md"
    posix = PurePosixPath(client_path.replace("\\", "/"))
    parts = [p for p in posix.parts if p not in ("", ".", "..") and not p.endswith(":")]
    is_relative_and_clean = (
        parts
        and not posix.is_absolute()
        and ".." not in Path(client_path).parts
        and not re.match(r"^[A-Za-z]:", client_path)
    )
    if is_relative_and_clean:
        candidate = sources.joinpath(*parts)
        candidate_resolved = candidate.resolve(strict=False)
        try:
            candidate_resolved.relative_to(sources_resolved)
        except ValueError:
            is_relative_and_clean = False
        else:
            return candidate
    digest = hashlib.sha256(client_path.encode("utf-8")).hexdigest()[:16]
    return sources / digest / name


async def add(request: Request) -> JSONResponse:
    state = get_state()
    if state.draining:
        return _draining_response()
    # M1: /add is a WRITE (materializes content into the vault). Writes NEVER
    # widen under --allow-remote — loopback-only even when bound for remote reads.
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "add is restricted to loopback callers; tunnel (e.g. SSH) to write remotely",
        )
    try:
        payload = await _read_json(request)
        path = _require(payload, "path", str)
        content = _require(payload, "content", str)
        # metadata + mime_type are optional and currently informational
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    async with state.writer_lock:
        if state.draining:
            return _draining_response()
        try:
            doc = await store_io(_add_materialized, state, path, content)
        except IntegrityFenceError as exc:
            return _integrity_fenced_response(exc)
        except IngestError as exc:
            return _graph_write_failed(exc)
        except VaultClosedError as exc:
            return _err(503, "vault_closed", str(exc))
        except OktoNeuronError as exc:
            return _graph_write_failed(exc)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("unexpected ingest failure")
            return _err(500, "internal", f"unexpected server error: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "document_id": doc.id,
            "chunks_ingested": 1,
            "embedding_model": EMBEDDING_MODEL,
        }
    )


def _add_materialized(state: ServerState | VaultRuntime, path: str, content: str) -> Any:
    """Store op for ``POST /add``; the caller holds the vault's writer lock."""
    graph_integrity.require_write_allowed(state, state.vault)
    # Vault.add takes a path; materialize the posted content to a DURABLE
    # location under the vault and ingest from there. The bytes MUST
    # persist: a Block's provenance stores this path as source_path, and
    # byte-range provenance is re-derived by re-reading + re-hashing the
    # source on disk ("markdown/vault is canonical"). Unlinking it would
    # leave that source_path dangling. We use .marginalia/sources/ — a
    # dedicated durable dir, distinct from the runner's .marginalia/
    # incoming inbox (which the runner drains and MOVES out), so an /add
    # source is never re-ingested or relocated out from under its prov.
    # The target is directory-aware (``_safe_add_target``): two posted
    # paths sharing a basename in different directories (e.g.
    # ``notes/a/README.md`` vs ``notes/b/README.md``) land at distinct
    # files instead of collapsing onto one, which used to silently
    # overwrite the first file's bytes and mint identical document ids
    # for genuinely different documents.
    sources = state.vault_path / ".marginalia" / "sources"
    target = _safe_add_target(sources, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    doc = state.vault.add(target)
    # Record the deterministic store in the durable ingest queue so
    # server-initiated /add is visible in /api/v1/ingest-queue and the UI.
    # Same semantics (no LLM); stage="stored" distinguishes it from a full
    # remember() extraction.
    iq.record_completed(
        state,
        name=Path(path).name,
        path=str(target),
        committed=1,
        stage="stored",
    )
    return doc


def _query_with_recall_cost(vault: Any, text: str, *, k: int) -> tuple[list[Any], dict[str, Any]]:
    """Run ordinary recall and report its measured cost when the vault supports it.

    The fallback keeps lightweight test/extension vaults compatible without claiming
    that completion or embedding work was measured when it was not.
    """
    measured_query = getattr(vault, "query_with_metrics", None)
    if callable(measured_query):
        return measured_query(text, k=k)

    started = time.perf_counter()
    hits = vault.query(text, k=k)
    return hits, {
        "schema_version": "recall_cost.v1",
        "measurement_status": "not_measured",
        "completion_calls": None,
        "generated_tokens": None,
        "query_embedding_calls": None,
        "total_latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "retrieved_results": len(hits),
        "completion_free": None,
    }


async def query(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        text = _require(payload, "query", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    k = payload.get("k", 10)
    if not isinstance(k, int) or k < 1:
        return _err(400, "bad_request", "missing or invalid field: k")
    k_cap_err = _k_cap_error(k)
    if k_cap_err is not None:
        return k_cap_err

    try:
        # Store read: vector search plus one short query embedding.
        hits, metrics = await store_io(_query_with_recall_cost, state.vault, text, k=k)
    except QueryError as exc:
        return _err(500, "query_failed", str(exc))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "query_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected query failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    results = [_rich_hit(hit) for hit in hits]
    return JSONResponse({"status": "ok", "results": results, "recall_cost": metrics})


def _rich_hit(hit: Any) -> dict[str, Any]:
    """Superset hit shape: back-compat keys (``document_id``, ``score``,
    ``snippet``) PLUS the rich provenance keys the CLI thin-client echoes
    (``claim_id`` + byte ranges + ``content_hash``). Byte/hash fields are read
    defensively so a non-Claim hit with empty provenance never raises."""
    node = hit.node
    title = node.title or ""
    return {
        "document_id": node.id,  # back-compat
        "claim_id": getattr(hit, "claim_id", None),
        "path": _safe(hit, "path", ""),
        "score": float(hit.score),
        "byte_start": _safe(hit, "byte_start", 0),
        "byte_end": _safe(hit, "byte_end", 0),
        "content_hash": _safe(hit, "content_hash", ""),
        "title": title,
        "snippet": title,  # back-compat for CLI text render
    }


def _safe(obj: Any, attr: str, default: Any) -> Any:
    try:
        value = getattr(obj, attr)
    except AttributeError:
        return default
    return default if value is None else value


async def detect_drift(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        corpus_root = _require(payload, "corpus_root", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    dry_run = bool(payload.get("dry_run", False))

    root = Path(corpus_root)
    if not root.is_absolute():
        return _err(400, "bad_request", "corpus_root missing or not absolute")

    # No writer_lock: every registered detector (see detectors.py) only reads
    # ``vault.store`` and returns Finding values — ``dry_run`` is echoed back but
    # never gates a write, and nothing here calls add_node/add_edge/upsert/etc.
    # This is a pure query, same footing as /query and /api/v1/nodes — holding
    # writer_lock here just meant drift detection hung behind an in-flight
    # ingest/rebuild/heal/reembed for no reason (the module docstring in
    # state.py is explicit: "Read handlers MUST NOT acquire it").
    try:
        body = await store_io(
            encode_op, _detect_drift_payload, state, root, dry_run, payload.get("mode", "on-query")
        )
    except _ApiError as exc:
        return exc.response()
    except OktoNeuronError as exc:
        return _err(500, "drift_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected drift failure")
        return _err(500, "internal", f"unexpected server error: {exc}")
    return json_bytes_response(body)


def _detect_drift_payload(
    state: ServerState | VaultRuntime, root: Path, dry_run: bool, mode: Any
) -> dict[str, Any]:
    """Store op: run every drift detector and resolve each finding's subject."""
    if not root.exists():
        raise _ApiError(404, "not_found", "corpus_root does not exist")
    by_detector = {name: run_detector(name, state.vault) for name in DETECTOR_NAMES}

    findings_json: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    for name, findings in by_detector.items():
        for f in findings:
            findings_json.append(_finding_payload(state, f))
            actions.append({"op": "update", "path": name, "finding": f.id})
    counts = {name: len(findings) for name, findings in by_detector.items()}
    total = len(findings_json)

    return {
        "status": "ok",
        "schema_version": "drift.v1",
        "vault_name": state.vault.root.name,
        "vault_root": str(state.vault.root.resolve()),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "findings": findings_json,
        "counts": counts,
        "total": total,
        # back-compat keys (pre-drift.v1 clients):
        "added": 0,
        "removed": 0,
        "changed": total,
        "actions": actions,
        "dry_run": dry_run,
    }


def _finding_payload(state: ServerState, finding: Any) -> dict[str, Any]:
    """Mirror of ``cli._finding_json`` / ``_finding_subject`` (server can't import
    the CLI without pulling in its full command surface)."""
    return {
        "finding_id": finding.id,
        "detector": finding.kind,
        "severity": finding.severity.value,
        "subject": _finding_subject(state, finding),
        "message": finding.message,
        "evidence_claim_ids": finding.evidence_claim_ids,
    }


def _finding_subject(state: ServerState, finding: Any) -> dict[str, Any]:
    evidence_id = finding.evidence_claim_ids[0]
    node = _store(state).get_node(evidence_id, include_embedding=False)
    facets = dict(getattr(node, "facets", {}) or {}) if node else {}
    node_type = str(getattr(node, "type", "")) if node else ""
    if node_type == "Block":
        return {
            "doc_path": facets.get("source_path") or facets.get("path"),
            "block_id": node.id,
        }
    if node_type == "Document":
        return {
            "doc_path": facets.get("path") or facets.get("uri"),
            "block_id": None,
        }
    return {"doc_path": None, "block_id": evidence_id}


# --------------------------- companion surface (Phase F) ---------------------------


def companion_for(vault):
    """Build a Companion bound to a specific vault handle.

    MCP and REST both resolve one immutable request runtime before building the
    Companion. One construction path keeps their behavior aligned."""
    from okto_neuron.companion import Companion

    return Companion(vault)


def _companion(state: ServerState):
    return companion_for(state.vault)


async def remember(request: Request) -> JSONResponse:
    state = get_state()
    if state.draining:
        return _draining_response()
    # M1: remember() is a WRITE. Writes NEVER widen under --allow-remote — even a
    # server bound for remote reads keeps the write surface loopback-only (an
    # operator must tunnel, e.g. SSH, to write remotely). Matches config-PATCH.
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "remember is restricted to loopback callers; tunnel (e.g. SSH) to write remotely",
        )
    # Lazy import (companion pulls the heavy consolidate/migrate stack); needed for
    # the out-of-vault source rejection below.
    from okto_neuron.companion import SourceOutsideVaultError

    try:
        payload = await _read_json(request)
        source = _require(payload, "source", str)
        sensitivity = _parse_sensitivity(payload)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    async with state.writer_lock:
        if state.draining:
            return _draining_response()
        try:
            await store_io(graph_integrity.require_write_allowed, state, state.vault)
            # Off-load the blocking LLM extraction so the event loop stays
            # responsive; writer_lock still serializes the write.
            result = await job_io(_companion(state).remember, source, sensitivity=sensitivity)
            # ADR 0009 P4: signal in-process ingest activity for the continuous
            # curation scheduler's debounce.
            now = time.time()
            state.last_ingest_at = now
            # The request middleware bound ``state`` to one immutable runtime;
            # key the scheduler signal to that exact path, matching MCP remember.
            state.last_ingest_at_by_vault[str(state.vault_path)] = now
        except IntegrityFenceError as exc:
            return _integrity_fenced_response(exc)
        except SourceOutsideVaultError as exc:
            log_remember_failure(exc, source)
            return _err(403, "forbidden", str(exc))
        except IngestError as exc:
            # Logged by the remember rule (a missing or out-of-vault source is
            # the caller's mistake, not a failed graph write); the response
            # body is unchanged.
            log_remember_failure(exc, source)
            return _graph_write_failed(exc, logged=True)
        except VaultClosedError as exc:
            return _err(503, "vault_closed", str(exc))
        except OktoNeuronError as exc:
            # A store GraphBackendError or a backend's exhausted-retry error is
            # a failed graph write and is logged as one, as MCP remember does.
            log_remember_failure(exc, source)
            return _err(500, "remember_failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            log_remember_failure(exc, source)
            return _err(500, "internal", f"unexpected server error: {exc}")

    # Reads the vault config and persists the job sidecar: store op.
    remember_outcome = await store_io(
        _curation.attach_verified_reconciliation_outcome,
        state,
        dict(getattr(result, "outcome", {}) or {}),
        trigger="verified_file_commit",
    )
    return JSONResponse(
        {
            "status": "ok",
            "document_id": result.document_id,
            "committed": result.committed,
            "queued": result.queued,
            "blocks_total": result.blocks_total,
            "nodes_extracted": result.nodes_extracted,
            "edges_extracted": result.edges_extracted,
            "claims_minted": result.claims_minted,
            "provider_error": result.provider_error,
            "provider_failures": result.provider_failures,
            "empty_after_retry_blocks": result.empty_after_retry_blocks,
            "llm_disabled": result.llm_disabled,
            "outcomes": [o.model_dump(mode="json") for o in result.outcomes],
            "outcome": remember_outcome,
        }
    )


async def recall(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        text = _require(payload, "query", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    k = payload.get("k", 10)
    if not isinstance(k, int) or k < 1:
        return _err(400, "bad_request", "missing or invalid field: k")
    k_cap_err = _k_cap_error(k)
    if k_cap_err is not None:
        return k_cap_err

    try:
        # Store read: vector search plus one short query embedding.
        hits, metrics = await store_io(_query_with_recall_cost, state.vault, text, k=k)
    except QueryError as exc:
        return _err(500, "query_failed", str(exc))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "query_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected recall failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    results = [
        {
            "document_id": hit.node.id,
            "path": hit.path,
            "score": float(hit.score),
            "snippet": hit.node.title or "",
        }
        for hit in hits
    ]
    return JSONResponse({"status": "ok", "results": results, "recall_cost": metrics})


async def ask(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        question = _require(payload, "question", str)
        retrieval_policy = _parse_ask_retrieval_policy(payload)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    # ask is one-shot (no agentic re-query), so it seeds wider than recall (k=10).
    # k=20 validated as the answer-presence ceiling on the grounded golden set
    # (0.677 -> 0.754) without regressing neg-control abstention. See docs/understanding.
    k = payload.get("k", 20)
    if not isinstance(k, int) or k < 1:
        return _err(400, "bad_request", "missing or invalid field: k")
    if retrieval_policy and retrieval_policy.seed_k is not None:
        k = retrieval_policy.seed_k
    k_cap_err = _k_cap_error(k)
    if k_cap_err is not None:
        return k_cap_err

    try:
        # Off-load the blocking LLM answer synthesis so the event loop stays
        # responsive while retrieval + generation runs.
        answer = await job_io(
            _companion(state).ask,
            question,
            k=k,
            retrieval_policy=retrieval_policy,
        )
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "ask_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected ask failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    # An empty or unfinished answer is never reported as "ok": anything but a
    # clean synthesis is "degraded", and retrieval.synthesis_status says why.
    from okto_neuron.companion import ask_status

    retrieval = dict(answer.retrieval)
    return JSONResponse(
        {
            "status": ask_status(retrieval),
            "text": answer.text,
            "citations": list(answer.citations),
            "retrieval": retrieval,
        }
    )


REVIEW_QUEUE_MAX_LIMIT = 1000


async def review_queue(request: Request) -> JSONResponse:
    state = get_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "review queue is restricted to loopback callers")
    if state.shutting_down:
        return _draining_response()
    raw_limit = request.query_params.get("limit")
    cursor = request.query_params.get("cursor") or None
    limit: int | None = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            return _err(400, "bad_request", "limit must be a non-negative integer")
        if limit < 0:
            return _err(400, "bad_request", "limit must be a non-negative integer")
        limit = min(limit, REVIEW_QUEUE_MAX_LIMIT)
    elif cursor is not None:
        return _err(400, "bad_request", "cursor requires limit")
    try:
        body = await store_io(encode_op, _review_queue_body, state, limit, cursor)
    except ReviewQueueMigrationRequired as exc:
        return _err(409, "review_queue_migration_required", str(exc))
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "review_queue_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected review_queue failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    # A request without ``limit`` keeps the full-list behaviour for clients that
    # predate pagination; it is deprecated and will be removed.
    headers = {"Deprecation": "true"} if limit is None else None
    return json_bytes_response(body, headers=headers)


def _review_queue_body(
    state: ServerState | VaultRuntime, limit: int | None = None, cursor: str | None = None
) -> dict[str, Any]:
    companion = _companion(state)
    if limit is None:
        items = companion.review_queue_all()
        next_cursor = None
        total = len(items)
    else:
        items, next_cursor, total = companion.review_queue_page(limit, cursor)
    return {
        "status": "ok",
        "items": _review_queue_items_payload(state, items),
        "next_cursor": next_cursor,
        "total": total,
    }


def _review_queue_payload(state: ServerState | VaultRuntime) -> list[dict[str, Any]]:
    """The full list with source evidence (kept for callers of the un-paged shape)."""
    return _review_queue_body(state)["items"]


def _review_queue_items_payload(
    state: ServerState | VaultRuntime, items: list[Any]
) -> list[dict[str, Any]]:
    """Store op: attach source evidence to ``items``.

    Every evidence Block is fetched with ONE ``get_nodes`` batch per request
    instead of one ``get_node`` round trip per item; a page only fetches its own.
    """
    payloads = [_review_item_payload(state, item) for item in items]
    block_ids = [
        str(payload["source_evidence"]["block_id"])
        for payload in payloads
        if payload["source_evidence"]["block_id"]
    ]
    blocks = (
        {str(node.id): node for node in state.vault.store.get_nodes(block_ids)}
        if block_ids
        else {}
    )
    for payload in payloads:
        _attach_block_excerpt(payload["source_evidence"], blocks)
    return payloads


def _review_item_payload(state: ServerState, item: Any) -> dict[str, Any]:
    """Build one discriminated, read-only node/relation review projection."""

    kind = str(getattr(item, "kind", "node"))
    if kind == "node":
        payload = item.model_dump(mode="json")
        block_id = getattr(item, "block_id", None)
        source_path = getattr(item, "source_path", None)
        byte_start = getattr(item, "byte_start", None)
        byte_end = getattr(item, "byte_end", None)
        content_hash = getattr(item, "content_hash", None)
    elif kind == "relation":
        candidate = item.candidate
        proposal = item.pinned_proposal
        object_label = (
            candidate.dst_literal if candidate.dst_literal is not None else candidate.dst_ref
        )
        payload = {
            "kind": "relation",
            "candidate_id": item.candidate_id,
            "type": candidate.type,
            "title": f"{candidate.src_ref} {proposal.admitted_predicate} {object_label}",
            "confidence": candidate.confidence,
            "reason": item.reason,
            "candidate": candidate.model_dump(mode="json"),
            "pinned_proposal": proposal.to_json(),
        }
        block_id = candidate.block_id
        source_path = None
        byte_start = candidate.byte_start
        byte_end = candidate.byte_end
        content_hash = candidate.content_hash
    else:
        raise ValueError(f"unknown review item kind: {kind!r}")

    # The evidence excerpt is attached by ``_attach_block_excerpt`` from one
    # batched Block read for the whole queue.
    payload["source_evidence"] = {
        "source_path": source_path or None,
        "block_id": block_id,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "content_hash": content_hash,
        "excerpt": None,
        "excerpt_truncated": False,
    }
    return payload


def _attach_block_excerpt(evidence: dict[str, Any], blocks: dict[str, Any]) -> None:
    block_id = evidence["block_id"]
    if not block_id:
        return
    block = blocks.get(str(block_id))
    if block is None or block.type != "Block":
        return
    text = str(block.content or "").strip()
    evidence["excerpt"] = text[:500] if text else None
    evidence["excerpt_truncated"] = len(text) > 500
    evidence["source_path"] = evidence["source_path"] or (
        str((block.facets or {}).get("source_path") or "") or None
    )


def _resolve_review_op(state: ServerState | VaultRuntime, candidate_id: str, action: str) -> Any:
    """Store op: apply one review decision (graph write under the writer lock)."""
    return _companion(state).resolve_review(candidate_id, action)  # type: ignore[arg-type]


_REVIEW_ACTIONS = {"commit", "discard", "merge"}
_REVIEW_BATCH_ACTIONS = {"commit", "discard"}


async def resolve_review(request: Request) -> JSONResponse:
    state = get_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "resolve-review is restricted to loopback callers")
    if state.draining:
        return _draining_response()
    try:
        payload = await _read_json(request)
        candidate_id = _require(payload, "candidate_id", str)
        action = _require(payload, "action", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    if action not in _REVIEW_ACTIONS:
        return _err(400, "bad_request", f"action must be one of {sorted(_REVIEW_ACTIONS)}")

    from okto_neuron.companion import ReviewItemNotFoundError

    try:
        async with _writer_lock_fast(state):
            outcome = await store_io(_resolve_review_op, state, candidate_id, action)
    except IntegrityFenceError as exc:
        return _integrity_fenced_response(exc)
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    except ReviewItemNotFoundError as exc:
        return _err(404, "review_item_not_found", str(exc))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "resolve_review_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected resolve_review failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    return JSONResponse({"status": "ok", "outcome": outcome.model_dump(mode="json")})


async def resolve_review_batch(request: Request) -> JSONResponse:
    state = get_state()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "resolve-review is restricted to loopback callers")
    if state.draining:
        return _draining_response()
    try:
        payload = await _read_json(request)
        candidate_ids = payload.get("candidate_ids")
        action = _require(payload, "action", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    if action not in _REVIEW_BATCH_ACTIONS:
        return _err(
            400,
            "bad_request",
            f"action must be one of {sorted(_REVIEW_BATCH_ACTIONS)}",
        )
    if not isinstance(candidate_ids, list) or not all(
        isinstance(candidate_id, str) and candidate_id for candidate_id in candidate_ids
    ):
        return _err(
            400,
            "bad_request",
            "candidate_ids must be a list of non-empty strings",
        )

    from okto_neuron.companion import ReviewItemNotFoundError

    def _run_batch() -> dict[str, Any]:
        companion = _companion(state)
        resolved = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        for candidate_id in candidate_ids:
            try:
                companion.resolve_review(candidate_id, action)  # type: ignore[arg-type]
                resolved += 1
            except ReviewItemNotFoundError:
                skipped += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": candidate_id, "error": str(exc)})
        return {
            "status": "ok",
            "resolved": resolved,
            "skipped": skipped,
            "errors": errors,
        }

    try:
        async with state.writer_lock:
            await store_io(graph_integrity.require_write_allowed, state, state.vault)
            result = await store_io(_run_batch)
    except IntegrityFenceError as exc:
        return _integrity_fenced_response(exc)
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "resolve_review_batch_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("unexpected resolve_review_batch failure")
        return _err(500, "internal", f"unexpected server error: {exc}")

    return JSONResponse(result)


# --------------------------- /api/v1 — KG browser (read-only) ---------------------------


def _store(state: ServerState) -> Any:
    """Return the vault's GraphStore (typed loosely as ``object`` on Vault)."""
    return state.vault.store


async def _graph_read(what: str, op: Callable[..., Any], *args: Any) -> Any:
    """Run one KG-browser store op off the loop; map its failures to a response.

    Returns the finished response: the op's payload JSON-encoded ON the worker
    (a big payload encoded on the loop stalls /health, REST and MCP), or an
    error response. A typed store/backend failure (e.g. GraphBackendError from
    a driver limit) carries a real cause; it is reported instead of the bare
    catch-all's "internal server error", which hid Grafx's 1024-element query
    cap.
    """
    try:
        return json_bytes_response(await store_io(encode_op, op, *args))
    except _ApiError as exc:
        return exc.response()
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "graph_read_failed", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected %s failure", what)
        return _internal_error()


async def _graph_read_shared(what: str, key: Any, op: Callable[..., Any], *args: Any) -> Any:
    """:func:`_graph_read` for an expensive full scan: concurrent identical
    requests share ONE execution (single-flight); the cached value is the
    encoded response body, not the object."""
    try:
        return json_bytes_response(await single_flight(key, encode_op, op, *args))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "graph_read_failed", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected %s failure", what)
        return _internal_error()


def _node_summary(node: Any) -> dict[str, Any]:
    facets = dict(getattr(node, "facets", {}) or {})
    name = getattr(node, "title", None) or facets.get("name") or None
    return {
        "id": str(getattr(node, "id", "")),
        "type": str(getattr(node, "type", "")),
        "name": name,
    }


def _matches_query(node: Any, q: str) -> bool:
    needle = q.casefold()
    haystacks = (
        str(getattr(node, "title", "") or ""),
        str(getattr(node, "content", "") or ""),
        str(getattr(node, "id", "") or ""),
    )
    return any(needle in h.casefold() for h in haystacks)


# Deterministic document-structure Claims: a markdown heading, tag or wikilink
# restated as a Claim so the section has a byte-anchored provenance target. Real,
# provenance-bearing graph nodes, but section labels rather than knowledge — they
# were a third of every Claim page in the KG browser. ADR 0031 keeps them out of
# recall/ask/subgraph; browse was never covered, so hide them here by default and
# let the UI ask for them back.
#
# Deliberately keyed on the Claim predicate, NOT on the ``_salience`` facet: that
# facet also marks relation endpoints the companion auto-promoted (companion
# Fix A), which are genuine entities ("Naturgy", "Itau VISA", a person's name).
# Hiding those would be far worse than the noise being removed.
_STRUCTURAL_CLAIM_PREDICATES: Final = _projection.STRUCTURAL_CLAIM_PREDICATES
_is_structural_claim = _projection.is_structural_claim


def _include_structural(request: Request) -> bool:
    """``?include_structural=1`` opts a browse/graph read back into the
    deterministic document-structure Claims hidden by default."""
    raw = (request.query_params.get("include_structural") or "").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


async def api_nodes_list(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()

    type_filter = request.query_params.get("type")
    if type_filter is not None and type_filter not in CLOSED_NODE_TYPES:
        return _err(
            400,
            "bad_request",
            f"unknown node type filter: {type_filter!r}; "
            f"must be one of the closed schema types {sorted(CLOSED_NODE_TYPES)}",
        )
    q = request.query_params.get("q") or ""
    try:
        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return _err(400, "bad_request", "limit and offset must be integers")
    if limit < 1 or limit > 500:
        return _err(400, "bad_request", "limit must be in [1, 500]")
    if offset < 0:
        return _err(400, "bad_request", "offset must be >= 0")
    include_structural = _include_structural(request)

    body = await _graph_read(
        "node-list",
        _nodes_list_payload,
        state,
        type_filter,
        q,
        limit,
        offset,
        include_structural,
    )
    return body


def _nodes_list_payload(
    state: ServerState | VaultRuntime,
    type_filter: str | None,
    q: str,
    limit: int,
    offset: int,
    include_structural: bool,
) -> dict[str, Any]:
    """Store op: filtered, equivalence-folded, paged node list."""
    all_nodes = [
        n
        for n in _store(state).list_nodes(type=type_filter)
        if str(getattr(n, "type", "")) in CLOSED_NODE_TYPES
        and not is_infra(n)
        and (include_structural or not _is_structural_claim(n))
    ]

    if q:
        all_nodes = [n for n in all_nodes if _matches_query(n, q)]

    # ADR 0009 P2: fold equivalence on READ so Browse dedupes like search. Off-graph
    # (variant nodes stay in the graph); a folded variant collapses onto its
    # canonical and the canonical carries a ``variant_count`` badge. The fold runs
    # over the WHOLE filtered set BEFORE paging so counts and pages are consistent.
    equivalence = _curation.equivalence_map(state)
    all_nodes, variant_counts = _curation.fold_node_list(all_nodes, equivalence)

    total = len(all_nodes)
    page = all_nodes[offset : offset + limit]

    def _summary_with_badge(n: Any) -> dict[str, Any]:
        row = _node_summary(n)
        vc = variant_counts.get(row["id"])
        if vc:
            row["variant_count"] = vc
        return row

    return {
        "status": "ok",
        "total": total,
        "limit": limit,
        "offset": offset,
        "nodes": [_summary_with_badge(n) for n in page],
    }


def _provenance_payload(state: ServerState, node: Any) -> dict[str, Any] | None:
    """Build the rich byte-range provenance dict for a Claim node (else None)."""
    if str(getattr(node, "type", "")) != "Claim":
        return None
    try:
        prov = state.vault._provenance_for_node(node)  # noqa: SLF001 — internal, intentional reuse
    except Exception:  # noqa: BLE001
        return None
    return prov.model_dump(mode="json")


async def api_node_detail(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    node_id = request.path_params["id"]
    body = await _graph_read("node-detail", _node_detail_payload, state, node_id)
    return body


def _node_detail_payload(state: ServerState | VaultRuntime, node_id: str) -> dict[str, Any]:
    """Store op: one node, its edges, provenance, and equivalence canonical."""
    node = _store(state).get_node(node_id, include_embedding=False)
    if node is None or str(getattr(node, "type", "")) not in CLOSED_NODE_TYPES or is_infra(node):
        raise _ApiError(404, "not_found", f"node not found: {node_id}")
    out_edges = [
        {"type": str(e.type), "dst": str(e.dst)} for e in _store(state).list_edges(src=node_id)
    ]
    in_edges = [
        {"type": str(e.type), "src": str(e.src)} for e in _store(state).list_edges(dst=node_id)
    ]
    provenance = _provenance_payload(state, node)
    block = None
    if provenance and provenance.get("block_id"):
        block_node = _store(state).get_node(provenance["block_id"], include_embedding=False)
        if block_node is not None:
            block = {
                "id": str(block_node.id),
                "facets": dict(getattr(block_node, "facets", {}) or {}),
            }

    node_payload = _node_summary(node)
    node_payload["facets"] = dict(getattr(node, "facets", {}) or {})
    # ADR 0009 P2: if this node is a FOLDED VARIANT, annotate its canonical so the
    # UI can redirect/badge. Off-graph: the variant still exists in the graph; we
    # only surface the equivalence. The canonical's own detail is unaffected.
    equivalence = _curation.equivalence_map(state)
    canonical_id = _curation.canonical_for(node_id, equivalence)
    return {
        "status": "ok",
        "node": node_payload,
        "edges": {"out": out_edges, "in": in_edges},
        "provenance": provenance,
        "block": block,
        "canonical_id": canonical_id,
    }


async def api_node_types(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    include_structural = _include_structural(request)
    body = await _graph_read("node-types", _node_types_payload, state, include_structural)
    return body


def _node_types_payload(
    state: ServerState | VaultRuntime, include_structural: bool
) -> dict[str, Any]:
    """Store op: per-type node census over the closed schema."""
    counts: dict[str, int] = {t: 0 for t in CLOSED_NODE_TYPES}
    for node in _store(state).list_nodes():
        if is_infra(node) or (not include_structural and _is_structural_claim(node)):
            continue
        t = str(getattr(node, "type", ""))
        if t in counts:
            counts[t] += 1
    types = [
        {
            "name": t,
            "kind": _TYPE_KIND[t],
            "count": counts[t],
            "minted_by_ingest": t not in _NEVER_MINTED_TYPES,
        }
        for t in (*_PRIMITIVE_TYPES, *_SUPPORT_TYPES)
    ]
    return {"status": "ok", "types": types}


# --------------------------- /api/v1 — graph visualization (read-only) ---------------------------

# Caps for the graph surface. Unlike /nodes (which 400s on limit>500), these
# CLAMP to the hard cap rather than rejecting — the frontend asks for "as much as
# it can render" and we silently bound the work, never error on an oversize ask.
GRAPH_DEFAULT_LIMIT = 1500
GRAPH_MAX_LIMIT = 5000
NEIGHBORS_DEFAULT_LIMIT = 500
NEIGHBORS_MAX_LIMIT = 2000
NEIGHBORS_DEFAULT_HOPS = 1
NEIGHBORS_MAX_HOPS = 3


def _graph_node_payload(node: Any, degree: int) -> dict[str, Any]:
    """Subgraph node row: the /nodes summary plus a ``degree`` for sizing.

    ``degree`` is computed by the caller over the relevant edge set (the full
    filtered subgraph for /graph, the returned set for /neighbors), so the same
    serializer serves both surfaces without baking in one degree semantics."""
    payload = _node_summary(node)
    payload["degree"] = degree
    return payload


def _graph_edge_payload(edge: Any) -> dict[str, Any]:
    return {"src": str(edge.src), "dst": str(edge.dst), "type": str(edge.type)}


def _csv_param(raw: str | None) -> set[str] | None:
    """Parse a comma-separated query param into a set, or None when absent/empty.

    None means "no filter" (include all); a non-empty set restricts."""
    if raw is None:
        return None
    tokens = {t.strip() for t in raw.split(",") if t.strip()}
    return tokens or None


def _visible_node(node: Any, *, include_structural: bool = False) -> bool:
    """Same visibility rule as the KG browser: closed-schema types only, no infra
    (SchemaMetadata and friends never leak into the graph), and — unless the
    caller opts in — no deterministic document-structure Claims."""
    if str(getattr(node, "type", "")) not in CLOSED_NODE_TYPES or is_infra(node):
        return False
    return include_structural or not _is_structural_claim(node)


def _validate_type_filter(types: set[str] | None) -> JSONResponse | None:
    """400 if any requested node type is outside the closed schema (mirrors
    ``api_nodes_list``). Edge ``relations`` are an open, pack-defined vocabulary
    and are NOT validated here — they are filtered as-is."""
    if types is None:
        return None
    unknown = sorted(t for t in types if t not in CLOSED_NODE_TYPES)
    if unknown:
        return _err(
            400,
            "bad_request",
            f"unknown node type filter: {unknown}; "
            f"must be among the closed schema types {sorted(CLOSED_NODE_TYPES)}",
        )
    return None


async def api_graph(request: Request) -> JSONResponse:
    """Capped, filtered overview subgraph — never a raw dump.

    Selects the top-``limit`` nodes by degree (degree = incident edges within the
    type/relation filter), then returns only edges whose BOTH endpoints survive
    the cap. ``truncated`` is true when the cap dropped nodes the filters kept.
    """
    state = get_state()
    if state.shutting_down:
        return _draining_response()

    types = _csv_param(request.query_params.get("types"))
    relations = _csv_param(request.query_params.get("relations"))
    bad_type = _validate_type_filter(types)
    if bad_type is not None:
        return bad_type
    try:
        limit = int(request.query_params.get("limit", str(GRAPH_DEFAULT_LIMIT)))
        min_degree = int(request.query_params.get("min_degree", "0"))
    except ValueError:
        return _err(400, "bad_request", "limit and min_degree must be integers")
    if limit < 1:
        return _err(400, "bad_request", "limit must be >= 1")
    if min_degree < 0:
        return _err(400, "bad_request", "min_degree must be >= 0")
    limit = min(limit, GRAPH_MAX_LIMIT)  # clamp, never 400 on oversize

    include_structural = _include_structural(request)
    body = await _graph_read(
        "graph-overview",
        _graph_payload,
        state,
        types,
        relations,
        limit,
        min_degree,
        include_structural,
    )
    return body


def _graph_payload(
    state: ServerState | VaultRuntime,
    types: set[str] | None,
    relations: set[str] | None,
    limit: int,
    min_degree: int,
    include_structural: bool,
) -> dict[str, Any]:
    """Store op: the capped overview subgraph (see :func:`api_graph`)."""
    store = _store(state)
    # 1. Visible nodes (optionally restricted to requested types).
    node_by_id: dict[str, Any] = {}
    if types is None:
        for n in store.list_nodes():
            if _visible_node(n, include_structural=include_structural):
                node_by_id[str(n.id)] = n
    else:
        for t in types:
            for n in store.list_nodes(type=t):
                if _visible_node(n, include_structural=include_structural):
                    node_by_id[str(n.id)] = n

    # 2. Edge universe: relation-filtered edges whose BOTH endpoints are
    #    visible nodes in the filtered set.
    universe: list[Any] = []
    for e in store.list_edges():
        if relations is not None and str(e.type) not in relations:
            continue
        if str(e.src) in node_by_id and str(e.dst) in node_by_id:
            universe.append(e)

    # 3. Degree over the full filtered subgraph (BEFORE the cap — ranking needs it).
    degree: dict[str, int] = dict.fromkeys(node_by_id, 0)
    for e in universe:
        degree[str(e.src)] += 1
        degree[str(e.dst)] += 1

    # 4. min_degree filter → candidates; total reflects post-filter, pre-cap size.
    candidates = [nid for nid in node_by_id if degree[nid] >= min_degree]
    total_nodes = len(candidates)
    total_edges = len(universe)

    # 5. Rank by (degree desc, id asc) — id tiebreak keeps the cap deterministic.
    candidates.sort(key=lambda nid: (-degree[nid], nid))
    kept_ids = set(candidates[:limit])

    # 6. Edges with BOTH endpoints in the returned node set.
    kept_edges = [e for e in universe if str(e.src) in kept_ids and str(e.dst) in kept_ids]

    nodes_out = [_graph_node_payload(node_by_id[nid], degree[nid]) for nid in candidates[:limit]]
    edges_out = [_graph_edge_payload(e) for e in kept_edges]

    # ADR 0009 P2: fold equivalence on READ so the Graph dedupes like search.
    # Collapse variant nodes onto canonical AND remap every edge through the map
    # (drop self-loops the collapse creates, de-dupe) — collapsing nodes without
    # remapping edges would leave edges pointing at dropped nodes. Off-graph: the
    # stored topology is untouched; this is a read transform only.
    equivalence = _curation.equivalence_map(state)
    nodes_out, edges_out = _curation.fold_graph(nodes_out, edges_out, equivalence)

    return {
        "status": "ok",
        "truncated": len(kept_ids) < total_nodes,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "returned_nodes": len(nodes_out),
        "returned_edges": len(edges_out),
        "nodes": nodes_out,
        "edges": edges_out,
    }


async def api_node_neighbors(request: Request) -> JSONResponse:
    """Incremental expand-from-seed: BFS out from one node, capped.

    Uses targeted ``list_edges(src=)``/``list_edges(dst=)`` per frontier (never a
    full edge scan), expanding undirected and respecting the type/relation filters.
    Degrees in the response are computed WITHIN the returned set (per contract),
    which differs from /graph's full-subgraph degree.
    """
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    seed = request.path_params["id"]

    types = _csv_param(request.query_params.get("types"))
    relations = _csv_param(request.query_params.get("relations"))
    bad_type = _validate_type_filter(types)
    if bad_type is not None:
        return bad_type
    try:
        hops = int(request.query_params.get("hops", str(NEIGHBORS_DEFAULT_HOPS)))
        limit = int(request.query_params.get("limit", str(NEIGHBORS_DEFAULT_LIMIT)))
    except ValueError:
        return _err(400, "bad_request", "hops and limit must be integers")
    if hops < 1:
        return _err(400, "bad_request", "hops must be >= 1")
    if limit < 1:
        return _err(400, "bad_request", "limit must be >= 1")
    hops = min(hops, NEIGHBORS_MAX_HOPS)  # clamp, never 400
    limit = min(limit, NEIGHBORS_MAX_LIMIT)

    include_structural = _include_structural(request)
    body = await _graph_read(
        "node-neighbors",
        _neighbors_payload,
        state,
        seed,
        types,
        relations,
        hops,
        limit,
        include_structural,
    )
    return body


def _neighbors_payload(
    state: ServerState | VaultRuntime,
    seed: str,
    types: set[str] | None,
    relations: set[str] | None,
    hops: int,
    limit: int,
    include_structural: bool,
) -> dict[str, Any]:
    """Store op: capped BFS neighbourhood (see :func:`api_node_neighbors`)."""
    store = _store(state)
    seed_node = store.get_node(seed, include_embedding=False)
    # 404 mirrors api_node_detail: missing OR hidden (not closed / infra).
    # The seed is exempt from the structural filter for the same reason
    # api_node_detail is — the caller navigated to this id on purpose, so a
    # direct link to a structural anchor must still resolve. The filter only
    # governs what the expansion admits around it.
    if seed_node is None or not _visible_node(seed_node, include_structural=True):
        raise _ApiError(404, "not_found", f"node not found: {seed}")

    def _admit(node: Any) -> bool:
        if node is None or not _visible_node(node, include_structural=include_structural):
            return False
        if types is not None and str(node.type) not in types:
            return False
        return True

    kept: dict[str, Any] = {seed: seed_node}
    kept_edges: dict[str, Any] = {}
    frontier = {seed}
    for _hop in range(hops):
        if not frontier or len(kept) >= limit:
            break
        next_frontier: set[str] = set()
        incident: list[Any] = []
        for nid in frontier:
            incident.extend(store.list_edges(src=nid))
            incident.extend(store.list_edges(dst=nid))
        for e in incident:
            if relations is not None and str(e.type) not in relations:
                continue
            src, dst = str(e.src), str(e.dst)
            other = dst if src in frontier else src
            if other not in kept:
                if len(kept) >= limit:
                    continue  # node cap hit — skip new nodes (keep edges among kept)
                other_node = store.get_node(other, include_embedding=False)
                if not _admit(other_node):
                    continue
                kept[other] = other_node
                next_frontier.add(other)
            # Record the edge only when BOTH endpoints are kept.
            if src in kept and dst in kept:
                kept_edges[str(e.id)] = e
        frontier = next_frontier

    # Closing pass: BFS records an edge only while processing the frontier
    # that DISCOVERS the far endpoint, so same-ring edges in the OUTERMOST
    # ring (added at the final hop, never processed) are otherwise missed —
    # most visible at the default hops=1, where neighbors-of-the-seed edges
    # would all drop. Sweep the final frontier; both endpoints are already in
    # ``kept`` so this adds no nodes and the cap is unaffected. Still fully
    # targeted (src=/dst= per node), never a full edge scan.
    for nid in frontier:
        for e in (*store.list_edges(src=nid), *store.list_edges(dst=nid)):
            if relations is not None and str(e.type) not in relations:
                continue
            if str(e.src) in kept and str(e.dst) in kept:
                kept_edges[str(e.id)] = e

    # Degree WITHIN the returned set (per the neighbors contract).
    degree: dict[str, int] = dict.fromkeys(kept, 0)
    for e in kept_edges.values():
        degree[str(e.src)] += 1
        degree[str(e.dst)] += 1

    nodes_out = [_graph_node_payload(kept[nid], degree[nid]) for nid in kept]
    edges_out = [_graph_edge_payload(e) for e in kept_edges.values()]
    return {
        "status": "ok",
        "seed": seed,
        "total_nodes": len(nodes_out),
        "total_edges": len(edges_out),
        "returned_nodes": len(nodes_out),
        "returned_edges": len(edges_out),
        "nodes": nodes_out,
        "edges": edges_out,
    }


async def api_graph_stats(request: Request) -> JSONResponse:
    """Counts for the filter controls: node-type counts (closed schema) + the
    open-vocabulary edge-type counts, plus totals.

    Served from the vault's maintained projection (``server/_projection.py``): it never
    scans the graph itself. The body carries ``stale`` and ``rebuilding`` flags next to the
    counts; a vault with no projection yet answers 202 ``{"status": "building"}``."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    include_structural = _include_structural(request)
    try:
        read = await _projection.manager_for(state.vault_path).read(state)
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "graph_read_failed", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected graph-stats failure")
        return _internal_error()
    if read.projection is None:
        return JSONResponse({"status": "building"}, status_code=202)
    # The counts arrive pre-encoded from the worker that built them; only the small flag
    # fields are encoded here, then spliced in.
    tail = encode_json(
        {
            "stale": read.stale,
            "rebuilding": read.rebuilding,
            "projection_age_s": round(max(0.0, time.time() - read.projection.built_at), 1),
        }
    )[1:]
    body = read.projection.graph_stats_json[include_structural]
    return json_bytes_response(body[:-1] + b"," + tail)


async def api_graph_integrity(request: Request) -> JSONResponse:
    """Return the selected vault's durable integrity verdict without mutating it."""
    state = get_state()
    if not isinstance(state, VaultRuntime):
        return _err(409, "no_active_vault", "select a vault to inspect graph integrity")
    # Reads the durable verdict sidecar; concurrent polls share one read.
    integrity = await single_flight(
        ("graph_integrity_summary", str(state.vault_path)),
        _integrity_summary,
        state,
    )
    return JSONResponse(
        {
            "status": "ok",
            "integrity": integrity,
        },
        headers={"Cache-Control": "no-store"},
    )


def _integrity_summary(state: VaultRuntime) -> dict[str, object]:
    return graph_integrity.summary(state, state.vault)


async def api_graph_integrity_run(request: Request) -> JSONResponse:
    """Run the read-only audit under writer serialization and persist its verdict."""
    state = get_state()
    if not isinstance(state, VaultRuntime):
        return _err(409, "no_active_vault", "select a vault before auditing graph integrity")
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "graph integrity audit is restricted to loopback callers")
    async with state.writer_lock:
        if state.draining:
            return _draining_response()
        integrity, audit_error, deferred_cancellation = await _run_blocking_to_completion(
            lambda: _fresh_graph_integrity_report(state)
        )
        if audit_error is not None:
            raise audit_error
        if deferred_cancellation is not None:
            raise deferred_cancellation
        assert isinstance(integrity, dict)
    return JSONResponse(
        {
            "status": "ok",
            "integrity": integrity,
        },
        headers={"Cache-Control": "no-store"},
    )


def _fresh_graph_integrity_report(state: VaultRuntime) -> dict[str, object]:
    """Audit one stable graph snapshot while direct SDK writes are excluded."""

    with state.vault._integrity_scan_guard():  # noqa: SLF001 - shared integrity boundary
        graph_integrity.run_audit(state, state.vault)
        return graph_integrity.summary(state, state.vault)


def _fresh_semantic_quality_report(
    state: Any,
    *,
    recall_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit and scan one store snapshot while direct SDK writes are excluded."""

    with state.vault._integrity_scan_guard():  # noqa: SLF001 - shared integrity boundary
        fingerprints = _materialized_semantic_fingerprints(state)
        registered_predicates = PredicateRegistry(state.vault.path).labels()
        audit_state, audit_result = graph_integrity.run_audit(state, state.vault)
        integrity = graph_integrity.summary(state, state.vault)
        if audit_result is None:
            integrity.pop("last_audit", None)
        integrity["fresh_for_semantic_scan"] = bool(
            audit_result is not None
            and audit_result.graph_generation == audit_state.graph_generation
            and audit_result.status is audit_state.status
        )
        return evaluate_semantic_quality(
            _store(state),
            integrity=integrity,
            recall_samples=recall_samples,
            registered_predicates=registered_predicates,
            config_fingerprint=fingerprints.get("config"),
            extraction_fingerprint=fingerprints.get("extraction"),
            semantic_policy_fingerprint=fingerprints.get("semantic_policy"),
        )


def _fresh_candidate_ledger_quality_report(
    state: Any,
    *,
    run_ids: list[str],
    recall_samples: list[dict[str, Any]] | None = None,
    registered_predicates: list[str] | None = None,
    adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan one stable ledger snapshot without claiming stored-graph authority."""

    from okto_neuron.consolidate.ledger import CandidateLedger

    with state.vault._integrity_scan_guard():  # noqa: SLF001 - shared evidence boundary
        ledger = CandidateLedger(Path(state.vault.path) / ".marginalia")
        return evaluate_semantic_ledger_scan(
            ledger.scan(),
            run_ids,
            recall_samples=recall_samples,
            registered_predicates=registered_predicates,
            adjudication=adjudication,
        )


def _identity_governance_record(decision: Any) -> dict[str, Any]:
    """Secret-free identity-decision projection for the governance read model.

    ``evidence`` is deliberately excluded: it is an open-ended operator/model
    payload and may contain source excerpts.  The typed decision fields below
    are sufficient to understand the durable semantic decision.
    """

    raw = decision.to_json()
    allowed = (
        "kind",
        "decision_id",
        "candidate_id",
        "candidate_ids",
        "left_id",
        "right_id",
        "previous_type",
        "corrected_type",
        "possible_types",
        "reason",
        "judge_model",
        "prompt_version",
        "semantic_policy_fingerprint",
        "created_at",
    )
    return {field: raw[field] for field in allowed if field in raw}


def _semantic_governance_payload(state: Any) -> dict[str, Any]:
    """Read the current semantic policy and its durable governance side-stores."""

    from okto_neuron.consolidate.ledger import (
        _ACCEPTED_LEDGER_VERSIONS,
        CandidateLedger,
    )
    from okto_neuron.semantic_fingerprint import (
        load_semantic_materialization,
        semantic_materialization_path,
    )

    vault_path = Path(state.vault_path)
    current = _effective_semantic_fingerprints(state).semantic_policy_fingerprint
    graph_generation = state.vault.store.generation()
    materialization = load_semantic_materialization(
        semantic_materialization_path(vault_path),
        expected_graph_generation=graph_generation or None,
    )
    materialized_policy = (
        str(materialization["fingerprints"]["semantic_policy"])
        if materialization is not None
        else None
    )

    # Run rows only, read by offset: the cost follows the number of runs, not the ledger.
    run_rows = CandidateLedger(vault_path / ".marginalia").ingest_run_records()
    starts: dict[str, dict[str, Any]] = {}
    completed_runs: list[dict[str, str]] = []
    latest_by_document: dict[str, dict[str, str]] = {}
    for record in run_rows:
        if record.get("ledger_version") not in _ACCEPTED_LEDGER_VERSIONS:
            continue
        if record.get("kind") != "ingest_run":
            continue
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            continue
        run_state = str(record.get("state") or "").strip()
        if run_state == "started":
            starts[run_id] = record
            continue
        if run_state != "completed":
            continue
        started = starts.get(run_id, {})
        document_id = str(started.get("document_id") or "").strip()
        if not document_id:
            continue
        fingerprint = str(
            record.get("post_semantic_policy_fingerprint")
            or started.get("semantic_policy_fingerprint")
            or ""
        ).strip()
        applied = {
            "run_id": run_id,
            "document_id": document_id,
            "fingerprint": fingerprint,
        }
        completed_runs.append(applied)
        latest_by_document[document_id] = applied

    observed: dict[str, set[str]] = {}
    runs_without_fingerprint: set[str] = set()
    for run in completed_runs:
        fingerprint = run["fingerprint"]
        if fingerprint:
            observed.setdefault(fingerprint, set()).add(run["run_id"])
        else:
            runs_without_fingerprint.add(run["run_id"])
    latest_by_fingerprint: dict[str, int] = {}
    latest_without_fingerprint = 0
    for run in latest_by_document.values():
        fingerprint = run["fingerprint"]
        if fingerprint:
            latest_by_fingerprint[fingerprint] = latest_by_fingerprint.get(fingerprint, 0) + 1
        else:
            latest_without_fingerprint += 1
    observed_rows = [
        {
            "fingerprint": fingerprint,
            "run_count": len(run_ids),
            "run_ids": sorted(run_ids),
            "latest_applied_run_count": latest_by_fingerprint.get(fingerprint, 0),
        }
        for fingerprint, run_ids in sorted(observed.items())
    ]

    registry_records = PredicateRegistry(vault_path).records()
    registry_counts = {"canonical": 0, "provisional": 0, "total": len(registry_records)}
    for record in registry_records:
        registry_counts[record.lifecycle] += 1

    identity_records = _curation.identity_decision_index(state).records()
    identity_counts = {
        "type_correction": 0,
        "distinct": 0,
        "ambiguous_review": 0,
        "total": len(identity_records),
    }
    projected_identity_records = []
    for decision in identity_records:
        row = _identity_governance_record(decision)
        identity_counts[str(row["kind"])] += 1
        projected_identity_records.append(row)

    ledger_requires_rebuild = bool(latest_by_document) and (
        latest_without_fingerprint > 0 or any(value != current for value in latest_by_fingerprint)
    )
    return {
        "current_semantic_policy_fingerprint": current,
        "materialized_graph_generation": (
            str(materialization["graph_generation"]) if materialization is not None else None
        ),
        "materialized_semantic_policy_fingerprint": materialized_policy,
        "materialization_source": (
            str(materialization["source"]) if materialization is not None else None
        ),
        "observed_semantic_policy_fingerprints": observed_rows,
        "observed_run_count": sum(row["run_count"] for row in observed_rows),
        "runs_without_fingerprint": len(runs_without_fingerprint),
        "latest_applied_run_count": len(latest_by_document),
        "latest_applied_runs_without_fingerprint": latest_without_fingerprint,
        "superseded_completed_run_count": len(completed_runs) - len(latest_by_document),
        "rebuild_required": (
            materialized_policy != current
            if materialized_policy is not None
            else ledger_requires_rebuild
        ),
        "predicate_registry": {
            "counts": registry_counts,
            "records": [record.to_json() for record in registry_records],
        },
        "identity_decisions": {
            "counts": identity_counts,
            "records": projected_identity_records,
        },
    }


def _effective_semantic_fingerprints(state: Any) -> Any:
    """Fingerprint the configured ingest policy used by governance and audits."""

    from okto_neuron.companion import _incremental
    from okto_neuron.semantic_fingerprint import semantic_fingerprints

    cfg = _load_vault_config(state)
    return semantic_fingerprints(
        cfg,
        Path(state.vault_path),
        ingest_config=cfg.ingest,
        effective_incremental=_incremental.incremental_enabled(cfg.ingest),
        effective_subchunk=_incremental.subchunk_enabled(cfg.ingest),
    )


def _materialized_semantic_fingerprints(state: Any) -> dict[str, str | None]:
    """Return the fingerprint triplet bound to the open graph generation."""

    from okto_neuron.semantic_fingerprint import materialized_semantic_fingerprints

    generation = state.vault.store.generation()
    return materialized_semantic_fingerprints(
        state.vault_path,
        graph_generation=generation or None,
    )


async def api_semantic_governance(request: Request) -> JSONResponse:
    """Return the loopback-only, secret-free semantic governance read model."""

    state = get_state()
    if state.shutting_down:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "semantic governance is restricted to loopback callers",
        )
    try:
        async with state.config_lock:
            body = await store_io(
                encode_op, lambda: {"status": "ok", **_semantic_governance_payload(state)}
            )
    except (PredicateRegistryError, ValueError):
        _LOG.exception("semantic governance side-store validation failed")
        return _internal_error()
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected semantic governance read failure")
        return _internal_error()
    return json_bytes_response(body, headers={"Cache-Control": "no-store"})


async def api_semantic_quality(request: Request) -> JSONResponse:
    """Run a loopback-only graph or pre-commit semantic evidence scan."""

    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "semantic quality audit is restricted to loopback callers")
    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    raw_samples = payload.get("recall_samples")
    if raw_samples is not None:
        if not isinstance(raw_samples, list):
            return _err(400, "bad_request", "recall_samples must be a list")
        if len(raw_samples) > 1000:
            return _err(400, "bad_request", "recall_samples must contain at most 1000 rows")
        if any(not isinstance(sample, dict) for sample in raw_samples):
            return _err(400, "bad_request", "every recall sample must be an object")
        recall_samples = [dict(sample) for sample in raw_samples]
    else:
        recall_samples = None
    raw_registered = payload.get("registered_predicates")
    if raw_registered is not None:
        if not isinstance(raw_registered, list):
            return _err(400, "bad_request", "registered_predicates must be a list")
        if len(raw_registered) > 10000:
            return _err(
                400,
                "bad_request",
                "registered_predicates must contain at most 10000 values",
            )
        if any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in raw_registered
        ):
            return _err(
                400,
                "bad_request",
                "every registered predicate must be non-empty text",
            )
        registered_predicates = list(raw_registered)
    else:
        registered_predicates = None
    raw_adjudication = payload.get("adjudication")
    if raw_adjudication is not None and not isinstance(raw_adjudication, dict):
        return _err(400, "bad_request", "adjudication must be an object")
    adjudication = dict(raw_adjudication) if isinstance(raw_adjudication, dict) else None
    variant = payload.get("variant", "stored_graph")
    if variant not in {"stored_graph", "candidate_ledger"}:
        return _err(
            400,
            "bad_request",
            "variant must be 'stored_graph' or 'candidate_ledger'",
        )
    run_ids: list[str] = []
    if variant == "candidate_ledger":
        raw_run_ids = payload.get("run_ids")
        if not isinstance(raw_run_ids, list) or not raw_run_ids:
            return _err(
                400,
                "bad_request",
                "candidate_ledger variant requires a non-empty run_ids list",
            )
        if len(raw_run_ids) > 100:
            return _err(400, "bad_request", "run_ids must contain at most 100 values")
        if any(not isinstance(run_id, str) or not run_id.strip() for run_id in raw_run_ids):
            return _err(400, "bad_request", "every run_id must be a non-empty string")
        run_ids = [run_id.strip() for run_id in raw_run_ids]
    elif registered_predicates is not None or adjudication is not None:
        return _err(
            400,
            "bad_request",
            "registered_predicates and adjudication apply only to candidate_ledger",
        )
    try:
        # A verified-snapshot job can legitimately own writer_lock for minutes
        # while it calls a model. An interactive/read-only audit must never wait
        # behind that job until the HTTP client times out: acquire briefly or
        # return a retryable, named boundary. The scan still owns writer_lock for
        # its complete audit+store pass, so a successful response is one stable
        # snapshot rather than a best-effort concurrent read.
        async with _writer_lock_fast(
            state,
            timeout=1.0,
            verify_write_allowed=False,
        ):
            if state.draining:
                return _draining_response()
            if variant == "candidate_ledger":
                report = await store_io(
                    _fresh_candidate_ledger_quality_report,
                    state,
                    run_ids=run_ids,
                    recall_samples=recall_samples,
                    registered_predicates=registered_predicates,
                    adjudication=adjudication,
                )
            else:
                report = await store_io(
                    _fresh_semantic_quality_report,
                    state,
                    recall_samples=recall_samples,
                )
    except _LockBusy:
        return _err(
            409,
            "audit_busy",
            "semantic quality audit needs a stable graph snapshot; retry after the active "
            "ingest or curation snapshot finishes",
            retryable=True,
        )
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except PredicateRegistryError:
        _LOG.exception("semantic-quality predicate registry validation failed")
        return _internal_error()
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected semantic-quality evaluation failure")
        return _internal_error()
    return json_bytes_response(
        await store_io(encode_json, {"status": "ok", "semantic_quality": report}),
        headers={"Cache-Control": "no-store"},
    )


# --------------------------- /api/v1 — config read/write ---------------------------


def _config_payload(cfg: Any, *, scope: str) -> dict[str, Any]:
    from okto_neuron.curator import candidate_curator_system, relation_curator_system
    from okto_neuron.extract import extraction_system_prompt
    from okto_neuron.resolve import _VERDICT_SYSTEM

    try:
        from okto_neuron.companion import _ASK_SYSTEM  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        # Stage C (backend-engineer) hasn't landed _ASK_SYSTEM yet; use the
        # current inline fallback from companion.ask().
        _ASK_SYSTEM = "You answer grounded in the provided notes. Be concise."

    resolved_embedding = cfg.embedding.resolved_provider()
    resolved_defaults = cfg.llm.resolved_defaults()
    resolved_steps = [
        cfg.llm.resolved(step)
        for step in ("extraction", "judge", "curator", "relation_curator", "ask")
    ]
    credential_names = {
        name
        for name in (
            resolved_embedding.api_key_env,
            resolved_defaults.api_key_env,
            *(step.api_key_env for step in resolved_steps),
        )
        if isinstance(name, str) and name
    }

    defaults_payload = cfg.llm.defaults.model_dump(mode="json")
    defaults_payload.update(
        provider=resolved_defaults.provider,
        api_base=resolved_defaults.api_base,
        api_key_env=resolved_defaults.api_key_env,
    )

    return {
        "scope": scope,
        "inherits_application_defaults": bool(getattr(cfg, "inherits_application_defaults", False)),
        "embedding": {
            "provider_ref": cfg.embedding.provider_ref,
            "provider": resolved_embedding.provider,
            "model": resolved_embedding.model,
            "api_base": resolved_embedding.api_base,
            "api_key_env": resolved_embedding.api_key_env,
            "allow_remote": resolved_embedding.allow_remote,
            "dimension": resolved_embedding.dimension,
            "batch_size": resolved_embedding.batch_size,
            "max_concurrent_batches": resolved_embedding.max_concurrent_batches,
        },
        "llm": {
            "allow_remote": cfg.llm.allow_remote,
            "defaults": defaults_payload,
            "extraction": cfg.llm.extraction.model_dump(mode="json"),
            "judge": cfg.llm.judge.model_dump(mode="json"),
            "curator": cfg.llm.curator.model_dump(mode="json"),
            "relation_curator": cfg.llm.relation_curator.model_dump(mode="json"),
            "ask": cfg.llm.ask.model_dump(mode="json"),
            "step_default_prompts": {
                "extraction": extraction_system_prompt(cfg.packs),
                "judge": _VERDICT_SYSTEM,
                "curator": candidate_curator_system(cfg.packs),
                "relation_curator": relation_curator_system(cfg.packs),
                "ask": _ASK_SYSTEM,
            },
        },
        "consolidation": {
            "auto_commit_threshold": cfg.consolidation.auto_commit_threshold,
            "review_on_contradiction": cfg.consolidation.review_on_contradiction,
            "type_adjudication_enabled": (cfg.consolidation.type_adjudication_enabled),
            "relation_curator_enabled": cfg.consolidation.relation_curator_enabled,
            "audit_superseded_nodes_with_llm": (cfg.consolidation.audit_superseded_nodes_with_llm),
            "audit_superseded_relations_with_llm": (
                cfg.consolidation.audit_superseded_relations_with_llm
            ),
            # ADR 0015 performance knobs (D1 concurrency, D4 batching, D2 prefilter).
            # NOTE: this stays the *configured* value. The clamped value the
            # backend will actually run is reported separately under "capacity",
            # so configured != effective remains visible instead of silently
            # rewriting what the user stored.
            "curation_max_concurrent": cfg.consolidation.curation_max_concurrent,
            "curation_call_timeout_s": cfg.consolidation.curation_call_timeout_s,
            "curation_batch_size": cfg.consolidation.curation_batch_size,
            "prefilter": cfg.consolidation.prefilter.model_dump(mode="json"),
        },
        # Read-only configured-vs-effective LLM concurrency. Owned by
        # okto_neuron.config._capacity, the single helper both the extraction and
        # curation fan-outs consume, so the UI cannot drift from the runtime.
        "capacity": capacity_report(cfg),
        "upkeep": cfg.upkeep.model_dump(mode="json"),
        "folder_watch": cfg.folder_watch.model_dump(mode="json"),
        "ingest": cfg.ingest.model_dump(mode="json"),
        "packs": list(cfg.packs),
        "credential_status": {
            name: bool(_secret_env(name)) for name in sorted(credential_names)
        },
        "managed_credentials_supported": True,
        "reembed_required_fields": [
            "embedding.provider_ref",
            "embedding.provider",
            "embedding.model",
            "embedding.dimension",
        ],
        "semantic_rebuild_required_fields": [
            "consolidation.type_adjudication_enabled",
            "consolidation.relation_curator_enabled",
        ],
    }


def _load_vault_config(state: ServerState | VaultRuntime) -> Any:
    from okto_neuron.config import VaultConfig
    from okto_neuron.errors import ConfigNotFound, ConfigParseError

    if state.vault_path is None:
        return VaultConfig.load_application_defaults()
    try:
        return VaultConfig.load(state.vault_path)
    except (ConfigNotFound, ConfigParseError, FileNotFoundError):
        return VaultConfig()


async def api_config_get(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await store_io(_vault_config_payload, state)
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected config-read failure")
        return _internal_error()
    return JSONResponse(payload)


def _vault_config_payload(state: ServerState | VaultRuntime) -> dict[str, Any]:
    """Store op: load this vault's YAML and render the config view (pack
    prompts are read from disk)."""
    payload = _config_payload(_load_vault_config(state), scope="vault")
    payload["status"] = "ok"
    return payload


async def api_application_config_get(request: Request) -> JSONResponse:
    state = get_server_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await store_io(_application_config_payload)
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected application-config read failure")
        return _internal_error()
    return JSONResponse(payload)


def _application_config_payload() -> dict[str, Any]:
    from okto_neuron.config import VaultConfig

    payload = _config_payload(VaultConfig.load_application_defaults(), scope="application")
    payload["status"] = "ok"
    return payload


def _prepare_config_patch(patch: dict[str, Any]) -> str | None:
    """Apply shared config-write safety rules; return an error detail if invalid."""

    if _contains_literal_secret_field(patch):
        return "literal API keys are not accepted in config; use the credential endpoint"
    embedding_patch = patch.get("embedding")
    if (
        isinstance(embedding_patch, dict)
        and "api_key_env" not in embedding_patch
        and ({"provider", "api_base"} & set(embedding_patch))
    ):
        embedding_patch["api_key_env"] = None
    return None


def _stored_embedding_width(vault: Any) -> int | None:
    """The vector width an OPEN vault's graph was built at, read from the live handle.

    Never opens or leases anything: a cold graph cannot be read without a writable open
    (a read-only grafx open needs a checkpoint-complete database), so a vault that is not
    open reports ``None`` and is checked when it opens.
    """
    store = getattr(vault, "store", None)
    for holder, attr in (
        (store, "_embedding_dim"),
        (getattr(store, "_graph_handle", None), "embedding_dim"),
    ):
        value = getattr(holder, attr, None) if holder is not None else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _embedding_width_report(
    state: ServerState | VaultRuntime,
    entries: list[tuple[Path, int | None, int | None]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Per-vault stored-vs-configured width after an embedding config change.

    ``entries`` are ``(vault path, configured width before, configured width after)``.
    Names only (never paths). A vault whose graph is open is compared exactly; one that is
    not open is reported as not checked, with the conditional consequence. The remedy
    names the route that works while a vault cannot be opened.
    """
    from okto_neuron.server._open_failure import reembed_remedy, registered_vault_name

    report: list[dict[str, object]] = []
    notes: list[str] = []
    for path, before, after in entries:
        name = registered_vault_name(path)
        label = f"vault '{name}'" if name else "a vault"
        stored = _stored_embedding_width(state.vault_pool.peek(path))
        item: dict[str, object] = {
            "vault": name,
            "configured_before": before,
            "configured_after": after,
            "stored": stored,
            "checked": stored is not None,
            "refuses_to_open": (stored != after) if stored is not None and after else None,
        }
        report.append(item)
        if stored is not None and after and stored != after:
            notes.append(
                f"{label}: stored graph width {stored} differs from the configured width {after}; "
                "it will refuse to open (embedding_dim_mismatch) until it is re-embedded. "
                f"{reembed_remedy(name)}"
            )
        elif stored is None and before != after:
            notes.append(
                f"{label}: its stored graph width was not read (graph not open); the configured "
                f"width changed {before} -> {after}. If the graph was built at {before} it will "
                "refuse to open (embedding_dim_mismatch) until it is re-embedded. "
                f"{reembed_remedy(name)}"
            )
    return report, notes


def _configured_embedding_dimension(vault_path: Path) -> int | None:
    from okto_neuron.config import VaultConfig

    try:
        return int(VaultConfig.load(vault_path).embedding.dimension)
    except Exception:  # noqa: BLE001 - a width hint is never worth failing a config write
        return None


async def api_config_patch(request: Request) -> JSONResponse:
    state = get_state()
    if state.draining:
        return _draining_response()
    # M1: config-write is a sensitive route and is always loopback-only.
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "config write is restricted to loopback callers; use a "
            "loopback-preserving SSH tunnel for remote administration",
        )
    try:
        patch = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    patch_error = _prepare_config_patch(patch)
    if patch_error:
        return _err(400, "bad_request", patch_error)

    from okto_neuron.config import VaultConfig

    # config_lock, NOT writer_lock: an ingest item holds writer_lock for its
    # whole run (minutes+), which starved the UI's Save behind in-flight
    # ingestion (observed live: PATCH {} timed out while an item was in dedup).
    # Config writes only touch okto-neuron.yaml + runtime caches, and the live
    # semantics below already tolerate in-flight work on the old settings.
    async with state.config_lock:
        if state.draining:
            return _draining_response()
        width_before = await store_io(_configured_embedding_dimension, state.vault_path)
        try:
            cfg, changed = await store_io(_apply_vault_config_patch, state, patch)
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected config-write failure")
            return _internal_error()

    reembed = any(c in VaultConfig.REEMBED_FIELDS for c in changed)
    semantic_rebuild = any(c in VaultConfig.SEMANTIC_REBUILD_FIELDS for c in changed)
    notes: list[str] = []
    if semantic_rebuild:
        applied = "rebuild"
        notes.append(
            "semantic model stage changed: rebuild existing sources before treating "
            "the stored graph as representative of the new materialization policy"
        )
        if reembed:
            notes.append(
                "the embedding space also changed; the semantic rebuild must regenerate "
                "the graph and its vectors"
            )
    elif reembed:
        applied = "reembed"
        notes.append(
            "embedding provider/model changed: re-embed the vault before querying; "
            "stored embeddings from the old model are incompatible and will score "
            "incorrectly until the vault is re-embedded; no restart is required"
        )
    elif changed:
        applied = "live"
        if any(
            field in changed
            for field in (
                "ingest.chunk_size_bytes",
                "ingest.chunk_overlap_bytes",
            )
        ):
            notes.append(
                "chunking changes apply on the next ingest; reingest existing sources "
                "to rebuild their Blocks and extraction results with the new partition"
            )
        elif any(
            field in changed
            for field in (
                "embedding.batch_size",
                "embedding.max_concurrent_batches",
            )
        ):
            notes.append(
                "active embedding work adopts the new batch limits within 250 ms "
                "or at its next completed-batch scheduling boundary"
            )
        elif "llm.extraction.max_concurrent" in changed:
            notes.append(
                "active extraction adopts the new concurrency limit within 250 ms "
                "or at the next completed-chunk scheduling boundary"
            )
        else:
            notes.append("change takes effect on the next request")
    else:
        applied = "live"
        notes.append("no fields changed")

    width_report: list[dict[str, object]] = []
    if reembed:
        width_report, width_notes = _embedding_width_report(
            state, [(state.vault_path, width_before, int(cfg.embedding.dimension))]
        )
        notes.extend(width_notes)

    payload = await store_io(_config_payload, cfg, scope="vault")
    payload["status"] = "ok"
    response: dict[str, object] = {
        "status": "ok",
        "config": payload,
        "applied": applied,
        "changed": changed,
        "notes": notes,
        "affected_vaults": [str(state.vault_path)] if reembed or semantic_rebuild else [],
        "rebuild_required_vaults": ([str(state.vault_path)] if semantic_rebuild else []),
    }
    if width_report:
        response["embedding_width"] = width_report
    return JSONResponse(response)


def _apply_vault_config_patch(
    state: ServerState | VaultRuntime, patch: dict[str, Any]
) -> tuple[Any, list[str]]:
    """Store op: write the vault YAML patch and drop stale runtime caches."""
    from okto_neuron.config import VaultConfig

    cfg, changed = VaultConfig.apply_patch(state.vault_path, patch)
    if changed and state.vault is not None:
        # The vault caches its resolved embedder for process life; without
        # this, a PATCHed embedding model only takes effect after a daemon
        # restart (stale-embedder bug). LLM providers are rebuilt from
        # config per call, so the embedder is the only runtime cache to
        # drop. In-flight work keeps its old instance; the next use
        # constructs fresh. The dimension-mismatch guard is untouched —
        # the reembed note below and the ``embedding_dim_mismatch`` vault
        # warning still apply to stored vectors.
        state.vault.invalidate_runtime_caches()
    return cfg, changed


def _apply_application_config_patch(
    state: ServerState, patch: dict[str, Any]
) -> tuple[Any, list[str], list[str], list[str], list[str], list[tuple[Path, int | None, int | None]]]:
    """Store op: write the app defaults and walk every inheriting vault's YAML.

    The last element is ``(path, width before, width after)`` for each inheriting vault whose
    embedding space changed, for the stored-vs-configured width report.
    """
    from okto_neuron.config import VaultConfig

    before = VaultConfig.load_application_defaults()
    cfg, changed = VaultConfig.apply_application_defaults_patch(patch)
    embedding_changed, reembed_vaults, rebuild_vaults = _application_config_effects(before, cfg)
    for path in embedding_changed:
        cached = state.vault_pool.peek(Path(path))
        if cached is not None:
            cached.invalidate_runtime_caches()
    widths: list[tuple[Path, int | None, int | None]] = []
    for path in reembed_vaults:
        try:
            widths.append(
                (
                    Path(path),
                    int(VaultConfig.load(Path(path), application_defaults=before).embedding.dimension),
                    int(VaultConfig.load(Path(path), application_defaults=cfg).embedding.dimension),
                )
            )
        except Exception:  # noqa: BLE001 - a width hint is never worth failing a config write
            widths.append((Path(path), None, None))
    return cfg, changed, embedding_changed, reembed_vaults, rebuild_vaults, widths


def _application_config_effects(
    before: Any,
    after: Any,
) -> tuple[list[str], list[str], list[str]]:
    """Return inherited cache, re-embed, and semantic-rebuild effects."""

    from okto_neuron.config import VaultConfig

    embedding_changed: list[str] = []
    reembed_required: list[str] = []
    semantic_rebuild_required: list[str] = []
    for entry in list_vaults():
        try:
            raw = VaultConfig.load_raw(entry.path)
            if raw.get("inherits_application_defaults") is not True:
                continue
            before_effective = VaultConfig.load(entry.path, application_defaults=before)
            after_effective = VaultConfig.load(entry.path, application_defaults=after)
        except Exception:  # noqa: BLE001
            _LOG.warning("could not evaluate inherited config for %s", entry.path)
            continue
        path = str(entry.path)
        if before_effective.embedding != after_effective.embedding:
            embedding_changed.append(path)
        if any(
            getattr(before_effective.embedding, field) != getattr(after_effective.embedding, field)
            for field in ("provider_ref", "provider", "model", "dimension")
        ):
            reembed_required.append(path)
        if any(
            getattr(before_effective.consolidation, field)
            != getattr(after_effective.consolidation, field)
            for field in ("type_adjudication_enabled", "relation_curator_enabled")
        ):
            semantic_rebuild_required.append(path)
    return embedding_changed, reembed_required, semantic_rebuild_required


async def api_application_config_patch(request: Request) -> JSONResponse:
    state = get_server_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "config write is restricted to loopback callers")
    try:
        patch = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    patch_error = _prepare_config_patch(patch)
    if patch_error:
        return _err(400, "bad_request", patch_error)


    async with state.config_lock:
        if state.draining:
            return _draining_response()
        try:
            (
                cfg,
                changed,
                _embedding_changed,
                reembed_vaults,
                rebuild_vaults,
                width_entries,
            ) = await store_io(_apply_application_config_patch, state, patch)
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected application-config write failure")
            return _internal_error()

    notes: list[str] = []
    if rebuild_vaults:
        applied = "rebuild"
        notes.append(
            f"semantic model stage changed for {len(rebuild_vaults)} inheriting vault(s); "
            "rebuild their existing sources before using the graph as new-policy output"
        )
        if reembed_vaults:
            notes.append(
                f"embedding space also changed for {len(reembed_vaults)} inheriting "
                "vault(s); the semantic rebuild must regenerate their vectors"
            )
    elif reembed_vaults:
        applied = "reembed"
        notes.append(
            f"embedding space changed for {len(reembed_vaults)} inheriting vault(s); "
            "re-embed is required before querying those vaults"
        )
    elif changed:
        applied = "live"
        if any(
            field in changed
            for field in (
                "ingest.chunk_size_bytes",
                "ingest.chunk_overlap_bytes",
            )
        ):
            notes.append(
                "chunking defaults apply on the next ingest in inheriting and new vaults; "
                "reingest existing sources to rebuild their Blocks and extraction results"
            )
        elif any(
            field in changed
            for field in (
                "embedding.batch_size",
                "embedding.max_concurrent_batches",
            )
        ):
            notes.append(
                "active embedding work in inheriting vaults adopts the new batch "
                "limits within 250 ms or at its next completed-batch boundary"
            )
        elif "llm.extraction.max_concurrent" in changed:
            notes.append(
                "active extraction in inheriting vaults adopts the new concurrency limit "
                "within 250 ms or at the next completed-chunk scheduling boundary"
            )
        else:
            notes.append("defaults take effect immediately for inheriting and newly created vaults")
    else:
        applied = "live"
        notes.append("no fields changed")

    width_report: list[dict[str, object]] = []
    if reembed_vaults:
        width_report, width_notes = _embedding_width_report(state, width_entries)
        notes.extend(width_notes)

    payload = await store_io(_config_payload, cfg, scope="application")
    payload["status"] = "ok"
    response: dict[str, object] = {
        "status": "ok",
        "config": payload,
        "applied": applied,
        "changed": changed,
        "notes": notes,
        "affected_vaults": sorted(set(reembed_vaults) | set(rebuild_vaults)),
        "rebuild_required_vaults": rebuild_vaults,
    }
    if width_report:
        response["embedding_width"] = width_report
    return JSONResponse(response)


def _contains_literal_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"api_key", "apikey", "api_secret"}:
                return True
            if _contains_literal_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_literal_secret_field(child) for child in value)
    return False


async def _api_provider_credential_put(
    request: Request,
    *,
    default_kind: str | None = None,
) -> JSONResponse:
    """Persist one provider API key without putting it in vault configuration.

    The credential-free application route remains loopback-only and inherits
    the REST cross-origin and JSON-write defenses. The response contains only
    the generated env-var reference; the secret is never returned or logged.
    """

    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "credential writes are restricted to loopback callers",
        )
    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    unknown_fields = set(payload) - {"kind", "provider", "api_base", "api_key"}
    if unknown_fields:
        return _err(400, "bad_request", "credential request contains unknown fields")

    kind = payload.get("kind", default_kind)
    provider = payload.get("provider")
    api_base = payload.get("api_base") or None
    api_key = payload.get("api_key")
    if kind not in {"llm", "embedding"}:
        return _err(400, "bad_request", "kind must be 'llm' or 'embedding'")
    if not isinstance(provider, str) or not provider.strip():
        return _err(400, "bad_request", "missing or invalid field: provider")
    provider = provider.strip()
    if api_base is not None and not isinstance(api_base, str):
        return _err(400, "bad_request", "invalid field: api_base")
    if not isinstance(api_key, str) or not api_key:
        return _err(400, "bad_request", "missing or invalid field: api_key")

    from okto_neuron.onboarding import (
        MANAGED_API_KEY_PROVIDERS,
        MANAGED_EMBEDDING_API_KEY_PROVIDERS,
        default_api_key_env,
        write_user_env_secret,
    )

    try:
        managed_providers = (
            MANAGED_API_KEY_PROVIDERS if kind == "llm" else MANAGED_EMBEDDING_API_KEY_PROVIDERS
        )
        if provider not in managed_providers:
            raise ValueError(
                "this provider does not support Okto Neuron-managed API keys; "
                "use its provider-specific environment credential"
            )
        env_name = default_api_key_env(provider, api_base)
        # The env file is application-scoped read-modify-replace state. Serialize
        # browser/API saves so two tabs cannot each persist a stale snapshot and
        # silently lose one key on restart. The synchronous lock is acquired by
        # the worker itself, so request cancellation cannot release serialization
        # while filesystem/DPAPI work is still running off-loop.
        state = get_server_state()
        async with state.config_lock:
            await store_io(
                state.run_application_mutation,
                functools.partial(write_user_env_secret, env_name, api_key),
            )
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except OSError:
        _LOG.exception("credential write failed for provider %s", provider)
        return _internal_error()

    return JSONResponse(
        {
            "status": "ok",
            "api_key_env": env_name,
            "configured": True,
        },
        headers={"Cache-Control": "no-store"},
    )


async def api_provider_credential_put(request: Request) -> JSONResponse:
    """Store an LLM or embedding provider credential through one contract."""

    return await _api_provider_credential_put(request)


async def api_llm_credential_put(request: Request) -> JSONResponse:
    """Compatibility alias for clients released before the generic route."""

    return await _api_provider_credential_put(request, default_kind="llm")


def _provider_ref_profile(provider_ref: str) -> tuple[Any, str | None, Any]:
    """Store op: resolve a saved provider connection and the uses its driver
    supports (both read the provider registry YAML)."""
    from okto_neuron.providers import ProviderRegistry, resolve_provider

    profile, api_key_env = resolve_provider(provider_ref)
    return profile, api_key_env, ProviderRegistry.load().provider_uses(profile.driver)


def _credential_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "configured": bool(_secret_env(record.env_name)),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _provider_payload(registry: Any, profile: Any) -> dict[str, Any]:
    credential = (
        registry.credential(profile.credential_id) if profile.credential_id is not None else None
    )
    return {
        "id": profile.id,
        "name": profile.name,
        "driver": profile.driver,
        "api_base": profile.api_base,
        "allow_remote": profile.allow_remote,
        "credential_id": profile.credential_id,
        "credential_name": credential.name if credential is not None else None,
        "credential_configured": bool(
            credential is not None and _secret_env(credential.env_name)
        ),
        "api_key_env": credential.env_name if credential is not None else None,
        "parameter_mode": profile.parameter_mode,
        "request_timeout_s": profile.request_timeout_s,
        "uses": registry.provider_uses(profile.driver),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


async def _run_provider_registry_mutation(operation: Any) -> Any:
    state = get_server_state()
    async with state.config_lock:
        return await store_io(state.run_application_mutation, operation)


async def api_credentials_list(request: Request) -> JSONResponse:
    try:
        credentials = await store_io(_credentials_payload)
    except ValueError as exc:
        return _err(500, "provider_registry_invalid", str(exc))
    return JSONResponse(
        {"status": "ok", "credentials": credentials},
        headers={"Cache-Control": "no-store"},
    )


def _credentials_payload() -> list[dict[str, Any]]:
    from okto_neuron.providers import ProviderRegistry

    registry = ProviderRegistry.load()
    return [_credential_payload(record) for record in registry.document.credentials]


async def api_credentials_create(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "credential writes are restricted to loopback callers")
    try:
        payload = await _read_json(request)
        name = _require(payload, "name", str)
        api_key = _require(payload, "api_key", str)
        if set(payload) - {"name", "api_key"}:
            raise _BadRequest("credential request contains unknown fields")
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    from okto_neuron.onboarding import delete_user_env_secret, write_user_env_secret
    from okto_neuron.providers import ProviderRegistry

    def _mutate() -> Any:
        registry = ProviderRegistry.load()
        record = registry.add_credential(name)
        write_user_env_secret(record.env_name, api_key)
        try:
            registry.save()
        except Exception:
            delete_user_env_secret(record.env_name)
            raise
        return record

    try:
        record = await _run_provider_registry_mutation(_mutate)
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except OSError:
        _LOG.exception("named credential creation failed")
        return _internal_error()
    return JSONResponse(
        {"status": "ok", "credential": _credential_payload(record)},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def api_credential_update(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "credential writes are restricted to loopback callers")
    try:
        payload = await _read_json(request)
        if set(payload) - {"name", "api_key"}:
            raise _BadRequest("credential request contains unknown fields")
        if not payload:
            raise _BadRequest("credential update must include name or api_key")
        name = payload.get("name")
        api_key = payload.get("api_key")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise _BadRequest("credential name must not be blank")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise _BadRequest("API key must not be blank")
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    credential_id = request.path_params["credential_id"]
    from okto_neuron.onboarding import write_user_env_secret
    from okto_neuron.providers import ProviderRegistry

    def _mutate() -> Any:
        registry = ProviderRegistry.load()
        record = registry.credential(credential_id)
        if name is not None:
            registry.update_credential_name(credential_id, name)
        if api_key is not None:
            write_user_env_secret(record.env_name, api_key)
            record.updated_at = datetime.now(timezone.utc).isoformat()
        registry.save()
        return record

    try:
        record = await _run_provider_registry_mutation(_mutate)
    except ValueError as exc:
        return _err(404 if "not found" in str(exc) else 400, "bad_request", str(exc))
    except OSError:
        _LOG.exception("named credential update failed")
        return _internal_error()
    return JSONResponse(
        {"status": "ok", "credential": _credential_payload(record)},
        headers={"Cache-Control": "no-store"},
    )


async def api_credential_delete(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "credential writes are restricted to loopback callers")
    credential_id = request.path_params["credential_id"]
    from okto_neuron.onboarding import delete_user_env_secret
    from okto_neuron.providers import ProviderRegistry

    def _mutate() -> Any:
        registry = ProviderRegistry.load()
        record = registry.remove_credential(credential_id)
        registry.save()
        delete_user_env_secret(record.env_name)
        return record

    try:
        record = await _run_provider_registry_mutation(_mutate)
    except ValueError as exc:
        status = 409 if "used by provider" in str(exc) else 404
        return _err(status, "credential_in_use" if status == 409 else "not_found", str(exc))
    except OSError:
        _LOG.exception("named credential deletion failed")
        return _internal_error()
    return JSONResponse({"status": "ok", "deleted": record.id})


def _provider_reference_locations(provider_id: str, *, embedding_only: bool = False) -> list[str]:
    """Return raw config locations that hold one provider reference."""

    import yaml as _yaml

    from okto_neuron.config import VaultConfig

    def _contains(value: Any, *, in_embedding: bool = False) -> bool:
        if isinstance(value, dict):
            if value.get("provider_ref") == provider_id and (in_embedding or not embedding_only):
                return True
            return any(
                _contains(child, in_embedding=in_embedding or key == "embedding")
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(_contains(child, in_embedding=in_embedding) for child in value)
        return False

    locations: list[str] = []
    defaults_path = VaultConfig.application_defaults_path()
    try:
        defaults_raw = _yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, _yaml.YAMLError):
        defaults_raw = {}
    if _contains(defaults_raw):
        locations.append("application defaults")
    for entry in list_vaults():
        try:
            raw = VaultConfig.load_raw(entry.path)
        except Exception:  # noqa: BLE001
            continue
        if _contains(raw):
            locations.append(str(entry.path))
    return locations


async def api_providers_list(request: Request) -> JSONResponse:
    try:
        providers = await store_io(_providers_payload)
    except ValueError as exc:
        return _err(500, "provider_registry_invalid", str(exc))
    return JSONResponse(
        {"status": "ok", "providers": providers},
        headers={"Cache-Control": "no-store"},
    )


def _providers_payload() -> list[dict[str, Any]]:
    from okto_neuron.providers import ProviderRegistry

    registry = ProviderRegistry.load()
    return [_provider_payload(registry, profile) for profile in registry.document.providers]


async def api_provider_types(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "provider_types": await store_io(_provider_types)})


def _provider_types() -> list[dict[str, Any]]:
    """Registry read (YAML) + driver catalog for the provider editor."""
    from okto_neuron.config._vault import _EMBEDDING_PROVIDERS, _LLM_PROVIDERS
    from okto_neuron.onboarding import (
        MANAGED_API_KEY_PROVIDERS,
        MANAGED_EMBEDDING_API_KEY_PROVIDERS,
        PROVIDER_PRESETS,
    )
    from okto_neuron.providers import LOCAL_EXTENDED_DRIVERS, ProviderRegistry

    preset_by_driver = {
        preset.provider: preset
        for preset in PROVIDER_PRESETS
        if preset.provider and preset.key == preset.provider
    }
    registry = ProviderRegistry.load()
    drivers = sorted(_LLM_PROVIDERS | _EMBEDDING_PROVIDERS)
    types = []
    for driver in drivers:
        preset = preset_by_driver.get(driver)
        types.append(
            {
                "driver": driver,
                "label": (preset.label if preset is not None else driver.replace("_", " ").title()),
                "uses": registry.provider_uses(driver),
                "default_api_base": preset.api_base if preset is not None else None,
                "managed_api_key": (
                    driver in MANAGED_API_KEY_PROVIDERS
                    or driver in MANAGED_EMBEDDING_API_KEY_PROVIDERS
                ),
                "local_extended_allowed": driver in LOCAL_EXTENDED_DRIVERS,
            }
        )
    return types


async def api_providers_create(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "provider writes are restricted to loopback callers")
    try:
        payload = await _read_json(request)
        allowed = {
            "name",
            "driver",
            "api_base",
            "allow_remote",
            "credential_id",
            "parameter_mode",
            "request_timeout_s",
        }
        if set(payload) - allowed:
            raise _BadRequest("provider request contains unknown fields")
        name = _require(payload, "name", str)
        driver = _require(payload, "driver", str)
        api_base = payload.get("api_base") or None
        allow_remote = payload.get("allow_remote", True)
        credential_id = payload.get("credential_id") or None
        parameter_mode = payload.get("parameter_mode", "safe")
        request_timeout_s = payload.get("request_timeout_s")
        if api_base is not None and not isinstance(api_base, str):
            raise _BadRequest("invalid field: api_base")
        if not isinstance(allow_remote, bool):
            raise _BadRequest("invalid field: allow_remote")
        if credential_id is not None and not isinstance(credential_id, str):
            raise _BadRequest("invalid field: credential_id")
        if parameter_mode not in {"safe", "local_extended"}:
            raise _BadRequest("parameter_mode must be safe or local_extended")
        if request_timeout_s is not None and (
            not isinstance(request_timeout_s, (int, float))
            or isinstance(request_timeout_s, bool)
            or request_timeout_s <= 0
        ):
            raise _BadRequest("request_timeout_s must be a positive number or null")
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    from okto_neuron.providers import ProviderRegistry

    def _mutate() -> tuple[Any, Any]:
        registry = ProviderRegistry.load()
        profile = registry.add_provider(
            name=name,
            driver=driver,
            api_base=api_base,
            allow_remote=allow_remote,
            credential_id=credential_id,
            parameter_mode=parameter_mode,
            request_timeout_s=request_timeout_s,
        )
        registry.save()
        return registry, profile

    try:
        registry, profile = await _run_provider_registry_mutation(_mutate)
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))
    except OSError:
        _LOG.exception("provider creation failed")
        return _internal_error()
    return JSONResponse(
        {"status": "ok", "provider": _provider_payload(registry, profile)},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def api_provider_update(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "provider writes are restricted to loopback callers")
    try:
        patch = await _read_json(request)
        if "allow_remote" in patch and not isinstance(patch["allow_remote"], bool):
            raise _BadRequest("invalid field: allow_remote")
        request_timeout_s = patch.get("request_timeout_s")
        if (
            "request_timeout_s" in patch
            and request_timeout_s is not None
            and (
                not isinstance(request_timeout_s, (int, float))
                or isinstance(request_timeout_s, bool)
                or request_timeout_s <= 0
            )
        ):
            raise _BadRequest("request_timeout_s must be a positive number or null")
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    provider_id = request.path_params["provider_id"]
    from okto_neuron.providers import ProviderRegistry

    class _EmbeddingProviderInUseError(ValueError):
        pass

    def _mutate() -> tuple[Any, Any]:
        registry = ProviderRegistry.load()
        current = registry.provider(provider_id)
        # A provider connection is reusable identity. Mutating the driver/endpoint
        # underneath an active embedding reference would silently change vector
        # space without passing through Config's explicit re-embed confirmation.
        connection_changed = ("driver" in patch and patch["driver"] != current.driver) or (
            "api_base" in patch and patch["api_base"] != current.api_base
        )
        if connection_changed and _provider_reference_locations(provider_id, embedding_only=True):
            raise _EmbeddingProviderInUseError
        profile = registry.update_provider(provider_id, patch)
        registry.save()
        return registry, profile

    try:
        registry, profile = await _run_provider_registry_mutation(_mutate)
    except _EmbeddingProviderInUseError:
        return _err(
            409,
            "embedding_provider_in_use",
            "this connection is used for embeddings; create a new provider and "
            "switch to it in Config > Embedding so Okto Neuron can confirm and "
            "run the required re-embed",
        )
    except ValueError as exc:
        return _err(404 if "not found" in str(exc) else 400, "bad_request", str(exc))
    except OSError:
        _LOG.exception("provider update failed")
        return _internal_error()
    return JSONResponse(
        {"status": "ok", "provider": _provider_payload(registry, profile)},
        headers={"Cache-Control": "no-store"},
    )


async def api_provider_delete(request: Request) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "provider writes are restricted to loopback callers")
    provider_id = request.path_params["provider_id"]
    references = await store_io(_provider_reference_locations, provider_id)
    if references:
        return _err(
            409,
            "provider_in_use",
            f"provider is referenced by: {', '.join(references)}",
        )
    from okto_neuron.providers import ProviderRegistry

    def _mutate() -> Any:
        registry = ProviderRegistry.load()
        profile = registry.remove_provider(provider_id)
        registry.save()
        return profile

    try:
        profile = await _run_provider_registry_mutation(_mutate)
    except ValueError as exc:
        return _err(404, "not_found", str(exc))
    except OSError:
        _LOG.exception("provider deletion failed")
        return _internal_error()
    return JSONResponse({"status": "ok", "deleted": profile.id})


# --------------------------- /api/v1 — reset (start fresh) ---------------------------


async def api_reset(request: Request) -> JSONResponse:
    """Wipe the vault and start fresh: delete the graph, vault-local derived
    app state, and user content, then re-open an empty vault — preserving
    ``okto-neuron.yaml``. Destructive, irreversible, and a sensitive write:
    always loopback-only (matches config-write)."""
    state = get_state()
    if not isinstance(state, VaultRuntime):
        return _err(409, "no_active_vault", "select a vault before resetting")
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "reset is restricted to loopback callers; use a "
            "loopback-preserving SSH tunnel for remote administration",
        )
    if state.draining:
        return _draining_response()
    blocker = _maintenance_blocker(state)
    if blocker is not None:
        return _err(409, "busy", f"reset is blocked: {blocker}")
    if state.vault_pool.is_fenced(state.vault_path):
        return _err(409, "vault_fenced", "this vault is already fenced for maintenance")

    pool = state.vault_pool
    # Drain first (no await since the checks above); the fence takes the pool
    # lock, so it runs off-loop. Leases taken in between are waited out below.
    state.mark_draining()
    await store_io(pool.fence, state.vault_path)
    released = False
    safe_to_unfence = True
    ownership = None
    try:
        await _wait_for_runtime_leases(state, timeout=_VAULT_DELETE_LEASE_WAIT_S)
        async with state.writer_lock, state.config_lock:
            busy = _runtime_delete_busy(state)
            if any(bool(value) for value in busy.values()):
                state.draining = False
                return _err(409, "busy", "reset is blocked by active work", busy=busy)
            await store_io(pool.release_path, state.vault_path, require_fenced=True)
            released = True
            safe_to_unfence = False
            ownership = await store_io(pool.claim_fenced_ownership, state.vault_path)
            await store_io(wipe_vault, state.vault_path, keep_config=True)
            ownership.require_held()
            reopened = await store_io(Vault.open, state.vault_path)
            try:
                await store_io(state.install_fenced_vault, reopened, ownership=ownership)
                ownership = None
            except Exception:
                await store_io(reopened.close)
                raise
            released = False
            safe_to_unfence = True
            state.reset_after_wipe()
        state.draining = False
        return JSONResponse({"status": "ok", "wiped": True})
    except VaultPoolError as exc:
        state.draining = False
        return _err(409, exc.code, str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("vault reset failed mid-wipe")
        if (
            released
            and await store_io(state.vault_path.exists)
            and pool.peek(state.vault_path) is None
        ):
            try:
                async with state.writer_lock, state.config_lock:
                    if ownership is None:
                        await _open_and_install_fenced(state)
                    else:
                        ownership.require_held()
                        reopened = await store_io(Vault.open, state.vault_path)
                        try:
                            await store_io(
                                state.install_fenced_vault,
                                reopened,
                                ownership=ownership,
                            )
                            ownership = None
                        except Exception:
                            await store_io(reopened.close)
                            raise
                safe_to_unfence = True
                state.draining = False
            except Exception:  # noqa: BLE001
                _LOG.exception("could not restore vault handle after failed reset")
        detail = (
            "vault reset failed; the previous graph handle was restored"
            if safe_to_unfence
            else "vault reset failed after deletion began; restart the server"
        )
        return _err(500, "reset_failed", detail)
    finally:
        if ownership is not None:
            await store_io(ownership.release)
        if safe_to_unfence:
            await store_io(pool.unfence, state.vault_path)


# --------------------------- /api/v1 — query (rich provenance) ---------------------------


def validity_subset(node: Any) -> dict[str, Any]:
    """ADR 0024 validity subset for a node — superseded claims are filtered from
    recall by default, but a caller that does see one (e.g. via subgraph
    expansion or a future include-superseded flag) should see why it's stale,
    not silently treat it as current.

    Factored out of :func:`_serialize_hit` so the MCP ``ask`` surface
    (``server/runtime.py``) surfaces the SAME keys instead of carrying a second,
    drifting copy of this logic. Key order is preserved for REST byte-parity.
    """
    facets = getattr(node, "facets", None) or {}
    out: dict[str, Any] = {}
    if facets.get("_superseded"):
        out["superseded"] = True
        if facets.get("valid_until"):
            out["valid_until"] = facets["valid_until"]
    if facets.get("_detached"):
        out["detached"] = True
        if facets.get("valid_as_of"):
            out["valid_as_of"] = facets["valid_as_of"]
    return out


def _serialize_hit(hit: Any) -> dict[str, Any]:
    node = hit.node
    node_payload: dict[str, Any] = {
        "id": str(node.id),
        "type": str(node.type),
        "name": getattr(node, "name", None) or None,
    }
    node_payload.update(validity_subset(node))
    return {
        "node": node_payload,
        "score": float(hit.score),
        "provenance": hit.provenance.model_dump(mode="json"),
        "context_spans": [s.model_dump(mode="json") for s in getattr(hit, "context_spans", ())],
    }


# --------------------------- /api/v1 — ingest (companion write) ---------------------------


async def api_ingest(request: Request) -> JSONResponse:
    """Paste/write/attach -> usable knowledge. Materializes the posted markdown
    to a durable vault source, then runs the companion (LLM extraction + gate)
    so what comes in is queryable via recall/ask. Ingest is a sensitive write:
    loopback-only, even under ``--allow-remote`` (matches config-write)."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "ingest is restricted to loopback callers; tunnel (e.g. SSH) to ingest remotely",
        )
    try:
        payload = await _read_json(request)
        content = _require(payload, "content", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    if not content.strip():
        return _err(400, "bad_request", "content is empty")
    raw_name = str(payload.get("filename") or payload.get("title") or "")

    async with state.writer_lock:
        if state.draining:
            return _draining_response()
        try:
            target = await store_io(_write_ingest_source, state, raw_name, content)
            filename = target.name
            # Off-load the blocking LLM extraction so the event loop stays
            # responsive; writer_lock still serializes the write.
            result = await job_io(_companion(state).remember, target)
        except IntegrityFenceError as exc:
            return _integrity_fenced_response(exc)
        except IngestError as exc:
            return _graph_write_failed(exc)
        except VaultClosedError as exc:
            return _err(503, "vault_closed", str(exc))
        except OktoNeuronError as exc:
            if isinstance(exc, _GRAPH_WRITE_ERRORS):
                log_graph_write_failure(exc)
            return _err(500, "remember_failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("unexpected ingest failure")
            return _err(500, "internal", f"unexpected server error: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "document_id": result.document_id,
            "filename": filename,
            "committed": result.committed,
            "queued": result.queued,
            "blocks_total": result.blocks_total,
            "nodes_extracted": result.nodes_extracted,
            "edges_extracted": result.edges_extracted,
            "claims_minted": result.claims_minted,
            "provider_error": result.provider_error,
            "outcomes": [o.model_dump(mode="json") for o in result.outcomes],
            "outcome": dict(getattr(result, "outcome", {}) or {}),
        }
    )


def _write_ingest_source(state: ServerState | VaultRuntime, raw_name: str, content: str) -> Path:
    """Store op for ``POST /api/v1/ingest``: integrity gate + durable source copy."""
    graph_integrity.require_write_allowed(state, state.vault)
    sources = state.vault_path / ".marginalia" / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    # Mirrors any directory prefix in the posted name and never
    # clobbers a different document's bytes (a Block anchors to this
    # path); a flat basename here used to overwrite silently.
    target = iq.upload_target_path(sources, raw_name, content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --------------------------- /api/v1 — bulk ingest (folder + upload queue) ---------------------------


def _folder_watch_globs(state: Any) -> tuple[list[str] | None, list[str] | None]:
    """The selected vault's source-selection globs, or ``(None, None)``.

    Shared by ``/ingest-folder`` (which walks the disk) and ``/ingest-batch``
    (which is handed a list), so a customized ``folder_watch`` config applies
    identically to both surfaces. ``None`` means "caller falls back to the
    packaged defaults" — an unreadable config must not silently disable the
    policy on the very vault whose config is broken."""
    from okto_neuron.config import VaultConfig

    if state.vault_path is None:
        return (None, None)
    try:
        fw = VaultConfig.load(state.vault_path).folder_watch
    except Exception:  # noqa: BLE001 — unreadable config falls back to defaults
        return (None, None)
    return (fw.ignore_globs, fw.ignore_dir_globs)


async def api_ingest_folder(request: Request) -> JSONResponse:
    """Point at a local folder, queue every markdown/text file, rip it through
    the companion in the background. Server-side path read (loopback-only): the
    server walks its own disk, like ``/detect-drift`` does with ``corpus_root``."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "ingest is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        raw_path = _require(payload, "path", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    recursive = bool(payload.get("recursive", True))

    root = Path(raw_path).expanduser()
    if not root.is_absolute():
        return _err(400, "bad_request", "path must be absolute")
    walk_stats: dict = {}
    try:
        found = await store_io(_discover_ingest_folder, state, root, recursive, walk_stats)
    except _ApiError as exc:
        return exc.response()
    except OSError as exc:
        return _err(400, "bad_request", f"could not read folder: {exc}")
    if not found:
        return _err(400, "bad_request", "no .md/.markdown/.txt files found under that folder")
    truncated = len(found) > iq.MAX_ENQUEUE
    found = found[: iq.MAX_ENQUEUE]

    sources = state.vault_path / ".marginalia" / "sources"
    enqueue_stats: dict = {}
    queued_items = await iq.enqueue_paths_async(
        state, found, sources, rel_root=root, stats=enqueue_stats
    )
    iq.ensure_worker(state, _companion)
    snap = iq.snapshot(state)
    snap["enqueued"] = len(queued_items)
    snap["refreshed"] = int(enqueue_stats.get("refreshed", 0))
    snap["truncated"] = truncated
    snap["skipped_non_text"] = int(walk_stats.get("skipped_non_text", 0))
    return JSONResponse(snap)


def _discover_ingest_folder(
    state: ServerState | VaultRuntime, root: Path, recursive: bool, walk_stats: dict
) -> list[Path]:
    """Store op: probe the folder, read the vault's exclusion policy, walk it."""
    if not root.exists() or not root.is_dir():
        raise _ApiError(404, "not_found", f"folder not found: {root}")
    # Enumerate with the vault's own folder_watch exclusion policy so a
    # customized ignore_globs/ignore_dir_globs keeps one-shot ingest and the
    # continuous watcher identical (falls back to packaged defaults).
    fw_ignore_globs, fw_ignore_dir_globs = _folder_watch_globs(state)
    return iq.discover_folder(
        root,
        recursive=recursive,
        ignore_globs=fw_ignore_globs,
        ignore_dir_globs=fw_ignore_dir_globs,
        stats=walk_stats,
    )


# --------------------------- /api/v1 — folder-watch (ADR 0025) ---------------------------


async def api_folder_watch_status(request: Request) -> JSONResponse:
    """Live folder-watch status for the request-selected vault and all roots.

    Stat-only: reads the in-memory snapshot the watch loop writes each tick and
    never opens a graph (ADR 0025).
    """
    from okto_neuron.server._folder_watch import get_watch_status

    state = get_state()
    if state.shutting_down:
        return _draining_response()

    snapshot = get_watch_status()
    vaults: dict[str, dict] = dict(snapshot)

    # Always include the selected runtime, even if the loop has not ticked for it
    # yet (e.g. just enabled, daemon just started).
    selected = str(state.vault_path) if state.vault_path else None
    if selected and selected not in vaults:
        vaults[selected] = get_watch_status(selected)

    return JSONResponse({"status": "ok", "selected_vault": selected, "vaults": vaults})


async def api_folder_watch_roots_add(request: Request) -> JSONResponse:
    """Add a root to the selected vault's ``folder_watch.roots`` and enable
    folder-watch. Loopback-only, mirrors ``okto-neuron watch-folder add``."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "folder-watch config is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        raw_path = _require(payload, "path", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    root, root_is_dir = await store_io(_resolve_watch_root, raw_path)
    if not root_is_dir:
        return _err(404, "not_found", f"folder not found: {root}")

    # config_lock, NOT writer_lock: this only PATCHes okto-neuron.yaml
    # (folder_watch.roots), same underlying write as api_config_patch — an
    # in-flight ingest/rebuild/heal holding writer_lock for minutes+ must not
    # stall this Save button too (see ServerState.config_lock docstring).
    async with state.config_lock:
        if state.draining:
            return _draining_response()
        try:
            updated = await store_io(_patch_watch_roots, state, str(root), True)
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected folder-watch root-add failure")
            return _internal_error()

    return JSONResponse(
        {"status": "ok", "folder_watch": updated.folder_watch.model_dump(mode="json")}
    )


async def api_folder_watch_roots_remove(request: Request) -> JSONResponse:
    """Remove a root from the selected vault's ``folder_watch.roots``."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "folder-watch config is restricted to loopback callers")
    try:
        payload = await _read_json(request)
        raw_path = _require(payload, "path", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    root, _root_is_dir = await store_io(_resolve_watch_root, raw_path)

    # config_lock, NOT writer_lock — see api_folder_watch_roots_add.
    async with state.config_lock:
        if state.draining:
            return _draining_response()
        try:
            updated = await store_io(_patch_watch_roots, state, str(root), False)
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
        except Exception:  # noqa: BLE001
            _LOG.exception("unexpected folder-watch root-remove failure")
            return _internal_error()

    return JSONResponse(
        {"status": "ok", "folder_watch": updated.folder_watch.model_dump(mode="json")}
    )


def _resolve_watch_root(raw_path: str) -> tuple[Path, bool]:
    root = Path(raw_path).expanduser().resolve()
    return root, root.is_dir()


def _patch_watch_roots(state: ServerState | VaultRuntime, root_str: str, add: bool) -> Any:
    """Store op: add (and enable) or remove one ``folder_watch.roots`` entry."""
    from okto_neuron.config import VaultConfig

    cfg = _load_vault_config(state)
    if add:
        roots = list(cfg.folder_watch.roots)
        if root_str not in roots:
            roots.append(root_str)
        patch: dict[str, Any] = {"folder_watch": {"roots": roots, "enabled": True}}
    else:
        roots = [r for r in cfg.folder_watch.roots if r != root_str]
        patch = {"folder_watch": {"roots": roots}}
    updated, _changed = VaultConfig.apply_patch(state.vault_path, patch)
    return updated


# Per-file upload size cap (chars) — a backstop so a single huge paste/upload
# can't balloon the queue's in-memory footprint.
_MAX_UPLOAD_CHARS = 2_000_000
# Upper bound on the per-file skip report echoed back to the operator. The
# COUNTS are always exact; only the itemized list is capped so a 2000-file
# rejection can't return a megabyte of JSON.
_MAX_REPORTED_SKIPS = 200
# Human labels for the reason codes, used to build the "nothing ingestible"
# detail string the UI shows verbatim.
_SKIP_REASON_LABELS = {
    "ignored_dir": "in an excluded directory",
    "denylisted": "tooling/scaffolding files",
    "ignored_glob": "matched an ignore glob",
    "non_text_suffix": "not markdown/text",
    "empty": "empty",
}


async def api_ingest_batch(request: Request) -> JSONResponse:
    """Queue uploaded files (browser drag-drop). Each ``{filename, content}`` is
    materialized to the durable sources dir, then drained by the worker.

    Applies the SAME source-selection policy ``/ingest-folder`` applies, via the
    shared ``_folder_watch`` predicate. This endpoint takes a client-supplied
    LIST, so it cannot reuse the folder walk — it reuses the walk's per-path
    predicate instead (ADR 0025 F1: one policy, so exclusion behaves identically
    everywhere; ADR 0026: the two source selections must agree). Before this,
    the only filter on the Web UI's "Choose a folder" button was a browser-side
    regex, and a folder ``/ingest-folder`` reduced to 76 files was queued here as
    168 — 51% dot-directory scaffolding and agent tooling notes.

    Rejections are REPORTED, never silently dropped: the response carries exact
    counts plus a per-file ``skipped`` list with a reason code."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "ingest is restricted to loopback callers")
    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return _err(400, "bad_request", "files must be a non-empty list")
    if len(raw_files) > iq.MAX_ENQUEUE:
        return _err(400, "bad_request", f"too many files (max {iq.MAX_ENQUEUE})")

    fw_ignore_globs, fw_ignore_dir_globs = await store_io(_folder_watch_globs, state)
    files: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    skip_counts: Counter[str] = Counter()

    def _skip(name: str, reason: str) -> None:
        skip_counts[reason] += 1
        if len(skipped) < _MAX_REPORTED_SKIPS:
            skipped.append({"filename": name, "reason": reason})

    for entry in raw_files:
        if not isinstance(entry, dict):
            return _err(400, "bad_request", "each file must be an object")
        raw_name = str(entry.get("filename") or "")
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            # Reported, not silent: an empty file the operator meant to import
            # is exactly the kind of quiet drop that hides a bad selection.
            _skip(raw_name, "empty")
            continue
        if len(content) > _MAX_UPLOAD_CHARS:
            return _err(400, "bad_request", "a file exceeds the size limit")
        reason = iq.classify_upload_name(
            raw_name,
            ignore_globs=fw_ignore_globs,
            ignore_dir_globs=fw_ignore_dir_globs,
        )
        if reason is not None:
            _skip(raw_name, reason)
            continue
        files.append((raw_name, content))

    skipped_non_text = skip_counts["non_text_suffix"]
    skipped_empty = skip_counts["empty"]
    skipped_excluded = sum(
        count for reason, count in skip_counts.items() if reason not in ("non_text_suffix", "empty")
    )
    if not files:
        # Nothing survived the policy. Say exactly why — the owner just lost a
        # run to over-inclusion nobody could see, so the failure has to name
        # what it rejected instead of a generic "nothing to ingest".
        breakdown = ", ".join(
            f"{count} {_SKIP_REASON_LABELS.get(reason, reason)}"
            for reason, count in sorted(skip_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        detail = "no ingestible files to queue"
        if breakdown:
            detail = f"{detail}: {breakdown}"
        return _err(
            400,
            "bad_request",
            detail,
            skipped=skipped,
            skipped_non_text=skipped_non_text,
            skipped_excluded=skipped_excluded,
            skipped_empty=skipped_empty,
        )

    # Materializing source files to disk is not a vault write — don't hold the
    # writer lock (that would stall enqueue behind the worker's current file).
    sources = state.vault_path / ".marginalia" / "sources"
    queued_items = await iq.enqueue_uploads_async(state, files, sources)
    iq.ensure_worker(state, _companion)
    snap = iq.snapshot(state)
    snap["enqueued"] = len(queued_items)
    snap["enqueued_item_ids"] = [item.id for item in queued_items]
    # Mirrors /ingest-folder's skipped_non_text, plus the policy exclusions that
    # only this surface can produce (the walk never yields them at all).
    snap["skipped_non_text"] = skipped_non_text
    snap["skipped_excluded"] = skipped_excluded
    snap["skipped_empty"] = skipped_empty
    snap["skipped"] = skipped
    return JSONResponse(snap)


async def api_ingest_queue(request: Request) -> JSONResponse:
    """Live queue status for the UI poll (read-only)."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    return JSONResponse(iq.snapshot(state))


async def api_ingest_queue_item(request: Request) -> JSONResponse:
    """Rich per-file ingest details for the selected queue row."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    item_id = request.path_params.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return _err(400, "bad_request", "missing item id")
    detail = await iq.item_detail_async(state, item_id)
    if detail is None:
        return _err(404, "not_found", f"ingest queue item not found: {item_id}")
    # Up to ~80 events of up to 12 KB text each: encode off the loop.
    return json_bytes_response(await store_io(encode_json, detail))


async def api_ingest_queue_retry(request: Request) -> JSONResponse:
    """Re-enqueue an errored queue item (loopback-gated like other writes)."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "ingest-queue retry is restricted to loopback callers; use a "
            "loopback-preserving SSH tunnel for remote administration",
        )
    item_id = request.path_params.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return _err(400, "bad_request", "missing item id")
    candidate = iq._find_item(state, item_id)
    if candidate is not None:
        # The live-graph receipt check reads the store: off-loop first, then the
        # re-queue itself stays on the loop with the drain worker's flips.
        await iq.verify_receipt_async(state, candidate)
    item, error = iq.retry_item(state, item_id, verify=False, persist_now=False)
    if error is None:
        await store_io(iq.persist, state)
    if error == "not_found":
        return _err(404, "not_found", f"ingest queue item not found: {item_id}")
    if error == "conflict":
        status = item.status if item is not None else "unknown"
        return _err(
            409,
            "conflict",
            f"only errored or provider-degraded items can be retried (status: {status})",
        )
    iq.ensure_worker(state, _companion)
    return JSONResponse(iq.snapshot(state))


async def api_ingest_queue_delete(request: Request) -> JSONResponse:
    """Remove a terminal (done/error/cancelled) item from the queue history."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "ingest-queue delete is restricted to loopback callers; use a "
            "loopback-preserving SSH tunnel for remote administration",
        )
    item_id = request.path_params.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return _err(400, "bad_request", "missing item id")
    item, error = iq.delete_item(state, item_id, persist_now=False)
    if error is None:
        await store_io(iq.persist, state)
    if error == "not_found":
        return _err(404, "not_found", f"ingest queue item not found: {item_id}")
    if error == "conflict":
        status = item.status if item is not None else "unknown"
        return _err(
            409,
            "conflict",
            f"only terminal (done/error/cancelled) items can be removed (status: {status})",
        )
    return JSONResponse(iq.snapshot(state))


async def api_ledger_runs(request: Request) -> JSONResponse:
    """Durable ADR 0013 candidate-ledger run list (a whole-ledger read: concurrent
    polls of the same vault and limit share one scan)."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(int(raw_limit), 500))
    except ValueError:
        return _err(400, "bad_request", "limit must be an integer")
    body = await single_flight(
        ("ledger_runs", str(state.vault_path), limit), _ledger_runs, state.vault_path, limit
    )
    return json_bytes_response(body)


def _ledger_runs(vault_path: Path, limit: int) -> bytes:
    """Store op: the encoded response body (a whole-ledger read)."""
    from okto_neuron.consolidate.ledger import CandidateLedger

    runs = CandidateLedger(Path(vault_path) / ".marginalia").run_summaries(limit=limit)
    return encode_json({"status": "ok", "runs": runs})


async def api_ledger_run_detail(request: Request) -> JSONResponse:
    """Durable ADR 0013 candidate-ledger detail for one run."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    run_id = request.path_params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _err(400, "bad_request", "missing run id")
    body = await store_io(_ledger_run_detail, state.vault_path, run_id)
    if body is None:
        return _err(404, "not_found", f"ledger run not found: {run_id}")
    return json_bytes_response(body)


def _ledger_run_detail(vault_path: Path, run_id: str) -> bytes | None:
    """Store op: the encoded response body, or ``None`` for an unknown run."""
    from okto_neuron.consolidate.ledger import CandidateLedger

    detail = CandidateLedger(Path(vault_path) / ".marginalia").run_detail(run_id)
    return None if detail is None else encode_json({"status": "ok", **detail})


async def api_ledger_summary(request: Request) -> JSONResponse:
    """Compact ADR 0013 ledger progress for committed-vs-pending UI surfaces (a
    whole-ledger read: concurrent identical polls share one scan)."""
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    raw_limit = request.query_params.get("limit", "12")
    try:
        limit = max(1, min(int(raw_limit), 50))
    except ValueError:
        return _err(400, "bad_request", "limit must be an integer")
    raw_run_id = request.query_params.get("run_id")
    run_id = str(raw_run_id) if raw_run_id else None
    body = await single_flight(
        ("ledger_summary", str(state.vault_path), run_id, limit),
        _ledger_summary,
        state.vault_path,
        run_id,
        limit,
    )
    return json_bytes_response(body)


def _ledger_summary(vault_path: Path, run_id: str | None, limit: int) -> bytes:
    """Store op: the encoded response body (a whole-ledger read)."""
    from okto_neuron.consolidate.ledger import CandidateLedger

    summary = CandidateLedger(Path(vault_path) / ".marginalia").run_progress_summary(
        run_id, limit=limit
    )
    if summary is None:
        return encode_json({"status": "ok", "run": None})
    return encode_json({"status": "ok", **summary})


async def api_ingest_cancel(request: Request) -> JSONResponse:
    """Cooperatively stop bulk ingest at the next safe pre-commit checkpoint."""
    state = get_state()
    if state.draining:
        return _draining_response()
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "ingest cancellation is restricted to loopback callers")
    snap = iq.cancel(state, persist_now=False)
    await store_io(iq.persist, state)
    return JSONResponse(snap)


async def api_recall(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        text = _require(payload, "query", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    k = payload.get("k", 10)
    if not isinstance(k, int) or k < 1:
        return _err(400, "bad_request", "missing or invalid field: k")
    k_cap_err = _k_cap_error(k)
    if k_cap_err is not None:
        return k_cap_err

    try:
        # Store read: vector search plus one short query embedding.
        hits, metrics = await store_io(_query_with_recall_cost, state.vault, text, k=k)
    except QueryError as exc:
        return _err(500, "query_failed", str(exc))
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "query_failed", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected recall failure")
        return _internal_error()

    return JSONResponse(
        {
            "status": "ok",
            "hits": [_serialize_hit(h) for h in hits],
            "recall_cost": metrics,
        }
    )


async def api_ask(request: Request) -> JSONResponse:
    state = get_state()
    if state.shutting_down:
        return _draining_response()
    try:
        payload = await _read_json(request)
        question = _require(payload, "question", str)
        retrieval_policy = _parse_ask_retrieval_policy(payload)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    # ask is one-shot: seed wider than recall (k=10). k=20 = grounded-golden
    # answer-presence ceiling (0.677 -> 0.754), neg-control abstention held.
    k = payload.get("k", 20)
    if not isinstance(k, int) or k < 1:
        return _err(400, "bad_request", "missing or invalid field: k")
    if retrieval_policy and retrieval_policy.seed_k is not None:
        k = retrieval_policy.seed_k
    k_cap_err = _k_cap_error(k)
    if k_cap_err is not None:
        return k_cap_err

    try:
        # Off-load the blocking LLM answer synthesis so the event loop stays
        # responsive while retrieval + generation runs.
        answer = await job_io(
            _companion(state).ask,
            question,
            k=k,
            retrieval_policy=retrieval_policy,
        )
    except VaultClosedError as exc:
        return _err(503, "vault_closed", str(exc))
    except OktoNeuronError as exc:
        return _err(500, "ask_failed", str(exc))
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected ask failure")
        return _internal_error()

    from okto_neuron.companion import ask_status

    # Same rule as the bare /ask route and the MCP tool: "ok" only for a clean
    # answer, "degraded" otherwise, with the reason in retrieval.synthesis_status.
    retrieval = dict(answer.retrieval)
    return JSONResponse(
        {
            "status": ask_status(retrieval),
            "text": answer.text,
            "citations": list(answer.citations),
            "hits": [_serialize_hit(h) for h in getattr(answer, "hits", ())],
            "retrieval": retrieval,
        }
    )


# --------------------------- /api/v1/llm/test ---------------------------


async def api_llm_test(request: Request) -> JSONResponse:
    """Probe an LLM endpoint for reachability and model listing.

    Loopback-gated with the same guard as config-PATCH/reset (``remote_config_allowed``).
    The API key is resolved from the environment at call-time and is NEVER echoed in the
    response body, error string, or logs.

    Request body: {provider, model?, api_base?, api_key_env?}
    Response:     {ok: bool, models: list[str], error: str|null}
    """
    # L1 / M1: only loopback callers may probe.  LoopbackHostMiddleware already
    # caught spoofed Host headers (returns forbidden_host before we get here); this
    # second check gates on peer IP so host-header spoofing at the TCP layer is also
    # blocked.
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "llm/test is restricted to loopback callers",
        )

    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    provider_ref = payload.get("provider_ref") or None
    if provider_ref is not None and not isinstance(provider_ref, str):
        return _err(400, "bad_request", "invalid field: provider_ref")
    provider = payload.get("provider", "")
    provider_allow_remote: bool | None = None
    if provider_ref is not None:
        try:
            profile, managed_api_key_env, uses = await store_io(
                _provider_ref_profile, provider_ref
            )
            if "llm" not in uses:
                raise ValueError(f"provider {provider_ref!r} does not support LLM calls")
            provider = profile.driver
            provider_allow_remote = profile.allow_remote
            payload["api_base"] = profile.api_base
            payload["api_key_env"] = managed_api_key_env
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
    if not isinstance(provider, str) or not provider:
        return _err(400, "bad_request", "missing or invalid field: provider")

    # Stub short-circuit — CI / UI default / smoke tests; no network call needed.
    if provider == "stub":
        return JSONResponse({"ok": True, "models": ["stub"], "error": None})

    # claude_cli: no HTTP endpoint to probe — check the local binary instead.
    # The model list is a hint (aliases); any --model value is accepted verbatim.
    if provider in ("claude_cli", "claude-code", "claude_code"):
        import shutil as _shutil
        import subprocess as _subprocess

        binary = _shutil.which("claude")
        if binary is None:
            return JSONResponse(
                {"ok": False, "models": [], "error": "claude CLI not found on PATH"}
            )
        try:
            proc = await job_io(
                _subprocess.run,
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm/test claude CLI probe failed: %s", type(exc).__name__)
            return JSONResponse({"ok": False, "models": [], "error": "claude CLI probe failed"})
        if proc.returncode != 0:
            return JSONResponse(
                {
                    "ok": False,
                    "models": [],
                    "error": f"claude CLI exited {proc.returncode}",
                }
            )
        return JSONResponse(
            {"ok": True, "models": ["sonnet", "haiku", "opus", "fable"], "error": None}
        )

    # pi_cli / codex_cli: also no HTTP endpoint to probe — reuse the same
    # subprocess-based discovery onboarding uses, so Test surfaces real models.
    if provider in ("pi_cli", "pi"):
        from okto_neuron.onboarding import _discover_pi_cli_models

        result = await job_io(_discover_pi_cli_models, timeout=10.0)
        if not result.models and result.error:
            return JSONResponse({"ok": False, "models": [], "error": result.error})
        return JSONResponse({"ok": True, "models": result.models, "error": None})

    if provider in ("codex_cli", "codex"):
        from okto_neuron.onboarding import _discover_codex_cli_models

        result = await job_io(_discover_codex_cli_models)
        if not result.models and result.error:
            return JSONResponse({"ok": False, "models": [], "error": result.error})
        return JSONResponse({"ok": True, "models": result.models, "error": None})

    api_base: str | None = payload.get("api_base") or None
    api_key_env: str | None = payload.get("api_key_env") or None

    # H1: validate api_key_env against the namespace allowlist BEFORE touching
    # os.environ.  Without this, a CSRF-to-loopback could set api_key_env to
    # "AWS_SECRET_ACCESS_KEY" and exfiltrate the secret via a controlled api_base.
    if api_key_env is not None:
        from okto_neuron.config._vault import _check_api_key_env as _cfg_check_api_key_env

        try:
            _cfg_check_api_key_env(api_key_env)
        except ValueError as exc:
            return JSONResponse({"ok": False, "models": [], "error": str(exc)})

    # Determine effective api_base (body overrides vault config default).
    state = get_state()
    try:
        cfg = await store_io(_load_vault_config, state)
        cfg_allow_remote: bool = (
            provider_allow_remote if provider_allow_remote is not None else cfg.llm.allow_remote
        )
        effective_api_base: str = api_base or cfg.llm.defaults.api_base
    except Exception:  # noqa: BLE001
        cfg_allow_remote = provider_allow_remote or False
        effective_api_base = api_base or "http://127.0.0.1:8123/v1"

    # SSRF guard: reject non-loopback targets unless allow_remote is explicitly set.
    from okto_neuron.config._vault import _check_api_base as _cfg_check_api_base

    try:
        _cfg_check_api_base(effective_api_base, cfg_allow_remote)
    except ValueError as exc:
        return JSONResponse({"ok": False, "models": [], "error": str(exc)})

    if provider == "bedrock":
        from okto_neuron.llm import (
            LLMProviderError,
        )
        from okto_neuron.llm import (
            _preflight_provider_optional_dependencies as _llm_provider_preflight,
        )

        try:
            _llm_provider_preflight(provider)
        except LLMProviderError as exc:
            return JSONResponse({"ok": False, "models": [], "error": str(exc)})

    # Resolve API key from environment — never echo the secret back.
    api_key: str | None = None
    if api_key_env:
        api_key = _secret_env(api_key_env)

    # Known onboarding presets own provider-specific discovery auth and URL
    # shapes (Anthropic/Gemini/OpenRouter/LiteLLM Proxy). Only use a preset for
    # its exact endpoint; a custom endpoint must receive the generic
    # OpenAI-compatible Bearer + /models probe below.
    from okto_neuron.onboarding import PROVIDER_PRESETS, discover_models

    normalized_base = effective_api_base.rstrip("/")
    discovery_preset = next(
        (
            preset
            for preset in PROVIDER_PRESETS
            if preset.provider == provider
            and preset.api_base
            and preset.api_base.rstrip("/") == normalized_base
            and preset.discovery not in {"auto", "none", "pi_cli", "codex_cli"}
        ),
        None,
    )
    if discovery_preset is not None:
        result = await job_io(
            discover_models,
            discovery_preset,
            api_base=effective_api_base,
            api_key=api_key,
            timeout=5.0,
            allow_remote=cfg_allow_remote,
            remote_confirmed=cfg_allow_remote,
        )
        return JSONResponse(
            {
                "ok": result.error is None,
                "models": result.models,
                "error": result.error,
            }
        )

    # Probe the canonical model list with a tight timeout. OpenAI-compatible
    # endpoints (the ones this GET-shape probe targets) are resolved through
    # the shared base-URL resolver so the server-root and /v1 input forms hit
    # the same {root}/v1/models the completion path implies; providers with
    # their own URL shapes keep the legacy probe shape unchanged.
    import httpx

    from okto_neuron.config._vault import _check_provider as _cfg_check_provider
    from okto_neuron.providers import (
        OPENAI_V1_DISCOVERY_ONLY_DRIVERS,
        OPENAI_V1_DRIVERS,
        resolve_openai_base,
    )

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        probe_provider = _cfg_check_provider(provider) or provider
    except ValueError:
        probe_provider = provider  # unknown driver: keep the legacy probe shape
    if probe_provider in OPENAI_V1_DRIVERS | OPENAI_V1_DISCOVERY_ONLY_DRIVERS:
        probe_url = resolve_openai_base(effective_api_base).models_url
    else:
        probe_url = f"{effective_api_base.rstrip('/')}/models"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(probe_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            model_ids = [
                entry.get("id", "") for entry in data.get("data", []) if isinstance(entry, dict)
            ]
            model_ids = [m for m in model_ids if m]
    except httpx.HTTPStatusError as exc:
        _LOG.warning(
            "llm/test HTTP error from %s: %s",
            effective_api_base,
            exc.response.status_code,
        )
        return JSONResponse(
            {"ok": False, "models": [], "error": f"HTTP {exc.response.status_code}"}
        )
    except httpx.TimeoutException:
        _LOG.warning("llm/test probe timed out: %s", effective_api_base)
        return JSONResponse({"ok": False, "models": [], "error": "request timed out"})
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("llm/test probe failed (%s): %s", effective_api_base, type(exc).__name__)
        return JSONResponse({"ok": False, "models": [], "error": "probe failed"})

    return JSONResponse({"ok": True, "models": model_ids, "error": None})


# --------------------------- /api/v1/llm/test-completion ---------------------------

_TEST_COMPLETION_PROMPT = "Reply with exactly one word: pong"
_TEST_COMPLETION_TIMEOUT_S = 60.0


def _redact_provider_error(error: BaseException, api_key_env: str | None) -> str:
    text = str(error)
    secret = _secret_env(api_key_env) if api_key_env else None
    if secret:
        text = text.replace(secret, "[redacted]")
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", text)


async def api_llm_test_completion(request: Request) -> JSONResponse:
    """Round-trip a real completion through the configured provider/model.

    ``/llm/test`` (reachability + model listing) cannot catch a wrong model id,
    a misconfigured provider auth, or a CLI provider that's on PATH but broken
    end to end — those only surface on an actual call. This sends one trivial
    prompt through the exact same :func:`okto_neuron.llm.get_provider` path
    every real ask/ingest call uses and returns the reply.

    Loopback-gated like ``/llm/test``. The API key is resolved from the
    environment at call-time and is NEVER echoed in the response body, error
    string, or logs.

    Request body: {provider, model, api_base?, api_key_env?}
    Response:     {ok: bool, reply: str|null, error: str|null, duration_s: float|null}
    """
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "llm/test-completion is restricted to loopback callers",
        )

    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    provider_ref = payload.get("provider_ref") or None
    if provider_ref is not None and not isinstance(provider_ref, str):
        return _err(400, "bad_request", "invalid field: provider_ref")
    provider = payload.get("provider", "")
    parameter_mode = payload.get("parameter_mode", "auto")
    provider_allow_remote: bool | None = None
    provider_request_timeout_s: float | None = None
    if provider_ref is not None:
        try:
            profile, managed_api_key_env, uses = await store_io(
                _provider_ref_profile, provider_ref
            )
            if "llm" not in uses:
                raise ValueError(f"provider {provider_ref!r} does not support LLM calls")
            provider = profile.driver
            provider_allow_remote = profile.allow_remote
            payload["api_base"] = profile.api_base
            payload["api_key_env"] = managed_api_key_env
            parameter_mode = profile.parameter_mode
            provider_request_timeout_s = profile.request_timeout_s
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))
    model = payload.get("model", "")
    if not isinstance(provider, str) or not provider:
        return _err(400, "bad_request", "missing or invalid field: provider")
    if not isinstance(model, str) or not model:
        return _err(400, "bad_request", "missing or invalid field: model")

    api_base: str | None = payload.get("api_base") or None
    api_key_env: str | None = payload.get("api_key_env") or None

    from okto_neuron.config._vault import _check_api_key_env as _cfg_check_api_key_env

    if api_key_env is not None:
        try:
            _cfg_check_api_key_env(api_key_env)
        except ValueError as exc:
            return JSONResponse({"ok": False, "reply": None, "error": str(exc), "duration_s": None})

    # Determine effective api_base (body overrides vault config default) — same
    # SSRF-safe resolution as /llm/test. CLI providers ignore api_base entirely,
    # but ResolvedLLM requires the field, so a harmless default is used for them.
    state = get_state()
    try:
        cfg = await store_io(_load_vault_config, state)
        cfg_allow_remote: bool = (
            provider_allow_remote if provider_allow_remote is not None else cfg.llm.allow_remote
        )
        effective_api_base: str = api_base or cfg.llm.defaults.api_base
    except Exception:  # noqa: BLE001
        cfg_allow_remote = provider_allow_remote or False
        effective_api_base = api_base or "http://127.0.0.1:8123/v1"

    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.config._vault import _check_api_base as _cfg_check_api_base

    _CLI_PROVIDERS = (
        "stub",
        "claude_cli",
        "claude-code",
        "claude_code",
        "pi_cli",
        "pi",
        "codex_cli",
        "codex",
    )
    if provider not in _CLI_PROVIDERS:
        try:
            _cfg_check_api_base(effective_api_base, cfg_allow_remote)
        except ValueError as exc:
            return JSONResponse({"ok": False, "reply": None, "error": str(exc), "duration_s": None})

    if provider == "bedrock":
        from okto_neuron.llm import (
            LLMProviderError,
        )
        from okto_neuron.llm import (
            _preflight_provider_optional_dependencies as _llm_provider_preflight,
        )

        try:
            _llm_provider_preflight(provider)
        except LLMProviderError as exc:
            return JSONResponse({"ok": False, "reply": None, "error": str(exc), "duration_s": None})

    try:
        resolved = ResolvedLLM(
            provider_ref=provider_ref,
            parameter_mode=parameter_mode,
            provider=provider,
            api_base=effective_api_base,
            model=model,
            api_key_env=api_key_env,
            request_timeout_s=provider_request_timeout_s,
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            top_k=payload.get("top_k"),
            min_p=payload.get("min_p"),
            presence_penalty=payload.get("presence_penalty"),
            enable_thinking=payload.get("enable_thinking"),
            parameters=payload.get("parameters", {}),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, bad provider, etc.
        return JSONResponse({"ok": False, "reply": None, "error": str(exc), "duration_s": None})

    from okto_neuron.llm import (
        LLMProviderError,
        Message,
        get_provider,
        last_parameter_plan,
    )

    # This endpoint is an explicitly bounded connectivity probe.  When the
    # reusable provider has no normal request deadline, give this one probe the
    # same disclosed budget as the HTTP await so its worker thread cannot outlive
    # the response and exhaust the server executor under repeated tests.
    probe_resolved = (
        resolved
        if resolved.request_timeout_s is not None
        else resolved.model_copy(update={"request_timeout_s": _TEST_COMPLETION_TIMEOUT_S})
    )

    def _run_completion() -> tuple[str, dict[str, object] | None]:
        llm_provider = get_provider(probe_resolved)
        reply = llm_provider.complete(
            [Message(role="user", content=_TEST_COMPLETION_PROMPT)],
            max_tokens=probe_resolved.max_tokens,
            temperature=probe_resolved.temperature,
            top_p=probe_resolved.top_p,
            top_k=probe_resolved.top_k,
            min_p=probe_resolved.min_p,
            presence_penalty=probe_resolved.presence_penalty,
            enable_thinking=probe_resolved.enable_thinking,
        )
        return reply, last_parameter_plan()

    started = time.monotonic()
    try:
        reply, parameter_plan = await asyncio.wait_for(
            job_io(_run_completion), timeout=_TEST_COMPLETION_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {
                "ok": False,
                "reply": None,
                "error": f"completion timed out after {_TEST_COMPLETION_TIMEOUT_S:.0f}s",
                "duration_s": round(time.monotonic() - started, 1),
            }
        )
    except LLMProviderError as exc:
        safe_error = _redact_provider_error(exc, api_key_env)
        return JSONResponse(
            {
                "ok": False,
                "reply": None,
                "error": safe_error,
                "duration_s": round(time.monotonic() - started, 1),
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface any other provider failure
        safe_error = _redact_provider_error(exc, api_key_env)
        _LOG.warning("llm/test-completion failed for %s/%s: %s", provider, model, safe_error)
        return JSONResponse(
            {
                "ok": False,
                "reply": None,
                "error": f"{type(exc).__name__}: {safe_error}",
                "duration_s": round(time.monotonic() - started, 1),
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "reply": reply,
            "error": None,
            "duration_s": round(time.monotonic() - started, 1),
            "parameter_plan": parameter_plan,
        }
    )


# --------------------------- /api/v1/embedding (reembed + test) ---------------------------

_REEMBED_STATE_FILE = "reembed.state.json"
_REBUILD_STATE_FILE = "rebuild.state.json"  # ADR 0009 P3 in-process rebuild/heal progress
_ROLLBACK_STATE_FILE = "rollback.state.json"
# In-process embedding providers that need no network probe.
_LOCAL_EMBEDDING_PROVIDERS = frozenset({"stub", "fastembed", "sentence-transformers"})


async def api_embedding_reembed(request: Request) -> JSONResponse:
    """Start a vectors-only re-embed: recompute every vector at the configured
    embedding width, no LLM re-extraction. Heavy and rebuilds the graph in place, so
    it is always loopback-only (matches config-write/reset).

    Returns ``202 {started}`` immediately and runs in the background; poll
    ``GET /api/v1/embedding/reembed/status`` for progress. Runs ONLY on this explicit
    action — a config PATCH never triggers it (PATCH just reports ``applied:reembed``).
    """
    state = get_state()
    if not isinstance(state, VaultRuntime):
        return _err(409, "no_active_vault", "select a vault before reembedding")
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "reembed is restricted to loopback callers; use a "
            "loopback-preserving SSH tunnel for remote administration",
        )
    if state.draining or state.vault_reembed_active:
        return _err(409, "busy", "a vault-wide operation is already in progress")
    busy = _runtime_delete_busy(state)
    if any(bool(value) for value in busy.values()):
        return _err(409, "busy", "this vault has active work", busy=busy)
    if state.vault_pool.is_fenced(state.vault_path):
        return _err(409, "vault_fenced", "this vault is already fenced for maintenance")

    # Claim + drain with no await since the checks; the fence takes the pool
    # lock, so it runs off-loop before 202 is returned.
    state.mark_draining()
    state.vault_reembed_active = True
    state.vault_reembed_path = str(state.vault_path)
    await store_io(state.vault_pool.fence, state.vault_path)
    try:
        _start_owned_maintenance(
            state,
            lambda: _run_runtime_reembed(state),
            name="okto-neuron-reembed",
        )
    except Exception:  # noqa: BLE001
        state.vault_reembed_active = False
        state.draining = False
        await store_io(state.vault_pool.unfence, state.vault_path)
        raise
    return JSONResponse({"status": "started", "vault": str(state.vault_path)}, status_code=202)


async def api_embedding_reembed_status(request: Request) -> JSONResponse:
    """Report the latest reembed phase from ``.marginalia/reembed.state.json``.

    Read-only and available even while draining (the UI polls this during a job).
    """
    state = get_state()
    if not isinstance(state, VaultRuntime):
        return _err(409, "no_active_vault", "select a vault to inspect reembed status")
    return JSONResponse(await store_io(_reembed_status_payload, state, state.vault_path))


async def api_embedding_models(request: Request) -> JSONResponse:
    """Discover embedding model aliases from a reusable provider connection."""

    if not remote_config_allowed(request):
        return _err(403, "forbidden", "embedding/models is restricted to loopback callers")
    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    allowed_fields = {
        "provider_ref",
        "provider",
        "api_base",
        "api_key_env",
        "allow_remote",
    }
    if set(payload) - allowed_fields:
        return _err(400, "bad_request", "embedding model discovery contains unknown fields")

    provider_ref = payload.get("provider_ref") or None
    provider = payload.get("provider") or ""
    api_base = payload.get("api_base") or ""
    api_key_env = payload.get("api_key_env") or None
    allow_remote = payload.get("allow_remote", False)
    if provider_ref is not None and not isinstance(provider_ref, str):
        return _err(400, "bad_request", "invalid field: provider_ref")
    if not isinstance(provider, str):
        return _err(400, "bad_request", "invalid field: provider")
    if not isinstance(api_base, str):
        return _err(400, "bad_request", "invalid field: api_base")
    if api_key_env is not None and not isinstance(api_key_env, str):
        return _err(400, "bad_request", "invalid field: api_key_env")
    if not isinstance(allow_remote, bool):
        return _err(400, "bad_request", "invalid field: allow_remote")

    if provider_ref is not None:
        try:
            profile, api_key_env, uses = await store_io(_provider_ref_profile, provider_ref)
            if "embedding" not in uses:
                raise ValueError(f"provider {provider_ref!r} does not support embeddings")
            provider = profile.driver
            api_base = profile.api_base or ""
            allow_remote = profile.allow_remote
        except ValueError as exc:
            return _err(400, "bad_request", str(exc))

    if provider != "litellm_proxy":
        return JSONResponse(
            {
                "ok": True,
                "known": False,
                "source": "provider",
                "models": [],
                "error": None,
            },
            headers={"Cache-Control": "no-store"},
        )
    if not api_base:
        return _err(400, "bad_request", "LiteLLM gateway Base URL is required")

    from okto_neuron.config._vault import (
        _check_api_base as _cfg_check_api_base,
    )
    from okto_neuron.config._vault import (
        _check_api_key_env as _cfg_check_api_key_env,
    )

    try:
        _cfg_check_api_base(api_base, allow_remote)
        if api_key_env is not None:
            _cfg_check_api_key_env(api_key_env)
    except ValueError as exc:
        return _err(400, "bad_request", str(exc))

    from okto_neuron.providers import litellm_proxy_models

    try:
        catalog = await job_io(
            litellm_proxy_models,
            api_base=api_base,
            api_key_env=api_key_env,
        )
    except Exception:  # noqa: BLE001
        _LOG.warning("LiteLLM gateway embedding model discovery failed")
        return JSONResponse(
            {
                "ok": False,
                "known": False,
                "source": "litellm_gateway",
                "models": [],
                "error": "could not discover LiteLLM gateway models",
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "known": True,
            "source": "litellm_gateway",
            "models": sorted({model.id for model in catalog if model.mode == "embedding"}),
            "error": None,
        },
        headers={"Cache-Control": "no-store"},
    )


async def api_embedding_test(request: Request) -> JSONResponse:
    """Run one real two-input batch through the draft provider configuration.

    A model-list endpoint is not proof that the configured model, credential,
    vector width, and provider routing work together. This route exercises the
    same provider factory used by vault reads/writes and returns metadata only;
    neither the API key nor the generated vector crosses the browser boundary.
    """
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "embedding/test is restricted to loopback callers")

    try:
        payload = await _read_json(request)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)

    allowed_fields = {
        "provider",
        "model",
        "dimension",
        "api_base",
        "api_key_env",
        "allow_remote",
    }
    if set(payload) - allowed_fields:
        return _err(400, "bad_request", "embedding test contains unknown fields")

    provider = payload.get("provider")
    model = payload.get("model")
    dimension = payload.get("dimension")
    api_base = payload.get("api_base") or None
    api_key_env = payload.get("api_key_env") or None
    allow_remote = payload.get("allow_remote", False)
    if not isinstance(provider, str) or not provider.strip():
        return _err(400, "bad_request", "missing or invalid field: provider")
    if not isinstance(model, str) or not model.strip():
        return _err(400, "bad_request", "missing or invalid field: model")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        return _err(400, "bad_request", "missing or invalid field: dimension")
    if api_base is not None and not isinstance(api_base, str):
        return _err(400, "bad_request", "invalid field: api_base")
    if api_key_env is not None and not isinstance(api_key_env, str):
        return _err(400, "bad_request", "invalid field: api_key_env")
    if not isinstance(allow_remote, bool):
        return _err(400, "bad_request", "invalid field: allow_remote")

    from okto_neuron.config import EmbeddingConfig
    from okto_neuron.embed import EmbeddingProviderError, embed_many, get_provider

    try:
        config = EmbeddingConfig(
            provider=provider.strip(),
            model=model.strip(),
            dimension=dimension,
            api_base=api_base,
            api_key_env=api_key_env,
            allow_remote=allow_remote,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "models": [], "error": str(exc)})

    try:
        vectors = await job_io(
            embed_many,
            get_provider(config),
            [
                "Okto Neuron embedding batch test alpha",
                "Okto Neuron embedding batch test beta",
            ],
        )
    except EmbeddingProviderError as exc:
        return JSONResponse({"ok": False, "models": [], "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("embedding test failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "models": [], "error": "embedding test failed"})

    if len(vectors) != 2 or any(len(vector) != dimension for vector in vectors):
        # Defensive: every built-in provider already enforces this invariant.
        return JSONResponse({"ok": False, "models": [], "error": "embedding batch mismatch"})
    return JSONResponse(
        {
            "ok": True,
            "models": [config.model],
            "dimension": len(vectors[0]),
            "vectors": len(vectors),
            "error": None,
        },
        headers={"Cache-Control": "no-store"},
    )


# --------------------------- reconcile / authority (ADR 0009 P2) ---------------------------
# Off-graph entity reconciliation + the equivalence-authority hub, exposed over the
# request-selected runtime's pool-owned handle via the generic curation job queue
# (``_jobs``). NO second Vault/VaultConnection is ever opened for that path (the
# ADR-0007 corruption path) — runners in ``_curation`` use the runtime lease.
# Writes are off-graph only (JSON side-files); the graph topology is never touched.
# All of these are loopback-gated like the other sensitive admin routes.


def _reconcile_gate(request: Request) -> JSONResponse | None:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "reconcile is restricted to loopback callers")
    state = get_state()
    if state.draining:
        return _draining_response()
    return None


async def api_reconcile_propose(request: Request) -> JSONResponse:
    """Submit a READ-ONLY propose job (candidate clusters + verdicts). Returns a
    job id to poll via /api/v1/reconcile/status. Writes NOTHING to graph or
    side-files."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    payload = await request.json() if (await request.body()) else {}
    if not isinstance(payload, dict):
        payload = {}
    params = {
        "type": payload.get("type"),
        "use_cluster_judge": bool(payload.get("use_cluster_judge", False)),
    }
    job = await store_io(_jobs.submit, get_state(), "reconcile-propose", label="reconcile propose", params=params)
    return JSONResponse({"status": "ok", "job": job.to_public()})


async def api_reconcile_apply(request: Request) -> JSONResponse:
    """Submit an APPLY job: auto-merge high-confidence clusters to the off-graph
    AuthorityIndex, queue the rest. OFF-GRAPH only (apply_reconciliation never calls
    add_node/add_edge). Returns a job id to poll."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    payload = await request.json() if (await request.body()) else {}
    if not isinstance(payload, dict):
        payload = {}
    params = {
        "type": payload.get("type"),
        "use_cluster_judge": bool(payload.get("use_cluster_judge", False)),
    }
    job = await store_io(_jobs.submit, get_state(), "reconcile-apply", label="reconcile apply", params=params)
    return JSONResponse({"status": "ok", "job": job.to_public()})


async def api_reconcile_status(request: Request) -> JSONResponse:
    """Last reconcile run + live job status. ``job_id`` query param polls a
    specific job; otherwise returns the latest propose/apply jobs + the queue
    summary."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    job_id = request.query_params.get("job_id")
    if job_id is not None:
        job = _jobs.get_job(state, job_id)
        if job is None:
            return _err(404, "not_found", f"job not found: {job_id}")
        return JSONResponse({"status": "ok", "job": job.to_public()})
    last_propose = _jobs.latest_of_kind(state, "reconcile-propose")
    last_apply = _jobs.latest_of_kind(state, "reconcile-apply")
    queue_len, authority_len = await store_io(_reconcile_counts, state)
    return JSONResponse(
        {
            "status": "ok",
            "last_propose": last_propose.to_public() if last_propose else None,
            "last_apply": last_apply.to_public() if last_apply else None,
            "queue_count": queue_len,
            "authority_count": authority_len,
            "worker_active": state.curation_worker_active,
        }
    )


def _reconcile_counts(state: ServerState | VaultRuntime) -> tuple[int, int]:
    """Store op: reconcile-queue and authority-index sizes (JSON side-files)."""
    try:
        return (
            len(_curation.reconcile_queue(state)),
            len(_curation.authority_index(state).records()),
        )
    except Exception:  # noqa: BLE001
        return 0, 0


async def api_reconcile_queue(request: Request) -> JSONResponse:
    """List the reconcile review queue (parked clusters awaiting confirmation).
    SEPARATE from the companion review queue (/review-queue) by design."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    try:
        body = await store_io(encode_op, _reconcile_queue_body, state)
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected reconcile-queue failure")
        return _internal_error()
    return json_bytes_response(body)


def _reconcile_queue_body(state: ServerState | VaultRuntime) -> dict[str, Any]:
    rows = [_curation.queued_cluster_row(qc) for qc in _curation.reconcile_queue(state).list()]
    return {"status": "ok", "entries": rows}


async def api_reconcile_review_confirm(request: Request) -> JSONResponse:
    """Confirm a parked cluster → off-graph AuthorityIndex; dequeue. Off-graph JSON
    writes only; serialized under writer_lock (via ``_writer_lock_fast``, so it
    cannot interleave with an apply job's side-file writes but also cannot hang
    behind an in-flight ingest/rebuild/heal/reembed — fails fast with 503)."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    try:
        payload = await _read_json(request)
        cluster_id = _require(payload, "cluster_id", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    try:
        async with _writer_lock_fast(state):
            try:
                rec = await store_io(_reconcile_confirm, state, cluster_id)
            except KeyError:
                return _err(404, "not_found", f"queued cluster not found: {cluster_id}")
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("unexpected reconcile confirm failure")
                return _err(500, "reconcile_confirm_failed", str(exc))
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    return JSONResponse({"status": "ok", "record": _curation.authority_record_row(rec)})


async def api_reconcile_review_reject(request: Request) -> JSONResponse:
    """Reject (drop) a parked cluster from the reconcile review queue. Off-graph."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    try:
        payload = await _read_json(request)
        cluster_id = _require(payload, "cluster_id", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    try:
        async with _writer_lock_fast(state):
            try:
                await store_io(_reconcile_reject, state, cluster_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("unexpected reconcile reject failure")
                return _err(500, "reconcile_reject_failed", str(exc))
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    return JSONResponse({"status": "ok", "cluster_id": cluster_id})


def _reconcile_confirm(state: ServerState | VaultRuntime, cluster_id: str) -> Any:
    """Store op (caller holds writer_lock): queued cluster -> AuthorityIndex."""
    return _curation.reconcile_queue(state).confirm(
        cluster_id, judge_model=_curation._judge_model(state)
    )


def _reconcile_reject(state: ServerState | VaultRuntime, cluster_id: str) -> None:
    """Store op (caller holds writer_lock): drop one parked cluster."""
    _curation.reconcile_queue(state).reject(cluster_id)


# --------------------------- predicate upkeep (ADR 0017 workstream B) ---------------------------


def _upkeep_gate(request: Request) -> JSONResponse | None:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "predicate upkeep is restricted to loopback callers")
    state = get_state()
    if state.draining:
        return _draining_response()
    return None


async def api_upkeep_rebuild_stats(request: Request) -> JSONResponse:
    """Rebuild the vault's maintained projection now (predicate stats + graph counts).

    Read-only on the graph. Answers 202 at once: the rebuild runs on the job executor, skips
    the usual spacing, and joins the one already in flight when there is one (``started`` is
    false then, and the running rebuild rescans once more). Poll ``GET /api/v1/graph/stats``
    or ``/api/v1/upkeep/predicates`` for ``rebuilding: false``."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    started = _projection.manager_for(state.vault_path).ensure(state, force=True)
    return JSONResponse({"status": "rebuilding", "started": started}, status_code=202)


async def api_predicate_upkeep_propose(request: Request) -> JSONResponse:
    """Submit a READ-ONLY predicate canonicalization proposal job."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate
    payload = await request.json() if (await request.body()) else {}
    if not isinstance(payload, dict):
        payload = {}
    params = {
        "judged_pairs": payload.get("judged_pairs"),
    }
    job = await store_io(_jobs.submit, get_state(), "predicate-propose", label="predicate propose", params=params)
    return JSONResponse({"status": "ok", "job": job.to_public()})


async def api_predicate_upkeep_apply(request: Request) -> JSONResponse:
    """Submit an off-graph predicate alias ledger apply job."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate
    payload = await request.json() if (await request.body()) else {}
    if not isinstance(payload, dict):
        payload = {}
    params = {
        "job_id": payload.get("job_id"),
        "records": payload.get("records"),
        "proposals": payload.get("proposals"),
        "outcomes": payload.get("outcomes"),
    }
    job = await store_io(_jobs.submit, get_state(), "predicate-apply", label="predicate apply", params=params)
    return JSONResponse({"status": "ok", "job": job.to_public()})


async def api_companion_triage(request: Request) -> JSONResponse:
    """Submit a write job that judge-triages companion review-queue candidates."""
    if not remote_config_allowed(request):
        return _err(
            403,
            "forbidden",
            "companion triage is restricted to loopback callers",
        )
    state = get_state()
    if state.draining:
        return _draining_response()
    job = await store_io(
        _jobs.submit,
        state,
        "companion-triage",
        label="companion triage",
        params={},
    )
    return JSONResponse({"status": "ok", "job": job.to_public()})


async def api_predicate_upkeep_snapshot(request: Request) -> JSONResponse:
    """Predicate alias index snapshot grouped by status.

    The UI polls this every few seconds from several panels. The vocabulary size comes from
    the vault's maintained projection (``server/_projection.py``), never from a scan here:
    the body carries ``stale``/``rebuilding`` next to it, and a vault with no projection yet
    answers 202 ``{"status": "building"}``. The alias-index read is ONE single-flight store
    op per vault, so concurrent polls share a single execution."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate

    state = get_state()
    try:
        read = await _projection.manager_for(state.vault_path).read(state)
        if read.projection is None:
            return JSONResponse({"status": "building"}, status_code=202)
        vocabulary_size = len(read.projection.stats.vocabulary)
        records_json, counts = await single_flight(
            ("predicate_snapshot", str(state.vault_path)),
            _predicate_snapshot,
            state,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected predicate upkeep snapshot failure")
        return _internal_error()

    last_propose = _jobs.latest_of_kind(state, "predicate-propose")
    last_apply = _jobs.latest_of_kind(state, "predicate-apply")
    # The big part (the records) arrives pre-encoded from the worker; only the
    # small job/worker fields are encoded here, then spliced in key order.
    head = encode_json(
        {
            "status": "ok",
            "vocabulary_size": vocabulary_size,
            "stale": read.stale,
            "rebuilding": read.rebuilding,
        }
    )[:-1]
    tail = encode_json(
        {
            "counts": counts,
            "last_propose": last_propose.to_public() if last_propose else None,
            "last_apply": last_apply.to_public() if last_apply else None,
            "worker_active": state.curation_worker_active,
        }
    )[1:]
    return json_bytes_response(head + b',"records":' + records_json + b"," + tail)


def _predicate_snapshot(
    state: ServerState | VaultRuntime,
) -> tuple[bytes, dict[str, int]]:
    """Store op: alias records grouped by status, encoded, + counts."""
    records = _curation.predicate_alias_index(state).records()
    grouped: dict[str, list[dict[str, Any]]] = {
        "auto": [],
        "confirmed": [],
        "queued": [],
        "rejected": [],
    }
    for record in records:
        grouped.setdefault(record.status, []).append(_curation.predicate_record_row(record))
    counts = {status: len(rows) for status, rows in grouped.items()}
    return encode_json(grouped), counts


def _set_predicate_record_status(
    state: ServerState | VaultRuntime,
    record_id: str,
    status: str,
    required_status: str | None,
) -> Any:
    """Store op (caller holds writer_lock): move one alias record to ``status``."""
    index = _curation.predicate_alias_index(state)
    record = next((rec for rec in index.records() if rec.id == record_id), None)
    if record is None:
        raise _ApiError(404, "not_found", f"predicate record not found: {record_id}")
    if required_status is not None and record.status != required_status:
        raise _ApiError(
            409,
            "conflict",
            f"predicate record {record_id} is {record.status}, not {required_status}",
        )
    updated = replace(record, status=status)
    index.upsert(updated)
    return updated


async def api_predicate_upkeep_confirm(request: Request) -> JSONResponse:
    """Confirm a queued predicate mapping record."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate
    record_id = str(request.path_params.get("record_id") or "")
    state = get_state()
    try:
        async with _writer_lock_fast(state):
            try:
                updated = await store_io(
                    _set_predicate_record_status, state, record_id, "confirmed", "queued"
                )
            except _ApiError as exc:
                return exc.response()
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("unexpected predicate confirm failure")
                return _err(500, "predicate_confirm_failed", str(exc))
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    return JSONResponse({"status": "ok", "record": _curation.predicate_record_row(updated)})


async def api_predicate_upkeep_reject(request: Request) -> JSONResponse:
    """Reject a predicate mapping record."""
    gate = _upkeep_gate(request)
    if gate is not None:
        return gate
    record_id = str(request.path_params.get("record_id") or "")
    state = get_state()
    try:
        async with _writer_lock_fast(state):
            try:
                updated = await store_io(
                    _set_predicate_record_status, state, record_id, "rejected", None
                )
            except _ApiError as exc:
                return exc.response()
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("unexpected predicate reject failure")
                return _err(500, "predicate_reject_failed", str(exc))
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    return JSONResponse({"status": "ok", "record": _curation.predicate_record_row(updated)})


async def api_authority_list(request: Request) -> JSONResponse:
    """List off-graph AuthorityRecords (equivalence classes) from the authority
    index.json."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    try:
        body = await store_io(encode_op, _authority_body, state)
    except Exception:  # noqa: BLE001
        _LOG.exception("unexpected authority-list failure")
        return _internal_error()
    return json_bytes_response(body)


def _authority_body(state: ServerState | VaultRuntime) -> dict[str, Any]:
    rows = [_curation.authority_record_row(r) for r in _curation.authority_index(state).records()]
    return {"status": "ok", "records": rows}


def _authority_unmerge(state: ServerState | VaultRuntime, cluster_id: str) -> bool:
    """Store op (caller holds writer_lock): drop one AuthorityRecord; returns
    whether it existed."""
    index = _curation.authority_index(state)
    existed = any(r.cluster_id == cluster_id for r in index.records())
    index.remove(cluster_id)
    return existed


async def api_authority_unmerge(request: Request) -> JSONResponse:
    """Un-merge (reverse) an equivalence class: drop its AuthorityRecord. REVERSIBLE
    — Option A never touched the graph, so removal restores pre-merge reads with
    nothing to repair. Off-graph JSON write only."""
    gate = _reconcile_gate(request)
    if gate is not None:
        return gate
    state = get_state()
    try:
        payload = await _read_json(request)
        cluster_id = _require(payload, "cluster_id", str)
    except _BadRequest as exc:
        return _err(400, "bad_request", exc.detail)
    try:
        async with _writer_lock_fast(state):
            try:
                existed = await store_io(_authority_unmerge, state, cluster_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("unexpected authority unmerge failure")
                return _err(500, "authority_unmerge_failed", str(exc))
    except _LockBusy:
        return _err(
            503,
            "busy",
            "vault is busy ingesting/curating — retry when the current item finishes",
        )
    if not existed:
        return _err(404, "not_found", f"authority record not found: {cluster_id}")
    return JSONResponse({"status": "ok", "cluster_id": cluster_id, "removed": True})


async def api_curation_jobs(request: Request) -> JSONResponse:
    """Generic curation job-queue snapshot (all kinds, or filtered by ``kind``).
    Powers the Dashboard run-now status + the Reconcile Run & Status live view."""
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "curation jobs are restricted to loopback callers")
    state = get_state()
    kind = request.query_params.get("kind")
    return JSONResponse(_jobs.snapshot(state, kind=kind))


async def api_curation_scheduler(request: Request) -> JSONResponse:
    """Continuous-curation-loop status (ADR 0009 P4) — surfaces the auto-sweep loop
    so the user SEES it working: loop enabled?, last sweep time + outcome, next
    eligible sweep, and the loop's own recent sweep history (auto sweeps only,
    distinct from manual runs).

    Loopback-gated like the other curation endpoints. Read-only."""
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "curation scheduler is restricted to loopback callers")
    state = get_state()
    cfg = await store_io(_scheduler._load_scheduler_config, state)
    now = time.time()
    # Recent history = the loop's OWN auto sweeps: sweep-kind jobs tagged
    # trigger=scheduler. Most-recent first, capped for the panel.
    recent = [
        j.to_public()
        for j in reversed(state.curation_jobs)
        if j.kind in _scheduler.SWEEP_KINDS and (j.params or {}).get("trigger") == "scheduler"
    ][:20]
    return JSONResponse(
        {
            "status": "ok",
            "enabled": cfg.enabled,
            "quiet_debounce_s": cfg.quiet_debounce_s,
            "min_interval_s": cfg.min_interval_s,
            "last_ingest_at": state.last_ingest_at,
            "last_sweep_at": state.last_sweep_at,
            "last_sweep_outcome": state.last_sweep_outcome,
            "next_eligible": _scheduler.next_eligible(
                now, state.last_ingest_at, state.last_sweep_at, cfg
            ),
            "sweep_pending": _scheduler._sweep_job_pending(state),
            "recent": recent,
        }
    )

_REBUILD_KINDS = frozenset({"rebuild", "rollback", "heal", "reembed"})


_VAULTWIDE_SUBMIT_LOCK = threading.Lock()


async def _submit_rebuild_family(
    request: Request,
    kind: str,
    label: str,
    *,
    params: dict | None = None,
) -> JSONResponse:
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "this operation is restricted to loopback callers")
    try:
        job = await store_io(_gated_rebuild_submit, get_state(), kind, label, params)
    except _ApiError as exc:
        return exc.response()
    return JSONResponse({"status": "ok", "job": job.to_public()}, status_code=202)


def _vaultwide_busy(state: ServerState | VaultRuntime) -> _ApiError | None:
    if state.draining:
        return _ApiError(409, "busy", "a vault-wide operation is already in progress")
    # A queued/running rebuild-family job also blocks (it will set draining once it
    # starts; reject early so two never queue back-to-back unexpectedly).
    for job in state.curation_jobs:
        if job.kind in _REBUILD_KINDS and job.status in ("queued", "running"):
            return _ApiError(409, "busy", f"a {job.kind} job is already {job.status}")
    return None


def _gated_rebuild_submit(
    state: ServerState | VaultRuntime,
    kind: str,
    label: str,
    params: dict | None,
    *,
    prepare: Callable[[], dict | None] | None = None,
) -> Any:
    """Store op: the vault-wide busy gate and the job submit (sidecar write) as
    one atomic step, so two concurrent requests can never both pass the gate."""
    with _VAULTWIDE_SUBMIT_LOCK:
        busy = _vaultwide_busy(state)
        if busy is not None:
            raise busy
        if prepare is not None:
            params = prepare()
        return _jobs.submit(state, kind, label=label, params=params)


async def api_curation_rebuild(request: Request) -> JSONResponse:
    """Submit a fresh-graph rebuild from the markdown trust root. Re-extracts every
    source file into a brand-new graph and atomic-swaps it onto LIVE. Returns 202 +
    the job; poll GET /api/v1/curation/rebuild/status for per-file progress."""
    return await _submit_rebuild_family(request, "rebuild", "rebuild")


async def api_curation_rollback(request: Request) -> JSONResponse:
    """Restore the verified graph checkpoint immediately before the live rebuild.

    The current graph generation selects the checkpoint; callers cannot supply a
    filesystem path. The runner requires the effective semantic configuration to
    match that checkpoint and restores its saved decision files with the graph.
    """
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "this operation is restricted to loopback callers")
    state = get_state()
    try:
        job = await store_io(
            _gated_rebuild_submit,
            state,
            "rollback",
            "rollback",
            None,
            prepare=functools.partial(_rollback_params, state),
        )
    except _ApiError as exc:
        return exc.response()
    return JSONResponse({"status": "ok", "job": job.to_public()}, status_code=202)


def _rollback_params(state: ServerState | VaultRuntime) -> dict[str, object]:
    """Store op part: bind the rollback to the verified checkpoint for the live
    graph generation, or refuse when there is no rollback evidence."""
    from okto_neuron.cli import kg as kg_cli
    from okto_neuron.config import VaultConfig
    from okto_neuron.curation.orchestrate import rollback_candidate

    generation = state.vault.store.generation()
    backend_name = kg_cli._resolve_pinned_backend(state.vault_path)
    storage_config = VaultConfig.load(state.vault_path).storage
    candidate = rollback_candidate(state.vault_path, backend_name, storage_config)
    if not generation or candidate is None:
        raise _ApiError(
            409,
            "rollback_unavailable",
            f"missing verified rollback evidence: no rollback candidate for backend "
            f"{backend_name!r} at generation {generation or 'missing'}",
        )
    return {"from_generation": generation}


async def api_curation_heal(request: Request) -> JSONResponse:
    """Submit a heal: a deterministic, no-LLM canonicalizing COPY of the live graph
    that materializes the confirmed off-graph equivalences (authority records) into the
    topology — every variant node is folded onto its canonical and the copy is atomic-
    swapped onto LIVE, so the graph has ONE physical node per merged entity. NOT an
    extractor rebuild: the entities are already extracted; the heal only collapses
    topology. Reversible (drop the authority record + re-heal). The copy reads the live
    graph but never writes it, so reads return 200 for the WHOLE copy; the only blip is a
    brief (~1s) added latency on any read in flight during the final atomic swap (it is
    served against the reopened graph, not 503'd). Returns 202 + the job; poll
    GET /api/v1/curation/rebuild/status?kind=heal."""
    return await _submit_rebuild_family(request, "heal", "heal")


async def api_curation_reembed(request: Request) -> JSONResponse:
    """Submit an in-process vectors-only reembed (no LLM re-extraction). Same
    fresh-build + atomic-swap pattern as rebuild. Returns 202 + the job."""
    return await _submit_rebuild_family(request, "reembed", "reembed")


async def api_curation_rebuild_status(request: Request) -> JSONResponse:
    """Merge the latest rebuild/heal/reembed job lifecycle with the per-file
    ``rebuild.state.json`` phase payload, so the UI sees both job status and live
    progress. ``kind`` query param selects which family (default rebuild)."""
    if not remote_config_allowed(request):
        return _err(403, "forbidden", "this operation is restricted to loopback callers")
    state = get_state()
    requested_kind = request.query_params.get("kind")
    last_rebuild = _jobs.latest_of_kind(state, "rebuild")
    last_heal = _jobs.latest_of_kind(state, "heal")
    last_reembed = _jobs.latest_of_kind(state, "reembed")
    last_rollback = _jobs.latest_of_kind(state, "rollback")
    active = next(
        (
            job
            for job in reversed(state.curation_jobs)
            if job.kind in _REBUILD_KINDS and job.status in ("queued", "running")
        ),
        None,
    )
    kind = requested_kind or (active.kind if active is not None else "rebuild")
    # rebuild + heal share rebuild.state.json; rollback/reembed own their state.
    state_file = {
        "reembed": _REEMBED_STATE_FILE,
        "rollback": _ROLLBACK_STATE_FILE,
    }.get(kind, _REBUILD_STATE_FILE)
    phase_payload = await store_io(
        _read_phase_file, state.vault_path / ".marginalia" / state_file
    )
    return JSONResponse(
        {
            "status": "ok",
            "running": state.draining,
            "worker_active": state.curation_worker_active,
            "last_rebuild": last_rebuild.to_public() if last_rebuild else None,
            "last_heal": last_heal.to_public() if last_heal else None,
            "last_reembed": last_reembed.to_public() if last_reembed else None,
            "last_rollback": last_rollback.to_public() if last_rollback else None,
            "phase": phase_payload,
        }
    )


def _read_phase_file(state_path: Path) -> dict:
    """Store op: one maintenance phase sidecar (idle when absent)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"phase": "idle"}
    except Exception:  # noqa: BLE001
        return {"phase": "unknown"}
    return data if isinstance(data, dict) else {"phase": "idle"}


# --------------------------- web-UI (built SPA) ---------------------------


def _webui_dir() -> Path | None:
    """Resolve the directory holding the built SPA (``index.html`` + ``assets/``).

    Two layouts must work, checked in order:

    1. **Installed wheel** — ``pyproject.toml`` force-includes the repo-root
       ``frontend_dist/`` as package data at ``okto_neuron/_webui``. A normal
       ``pip install`` therefore ships the UI inside the package.
    2. **Editable / dev checkout** — ``pip install -e`` does NOT materialise the
       force-include, so fall back to the repo-root ``frontend_dist/`` sitting
       next to ``src/`` (this file is ``src/okto_neuron/server/http.py``).

    Returns ``None`` when no build is present (API-only mode: the server still
    boots, CLI/MCP are unaffected, and the UI route simply 404s).
    """
    # 1. installed package data
    pkg_webui = Path(__file__).resolve().parent.parent / "_webui"
    if (pkg_webui / "index.html").is_file():
        return pkg_webui
    # 2. repo-root frontend_dist (dev / editable install)
    #    http.py → server → okto_neuron → src → <repo root>
    repo_dist = Path(__file__).resolve().parents[3] / "frontend_dist"
    if (repo_dist / "index.html").is_file():
        return repo_dist
    return None


# --------------------------- app factory ---------------------------


def _routes() -> list[Route]:
    return [
        # ── /api/v1/* — web-UI surface. MUST precede the bare CLI routes and any
        #    SPA StaticFiles fallback (route order is load-bearing in Starlette).
        Route("/api/v1/nodes", api_nodes_list, methods=["GET"]),
        Route("/api/v1/nodes/{id}/neighbors", api_node_neighbors, methods=["GET"]),
        Route("/api/v1/nodes/{id}", api_node_detail, methods=["GET"]),
        Route("/api/v1/node-types", api_node_types, methods=["GET"]),
        # /graph and /graph/stats are distinct full paths (neither is a prefix of
        # the other in Starlette's matcher), so registration order is immaterial.
        Route("/api/v1/graph", api_graph, methods=["GET"]),
        Route("/api/v1/graph/stats", api_graph_stats, methods=["GET"]),
        Route("/api/v1/graph/integrity", api_graph_integrity, methods=["GET"]),
        Route("/api/v1/graph/integrity", api_graph_integrity_run, methods=["POST"]),
        Route("/api/v1/quality/semantic", api_semantic_quality, methods=["POST"]),
        Route(
            "/api/v1/quality/semantic/governance",
            api_semantic_governance,
            methods=["GET"],
        ),
        Route("/api/v1/vaults", api_vaults, methods=["GET"]),
        Route("/api/v1/vaults", api_vault_create, methods=["POST"]),
        Route("/api/v1/backends", api_backends, methods=["GET"]),
        Route("/api/v1/vaults/current", api_vault_current, methods=["GET"]),
        Route("/api/v1/vaults/switch", api_vault_switch, methods=["POST"]),
        Route("/api/v1/vaults/reembed", api_vault_reembed, methods=["POST"]),
        Route("/api/v1/vaults/reembed/status", api_vault_reembed_status, methods=["GET"]),
        Route("/api/v1/vaults/{vault_id}", api_vault_delete, methods=["DELETE"]),
        Route("/api/v1/config", api_config_get, methods=["GET"]),
        Route("/api/v1/config", api_config_patch, methods=["PATCH"]),
        Route(
            "/api/v1/config/defaults",
            api_application_config_get,
            methods=["GET"],
        ),
        Route(
            "/api/v1/config/defaults",
            api_application_config_patch,
            methods=["PATCH"],
        ),
        Route("/api/v1/provider-types", api_provider_types, methods=["GET"]),
        Route("/api/v1/credentials", api_credentials_list, methods=["GET"]),
        Route("/api/v1/credentials", api_credentials_create, methods=["POST"]),
        Route(
            "/api/v1/credentials/provider",
            api_provider_credential_put,
            methods=["PUT"],
        ),
        Route(
            "/api/v1/credentials/{credential_id}",
            api_credential_update,
            methods=["PUT"],
        ),
        Route(
            "/api/v1/credentials/{credential_id}",
            api_credential_delete,
            methods=["DELETE"],
        ),
        Route("/api/v1/providers", api_providers_list, methods=["GET"]),
        Route("/api/v1/providers", api_providers_create, methods=["POST"]),
        Route(
            "/api/v1/providers/{provider_id}",
            api_provider_update,
            methods=["PATCH"],
        ),
        Route(
            "/api/v1/providers/{provider_id}",
            api_provider_delete,
            methods=["DELETE"],
        ),
        Route("/api/v1/llm/credential", api_llm_credential_put, methods=["PUT"]),
        Route("/api/v1/llm/test", api_llm_test, methods=["POST"]),
        Route(
            "/api/v1/config/defaults/llm/test",
            api_llm_test,
            methods=["POST"],
        ),
        Route("/api/v1/llm/test-completion", api_llm_test_completion, methods=["POST"]),
        Route(
            "/api/v1/config/defaults/llm/test-completion",
            api_llm_test_completion,
            methods=["POST"],
        ),
        Route("/api/v1/embedding/models", api_embedding_models, methods=["POST"]),
        Route(
            "/api/v1/config/defaults/embedding/models",
            api_embedding_models,
            methods=["POST"],
        ),
        Route("/api/v1/embedding/test", api_embedding_test, methods=["POST"]),
        Route(
            "/api/v1/config/defaults/embedding/test",
            api_embedding_test,
            methods=["POST"],
        ),
        Route("/api/v1/embedding/reembed", api_embedding_reembed, methods=["POST"]),
        Route(
            "/api/v1/embedding/reembed/status",
            api_embedding_reembed_status,
            methods=["GET"],
        ),
        Route("/api/v1/reset", api_reset, methods=["POST"]),
        Route("/api/v1/recall", api_recall, methods=["POST"]),
        Route("/api/v1/ask", api_ask, methods=["POST"]),
        Route("/api/v1/ingest", api_ingest, methods=["POST"]),
        Route("/api/v1/ingest-folder", api_ingest_folder, methods=["POST"]),
        Route("/api/v1/folder-watch/status", api_folder_watch_status, methods=["GET"]),
        Route("/api/v1/folder-watch/roots", api_folder_watch_roots_add, methods=["POST"]),
        Route("/api/v1/folder-watch/roots", api_folder_watch_roots_remove, methods=["DELETE"]),
        Route("/api/v1/ingest-batch", api_ingest_batch, methods=["POST"]),
        Route("/api/v1/ingest-queue", api_ingest_queue, methods=["GET"]),
        Route("/api/v1/ingest-queue/{item_id}", api_ingest_queue_item, methods=["GET"]),
        Route(
            "/api/v1/ingest-queue/{item_id}",
            api_ingest_queue_delete,
            methods=["DELETE"],
        ),
        Route(
            "/api/v1/ingest-queue/{item_id}/retry",
            api_ingest_queue_retry,
            methods=["POST"],
        ),
        Route("/api/v1/ingest-cancel", api_ingest_cancel, methods=["POST"]),
        Route("/api/v1/ledger/runs", api_ledger_runs, methods=["GET"]),
        Route("/api/v1/ledger/runs/{run_id}", api_ledger_run_detail, methods=["GET"]),
        Route("/api/v1/ledger/summary", api_ledger_summary, methods=["GET"]),
        # ── ADR 0009 P2: reconcile / authority / curation jobs (off-graph) ──
        Route("/api/v1/reconcile/propose", api_reconcile_propose, methods=["POST"]),
        Route("/api/v1/reconcile/apply", api_reconcile_apply, methods=["POST"]),
        Route("/api/v1/reconcile/status", api_reconcile_status, methods=["GET"]),
        Route("/api/v1/reconcile/queue", api_reconcile_queue, methods=["GET"]),
        Route(
            "/api/v1/reconcile/review/confirm",
            api_reconcile_review_confirm,
            methods=["POST"],
        ),
        Route(
            "/api/v1/reconcile/review/reject",
            api_reconcile_review_reject,
            methods=["POST"],
        ),
        Route(
            "/api/v1/upkeep/rebuild-stats",
            api_upkeep_rebuild_stats,
            methods=["POST"],
        ),
        Route(
            "/api/v1/upkeep/predicates/propose",
            api_predicate_upkeep_propose,
            methods=["POST"],
        ),
        Route(
            "/api/v1/upkeep/predicates/apply",
            api_predicate_upkeep_apply,
            methods=["POST"],
        ),
        Route(
            "/api/v1/upkeep/predicates",
            api_predicate_upkeep_snapshot,
            methods=["GET"],
        ),
        Route(
            "/api/v1/upkeep/predicates/{record_id}/confirm",
            api_predicate_upkeep_confirm,
            methods=["POST"],
        ),
        Route(
            "/api/v1/upkeep/predicates/{record_id}/reject",
            api_predicate_upkeep_reject,
            methods=["POST"],
        ),
        Route(
            "/api/v1/curation/companion-triage",
            api_companion_triage,
            methods=["POST"],
        ),
        Route("/api/v1/authority", api_authority_list, methods=["GET"]),
        Route("/api/v1/authority/unmerge", api_authority_unmerge, methods=["POST"]),
        Route("/api/v1/curation/jobs", api_curation_jobs, methods=["GET"]),
        Route("/api/v1/curation/scheduler", api_curation_scheduler, methods=["GET"]),
        # ── ADR 0009 P3: in-process rebuild / heal / reembed (fresh-graph swap) ──
        Route("/api/v1/curation/rebuild", api_curation_rebuild, methods=["POST"]),
        Route("/api/v1/curation/rollback", api_curation_rollback, methods=["POST"]),
        Route("/api/v1/curation/heal", api_curation_heal, methods=["POST"]),
        Route("/api/v1/curation/reembed", api_curation_reembed, methods=["POST"]),
        Route(
            "/api/v1/curation/rebuild/status",
            api_curation_rebuild_status,
            methods=["GET"],
        ),
        # Versioned aliases for the remaining companion/curation controls. The
        # bare routes below stay for CLI/backward compatibility; browser code
        # uses these so Vite's documented /api proxy exercises the real contract.
        Route("/api/v1/detect-drift", detect_drift, methods=["POST"]),
        Route("/api/v1/review-queue", review_queue, methods=["GET"]),
        Route(
            "/api/v1/review-queue/batch",
            resolve_review_batch,
            methods=["POST"],
        ),
        Route("/api/v1/resolve-review", resolve_review, methods=["POST"]),
        # ── existing bare routes — KEEP (the CLI speaks these over loopback) ──
        Route("/add", add, methods=["POST"]),
        Route("/query", query, methods=["POST"]),
        Route("/detect-drift", detect_drift, methods=["POST"]),
        Route("/remember", remember, methods=["POST"]),
        Route("/recall", recall, methods=["POST"]),
        Route("/ask", ask, methods=["POST"]),
        Route("/review-queue", review_queue, methods=["GET"]),
        Route("/review-queue/batch", resolve_review_batch, methods=["POST"]),
        Route("/resolve-review", resolve_review, methods=["POST"]),
        Route("/api/v1/status", api_status, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/version", version, methods=["GET"]),
    ]


def build_rest_app(state: ServerState | None = None) -> Starlette:
    """Build the REST Starlette app. ``state`` must have been initialized via
    :func:`okto_neuron.server.state.init_state`. The ``state`` argument is
    accepted for explicit wiring/tests but the handlers read the module
    singleton at request time so a single shared instance is used.
    """
    if state is not None:
        # Touch ensures state is initialized before any request lands.
        assert state is get_state()  # pragma: no cover - defensive
    # ADR 0009 P2: register curation job runners with the generic queue so the
    # selected runtime can drain reconcile jobs through its leased handle (idempotent).
    _curation.register_runners()
    routes: list[Any] = list(_routes())
    # SPA mount goes LAST — Starlette matches routes in order, so a catch-all
    # StaticFiles at "/" would shadow the /api/v1/* and bare CLI routes if placed
    # earlier. ``html=True`` serves index.html at "/" and resolves /assets/*; the
    # app is single-view (no client-side router) so no deep-link fallback is
    # needed. When no build exists the mount is omitted (graceful API-only mode).
    webui = _webui_dir()
    if webui is not None:
        routes.append(Mount("/", app=StaticFiles(directory=str(webui), html=True), name="webui"))
        _LOG.info("web UI mounted from %s", webui)
    else:
        _LOG.info("no web UI build found (frontend_dist/ absent); serving API only")
    # L1: the loopback Host-header guard wraps EVERY route (API, bare CLI, SPA).
    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers={IntegrityFenceError: _integrity_fence_handler},
        middleware=[
            # Outermost: bind request_id first so EVERY downstream log line (guard
            # rejections, endpoints, degraded-health errors) carries it.
            Middleware(RequestIdMiddleware),
            Middleware(LoopbackHostMiddleware),
            # Credential-free local browser access remains safe because writes
            # reject cross-site browser requests and non-JSON payloads.
            Middleware(LocalAppSecurityMiddleware),
            Middleware(ActiveVaultMiddleware),
        ],
    )
    return app


__all__ = ["build_rest_app", "API_VERSION", "EMBEDDING_MODEL", "CLOSED_NODE_TYPES"]
