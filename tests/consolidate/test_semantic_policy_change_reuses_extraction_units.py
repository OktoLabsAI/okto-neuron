"""ADR 0039 D5 clarification pin (plan-10 item (e), 2026-07-29 session).

D5 states: "a changed extraction fingerprint starts a new extraction run; a
downstream semantic-policy change may reuse raw unit output but recomputes
incompatible decisions and plans." Investigation found the clause is already
satisfied one layer below whole-run resume:

- ``CandidateLedger.find_resumable_run`` requires ``document_id``,
  ``blocks_total``, ``model``, ``extraction_fingerprint`` AND
  ``semantic_policy_fingerprint`` to match, so a pure semantic-policy change
  correctly declines to resume the old run (its curator/relation-curator
  verdicts are policy-dependent).
- ``CandidateLedger.successful_extraction_units`` is keyed only on
  ``document_id`` and ``extraction_fingerprint`` — no semantic-policy term —
  so the new run started after a policy change still reuses every durable
  successful extraction unit and pays no re-extraction provider cost.

This is a ledger-level pin of that invariant: no new machinery, per the
plan's recommendation, just a regression test proving the split is real.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.consolidate.ledger import CandidateLedger, extraction_unit_id

_EXTRACTION_FINGERPRINT = "sha256:" + "a" * 64
_POLICY_FINGERPRINT_V1 = "sha256:" + "b" * 64
_POLICY_FINGERPRINT_V2 = "sha256:" + "c" * 64  # a downstream semantic-policy change
_CONTENT_HASH = "sha256:" + "d" * 64
_UNIT_ID = extraction_unit_id(
    block_id="block-1",
    byte_start=0,
    byte_end=10,
    content_hash=_CONTENT_HASH,
    extraction_fingerprint=_EXTRACTION_FINGERPRINT,
)


def test_policy_change_declines_whole_run_resume_but_keeps_extraction_units(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)

    # Run 1: extraction succeeds for one unit, then the run is left "started"
    # (as if interrupted before a completed/failed ingest_run record), which is
    # the precondition find_resumable_run keys on.
    run1 = ledger.start_run(
        document_id="doc-1",
        source="note.md",
        blocks_total=1,
        model="fixture-model",
        semantic_policy_fingerprint=_POLICY_FINGERPRINT_V1,
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
    )
    ledger.record_extraction_unit(
        run1,
        document_id="doc-1",
        unit_id=_UNIT_ID,
        block_id="block-1",
        byte_start=0,
        byte_end=10,
        content_hash=_CONTENT_HASH,
        source_path="note.md",
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        attempt=0,
        status="succeeded",
        result={"node_candidates": [], "edge_candidates": []},
    )

    # Same document/blocks/model/extraction fingerprint as run1, but the
    # semantic policy changed underneath (e.g. a curator prompt/pack update) —
    # this is what a re-ingest after a semantic-policy sidefile edit looks
    # like at the ledger layer.
    resumable = ledger.find_resumable_run(
        document_id="doc-1",
        blocks_total=1,
        model="fixture-model",
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_POLICY_FINGERPRINT_V2,
    )
    assert resumable is None, (
        "a semantic-policy change must NOT resume the old run — its curator "
        "verdicts were computed under the old policy and are incompatible"
    )

    # But the same document/extraction-fingerprint pair still surfaces the
    # unit as reusable: successful_extraction_units carries no policy term.
    reusable = ledger.successful_extraction_units(
        document_id="doc-1",
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
    )
    assert _UNIT_ID in reusable, (
        "extraction-unit reuse must survive a semantic-policy change — only "
        "whole-run resume (which replays policy-dependent verdicts) is "
        "correctly declined above"
    )

    # Sanity: an unchanged policy DOES resume the same run (the positive
    # control proving find_resumable_run's exact-match semantics, not just
    # its rejection path).
    resumable_same_policy = ledger.find_resumable_run(
        document_id="doc-1",
        blocks_total=1,
        model="fixture-model",
        extraction_fingerprint=_EXTRACTION_FINGERPRINT,
        semantic_policy_fingerprint=_POLICY_FINGERPRINT_V1,
    )
    assert resumable_same_policy == run1

    # And a changed extraction fingerprint (not just policy) must NOT reuse
    # the unit — D5's other clause ("a changed extraction fingerprint starts
    # a new extraction run").
    reusable_after_extraction_change = ledger.successful_extraction_units(
        document_id="doc-1",
        extraction_fingerprint="sha256:" + "e" * 64,
    )
    assert _UNIT_ID not in reusable_after_extraction_change
