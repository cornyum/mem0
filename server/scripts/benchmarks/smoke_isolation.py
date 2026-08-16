#!/usr/bin/env python
"""Isolation smoke: tenant_id / user_id / agent_id scoping on /v1/memory/*.

Uses mode=append (no LLM) so the checks are deterministic, fast, and free.
Verifies the subset-matching semantics documented in
design/cn-mempal-beam-benchmark-plan.md §4:

- same tenant, user A cannot see user B's memory
- same tenant, agent A cannot see agent B's memory
- same tenant+user must hit its own memory
- a different tenant must not leak (even with the same user_id)

Usage:
    python server/scripts/benchmarks/smoke_isolation.py \
        --email e2e-v3@example.com --password ...
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _http import ServerClient  # noqa: E402

TENANT = "bench-smoke-isolation"


def remember(client, body):
    resp = client.post("/v1/memory/remember", body, timeout=60)
    results = resp.get("results") or []
    if not results or results[0].get("outcome") not in ("created", "updated", "noop"):
        raise RuntimeError(f"unexpected remember response: {resp}")
    return results


def recall_texts(client, body):
    resp = client.post("/v1/memory/recall", body, timeout=60)
    return [item.get("text") or "" for item in resp.get("results") or []]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    client = ServerClient(args.base_url, email=args.email, password=args.password)
    facts = {
        "user_a": "isolation-probe-user-a-secret-42",
        "user_b": "isolation-probe-user-b-secret-84",
        "agent_a": "isolation-probe-agent-a-secret-7",
        "agent_b": "isolation-probe-agent-b-secret-9",
        "other_tenant": "isolation-probe-other-tenant-secret-11",
    }
    for name, text in facts.items():
        if name == "user_a":
            body = {"tenant_id": TENANT, "user_id": "iso-user-a", "text": text, "mode": "append"}
        elif name == "user_b":
            body = {"tenant_id": TENANT, "user_id": "iso-user-b", "text": text, "mode": "append"}
        elif name == "agent_a":
            body = {"tenant_id": TENANT, "agent_id": "iso-agent-a", "text": text, "mode": "append"}
        elif name == "agent_b":
            body = {"tenant_id": TENANT, "agent_id": "iso-agent-b", "text": text, "mode": "append"}
        else:
            body = {"tenant_id": TENANT + "-other", "user_id": "iso-user-a", "text": text, "mode": "append"}
        remember(client, body)

    def q(**ids):
        body = {"query": "isolation probe secret", "limit": 10, "mode": "keyword", **ids}
        return recall_texts(client, body)

    checks = []
    # 1. user A sees its own memory and not B's
    own_a = q(tenant_id=TENANT, user_id="iso-user-a")
    checks.append(("user_a sees own fact", facts["user_a"] in own_a))
    checks.append(("user_a isolated from user_b", facts["user_b"] not in own_a))
    # 2. agent A sees its own and not agent B's
    own_agent = q(tenant_id=TENANT, agent_id="iso-agent-a")
    checks.append(("agent_a sees own fact", facts["agent_a"] in own_agent))
    checks.append(("agent_a isolated from agent_b", facts["agent_b"] not in own_agent))
    # 3. tenant barrier: other tenant with same user_id does not leak
    other = q(tenant_id=TENANT + "-other", user_id="iso-user-a")
    checks.append(("other tenant isolated", facts["user_a"] not in other and facts["other_tenant"] in other))
    # 4. user A must not see agent-scoped memory (different scope shape)
    checks.append(("user_a not agent-scoped leak", facts["agent_a"] not in own_a))

    report = {"checks": [{"name": name, "passed": passed} for name, passed in checks]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(passed for _, passed in checks):
        sys.exit(1)
    print("ISOLATION SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
