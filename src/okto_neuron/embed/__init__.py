"""Embedding providers — fastembed (local default), external (LiteLLM), or stub.

``get_provider(config)`` is the factory. It takes a resolved
:class:`~okto_neuron.config._vault.EmbeddingConfig` (provider/model/dimension plus
the transport knobs an external endpoint needs) and returns a provider whose
vectors are exactly ``config.dimension`` wide. ``None`` returns the deterministic
:class:`StubEmbedder` (offline/CI). A bare provider name (or ``(name, model)``)
is still accepted for back-compat and coerced into an ``EmbeddingConfig``.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

from okto_neuron._compat import secret_env as _secret_env
from okto_neuron.errors import OktoNeuronError

if TYPE_CHECKING:
    from okto_neuron.config._vault import EmbeddingConfig


class EmbeddingProvider(Protocol):
    dim: int
    provider_name: str
    model_name: str
    execution_location: str

    def embed(self, text: str) -> list[float]: ...


class EmbeddingProviderError(OktoNeuronError):
    """The embedding provider could not be reached or returned a bad response.

    Carries the same normalized provider-failure policy fields the completion
    path already exposes on ``llm.LLMProviderError`` (``category``,
    ``retry_after_s``, ``retryable``) plus the upstream ``status_code`` when the
    adapter exposed one. Third-party exception *text* is still never forwarded —
    it can contain request headers or the literal API key — so the diagnosable
    boundary is the structured classification, not a message copy.
    """

    default_message = "embedding provider error"

    def __init__(
        self,
        message: str | None = None,
        *,
        category: str = "unknown",
        retry_after_s: float | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.category = category
        self.retry_after_s = retry_after_s
        self.retryable = bool(retryable) if retryable is not None else False
        self.status_code = status_code


def _provider_status_code(exc: BaseException) -> int | None:
    """Best-effort upstream HTTP status from an adapter exception chain.

    Only integers are read; no exception text is inspected or retained, so this
    cannot leak credentials.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (
            getattr(current, "status_code", None),
            getattr(getattr(current, "response", None), "status_code", None),
            getattr(current, "exception_status_code", None),
        ):
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
        current = current.__cause__ or current.__context__
    return None


_MAX_CONCURRENT_BATCHES = 32
_BATCH_SETTINGS_POLL_SECONDS = 0.25
_embedding_call_stats = threading.local()


def _set_embedding_call_stats(stats: dict[str, int] | None) -> None:
    _embedding_call_stats.value = stats


def _last_embedding_call_stats() -> dict[str, int] | None:
    value = getattr(_embedding_call_stats, "value", None)
    return dict(value) if isinstance(value, dict) else None


def _value(obj: object, name: str) -> object:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _validate_vectors(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
    dimension: int,
    provider: str,
) -> list[list[float]]:
    """Normalize and validate a provider's complete bulk response."""
    if len(vectors) != expected_count:
        raise EmbeddingProviderError(
            f"embedding provider {provider!r} returned {len(vectors)} vectors "
            f"for {expected_count} inputs",
            category="malformed_output",
        )
    normalized: list[list[float]] = []
    for index, vector in enumerate(vectors):
        out = [float(value) for value in vector]
        if len(out) != dimension:
            raise EmbeddingProviderError(
                f"embedding provider {provider!r} returned a {len(out)}-dim vector "
                f"at index {index}, but config declares dimension {dimension}",
                category="malformed_output",
            )
        normalized.append(out)
    return normalized


def _embed_many_with_usage(
    embedder: EmbeddingProvider,
    texts: Sequence[str],
) -> tuple[list[list[float]], dict[str, int]]:
    """Embed texts and return exact request plus provider-usage accounting.

    The fallback keeps injected/test providers compatible. Built-in providers
    implement ``embed_many`` and therefore issue one native multi-input request.
    """
    items = list(texts)
    if not items:
        return [], {
            "embedding_calls": 0,
            "embedding_calls_with_usage": 0,
            "embedding_inputs": 0,
            "input_tokens": 0,
        }
    native = getattr(embedder, "embed_many", None)
    usage_rows: list[dict[str, int] | None] = []
    if callable(native):
        _set_embedding_call_stats(None)
        vectors = native(items)
        usage_rows.append(_last_embedding_call_stats())
    else:
        vectors = []
        for text in items:
            _set_embedding_call_stats(None)
            vectors.append(embedder.embed(text))
            usage_rows.append(_last_embedding_call_stats())
    normalized = _validate_vectors(
        vectors,
        expected_count=len(items),
        dimension=int(embedder.dim),
        provider=type(embedder).__name__,
    )
    reported = [row for row in usage_rows if isinstance(row, dict)]
    return normalized, {
        "embedding_calls": len(usage_rows),
        "embedding_calls_with_usage": len(reported),
        "embedding_inputs": len(items),
        "input_tokens": sum(int(row.get("input_tokens", 0)) for row in reported),
    }


def embed_many(
    embedder: EmbeddingProvider,
    texts: Sequence[str],
) -> list[list[float]]:
    """Embed ``texts`` through a native bulk method or the legacy single API."""

    vectors, _usage = _embed_many_with_usage(embedder, texts)
    return vectors


def embed_in_batches(
    embedder: EmbeddingProvider,
    texts: Sequence[str],
    *,
    batch_size: int = 32,
    max_concurrent_batches: int = 1,
    settings: Callable[[], tuple[int, int]] | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_usage: Callable[[Mapping[str, int]], None] | None = None,
) -> list[list[float]]:
    """Embed in bounded, source-ordered batches.

    ``settings`` is re-read while scheduling and every 250 ms while requests are
    in flight, allowing a running job to adopt new execution limits. Already
    submitted calls are never cancelled merely because the limit decreases.
    """
    items = list(texts)
    total = len(items)
    if not items:
        if progress is not None:
            progress(0, 0)
        return []

    def _limits() -> tuple[int, int]:
        current_batch_size, current_concurrency = (
            settings() if settings is not None else (batch_size, max_concurrent_batches)
        )
        if not 1 <= current_batch_size <= 256:
            raise ValueError("embedding batch_size must be between 1 and 256")
        if not 1 <= current_concurrency <= _MAX_CONCURRENT_BATCHES:
            raise ValueError("embedding max_concurrent_batches must be between 1 and 32")
        return current_batch_size, current_concurrency

    results: list[list[float] | None] = [None] * total
    futures: dict[Future[tuple[list[list[float]], dict[str, int]]], tuple[int, int]] = {}
    cursor = 0
    completed = 0
    executor = ThreadPoolExecutor(
        max_workers=min(_MAX_CONCURRENT_BATCHES, total),
        thread_name_prefix="okto-neuron-embed",
    )
    try:
        while cursor < total or futures:
            current_batch_size, current_concurrency = _limits()
            while cursor < total and len(futures) < current_concurrency:
                start = cursor
                end = min(total, start + current_batch_size)
                future = executor.submit(_embed_many_with_usage, embedder, items[start:end])
                futures[future] = (start, end)
                cursor = end
                current_batch_size, current_concurrency = _limits()

            if not futures:
                continue
            done, _pending = wait(
                tuple(futures),
                timeout=_BATCH_SETTINGS_POLL_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            for future in sorted(done, key=lambda item: futures[item][0]):
                start, end = futures.pop(future)
                vectors, usage = future.result()
                results[start:end] = vectors
                completed += end - start
                if on_usage is not None:
                    on_usage(usage)
                if progress is not None:
                    progress(completed, total)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if any(vector is None for vector in results):
        raise EmbeddingProviderError("embedding batches completed with missing results")
    return [vector for vector in results if vector is not None]


class StubEmbedder:
    """Hash-seeded deterministic vectors. CI / smoke-test only."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.provider_name = "stub"
        self.model_name = "sha256-deterministic"
        self.execution_location = "local"

    def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # tile to dim
        out: list[float] = []
        i = 0
        while len(out) < self.dim:
            out.append((h[i % len(h)] - 128) / 128.0)
            i += 1
        return out

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


def _coerce_config(
    config: "EmbeddingConfig | str | None",
    model: str | None,
) -> "EmbeddingConfig":
    """Normalize the legacy call forms into an ``EmbeddingConfig``.

    - ``None`` → stub (the historical no-arg default).
    - ``str`` → that provider name (plus optional ``model``).
    - ``EmbeddingConfig`` → returned unchanged (``model`` ignored).
    """
    from okto_neuron.config._vault import EmbeddingConfig

    if config is None:
        return EmbeddingConfig(provider="stub")
    if isinstance(config, str):
        name = "fastembed" if config in {"default", "bge-small-en-v1.5"} else config
        if model is not None:
            return EmbeddingConfig(provider=name, model=model)
        return EmbeddingConfig(provider=name)
    return config


def get_provider(
    config: "EmbeddingConfig | str | None" = None,
    model: str | None = None,
) -> EmbeddingProvider:
    """Build an embedding provider from a resolved ``EmbeddingConfig``."""
    cfg = _coerce_config(config, model)
    provider = cfg.provider
    dim = int(cfg.dimension)

    if provider == "stub":
        return StubEmbedder(dim=dim)

    if provider in {"fastembed", "bge-small-en-v1.5"}:
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as exc:
            raise EmbeddingProviderError(
                "FastEmbed is not installed; install okto-neuron[embeddings] "
                "to use the fastembed provider"
            ) from exc

        class _FastEmbedProvider:
            def __init__(self, model_name: str, dimension: int) -> None:
                self._model_name = model_name
                self._m = TextEmbedding(model_name=model_name)
                self.dim = dimension
                self.provider_name = "fastembed"
                self.model_name = model_name
                self.execution_location = "local"

            def embed(self, text: str) -> list[float]:
                return self.embed_many([text])[0]

            def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
                vectors = list(self._m.embed(list(texts)))
                return _validate_vectors(
                    vectors,
                    expected_count=len(texts),
                    dimension=self.dim,
                    provider=self._model_name,
                )

        return _FastEmbedProvider(cfg.model, dim)

    if provider == "sentence-transformers":
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise EmbeddingProviderError(
                "Sentence Transformers is not installed; install "
                "okto-neuron[sentence-transformers] to use this provider"
            ) from exc

        class _SentenceTransformersProvider:
            def __init__(self, model_name: str, dimension: int) -> None:
                self._model_name = model_name
                self._m = SentenceTransformer(model_name)
                self.dim = int(self._m.get_sentence_embedding_dimension() or dimension)
                self.provider_name = "sentence-transformers"
                self.model_name = model_name
                self.execution_location = "local"

            def embed(self, text: str) -> list[float]:
                return self.embed_many([text])[0]

            def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
                vectors = self._m.encode(list(texts), normalize_embeddings=True).tolist()
                return _validate_vectors(
                    vectors,
                    expected_count=len(texts),
                    dimension=self.dim,
                    provider=self._model_name,
                )

        return _SentenceTransformersProvider(cfg.model, dim)

    # Everything else is routed through LiteLLM against an external endpoint.
    return _LiteLLMEmbedder(cfg)


class _LiteLLMEmbedder:
    """LiteLLM-backed embedder mirroring ``llm.LiteLLMProvider``.

    ``model`` carries the canonical litellm-prefixed string. The API key is read
    from the environment AT CALL TIME (never stored on the instance or logged) so
    rotation needs no restart and it never appears in repr/logs. The configured
    ``dimension`` is asserted on every response so a misconfigured width fails
    loud rather than silently corrupting retrieval.
    """

    def __init__(self, config: "EmbeddingConfig") -> None:
        from okto_neuron.config._vault import classify_api_base

        self.dim = int(config.dimension)
        self._provider = config.provider
        self.api_base: str | None = config.api_base
        self._api_key_env: str | None = config.api_key_env
        self.model: str = f"{config.provider}/{config.model}"
        self.provider_name = config.provider
        self.model_name = self.model
        if config.api_base is None:
            self.execution_location = "remote"
        else:
            endpoint_class = classify_api_base(config.api_base, resolve=False)
            self.execution_location = (
                "local" if endpoint_class in {"loopback", "private"} else "remote"
            )

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            import litellm  # lazy import — optional dep, not in core
        except ImportError as exc:
            raise EmbeddingProviderError(
                "LiteLLM is not installed; install okto-neuron[litellm] "
                "to use external embedding providers"
            ) from exc

        api_key = _secret_env(self._api_key_env) if self._api_key_env else None

        kwargs: dict = {
            "model": self.model,
            "input": list(texts),
            # Silently drop params the chosen provider/model doesn't support;
            # fail loud on REQUIRED params (wrong prefix, bad api_base).
            "drop_params": True,
        }
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        if api_key is not None:
            kwargs["api_key"] = api_key

        try:
            response = litellm.embedding(**kwargs)
            usage = _value(response, "usage")
            input_tokens = _value(usage, "prompt_tokens")
            if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
                input_tokens = _value(usage, "total_tokens")
            _set_embedding_call_stats(
                {"input_tokens": input_tokens}
                if isinstance(input_tokens, int)
                and not isinstance(input_tokens, bool)
                and input_tokens >= 0
                else None
            )
            data = list(response.data)
            if len(data) != len(texts):
                raise EmbeddingProviderError(
                    f"embedding provider {self.model!r} returned {len(data)} vectors "
                    f"for {len(texts)} inputs",
                    category="malformed_output",
                )
            indexed: list[tuple[int | None, Any]] = []
            for item in data:
                index = (
                    item.get("index") if isinstance(item, dict) else getattr(item, "index", None)
                )
                indexed.append((index, item))
            present_indices = [index for index, _item in indexed if index is not None]
            if present_indices and len(present_indices) != len(indexed):
                raise EmbeddingProviderError(
                    f"embedding provider {self.model!r} returned incomplete response indices",
                    category="malformed_output",
                )
            if present_indices:
                expected_indices = list(range(len(texts)))
                if sorted(present_indices) != expected_indices:
                    raise EmbeddingProviderError(
                        f"embedding provider {self.model!r} returned invalid response indices",
                        category="malformed_output",
                    )
                indexed.sort(key=lambda pair: int(pair[0]))
            vectors = [
                item["embedding"] if isinstance(item, dict) else item.embedding
                for _index, item in indexed
            ]
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            # Provider exceptions can reflect request headers or the literal API
            # key. Keep the actionable provider/model boundary and never forward
            # third-party exception text or attach it as a user-visible cause —
            # but DO carry the structured classification, so an operator can tell
            # a gateway rejection from an unavailable upstream without the
            # secret-bearing text. Only the exception type/status is consulted
            # for the rendered message; no upstream string is interpolated.
            from okto_neuron.llm import classify_provider_exception

            classification = classify_provider_exception(exc)
            status_code = _provider_status_code(exc)
            detail = f"category={classification.category}"
            if status_code is not None:
                detail = f"{detail}, status={status_code}"
            raise EmbeddingProviderError(
                f"litellm embedding failed for {self.model} ({detail})",
                category=classification.category,
                retry_after_s=classification.retry_after_s,
                retryable=classification.retryable,
                status_code=status_code,
            ) from None

        return _validate_vectors(
            vectors,
            expected_count=len(texts),
            dimension=self.dim,
            provider=self.model,
        )


__all__ = [
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "StubEmbedder",
    "embed_in_batches",
    "embed_many",
    "get_provider",
]
