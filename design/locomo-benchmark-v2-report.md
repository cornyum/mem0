# LOCOMO Benchmark v2 报告（抽取 v2 × `/v1/memory/*`）

> **消融矩阵已完成**：recall 通道 / rerank / chunk-size / 抽取模型对比见 [`design/locomo-benchmark-v2-ablation-report.md`](locomo-benchmark-v2-ablation-report.md)。

> 运行日期：2026-08-16 · 代码基线：`feature/memory-research` @ `7f3f6fa3`
> 结果文件：`server/scripts/benchmarks/locomo/results/v2-full-chunk5-extract-rerank.json`
> 方案：`design/locomo-benchmark-v2-plan.md` · 对比基线：`design/locomo-benchmark-report.md`（v1）

## 1. TL;DR

| 指标 | v1（基线） | v2 | 变化 |
|---|---|---|---|
| **总体准确率（1540 题）** | **33.3%**（513/1540） | **72.7%**（1120/1540） | **+39.4pp** |
| temporal（321 题） | 16.5%（53） | **75.7%**（243） | **+59.2pp** |
| multi-hop（282 题） | 45.7%（129） | **67.4%**（190） | +21.7pp |
| single-hop（841 题） | 36.4%（306） | **76.5%**（643） | +40.1pp |
| open-domain（96 题） | 26.0%（25） | 45.8%（44） | +19.8pp |
| 摄取事实数（5,882 轮） | 2,153（36.6%） | **4,504（76.6%）** | 2.09× |
| “无信息”答案占比 | 39.2% | **7.0%** | -32.2pp |
| 召回 p50 / p95 | 716.7 / 974.2 ms | 897.9 / 1229.0 ms | 延迟上升（见 §5） |

结论：抽取质量是 v1 的主要瓶颈；本次只改知识加工侧（prompt + timestamp + 角色映射 +
chunk 5），未动召回/答题/裁判，总体准确率翻倍以上，temporal 提升 4.6 倍。

## 2. 方案与口径

### 2.1 数据集

- `server/scripts/benchmarks/locomo/dataset/locomo10.json`（2.8 MB），已下载并校验：
  10 会话 / 272 非空 session / 5,882 轮 / 1,986 题；
  category 分布 1:282 / 2:321 / 3:96 / 4:841 / 5:446，与官方一致。
- 评分：category 1-4（1,540 题）计分；category 5 剔除。官方 LOCOMO 协议。

### 2.2 数据录入（知识加工）

- `POST /v1/memory/remember`，`mode=extract`，`kind=fact`，`categories=["locomo"]`。
- **抽取 prompt**：`ADDITIVE_EXTRACTION_PROMPT`（双说话人、exhaustive checklist、
  relative-date 归一化规则）。
- **时间锚点**：每批请求 `timestamp="YYYY-MM-DD"`（session 绝对日期）；
  默认同时把日期织入 content，便于与 v1 衔接（`--no-inline-session-date` 可消融）。
- **角色映射**：官方 runner 口径，`speaker_a→user`、`speaker_b→assistant`。
- **batch**：`--chunk-size 5`（v1 为 25；官方 runner 为 1）。
- 图片 blip/query 按官方方式合并；会话按时间戳排序录入，跨会话并发 2。
- metadata：`locomo_sample_id / locomo_session / session_date`。

### 2.3 存储与清库

- `ONLY_VDB`，Elasticsearch 8.17 单一权威（`agentar_mem0` 五族索引 + analysis-ik），
  MySQL 仅应用表。
- 运行前 `POST /reset`：本运行暴露并修复了 `delete_all` 在“旧 concrete index 名与
  新 alias 名相同”时 `ensure_indices` 报 `invalid_alias_name_exception` 的问题
  （commit `7f3f6fa3`）。修复后 reset 200，五族 alias 均指向 `*_v1`。
- 会话隔离：`user_id = locomo_extract-auto-rerank_{sample_id}`。

### 2.4 模型（运行时 `/v1/capabilities` 采集）

| 角色 | 模型 | 说明 |
|---|---|---|
| 抽取 LLM | `qwen-plus-latest` | ADDITIVE prompt |
| 向量 | `qwen3.7-text-embedding`（1024 维） | ES dense_vector cosine |
| 重排 | `qwen3-vl-rerank` | recall `rerank=true` |
| 答案生成 | `qwen-plus-latest`（temperature 0） | benchmark 侧 DashScope 调用 |
| 裁判 | `qwen-plus-latest`（temperature 0） | 官方容忍规则 |

### 2.5 评测管线

五阶段 checkpoint，与 v1 同一脚本：

1. preflight：`/health/ready` → `/v1/capabilities` → `POST /reset`。
2. ingest：1283 批 `remember`，统计 created/updated/noop/errors。
3. recall：1,540 题 `POST /v1/memory/recall`（`mode=auto`、`rerank=true`、`limit=50`）。
4. answer：官方式答案生成（参考日期 = 会话最后 session 日期），并发 4。
5. judge：官方容忍规则二值判分，并发 4。

## 3. 总体结果

### 3.1 指标

| 指标 | 值 |
|---|---|
| 总体准确率 | **72.7%（1120/1540）** |
| 摄取 | 1283 批 / **4,504 条 fact** / noop 0 / 0 错误 / 3,707 s |
| 召回 | 1,540 题 / p50 **897.9 ms** / p95 **1229.0 ms** / avg_results 50.0 / 1,441 s |
| 答案 | 1,540 题 / 0 错误 / 447 s |
| 裁判 | 1,540 题 / 0 错误 / 232 s |
| 端到端 | ~97 min（run_id 11:33:41 → finished 13:10:51） |

### 3.2 分 category

| category | 题数 | v1 正确 | v2 正确 | v1 准确率 | v2 准确率 | v2 “无信息” |
|---|---|---|---|---|---|---|
| 1 multi-hop | 282 | 129 | **190** | 45.7% | **67.4%** | 6（2.1%） |
| 2 temporal | 321 | 53 | **243** | 16.5% | **75.7%** | 30（9.3%） |
| 3 open-domain | 96 | 25 | 44 | 26.0% | 45.8% | 32（33.3%） |
| 4 single-hop | 841 | 306 | **643** | 36.4% | **76.5%** | 40（4.8%） |

“无信息”判定：预测文本含 `not specified / not mentioned / no record / no
information / unknown / cannot be determined / no relevant` 等拒绝式措辞。
v1 用同一规则复算为 604/1540（39.2%，与报告一致）；v2 为 **108/1540（7.0%）**。

### 3.3 每会话

| session | v1 | v2 | session | v1 | v2 |
|---|---|---|---|---|---|
| conv-26 | 33.6% | 67.8% | conv-44 | 35.0% | 77.2% |
| conv-30 | 34.6% | 70.4% | conv-47 | 27.3% | 70.7% |
| conv-41 | 32.2% | 74.3% | conv-48 | 31.4% | 74.9% |
| conv-42 | 31.2% | 71.9% | conv-49 | 33.3% | 74.4% |
| conv-43 | 38.8% | 69.7% | conv-50 | 36.7% | 76.0% |

v2 离散度 67.8%~77.2%，全体显著抬升，无异常会话。

## 4. 关键质量分析

### 4.1 相对时间归一化：根因修复生效

- temporal 从 16.5% → 75.7%；“无信息”从 187/321（58.3%）→ 30/321（9.3%）。
- 4,504 条 v2 fact 中 **3,346 条（74.3%）含显式年份**（ES head 全量统计）；
  相对时间被写成绝对时间。
- 抽查示例：
  - Q: “When did Caroline go to the LGBTQ support group?” Gold: 7 May 2023
    → fact: “attended an LGBTQ support group on **May 7, 2023**” → 答对。
  - Q: “When did Caroline meet up with her friends, family, and mentors?”
    Gold: the week before 9 June 2023
    → fact: “the week of **June 2–8, 2023**” → 答对。

### 4.2 信息抽取密度：2.09×，且几乎消灭“无信息”式失败

- 5,882 轮 → 4,504 条 fact（76.6%），v1 为 2,153（36.6%）。
- 每会话事实数：v1 117~272 → v2 297~548。
- v1 39.2% 题目答“无信息” → v2 7.0%。抽取覆盖面成为整体准确率的最大提升源，
  与 v1 报告“瓶颈不在检索融合，而在上游事实覆盖”的判断一致。

### 4.3 多跳与单跳同步受益

- multi-hop 45.7% → 67.4%，说明混合召回 + rerank 在 fact 入库后继续有效；
- single-hop 36.4% → 76.5%，说明此前主要不是检索问题，而是 fact 缺失/角色丢失问题。

### 4.4 延迟与成本

- 召回 p50 716.7 → 897.9 ms（+25%），p95 974.2 → 1229.0 ms（+26%）；
  与 v1 报告一致，rerank LLM 调用占大头；候选数仍为 limit 50。
- 摄取从 331 批 → 1,283 批，耗时 668 s → 3,707 s（+5.5×），与 chunk 25→5
  和 33KB ADDITIVE prompt 的输入 token 增加一致。这是质量换取的直接成本，
  `--chunk-size 1`（官方同参）预期更贵。

## 5. v1 → v2 完整对比

| 轴 | v1 | v2 |
|---|---|---|
| 代码基线 | `b2becb02` 运行时代码 | `7f3f6fa3` |
| 抽取 prompt | OSS user-only（惩罚 assistant） | ADDITIVE（双说话人 + 穷举清单 + 时间规则） |
| 时间锚点 | content 织入日期，prompt 用“今天” | `timestamp` → Observation Date 归一化 |
| 角色映射 | 双 user 规避 prompt 惩罚 | speaker_a→user / speaker_b→assistant |
| chunk size | 25 | 5 |
| 事实数 | 2,153 | **4,504** |
| 总体 | 33.3% | **72.7%** |
| temporal | 16.5% | **75.7%** |
| “无信息” | 39.2% | **7.0%** |
| 召回 p50/p95 | 716.7 / 974.2 ms | 897.9 / 1229.0 ms |
| 摄取耗时 | 668 s | 3,707 s |

## 6. 口径差异与外部数字

本结果仍不可直接等同官方平台 92.5%：

1. 模型栈：qwen-plus 全链 vs 官方平台更强抽取模型 + OpenAI 系裁判；
2. 官方 runner chunk=1，本运行 chunk=5；
3. 裁判模型与官方不同，二值判分严格度存在系统差；
4. 本报告是纵向对比：同一脚本、同一数据、同一接口，只变抽取管线。

## 7. 局限

- 单次运行；未做多次方差分析。
- 无 evidence（dia_id）链路的检索指标；extract fact 无法回指原文轮次。
- category 5 按官方协议剔除；对抗鲁棒性未评。
- 延迟为串行单客户端测量。

## 8. 复现命令

```bash
cd server && make up
python3 server/scripts/benchmarks/locomo/run.py \
  --email e2e-v3@example.com --password <local-dev-password> \
  --reset --samples 10 \
  --out server/scripts/benchmarks/locomo/results/v2-full-chunk5-extract-rerank.json
```

## 9. 后续消融

| 轴 | 命令 | 验证假设 |
|---|---|---|
| 官方同参 | `--chunk-size 1` | 能否继续逼近官方 temporal/总体 |
| 时间锚点归因 | `--no-inline-session-date` | timestamp 单独能否支撑 temporal 75% |
| 重排收益 | `--no-rerank` | 900ms→~300ms 换多少准确率 |
| 召回通道 | `--recall-mode semantic/keyword` | RRF 混合增益 |
| 原文摄取 | `--ingest append` | 知识加工 vs 原文存储 |
