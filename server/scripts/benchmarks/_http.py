#!/usr/bin/env python
"""Shared stdlib-only HTTP/DashScope helpers for `/v1/memory/*` benchmark runners.

Kept intentionally small: benchmark runners must run on any Python 3.10+
host without third-party dependencies.
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER_ENV = Path(__file__).resolve().parents[2] / ".env"

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


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


class ServerClient:
    def __init__(self, base_url, *, email=None, password=None, no_auth=False):
        self.base = base_url.rstrip("/")
        self.no_auth = no_auth
        self.email = email
        self.password = password
        self.headers = {}
        if not no_auth:
            self._login()

    def _login(self):
        token = http_json(
            "POST",
            f"{self.base}/auth/login",
            body={"email": self.email, "password": self.password},
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
            if exc.status != 401 or self.no_auth:
                raise
            self._login()
            return http_json(method, f"{self.base}{path}", headers=self.headers, body=body, timeout=timeout)


def resolve_dashscope_key(explicit=None):
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


class DashScopeClient:
    def __init__(self, api_key, model, base_url=DASHSCOPE_BASE):
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model = model
        self.base_url = base_url.rstrip("/")

    def chat(self, prompt, system=None, json_mode=False):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.model, "temperature": 0, "messages": messages}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = http_json(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            body=body,
            timeout=180.0,
        )
        return resp["choices"][0]["message"]["content"].strip()


def parse_json_object(raw):
    """Best-effort JSON object extraction (code fences, prose, markdown)."""
    if not raw:
        return None
    text = raw.strip()
    candidates = []
    for fence in ("```json", "```"):
        if fence in text:
            text = text.split(fence)[-1]
            if "```" in text:
                text = text.split("```")[0]
    candidates.append(text)
    try:
        parsed = json.loads(text, strict=False)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1], strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


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


def reset_server(client, *, es_url="http://localhost:9200", es_prefix="agentar_mem0"):
    try:
        client.post("/reset", {})
        print("[reset] POST /reset ok", flush=True)
        return
    except Exception as exc:  # noqa: BLE001 - fall through to direct ES delete
        print(f"[reset] POST /reset failed ({exc}); falling back to direct ES delete", flush=True)
    http_json("DELETE", f"{es_url}/{es_prefix}_*", timeout=60.0)
    print(f"[reset] deleted ES indices {es_prefix}_*", flush=True)
