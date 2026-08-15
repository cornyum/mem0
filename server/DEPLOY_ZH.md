# Agentar 记忆平台 · 私有化部署指南

本仓库是面向全私有化、国产化环境的记忆平台交付版本，包含**服务端（FastAPI）**与**前端控制台（Next.js dashboard）**。所有能力均可本地部署，默认不出网（遥测已默认关闭）。

## 一、整体架构

| 组件 | 说明 | 默认端口 |
|------|------|---------|
| server | FastAPI 服务（记忆写入/检索/多租户隔离） | 8000（compose 映射 8888） |
| dashboard | Next.js 前端控制台（全中文） | 3000 |
| MySQL 8 | 应用关系库（用户/密钥/请求日志/配置） | 3306 |
| pgvector | 向量库（记忆向量与索引） | 5432 |

## 二、Docker 镜像构建（阿里龙蜥 Anolis OS 8 · x86_64）

两个镜像均以 `openanolis/anolisos:8.8`（Docker Hub；龙蜥 Anolis OS 8，内网可替换为私有仓库镜像）为 base，Python 侧使用 **Miniconda**（清华 TUNA 镜像源，阿里云镜像未收录 miniconda 安装包）管理依赖，环境名为 `agentar_mem0`；pip 使用阿里云 PyPI 源，npm 使用 npmmirror 源。

```bash
# 服务端（在仓库根目录执行）
docker build --platform linux/amd64 \
  -f server/Dockerfile.agentar -t agentar-mem-server:latest .

# 前端（在 server/dashboard 目录执行）
docker build --platform linux/amd64 \
  -f Dockerfile.agentar -t agentar-mem-dashboard:latest .
```

- 构建 ARM 机器（如 Apple Silicon）上需加 `--platform linux/amd64`（QEMU 模拟，速度较慢）。
- `BASE_IMAGE` 可用 `--build-arg` 覆盖（如内网镜像仓库中的龙蜥镜像）。

### 一键编排启动

```bash
cd server
cp .env.example .env   # 填写 APP_DB_PASSWORD / MYSQL_ROOT_PASSWORD / POSTGRES_PASSWORD / JWT_SECRET 等
docker compose -f docker-compose.agentar.yaml up -d --build
```

启动后访问 `http://<host>:3000` 进入中文控制台初始化向导；API 文档在 `http://<host>:8888/docs`。

## 三、环境变量

### 应用关系库（MySQL 8 / PostgreSQL 切换）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_DB_BACKEND` | `postgres` | `mysql`（MySQL 8 兼容，驱动 PyMySQL）或 `postgres` |
| `APP_DB_HOST/PORT/USER/PASSWORD/NAME` | 见 `.env.example` | 通用连接变量，优先级最高 |
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | — | `mysql` 后端的回退变量 |
| `POSTGRES_HOST/PORT/USER/PASSWORD`、`APP_DB_NAME` | — | `postgres` 后端的回退变量 |
| `APP_TABLE_PREFIX` | `agentar_mem_app_` | 所有业务表前缀（含 alembic 版本表） |

MySQL 8 建库示例：

```sql
CREATE DATABASE agentar_mem_app CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'agentar'@'%' IDENTIFIED BY '<密码>';
GRANT ALL PRIVILEGES ON agentar_mem_app.* TO 'agentar'@'%';
```

> 注意：MySQL 8 不支持部分索引，「仅一个管理员」的唯一性由应用层保证（PostgreSQL 上仍由部分唯一索引保证）。

### 向量库与索引

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `POSTGRES_COLLECTION_NAME` | `agentar_mem0` | 向量集合名；实体集合自动派生为 `agentar_mem0_entities` |
| `MEM0_EMBEDDING_DIMS` | 1536 / 1024(百炼) | 向量维度 |

SDK 侧所有向量库默认集合/索引名也已由 `mem0` 改为 `agentar_mem0`（`mem0/configs/vector_stores/*`）。

### 自定义分类（Categories）

管理员在控制台「进阶能力 → 分类」维护分类体系（≤50 个，名称 ≤64 字符），存储于应用库 Settings 表（key=`memory_categories`，无额外迁移）。保存后服务端立即把分类体系编译为抽取指令注入 Memory 实例（无需重启），LLM 在记忆写入时自动为每条记忆打 0..N 个分类标签，落入记忆 payload 的 `categories` 字段。

```bash
# 管理 API
curl -X PUT http://localhost:8888/categories -H "Authorization: Bearer <admin>" \
  -H "Content-Type: application/json" \
  -d '{"categories":[{"name":"健康","description":"饮食运动睡眠"},{"name":"工作","description":"职业与项目"}]}'
curl http://localhost:8888/categories -H "Authorization: Bearer <token>"

# 按分类筛选记忆（与任意标识符组合；管理员可仅凭 category 列全量）
curl "http://localhost:8888/memories?user_id=u1&category=健康" -H "Authorization: Bearer <token>"
```

**多 worker 注意**：`MEM0_WORKERS>1` 时各 worker 进程独立持有 Memory 配置，保存分类后仅处理该请求的进程立即生效，其余进程需重启（或滚动重启）后生效——多 worker 场景下建议在低峰期修改分类并重启服务（该限制与 `POST /configure` 一致，属平台既有约束）。

**孤儿标签语义**：删除或重命名分类不会清除已写入记忆的历史标签——历史标签仍可被 `?category=旧名` 查询与导出（数据不因定义演化丢失），但不会出现在控制台下拉选项中；重打标仅对新写入记忆生效。

注意：分类标签仅对**保存分类之后新写入**的记忆生效（存量不自动回填）；语义搜索（POST /search）若需按分类过滤，在 `filters` 中使用 `{"categories": {"contains": "健康"}}`（向量库文本包含语义，存在词包含近似，如「工作」可能匹配「工作经历」；列表与导出接口为精确匹配，无此问题）。

### 记忆导出（Export）

管理员可通过控制台「进阶能力 → 导出」或 API 导出记忆，支持 JSON / CSV：

```bash
curl -o memories.json "http://localhost:8888/export?format=json&tenant_id=tenant-a&category=健康" \
  -H "Authorization: Bearer <admin>"
```

- 筛选维度：`user_id / agent_id / run_id / tenant_id / session_id / category`（全部可选，全空为全量导出，需管理员权限）
- JSON：`{meta: {exported_at, filters, total}, memories: [...]}`；CSV 为 UTF-8（带 BOM，Excel 兼容），`categories` 列以 `;` 连接，并对以 `= + - @` 开头的单元格做公式注入防护
- 服务端按 id 游标分批（500/批）遍历向量库，单次导出上限 10 万条；达到上限或向量库不支持游标分页时，JSON `meta.truncated` 与响应头 `X-Agentar-Truncated: true` 会显式标注「结果可能不完整」，绝不静默截断

### 多租户隔离（tenant_id / session_id）

记忆 API 在原有 `user_id` / `agent_id` / `run_id` 基础上新增 `tenant_id`（租户）与 `session_id`（会话）两个隔离维度，全链路（写入 payload、向量过滤、历史消息 session scope、实体库、REST API）生效：

```bash
# 写入（五选一即可）
curl -X POST http://localhost:8888/members -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"我喜欢在周末徒步"}],
       "tenant_id":"tenant-a","session_id":"sess-001"}'

# 检索（filters 里组合任意维度）
curl -X POST http://localhost:8888/search -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"query":"周末做什么","filters":{"tenant_id":"tenant-a","session_id":"sess-001"}}'

# 列表 / 删除同理
curl "http://localhost:8888/memories?tenant_id=tenant-a&session_id=sess-001" -H "Authorization: Bearer <token>"
curl -X DELETE "http://localhost:8888/memories?tenant_id=tenant-a" -H "Authorization: Bearer <token>"
```

SDK 直接调用：`Memory.add(messages, tenant_id=..., session_id=...)`、`search(query, filters={"tenant_id": ...})`、`delete_all(tenant_id=...)`。

### 运行时与日志

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEM0_WORKERS` | `1` | FastAPI（uvicorn）worker 数 |
| `LOG_DIR` | 项目同级目录下的 `logs/`（容器内为 `/logs`） | 日志目录 |
| `LOG_BACKUP_COUNT` | `14` | 按天滚动的保留天数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `MEM0_TELEMETRY` | `false` | 私有化版本遥测默认关闭，不向任何公网端点上报 |

日志文件名为 `agentar-mem-server.log`，每日 0 点滚动，历史文件带日期后缀。compose 部署时 `server/../logs`（即 `server` 同级的 `logs/`）会挂载到容器 `/logs`。

### 认证

| 变量 | 说明 |
|------|------|
| `JWT_SECRET` | 必填（`openssl rand -base64 48` 生成），除非 `AUTH_DISABLED=true` |
| `ADMIN_API_KEY` | 管理 API key（≥16 字符） |
| `AUTH_DISABLED` | 仅本地开发设 true |

## 四、源码方式运行（开发）

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -e ..
export APP_DB_BACKEND=mysql APP_DB_HOST=127.0.0.1 APP_DB_PASSWORD=... JWT_SECRET=...
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${MEM0_WORKERS:-1}
```

前端开发：`cd server/dashboard && pnpm install && pnpm dev`。

## 五、LLM / Embedding 供应商

默认支持 OpenAI 兼容接口；设置 `DASHSCOPE_API_KEY` 后自动切换为百炼（Qwen + GTE rerank）。私有化环境可将 `openai_base_url` 指向本地兼容服务（vLLM/Ollama 等）。支持的供应商受 `server/main.py` 中 `BUNDLED_*_PROVIDERS` 限制，扩展时需加装依赖并重建镜像。
