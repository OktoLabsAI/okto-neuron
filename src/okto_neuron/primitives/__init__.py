"""Public surface for ``okto_neuron.primitives``.

DEC-008: the primitive set is closed at five — ``Agent``, ``Activity``,
``InformationObject``, ``Concept``, ``Place``. Adding a sixth requires
an RFC + DEC-008 amendment + code edit; there is **no** runtime
``register_primitive()`` API.

The aggregation below imports each subclass from its sibling module and
freezes them into ``PRIMITIVES``, ``PRIMITIVE_NAMES`` and the
``PRIMITIVES_BY_NAME`` ``MappingProxyType``. Subclass modules are
imported defensively: cluster 1A is being implemented as parallel cards,
so a subclass module may not exist on a partial checkout. Whatever
subclasses *are* present at import time are registered; the closed-set
snapshot tests in :mod:`tests.primitives.test_registry` assert the
final set of five once all sibling cards land.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from ._base import Primitive
from ._lang import normalize_bcp47
from .errors import ERROR_CODES

# NOTE: there is deliberately no module-level ``__schema_version__`` here.
# A dead, unread ``Final[int]`` constant used to live at this spot alongside
# each primitive subclass's own ``__schema_version__`` ClassVar (a semver
# string, e.g. ``Agent.__schema_version__ == "0.1.0"``) — two markers, one
# read by nothing, the other the real per-primitive version. The
# schema-compatibility gate that actually matters at runtime is unrelated to
# either: ``store/schema.py``'s ``CURRENT_SCHEMA_VERSION`` guards the graph
# DDL, not the Python model shape. Keep exactly one source of truth for a
# primitive's own version: the ClassVar on the primitive itself.

# Conditional subclass imports — sibling implementation cards land these
# modules in parallel. Each block is independent so a missing module
# does not break the package.
_subclasses: list[type[Primitive]] = []

try:
    from ._agent import Agent  # type: ignore[import-not-found]

    _subclasses.append(Agent)
except ImportError:
    Agent = None  # type: ignore[assignment]

try:
    from ._activity import Activity  # type: ignore[import-not-found]

    _subclasses.append(Activity)
except ImportError:
    Activity = None  # type: ignore[assignment]

try:
    from ._infoobj import InformationObject  # type: ignore[import-not-found]

    _subclasses.append(InformationObject)
except ImportError:
    InformationObject = None  # type: ignore[assignment]

try:
    from ._concept import Concept  # type: ignore[import-not-found]

    _subclasses.append(Concept)
except ImportError:
    Concept = None  # type: ignore[assignment]

try:
    from ._place import Place  # type: ignore[import-not-found]

    _subclasses.append(Place)
except ImportError:
    Place = None  # type: ignore[assignment]

PRIMITIVES: Final[frozenset[type[Primitive]]] = frozenset(_subclasses)
PRIMITIVE_NAMES: Final[frozenset[str]] = frozenset(c.__name__ for c in _subclasses)
PRIMITIVES_BY_NAME: Final[Mapping[str, type[Primitive]]] = MappingProxyType(
    {c.__name__: c for c in _subclasses}
)

__all__ = [
    "Primitive",
    "Agent",
    "Activity",
    "InformationObject",
    "Concept",
    "Place",
    "PRIMITIVES",
    "PRIMITIVE_NAMES",
    "PRIMITIVES_BY_NAME",
    "ERROR_CODES",
    "normalize_bcp47",
]
