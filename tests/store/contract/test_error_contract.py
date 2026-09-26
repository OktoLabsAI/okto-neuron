"""Backend-agnostic error contract: a driver exception must not escape the store.

Every backend wraps some third-party driver. When that driver's own exception
type reaches a caller, the store abstraction has leaked: to `server/http.py` the
failure is an ordinary `Exception`, so it falls past `except OktoNeuronError`
into the bare catch-all and the client is told only "internal server error",
with the real cause visible nowhere but the daemon's stderr.

That is not hypothetical. Grafx caps a query list at 1024 elements; `ask` on a
1,477-embedded-node vault running the published 0.0.50 wheel answered

    {"error":"internal","detail":"internal server error"}

while the actual `GrafxConfigurationError` went to stderr. See
`okto_neuron.errors.GraphBackendError`, added under D-41's reversal trigger in
the internal ADR 0041 plan.

The assertion here is deliberately "no RAW DRIVER exception escapes", not
"everything is a OktoNeuronError". `test_graph_store.py` already pins `ValueError`
across all five backends for invalid edge endpoints and off-schema node types
(lines 39 and 48), and those are caller-input errors, not backend failures — a
blanket rule would fight a contract that is working as intended.
"""

from __future__ import annotations


from okto_neuron.errors import OktoNeuronError

# Driver packages a backend may wrap. An exception whose class lives in one of
# these module namespaces is by definition un-translated.
_DRIVER_ROOTS = ("okto_grafx", "ladybug", "neo4j")


def _driver_package(exc: BaseException) -> str | None:
    module = type(exc).__module__ or ""
    for root in _DRIVER_ROOTS:
        if module == root or module.startswith(root + "."):
            return root
    return None


def _assert_not_a_driver_exception(exc: BaseException, operation: str) -> None:
    leaked = _driver_package(exc)
    assert leaked is None, (
        f"{operation} leaked {type(exc).__module__}.{type(exc).__name__} from the "
        f"{leaked!r} driver. Translate it at the store boundary "
        f"(okto_neuron.errors.GraphBackendError) so callers can catch it as a "
        f"OktoNeuronError instead of hitting the server's bare catch-all."
    )


def test_reads_against_a_closed_store_do_not_leak_driver_exceptions(graph_store) -> None:
    """Closing then reading is the cheapest failure every backend can produce."""
    graph_store.close()

    for operation, call in (
        ("get_node", lambda: graph_store.get_node("no-such-node")),
        ("get_nodes", lambda: graph_store.get_nodes(["no-such-node"])),
        ("list_nodes", lambda: list(graph_store.list_nodes())),
        ("list_edges", lambda: list(graph_store.list_edges())),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 — the type is what's under test
            _assert_not_a_driver_exception(exc, operation)


def test_oversized_id_batch_does_not_leak_a_driver_exception(graph_store) -> None:
    """A read far wider than any backend's internal query-list cap.

    This is the shape that broke `ask` in 0.0.50: Grafx rejects an ``IN $ids``
    list over 1024 elements, and `query._vector_seeds` passes every embedded
    node id in one call. `GrafxStore.get_nodes` now batches, so this must
    succeed — and if a backend ever fails here instead, it must fail in
    Okto Neuron's own vocabulary rather than its driver's.
    """
    ids = [f"absent-{i:05d}" for i in range(2500)]
    try:
        result = graph_store.get_nodes(ids)
    except Exception as exc:  # noqa: BLE001 — the type is what's under test
        _assert_not_a_driver_exception(exc, "get_nodes(2500 ids)")
        raise
    assert result == [], "none of these ids exist, so nothing should come back"


def test_graph_backend_error_is_catchable_as_a_marginalia_error() -> None:
    """The point of the whole contract: `except OktoNeuronError` must catch it."""
    from okto_neuron.errors import GraphBackendError

    assert issubclass(GraphBackendError, OktoNeuronError)

    err = GraphBackendError("driver said no", backend="grafx")
    assert err.backend == "grafx"
    assert "grafx backend" in str(err)
