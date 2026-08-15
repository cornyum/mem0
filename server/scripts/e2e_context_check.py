#!/usr/bin/env python
"""End-to-end context-layer verification against a running Agentar server.

Drives the full explicit-lifecycle contract over HTTP with concurrent
writers and a real dataset (PowerContext locomo subset + a curated Chinese
fact set), then exercises idempotency, pagination, citations and the error
contract. Designed for post-deploy smoke verification:

    python server/scripts/e2e_context_check.py \
        --base-url http://localhost:8888 \
        --email admin@example.com --password '...' \
        --locomo /path/to/locomo10.json

Exit code 0 = all invariants held (ingest without errors, replay purely
no-op, lifecycle/citation/error contract as designed).
"""

import argparse
import concurrent.futures as futures
import json
import sys
import threading
import time
import uuid

import httpx

KINDS = ["fact", "preference", "decision", "constraint", "working_note"]
CATEGORIES = ["个人偏好", "工作安排", "行程规划", "健康饮食"]

CHINESE_FACTS = [
    "用户偏好深色模式的界面主题",
    "用户每天早上七点起床晨跑三十分钟",
    "用户的团队每两周进行一次迭代回顾",
    "用户对花生严重过敏，绝对不能食用",
    "用户计划十月去成都旅行五天",
    "用户的公司使用 MySQL 和 Elasticsearch 作为核心存储",
    "用户的女儿正在学习钢琴，每周三上课",
    "用户不喜欢在会议上被打断发言",
    "用户的上班通勤时间是四十分钟地铁",
    "用户最近在阅读《设计数据密集型应用》",
    "用户的备用联系方式是公司邮箱",
    "用户习惯用番茄工作法管理下午的时间",
    "用户所在项目的代码仓库在内部 GitLab",
    "用户每周五下午预留深度工作时间",
    "用户的体检报告显示胆固醇略高，需要控制饮食",
    "用户偏好用中文撰写技术文档",
    "用户的家里养了一只橘猫叫大米",
    "用户拒绝在周末处理非紧急工作消息",
    "用户的年度目标是完成一次全程马拉松",
    "用户的团队信奉文档先行的协作文化",
    "用户对辣度接受度中等，偏爱川菜",
    "用户的睡眠时间通常在晚上十一点半",
    "用户订阅了三个技术周刊",
    "用户的显示偏好是系统字体放大到百分之百十五",
    "用户的公司在杭州滨江的园区办公",
    "用户喜欢在通勤路上听播客",
    "用户的父母住在苏州，每月探望一次",
    "用户的笔记本电脑是十四寸的国产型号",
    "用户参与的开源项目使用 Apache 2.0 许可证",
    "用户的会议纪要模板包含决议与待办两个区块",
]

E2E_RUN_TAG = "e2e"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", required=True, help="existing account email")
    parser.add_argument("--password", required=True, help="existing account password")
    parser.add_argument("--locomo", default=None, help="optional locomo10.json path")
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def build_dataset(locomo_path):
    jobs = []
    if locomo_path:
        dataset = json.load(open(locomo_path))
        for sample in dataset[:2]:
            sample_id = sample.get("sample_id") or uuid.uuid4().hex[:6]
            conv = sample["conversation"]
            session_keys = sorted(
                (k for k in conv if k.startswith("session_") and not k.endswith("date_time")),
                key=lambda k: int(k.split("_")[1]),
            )
            for skey in session_keys[:4]:
                session_no = int(skey.split("_")[1])
                for turn in conv[skey]:
                    text = (turn.get("text") or "").strip()
                    if not text:
                        continue
                    jobs.append({
                        "user_id": f"{E2E_RUN_TAG}_locomo_{sample_id}",
                        "session_id": f"s{session_no}",
                        "text": text[:2000],
                        "kind": "working_note",
                        "categories": [f"locomo_s{session_no}"],
                        "mode": "append",
                    })
    run_tag = uuid.uuid4().hex[:8]
    for i, fact in enumerate(CHINESE_FACTS):
        jobs.append({
            "tenant_id": f"{E2E_RUN_TAG}_{run_tag}",
            "user_id": f"zh_user_{i % 3}",
            "text": fact,
            "kind": KINDS[i % len(KINDS)],
            "categories": [CATEGORIES[i % len(CATEGORIES)]],
            "mode": "append",
        })
    return jobs


def main() -> int:
    args = parse_args()
    summary = {}
    client = httpx.Client(base_url=args.base_url, timeout=30.0)

    r = client.post("/auth/login", json={"email": args.email, "password": args.password})
    r.raise_for_status()
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    summary["auth"] = "ok"

    summary["capabilities"] = client.get("/v1/capabilities", headers=headers).json()["memory"]

    jobs = build_dataset(args.locomo)
    summary["dataset_jobs"] = len(jobs)

    outcomes = {"created": 0, "noop": 0, "error": 0}
    lock = threading.Lock()
    errors = []

    def run_job(body):
        try:
            resp = client.post("/v1/memory/remember", json=body, headers=headers)
            with lock:
                if resp.status_code == 200:
                    outcomes[resp.json()["outcome"]] += 1
                else:
                    outcomes["error"] += 1
                    errors.append((resp.status_code, resp.text[:120]))
        except Exception as exc:
            with lock:
                outcomes["error"] += 1
                errors.append(("transport", str(exc)[:120]))

    t0 = time.time()
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_job, jobs))
    elapsed = time.time() - t0
    summary["ingest"] = {**outcomes, "elapsed_s": round(elapsed, 1), "rps": round(len(jobs) / elapsed, 1)}
    if errors:
        summary["ingest_errors"] = errors[:5]

    zh_scope = dict(jobs[-1])
    scope_keys = ("tenant_id", "user_id")
    zh = {k: zh_scope[k] for k in scope_keys}

    # Idempotent replay of the same dataset must be purely no-op.
    replay = {"created": 0, "noop": 0}
    for body in jobs[-len(CHINESE_FACTS):]:
        probe = {k: body[k] for k in scope_keys}
        resp = client.post("/v1/memory/remember", json=body, headers=headers)
        replay[resp.json()["outcome"]] += 1
        del probe
    summary["replay"] = replay

    # Lifecycle on the zh scope.
    changes = client.post("/v1/memory/changes", json=zh, headers=headers).json()["changes"]
    target = changes[0]
    retired = client.post("/v1/memory/retire", json={
        **zh, "entry_id": target["entry_id"], "reason": "e2e verify",
    }, headers=headers).json()["outcome"]
    retired_again = client.post("/v1/memory/retire", json={
        **zh, "entry_id": target["entry_id"],
    }, headers=headers).json()["outcome"]
    reactivated = client.post("/v1/memory/reactivate", json={
        **zh, "entry_id": target["entry_id"],
    }, headers=headers).json()["outcome"]
    summary["lifecycle"] = {"retire": retired, "retire_again": retired_again, "reactivate": reactivated}

    expanded = client.post("/v1/memory/expand", json={
        **zh,
        "citation": {
            "artifact_id": _artifact(client, headers, zh),
            "entry_id": target["entry_id"],
            "entry_version_id": target["entry_version_id"],
        },
    }, headers=headers)
    summary["expand_status"] = expanded.status_code

    r501 = client.post("/v1/memory/remember", json={**zh, "text": "x", "mode": "extract"}, headers=headers)
    r409 = client.post("/v1/memory/remember", json={**zh, "text": f"CAS {uuid.uuid4().hex[:6]}", "expected_revision": 10**6}, headers=headers)
    r422 = client.post("/v1/memory/remember", json={**zh, "text": "x" * 9000}, headers=headers)
    summary["error_contract"] = {"extract": r501.status_code, "cas_conflict": r409.status_code, "oversized": r422.status_code}

    pages, seen, cursor = 0, 0, None
    while True:
        body = {**zh, "limit": 20}
        if cursor:
            body["cursor"] = cursor
        page = client.post("/v1/memory/changes", json=body, headers=headers).json()["changes"]
        seen += len(page)
        pages += 1
        if page and page[-1].get("next_cursor"):
            cursor = page[-1]["next_cursor"]
        else:
            break
        if pages > 100:
            break
    summary["changes_pagination"] = {"pages": pages, "records": seen}
    summary["health"] = client.get("/health/ready").json()["state"]

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = (
        outcomes["error"] == 0
        and replay["created"] == 0
        and retired in ("updated", "noop")
        and retired_again == "noop"
        and reactivated in ("updated", "noop")
        and expanded.status_code == 200
        and summary["error_contract"] == {"extract": 501, "cas_conflict": 409, "oversized": 422}
        and summary["health"] in ("ready", "degraded")
    )
    return 0 if ok else 1


def _artifact(client, headers, scope):
    anchor = client.post("/v1/memory/remember", json={
        **scope, "text": f"e2e anchor {uuid.uuid4().hex[:8]}", "kind": "fact",
    }, headers=headers).json()
    return anchor["artifact_id"]


if __name__ == "__main__":
    sys.exit(main())
