"""Pack registry and type resolution."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from .errors import (
    AmbiguousTypeError,
    NamespaceShadowingFinding,
    UnresolvedImportError,
)
from .loader import Pack
from .manifest import EdgeTypeDecl, TypeDecl
from .qualified import PRIMITIVE_NAMES, QualifiedTypeName


class PackRegistry:
    def __init__(self) -> None:
        self._packs: dict[str, Pack] = {}
        self._findings: list[NamespaceShadowingFinding] = []

    def register(self, pack: Pack) -> None:
        logger = logging.getLogger("okto_neuron.core.schema")
        for name in pack.types:
            for existing in self._packs.values():
                if name not in existing.types:
                    continue
                logger.warning(
                    "namespace shadowing",
                    extra={
                        "pack_id": pack.id,
                        "shadowed_name": name,
                        "shadowed_by_pack_id": existing.id,
                    },
                )
                self._findings.append(
                    NamespaceShadowingFinding(
                        pack_id=pack.id,
                        shadowed_name=name,
                        shadowed_by_pack_id=existing.id,
                        source=pack.source,
                    )
                )
        self._packs[pack.id] = pack

    def get(self, pack_id: str) -> Pack | None:
        return self._packs.get(pack_id)

    def has(self, pack_id: str) -> bool:
        return pack_id in self._packs

    def list_packs(self) -> list[str]:
        return list(self._packs)

    def types_for(self, pack_id: str) -> Mapping[str, TypeDecl]:
        pack = self.get(pack_id)
        if pack is None:
            raise UnresolvedImportError(pack_id=pack_id, offending_value=pack_id)
        return pack.types

    def edge_types_for(self, pack_id: str) -> Mapping[str, EdgeTypeDecl]:
        pack = self.get(pack_id)
        if pack is None:
            raise UnresolvedImportError(pack_id=pack_id, offending_value=pack_id)
        return pack.edge_types

    def all_types(self) -> list[QualifiedTypeName]:
        return [
            QualifiedTypeName(pack_id=pack_id, name=name)
            for pack_id, pack in self._packs.items()
            for name in pack.types
        ]

    def primitive_of(self, qname: QualifiedTypeName) -> str:
        if qname.pack_id == "core" and qname.name in PRIMITIVE_NAMES:
            return qname.name
        pack = self.get(qname.pack_id)
        if pack is None or qname.name not in pack.types:
            raise UnresolvedImportError(
                pack_id=qname.pack_id,
                field_path="types",
                offending_value=qname.name,
            )
        return pack.types[qname.name].kind_of

    def resolve_type(self, name: str, pack_id: str | None = None) -> QualifiedTypeName:
        if pack_id is None and ":" in name:
            pack_id, name = name.split(":", 1)

        if pack_id is not None:
            if pack_id == "core" and name in PRIMITIVE_NAMES:
                return QualifiedTypeName(pack_id="core", name=name)
            pack = self.get(pack_id)
            if pack is not None and name in pack.types:
                return QualifiedTypeName(pack_id=pack_id, name=name)
            raise UnresolvedImportError(
                pack_id=pack_id,
                field_path="types",
                offending_value=name,
            )

        matches = [
            QualifiedTypeName(pack_id=pid, name=name)
            for pid, pack in self._packs.items()
            if name in pack.types
        ]
        if len(matches) >= 2:
            raise AmbiguousTypeError(name, tuple(matches))
        if len(matches) == 1:
            return matches[0]
        if name in PRIMITIVE_NAMES:
            return QualifiedTypeName(pack_id="core", name=name)
        raise UnresolvedImportError(field_path="types", offending_value=name)

    def find_shadowings(self) -> list[NamespaceShadowingFinding]:
        return list(self._findings)

    @property
    def findings(self) -> list[NamespaceShadowingFinding]:
        return list(self._findings)


_DEFAULT_REGISTRY: PackRegistry | None = None


def default_registry() -> PackRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = PackRegistry()
    return _DEFAULT_REGISTRY
