# Jev 语义阈值校准：脱敏中文标注集

本文定义把 Jev 从 `shadow` 切换到 `assist` 前必须完成的标注与校准流程。

**核心前提：Jev 的 `confidence` 只是分布集中度，不是准确率；`noul` 是 yes 概率，不是正确率。**
所有阈值必须用本项目自己的中文标注集校准，不得照搬示例值（当前默认 `yes=0.7` / `no=0.3` 只是占位）。

## 1. 采样与脱敏

**采样**：从真实群聊按能力分别分层抽样，刻意包含难例，避免只抽「好判断」的样本。

| 能力 | 样本单元 | 必须覆盖的难例 |
|---|---|---|
| `runtime_classification` | 单条消息 | 闲聊、接话、表情、纯图片 `[图片]`、撤回、一题多问、提问+人工回答重叠 |
| `human_answer_matching` | (新消息, 候选问题) 对 | 相近候选、答非所问、只回「好」「收到」、跨时间回答 |
| `issue_quality_gate` | 单个日报候选 | 寒暄被误判为问题、多问题混合、依赖截图但图片缺失 |

**建议规模**：每个能力 ≥ 200 个单元；`human_answer_matching` 至少包含 50 个真实正例（否则召回无意义）。

**脱敏规则（入集前必须执行）**：
- 删除/替换：人名、手机号、微信号、userid、订单号、商品 ID、Jira 号
- 删除：API Key、Bearer token、群 webhook URL、本地路径、base64/图片字节
- 图片一律替换为 `[图片]` 占位并记录 `image_status`，不保留像素
- 群名替换为 `群A/群B...`；时间保留到分钟即可

脱敏后的样本集存放在仓库外（本机或内部共享盘），**不入库、不随变更归档**。

## 2. 标注契约

**`runtime_classification`** —— 多标签，逐条独立判定（不互补）：

```
sample_id, text_ref, is_question, is_supplement, is_human_answer, is_withdrawn, is_chat, notes
```

允许重叠（例如同时为 `is_question` 和 `is_human_answer`）；**不得**用 `is_chat = 1 - is_question` 推导。

**`human_answer_matching`** —— 每个 (消息, 候选问题) 对独立标注：

```
sample_id, message_ref, candidate_ref, answered(0/1), tie_with(其他 candidate_ref), notes
```

**`issue_quality_gate`** —— 逐候选：

```
sample_id, candidate_ref, is_real_issue(0/1), evidence_sufficient(0/1), context_is_one_issue(0/1), image_status, notes
```

标注由两人独立完成，分歧先讨论再定稿；**分歧样本单独保留**，它们正是阈值最敏感的区域。

## 3. 必须记录的基线指标

对每个能力、每个阈值候选点，基于 `semantic_judgments` 审计表（`shadow` 阶段已积累）计算：

| 指标 | 说明 |
|---|---|
| **disagreement** | Jev 与现有 LLM/确定性路径不一致的比例（来自 `comparison_json`） |
| **precision / recall** | 以标注集为金标准，按 `yes` 判定计算 |
| **review 比例** | 落入 `[no, yes)` 区间、导致回退的比例 —— 直接决定可用性 |
| **fallback rate** | 全部回退原因占比：`not_eligible` / `image_only` / `too_large` / `timeout` / `invalid_response` |
| **latency** | p50 / p95（`latency_ms`） |
| **cost** | 按 `usage.input_tokens` / `output_tokens` 汇总 |

**判据**：只有当某能力在选定阈值下 **recall 不劣于现有路径**，且 review 比例低到不显著增加人工负担时，才可对该能力开启 `assist`。

## 4. 阈值落位

1. 全局默认 `decision_threshold` / `review_floor` 保持不变；
2. 按能力在 `jev.thresholds.<feature>.yes/.no` 覆盖；
3. 每次调整后重跑标注集，把指标追加到下方记录表；
4. 任何一次 `assist` 回归都可只回退该能力的阈值或开关，不影响其它能力与既有 work / issue key / 快照 / Smart Sheet 台账。

```
| 日期 | 能力 | yes | no | precision | recall | review比例 | fallback率 | p95延迟 | 结论 |
|------|------|-----|----|-----------|--------|-----------|-----------|---------|------|
|      |      |     |    |           |        |           |           |         |      |
```

## 5. 上线门禁

- 三个能力默认全部 `false`；`enabled=true` 也不会自动开启任何能力（已由 `normalize_jev_config` 保证）。
- 默认 `mode=shadow`；`shadow` 只记录，不改变任何业务结果。
- 切换到 `assist` 必须由 owner 逐能力显式开启，且必须先有本节第 3 节的基线记录。
- 不开放自动删除日报候选或自动改变高风险结果的 authoritative 模式。
