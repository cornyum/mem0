#!/usr/bin/env python
"""cn-Mem-PAL v1 over the v3 `/v1/memory/*` HTTP surface.

Official PAL-Bench (Mem-PAL, AAAI 2026) evaluation adapted to this fork:

- ingest : 100 users x (30 history + 5 query samples). Every sample is one
           remember-extract call. Logs are user messages with their own
           ``[log YYYY-MM-DD HH:MM]`` timestamp prefix; dialogue turns are
           normal user/assistant messages. The remember call carries
           ``timestamp=dialogue_timestamp`` and ``timezone=Asia/Shanghai`` so
           the new Chinese temporal processing is exercised end to end.
           A per-call prompt makes the two anchors explicit: log-internal
           relative time resolves against the log line timestamp, dialogue
           relative time against Observation Date.
- tasks  : 826 query topics -> requirement restatement (official Mem-PAL
           judge, score 0..2) and solution selection (official deterministic
           pos/neg scoring, score -2..+2).
- recall : auto + rerank, top_k=50 (API limit), tenant+user scope replay.

Every phase checkpoints into the results JSON; --resume skips done work.

    python server/scripts/benchmarks/mempal/run.py \
        --email e2e-v3@example.com --password ... \
        --users 0000 --history-limit 2 --query-limit 1 --topic-limit 1 \
        --reset --out .../results/v1-smoke.json
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from _http import (  # noqa: E402
    DashScopeClient,
    ServerClient,
    parse_json_object,
    reset_server,
    resolve_dashscope_key,
    wait_ready,
)

DEFAULT_DATASET = SCRIPT_DIR / "dataset" / "input.json"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"

ANSWER_MODEL = "qwen-plus-latest"
JUDGE_MODEL = "qwen-plus-latest"
TIMEZONE = "Asia/Shanghai"
TENANT_ID = "bench-mempal-v1"

# Official PAL-Bench prompts (verbatim from hzp3517/Mem-PAL, with the
# $${...}$$ wrapper stripped exactly like the official runner does).
REQUIREMENT_SYSTEM = "你是一个个性化交互助手，能够结合用户历史理解用户需求。"
REQUIREMENT_USER = '''你正在与用户进行交互，现在给你一些用户的个性化信息作为参考，同时给你当前的用户询问，请你结合用户历史深入理解用户当前的实际需求，并用一句话描述用户的实际需求。
# 用户个性化信息
<memory>
# 当前用户询问
"""
<user_query>
"""

# 输出模板
以下为json格式的输出模板，"requirement"为待生成的回复内容。
"""
{"requirement": "..."}
"""

# 输出要求
1. 结合输入中给出的用户历史信息，理解与当前用户询问相关的用户背景情境，用一句话的形式描述用户的实际需求。
2. 除要求的json格式输出外，不要输出任何其它内容，也不需要在json中添加注释进行额外说明。

现在，请你按照输出要求和模板给出输出。'''

REQUIREMENT_JUDGE_SYSTEM = "你是一个良好的打分器，可以按照要求为待测的助手模型生成的内容进行打分。"
REQUIREMENT_JUDGE_USER = '''考虑用户与日常助手进行交互的场景，助手模型需要结合用户的个性化信息，针对用户当前的询问深入理解用户的隐式需求，并给出对于用户实际需求的描述。现在，给你用户的初始询问作为背景，同时给你待测模型的预测内容以及作为预测目标的用户实际需求参考内容，需要你将预测内容与参考内容进行对比，为模型的预测效果打分。

# 用户初始询问
"""
<user_query>
"""

# 用户实际需求（参考内容）
包含用户完整需求描述("requirement")以及用户的隐式需求列表("implicit_needs")两部分。其中用户的隐式需求列表包含2个条目，对应于用户在询问中未明确提及的2方面隐式需求内容。
"""
<reference>
"""

# 待测模型预测内容
"""
<prediction>
"""

# 评测要求及输出模板
## 评测要求
1. 输入中提供的"用户初始询问"仅作为背景，请着重关注预测内容与参考内容之间的匹配程度。
2. 在输入中给出的"用户实际需求"（即参考内容）中，用户完整需求描述("requirement")可以看作结合了隐式需求列表("implicit_needs")中两个条目的总体描述，可以作为预测内容的参考。但对于预测内容效果的评价应聚焦于预测内容与隐式需求列表中2个条目的匹配程度。
3. 评分范围为[0, 2]区间，如果预测内容能够匹配上参考内容中的2个条目，则记2分；如果预测内容只能匹配参考内容中的1个条目，则记1分；如果预测的条目未能匹配任何参考条目，则记0分。不考虑2条参考条目之间的顺序因素。
4. 在考虑预测内容与参考的隐式需求列表条目的匹配情况时，主要关注每个参考条目的核心要素是否在预测内容中得到了体现即可，不需要过分关注语言的匹配。如果预测内容的某部分描述属于某条参考内容所关注方面的更具体细节，可以认为两者在对应方面是匹配的。
5. 考虑到预测内容与某个参考条目可能存在部分匹配的情况，可以对部分匹配的条目记0.5分。
6. 需要按照下面的json格式输出模板给出输出，先用一段话给出对于当前样例评测的分析，随后给出待测模型的得分。

## 输出模板
"""
{
    "analysis": "...", // 评测分析
    "score": <分数> // 取值范围：{0, 0.5, 1, 1.5, 2}
}
"""

现在，请你按照要求给出评测分析和分数。'''

SOLUTION_SYSTEM = "你是一个个性化助手，能够针对用户的需求给出符合其个性化偏好的解决方案。"
SOLUTION_USER = '''你正在与用户进行交互，现在给你一些用户的个性化信息作为参考，同时给你当前的用户需求描述以及针对当前需求的8条候选解决方案建议，请你结合用户历史深入理解用户的个性化偏好，并从候选方案建议中选出2条最符合用户偏好的方案。
# 用户个性化信息
<memory>
# 当前用户需求
"""
<requirement>
"""

# 候选方案建议
以下为针对当前用户需求的8条候选方案，包括各条方案的id和方案的内容。
"""
<candidate_solutions>
"""

# 输出模板
以下为json格式的输出模板，"solution"为待生成的方案内容。
"""
{
    "analysis": "...", // 用一段话给出对于所有候选方案的整体分析，关注于用户的个性化偏好
    "selected_solutions": [...] // 列出2个最符合用户偏好的方案id
}
"""

# 输出要求
1. 结合输入中给出的用户历史信息，理解用户的个性化偏好，并基于用户偏好给出对于候选方案的分析和选择。
2. 遵循上述输出模板给出输出，其中"selected_solutions"部分只能选择最符合偏好的2项方案id作为输出，不能包含更多或更少的方案id。
3. 除要求的json格式输出外，不要输出任何其它内容，也不需要在json中添加注释进行额外说明。

现在，请你按照输出要求和模板给出输出。'''

LOG_ANCHOR_PROMPT = (
    "基准专用时间规则：日志行以“[log YYYY-MM-DD HH:MM]”前缀给出该条日志自身的时间戳，"
    "日志内部的相对时间（昨天/上周/明天等）一律以该行自身时间戳为锚点换算；"
    "对话消息中的相对时间仍以 Observation Date 为锚点。"
    "绝对日期统一写作“YYYY年M月D日（星期X）”，日期与星期必须一致。"
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--users", default=None, help="comma-separated user ids, e.g. 0000,0001 (default: all)")
    parser.add_argument("--history-limit", type=int, default=0, help="first N history samples per user (0=all)")
    parser.add_argument("--query-limit", type=int, default=0, help="first N query samples per user (0=all)")
    parser.add_argument("--topic-limit", type=int, default=0, help="first N topics per query sample (0=all)")
    parser.add_argument("--tenant", default=TENANT_ID, help="tenant_id isolation label")
    parser.add_argument("--timezone", default=TIMEZONE)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="reuse an existing corpus: skip ingest and point recall at --ingest-tag tenant",
    )
    parser.add_argument("--ingest-tag", default=None, help="tenant of the existing corpus when --skip-ingest")
    parser.add_argument("--ingest-concurrency", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-answer", action="store_true", help="stop after recall (latency/scope check only)")
    parser.add_argument("--dashscope-key", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not args.no_auth and (not args.email or not args.password):
        parser.error("--email/--password required (or --no-auth)")
    if not args.no_answer and not resolve_dashscope_key(args.dashscope_key):
        parser.error("DASHSCOPE_API_KEY missing for answer/judge (env or server/.env)")
    if args.skip_ingest and args.reset:
        parser.error("--skip-ingest and --reset are mutually exclusive")
    if args.skip_ingest and not args.ingest_tag:
        parser.error("--skip-ingest requires --ingest-tag identifying the existing tenant")
    return args


def load_dataset(args):
    path = Path(args.dataset)
    if not path.is_file():
        raise SystemExit(f"dataset not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"dataset {path}: expected a non-empty JSON object")
    if args.users:
        wanted = [uid.strip() for uid in args.users.split(",") if uid.strip()]
        missing = set(wanted) - set(data)
        if missing:
            raise SystemExit(f"dataset {path}: users not found: {sorted(missing)}")
        data = {uid: data[uid] for uid in wanted}
    return data


def sorted_topic_ids(topics):
    def key(topic_id):
        match = re.match(r"^topic-(\d+)$", topic_id)
        return (0, int(match.group(1))) if match else (1, topic_id)

    return sorted(topics, key=key)


def build_sample_messages(sample):
    messages = []
    for log in sample.get("logs") or []:
        ts = (log.get("timestamp") or "").strip()
        content = (log.get("content") or "").strip()
        if not content:
            continue
        prefix = f"[log {ts}] " if ts else ""
        messages.append({"role": "user", "content": f"{prefix}{content}"})
    dialogue = sample.get("dialogue") or {}
    turn_keys = []
    for key in dialogue:
        match = re.match(r"^turn_(\d+)$", key)
        if match:
            turn_keys.append((int(match.group(1)), key))
    for _, key in sorted(turn_keys):
        turn = dialogue[key]
        if isinstance(turn, dict):
            for role in ("user", "assistant"):
                speaker = turn.get(role) or {}
                if isinstance(speaker, dict):
                    content = str(speaker.get("content") or "").strip()
                    if content:
                        messages.append({"role": role, "content": content})
    return messages


def ingest_user(user_id, user_data, args, client, scope_tag, report):
    user_scope = f"mempal_{user_id}"
    history = list(user_data.get("history") or [])
    query = list(user_data.get("query") or [])
    if args.history_limit:
        history = history[: args.history_limit]
    if args.query_limit:
        query = query[: args.query_limit]
    samples = history + query
    progress_path = Path(args.out).parent / f"{Path(args.out).stem}.ingest.{user_id}.json"
    existing = {}
    if progress_path.is_file():
        try:
            existing = json.loads(progress_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    records = list(existing.get("records") or [])
    done_samples = {rec.get("sample") for rec in records}
    totals = {
        key: sum(rec.get(key, 0) for rec in records)
        for key in ("batches", "created", "updated", "noop", "errors", "facts")
    }
    for sample in samples:
        sample_id = sample.get("sample_id")
        if sample_id in done_samples:
            continue
        messages = build_sample_messages(sample)
        if not messages:
            records.append({"sample": sample_id, "messages": 0, "skipped": True})
            continue
        body = {
            "tenant_id": scope_tag,
            "user_id": user_scope,
            "mode": "extract",
            "kind": "fact",
            "categories": ["mempal"],
            "messages": messages,
            "timestamp": sample.get("dialogue_timestamp") or None,
            "timezone": args.timezone,
            "prompt": LOG_ANCHOR_PROMPT,
            "metadata": {
                "benchmark": "mempal",
                "user_id": user_id,
                "sample_id": sample_id,
                "sample_kind": "query" if sample_id in [s.get("sample_id") for s in (user_data.get("query") or [])] else "history",
                "dialogue_timestamp": sample.get("dialogue_timestamp"),
                "log_count": len(sample.get("logs") or []),
            },
        }
        started = time.time()
        try:
            resp = client.post("/v1/memory/remember", body, timeout=300.0)
        except Exception as exc:  # noqa: BLE001
            totals["errors"] += 1
            records.append({"sample": sample_id, "error": str(exc)[:300]})
            print(f"[ingest-error] {user_id} {sample_id}: {exc}", flush=True)
            continue
        totals["batches"] += 1
        facts = []
        for result in resp.get("results", []):
            outcome = result.get("outcome", "noop")
            totals[outcome] = totals.get(outcome, 0) + 1
            entry = result.get("entry") or {}
            text = entry.get("text") or ""
            if text:
                facts.append({"text": text, "outcome": outcome})
        totals["facts"] += len(facts)
        records.append(
            {
                "sample": sample_id,
                "messages": len(messages),
                "elapsed_s": round(time.time() - started, 1),
                "created": len([f for f in facts if f["outcome"] == "created"]),
                "facts": facts,
            }
        )
        progress = {
            "user": user_id,
            "user_id": user_scope,
            "records": records,
            **totals,
        }
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=1))
        if len(records) % 5 == 0:
            print(f"[ingest] {user_id}: {len(records)} samples, {totals}", flush=True)
    return {
        "user": user_id,
        "user_id": user_scope,
        "records": records,
        **totals,
    }


def run_ingest(client, dataset, args, report, scope_tag):
    phase = report["phases"]["ingest"]
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(args.ingest_concurrency, 1)) as pool:
        futures = {
            pool.submit(ingest_user, uid, user_data, args, client, scope_tag, report): uid
            for uid, user_data in dataset.items()
        }
        for future in as_completed(futures):
            record = future.result()
            phase.setdefault("samples", {})[record["user"]] = record
            save(args, report)
            print(f"[ingest] {record['user']} done: {record['batches']} batches, {record['facts']} facts, {record['errors']} errors", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    records = phase.get("samples", {}).values()
    totals = {k: sum(rec.get(k, 0) for rec in records) for k in ("batches", "created", "updated", "noop", "errors", "facts")}
    print(f"[ingest] done in {phase['elapsed_s']}s: {totals}", flush=True)


def collect_topics(dataset, args, scope_tag):
    topics = []
    for uid, user_data in dataset.items():
        query_samples = list(user_data.get("query") or [])
        if args.query_limit:
            query_samples = query_samples[: args.query_limit]
        for sample in query_samples:
            topic_ids = sorted_topic_ids((sample.get("topics") or {}).keys())
            if args.topic_limit:
                topic_ids = topic_ids[: args.topic_limit]
            for topic_id in topic_ids:
                topic = sample["topics"][topic_id]
                candidates = []
                for idx, candidate in enumerate(topic.get("candidate_solutions") or [], start=1):
                    candidates.append(
                        {
                            "id": f"S{idx}",
                            "solution": candidate.get("solution", ""),
                            "feedback": candidate.get("feedback", ""),
                        }
                    )
                topics.append(
                    {
                        "qid": f"{uid}|{sample.get('sample_id')}|{topic_id}",
                        "user_id": uid,
                        "sample_id": sample.get("sample_id"),
                        "topic_id": topic_id,
                        "scope": {"tenant_id": scope_tag, "user_id": f"mempal_{uid}"},
                        "user_query": topic.get("user_query", ""),
                        "requirement": topic.get("requirement", ""),
                        "implicit_needs": topic.get("implicit_needs") or [],
                        "candidates": candidates,
                    }
                )
    return topics


def recall_once(client, topic, query, top_k):
    t0 = time.perf_counter()
    resp = client.post(
        "/v1/memory/recall",
        {
            "tenant_id": topic["scope"]["tenant_id"],
            "user_id": topic["scope"]["user_id"],
            "query": query[:8000],
            "limit": top_k,
            "mode": "auto",
            "rerank": True,
        },
        timeout=120.0,
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    results = resp.get("results", [])
    return {
        "latency_ms": round(latency_ms, 1),
        "search_mode": resp.get("search_mode"),
        "degraded": resp.get("degraded"),
        "rerank_status": resp.get("rerank_status"),
        "result_count": len(results),
        "memories": [(r.get("text") or "").strip() for r in results],
        "scores": [r.get("score") for r in results],
        "created_at": [r.get("created_at") for r in results],
    }


def run_recall(client, topics, args, report):
    phase = report["phases"]["recall"]
    done = {item["qid"] for item in phase.get("items", []) if item.get("req") and not item["req"].get("error")}
    pending = [t for t in topics if t["qid"] not in done]
    completed = 0
    started = time.time()

    def work(topic):
        req = recall_once(client, topic, topic["user_query"], args.top_k)
        sol = recall_once(client, topic, topic["requirement"], args.top_k)
        return {**topic, "req": req, "sol": sol}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, t): t["qid"] for t in pending}
        for future in as_completed(futures):
            item = future.result()
            phase.setdefault("items", []).append(item)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[recall] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    print(f"[recall] done: {len(pending)} topics", flush=True)


def memories_block(item, kind="req", top_k=50):
    data = item.get(kind) or {}
    memories = data.get("memories") or []
    dates = data.get("created_at") or []
    lines = []
    for idx, text in enumerate(memories[:top_k]):
        date = dates[idx][:10] if idx < len(dates) and dates[idx] else ""
        lines.append(f"{idx + 1}. [{date}] {text}" if date else f"{idx + 1}. {text}")
    return "\n".join(lines) if lines else "（无可用记忆）"


def run_requirement_answers(dashscope, args, report):
    phase = report["phases"]["requirement_answer"]
    recall_items = report["phases"]["recall"].get("items", [])
    done = {item["qid"] for item in phase.get("items", [])}
    pending = [item for item in recall_items if item.get("req") and not item["req"].get("error") and item["qid"] not in done]
    completed = 0

    def work(item):
        prompt = REQUIREMENT_USER.replace("<memory>", memories_block(item, "req", args.top_k)).replace(
            "<user_query>", item["user_query"]
        )
        raw = dashscope.chat(prompt, system=REQUIREMENT_SYSTEM, json_mode=True)
        parsed = parse_json_object(raw) or {}
        prediction = str(parsed.get("requirement") or raw)[:2000]
        return {"qid": item["qid"], "prediction": prediction, "raw": raw[:2000]}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, item): item["qid"] for item in pending}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                phase.setdefault("errors", []).append({"qid": futures[future], "error": str(exc)[:300]})
                print(f"[requirement-answer-error] {futures[future]}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[requirement-answer] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    save(args, report)
    print("[requirement-answer] done", flush=True)


def run_requirement_judge(dashscope, args, report):
    phase = report["phases"]["requirement_judge"]
    answers = {item["qid"]: item.get("prediction", "") for item in report["phases"]["requirement_answer"].get("items", [])}
    recall_items = {item["qid"]: item for item in report["phases"]["recall"].get("items", [])}
    done = {item["qid"] for item in phase.get("items", []) if "score" in item}
    pending = [item for item in recall_items.values() if item["qid"] in answers and item["qid"] not in done]
    completed = 0

    def work(item):
        reference = json.dumps(
            {"requirement": item["requirement"], "implicit_needs": item["implicit_needs"]},
            ensure_ascii=False,
            indent=4,
        )
        prompt = (
            REQUIREMENT_JUDGE_USER.replace("<user_query>", item["user_query"])
            .replace("<reference>", reference)
            .replace("<prediction>", answers[item["qid"]] or "（空）")
        )
        raw = dashscope.chat(prompt, system=REQUIREMENT_JUDGE_SYSTEM, json_mode=True)
        parsed = parse_json_object(raw)
        if not parsed:
            raise RuntimeError(f"judge JSON parse failed: {raw[:200]!r}")
        try:
            score = float(parsed["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"judge score invalid ({raw[:200]!r})") from exc
        if score not in (0.0, 0.5, 1.0, 1.5, 2.0):
            raise RuntimeError(f"judge score out of range: {score} ({raw[:200]!r})")
        return {
            "qid": item["qid"],
            "score": score,
            "analysis": str(parsed.get("analysis", ""))[:500],
            "judge_raw": raw[:500],
        }

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, item): item["qid"] for item in pending}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                phase.setdefault("errors", []).append({"qid": futures[future], "error": str(exc)[:300]})
                print(f"[requirement-judge-error] {futures[future]}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[requirement-judge] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    save(args, report)
    print("[requirement-judge] done", flush=True)


def run_solution_answers(dashscope, args, report):
    phase = report["phases"]["solution_answer"]
    recall_items = report["phases"]["recall"].get("items", [])
    done = {item["qid"] for item in phase.get("items", [])}
    pending = [item for item in recall_items if item.get("sol") and not item["sol"].get("error") and item["qid"] not in done]
    completed = 0

    def work(item):
        candidate_json = json.dumps({c["id"]: c["solution"] for c in item["candidates"]}, ensure_ascii=False, indent=4)
        prompt = (
            SOLUTION_USER.replace("<memory>", memories_block(item, "sol", args.top_k))
            .replace("<requirement>", item["requirement"])
            .replace("<candidate_solutions>", candidate_json)
        )
        raw = dashscope.chat(prompt, system=SOLUTION_SYSTEM, json_mode=True)
        parsed = parse_json_object(raw)
        if not parsed:
            raise RuntimeError(f"solution JSON parse failed: {raw[:200]!r}")
        selected = parsed.get("selected_solutions")
        if not isinstance(selected, list) or len(selected) != 2:
            raise RuntimeError(f"solution selection invalid: {raw[:200]!r}")
        valid = {f"S{i}" for i in range(1, 9)}
        if selected[0] not in valid or selected[1] not in valid or selected[0] == selected[1]:
            raise RuntimeError(f"solution ids invalid: {selected}")
        return {
            "qid": item["qid"],
            "selected_solutions": selected,
            "analysis": str(parsed.get("analysis", ""))[:500],
            "raw": raw[:500],
        }

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, item): item["qid"] for item in pending}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                phase.setdefault("errors", []).append({"qid": futures[future], "error": str(exc)[:300]})
                print(f"[solution-answer-error] {futures[future]}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[solution-answer] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    save(args, report)
    print("[solution-answer] done", flush=True)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * q), len(ordered) - 1)], 1)


def solution_score(topic, answer):
    score = 0
    hits_pos = 0
    for solution_id in answer["selected_solutions"]:
        candidate = next((c for c in topic["candidates"] if c["id"] == solution_id), None)
        if candidate is None:
            continue
        if candidate["feedback"] == "pos":
            score += 1
            hits_pos += 1
        elif candidate["feedback"] == "neg":
            score -= 1
    pos_ids = {c["id"] for c in topic["candidates"] if c["feedback"] == "pos"}
    return {
        "score": score,
        "exact_pos": int(set(answer["selected_solutions"]) == pos_ids),
        "hits_pos": hits_pos,
    }


def compute_metrics(report):
    topics = report["phases"]["recall"].get("items", [])
    req_judge = {item["qid"]: item for item in report["phases"]["requirement_judge"].get("items", []) if "score" in item}
    sol_answers = {item["qid"]: item for item in report["phases"]["solution_answer"].get("items", [])}

    req_scores = [item["score"] for item in req_judge.values()]
    sol_rows = {}
    per_user = {}
    for topic in topics:
        uid = topic["user_id"]
        answer = sol_answers.get(topic["qid"])
        if answer:
            sol_rows[topic["qid"]] = solution_score(topic, answer)
        judge = req_judge.get(topic["qid"])
        if judge is None:
            continue
        per_user.setdefault(uid, {"req_scores": [], "sol_scores": []})
        per_user[uid]["req_scores"].append(judge["score"])
        if topic["qid"] in sol_rows:
            per_user[uid]["sol_scores"].append(sol_rows[topic["qid"]]["score"])

    sol_scores = [row["score"] for row in sol_rows.values()]
    metrics = {
        "requirement": {
            "topics": len(req_scores),
            "score_100": round(sum(req_scores) / len(req_scores) * 50, 2) if req_scores else None,
            "mean_raw_score": round(sum(req_scores) / len(req_scores), 4) if req_scores else None,
            "full_2_rate": round(sum(1 for s in req_scores if s == 2.0) / len(req_scores), 4) if req_scores else None,
            "ge_1_rate": round(sum(1 for s in req_scores if s >= 1.0) / len(req_scores), 4) if req_scores else None,
            "judge_errors": len(report["phases"]["requirement_judge"].get("errors", [])),
            "answer_errors": len(report["phases"]["requirement_answer"].get("errors", [])),
        },
        "solution": {
            "topics": len(sol_scores),
            "score_100": round(sum(sol_scores) / len(sol_scores) * 50, 2) if sol_scores else None,
            "mean_raw_score": round(sum(sol_scores) / len(sol_scores), 4) if sol_scores else None,
            "exact_pos_rate": round(sum(r["exact_pos"] for r in sol_rows.values()) / len(sol_rows), 4) if sol_rows else None,
            "ge_1_pos_rate": round(sum(1 for r in sol_rows.values() if r["hits_pos"] >= 1) / len(sol_rows), 4) if sol_rows else None,
            "answer_errors": len(report["phases"]["solution_answer"].get("errors", [])),
        },
        "recall": {
            "topics": len(topics),
            "req_p50_ms": percentile([item["req"]["latency_ms"] for item in topics if item.get("req") and item["req"].get("latency_ms")], 0.5),
            "req_p95_ms": percentile([item["req"]["latency_ms"] for item in topics if item.get("req") and item["req"].get("latency_ms")], 0.95),
            "req_avg_results": round(sum(item["req"].get("result_count", 0) for item in topics) / len(topics), 2) if topics else None,
            "errors": sum(1 for item in topics if item.get("req", {}).get("error") or item.get("sol", {}).get("error")),
        },
        "ingest": {
            key: sum(rec.get(key, 0) for rec in report["phases"]["ingest"].get("samples", {}).values())
            for key in ("batches", "created", "updated", "noop", "errors", "facts")
        },
    }

    metrics["per_user"] = {}
    for uid, rows in per_user.items():
        metrics["per_user"][uid] = {
            "requirement_score_100": round(sum(rows["req_scores"]) / len(rows["req_scores"]) * 50, 2) if rows["req_scores"] else None,
            "solution_score_100": round(sum(rows["sol_scores"]) / len(rows["sol_scores"]) * 50, 2) if rows["sol_scores"] else None,
            "topics": len(rows["req_scores"]),
        }
    return metrics


SAVE_LOCK = threading.Lock()


def save(args, report):
    with SAVE_LOCK:
        report["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        tmp.replace(path)


def main(argv=None):
    args = parse_args(argv)
    scope_tag = args.ingest_tag if args.skip_ingest else args.tenant
    if not args.out:
        DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        args.out = str(DEFAULT_RESULTS_DIR / f"mempal-v1-{datetime.now():%Y%m%dT%H%M%S}.json")

    dataset = load_dataset(args)
    print(f"[dataset] {len(dataset)} users from {args.dataset}", flush=True)

    report = {
        "run_id": f"mempal-v1-{datetime.now():%Y%m%dT%H%M%S}",
        "config": {
            "base_url": args.base_url,
            "users": list(dataset),
            "history_limit": args.history_limit,
            "query_limit": args.query_limit,
            "topic_limit": args.topic_limit,
            "tenant": scope_tag,
            "timezone": args.timezone,
            "top_k": args.top_k,
            "answer_model": ANSWER_MODEL,
            "judge_model": JUDGE_MODEL,
            "skip_ingest": args.skip_ingest,
        },
        "phases": {
            "ingest": {},
            "recall": {},
            "requirement_answer": {},
            "requirement_judge": {},
            "solution_answer": {},
        },
    }
    if args.resume and Path(args.out).is_file():
        prior = json.loads(Path(args.out).read_text())
        prior_config = prior.get("config", {})
        if (
            prior_config.get("tenant") == scope_tag
            and prior_config.get("users") == list(dataset)
            and prior_config.get("history_limit") == args.history_limit
            and prior_config.get("query_limit") == args.query_limit
            and prior_config.get("topic_limit") == args.topic_limit
        ):
            report = prior
            print(f"[resume] reusing {args.out}", flush=True)
        else:
            print("[resume] config mismatch; starting fresh", flush=True)

    client = ServerClient(args.base_url, email=args.email, password=args.password, no_auth=args.no_auth)
    wait_ready(client)
    report["config"]["capabilities"] = client.get("/v1/capabilities")
    save(args, report)

    if args.skip_ingest:
        report["phases"]["ingest"] = {"done": True, "reused_tenant": scope_tag, "note": "ingest skipped"}
        save(args, report)
        print(f"[ingest] skipped; recall targets tenant={scope_tag}", flush=True)
    elif args.reset and not report["phases"]["ingest"].get("done"):
        reset_server(client)
        wait_ready(client)

    if not report["phases"]["ingest"].get("done"):
        run_ingest(client, dataset, args, report, scope_tag)
    else:
        print("[ingest] already done (resume/skip)", flush=True)

    topics = collect_topics(dataset, args, scope_tag)
    print(f"[plan] {len(topics)} topics (requirement + solution)", flush=True)
    if not report["phases"]["recall"].get("done"):
        run_recall(client, topics, args, report)
    else:
        print("[recall] already done (resume)", flush=True)

    if args.no_answer:
        report["metrics"] = compute_metrics(report)
        save(args, report)
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        return 0

    dashscope = DashScopeClient(resolve_dashscope_key(args.dashscope_key), ANSWER_MODEL)
    judge = DashScopeClient(resolve_dashscope_key(args.dashscope_key), JUDGE_MODEL)
    if not report["phases"]["requirement_answer"].get("done"):
        run_requirement_answers(dashscope, args, report)
    if not report["phases"]["requirement_judge"].get("done"):
        run_requirement_judge(judge, args, report)
    if not report["phases"]["solution_answer"].get("done"):
        run_solution_answers(dashscope, args, report)

    report["metrics"] = compute_metrics(report)
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save(args, report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"[done] results: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
