# LOCOMO Benchmark v2 方案（抽取管线 v2 × /v1/memory/*）

> 代码基线：`feature/memory-research` @ `a26b61b3`
> 对比基线：`design/locomo-benchmark-report.md`（v1，chunk=25，旧 OSS 抽取 prompt，33.3%）
> 本方案执行日期：2026-08-16

## 1. 目标

1. 验证 `design/extraction-quality-plan.md` 的抽取 v2 修改（ADDITIVE prompt + observation
   timestamp + 双说话人角色映射 + chunk 5）在完整 LOCOMO-10 上的效果。
2. 与 v1 报告做同口径纵向对比：总体准确率、category 1-4 准确率、fact 摄取密度、
   无信息比例、召回延迟。
3. 产出可复现报告与逐题明细，全部走 `/v1/memory/*` HTTP 接口。

## 2. 数据集

- 文件：`server/scripts/benchmarks/locomo/dataset/locomo10.json`（2.8 MB，已下载）。
- 校验：10 会话 / 272 非空 session / 5,882 轮 / 1,986 题；
  category 分布 1:282 / 2:321 / 3:96 / 4:841 / 5:446，与官方一致。
- 评分口径：category 1-4（1,540 题）计分，category 5（446 对抗题）剔除，官方 LOCOMO 协议。
- 数据集与结果目录 gitignore，不入库。

## 3. 服务与存储

- 服务：Docker compose dev 栈，FastAPI `:8888`；已 `docker compose restart mem0`，
  `/health/ready` ready，登录探活 200，`/v1/memory/remember` 冒烟确认 v2 代码生效。
- 存储：`ONLY_VDB`，Elasticsearch 8.17 单一权威（`agentar_mem0` 五族索引 + ik），
  MySQL 仅应用表。
- 清库：运行前 `POST /reset`（admin Bearer），失败回退直删
  `http://localhost:9200/agentar_mem0_*`。**本方案明确清空向量库后重建。**
- 会话隔离：`user_id = locomo_{ingest}-{recall_mode}-rerank_{sample_id}`，多配置互不污染。

## 4. 模型选择（运行时经 `/v1/capabilities` 采集，结果回写报告）

| 角色 | 模型 | 备注 |
|---|---|---|
| 记忆抽取 LLM | `qwen-plus-latest` | 新 ADDITIVE prompt；DashScope OpenAI 兼容 |
| 向量 | `qwen3.7-text-embedding`（1024 维） | ES dense_vector cosine |
| 重排 | `qwen3-vl-rerank` | recall `rerank=true` |
| 答案生成 | `qwen-plus-latest`（temperature 0） | benchmark 侧独立 DashScope 调用 |
| 裁判 | `qwen-plus-latest`（temperature 0） | 官方容忍规则 |

## 5. 数据录入（知识加工）

- `POST /v1/memory/remember`，`mode=extract`，`kind=fact`，`categories=["locomo"]`。
- 每 session 按 **5 轮/批**（v2 默认；`--chunk-size 1` 为官方同参消融预留）。
- 每批请求携带 `timestamp = "YYYY-MM-DD"`（session 绝对日期，抽取时间锚点）。
- 角色映射对齐官方 runner：`speaker_a → user`，`speaker_b → assistant`；
  ADDITIVE prompt 从双角色抽取，并禁止 assistant echo。
- `blip_caption/query` 图片描述按官方方式合并进文本。
- 默认把 session 日期织入 content（`--no-inline-session-date` 可消融关闭）。
- metadata 携带 `sample_id / session / session_date`；会话按时间戳排序，跨会话并发 2。

## 6. 评测管线

沿用 `server/scripts/benchmarks/locomo/run.py` 五阶段 checkpoint + `--resume`：

1. preflight：`/health/ready` → `/v1/capabilities` → `--reset` 清库。
2. ingest：统计 created/updated/noop/errors 与耗时。
3. recall：1,540 题逐题 `POST /v1/memory/recall`（`mode=auto`，`rerank=true`，
   `limit=50`），串行记录 p50/p95、`search_mode`、`degraded`、命中全文。
4. answer：参考日期=会话最后 session 日期，官方式答案生成，并发 4。
5. judge：官方容忍规则二值判分，并发 4，原始判定落盘。

## 7. v1 → v2 对比轴

| 轴 | v1 | v2 |
|---|---|---|
| 抽取 prompt | OSS user-only prompt，惩罚 assistant | ADDITIVE prompt，双说话人 + 密度清单 |
| 时间锚点 | 仅 content 织入日期，prompt 用“今天” | `timestamp` → Observation Date 归一化 |
| 角色映射 | 双 user 规避 prompt 惩罚 | 官方 speaker_a→user / speaker_b→assistant |
| chunk | 25 | 5 |
| 召回/答题/裁判 | auto + rerank，同 | 同（保持纵向可比） |

核心验证假设：
1. temporal（v1 16.5%）显著提升，58.3% 无信息率下降；
2. fact 密度（v1 2,153 条）上升，全量“无信息”比例（39%）下降；
3. multi-hop（v1 45.7%）在密度提升后进一步受益；
4. 延迟随调用量上升而增加，单独记录以量化成本。

## 8. 执行

```bash
# 0) 快速冒烟（清库 + 1 会话 + 只测摄取与召回，不用答案模型）
python3 server/scripts/benchmarks/locomo/run.py \
  --email e2e-v3@example.com --password <local-dev-password> \
  --samples 1 --sessions 1 --reset --no-answer \
  --out server/scripts/benchmarks/locomo/results/v2-smoke.json

# 1) 完整 v2（清库 + 10 会话 + 1540 题全评）
python3 server/scripts/benchmarks/locomo/run.py \
  --email e2e-v3@example.com --password <local-dev-password> \
  --reset --samples 10 \
  --out server/scripts/benchmarks/locomo/results/v2-full-chunk5-extract-rerank.json

# 2) 中断恢复
python3 ... --resume --out 同上
```

## 9. 报告产出

- `design/locomo-benchmark-v2-report.md`：总体/分 category/每会话准确率、摄取密度、
  延迟、v1→v2 对比表、口径差异说明、后续消融。
- 结果 JSON 保留逐题 `memories` 与原始判定，可审计。
