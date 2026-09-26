import pytest

from okto_neuron.core.schema import PackLoader, SelfImportError


def test_ts5_self_import_is_distinct_error():
    loader = PackLoader(
        registry={
            "selfish": {
                "id": "selfish",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "imports": ["selfish"],
            }
        }
    )

    with pytest.raises(SelfImportError) as exc_info:
        loader.load("selfish")

    assert exc_info.value.code == "MARG-PACK-009"
