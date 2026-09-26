# Creating the temporal-chat dataset

This is a synthetic, privacy-safe ADR 0040 corpus. It contains no exported private messages and
uses invented people, places, documents, activities, and rules.

The corpus deliberately separates three source-time states:

1. an initial owner and venue;
2. a later explicit correction; and
3. a later confirmation that also resolves an informal alias.

The Golden targets preserve both historical and corrected bytes. Passing recall therefore means
recovering the relevant evidence; it does not by itself prove first-class valid-time semantics.
The ADR 0040 temporal artifact must separately show that source time and correction order remain
available and that current-state queries retrieve the corrected owner and venue.

## Ground-truth review

- Dataset authoring: automated draft, 2026-07-17.
- Byte-target validation: automated harness validation.
- Owner review of expected answers and semantic adjudication: **pending**.

Until the owner review is recorded here, this dataset may be used for diagnostics but cannot satisfy
the human-reviewed ADR 0040 acceptance gate.
