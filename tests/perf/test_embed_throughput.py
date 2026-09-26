import json
import os
import pathlib
import statistics
import time
import warnings

import pytest


pytestmark = [pytest.mark.perf]
CORPUS_PATH = pathlib.Path(__file__).resolve().parents[1] / "data" / "throughput_corpus.jsonl"


def _ensure_corpus(path: pathlib.Path = CORPUS_PATH) -> pathlib.Path:
    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as corpus:
        for index in range(1000):
            sentence = f"the quick brown fox jumps over fence number {index}"
            corpus.write(json.dumps({"text": sentence}) + "\n")
    return path


def test_embed_throughput_floor() -> None:
    if os.environ.get("OKTO_NEURON_PERF") != "1":
        pytest.skip("set OKTO_NEURON_PERF=1 to run throughput benchmark")

    from okto_neuron.models.embed import LocalEmbedder

    emb = LocalEmbedder()
    emb.preload()
    corpus_path = _ensure_corpus()
    with corpus_path.open(encoding="utf-8") as corpus:
        sentences = [json.loads(line)["text"] for line in corpus]

    emb.embed_batch(sentences[:32])
    n = 1000
    rates = []
    for _ in range(5):
        start = time.perf_counter()
        emb.embed_batch(sentences[:n])
        elapsed = time.perf_counter() - start
        rates.append(n / elapsed)

    median = statistics.median(rates)
    print(f"throughput median={median:.1f} texts/s")
    if median < 100:
        pytest.fail(f"throughput floor breach: {median} < 100 texts/s")
    if median < 200:
        warnings.warn(f"throughput soft warn: {median} < 200 texts/s")
