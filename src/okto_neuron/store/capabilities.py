"""Backend capability flags (plan section 3.6, M3 spec section 2.3).

Read by onboarding/docs surfaces (and the ``GET /api/v1/backends`` route) to
describe a backend's shape without opening it — never by the contract test
suite, which instead exercises real behavior against a live store per
``tests/store/contract``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BackendCapabilities:
    """Declarative shape of one graph backend, independent of whether it's installed."""

    name: str
    server_side_schema: bool
    native_traversal: bool
    requires_network: bool
    concurrency_model: Literal["single_writer", "mvcc", "server"]
    # Beyond plan section 3.6's literal field list (M3 spec section 2.3, D-48).
    audit_supported: bool = True
    # Whether this backend enforces the generation-scoped integrity fence on writes (a first write
    # audits the generation, a failed or incomplete audit blocks semantic writes). Only the
    # Ladybug bootstrap writes the integrity sidecar and only its store exposes the graph handle
    # the fence needs (`server/_integrity.py` `require_write_allowed`). A backend with
    # ``write_fence=False`` still supports an on-demand audit (``audit_supported``): it just never
    # blocks a write on one.
    write_fence: bool = False
    checkpoint_is_noop: bool = False
    # M4 spec section 2: CLI/onboarding/frontend-only gate (D-12) for a
    # pre-alpha-format backend -- never read by store/registry.py's
    # resolution path, which fails closed on its own structural terms.
    experimental: bool = False


LADYBUG_CAPABILITIES = BackendCapabilities(
    name="ladybug",
    server_side_schema=True,
    native_traversal=True,
    requires_network=False,
    concurrency_model="single_writer",
    write_fence=True,
    # Ladybug's checkpoint() does real WAL-merge work (store/ladybug.py); the
    # no-op default matches InMemoryStore's checkpoint() instead (D-16's
    # future Grafx supports_checkpoint flag, generalized).
)

GRAFX_CAPABILITIES = BackendCapabilities(
    name="grafx",
    server_side_schema=False,
    native_traversal=True,
    requires_network=False,
    concurrency_model="mvcc",
    # okto-grafx's own Database.checkpoint() does real WAL-recycling work
    # (verified live, returns a RecycleReport) -- True here only until M4's
    # gate 5's two-process test (OQ2) earns flipping it to False; GrafxStore
    # itself must still implement a real checkpoint() body regardless.
    checkpoint_is_noop=True,
    # No longer D-12-gated: the owner decision retiring D-12 (see
    # cli/__init__.py's now-removed `_confirm_experimental_backend`) made
    # Grafx the default, non-experimental graph backend -- the on-disk
    # format and Elastic License 2.0 + Addendum license are unchanged, but
    # neither is gated by onboarding/CLI/frontend consent any more. This
    # flag stays available on `BackendCapabilities` for any future backend
    # (in-tree or third-party via the `okto_neuron.graph_backends` entry
    # point) that genuinely needs the same one-time disclosure.
    experimental=False,
)

NEO4J_CAPABILITIES = BackendCapabilities(
    name="neo4j",
    server_side_schema=True,
    native_traversal=True,
    requires_network=True,
    concurrency_model="server",
    # Neo4j's own transaction log is the durability authority for every
    # committed write -- no Ladybug-style mid-session merge gap here (M5
    # spec §2, mirrors Grafx's D-16 rationale).
    checkpoint_is_noop=True,
    # No D-12 gate -- a stable, licensed, server-side-schema backend, not
    # pre-alpha wire format like Grafx.
    experimental=False,
)

_CAPABILITIES: dict[str, BackendCapabilities] = {
    "ladybug": LADYBUG_CAPABILITIES,
    "grafx": GRAFX_CAPABILITIES,
    "neo4j": NEO4J_CAPABILITIES,
}


def capabilities_for(name: str) -> BackendCapabilities | None:
    """The known capability flags for ``name``, or None if not registered here.

    A backend can resolve via :mod:`okto_neuron.store.registry` without a
    capabilities entry existing yet — this is a separate, optional lookup
    for onboarding/docs surfaces, not a gate on whether a backend can open.
    """
    return _CAPABILITIES.get(name)


__all__ = [
    "BackendCapabilities",
    "GRAFX_CAPABILITIES",
    "LADYBUG_CAPABILITIES",
    "NEO4J_CAPABILITIES",
    "capabilities_for",
]
