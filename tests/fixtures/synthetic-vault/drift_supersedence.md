---
type: decision
created_at: 2026-03-10T09:00:00Z
agent: agent:alice
decision_id: dec-002
supersedes: dec-001
head: false
---

# Stale Supersedence Head

This decision record represents the middle entry in a fabricated decision chain. It supersedes `dec-001`, but it is also marked `head: false`, which means it should not be treated as the current governing decision.

The later chain member is `dec-003`, recorded in `decisions/decision-2026-02-15.md`. That newer entry supersedes this one and carries `head: true`. Keeping this middle note visible helps the vault exercise stale-head detection without requiring a large history.

The content describes a synthetic operating rule for intake triage. The team first agreed that all new notes should be tagged manually, then discovered that manual tagging created inconsistent categories across the vault. This middle decision proposed a controlled vocabulary, but it was later replaced by a narrower rule that only normalized high-risk records.

No real customer, domain, or production system is referenced here. The purpose is to create a deterministic supersedence path that a detector can traverse: `dec-001` is older, `dec-002` replaced it, and `dec-003` replaced `dec-002`.

## Chain Notes

The detector named `supersedence_stale_head` should flag this file if it expects a superseding decision to be current. This entry is intentionally stale because it points backward while another decision points to it as the replaced item.
