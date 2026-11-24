"""随办托管模型网关 (Suiban hosted-model gateway).

OpenAI-compatible proxy in front of DashScope/Bailian:
- devices auth with per-device bearer tokens issued by admin;
- model aliases (suiban-main/vision/light) route to real models;
- per-device rolling-24h token budget, usage metering, cost estimate;
- SUISBAN_MOCK=1 uses a canned upstream for local tests.

Run: uvicorn app:app --host 0.0.0.0 --port 8800
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("SUISBAN_DB", str(BASE_DIR / "gateway.db"))
UPSTREAM_BASE = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
UPSTREAM_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
ADMIN_TOKEN = os.environ.get("SUISBAN_ADMIN_TOKEN", "")
MOCK = os.environ.get("SUISBAN_MOCK", "") == "1"
DEFAULT_DAILY_BUDGET = int(os.environ.get("SUISBAN_DAILY_TOKEN_BUDGET", "2000000"))

MODEL_MAP = json.loads(os.environ.get("SUISBAN_MODEL_MAP", json.dumps({
    "suiban-main": "qwen3-max",
    "suiban-vision": "qwen-vl-plus",
    "suiban-light": "qwen-flash",
})))

# RMB per 1k tokens. Rough estimates, override via SUISBAN_PRICES; billing is authoritative.
PRICES = json.loads(os.environ.get("SUISBAN_PRICES", json.dumps({
    "qwen3-max": {"in": 0.006, "out": 0.024},
    "qwen-vl-plus": {"in": 0.0015, "out": 0.0045},
    "qwen-flash": {"in": 0.00015, "out": 0.0015},
})))

app = FastAPI(title="Suiban Gateway", version="0.1.0")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            "CREATE TABLE IF NOT EXISTS devices("
            "id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE,"
            "created_at INTEGER, expires_at INTEGER, revoked INTEGER DEFAULT 0,"
            "daily_token_budget INTEGER);"
            "CREATE TABLE IF NOT EXISTS usage("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, alias TEXT, model TEXT,"
            "prompt_tokens INTEGER, completion_tokens INTEGER, cost_rmb REAL, ts INTEGER);"
        )


init_db()


def err(status, message, kind="invalid_request_error"):
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind}})


def device_from_request(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    h = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    with db() as c:
        row = c.execute("SELECT * FROM devices WHERE token_hash=?", (h,)).fetchone()
    if row is None or row["revoked"] or row["expires_at"] < time.time():
        raise HTTPException(401, "invalid, revoked or expired device token")
    return row


def require_admin(request: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(503, "admin disabled: set SUISBAN_ADMIN_TOKEN")
    if request.headers.get("x-admin-token") != ADMIN_TOKEN:
        raise HTTPException(401, "bad admin token")


def tokens_last_24h(device_id):
    since = int(time.time()) - 86400
    with db() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) t FROM usage WHERE device_id=? AND ts>=?",
            (device_id, since)).fetchone()
    return int(r["t"])


def record_usage(device_id, alias, model, usage):
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    p = PRICES.get(model, {"in": 0.0, "out": 0.0})
    cost = pt / 1000.0 * p["in"] + ct / 1000.0 * p["out"]
    with db() as c:
        c.execute(
            "INSERT INTO usage(device_id,alias,model,prompt_tokens,completion_tokens,cost_rmb,ts) VALUES(?,?,?,?,?,?,?)",
            (device_id, alias, model, pt, ct, cost, int(time.time())))


@app.get("/healthz")
def healthz():
    return {"ok": True, "mock": MOCK, "models": list(MODEL_MAP)}


@app.post("/admin/devices")
async def create_device(request: Request):
    require_admin(request)
    body = await request.json()
    dev_id = "dev_" + secrets.token_hex(6)
    token = "sk-suiban-" + secrets.token_urlsafe(32)
    days = int(body.get("days", 30))
    budget = int(body.get("daily_token_budget", DEFAULT_DAILY_BUDGET))
    now = int(time.time())
    with db() as c:
        c.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?)",
                  (dev_id, body.get("name", ""), hashlib.sha256(token.encode()).hexdigest(),
                   now, now + days * 86400, 0, budget))
    return {"device_id": dev_id, "token": token, "expires_at": now + days * 86400,
            "daily_token_budget": budget, "note": "token shown once; store it on the device"}


@app.get("/admin/devices")
def list_devices(request: Request):
    require_admin(request)
    with db() as c:
        rows = c.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        out.append({"device_id": r["id"], "name": r["name"], "revoked": bool(r["revoked"]),
                    "expires_at": r["expires_at"], "daily_token_budget": r["daily_token_budget"],
                    "tokens_24h": tokens_last_24h(r["id"])})
    return {"devices": out}


@app.delete("/admin/devices/{device_id}")
def revoke_device(device_id: str, request: Request):
    require_admin(request)
    with db() as c:
        cur = c.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "device not found")
    return {"ok": True, "revoked": device_id}


@app.get("/admin/usage")
def usage_stats(request: Request, device_id: str = "", hours: int = 24):
    require_admin(request)
    since = int(time.time()) - hours * 3600
    q = "SELECT device_id, alias, model, prompt_tokens, completion_tokens, cost_rmb, ts FROM usage WHERE ts>=?"
    args = [since]
    if device_id:
        q += " AND device_id=?"
        args.append(device_id)
    with db() as c:
        rows = c.execute(q + " ORDER BY ts DESC LIMIT 2000", args).fetchall()
    total_cost = sum(r["cost_rmb"] for r in rows)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
    return {"rows": [dict(r) for r in rows], "total_tokens": total_tokens,
            "total_cost_rmb": round(total_cost, 4), "hours": hours}


@app.get("/v1/models")
def list_models(request: Request):
    device_from_request(request)
    return {"object": "list",
            "data": [{"id": a, "object": "model", "created": 0, "owned_by": "suiban"} for a in MODEL_MAP]}


def _mock_tool_call(fwd):
    tools = fwd.get("tools") or []
    first = tools[0] if tools else {}
    fn = first.get("function", first)
    name = fn.get("name", "ping")
    schema = fn.get("parameters") or fn.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or list(props.keys())
    args = {}
    for k in required:
        spec = props.get(k) or {}
        t = spec.get("type", "string")
        if spec.get("enum"):
            args[k] = spec["enum"][0]
        elif t in ("number", "integer"):
            args[k] = 1
        elif t == "boolean":
            args[k] = True
        elif t == "array":
            args[k] = []
        elif t == "object":
            args[k] = {}
        else:
            args[k] = "test"
    return {"id": "call_mock_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def mock_completion(fwd, dev, alias, real):
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    last = ""
    msgs = fwd.get("messages") or []
    if msgs and isinstance(msgs[-1].get("content"), str):
        last = msgs[-1]["content"][:40]
    text = "【随办网关 MOCK】已收到请求。" + last
    tool_call = _mock_tool_call(fwd) if fwd.get("tools") else None
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": real}
    if fwd.get("stream"):
        async def gen():
            if tool_call:
                d1 = {"role": "assistant", "tool_calls": [{"index": 0, "id": tool_call["id"], "type": "function",
                      "function": {"name": tool_call["function"]["name"], "arguments": ""}}]}
                yield "data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": d1, "finish_reason": None}]), ensure_ascii=False) + "\n\n"
                d2 = {"tool_calls": [{"index": 0, "function": {"arguments": tool_call["function"]["arguments"]}}]}
                yield "data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": d2, "finish_reason": None}]), ensure_ascii=False) + "\n\n"
                finish = "tool_calls"
            else:
                for piece in ["【随办网关", " MOCK】", "已收到请求。" + last]:
                    yield "data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": {"content": piece}, "finish_reason": None}]), ensure_ascii=False) + "\n\n"
                finish = "stop"
            yield "data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}]), ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"id": "chatcmpl-mock", "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
            record_usage(dev["id"], alias, real, usage)
        return StreamingResponse(gen(), media_type="text/event-stream")
    record_usage(dev["id"], alias, real, usage)
    if tool_call:
        message = {"role": "assistant", "content": None, "tool_calls": [tool_call]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"
    return JSONResponse({"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
                         "model": real, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": usage})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        dev = device_from_request(request)
    except HTTPException as e:
        return err(e.status_code, e.detail, "authentication_error")
    body = await request.json()
    alias = body.get("model", "")
    if alias not in MODEL_MAP:
        return err(404, "unknown model '%s'; use one of %s" % (alias, list(MODEL_MAP)))
    if tokens_last_24h(dev["id"]) >= dev["daily_token_budget"]:
        return err(429, "daily token budget exceeded for this device", "rate_limit_error")
    real = MODEL_MAP[alias]
    fwd = dict(body)
    fwd["model"] = real
    if MOCK:
        return mock_completion(fwd, dev, alias, real)
    if not UPSTREAM_KEY:
        return err(503, "gateway misconfigured: DASHSCOPE_API_KEY not set")
    headers = {"Authorization": "Bearer " + UPSTREAM_KEY, "Content-Type": "application/json"}
    url = UPSTREAM_BASE + "/chat/completions"
    if body.get("stream"):
        fwd.setdefault("stream_options", {"include_usage": True})

        async def gen():
            usage = {}
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream("POST", url, json=fwd, headers=headers) as resp:
                        if resp.status_code != 200:
                            detail = (await resp.aread()).decode(errors="replace")[:500]
                            yield "data: " + json.dumps({"error": {"message": detail, "code": resp.status_code}}) + "\n\n"
                            return
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                yield "data: [DONE]\n\n"
                                continue
                            try:
                                chunk = json.loads(payload)
                                if chunk.get("usage"):
                                    usage = chunk["usage"]
                            except Exception:
                                pass
                            yield line + "\n\n"
            finally:
                if usage:
                    record_usage(dev["id"], alias, real, usage)

        return StreamingResponse(gen(), media_type="text/event-stream")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=fwd, headers=headers)
    try:
        data = resp.json()
    except Exception:
        data = {"error": {"message": resp.text[:500]}}
    if resp.status_code == 200:
        record_usage(dev["id"], alias, real, data.get("usage") or {})
    return JSONResponse(status_code=resp.status_code, content=data)
