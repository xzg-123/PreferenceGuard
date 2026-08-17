# Phase 1B-1 Complete Baseline Report

## 最终状态

```text
PHASE_1B1_COMPLETE /
DEVELOPMENT_BASELINE_QUALITY_COMPLETE /
READY_FOR_FAILURE_ANALYSIS
```

Development Main 已形成可恢复的 140/140 quality records：Cross-domain 60/60、Sycophancy 60/60 uniform official scores、Beneficial Memory 20/20。没有打开 Development Reserve 或 Frozen Validation。

## 完整性

| Task | logical samples | epoch records | valid scores | errors |
| --- | ---: | ---: | ---: | ---: |
| Cross-domain | 20 | 60 | 60 | 0 |
| Sycophancy | 20 | 60 | 60 | 0 |
| Beneficial Memory | 20 | 20 | 20 | 0 |
| Total | 60 | 140 | 140 | 0 |

Sycophancy canonical responses 为 57 `ORIGINAL_GENERATION` + 3 `RUNTIME_RECOVERY_GENERATION`；其 60 条正式分数全部来自单一 R3 uniform Kimi scoring artifact，未拼接历史 31 分数。

## 关键 artifacts

| Artifact | SHA-256 |
| --- | --- |
| Cross-domain eval | `e72d9f2d8c2f06f445c80c850a3bad84e9d0f32fd4b25b8a305194512a561428` |
| Sycophancy uniform scored eval | `05f4e40d5421ecffcfc04f915287d4913ddce499173f6fdb0427445bac57e19f` |
| Beneficial eval | `a83a5f8225fc02d17a0a419fdde5d1495e0aa9970f1d735bc461243cc6e39e9e` |

已生成无 attribution 结论的 Failure Analysis evidence pack、9/12 条可用的确定性 passed controls 和 30 条盲 Human–Kimi spot-check pack。Sycophancy 只有 1 条 passed logical sample，因此冻结的“每 task 4 条 passed controls”规则存在 3 条不可填补缺口；没有使用 failed sample 替补。上述资产不构成 dominant failure、treatment 或 PreferenceGuard 设计。

## 边界确认

- Generator：`deepseek/deepseek-v4-flash`；Judge：`openrouter/moonshotai/kimi-k2-thinking`。
- Judge 保持 temperature 0、reasoning effort high、reasoning enabled、Google Vertex routing、fallback disabled。
- PersistBench official threshold 与 task epoch 未变；Sycophancy R3 使用已经证明等价的 frozen direct-file scorer。
- Development Reserve = 0；Frozen Validation = 0；split contamination = 0。
- 未实现 PreferenceGuard，未进行 failure attribution、dominant failure 判定或 treatment。
