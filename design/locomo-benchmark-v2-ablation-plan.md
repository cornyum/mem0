# LOCOMO v2 消融矩阵方案（v2-a/b/c/d，控制变量）

> 基线：v2 报告（qwen-plus-latest 抽取，chunk=5，recall=auto + rerank，72.7%）
> 代码基线：包含 `--skip-ingest / --ingest-tag` 的 runner 扩展
> 目标：一次只改变一个变量，分别验证召回通道、rerank、加工频率、加工模型。

## 1. 控制变量矩阵

| 运行 | 复用语料（scope tag） | chunk | 抽取模型 | recall mode | rerank | 唯一变化 |
|---|---|---|---|---|---|---|
| **v2 基线** | extract-auto-rerank | 5 | qwen-plus-latest | auto | true | — |
| **v2-a-semantic** | extract-auto-rerank | 5 | qwen-plus-latest | semantic | true | recall mode |
| **v2-a-keyword** | extract-auto-rerank | 5 | qwen-plus-latest | keyword | true | recall mode |
| **v2-b-norerank** | extract-auto-rerank | 5 | qwen-plus-latest | auto | **false** | 去掉 rerank（仅 RRF） |
| **v2-c-chunk1** | chunk1-qwen（新摄取） | **1** | qwen-plus-latest | auto | true | 加工频率 |
| **v2-d-dsv4** | dsv4-chunk5（新摄取） | 5 | **deepseek-v4-flash-0731** | auto | true | 加工模型 |

`--skip-ingest --ingest-tag` 使 v2-a/b 直接复用 v2 的同一份摄取语料，因此
召回通道/rerank 的对比不引入任何摄取差异。

## 2. 语料与存储

- 数据集：`server/scripts/benchmarks/locomo/dataset/locomo10.json`（10 会话，
  1,540 计分题；已校验，不入库）。
- 存储：`ONLY_VDB` Elasticsearch 8.17；五族 alias `agentar_mem0_*`。
- 摄入：`POST /v1/memory/remember`，`mode=extract`，`kind=fact`，
  `categories=["locomo"]`，每批带 session `timestamp`；官方角色映射。
- scope：`user_id = locomo_{scope_tag}_{sample_id}`。
- 新摄取前 `POST /reset`；复用语料运行**绝不** reset。
- 模型切换：`POST /configure` 只改 `llm.config.model`，服务构建后
  `/v1/capabilities` 复核；deepseek 完成后恢复 qwen-plus-latest。

## 3. 固定不变

- 抽取 prompt：ADDITIVE（多说话人、时间归一化、密度清单）。
- 召回：`limit=50`，串行测延迟；`mode` 与 `rerank` 按矩阵。
- 答题/裁判：qwen-plus-latest，temperature 0；官方容忍规则。
- 答案参考日期、双说话人 profile、并发 4。
- dataset、category 1-4 计分、category 5 剔除。

## 4. 执行顺序与命令

```bash
# 1) v2-a-semantic（约 35 min，复用 v2 语料）
python3 server/scripts/benchmarks/locomo/run.py \
  --email e2e-v3@example.com --password <pw> --samples 10 \
  --skip-ingest --ingest-tag extract-auto-rerank --recall-mode semantic \
  --out server/scripts/benchmarks/locomo/results/v2a-semantic.json

# 2) v2-a-keyword
... --recall-mode keyword --out .../v2a-keyword.json

# 3) v2-b-norerank
... --recall-mode auto --no-rerank --out .../v2b-norerank.json

# 4) v2-c-chunk1（新摄取，--ingest-concurrency 4，约 1.5-2.5 h）
... --chunk-size 1 --ingest-tag chunk1-qwen --ingest-concurrency 4 \
   --reset --out .../v2c-chunk1.json

# 5) v2-d-dsv4（切换模型后新摄取，约 1-1.5 h）
curl -X POST /configure -d '{"llm":{"provider":"openai","config":{...,"model":"deepseek-v4-flash-0731"}}}'
... --ingest-tag dsv4-chunk5 --ingest-concurrency 4 --reset --out .../v2d-dsv4.json
```

## 5. 产出

- `design/locomo-benchmark-v2-ablation-report.md`：
  - 各运行总体/分 category/每会话/无信息率/延迟；
  - 与 v2 基线逐项对比；
  - 每个变量的归因结论；
  - 后续建议（例如最佳组合是否应为 `chunk1 + semantic + rerank` 或 `dsv4 + no-rerank`）。
- 结果 JSON 逐题可回溯，全部走 `/v1/memory/*`。
