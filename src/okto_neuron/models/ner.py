from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    label: str
    text: str
    score: float


@runtime_checkable
class EntityExtractor(Protocol):
    model_id: str

    def extract(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float = 0.5,
    ) -> list[Span]: ...


class GLiNERNER:
    """Deprecated historical extractor retained only for import compatibility.

    GLiNER was never wired into production ingest. Okto Neuron's production
    extraction path is the configured LLM propose/resolve pipeline, so shipping
    an undeclared heavyweight runtime behind this dormant class was misleading.
    """

    def __init__(self, model_id: str = "urchade/gliner_medium-v2.1") -> None:
        self.model_id = model_id
        warnings.warn(
            "GLiNERNER is retired and is not part of Okto Neuron's production extraction pipeline",
            DeprecationWarning,
            stacklevel=2,
        )

    def _get_model(self):
        raise RuntimeError(
            "GLiNERNER is retired; use Okto Neuron's configured LLM extraction pipeline"
        )

    def extract(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float = 0.5,
    ) -> list[Span]:
        model = self._get_model()
        raw = model.predict_entities(text, list(labels), threshold=threshold)
        return [
            Span(
                start=int(r["start"]),
                end=int(r["end"]),
                label=str(r["label"]),
                text=str(r["text"]),
                score=float(r["score"]),
            )
            for r in raw
        ]

    def preload(self) -> None:
        self._get_model()
