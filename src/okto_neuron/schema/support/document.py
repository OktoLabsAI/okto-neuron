"""Document support type — file/URL/email/message carrier.

DCMI Terms-aligned. Per dec_a52ef052 + br_cf4fc63f: Document has NO
`same_work_as` field; that relation is the registered edge SAME_WORK_AS_EDGE
mapping to CURIE `frbr-lrm:R3` (IRI http://iflastandards.info/ns/lrm/lrmer/R3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from ._common import HEX64, SupportBase

__all__ = ["Document", "SAME_WORK_AS_EDGE"]


class Document(SupportBase):
    """A carrier of content (file, URL, email, message).

    Required fields per br_7ca0c809. There is intentionally no `same_work_as`
    attribute — equivalence is an edge, not a field.
    """

    id: str
    uri: str
    media_type: str
    byte_length: int
    sha256: HEX64
    discovered_at: datetime


# Symmetric Document<->Document edge registered with the standards-mapping
# registry. Locked by dec_a52ef052 + br_cf4fc63f.
SAME_WORK_AS_EDGE: Final[dict[str, object]] = {
    "name": "same_work_as",
    "curie": "frbr-lrm:R3",
    "iri": "http://iflastandards.info/ns/lrm/lrmer/R3",
    "domain": Document,
    "range": Document,
    "symmetric": True,
}
