# LOCOMO 抽取质量提升方案：相对时间归一化 + 信息抽取密度

> 基线：`design/locomo-benchmark-report.md`（2026-08-16，总体 33.3%，temporal 16.5%）
> 目标：提升 temporal 准确率与 fact 抽取密度，缩小与官方 92.5% / temporal 92.0% 的差距。

## 1. 证据与根因

### 1.1 本仓库基线暴露的两个直接根因

- **相对时间未归一化**：抽取 LLM 把 `last week / this month / yesterday` 原样写入 fact；
  输入里的 session 绝对日期只是“内容装饰”，抽取 prompt 没有把日期设为时间锚点的规则。
  58.3% temporal 题连“有答案”都达不到。
- **抽取密度不足**：5,882 轮只产出 2,153 条 fact（约 37%），39% 题目答案为“无信息”。
  当前 prompt 是 `USER_MEMORY_EXTRACTION_PROMPT`：惩罚 assistant 消息、few-shot 密度弱、
  无“逐 topic 穷举”约束；且 v3 extract 路径没有 observation timestamp。

### 1.2 官方实现与实验对照

| 证据 | 结论 |
|---|---|
| `evaluation/benchmarks/locomo/run.py`：`CHUNK_SIZE = 1` | 官方按轮抽取，而不是 25 轮一批 |
| 官方 runner：`mem0.add(messages, user_id, timestamp=session_epoch)` | 官方抽取有 observation timestamp |
| `evaluation/README.md`：LOCOMO top-200 92.5%，temporal 92.0%；LongMemEval temporal 97.0% | 官方平台管线的目标质量 |
| `mem0/configs/prompts.py`：`ADDITIVE_EXTRACTION_PROMPT` + `generate_additive_extraction_prompt()` | 时间归一化与密度规则已经 port 到仓库 |
| `mem0/memory/main.py` `_add_to_vector_store`：已使用 ADDITIVE prompt | v3 `context/vdb/service.py` 尚未对齐 |
| Mem0 论文 arXiv:2504.19413 | 写路径 = 按交互单元抽取候选 fact + 检索相似记忆 + ADD/UPDATE/DELETE/NOOP |
| LOCOMO 论文 arXiv:2402.17753 | temporal QA 依赖事件时间线 grounding；检索错误会主动伤害答案 |
| LongMemEval arXiv:2410.10813 | temporal-reasoning 是长期记忆的核心题型之一 |

### 1.3 旧 prompt 的具体缺陷

1. `USER_MEMORY_EXTRACTION_PROMPT` 明确“只提取 user 消息，惩罚 assistant/system”，
   LOCOMO 第二说话人被映射到 assistant 时一半信息被丢弃（baseline harness 被迫双 user 规避）。
2. `Today's date is {datetime.now()}` 在模块 import 时求值，且是“今天”而非“对话发生日”，
   与 LOCOMO 的 2023 会话日期语义相反。
3. 输出 `{"facts": ["..."]}`，无 `attributed_to`、无自包含事实约束，主语和日期常常丢失。
4. 没有穷举清单：长 batch 中 LLM 常见“首个 topic 主导”，后面 topic 丢失。

## 2. 方案设计

### 2.1 抽取 prompt 切换到 ADDITIVE 管线（核心改动）

`mem0/context/vdb/service.py::_remember_extract` 不再调用
`get_fact_retrieval_messages(transcript)`，改为：

```python
system_prompt = ADDITIVE_EXTRACTION_PROMPT
if agent-only scope:
    system_prompt += AGENT_CONTEXT_SUFFIX

user_prompt = generate_additive_extraction_prompt(
    new_messages=messages,                # 保留 role，双说话人各自归属
    timestamp=observation_date,           # 相对时间锚点
    custom_instructions=custom_instructions or prompt_override,
    use_input_language=True,
)
```

理由：

- ADDITIVE prompt 自带 `Observation Date` 归一化规则（yesterday/last week/next
  month/recently 必须换算为绝对日期），直接针对 temporal 16.5% 的根因。
- 自带 “exhaustive extraction checklist / 5-15 memories for 10+ messages /
  first-topic dominance 是错误” 的密度规则，直接针对 fact 密度 37% 的根因。
- 同时从 user/assistant（含命名多说话人）抽取，允许 harness 恢复官方角色映射。
- `server_state.apply_category_instructions()` 生成的 instructions 本来就按
  `{"id","text","categories"}` memory object 格式书写，与 ADDITIVE 输出格式一致。

### 2.2 timestamp 贯通（对齐官方 runner）

- `RememberRequest` 增加 `timestamp`（ISO string / epoch int / epoch float）。
- service 将 timestamp 归一化为 `YYYY-MM-DD` observation date；
  缺失时回退 metadata 的 `session_date` / `observation_date` / `timestamp` /
  `created_at`（兼容现有 harness 与 LOCOMO `"1:56 pm on 8 May, 2023"` 格式）。
- `prompt` 字段同步补上：service 已支持 override，但 HTTP 层没有暴露。

### 2.3 解析器增强

`parse_extraction_facts` 需要同时支持新旧格式：

- `{"memory": [{"id":"0","text":"..."}]}`（ADDITIVE 输出）
- `{"facts": ["..."]}`（旧 prompt 输出，兼容既有调用方）
- 顶层 list、`{"text"|"memory"|"fact"}` 对象
- ```json 代码围栏 / `<think>` 标签
- 批内语义去重（保留首个，防重复写入）

### 2.4 benchmark harness 对齐官方口径

- 每批请求携带 `timestamp = "YYYY-MM-DD"`（session 绝对日期）。
- `build_messages` 恢复官方角色映射：`speaker_a → user`，`speaker_b → assistant`。
- 默认 `--chunk-size` 25 → **5**（密度优先；`--chunk-size 1` 保留官方同参消融）。
- 新增 `--no-inline-session-date`：抽取输入不再把日期织入 content，
  用于区分“prompt 时间锚点”与“content 日期”的贡献。

## 3. 成本与风险

| 项 | 评估 |
|---|---|
| prompt 长度 | ADDITIVE prompt 约 33KB，qwen-plus 128k 上下文可承受；输入 token 增加约 4-5x |
| chunk 5 | 调用量约为基线 5 倍，比 chunk 1 的 17 倍温和；质量收益是主要换取 |
| 兼容性 | 解析器继续接受 facts 格式；v3 写协议不变；append 路径不感知 timestamp |
| 模型差异 | 小模型可能不服从 33KB prompt；后续可把 prompt 选择做成配置，本次不做新配置项 |
| API | `/v1/memory/remember` 新增可选字段，向后兼容；OpenAPI 同步更新 |

## 4. Review 记录

### Review 1（方案自审）

- 问题：直接套用 ADDITIVE prompt 是否过重？→ 结论：接受。官方平台同款 prompt 支撑
  LOCOMO 92.5% / temporal 92.0%，且本仓库已维护，避免再造 prompt。
- 问题：harness 曾把 speaker_b 双 user 作为“信息保真修正”，恢复 assistant 是否会回退？
  → 结论：不会。旧 prompt 惩罚 assistant 才需要双 user；ADDITIVE prompt 的
  Example 12 明确要求抽取 assistant 角色里真实说话人的个人信息，并禁止 echo。
- 问题：timestamp 放哪一层？→ 结论：作为 `remember` 显式参数进 service；metadata
  只做回退，不发明隐式契约。
- 问题：显式 timestamp 非法时是否静默回退？→ 结论：否。显式参数非法返回 422，
  只有 metadata 回退失败才静默用当前日期，避免脏数据悄悄污染时间语义。

### Review 2（兼容与契约）

- `parse_extraction_facts` 必须保留 `facts` 兼容，旧测试/旧调用方不得破坏。
- `response_format={"type": "json_object"}` 的转发语义不能变（回归测试保留）。
- router 必须把 `prompt` 和 `timestamp` 都传给 service；service 已支持 prompt 但
  router 漏传，属于既有 API 断点。
- OpenAPI 3.0.3：`timestamp` 用 `nullable + oneOf(string/integer/number)`。
- 不改 `mem0/memory/main.py`（OSS SDK 的 timestamp 仍是 platform-only 语义），
  只改 v3 context service 与 server 暴露面。

### Review 3（测试与验收）

- 先红后绿：解析器 memory 格式、prompt 切换、timestamp 归一化、router 透传。
- 基线相关套件：`tests/context/test_vdb_kernel.py` + `tests/test_context_router.py`。
- lint：ruff 120。
- benchmark 冒烟（不重置线上数据）：`--samples 1 --sessions 1 --no-answer` 级别。

## 5. 验证命令

```bash
.venv-p0/bin/python -m pytest tests/context/test_vdb_kernel.py tests/test_context_router.py -q
.venv-p0/bin/ruff check mem0/context/vdb/service.py mem0/context/models.py server/routers/context_router.py server/scripts/benchmarks/locomo/run.py
```

## 6. 实施结果与 Review 4（实现后自审）

### 6.1 改动清单

- `mem0/context/vdb/service.py`：ADDITIVE prompt 接入、timestamp 解析与 metadata
  回退、`parse_extraction_facts` 支持 memory/facts 双格式 + 代码围栏 + 批内去重。
- `mem0/context/models.py` / `server/routers/context_router.py`：`timestamp`、
  `prompt` 字段贯通 HTTP → service（修复 prompt 既有断点）。
- `server/scripts/benchmarks/locomo/run.py`：官方角色映射、per-batch timestamp、
  默认 chunk 5、`--no-inline-session-date` 消融开关。
- `design/openapi/context-v3.openapi.yaml`：请求 schema 同步。
- `tests/context/test_vdb_kernel.py`、`tests/test_context_router.py`：先红后绿的
  回归测试。

### 6.2 验证结果

- 相关套件 238 passed（context + router + legacy + prompts + memory_utils）。
- ruff 全部通过；`git diff --check` 干净。
- 直连 qwen-plus-latest 对新 prompt 做单次 LLM 冒烟（observation date =
  2023-05-08，输入 "last week / this month / on Saturday"）：
  - `last week` → `around May 1-2, 2023`
  - `this month` → `May 2023`
  - `on Saturday` → `Saturday, May 6, 2023`
  输出为 `{"memory": [...]}`，与解析器匹配。

### 6.3 待跑消融（不伪造数字）

完整 LOCOMO 10 会话需按 `design/locomo-benchmark-report.md` §9 命令重跑；建议顺序：

1. `--chunk-size 5`（新默认，预期密度↑ / temporal↑）
2. `--chunk-size 1`（官方同参）
3. `--no-inline-session-date`（区分 prompt 时间锚点与 content 日期的贡献）
4. `--no-rerank` / recall-mode 消融（与抽取改进正交）

