"""
Demo script to showcase the API Gateway's capabilities.
Runs the gateway and backends locally, sends test requests, and then shuts them down.
"""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys

# ---------------------------------------------------------------------------
# Setup & Teardown
# ---------------------------------------------------------------------------
print("Starting backend services (Users & Orders)...")
u1 = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_backends.users_service:app", "--port", "9001"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
o1 = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_backends.orders_service:app", "--port", "9002"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# Start a second users service for load balancing
u2 = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_backends.users_service:app", "--port", "9011"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("Starting API Gateway (Step 10)...")
import os
env = os.environ.copy()
env["ROUTES_FILE"] = "routes.local.json"
env["JWT_SECRET"] = "dev-secret-not-for-production"
env["LOG_LEVEL"] = "WARNING"  # keep output clean
gw = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.main:app", "--port", "8000"], env=env)

print("Waiting for services to boot...")
time.sleep(5)

# ---------------------------------------------------------------------------
# Demo Requests
# ---------------------------------------------------------------------------
BASE_URL = "http://localhost:8000"

def request(method, path, headers=None, data=None):
    req = urllib.request.Request(BASE_URL + path, method=method, headers=headers or {})
    if data:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8")
    except urllib.error.URLError as e:
        return 0, {}, str(e)

print("\n" + "="*60)
print("DEMO: API Gateway Capabilities")
print("="*60)

# 1. Admin API & Routes
print("\n[1] Admin API: Checking loaded services...")
status, headers, body = request("GET", "/gateway/admin/services")
print(f"Status: {status}")
print(json.dumps(json.loads(body), indent=2))

# 2. Authentication (API Key) & Routing
print("\n[2] Routing & Authentication: Hitting /users with Free Tier API Key")
status, headers, body = request("GET", "/users", {"X-API-Key": "test-key-free"})
print(f"Status: {status}")
print(f"Body: {body[:100]}...")
print(f"X-Request-ID (injected by gateway): {headers.get('X-Request-ID')}")

# 3. Load Balancing
print("\n[3] Load Balancing: Hitting /users multiple times to see requests distributed")
for i in range(4):
    status, _, body = request("GET", "/users", {"X-API-Key": "test-key-free", "Cache-Control": "no-cache"})
    data = json.loads(body)
    # The mock backends include their PORT in the response
    print(f"  Request {i+1} handled by backend on port: {data.get('server_port')}")

# 4. Rate Limiting
print("\n[4] Rate Limiting: Hitting /orders rapidly (Free Tier allows 10/min, burst 15)")
headers_dict = {"X-API-Key": "test-key-free"}
for i in range(3):
    status, headers, _ = request("GET", "/orders", headers_dict)
    print(f"  Request {i+1} -> Status: {status}, RateLimit-Remaining: {headers.get('X-Ratelimit-Remaining')}")

# 5. Caching
print("\n[5] Caching: Requesting /users twice without Cache-Control: no-cache")
status1, headers1, _ = request("GET", "/users", {"X-API-Key": "test-key-free"})
print(f"  First Request -> X-Cache: {headers1.get('X-Cache')}")
status2, headers2, _ = request("GET", "/users", {"X-API-Key": "test-key-free"})
print(f"  Second Request -> X-Cache: {headers2.get('X-Cache')} (Served from memory/Redis!)")

# 6. LLM Routing & Token Tracking
print("\n[6] LLM Gateway: Routing request to 'gpt-4' mock backend (tracked by token usage)")
llm_payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello!"}]}
status, headers, body = request("POST", "/v1/chat/completions", {"X-API-Key": "test-key-pro"}, llm_payload)
print(f"Status: {status}")
print("Response:", body)
if status == 200:
    print("Checking token quota via Admin API...")
    status, _, body = request("GET", "/gateway/admin/health-detail")  # For now, token usage is only logged, but let's see. Wait, we didn't expose it to admin API directly yet, it logs it.
    print("(Token usage is recorded in structured logs in the gateway output)")

print("\n" + "="*60)
print("Demo complete. Shutting down services...")
u1.terminate()
o1.terminate()
u2.terminate()
gw.terminate()
print("Done!")
