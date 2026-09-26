"""Index-backend registry: mirrors ``store/registry.py`` for ``IndexStore``.

Two sources, official-first (plan section 3.5, M3 spec section 2.2):

- ``_OFFICIAL`` — a small in-tree ``name -> "module:attr"`` map, imported
  lazily inside :func:`resolve_index_backend`.
- the ``okto_neuron.index_backends`` entry-point group (and the pre-0.3.0
  ``marginalia.index_backends`` group, still read) — anything installed
  separately.

Resolution fails closed: an unresolvable name, or a resolved class that does
not structurally satisfy
:class:`~okto_neuron.store.index.protocol.IndexStore` (including an
incompatible method signature), both raise :class:`NoSuchIndexBackendError`
at registration time, not on first write.
"""

from __future__ import annotations

import importlib
import inspect
from okto_neuron._compat import (
    INDEX_BACKENDS_GROUP,
    LEGACY_INDEX_BACKENDS_GROUP,
    iter_entry_points,
)
from typing import ClassVar

from okto_neuron.errors import OktoNeuronError
from okto_neuron.store.index.protocol import IndexStore


# "module:attr" targets, imported lazily — see module docstring.
_OFFICIAL: dict[str, str] = {
    "ladybug_bm25+vector_scan": "okto_neuron.store.index.default:DefaultIndexStore",
}


class NoSuchIndexBackendError(OktoNeuronError):
    """A requested index-backend name did not resolve to a usable class."""

    default_message: ClassVar[str] = "no such index backend is registered"


def resolve_index_backend(name: str) -> type[IndexStore]:
    """Resolve ``name`` to its ``IndexStore`` class.

    Checks :data:`_OFFICIAL` first, then the ``okto_neuron.index_backends`` (then legacy ``marginalia.index_backends``)
    entry-point group. Raises :class:`NoSuchIndexBackendError` when neither
    source has ``name``, or when the resolved class fails structural
    validation against :class:`IndexStore` (missing methods, or a method
    whose signature can't accept the protocol's own call shape).
    """
    target = _OFFICIAL.get(name)
    cls = _import_target(target) if target is not None else _resolve_from_entry_points(name)
    _validate_backend_class(cls)
    return cls


def list_index_backends() -> list[str]:
    """Names of every index backend resolvable in this process right now.

    Official names first (in :data:`_OFFICIAL`'s declaration order), then
    any additional entry-point-registered name, deduplicated. Enumerates
    names only — does not import or validate any class.
    """
    names = list(_OFFICIAL)
    for entry_point in iter_entry_points(INDEX_BACKENDS_GROUP, LEGACY_INDEX_BACKENDS_GROUP):
        if entry_point.name not in names:
            names.append(entry_point.name)
    return names


def _import_target(target: str) -> type:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _resolve_from_entry_points(name: str) -> type:
    for entry_point in iter_entry_points(
        INDEX_BACKENDS_GROUP, LEGACY_INDEX_BACKENDS_GROUP, name=name
    ):
        return entry_point.load()
    raise NoSuchIndexBackendError(f"no such index backend is registered: {name!r}")


def _validate_backend_class(cls: type) -> None:
    """Fail closed on a backend class that can't actually serve as an ``IndexStore``.

    Same rationale as ``store/registry.py``'s sibling: ``issubclass`` against
    an ``@runtime_checkable`` Protocol only checks method presence, not
    signature compatibility, so check every protocol method's signature
    against the concrete class's own here.
    """
    method_names = _index_store_method_names()
    if not issubclass(cls, IndexStore):
        missing = sorted(name for name in method_names if not hasattr(cls, name))
        detail = f"missing methods: {', '.join(missing)}" if missing else "structural check failed"
        raise NoSuchIndexBackendError(
            f"{cls.__module__}.{cls.__qualname__} does not implement IndexStore ({detail})"
        )
    for name in method_names:
        _require_compatible_signature(cls, name)


def _index_store_method_names() -> list[str]:
    return [name for name, value in vars(IndexStore).items() if not name.startswith("_") and callable(value)]


def _require_compatible_signature(cls: type, name: str) -> None:
    protocol_func = vars(IndexStore)[name]
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
        raise NoSuchIndexBackendError(
            f"{cls.__module__}.{cls.__qualname__}.{name} has a signature incompatible "
            f"with IndexStore.{name}: {exc}"
        ) from exc


__all__ = [
    "INDEX_BACKENDS_GROUP",
    "NoSuchIndexBackendError",
    "list_index_backends",
    "resolve_index_backend",
]
