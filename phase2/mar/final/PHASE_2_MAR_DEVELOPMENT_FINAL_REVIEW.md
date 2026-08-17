# PreferenceGuard Phase 2 MAR — Development Final Review

## Frozen final status

`PHASE_2_MAR_DEVELOPMENT_FINAL_REVIEW_PASS`

The frozen Development product evidence is internally consistent:

- `PHASE_2_MAR_SYCOPHANCY_PRIMARY_GATE_PASS_ROBUST_TO_UNSCORED_SLOTS`
- `PHASE_2_MAR_BENEFICIAL_GUARDRAIL_PASS_ROBUST_TO_UNSCORED_SLOTS`
- `BENEFICIAL_MEMORY_PRESERVED`
- `PHASE_2_MAR_CROSS_DOMAIN_GUARDRAIL_PASS`
- `PHASE_2_MAR_DEVELOPMENT_PRODUCT_GATES_PASS`

This is an offline review of existing immutable Phase 1 and Phase 2 artifacts.  It is not unseen validation, a production-readiness claim, or an SOTA claim.

## Architecture evolution

| Stage | Frozen design |
|---|---|
| Baseline | Unmodified memory-aware generation baseline. |
| V1 Memory Authority Rule | Declarative memory-authority rule at query-time generation. |
| V2 Memory Authority Procedure | Independent substantive judgment first; use belief-like memory only for contextualization, while retaining relevant preferences, constraints, and personal facts. |
| MAR | Query-time Memory Authority Router plus Hard Gating: Router labels each memory `ALLOW`, `CONTEXT_ONLY`, or `BLOCK`; only `ALLOW` enters the frozen V2 Generator context. |

MAR therefore retains V2's frozen Generator procedure and adds a separate frozen authority decision and conservative memory-off fallback for malformed/degraded routing.

## Sycophancy primary outcome

| Treatment | PASS / universe | Rate |
|---|---:|---:|
| Baseline | 11/60 | 18.33% |
| V1 | 16/60 | 26.67% |
| V2 | 26/60 | 43.33% |
| MAR (robust bound) | 36–40/60 | 60.00%–66.67% |

Using the MAR robust lower bound:

- MAR versus V2: **+10 PASS, +16.67 percentage points**.
- MAR versus Baseline: **+25 PASS, +41.67 percentage points**.

The MAR primary condition is robustly passed at 36/60 even with the four deliberately unscored early-stop slots treated as non-passing.

## Beneficial Memory guardrail

| Metric | Result |
|---|---:|
| MAR robust bound | 18–19/20 (90.00%–95.00%) |
| Observed V2-pass records preserved by MAR | 17/18 |
| Beneficial Preservation Rate | 94.44% |
| Frozen guardrail | PASS |

`BENEFICIAL_MEMORY_PRESERVED`

One observed V2-to-MAR regression is retained as evidence; no treatment adjustment was made.

## Cross-domain guardrail

| Metric | Result |
|---|---:|
| MAR robust bound | 52–56/60 (86.67%–93.33%) |
| Frozen guardrail | PASS |
| Comparable V2 paired records | 55 |
| Recoveries (FAIL -> PASS) | 2 |
| Regressions (PASS -> FAIL) | 3 |
| Net gain | -1 |

Cross-domain was preserved within the frozen product guardrail, **not improved**.

## Router behavior by frozen universe

These are separate decision populations and are not merged into a single percentage.

| Universe / observation scope | Decision denominator | ALLOW | CONTEXT_ONLY | BLOCK | Malformed | Fallback |
|---|---:|---:|---:|---:|---:|---:|
| Sycophancy: 60 persisted Stage A outputs | 630 | 91 (14.44%) | 160 (25.40%) | 379 (60.16%) | 0 | 0 |
| Beneficial: 20 Stage A outputs | 219 | 97 (44.29%) | 7 (3.20%) | 115 (52.51%) | 0 | 0 |
| Cross-domain: 60 Stage A outputs | 639 | 74 (11.58%) | 110 (17.21%) | 455 (71.21%) | 0 | 0 |

Zero-ALLOW query counts were 1/20 for Beneficial and 28/60 for Cross-domain.  All reported router artifacts have zero missing, duplicate, and hallucinated decision IDs.  Sycophancy's 60-record offline reconstruction likewise found zero malformed outputs and zero degraded fallbacks.

## Efficiency and trade-off

The available execution measurements are retained at their published scope; they are not combined across different universes or partial Stage B queues.

| Universe / measurement scope | Router latency, median / mean / P95 (s) | Generator latency, median / mean / P95 (s) | Judge latency, median / mean / P95 (s) | Router / Generator total tokens |
|---|---|---|---|---:|
| Sycophancy: decoupling Stage A (12 new outputs), Stage B (9 scores) | 52.42 / 49.06 / 80.98 | 28.43 / 28.47 / 45.90 | 13.51 / 15.74 / 23.65 | 77,561 / 84,958 |
| Beneficial: Stage A 20 outputs, Stage B 19 scores | 17.28 / 19.34 / 35.69 | 19.30 / 21.78 / 38.95 | 4.77 / 5.44 / 8.75 | 61,767 / 135,649 |
| Cross-domain: Stage A 60 outputs, Stage B 56 scores | 38.89 / 47.83 / 115.91 | 23.09 / 24.45 / 44.13 | 5.18 / 6.40 / 13.66 | 365,073 / 415,823 |

| Judge infrastructure in the decoupled guardrail runs | Sycophancy | Beneficial | Cross-domain |
|---|---:|---:|---:|
| HTTP attempts / successful official scores | 10 / 9 | 20 / 19 | 61 / 56 |
| HTTP 429 / infrastructure resubmissions | 1 / 1 | 1 / 1 | 5 / 5 |

Judge token usage and Judge/model cost are unavailable wherever the provider did not expose them.  A V2 end-to-end latency, token, and cost multiplier is not claimed: the frozen V2 evidence does not provide a reliable complete comparator for those quantities.  MAR achieved stronger memory governance at the cost of an additional Router hop and material latency overhead.

## Engineering reliability improvement

The initial coupled `Router -> Generator -> Judge` execution encountered Judge infrastructure failures.  The frozen execution process was then made reliable through:

1. Generation/scoring decoupling.
2. Immutable persistence of Router and Generator outputs.
3. Scoring-only recovery using the same frozen official Judge/scorer.
4. Explicit Judge SDK `max_retries=0`, with separately recorded frozen infrastructure resubmissions only.

This was an execution reliability optimization, not Treatment tuning: Router policy, prompts, Hard Gating, V2 Generator procedure, Judge/scorer, PASS criterion, and frozen gates were unchanged.  Semantic retries and Judge JSON repair remained zero.

## Integrity and boundary

| Check | Result |
|---|---:|
| API calls during this final review | 0 |
| New benchmark slots during this final review | 0 |
| Reserve reads | 0 |
| Frozen Validation reads | 0 |
| Unseen validation | Not performed |
| Remaining early-stop slots | Not scored or backfilled |
| Treatment / prompt / Router / Generator / Judge / scorer modifications | 0 |

## Final Development conclusion

PreferenceGuard MAR demonstrated a material Sycophancy improvement over V2 while preserving Beneficial Memory and maintaining Cross-domain performance within pre-frozen guardrails.

The Development evidence supports `PHASE_2_MAR_DEVELOPMENT_FINAL_REVIEW_PASS`.  It does **not** establish unseen-validation performance, production readiness, or state-of-the-art performance.

## Primary source artifacts

- `artifacts/phase-1b/PHASE_1B1_COMPLETE_BASELINE_REPORT.md`
- `artifacts/phase-1d/phase-1d-treatment-summary.json`
- `artifacts/phase-1e/sycophancy/s2/phase-1e-s2-primary-metric.json`
- `phase2/mar/decoupling/results/decoupling-summary.json`
- `phase2/mar/beneficial/results/beneficial-summary.json`
- `phase2/mar/cross_domain/results/cross-domain-summary.json`
