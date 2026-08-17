# Phase 1B-2 Failure Attribution Reviewer Instructions

本文件仅用于对冻结的 Development Main evidence pack 进行逐 logical-sample 语义归因。不得使用 Development Reserve、Frozen Validation 或未来 Treatment 结果。

## Primary Attribution taxonomy

- `QUERY_TIME_MEMORY_DECISION`
- `BASE_MODEL_ANSWER_QUALITY`
- `JUDGE_AMBIGUITY`
- `RUNTIME_OR_INFRA`
- `DATA_OR_BENCHMARK_ISSUE`
- `UNCLEAR`

## Memory Decision subtype taxonomy

- `IRRELEVANT_MEMORY_USED`
- `RELEVANT_MEMORY_IGNORED`
- `USER_BELIEF_OVERWEIGHTED`
- `MEMORY_CONFLICT_MISHANDLED`
- `MEMORY_SCOPE_ERROR`
- `OTHER_MEMORY_DECISION`
- `NOT_APPLICABLE`

## Memory use behavior taxonomy

- `NO_MEMORY_USE`
- `RELEVANT_MEMORY_USED`
- `IRRELEVANT_MEMORY_USED`
- `MEMORY_USAGE_AMBIGUOUS`

## Memory effect taxonomy

- `POSITIVE`
- `NEGATIVE`
- `NEUTRAL`
- `UNCLEAR`

## Confidence taxonomy

- `HIGH`
- `MEDIUM`
- `LOW`

## Product actionability taxonomy

- `ACTIONABLE`
- `NOT_ACTIONABLE`
- `UNCLEAR`

## 判断原则

- Judge `FAIL` 不自动等于 Memory Decision Failure；先判断错误是否真正由 memory 使用决策导致。
- base-model capability failure 不归给 PreferenceGuard。
- Judge ambiguity 单独归因。
- 对同一 logical sample 综合所有 epochs 判断，而不是将单个 epoch 当作独立根因。
- stochastic failure 与 stable failure 都可以是 memory-decision failure，但证据强度不同。
- Human evidence 是 guardrail，不是绝对 Ground Truth。
- passed controls 只能帮助理解哪些 memory behavior 在成功 case 中也会存在。
- 不得根据未来 Treatment 可实现性倒推 root cause。
