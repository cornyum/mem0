#!/usr/bin/env python
"""LOCOMO benchmark over the v3 `/v1/memory/*` HTTP surface.

Full official-protocol pipeline (category 1-4 scored, category 5 excluded):

- ingest : each session (5 turns/batch by default; --chunk-size 1 mirrors the
           official runner) -> POST /v1/memory/remember (mode=extract, LLM
           knowledge processing, timestamp=session date), one scope per
           conversation: user_id = locomo_{ingest_tag}_{sample_id}.
- recall : every scored QA -> POST /v1/memory/recall (mode auto/semantic/
           keyword, optional rerank), sequential for latency fidelity.
- answer : qwen-plus-latest generates an answer from the recalled memories
           (reference date = last session date of that conversation).
- judge  : qwen-plus-latest judges CORRECT/WRONG with the official tolerance
           rules (partial credit, paraphrase, +/-14 days, 50% duration).

Every phase checkpoints into the results JSON; `--resume` reloads it and
skips already-completed work. Stdlib only.

    python server/scripts/benchmarks/locomo/run.py \
        --email e2e-v3@example.com --password ... \
        --reset --samples 1 --judge \
        --out server/scripts/benchmarks/locomo/results/smoke.json
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = SCRIPT_DIR / "dataset" / "locomo10.json"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
SERVER_ENV = SCRIPT_DIR.parents[2] / ".env"

ANSWER_MODEL = "qwen-plus-latest"
JUDGE_MODEL = "qwen-plus-latest"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

CATEGORY_NAMES = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}
EVAL_CATEGORIES = (1, 2, 3, 4)
DATE_FORMATS = (
    "%I:%M %p on %d %B, %Y",
    "%I:%M %p on %d %B %Y",
    "%d %B, %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%d/%m/%Y",
)

ANSWER_PROMPT = """You are an expert at answering questions from conversation memories.

Today's date (reference): {reference_date}
{profile}

You have the following memories extracted from the conversation history:
{memories}

Question: {question}

Instructions:
- Answer using ONLY the information in the memories above.
- If the memories do not contain the answer, reply exactly: ANSWER: No information available.
- Prefer a short phrase (a name, date, number, or short list) over a full sentence.
- Do not add explanations.
End your response with the final answer on its own line in the format:
ANSWER: <final answer>"""

JUDGE_PROMPT = """You are an impartial judge evaluating question-answering accuracy.

Question: {question}
Gold answer: {gold}
Predicted answer: {prediction}

Judge rules:
- The prediction is CORRECT if it matches the gold answer in meaning; paraphrases, synonyms and aliases are acceptable.
- For questions asking for multiple items, the prediction is CORRECT if it contains at least one correct item.
- Date answers within +/-14 days of the gold date are CORRECT.
- Duration answers within 50% of the gold duration are CORRECT.
- Predictions referring to the same entity as the gold answer are CORRECT.
- "No information available", empty or unrelated predictions are WRONG.

Reply with exactly one word: CORRECT or WRONG."""


class BenchmarkHttpError(RuntimeError):
    def __init__(self, status, payload):
        super().__init__(f"HTTP {status}: {payload[:300]}")
        self.status = status
        self.payload = payload


def http_json(method, url, *, headers=None, body=None, timeout=60.0, attempts=3, backoff=10.0):
    """JSON request with bounded retry: 5xx/429/transport errors retried, 4xx not."""
    last = None
    for attempt in range(1, attempts + 1):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors="replace")
            if exc.code < 500 and exc.code != 429:
                raise BenchmarkHttpError(exc.code, payload) from None
            last = BenchmarkHttpError(exc.code, payload)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        if attempt < attempts:
            time.sleep(min(backoff * attempt, 30.0))
    raise last


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", default=None, help="login email (admin; else --no-auth)")
    parser.add_argument("--password", default=None)
    parser.add_argument("--no-auth", action="store_true", help="server running with AUTH_DISABLED=true")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--samples", type=int, default=0, help="first N conversations (0 = all 10)")
    parser.add_argument("--sample-ids", default=None, help="comma-separated sample_ids override --samples")
    parser.add_argument("--sessions", type=int, default=0, help="first N sessions per conversation (0 = all)")
    parser.add_argument("--ingest", choices=("extract", "append"), default="extract")
    parser.add_argument(
        "--chunk-size", type=int, default=5, help="turns per remember-extract batch (official runner uses 1)"
    )
    parser.add_argument(
        "--inline-session-date",
        dest="inline_session_date",
        action="store_true",
        default=True,
        help="weave the session date into each turn's content (default); --no-inline-session-date relies on timestamp only",
    )
    parser.add_argument(
        "--no-inline-session-date",
        dest="inline_session_date",
        action="store_false",
        help="do not weave the session date into turn content; pass it as the remember timestamp instead",
    )
    parser.add_argument("--recall-mode", choices=("auto", "semantic", "keyword"), default="auto")
    parser.add_argument("--rerank", dest="rerank", action="store_true", default=True)
    parser.add_argument("--no-rerank", dest="rerank", action="store_false")
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--reset", action="store_true", help="POST /reset (or direct ES index delete) before ingest")
    parser.add_argument("--es-url", default="http://localhost:9200", help="ES fallback for --reset")
    parser.add_argument("--es-prefix", default="agentar_mem0")
    parser.add_argument("--no-answer", action="store_true", help="stop after recall (latency only)")
    parser.add_argument("--dashscope-key", default=None, help="explicit key (else env DASHSCOPE_API_KEY / server/.env)")
    parser.add_argument("--ingest-concurrency", type=int, default=2, help="conversations ingested in parallel")
    parser.add_argument("--concurrency", type=int, default=4, help="answer/judge parallelism")
    parser.add_argument("--resume", action="store_true", help="reuse completed phases from --out")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not args.no_auth and (not args.email or not args.password):
        parser.error("--email/--password required (or --no-auth)")
    if not args.no_answer and not resolve_dashscope_key(args.dashscope_key):
        parser.error("DASHSCOPE_API_KEY missing for answer/judge (env or server/.env)")
    return args


def resolve_dashscope_key(explicit):
    if explicit:
        return explicit
    import os

    env_key = os.environ.get("DASHSCOPE_API_KEY")
    if env_key:
        return env_key
    if SERVER_ENV.is_file():
        match = re.search(r"^DASHSCOPE_API_KEY=(.+)$", SERVER_ENV.read_text(), re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


class ServerClient:
    def __init__(self, args):
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.headers = {}
        if not args.no_auth:
            self._login()

    def _login(self):
        token = http_json(
            "POST",
            f"{self.base}/auth/login",
            body={"email": self.args.email, "password": self.args.password},
            timeout=30,
        )["access_token"]
        self.headers["Authorization"] = f"Bearer {token}"

    def post(self, path, body, timeout=60.0):
        return self._call("POST", path, body=body, timeout=timeout)

    def get(self, path, timeout=30.0):
        return self._call("GET", path, timeout=timeout)

    def _call(self, method, path, body=None, timeout=60.0):
        # JWTs expire mid-run on long benchmarks: one transparent re-login on 401
        try:
            return http_json(method, f"{self.base}{path}", headers=self.headers, body=body, timeout=timeout)
        except BenchmarkHttpError as exc:
            if exc.status != 401 or self.args.no_auth:
                raise
            self._login()
            return http_json(method, f"{self.base}{path}", headers=self.headers, body=body, timeout=timeout)


def load_dataset(args):
    path = Path(args.dataset)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[dataset] downloading {LOCOMO_URL} -> {path}", flush=True)
        req = urllib.request.Request(LOCOMO_URL, headers={"User-Agent": "mem0-locomo-benchmark"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as fh:
            fh.write(resp.read())
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not data:
        raise SystemExit(f"dataset {path}: expected a non-empty JSON array")
    if args.sample_ids:
        wanted = [sid.strip() for sid in args.sample_ids.split(",") if sid.strip()]
        data = [item for item in data if item.get("sample_id") in wanted]
        missing = set(wanted) - {item.get("sample_id") for item in data}
        if missing:
            raise SystemExit(f"dataset {path}: sample_ids not found: {sorted(missing)}")
    elif args.samples:
        data = data[: args.samples]
    for item in data:
        if not item.get("qa"):
            raise SystemExit(f"dataset item {item.get('sample_id')}: no qa list")
    return data


def parse_session_date(raw):
    text = re.sub(r"\s+", " ", (raw or "").strip())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def iter_sessions(conv):
    """(session_key, date_str, parsed_date|None, turns) in chronological order."""
    entries = []
    for key in conv:
        match = re.match(r"^session_(\d+)$", key)
        if not match:
            continue
        turns = [t for t in conv[key] or [] if (t.get("text") or "").strip() or (t.get("blip_caption") or "").strip()]
        if not turns:
            continue
        date_raw = conv.get(f"{key}_date_time") or ""
        entries.append((int(match.group(1)), key, date_raw, parse_session_date(date_raw), turns))
    entries.sort(key=lambda e: (e[3] or datetime.min, e[0]))
    return [(key, date_raw, parsed, turns) for _, key, date_raw, parsed, turns in entries]


def turn_text(turn):
    """Official-runner photo merge: blip_caption/query become an inline image tag."""
    text = (turn.get("text") or "").strip()
    blip = (turn.get("blip_caption") or "").strip()
    query = (turn.get("query") or "").strip()
    if query and blip:
        photo = f"[Sharing image - query: {query}. The image shows: {blip}]"
    elif blip:
        photo = f"[Sharing image that shows: {blip}]"
    elif query:
        photo = f"[Sharing image - query for: {query}]"
    else:
        photo = ""
    return f"{text} {photo}".strip() if photo else text


def date_display(parsed, date_raw):
    return parsed.strftime("%d %B %Y") if parsed else (date_raw or "").strip()


def build_messages(turns, parsed_date, date_raw, speaker_a, inline_date=True):
    """Official runner role mapping: speaker_a -> user, everyone else assistant.

    The v3 extract prompt (ADDITIVE pipeline) extracts from BOTH roles, so the
    old "double user" workaround for the OSS assistant-penalizing prompt is no
    longer needed. The session date is optionally woven into each turn and is
    always available separately as the remember ``timestamp``.
    """
    stamp = date_display(parsed_date, date_raw)
    prefix = f" ({stamp})" if stamp and inline_date else ""
    messages = []
    for turn in turns:
        content = turn_text(turn)
        if not content:
            continue
        role = "user" if not speaker_a or turn.get("speaker") == speaker_a else "assistant"
        messages.append({"role": role, "content": f"{turn.get('speaker', '?')}{prefix}: {content}"})
    return messages


def observation_date(parsed, date_raw):
    """ISO observation date for the remember timestamp."""
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return (date_raw or "").strip() or None


def ingest_sample(client, sample, args, tag):
    sample_id = sample.get("sample_id")
    conv = sample["conversation"]
    speaker_a = conv.get("speaker_a")
    user_id = f"locomo_{tag}_{sample_id}"
    sessions = iter_sessions(conv)
    if args.sessions:
        sessions = sessions[: args.sessions]
    totals = {"batches": 0, "created": 0, "updated": 0, "noop": 0, "errors": 0}
    for key, date_raw, parsed, turns in sessions:
        if args.ingest == "append":
            stamp = date_display(parsed, date_raw)
            prefix = f" ({stamp})" if stamp else ""
            units = [{"text": f"{t.get('speaker', '?')}{prefix}: {turn_text(t)}"} for t in turns if turn_text(t)]
        else:
            units = [
                {
                    "messages": build_messages(
                        turns[i : i + args.chunk_size],
                        parsed,
                        date_raw,
                        speaker_a,
                        inline_date=args.inline_session_date,
                    ),
                    "timestamp": observation_date(parsed, date_raw),
                }
                for i in range(0, len(turns), args.chunk_size)
            ]
        units = [u for u in units if u.get("messages") or u.get("text")]
        for unit in units:
            body = {
                "user_id": user_id,
                "mode": args.ingest,
                "kind": "fact",
                "categories": ["locomo"],
                "metadata": {"locomo_sample_id": sample_id, "locomo_session": key, "session_date": date_raw},
                **unit,
            }
            try:
                resp = client.post("/v1/memory/remember", body, timeout=300.0)
            except Exception as exc:  # noqa: BLE001 - count and continue, surfaced in report
                totals["errors"] += 1
                print(f"[ingest-error] {sample_id} {key}: {exc}", flush=True)
                continue
            totals["batches"] += 1
            for result in resp.get("results", []):
                outcome = result.get("outcome", "noop")
                totals[outcome] = totals.get(outcome, 0) + 1
    record = {"sample": sample_id, "user_id": user_id, "sessions": len(sessions), **totals}
    print(f"[ingest] {sample_id}: {len(sessions)} sessions, {totals['batches']} batches, {totals}", flush=True)
    return record


def run_ingest(client, dataset, args, tag, report):
    existing = {rec["sample"]: rec for rec in report["phases"]["ingest"].get("samples", [])}
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(args.ingest_concurrency, 1)) as pool:
        futures = {
            pool.submit(ingest_sample, client, sample, args, tag): sample.get("sample_id")
            for sample in dataset
            if sample.get("sample_id") not in existing
        }
        for future in as_completed(futures):
            report["phases"]["ingest"].setdefault("samples", []).append(future.result())
    phase = report["phases"]["ingest"]
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    totals = {
        k: sum(rec.get(k, 0) for rec in phase["samples"]) for k in ("batches", "created", "updated", "noop", "errors")
    }
    print(f"[ingest] done in {phase['elapsed_s']}s: {totals}", flush=True)


def collect_questions(dataset, args, tag):
    """Flatten scored QAs; qid = {sample_id}#{index-in-original-qa-list}."""
    questions = []
    for sample in dataset:
        sample_id = sample.get("sample_id")
        user_id = f"locomo_{tag}_{sample_id}"
        for idx, qa in enumerate(sample.get("qa", [])):
            category = qa.get("category")
            if category not in EVAL_CATEGORIES:
                continue
            question = str(qa.get("question") or "").strip()
            if not question:
                continue
            gold = str(qa.get("answer") or "").strip()  # 6 scored answers are ints in locomo10.json
            if category == 3 and ";" in gold:  # official preprocess: category-3 gold truncates at ';'
                gold = gold.split(";", 1)[0].strip()
            questions.append(
                {
                    "qid": f"{sample_id}#{idx}",
                    "sample_id": sample_id,
                    "user_id": user_id,
                    "question": question,
                    "answer": gold,
                    "category": category,
                }
            )
    return questions


def run_recall(client, questions, args, report):
    phase = report["phases"]["recall"]
    done = {item["qid"] for item in phase.get("items", []) if not item.get("error")}  # errors are retried
    started = time.time()
    for pos, question in enumerate(questions):
        if question["qid"] in done:
            continue
        t0 = time.perf_counter()
        try:
            resp = client.post(
                "/v1/memory/recall",
                {
                    "user_id": question["user_id"],
                    "query": question["question"][:8000],
                    "limit": args.top_k,
                    "mode": args.recall_mode,
                    "rerank": args.rerank,
                },
                timeout=120.0,
            )
        except Exception as exc:  # noqa: BLE001 - per-question errors are counted, not fatal
            item = {**question, "error": str(exc)[:300]}
            print(f"[recall-error] {question['qid']}: {exc}", flush=True)
        else:
            latency_ms = (time.perf_counter() - t0) * 1000
            results = resp.get("results", [])
            item = {
                **question,
                "latency_ms": round(latency_ms, 1),
                "search_mode": resp.get("search_mode"),
                "degraded": resp.get("degraded"),
                "rerank_status": resp.get("rerank_status"),
                "result_count": len(results),
                "memories": [(r.get("text") or "").strip() for r in results],
                "scores": [r.get("score") for r in results],
            }
        phase.setdefault("items", []).append(item)
        if (pos + 1) % 100 == 0:
            save(args, report)
            print(f"[recall] {pos + 1}/{len(questions)}", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    print(f"[recall] done in {phase['elapsed_s']}s", flush=True)


class DashScopeClient:
    def __init__(self, api_key):
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def chat(self, prompt, model, system=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = http_json(
            "POST",
            f"{DASHSCOPE_BASE}/chat/completions",
            headers=self.headers,
            body={"model": model, "temperature": 0, "messages": messages},
            timeout=180.0,
        )
        return resp["choices"][0]["message"]["content"].strip()


def reference_profile(sample):
    conv = sample["conversation"]
    speaker_a = conv.get("speaker_a", "the user")
    speaker_b = conv.get("speaker_b", "another person")
    sessions = iter_sessions(conv)
    dates = [parsed for _, _, parsed, _ in sessions if parsed]
    reference = max(dates).strftime("%d %B %Y") if dates else "unknown"
    profile = (
        f"User profile: the memories come from long-running conversations between "
        f"{speaker_a} and {speaker_b}; facts about both speakers may appear."
    )
    return reference, profile


def answer_question(dashscope, question, reference, profile, top_k):
    memories = question.get("memories") or []
    block = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(memories[:top_k])) or "(no memories retrieved)"
    content = dashscope.chat(
        ANSWER_PROMPT.format(
            reference_date=reference,
            profile=profile,
            memories=block,
            question=question["question"],
        ),
        ANSWER_MODEL,
    )
    parts = re.split(r"ANSWER:", content, flags=re.IGNORECASE)
    return (parts[-1].strip() if len(parts) > 1 else content.strip())[:2000]


def run_answer(dashscope, dataset, args, report):
    phase = report["phases"]["answer"]
    done = {item["qid"]: item for item in phase.get("items", [])}
    references = {s.get("sample_id"): reference_profile(s) for s in dataset}
    pending = [
        item for item in report["phases"]["recall"]["items"] if not item.get("error") and item["qid"] not in done
    ]
    started = time.time()
    completed = 0

    def work(question):
        reference, profile = references[question["sample_id"]]
        prediction = answer_question(dashscope, question, reference, profile, args.top_k)
        return {"qid": question["qid"], "prediction": prediction}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, q): q["qid"] for q in pending}
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
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    print(f"[answer] done in {phase['elapsed_s']}s", flush=True)


def parse_verdict(text):
    lines = [line.strip().upper() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if "WRONG" in line or "INCORRECT" in line:
            return False
        if "CORRECT" in line:
            return True
    if lines and lines[-1]:
        return "CORRECT" in text.upper()
    return None


def run_judge(dashscope, args, report):
    phase = report["phases"]["judge"]
    answers = {item["qid"]: item.get("prediction", "") for item in report["phases"]["answer"].get("items", [])}
    done = {item["qid"] for item in phase.get("items", []) if "verdict" in item}
    pending = [
        item for item in report["phases"]["recall"]["items"] if item["qid"] in answers and item["qid"] not in done
    ]
    started = time.time()
    completed = 0

    def work(question):
        verdict_text = dashscope.chat(
            JUDGE_PROMPT.format(
                question=question["question"],
                gold=question["answer"],
                prediction=answers[question["qid"]] or "(empty)",
            ),
            JUDGE_MODEL,
        )
        verdict = parse_verdict(verdict_text)
        if verdict is None:
            raise RuntimeError(f"unparseable verdict: {verdict_text[:120]!r}")
        return {"qid": question["qid"], "verdict": verdict, "judge_raw": verdict_text[:200]}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        futures = {pool.submit(work, q): q["qid"] for q in pending}
        for future in as_completed(futures):
            try:
                phase.setdefault("items", []).append(future.result())
            except Exception as exc:  # noqa: BLE001
                phase.setdefault("errors", []).append({"qid": futures[future], "error": str(exc)[:300]})
                print(f"[judge-error] {futures[future]}: {exc}", flush=True)
            completed += 1
            if completed % 100 == 0:
                save(args, report)
                print(f"[judge] {completed}/{len(pending)}", flush=True)
    phase["done"] = True
    phase["elapsed_s"] = round(time.time() - started, 1)
    save(args, report)
    print(f"[judge] done in {phase['elapsed_s']}s", flush=True)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * q), len(ordered) - 1)], 1)


def compute_metrics(report):
    recall_items = report["phases"].get("recall", {}).get("items", [])
    answer_errors = len(report["phases"].get("answer", {}).get("errors", []))
    judge_items = [i for i in report["phases"].get("judge", {}).get("items", []) if "verdict" in i]
    judge_errors = len(report["phases"].get("judge", {}).get("errors", []))

    by_qid = {i["qid"]: i for i in judge_items}
    metrics = {
        "questions_scored": len(judge_items),
        "judge_errors": judge_errors,
        "answer_errors": answer_errors,
        "recall_errors": sum(1 for i in recall_items if i.get("error")),
        "accuracy": None,
        "per_category": {},
        "per_sample": {},
        "recall": {
            "questions": len(recall_items),
            "p50_ms": percentile([i["latency_ms"] for i in recall_items if i.get("latency_ms")], 0.5),
            "p95_ms": percentile([i["latency_ms"] for i in recall_items if i.get("latency_ms")], 0.95),
            "avg_results": round(sum(i.get("result_count", 0) for i in recall_items) / len(recall_items), 2)
            if recall_items
            else None,
        },
    }
    if judge_items:
        # judge items carry only qid/verdict; join back to recall items for
        # category/sample_id dimensions
        meta = {i["qid"]: i for i in recall_items}
        metrics["accuracy"] = round(sum(1 for i in judge_items if i["verdict"]) / len(judge_items), 4)
        for category, name in CATEGORY_NAMES.items():
            if category not in EVAL_CATEGORIES:
                continue
            subset = [i for i in judge_items if meta[i["qid"]]["category"] == category]
            if subset:
                metrics["per_category"][name] = {
                    "total": len(subset),
                    "correct": sum(1 for i in subset if i["verdict"]),
                    "accuracy": round(sum(1 for i in subset if i["verdict"]) / len(subset), 4),
                }
        sample_ids = []
        for item in judge_items:
            sample_id = meta[item["qid"]]["sample_id"]
            if sample_id not in sample_ids:
                sample_ids.append(sample_id)
        for sample_id in sample_ids:
            subset = [by_qid[qid] for qid in by_qid if meta[qid]["sample_id"] == sample_id]
            metrics["per_sample"][sample_id] = {
                "total": len(subset),
                "correct": sum(1 for i in subset if i["verdict"]),
                "accuracy": round(sum(1 for i in subset if i["verdict"]) / len(subset), 4) if subset else None,
            }
    return metrics


def save(args, report):
    report["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    tmp.replace(path)


def reset_server(client, args):
    try:
        client.post("/reset", {})
        print("[reset] POST /reset ok", flush=True)
        return
    except Exception as exc:  # noqa: BLE001 - fall through to direct ES delete
        print(f"[reset] POST /reset failed ({exc}); falling back to direct ES delete", flush=True)
    http_json("DELETE", f"{args.es_url}/{args.es_prefix}_*", timeout=60.0)
    print(f"[reset] deleted ES indices {args.es_prefix}_*", flush=True)


def wait_ready(client, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = client.get("/health/ready", timeout=10.0).get("state")
            if state == "ready":
                return
        except Exception:  # noqa: BLE001 - poll until deadline
            pass
        time.sleep(3.0)
    raise SystemExit("server did not become ready in time")


def main(argv=None):
    args = parse_args(argv)
    tag = f"{args.ingest}-{args.recall_mode}" + ("-rerank" if args.rerank else "-norank")
    if not args.out:
        DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        args.out = str(DEFAULT_RESULTS_DIR / f"locomo-{tag}-{datetime.now():%Y%m%dT%H%M%S}.json")

    dataset = load_dataset(args)
    print(f"[dataset] {len(dataset)} conversations from {args.dataset}", flush=True)

    report = {
        "run_id": f"{tag}-{datetime.now():%Y%m%dT%H%M%S}",
        "config": {
            "base_url": args.base_url,
            "ingest_mode": args.ingest,
            "recall_mode": args.recall_mode,
            "rerank": args.rerank,
            "top_k": args.top_k,
            "samples": [s.get("sample_id") for s in dataset],
            "sessions_limit": args.sessions,
            "answer_model": ANSWER_MODEL,
            "judge_model": JUDGE_MODEL,
            "capabilities": None,
        },
        "phases": {"ingest": {}, "recall": {}, "answer": {}, "judge": {}},
    }
    if args.resume and Path(args.out).is_file():
        prior = json.loads(Path(args.out).read_text())
        if (
            prior.get("config", {}).get("ingest_mode") == args.ingest
            and prior["config"].get("recall_mode") == args.recall_mode
        ):
            report = prior
            print(
                f"[resume] reusing {args.out} (ingest/recall done: "
                f"{report['phases']['ingest'].get('done')}/{report['phases']['recall'].get('done')})",
                flush=True,
            )
        else:
            print("[resume] config mismatch in existing file; starting fresh", flush=True)

    client = ServerClient(args)
    wait_ready(client)
    report["config"]["capabilities"] = client.get("/v1/capabilities")
    save(args, report)

    if args.reset and not report["phases"]["ingest"].get("done"):
        reset_server(client, args)
        wait_ready(client)

    if not report["phases"]["ingest"].get("done"):
        run_ingest(client, dataset, args, tag, report)
    else:
        print("[ingest] already done (resume)", flush=True)

    questions = collect_questions(dataset, args, tag)
    print(f"[plan] {len(questions)} scored questions (categories {EVAL_CATEGORIES})", flush=True)
    if not report["phases"]["recall"].get("done"):
        run_recall(client, questions, args, report)
    else:
        print("[recall] already done (resume)", flush=True)

    if args.no_answer:
        report["metrics"] = compute_metrics(report)
        save(args, report)
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        return 0

    dashscope = DashScopeClient(resolve_dashscope_key(args.dashscope_key))
    if not report["phases"]["answer"].get("done"):
        run_answer(dashscope, dataset, args, report)
    if not report["phases"]["judge"].get("done"):
        run_judge(dashscope, args, report)

    report["metrics"] = compute_metrics(report)
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save(args, report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"[done] results: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
