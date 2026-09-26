"""Graph-backend registry: resolves a backend name to a ``GraphStore`` class.

Two sources, official-first (plan section 3.5, M3 spec section 2.2):

- ``_OFFICIAL`` — a small in-tree ``name -> "module:attr"`` map, imported
  lazily inside :func:`resolve_graph_backend` so merely importing this
  module never pulls in a backend's own dependencies (mirrors
  ``Vault._open_store``'s ``ModuleNotFoundError`` handling for the optional
  ``ladybug`` extra).
- the ``okto_neuron.graph_backends`` entry-point group (and the pre-0.3.0
  ``marginalia.graph_backends`` group, still read) — anything installed
  separately (a third-party backend, or a test fixture stub package) that
  never needs an in-tree change to become resolvable.

Resolution fails closed: an unresolvable name, or a resolved class that does
not structurally satisfy :class:`~okto_neuron.store.protocol.GraphStore`
(including an incompatible method signature), both raise
:class:`NoSuchBackendError` at registration time, not on first write.
"""

from __future__ import annotations

import importlib
import inspect
from okto_neuron._compat import (
    GRAPH_BACKENDS_GROUP,
    LEGACY_GRAPH_BACKENDS_GROUP,
    iter_entry_points,
)
from typing import ClassVar

from okto_neuron.errors import OktoNeuronError
from okto_neuron.store.protocol import GraphStore


# "module:attr" targets, imported lazily — see module docstring. Declaration
# order is enumeration order (`list_graph_backends()` below): grafx first,
# since it is now the default, non-experimental graph backend; ladybug and
# neo4j remain fully supported, selectable backends.
_OFFICIAL: dict[str, str] = {
    "grafx": "okto_neuron.store.grafx:GrafxStore",
    "ladybug": "okto_neuron.store.ladybug:LadybugStore",
    "neo4j": "okto_neuron.store.neo4j:Neo4jStore",
}


class NoSuchBackendError(OktoNeuronError):
    """A requested graph-backend name did not resolve to a usable class."""

    default_message: ClassVar[str] = "no such graph backend is registered"


def resolve_graph_backend(name: str) -> type[GraphStore]:
    """Resolve ``name`` to its ``GraphStore`` class.

    Checks :data:`_OFFICIAL` first, then the ``okto_neuron.graph_backends`` (then legacy ``marginalia.graph_backends``)
    entry-point group. Raises :class:`NoSuchBackendError` when neither source
    has ``name``, or when the resolved class fails structural validation
    against :class:`GraphStore` (a missing method or property, or a method
    whose signature can't accept the protocol's own call shape).
    """
    target = _OFFICIAL.get(name)
    cls = _import_target(name, target) if target is not None else _resolve_from_entry_points(name)
    _validate_backend_class(cls)
    return cls


def list_graph_backends() -> list[str]:
    """Names of every backend resolvable in this process right now.

    Official names first (in :data:`_OFFICIAL`'s declaration order), then
    any additional entry-point-registered name, deduplicated. Enumerates
    names only — does not import or validate any class, so it is safe to
    call before deciding whether a given backend is actually usable
    (that's what :func:`resolve_graph_backend` is for). Backs the
    ``GET /api/v1/backends`` route and onboarding text.
    """
    names = list(_OFFICIAL)
    for entry_point in iter_entry_points(GRAPH_BACKENDS_GROUP, LEGACY_GRAPH_BACKENDS_GROUP):
        if entry_point.name not in names:
            names.append(entry_point.name)
    return names


def _import_target(name: str, target: str) -> type:
    """Import an ``_OFFICIAL`` "module:attr" target, naming the missing extra
    when it fails because the backend's own optional dependency isn't
    installed (mirrors ``Vault._open_store``'s ``ModuleNotFoundError``
    handling for the ``ladybug`` extra, generalized to any official
    backend).

    Two distinct failures both surface as ``ModuleNotFoundError`` here, and
    only one of them is a missing-extra problem:

    - ``exc.name == module_name``: the in-tree module itself (e.g.
      ``okto_neuron.store.grafx``) doesn't exist yet -- a real bug, not
      something installing an extra would fix. Re-raised unchanged.
    - any other ``exc.name``: ``module_name`` imported far enough to reach
      one of *its own* imports (e.g. ``okto_neuron.store.ladybug``'s
      ``import ladybug``), which failed -- that's the optional dependency
      missing. Raised as a :class:`NoSuchBackendError` naming
      ``okto-neuron[<name>]`` as the fix, so a caller sees actionable text
      instead of a bare third-party ``ModuleNotFoundError``.
    """
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise
        raise NoSuchBackendError(
            f"graph backend {name!r} is registered but its optional dependency "
            f"is not installed ({exc}); install okto-neuron[{name}] to use it"
        ) from exc
    return getattr(module, attr)


def _resolve_from_entry_points(name: str) -> type:
    for entry_point in iter_entry_points(
        GRAPH_BACKENDS_GROUP, LEGACY_GRAPH_BACKENDS_GROUP, name=name
    ):
        return entry_point.load()
    raise NoSuchBackendError(f"no such graph backend is registered: {name!r}")


def _validate_backend_class(cls: type) -> None:
    """Fail closed on a backend class that can't actually serve as a ``GraphStore``.

    Deliberately does NOT use ``issubclass(cls, GraphStore)``: since D-49
    added ``is_closed`` (a ``@property``, a non-method member) to the
    Protocol, ``@runtime_checkable``'s own ``issubclass`` support raises
    ``TypeError("Protocols with non-method members don't support
    issubclass()")`` unconditionally (a hard CPython ``typing`` limitation
    on any runtime-checkable protocol with a non-callable member — nothing
    to do with a particular candidate class). ``isinstance()`` on an
    instance still works, but validating a *class* here (before ever
    constructing one) is the whole point (module docstring: "fails closed
    ... at registration time, not on first write"), so this checks presence
    of every protocol member (methods AND properties) via plain
    ``hasattr``/``vars`` introspection instead of Python's Protocol
    machinery, then separately verifies every *method*'s call signature is
    compatible, and every *property* is actually a property rather than a
    plain method wearing the right name (a property has no call signature
    to bind against, but a bound-method-instead-of-a-property is its own
    silent failure mode — see :func:`_require_property_member`).
    """
    method_names = _graph_store_method_names()
    property_names = _graph_store_property_names()
    missing = sorted(name for name in (*method_names, *property_names) if not hasattr(cls, name))
    if missing:
        raise NoSuchBackendError(
            f"{cls.__module__}.{cls.__qualname__} does not implement GraphStore "
            f"(missing members: {', '.join(missing)})"
        )
    for name in method_names:
        _require_compatible_signature(cls, name)
    for name in property_names:
        _require_property_member(cls, name)


def _graph_store_method_names() -> list[str]:
    return [name for name, value in vars(GraphStore).items() if not name.startswith("_") and callable(value)]


def _graph_store_property_names() -> list[str]:
    """Protocol members declared as ``@property`` (e.g. ``is_closed``).

    A ``property`` object is not itself callable, so these are excluded from
    :func:`_graph_store_method_names` and from the per-method signature check
    (a property has no call signature to bind against); they still count
    toward "does this class implement GraphStore at all" so a class missing
    one fails closed with the property named in the detail, not silently
    folded into a generic "structural check failed".
    """
    return [name for name, value in vars(GraphStore).items() if not name.startswith("_") and isinstance(value, property)]


def _require_compatible_signature(cls: type, name: str) -> None:
    protocol_func = vars(GraphStore)[name]
    concrete_func = getattr(cls, name)
    try:
        protocol_params = list(inspect.signature(protocol_func).parameters.values())[1:]  # drop self
        concrete_sig = inspect.signature(concrete_func)
    except (TypeError, ValueError):
        return  # not introspectable (e.g. a C-extension method) — nothing more to check
    kwargs = {
        param.name: object()
        for param in protocol_params
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }
    try:
        concrete_sig.bind(object(), **kwargs)  # object() stands in for the bound `self`
    except TypeError as exc:
        raise NoSuchBackendError(
            f"{cls.__module__}.{cls.__qualname__}.{name} has a signature incompatible "
            f"with GraphStore.{name}: {exc}"
        ) from exc


def _require_property_member(cls: type, name: str) -> None:
    """A GraphStore property member (e.g. ``is_closed``) must actually be a
    property (or another non-callable descriptor) on the concrete class,
    not a plain method wearing the right name.

    ``hasattr(cls, name)`` alone can't tell those apart — a method passes it
    too. The distinction matters because every caller reads a property
    member as a value, never calls it: ``store/vault.py``'s cache-freshness
    check does ``not cached.is_closed``, and an unbound-but-accessed method
    evaluates to a bound-method object, which is always truthy — so a
    backend that defines ``def is_closed(self)`` instead of
    ``@property def is_closed(self)`` would silently defeat that check
    (every open forever looks "not closed", or the cache is popped and a
    fresh handle opened on every single call, depending on which branch's
    truthiness this trips) instead of failing at registration the way this
    whole validator exists to guarantee.

    Uses ``inspect.getattr_static`` (not ``getattr``) so this reads the raw
    class-level descriptor without triggering the property's own getter (or
    any ``__get__``) as a side effect during validation.
    """
    try:
        attr = inspect.getattr_static(cls, name)
    except AttributeError:
        # Not resolvable via static class/MRO lookup (e.g. only reachable
        # through a dynamic __getattr__) -- already covered by the earlier
        # hasattr-based presence check; nothing more to introspect here.
        return
    if callable(attr) and not isinstance(attr, (property, staticmethod, classmethod)):
        raise NoSuchBackendError(
            f"{cls.__module__}.{cls.__qualname__}.{name} must be a read-only property "
            f"(GraphStore.{name} is declared as a property), not a callable method"
        )


__all__ = [
    "GRAPH_BACKENDS_GROUP",
    "NoSuchBackendError",
    "list_graph_backends",
    "resolve_graph_backend",
]
