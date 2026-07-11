# API Gateway -Portfolio-Project 

A production-grade API Gateway built incrementally in **Python 3.11 + FastAPI**, mirroring the architecture of Kong, AWS API Gateway, and Apigee — but written from scratch so every algorithm is visible and documented.

> **Tech Stack:** Python · FastAPI · httpx · Redis · PostgreSQL · Docker

---

## Architecture Overview

```
               ┌──────────────────────────────────────────────┐
 Client ──────▶│                 API Gateway                  │
               │  ┌──────────┐  ┌────────────┐  ┌─────────┐  │
               │  │   Auth   │  │Rate Limiter│  │  Cache  │  │
               │  └────┬─────┘  └─────┬──────┘  └────┬────┘  │
               │       │              │               │        │
               │  ┌────▼──────────────▼───────────────▼────┐  │
               │  │          Reverse Proxy / Router         │  │
               │  └─────────────────────┬───────────────────┘  │
               │              ┌─────────▼────────┐             │
               │              │  Load Balancer   │             │
               │              │  Circuit Breaker │             │
               └──────────────┴─────────┬────────┴─────────────┘
                                        │
                    ┌───────────────────┼───────────────┐
                    ▼                   ▼               ▼
              users-service-1   users-service-2   orders-service
```

---

## Steps Implemented

| # | Feature | Status |
|---|---------|--------|
| 1 | Basic reverse proxy | ✅ Done |
| 2 | Dynamic routing (path prefix → backend) | ⏳ Next |
| 3 | Auth middleware (API key + JWT) | ⏳ |
| 4 | Rate limiting (token bucket, Redis) | ⏳ |
| 5 | Load balancing (round-robin + least-conn) | ⏳ |
| 6 | Response caching (Redis, per-route TTL) | ⏳ |
| 7 | Logging & tracing (request ID, JSON, /metrics) | ⏳ |
| 8 | Circuit breaker (closed/open/half-open) | ⏳ |
| 9 | Admin API | ⏳ |
| 10 | LLM-aware mode (model routing, token usage) | ⏳ |

---

## Quick Start

### With Docker (recommended)

```bash
# Clone and enter project
cd api-gateway

# Build and start everything
docker-compose up --build

# Verify gateway health
curl http://localhost:8000/gateway/health

# Call through the proxy (forwards to users-service)
curl http://localhost:8000/users
curl http://localhost:8000/users/1
```

### Without Docker (local dev)

```bash
# Install deps
pip install -r requirements.txt

# Copy env file
cp .env.example .env

# Terminal 1 – start users mock backend
uvicorn mock_backends.users_service:app --port 9001 --reload

# Terminal 2 – start orders mock backend
uvicorn mock_backends.orders_service:app --port 9002 --reload

# Terminal 3 – start gateway
uvicorn gateway.main:app --port 8000 --reload
```

---

## Project Structure

```
api-gateway/
├── gateway/
│   ├── main.py                  # FastAPI app, lifecycle, catch-all proxy route
│   ├── config.py                # Pydantic-settings (env vars)
│   ├── proxy/
│   │   └── reverse_proxy.py     # Core httpx forwarding logic
│   ├── routing/                 # Step 2: dynamic path routing
│   ├── middleware/              # Steps 3,4,6,7: auth, rate-limit, cache, request-id
│   ├── loadbalancer/            # Step 5: round-robin & least-connections
│   ├── circuit_breaker/         # Step 8: closed/open/half-open FSM
│   ├── logging_tracing/         # Step 7: structured logging + Prometheus
│   ├── admin/                   # Step 9: admin REST API
│   └── llm/                     # Step 10: LLM-aware routing
├── mock_backends/
│   ├── users_service.py         # Mock /users/* backend
│   └── orders_service.py        # Mock /orders/* backend
├── tests/
│   ├── test_rate_limiter.py     # Token bucket unit tests
│   ├── test_circuit_breaker.py  # FSM state machine tests
│   └── test_load_balancer.py    # Round-robin / least-conn tests
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Component Deep-Dives

### Step 1 — Reverse Proxy (`gateway/proxy/reverse_proxy.py`)

The core of any API Gateway. We use **`httpx.AsyncClient`** with connection pooling because:

- FastAPI is fully async (ASGI); using a sync HTTP client would block the event loop
- A shared client reuses TCP connections across requests (connection pooling), cutting latency vs opening a new socket per request
- `httpx` supports HTTP/1.1 and HTTP/2, timeout configuration, and streaming

**Hop-by-hop header stripping:** RFC 2616 §13.5.1 defines headers that are only meaningful for a single TCP link (e.g., `Transfer-Encoding`, `Connection`). These must be removed before forwarding to avoid corrupting the backend connection.

### Step 4 — Token Bucket Rate Limiter *(coming)*

We chose **token bucket** over sliding window because:
- Sliding window is more accurate but requires storing per-second sub-buckets in Redis (higher memory)
- Token bucket allows short bursts (better for real APIs) while still enforcing a sustained rate
- Implemented with `INCRBY` + `EXPIRE` in Redis (atomic, no race conditions)

### Step 5 — Load Balancer *(coming)*

Two strategies:
- **Round-robin**: Simple O(1) selection, great for homogeneous backends
- **Least-connections**: Picks the instance with the fewest in-flight requests — better when request durations vary significantly

### Step 8 — Circuit Breaker *(coming)*

Implements the standard three-state FSM:
- **Closed** → normal operation, failures tracked
- **Open** → all requests immediately fail-fast (no backend hammering)
- **Half-open** → one probe request allowed; success → Closed, failure → Open

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BACKEND_URL` | `http://localhost:9001` | Step 1 hardcoded backend |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection (Steps 4, 6) |
| `POSTGRES_DSN` | `postgresql://...` | DB connection (Steps 2+) |
| `JWT_SECRET` | `changeme` | JWT signing key (Step 3) |
