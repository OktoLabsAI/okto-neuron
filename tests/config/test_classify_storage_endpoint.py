"""``_classify_storage_endpoint`` contract (M3 spec §2.7/§4).

Sibling of ``classify_api_base``: same SSRF host/IP classification, a wider
scheme allowlist (the full Neo4j driver scheme set plus http/https). The
last test in this file is a regression guard (D-04): ``classify_api_base``
itself must stay completely untouched by the sibling's addition.
"""

from __future__ import annotations

import pytest

from okto_neuron.config._vault import _classify_storage_endpoint, classify_api_base

_STORAGE_SCHEMES = ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc", "http", "https")


@pytest.mark.parametrize("scheme", _STORAGE_SCHEMES)
def test_loopback_host_classifies_as_loopback_across_every_scheme(scheme: str) -> None:
    assert _classify_storage_endpoint(f"{scheme}://127.0.0.1:7687") == "loopback"
    assert _classify_storage_endpoint(f"{scheme}://localhost:7687") == "loopback"


def test_private_ip_literal_classifies_as_private() -> None:
    assert _classify_storage_endpoint("bolt://10.0.0.5:7687") == "private"


def test_public_ip_literal_classifies_as_public() -> None:
    assert _classify_storage_endpoint("bolt://8.8.8.8:7687") == "public"


def test_a_plain_hostname_classifies_as_public_without_resolving() -> None:
    """resolve=False is the default -- config loading must not depend on DNS."""
    assert _classify_storage_endpoint("neo4j://graph.example.com:7687") == "public"


@pytest.mark.parametrize("scheme", _STORAGE_SCHEMES)
def test_every_storage_scheme_in_the_allowlist_is_accepted(scheme: str) -> None:
    assert _classify_storage_endpoint(f"{scheme}://graph.example.com") == "public"


def test_ftp_scheme_raises() -> None:
    with pytest.raises(ValueError, match="storage endpoint scheme"):
        _classify_storage_endpoint("ftp://graph.example.com")


def test_rejects_credentials_embedded_in_the_url() -> None:
    with pytest.raises(ValueError, match="username or password"):
        _classify_storage_endpoint("bolt://user:pass@graph.example.com:7687")


def test_rejects_a_missing_host() -> None:
    with pytest.raises(ValueError, match="must include a host"):
        _classify_storage_endpoint("bolt://")


def test_rejects_an_encoded_ip_host() -> None:
    with pytest.raises(ValueError, match="encoded IP literal"):
        _classify_storage_endpoint("bolt://2130706433:7687")


def test_classify_api_base_is_untouched_and_still_rejects_bolt_scheme() -> None:
    """Regression guard (D-04): the sibling's wider scheme allowlist must not
    have leaked into classify_api_base, which stays http/https-only."""
    with pytest.raises(ValueError, match="http or https"):
        classify_api_base("bolt://localhost:7687")
    assert classify_api_base("http://127.0.0.1:8100") == "loopback"
