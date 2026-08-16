# LOCOMO × v3 `/v1/memory/*` 评测基准报告

> 运行日期：2026-08-16 · 代码基线：`feature/memory-research` @ `e7c7093b` + 本次评测提交
> 结果文件：`server/scripts/benchmarks/locomo/results/full-extract-auto-rerank.json`（每题明细可回溯）

## 1. 背景与目标

用官方 LOCOMO 10 会话数据集，端到端评测本 fork v3 记忆平台的**知识加工（`/v1/memory/remember` extract）与召回（`/v1/memory/recall`）**质量，建立可复现的 benchmark 基线，并产出质量分析。

评测口径对齐官方 LOCOMO 协议：**category 1-4 计分（1540 题），category 5（对抗题 446 题）剔除**；召回记忆 → LLM 生成答案 → LLM 裁判二值判分。

## 2. 方案设计

### 2.1 数据集

- 完整 `locomo10.json`（2.8 MB）下载至 `server/scripts/benchmarks/locomo/dataset/locomo10.json`；脚本缺失时自动从 `snap-research/locomo` GitHub 原始地址下载，支持 `--dataset` 覆盖。
- 校验通过：10 会话 / 272 个非空 session / 5,882 轮对话 / 1,986 题，category 分布 **1:282 / 2:321 / 3:96 / 4:841 / 5:446**，与官方公布完全一致；session 日期 100% 可解析（`"1:56 pm on 8 May, 2023"` 格式）。
- 数据集与结果目录已 gitignore，不入库。

### 2.2 数据录入（知识加工）

- 接口：`POST /v1/memory/remember`，`mode=extract` + `messages`（LLM 事实抽取管线），`kind=fact`，`categories=["locomo"]`，`metadata` 携带 `sample_id/session/session_date`。
- 每 session 按 **25 轮/批**切块（本报告基线运行值；官方 runner 为逐轮调用，成本 17 倍）。 后续已按 `design/extraction-quality-plan.md` 将 runner 默认值改为 5、补齐 per-batch `timestamp` 并恢复官方角色映射。
- 消息构造（与官方 runner 的两处**有意差异**，见 §7）：
  1. **双说话人均映射 `role=user`**——OSS 抽取 prompt 明确惩罚 assistant 消息内容，若照搬官方 speaker_b→assistant 映射，一半对话信息会被主动丢弃；
  2. **每轮内容织入 session 绝对日期**（`"Caroline (08 May 2023): ..."`）——session 时间戳只存在于元数据，不进 transcript 则 temporal 事实全部丢失；
  3. blip_caption/query 图片描述按官方方式合并进文本。
- 会话内 session 按时间戳排序录入；跨会话并发 2（不同 scope 无 CAS 竞争）。

### 2.3 存储与清库

- 存储：Elasticsearch 8.17 单一权威（`ONLY_VDB` 模式，`agentar_mem0` 前缀五族索引 + analysis-ik 中文分词），MySQL 8.0 仅存应用表（auth/settings），无 SQL 记忆表。
- 清库：运行前 `POST /reset`（admin Bearer）→ 五族索引删除重建；失败回退直删 `http://localhost:9200/agentar_mem0_*`。
- 会话间隔离：`user_id = locomo_{ingest}-{recall_mode}-rerank_{sample_id}`，多配置共存互不污染。

### 2.4 模型选择（server 当前配置，运行时经 `GET /v1/capabilities` 采集）

| 角色 | 模型 | 说明 |
|---|---|---|
| 记忆抽取 LLM | `qwen-plus-latest` | DashScope OpenAI 兼容端点 |
| 向量 | `qwen3.7-text-embedding`（1024 维） | ES dense_vector cosine |
| 重排 | `qwen3-vl-rerank`（dashscope provider） | recall `rerank=true` 时在链路上 |
| 答案生成 / 裁判 | `qwen-plus-latest`（temperature 0） | benchmark 侧独立调用 DashScope |

### 2.5 评测管线（`server/scripts/benchmarks/locomo/run.py`，纯标准库）

五阶段，每阶段落盘 checkpoint，`--resume` 断点续跑：

1. **preflight**：`/health/ready` → `/v1/capabilities` 采集 → 可选 `--reset` 清库；
2. **ingest**：§2.2，统计 created/updated/noop（noop=dedup 命中）与耗时；
3. **recall**：1,540 题逐题 `POST /v1/memory/recall`（`mode=auto` 混合 RRF + rerank，`limit=50`），串行保延迟准确，记录 p50/p95、`search_mode`、`degraded`、每题命中记忆全文；
4. **answer**：官方式答案生成（问题 + top-50 记忆 + 参考日期=该会话最后 session 日期 + 双说话人 profile），"ANSWER:" 解析，并发 4；
5. **judge**：官方容忍规则（列表部分给分、释义容忍、日期 ±14 天、时长 50%、category 3 金标分号截断），输出 CORRECT/WRONG，并发 4，原始判定落盘备审计。

指标：总体/分类别/每会话准确率、召回延迟分位、摄取统计、错误计数。**运行中 JWT 过期自动重登**（本次实测在 ~30 分钟点触发过一次）。

## 3. 运行环境

- Docker Compose dev 栈：FastAPI server（:8888）、ES 8.17（:9200）、MySQL 8.0（:8306）；`AUTH_DISABLED=false`，admin 账号登录（Bearer JWT）。
- `MEMORY_STORAGE_MODE=ONLY_VDB`（compose 默认）。

## 4. 结果

### 4.1 总体（1,540/1,540 题全评，全阶段 0 错误）

| 指标 | 值 |
|---|---|
| **总体准确率** | **33.3%（513/1540）** |
| 摄取 | 331 批 / **2,153 条事实** / noop 342（dedup 命中 13.7%）/ 0 错误 / 668 s |
| 召回 | 1,540 题 / p50 **716.7 ms** / p95 **974.2 ms** / 平均命中 50.0 条（limit 打满）/ 1,132 s |
| 答案 + 裁判 | 1,540 × 2 次 LLM 调用 / 0 错误 / ~590 s |
| 端到端墙钟 | ~42 分钟（含 27 题补评） |

### 4.2 分维度

| category | 题数 | 正确 | 准确率 | 其中"无信息" |
|---|---|---|---|---|
| 1 multi-hop | 282 | 129 | **45.7%** | 50（17.7%） |
| 2 temporal | 321 | 53 | **16.5%** | 187（58.3%） |
| 3 open-domain | 96 | 25 | 26.0% | 41（42.7%） |
| 4 single-hop | 841 | 306 | 36.4% | 325（38.6%） |

### 4.3 每会话

conv-26 33.6% · conv-30 34.6% · conv-41 32.2% · conv-42 31.2% · conv-43 38.8% · conv-44 35.0% · conv-47 27.3% · conv-48 31.4% · conv-49 33.3% · conv-50 36.7% —— 离散度小（27.3%~38.8%），无异常会话，结果稳定。

## 5. 评测过程中发现并修复的缺陷

1. **[server · critical] extract 路径 `response_format` 双重包裹**：`service.py` 以位置参数传 `{"response_format": {...}}`，落到 `OpenAILLM.generate_response` 的 `response_format` 形参上，实际发出 `{"response_format": {"response_format": ...}}`，DashScope 400 → 全部 extract 写入 503 `primary_unavailable`。该 bug 自 v3 初始提交即存在（此前 e2e 未覆盖带 LLM 的 extract 真实链路）。修复为关键字传参；新增回归测试 `test_remember_extract_forwards_json_object_response_format`（先红后绿），`tests/context/` + router/legacy 套件 206/206、server 套件 124/124 通过，线上实测抽取恢复正常。
2. **[harness] 摄取口径两轮迭代**（conv-26 冒烟）：官方角色映射 + 无日期 → 24.3%；改双 user 映射 + 日期织入 + blip 合并 → 33.6%。均为信息保真修正而非调参刷分。
3. **[harness] JWT 过期**：全量运行 ~30 分钟处 token 失效，27 题召回 401；补自动重登 + 错误条目可续跑后补评完整。

## 6. 质量分析

- **temporal 是最大短板（16.5%）且根因明确**：抽取 LLM 把相对日期原样写入事实（`"Gave a talk ... last week"`、`"Going to a transgender conference this month"`），未结合输入中的 session 绝对日期归一化；答题侧照抄相对表述，与金标绝对日期不匹配。58% 的 temporal 题连"有答案"都达不到。改进方向：抽取 prompt 增加相对→绝对日期归一化要求（属产品/prompt 改进，benchmark 已保留输入侧日期）。
- **抽取信息密度不足**：5,882 轮 → 2,153 条事实（~37%），全量 39% 的题答案为"无信息"。chunk=25 批量抽取下 LLM 只保留显著事实；官方 runner 逐轮抽取（chunk=1）密度更高，是首要消融轴。
- **multi-hop 反而最强（45.7%）**：说明混合召回 + rerank 在事实已入库时能很好组合多条记忆；瓶颈不在检索融合，而在上游事实覆盖。
- **dedup 有效工作**：342 个候选因与已有事实重复被 noop（13.7%），跨 session 重复信息被正确合并。
- **rerank 延迟代价**：p50 717 ms（含 qwen3-vl-rerank LLM 调用），对比 2026-08-15 无 rerank 基线 p50 265 ms，重排占约 2/3 延迟；召回质量对比需 `--no-rerank` 消融量化。

## 7. 与外部数字的对比与口径差异

README/官方平台报告 LOCOMO 71.4→92.5，**本结果 33.3% 不可直接对标**，差异来源：

1. 模型栈不同：官方为托管平台管线（更强抽取模型 + OpenAI 系裁判）；本评测全链 qwen-plus 自托管。
2. 抽取粒度不同：官方 runner 逐轮 add（chunk=1）；本评测 chunk=25（成本约束，已列为消融轴）。
3. 角色映射不同：官方 runner speaker_b→assistant（适配平台抽取 prompt）；本评测双 user（适配 OSS 抽取 prompt 的 assistant 惩罚条款）。
4. 裁判模型不同（qwen-plus vs OpenAI GPT 系），判分严格度存在系统性差异。

纵向可比对象是本仓库自己的后续运行（同脚本同口径）。

## 8. 局限

- 单次运行、temperature 0（抽取 server 侧 0.2 / 答案裁判 0），未做多次方差分析。
- 无 evidence（dia_id）链路评测：extract 模式记忆为 LLM 压缩事实，无法回指原文轮次，Hit/Recall/MRR 类检索指标不适用。
- category 5 按官方协议剔除；对抗鲁棒性未评。
- 延迟为单客户端串行测量，非并发压测。

## 9. 复现命令

```bash
cd server && make up                      # 栈就绪（ES/MySQL/API :8888）
# 数据集自动下载/复用 server/scripts/benchmarks/locomo/dataset/locomo10.json
python3 server/scripts/benchmarks/locomo/run.py \
    --email <admin-email> --password <password> \
    --reset --samples 10 \
    --out server/scripts/benchmarks/locomo/results/full-extract-auto-rerank.json
# 冒烟：--samples 1；仅延迟：--no-answer；断点续跑：--resume
```

## 10. 后续工作（消融轴）

| 轴 | 命令 | 验证假设 |
|---|---|---|
| 抽取粒度 | `--chunk-size 1`（runner 默认已改为 5） | 事实密度↑ → 准确率显著↑（chunk 1 成本约 17 倍） |
| 重排收益 | `--no-rerank` | 717ms→~270ms 延迟换多少准确率 |
| 召回通道 | `--recall-mode semantic` / `keyword` | RRF 混合增益 |
| 原文摄取 | `--ingest append` | 知识加工 vs 原文存储的收益 |
| 抽取 prompt | 相对日期归一化指令 | temporal 16.5% 能否翻倍 |
