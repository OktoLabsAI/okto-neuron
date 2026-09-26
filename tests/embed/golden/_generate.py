from okto_neuron.models.embed import LocalEmbedder
import pathlib

import numpy as np

e = LocalEmbedder()
v = e.embed_one("Marginalia is a local-first knowledge graph.")
p = pathlib.Path("tests/embed/golden")
p.mkdir(parents=True, exist_ok=True)
np.save(p / "bge_small_en_v1_5__384.npy", v.astype(np.float32))
print("saved", v.shape, v.dtype)
