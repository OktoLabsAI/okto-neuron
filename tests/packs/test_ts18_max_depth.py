import pytest

from okto_neuron.core.schema import CircularImportError, PackLoader


def test_ts18_import_depth_limit_raises_circular_import_error():
    manifests = {}
    for index in range(33):
        pack_id = f"pack-{index:02d}"
        manifest = {
            "id": pack_id,
            "version": "0.1.0",
            "compatRange": ">=0.0.1",
        }
        if index < 32:
            manifest["imports"] = [f"pack-{index + 1:02d}"]
        manifests[pack_id] = manifest

    with pytest.raises(CircularImportError) as exc_info:
        PackLoader(registry=manifests).load("pack-00")

    assert exc_info.value.code == "MARG-PACK-008"
    assert exc_info.value.depth_exceeded is True
