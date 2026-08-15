"""Review Inbox page: a dependency-free HTML shell served by the API
process (design §7 "Dashboard 审查页"). All operations go through the
admin-authenticated /v1/artifact-candidates/* endpoints — the page itself
carries no authority, the operator supplies admin credentials."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Agentar · 候选审查</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;margin:2rem;background:#0f1115;color:#e6e6e6}
 h1{font-size:1.2rem} .card{border:1px solid #2a2f3a;border-radius:8px;padding:1rem;margin:1rem 0;background:#161a22}
 .meta{color:#8b93a7;font-size:.8rem} pre{white-space:pre-wrap;font-size:.8rem}
 button{margin-right:.5rem;padding:.35rem .8rem;border-radius:6px;border:0;cursor:pointer}
 .approve{background:#2e9e5b;color:#fff}.reject{background:#c0392b;color:#fff}
 input,select{padding:.35rem;border-radius:6px;border:1px solid #2a2f3a;background:#0f1115;color:#e6e6e6}
</style>
</head>
<body>
<h1>Agentar 记忆平台 · 候选审查（Review Inbox）</h1>
<div>
 <input id="token" type="password" placeholder="管理员 JWT（/auth/login 获取）" size="48">
 <select id="scopeKey"><option value="">选择 scope（格式 user_id=...）</option></select>
 <button onclick="load()">刷新待审</button>
</div>
<div id="list"></div>
<script>
const api = (path, body) => fetch(path, {
  method: "POST",
  headers: {"Content-Type": "application/json", "Authorization": "Bearer " + document.getElementById("token").value},
  body: JSON.stringify(body || {}),
});
async function load() {
  const scope = parseScope();
  const res = await api("/v1/artifact-candidates/list", {...scope, status: "pending"});
  const data = await res.json();
  const list = document.getElementById("list");
  list.innerHTML = "";
  for (const c of (data.candidates || [])) {
    const card = document.createElement("div");
    card.className = "card";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(c, null, 2);
    const approve = document.createElement("button");
    approve.className = "approve"; approve.textContent = "批准";
    approve.onclick = async () => { await api("/v1/artifact-candidates/approve", {...scope, candidate_id: c.candidate_id, expected_version: c.version}); load(); };
    const reject = document.createElement("button");
    reject.className = "reject"; reject.textContent = "拒绝";
    reject.onclick = async () => {
      const reason = prompt("拒绝理由：") || "";
      await api("/v1/artifact-candidates/reject", {...scope, candidate_id: c.candidate_id, expected_version: c.version, decision_reason: reason});
      load();
    };
    card.append(pre, approve, reject);
    list.append(card);
  }
  if (!(data.candidates || []).length) list.innerHTML = "<p class='meta'>暂无待审候选</p>";
}
function parseScope() {
  const raw = prompt("输入 scope（如 user_id=u1 或 tenant_id=t1&user_id=u1）：", localStorage.getItem("scope") || "user_id=u1") || "";
  localStorage.setItem("scope", raw);
  const scope = {};
  for (const kv of raw.split("&")) { const [k, v] = kv.split("="); if (k && v) scope[k.trim()] = v.trim(); }
  return scope;
}
</script>
</body>
</html>
"""


@router.get("/review-inbox", response_class=HTMLResponse, include_in_schema=False)
def review_inbox_page():
    return HTMLResponse(_PAGE)
