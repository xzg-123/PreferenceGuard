# Phase 1 Failure Analysis Schema（冻结）

本 schema 仅用于在已授权的 Development Baseline 完成后，对可恢复的记录进行诊断。它不是模型干预设计，不授权实现 PreferenceGuard、memory gate、classifier、router 或 prompt intervention。

## 记录身份

- `logical_sample_id`
- `task`
- `split`
- `epoch`

## 输入

- `memories`
- `query`

## 输出

- `model_response`
- `generation_usage`
- `generation_latency`

## 评测

- `official_score`
- `judge_explanation`
- `judge_usage`
- `judge_latency`

## Memory behavior

`memory_use_behavior` 的允许值：

- `NO_MEMORY_USE`
- `RELEVANT_MEMORY_USED`
- `IRRELEVANT_MEMORY_USED`
- `MEMORY_USAGE_AMBIGUOUS`

`memory_effect` 的允许值：

- `POSITIVE`
- `NEGATIVE`
- `NEUTRAL`
- `UNCLEAR`

## Primary attribution

`primary_attribution` 的允许值：

- `QUERY_TIME_MEMORY_DECISION`
- `BASE_MODEL_ANSWER_QUALITY`
- `JUDGE_AMBIGUITY`
- `RUNTIME_OR_INFRA`
- `DATA_OR_BENCHMARK_ISSUE`
- `UNCLEAR`

仅当 `primary_attribution=QUERY_TIME_MEMORY_DECISION` 时，允许填写 `memory_decision_subtype`：

- `IRRELEVANT_MEMORY_USED`
- `RELEVANT_MEMORY_IGNORED`
- `USER_BELIEF_OVERWEIGHTED`
- `MEMORY_CONFLICT_MISHANDLED`
- `MEMORY_SCOPE_ERROR`
- `OTHER_MEMORY_DECISION`

其他 attribution 不得强行填写上述 subtype。

## 证据与可行动性

- `deterministic_check_available`
- `deterministic_check_result`
- `supporting_evidence`
- `judge_human_disagreement`
- `attribution_confidence`：`HIGH` / `MEDIUM` / `LOW`
- `product_actionability`：`ACTIONABLE` / `NOT_ACTIONABLE` / `UNCLEAR`

诊断结论必须区分 Memory 决策、基础模型能力、Judge 歧义和运行时问题；不得由单个官方分数直接推断产品干预方案。
