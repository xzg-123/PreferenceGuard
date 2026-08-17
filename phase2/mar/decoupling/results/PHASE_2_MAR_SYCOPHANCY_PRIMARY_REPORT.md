# PreferenceGuard MAR — Sycophancy Primary Completion

Status: `PHASE_2_MAR_SYCOPHANCY_PRIMARY_GATE_PASS_ROBUST_TO_UNSCORED_SLOTS`

The frozen primary gate is robustly passed. Stage B stopped immediately after
the sixth newly official PASS; Cross-domain and Beneficial Memory were not run.

| Primary metric | Result |
| --- | ---: |
| Existing official PASS / FAIL | 30 / 17 |
| New official PASS / FAIL | 6 / 3 |
| Cumulative PASS / FAIL | 36 / 20 |
| Officially scored / unscored | 56 / 4 |
| Robust lower / upper bound | 36/60 (60.00%) – 40/60 (66.67%) |

## Decoupled execution

| Item | Result |
| --- | ---: |
| Existing persisted outputs | 48 |
| Newly generated outputs | 12 |
| Total frozen Sycophancy outputs | 60 / 60 |
| New Router / Generator calls | 12 / 12 |
| New Judge-scored slots | 9 |
| Judge HTTP attempts / successes | 10 / 9 |
| HTTP 429 / infrastructure resubmissions | 1 / 1 |
| Judge SDK max retries | 0 |
| Infrastructure wait overhead | 120 seconds |

## Latency, tokens, cost

- Stage A Router latency (median / mean / P95): `52.42 / 49.06 / 80.98 s`.
- Stage A Generator latency (median / mean / P95): `28.43 / 28.47 / 45.90 s`.
- Stage A model E2E, excluding infrastructure waits: `72.32 / 77.53 / 112.67 s`.
- Stage B Judge latency (median / mean / P95): `13.51 / 15.74 / 23.65 s`.
- Router tokens: input `3,042`, cache-read `9,216`, output `65,303`, reasoning `61,221`, total `77,561`.
- Generator tokens: input `1,951`, cache-read `57,344`, output `25,663`, reasoning `13,049`, total `84,958`.
- Judge token usage and all model cost: `UNAVAILABLE` (the official scorer/provider did not expose them).

## Integrity

- Router / Generator / Judge semantic retries: `0 / 0 / 0`.
- Reserve / Frozen Validation reads: `0 / 0`.
- Cross-domain / Beneficial calls: `0 / 0`.
- Protocol SHA256: `f4b7783192e832e8ac94a38a84d9f0b29995fc91a013e9b9ba27a4e904011805`.
