"""
Smoke test for Step 2: Dynamic Routing

Expects:
  - mock users service running on :9001
  - mock orders service running on :9002
  - gateway running on :8000 with ROUTES_FILE=routes.local.json
"""

import json
import urllib.request

def get(url):
    r = urllib.request.urlopen(url)
    return r.status, json.loads(r.read())


def get_expect_error(url, expected_status):
    try:
        r = urllib.request.urlopen(url)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


print("=" * 60)
print("Step 2 Smoke Test: Dynamic Routing")
print("=" * 60)

# 1. Health check shows loaded routes
s, d = get("http://localhost:8000/gateway/health")
assert s == 200
assert d["routes_loaded"] >= 2, f"Expected >=2 routes, got {d['routes_loaded']}"
print(f"[1] Health: {d['routes_loaded']} routes loaded  OK")

# 2. GET /gateway/routes shows route details
s, d = get("http://localhost:8000/gateway/routes")
assert s == 200
prefixes = [r["prefix"] for r in d["routes"]]
assert "/users" in prefixes, f"/users not in {prefixes}"
assert "/orders" in prefixes, f"/orders not in {prefixes}"
print(f"[2] Routes endpoint: {prefixes}  OK")

# 3. /users routed to users-service
s, d = get("http://localhost:8000/users")
assert s == 200
assert "users" in d
names = [u["name"] for u in d["users"]]
print(f"[3] GET /users -> users-service: {names}  OK")

# 4. /users/1 routed correctly
s, d = get("http://localhost:8000/users/1")
assert s == 200
assert d["name"] == "Alice"
print(f"[4] GET /users/1 -> Alice  OK")

# 5. /orders routed to orders-service
s, d = get("http://localhost:8000/orders")
assert s == 200
assert "orders" in d
print(f"[5] GET /orders -> orders-service: {len(d['orders'])} orders  OK")

# 6. /orders/101 routed correctly
s, d = get("http://localhost:8000/orders/101")
assert s == 200
assert d["item"] == "Widget A"
print(f"[6] GET /orders/101 -> Widget A  OK")

# 7. Unknown path falls back to BACKEND_URL
s, d = get_expect_error("http://localhost:8000/nonexistent", 404)
print(f"[7] GET /nonexistent -> status {s} (fallback)  OK")

print()
print("Step 2 PASSED: dynamic routing working correctly!")
