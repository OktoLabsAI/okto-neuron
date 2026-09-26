"""The one shared graph-swap tail behind ``kg rebuild`` / ``reconcile heal`` /
``kg reembed`` (M2b).

Before this module, the sequence "pre-swap audit -> require the exclusivity
lock is still held -> atomically commit the staged graph onto live -> fence
the new generation as VERIFYING -> audit it again post-swap -> publish the
verdict" was hand-copied across ``cli/kg.py``'s two offline owners and
``server/_curation.py``'s four daemon runners (M2b spec §1). :func:`rebuild`,
:func:`heal`, and :func:`reembed` each run their own verb-specific BUILD head
(unchanged: ``cli/kg.py``'s full ingest pipeline for rebuild,
``store/reembed.py``'s ``copy_graph_canonicalizing``/``copy_graph_reembedding``
for heal/reembed) and then delegate to :func:`finish_staged_swap` for the
part that used to be duplicated.

There is deliberately **no** ``rollback()`` wrapper: rollback has no offline
CLI owner today, so its one caller (``server/_curation.py``'s
``run_rollback``) can call :func:`finish_staged_swap` directly with
``stage_prefix="rollback"``.

Import boundary (load-bearing, not stylistic)
----------------------------------------------
This module NEVER imports ``okto_neuron.cli.kg`` or ``okto_neuron.server``:
``cli/kg.py`` is this module's own future caller, and ``server/_curation.py``
already imports ``cli.kg`` — importing either back from here would be
circular. Several pieces of machinery this module needs (the pre-swap/
post-swap identity audit, the "fence this generation" and "publish this
audit verdict" integrity-state writers, the tmp-graph bootstrapper, and — for
``rebuild`` only — the whole markdown/LLM ingest build head) live in
``cli/kg.py`` today and are NOT part of the M2b relocation (spec §1, §7 OQ3:
"the tail is exactly the part hand-copied byte-for-byte 7 times ... moving
only that removes the duplication"). Every one of those is accepted here as a
keyword-only callable instead, with a name chosen to read naturally at the
call site. Concretely, ``cli/kg.py`` is expected to pass:

- ``audit_graph_path``            -> its own ``_audit_rebuild_graph_path``
- ``mark_generation_verifying``   -> its own ``_mark_rebuild_generation_verifying``
- ``publish_integrity_result``    -> its own ``_publish_integrity_result``
- ``bootstrap_graph_at_path``     -> its own ``_bootstrap_graph_at_path``
- ``close_live_handles``          -> its own ``_close_live_graph_handles``
  (offline/CLI callers only; the daemon's ``_swap_under_runtime_fence``
  already achieves the equivalent effect by fencing+draining the pool, so
  online callers pass ``None``)
- ``rebuild``'s ``build``         -> a closure over its own
  ``_build_fresh_graph`` with ``state_path``/``started_at`` already bound
  (``_build_fresh_graph`` also needs ``vault_path``/``tmp_graph_path``/
  ``ingest``/``source_files``/``interrupt_check``, which this module already
  has in scope and passes through)
- ``rebuild``'s ``require_candidate`` -> a closure over its own
  ``_require_rebuild_candidate`` (the rebuild-only semantic swap-gate that
  reads ``built["swap_allowed"]``/``built["final_audit"]``; distinct from the
  generic post-build identity audit ``audit_graph_path`` runs, and NOT part
  of the relocated tail — see the module docstring above)

``rebuild``'s generation-keyed rollback-artifact backup
(``_prepare_rebuild_backup``/``rebuild-artifacts/<generation>/``) and its
semantic-materialization publish/sidefile-restore-on-exception stay entirely
in ``cli/kg.py``, wrapping the call into :func:`rebuild` — none of that is
swap machinery and none of it is touched here (out of scope per spec §6).

``store.generation()``/``store.health()`` (spec §2.4)
-------------------------------------------------------
Where a verb needs a bare Ladybug health check on the staged graph
(``heal``/``reembed``, today's ``verify_ladybug_db_health(tmp_graph_path)``
called by name), this module calls ``store.health()`` on the
:class:`~okto_neuron.store.ladybug.LadybugStore` it already built instead —
per spec §2.4/§3, retiring that by-name import. This module has no
``_graph_handle`` reflection sites to retire (the five listed in the facts
sheet all live in ``server/_curation.py``, out of this file's scope), so
``store.generation()`` is not called here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from okto_neuron.errors import RebuildAuditFailed, VaultCorrupted
from okto_neuron.predicates import PredicateAliasIndex
from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME, AuthorityIndex
from okto_neuron.store import schema
from okto_neuron.store._bootstrap import (
    _GRAPH_FILE,
    _MARGINALIA_DIR,
    _resolve_configured_dim,
    reset_bootstrap_cache_for_tests,
)
from okto_neuron.store.integrity_state import require_unfenced_generation
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.reembed import copy_graph_canonicalizing, copy_graph_reembedding

if TYPE_CHECKING:
    from okto_neuron.core.schema import Edge, Node
    from okto_neuron.embed import EmbeddingProvider
    from okto_neuron.store._bootstrap import VaultGraphHandle
    from okto_neuron.store.integrity import IntegrityAuditResult
    from okto_neuron.store.protocol import BackendHealth, GraphStore
    from okto_neuron.store.rebuild_lock import RebuildLockHandle
    from okto_neuron.store.staging import StagingPort

# ── callable shapes accepted from the caller (see module docstring) ────────
# Signature matches cli/kg.py's ``_audit_rebuild_graph_path``: reopens the
# durable bytes at ``graph_path`` read-only and audits them against
# ``expected_identity`` at width ``dim``, tagging the result with ``stage``.
AuditGraphPathFn = Callable[..., "tuple[IntegrityAuditResult, dict[str, object]]"]
# Matches cli/kg.py's ``_mark_rebuild_generation_verifying(vault_path,
# graph_generation, *, audit_id=...)``.
MarkGenerationVerifyingFn = Callable[..., None]
# Matches cli/kg.py's ``_publish_integrity_result(vault_path, result, *,
# audit_id=...)``.
PublishIntegrityResultFn = Callable[..., None]
# Matches cli/kg.py's ``_bootstrap_graph_at_path(vault_path, graph_path,
# dim=None)``.
BootstrapGraphAtPathFn = Callable[[Path, Path, "int | None"], "VaultGraphHandle"]
# Matches cli/kg.py's ``_close_live_graph_handles(vault_path)``.
CloseLiveHandlesFn = Callable[[Path], None]
# ``heal``/``reembed``'s one generalized construction seam (M4 spec §2): open
# (i.e. bootstrap fresh, for both backends today) a staged ``GraphStore`` at
# ``staged_path`` built for vector width ``dim``, returning it already open
# alongside the identity that graph was born with. ``dim`` is always a
# concrete int here — callers resolve "use the vault's configured width"
# themselves (matching what ``_bootstrap_graph_at_path(..., dim=None)``
# resolves internally today) before calling this, so every implementation of
# this seam — Ladybug's default closure below, or a backend-specific one a
# caller supplies — sees the same width.
OpenStagedStoreFn = Callable[[Path, Path, int], "tuple[GraphStore, schema.GraphIdentity]"]


def _ladybug_open_staged_store(
    bootstrap_graph_at_path: BootstrapGraphAtPathFn,
) -> OpenStagedStoreFn:
    """The byte-identical default for ``open_staged_store``: today's
    Ladybug-only bootstrap-then-wrap sequence, unchanged when a caller
    supplies no backend-specific opener of its own.

    Keys the wrapping ``LadybugStore`` on ``staged_path`` (the fresh graph
    file this seam just bootstrapped), NOT ``vault_path``: this closure is
    now shared by every caller of this seam, including daemon runners
    (``server/_curation.py``'s ``run_heal``/``run_reembed``) that keep a
    LIVE handle open on ``vault_path`` for the whole build. ``LadybugStore``
    caches one ``VaultConnection`` handle per resolved first-arg path
    (``store/ladybug.py``'s ``VaultConnection._handles``); keying on
    ``vault_path`` while a live handle is already cached there makes every
    operation on this "staged" store silently hit the CACHED LIVE handle
    instead of the ``tmp_handle`` this seam just built — cli/kg.py's
    ``_build_fresh_graph_pass`` (853) and the pre-M4 daemon runners both
    already avoided this by keying on the staged/tmp path; this closure now
    matches that established, safe pattern instead of orchestrate.py's own
    pre-seam ``vault_path`` keying (safe there only because those callers
    always close the live handle before this ever ran).
    """

    def _open(vault_path: Path, staged_path: Path, dim: int) -> tuple["GraphStore", schema.GraphIdentity]:
        tmp_handle = bootstrap_graph_at_path(vault_path, staged_path, dim)
        identity = schema.GraphIdentity(
            tmp_handle.graph_generation,
            tmp_handle.identity_contract_version,
        )
        return LadybugStore(staged_path, graph_handle=tmp_handle), identity

    return _open


@dataclass(frozen=True)
class StagedSwapResult:
    """What :func:`finish_staged_swap` actually did, for the caller's state file."""

    graph_generation: str
    backup_path: Path
    pre_swap_audit: dict[str, object]
    # ``None`` exactly when ``publish_integrity=False`` (reembed): no post-swap
    # audit ran, so there is no verdict to report.
    post_swap_audit: dict[str, object] | None


def finish_staged_swap(
    vault_path: Path,
    staging: "StagingPort",
    staged_path: Path,
    *,
    lock: "RebuildLockHandle",
    backup_tag: str,
    expected_identity: schema.GraphIdentity,
    dim: int,
    stage_prefix: str,
    audit_graph_path: AuditGraphPathFn | None = None,
    mark_generation_verifying: MarkGenerationVerifyingFn | None = None,
    publish_integrity_result: PublishIntegrityResultFn | None = None,
    pre_swap_stage: str | None = None,
    pre_swap_failure_message: str | None = None,
    post_swap_failure_message: Callable[[Path], str] | None = None,
    commit: Callable[[], Path] | None = None,
    publish_integrity: bool = True,
    close_live_handles: CloseLiveHandlesFn | None = None,
    live_graph_path: Path | None = None,
) -> StagedSwapResult:
    """Run the swap tail every rebuild/heal/reembed/rollback owner hand-copied.

    ``staged_path`` must already be a fully built, durably-flushed candidate
    graph (this function builds nothing). Sequence, matching today's exact
    behavior in every real caller (M2b spec §1/§3, facts sheet item 3):

    1. **Pre-swap audit** (only when ``pre_swap_stage`` is given — rebuild has
       no equivalent call today, so it passes ``None``; heal passes a stage
       name and gates on it, matching ``heal.py``'s ``_audit_rebuild_graph_path``
       call on the staged bytes before it ever touches live). A failed audit
       discards the staged graph via ``staging.discard`` and raises
       :class:`~okto_neuron.errors.RebuildAuditFailed` — live is never touched
       (invariant 1).
    2. ``close_live_handles`` (offline/CLI callers only — see module
       docstring), then ``lock.require_held()`` **immediately** before commit,
       not only at acquisition (invariant 2) — a lock lost in between fails
       closed here rather than swapping.
    3. **Commit**: ``commit()`` when the caller supplies one (the daemon
       passes a fenced wrapper around this same commit, per spec §2.3), else
       ``staging.commit(staged_path, backup_tag)``.
    4. Unless ``publish_integrity=False`` (reembed's documented asymmetry,
       spec §1/§7 OQ2, invariant 4): fence the new generation as VERIFYING,
       audit the now-live bytes, publish the verdict, and — if that audit
       fails — raise :class:`RebuildAuditFailed` naming the backup path
       WITHOUT restoring it (invariant 3; recovery from a fenced generation
       is an operator decision, exactly as today).

    ``audit_graph_path``/``mark_generation_verifying``/
    ``publish_integrity_result`` are required whenever they would actually be
    called (``pre_swap_stage`` is set, and/or ``publish_integrity`` is true);
    reembed's caller can omit all three.

    ``pre_swap_failure_message``/``post_swap_failure_message`` let a caller
    override the generic ``"staged {stage_prefix} graph ..."``/``"post-swap
    {stage_prefix} graph ..."`` wording with the exact legacy text its own
    hand-copied tail used to raise (e.g. heal's ``"healed staging graph
    failed integrity verification; live graph left unchanged"`` /
    ``"post-swap healed graph failed integrity verification; previous graph
    retained at {backup_path}"`` — plain English that predates this
    function and is asserted verbatim by existing tests). Omit either to get
    the generic template; ``post_swap_failure_message`` receives the backup
    path so it can name it however its caller's legacy text did.

    ``live_graph_path`` names the on-disk LIVE graph the post-swap audit
    reopens (step 4). Omitted (``None``, every pre-M4 caller), it defaults to
    ``vault_path / _GRAPH_FILE`` — today's exact Ladybug literal, unchanged.
    A non-Ladybug backend's live graph is not that literal (e.g. Grafx's is a
    directory, ``vault_path / "graph.grafx"``), so a caller staging one
    supplies its own resolved path here instead (M4 spec §2 item 1/3).
    """
    if pre_swap_stage is not None and audit_graph_path is None:
        raise TypeError("finish_staged_swap: pre_swap_stage requires audit_graph_path")
    if publish_integrity and (
        audit_graph_path is None
        or mark_generation_verifying is None
        or publish_integrity_result is None
    ):
        raise TypeError(
            "finish_staged_swap: publish_integrity=True requires audit_graph_path, "
            "mark_generation_verifying, and publish_integrity_result"
        )

    pre_swap_audit: dict[str, object] = {}
    if pre_swap_stage is not None:
        assert audit_graph_path is not None  # narrowed above
        pre_result, pre_audit = audit_graph_path(
            staged_path,
            dim=dim,
            expected_identity=expected_identity,
            stage=pre_swap_stage,
        )
        pre_swap_audit = pre_audit
        if not pre_result.verified:
            staging.discard(staged_path)
            raise RebuildAuditFailed(
                vault_path,
                staging_path=staged_path,
                audit_status=pre_result.status.value,
                message=(
                    pre_swap_failure_message
                    if pre_swap_failure_message is not None
                    else (
                        f"staged {stage_prefix} graph failed integrity verification; "
                        "live graph left unchanged"
                    )
                ),
            )

    if close_live_handles is not None:
        close_live_handles(vault_path)

    lock.require_held()
    backup_path = commit() if commit is not None else staging.commit(staged_path, backup_tag)
    reset_bootstrap_cache_for_tests(vault_path)

    graph_generation = str(expected_identity.graph_generation or "")
    post_swap_audit: dict[str, object] | None = None
    if publish_integrity:
        assert audit_graph_path is not None  # narrowed above
        assert mark_generation_verifying is not None
        assert publish_integrity_result is not None
        integrity_audit_id = uuid4().hex
        mark_generation_verifying(vault_path, graph_generation, audit_id=integrity_audit_id)
        graph_path = live_graph_path if live_graph_path is not None else vault_path / _GRAPH_FILE
        post_result, post_audit = audit_graph_path(
            graph_path,
            dim=dim,
            expected_identity=expected_identity,
            stage=f"{stage_prefix}_after_swap_reopen",
        )
        publish_integrity_result(vault_path, post_result, audit_id=integrity_audit_id)
        post_swap_audit = post_audit
        if not post_result.verified:
            raise RebuildAuditFailed(
                vault_path,
                staging_path=graph_path,
                audit_status=post_result.status.value,
                message=(
                    post_swap_failure_message(backup_path)
                    if post_swap_failure_message is not None
                    else (
                        f"post-swap {stage_prefix} graph failed integrity verification; "
                        f"previous graph retained at {backup_path}"
                    )
                ),
            )

    return StagedSwapResult(
        graph_generation=graph_generation,
        backup_path=backup_path,
        pre_swap_audit=pre_swap_audit,
        post_swap_audit=post_swap_audit,
    )


def rebuild(
    vault_path: Path,
    lock: "RebuildLockHandle",
    *,
    ingest: Callable[[Path, LadybugStore], object | None] | None = None,
    source_files: Sequence[Path] | None = None,
    interrupt_check: Callable[..., None] | None = None,
    staging: "StagingPort",
    build: Callable[..., dict[str, object]],
    require_candidate: Callable[[dict[str, object]], None],
    audit_graph_path: AuditGraphPathFn,
    mark_generation_verifying: MarkGenerationVerifyingFn,
    publish_integrity_result: PublishIntegrityResultFn,
    close_live_handles: CloseLiveHandlesFn | None = None,
    open_staged_store: OpenStagedStoreFn | None = None,
    live_graph_path: Path | None = None,
) -> StagedSwapResult:
    """Rebuild a vault graph from markdown into a fresh graph and swap it live.

    ``ingest``/``source_files``/``interrupt_check`` are the same seams
    ``cli/kg.py``'s ``_kg_rebuild_owned``/``_build_fresh_graph`` already
    expose (the per-file test-injection ingest callable, the acceptance-only
    exact-permutation file ordering, and the Ctrl-C interrupt-check hook);
    they are threaded straight through to ``build``.

    ``build`` and ``require_candidate`` are ``cli/kg.py``'s own
    ``_build_fresh_graph``/``_require_rebuild_candidate`` — see the module
    docstring for why they are accepted as callables rather than imported.
    ``build(vault_path, staged_path, *, ingest=, source_files=,
    interrupt_check=)`` must return the same ``built`` dict shape
    ``_build_fresh_graph`` returns today (at least ``graph_generation``,
    ``identity_contract_version``, and ``embedding_dim``); ``cli/kg.py``
    keeps its own reference to that dict (e.g. via a closure over ``build``)
    for the semantic-materialization publish and final state write it still
    owns after this call returns — this function does not hand it back.
    ``require_candidate(built)`` must raise
    :class:`~okto_neuron.errors.RebuildAuditFailed` itself when the build is
    not swap-eligible; it runs before anything touches live (invariant 1),
    the same as today's ``_require_rebuild_candidate``.

    No pre-swap identity audit runs here (``_kg_rebuild_owned`` has none
    today — its only pre-swap gate is ``require_candidate``); rebuild only
    exercises the shared tail's post-swap half.

    ``open_staged_store``/``live_graph_path`` are the M4 spec §2 seam, added
    symmetrically to :func:`heal`/:func:`reembed`'s own (default ``None``
    for both, preserving today's exact call shape — including every
    existing ``build`` test double with the narrower ``(vault_path,
    staged_path, *, ingest=, source_files=, interrupt_check=)`` signature
    this docstring already documents): ``open_staged_store`` is forwarded to
    ``build`` only when given, so a caller's ``build`` need not accept it
    unless it actually wants the seam.
    """
    staged_path = staging.stage_path("rebuild")
    build_kwargs: dict[str, object] = {
        "ingest": ingest,
        "source_files": source_files,
        "interrupt_check": interrupt_check,
    }
    if open_staged_store is not None:
        build_kwargs["open_staged_store"] = open_staged_store
    built = build(vault_path, staged_path, **build_kwargs)
    require_candidate(built)

    expected_identity = schema.GraphIdentity(
        str(built["graph_generation"]),
        str(built["identity_contract_version"]),
    )
    dim = int(built["embedding_dim"])  # type: ignore[arg-type]

    return finish_staged_swap(
        vault_path,
        staging,
        staged_path,
        lock=lock,
        backup_tag="rebuild",
        expected_identity=expected_identity,
        dim=dim,
        stage_prefix="rebuild",
        audit_graph_path=audit_graph_path,
        mark_generation_verifying=mark_generation_verifying,
        publish_integrity_result=publish_integrity_result,
        close_live_handles=close_live_handles,
        live_graph_path=live_graph_path,
    )


def heal(
    vault_path: Path,
    lock: "RebuildLockHandle",
    *,
    authority: AuthorityIndex | None = None,
    live_nodes: Sequence["Node"],
    live_edges: Sequence["Edge"],
    staging: "StagingPort",
    bootstrap_graph_at_path: BootstrapGraphAtPathFn,
    open_staged_store: OpenStagedStoreFn | None = None,
    audit_graph_path: AuditGraphPathFn,
    mark_generation_verifying: MarkGenerationVerifyingFn,
    publish_integrity_result: PublishIntegrityResultFn,
    close_live_handles: CloseLiveHandlesFn | None = None,
    live_graph_path: Path | None = None,
) -> tuple[StagedSwapResult, dict[str, int]]:
    """Fold confirmed off-graph equivalences into a fresh graph and swap it live.

    ``live_nodes``/``live_edges`` are already-read (the read strategy is
    call-context-specific: the offline owner closes live handles first and
    reads raw, the daemon reads through the still-serving live store — spec
    §2.3), so this owns everything from equivalence/predicate-alias
    resolution through the swap tail. ``authority`` defaults to
    ``<vault>/.marginalia/authority``, matching ``heal_via_copy`` today;
    an empty/absent index degenerates the heal to a verbatim copy.

    ``open_staged_store`` is the one generalized construction seam (M4 spec
    §2): omitted (``None``, every caller today), it defaults to a closure
    over ``bootstrap_graph_at_path`` + ``LadybugStore(...)`` — byte-identical
    to what this function did before the seam existed. A caller staging a
    non-Ladybug backend supplies its own opener instead (e.g. one that
    bootstraps a fresh ``GrafxStore`` at the staged path). ``live_graph_path``
    is threaded straight through to :func:`finish_staged_swap`'s own
    same-named parameter (see there) — omitted, the post-swap audit reopens
    Ladybug's ``vault_path / _GRAPH_FILE`` literal, unchanged.

    Returns ``(StagedSwapResult, stats)`` where ``stats`` is
    :func:`~okto_neuron.store.reembed.copy_graph_canonicalizing`'s own return
    value, for the caller's progress/state file.
    """
    # ADR 0039: never canonicalize a generation already proven bad (or one an
    # audit could not finish auditing) — matches heal.py's own pre-lock gate.
    require_unfenced_generation(vault_path)

    if authority is None:
        authority = AuthorityIndex(vault_path / _MARGINALIA_DIR / AUTHORITY_DIRNAME)
    equivalence = authority.equivalence_map() or None
    predicate_index = PredicateAliasIndex(vault_path)
    predicate_aliases = predicate_index.alias_map() or None
    inverse_aliases = predicate_index.inverse_map() or None

    staged_path = staging.stage_path("heal")
    # Bootstrap at the EXISTING vectors' width — heal copies embeddings
    # verbatim (no embedder on this path), so the staged graph's fixed-width
    # vector column must match what it is about to receive. Resolved to a
    # concrete int here (rather than handed to the opener as ``None``) so
    # every ``open_staged_store`` implementation sees the same width
    # ``_bootstrap_graph_at_path(..., dim=None)`` would have resolved
    # internally — behaviour for the Ladybug default is unchanged.
    live_dim = _stored_embedding_dim(live_nodes)
    dim = live_dim if live_dim is not None else _resolve_configured_dim(vault_path)

    open_store = open_staged_store or _ladybug_open_staged_store(bootstrap_graph_at_path)
    store, identity = open_store(vault_path, staged_path, dim)
    try:
        stats = copy_graph_canonicalizing(
            live_nodes,
            live_edges,
            equivalence,
            store,
            predicate_aliases=predicate_aliases,
            inverse_aliases=inverse_aliases,
        )
        store.close()
        _require_healthy(store, staged_path)
    except Exception:
        staging.discard(staged_path)
        raise

    result = finish_staged_swap(
        vault_path,
        staging,
        staged_path,
        lock=lock,
        # A bare ".bak" suffix, matching every other verb (and NOT a per-verb
        # tag): _active_graph_sidecars (store/staging.py) deliberately
        # excludes anything with ".bak" in its suffixes from the "live
        # sidecars" it sweeps up on the next swap, so a stale backup left
        # behind by a prior verb never gets mistaken for a live sidecar.
        # See spec facts sheet item 2 / the heal.py handoff note this fixes.
        backup_tag="bak",
        expected_identity=identity,
        dim=dim,
        stage_prefix="heal",
        audit_graph_path=audit_graph_path,
        mark_generation_verifying=mark_generation_verifying,
        publish_integrity_result=publish_integrity_result,
        # Matches heal.py's own pre-swap audit stage today (NOT the generic
        # "heal_before_swap" — see the two message overrides below for why
        # this whole call site pins heal's exact legacy wording).
        pre_swap_stage="heal_after_close_reopen",
        pre_swap_failure_message=(
            "healed staging graph failed integrity verification; "
            "live graph left unchanged"
        ),
        post_swap_failure_message=lambda backup_path: (
            "post-swap healed graph failed integrity verification; "
            f"previous graph retained at {backup_path}"
        ),
        close_live_handles=close_live_handles,
        live_graph_path=live_graph_path,
    )
    return result, stats


def reembed(
    vault_path: Path,
    lock: "RebuildLockHandle",
    *,
    live_nodes: Sequence["Node"],
    live_edges: Sequence["Edge"],
    embedder: "EmbeddingProvider",
    dim: int,
    progress: Callable[[int, int], None] | None = None,
    staging: "StagingPort",
    bootstrap_graph_at_path: BootstrapGraphAtPathFn,
    open_staged_store: OpenStagedStoreFn | None = None,
    close_live_handles: CloseLiveHandlesFn | None = None,
    batch_size: int = 32,
    max_concurrent_batches: int = 1,
    embedding_settings: Callable[[], tuple[int, int]] | None = None,
    live_graph_path: Path | None = None,
) -> tuple[StagedSwapResult, dict[str, int]]:
    """Recompute every vector at ``dim`` and swap the re-embedded graph live.

    Vectors-only — no LLM re-extraction, no topology change. ``live_nodes``/
    ``live_edges`` are already read (same call-context-specific split as
    :func:`heal`). Preserves today's documented asymmetry exactly (spec §1/§7
    OQ2): only a bare health check gates the swap, no post-swap audit runs,
    no generation gets fenced/published (``finish_staged_swap`` is called
    with ``publish_integrity=False``) — reembed changes no topology, so its
    result was never given the same scrutiny as a rebuild/heal.

    ``open_staged_store`` is the same generalized construction seam
    :func:`heal` accepts (M4 spec §2) — omitted, it defaults to the
    byte-identical Ladybug bootstrap-then-wrap closure. ``live_graph_path``
    is accepted symmetrically with :func:`heal`'s own but is inert here:
    ``publish_integrity=False`` below means :func:`finish_staged_swap` never
    reaches the post-swap audit block that parameter feeds.

    Returns ``(StagedSwapResult, stats)`` where ``stats`` is
    :func:`~okto_neuron.store.reembed.copy_graph_reembedding`'s own return
    value, for the caller's progress/state file.
    """
    staged_path = staging.stage_path("reembed")
    open_store = open_staged_store or _ladybug_open_staged_store(bootstrap_graph_at_path)
    store, identity = open_store(vault_path, staged_path, dim)
    try:
        stats = copy_graph_reembedding(
            live_nodes,
            live_edges,
            store,
            embedder,
            batch_size=batch_size,
            max_concurrent_batches=max_concurrent_batches,
            embedding_settings=embedding_settings,
            progress=progress,
        )
        store.close()
        _require_healthy(store, staged_path)
    except Exception:
        staging.discard(staged_path)
        raise

    result = finish_staged_swap(
        vault_path,
        staging,
        staged_path,
        lock=lock,
        # A bare ".bak" suffix, matching legacy reembed (kg.py's own
        # backup_graph_path was always graph_path.with_name(f"{name}.bak"))
        # and every other verb after the same fix in heal() above — see that
        # comment for why a per-verb tag is a live-sidecar-detection hazard
        # for the NEXT swap, not just a cosmetic filename mismatch.
        backup_tag="bak",
        expected_identity=identity,
        dim=dim,
        stage_prefix="reembed",
        publish_integrity=False,
        close_live_handles=close_live_handles,
        live_graph_path=live_graph_path,
    )
    return result, stats


def _require_healthy(store: "GraphStore", staged_path: Path) -> None:
    """Bare durability check on the just-closed staged graph.

    Uses ``store.health()`` (spec §2.4/§3) instead of importing
    ``verify_ladybug_db_health`` by name, retiring that by-name import at the
    two call sites this module owns (today's ``heal.py:196``,
    ``kg.py:1216``). A backend's ``health()`` opens its own fresh read-only
    connection independent of this (now-closed) handle, so it verifies the
    durably-flushed bytes exactly as the by-name call did for Ladybug.
    ``store`` is typed generically (``GraphStore``, not ``LadybugStore``)
    since ``open_staged_store`` (M4 spec §2) may hand back any backend's
    implementation.
    """
    health: "BackendHealth" = store.health()
    if not health.healthy:
        raise VaultCorrupted(staged_path, message=health.detail)


def _stored_embedding_dim(nodes: Sequence["Node"]) -> int | None:
    """Width of the first stored vector, or ``None`` when no node carries one
    (the caller then falls back to the vault's configured embedding width).
    Mirrors ``reconcile/heal.py``'s ``_stored_embedding_dim`` exactly.
    """
    for node in nodes:
        embedding = getattr(node, "embedding", None)
        if embedding:
            return len(embedding)
    return None


# ── Backend-neutral rollback availability (D-84) ──────────────────────────
#
# ``server/http.py``'s REST rollback gate and ``server/_curation.py``'s three
# daemon rollback runners each grew their own backend-specific "is there
# something to roll back to" check (Ladybug: a hardcoded artifact-dir tuple
# in ``http.py``; grafx: ``_grafx_rollback_backup_candidate``; neo4j:
# ``_neo4j_rollback_backup_candidate``) — same question, three private
# answers, one of which (Ladybug) only the REST layer asked and the daemon
# runner itself never re-verified. :func:`rollback_candidate` is the one
# shared answer: it reads each backend's own durable "previous generation"
# evidence and returns ``None`` when there is nothing to roll back to,
# without staging, auditing, or swapping anything. Both the REST gate and
# the daemon runners call it; there is no more private per-backend check.
#
# This module still never imports ``okto_neuron.cli.kg`` or
# ``okto_neuron.server`` (see the module docstring's Import boundary note),
# so the handful of directory-name literals below (Ladybug's
# ``rebuild-artifacts`` and grafx's ``.rebuild``/``.bak`` backup suffixes)
# are intentionally duplicated from their other owners
# (``cli/kg.py:_REBUILD_ARTIFACTS_DIR`` and
# ``server/_curation.py:_grafx_rollback_backup_candidate`` respectively)
# rather than imported across that boundary. They are stable on-disk format
# constants, not behaviour that drifts independently.
_REBUILD_ARTIFACTS_DIRNAME = "rebuild-artifacts"
_PREVIOUS_GRAPH_LBUG = "previous-graph.lbug"
_PREVIOUS_SEMANTIC_MATERIALIZATION = "previous-semantic-materialization.json"
_PREVIOUS_SEMANTIC_POLICY = "previous-semantic-policy.json"


@dataclass(frozen=True)
class RollbackCandidate:
    """One backend's evidence that a rollback has somewhere to go.

    ``to_generation`` is the generation tag the rollback would restore
    (populated for Ladybug and neo4j, whose checkpoints are generation-
    keyed; grafx's backup directories carry no generation tag of their own,
    so it stays ``""`` there — callers needing a display value fall back to
    ``source``). ``source`` is the human-readable evidence location (a file
    or directory path for Ladybug/grafx, the backup generation tag itself
    for neo4j) surfaced in logs/errors, not consumed programmatically.
    """

    backend: str
    to_generation: str
    source: str


def rollback_candidate(
    vault_path: Path,
    backend_name: str,
    storage_config: Any | None = None,
) -> "RollbackCandidate | None":
    """Whether ``backend_name`` has verified rollback evidence for this vault.

    Returns ``None`` when there is nothing to roll back to (a fresh vault
    that has never rebuilt/healed/reembedded, or a backup already consumed).
    Never stages, audits, or swaps anything — this is the read-only
    availability check both ``server/http.py``'s REST gate and
    ``server/_curation.py``'s daemon runners call before (respectively)
    accepting or executing a rollback job.

    - **ladybug**: the current live generation's rebuild-artifact checkpoint
      (``previous-graph.lbug`` plus its semantic-materialization/policy
      receipts) under ``<vault>/.marginalia/rebuild-artifacts/<generation>/``
      — the same generation-keyed evidence ``run_rollback``'s Ladybug branch
      restores from.
    - **grafx**: the newest sibling backup directory (``graph.grafx.rebuild``
      or ``graph.grafx.bak``) the last swap produced next to the live graph.
    - **neo4j**: the metadata singleton's ``backup_tag`` property, verified
      to still tag at least one live node — a pointer flip leaves no
      filesystem trace, so the candidate is only real if nodes at that
      generation still exist.
    """
    if backend_name == "grafx":
        return _grafx_rollback_candidate(vault_path)
    if backend_name == "neo4j":
        return _neo4j_rollback_candidate(vault_path, storage_config)
    return _ladybug_rollback_candidate(vault_path)


def _ladybug_rollback_candidate(vault_path: Path) -> "RollbackCandidate | None":
    graph_path = vault_path / _GRAPH_FILE
    if not graph_path.exists():
        return None
    identity = schema.read_graph_identity_path(graph_path)
    generation = identity.graph_generation or ""
    if not generation:
        return None
    artifact_dir = vault_path / _MARGINALIA_DIR / _REBUILD_ARTIFACTS_DIRNAME / generation
    required = (
        artifact_dir / _PREVIOUS_GRAPH_LBUG,
        artifact_dir / _PREVIOUS_SEMANTIC_MATERIALIZATION,
        artifact_dir / _PREVIOUS_SEMANTIC_POLICY,
    )
    if not all(path.is_file() for path in required):
        return None
    return RollbackCandidate(
        backend="ladybug",
        to_generation=generation,
        source=str(artifact_dir / _PREVIOUS_GRAPH_LBUG),
    )


def _grafx_rollback_candidate(vault_path: Path) -> "RollbackCandidate | None":
    from okto_neuron.store.grafx import _GRAPH_DIR_NAME

    graph_path = vault_path / _GRAPH_DIR_NAME
    candidates = [graph_path.with_name(f"{graph_path.name}.{tag}") for tag in ("rebuild", "bak")]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    newest = max(existing, key=lambda path: path.stat().st_mtime)
    return RollbackCandidate(backend="grafx", to_generation="", source=str(newest))


def _neo4j_rollback_candidate(
    vault_path: Path, storage_config: Any | None
) -> "RollbackCandidate | None":
    from okto_neuron.store.neo4j import Neo4jStore

    store = Neo4jStore.from_vault(vault_path, storage_config)
    try:
        with store._driver_boundary(), store._driver.session(database=store._database) as session:  # noqa: SLF001
            record = session.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "RETURN m.backup_tag AS backup_tag",
                {
                    "id": schema.SCHEMA_METADATA_NODE_ID,
                    "vault_id": store.vault_id,
                    "meta_generation": schema.NEO4J_METADATA_GENERATION,
                },
            ).single()
            if record is None or record["backup_tag"] is None:
                return None
            backup_tag = str(record["backup_tag"])
            count_record = session.run(
                "MATCH (n:Node {vault_id: $vault_id, _generation: $generation}) "
                "RETURN count(n) AS node_count",
                {"vault_id": store.vault_id, "generation": backup_tag},
            ).single()
            if count_record is None or not count_record["node_count"]:
                return None
        return RollbackCandidate(backend="neo4j", to_generation=backup_tag, source=backup_tag)
    finally:
        store.close()
