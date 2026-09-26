"""Pack manifest error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal


class PackManifestError(Exception):
    """Base error for pack manifest loading and validation."""

    code: ClassVar[str] = "MARG-PACK-000"

    def __init__(
        self,
        message: str | None = None,
        *,
        pack_id: str | None = None,
        field_path: str | None = None,
        offending_value: object | None = None,
        source: str | None = None,
    ) -> None:
        self.pack_id = pack_id
        self.field_path = field_path
        self.offending_value = offending_value
        self.source = source
        detail = message or self._default_message()
        super().__init__(f"{self.code}: {detail}")

    def _default_message(self) -> str:
        parts = [self.__class__.__name__]
        if self.pack_id is not None:
            parts.append(f"pack_id={self.pack_id!r}")
        if self.field_path is not None:
            parts.append(f"field_path={self.field_path!r}")
        if self.offending_value is not None:
            parts.append(f"offending_value={self.offending_value!r}")
        if self.source is not None:
            parts.append(f"source={self.source!r}")
        return ", ".join(parts)


class YamlParseError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-001"


class SchemaValidationError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-002"

    def __init__(
        self,
        message: str | None = None,
        *,
        errors: list[dict[str, object]] | None = None,
        **kwargs: object,
    ) -> None:
        self.errors = errors or []
        super().__init__(message, **kwargs)


class MissingKindOfError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-003"


class UnknownPrimitiveError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-004"


class ClosedSetViolationError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-005"

    def __init__(
        self,
        message: str | None = None,
        *,
        closed_set: tuple[str, ...],
        **kwargs: object,
    ) -> None:
        self.closed_set = closed_set
        super().__init__(message, **kwargs)


class UnknownCURIEPrefixError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-006"

    def __init__(
        self,
        message: str | None = None,
        *,
        prefix: str,
        **kwargs: object,
    ) -> None:
        self.prefix = prefix
        super().__init__(message, **kwargs)


class MalformedCURIEError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-007"


class CircularImportError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-008"

    def __init__(
        self,
        message: str | None = None,
        *,
        cycle: tuple[str, ...],
        depth_exceeded: bool = False,
        **kwargs: object,
    ) -> None:
        self.cycle = cycle
        self.depth_exceeded = depth_exceeded
        super().__init__(message, **kwargs)


class SelfImportError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-009"


class UnresolvedImportError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-010"


class SemverError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-011"


class IncompatibleCoreVersionError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-012"


class DuplicateTypeNameError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-013"

    def __init__(
        self,
        message: str | None = None,
        *,
        kind: Literal["type", "edge_type"],
        **kwargs: object,
    ) -> None:
        self.kind = kind
        super().__init__(message, **kwargs)


class PackVersionConflictError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-014"

    def __init__(
        self,
        message: str | None = None,
        *,
        source_a: str,
        source_b: str,
        hash_a: str,
        hash_b: str,
        **kwargs: object,
    ) -> None:
        self.source_a = source_a
        self.source_b = source_b
        self.hash_a = hash_a
        self.hash_b = hash_b
        super().__init__(message, **kwargs)


class ReservedPackManifestError(PackManifestError):
    code: ClassVar[str] = "MARG-PACK-015"


@dataclass(frozen=True, slots=True)
class NamespaceShadowingFinding:
    pack_id: str
    shadowed_name: str
    shadowed_by_pack_id: str
    source: str


class RegistryError(Exception):
    """Base error for pack registry operations."""


class AmbiguousTypeError(RegistryError):
    def __init__(self, name: str, matches: tuple[object, ...]) -> None:
        self.name = name
        self.matches = matches
        joined = ", ".join(str(match) for match in matches)
        super().__init__(f"ambiguous type {name!r}: {joined}")
