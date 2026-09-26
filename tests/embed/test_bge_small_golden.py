import importlib.util
import pathlib

import numpy as np
import pytest

from okto_neuron.models.embed import LocalEmbedder

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fastembed") is None,
    reason="fastembed unavailable; local BGE golden tests require optional fastembed extra",
)


GOLDEN = pathlib.Path(__file__).parent / "golden" / "bge_small_en_v1_5__384.npy"
FIXTURE_SENT = "Marginalia is a local-first knowledge graph."


def test_bge_small_golden_vector():
    vec = LocalEmbedder().embed_one(FIXTURE_SENT)
    golden = np.load(GOLDEN)
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
    assert abs(float((vec * vec).sum()) - 1.0) < 1e-5
    assert np.allclose(vec, golden, atol=1e-4, rtol=1e-3)
    assert list(np.argsort(np.abs(vec))[-5:]) == list(np.argsort(np.abs(golden))[-5:])


def test_embed_one_equals_batch_bytewise():
    from okto_neuron.models.embed import LocalEmbedder

    e = LocalEmbedder()
    t = "Bytewise equality test."
    assert e.embed_one(t).tobytes() == e.embed_batch([t])[0].tobytes()


def test_determinism_across_two_calls():
    import numpy as np
    from okto_neuron.models.embed import LocalEmbedder

    e = LocalEmbedder()
    t = "Marginalia indexes Markdown with provenance."
    a = e.embed_one(t)
    b = e.embed_one(t)
    assert np.array_equal(a, b)
