"""Pack manifest loader."""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import yaml
from packaging.specifiers import SpecifierSet

from .errors import (
    CircularImportError,
    IncompatibleCoreVersionError,
    PackVersionConflictError,
    SelfImportError,
    UnresolvedImportError,
)
from .manifest import EdgeTypeDecl, TypeDecl, parse_manifest
from okto_neuron._compat import getenv as _compat_getenv


@dataclass(frozen=True, slots=True)
class Pack:
    id: str
    version: str
    content_hash: str
    types: MappingProxyType[str, TypeDecl]
    edge_types: MappingProxyType[str, EdgeTypeDecl]
    prefixes: MappingProxyType[str, str]
    source: str


class PackLoader:
    MAX_DEPTH: ClassVar[int] = 32

    def __init__(self, registry: object | None = None, search_paths: tuple[Path, ...] = ()) -> None:
        self.registry = registry
        self.search_paths = tuple(Path(path) for path in search_paths)
        self._cache: dict[tuple[str, str], Pack] = {}
        self._sources: Mapping[str, object] = registry if isinstance(registry, Mapping) else {}

    def load(
        self, pack_ref: str | Path | dict[str, object], *, _visiting: tuple[str, ...] = ()
    ) -> Pack:
        if len(_visiting) >= self.MAX_DEPTH:
            raise CircularImportError(cycle=_visiting, depth_exceeded=True)

        if isinstance(pack_ref, str) and pack_ref in _visiting:
            if pack_ref == _visiting[-1]:
                raise SelfImportError(
                    pack_id=pack_ref,
                    field_path="imports",
                    offending_value=pack_ref,
                )
            raise CircularImportError(
                cycle=_visiting + (pack_ref,),
                pack_id=pack_ref,
                field_path="imports",
                offending_value=pack_ref,
            )

        existing = self._existing_registered_pack(pack_ref)
        if existing is not None:
            return existing

        raw_bytes, source = self._resolve(pack_ref)
        text = unicodedata.normalize("NFC", raw_bytes.decode("utf-8"))
        content_hash = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        manifest = parse_manifest(text, source)

        if manifest.id in _visiting:
            if manifest.id == _visiting[-1]:
                raise SelfImportError(
                    pack_id=manifest.id,
                    field_path="imports",
                    offending_value=manifest.id,
                    source=source,
                )
            raise CircularImportError(
                cycle=_visiting + (manifest.id,),
                pack_id=manifest.id,
                field_path="imports",
                offending_value=manifest.id,
                source=source,
            )

        key = (manifest.id, manifest.version)
        cached = self._cache.get(key)
        if cached is not None:
            if cached.content_hash != content_hash:
                raise PackVersionConflictError(
                    pack_id=manifest.id,
                    field_path="version",
                    offending_value=manifest.version,
                    source=source,
                    source_a=cached.source,
                    source_b=source,
                    hash_a=cached.content_hash,
                    hash_b=content_hash,
                )
            return cached

        self._check_core_compat(manifest.id, manifest.compatRange, source)

        for import_ref in manifest.imports:
            self.load(import_ref, _visiting=_visiting + (manifest.id,))

        pack = Pack(
            id=manifest.id,
            version=manifest.version,
            content_hash=content_hash,
            types=MappingProxyType({type_decl.name: type_decl for type_decl in manifest.types}),
            edge_types=MappingProxyType(
                {edge_decl.name: edge_decl for edge_decl in manifest.edge_types}
            ),
            prefixes=MappingProxyType(dict(manifest.prefixes)),
            source=source,
        )
        self._cache[key] = pack
        self._register_pack(pack)
        return pack

    def _resolve(self, pack_ref: str | Path | dict[str, object]) -> tuple[bytes, str]:
        if isinstance(pack_ref, dict):
            pack_id = str(pack_ref.get("id", "<dict>"))
            text = yaml.safe_dump(pack_ref, sort_keys=False, allow_unicode=True)
            return text.encode("utf-8"), f"<dict:{pack_id}>"

        if isinstance(pack_ref, Path):
            if pack_ref.is_file():
                return pack_ref.read_bytes(), str(pack_ref)
            raise UnresolvedImportError(
                field_path="imports",
                offending_value=str(pack_ref),
                source=str(pack_ref),
            )

        pack_id = str(pack_ref)
        if pack_id in self._sources:
            return self._coerce_source(pack_id, self._sources[pack_id], f"<registry:{pack_id}>")

        explicit = Path(pack_id)
        if explicit.is_file():
            return explicit.read_bytes(), str(explicit)

        for candidate in self._candidate_paths(self.search_paths, pack_id):
            if candidate.is_file():
                return candidate.read_bytes(), str(candidate)

        env_paths = tuple(
            Path(path)
            for path in _compat_getenv("OKTO_NEURON_PACK_PATH", "").split(os.pathsep)
            if path
        )
        for candidate in self._candidate_paths(env_paths, pack_id):
            if candidate.is_file():
                return candidate.read_bytes(), str(candidate)

        builtin = self._resolve_builtin(pack_id)
        if builtin is not None:
            return builtin

        raise UnresolvedImportError(
            pack_id=pack_id,
            field_path="imports",
            offending_value=pack_id,
        )

    def _coerce_source(self, pack_id: str, value: object, default_source: str) -> tuple[bytes, str]:
        if isinstance(value, tuple) and len(value) == 2:
            content, source = value
            raw, _ = self._coerce_source(pack_id, content, str(source))
            return raw, str(source)
        if isinstance(value, dict):
            text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
            return text.encode("utf-8"), default_source
        if isinstance(value, bytes):
            return value, default_source
        if isinstance(value, Path):
            if value.is_file():
                return value.read_bytes(), str(value)
            raise UnresolvedImportError(
                pack_id=pack_id,
                field_path="imports",
                offending_value=str(value),
                source=str(value),
            )
        if isinstance(value, str):
            path = Path(value)
            if path.is_file():
                return path.read_bytes(), str(path)
            return value.encode("utf-8"), default_source
        raise UnresolvedImportError(pack_id=pack_id, field_path="imports", offending_value=value)

    def _candidate_paths(self, roots: tuple[Path, ...], pack_id: str) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for root in roots:
            candidates.extend(
                (
                    root / pack_id,
                    root / f"{pack_id}.yaml",
                    root / f"{pack_id}.yml",
                    root / pack_id / "pack.yaml",
                    root / pack_id / "manifest.yaml",
                )
            )
        return tuple(candidates)

    def _resolve_builtin(self, pack_id: str) -> tuple[bytes, str] | None:
        try:
            package_files = resources.files("okto_neuron.core.schema._builtin_packs")
        except ModuleNotFoundError:
            return None

        for filename in (f"{pack_id}.yaml", f"{pack_id}.yml"):
            candidate = package_files.joinpath(filename)
            if candidate.is_file():
                return candidate.read_bytes(), str(candidate)
        return None

    def _check_core_compat(self, pack_id: str, compat_range: str, source: str) -> None:
        core_version = _package_version()
        if not SpecifierSet(compat_range).contains(core_version, prereleases=True):
            raise IncompatibleCoreVersionError(
                pack_id=pack_id,
                field_path="compatRange",
                offending_value=compat_range,
                source=source,
            )

    def _existing_registered_pack(self, pack_ref: object) -> Pack | None:
        if not isinstance(pack_ref, str) or pack_ref in self._sources:
            return None
        get = getattr(self.registry, "get", None)
        if get is None:
            return None
        pack = get(pack_ref)
        return pack if isinstance(pack, Pack) else None

    def _register_pack(self, pack: Pack) -> None:
        register = getattr(self.registry, "register", None)
        if register is not None and not isinstance(self.registry, Mapping):
            register(pack)


def _package_version() -> str:
    try:
        import okto_neuron

        return okto_neuron.__version__
    except (ImportError, AttributeError):
        return "0.0.1"
