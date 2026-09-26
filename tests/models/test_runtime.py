import importlib.util
import logging
import platform

import pytest

import okto_neuron.models.runtime as rt


@pytest.fixture(autouse=True)
def _reset_runtime():
    rt._LOGGED.clear()
    rt._detect_cached.cache_clear()
    yield
    rt._LOGGED.clear()
    rt._detect_cached.cache_clear()


class FakeOllama:
    def __init__(self, version="0.8.0", runners=("mlx", "llama_cpp")):
        self._info = {"version": version, "runners": list(runners)}

    def info(self):
        return dict(self._info)


def test_detect_backend_mlx_on_darwin_arm64_with_mlx_runner(monkeypatch):
    monkeypatch.delenv("OKTO_NEURON_OLLAMA_BACKEND", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    backend = rt.detect_backend(None, ollama_client=FakeOllama(version="0.8.1", runners=("mlx",)))
    assert backend == "mlx"


def test_detect_backend_non_darwin(monkeypatch):
    monkeypatch.delenv("OKTO_NEURON_OLLAMA_BACKEND", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert rt.detect_backend(None) == "llama_cpp"


def test_env_override_beats_auto_on_apple_silicon(monkeypatch):
    monkeypatch.setenv("OKTO_NEURON_OLLAMA_BACKEND", "llama_cpp")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert rt.detect_backend(None, ollama_client=FakeOllama()) == "llama_cpp"


def test_lru_cache_idempotency_and_single_log_line(monkeypatch, caplog):
    monkeypatch.delenv("OKTO_NEURON_OLLAMA_BACKEND", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    with caplog.at_level(logging.INFO, logger="okto_neuron.models.runtime"):
        b1 = rt.detect_backend(None)
        b2 = rt.detect_backend(None)
    assert b1 == b2 == "llama_cpp"
    lines = [r for r in caplog.records if r.name == "okto_neuron.models.runtime"]
    assert len(lines) == 1, (
        f"expected exactly 1 log line, got {len(lines)}: {[r.message for r in lines]}"
    )


def test_preload_bounded_load(monkeypatch):
    if importlib.util.find_spec("fastembed") is None:
        pytest.skip("fastembed unavailable; preload test requires optional fastembed extra")
    from okto_neuron.models.embed import LocalEmbedder

    e = LocalEmbedder()
    calls = {"n": 0}
    orig_get = type(e).__dict__["_model"].func

    def counting(self):
        calls["n"] += 1
        return orig_get(self)

    monkeypatch.setattr(type(e).__dict__["_model"], "func", counting)
    e.preload()
    e.preload()
    e.embed_one("hi")
    assert calls["n"] == 1, f"expected exactly 1 model load, got {calls['n']}"
