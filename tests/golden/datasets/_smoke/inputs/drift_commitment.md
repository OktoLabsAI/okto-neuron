---
type: commitment
created_at: 2026-01-15T10:00:00Z
agent: agent:alice
committed_at: 2026-01-15T10:00:00Z
due: 2026-02-15
---

# Overdue Intake Commitment

agent:alice committed to reconcile the synthetic vault intake map before the February review window closed. The promised work was to compare incoming notes against the agreed data-quality checklist, identify unresolved provenance gaps, and prepare a short status note for the next planning review.

The commitment was recorded on 2026-01-15T10:00:00Z with a due date of 2026-02-15. That due date is intentionally in the past relative to the fixture reference date of 2026-05-19, so a temporal commitment detector can treat the entry as stale.

This file deliberately contains no linked proof item, no completion marker, and no closure record. It is written as a clean negative fixture: the agent made a time-bound promise, the promise is now overdue, and the surrounding vault does not provide an attached artifact that resolves the obligation.

The text stays plain so downstream tests can assert the drift without depending on layout quirks. Any remediation note, review packet, or validation record would need to be added in a separate artifact, but this fixture intentionally omits that follow-up.

## Expected Fixture Behavior

The detector named `commitment_temporal_shacl` should find this note because it has a commitment type, a committed timestamp, an elapsed due date, and no closing evidence path. The absence is part of the test contract rather than an editorial oversight.

## Review Notes

The commitment text intentionally avoids naming a follow-up artifact. A reviewer can understand the promise and the overdue state from this file alone, but cannot resolve it from the surrounding markdown because no closure note is supplied.
