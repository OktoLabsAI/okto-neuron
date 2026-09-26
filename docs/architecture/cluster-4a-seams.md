# Cluster 4A — Cross-Topic Seams

## Topic 02/03 — Ingest

Ingest pipelines consume `Embedder.embed_batch(list[str]) -> np.ndarray (N, dim) float32` and `EntityExtractor.extract(text, labels, threshold) -> list[Span]`. Both APIs are stable across Cluster 4A; consumers MUST NOT depend on `_model_instance` or any private attributes.

## Topic 04 (Cluster 4C) — Model selection / quant

Cluster 4C reads `embedder.model_id` and extends `src/marginalia/models/manifest.toml` with sha256 + quant fields. 4A keeps manifest to `model_id` + `provider` only. 4C MUST be additive — no field renames.

## Topic 06 — Provenance

Provenance records persist `embedder.model_id` (string) and the detected runtime backend (`mlx` | `llama_cpp`). They MUST NOT persist embedder vectors or NER configuration — only identifiers, so reruns can detect drift.

## Topic 09 — MCP surface

MCP `marginalia://runtime/info` exposes `embedder.model_id`, `embedder.dim`, `extractor.model_id`, and the result of `detect_backend()`. The single backend-detection log line (`backend=… reason=…`) is the canonical evidence emitted at process start.
