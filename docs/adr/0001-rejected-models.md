# ADR 0001 — Rejected Models for Cluster 4A

Status: Accepted
Date: 2026-05-20

> **Lifecycle disposition — 2026-07-13:** this ADR preserves the Cluster 4A model-selection
> record. The production runtime uses FastEmbed by default and configured LLM extraction; it does
> not ship a dedicated GLiNER NER stage. The corrective post-`0.0.40` source candidate removes the
> remaining GLiNER dependency, manifest, and executable model path while retaining only a
> deprecated import-compatible tombstone that fails closed. The rejection criteria below
> remain historical evidence, not a requirement to select a replacement NER model.

## Context

Cluster 4A picks one embedder and one NER model for the local-first MVP, so all other candidates were rejected with explicit criteria so re-evaluation has a tripwire.

### OpenIE (Stanford CoreNLP)

OpenIE was rejected because its GPL-2 license is incompatible with Apache-2.0 redistribution.

- Would re-evaluate if relicensed.

### intfloat/e5-mistral-7b-instruct

`intfloat/e5-mistral-7b-instruct` was rejected because its 7B parameters blow past the 200 MB embed-side ceiling and laptop memory budget.

- Would re-evaluate if a 384-dim distilled head ships under 600 MB.

### jinaai/jina-embeddings-v3

`jinaai/jina-embeddings-v3` was rejected because its CC-BY-NC license restricts commercial use.

- Would re-evaluate if licensed under Apache-2.0 or MIT.

### Babelscape/REBEL

`Babelscape/REBEL` was rejected because its triple-extraction quality is high, but 460M parameters plus seq2seq decoding are too slow for local CPU.

- Would re-evaluate if a quantized <120M variant ships.

### Apple CoreML embedders

Apple CoreML embedders were rejected because Apple-platform lock-in violates the cross-OS portability requirement.

- Would re-evaluate if ONNX export with parity becomes official.
