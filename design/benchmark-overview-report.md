# Mem0 自托管 Memory Benchmark 整合报告（对外展示版）

> 版本：v1.1 · 整理日期：2026-08-17
> 整合范围：
> - [`design/locomo-benchmark-v2-report.md`](locomo-benchmark-v2-report.md)（LOCOMO v2 主运行）
> - [`design/locomo-benchmark-v2-ablation-report.md`](locomo-benchmark-v2-ablation-report.md)（LOCOMO v2 消融矩阵）
> - [`design/cn-mempal-beam-benchmark-report.md`](cn-mempal-beam-benchmark-report.md)（中文 Mem-PAL 与 BEAM v1）
>
> 报告定位：把三份 benchmark 报告合并为一份可对外展示的整体报告，采用“总览 → 环境与模型 → 方案 → 数据口径 → 跑分 → p50/p95 延迟 → 归因与建议 → 外部项目/论文对比”的自顶向下结构。第 1–9 章数字均来自上述三份原始报告，未重新测量；第 10 章为 MCP 检索的外部生态数据。

---

## 1. 一页总览（TL;DR）

### 1.1 结论先行

1. **抽取质量是当前最大的性能杠杆。** LOCOMO 只改知识加工侧（ADDITIVE prompt + 时间锚点 + 角色映射 + chunk=5），总体准确率从 33.3% 提升到 **72.7%**，temporal 从 16.5% 提升到 **75.7%**，“无信息”式失败从 39.2% 降到 7.0%。
2. **在 LOCOMO 消融中，`semantic` 召回 + rerank 是当前最优配置**：总体 **73.8%**，同时 p50 延迟比默认 `auto` 更低（730.5ms vs 897.9ms）。rerank 值 **+2.1pp**，代价约 +520ms p50；chunk=1 无收益，不建议采用。
3. **中文链路在真实 benchmark 上生效。** cn-Mem-PAL 隐式需求改写（Requirement Restatement）**84.35/100**；中文相对时间、星期自洽、`Asia/Shanghai` 时区在 100 用户全量上稳定工作。
4. **当前主要短板明确。** cn-Mem-PAL 方案推荐（Solution Selection）仅 **54.78/100**；BEAM 总体 **58.0%**，summarization 27.5%、event_ordering 25.0% 是明显弱项。对应改进方向为“事实层之上的原则/归纳层”与长程事件结构理解。
5. **生态定位（MCP 检索，截至 2026-08-17）。** 本报告 qwen 自托管 LOCOMO 72.7% 介于 Mem0 论文旧算法与 Mem0 托管最新算法 92.5% 之间；BEAM 100K 58.0% 高于论文 RAG/LIGHT 基线（32–36%），低于 Hindsight（73.4–86.2%）与 Exabase M-1（76.9%）等头部系统。详细项目/论文对比见 §10。

### 1.2 总跑分卡

| Benchmark / 运行 | 数据集与题目口径 | 核心分数 | 召回 p50 / p95 | 一句话结论 |
|---|---:|---:|---:|---|
| LOCOMO v2（主运行） | 10 会话，1,540 题 | **72.7%**（1120/1540） | 897.9 / 1229.0 ms | 抽取升级使总体较 v1 **+39.4pp** |
| LOCOMO v2 消融最优（semantic + rerank） | 同一 1,540 题 | **73.8%** | 730.5 / 907.5 ms | 质量与延迟同时优于 auto 基线 |
| cn-Mem-PAL v1 | 中文 PAL-Set 100 用户，826 topics | 需求改写 **84.35/100**；方案推荐 **54.78/100** | 1353.7 / 1940.0 ms（requirement query） | 中文时间链路与隐式需求理解强，方案归纳弱 |
| BEAM v1（100K 全量） | 20 conversations，400 题，1051 nuggets | **58.0%** pass；avg score **0.5329** | 1265.6 / 1994.4 ms | preference/instruction/information_extraction 均 ≥82.5% |

> 注意：四行分数分别来自不同数据集、不同指标、不同语言，**横向不能直接比大小**；可以比较的是同一 benchmark 内部的 v1→v2 或消融差异。详见 §11。

---

## 2. 实验环境与模型选择

### 2.1 统一实验底座

| 维度 | 口径 |
|---|---|
| 被测对象 | 自托管 Mem0 server（`docker compose` 运行），全部走 `/v1/memory/*` HTTP API |
| 代码基线 | 均为 `feature/memory-research` 分支；LOCOMO v2 @ `7f3f6fa3`，LOCOMO 消融 @ `854e4be3`，cn-Mem-PAL/BEAM @ `254fb50d` + 中文时间加工 `a50e2f13` + v1 runner `58395b90` |
| 运行日期 | LOCOMO v2 与消融：2026-08-16；cn-Mem-PAL/BEAM 见对应原始报告 commit 记录 |
| 健康检查 / 准备 | LOCOMO：preflight `/health/ready` → `/v1/capabilities` → `POST /reset`；cn-Mem-PAL/BEAM：重启服务后 `/health/ready`，并经 `/configure` 将 LLM `max_tokens` 调至 8000 |
| 存储模式 | v3 `ONLY_VDB`；Elasticsearch 8.17 为单一权威存储 |
| 索引 | `agentar_mem0` 五族索引：`scope / head / version / event / dedup`，写入后立即 refresh；中文使用 `text_zh`（ik_max_word / ik_smart），LOCOMO 使用 analysis-ik |
| 数据库 | MySQL 仅存应用表，不承载记忆数据 |
| 召回接口 | `POST /v1/memory/recall`，默认 `mode=auto`（semantic kNN + BM25 RRF）+ `rerank=true` + `top_k/limit=50`（`/v1` limit 上限） |
| 隔离 | cn-Mem-PAL `tenant_id=bench-mempal-v1`；BEAM `tenant_id=bench-beam-v1`；LOCOMO 每会话独立 `user_id`。召回在相同 tenant/user scope 内回放 |
| 关键配置修复 | LLM `max_tokens` 由 2000 调至 8000（`POST /configure`），否则长 JSON 抽取会被静默截断（见 §8 第 6 条） |

### 2.2 模型选择

| 角色 | 统一选择 | 例外 / 消融 | 说明 |
|---|---|---|---|
| 抽取 LLM | `qwen-plus-latest` | 消融 v2-d：`deepseek-v4-flash-0731`（`enable_thinking=false`） | DashScope OpenAI-compatible；cn-Mem-PAL/BEAM 抽取 temperature=0.2 |
| 答案生成 | `qwen-plus-latest` | 无 | LOCOMO temperature=0；cn-Mem-PAL/BEAM temperature=0.2；benchmark runner 侧调用 |
| 裁判 | `qwen-plus-latest` | 无 | 温度同上；保证各运行内部裁判口径一致 |
| 向量嵌入 | `qwen3.7-text-embedding`（1024 维） | 无 | ES dense_vector，cosine |
| 重排 | `qwen3-vl-rerank` | 消融 v2-b 关闭 rerank | recall `rerank=true` 时生效 |

**模型选择逻辑（由三份报告口径归纳）**：全链路统一走 DashScope，中英文共用同一套 `qwen-plus-latest` 抽取/答题/裁判；向量与重排选择 qwen 同族模型；消融同时验证 `deepseek-v4-flash-0731` 作为低成本抽取替代。cn-Mem-PAL/BEAM 全量前先做 few-shot smoke 与 228 项中文时间/时区回归测试，模型与配置生效后再跑全量。

---

## 3. 总体方案与链路

### 3.1 共同链路

```text
数据集 → preflight → remember（抽取加工 + 时间锚定 + 五索引落盘）
      → recall（hybrid/RRF + rerank + top-50）
      → answer（benchmark runner 调用 LLM）
      → judge（官方规则或 LLM 裁判）
```

所有 benchmark 都统计摄入、召回、答题、裁判四段，并记录每段错误数。LOCOMO v2、cn-Mem-PAL、BEAM 三个主运行的 **remember/answer/judge 错误数均为 0**（消融运行复用同一评测管线）；cn-Mem-PAL/BEAM 全量期间另有 3 次 JWT 过期 401，由 runner 透明 re-login 恢复，不计失败（LOCOMO 系列无此记录）。

### 3.2 各 benchmark 方案差异

| 方案点 | LOCOMO v2 | cn-Mem-PAL v1 | BEAM v1 |
|---|---|---|---|
| 抽取 prompt | `ADDITIVE_EXTRACTION_PROMPT`：双说话人、exhaustive checklist、relative-date 归一化 | qwen-plus-latest 中文抽取 + 中文时间加工 | qwen-plus-latest 抽取（原报告未单列 prompt 名称） |
| 时间锚点 | 每批 `timestamp="YYYY-MM-DD"`，同时把日期织入 content（可消融） | 日志行内相对时间锚到行内时间戳；绝对日期带星期且自洽 | batch `time_anchor` 作为 chunk timestamp |
| 角色 / 隔离 | 官方口径：`speaker_a→user`、`speaker_b→assistant` | `tenant_id=bench-mempal-v1`，user_id 按用户/会话唯一 | `tenant_id=bench-beam-v1`，user_id 按 conversation 隔离 |
| batch/chunk | chunk=5（v1 为 25；官方 runner 为 1） | 每 sample 一次 remember | 5 turns/chunk |
| 召回 | auto + rerank，top-50 | auto + rerank，top-50 | auto + rerank，top-50 |
| 答题/裁判 | qwen-plus-latest，官方容忍规则 | 官方 GPT-Score / 确定性 selection 打分 | 官方 rubric-nugget 裁判，pass≥0.5 |

### 3.3 LOCOMO v2 消融矩阵（控制变量）

| 运行 | 唯一变化 | 对照基线 |
|---|---|---|
| v2 基线 | — | qwen 抽取 + chunk5 + auto + rerank |
| v2-a-semantic | `recall_mode=semantic` | 复用 v2 同一份 4,504 条 fact 语料，摄入侧零差异 |
| v2-a-keyword | `recall_mode=keyword` | 同上 |
| v2-b-norerank | 去掉 rerank（纯 RRF） | 同上 |
| v2-c-chunk1 | `chunk=1` | 独立语料，其余 prompt/角色/时间戳/召回全部相同 |
| v2-d-dsv4 | 抽取模型 `deepseek-v4-flash-0731`（`enable_thinking=false`） | 独立语料，其余全部相同 |

---

## 4. 数据口径

| 项 | LOCOMO v2 / 消融 | cn-Mem-PAL v1 | BEAM v1 |
|---|---|---|---|
| 数据集 | `locomo10.json`（2.8 MB，已下载校验） | PAL-Set 中文原版 `input.json`（约 70 MB，完整 100 用户） | BEAM 四个 split 全部下载并校验 SHA-256（约 423 MB） |
| 规模 | 10 会话 / 272 非空 session / 5,882 轮 / 1,986 题 | 100 用户 / 2,890 samples / 826 topics | 四 split 20/35/35/10 conversations；**v1 执行 100K 全量**：20 conversations / 1,181 chunks / 400 题 / 1,051 nuggets |
| 计分题目 | category 1-4 共 **1,540 题**；category 5 按官方协议剔除 | 826 topics，requirement 与 solution 各 826 题 | 400 题全部计分 |
| 评分口径 | 官方容忍规则二值判分（accuracy） | Requirement：官方 GPT-Score 0-2 分 ×50，满分 100；Solution：官方确定性打分，pos+1/neg-1，每题 [-2,2] ×50 | 官方 rubric-nugget 裁判，单题 pass 阈值 ≥0.5；accuracy 与 avg_score 双指标 |
| 题目类别 | multi-hop 282 / temporal 321 / open-domain 96 / single-hop 841 / cat5 446 | 同一 826 topics 双任务 | 10 类 question_type |
| “无信息”判定 | 拒绝式措辞（`not specified / not mentioned / no record / no information / unknown / cannot be determined / no relevant` 等） | 不适用 | 不适用 |
| 完整性 | 类别分布与官方一致 | 中文原版全量 | SHA-256 已记录；100K 全量执行，其余 split 缓存待 v2 |

---

## 5. 跑分对比

### 5.1 LOCOMO v2 主运行：抽取升级带来 +39.4pp

| 指标 | v1 基线 | v2 | 变化 |
|---|---:|---:|---:|
| 总体准确率（1,540 题） | 33.3%（513） | **72.7%**（1120） | **+39.4pp** |
| multi-hop（282） | 45.7% | **67.4%** | +21.7pp |
| temporal（321） | 16.5% | **75.7%** | **+59.2pp** |
| open-domain（96） | 26.0% | 45.8% | +19.8pp |
| single-hop（841） | 36.4% | **76.5%** | +40.1pp |
| 摄取事实数（5,882 轮） | 2,153（36.6%） | **4,504（76.6%）** | 2.09× |
| “无信息”答案占比 | 39.2% | **7.0%** | -32.2pp |
| 召回 p50 / p95 | 716.7 / 974.2 ms | 897.9 / 1229.0 ms | 延迟上升（rerank 占大头） |

- 每会话 v2 准确率 67.8%–77.2%，无异常会话；全部显著高于 v1（27.3%–38.8%）。
- v2 的 4,504 条 fact 中 **3,346 条（74.3%）含显式年份**，相对时间被归一化为绝对时间，temporal “无信息”从 187/321 降至 30/321。
- 端到端耗时约 97 分钟（11:33:41 → 13:10:51），摄入 3,707s（0 错误）、召回 1,441s、答题 447s（0 错误）、裁判 232s（0 错误）。

### 5.2 LOCOMO v2 消融矩阵：semantic + rerank 最优

**总体与分 category 准确率**

| category | v2 基线 | semantic | keyword | no-rerank | chunk1 | dsv4 |
|---|---:|---:|---:|---:|---:|---:|
| multi-hop | 67.4% | 68.8% | 61.7% | 60.3% | **68.8%** | **70.6%** |
| temporal | 75.7% | **78.2%** | 75.7% | 77.9% | 75.1% | 75.1% |
| open-domain | 45.8% | 46.9% | 42.7% | 43.8% | 38.5% | **47.9%** |
| single-hop | 76.5% | **76.8%** | 74.9% | 74.4% | 72.1% | 72.3% |
| **总体** | 72.7% | **73.8%** | 70.7% | 70.7% | 70.0% | 71.0% |

**“无信息”答案（合计 / 占比）**

| 运行 | multi-hop | temporal | open-domain | single-hop | 合计 |
|---|---:|---:|---:|---:|---:|
| v2 基线 | 6 | 30 | 32 | 40 | 108（7.0%） |
| semantic | 6 | **25** | 30 | 42 | **103（6.7%）** |
| keyword | 14 | 30 | 31 | 44 | 119（7.7%） |
| no-rerank | 21 | 27 | 35 | 55 | 138（9.0%） |
| chunk1 | 12 | 27 | 35 | 66 | 140（9.1%） |
| dsv4 | 8 | 34 | 29 | 74 | 145（9.4%） |

**消融结论**

- `recall_mode`：semantic（73.8%）> auto/RRF（72.7%）> keyword（70.7%）；semantic 同时更快（少走一条 keyword 通道）。
- rerank：+2.1pp（72.7% vs 70.7%），质量优先时应保留。
- chunk=1：事实数几乎不变（4,501 vs 4,504），总分为 -2.7pp，批次成本为 4.6×，不采用。
- 抽取模型：dsv4 总体 71.0%（-1.7pp），但事实数仅 qwen 的 61%，multi-hop（70.6%）与 open-domain（47.9%）反而更好，是成本敏感场景的候选。

### 5.3 cn-Mem-PAL v1：中文隐式需求强，方案归纳弱

**Requirement Restatement（官方 GPT-Score，0-2 分 ×50）**

| 指标 | 值 |
|---|---:|
| topics | 826 |
| **score_100** | **84.35** |
| mean raw score | 1.687 |
| 满分率（score=2） | **67.07%** |
| ≥1 分率 | 94.31% |
| 分数分布 | 2.0×554、1.5×102、1.0×123、0.5×19、0.0×28 |
| answer / judge errors | 0 / 0 |
| per-user score | min 59.38 / p25 79.17 / **p50 85.71** / p75 91.67 / max 100 |

**Solution Selection（官方确定性打分，pos+1 / neg-1，每题 [-2,2] ×50）**

| 指标 | 值 |
|---|---:|
| topics | 826 |
| **score_100** | **54.78** |
| mean raw score | 1.096 |
| 精确命中 2 个 pos | **33.05%** |
| 至少命中 1 个 pos | 89.83% |
| answer errors | 0 |
| per-user score | min 22.22 / p25 44.44 / **p50 54.17** / p75 66.67 / max 91.67 |

**摄入口径**：2,890 次 remember 全部 200；抽取 118,363 条 fact（created 118,362 + noop 1，0 错误）；单 sample 事实数 mean 41.0 / median 31 / p90 72 / max 107；修复 `max_tokens` 后 0-fact sample 为 0。

### 5.4 BEAM v1：100K 全量，强项明确、长程结构弱

| question_type | accuracy | avg_score |
|---|---:|---:|
| **overall** | **58.0%** | **0.5329** |
| preference_following | **97.5%** | 0.8750 |
| instruction_following | **87.5%** | 0.7875 |
| information_extraction | **82.5%** | 0.7625 |
| abstention | 57.5% | 0.5750 |
| multi_session_reasoning | 52.5% | 0.4415 |
| temporal_reasoning | 52.5% | 0.3688 |
| contradiction_resolution | 50.0% | 0.4562 |
| knowledge_update | 47.5% | 0.4750 |
| summarization | 27.5% | 0.3516 |
| event_ordering | 25.0% | 0.2359 |

**摄入口径**：1,181 chunks（5 turns/chunk），20/20 conversations 成功；7,813 条 fact（created 7,799 + noop 14，0 错误）。conversation accuracy 介于 0.35（conv 3/7）至 0.75（conv 15/18），大多数在 0.45–0.70。

---

## 6. p50 / p95 延迟对比（召回阶段）

> 口径：单请求召回耗时，含 hybrid/RRF 与 rerank（如开启）。LOCOMO 系列报告明确标注为串行单客户端；cn-Mem-PAL/BEAM 记录为 recall 请求延迟，原始报告未单列并发口径。这是延迟基准，不是并发吞吐结论。

| 运行 / 变体 | p50 | p95 | p99 | max | avg results |
|---|---:|---:|---:|---:|---:|
| LOCOMO v2 基线（auto + rerank） | 897.9 ms | 1229.0 ms | — | — | 50.0 |
| LOCOMO v2-a semantic + rerank | **730.5 ms** | **907.5 ms** | — | — | 50.0 |
| LOCOMO v2-a keyword + rerank | 522.1 ms | 704.0 ms | — | — | 49.9 |
| LOCOMO v2-b no-rerank（纯 RRF） | **377.6 ms** | **526.2 ms** | — | — | 50.0 |
| LOCOMO v2-c chunk1（auto + rerank） | 972.3 ms | 1228.9 ms | — | — | 50.0 |
| LOCOMO v2-d dsv4（auto + rerank） | 939.7 ms | 1337.6 ms | — | — | 50.0 |
| cn-Mem-PAL requirement query | 1353.7 ms | 1940.0 ms | 2765.8 ms | 8498.8 ms | top-50 满返 |
| cn-Mem-PAL solution query | 1352.2 ms | 1897.9 ms | — | — | top-50 满返 |
| BEAM v1 100K | 1265.6 ms | 1994.4 ms | 2159.9 ms | 2369.3 ms | 50.0 |

**延迟侧结论**

- 在可对比的 LOCOMO 同语料消融中，rerank 是最大单项延迟成本：开 rerank p50 897.9ms，关闭后 **377.6ms**（-520ms），p95 由 1229.0ms 降至 526.2ms（-703ms）。
- semantic 召回比 auto 更快且分数更高：p50 897.9→730.5ms（-167ms），p95 1229.0→907.5ms（-322ms）。
- cn-Mem-PAL/BEAM 的 p95 在 1.9–2.0s 区间，p50 在 1.27–1.35s 区间；BEAM 尾延迟最平（p99 仅 2159.9ms），cn-Mem-PAL 存在 8.5s 级长尾。
- 报告记录的各变体候选返回为 49.9–50.0 条；cn-Mem-PAL/BEAM 明确记录 `hybrid`、`rerank applied`、`degraded=false`。

**摄入与端到端耗时（参考）**

| 运行 | 批次/请求数 | 事实数 | 摄取耗时 | 并发 | 端到端 |
|---|---|---:|---:|---:|---|
| LOCOMO v2 | 1,283 | 4,504 | 3,707 s | 2 | ~97 min |
| LOCOMO chunk1 | 5,882 | 4,501 | 3,196 s | 4 | — |
| LOCOMO dsv4 | 1,283 | 2,748 | 1,018 s | 4 | — |
| cn-Mem-PAL | 2,890 | 118,363 | 11,625 s | 20 | — |
| BEAM 100K | 1,181 | 7,813 | 4,163.9 s | 4 | — |

> 摄入耗时跨运行并发不一致（2/4/20），只作量级参考，不作严格成本对比。

---

## 7. 稳定性与可靠性

| 检查项 | 结果 |
|---|---|
| 中文时间/时区回归测试（cn-Mem-PAL/BEAM） | **228 passed**（`tests/utils/test_temporal.py` 等 6 个测试文件） |
| 隔离 smoke（cn-Mem-PAL/BEAM） | **6/6 PASS**（tenant 内 user/agent 隔离、跨 tenant 同 user_id 不串、user scope 不读 agent scope） |
| LOCOMO v2 | remember/answer/judge 0 错误；1,540 题召回全部完成，avg results 50.0 |
| LOCOMO 消融 | 6 个运行共用同一评测管线；a/b 复用同一份 4,504 fact 语料，摄入侧零差异 |
| cn-Mem-PAL | 2,890 remember 全 200、118,363 facts、0 errors；826 题全判，judge/answer 0 errors |
| BEAM | 1,181 chunks 全成功、7,813 facts、0 errors；400/400 召回 200，hybrid + rerank applied + degraded=false |
| cn-Mem-PAL/BEAM 日志 | remember/recall 全部 200（除 3 次 JWT 401，runner 自动 re-login 恢复）；无 422/429/5xx、无 Traceback、无 ES 4xx/5xx |

---

## 8. 关键发现与归因

1. **知识加工是主要瓶颈，而不是召回融合。** LOCOMO v1→v2 未动召回/答题/裁判，只改抽取侧，总体 +39.4pp、temporal +59.2pp、“无信息” -32.2pp，直接验证了该判断。
2. **相对时间归一化是 temporal 能力的前提。** v2 将相对日期写成绝对日期（74.3% fact 含显式年份），中文链路将“今天/昨天/上周”锚定到行内时间戳并保证星期自洽，temporal 均成为增长最大的类别。
3. **召回通道排序为 semantic > auto(RRF) > keyword。** semantic 在 LOCOMO 上 73.8%，比 auto +1.0pp，且更省延迟；keyword 的 multi-hop 掉到 61.7%，纯关键词不足以组合多条事实。
4. **rerank 值得保留，但可按场景取舍。** 质量优先开 rerank（+2.1pp，p50 约 900ms）；延迟优先关 rerank（p50 378ms）。
5. **chunk=1 没有质量收益。** ADDITIVE prompt 在 chunk5 已能穷举 batch 内 topic；chunk1 只是把事实拆碎，总体 -2.7pp，批次成本 4.6×。
6. **`max_tokens=2000` 曾造成 critical 静默丢数。** 中文长 JSON 抽取在 2000 token 处被截断，首批 30.7% sample 返回 0 facts 但 HTTP 200；调至 8000 后 0-fact 率归零。对外部署时应作为默认配置红线。
7. **各 benchmark 的强弱项高度一致。** 显式偏好、指令、信息抽取强（BEAM 三个类别 ≥82.5%）；归纳式方案推荐、summarization、event_ordering 弱，提示下一阶段需要“事实之上的原则/结构层”，而不仅是更多事实。

---

## 9. 推荐配置（基于本次测量）

| 目标 | 推荐配置 | 已测 LOCOMO 分数 | 已测 p50 | 备注 |
|---|---:|---:|---:|---|
| **质量优先** | qwen-plus-latest 抽取 + ADDITIVE prompt + 时间锚点 + chunk5 + `recall_mode=semantic` + rerank + top50 + `max_tokens=8000` | **73.8%** | 730.5 ms | 当前最佳综合选择 |
| **延迟优先** | 同上抽取，`recall_mode=auto` + 关闭 rerank | 70.7% | **377.6 ms** | 以 -3.1pp 换约 -520ms p50 |
| **成本优先（候选）** | `deepseek-v4-flash-0731`（`enable_thinking=false`）+ chunk5 + semantic + rerank | 待组合验证 | 待测 | 已测 dsv4 + auto + rerank = 71.0%；摄取成本趋势显著更低 |
| **不推荐** | chunk=1 | 70.0% | 972.3 ms | 质量无收益、批次成本 4.6× |

---

## 10. 与主流开源项目及论文的对比（MCP 检索 · 数据截至 2026-08-17）

> 本节数据通过 MCP web 检索工具（Exa / Tavily）于 2026-08-17 获取，来源为项目官网、论文、GitHub 与第三方评测。记忆领域的公开跑分普遍存在“供应商自报”与“独立复现”的显著差异，因此下表显式区分**自报**与**独立/第三方**结果，并在 §10.8 给出读取规则。

### 10.1 对比范围与项目定位

对比对象覆盖四类主流方案：**事实抽取式记忆层**（Mem0、LangMem、PropMem）、**Agent 运行时内建记忆**（Letta/MemGPT）、**时序知识图谱**（Zep/Graphiti、HippoRAG 等）、**结构化/多策略记忆**（Hindsight、A-MEM、Mem-PAL H2Memory、MemPalace、Exabase M-1）。

| 项目 / 论文 | 类型 | 许可证 | 核心架构 | 最新公开结果（截至检索日） |
|---|---|---|---|---|
| **Mem0（本报告，自托管 qwen）** | 记忆层 | Apache-2.0 | `ONLY_VDB` + ES 五索引，qwen 抽取，hybrid/RRF + rerank，top-50 | LOCOMO **72.7%**；BEAM 100K **58.0%**；Mem-PAL req **84.35** / solution **54.78**（本报告实测） |
| **Mem0 托管平台（2026-04 新算法）** | 记忆层 + 托管服务 | OSS core + 托管 | 单遍 ADD-only 抽取 + entity linking + 多信号检索 | LoCoMo **92.5**、LongMemEval **94.4**、BEAM 1M **64.1** / 10M **48.6**；自报 p50 0.88–1.09s [S1][S2] |
| **Letta（原 MemGPT）** | Agent 运行时 + 自编辑记忆 | Apache-2.0 | Memory blocks + archival memory，Agent 自己读写记忆（源自 MemGPT 论文 [S19]） | 检索到的资料无标准 LOCOMO 当前分数；第三方 60 题客服机器人实测 recall 86%、+520ms/turn [S3][S14] |
| **Zep / Graphiti** | 时序知识图谱 | Apache-2.0（Graphiti） | 双时序实体-关系图，旧事实失效而非删除 | 论文自报 DMR 94.8 vs MemGPT 93.4 [S4]；LOCOMO 存在 84 / 58.44 / 75.14 三方争议 [S5][S14]；第三方 60 题客服实测 recall 91%、+310ms/turn [S3] |
| **LangMem** | LangGraph 记忆工具库 | LangChain 开源 | 开发者自定义 memory stores + 工作流 | Mem0 论文复现 LOCOMO J=58.10，但 search p50/p95 达 17.99/59.82s [S7]；客服机器人实测 79% [S3] |
| **Hindsight** | 结构化多策略记忆 | MIT（第三方资料） | 四网络 + semantic/BM25/graph/temporal 并行检索 + rerank | AMB：LOCOMO 92.0、LongMemEval 94.6；BEAM 100K 86.2%（RAG mode）/ 73.4%（single-query）、10M 64.1 [S8][S9] |
| **PropMem（Prosus MemEval）** | 实体过滤命题记忆 | Apache-2.0 | 原子事实按实体打标，query 时实体过滤 + CoT | 独立统一评测 MemEval：LOCOMO F1 **0.605** / Judge **0.823**，为 9 系统第一 [S6] |
| **HippoRAG（论文）** | 图式 RAG | 论文方法 | 实体图 + 个性化 PageRank 图检索 | 检索资料无同口径 LOCOMO 主分数；2026-05 规模审计显示 LongMemEval 注入无关会话后 Pass@2 下降 16–20pp [S17] |
| **A-MEM（论文）** | Agentic 联想记忆 | 论文方法 | 互联想 notes，语义检索 + LLM 动态连边 | Mem0 论文复现 LOCOMO J=48.38，total p50/p95 1.410/4.374s [S7] |
| **Mem-PAL H2Memory（论文）** | 分层异构记忆（论文） | 论文方法 | Log Graph + Background + Topic Outline + Principle + RAG | PAL-Bench：req BLEU-1 **26.67** / G-Score **32.54**；solution S-Score **38.32** [S11] |
| **MemPalace** | 原文全量记忆 | OSS | 对话原文全部入 ChromaDB，palace 结构 | 自报 LongMemEval **96.6%**（raw mode）；约 43.5k stars（2026-08） [S12] |
| **Exabase M-1** | 数据基础设施/记忆引擎 | 商用 | 未公开细节 | 供应商 PR：BEAM 100K **76.9** / 1M **75.0** / 10M **68.0**，LongMemEval **96.4** [S13] |

### 10.2 LOCOMO 同基准对照

| 系统 | 分数 | 口径 / harness | 来源性质 |
|---|---:|---|---|
| Mem0 托管平台（新算法） | **92.5%** | 官方 harness，top-200，~6,956 tokens/query | 供应商自报 [S1][S2] |
| Hindsight（local） | **92.0%** | Agent Memory Benchmark（AMB） | 供应商自报 [S8] |
| **本报告 Mem0 自托管 qwen** | **72.7%** | 官方 1,540 题二值判分，qwen 全链，top-50 | 本报告实测 |
| Zep（LOCOMO 争议值） | 84% / 58.44% / 75.14% | Zep 原报 → Mem0 复现 → Zep 反驳 | 供应商互相复现 [S5][S14] |
| Full-context（Mem0 论文） | J=72.90 | LLM-as-judge，10 次平均 | 论文 [S7] |
| Mem0g（Mem0 论文） | J=68.44 | 同上 | 论文 [S7] |
| Mem0（Mem0 论文） | J=66.88 | 同上 | 论文 [S7] |
| Zep（Mem0 论文） | J=65.99 | 同上 | 论文 [S7] |
| LangMem（Mem0 论文） | J=58.10 | 同上 | 论文 [S7] |
| OpenAI Memory（Mem0 论文） | J=52.90 | 同上 | 论文 [S7] |
| A-MEM（Mem0 论文） | J=48.38 | 同上 | 论文 [S7] |
| PropMem（MemEval） | F1 **0.605** / J **0.823** | 统一 LLM/embedding/judge 的独立评测 | 独立评测 [S6] |
| OpenClaw（MemEval） | F1 0.557 / J 0.725 | 同上 | 独立评测 [S6] |
| Full-context（MemEval） | F1 0.542 / J 0.709 | 同上 | 独立评测 [S6] |
| Hindsight（MemEval） | F1 0.489 / J 0.676 | 同上 | 独立评测 [S6] |
| Graphiti（MemEval） | F1 0.416 / J 0.573 | 同上 | 独立评测 [S6] |
| Mem0（MemEval） | F1 0.344 / J 0.497 | 同上；评测方注明 Mem0 temporal F1=0.104 疑受 timestamp issue `mem0ai/mem0#3944` 影响 | 独立评测 [S6] |

### 10.3 BEAM 同基准对照

| 系统 | 100K | 500K | 1M | 10M | 口径 |
|---|---:|---:|---:|---:|---|
| Exabase M-1 | **76.9%** | — | **75.0%** | **68.0%** | 供应商 PR，Gemini 3 Flash [S13] |
| Hindsight（AMB，RAG mode） | **86.2%** | **80.1%** | **79.1%** | — | AMB 公开榜单，context 23.7–31.7K tokens；是否与 Hindsight 同源未核实 [S8][S15] |
| Hindsight（AMB，single-query） | 73.4% | 71.1% | 73.9% | 64.1% | AMB 公开榜单，context 17.7–27.3K tokens；是否与 Hindsight 同源未核实 [S8][S15] |
| Honcho（未验证转载） | 63.0% | 64.9% | 63.1% | 40.6% | 来自 Honcho evals，AMB 标注“未验证” [S8][S9] |
| Mem0 托管平台 | — | — | 64.1% | 48.6% | 官方，gpt-5 reader，~6.7–6.9K tokens/query [S1][S2] |
| **本报告 Mem0 自托管 qwen** | **58.0%** | — | — | — | 100K 全量 400 题，qwen 全链，top-50 |
| LIGHT（BEAM 论文基线） | 35.8% | 35.9% | 33.6% | 26.6% | 论文基线 [S9][S18] |
| RAG（BEAM 论文基线） | 32.3% | 33.0% | 30.7% | 24.9% | 论文基线 [S9][S18] |

> BEAM 各家的 reader/judge/top-k/摄入方式差异极大。例如 Mnemoverse 明确提示其 10M 61% 使用 claude-sonnet reader 且上下文 ~13 万 tokens，与 Mem0 官方 ~6.9K tokens 的跑法不可直接排名 [S16]。本报告的 58.0% 是 qwen 自托管、top-50 单 cutoff，应作为同协议自托管基线读取。

### 10.4 LongMemEval 外部对照（本报告未跑主运行，仅作生态背景）

| 系统 | 公开分数 | 口径 | 来源性质 |
|---|---:|---|---|
| MemPalace | **96.6%** | raw mode，原文全量检索 | 供应商自报 [S12] |
| Exabase M-1 | **96.4%** | 供应商发布 | 供应商 PR [S13] |
| Hindsight | 94.6%（AMB） / 91.4%（另文） | LongMemEval-S | 供应商自报 [S8][S10] |
| Mem0 托管平台 | **94.4%** | 官方 harness，~6,787 tokens/query | 供应商自报 [S1][S2] |
| PropMem（MemEval） | F1 0.550 / J 0.716 | 102 题分层样本，统一协议 | 独立评测 [S6] |
| SimpleMem（MemEval） | F1 0.480 / J 0.667 | 同上 | 独立评测 [S6] |
| Mem0（Vectorize 独立复现） | 49.0% | 独立复现 | 独立评测 [S14][S10] |

> LongMemEval 是“供应商自报 vs 独立复现”差异最大的 benchmark：Mem0 自报 94.4，第三方复现 49.0（差 45.4pp）。这说明 harness、检索配置、裁判模型的系统差远大于直觉，任何跨项目比较都必须附带协议说明 [S14]。

### 10.5 cn-Mem-PAL 论文对照

| 系统 | Requirement Restatement | Solution Selection |
|---|---|---|
| Vanilla（w/o log） | BLEU-1 13.59 / G-Score 17.50 | BLEU-1 18.85 / S-Score 18.95 |
| MemoryBank | BLEU-1 23.89 / G-Score 28.57 | BLEU-1 20.49 / S-Score 29.85 |
| **H2Memory（Mem-PAL）** | BLEU-1 **26.67** / G-Score **32.54** | BLEU-1 **22.24** / S-Score **38.32** |
| **本报告：Mem0 自托管 qwen** | GPT-Score **84.35/100** | Selection Score **54.78/100** |

> 论文 G-Score/S-Score 与本报告的 GPT-Score/Selection Score 名称相近，但执行 harness、裁判模型与 top-k 未逐项对齐；只应读作“方向上更强”，不应作为同协议头部对比。本报告此前只引用了论文 BLEU 口径（13.59→26.67）并说明不可直接比大小，此处补齐论文完整 G/S 分数 [S11]。

### 10.6 p50 / p95 延迟外部对照

| 系统 / 口径 | p50 | p95 | 说明 |
|---|---:|---:|---|
| Mem0 论文（search-only，GPT-4o-mini 栈） | **0.148s** | **0.200s** | LOCOMO 检索阶段 [S7] |
| Zep 商用平台（第三方资料） | — | **<0.2s** | 检索 p95 供应商口径 [S10] |
| Mem0 论文（total：检索 + 答题） | 0.708s | 1.440s | LOCOMO 端到端 [S7] |
| **本报告 Mem0 自托管 qwen（LOCOMO recall）** | **0.898s** | **1.229s** | 含 hybrid/RRF + qwen rerank，串行单客户端 |
| Mem0 托管平台（LOCOMO p50） | 0.88s | 未报告 | 新算法单遍检索 [S2] |
| Mem0g（Mem0 论文 total） | 1.091s | 2.590s | [S7] |
| Zep（Mem0 论文 total） | 1.292s | 2.926s | [S7] |
| **本报告 BEAM 100K recall** | **1.266s** | **1.994s** | qwen 全链，top-50 |
| **本报告 cn-Mem-PAL requirement recall** | **1.354s** | **1.940s** | 含 qwen rerank |
| A-MEM（Mem0 论文 total） | 1.410s | 4.374s | [S7] |
| Full-context（Mem0 论文 total） | 9.870s | 17.117s | 全文灌入上下文 [S7] |
| LangMem（Mem0 论文 total） | 18.53s | 60.40s | [S7] |
| 第三方 60 题客服机器人（added latency/turn） | Mem0 ~120ms；Zep ~310ms；Letta ~520ms | — | 非标准 benchmark，仅方向参考 [S3] |

### 10.7 与本报告的直接关系

1. **LOCOMO 位置**：本报告 72.7%（二值准确率）与 Mem0 论文旧算法 J=66.88（LLM-as-judge，10 次平均）不是同一指标，不能直接做差；两者都低于 Mem0 托管最新算法的 92.5%。在 MemEval 独立评测中，Mem0 OSS 为 J=0.497/F1=0.344，主要拖累项是 temporal F1=0.104，而本报告通过时间锚点修复把 temporal 提升到 75.7%，改进方向吻合。
2. **BEAM 位置**：本报告 58.0%（100K）处于论文 RAG/LIGHT 基线（32–36%）与头部系统 100K 分数（Hindsight 73.4–86.2%、Exabase 76.9%）之间；Mem0 托管平台在更难的 1M/10M 分别为 64.1%/48.6%。扩到 1M/10M、提高 top-k、更换 reader/judge 是本报告与托管平台的明确差距来源。
3. **Mem-PAL 位置**：本报告需求改写 84.35/方案推荐 54.78 高于论文 H2Memory 的 32.54/38.32（口径未逐项对齐），但方案推荐相对弱于需求改写这一结论与论文“抽象偏好比近期需求更难建模”一致；论文 principle/topic-outline 记忆也为本报告 §8 的改进方向提供了外部证据。
4. **延迟位置**：本报告 recall p50 0.898–1.354s、p95 1.229–1.994s，接近 Mem0 托管平台 p50 0.88s，远优于 LangMem/full-context；若关闭 rerank，LOCOMO p50 可到 0.378s（见 §6），已进入 Zep 商用检索延迟量级。
5. **可信度结论**：MemEval 独立评测、LOCOMO 三方争议、LongMemEval 94.4 vs 49.0 的 45.4pp 缺口共同说明——对外展示时不应只引用供应商分数，本报告采用“同协议自托管实测 + 明确 harness + 区分自报/独立复现”的写法更站得住。

### 10.8 检索来源

- [S1] Mem0 Research（2026-08）：<https://mem0.ai/research>
- [S2] Mem0 GitHub README 新算法表：<https://github.com/mem0ai/mem0>
- [S3] Hamza Shabbir, “Agent Memory in 2026: Mem0 vs Letta vs Zep vs LangMem”（2026-06）：<https://hamzashabbir.dev/article/agent-memory-mem0-vs-letta-vs-zep-vs-langmem-benchmark-2026>
- [S4] Zep/Graphiti 论文（arXiv:2501.13956）：<https://arxiv.org/abs/2501.13956>
- [S5] Developers Digest, “Best AI Agent Memory Providers in 2026”（2026-07）：<https://www.developersdigest.tech/blog/best-ai-agent-memory-providers-2026>
- [S6] ProsusAI/MemEval（独立统一评测）：<https://github.com/ProsusAI/MemEval>
- [S7] Mem0 论文（arXiv:2504.19413）：<https://arxiv.org/abs/2504.19413>
- [S8] Agent Memory Benchmark（AMB）：<https://agentmemorybenchmark.ai/>
- [S9] Hindsight, “Hindsight Is #1 on BEAM”（2026-04）：<https://hindsight.vectorize.io/blog/2026/04/02/beam-sota>
- [S10] Turion, “Mem0 vs Zep vs LangMem”（2026-05）：<https://turion.ai/blog/mem0-vs-zep-vs-langmem-agent-memory-comparison-2026/>
- [S11] Mem-PAL 论文（arXiv:2511.13410）：<https://arxiv.org/html/2511.13410v1>
- [S12] OSS Insight, “The Agent Memory Race of 2026”：<https://ossinsight.io/blog/agent-memory-race-2026>
- [S13] Exabase M-1 BEAM SOTA 发布稿（2026-07）：<https://www.prnewswire.com/news-releases/exabase-achieves-state-of-the-art-on-beam-memory-benchmark-at-every-scale-using-a-smaller-cheaper-model-302835412.html>
- [S14] Digital Applied, “Open-Source Agent Memory: Mem0 vs Letta vs Zep”（2026-08）：<https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared>
- [S15] AMB BEAM 数据集页：<https://agentmemorybenchmark.ai/dataset/beam>
- [S16] Mnemoverse BEAM 口径说明（协议差异警示）：<https://mnemoverse.com/docs/technology/benchmarks/beam>
- [S17] “When Stored Evidence Stops Being Usable”（规模条件化审计，arXiv:2605.07313）：<https://arxiv.org/html/2605.07313>
- [S18] BEAM 论文（arXiv:2510.27246）：<https://arxiv.org/abs/2510.27246>
- [S19] MemGPT 论文（arXiv:2310.08560）：<https://arxiv.org/abs/2310.08560>

## 11. 对外比较口径与边界

1. **横向不可直接比大小。** LOCOMO 是英文长会话二值准确率，cn-Mem-PAL 是中文 GPT-Score/确定性 selection 的 0-100 分，BEAM 是 rubric-nugget pass≥0.5；三者数据集、语言、指标均不同。对外展示应强调“同一协议下的自托管基线”和“同基准内部差异”。
2. **LOCOMO 官方平台 92.5% 不可直接对标。** 官方为更强抽取模型 + OpenAI 系裁判 + chunk=1；本次为 qwen 全链 + chunk=5 + qwen 裁判。本报告价值是纵向对比：同一脚本、同一数据、同一接口，只变抽取管线。
3. **BEAM 官方 runner 默认 gpt-4o-mini 抽取 + gpt-4 答题/裁判 + top-k 200 多 cutoff。** 本次 v1 为 qwen 全链 + top-50 单 cutoff，仅作为同协议下的自托管基线；且只执行了 100K bucket。
4. **cn-Mem-PAL 论文 H2Memory 使用 BLEU 口径。** 论文 BLEU-1 13.59→26.67 与本次 GPT-Score 84.35 / selection 54.78 是不同任务指标，不能直接比大小。
5. **所有分数为单次运行结果。** 未做多次方差分析；裁判模型变更会引入系统差；延迟为串行单客户端测量。

---

## 12. 局限与下一步

**已识别局限**

- LOCOMO：单次运行；无 evidence（dia_id）检索指标；category 5 按官方协议剔除。
- 消融：摄取并发不一致（qwen 基线 2、chunk1/dsv4 4），摄取耗时只可看趋势；dsv4 与 semantic/rerank 的组合尚未实测。
- cn-Mem-PAL：solution selection 仅有事实记忆，未建 principle/归纳层；未调 recall threshold/semantic 变体。
- BEAM：仅执行 100K split；event_ordering 未实现官方 Kendall tau-b 附加分。

**建议下一步**

1. 实测“dsv4 + semantic + rerank”组合，确认成本优先配置的最终质量。
2. BEAM 扩展到 500K/1M/10M split，并补 event_ordering 附加分与 top-k/多 cutoff 对比。
3. cn-Mem-PAL 增加原则层/偏好归纳层，重点提升 Solution Selection 精确双 pos 命中率。
4. 对 semantic + rerank 最优配置做多次重复运行，给出方差与置信区间。
5. 增加 evidence（dia_id）检索与原文回指评估，打通“答案 → 支撑事实 → 原文轮次”链路。

---

## 13. 附录：来源报告与结果文件

| 报告 | 文件 | 代码基线 |
|---|---|---|
| LOCOMO v2 主报告 | [`design/locomo-benchmark-v2-report.md`](locomo-benchmark-v2-report.md) | `feature/memory-research` @ `7f3f6fa3` |
| LOCOMO v2 消融报告 | [`design/locomo-benchmark-v2-ablation-report.md`](locomo-benchmark-v2-ablation-report.md) | `feature/memory-research` @ `854e4be3` |
| cn-Mem-PAL × BEAM 报告 | [`design/cn-mempal-beam-benchmark-report.md`](cn-mempal-beam-benchmark-report.md) | `254fb50d` + `a50e2f13` + `58395b90` |
| LOCOMO v1 基线（作为上下文引用） | [`design/locomo-benchmark-report.md`](locomo-benchmark-report.md) | `b2becb02` |

结果文件：

- LOCOMO：`server/scripts/benchmarks/locomo/results/v2-full-chunk5-extract-rerank.json`、`v2{a,b,c,d}*.json`
- cn-Mem-PAL：`server/scripts/benchmarks/mempal/results/v1-full.json`
- BEAM：`server/scripts/benchmarks/beam/results/v1-full.json`

复现入口：

```bash
# LOCOMO v2
python3 server/scripts/benchmarks/locomo/run.py \
  --email e2e-v3@example.com --password <local-dev-password> \
  --reset --samples 10 \
  --out server/scripts/benchmarks/locomo/results/v2-full-chunk5-extract-rerank.json

# LOCOMO 消融示例：复用 v2 语料，只换召回通道
python3 server/scripts/benchmarks/locomo/run.py --email ... --password ... \
  --samples 10 --skip-ingest --ingest-tag extract-auto-rerank \
  --recall-mode semantic --out .../v2a-semantic.json
```

> 详细复现命令与参数以三份原始报告为准。
