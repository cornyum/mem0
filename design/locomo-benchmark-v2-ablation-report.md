# LOCOMO v2 消融报告（v2-a/b/c/d，控制变量）

> 运行日期：2026-08-16 · 代码基线：`feature/memory-research` @ `854e4be3`
> 基线：`design/locomo-benchmark-v2-report.md`（v2：qwen-plus-latest 抽取，chunk=5，
> recall=auto + rerank，72.7%）
> 方案：`design/locomo-benchmark-v2-ablation-plan.md`
> 结果：`server/scripts/benchmarks/locomo/results/v2{a,b,c,d}*.json`

## 1. TL;DR

| 运行 | 唯一变化 | 总体 | temporal | 召回 p50 | 结论 |
|---|---|---|---|---|---|
| v2 基线 | — | **72.7%** | 75.7% | 897.9 ms | — |
| v2-a-semantic | recall=semantic | **73.8%** | **78.2%** | 730.5 ms | **最优，且更快** |
| v2-a-keyword | recall=keyword | 70.7% | 75.7% | 522.1 ms | 最便宜，但 multi-hop 弱 |
| v2-b-norerank | 去掉 rerank（纯 RRF） | 70.7% | 77.9% | **377.6 ms** | rerank 值 +2.1pp |
| v2-c-chunk1 | chunk=1 | 70.0% | 75.1% | 972.3 ms | 无收益，不建议 |
| v2-d-dsv4 | 抽取模型 deepseek-v4-flash-0731 | 71.0% | 75.1% | 939.7 ms | 更省事、接近 qwen |

## 2. 控制变量保证

- v2-a/b 通过 `--skip-ingest --ingest-tag extract-auto-rerank` 复用 v2 的**同一份**
  qwen chunk5 语料（4,504 条 fact），摄入侧零差异。
- v2-c / v2-d 是独立语料：chunk 和抽取模型各自是唯一变化；其余
  prompt、角色映射、timestamp、图片合并、recall=auto + rerank、答题/裁判
  qwen-plus-latest、官方容忍规则全部相同。
- 所有运行都走 `/v1/memory/remember` 与 `/v1/memory/recall`，category 1-4
  计分 1,540 题，category 5 剔除。

## 3. 分项结果

### 3.1 总体与分 category

| category | v2 基线 | semantic | keyword | no-rerank | chunk1 | dsv4 |
|---|---|---|---|---|---|---|
| multi-hop | 67.4% | **68.8%** | 61.7% | 60.3% | 68.8% | **70.6%** |
| temporal | 75.7% | **78.2%** | 75.7% | 77.9% | 75.1% | 75.1% |
| open-domain | 45.8% | 46.9% | 42.7% | 43.8% | 38.5% | **47.9%** |
| single-hop | 76.5% | **76.8%** | 74.9% | 74.4% | 72.1% | 72.3% |
| **总体** | **72.7%** | **73.8%** | 70.7% | 70.7% | 70.0% | 71.0% |

### 3.2 “无信息”答案

| 运行 | multi-hop | temporal | open-domain | single-hop | 合计 |
|---|---|---|---|---|---|
| v2 基线 | 6 | 30 | 32 | 40 | 108（7.0%） |
| semantic | 6 | **25** | 30 | 42 | 103（6.7%） |
| keyword | 14 | 30 | 31 | 44 | 119（7.7%） |
| no-rerank | 21 | 27 | 35 | 55 | 138（9.0%） |
| chunk1 | 12 | 27 | 35 | 66 | 140（9.1%） |
| dsv4 | 8 | 34 | 29 | 74 | 145（9.4%） |

### 3.3 召回延迟（串行单客户端）

| 运行 | p50 | p95 | avg results |
|---|---|---|---|
| v2 基线 | 897.9 ms | 1229.0 ms | 50.0 |
| semantic | 730.5 ms | 907.5 ms | 50.0 |
| keyword | **522.1 ms** | 704.0 ms | 49.9 |
| no-rerank | **377.6 ms** | **526.2 ms** | 50.0 |
| chunk1 | 972.3 ms | 1228.9 ms | 50.0 |
| dsv4 | 939.7 ms | 1337.6 ms | 50.0 |

### 3.4 摄取侧

| 运行 | chunk | 抽取模型 | 批次数 | 事实数 | 摄取耗时 | 备注 |
|---|---|---|---|---|---|---|
| v2 基线 | 5 | qwen-plus-latest | 1,283 | **4,504** | 3,707 s | concurrency 2 |
| v2-c | 1 | qwen-plus-latest | 5,882 | 4,501 | 3,196 s | concurrency 4 |
| v2-d | 5 | deepseek-v4-flash-0731 | 1,283 | 2,748 | 1,018 s | concurrency 4，`enable_thinking=false` |

## 4. 逐项归因

### 4.1 recall-mode：semantic > auto(RRF) > keyword

- 同一语料下 semantic 73.8%，auto 72.7%，keyword 70.7%。
- semantic 比 auto 高 +1.0pp，且 temporal 到 78.2%，召回延迟反而低
  （730 vs 898 ms，因为少走一条 keyword 通道）。
- keyword 在 multi-hop 掉到 61.7%（-5.7pp vs auto），说明纯关键词通道
  不足以支撑需要组合多条事实的题目；auto 的 RRF 确实补齐了 keyword 短板。

### 4.2 rerank：值得保留

- 同一语料、同 auto：rerank 72.7% vs no-rerank 70.7%，**rerank 贡献 +2.1pp**。
- 代价：p50 898 → 378 ms，即 rerank 约 +520 ms；p95 1229 → 526 ms。
- 质量优先选 rerank；成本敏感且 query 多为单跳时可关。

### 4.3 chunk-size：1 没有收益

- chunk1 与 chunk5 事实数几乎相同（4,501 vs 4,504），说明 ADDITIVE prompt
  在 chunk5 已经能把 batch 内的 topic 穷举出来；chunk1 只是把同一个事实
  拆得更碎，反而使 single-hop/open-domain 小幅下降，总体 -2.7pp。
- 成本高 4.6×（5,882 vs 1,283 批）。结论：**不采用 chunk1**。

### 4.4 抽取模型：deepseek-v4-flash-0731 接近 qwen，单位事实效率更高

- 总体 71.0% vs qwen 72.7%（-1.7pp），但 deepseek 只抽取 2,748 条事实
  （qwen 4,504 条的 61%），multi-hop（70.6%）和 open-domain（47.9%）反而更好。
- temporal 与 qwen 持平（75.1% vs 75.7%）。
- 摄取成本：dsv4（concurrency 4，1,018 s）显著低于 qwen chunk5
  （concurrency 2，3,707 s）；严格成本对比需要同并发重测，但趋势明确：
  dsv4 是“更少事实、接近质量、更快/更省”的选择。
- 技术前提：DashScope 的 deepseek-v4-flash-0731 默认输出长
  `reasoning_content`；本次通过新增的 `extra_body: {"enable_thinking": false}`
  关闭思考后，单批抽取从约几十秒级降到 2.6 s（commit `854e4be3`）。

## 5. 组合建议

按本次单变量结果：

1. **质量优先**：qwen-plus-latest 抽取 + chunk5 + `recall_mode=semantic` + rerank
   （预期 ~73.8%，延迟比 auto 还低）。
2. **延迟优先**：qwen-plus-latest 抽取 + chunk5 + auto + **no-rerank**
   （70.7%，p50 378 ms）；或 keyword + rerank（70.7%，p50 522 ms）。
3. **成本/推理优先**：deepseek-v4-flash-0731（enable_thinking=false）+ chunk5
   + semantic + rerank（建议作为下一个组合消融）。
4. **不建议**：chunk1（无质量收益，成本 4.6×）。

## 6. 文件与命令

```bash
# 召回通道/rerank 消融（复用 v2 语料）
python3 server/scripts/benchmarks/locomo/run.py --email ... --password ... \
  --samples 10 --skip-ingest --ingest-tag extract-auto-rerank \
  --recall-mode semantic --out .../v2a-semantic.json
# ... keyword / --no-rerank

# chunk1
python3 ... --chunk-size 1 --ingest-tag chunk1-qwen --ingest-concurrency 4 \
  --reset --out .../v2c-chunk1.json

# deepseek（先 /configure 切换模型，跑完后恢复 qwen-plus-latest）
python3 ... --ingest-tag dsv4-chunk5 --ingest-concurrency 4 \
  --reset --out .../v2d-dsv4.json
```

结果文件：

- `v2a-semantic.json`、`v2a-keyword.json`、`v2b-norerank.json`
- `v2c-chunk1.json`、`v2d-dsv4.json`

## 7. 局限

- 单次运行；qwen chunk5 基线摄取 concurrency=2，chunk1/dsv4 concurrency=4，
  摄取耗时跨配置不完全可比（质量指标不受影响）。
- 裁判/答题模型仍为 qwen-plus-latest；换裁判会引入系统差。
- 无 evidence（dia_id）检索指标；category 5 未评。
