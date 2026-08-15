# 记忆存储与召回收敛设计 v3（纯 VDB 权威版）

> 状态：待实施
>
> 适用范围：记忆加工、记忆持久化、记忆召回、存储模式配置、统一入口与兼容适配
>
> 配置默认值：`MEMORY_STORAGE_MODE=ONLY_VDB`
>
> 设计关系：本文替代 v2 与上一版 `memory-storage-mode-design.md`，并在基础记忆路径上优先于
> `design/powercontext-fusion-design.md`。
>
> **核心变更**：`ONLY_VDB` 模式下不创建、不依赖任何 `ctx_*` SQL 记忆表。记忆权威与检索都在
> Elasticsearch 中完成。`HYBRID_STORAGE` 只在 ES 之外增加最小 SQL 召回副本，用于故障兜底。
> 多租户仍不要求请求级授权；MCP 只要求服务级鉴权。

---

## 0. 设计判断与收敛原则

上一版 v2 把“SQL 权威 + ES 投影”作为默认路径，虽然实现简单，但无法满足
“`ONLY_VDB` 不建 SQL 记忆表”的要求。本版做以下取舍：

1. **默认路径以 ES 为唯一记忆权威**：不建 `ctx_*` 表，不需要 SQL 事务参与记忆写读。
2. **ES 权威复杂度必须收敛到最小协议**，不接受七类记录 + ScopeManifest + 双 Head + 租约
   的完整状态机。
3. **所有衍生数据都可从权威控制记录重建**：ES 内部没有跨文档事务，因此采用
   “一个 scope 控制文档作为线性化点 + 若干可重建文档”的模型。
4. **接口语义优先保持稳定**：`entry_id / entry_version_id / artifact_id / outcome /
   pending_embed / search_mode / matched_by` 等现有字段不破坏。
5. **SQL 只在 `HYBRID_STORAGE` 出现，且只做召回副本**，不作为第二权威，不影响写入成功。

本方案目标不是“在 ES 里复刻一个关系数据库”，而是只保留纯 VDB 权威**必要且充分**的
五类逻辑记录：`scope / head / version / event / dedup`。

---

## 1. 设计目标

基础记忆系统只提供两条主路径：

1. **写**：接收文本或消息，完成加工，持久化为统一记忆记录；
2. **读**：接收查询与 scope，按明确策略返回记忆结果。

`MEMORY_STORAGE_MODE` 控制 SQL 召回副本是否启用：

| 模式 | ES 权威 | ES 召回 | SQL 记忆表 | SQL 兜底召回 |
|---|---|---|---|---|
| `ONLY_VDB`（默认） | 是 | 是 | **不创建** | 否 |
| `HYBRID_STORAGE` | 是 | 是 | 仅创建召回副本表 | 是 |

`ONLY_VDB` 下记忆域完全不依赖 SQL。服务认证、Settings、审计等非记忆能力是否使用应用库
由部署自行决定，与记忆路径解耦。

**能力边界**：v3 首期只保证基础记忆路径（remember / recall / revise / retire / reactivate /
expand / changes / get）。当前仍依赖 SQL 的 Source Store、Handoff、Review Inbox 在
`ONLY_VDB` 下标记为不可用（capabilities=false，调用返回 501），后续单独迁移，不阻塞基础
记忆能力。

---

## 2. 总体架构

```
REST /v1/memory/*  ─┐
MCP tools（服务鉴权）─┼──> MemoryApplicationService
Legacy /memories,* ─┘            │
  LegacyAdapter                   │
                                  ├── WriteCoordinator ──────────────> ES 权威文档族
                                  │        scope / version / head / event / dedup
                                  ├── RecallCoordinator ─────────────> ES head 索引
                                  │        (kNN + BM25, 候选并集, RRF)
                                  ├── RecoveryReconciler ────────────> 修复未完成发布
                                  └── HYBRID_STORAGE only:
                                       FallbackReconciler ───────────> SQL 召回副本
                                       RecallCoordinator ──故障兜底──> SQL FTS
```

模块边界：

| 模块 | 职责 |
|---|---|
| `MemoryApplicationService` | 唯一领域入口：remember / recall / revise / retire / reactivate / expand / changes / get |
| `WriteCoordinator` | 执行 ES 发布协议、CAS、去重、幂等重试 |
| `RecallCoordinator` | 通道选择、候选并集、RRF、rerank、SQL 兜底、写后可见 |
| `ElasticsearchMemoryStore` | ES 文档族读写、refresh、错误分类 |
| `RecoveryReconciler` | 修复 CAS 成功但 head/event/dedup 未完成的派生写入 |
| `FallbackReconciler` | 仅 HYBRID：把 ES event 同步到 SQL 召回副本 |
| `LegacyAdapter` | 旧 `/memories`、`/search` 的协议转换，不承载存储语义 |

禁止项：

- 路由、MCP、Dashboard 不得直接调用 ES 或 SQL；
- Legacy 接口不得维护第二套写入/删除语义；
- `ONLY_VDB` 下任何记忆请求不得触碰 SQL。

---

## 3. 存储模式配置

```dotenv
# 默认：ES 权威 + ES 召回，不建 SQL 记忆表
MEMORY_STORAGE_MODE=ONLY_VDB

# ES 权威 + ES 召回 + SQL FTS 故障兜底
MEMORY_STORAGE_MODE=HYBRID_STORAGE
```

解析规则：

| 项目 | 规则 |
|---|---|
| 缺少变量 | `ONLY_VDB` |
| 大小写 | `strip().upper()` |
| 合法值 | `ONLY_VDB`、`HYBRID_STORAGE`；兼容历史拼写 `HYBRIRD_STORAGE`，告警一次 |
| 非法值 | 启动失败 |
| 生效时机 | 启动时读取，重启生效；禁止 `/configure` 热切换 |

主存储固定为 Elasticsearch 8.17 + analysis-ik。`VECTOR_STORE_PROVIDER` 当前只接受
`elasticsearch`，其他值启动失败。SQL 兜底使用应用库 MySQL 8 / PostgreSQL，不单独引入 DSN。

---

## 4. ES 权威模型

### 4.1 五类逻辑记录

| 逻辑记录 | 确定性 `_id` | 内容 | 用途 |
|---|---|---|---|
| `scope` | `s:{scope_key}` | `artifact_id`、`published_revision`、`last_event_type`、`last_entry_id`、`last_entry_version_id`、身份五列 | 单 scope 的 CAS 与发布水位 |
| `version` | `v:{scope_key}:{entry_id}:{version}` | 不可变内容、hash、refs、categories、`scope_revision` | `expand` 精确重读 |
| `head` | `h:{scope_key}:{entry_id}` | 当前版本指针、state、searchable_text、vector、embedding_status、身份五列 | 召回与 `get_all` |
| `event` | `e:{scope_key}:{revision}` | event_type、entry_id、entry_version_id、`scope_revision` | `/changes` 增量游标 |
| `dedup` | `d:{scope_key}:{dedup_key}` | status=`prepared|active|released`、entry_id、version_id | 同内容并发去重与 noop |

其中只有 `scope` 文档承担并发控制；其他四类都是可重建数据。这就是纯 VDB 权威的最小集合，
不引入 Operation 索引、Schema 索引、Published/Pending 双 Head、租约表。

`scope_key` 继续沿用现有语义：五个身份字段 JCS 序列化的 SHA-256。`entry_id`、
`entry_version_id` 为 UUID，`artifact_id` 首次创建 scope 时生成并存入 `scope` 文档。

### 4.2 物理索引

- `memory_scope`
- `memory_head`（`dense_vector`）
- `memory_version`
- `memory_event`
- `memory_dedup`

五个索引按部署共享，不按租户动态建索引。`tenant_id / user_id / agent_id / run_id /
session_id / scope_key / state / kind / entry_id / entry_version_id` 使用 `keyword`；
正文使用 `text`；`text_zh` 使用 ik_max_word/ik_smart；`metadata` 使用 `flattened`。
精确 scope 读写使用 `_routing=scope_key`；子集召回按身份字段过滤，不做路由。

---

## 5. 写入语义

### 5.1 统一命令

```python
class MemoryApplicationService:
    def remember(command: RememberCommand) -> RememberResult: ...
    def revise(command: ReviseCommand) -> RememberResult: ...
    def retire(scope, entry_id, *, reason=None, expected_revision=None) -> RememberResult: ...
    def reactivate(scope, entry_id, *, expected_revision=None) -> RememberResult: ...
    def recall(query: RecallQuery) -> RecallResult: ...
    def expand(citation) -> EntryVersionBody: ...
    def changes(scope, *, since_revision=0, limit=200, cursor=None) -> ChangesResult: ...
    def get(scope_or_entry_id, ...): ...   # Legacy/Export 适配
```

`RememberCommand`：

| mode | 输入 | 模型调用 | 结果 |
|---|---|---|---|
| `append` | `text` 必填 | 禁用 | 单候选，dedup 后 created/noop |
| `auto` | `text` 或 `messages` 二选一 | text→append；messages→extract | 与所选模式相同 |
| `extract` | `messages` 或 Source 窗口 | 启用 | 0..N 候选；未实现时 501，capabilities 如实为 false |

显式修订使用 `ReviseCommand`，目标 `entry_id` 只能由服务端在当前 scope 内解析，LLM 不得
生成或直写任何 ID。

### 5.2 发布协议（写路径的线性化点）

所有写操作遵循同一条协议：

1. **准备阶段**
   - 计算 `scope_key`、`content_hash`、`dedup_key`；
   - `create`/`revise` 先尝试 `op_type=create` 写 `dedup` 文档：
     - 成功：状态 `prepared`，记录 `entry_id / entry_version_id / dedup_key`；
     - 已存在且 `active`：返回 `noop`；
     - 已存在且 `prepared`：读取并继续完成发布（幂等恢复），或返回 409
       `operation_in_progress`；
   - 首次确保 `scope` 文档存在（`published_revision=0`）。

2. **读取 CAS 基线**
   - GET `scope` 文档，取得 `published_revision`、`_seq_no`、`_primary_term`；
   - 校验 `expected_revision`：显式传入且不一致时直接返回 409 `revision_conflict`。

3. **写不可变 version**
   - `version` 文档使用确定性 `_id`，upsert 写入完整内容与 `scope_revision=new_revision`；
   - 若本次是 retire/reactivate，不写 version。

4. **CAS 发布**
   - 条件更新 `scope` 文档：

     ```text
     if_seq_no = 基线 _seq_no
     if_primary_term = 基线 _primary_term
     published_revision = new_revision
     last_event_type = created | revised | retired | reactivated
     last_entry_id = ...
     last_entry_version_id = ...
     ```

   - 这是整个写操作的**唯一线性化点**。成功即领域发布；409 表示并发竞争：
     - 显式 `expected_revision`：返回客户端 409；
     - 未显式：有界重试，失败后返回 409。

5. **发布后派生写入（可重建）**
   - upsert `head`：指向新 version/state，`scope_revision=new_revision`；
   - create `event`：`e:{scope_key}:{new_revision}`；
   - 更新 `dedup`：`prepared -> active`，或 retire 时释放；
   - 以上写入完成后执行 `refresh=wait_for`。

6. **响应**
   - 全部完成：HTTP 200，`outcome=created|updated|noop`；
   - 若第 5 步在重试后仍失败：权威已经发布，返回 HTTP 503，
     `code=memory_published_repair_pending`，由 RecoveryReconciler 补齐；客户端重试同一命令
     会通过 dedup/state 幂等收敛。

关键点：

- **head 不会先于 CAS 可见**。旧 head 在 CAS 前保持原状，CAS 成功后才覆盖，避免 Pending
  Head 污染召回；
- **失败 CAS 不产生可见垃圾**：version 可复用，dedup 处于 prepared，head 未变化；
- **崩溃恢复只需看 scope 文档**：`last_event_type + last_entry_version_id` 足以重建 head、
  event 和 dedup，不需要租约与 Operation 日志。

### 5.3 去重与生命周期

- `dedup_key = SHA-256(scope_key, kind, content_hash)`；
- active dedup 命中 → `noop`，不推进 revision；
- retire 释放 dedup，因此同一事实退役后允许以新 `entry_id` 再次 remember；
- reactivate 重新获取 dedup；若相同内容已被其他 active entry 持有，返回 409；
- revise 改变内容时，先为**新** dedup_key 创建 prepared claim，发布成功后释放旧 claim；
- 同内容重复 revise 返回 `noop`。

### 5.4 RecoveryReconciler

周期执行，每 30s：

1. 扫描 `dedup.status=prepared` 且超过 TTL 的 claim；
2. 读取对应 `scope` 文档：
   - `published_revision >= claim.scope_revision` 且 head/event 完整 → 完成发布；
   - 否则回滚/保留 claim 并退避；
3. 扫描近期 `scope` 文档的 `last_*`，校验 head/event 是否存在且匹配，缺失则从 version 重建；
4. 清理已发布 scope 之外的孤儿 version/prepared claim。

多实例同时运行安全：所有修复动作都是确定性 `_id` upsert，幂等；scope 文档条件更新失败
则让出。

---

## 6. 召回语义

### 6.1 模式

| mode | 主通道 | 行为 |
|---|---|---|
| `auto` | ES kNN ∪ ES BM25 | 候选并集，RRF 融合；单通道不可用时使用另一通道 |
| `semantic` | ES kNN | Embedder 缺失 501；ES 运行故障 503，禁止 SQL 兜底 |
| `keyword` | ES BM25（text_zh 原文 + text_lemmatized token） | ES 运行故障时仅 HYBRID_STORAGE 允许 SQL FTS |

### 6.2 主存储召回

1. 每通道取 `candidate_limit = min(max(limit * 4, 50), 200)`；
2. 以 `entry_id` 去重后取**并集**：
   `candidates = semantic_hits ∪ keyword_hits`；
3. RRF 融合，`k=60`，语义/关键词等权；
4. `threshold` 只过滤语义通道原始相似度；`keyword` 模式携带 threshold 返回 422；
5. 实体只给已有候选标记 `matched_by=entity`，不改变基础排序；
6. `rerank=true` 只改变位次，失败保留 RRF 顺序并返回 `rerank_status=fallback`；
7. 每条结果标记 `matched_by ⊆ {semantic, keyword, entity, rerank, fts_sidecar}`。

`head` 索引里的文档就是已发布权威 Head，召回不需要逐条 join scope 或校验
`published_revision`。`state=inactive`、过期、`embedding_status=failed` 的文档由查询过滤。

### 6.3 SQL FTS 兜底（仅 HYBRID_STORAGE）

触发条件：

1. `MEMORY_STORAGE_MODE=HYBRID_STORAGE`；
2. `mode ∈ {auto, keyword}`；
3. ES 错误分类为 `UNAVAILABLE / TIMEOUT / THROTTLED`；
4. SQL FTS 探针健康。

禁止触发：ES 正常空结果、`semantic`、`ONLY_VDB`、ES 409/400/401/403/404、参数错误。

响应：

```yaml
search_mode: fts_sidecar
storage_source: sql_fts
degraded: true
as_of_revision: <检查点 revision>
```

SQL 副本是异步的，因此兜底结果必须携带检查点，声明“截至某 revision 的历史快照”，不得
宣称 ES 当前态。

### 6.4 写后可见

- 写成功前所有可见文档执行 `refresh=wait_for`；
- 若 publish 后派生写入失败并返回 503 `memory_published_repair_pending`，恢复完成后重试
  查询即可见；
- `embedding_status=pending` 的 head 没有向量，不能进入语义通道，但可进入关键词通道；
  EmbeddingReconciler 后续补齐向量。

---

## 7. 接口语义

### 7.1 现有字段保持稳定

- 继续使用 `entry_id / entry_version_id / artifact_id / outcome / pending_embed`；
- Recall 增加 `storage_source / degraded / degraded_channels / rerank_status`；
- `expand` 只凭 `citation` 即可定位 version，不需要额外 scope；`artifact_id` 可查 scope。

### 7.2 Legacy 映射

| Legacy | 目标 |
|---|---|
| `POST /memories` infer=false | `remember(mode=append)` |
| `POST /memories` infer=true | 兼容抽取引擎 → 逐条 `remember` |
| `GET /memories` | 查询 head，只返回 active |
| `POST /search` | `recall`，filters 拆为 scope + 元数据 |
| `PUT /memories/{id}` | `revise` |
| `DELETE /memories/{id}` | `retire`；`purge=true` 才物理删除 |

Legacy `memory_id` 与 `entry_id` 的映射通过 head/version 文档的 `legacy_ids` 字段保存，
由 Adapter 翻译，不侵入领域模型。

### 7.3 MCP

- 工具从 `MemoryApplicationService` 生成，不手写第二套参数；
- MCP 必须校验 Bearer / X-API-Key，复用 `verify_auth`；
- 默认不映射宿主机端口。

### 7.4 错误与状态码

| 错误 | HTTP | 触发 |
|---|---|---|
| `revision_conflict` | 409 | scope CAS 失败且调用方显式 expected_revision |
| `operation_in_progress` | 409 | 同 dedup 正在 prepared 且无法立即恢复 |
| `evidence_expired` | 410 | expand hash 校验失败 |
| `validation_error` | 422 | 参数/scope/文本非法 |
| `capability_not_supported` | 501 | extract/semantic 能力缺失 |
| `primary_unavailable` | 503 | ES 运行故障，ONLY_VDB 或 semantic |
| `memory_published_repair_pending` | 503 | CAS 已发布但 head/event 尚未补齐 |

### 7.5 ES 重试与 409 语义

- transport 显式配置：`max_retries=3`、`retry_on_status=(429,502,503,504)`、
  `retry_on_timeout=true`；
- `429/502/503/504/超时`：重试耗尽后分类 `THROTTLED/UNAVAILABLE/TIMEOUT`，仅这三类允许
  HYBRID SQL 兜底；
- **scope 文档条件更新的 409 是正常领域 CAS 竞争**，用于 `revision_conflict`；
- **dedup create 的 409 是正常去重流程**，用于 noop 或幂等恢复；
- 其他 ES 409（如非预期 version conflict）映射 `PRIMARY_CONFLICT`，不重试、不兜底，
  召回 503、写路径进入 Reconciler 退避。

### 7.6 第三方 REST API 清单

收敛原则：

1. 第三方系统只允许依赖 `/v1/memory/*`、`/v1/context/prepare`、`/v1/capabilities` 与
   `/health/ready`；OpenAPI 3.0 契约草稿见
   `design/openapi/context-v3.openapi.yaml`；
2. 领域命令统一 POST + JSON，scope 放在 body，**绝不进入 URL 或 query string**；
3. 无前缀的 `/memories`、`/search` 是兼容接口，继续保留行为但不作为新系统接入推荐；
4. 运维动作（rebuild/reconcile/backfill/迁移）从第三方 API 中分离，归属 admin namespace；
5. 错误统一返回 `{ code, message, request_id }`，HTTP 状态按 §7.4。

#### 7.6.1 第三方稳定 API

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/memory/remember` | 服务级 | 统一写入：append / auto / extract；extract 能力缺失返回 501 |
| POST | `/v1/memory/revise` | 服务级 | 显式修订指定 entry；Legacy `PUT /memories/{id}` 映射到同一命令 |
| POST | `/v1/memory/retire` | 服务级 | 逻辑失效，保留版本与审计 |
| POST | `/v1/memory/reactivate` | 服务级 | 重新激活 |
| POST | `/v1/memory/expand` | 服务级 | 按 `{artifact_id, entry_id, entry_version_id}` 精确重读并校验 hash |
| POST | `/v1/memory/recall` | 服务级 | 统一召回：auto/semantic/keyword，RRF，matched_by，故障降级语义 |
| POST | `/v1/memory/changes` | 服务级 | revision 游标读取变化事件；retire/reactivate 同样产生事件 |
| POST | `/v1/memory/get` | 服务级 | 按 `entry_id` 读当前 Head，供第三方点查 |
| POST | `/v1/context/prepare` | 服务级 | 把召回结果装配为带 citation 的预算化上下文 |
| GET  | `/v1/capabilities` | 服务级 | 能力与存储模式声明 |
| GET  | `/health/ready` | 公开 | 三态 readiness：ready/degraded/not_ready |

注：`/v1/memory/revise`、`/v1/memory/get` 在 v3 实现阶段补齐；在补齐前，对应能力只通过
Legacy 兼容接口提供。

#### 7.6.2 运维/管理 API（不对第三方开放）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/v1/admin/memory/reconcile` | admin | 触发 RecoveryReconciler / EmbeddingReconciler |
| POST | `/v1/admin/memory/rebuild` | admin | 从 version/event 重建 head |
| POST | `/v1/admin/memory/backfill` | admin | 旧向量数据导入 |
| POST | `/v1/admin/memory/migrate` | admin | SQL ctx → ES 权威迁移任务 |
| GET  | `/metrics` | admin/内网 | Prometheus 指标，禁止内容标签 |

当前实现中的 `/v1/memory/reconcile|backfill|rebuild` 迁移到上述 namespace，旧路径保留一个
版本并标记 deprecated。

#### 7.6.3 兼容 API（不推荐新接入）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/memories` | Legacy 写入，Adapter 转 `remember` |
| GET  | `/memories` | Legacy 列表，Adapter 查 head |
| GET  | `/memories/{memory_id}` | Legacy 点查 |
| PUT  | `/memories/{memory_id}` | Legacy 修订，Adapter 转 `revise` |
| DELETE | `/memories/{memory_id}` | Legacy 删除，默认转 `retire` |
| GET  | `/memories/{memory_id}/history` | 兼容返回 version 历史摘要 |
| POST | `/search` | Legacy 检索，Adapter 转 `recall` |

兼容 API 保持现有响应结构；其内部语义必须与 `/v1/memory/*` 完全一致，禁止维护第二套
存储行为。

#### 7.6.4 未迁移能力

`/v1/sources/content`、`/v1/handoff/*`、`/v1/artifact-candidates/*` 在 `ONLY_VDB`
首期仍依赖 SQL，Capabilities 返回 false，调用返回 501；迁移到 ES 后再重新开放为稳定 API。

---

## 8. HYBRID_STORAGE 的 SQL 边界

只创建两张记忆表：

```sql
memory_recall_head (
  scope_key, tenant_id, user_id, agent_id, run_id, session_id,
  entry_id, entry_version_id, state, text, searchable_text,
  scope_revision, updated_at,
  PRIMARY KEY (scope_key, entry_id)
)

memory_recall_checkpoint (
  scope_key PRIMARY KEY,
  published_revision,
  as_of_at,
  last_verified_at
)
```

- 不使用 `ctx_*` 表；
- FallbackReconciler 从 ES `event` 文档按 revision 顺序同步；
- 每个 scope 的 checkpoint 与 head 变更在同一个 SQL 事务中提交；
- 主 ES 正常时 SQL 只同步、不参与召回；
- SQL 写入失败不影响 ES 写成功。

`ONLY_VDB` 不创建、不连接、不检查这两张表。

---

## 9. 迁移

从当前“SQL ctx 权威 + 向量投影”迁移到“ES 权威”：

1. 迁移工具只读旧 `ctx_*` 与旧向量库，不修改旧表；
2. 按 `scope_key -> scope`、`entry_versions -> version`、`entry_heads -> head`、
   revision -> `event`、active hash -> `dedup` 的顺序重建 ES 文档族；
3. 未绑定 ctx 的 legacy 向量行以 `provenance=legacy_backfill` 合成初始 version/event/head；
4. 校验：scope 数、head 数、version 数、active dedup 数、抽样 expand 与 changes 对账；
5. 通过后部署切换到 `ONLY_VDB`，旧表只读保留一个发布周期，不自动删除；
6. `HYBRID_STORAGE` 从 ES event 重新构建 SQL 召回副本，不沿用旧 ctx Head 表。

---

## 10. Readiness 与 Capabilities

| 模式与依赖 | readiness | 写 | 召回 |
|---|---|---|---|
| ONLY_VDB，ES 正常 | ready | 正常 | ES |
| ONLY_VDB，ES 运行故障 | not_ready | 503 | 503 |
| HYBRID，ES 故障、SQL FTS 正常 | degraded | 503 | keyword/auto 走 SQL FTS；semantic 503 |
| HYBRID，ES/SQL 均故障 | not_ready | 503 | 503 |
| LLM/Embedder/Reranker 未配置 | 不影响 readiness | 非依赖命令正常 | auto 降级 |
| 配置但故障 | degraded | 非依赖命令正常 | auto 降级 |

Capabilities：

```yaml
storage:
  mode: only_vdb | hybrid_storage
  primary_provider: elasticsearch
  sql_fallback_enabled: boolean
memory:
  extraction: boolean
  semantic_search: boolean
  keyword_search: boolean
  embedding_profile: string|null
  lifecycle: true
```

`extraction`、`semantic_search`、`keyword_search`、`sql_fallback_enabled` 必须按实际实现和
探针返回，禁止只读配置文件。

---

## 11. 实施阶段

### 阶段一：ES 权威内核（1.5 周）

1. `ElasticsearchMemoryStore`：五类文档、索引 mapping、refresh、错误分类；
2. `WriteCoordinator` 发布协议、dedup、CAS、幂等恢复；
3. `RecallCoordinator`：候选并集、RRF、matched_by、写后可见；
4. `RecoveryReconciler` 与 `EmbeddingReconciler`；
5. 修复 ES `text_zh` 查询原文与 update 语义。

### 阶段二：接口收敛（1 周）

1. `MemoryApplicationService` 统一 REST/MCP/Legacy；
2. `/v1` 薄路由，MCP 服务鉴权，Legacy Adapter；
3. capabilities/readiness 按真实能力输出；
4. 故障注入：429/503/409、崩溃点恢复、正常空结果。

### 阶段三：迁移与 Hybrid（1 周）

1. SQL→ES 迁移工具与对账；
2. `HYBRID_STORAGE` 两张 SQL 副本表 + FallbackReconciler；
3. 切换演练与旧表保留策略。

---

## 12. 验收条件

1. `ONLY_VDB` 启动与运行不创建、不查询任何 SQL 记忆表；
2. 同内容并发 remember 只有一个 created，其余 noop；显式 expected_revision 竞争返回 409；
3. CAS 成功后任意步骤崩溃，RecoveryReconciler 均可在下一轮恢复，召回结果正确；
4. remember 成功后立即 recall 可见；关键词通道可召回 embedding_status=pending 的 head；
5. `auto` 包含关键词独占命中；无通道可用时返回 501/503，不返回空 200；
6. ONLY_VDB 下 ES 故障召回 503；HYBRID 下 auto/keyword 返回 `storage_source=sql_fts`
   且带 `as_of_revision`；正常空结果绝不兜底；
7. ES 409 按 §7.5 区分 scope CAS、dedup、内部冲突三种语义；
8. Legacy `/search` 不返回 inactive/superseded；MCP 未带凭证返回 401；
9. 迁移后抽样 expand/changes/recall 与旧数据对账一致。

---

## 附录 A：ES 索引建表语句（ES 8.17 + analysis-ik）

以下语句以默认前缀 `agentar_mem0` 为例，`dims` 必须与部署 Embedder 实际维度一致；
示例为 DashScope/Qwen 1024 维。若使用其他维度，替换所有 `dims` 值。

```http
PUT /agentar_mem0_scope_v1
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "1s"
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "scope_key":            { "type": "keyword" },
      "tenant_id":            { "type": "keyword" },
      "user_id":              { "type": "keyword" },
      "agent_id":             { "type": "keyword" },
      "run_id":               { "type": "keyword" },
      "session_id":           { "type": "keyword" },
      "artifact_id":          { "type": "keyword" },
      "published_revision":   { "type": "long" },
      "last_event_type":      { "type": "keyword" },
      "last_entry_id":        { "type": "keyword" },
      "last_entry_version_id": { "type": "keyword" },
      "created_at":           { "type": "date" },
      "updated_at":           { "type": "date" }
    }
  }
}

PUT /agentar_mem0_head_v1
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "1s"
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "scope_key":          { "type": "keyword" },
      "tenant_id":          { "type": "keyword" },
      "user_id":            { "type": "keyword" },
      "agent_id":           { "type": "keyword" },
      "run_id":             { "type": "keyword" },
      "session_id":         { "type": "keyword" },
      "entry_id":           { "type": "keyword" },
      "entry_version_id":   { "type": "keyword" },
      "version":            { "type": "long" },
      "kind":               { "type": "keyword" },
      "state":              { "type": "keyword" },
      "content_hash":       { "type": "keyword" },
      "scope_revision":     { "type": "long" },
      "text":               { "type": "text" },
      "searchable_text":    { "type": "text", "analyzer": "standard" },
      "text_zh": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },
      "categories":         { "type": "keyword" },
      "source_refs":        { "type": "keyword" },
      "artifact_refs":      { "type": "keyword" },
      "embedding_status":   { "type": "keyword" },
      "vector": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine"
      },
      "metadata":           { "type": "flattened" },
      "legacy_ids":         { "type": "keyword" },
      "expires_at":         { "type": "date" },
      "created_at":         { "type": "date" },
      "updated_at":         { "type": "date" }
    }
  }
}

PUT /agentar_mem0_version_v1
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "1s"
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "scope_key":          { "type": "keyword" },
      "tenant_id":          { "type": "keyword" },
      "user_id":            { "type": "keyword" },
      "agent_id":           { "type": "keyword" },
      "run_id":             { "type": "keyword" },
      "session_id":         { "type": "keyword" },
      "entry_id":           { "type": "keyword" },
      "entry_version_id":   { "type": "keyword" },
      "version":            { "type": "long" },
      "kind":               { "type": "keyword" },
      "content_hash":       { "type": "keyword" },
      "text":               { "type": "text" },
      "source_refs":        { "type": "keyword" },
      "artifact_refs":      { "type": "keyword" },
      "categories":         { "type": "keyword" },
      "scope_revision":     { "type": "long" },
      "provenance":         { "type": "keyword" },
      "created_at":         { "type": "date" }
    }
  }
}

PUT /agentar_mem0_event_v1
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "1s"
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "scope_key":          { "type": "keyword" },
      "scope_revision":     { "type": "long" },
      "event_type":         { "type": "keyword" },
      "entry_id":           { "type": "keyword" },
      "entry_version_id":   { "type": "keyword" },
      "content_hash":       { "type": "keyword" },
      "state_after":        { "type": "keyword" },
      "reason":             { "type": "keyword" },
      "created_at":         { "type": "date" }
    }
  }
}

PUT /agentar_mem0_dedup_v1
{
  "settings": {
    "index": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "1s"
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "scope_key":          { "type": "keyword" },
      "dedup_key":          { "type": "keyword" },
      "status":             { "type": "keyword" },
      "entry_id":           { "type": "keyword" },
      "entry_version_id":   { "type": "keyword" },
      "scope_revision":     { "type": "long" },
      "created_at":         { "type": "date" },
      "updated_at":         { "type": "date" }
    }
  }
}

POST /_aliases
{
  "actions": [
    { "add": { "index": "agentar_mem0_scope_v1",   "alias": "agentar_mem0_scope" } },
    { "add": { "index": "agentar_mem0_head_v1",    "alias": "agentar_mem0_head" } },
    { "add": { "index": "agentar_mem0_version_v1", "alias": "agentar_mem0_version" } },
    { "add": { "index": "agentar_mem0_event_v1",   "alias": "agentar_mem0_event" } },
    { "add": { "index": "agentar_mem0_dedup_v1",   "alias": "agentar_mem0_dedup" } }
  ]
}
```

注意事项：

1. `text_zh` 依赖 analysis-ik 插件；未安装时不要创建该字段，否则 mapping 创建失败。
2. `dims=1024` 必须与 Embedder 输出完全一致；不匹配会导致写入 400。
3. `number_of_replicas=0` 适合单节点/开发；生产至少 1，并相应调整 readiness。
4. `dynamic=strict` 防止身份/控制字段漂移；扩展业务字段只允许进入 `metadata`。
5. 服务运行只访问别名；维度/分词变更时新建 `_v2` 索引，重建校验后切换别名。

---

## 13. ADR 摘要

- **ADR-1 ES 为唯一记忆权威**：`ONLY_VDB` 不建 SQL 记忆表。
  后果：必须引入最小发布协议与 RecoveryReconciler，复杂度上移但集中在 WriteCoordinator。
- **ADR-2 scope 文档是唯一线性化点**：CAS 只发生在 scope 文档。
  后果：head/version/event/dedup 均可重建，不需要跨文档事务。
- **ADR-3 五类记录，不引入 Operation/Schema/双 Head**：保持纯 VDB 权威的最小集合。
  后果：命令幂等由内容 hash、确定性 ID、dedup 与状态检查提供。
- **ADR-4 head 后置发布**：CAS 成功后才覆盖 head。
  后果：召回永远只看到已发布 Head，读路径无需逐条校验 scope 水位。
- **ADR-5 SQL 只在 HYBRID 作为召回副本**：两张表 + checkpoint，异步、历史快照语义。
  后果：SQL 故障不影响 ES 写路径。
- **ADR-6 接口字段稳定**：继续 `entry_id/entry_version_id/artifact_id`，Legacy 用 Adapter
  映射。
- **ADR-7 拓扑启动期固定**：存储模式与 Provider 禁止热切换。
