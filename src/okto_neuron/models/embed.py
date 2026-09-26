from __future__ import annotations

import threading
from functools import cached_property
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    dim: int
    normalized: bool

    def embed_one(self, text: str) -> "np.ndarray": ...

    def embed_batch(self, texts: list[str]) -> "np.ndarray": ...


class LocalEmbedder:
    def __init__(
        self,
        model_id: str = "BAAI/bge-small-en-v1.5",
        *,
        dim: int = 384,
        normalized: bool = True,
    ) -> None:
        self.model_id = model_id
        self.dim = dim
        self.normalized = normalized
        self._lock = threading.Lock()
        self._model_instance = None

    @cached_property
    def _model(self):
        model = getattr(self, "_model_instance", None)
        if model is not None:
            return model

        with self._lock:
            model = getattr(self, "_model_instance", None)
            if model is None:
                from fastembed import TextEmbedding

                model = TextEmbedding(model_name=self.model_id)
                self._model_instance = model
            return model

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        vectors = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)
        if vectors.size == 0:
            vectors = vectors.reshape(0, self.dim)

        if self.normalized:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.clip(norms, 1e-12, None)

        return vectors.astype(np.float32, copy=False)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]

    def preload(self) -> None:
        _ = self._model
