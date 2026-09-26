"""Abstract ``Primitive`` base class for the five core types.

Per DEC-008 the primitive set is closed: ``Agent``, ``Activity``,
``InformationObject``, ``Concept``, ``Place``. Subclasses live in sibling
modules and are aggregated in :mod:`okto_neuron.primitives.__init__`.

Design invariants enforced here:

* ``model_config`` is frozen, ``extra='forbid'``, ``validate_assignment=True``.
* Direct instantiation of ``Primitive`` raises a validation error (abstract guard).
* Every concrete subclass MUST declare a non-empty
  ``__standards__: ClassVar[tuple[str, ...]]`` of CURIEs; this is checked
  at class-creation time via ``__init_subclass__`` (raises ``TypeError`` per
  ``errors.py``).
* ``model_json_schema()`` of any subclass exposes
  ``x-marginalia-standards`` (vendor extension) containing the merged
  ``__standards__`` MRO tuple — see :meth:`Primitive.merge_pack`.
* ``id`` and ``name`` are stripped/control-char checked; ``type`` is a
  CURIE; ``created_at`` MUST be timezone-aware and is normalized to UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

__all__ = ["Primitive", "CURIE_RE"]

# CURIE = prefix:reference; prefix is NCName-ish, reference allows dots/dashes.
CURIE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:[A-Za-z_][A-Za-z0-9_.-]*$")


def _standards_json_schema_extra(cls: type["Primitive"]) -> Any:
    """Return a ``json_schema_extra`` callable that injects
    ``x-marginalia-standards`` into the generated JSON schema.

    Subclasses opt in by setting ``json_schema_extra=_standards_json_schema_extra(cls)``
    in their own ``model_config``. Implemented as a closure over the
    subclass so the MRO-merged standards tuple is captured at class
    definition time.
    """
    merged = Primitive.merge_pack(cls)

    def _inject(schema: dict[str, Any]) -> None:
        schema["x-marginalia-standards"] = list(merged)

    return _inject


def _raise(code: str, msg: str) -> None:
    raise PydanticCustomError(code, msg, {"code": code})


class Primitive(BaseModel):
    """Abstract base for all five core primitives.

    Not instantiable directly; the ``_not_abstract`` model validator raises
    a coded validation error if anyone tries ``Primitive(...)``.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        populate_by_name=False,
    )

    id: str
    type: str  # no default on base → forces subclass override
    name: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __standards__: ClassVar[tuple[str, ...]] = ()

    # ------------------------------------------------------------------ #
    # class-definition-time checks
    # ------------------------------------------------------------------ #
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        std = cls.__dict__.get("__standards__", None)
        if std is None:
            # subclass did not redeclare — inherit, do not re-validate
            # (allows pack-generated sub-subclasses to extend safely)
            return
        if not std:
            raise TypeError(f"{cls.__name__} must define non-empty __standards__")
        if not isinstance(std, tuple) or not all(isinstance(s, str) for s in std):
            raise TypeError(f"{cls.__name__}.__standards__ must be tuple[str, ...]")
        for curie in std:
            if not CURIE_RE.match(curie):
                raise TypeError(f"{cls.__name__}.__standards__ has invalid CURIE: {curie!r}")

    # ------------------------------------------------------------------ #
    # field validators
    # ------------------------------------------------------------------ #
    @field_validator("id", mode="after")
    @classmethod
    def _id_clean(cls, v: str) -> str:
        if not v or len(v) > 512 or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            _raise(
                "id_invalid_format",
                "id must be non-empty, <=512 chars, and contain no whitespace or control chars",
            )
        return v

    @field_validator("type", mode="after")
    @classmethod
    def _type_curie(cls, v: str) -> str:
        if not CURIE_RE.match(v):
            _raise("curie_invalid", "invalid CURIE format")
        return v

    @field_validator("name", mode="after")
    @classmethod
    def _name_clean(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v != v.strip() or not v:
            _raise("name_invalid", "name must be non-empty and stripped when provided")
        return v

    @field_validator("created_at", mode="after")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            _raise("datetime_not_tz_aware", "datetime must be timezone-aware")
        return v.astimezone(timezone.utc)

    # ------------------------------------------------------------------ #
    # whole-model validator: abstract guard
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _not_abstract(self) -> "Primitive":
        if type(self) is Primitive:
            _raise(
                "base_abstract_instantiation",
                "Primitive is abstract; instantiate Agent/Activity/InformationObject/Concept/Place",
            )
        return self

    # ------------------------------------------------------------------ #
    # standards-tuple MRO merge (used by exporter + pack loader)
    # ------------------------------------------------------------------ #
    @classmethod
    def merge_pack(cls, *extra_classes: type) -> tuple[str, ...]:
        """Return the MRO-merged ``__standards__`` for ``cls`` plus any
        ``extra_classes`` (e.g. pack-supplied mixins), preserving order
        and de-duplicating.

        Used by:
        * topic 07 exporter (MRO walk)
        * cluster 1C pack loader (``BaseCls.__standards__ + tuple(pack_extra)``)
        * the ``json_schema_extra`` injector in this module
        """
        seen: list[str] = []
        chain: list[type] = list(reversed(cls.__mro__)) + list(extra_classes)
        for c in chain:
            for s in getattr(c, "__standards__", ()) or ():
                if s not in seen:
                    seen.append(s)
        return tuple(seen)
