# 中文相对时间 + 多时区抽取方案（实现与验证）

> 基线：`feature/memory-research`，v3 `/v1/memory/remember` 抽取链路。
> 背景：LOCOMO/LongMemEval/BEAM 均只有英文语料；中文相对时间（上周3 7点、前天下午、3天后）此前只依赖 LLM 自行计算，无中文 few-shot、无星期一致性规则、无时区锚点。

## 1. 结论

- 写入抽取：v3 服务链路已支持中文相对时间归一化，并且新增 `timezone` 参数（默认系统本地时区）。
- 中文 few-shot 由**实际 Observation Date 动态计算**，不会因固定示例日期与真实输入相同而被模型复制。
- 提示词强制输出 `YYYY年M月D日（星期X）`，并要求日期与星期自洽；“上周X”定义为上一个自然周（周一开始）。
- OSS SDK `Memory.add` / `AsyncMemory.add` 同样新增 `timezone`（`timestamp` 平台参数仍按既有 contract 报错）。
- 查询端 `reference_date` / 结构化 event_date 过滤仍不在本次范围，见 §6 后续工作。

## 2. 设计

### 2.1 时区解析（`mem0/utils/temporal.py`）

- `None` / `system` / `local` → `datetime.now().astimezone().tzinfo`（默认当前系统时区）。
- 支持 IANA（`Asia/Shanghai`）、固定偏移（`+08:00`、`-0530`）、`UTC/GMT`。
- 无效显式时区抛 `ValueError`，API 层转 422，绝不静默回退到错误时区。
- timestamp 解析接受 ISO-8601、epoch 秒/毫秒、LOCOMO 风格命名日期（`1:56 pm on 8 May, 2023`）；naive 字符串按调用方指定的时区解释。

### 2.2 提示词（`mem0/configs/prompts.py`）

Observation/Current Date 段现在形如：

```text
## Observation Date
2026-08-16 (星期日) — Asia/Shanghai (+08:00)
All relative time expressions ... computed against this local calendar date.
```

- 系统 prompt 增加中英文相对时间规则与星期自洽要求。
- CJK 输入 + `use_input_language=True` 时，注入动态中文 few-shot：
  - `上周三 7 点…` → `2026年8月5日（星期三）7点（上午/下午未说明）…`
  - `前天下午…` → 实际观察日前 2 天 + 正确星期
  - `3天后…` → 实际观察日后 3 天 + 正确星期
- few-shot 日期由 Python 根据真实 Observation Date 计算，不是写死的字符串。

### 2.3 上游接口

| 接口 | 字段 | 默认 |
|---|---|---|
| `POST /v1/memory/remember` | `timezone` | 系统本地时区 |
| `POST /memories`（legacy） | `timezone` | 系统本地时区 |
| `Memory.add` / `AsyncMemory.add` | `timezone` | 系统本地时区 |
| `metadata.timezone` / `time_zone` / `tz` | 服务端 fallback | — |

### 2.4 中文星期与“上周”规则

- `weekday()`：0=星期一 … 6=星期日。
- `上周X` = 上一个自然周（周一为一周开始）的星期 X。
- 时间点不推断上午/下午：原文 `7点` 输出 `7点（上午/下午未说明）`。

## 3. 验证

### 3.1 单元/接口测试

- `tests/utils/test_temporal.py`：时区解析、epoch 跨日边界、中文星期、CJK few-shot 注入、英文输入不注入。
- `tests/context/test_vdb_kernel.py`：service 将 `timezone` 与 metadata fallback 注入 prompt；显式无效时区 422。
- `tests/test_context_router.py`：`/v1/memory/remember` 透传 `timezone`。
- `tests/test_server_params.py`：legacy `/memories` 透传 `timezone`、OpenAPI schema 包含字段。
- `tests/memory/test_main.py`：`Memory.add` / `AsyncMemory.add` 将 `timezone` 传给抽取管线。

### 3.2 真实 LLM 边界验证（qwen-plus-latest + ES）

timestamp=`2026-08-16T20:30:00+00:00`，消息=`前天下午我在公司开了一个重要会议。`

| timezone | Observation Date | 抽取结果 |
|---|---|---|
| `UTC` | 2026-08-16（星期日） | `2026年8月14日（星期五）下午…` |
| `Asia/Shanghai` | 2026-08-17（星期一） | `2026年8月15日（星期六）下午…` |

同一条消息、同一 epoch，两个时区得到两个正确且不同的本地日期。

### 3.3 中文评测集端到端

- 数据集：`shiliu-memory/longmemeval-cn`（[HuggingFace](https://huggingface.co/datasets/shiliu-memory/longmemeval-cn)）提供的 500 题中文问题/参考答案；会话语料来自官方 `xiaowu0162/longmemeval-cleaned` 的真实 LongMemEval sessions，经 qwen-plus-latest 翻译为中文。
- 抽样 temporal-reasoning 题，会话按原始 session timestamp 以 `Asia/Shanghai` 时区逐 session 摄入。
- 召回 → qwen-plus-latest 生成答案 → 与中文参考答案比对。
- 完整脚本与结果见 `server/scripts/e2e_zh_temporal_smoke.py`（待本轮实现）。

## 4. 兼容性

- 未传 `timezone` 的所有旧调用路径仍使用系统本地时区，日期展示新增 `(weekday)` 与 `— Zone`。
- 英文输入不注入中文 few-shot。
- `timestamp` 平台参数 stub 行为保持不变。

## 5. 已发现并修正的实现坑

1. 内部参数名 `timezone` 遮蔽 `datetime.timezone` 模块 → 统一使用 `timezone_spec`。
2. 固定日期中文 few-shot 会被模型照抄（输入恰好与示例相同） → 改为动态计算示例日期。

## 6. 后续工作（不在本轮）

- 查询端 `reference_date`：把 `上周3 7点` 查询解析为绝对日期区间。
- 结构化 `event_date` 写入 ES date 字段并支持 range 过滤。
- 中文 `dateparser` 规则引擎 + 抽取后日期/星期校验。
- 500 题完整中文 LongMemEval 回归流水线。
