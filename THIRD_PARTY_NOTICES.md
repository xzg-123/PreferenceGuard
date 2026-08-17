# Third-party notices and reproducibility boundaries

PreferenceGuard is an original product/evaluation layer built **on top of**
existing open-source evaluation projects. This repository does not vendor their
source code, datasets, virtual environments, or generated evaluation logs.

## MemSyco-Bench

- Upstream: <https://github.com/XMUDeepLIT/MemSyco-Bench>
- Pinned revision used during the frozen work:
  `bdb670aaca99503caf9c9609a7f7ae2cd19b0049`
- License: MIT, copyright 2025 XMU-DeepLIT.
- Role here: source benchmark and source-data provenance for the memory-use
  evaluation framing.

The upstream repository's MIT license permits redistribution, but its dataset
files and derivative row-level artifacts are deliberately excluded from this
portfolio repository. Obtain the benchmark directly from its upstream project
and follow its current terms, citation guidance, and dataset documentation.

## Inspect Evals and PersistBench

- Upstream: <https://github.com/UKGovernmentBEIS/inspect_evals>
- Pinned revision used during the frozen work:
  `7ee087d4138ec1d869abc6a3beff30fb9b18f6b8`
- License: MIT, copyright 2024 UK AI Security Institute.
- Role here: evaluation harness and the upstream PersistBench task/scorer
  implementation used for frozen scoring.

`inspect_evals` includes its own `NOTICE` for components under additional
licenses. No `inspect_evals` source, bundled third-party component, or dataset
is copied into this repository; users should install it from upstream and retain
its notices.

## What PreferenceGuard adds

The original contribution in this repository is the query-time **Memory
Authority Router (MAR)** product treatment, its frozen decision contract,
hard-gating implementation, evaluation design, failure-analysis artifacts, and
product interpretation. It is not a reimplementation of either upstream
project.
