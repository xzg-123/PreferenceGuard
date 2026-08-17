# PreferenceGuard MAR — Beneficial Memory guardrail

Status: `PHASE_2_MAR_BENEFICIAL_GUARDRAIL_PASS_ROBUST_TO_UNSCORED_SLOTS`  
Conclusion: `BENEFICIAL_MEMORY_PRESERVED`

| Result | Value |
| --- | ---: |
| Official PASS / FAIL | 18 / 1 |
| Officially scored / unscored | 19 / 1 |
| Robust lower / upper bound | 18/20 (90.00%) – 19/20 (95.00%) |
| Frozen gate | PASS |

## Preservation

- Observed comparable V2 PASS: `18` (V2 has `19` official PASS among `19`
  scored slots; one MAR slot remained unscored after early stop).
- V2 PASS → MAR PASS: `17`; V2 PASS → MAR FAIL: `1`.
- Observed Beneficial Preservation Rate: `17/18 = 94.44%`.

## Router behavior

- ALLOW `97` (44.29%); CONTEXT_ONLY `7` (3.20%); BLOCK `115` (52.51%).
- Zero-ALLOW queries: `1/20`.
- Malformed/degraded/missing/duplicate/hallucinated: `0/0/0/0/0`.

The robust Beneficial pass, 94.44% observed preservation among comparable V2
PASS records, and only one zero-ALLOW case support selective memory governance
rather than wholesale memory disablement. The single observed regression remains
material and is preserved as evidence; no treatment adjustment was made.

## Execution efficiency

- Router latency median / mean / P95: `17.28 / 19.34 / 35.69 s`.
- Generator latency: `19.30 / 21.78 / 38.95 s`.
- Judge latency: `4.77 / 5.44 / 8.75 s`.
- Router total tokens: `61,767`; Generator total tokens: `135,649`.
- Judge tokens and model cost: unavailable.
- Judge HTTP attempts/successes/429/resubmissions: `20/19/1/1`.

## Integrity

- Router / Generator / Judge semantic retries: `0 / 0 / 0`.
- Judge SDK max retries: `0`.
- Reserve / Frozen Validation reads: `0 / 0`.
- Cross-domain / additional Sycophancy calls: `0 / 0`.
