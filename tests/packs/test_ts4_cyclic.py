import pytest

from okto_neuron.core.schema import CircularImportError, PackLoader


def test_ts4_cycle_has_witness_tuple():
    loader = PackLoader(
        registry={
            "biz": {
                "id": "biz",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "imports": ["research"],
            },
            "research": {
                "id": "research",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "imports": ["biz"],
            },
        }
    )

    with pytest.raises(CircularImportError) as exc_info:
        loader.load("biz")

    assert exc_info.value.cycle == ("biz", "research", "biz")
    assert exc_info.value.depth_exceeded is False
