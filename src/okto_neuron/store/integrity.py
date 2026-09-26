"""Read-only, storage-agnostic graph integrity auditing.

The generic auditor proves invariants available through :class:`GraphStore`:
complete node/edge enumeration, live edge endpoints, live Claim facet references,
and optional plan-projected expected artifacts.  Backend-specific checks (for
example Ladybug relationship adjacency versus stored endpoint properties) can
extend this result without duplicating these generic checks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Literal, Protocol, runtime_checkable

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.protocol import GraphStore
from okto_neuron.store.schema import DETERMINISTIC_EDGE_IDENTITY_CONTRACTS


class AuditStatus(StrEnum):
    """Lifecycle and terminal states for one graph generation's audit."""

    UNVERIFIED = "unverified"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


ArtifactKind = Literal["node", "edge"]


@dataclass(frozen=True, slots=True)
class ExpectedArtifact:
    """Ledger-independent projection of one artifact a plan expects to exist."""

    kind: ArtifactKind
    id: str
    type: str | None = None
    src: str | None = None
    dst: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedArtifactManifest:
    """Expected artifacts projected from durable plans or receipts.

    ``complete=False`` is intentionally contagious: a partial/paginated manifest
    can still reveal mismatches, but it can never produce a verified audit.
    """

    artifacts: tuple[ExpectedArtifact, ...] = ()
    complete: bool = True
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScanCompleteness:
    """Caller attestation for adapters that expose paginated populations.

    ``GraphStore.list_nodes`` and ``list_edges`` are complete-population APIs, so
    direct store callers use the defaults.  A paginated adapter must set the
    affected flag to ``False`` unless it reached the final page.
    """

    nodes: bool = True
    edges: bool = True
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EdgeAdjacencyObservation:
    """One edge's physical adjacency and independently stored properties."""

    edge_id: str | None
    edge_type: str | None
    actual_src: str
    actual_dst: str
    stored_src: str | None
    stored_dst: str | None


@runtime_checkable
class EdgeAdjacencyReader(Protocol):
    """Optional backend capability for independent adjacency observation."""

    def list_edge_adjacency(self) -> Iterable[EdgeAdjacencyObservation]: ...


@dataclass(frozen=True, slots=True, order=True)
class IntegrityIssue:
    """Stable, sortable issue record safe for diagnostics."""

    code: str
    artifact_kind: str
    artifact_id: str
    reference: str = ""
    expected: str = ""
    observed: str = ""


@dataclass(frozen=True, slots=True)
class IntegrityAuditResult:
    status: AuditStatus
    nodes_scanned: int
    edges_scanned: int
    adjacency_scanned: int
    expected_artifacts_checked: int
    issue_count: int
    issues: tuple[IntegrityIssue, ...]
    nodes_complete: bool
    edges_complete: bool
    adjacency_complete: bool | None
    manifest_complete: bool
    duration_ms: float
    incomplete_reasons: tuple[str, ...] = ()
    graph_generation: str | None = None
    identity_contract_version: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.nodes_complete
            and self.edges_complete
            and self.adjacency_complete is not False
            and self.manifest_complete
        )

    @property
    def verified(self) -> bool:
        return self.status is AuditStatus.VERIFIED


_CLAIM_REFERENCE_FACETS: tuple[str, ...] = (
    "O_id",
    "S_id",
    "agent_id",
    "block_id",
    "extraction_activity_id",
)

# Deterministic ids are 64 lowercase hex characters (``sha256_hex``); random edge
# ids are 16 (``_uid``), so the shape alone qualifies an edge as content-addressed.
# Qualifying by shape rather than by edge type is deliberate: it covers every
# deterministic family (provenance, source-mention/bridge, supersedence, and
# deterministic topology edges, whose predicates are open) without claiming a
# promise about hand-assigned ids that were never content-addressed.
_DETERMINISTIC_ID = re.compile(r"^[0-9a-f]{64}$")

_INCOMPLETE_ISSUE_CODES = frozenset(
    {
        "edge_scan_error",
        "adjacency_scan_error",
        "manifest_incomplete",
        "node_scan_error",
        "population_incomplete",
    }
)


def audit_graph(
    store: GraphStore,
    *,
    expected: ExpectedArtifactManifest | Iterable[ExpectedArtifact] | None = None,
    scan: ScanCompleteness | None = None,
    graph_generation: str | None = None,
    identity_contract_version: str | None = None,
    max_issue_samples: int = 100,
) -> IntegrityAuditResult:
    """Audit ``store`` without mutating it.

    Enumeration exceptions preserve the successfully scanned prefix but produce
    ``incomplete`` unless another observed mismatch already proves ``failed``.
    Issue samples are sorted before truncation, so diagnostics do not depend on
    backend iteration order.
    """
    if max_issue_samples < 0:
        raise ValueError("max_issue_samples must be non-negative")

    started_at = perf_counter()
    manifest = _coerce_manifest(expected)
    completeness = scan or ScanCompleteness()
    issues: list[IntegrityIssue] = []
    incomplete_reasons: list[str] = []

    nodes, nodes_scanned, nodes_complete = _scan_nodes(store, issues, incomplete_reasons)
    edges, edges_scanned, edges_complete = _scan_edges(store, issues, incomplete_reasons)
    adjacency, adjacency_scanned, adjacency_complete = _scan_adjacency(
        store, issues, incomplete_reasons
    )
    nodes_complete = nodes_complete and completeness.nodes
    edges_complete = edges_complete and completeness.edges
    if not completeness.nodes or not completeness.edges:
        reason = completeness.incomplete_reason or "caller did not attest complete populations"
        incomplete_reasons.append(reason)
        issues.append(
            IntegrityIssue(
                code="population_incomplete",
                artifact_kind="graph",
                artifact_id="",
                expected="complete node and edge populations",
                observed=reason,
            )
        )

    if nodes_complete:
        _validate_edges(nodes, edges, issues)
        _validate_facet_references(nodes, issues)
    if identity_contract_version in DETERMINISTIC_EDGE_IDENTITY_CONTRACTS:
        _validate_deterministic_edge_identity(edges, issues)
    if adjacency_complete is not None:
        _validate_adjacency(
            edges,
            adjacency,
            edges_complete=edges_complete,
            adjacency_complete=adjacency_complete,
            issues=issues,
        )
    expected_artifacts_checked = _validate_expected_artifacts(
        nodes,
        edges,
        manifest.artifacts,
        nodes_complete=nodes_complete,
        edges_complete=edges_complete,
        issues=issues,
    )

    if not manifest.complete:
        reason = manifest.incomplete_reason or "expected-artifact manifest is incomplete"
        incomplete_reasons.append(reason)
        issues.append(
            IntegrityIssue(
                code="manifest_incomplete",
                artifact_kind="manifest",
                artifact_id="",
                expected="complete expected-artifact population",
                observed=reason,
            )
        )

    ordered_issues = tuple(sorted(issues))
    has_proven_mismatch = any(issue.code not in _INCOMPLETE_ISSUE_CODES for issue in ordered_issues)
    complete = (
        nodes_complete and edges_complete and adjacency_complete is not False and manifest.complete
    )
    if has_proven_mismatch:
        status = AuditStatus.FAILED
    elif not complete:
        status = AuditStatus.INCOMPLETE
    else:
        status = AuditStatus.VERIFIED

    return IntegrityAuditResult(
        status=status,
        nodes_scanned=nodes_scanned,
        edges_scanned=edges_scanned,
        adjacency_scanned=adjacency_scanned,
        expected_artifacts_checked=expected_artifacts_checked,
        issue_count=len(ordered_issues),
        issues=ordered_issues[:max_issue_samples],
        nodes_complete=nodes_complete,
        edges_complete=edges_complete,
        adjacency_complete=adjacency_complete,
        manifest_complete=manifest.complete,
        duration_ms=(perf_counter() - started_at) * 1000,
        incomplete_reasons=tuple(sorted(set(incomplete_reasons))),
        graph_generation=graph_generation,
        identity_contract_version=identity_contract_version,
    )


def _coerce_manifest(
    expected: ExpectedArtifactManifest | Iterable[ExpectedArtifact] | None,
) -> ExpectedArtifactManifest:
    if expected is None:
        return ExpectedArtifactManifest()
    if isinstance(expected, ExpectedArtifactManifest):
        return expected
    artifacts: list[ExpectedArtifact] = []
    try:
        artifacts.extend(expected)
    except Exception as exc:
        return ExpectedArtifactManifest(
            artifacts=tuple(artifacts),
            complete=False,
            incomplete_reason=f"expected-artifact scan failed: {_exception_text(exc)}",
        )
    return ExpectedArtifactManifest(artifacts=tuple(artifacts))


def _scan_nodes(
    store: GraphStore,
    issues: list[IntegrityIssue],
    incomplete_reasons: list[str],
) -> tuple[dict[str, Node], int, bool]:
    nodes: dict[str, Node] = {}
    scanned = 0
    try:
        for node in store.list_nodes():
            scanned += 1
            node_id = str(node.id)
            if node_id in nodes:
                issues.append(
                    IntegrityIssue(
                        code="duplicate_node_id",
                        artifact_kind="node",
                        artifact_id=node_id,
                        expected="one node per id",
                        observed="duplicate",
                    )
                )
                continue
            nodes[node_id] = node
    except Exception as exc:  # store adapters define their own failure hierarchy
        reason = _exception_text(exc)
        incomplete_reasons.append(f"node scan failed: {reason}")
        issues.append(
            IntegrityIssue(
                code="node_scan_error",
                artifact_kind="graph",
                artifact_id="",
                expected="complete node population",
                observed=reason,
            )
        )
        return nodes, scanned, False
    return nodes, scanned, True


def _scan_edges(
    store: GraphStore,
    issues: list[IntegrityIssue],
    incomplete_reasons: list[str],
) -> tuple[dict[str, Edge], int, bool]:
    edges: dict[str, Edge] = {}
    scanned = 0
    try:
        for edge in store.list_edges():
            scanned += 1
            edge_id = str(edge.id)
            if edge_id in edges:
                issues.append(
                    IntegrityIssue(
                        code="duplicate_edge_id",
                        artifact_kind="edge",
                        artifact_id=edge_id,
                        expected="one edge per id",
                        observed="duplicate",
                    )
                )
                continue
            edges[edge_id] = edge
    except Exception as exc:  # store adapters define their own failure hierarchy
        reason = _exception_text(exc)
        incomplete_reasons.append(f"edge scan failed: {reason}")
        issues.append(
            IntegrityIssue(
                code="edge_scan_error",
                artifact_kind="graph",
                artifact_id="",
                expected="complete edge population",
                observed=reason,
            )
        )
        return edges, scanned, False
    return edges, scanned, True


def _scan_adjacency(
    store: GraphStore,
    issues: list[IntegrityIssue],
    incomplete_reasons: list[str],
) -> tuple[dict[str, EdgeAdjacencyObservation], int, bool | None]:
    if not isinstance(store, EdgeAdjacencyReader):
        return {}, 0, None

    observations: dict[str, EdgeAdjacencyObservation] = {}
    scanned = 0
    try:
        for observation in store.list_edge_adjacency():
            scanned += 1
            edge_id = observation.edge_id
            if edge_id is None:
                issues.append(
                    IntegrityIssue(
                        code="missing_edge_property",
                        artifact_kind="edge",
                        artifact_id=f"{observation.actual_src}->{observation.actual_dst}",
                        reference="id",
                        expected="stored edge id",
                        observed="missing",
                    )
                )
                continue
            if observation.edge_type is None:
                issues.append(
                    IntegrityIssue(
                        code="missing_edge_property",
                        artifact_kind="edge",
                        artifact_id=edge_id,
                        reference="type",
                        expected="stored edge type",
                        observed="missing",
                    )
                )
            if edge_id in observations:
                issues.append(
                    IntegrityIssue(
                        code="duplicate_adjacency_edge_id",
                        artifact_kind="edge",
                        artifact_id=edge_id,
                        expected="one physical relationship per edge id",
                        observed="duplicate",
                    )
                )
                continue
            observations[edge_id] = observation
    except Exception as exc:  # backend adapters define their own failure hierarchy
        reason = _exception_text(exc)
        incomplete_reasons.append(f"adjacency scan failed: {reason}")
        issues.append(
            IntegrityIssue(
                code="adjacency_scan_error",
                artifact_kind="graph",
                artifact_id="",
                expected="complete physical edge adjacency population",
                observed=reason,
            )
        )
        return observations, scanned, False
    return observations, scanned, True


def _validate_edges(
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    issues: list[IntegrityIssue],
) -> None:
    for edge_id in sorted(edges):
        edge = edges[edge_id]
        for endpoint, node_id in (("src", str(edge.src)), ("dst", str(edge.dst))):
            if node_id in nodes:
                continue
            issues.append(
                IntegrityIssue(
                    code="missing_edge_endpoint",
                    artifact_kind="edge",
                    artifact_id=edge_id,
                    reference=endpoint,
                    expected=node_id,
                    observed="missing",
                )
            )


def _validate_adjacency(
    edges: dict[str, Edge],
    observations: dict[str, EdgeAdjacencyObservation],
    *,
    edges_complete: bool,
    adjacency_complete: bool,
    issues: list[IntegrityIssue],
) -> None:
    for edge_id in sorted(observations):
        observation = observations[edge_id]
        for endpoint, actual, stored in (
            ("src", observation.actual_src, observation.stored_src),
            ("dst", observation.actual_dst, observation.stored_dst),
        ):
            if actual == stored:
                continue
            issues.append(
                IntegrityIssue(
                    code="adjacency_property_mismatch",
                    artifact_kind="edge",
                    artifact_id=edge_id,
                    reference=endpoint,
                    expected=actual,
                    observed=stored if stored is not None else "missing",
                )
            )

    if not edges_complete or not adjacency_complete:
        return
    edge_ids = set(edges)
    observation_ids = set(observations)
    for edge_id in sorted(edge_ids - observation_ids):
        issues.append(
            IntegrityIssue(
                code="missing_adjacency_observation",
                artifact_kind="edge",
                artifact_id=edge_id,
                expected="physical relationship",
                observed="missing",
            )
        )
    for edge_id in sorted(observation_ids - edge_ids):
        issues.append(
            IntegrityIssue(
                code="unlisted_physical_edge",
                artifact_kind="edge",
                artifact_id=edge_id,
                expected="edge property scan record",
                observed="physical relationship only",
            )
        )


def _validate_deterministic_edge_identity(
    edges: dict[str, Edge],
    issues: list[IntegrityIssue],
) -> None:
    """Prove content-addressed edge ids against their own semantic identity.

    Runs only for identity contracts that declare the deterministic edge-id
    promise. Every deterministic edge id is ``sha256_hex("edge", src, type, dst)``
    (companion provenance, source-mention and bridge edges, supersedence, and
    deterministic topology edges all mint it that way), so an endpoint or type
    rewritten under a standing id is observable without any external manifest.
    """
    from okto_neuron.ingest.markdown import sha256_hex

    by_identity: dict[str, list[str]] = {}
    for edge_id in sorted(edges):
        edge = edges[edge_id]
        if not _DETERMINISTIC_ID.match(edge_id):
            continue
        identity = sha256_hex("edge", str(edge.src), str(edge.type), str(edge.dst))
        by_identity.setdefault(identity, []).append(edge_id)
        if identity == edge_id:
            continue
        issues.append(
            IntegrityIssue(
                code="content_addressed_id_mismatch",
                artifact_kind="edge",
                artifact_id=edge_id,
                reference="id",
                expected=identity,
                observed=edge_id,
            )
        )

    for identity in sorted(by_identity):
        edge_ids = sorted(by_identity[identity])
        if len(edge_ids) < 2:
            continue
        # Scrambled endpoints can converge thousands of edges on one identity, so
        # the shared sample is built once and truncated rather than per issue.
        observed = f"{len(edge_ids)} ids: " + ",".join(edge_ids[:3])
        for edge_id in edge_ids:
            issues.append(
                IntegrityIssue(
                    code="conflicting_deterministic_identity",
                    artifact_kind="edge",
                    artifact_id=edge_id,
                    reference=identity,
                    expected="one edge id per deterministic semantic identity",
                    observed=observed,
                )
            )


def _validate_facet_references(nodes: dict[str, Node], issues: list[IntegrityIssue]) -> None:
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if node.type != "Claim":
            continue
        facets = node.facets or {}
        for facet_name in _CLAIM_REFERENCE_FACETS:
            value = facets.get(facet_name)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                issues.append(
                    IntegrityIssue(
                        code="invalid_facet_reference",
                        artifact_kind="node",
                        artifact_id=node_id,
                        reference=facet_name,
                        expected="node id string",
                        observed=type(value).__name__,
                    )
                )
                continue
            if value not in nodes:
                issues.append(
                    IntegrityIssue(
                        code="missing_facet_reference",
                        artifact_kind="node",
                        artifact_id=node_id,
                        reference=facet_name,
                        expected=value,
                        observed="missing",
                    )
                )


def _validate_expected_artifacts(
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    artifacts: tuple[ExpectedArtifact, ...],
    *,
    nodes_complete: bool,
    edges_complete: bool,
    issues: list[IntegrityIssue],
) -> int:
    seen: dict[tuple[ArtifactKind, str], ExpectedArtifact] = {}
    for artifact in sorted(artifacts, key=_expected_artifact_key):
        if artifact.kind not in ("node", "edge"):
            issues.append(
                IntegrityIssue(
                    code="invalid_expected_artifact_kind",
                    artifact_kind=str(artifact.kind),
                    artifact_id=artifact.id,
                    expected="node or edge",
                    observed=str(artifact.kind),
                )
            )
            continue
        identity = (artifact.kind, artifact.id)
        if identity in seen:
            if seen[identity] != artifact:
                issues.append(
                    IntegrityIssue(
                        code="conflicting_expected_artifact",
                        artifact_kind=artifact.kind,
                        artifact_id=artifact.id,
                        expected=_expected_artifact_description(seen[identity]),
                        observed=_expected_artifact_description(artifact),
                    )
                )
            continue
        seen[identity] = artifact
        observed = nodes.get(artifact.id) if artifact.kind == "node" else edges.get(artifact.id)
        if observed is None:
            population_complete = nodes_complete if artifact.kind == "node" else edges_complete
            if not population_complete:
                continue
            issues.append(
                IntegrityIssue(
                    code="missing_expected_artifact",
                    artifact_kind=artifact.kind,
                    artifact_id=artifact.id,
                    expected=artifact.type or artifact.kind,
                    observed="missing",
                )
            )
            continue
        if artifact.type is not None and observed.type != artifact.type:
            issues.append(
                IntegrityIssue(
                    code="expected_artifact_type_mismatch",
                    artifact_kind=artifact.kind,
                    artifact_id=artifact.id,
                    reference="type",
                    expected=artifact.type,
                    observed=str(observed.type),
                )
            )
        if artifact.kind != "edge":
            continue
        edge = observed
        if artifact.src is not None and edge.src != artifact.src:
            issues.append(
                IntegrityIssue(
                    code="expected_artifact_endpoint_mismatch",
                    artifact_kind="edge",
                    artifact_id=artifact.id,
                    reference="src",
                    expected=artifact.src,
                    observed=str(edge.src),
                )
            )
        if artifact.dst is not None and edge.dst != artifact.dst:
            issues.append(
                IntegrityIssue(
                    code="expected_artifact_endpoint_mismatch",
                    artifact_kind="edge",
                    artifact_id=artifact.id,
                    reference="dst",
                    expected=artifact.dst,
                    observed=str(edge.dst),
                )
            )
    return len(seen)


def _expected_artifact_key(artifact: ExpectedArtifact) -> tuple[str, ...]:
    return (
        artifact.kind,
        artifact.id,
        artifact.type or "",
        artifact.src or "",
        artifact.dst or "",
    )


def _expected_artifact_description(artifact: ExpectedArtifact) -> str:
    return "|".join((artifact.kind, artifact.type or "", artifact.src or "", artifact.dst or ""))


def _exception_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


__all__ = [
    "AuditStatus",
    "EdgeAdjacencyObservation",
    "EdgeAdjacencyReader",
    "ExpectedArtifact",
    "ExpectedArtifactManifest",
    "IntegrityAuditResult",
    "IntegrityIssue",
    "ScanCompleteness",
    "audit_graph",
]
