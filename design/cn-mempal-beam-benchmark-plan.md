# cn-Mem-PAL v1 × BEAM v1 基准计划（中文时间加工 + v2 基线环境）

> 基线：`feature/memory-research` @ 254fb50d + 未提交的中文相对时间/多时区加工（`mem0/utils/temporal.py`、动态中文 few-shot、`/v1/memory/remember.timezone`）。
> 环境：v3 `ONLY_VDB` + Elasticsearch + MySQL(app-only)，模型栈为 v2 基线（qwen-plus-latest 抽取/答题/裁判、qwen3.7-text-embedding、qwen3-vl-rerank）。
> 目标：在真实 `/v1/memory/*` HTTP 表面上，对**中文 PAL-Set（Mem-PAL 2026）**与**英文 BEAM 100K**各产出一个 v1 质量报告。

## 1. 定义

| 名称 | 数据集 | v1 范围 | 主要任务 |
|---|---|---|---|
| **cn-Mem-PAL v1** | `hzp3517/Mem-PAL` `data_synthesis_v2/data/input.json`（中文原版 PAL-Set） | 全部 100 用户：30 段历史 sample + 5 段 query sample；826 个 query topic | Requirement Restatement（LLM 裁判 0-2 分）+ Solution Selection（pos/neg 确定性打分） |
| **BEAM v1** | `Mohammadta/BEAM` 100K bucket（parquet 20 conversations） | 全部 20 会话 × 20 题 = 400 题（1051 rubric nuggets） | 召回 → 答题 → BEAM rubric nugget 裁判（0/0.5/1.0） |

两个 v1 均走 `POST /v1/memory/remember`（mode=extract）与 `POST /v1/memory/recall`，不直接碰 ES/SQL。

## 2. v2 基线环境（本次不改变量）

- remember：mode=extract，qwen-plus-latest，ADDITIVE 抽取提示词；LLM `max_tokens=8000`（v2 默认 2000 会被中文 PAL-Set 单 sample 的 40-60 条事实输出截断——已实测复现 30% sample 得 0 facts，根因确认后通过 `/configure` 调整并验证 sample0 恢复 48 facts）。
- 中文时间加工：cn-Mem-PAL 全部写入传 `timezone="Asia/Shanghai"`，由服务端做时区解析、日期/星期自洽、动态中文 few-shot；BEAM 沿用 v2 默认（不传 timezone，只传 batch `timestamp`）。
- recall：mode=auto（semantic kNN + BM25 RRF）+ rerank=true，top_k=50（`/v1/memory/recall` limit 上限 50）。
- 答题/裁判：qwen-plus-latest，DashScope OpenAI-compatible，temperature=0。
- 服务重启已执行：`docker compose -f server/docker-compose.yaml restart mem0`，`/health/ready` 全部 ready。

## 3. 数据与评测协议

### 3.1 cn-Mem-PAL v1

#### 3.1.1 摄入（每 sample 一次 remember-extract）

- 写入 scope：`tenant_id=bench-mempal-v1`，`user_id=mempal_{user_id}`（0000..0099）。
- 30 个 history sample + 5 个 query sample 严格按时间顺序摄入；同一 user 内顺序执行（保证去重/修订视角与历史累积一致），不同 user 并行。
- 每条 log 转成 `user` 消息，内容带自身时间戳前缀：`[log 2024-01-01 09:15] 用户搜索了…`；对话按 turn 顺序转 `user`/`assistant` 消息。
- remember 参数：`timestamp=dialogue_timestamp`、`timezone="Asia/Shanghai"`、`categories=["mempal"]`、`metadata={benchmark, sample_id, sample_kind}`。
- 双时间锚点的 per-call `prompt`（写入 benchmark 的 custom instruction）：
  > 日志行以 `[log YYYY-MM-DD HH:MM]` 前缀给出该条日志自身时间戳：日志内部的相对时间（昨天/上周等）以**该行自身时间戳**为锚点；对话消息中的相对时间以 Observation Date 为锚点。日期统一写 `YYYY年M月D日（星期X）` 且与星期自洽。
- 不使用 topics/ground truth 任何字段作为输入；query sample 只摄入其 logs+dialogue（与官方 PAL-Bench 将当前 sample 作为情境来源一致）。

**成本/规模估算（v1 全量）**：2890 次 remember-extract（100 用户 × 35 sample，99619 logs + ~4 万对话消息一次进提示词）；用户级并行 4，预计 20-40 分钟。

#### 3.1.2 Requirement Restatement（826 topic）

- 对每个 query topic：`recall(user_query)` → top-50 memories。
- 答题：qwen-plus-latest，官方系统提示词（“个性化交互助手…”）+ 官方模板，输出 JSON `{"requirement": "..."}`。
- 裁判：qwen-plus-latest，**官方 Mem-PAL requirement 裁判提示词逐字复用**（背景=user_query、参考=requirement+implicit_needs、预测=模型输出；输出 `{analysis, score}`，score ∈ {0,0.5,1,1.5,2}）。
- 指标：官方口径 `score_100 = mean(score) * 50`；另报 score=2 的 topic 占比（完全命中 2 个隐式需求）、≥1 占比、按 user 平均、judge 解析失败数。

#### 3.1.3 Solution Selection（826 topic）

- 输入当前 topic 的 **gold requirement**（与官方 `solution_selection_memory_rag_v2.py` 一致）作为 recall query；8 个 candidate_solutions 全部给出，S1..S8。
- 答题：qwen-plus-latest，官方系统/用户提示词，要求严格输出 2 个不同 id。
- 打分（确定性，不调 LLM）：选中 pos +1，选中 neg -1，其他 0；单题 [-2,2]，官方口径 `score_100 = mean(sample_score) * 50`；另报精确命中 2 个 pos 的比例、命中 ≥1 pos 的比例、解析失败数。

### 3.2 BEAM v1

#### 3.2.1 摄入

- 数据集：HF `Mohammadta/BEAM` `data/100K-00000-of-00001.parquet` 转为 `server/scripts/benchmarks/beam/dataset/beam_100k.json`（本地缓存，不提交）。
- 20 conversations，每个 chat 是 3 个带 `time_anchor` 的 batch（共约 188-200 turns/conv）。
- 写入 scope：`tenant_id=bench-beam-v1`，`user_id=beam_100k_{conversation_id}`。
- batch 内按 **5 turns/chunk**（v2 基线 chunk5）切分；`timestamp=time_anchor`（epoch），不传 timezone（与 v2 LOCOMO 一致）；`categories=["beam"]`、`metadata={benchmark, conversation_id, batch_idx}`。
- 并行按 conversation（4 worker），同一 conversation 内严格按时间顺序。

#### 3.2.2 召回/答题/裁判

- 每题 `recall(question)` → top-50，mode=auto，rerank=true。
- 答题：qwen-plus-latest + 官方 `get_beam_answer_generation_prompt`（记忆按 created_at 旧→新）。
- 裁判：逐 rubric nugget 独立调用 qwen-plus-latest + 官方 `get_beam_nugget_judge_prompt`，解析 JSON score 并 clamp 到 {0,0.5,1.0}。
- 指标：与官方 runner 一致——每题 score=mean(nugget scores)，pass 阈值 0.5；报 overall accuracy/avg_score、10 类 question_type 分组、cutoff=50。
- event_ordering 的 Kendall tau-b 附加指标：v1 仅实现官方 nugget 主指标；tau-b 留到 v2（报告中显式注明），避免 v1 引入额外对齐调用放大裁判方差。

## 4. 数据隔离 review（agentID / 租户ID）

结论先行：**v1 使用“tenant_id 隔离 benchmark + user_id 隔离用户/会话”，不依赖 agent_id。**

- `/v1/memory/*` 五个 scope 字段（tenant_id/user_id/agent_id/run_id/session_id）任意子集写入，查询按**子集匹配**（mem0 语义，`mem0/context/scope.py` + recall 的 identity_filters）。
- 关键风险：若写入 `tenant_id+user_id`，查询只给 `tenant_id` 会命中该租户下所有 user。因此两个 runner 的 recall **永远回放与写入完全相同的 tenant_id+user_id 二元组**，并在结果 JSON 中记录 scope，防止混读。
- agent_id 本次仅作“审计位”测试：smoke 中同一 tenant 下 `agent_id=probe-a/probe-b` 各写一条互异事实，验证 `agent_id` 可再切一层；全量基准不引入 agent_id（避免与现有 LOCOMO 用户维度语义混淆）。
- 两基准使用不同 tenant：`bench-mempal-v1` vs `bench-beam-v1`；即便 user_id 撞名也不会交叉。
- smoke 断言（自动化）：
  1. mempal 用户 A 写入后，用用户 B 查询 A 的专属事实，召回为空；
  2. beam tenant 查询 mempal tenant 同 user_id，召回为空；
  3. 同租户同 user 再次查询能命中，确认不是全局故障；
  4. `/reset` 前记录两个 tenant 的 scope 数量，重置后再查为空。

## 5. 执行流程（顺序）

1. ✅ 重启 mem0 服务并确认 `/health/ready`（已在计划前完成）。
2. 跑中文时间/时区相关回归测试（服务容器内 pytest）。
3. 准备数据集：`input.json`（已下载 /tmp，复制进 benchmark dataset 缓存）、BEAM 100K parquet → JSON。
4. **few-shot smoke（先于全量，强制）**
   - cn-Mem-PAL smoke：user `0000` 的 2 个 history sample + 1 个 query sample；跑 1 个 topic 的 requirement restatement + solution selection；核对（a）隔离断言 4 条，（b）抽取出的中文日期星期自洽、时区为 Asia/Shanghai，（c）日志内相对时间锚到行内时间戳，对话相对时间锚到 Observation Date。
   - BEAM smoke：conversation 1 的前 1 个 batch、1 个 question_type 各 1 题；核对 ingest/recall/answer/judge 四阶段 JSON 与 checkpoint。
   - smoke 通过 review 后清空 smoke 数据（不污染全量，或使用独立 tenant `bench-smoke-*`）。
5. 全量 cn-Mem-PAL v1（ingest → recall → requirement answer+judge → solution answer+score）。
6. 全量 BEAM v1（ingest → recall → answer → nugget judge）。
7. 产出 `design/cn-mempal-beam-benchmark-report.md`（v1 报告：协议、隔离、指标、错误清单、日志观察、结论）。
8. 观察日志（见 §6），把有代表性的 `/v1/memory` 加工/存储/召回日志与异常摘进报告。

## 6. 日志观察 checklist（要求 5）

- `docker logs mem0-dev-mem0-1` 按时间段截取；关注：
  - remember：请求进入、scope/scope_key、extract 是否调用 LLM、created/updated/noop 计数、有无 422/503；
  - recall：mode auto 的 RRF 两路、rerank 状态、degraded 标记；
  - ES 错误、LLM 超时、重试、JWT 刷新；
  - 中文样例抽 2-3 条原始消息与抽取结果对照，验证日期/星期/时区正确。
- 结果文件记录：每次 remember 的 facts 数量、每次 recall 的 latency/result_count/rerank_status，供报告汇总。

## 7. 产物与落盘

| 类型 | 路径 |
|---|---|
| 计划 | `design/cn-mempal-beam-benchmark-plan.md`（本文） |
| runner | `server/scripts/benchmarks/mempal/run.py`、`server/scripts/benchmarks/beam/run.py`（stdlib + 五阶段 checkpoint/resume，风格同 LOCOMO runner） |
| 数据集缓存 | `server/scripts/benchmarks/{mempal,beam}/dataset/`（.gitignore，不提交） |
| 结果 | `server/scripts/benchmarks/{mempal,beam}/results/v1-*.json`（.gitignore） |
| 报告 | `design/cn-mempal-beam-benchmark-report.md` |

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 双时间锚点（日志行时间 vs Observation Date）LLM 不稳定 | smoke 中用构造/真实“昨天/上周”日志逐条核对；若不稳，回退为“含相对时间的日期单独成批”策略 |
| 2890 次中文长批次抽取超时/慢 | remember timeout=300s、用户并行 4；若 >50s/批，改为 logs 与 dialogue 两次调用或日期分桶 |
| judge JSON 解析失败 | 失败即重试 1 次；仍失败记为 error 并计入报告，不计正确 |
| BEAM 数据集格式差异 | 先用 parquet→JSON 标准化脚本 + smoke 校验 question_type/rubric |
| top_k 50 与官方 100/200 不同 | 报告显式标注 cutoff=50，不宣称与官方平台数值严格可比 |

## 9. Review 记录

- [x] 隔离 review：采用 tenant+user 双字段、召回回放同一 scope；agent_id 仅 smoke 验证，不用于全量（见 §4）。
- [x] smoke 结果 review（2026-08-16，cn-Mem-PAL 与 BEAM 均通过）：
  - 回归测试：容器内 pytest（temporal/vdb_kernel/router/memory/server_params）167+54 全过。
  - 隔离断言：`server/scripts/benchmarks/smoke_isolation.py` 6/6 PASS（user A/B、agent A/B、跨 tenant、user↔agent 不串）。
  - cn-Mem-PAL smoke（user 0000，3 samples，1 topic）：3 remember 全部 200，26 facts 中文日期与星期自洽；相对时间日志（`2024-01-15 16:45 …今天就不吃甜点`）被正确锚到日志行时间戳（`2024年1月15日（星期一）`）；recall hybrid+rerank 正常；requirement judge=2.0/2，solution selection 解析与打分正常。单 sample 摄入约 45-50s。
  - BEAM smoke（conv 1，batch 0 共 12 chunks，1 题）：12 remember 全部 200、94 facts；recall hybrid+rerank 正常；answer/judge 通过（information_extraction 1.0）。单 chunk 摄入约 14s。
  - 发现并修复：`--history-limit/--query-limit` 最初未作用于 ingest（smoke 误吞全量 sample）；已修复并改为逐 user 独立 progress 文件（避免大结果文件每 sample 全量落盘）。
- [x] 全量前置根因 review（2026-08-16）：首批全量 ingest 出现 30.7% sample 0 facts；逐层复现定位为 LLM `max_tokens=2000` 截断长 JSON（直接调用 DashScope 可稳定返回 47-48 条，服务路径解析一致），`/configure` 调至 8000 后 sample0 恢复 48 facts。已删除 `bench-mempal-v1` 污染数据（head/version/scope/event/dedup 各 11041/45 条）并从零重跑。
- [ ] 全量结果 review（待执行后回填）。
