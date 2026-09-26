"""ts_5afb70ea — same_work_as edge registration + symmetry.

RFC §4.2 says Document "may carry optional self-edge" (edge, not field). The
registered edge maps to `frbr-lrm:R3` (dec_a52ef052).
"""

from __future__ import annotations

from okto_neuron.schema.support import SAME_WORK_AS_EDGE, Document


def test_same_work_as_edge_curie_and_iri():
    assert SAME_WORK_AS_EDGE["name"] == "same_work_as"
    assert SAME_WORK_AS_EDGE["curie"] == "frbr-lrm:R3"
    assert SAME_WORK_AS_EDGE["iri"] == "http://iflastandards.info/ns/lrm/lrmer/R3"


def test_same_work_as_is_symmetric_doc_to_doc():
    assert SAME_WORK_AS_EDGE["symmetric"] is True
    assert SAME_WORK_AS_EDGE["domain"] is Document
    assert SAME_WORK_AS_EDGE["range"] is Document


def test_document_has_no_same_work_as_field():
    # Field-not-attribute is part of the contract (dec_a52ef052).
    assert "same_work_as" not in Document.model_fields
