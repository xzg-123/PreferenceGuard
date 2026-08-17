# Phase 1 Baseline–Treatment Control Contract（冻结）

未来只有在产品层明确授权 Treatment 后，才可以运行 paired Baseline vs Treatment。Treatment 完全冻结前，Frozen Validation 不得运行。

## 唯一可变项

Baseline 与 Treatment 只能存在一个已预先定义、可描述、可审计的 intervention variable。不得同时修改多个变量，也不得用 Judge、阈值、样本或重试策略抵消 Treatment 效应。

## 必须保持一致的控制变量

- Frozen Validation IDs
- memories
- queries
- Generator model
- generation parameters
- Judge
- Judge parameters
- routing
- fallback setting
- scorer
- threshold
- epoch
- Inspect CLI
- runtime version
- logging mode
- output schema
- retry policy

正式 Evaluation Stack 为 PersistBench / Inspect、`deepseek/deepseek-v4-flash` Generator、官方 `openrouter/moonshotai/kimi-k2-thinking` Judge、官方 scorer/threshold/epoch、Official Inspect CLI 与项目内日志运行时；原始 eval logging 为 `--no-log-realtime`。

## Final Validation 对比记录

最终 Validation 必须是 paired Baseline vs Treatment，并保留每个匹配 record 的：

- `treatment_wins`
- `baseline_wins`
- `both_pass`
- `both_fail`

已知 `OFFICIAL_JUDGE_LOCAL_RELIABILITY_RISK` 必须随正式实验报告保留；它不授权替换 Judge 或修改配置。
