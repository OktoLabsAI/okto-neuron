"""Cluster 4A model layer: embedders, NER, runtime backend detection."""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_legacy_path = Path(__file__).resolve().parent.parent / "models.py"
_legacy_spec = spec_from_file_location("okto_neuron._legacy_models", _legacy_path)
if _legacy_spec is None or _legacy_spec.loader is None:
    raise ImportError(f"Unable to load legacy models from {_legacy_path}")

_legacy_models = module_from_spec(_legacy_spec)
sys.modules[_legacy_spec.name] = _legacy_models
_legacy_spec.loader.exec_module(_legacy_models)

ContextSpan = _legacy_models.ContextSpan
Document = _legacy_models.Document
ExportScope = _legacy_models.ExportScope
IngestResult = _legacy_models.IngestResult
Node = _legacy_models.Node
Provenance = _legacy_models.Provenance
QueryHit = _legacy_models.QueryHit

__all__ = [
    "ContextSpan",
    "Document",
    "ExportScope",
    "IngestResult",
    "Node",
    "Provenance",
    "QueryHit",
]
