# cn-Mem-PAL v1 × BEAM v1 基准质量报告

> 环境：`feature/memory-research` @ `254fb50d` + `a50e2f13` 中文时间加工 + `58395b90` 两个 v1 runner（后续修订见 commit 历史）。
> 计划：`design/cn-mempal-beam-benchmark-plan.md`
> 结果文件：`server/scripts/benchmarks/mempal/results/v1-full.json`、`server/scripts/benchmarks/beam/results/v1-full.json`（gitignored，可复跑）。

## 1. TL;DR

| Benchmark | 核心结果 | 稳定性 |
|---|---|---|
| **cn-Mem-PAL v1** | Requirement Restatement **84.35/100**（67.1% 题目满分 2/2）；Solution Selection **54.78/100**（33.1% 精确命中 2 个 pos） | 2890 次 remember、118,363 facts、**0 errors**；826 题全判，judge/answer 0 errors |
| **BEAM v1（100K 全量）** | **58.0% pass accuracy**，avg score **0.5329**（400 题 / 1051 nuggets） | 1181 chunks、7,813 facts、0 errors；strongest: preference 97.5% / instruction 87.5% / information_extraction 82.5% |

- 中文时间加工在真实链路生效：PAL-Set 日志行内相对时间（“今天/昨天/上周”）锚定到行内时间戳，绝对日期带星期且自洽。
- 过程中发现并修复一个 **critical 静默数据丢失**：LLM `max_tokens=2000` 截断中文长 JSON，导致首批 30.7% sample 得到 0 facts；调至 8000 后 0-fact 率归零（§6）。
- 日志观察：remember/recall 全部 200，recall 全部 hybrid（RRF）+ rerank applied，ES 五索引读写无错误；3 次 JWT 过期 401 由 runner 自动 re-login 恢复，不计失败。

## 2. 环境与口径

| 项 | 值 |
|---|---|
| 服务 | `docker compose restart mem0` 后 `/health/ready` ready；LLM `max_tokens` 经 `/configure` 重建为 8000 |
| 存储 | v3 `ONLY_VDB`，ES 8.17（`text_zh` ik_max_word / ik_smart） |
| 抽取/答题/裁判 | qwen-plus-latest（DashScope OpenAI-compatible，temperature=0.2） |
| 嵌入 | qwen3.7-text-embedding（1024d） |
| rerank | qwen3-vl-rerank |
| 召回 | `/v1/memory/recall` mode=auto（semantic+BM25 RRF）+ rerank=true，top_k=50（`/v1` limit 上限） |
| 隔离 | Mem-PAL `tenant_id=bench-mempal-v1`；BEAM `tenant_id=bench-beam-v1`；user_id 按用户/会话唯一；召回回放相同 tenant+user scope |
| 数据集 | PAL-Set 中文原版 `input.json`（完整 100 用户）；BEAM 四个 split 已完整下载并校验 SHA-256（100K/500K/1M/10M = 20/35/35/10 conversations），**v1 执行 100K 全量**，其余 split 已缓存留待 v2 |

## 3. few-shot smoke 验证（全量前强制关卡）

- 中文时间/时区回归测试：**228 passed**（`tests/utils/test_temporal.py`、`tests/context/test_vdb_kernel.py`、`tests/test_context_router.py`、`tests/memory/test_main.py`、`tests/test_server_params.py`、`tests/test_mempal_beam_benchmark.py`）。
- 隔离 smoke：`server/scripts/benchmarks/smoke_isolation.py` **6/6 PASS**（tenant 内 user A/B 隔离、tenant 内 agent A/B 隔离、跨 tenant 同 user_id 不串、user scope 不读 agent scope 记忆）。
- cn-Mem-PAL smoke（user 0000，3 samples，1 topic）：ingest→recall→requirement answer/judge→solution answer 全链路通过；日志行 `[log 2024-01-15 16:45]` 的“今天”被正确锚到 `2024年1月15日（星期一）`；requirement judge 2/2。
- BEAM smoke（conv 1，batch 0 = 12 chunks，1 题）：94 facts；information_extraction 答题/裁判 1.0。
- smoke 后修复：`--history-limit/--query-limit` 未作用于 ingest 的 bug；根因定位见 §6。

## 4. cn-Mem-PAL v1（全量 100 用户 / 2890 samples / 826 topics）

### 4.1 摄入

| 指标 | 值 |
|---|---|
| remember batches | 2,890（全部 200） |
| 抽取 facts | **118,363**（created 118,362 + noop 1；updated/errors 0） |
| 单 sample facts | mean 41.0 / median 31 / p90 72 / max 107 |
| 单 sample 耗时 | mean 72.1s / p50 54.0s / p90 126.2s / max 218.7s；总 11,625s（ingest concurrency 20） |
| 0-fact sample | **0**（修复后全量重跑） |
| 中文时间样例 | `2024-01-15 16:45 …今天就不吃甜点` → `用户于2024年1月15日（星期一）16:45向客户婉拒甜点…`（星期自洽） |

### 4.2 Requirement Restatement（官方 GPT-Score，0-2 分 ×50）

| 指标 | 值 |
|---|---|
| topics | 826 |
| **score_100** | **84.35** |
| mean raw score | 1.687 |
| 满分率（score=2） | **67.07%** |
| ≥1 分率 | 94.31% |
| 分数分布 | 2.0×554、1.5×102、1.0×123、0.5×19、0.0×28 |
| answer/judge errors | 0 / 0 |
| per-user score | min 59.38 / p25 79.17 / p50 85.71 / p75 91.67 / max 100 |

### 4.3 Solution Selection（官方确定性打分，pos+1 / neg-1，每题 [-2,2] ×50）

| 指标 | 值 |
|---|---|
| topics | 826 |
| **score_100** | **54.78** |
| mean raw score | 1.096 |
| 精确命中 2 个 pos | **33.05%** |
| 至少命中 1 个 pos | 89.83% |
| answer errors | 0 |
| per-user score | min 22.22 / p25 44.44 / p50 54.17 / p75 66.67 / max 91.67 |

### 4.4 召回性能与隔离

- 召回 826 topics × 2 query（user_query 与 requirement 各一次），全部 200。
- `search_mode=hybrid` 826/826，`rerank_status=applied` 826/826，`degraded=false` 826/826；每次 top-50 全部返回满 50。
- latency：requirement query p50 **1353.7ms** / p95 **1940.0ms** / p99 2765.8ms / max 8498.8ms；solution query p50 1352.2ms / p95 1897.9ms。

## 5. BEAM v1（100K bucket 全量：20 conversations / 400 questions / 1051 rubric nuggets）

### 5.1 摄入

- 1181 chunks（5 turns/chunk，batch `time_anchor` 作为 timestamp），20/20 conversations 成功。
- **7,813 facts**（created 7,799 + noop 14；errors 0），总 4,163.9s（ingest concurrency 4）。

### 5.2 总体与 10 类 question_type（官方 rubric-nugget 裁判，pass≥0.5）

| question_type | accuracy | avg_score |
|---|---|---|
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

### 5.3 召回性能与 per-conversation

- 400/400 召回 200：`search_mode=hybrid`、`rerank_status=applied`、`degraded=false`、`result_count=50`。
- latency p50 **1265.6ms** / p95 **1994.4ms** / p99 2159.9ms / max 2369.3ms。
- conversation accuracy 0.35（conv 3/7）— 0.75（conv 15/18），大多数在 0.45-0.70 之间。

## 6. 关键问题与修复

1. **LLM `max_tokens=2000` 截断（critical，静默丢数）**
   - 现象：首批全量 ingest 出现 30.7% sample 返回 `results: []`（HTTP 200），无任何报错。
   - 复现：同一 sample 直接调 DashScope 稳定返回 47-48 条合法 JSON，`parse_extraction_facts` 对原始输出解析正常；问题只在服务配置 `max_tokens=2000`——PAL-Set 单 sample 输出 40-107 条事实（JSON 约 6000+ 汉字），在 2000 token 处被截断成非法 JSON。
   - 修复：`POST /configure` 将 llm `max_tokens` 升至 8000；sample0 从 0 facts 恢复 48 facts。删除污染 tenant（head/version/scope/event/dedup）后从零重跑，0-fact 率 **0%**。
2. **JWT 长跑过期**：日志捕捉到 3 次 `/v1/memory/remember 401`；runner 内置一次透明 re-login 重试，全部恢复成功，结果文件 errors=0。
3. **smoke 范围 bug**：`--history-limit/--query-limit` 初始未作用于 ingest（会误吞全量 35 samples），已修复并加入逐 user progress 文件（支持断点续跑）。
4. **数据集完整性**：Mem-PAL 完整 `input.json`（70MB，100 用户）与 BEAM 全部四个 split（约 423MB）均自行下载，`beam_dataset.sha256` 记录校验值。

## 7. 日志观察（要求 5：check /v1/memory 加工、存储、召回）

- 两个全量 run 期间，`docker logs mem0-dev-mem0-1` 中 remember/recall 状态码：除 3 次透明恢复的 401 外，**全部 200**；未观察到 422/429/5xx、Traceback、ES `status:4xx/5xx`。
- remember 写路径按设计逐级落盘：DashScope chat 抽取 → 逐 fact embedding → `scope/head/version/event/dedup` 五索引 PUT/POST 全部 `status:200/201`，写入后立即 refresh。
- recall 读路径：`scope/dedup/version/head/event` 查询后回放结果，响应统一 `search_mode=hybrid`（semantic kNN + BM25 RRF）+ `rerank_status=applied` + `degraded=false`。
- 代表性日志时序：

```text
INFO: ... - "POST /v1/memory/remember HTTP/1.1" 200 OK
INFO: ... HTTP Request: POST https://dashscope.../chat/completions "HTTP/1.1 200 OK"
INFO: ... PUT .../agentar_mem0_head/_doc/... [status:201 ...]
INFO: ... POST .../agentar_mem0_event/_refresh [status:200 ...]
INFO: ... - "POST /v1/memory/recall HTTP/1.1" 200 OK
```

## 8. 结论、比较与限制

1. **中文时间加工收益落地**：在 PAL-Set 中文全量上，中文相对时间/星期自洽/`Asia/Shanghai` 时区链路稳定，日志内与对话内时间都正确锚定；这是 requirement restatement 达到 84.35/100 的重要前提。
2. **强项**：Mem-PAL 隐式需求理解（94.3% 题目至少命中 1 个隐式需求）与 BEAM 的 preference/instruction/information_extraction 能力接近或超过 80% accuracy。
3. **短板**：Mem-PAL solution selection 只有 33.1% 精确双 pos（说明 ADD-only 事实记忆对“偏好归纳”不如论文 H2Memory 的 principle 层）；BEAM 的 summarization（27.5%）和 event_ordering（25.0%）明显偏弱。
4. **外部对比口径**：Mem-PAL 论文（AAAI 2026 Oral）报告 H2Memory 在需求改写/方案推荐上 BLEU-1 从 13.59→26.67（BLEU 口径，与本次 GPT-Score/selection 打分为不同任务指标，不能直接比大小）；BEAM 官方 runner 默认 gpt-4o-mini 抽取 + gpt-4 答题/裁判 + top-k 200 多 cutoff，本 v1 为 qwen 全链 + top-50 单 cutoff，只能作为同协议下的自托管基线。
5. **v1 边界**：BEAM 只执行 100K bucket（其余三个 split 已缓存）；event_ordering 未实现官方 Kendall tau-b 附加分；未调 `/v1/memory/recall` 的 threshold/semantic 变体。这些列入 v2。
