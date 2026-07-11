"""
Smoke test for Step 3: Authentication Middleware

Tests:
  1. Gateway endpoints (/gateway/*) are exempt from auth
  2. No credentials -> 401
  3. Invalid API key -> 403
  4. Valid API key -> proxied successfully
  5. Valid JWT -> proxied successfully
  6. Expired JWT -> 401
  7. Malformed JWT -> 401
"""

import json
import time
import urllib.request

# Use PyJWT to create test tokens
import jwt as pyjwt


BASE = "http://localhost:8000"
JWT_SECRET = "dev-secret-not-for-production"


def request(url, headers=None):
    """Make GET request, return (status, body_dict, headers_obj)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


print("=" * 60)
print("Step 3 Smoke Test: Authentication Middleware")
print("=" * 60)

# 1. /gateway/health is exempt (no credentials needed)
s, d, h = request(f"{BASE}/gateway/health")
assert s == 200 and d["status"] == "ok"
print(f"[1] /gateway/health exempt from auth: {s}  OK")

# 2. No credentials -> 401
s, d, h = request(f"{BASE}/users")
assert s == 401
assert "Missing credentials" in d["detail"]
print(f"[2] No credentials -> {s}  OK")

# 3. Invalid API key -> 403
s, d, h = request(f"{BASE}/users", {"X-API-Key": "bogus-key"})
assert s == 403
print(f"[3] Invalid API key -> {s}  OK")

# 4. Valid API key (free tier) -> proxied
s, d, h = request(f"{BASE}/users", {"X-API-Key": "test-key-free"})
assert s == 200
assert "users" in d
# headers obj supports case-insensitive lookup via .get()
client = h.get("x-authenticated-client") or h.get("X-Authenticated-Client")
assert client == "client-free-1", f"Got client={client}"
print(f"[4] Valid API key -> {s}, client={client}  OK")

# 5. Valid API key (pro tier) -> proxied with user detail
s, d, h = request(f"{BASE}/users/1", {"X-API-Key": "test-key-pro"})
assert s == 200
assert d["name"] == "Alice"
client = h.get("x-authenticated-client") or h.get("X-Authenticated-Client")
assert client == "client-pro-1", f"Got client={client}"
print(f"[5] Pro API key -> {s}, client={client}  OK")

# 6. Valid JWT -> proxied
token = pyjwt.encode(
    {"sub": "jwt-client-1", "tier": "pro", "exp": int(time.time()) + 3600},
    JWT_SECRET,
    algorithm="HS256",
)
s, d, h = request(f"{BASE}/orders", {"Authorization": f"Bearer {token}"})
assert s == 200
assert "orders" in d
client = h.get("x-authenticated-client") or h.get("X-Authenticated-Client")
assert client == "jwt-client-1", f"Got client={client}"
print(f"[6] Valid JWT -> {s}, client={client}  OK")

# 7. Expired JWT -> 401
expired = pyjwt.encode(
    {"sub": "expired-client", "tier": "free", "exp": int(time.time()) - 10},
    JWT_SECRET,
    algorithm="HS256",
)
s, d, h = request(f"{BASE}/users", {"Authorization": f"Bearer {expired}"})
assert s == 401
print(f"[7] Expired JWT -> {s}  OK")

# 8. Malformed JWT -> 401
s, d, h = request(f"{BASE}/users", {"Authorization": "Bearer not.a.jwt"})
assert s == 401
print(f"[8] Malformed JWT -> {s}  OK")

print()
print("Step 3 PASSED: authentication middleware working correctly!")
