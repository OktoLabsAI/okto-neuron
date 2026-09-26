"""LiteLLM embedding provider call-shape tests."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from okto_neuron.config import EmbeddingConfig
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.embed import (
    EmbeddingProviderError,
    _set_embedding_call_stats,
    embed_in_batches,
    embed_many,
    get_provider,
)
from okto_neuron.store.reembed import copy_graph_reembedding


def test_openai_embedding_provider_uses_openai_prefix_and_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="openai-compat",
            api_base="http://127.0.0.1:8123/v1",
            model="text-embedding-3-small",
            dimension=2,
        )
    )

    assert provider.embed("hello") == [0.1, 0.2]
    assert calls[0]["model"] == "openai/text-embedding-3-small"
    assert calls[0]["api_base"] == "http://127.0.0.1:8123/v1"


def test_lm_studio_embedding_provider_uses_litellm_prefix_and_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="lm_studio",
            api_base="http://127.0.0.1:1234/v1",
            model="nomic-embed-text",
            dimension=2,
        )
    )

    assert provider.embed("hello") == [0.1, 0.2]
    assert calls[0]["model"] == "lm_studio/nomic-embed-text"
    assert calls[0]["api_base"] == "http://127.0.0.1:1234/v1"


def test_litellm_proxy_embedding_provider_uses_proxy_prefix_and_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="litellm_proxy",
            api_base="http://127.0.0.1:4000/v1",
            model="embedding-alias",
            dimension=2,
        )
    )

    assert provider.embed("hello") == [0.1, 0.2]
    assert provider.provider_name == "litellm_proxy"
    assert provider.model_name == "litellm_proxy/embedding-alias"
    assert provider.execution_location == "local"
    assert calls[0]["model"] == "litellm_proxy/embedding-alias"
    assert calls[0]["api_base"] == "http://127.0.0.1:4000/v1"


def test_managed_embedding_provider_uses_litellm_prefix_without_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="voyage",
            model="voyage-3",
            dimension=2,
        )
    )

    assert provider.embed("hello") == [0.1, 0.2]
    assert provider.execution_location == "remote"
    assert calls[0]["model"] == "voyage/voyage-3"
    assert "api_base" not in calls[0]


def test_managed_embedding_provider_forwards_explicit_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="voyage",
            api_base="http://127.0.0.1:9999/v1",
            model="voyage-3",
            dimension=2,
        )
    )

    assert provider.embed("hello") == [0.1, 0.2]
    assert calls[0]["model"] == "voyage/voyage-3"
    assert calls[0]["api_base"] == "http://127.0.0.1:9999/v1"


def test_external_embedding_reads_managed_key_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys: list[str | None] = []

    def embedding(**kwargs):
        keys.append(kwargs.get("api_key"))
        return SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(
        EmbeddingConfig(
            provider="voyage",
            model="voyage-3",
            api_key_env="OKTO_NEURON_TEST_EMBEDDING_KEY",
            dimension=2,
        )
    )

    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", "first-key")
    provider.embed("first")
    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", "rotated-key")
    provider.embed("second")

    assert keys == ["first-key", "rotated-key"]


def test_external_embedding_error_never_reflects_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "sensitive-provider-value"

    def embedding(**kwargs):
        raise RuntimeError(f"authorization failed for {kwargs['api_key']}")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", secret)
    provider = get_provider(
        EmbeddingConfig(
            provider="voyage",
            model="voyage-3",
            api_key_env="OKTO_NEURON_TEST_EMBEDDING_KEY",
            dimension=2,
        )
    )

    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed("secret-safe probe")

    assert secret not in str(caught.value)
    assert secret not in caught.value.user_message()
    assert "voyage/voyage-3" in str(caught.value)


def test_litellm_embedding_batches_inputs_and_orders_indexed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[
                {"index": 1, "embedding": [2.0, 2.5]},
                {"index": 0, "embedding": [1.0, 1.5]},
            ]
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    provider = get_provider(EmbeddingConfig(provider="voyage", model="voyage-3", dimension=2))

    assert embed_many(provider, ["first", "second"]) == [
        [1.0, 1.5],
        [2.0, 2.5],
    ]
    assert calls == [
        {
            "model": "voyage/voyage-3",
            "input": ["first", "second"],
            "drop_params": True,
        }
    ]


def test_litellm_embedding_rejects_incomplete_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            embedding=lambda **_kwargs: SimpleNamespace(
                data=[{"index": 0, "embedding": [1.0, 1.5]}]
            )
        ),
    )
    provider = get_provider(EmbeddingConfig(provider="voyage", model="voyage-3", dimension=2))

    with pytest.raises(EmbeddingProviderError, match="returned 1 vectors for 2 inputs"):
        embed_many(provider, ["first", "second"])


def test_embedding_batches_are_bounded_parallel_and_return_source_order() -> None:
    class _ConcurrentEmbedder:
        dim = 1

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.release = threading.Event()
            self.active = 0
            self.peak = 0
            self.calls: list[list[str]] = []

        def embed(self, text: str) -> list[float]:
            return [float(text)]

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            with self.lock:
                self.calls.append(list(texts))
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 3:
                    self.release.set()
            try:
                assert self.release.wait(timeout=2), "three batches did not overlap"
                return [[float(text)] for text in texts]
            finally:
                with self.lock:
                    self.active -= 1

    provider = _ConcurrentEmbedder()
    vectors = embed_in_batches(
        provider,
        [str(index) for index in range(6)],
        batch_size=2,
        max_concurrent_batches=3,
    )

    assert vectors == [[float(index)] for index in range(6)]
    assert provider.peak == 3
    assert sorted(provider.calls) == [["0", "1"], ["2", "3"], ["4", "5"]]


def test_embedding_batches_report_exact_native_request_usage() -> None:
    class _UsageEmbedder:
        dim = 1

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            _set_embedding_call_stats({"input_tokens": len(texts) * 10})
            return [[float(text)] for text in texts]

    usage: list[dict[str, int]] = []
    vectors = embed_in_batches(
        _UsageEmbedder(),  # type: ignore[arg-type]
        [str(index) for index in range(5)],
        batch_size=2,
        max_concurrent_batches=1,
        on_usage=usage.append,
    )

    assert vectors == [[float(index)] for index in range(5)]
    assert sum(row["embedding_calls"] for row in usage) == 3
    assert sum(row["embedding_calls_with_usage"] for row in usage) == 3
    assert sum(row["embedding_inputs"] for row in usage) == 5
    assert sum(row["input_tokens"] for row in usage) == 50


def test_running_embedding_scheduler_adopts_a_higher_live_limit() -> None:
    class _BlockingEmbedder:
        dim = 1

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.release = threading.Event()
            self.first_started = threading.Event()
            self.second_started = threading.Event()
            self.active = 0

        def embed(self, text: str) -> list[float]:
            return [float(text)]

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            with self.lock:
                self.active += 1
                self.first_started.set()
                if self.active == 2:
                    self.second_started.set()
            try:
                assert self.release.wait(timeout=2)
                return [self.embed(text) for text in texts]
            finally:
                with self.lock:
                    self.active -= 1

    provider = _BlockingEmbedder()
    concurrency = [1]
    result: list[list[list[float]]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            embed_in_batches(
                provider,
                ["1", "2"],
                batch_size=1,
                max_concurrent_batches=1,
                settings=lambda: (1, concurrency[0]),
            )
        )
    )
    worker.start()
    assert provider.first_started.wait(timeout=1)
    concurrency[0] = 2
    assert provider.second_started.wait(timeout=1)
    provider.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [[[1.0], [2.0]]]


def test_vectors_only_reembed_uses_the_same_bulk_boundary() -> None:
    class _BatchEmbedder:
        dim = 2

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed(self, text: str) -> list[float]:
            return [float(len(text)), 1.0]

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [self.embed(text) for text in texts]

    class _Store:
        def __init__(self) -> None:
            self.nodes: list[Node] = []

        def add_node(self, node: Node) -> None:
            self.nodes.append(node)

        def add_edge(self, _edge) -> None:
            raise AssertionError("the fixture has no edges")

    provenance = Provenance(source="test", rule_id="batch-reembed")
    nodes = [
        Node(
            id="node-a",
            type="Concept",
            title="Alpha",
            content="First",
            embedding=[0.0, 0.0],
            provenance=provenance,
        ),
        Node(
            id="block-a",
            type="Block",
            title="Block",
            content="No vector",
            embedding=None,
            provenance=provenance,
        ),
        Node(
            id="node-b",
            type="Concept",
            title="Beta",
            content="Second",
            embedding=[0.0, 0.0],
            provenance=provenance,
        ),
    ]
    embedder = _BatchEmbedder()
    store = _Store()
    progress: list[tuple[int, int]] = []

    stats = copy_graph_reembedding(
        nodes,
        [],
        store,  # type: ignore[arg-type]
        embedder,
        batch_size=32,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert embedder.calls == [["Alpha\nFirst", "Beta\nSecond"]]
    assert [node.id for node in store.nodes] == ["node-a", "block-a", "node-b"]
    assert store.nodes[1].embedding is None
    assert stats == {"nodes": 3, "edges": 0, "recomputed": 2, "copied": 1}
    assert progress[-1] == (3, 3)


def _provider_raising(exc: BaseException, monkeypatch: pytest.MonkeyPatch):
    def embedding(**_kwargs):
        raise exc

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    return get_provider(
        EmbeddingConfig(
            provider="litellm_proxy",
            api_base="http://127.0.0.1:4000",
            model="desktop/qwen3-embedding-4b",
            dimension=2,
        )
    )


class _StatusError(Exception):
    """Adapter-shaped exception exposing an upstream HTTP status."""

    def __init__(self, status_code: int, message: str = "upstream said no") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected_category", "expected_status", "expected_retryable"),
    [
        (_StatusError(401, "Unauthorized"), "authentication", 401, False),
        (_StatusError(502, "Bad Gateway"), "unavailable", 502, True),
        (_StatusError(429, "Too Many Requests"), "rate_limited", 429, True),
        (_StatusError(400, "invalid request"), "invalid_request", 400, False),
        (TimeoutError("request timed out"), "timeout", None, True),
        (
            ConnectionRefusedError("connection refused"),
            "unavailable",
            None,
            True,
        ),
    ],
)
def test_embed_many_classifies_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_category: str,
    expected_status: int | None,
    expected_retryable: bool,
) -> None:
    """The embedding boundary must expose the same policy the LLM path does.

    Regression guard for the 2026-07-20 ADR 0040 live run, where every
    embedding failure collapsed to one opaque string and the owning hop
    (gateway rejection vs unavailable upstream) could not be identified from
    Okto Neuron's own evidence.
    """
    provider = _provider_raising(exc, monkeypatch)

    with pytest.raises(EmbeddingProviderError) as caught:
        embed_many(provider, ["first", "second"])

    error = caught.value
    assert error.category == expected_category
    assert error.status_code == expected_status
    assert error.retryable is expected_retryable
    assert f"category={expected_category}" in str(error)
    assert "litellm_proxy/desktop/qwen3-embedding-4b" in str(error)


def test_embed_many_classification_never_leaks_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured capture must not reintroduce upstream text carrying the key."""
    secret = "sk-" + "another-sensitive-provider-value"

    def embedding(**kwargs):
        raise _StatusError(401, f"Unauthorized: bearer {kwargs['api_key']}")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", secret)
    provider = get_provider(
        EmbeddingConfig(
            provider="voyage",
            model="voyage-3",
            api_key_env="OKTO_NEURON_TEST_EMBEDDING_KEY",
            dimension=2,
        )
    )

    with pytest.raises(EmbeddingProviderError) as caught:
        embed_many(provider, ["first", "second"])

    error = caught.value
    assert secret not in str(error)
    assert secret not in error.user_message()
    assert "Unauthorized" not in str(error)
    assert error.category == "authentication"
    assert error.status_code == 401
    assert error.retryable is False
    assert error.__cause__ is None


def test_malformed_embedding_response_is_categorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            embedding=lambda **_kwargs: SimpleNamespace(
                data=[{"index": 0, "embedding": [1.0, 1.5]}]
            )
        ),
    )
    provider = get_provider(EmbeddingConfig(provider="voyage", model="voyage-3", dimension=2))

    with pytest.raises(EmbeddingProviderError) as caught:
        embed_many(provider, ["first", "second"])

    assert caught.value.category == "malformed_output"
    assert caught.value.retryable is False
