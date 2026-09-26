"""Pack manifest Pydantic models and YAML parser."""

from __future__ import annotations

from typing import Any, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml.nodes import MappingNode

from .errors import (
    ClosedSetViolationError,
    DuplicateTypeNameError,
    MalformedCURIEError,
    MissingKindOfError,
    PackManifestError,
    SchemaValidationError,
    SemverError,
    UnknownCURIEPrefixError,
    UnknownPrimitiveError,
    YamlParseError,
)
from .qualified import PRIMITIVE_NAMES, STANDARDS_PREFIXES


class TypeDecl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    kind_of: Literal["Agent", "Asset", "Event", "Place", "Concept"]
    extends: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class EdgeTypeDecl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    domain: str
    range: str
    extends: list[str] = Field(default_factory=list)


class PackManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    version: str
    compatRange: str
    prefixes: dict[str, str] = Field(default_factory=dict)
    imports: list[str] = Field(default_factory=list)
    types: list[TypeDecl] = Field(default_factory=list)
    edge_types: list[EdgeTypeDecl] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _preflight_manifest(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        pack_id = data.get("id") if isinstance(data.get("id"), str) else None
        type_names: dict[str, int] = {}
        types = data.get("types", [])
        if isinstance(types, list):
            for index, item in enumerate(types):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str):
                    if name in type_names:
                        raise DuplicateTypeNameError(
                            kind="type",
                            pack_id=pack_id,
                            field_path=f"types.{index}.name",
                            offending_value=name,
                        )
                    type_names[name] = index
                if "kind_of" in item and item.get("kind_of") not in PRIMITIVE_NAMES:
                    raise ClosedSetViolationError(
                        closed_set=PRIMITIVE_NAMES,
                        pack_id=pack_id,
                        field_path=f"types.{index}.kind_of",
                        offending_value=item.get("kind_of"),
                    )

        edge_names: set[str] = set()
        edge_types = data.get("edge_types", [])
        if isinstance(edge_types, list):
            for index, item in enumerate(edge_types):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                if name in edge_names or name in type_names:
                    raise DuplicateTypeNameError(
                        kind="edge_type",
                        pack_id=pack_id,
                        field_path=f"edge_types.{index}.name",
                        offending_value=name,
                    )
                edge_names.add(name)

        return data

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise SemverError(field_path="version", offending_value=value) from exc
        return value

    @field_validator("compatRange")
    @classmethod
    def _validate_compat_range(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise SemverError(field_path="compatRange", offending_value=value) from exc
        return value

    @model_validator(mode="after")
    def _validate_semantics(self) -> "PackManifestModel":
        _validate_prefix_redefinitions(self.id, self.prefixes)
        allowed_prefixes = {**STANDARDS_PREFIXES, **self.prefixes}

        for type_index, type_decl in enumerate(self.types):
            for extends_index, curie in enumerate(type_decl.extends):
                _validate_curie(
                    curie,
                    allowed_prefixes,
                    pack_id=self.id,
                    field_path=f"types.{type_index}.extends.{extends_index}",
                )

        declared_type_names = {type_decl.name for type_decl in self.types}
        for edge_index, edge_decl in enumerate(self.edge_types):
            _validate_type_ref(
                edge_decl.domain,
                declared_type_names,
                allowed_prefixes,
                pack_id=self.id,
                field_path=f"edge_types.{edge_index}.domain",
            )
            _validate_type_ref(
                edge_decl.range,
                declared_type_names,
                allowed_prefixes,
                pack_id=self.id,
                field_path=f"edge_types.{edge_index}.range",
            )
            for extends_index, curie in enumerate(edge_decl.extends):
                _validate_curie(
                    curie,
                    allowed_prefixes,
                    pack_id=self.id,
                    field_path=f"edge_types.{edge_index}.extends.{extends_index}",
                )

        return self


def parse_manifest(yaml_text: str, source: str) -> PackManifestModel:
    """Parse YAML text into a validated pack manifest."""

    try:
        raw_data = yaml.load(yaml_text, Loader=DupRejectingSafeLoader)
    except PackManifestError as exc:
        raise _with_context(exc, source=source) from exc
    except yaml.YAMLError as exc:
        raise YamlParseError(str(exc), source=source) from exc

    if not isinstance(raw_data, dict):
        raise SchemaValidationError(
            "manifest root must be a YAML mapping",
            source=source,
            offending_value=raw_data,
        )

    try:
        return PackManifestModel(**raw_data)
    except PackManifestError as exc:
        raise _with_context(
            exc,
            source=source,
            pack_id=raw_data.get("id") if isinstance(raw_data.get("id"), str) else None,
        ) from exc
    except ValidationError as exc:
        raise _dispatch_validation_error(exc, raw_data, source) from exc


class DupRejectingSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[object, object]:
        if not isinstance(node, MappingNode):
            raise YamlParseError("expected a mapping node")

        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise YamlParseError(
                    "duplicate YAML key",
                    field_path=str(key),
                    offending_value=key,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _validate_prefix_redefinitions(pack_id: str, prefixes: dict[str, str]) -> None:
    for prefix, uri in prefixes.items():
        if prefix in STANDARDS_PREFIXES and STANDARDS_PREFIXES[prefix] != uri:
            raise MalformedCURIEError(
                "standard prefix redefinition must keep the standard URI",
                pack_id=pack_id,
                field_path=f"prefixes.{prefix}",
                offending_value=uri,
            )


def _validate_curie(
    value: str,
    prefixes: dict[str, str],
    *,
    pack_id: str,
    field_path: str,
) -> None:
    if value != value.strip() or ":" not in value:
        raise MalformedCURIEError(
            pack_id=pack_id,
            field_path=field_path,
            offending_value=value,
        )
    prefix, reference = value.split(":", 1)
    if not prefix or not reference:
        raise MalformedCURIEError(
            pack_id=pack_id,
            field_path=field_path,
            offending_value=value,
        )
    if prefix not in prefixes:
        raise UnknownCURIEPrefixError(
            prefix=prefix,
            pack_id=pack_id,
            field_path=field_path,
            offending_value=value,
        )


def _validate_type_ref(
    value: str,
    declared_type_names: set[str],
    prefixes: dict[str, str],
    *,
    pack_id: str,
    field_path: str,
) -> None:
    if ":" in value:
        _validate_curie(value, prefixes, pack_id=pack_id, field_path=field_path)
        return
    if value in PRIMITIVE_NAMES or value in declared_type_names:
        return
    raise UnknownPrimitiveError(
        pack_id=pack_id,
        field_path=field_path,
        offending_value=value,
    )


def _with_context(
    exc: PackManifestError,
    *,
    source: str,
    pack_id: str | None = None,
) -> PackManifestError:
    if exc.source is None:
        exc.source = source
    if exc.pack_id is None and pack_id is not None:
        exc.pack_id = pack_id
    return exc


def _dispatch_validation_error(
    exc: ValidationError,
    raw_data: dict[str, object],
    source: str,
) -> PackManifestError:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = tuple(first.get("loc", ()))
    field_path = _format_loc(loc)
    pack_id = raw_data.get("id") if isinstance(raw_data.get("id"), str) else None
    offending_value = first.get("input")
    error_type = str(first.get("type", ""))

    if loc and loc[-1] == "kind_of" and error_type == "missing":
        return MissingKindOfError(
            pack_id=pack_id,
            field_path=field_path,
            offending_value=offending_value,
            source=source,
        )
    if loc and loc[-1] == "kind_of" and "literal" in error_type:
        return ClosedSetViolationError(
            closed_set=PRIMITIVE_NAMES,
            pack_id=pack_id,
            field_path=field_path,
            offending_value=offending_value,
            source=source,
        )

    return SchemaValidationError(
        str(exc),
        errors=errors,
        pack_id=pack_id,
        field_path=field_path,
        offending_value=offending_value,
        source=source,
    )


def _format_loc(loc: tuple[object, ...]) -> str | None:
    if not loc:
        return None
    return ".".join(str(part) for part in loc)
