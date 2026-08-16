#!/usr/bin/env python
"""BEAM v1 (100K bucket) over the v3 `/v1/memory/*` HTTP surface.

Official-protocol pipeline adapted from `evaluation/benchmarks/beam/run.py`:

- ingest : 20 BEAM 100K conversations. Each chat is a list of time-anchored
           batches; batches are split into 5-turn chunks (v2 LOCOMO chunk
           baseline) and POSTed to /v1/memory/remember with
           timestamp=time_anchor date.
- recall : every probing question -> POST /v1/memory/recall, auto+rerank,
           top_k=50 (the /v1 API cap; official platform uses 100/200).
- answer : qwen-plus-latest with the official BEAM answer prompt.
- judge  : qwen-plus-latest with the official BEAM rubric-nugget prompt,
           one call per nugget, scores clamped to {0, 0.5, 1.0}. A question's
           score is the mean of its nugget scores; pass threshold is 0.5.

Kendall tau-b for event_ordering is intentionally not part of v1 (documented
in the benchmark plan); the rubric-nugget metric is the primary BEAM metric.

    python server/scripts/benchmarks/beam/run.py \
        --email e2e-v3@example.com --password ... \
        --reset --conversations 0 --batch-limit 1 \
        --types information_extraction --question-limit 1 \
        --out .../results/v1-smoke.json
"""

import argparse
import json
import re
import statistics
import sys
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

DEFAULT_DATASET = SCRIPT_DIR / "dataset" / "beam_100k.json"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"

ANSWER_MODEL = "qwen-plus-latest"
JUDGE_MODEL = "qwen-plus-latest"
TENANT_ID = "bench-beam-v1"

BEAM_ANSWER_PROMPT = """You are an AI assistant with access to stored memories from prior conversations with a user.
Use these memories to answer the following question as accurately and completely as possible.

IMPORTANT RULES:
1. Scan ALL provided memories before answering — do not stop after the first relevant one.
2. If multiple memories contain relevant information, combine and cross-reference them.
3. If the memories contain contradictory information, prefer the more recent one.
4. If the memories don't contain enough information to answer, say exactly: "I don't have enough information to answer this question."
5. For temporal questions: pay attention to dates and relative time references.
6. For ordering questions: present events in chronological order.
7. For preference questions: use the most recently stated preference.
8. Be specific and direct — include exact names, dates, numbers, and details from the memories.
9. Do NOT invent or assume information that isn't in the memories.

QUESTION: {question}

RETRIEVED MEMORIES:
{memories}

ANSWER:"""

BEAM_JUDGE_SYSTEM = (
    "You are an expert evaluator assessing whether an AI assistant's response satisfies "
    "specific rubric criteria. You must be objective, fair, and consistent. "
    "Return ONLY valid JSON with the exact format requested."
)

BEAM_JUDGE_USER = """Evaluate whether the following LLM response demonstrates compliance with the specified RUBRIC CRITERION.

QUESTION:
{question}

LLM RESPONSE:
{response}

RUBRIC CRITERION:
{criterion}

SCORING GUIDELINES:

First, determine whether the rubric criterion is a POSITIVE requirement (the response SHOULD include something) or a NEGATIVE constraint (the response SHOULD NOT include something).

**For POSITIVE requirements** (response should contain, mention, or demonstrate something):
- **1.0 (Complete Compliance)**: The required element is present, accurate, and complete. The response fully and clearly satisfies the rubric criterion.
- **0.5 (Partial Compliance)**: The required element is partially present, has minor inaccuracies, or is incomplete. The core intent is present but not fully realized.
- **0.0 (No Compliance)**: The required element is missing, incorrect, or the response is entirely off-topic / non-responsive.

**For NEGATIVE constraints** (response should NOT contain or should avoid something):
- **1.0 (Complete Compliance)**: The response is responsive to the question AND the prohibited element is absent.
- **0.5 (Partial Compliance)**: The response is responsive but contains a borderline or ambiguous reference to the prohibited element.
- **0.0 (No Compliance)**: The prohibited element is present in the response, OR the response is non-responsive (off-topic, refusal, empty).

**Compound statement handling**: If the rubric criterion contains "and" or commas connecting multiple required elements:
- All elements present and correct = 1.0
- Some (but not all) elements present and correct = 0.5
- No elements present or correct = 0.0

EVALUATION RULES:
1. **Semantic tolerance**: Paraphrases and synonyms are acceptable. The response does not need to use the exact same words as the rubric.
2. **Numeric and date equivalence**: Treat equivalent representations as identical. "$68,000" = "68k" = "sixty-eight thousand dollars". "2 years" = "24 months". Prefer normalized comparison for numbers, currencies, dates, and durations.
3. **Case / punctuation / whitespace tolerance**: Differences in capitalization, punctuation, and whitespace must be ignored when comparing content.
4. **Hedging tolerance**: Do not penalize hedging language ("I think", "probably", "it seems"), passive voice, or verbosity if the substantive content satisfies the rubric criterion.
5. **Style neutrality**: Do not penalize for tone, formatting, or length unless the rubric criterion specifically requires a particular format.
6. **Responsiveness**: If the LLM response is completely off-topic or refuses to answer, score 0.0 for all criteria.
7. **Independence**: Evaluate this criterion in isolation — do not consider other rubric items.
8. **Specificity matters**: Vague or generic answers that could apply to any question score lower than specific, detailed answers.

STEP-BY-STEP EVALUATION:
Follow these steps in order:
1. **Understand the Requirement**: Read the rubric criterion and classify it as a positive requirement or a negative constraint.
2. **Parse Compound Statements**: If the criterion contains multiple sub-requirements joined by "and" or commas, identify each element separately.
3. **Check Compliance**: Compare the LLM response against each element, applying the tolerance rules above (semantic, numeric, case, hedging).
4. **Assign Score**: Use the appropriate scoring table (positive or negative) and compound-statement rule to determine the score.
5. **Provide Reasoning**: Write a concise explanation referencing which elements were or were not satisfied.

Return your evaluation as a JSON object with exactly two fields:
{{"score": <0.0 or 0.5 or 1.0>, "reason": "<one concise sentence explaining your score>"}}"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--conversations", default=None, help="indices, e.g. 0-19 or 0,1 (default: all)")
    parser.add_argument("--batch-limit", type=int, default=0, help="first N chat batches per conversation (0=all)")
    parser.add_argument("--types", default=None, help="comma-separated question types (default: all 10)")
    parser.add_argument("--question-limit", type=int, default=0, help="first N questions per selected type (0=all)")
    parser.add_argument("--chunk-size", type=int, default=5)
    parser.add_argument("--tenant", default=TENANT_ID)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--skip-ingest", action="store_true", help="reuse an existing corpus (requires --ingest-tag)")
    parser.add_argument("--ingest-tag", default=None)
    parser.add_argument("--ingest-concurrency", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-answer", action="store_true", help="stop after recall (latency only)")
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


def parse_conversation_indices(spec, count):
    if not spec:
        return list(range(count))
    indices = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            indices.extend(range(int(lo), int(hi) + 1))
        else:
            indices.append(int(part))
    return [i for i in sorted(set(indices)) if 0 <= i < count]


def parse_time_anchor(raw):
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%B-%d-%Y", "%B %d, %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    print(f"[ingest] unparsable time_anchor {raw!r}; using None", flush=True)
    return None


def ingest_conversation(conv, conv_idx, args, client, scope_tag):
    conv_id = str(conv.get("conversation_id") or conv_idx)
    user_id = f"beam_100k_{conv_id}"
    batches = conv.get("chat") or []
    if args.batch_limit:
        batches = batches[: args.batch_limit]
    totals = {"batches": 0, "created": 0, "updated": 0, "noop": 0, "errors": 0}
    records = []
    for batch_idx, turns in enumerate(batches):
        if not isinstance(turns, list):
            continue
        anchor = None
        for turn in turns:
            if isinstance(turn, dict) and turn.get("time_anchor"):
                anchor = parse_time_anchor(turn["time_anchor"])
                break
        messages = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            content = str(turn.get("content") or "").strip()
            role = str(turn.get("role") or "user").strip()
            if not content:
                continue
            if role not in ("user", "assistant"):
                role = "user" if role.lower() in ("human", "user") else "assistant"
            messages.append({"role": role, "content": content})
        for start in range(0, len(messages), args.chunk_size):
            chunk = messages[start : start + args.chunk_size]
            if not chunk:
                continue
            body = {
                "tenant_id": scope_tag,
                "user_id": user_id,
                "mode": "extract",
                "kind": "fact",
                "categories": ["beam"],
                "messages": chunk,
                "timestamp": anchor,
                "metadata": {
                    "benchmark": "beam",
                    "conversation_id": conv_id,
                    "batch_idx": batch_idx,
                    "chunk_start": start,
                    "time_anchor": anchor,
                },
            }
            try:
                resp = client.post("/v1/memory/remember", body, timeout=300.0)
            except Exception as exc:  # noqa: BLE001
                totals["errors"] += 1
                records.append({"batch": batch_idx, "start": start, "error": str(exc)[:300]})
                print(f"[ingest-error] {conv_id} batch {batch_idx}: {exc}", flush=True)
                continue
            totals["batches"] += 1
            for result in resp.get("results", []):
                outcome = result.get("outcome", "noop")
                totals[outcome] = totals.get(outcome, 0) + 1
            records.append(
                {
                    "batch": batch_idx,
                    "start": start,
                    "messages": len(chunk),
                    "timestamp": anchor,
                    "created": sum(1 for r in resp.get("results", []) if r.get("outcome") == "created"),
                }
            )
        print(f"[ingest] {conv_id}: batch {batch_idx} done ({len(messages)} turns)", flush=True)
    return {"conversation_id": conv_id, "user_id": user_id, "records": records, **totals}


def run_ingest(client, dataset, indices, args, report, scope_tag):
    phase = report["phases"]["ingest"]
    done = {rec["conversation_id"] for rec in phase.get("samples", [])}
    started = time.time()
    pending = [idx for idx in indices if str(dataset[idx].get("conversation_id") or idx) not in done]
    with ThreadPoolExecutor(max_workers=max(args.ingest_concurrency, 1)) as pool:
        futures = {pool.submit(ingest_conversation, dataset[idx], idx, args, client, scope_tag): idx for idx in pending}
        for future in as_completed(futures):
            record = future.result()
            phase.setdefault("samples", []).append(record)
            save(args, report)
            print(f"[ingest] {record['conversation_id']} done: {record['batches']} batches", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    totals = {k: sum(rec.get(k, 0) for rec in phase["samples"]) for k in ("batches", "created", "updated", "noop", "errors")}
    print(f"[ingest] done in {phase['elapsed_s']}s: {totals}", flush=True)


def collect_questions(dataset, indices, args, scope_tag):
    questions = []
    for idx in indices:
        conv = dataset[idx]
        conv_id = str(conv.get("conversation_id") or idx)
        probing = conv.get("probing_questions") or {}
        for qtype, qs in probing.items():
            if args.types and qtype not in {t.strip() for t in args.types.split(",")}:
                continue
            if not isinstance(qs, list):
                qs = [qs] if isinstance(qs, dict) else []
            if args.question_limit:
                qs = qs[: args.question_limit]
            for qi, q in enumerate(qs):
                if not isinstance(q, dict):
                    continue
                question = str(q.get("question") or q.get("question_text") or "").strip()
                if not question:
                    continue
                rubric = q.get("rubric") or []
                if isinstance(rubric, dict):
                    rubric = rubric.get("nuggets") or []
                if not isinstance(rubric, list):
                    rubric = [str(rubric)]
                questions.append(
                    {
                        "qid": f"{conv_id}_q{qi}_{qtype}",
                        "conversation_id": conv_id,
                        "conversation_index": idx,
                        "question_type": qtype,
                        "question": question,
                        "rubric": [str(r) for r in rubric if str(r).strip()],
                        "difficulty": q.get("difficulty", "unknown"),
                        "scope": {"tenant_id": scope_tag, "user_id": f"beam_100k_{conv_id}"},
                    }
                )
    return questions


def run_recall(client, questions, args, report):
    phase = report["phases"]["recall"]
    done = {item["qid"] for item in phase.get("items", []) if not item.get("error")}
    pending = [q for q in questions if q["qid"] not in done]
    started = time.time()
    completed = 0

    def work(question):
        t0 = time.perf_counter()
        try:
            resp = client.post(
                "/v1/memory/recall",
                {
                    "tenant_id": question["scope"]["tenant_id"],
                    "user_id": question["scope"]["user_id"],
                    "query": question["question"][:8000],
                    "limit": args.top_k,
                    "mode": "auto",
                    "rerank": True,
                },
                timeout=120.0,
            )
        except Exception as exc:  # noqa: BLE001
            return {**question, "error": str(exc)[:300]}
        latency_ms = (time.perf_counter() - t0) * 1000
        results = resp.get("results", [])
        return {
            **question,
            "latency_ms": round(latency_ms, 1),
            "search_mode": resp.get("search_mode"),
            "degraded": resp.get("degraded"),
            "rerank_status": resp.get("rerank_status"),
            "result_count": len(results),
            "memories": [(r.get("text") or "").strip() for r in results],
            "scores": [r.get("score") for r in results],
            "created_at": [r.get("created_at") for r in results],
        }

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, q): q["qid"] for q in pending}
        for future in as_completed(futures):
            phase.setdefault("items", []).append(future.result())
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[recall] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    print("[recall] done", flush=True)


def memories_text(item, top_k):
    memories = item.get("memories") or []
    dates = item.get("created_at") or []
    if not memories:
        return "(No memories available)"
    lines = []
    for idx, text in enumerate(memories[:top_k]):
        date = dates[idx][:10] if idx < len(dates) and dates[idx] else ""
        lines.append(f"{idx + 1}. [{date}] {text}" if date else f"{idx + 1}. {text}")
    return "\n".join(lines)


def run_answer(dashscope, args, report):
    phase = report["phases"]["answer"]
    done = {item["qid"] for item in phase.get("items", [])}
    pending = [item for item in report["phases"]["recall"].get("items", []) if not item.get("error") and item["qid"] not in done]
    completed = 0

    def work(item):
        prompt = BEAM_ANSWER_PROMPT.format(question=item["question"], memories=memories_text(item, args.top_k))
        raw = dashscope.chat(prompt)
        if "ANSWER:" in raw:
            raw = raw.rsplit("ANSWER:", 1)[-1].strip()
        return {"qid": item["qid"], "prediction": raw[:2000]}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, item): item["qid"] for item in pending}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                phase.setdefault("errors", []).append({"qid": futures[future], "error": str(exc)[:300]})
                print(f"[answer-error] {futures[future]}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[answer] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    save(args, report)
    print("[answer] done", flush=True)


def clamp_score(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value >= 0.75:
        return 1.0
    if value >= 0.25:
        return 0.5
    return 0.0


def run_judge(dashscope, args, report):
    phase = report["phases"]["judge"]
    answers = {item["qid"]: item.get("prediction", "") for item in report["phases"]["answer"].get("items", [])}
    recall = {item["qid"]: item for item in report["phases"]["recall"].get("items", [])}
    done = {item["qid"] for item in phase.get("items", []) if "nugget_scores" in item or "error" in item}
    tasks = []
    for item in recall.values():
        if item.get("error") or item["qid"] not in answers or item["qid"] in done:
            continue
        for ni, nugget in enumerate(item["rubric"]):
            tasks.append((item["qid"], ni, nugget))
    completed = 0

    def work(task):
        qid, ni, nugget = task
        prompt = BEAM_JUDGE_USER.format(
            question=recall[qid]["question"],
            response=answers[qid] or "(empty)",
            criterion=nugget,
        )
        raw = dashscope.chat(prompt, system=BEAM_JUDGE_SYSTEM, json_mode=True)
        parsed = parse_json_object(raw)
        if not parsed:
            raise RuntimeError(f"judge JSON parse failed: {raw[:200]!r}")
        score = clamp_score(parsed.get("score"))
        if score is None:
            raise RuntimeError(f"judge score invalid: {raw[:200]!r}")
        return {"qid": qid, "nugget_index": ni, "nugget": nugget, "score": score, "reason": str(parsed.get("reason", ""))[:300]}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, task): task for task in tasks}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                qid, ni, _ = futures[future]
                phase.setdefault("errors", []).append({"qid": qid, "nugget_index": ni, "error": str(exc)[:300]})
                print(f"[judge-error] {qid} nugget {ni}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[judge] {completed}/{len(tasks)}", flush=True)
    phase["done"] = True
    save(args, report)
    print("[judge] done", flush=True)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * q), len(ordered) - 1)], 1)


def compute_metrics(report):
    recall_items = report["phases"]["recall"].get("items", [])
    answers = {item["qid"]: item.get("prediction", "") for item in report["phases"]["answer"].get("items", [])}
    judge_items = report["phases"]["judge"].get("items", [])
    nuggets_by_qid = {}
    for item in judge_items:
        nuggets_by_qid.setdefault(item["qid"], []).append(item)

    rows = []
    for question in recall_items:
        if question.get("error") or question["qid"] not in answers:
            continue
        nuggets = nuggets_by_qid.get(question["qid"], [])
        if len(nuggets) != len(question.get("rubric", [])):
            continue
        score = statistics.mean(n["score"] for n in nuggets)
        rows.append({**question, "score": score, "pass": score >= 0.5, "nuggets": nuggets})

    by_type = {}
    for row in rows:
        by_type.setdefault(row["question_type"], []).append(row)

    metrics = {
        "questions": len(rows),
        "accuracy": round(sum(1 for r in rows if r["pass"]) / len(rows), 4) if rows else None,
        "avg_score": round(sum(r["score"] for r in rows) / len(rows), 4) if rows else None,
        "answer_errors": len(report["phases"]["answer"].get("errors", [])),
        "judge_errors": len(report["phases"]["judge"].get("errors", [])),
        "recall_errors": sum(1 for item in recall_items if item.get("error")),
        "recall": {
            "questions": len(recall_items),
            "p50_ms": percentile([item["latency_ms"] for item in recall_items if item.get("latency_ms")], 0.5),
            "p95_ms": percentile([item["latency_ms"] for item in recall_items if item.get("latency_ms")], 0.95),
            "avg_results": round(sum(item.get("result_count", 0) for item in recall_items) / len(recall_items), 2) if recall_items else None,
        },
        "by_type": {
            name: {
                "total": len(type_rows),
                "accuracy": round(sum(1 for r in type_rows if r["pass"]) / len(type_rows), 4) if type_rows else None,
                "avg_score": round(sum(r["score"] for r in type_rows) / len(type_rows), 4) if type_rows else None,
            }
            for name, type_rows in sorted(by_type.items())
        },
        "ingest": {
            key: sum(rec.get(key, 0) for rec in report["phases"]["ingest"].get("samples", []))
            for key in ("batches", "created", "updated", "noop", "errors")
        },
    }
    return metrics


def save(args, report):
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
        args.out = str(DEFAULT_RESULTS_DIR / f"beam-v1-{datetime.now():%Y%m%dT%H%M%S}.json")

    dataset = json.loads(Path(args.dataset).read_text())
    if not isinstance(dataset, list) or not dataset:
        raise SystemExit(f"dataset {args.dataset}: expected a non-empty JSON array")
    indices = parse_conversation_indices(args.conversations, len(dataset))
    print(f"[dataset] {len(dataset)} conversations; running indices {indices}", flush=True)

    report = {
        "run_id": f"beam-v1-{datetime.now():%Y%m%dT%H%M%S}",
        "config": {
            "base_url": args.base_url,
            "conversations": indices,
            "batch_limit": args.batch_limit,
            "types": args.types,
            "question_limit": args.question_limit,
            "chunk_size": args.chunk_size,
            "tenant": scope_tag,
            "top_k": args.top_k,
            "answer_model": ANSWER_MODEL,
            "judge_model": JUDGE_MODEL,
            "skip_ingest": args.skip_ingest,
            "tau_b": "not-in-v1",
        },
        "phases": {"ingest": {}, "recall": {}, "answer": {}, "judge": {}},
    }
    if args.resume and Path(args.out).is_file():
        prior = json.loads(Path(args.out).read_text())
        prior_config = prior.get("config", {})
        if prior_config.get("tenant") == scope_tag and prior_config.get("conversations") == indices:
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
        run_ingest(client, dataset, indices, args, report, scope_tag)
    else:
        print("[ingest] already done (resume/skip)", flush=True)

    questions = collect_questions(dataset, indices, args, scope_tag)
    print(f"[plan] {len(questions)} questions selected", flush=True)
    if not report["phases"]["recall"].get("done"):
        run_recall(client, questions, args, report)
    else:
        print("[recall] already done (resume)", flush=True)

    if args.no_answer:
        report["metrics"] = compute_metrics(report)
        save(args, report)
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        return 0

    dashscope = DashScopeClient(resolve_dashscope_key(args.dashscope_key), ANSWER_MODEL)
    judge = DashScopeClient(resolve_dashscope_key(args.dashscope_key), JUDGE_MODEL)
    if not report["phases"]["answer"].get("done"):
        run_answer(dashscope, args, report)
    if not report["phases"]["judge"].get("done"):
        run_judge(judge, args, report)

    report["metrics"] = compute_metrics(report)
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save(args, report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"[done] results: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
