#!/usr/bin/env python
"""Private benchmark first run (design §11 P1-⑦).

Baseline-establishment harness — no pass/fail line by design:

- Ingest: real locomo conversations through the LLM extraction pipeline
  (qwen-plus-latest via POST /memories), recording write-path cost.
- Recall: every QA question through POST /v1/memory/recall (mode auto,
  hybrid over ES), recording p50/p95 latency.
- Judge: qwen-plus-latest as a FIXED judge scores whether the gold answer
  is derivable from the retrieved memories (supported/partial/unsupported).

    python server/scripts/private_benchmark.py \
        --base-url http://localhost:8888 --email ... --password ... \
        --locomo /path/to/locomo10.json --samples 4 --sessions 10 \
        --out /tmp/benchmark_report.json
"""

import argparse
import json
import sys
import time

import httpx

JUDGE_MODEL = "qwen-plus-latest"
JUDGE_PROMPT = """你是记忆检索质量评审。根据问题与检索到的记忆条目，判断标准答案能否由记忆推导。
只输出一个词：supported（记忆足以推出标准答案）、partial（记忆部分支持）、unsupported（记忆不含所需信息）。

问题：{question}
标准答案：{answer}

检索到的记忆：
{memories}"""


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8888")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--locomo", required=True)
    p.add_argument("--samples", type=int, default=4)
    p.add_argument("--sessions", type=int, default=10, help="sessions ingested per sample")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--judge", action="store_true", help="run the LLM judge pass")
    p.add_argument("--dashscope-key", default=None, help="explicit key (else env DASHSCOPE_API_KEY)")
    p.add_argument("--out", default=None)
    return p.parse_args()


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(len(ordered) * q), len(ordered) - 1)
    return round(ordered[idx] * 1000, 1)  # ms


def main() -> int:
    args = parse_args()
    client = httpx.Client(base_url=args.base_url, timeout=120.0)
    tok = client.post("/auth/login", json={"email": args.email, "password": args.password}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    dataset = json.load(open(args.locomo))[: args.samples]
    report = {
        "config": {"samples": len(dataset), "sessions_per_sample": args.sessions, "top_k": args.top_k, "judge_model": JUDGE_MODEL},
        "ingest": {"batches": 0, "memories": 0, "elapsed_s": 0.0},
        "recall": {"questions": 0, "errors": 0, "latency_ms": []},
        "judge": {"supported": 0, "partial": 0, "unsupported": 0, "errors": 0},
        "per_sample": [],
    }

    # ---- ingest ----
    scopes = {}
    t0 = time.time()
    for sample in dataset:
        sample_id = sample.get("sample_id")
        conv = sample["conversation"]
        keys = sorted(
            (k for k in conv if k.startswith("session_") and not k.endswith("date_time")),
            key=lambda k: int(k.split("_")[1]),
        )[: args.sessions]
        scope = {"user_id": f"bench_{sample_id}"}
        created = 0
        for key in keys:
            messages = [
                {"role": "assistant" if t["speaker"] != sample_id else "user", "content": t["text"]}
                for t in conv[key]
                if (t.get("text") or "").strip()
            ]
            if not messages:
                continue
            r = client.post("/memories", json={"messages": messages[:25], **scope}, headers=headers)
            r.raise_for_status()
            created += sum(1 for e in r.json().get("results", []) if e.get("event") == "ADD")
            report["ingest"]["batches"] += 1
        scopes[sample_id] = scope
        report["per_sample"].append({"sample": sample_id, "sessions": len(keys), "memories": created})
        report["ingest"]["memories"] += created
        print(f"[ingest] {sample_id}: {len(keys)} sessions -> {created} memories", flush=True)
    report["ingest"]["elapsed_s"] = round(time.time() - t0, 1)

    # ---- recall ----
    judged = []
    for sample in dataset:
        sample_id = sample.get("sample_id")
        for qa in sample.get("qa", []):
            question = (qa.get("question") or "").strip()
            if not question:
                continue
            start = time.perf_counter()
            try:
                r = client.post(
                    "/v1/memory/recall",
                    json={**scopes[sample_id], "query": question[:2000], "limit": args.top_k, "mode": "auto"},
                    headers=headers,
                )
                r.raise_for_status()
                body = r.json()
                report["recall"]["questions"] += 1
                report["recall"]["latency_ms"].append(time.perf_counter() - start)
                judged.append((question, qa.get("answer"), body))
            except Exception as exc:
                report["recall"]["errors"] += 1
                print(f"[recall-error] {exc}", flush=True)
    lat = report["recall"]["latency_ms"]
    report["recall"]["p50_ms"] = pct(lat, 0.5)
    report["recall"]["p95_ms"] = pct(lat, 0.95)
    report["recall"]["latency_ms"] = []

    # ---- judge ----
    if args.judge:
        import os

        key = args.dashscope_key or os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            print("DASHSCOPE_API_KEY missing; skipping judge pass", file=sys.stderr)
        else:
            judge_client = httpx.Client(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                headers={"Authorization": f"Bearer {key}"},
                timeout=60.0,
            )
            for question, answer, body in judged:
                memories = "\n".join(f"- {item['memory']}" for item in body.get("results", [])[: args.top_k])
                try:
                    r = judge_client.post(
                        "/chat/completions",
                        json={
                            "model": JUDGE_MODEL,
                            "temperature": 0,
                            "messages": [
                                {"role": "user", "content": JUDGE_PROMPT.format(question=question, answer=answer, memories=memories or "（无）")}
                            ],
                        },
                    )
                    r.raise_for_status()
                    verdict = r.json()["choices"][0]["message"]["content"].strip().lower()
                    for bucket in ("supported", "partial", "unsupported"):
                        if bucket in verdict:
                            report["judge"][bucket] += 1
                            break
                    else:
                        report["judge"]["errors"] += 1
                except Exception:
                    report["judge"]["errors"] += 1
            total = sum(report["judge"][b] for b in ("supported", "partial", "unsupported"))
            if total:
                report["judge"]["supported_rate"] = round(report["judge"]["supported"] / total, 3)

    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.out:
        open(args.out, "w").write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
