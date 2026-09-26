import pathlib
import tomllib


MANIFEST = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "okto_neuron" / "models" / "manifest.toml"
)


def test_manifest_toml_parses():
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "model" in data


def test_manifest_has_production_model_entries_only():
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    models = data["model"]
    assert len(models) == 1
    ids = {m["model_id"] for m in models}
    assert ids == {"BAAI/bge-small-en-v1.5"}
    for m in models:
        assert set(m.keys()) == {"model_id", "provider"}, f"Got extra keys: {m.keys()}"
        assert "sha256" not in m
