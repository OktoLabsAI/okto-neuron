import pytest

from okto_neuron.core.schema import PackLoader, PackVersionConflictError


def test_ts3_same_manifest_is_cached_by_identity():
    loader = PackLoader()
    manifest = {
        "id": "same",
        "version": "0.1.0",
        "compatRange": ">=0.0.1",
        "types": [{"name": "Paper", "kind_of": "Asset"}],
    }

    first = loader.load(manifest)
    second = loader.load(manifest)

    assert second is first


def test_ts3_same_id_version_different_hash_conflicts():
    loader = PackLoader()
    loader.load(
        {
            "id": "same",
            "version": "0.1.0",
            "compatRange": ">=0.0.1",
            "types": [{"name": "Paper", "kind_of": "Asset"}],
        }
    )

    with pytest.raises(PackVersionConflictError) as exc_info:
        loader.load(
            {
                "id": "same",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "types": [{"name": "Dataset", "kind_of": "Asset"}],
            }
        )

    assert exc_info.value.source_a
    assert exc_info.value.source_b
    assert exc_info.value.hash_a != exc_info.value.hash_b
