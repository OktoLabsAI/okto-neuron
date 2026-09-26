# Third-party notices

Okto Neuron is licensed under the Elastic License 2.0 with the Okto Labs
addendum (see `LICENSE`). It includes or adapts the third-party material
listed below, each under its own license. Python dependencies are installed
from PyPI at install time and are not bundled in this repository; their
licenses ship with each package.

## Web UI runtime dependencies

The built web UI in `frontend_dist/` (shipped inside the wheel as
`okto_neuron/_webui`) bundles the npm packages below: the non-dev entries of
`frontend/package-lock.json`, listed by package name. `zustand` appears twice
because `@xyflow/react` depends on an older major version. Their full license
texts ship with the bundle in `frontend_dist/THIRD_PARTY_LICENSES.txt`
(`okto_neuron/_webui/THIRD_PARTY_LICENSES.txt` in the wheel), generated at
build time by `frontend/scripts/third-party-licenses.mjs`.

| Package | Version | License |
|---|---|---|
| @types/d3-color | 3.1.3 | MIT |
| @types/d3-drag | 3.0.7 | MIT |
| @types/d3-interpolate | 3.0.4 | MIT |
| @types/d3-selection | 3.0.11 | MIT |
| @types/d3-transition | 3.0.9 | MIT |
| @types/d3-zoom | 3.0.8 | MIT |
| @xyflow/react | 12.10.2 | MIT |
| @xyflow/system | 0.0.76 | MIT |
| classcat | 5.0.5 | MIT |
| d3-color | 3.1.0 | ISC |
| d3-dispatch | 3.0.1 | ISC |
| d3-drag | 3.0.0 | ISC |
| d3-ease | 3.0.1 | BSD-3-Clause |
| d3-interpolate | 3.0.1 | ISC |
| d3-selection | 3.0.0 | ISC |
| d3-timer | 3.0.1 | ISC |
| d3-transition | 3.0.1 | ISC |
| d3-zoom | 3.0.0 | ISC |
| events | 3.3.0 | MIT |
| graphology | 0.26.0 | MIT |
| graphology-layout-forceatlas2 | 0.10.1 | MIT |
| graphology-types | 0.24.8 | MIT |
| graphology-utils | 2.5.2 | MIT |
| js-tokens | 4.0.0 | MIT |
| loose-envify | 1.4.0 | MIT |
| lucide-react | 0.460.0 | ISC |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |
| scheduler | 0.23.2 | MIT |
| sigma | 3.0.3 | MIT |
| use-sync-external-store | 1.6.0 | MIT |
| zustand | 4.5.7 | MIT |
| zustand | 5.0.14 | MIT |

## LLM judge prompt (Mem0 / MemGPT lineage)

The LoCoMo benchmark methodology (`docs/benchmarks/locomo.md`) uses an LLM
judge prompt, versioned `mem0-memgpt-v1`, that reproduces character for
character the judge prompt used in the Mem0 paper's LoCoMo evaluation, which follows the MemGPT
evaluation lineage:

- P. Chhikara et al., "Mem0: Building Production-Ready AI Agents with
  Scalable Long-Term Memory", arXiv:2504.19413, 2025.
- C. Packer et al., "MemGPT: Towards LLMs as Operating Systems",
  arXiv:2310.08560, 2023.

The prompt is reproduced so that scores are computed the same way as in
that published evaluation. It is credited to its authors and is not claimed
as Okto Labs work.

## LoCoMo dataset

Benchmark results refer to the LoCoMo dataset (A. Maharana et al.,
"Evaluating Very Long-Term Conversational Memory of LLM Agents", ACL 2024),
which is licensed CC BY-NC 4.0. No LoCoMo text is included in this
repository; only aggregate scores and per-question verdict labels are
published.
