# Agentar Memory（mem0 fork）自顶向下架构分析

> 基线：本仓库 `feature/memory-research` 工作区当前状态。
> 审计：由独立 subagent 对 `mem0/`、`server/`、`design/` 与上游 `mem0ai/mem0@main` 逐项核对（2026-08-16）。
> 文档性质：只读分析交付物，不含实施决策；实施决策以 `design/memory-storage-mode-design.md` 为准。

---

## 0. 定位与三份设计遗产

本仓库是 mem0ai/mem0 v3 基线的私有化 fork（origin=corneum fork，upstream_origin=mem0ai/mem0）。工作区并存三层架构遗产，读代码时务必区分：

1. **上游 mem0 v3 SDK 内核**：`mem0/memory/main.py`（已提交，含本地直改）。
2. **PowerContext 融合方案 v3.1**：`design/powercontext-fusion-design.md`（SQL ctx 权威 + ES 投影，部分实现：`mem0/context/power_memory.py`、`mem0/context/store.py`、alembic 007–009）。
3. **纯 VDB 权威 v3 方案（server 当前实际生效）**：`design/memory-storage-mode-design.md` + `mem0/context/vdb/`（未跟踪，ES 是记忆唯一权威）。

---

## 1. 自顶向下五层

```
接入层   REST /v1/memory/*（稳定契约，POST+JSON，scope 只入 body）
         Legacy /memories,/search（兼容面，Adapter 协议转换）
         MCP tools（FastMCP；内嵌投影或 mcp_standalone 独立服务；强制服务级鉴权）
         Dashboard/Review Inbox 页面、/health/ready 三态、/metrics
编排层   LegacyMemoryAdapter ──协议转换──▶ MemoryApplicationService（唯一领域入口）
           ├ WriteCoordinator      ES 发布协议 / CAS / 去重 / 幂等恢复
           ├ RecallCoordinator     通道选择 / 并集 / RRF / rerank / SQL 兜底
           ├ RecoveryReconciler    修复 CAS 成功但派生写入未完成
           ├ EmbeddingReconciler   补齐 pending 向量
           └ HybridSidecar        (仅 HYBRID) ES event → SQL 召回副本，手动触发同步
SDK 层    mem0.Memory / AsyncMemory（上游内核 + 本地直改）
          PowerMemory + ContextStore（旧 SQL ctx 权威子系统；v3 server 未启用）
          providers：LLM 24 / Embeddings 15 / VectorStore ~30 / Reranker 6
存储层    ES 8.17 五索引权威：scope / version / head / event / dedup
          app DB（MySQL8/PG）：auth / settings / request_log；HYBRID 加 2 张 SQL 召回副本表
          SDK 侧 SQLite：history / messages（每 session 滚动窗口 10 条）
          旧 ctx_* SQL 表族（alembic 007–009 仍可建表，但 v3 server 记忆路径不查询）
模型层    qwen-plus-latest / qwen3.7-embedding(1024d) / gte-rerank-v2
          三者均可缺席：LLM 缺席→不能 extract；embedder 缺席→auto 降级 keyword
观测层    Prometheus（agentar_mem_*，内容禁载）+ OTel + 三态 readiness（探针缓存）
```

关键纪律：
- 路由 / MCP / Dashboard 不得直触 ES 或 SQL；Legacy 不持有第二套存储语义。
- `server_state._build_memory()` 是唯一构造点，启动初始化与 `/configure` 热重建共用；`MEMORY_STORAGE_MODE` 与 `VECTOR_STORE_PROVIDER` 启动期固定，禁止热切换。
- `ONLY_VDB` 下任何记忆请求不触 SQL；`HYBRID_STORAGE` 的 SQL 只是召回副本，不是第二权威。

---

## 2. 记忆数据模型

### 2.1 SDK 层 MemoryItem 与向量 payload

`MemoryItem`（`mem0/configs/base.py`）：`id / memory / hash / metadata / score / created_at / updated_at`。

向量 payload 核心字段：`data`（正文）、`hash`（MD5 去重）、`text_lemmatized`（BM25）、`created_at`、`updated_at`、`attributed_to`。
本地新增 promoted 键：`tenant_id / session_id`（上游 OSS 无）+ `actor_id / role / expiration_date / categories`；其余折叠进 `metadata`。

### 2.2 Scope 身份模型

- 五元组：`tenant_id / user_id / agent_id / run_id / session_id`（均可空）。
- 检索语义：调用方提供的任意子集即可命中（`find_heads`/ES identity terms 只约束提供的列）。
- `scope_key = SHA-256("agentar:scope:v1\0" + JCS(写时出现的 id 集合))`：只做 CAS/去重定位键，不作检索键。
- `content_hash = SHA-256("agentar:entry-content:v1\0" + JCS(kind, text, source_refs, artifact_refs, categories))`：不含身份/版本字段，同内容重复 remember 天然 noop；文本上限 8 KiB。
- `dedup_key = SHA-256(scope_key, kind, content_hash)`。

### 2.3 ES 权威层五类逻辑记录（当前核心模型）

| 记录 | 确定性 `_id` | 要点 | 用途 |
|---|---|---|---|
| scope | `s:{scope_key}` | artifact_id、published_revision、last_event_type/entry_id/version_id、五列身份 | 唯一线性化点 |
| version | `v:{scope_key}:{entry_id}:{version}` | 不可变正文/hash/refs/categories/provenance | expand 精确重读 |
| head | `h:{scope_key}:{entry_id}` | 版本指针、state、searchable_text、vector、embedding_status | 召回/列表 |
| event | `e:{scope_key}:{revision}` | 事件类型、版本指针、scope_revision | changes 增量游标 |
| dedup | `d:{scope_key}:{dedup_key}` | status ∈ prepared/active/released | 并发去重与 noop |

- 只有 scope 承载并发控制；其余四类全部可从 scope + version 重建。
- 五个索引 `memory_*`（别名 `_v1`），`dynamic=strict`；精确读写 `_routing=scope_key`，子集召回按身份字段过滤。
- head mapping：`dense_vector`(cosine，dims=embedder 契约，默认 1024)、`text_zh`(ik_max_word 索引/ik_smart 查询，插件缺失自动不建)、`searchable_text`(standard)、`metadata`(flattened)。

### 2.4 生命周期

- version 永不 UPDATE；revise 追加 `version = previous + 1`。
- retire/reactivate 只翻 head.state，不产生内容版本；purge 才物理删除。
- 同 scope 同 content_hash 至多一个 active head；retire 释放 dedup。
- citation 三元组 `{artifact_id, entry_id, entry_version_id}`；expand 重算 hash，不一致 → 410。

---

## 3. 记忆加工（写路径）

### 3.1 SDK `Memory.add()`

`infer=False`：逐条原始写入（跳过 system，记录 role/actor_id），零 LLM。

`infer=True`（V3 八阶段，ADD-only）：

| 阶段 | 动作 |
|---|---|
| 0 | 取 SQLite 该 session_scope 最近 10 条消息 |
| 1 | 整段对话 embed → 向量库 top-10 相似记忆；UUID→整数映射（防 LLM 幻觉 ID） |
| 2 | 单次 LLM 抽取（ADDITIVE_EXTRACTION_PROMPT；agent 域加后缀；JSON memory[]）；失败抛 LLMError |
| 3 | embed_batch，失败逐条兜底 |
| 4/5 | MD5(text) 双重去重（存量 hash + 批内 seen） |
| 6 | 批量 insert（失败逐条兜底）+ SQLite history 批量 |
| 7 | spaCy 实体抽取 → 批量 embed → entity store 搜索（≥0.95）→ 新实体批量 insert / 旧实体追加 linked_memory_ids |
| 8 | 保存消息窗口，返回 ADD 事件 |

### 3.2 `/v1/memory/remember`

`append`（零 LLM，text→created/noop）/ `extract`（LLM 抽 facts，逐条独立发布）/ `auto`（text→append，messages→extract）。

信任边界：LLM 只产候选文本，不产 ID、不直写、不能 retire；ID 由服务端在 scope 内解析。experience/skill 候选走 Review Inbox（旧 SQL 实现），v3 存储模式下 501。

### 3.3 ES 发布协议（WriteCoordinator）

```
1 准备   create dedup(status=prepared)；active→noop；prepared→幂等接续恢复
2 基线   GET scope：published_revision + _seq_no + _primary_term；校验 expected_revision
3 版本   确定性 _id upsert 不可变 version
4 CAS    scope 条件更新（if_seq_no/if_primary_term），revision+1 —— 唯一线性化点
         409：显式 expected_revision→客户端 409；否则有界重试
5 派生   upsert head（CAS 成功后才可见）→ event → dedup prepared→active（revise 释放旧 claim）
         → refresh
6 响应   全成功 200；派生失败→503 memory_published_repair_pending（Reconciler 补）
```

- 失败 CAS 不产生可见垃圾；崩溃恢复只看 scope.last_*。
- **已实现约束（审计修正）**：`_refresh()` 只对 head 索引强制 `refresh=wait_for`；event/dedup 依赖索引 1s `refresh_interval`，存在秒级可见窗口。
- RecoveryReconciler / EmbeddingReconciler **没有后台定时器**，当前由 `POST /v1/admin/memory/reconcile` 手动触发（design 的 30s 周期尚未落地）。

---

## 4. 召回策略

### 4.1 SDK `Memory.search()`（加性融合）

- `mode ∈ auto/semantic/keyword`；embedder 缺失时 auto 落 keyword-only，semantic 硬错误。
- 预处理：拉丁走 spaCy lemma；CJK 走 Analyzer v1（unigram `uXXXX` + bigram `bXXXXYYYY`，token 严格 `[0-9a-z]`）。
- 语义超取 `max(4k,60)`；keyword_search（store 不支持则告警降级）；BM25 sigmoid 按查询长度自适应。
- 实体 boost：查询实体≤8 → entity store（相似度≥0.5）→ `sim×0.5×链接数阻尼`，取 max。
- 新近度（本地新增，issue #4956）：`0.5^(age_days/30)`，权重 0.25，无时间戳取 0.5；只影响排序。
- `score_and_rank`：`(semantic + bm25 + entity + recency)/max_possible`（自适应上限：基础 1.25，BM25 +1，实体 +0.5）；threshold 只门控语义原始分。
- 候选集：语义通道激活时来自语义池（与上游一致）；keyword-only 模式才用 BM25 池。每条带 `matched_by`。

### 4.2 `/v1/memory/recall`（并集 + RRF）

- 每通道 `candidate_limit = min(max(limit×4,50), 200)`；候选 = semantic ∪ keyword（keyword 独占命中可进入）。
- RRF：`score = Σ 1/(60 + rank)`，k=60，等权。
- threshold 只门控语义原始相似度；keyword+threshold → 422。
- 实体只给已有候选打 `matched_by=entity`，不改基础排序；rerank 只改位次，失败回退 RRF 序并标 `rerank_status=fallback`。
- SQL FTS 兜底（仅 HYBRID）：ES 错误分类 ∈ {UNAVAILABLE, TIMEOUT, THROTTLED} 且 SQL 探针健康、mode∈{auto,keyword}；正常空结果绝不兜底；兜底结果带 `as_of_revision` + `degraded:true`。

### 4.3 写后可见 / PreparedContext

- 写成功前 head 强制 refresh；`embedding_status=pending` 的 head 可进 keyword 通道。
- `/v1/context/prepare`：memory≤8 + experience≤2；单条≤2000B；预算 512B–32KB（默认 8000B）；trust prefix + BEGIN/END JSON 信封（`agentar.prepared-context.v1`）；二分字节截断；每条带 citation。

---

## 5. 存储策略

### 5.1 SDK 层

- VectorStoreFactory ~30 家；抽象接口 + 可选 `keyword_search`（不支持则混合检索降级纯语义）。
- 实体库 = 同介质第二 collection（懒加载）。
- SQLite：history（变更）+ messages（滚动窗口）。

### 5.2 Server 层

| 模式 | ES 权威 | ES 召回 | SQL 记忆表 | SQL 兜底 |
|---|---|---|---|---|
| ONLY_VDB（默认） | ✅ | ✅ | 不创建 | 否 |
| HYBRID_STORAGE | ✅ | ✅ | 2 张副本表 + checkpoint | 是 |

- HYBRID 副本：`memory_recall_head` + `memory_recall_checkpoint`；由 `HybridSidecar.sync_all/sync_scope` 从 ES event 同步（**当前 admin 手动触发，无周期调度/积压告警**）；SQL 写失败不影响 ES 写成功。
- app DB 仍承担 auth/settings/request_log；alembic 007–009 执行时仍会创建旧 ctx_* 表（运行时约定不查询，迁移未裁剪）。
- 一致性：scope CAS 线性化 + 派生可重建 + `POST /v1/admin/memory/{reconcile,rebuild,backfill,migrate}` 运维面（旧 `/v1/memory/reconcile|rebuild|backfill` 别名保留并 deprecated）。
- Readiness：blocking = app_db + ES；non-blocking = llm/embedder/reranker/sql_fts；探针 2s 超时、成功缓存 300s、失败缓存 30s。
- `/v1/capabilities` 按真实探针输出。

---

## 6. 与上游 mem0 对比

| 维度 | 上游 mem0（main） | 本 fork |
|---|---|---|
| 写加工 | v3 ADD-only 单次抽取八阶段；Platform 异步 PENDING/event/webhook | SDK 同源；/v1 增加 append/extract/auto + ES 发布协议（CAS、不可变版本、dedup claim） |
| 抽取引擎 | 单次 ADDITIVE_EXTRACTION_PROMPT | SDK 相同；/v1 extract 用 FACT_RETRIEVAL_PROMPT（两套并存） |
| scope | **OSS SDK：user/agent/run 三元**；Platform 文档：user/agent/app/run 四维 + implicit null scoping | 五元组 + 任意子集命中语义 |
| 存储 | 可插拔向量库 + entity store + SQLite history | SDK 同；server 以 ES 五文档为唯一权威 |
| 召回 | 语义池候选 + BM25/实体加性融合；无 recency/mode/matched_by | SDK 加 recency/mode/matched_by/embedder-optional/CJK；/v1 用并集+RRF |
| 实体/图谱 | v3 移除 OSS graph，spaCy 实体链接为第三信号 | 相同 |
| 生命周期 | ADD-only + 显式 update/delete（物理为主） | 不可变版本链 + retire/reactivate/purge + changes + expand 哈希复核 |
| 一致性/审计 | 向量库即事实源；SQLite history | scope CAS + 可重建派生 + 三类 reconciler + 审计导出 |
| 中文 | 英文 spaCy（中文退化为整句） | ES ik（text_zh）+ 跨介质 Analyzer v1 |
| 接入/观测 | 托管 API + SDK | REST /v1 + Legacy + MCP + capabilities/readiness/Prometheus/OTel |

上游 v3 公开基线（[官方架构文档](https://raw.githubusercontent.com/mem0ai/mem0/main/skills/mem0/references/architecture.md)、[v2→v3 迁移指南](https://docs.mem0.ai/migration/oss-v2-to-v3)）：LoCoMo 71.4→91.6、LongMemEval 67.8→93.4、抽取延迟约减半、混合检索典型 100–150ms。早期 v2 为两阶段写路径（抽取 + ADD/UPDATE/DELETE/no-op 决策），v3 已废弃 update 决策。

---

## 7. 已知风险 / 待办（审计确认）

1. `mem0/context/vdb/`、`server/legacy_adapter.py`、`design/memory-storage-mode-design.md` 尚未提交固化。
2. 无后台 reconcile / Hybrid 同步调度：design 的周期语义未实现，依赖 admin 手动触发。
3. 三套记忆子系统并存；server 只用 MemoryApplicationService。
4. Legacy 与 SDK 抽取引擎不同（FACT_RETRIEVAL vs ADDITIVE）。
5. Source Store / Handoff / Review Inbox API 在 ONLY_VDB 下 501（`/review-inbox` HTML 页面本身 200）。
6. alembic 007–009 旧 ctx 迁移未裁剪；执行迁移仍会建旧表。
7. ES 仅 head 强制 refresh，event/dedup 有 1s 可见窗口。
8. 与上游 scope 语义漂移（任意子集 vs implicit null scoping），cherry-pick 上游检索补丁需回归。

---

## 8. 参考

- 仓库内：`design/memory-storage-mode-design.md`（当前实施依据）、`design/powercontext-fusion-design.md`（v3.1 融合方案）、`mem0/context/vdb/`、`server/`。
- 上游：[mem0ai/mem0](https://github.com/mem0ai/mem0)、[Platform Architecture](https://raw.githubusercontent.com/mem0ai/mem0/main/skills/mem0/references/architecture.md)、[OSS v2→v3 迁移](https://docs.mem0.ai/migration/oss-v2-to-v3)、[issue #4956](https://github.com/mem0ai/mem0/issues/4956)。
