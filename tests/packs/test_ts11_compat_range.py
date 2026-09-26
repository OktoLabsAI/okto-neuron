import pytest

from okto_neuron.core.schema import IncompatibleCoreVersionError, PackLoader


def test_ts11_incompatible_core_version_is_error_012():
    loader = PackLoader()

    with pytest.raises(IncompatibleCoreVersionError) as exc_info:
        loader.load(
            {
                "id": "future-only",
                "version": "0.1.0",
                "compatRange": ">=99.0",
            }
        )

    assert exc_info.value.code == "MARG-PACK-012"
