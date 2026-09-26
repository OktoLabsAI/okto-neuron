# Okto Neuron — Understanding

A self-contained, two-tab visual walkthrough of how Okto Neuron is understood,
verified against the live code on 2026-07-28 (the last full-page audit was
2026-07-13; §08 "Resolve — the talk-back" was re-verified on 2026-07-28 when the
dead `find_answers` probe was deleted from `src/okto_neuron/resolve/`). §11
"Re-ingest is incremental" and §14 "The whole picture" were re-verified against
live code on 2026-09-03 (vault-relative Block ids, deterministic-claim
retirement, add-vs-remember, and REST/MCP surface-parity hardening). §06–§10
(the write path: the six-stage flow, intake/propose, resolve, gate/commit, and
the runner) were re-verified on 2026-09-18, which corrected the default-model
and temperature claims and added the runtime progress-stage vocabulary, the
empty-model pre-flight, and the measured ingest-cost split. The rest of the page
still reflects the 2026-07-28 full-page audit.

- **🧠 Conceptual** — what Okto Neuron *is*: the 5-primitive closed schema, the
  Claim-as-atomic-unit provenance chain, the shift from passive store to
  autonomous curating companion, the embeddings↔graph relay, and the honest
  frontier (precision at firehose scale + abstraction).
- **🔧 Mechanical** — how it *works*: the `remember(doc)` write pipeline
  (intake → propose → stage → resolve → gate → commit/queue), the confidence
  math, incremental re-ingest and correction handling, the ambient runner,
  unified retrieval, the default source-block answer path, the opt-in efficient
  hybrid graph path, and the post-ingest curation control plane.

## How to view

Open `index.html` in any browser. No build step, no external assets, works
offline.

```bash
open index.html        # macOS
```

Tabs switch with the **1** / **2** keys; animations reveal on scroll.

The HTML is hand-authored rather than generator-owned. Its verification date is
bumped only after the conceptual and mechanical claims are checked against the
live implementation; the generated documentation gate does not certify it by
proxy.
