# Phase 1A — Formal Baseline Evaluation Design & Split Freeze 报告

## 1. 做了什么

仅执行了 Phase 1A 的本地、确定性、无模型调用的设计冻结：恢复 Phase 0 暴露样本、重算数据集 hash、创建 exclusion manifest、按固定 hash namespace 划分 Development Main / Reserve / Frozen Validation、验证完整性，并冻结预算与合同文档。

未运行 Baseline、generation、Judge、DeepSeek API、OpenRouter API 或 Frozen Validation；未实现 PreferenceGuard、memory gate、classifier、router、prompt intervention 或 Treatment。Deep Diagnostic controls namespace 已冻结为 `PreferenceGuard_Phase1_DeepDiagnosticControls_v1`，Human Spot-check seed 已冻结为 `PreferenceGuard_Phase1_HumanSpotcheck_v1`。

## 2. 原始 dataset hashes

| Dataset | 本地 SHA-256 | Freeze 对比 |
| --- | --- | --- |
| Cross-domain | `883fbf9e5733cfb2f577e8cd18ffe9a7ec4ea96f0195449fabe8b3fa2b6687b4` | PASS |
| Sycophancy | `634f5a533b4a3a7af55052b2c4bd55b4d30634e862ea4a48fb8e4dbbab412858` | PASS |
| Beneficial Memory | `26a951bee5da72049616674ffacfc9c725cd65f88355bee579831e4f51a044d3` | PASS |

Bundled logical sample counts 为 Cross-domain 200、Sycophancy 200、Beneficial Memory 100。

## 3. 排除了多少 Phase 0 exposed logical samples

排除 12 条唯一 logical samples，全部标记 `PHASE_0_EXPOSED_ONLY` 且状态为从 Development Main、Development Reserve 和 Frozen Validation 全部排除：

| Task | Excluded |
| --- | ---: |
| Cross-domain | 3 |
| Sycophancy | 3 |
| Beneficial Memory | 6 |

排除来源覆盖 Phase 0D smoke/gate、Phase 0E DeepSeek Judge calibration、cross-model blind review、12-case Human Audit 与 Phase 0F Kimi rescore。相关样本继续拥有 `JUDGE_CALIBRATION_ONLY` 标记。

## 4. 三个 task 各剩多少 eligible samples

| Task | Eligible |
| --- | ---: |
| Cross-domain | 197 |
| Sycophancy | 197 |
| Beneficial Memory | 94 |

## 5. Development Main counts

Cross-domain 20、Sycophancy 20、Beneficial Memory 20，合计 60 logical samples。

## 6. Development Reserve counts

Cross-domain 10、Sycophancy 10、Beneficial Memory 10，合计 30 logical samples。

## 7. Frozen Validation counts

Cross-domain 20、Sycophancy 20、Beneficial Memory 20，合计 60 logical samples。该 manifest 只含 ID、task、deterministic rank 与数据集 provenance；本阶段没有 response、score 或 Judge artifact。

## 8. overlap / contamination checks

固定 namespace：`PreferenceGuard_Phase1A_v1`；排序键为 `SHA256(namespace|task|logical_sample_id)`，不依赖 dataset 当前顺序。

| Check | Result |
| --- | ---: |
| Development Main × Reserve | 0 |
| Development Main × Validation | 0 |
| Reserve × Validation | 0 |
| Exclusion × Development Main | 0 |
| Exclusion × Reserve | 0 |
| Exclusion × Validation | 0 |
| split logical sample duplicates | 0 |
| task counts exact | PASS |
| deterministic rerun identical | PASS |

## 9. manifest SHA-256

| Manifest | SHA-256 |
| --- | --- |
| exclusion | `36db78a41aa502d47d4885ec8f74543d60636a0274bdbdd3092cc58908c85e10` |
| Development Main | `4cf553e2da80e4226d83d85dd8274920cb19d12403ff7d03f2029c9cf379abf8` |
| Development Reserve | `4560df24d894d9b98f131d280c96a28a79a80d08c261ede84e119cc10043214c` |
| Frozen Validation | `d65ad6c336165d87106ad93bccf69e7383757bc49bfba0e7a89f653c7ac4d17a` |

## 10. official epoch counts

Cross-domain 3、Sycophancy 3、Beneficial Memory 1 epoch/logical sample。Development Main 对应 60、60、20 epoch records，合计 140。

## 11. projected API calls

Phase 1B Development Baseline（仅预算，未执行）将有：140 DeepSeek generation calls、140 Kimi Judge calls。

## 12. projected token / latency budget

Kimi empirical baseline 来自 Phase 0F 的 24 calls：230,669 total tokens、285.234 秒，即约 9,611.208 tokens/call、11.88475 秒/call。Phase 1B expected Kimi usage：1,345,569.167 tokens、1,663.865 秒 aggregate latency；hard alert 使用上取整 buffer 后为 2,100,000 tokens。

DeepSeek generator-only Phase 0 metadata可直接恢复：25 epoch records、182,222 total tokens（7,288.88 tokens/epoch）。Phase 1B expected 为 1,020,443.2 tokens；hard alert 使用上取整 buffer 后为 1,600,000 tokens。

## 13. 是否有任何 ambiguity

没有。所有 Phase 0 exposed record 都能通过冻结 manifest、blind mapping、human hidden mapping、Kimi results 或真实 Inspect artifact 映射到唯一 `(task, logical_sample_id)`。

## 14. 是否进行了 API call

没有。DeepSeek API calls = 0；OpenRouter API calls = 0。

## 15. 是否运行了 Baseline

没有。Baseline / generation / Judge / Frozen Validation execution 均为 0。

## 16. git workspace status

根 workspace 是 `master` 分支、无可解析的 `HEAD`，当前 Phase 0/Phase 1 工件为 untracked；PersistBench checkout `inspect_evals` 位于 commit `7ee087d`，其 tracked workspace clean。本阶段没有 commit 或 push。

## 最终判断

`PHASE_1A_PASS / FORMAL_BASELINE_CONTRACT_FROZEN / SPLITS_FROZEN`

所有 integrity checks 为 PASS，未发现 mapping ambiguity。本阶段结束后停止；仅待产品审核后，才可接收是否进入 Phase 1B 的授权。
