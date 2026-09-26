from pathlib import Path

import pytest

from okto_neuron.core.schema import (
    CircularImportError,
    ClosedSetViolationError,
    DuplicateTypeNameError,
    IncompatibleCoreVersionError,
    MalformedCURIEError,
    MissingKindOfError,
    PackLoader,
    PackVersionConflictError,
    SchemaValidationError,
    SelfImportError,
    SemverError,
    UnknownCURIEPrefixError,
    UnknownPrimitiveError,
    UnresolvedImportError,
    YamlParseError,
    parse_manifest,
)


INVALID_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pack-manifests" / "invalid"

EXPECTED = {
    "MARG-PACK-001": YamlParseError,
    "MARG-PACK-002": SchemaValidationError,
    "MARG-PACK-003": MissingKindOfError,
    "MARG-PACK-004": UnknownPrimitiveError,
    "MARG-PACK-005": ClosedSetViolationError,
    "MARG-PACK-006": UnknownCURIEPrefixError,
    "MARG-PACK-007": MalformedCURIEError,
    "MARG-PACK-008": CircularImportError,
    "MARG-PACK-009": SelfImportError,
    "MARG-PACK-010": UnresolvedImportError,
    "MARG-PACK-011": SemverError,
    "MARG-PACK-012": IncompatibleCoreVersionError,
    "MARG-PACK-013": DuplicateTypeNameError,
    "MARG-PACK-014": PackVersionConflictError,
}


@pytest.mark.parametrize("fixture", sorted(INVALID_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_ts2_negative_fixture_raises_matching_error(fixture):
    code = fixture.stem
    expected = EXPECTED[code]

    with pytest.raises(expected) as exc_info:
        _raise_for_fixture(code, fixture)

    assert exc_info.value.code == code


def _raise_for_fixture(code: str, fixture: Path) -> None:
    if code in {
        "MARG-PACK-001",
        "MARG-PACK-002",
        "MARG-PACK-003",
        "MARG-PACK-004",
        "MARG-PACK-005",
        "MARG-PACK-006",
        "MARG-PACK-007",
        "MARG-PACK-011",
        "MARG-PACK-013",
    }:
        parse_manifest(fixture.read_text(), str(fixture))
        return

    if code == "MARG-PACK-008":
        PackLoader(
            registry={
                "cycle-a": fixture,
                "cycle-b": {
                    "id": "cycle-b",
                    "version": "0.1.0",
                    "compatRange": ">=0.0.1",
                    "imports": ["cycle-a"],
                },
            }
        ).load("cycle-a")
        return

    if code == "MARG-PACK-009":
        PackLoader(registry={"self-import": fixture}).load("self-import")
        return

    if code == "MARG-PACK-010":
        PackLoader().load(fixture)
        return

    if code == "MARG-PACK-012":
        PackLoader().load(fixture)
        return

    if code == "MARG-PACK-014":
        loader = PackLoader()
        loader.load(fixture)
        loader.load(
            {
                "id": "conflict",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "types": [{"name": "Dataset", "kind_of": "Asset"}],
            }
        )
        return

    raise AssertionError(f"unhandled fixture {code}")
