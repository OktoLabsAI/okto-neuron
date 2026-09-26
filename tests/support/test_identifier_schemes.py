"""ts_f62e77db — Identifier per-scheme accept/reject matrix.

RFC §4.2 Identifier — multi-valued external IDs (QID/ORCID/DOI/ISBN/email/…).
Closed v0 registry per dec_57ecd6da.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Identifier


def _id(scheme, value, owner="agent:x"):
    return Identifier(scheme=scheme, value=value, owner_id=owner)


@pytest.mark.parametrize(
    "scheme,good,bad",
    [
        ("QID", "Q42", "Q0"),  # leading zero rejected
        ("QID", "Q123456", "X42"),  # non-Q rejected
        ("ORCID", "0000-0002-1825-0097", "0000-0002-1825-0090"),  # bad check digit
        ("ORCID", "0000-0001-2345-6789", "garbage"),
        ("DOI", "10.1038/nature12373", "11.1038/foo"),  # no 10. prefix
        ("ISBN", "9780306406157", "9780306406158"),  # ISBN-13 bad check
        ("ISBN", "0306406152", "0306406150"),  # ISBN-10 bad check
        ("EMAIL", "alex@oktolabs.ai", "not-an-email"),
        ("DOMAIN", "Oktolabs.AI", "..bad..tld"),
    ],
)
def test_identifier_per_scheme_accept_reject(scheme, good, bad):
    ok = _id(scheme, good)
    assert ok.scheme == scheme
    with pytest.raises(ValidationError):
        _id(scheme, bad)


def test_identifier_unknown_scheme_rejected():
    with pytest.raises(ValidationError):
        _id("NONESUCH", "whatever")


def test_identifier_domain_is_lowercased():
    i = _id("DOMAIN", "Oktolabs.AI")
    assert i.value == "oktolabs.ai"


def test_identifier_isbn_strips_separators():
    i = _id("ISBN", "978-0-306-40615-7")
    assert i.value == "9780306406157"
