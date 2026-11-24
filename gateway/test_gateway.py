"""Smoke tests for Suiban gateway (mock upstream, no real API key needed).
Run: python test_gateway.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_DB = HERE / "test_gateway.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["SUISBAN_MOCK"] = "1"
os.environ["SUISBAN_ADMIN_TOKEN"] = "test-admin"
os.environ["SUISBAN_DB"] = str(TEST_DB)
sys.path.insert(0, str(HERE))

from fastapi.testclient import TestClient  # noqa: E402
import app as gw  # noqa: E402

client = TestClient(gw.app)
ADMIN = {"x-admin-token": "test-admin"}
passed = 0


def check(name, cond):
    global passed
    assert cond, "FAIL: " + name
    passed += 1
    print("PASS:", name)


r = client.get("/healthz")
check("healthz", r.status_code == 200 and r.json()["mock"] is True)

r = client.post("/admin/devices", json={"name": "pixel-test", "days": 7}, headers=ADMIN)
check("create device", r.status_code == 200 and r.json()["token"].startswith("sk-suiban-"))
dev = r.json()
AUTH = {"Authorization": "Bearer " + dev["token"]}

r = client.post("/admin/devices", json={"name": "no-admin"})
check("admin rejects missing token", r.status_code == 401)

r = client.get("/v1/models", headers=AUTH)
check("model list", r.status_code == 200 and {m["id"] for m in r.json()["data"]} == {"suiban-main", "suiban-vision", "suiban-light"})

r = client.get("/v1/models")
check("device auth required", r.status_code == 401)

body = {"model": "suiban-vision", "messages": [{"role": "user", "content": "这条怎么回"}]}
r = client.post("/v1/chat/completions", json=body, headers=AUTH)
check("chat non-stream", r.status_code == 200 and "MOCK" in r.json()["choices"][0]["message"]["content"])
check("usage metered", r.json()["usage"]["total_tokens"] == 120)

r = client.post("/v1/chat/completions", json=dict(body, stream=True), headers=AUTH)
check("chat stream", r.status_code == 200 and "[DONE]" in r.text and "data:" in r.text)

r = client.post("/v1/chat/completions", json=dict(body, model="gpt-4o"), headers=AUTH)
check("unknown model 404", r.status_code == 404)

r = client.get("/admin/usage", headers=ADMIN, params={"device_id": dev["device_id"]})
check("admin usage stats", r.status_code == 200 and r.json()["total_tokens"] == 240)

r = client.post("/admin/devices", json={"name": "tiny-budget", "daily_token_budget": 50}, headers=ADMIN)
tiny = r.json()
ta = {"Authorization": "Bearer " + tiny["token"]}
r1 = client.post("/v1/chat/completions", json=body, headers=ta)
r2 = client.post("/v1/chat/completions", json=body, headers=ta)
check("budget enforcement", r1.status_code == 200 and r2.status_code == 429)

r = client.delete("/admin/devices/" + dev["device_id"], headers=ADMIN)
check("revoke device", r.status_code == 200)
r = client.get("/v1/models", headers=AUTH)
check("revoked token rejected", r.status_code == 401)

print("\n%d/%d tests passed" % (passed, passed))
