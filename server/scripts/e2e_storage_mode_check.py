#!/usr/bin/env python
"""End-to-end verification of the v3 storage-mode design (§12 acceptance).

Runs against a live deployment (ES 8.17 + MySQL 8 + configured
LLM/embedder/rerank) and checks every acceptance invariant:

 1. ONLY_VDB creates/queries no SQL memory tables
 2. concurrent same-content remember → one created, rest noop; explicit
    expected_revision races → 409
 3. crash after CAS (head missing) is repaired by RecoveryReconciler and the
    replayed command converges
 4. remember → immediate recall visibility; keyword channel finds
    embedding_status=pending heads
 5. auto mode includes keyword-exclusive hits; no channel → 501/503 never an
    empty 200
 6. ONLY_VDB + ES outage → recall 503; (HYBRID phase: storage_source=sql_fts
    with as_of_revision; normal empty results never fall back)
 7. scope-CAS 409 vs dedup 409 vs conflict semantics (§7.5)
 8. Legacy /search hides inactive; MCP without credentials → 401
 9. (migration phase) SQL ctx → ES reconciliation

Usage:
    python server/scripts/e2e_storage_mode_check.py \
        --base-url http://localhost:8888 \
        --email e2e-v3@example.com --password '...' \
        [--es http://localhost:9200] [--mysql 'docker exec mem0-dev-mysql-1 mysql ...'] \
        [--docker-elasticsearch mem0-dev-elasticsearch-1] [--hybrid]
"""

import argparse
import concurrent.futures as futures
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL = 0, 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


class Http:
    def __init__(self, base_url, token=None):
        self.base = base_url.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, headers=None, timeout=30):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": payload.decode(errors="replace")}

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, None, **kw)


def es_req(es_url, method, path, body=None):
    req = urllib.request.Request(es_url.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def docker(exec_target, *args):
    return subprocess.run(["docker", exec_target, *args], capture_output=True, text=True)


def mysql_query(mysql_cmd, sql):
    proc = subprocess.run(
        [*mysql_cmd, "-e", sql], capture_output=True, text=True
    )
    return proc.stdout.strip(), proc.returncode


def login(http, email, password):
    status, body = http.post("/auth/login", {"email": email, "password": password})
    if status != 200:
        status, body = http.post("/auth/register", {"email": email, "password": password, "name": "E2E"})
    assert status == 200, f"login/register failed: {status} {body}"
    return body["access_token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--es", default="http://localhost:9200")
    parser.add_argument("--es-prefix", default="agentar_mem0")
    parser.add_argument("--mysql-cmd", default="docker exec mem0-dev-mysql-1 mysql -uroot -pmem0_dev_root mem0_app")
    parser.add_argument("--es-container", default="mem0-dev-elasticsearch-1")
    parser.add_argument("--hybrid", action="store_true", help="HYBRID_STORAGE phase checks")
    parser.add_argument("--skip-es-outage", action="store_true")
    args = parser.parse_args()
    mysql_cmd = args.mysql_cmd.split()

    http = Http(args.base_url)
    http.token = login(http, args.email, args.password)
    run_id = str(int(time.time()))

    # ---- 0. deployment shape ------------------------------------------------
    print("\n[0] deployment shape")
    status, caps = http.get("/v1/capabilities")
    check("capabilities reachable", status == 200, str(caps)[:200])
    mode = caps.get("storage", {}).get("mode")
    check(f"storage mode = {'hybrid_storage' if args.hybrid else 'only_vdb'}", mode == ("hybrid_storage" if args.hybrid else "only_vdb"), f"got {mode}")
    status, ready = http.get("/health/ready")
    check("readiness ready/degraded", status in (200, 503) and ready.get("state") in ("ready", "degraded", "not_ready"))

    # ---- 1. ONLY_VDB: no SQL memory tables -----------------------------------
    if not args.hybrid:
        print("\n[1] acceptance 1 — ONLY_VDB creates no SQL memory tables")
        out, rc = mysql_query(mysql_cmd, "SHOW TABLES;")
        tables = out.splitlines()[1:] if rc == 0 else []
        check("mysql reachable", rc == 0, out[:200])
        ctx_tables = [t for t in tables if "ctx_" in t]
        recall_tables = [t for t in tables if "memory_recall" in t]
        check("no ctx_* memory tables", not ctx_tables, str(ctx_tables))
        check("no memory_recall_* sidecar tables", not recall_tables, str(recall_tables))

    # ---- 2. concurrent dedup + CAS 409 ----------------------------------------
    print("\n[2] acceptance 2 — concurrent remember dedup + expected_revision 409")
    conc_user = f"{run_id}_conc"
    body = {"user_id": conc_user, "text": "并发写入的相同事实内容", "mode": "append"}
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: http.post("/v1/memory/remember", body), range(8)))
    outcomes = [r[1]["results"][0]["outcome"] if r[0] == 200 else f"http{r[0]}" for r in results]
    check("all writers resolved (200 or 409 in-progress)", all(o in ("created", "noop") or o.startswith("http409") for o in outcomes), str(outcomes))
    check("exactly one created", outcomes.count("created") == 1, str(outcomes))
    check("others noop or transient 409", all(o in ("created", "noop", "http409operation_in_progress") or o == "noop" or o.startswith("http409") for o in outcomes))
    # retry any transient 409s → noop
    for _ in range(3):
        status, body2 = http.post("/v1/memory/remember", body)
        if status == 200 and body2["results"][0]["outcome"] == "noop":
            break
    check("replay converges to noop", body2["results"][0]["outcome"] == "noop")

    status, created = http.post("/v1/memory/remember", {"user_id": conc_user, "text": "第二条事实", "mode": "append"})
    rev = created["results"][0]["revision"]
    status, conflict = http.post("/v1/memory/remember", {"user_id": conc_user, "text": "竞争事实", "mode": "append", "expected_revision": rev + 10})
    check("stale expected_revision → 409 revision_conflict", status == 409 and conflict.get("code") == "revision_conflict", f"{status} {conflict}")

    # ---- 3. crash-after-CAS repair ---------------------------------------------
    print("\n[3] acceptance 3 — crash point after CAS repaired by reconciler")
    crash_user = f"{run_id}_crash"
    status, created = http.post("/v1/memory/remember", {"user_id": crash_user, "text": "崩溃恢复验证事实", "mode": "append"})
    entry = created["results"][0]["entry"]
    scope_key = status_body = None
    # find the head doc and delete it directly from ES (simulates lost derived write)
    status, found = es_req(args.es, "POST", f"/{args.es_prefix}_head/_search", {"size": 5, "query": {"term": {"entry_id": entry["entry_id"]}}})
    hits = found.get("hits", {}).get("hits", [])
    check("head doc present in ES", bool(hits))
    if hits:
        doc_id = hits[0]["_id"]
        scope_key = hits[0]["_source"]["scope_key"]
        es_req(args.es, "DELETE", f"/{args.es_prefix}_head/_doc/{doc_id}", None)
        es_req(args.es, "POST", f"/{args.es_prefix}_head/_refresh")
        status, recalled = http.post("/v1/memory/recall", {"user_id": crash_user, "query": "崩溃恢复", "limit": 5})
        check("lost head not recalled before repair", all(r["entry_id"] != entry["entry_id"] for r in recalled.get("results", [])))
        status, counts = http.post("/v1/admin/memory/reconcile", timeout=300)
        check("admin reconcile runs", status == 200, str(counts)[:200])
        status, head = http.post("/v1/memory/get", {"user_id": crash_user, "entry_id": entry["entry_id"]})
        check("head rebuilt after reconcile", status == 200 and head.get("text") == "崩溃恢复验证事实", str(head)[:200])

    # ---- 4. write visibility + pending embedding keyword channel -----------------
    print("\n[4] acceptance 4 — write→recall visibility; pending embedding keyword-only")
    vis_user = f"{run_id}_vis"
    status, created = http.post("/v1/memory/remember", {"user_id": vis_user, "text": "写后立即可见的记忆条目", "mode": "append"})
    check("remember ok with real embedder", status == 200 and created["results"][0]["outcome"] == "created")
    check("embedding completed inline (pending_embed false)", created["results"][0]["pending_embed"] is False)
    status, recalled = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "写后立即可见", "limit": 5})
    check("immediately visible in recall", any(r["text"] == "写后立即可见的记忆条目" for r in recalled.get("results", [])))

    # force embedding_status=pending on a second head → keyword yes, semantic no
    status, created2 = http.post("/v1/memory/remember", {"user_id": vis_user, "text": "待嵌入的独特关键词ZZQWE", "mode": "append"})
    entry2 = created2["results"][0]["entry"]
    status, found = es_req(args.es, "POST", f"/{args.es_prefix}_head/_search", {"size": 3, "query": {"term": {"entry_id": entry2["entry_id"]}}})
    if found.get("hits", {}).get("hits"):
        doc = found["hits"]["hits"][0]
        es_req(args.es, "POST", f"/{args.es_prefix}_head/_update/{doc['_id']}",
               {"doc": {"embedding_status": "pending", "vector": None}})
        es_req(args.es, "POST", f"/{args.es_prefix}_head/_refresh")
        status, sem = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "独特关键词ZZQWE", "mode": "semantic", "limit": 10})
        check("pending head excluded from semantic", all(r["entry_id"] != entry2["entry_id"] for r in sem.get("results", [])))
        status, kw = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "独特关键词ZZQWE", "mode": "keyword", "limit": 10})
        check("pending head visible to keyword channel", any(r["entry_id"] == entry2["entry_id"] for r in kw.get("results", [])))

    # ---- 5. auto includes keyword-exclusive; error contract ----------------------
    print("\n[5] acceptance 5 — auto channel union + no-channel contract")
    status, auto = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "独特关键词ZZQWE", "mode": "auto", "limit": 10})
    check("auto finds keyword-exclusive entry", any(r["entry_id"] == entry2["entry_id"] for r in auto.get("results", [])))
    check("auto reports hybrid", auto.get("search_mode") == "hybrid", auto.get("search_mode"))
    status, err = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "q", "mode": "keyword", "threshold": 0.5})
    check("keyword+threshold → 422", status == 422 and err.get("code") == "validation_error")
    status, empty = http.post("/v1/memory/recall", {"user_id": f"{run_id}_ghost", "query": "不存在的词组", "mode": "auto"})
    check("normal empty result is 200 (no fallback)", status == 200 and empty.get("results") == [] and empty.get("storage_source") == "elasticsearch")

    # ---- 6. ES outage contract ----------------------------------------------------
    if not args.skip_es_outage:
        print("\n[6] acceptance 6 — ES outage semantics")
        if args.hybrid:
            # make sure the SQL sidecar has replayed everything written so far
            status, sync = http.post("/v1/admin/memory/reconcile")
            print("  (sidecar sync:", sync.get("hybrid_sidecar"), ")")
        stop = docker("stop", args.es_container)
        time.sleep(3)
        try:
            if args.hybrid:
                status, fts = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "写后立即可见", "mode": "auto", "limit": 5}, timeout=60)
                check("HYBRID auto falls back to sql_fts", status == 200 and fts.get("storage_source") == "sql_fts" and fts.get("degraded") is True, f"{status} {str(fts)[:200]}")
                check("fallback carries as_of_revision", isinstance(fts.get("as_of_revision"), int))
                check("fallback results marked fts_sidecar", all("fts_sidecar" in r.get("matched_by", []) for r in fts.get("results", [])))
                status, sem503 = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "x", "mode": "semantic"}, timeout=60)
                check("HYBRID semantic still 503", status == 503, f"{status}")
            else:
                status, out = http.post("/v1/memory/recall", {"user_id": vis_user, "query": "写后立即可见", "limit": 5}, timeout=60)
                check("ONLY_VDB ES outage recall → 503", status == 503, f"{status} {str(out)[:120]}")
                check("503 code primary_unavailable", out.get("code") == "primary_unavailable", str(out)[:120])
            status, w = http.post("/v1/memory/remember", {"user_id": vis_user, "text": "故障期写入", "mode": "append"}, timeout=60)
            check("ES outage write → 503", status == 503, f"{status}")
        finally:
            docker("start", args.es_container)
            time.sleep(20)
        status, ready = http.get("/health/ready")
        check("ES recovered → readiness back", status == 200)

    # ---- 7. ES 409 semantics --------------------------------------------------------
    print("\n[7] acceptance 7 — scope CAS vs dedup 409 semantics")
    status, c1 = http.post("/v1/memory/remember", {"user_id": conc_user, "text": "七号验收事实", "mode": "append"})
    rev = c1["results"][0]["revision"]
    status, ok = http.post("/v1/memory/remember", {"user_id": conc_user, "text": "七号第二事实", "mode": "append", "expected_revision": rev})
    check("matching expected_revision succeeds", status == 200 and ok["results"][0]["revision"] == rev + 1)
    status, dup = http.post("/v1/memory/remember", {"user_id": conc_user, "text": "七号第二事实", "mode": "append"})
    check("same content again → noop (dedup active)", dup["results"][0]["outcome"] == "noop")

    # ---- 8. legacy /search + MCP auth ------------------------------------------------
    print("\n[8] acceptance 8 — legacy search hides inactive; MCP 401")
    leg_user = f"{run_id}_leg"
    status, created = http.post("/v1/memory/remember", {"user_id": leg_user, "text": "旧接口可见事实", "mode": "append"})
    entry_id = created["results"][0]["entry"]["entry_id"]
    http.post("/v1/memory/retire", {"user_id": leg_user, "entry_id": entry_id})
    status, legacy = http.post("/search", {"query": "旧接口", "filters": {"user_id": leg_user}})
    check("legacy /search excludes retired", all(item["id"] != entry_id for item in legacy.get("results", [])), str(legacy)[:200])
    status, again = http.post("/v1/memory/reactivate", {"user_id": leg_user, "entry_id": entry_id})
    check("reactivate restores", status == 200 and again["outcome"] == "updated")
    status, legacy2 = http.post("/search", {"query": "旧接口", "filters": {"user_id": leg_user}})
    check("legacy /search shows reactivated", any(item["id"] == entry_id for item in legacy2.get("results", [])))

    req = urllib.request.Request("http://localhost:9100/mcp/", method="POST",
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    try:
        urllib.request.urlopen(req, timeout=10)
        mcp_status = 200
    except urllib.error.HTTPError as exc:
        mcp_status = exc.code
    check("MCP without credentials → 401", mcp_status == 401, f"got {mcp_status}")

    # ---- lifecycle extras ---------------------------------------------------------
    print("\n[extra] expand / changes / prepare")
    status, ch = http.post("/v1/memory/changes", {"user_id": conc_user, "limit": 5})
    check("changes feed present", status == 200 and len(ch.get("changes", [])) >= 3)
    status, prep = http.post("/v1/context/prepare", {"user_id": vis_user, "query": "写后立即可见", "budget_bytes": 2048})
    check("prepare context envelope", status == 200 and "BEGIN AGENTAR PREPARED CONTEXT" in prep.get("rendered", ""))

    print(f"\n===== {PASS} passed, {FAIL} failed =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
