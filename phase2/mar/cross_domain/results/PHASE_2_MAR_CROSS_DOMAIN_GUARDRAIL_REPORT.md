# Phase 2 MAR — Cross-domain Development Guardrail Report

## Final decision

`PHASE_2_MAR_CROSS_DOMAIN_GUARDRAIL_PASS`

`PHASE_2_MAR_DEVELOPMENT_PRODUCT_GATES_PASS`

`READY_FOR_DEVELOPMENT_FINAL_REVIEW`

The frozen cross-domain robust lower bound is **52/60 (86.67%)**, meeting the frozen gate of **>=52/60**.  Stage B stopped under the frozen guaranteed-pass rule after 56 official scores; the remaining four frozen outputs were not submitted to the Judge.

## Frozen execution outcome

| Metric | Result |
|---|---:|
| Frozen universe | 60 |
| Stage A immutable Router + Generator outputs | 60/60 |
| Officially scored | 56 |
| Unscored after frozen early-stop | 4 |
| Official PASS / FAIL | 52 / 4 |
| Robust lower bound | 52/60 (86.67%) |
| Robust upper bound | 56/60 (93.33%) |
| Gate | PASS |

## Paired V2 -> MAR comparison

Comparison is available for 55 scored slots with a frozen V2 comparator.

| Transition | Count |
|---|---:|
| PASS -> PASS | 49 |
| PASS -> FAIL (regression) | 3 |
| FAIL -> PASS (recovery) | 2 |
| FAIL -> FAIL | 1 |
| Net gain (recoveries - regressions) | -1 |

## Router behavior

| Outcome | Count | Share |
|---|---:|---:|
| ALLOW | 74 | 11.58% |
| CONTEXT_ONLY | 110 | 17.21% |
| BLOCK | 455 | 71.21% |
| Zero-ALLOW queries | 28 | 46.67% of 60 |
| Malformed / degraded fallback | 0 / 0 |
| Missing / duplicate / hallucinated IDs | 0 / 0 / 0 |

## Efficiency and provider infrastructure

| Component | Median (s) | Mean (s) | P95 (s) |
|---|---:|---:|---:|
| Router | 38.885 | 47.827 | 115.911 |
| Generator | 23.086 | 24.453 | 44.129 |
| Judge | 5.181 | 6.398 | 13.658 |

| Usage | Result |
|---|---:|
| Router tokens (input / output / reasoning / total) | 12,985 / 310,872 / 294,348 / 365,073 |
| Generator tokens (input / output / reasoning / total) | 8,720 / 119,359 / 67,327 / 415,823 |
| Judge tokens | Unavailable: official scorer does not expose usage |
| Cost | Unavailable: providers did not expose cost |
| Judge HTTP attempts / successful scores | 61 / 56 |
| HTTP 429 / infrastructure resubmissions | 5 / 5 |

All five 429s were handled only through the frozen infrastructure-resubmission path.  There were no semantic retries, Judge JSON repairs, or changes to treatment semantics.

## Integrity

| Check | Result |
|---|---:|
| Reserve reads | 0 |
| Frozen Validation reads | 0 |
| Additional Sycophancy calls | 0 |
| Additional Beneficial calls | 0 |
| Cross-domain Router calls / Generator calls | 60 / 60 |
| Cross-domain Judge HTTP attempts | 61 |
| Semantic retries | 0 |
| Judge SDK `max_retries` | 0 |

## Immutable artifacts and SHA-256

| Artifact | SHA-256 |
|---|---|
| `MAR_CROSS_DOMAIN_GUARDRAIL_PROTOCOL.md` | `8bf477407fcd7b10b99d8a337f1e49212aa0da99b103f48a86c9697bc355266a` |
| `cross-domain-guardrail-config.json` | `ab1ff4d338c0ae8d2b83a7d3c31a3fd4171a51f27eb38d3b8f0ac4664ba6f27e` |
| `MAR_CROSS_DOMAIN_GUARDRAIL_FREEZE.json` | `fb1748942ac9425dccda1bdd2c80f08ba7603de470d63b88632fd2a50a36d1b5` |
| `cross-domain-manifest.json` | `833a68b26f2302d7022596bf4a3eab0b6a6823eaf04c443285cf7832aa82a23f` |
| `stage-a-frozen-outputs.jsonl` | `49d9c808017975647e19afacf194d1c1d5ade9fcea17eb335a7d62601209bca3` |
| `stage-b-official-scores.jsonl` | `74295b363571281b5fd3211a474c89cd3cd067d0f9ef568a46e3cf7efcdd9ebb` |
| `cross-domain-summary.json` | `b72d0e920965632e3c15b59cb63c66fcee46edb4f96cbeb9a4fb8a1c194e753f` |

Execution ended after the frozen cross-domain guaranteed-pass condition.  No Reserve, Frozen Validation, unseen validation, MemSyco-Bench, MAR tuning, or additional Sycophancy/Beneficial execution was performed.
