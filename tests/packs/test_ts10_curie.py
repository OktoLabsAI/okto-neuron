import pytest

from okto_neuron.core.schema import (
    MalformedCURIEError,
    UnknownCURIEPrefixError,
    parse_manifest,
)


def test_ts10_unknown_prefix_is_error_006():
    text = """
id: unknown-prefix
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: Paper
    kind_of: Asset
    extends:
      - nope:Thing
"""

    with pytest.raises(UnknownCURIEPrefixError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-006"
    assert exc_info.value.prefix == "nope"


def test_ts10_malformed_curie_is_error_007():
    text = """
id: malformed-curie
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: Paper
    kind_of: Asset
    extends:
      - not-a-curie
"""

    with pytest.raises(MalformedCURIEError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-007"


def test_ts10_pack_local_builtin_prefix_same_uri_is_ok():
    manifest = parse_manifest(
        """
id: same-prefix
version: "0.1.0"
compatRange: ">=0.0.1"
prefixes:
  bibo: "http://purl.org/ontology/bibo/"
types:
  - name: Paper
    kind_of: Asset
    extends:
      - bibo:Document
""",
        "inline",
    )

    assert manifest.prefixes["bibo"] == "http://purl.org/ontology/bibo/"


def test_ts10_pack_local_builtin_prefix_different_uri_is_error_007():
    text = """
id: bad-prefix
version: "0.1.0"
compatRange: ">=0.0.1"
prefixes:
  bibo: "https://example.test/not-bibo/"
types:
  - name: Paper
    kind_of: Asset
    extends:
      - bibo:Document
"""

    with pytest.raises(MalformedCURIEError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-007"
