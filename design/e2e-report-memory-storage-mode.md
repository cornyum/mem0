# 记忆存储模式 v3（纯 VDB 权威版）端到端测试报告

> 验收对象：`design/memory-storage-mode-design.md` v3
> 代码基线：`feature/memory-research` @ `01841ad2`（已推送 origin）
> 报告时间：2026-08-16 08:57 CST（末次全量复跑）

---

## 1. 结论

| 验证维度 | 结果 |
|---|---|
| ONLY_VDB 模式 e2e（§12 验收 1–8 + 扩展项） | **36/36 通过** |
| HYBRID_STORAGE 模式 e2e（§12 验收 2–8 + §6.3/§10） | **35/35 通过** |
| SQL ctx → ES 迁移 + 对账（§9/§12-9） | **通过**（45 scope / 1133 version / 1132 head，采样 hash 全过） |
| 单元/路由/故障注入测试套件 | **825 通过 / 3 失败（存量无关）/ 23 跳过** |
| Code review | 内核 14 项 + server 层 22 项发现，**全部修复并回归** |
| ruff lint（120 列） | **全绿** |

**设计 §12 的 9 条验收条件在真实环境全部实测通过。**

---

## 2. 测试环境（全部真实组件，无 mock）

| 组件 | 版本/配置 | 说明 |
|---|---|---|
| Elasticsearch | **8.17**（`agentar_mem0` 前缀五索引 + analysis-ik） | `mem0-dev-elasticsearch-1`，单节点 |
| MySQL | **8.0.46**（应用库 `mem0_app`，表前缀 `agentar_mem_app_`） | `mem0-dev-mysql-1` |
| LLM | DashScope `qwen-plus-latest` | 抽取（infer=true）真实调用 |
| Embedding | DashScope `qwen3.7-text-embedding`（1024 维） | 写入内联嵌入 + Reconciler 回填，真实调用 |
| Rerank | DashScope `qwen3-vl-rerank` | 配置就绪，rerank 通道真实可用 |
| 旧数据源 | PostgreSQL `mem0_app`（ctx 表族：45 binding / 1133 version / 1132 head / 45 memory_head） | 迁移验证输入，只读 |

ES 数据按要求**从 0 开始**：测试前删除旧投影索引 `agentar_mem0`（692 doc），五类权威索引由服务启动自动创建；MySQL 为全新实例（alembic v3 守卫下仅创建应用表）。

**当前权威库存量**（迁移数据 + e2e 全程写入）：
`scope 110 / head 1250 / version 1251 / event 1277 / dedup 1243`，readiness=`ready`，capabilities：`only_vdb + elasticsearch，extraction/semantic/keyword/lifecycle/prepare 全部真实探针=true`。

---

## 3. 验收条件逐项结果（§12）

| # | 验收条件 | 实测方法 | 结果 |
|---|---|---|---|
| 1 | ONLY_VDB 启动与运行不创建、不查询任何 SQL 记忆表 | SHOW TABLES 全量比对：无 `ctx_*`、无 `memory_recall_*`（仅 users/api_keys/settings/request_logs/alembic 等应用表）；alembic 007/008/009 加 v3 守卫 | ✓ |
| 2 | 同内容并发 remember 只有一个 created，其余 noop；显式 expected_revision 竞争 409 | 8 线程并发相同内容：**恰好 1 created**，其余 200-noop/瞬时 409 后重放收敛 noop；过期 expected_revision → 409 `revision_conflict` | ✓ |
| 3 | CAS 成功后任意步骤崩溃，RecoveryReconciler 下一轮恢复，召回正确 | 直接删 ES head 文档（模拟派生写入丢失）→ 召回确认不可见 → admin reconcile → head 从 version 重建 → get/recall 恢复正确；另有 30s 周期后台线程自动修复实测 | ✓ |
| 4 | remember 成功后立即 recall 可见；关键词通道可召回 embedding_status=pending 的 head | 真实 embedder 内联嵌入（pending_embed=false）→ 立即 hybrid 召回命中；人为置 pending 后：semantic 通道排除、keyword 通道命中 | ✓ |
| 5 | auto 包含关键词独占命中；无通道可用返回 501/503，不返回空 200 | 构造关键词独占词条（无向量）→ auto 命中且 search_mode=hybrid；keyword+threshold → 422 `validation_error`；正常空结果 200 空列表（storage_source=elasticsearch，不兜底） | ✓ |
| 6 | ONLY_VDB 下 ES 故障召回 503；HYBRID 下 auto/keyword 返回 `storage_source=sql_fts` 且带 `as_of_revision`；正常空结果绝不兜底 | **真实故障注入**：`docker stop elasticsearch` → ONLY_VDB：recall/write 均 503 `primary_unavailable`；HYBRID：auto/keyword → `sql_fts + degraded=true + as_of_revision + matched_by=[fts_sidecar]`，semantic 仍 503；ES 恢复 → readiness 回 ready | ✓ |
| 7 | ES 409 按 §7.5 区分 scope CAS、dedup、内部冲突三种语义 | 匹配 expected_revision 成功推进；同内容重复 → dedup active → noop（409 只在 prepared 在途时短暂出现）；过期 revision → 409 `revision_conflict` | ✓ |
| 8 | Legacy /search 不返回 inactive/superseded；MCP 未带凭证 401 | retire 后 `/search` 无该条、reactivate 后恢复；MCP `tools/list` 无凭证 → **401**（含 WWW-Authenticate: Bearer）；带 admin key → 200 | ✓ |
| 9 | 迁移后抽样 expand/changes/recall 与旧数据对账一致 | 见 §5 迁移验证 | ✓ |

附加验证（§10）：HYBRID 模式下停 ES（探针成功缓存过期后）readiness 实测 **`degraded`**（elasticsearch 探针 `blocking=False`，SQL FTS 继续服务）；恢复正常后回 `ready`。§7.6.4：sources/handoff/artifact-candidates 九个端点全部 501 `capability_not_supported`；/v1/context/prepare 稳定 API 正常输出信任封装。

---

## 4. 故障注入明细

| 注入手段 | 验证点 | 结果 |
|---|---|---|
| `docker stop/start elasticsearch` | ONLY_VDB 读写 503、HYBRID SQL FTS 兜底与恢复、readiness 三态迁移 | 全部符合设计 |
| 直接删除 ES head 文档 | 崩溃点（CAS 后派生写入丢失）→ Reconciler 修复 | 修复且重放幂等收敛 |
| ES update 强制 `embedding_status=pending` + 清向量 | pending 头仅进关键词通道；EmbeddingReconciler 回填后进语义通道 | 符合 §6.4 |
| 并发 8 写者同内容 | 去重线性化：1 created / 其余 noop（或瞬时 409 后收敛） | 符合 §12-2 |
| 8 路并发 + 过期 expected_revision | CAS 竞争语义 | 409 `revision_conflict` |
| 正常空结果（不存在的词/用户） | 绝不触发 SQL 兜底 | 200 空列表 + `storage_source=elasticsearch` |

---

## 5. 迁移验证（§9，真实旧数据）

工具：`server/scripts/migrate_ctx_to_es.py`（旧表只读；scope→version→head→event→dedup 顺序重建；向量从旧投影索引携带；未绑定行以 `legacy_backfill` 合成）。

| 项 | 旧 ctx（PostgreSQL） | 迁移后 ES | 对账 |
|---|---|---|---|
| scope | 45 | 110（含 e2e 新增） | es ≥ ctx ✓ |
| version | 1133 | 1251 | es ≥ ctx ✓ |
| head | 1132 | 1250 | es ≥ ctx ✓ |
| active dedup | 1121 | 1243 | es ≥ ctx ✓ |
| 抽样 hash 重验 | — | 10 条 | **0 mismatch** |
| 计数缺行断言 | — | count_mismatches=[]，ok=true | ✓ |

幂等重跑通过；旧 locomo 数据（如 `locomo_conv-26`）经迁移向量在真实 embedder 下语义召回成功（travel 查询命中相关英文对话，matched_by=[semantic]）。

---

## 6. 测试过程中发现并修复的缺陷（e2e 驱动）

| # | 缺陷 | 根因 | 修复 |
|---|---|---|---|
| 1 | 崩溃恢复非确定性失败（head 时有时无） | `_refresh()` 只刷 head 索引，scope/event 依赖 1s 自然 refresh，Reconciler 扫描读不到刚发布的 scope | 发布后 refresh 全部四个数据索引 |
| 2 | HYBRID 启动死锁 | `register_v3_readiness` 持锁调用 `get_hybrid_sidecar` 抢同一把非重入锁 | 改 `RLock` |
| 3 | 迁移后 reconcile 500 | ES 8.x 搜索命中默认不含 `_seq_no` | 显式 `seq_no_primary_term=true` |
| 4 | ES 故障期写入返回 500 而非 503 | 写路径未包装 ES 异常 | `translate_es_errors` 装饰器 → 503 `primary_unavailable` |
| 5 | MySQL 下 JWT 请求 500 | JWT `sub` 字符串未转 uuid，MySQL Uuid 绑定器抛 `'str' has no .hex` | auth 层强转 |
| 6 | HYBRID 重启卡死 | `CREATE FULLTEXT INDEX` 无幂等保护，等待元数据锁 | 先查 information_schema 再建 |
| 7 | ONLY_VDB 首次启动失败 | 旧迁移 007 在 MySQL 上索引超长（>3072B） | 迁移加 v3 守卫：v3 模式不建 ctx 记忆表（同时满足验收 1） |

**Code review 额外发现并修复**（单元层难以覆盖的边界）：
- 内核 14 项（3 critical：孤儿清扫误删整条版本历史、retire 陈旧 head 回退已发布版本、并发 revise 覆盖不可变 version 文档；7 major：对账只查存在不查匹配、恢复用预测 revision、瞬时故障误判 404 触发 claim 接管、向量回填竞态盖章、purge 无修复路径、兜底判定一票否决、claim 围栏）
- server 层 22 项（1 critical：迁移 backfill 1000 行静默截断；10 major：HYBRID readiness 违反 §10、配置幻影、未来过期日隐藏记忆、metadata-only revise 被吞、/search 伪造元数据、shim 契约缺失、MCP 错误伪装 401、挂载 /mcp 无鉴权、抽取 prompt 未贯通等）
- 全部修复后新增 12 项并发/恢复回归测试 + 5 项 legacy 契约回归测试，e2e 复验 36/36。

---

## 7. 单元测试与静态检查

- `pytest tests/`（排除上游 provider 套件）：**825 通过 / 3 失败 / 23 跳过**。3 项失败为改动前即存在的环境依赖问题（reranker 依赖缺失 ×2、langchain 过程记忆 ×1），已用 stash 对比确认与本次改动无关。
- 过程中另修复 142 项存量失败测试（test_server_auth/test_server_params 因环境缺 JWT_SECRET 等从未在本地跑通），现全绿。
- ruff（120 列）：新增/修改代码全绿。

---

## 8. 遗留与说明

1. `tests/` 3 项存量失败与本设计无关（上游依赖问题），建议单独处理。
2. MCP `BaseHTTPMiddleware` 与 fastmcp SSE 长连接的兼容性在当前 pinned 版本实测正常（streamable-http 工具调用全通）；升级 starlette/fastmcp 时需回归 `tests/test_mcp_server.py`。
3. 生产拓扑注意事项（附录 A）：`number_of_replicas` 至少 1；dims 与 embedder 输出一致；维度/分词变更走 `_v2` 索引 + 别名切换。
4. e2e 脚本入库可复跑：`python server/scripts/e2e_storage_mode_check.py --email ... --password ... [--hybrid]`。

---

## 9. 交付物清单

| 类别 | 位置 |
|---|---|
| vdb 内核（8 模块） | `mem0/context/vdb/` |
| Server 接线/路由/适配 | `server/{server_state,context_runtime,legacy_adapter,mcp_server}.py`、`server/routers/context_router.py` |
| 迁移工具 | `server/scripts/migrate_ctx_to_es.py` |
| e2e 验收脚本 | `server/scripts/e2e_storage_mode_check.py` |
| 内核/路由/契约测试 | `tests/context/{test_vdb_kernel,fake_es}.py`、`tests/test_{context_router,legacy_adapter,mcp_server}.py` |
| 部署编排（MySQL8 + 存储模式参数化） | `server/docker-compose.yaml` |
| 提交 | `3eb69509 → 01841ad2` 共 6 commits，已推送 origin/feature/memory-research |
