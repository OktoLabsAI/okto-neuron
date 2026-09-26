"""Extraction-granularity fix tests (north-star lever: mint quantitative/path/
value literals as atomic S-P-O claims).

Three mechanisms, three test groups — all model-free:
1. PROMPT (mechanism A): the SCALAR SWEEP completeness rule is present in
   ``_BASE_SYSTEM`` after the composite carve-out, covering fenced config
   blocks, table cells, and commented defaults.
2. PROMPT (mechanism B): the VERBATIM-STRING literal rule + Example 17 pin the
   "exact technical string survives entity extraction" behavior
   (yolo_nas_s.onnx must never be replaced by a prettified Concept title).
3. PARSE (mechanism C): ``parse_extraction`` binds claim subjects through
   casefold/whitespace normalization (exact match wins), and claims dropped
   for no-subject / metadata-predicate are COUNTED on ``ExtractionResult``
   instead of vanishing silently.
"""

from __future__ import annotations

import json

from okto_neuron.extract import _BASE_SYSTEM, ExtractionResult, parse_extraction


# ── 1. SCALAR SWEEP rule (mechanism A: model yield in dense blocks) ────────────


def test_scalar_sweep_rule_present() -> None:
    assert "SCALAR SWEEP" in _BASE_SYSTEM
    # the re-scan checklist is the load-bearing device
    assert "RE-SCAN" in _BASE_SYSTEM
    # sweep reaches fenced config blocks and table cells (qt-109 class)
    assert "fenced code/config blocks and table cells" in _BASE_SYSTEM
    # omission is an error; duplication is not (bias toward completeness)
    assert "Omitting a scalar is an error" in _BASE_SYSTEM
    # runaway guard: enumerations composite per group (16k completion cap —
    # 2 of 5 live runs on a changelog-heavy dense block truncated without this)
    assert "ONE composite literal per group" in _BASE_SYSTEM


def test_scalar_sweep_placement_after_composite_carveout() -> None:
    """The sweep is a completeness check appended AFTER the composite rules
    and before the empty-block rule — never before them, so the
    composite/atomic decision procedure is already established when it fires."""
    carveout = _BASE_SYSTEM.index("INDEPENDENT attributes stay ATOMIZED")
    sweep = _BASE_SYSTEM.index("SCALAR SWEEP")
    empty_rule = _BASE_SYSTEM.index("If the block has no entity AND no fact")
    assert carveout < sweep < empty_rule


def test_scalar_sweep_commented_default_is_a_fact() -> None:
    """qt-109 mechanism: a config default in a fenced YAML block must be swept."""
    assert "timeout: 30  # default: 30" in _BASE_SYSTEM
    assert '"detect timeout: 30 (default 30)"' in _BASE_SYSTEM


# ── 2. VERBATIM-STRING rule + Example 17 (mechanism B: literal→entity) ────────


def test_verbatim_string_rule_present() -> None:
    assert "VERBATIM-STRING literals" in _BASE_SYSTEM
    # the entity title must never replace the exact string (sup-003 mechanism)
    assert "never" in _BASE_SYSTEM and "replaces the literal" in _BASE_SYSTEM
    # both the edge and the literal claim are required
    assert "BOTH the topology edge" in _BASE_SYSTEM
    # verbatim is pinned as character-exact, with the prettification named as
    # the error (the model's observed failure mode)
    assert "CHARACTER-EXACT" in _BASE_SYSTEM


def test_example_17_verbatim_technical_strings() -> None:
    assert "Example 17" in _BASE_SYSTEM
    # the exact model-file string is carried verbatim inside a claim literal
    # (as a has_config value — the dense has_* family is ADR-0020 locked, so
    # no has_model coinage) ...
    assert '"object":"detection model: ssd_mobilenet_v2.onnx"' in _BASE_SYSTEM
    # ... alongside (not instead of) the entity node + uses edge
    assert '"title":"SSD-MobileNet-V2"' in _BASE_SYSTEM
    assert '"type":"uses","src":"Watchtower","dst":"SSD-MobileNet-V2"' in _BASE_SYSTEM
    # the filesystem path is minted verbatim (mh-005 mechanism)
    assert '"/mnt/nas/recordings/watchtower"' in _BASE_SYSTEM


def test_new_rules_do_not_embed_eval_corpus_strings() -> None:
    """The new rules/example must NOT quote the private evaluation corpus verbatim:
    a corpus string in the prompt makes A/B evidence circular (echo vs
    extraction is indistinguishable) and was observed to SUPPRESS emission
    (Example 14's verbatim '12,504 files...' was never emitted in 130k ledger
    rows; two live runs with 'yolo_nas_s.onnx' in the example never minted it
    from the real Frigate block)."""
    for corpus_string in ("yolo_nas_s", "Cameras/frigate", "fps: 5"):
        assert corpus_string not in _BASE_SYSTEM, corpus_string


def test_example_17_predicates_pass_metadata_filter() -> None:
    """Example 17's predicates must survive the parse-time metadata filter —
    a worked example that gets silently dropped would be a self-defeating
    prompt. (file_path/filepath ARE in _METADATA_PREDICATES; data_path /
    has_config are not.)"""
    from okto_neuron.extract import _is_metadata_predicate

    for pred in ("data_path", "has_config"):
        assert not _is_metadata_predicate(pred), pred


# ── 3. Parse hardening (mechanism C: silent claim drops) ──────────────────────


def _payload(nodes: list[dict], claims: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "edges": [], "claims": claims})


_FRIGATE_NODE = {
    "type": "Concept",
    "title": "Frigate",
    "content": "A camera NVR service.",
}


def test_claim_subject_binds_casefolded() -> None:
    result = parse_extraction(
        _payload(
            [_FRIGATE_NODE],
            [{"subject": "frigate", "predicate": "has_model", "object": "yolo_nas_s.onnx"}],
        )
    )
    claims = [e for e in result.edge_candidates if e.dst_literal is not None]
    assert len(claims) == 1
    assert claims[0].type == "has_model"
    assert claims[0].dst_literal == "yolo_nas_s.onnx"
    assert claims[0].src_ref == result.node_candidates[0].candidate_id
    assert result.claims_dropped_no_subject == 0


def test_claim_subject_binds_whitespace_normalized() -> None:
    node = {"type": "Concept", "title": "Google Drive", "content": "A storage service."}
    result = parse_extraction(
        _payload(
            [node],
            [{"subject": "google  drive", "predicate": "has_status", "object": "mirror"}],
        )
    )
    claims = [e for e in result.edge_candidates if e.dst_literal is not None]
    assert len(claims) == 1
    assert claims[0].src_ref == result.node_candidates[0].candidate_id


def test_exact_subject_match_wins_over_normalized() -> None:
    """Two same-block titles differing only by case: the exact spelling binds
    to its own node; the normalized fallback never shadows an exact match."""
    nodes = [
        {"type": "Concept", "title": "HP", "content": "An organization."},
        {"type": "Concept", "title": "hp", "content": "A unit of horsepower."},
    ]
    result = parse_extraction(
        _payload(
            nodes,
            [{"subject": "hp", "predicate": "has_value", "object": "745.7 W"}],
        )
    )
    by_title = {n.title: n.candidate_id for n in result.node_candidates}
    claims = [e for e in result.edge_candidates if e.dst_literal is not None]
    assert len(claims) == 1
    # Titles are no longer recased, so "hp" stays "hp" — the exact raw-title
    # match must win; the normalized fallback (which maps "hp" → the first
    # node, "HP") must not fire. The two ids still differ because candidate_id
    # folds only the TITLE; the distinct `content` keeps them separate.
    assert claims[0].src_ref == by_title["hp"]
    assert claims[0].src_ref != by_title["HP"]


def test_unresolvable_subject_is_counted_not_silent() -> None:
    result = parse_extraction(
        _payload(
            [_FRIGATE_NODE],
            [
                {"subject": "Nonexistent Node", "predicate": "has_value", "object": "42"},
                {"subject": "Frigate", "predicate": "has_config", "object": "fps=5"},
            ],
        )
    )
    assert result.claims_dropped_no_subject == 1
    claims = [e for e in result.edge_candidates if e.dst_literal is not None]
    assert len(claims) == 1  # the well-formed claim still lands


def test_metadata_predicate_drop_is_counted() -> None:
    """_METADATA_PREDICATES stays intact (deliberately not relaxed) but its
    kills are now visible: a model paraphrase to file_path is counted."""
    result = parse_extraction(
        _payload(
            [_FRIGATE_NODE],
            [
                {
                    "subject": "Frigate",
                    "predicate": "file_path",
                    "object": "/mnt/nas/Backup/Cameras/frigate",
                },
            ],
        )
    )
    assert result.claims_dropped_metadata == 1
    assert not [e for e in result.edge_candidates if e.dst_literal is not None]


def test_malformed_claims_do_not_inflate_drop_counters() -> None:
    """Noisy predicates / empty literals are pre-existing rejections, not
    subject-binding or metadata drops — they must not pollute the counters."""
    result = parse_extraction(
        _payload(
            [_FRIGATE_NODE],
            [
                {"subject": "Frigate", "predicate": "n_a", "object": "x"},
                {"subject": "Frigate", "predicate": "has_value", "object": ""},
                {"subject": "Frigate", "predicate": "has_value", "object": None},
            ],
        )
    )
    assert result.claims_dropped_no_subject == 0
    assert result.claims_dropped_metadata == 0
    assert not [e for e in result.edge_candidates if e.dst_literal is not None]


def test_counters_default_zero_on_empty_result() -> None:
    result = ExtractionResult()
    assert result.claims_dropped_no_subject == 0
    assert result.claims_dropped_metadata == 0
