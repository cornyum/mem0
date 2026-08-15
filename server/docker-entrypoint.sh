#!/bin/bash
# Agentar 记忆平台 · 服务端容器入口
# 1. 等待应用数据库可用并执行 alembic 迁移（MySQL8 / PostgreSQL 由 APP_DB_* 决定）
# 2. 以 MEM0_WORKERS 个 worker 启动 FastAPI（uvicorn）
set -e

cd /app/server

# 数据库可能刚启动，最多重试 30 次（约 60 秒）
n=0
until alembic upgrade head; do
    n=$((n + 1))
    if [ "$n" -ge 30 ]; then
        echo "[entrypoint] 数据库迁移失败，已达最大重试次数，退出" >&2
        exit 1
    fi
    echo "[entrypoint] 数据库未就绪，2 秒后重试迁移（第 ${n} 次）..."
    sleep 2
done

WORKERS="${MEM0_WORKERS:-1}"
echo "[entrypoint] 以 ${WORKERS} 个 worker 启动 Agentar 记忆平台服务端"

exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS}"
