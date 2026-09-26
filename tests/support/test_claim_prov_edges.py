"""ts_9c790c74 — CLAIM_PROV_EDGES constant integrity.

RFC §4.3: every Claim carries three PROV-O edges
(wasDerivedFrom→Block, wasGeneratedBy→ExtractionActivity,
wasAttributedTo→Agent). br_bf5139e2 exposes the mapping as a module constant.
"""

from __future__ import annotations

from okto_neuron.schema.support import CLAIM_PROV_EDGES


def test_claim_prov_edges_has_exactly_three_keys():
    assert set(CLAIM_PROV_EDGES) == {
        "wasDerivedFrom",
        "wasGeneratedBy",
        "wasAttributedTo",
    }


def test_claim_prov_edges_curies_and_targets():
    assert CLAIM_PROV_EDGES["wasDerivedFrom"] == {
        "curie": "prov:wasDerivedFrom",
        "target": "Block",
    }
    assert CLAIM_PROV_EDGES["wasGeneratedBy"] == {
        "curie": "prov:wasGeneratedBy",
        "target": "ExtractionActivity",
    }
    assert CLAIM_PROV_EDGES["wasAttributedTo"] == {
        "curie": "prov:wasAttributedTo",
        "target": "Agent",
    }


def test_claim_prov_edges_all_use_prov_namespace():
    for edge in CLAIM_PROV_EDGES.values():
        assert edge["curie"].startswith("prov:")
