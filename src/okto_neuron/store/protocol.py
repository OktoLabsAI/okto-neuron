"""GraphStore protocol — the contract every backend implements.

Retrieval (BM25, vector scan) lives behind okto_neuron.store.index.IndexStore.

Optional extension (M2c, D-39, plan §3.1): a backend MAY additionally expose

    def snapshot(self) -> AbstractContextManager[str]: ...

Entering it pins one consistent read point for the duration of the ``with``
block and yields the generation id observed at that instant; every
``list_nodes``/``list_edges`` call issued through the *same store instance*
while the block is open reads through that pinned point instead of the live
one. Deliberately left off the ``GraphStore`` class body below (not a
required Protocol member): ``GraphStore`` is ``@runtime_checkable``, and this
capability is genuinely optional — Grafx/Neo4j/Neptune do not implement it
today, degrading instead to ``store/snapshot.py``'s documented refuse-while-
unlocked fallback — so making it a structural requirement would make
``isinstance(store, GraphStore)`` reject those backends. Callers detect
support with ``hasattr(store, "snapshot")``. Only :class:`~okto_neuron.store.
ladybug.LadybugStore` implements it as of M2c; see that module for the
mechanism (a fresh read-only handle held open across a concurrent physical
file swap, verified against Ladybug's actual connection-reuse behavior per
the plan's own explicit flag rather than assumed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

from okto_neuron.core.schema import Edge, Node


@dataclass(frozen=True)
class BackendHealth:
    """Result of a backend self-check: :meth:`GraphStore.health`."""

    healthy: bool
    detail: str


@dataclass(frozen=True)
class RecoveryStatus:
    """Result of :meth:`GraphStore.recovery_status`.

    ``mode`` is backend-defined and only meaningful when ``recovered`` is
    True (Ladybug uses ``"checkpoint"``/``"empty"``, see
    ``VaultGraphHandle.recovered_mode``); ``detail`` is an optional
    free-text elaboration for logs/UI, not for programmatic branching.
    """

    recovered: bool
    mode: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DriftReport:
    """Result of :meth:`GraphStore.detect_drift` when drift was found."""

    reason: str


@runtime_checkable
class GraphStore(Protocol):
    def add_node(self, node: Node) -> None: ...
    def add_edge(self, edge: Edge) -> None: ...
    def get_node(self, node_id: str) -> Optional[Node]: ...
    def get_nodes(self, node_ids: Iterable[str]) -> list[Node]:
        """Batch read in input order; duplicates collapse, missing ids are skipped."""
        ...

    def list_nodes(self, type: Optional[str] = None) -> Iterable[Node]: ...
    def list_edges(
        self, src: Optional[str] = None, dst: Optional[str] = None, type: Optional[str] = None
    ) -> Iterable[Edge]: ...
    def checkpoint(self) -> None:
        """Force durably-committed writes so far to survive an unclean shutdown.

        A backend that only merges its write-ahead log into the durable file on a
        clean close (Ladybug) must expose an explicit mid-session merge here so a
        long-running writer can checkpoint at safe drain points, not only at exit.
        A backend with no such distinction (in-memory, or one that is already
        durable per-write) may implement this as a no-op.
        """
        ...

    def close(self) -> None: ...

    @property
    def is_closed(self) -> bool:
        """True once ``close()`` has run on this store, False before.

        Read-only. The vault open cache (``store/vault.py``'s
        ``_open_vault``) reads this to tell a still-live cached handle apart
        from one a caller has already closed (e.g. via a prior CLI
        invocation's own cleanup), so it drops the stale entry and opens a
        fresh handle instead of reflecting into a backend-private attribute
        (D-49).
        """
        ...

    def generation(self) -> str:
        """This store's current graph-identity stamp.

        A narrow, deliberately-scoped slice of the eventual ``HealthPort``
        (M2b): just enough for a caller to fence a swap against the generation
        it observed, without reflecting into backend-private attributes.
        """
        ...

    def health(self) -> "BackendHealth":
        """Self-check this store's backing storage without raising.

        Same M2b scoping note as :meth:`generation`.
        """
        ...

    def recovery_status(self) -> "RecoveryStatus":
        """Whether opening this store had to recover from a bad on-disk state.

        The M3 slice of ``HealthPort`` (see plan section 3.8): retires the
        ``getattr(store, "_graph_handle", None).recovered_from_corruption``-
        family reflection sites without exposing the handle itself.
        """
        ...

    def detect_drift(self, expected_generation: str | None) -> "DriftReport | None":
        """Check this store's on-disk identity against ``expected_generation``.

        Returns None when nothing has drifted (the on-disk graph still
        matches both ``expected_generation`` and the identity this store
        observed when it was opened); otherwise a :class:`DriftReport`
        naming what moved. Backend-internal — no hardcoded file name, no
        caller-side reflection into private attributes.
        """
        ...
