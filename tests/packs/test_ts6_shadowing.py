import logging

from okto_neuron.core.schema import PackLoader, PackRegistry


def test_ts6_shadowing_logs_and_records_finding(caplog):
    registry = PackRegistry()
    loader = PackLoader(registry=registry)

    loader.load(
        {
            "id": "alpha",
            "version": "0.1.0",
            "compatRange": ">=0.0.1",
            "types": [{"name": "Shared", "kind_of": "Concept"}],
        }
    )

    with caplog.at_level(logging.WARNING, logger="okto_neuron.core.schema"):
        loader.load(
            {
                "id": "beta",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "types": [{"name": "Shared", "kind_of": "Concept"}],
            }
        )

    record = next(record for record in caplog.records if record.message == "namespace shadowing")
    assert record.pack_id == "beta"
    assert record.shadowed_name == "Shared"
    assert record.shadowed_by_pack_id == "alpha"
    assert registry.findings == registry.find_shadowings()
    assert registry.findings[0].pack_id == "beta"
    assert registry.findings[0].shadowed_name == "Shared"
    assert registry.findings[0].shadowed_by_pack_id == "alpha"
