"""
LLM-Aware Gateway Mode (Step 10)
==================================
A differentiating feature that transforms the API gateway into an
**AI API Gateway** — routing by model name and tracking per-client
token usage instead of just request counts.

Design decisions
-----------------
- **Model-based routing**: parse the ``model`` field from the JSON body
  of POST requests and route to the appropriate backend based on a
  model→backend mapping.  This mirrors how OpenAI-compatible proxies
  (LiteLLM, Portkey) work.
- **Token usage tracking**: instead of (or in addition to) counting
  requests, track prompt_tokens + completion_tokens per client.  This
  enables usage-based billing and quota enforcement for AI workloads.
- **Non-invasive**: this is implemented as an optional middleware that
  only activates for configured LLM routes.  Non-LLM traffic passes
  through unchanged.

Usage:
  POST /v1/chat/completions  (OpenAI-compatible)
  Body: {"model": "gpt-4", "messages": [...]}

  The gateway reads the "model" field, looks up the backend in
  MODEL_ROUTING_TABLE, and forwards the request.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = logging.getLogger("gateway.llm")


# ---------------------------------------------------------------------------
# Model → Backend routing table
# ---------------------------------------------------------------------------
# In production this would be in PostgreSQL; here it's a dict for clarity.
# The key is the model name as it appears in the request body.
# The value is the backend URL to forward to.
MODEL_ROUTING_TABLE: dict[str, str] = {
    # OpenAI-compatible models
    "gpt-4": "http://localhost:9001",
    "gpt-4-turbo": "http://localhost:9001",
    "gpt-3.5-turbo": "http://localhost:9001",
    # Anthropic models
    "claude-3-opus": "http://localhost:9002",
    "claude-3-sonnet": "http://localhost:9002",
    # Local/self-hosted models
    "llama-3-70b": "http://localhost:9003",
    "mistral-7b": "http://localhost:9003",
}

# LLM endpoint prefixes that trigger model-based routing
LLM_PATH_PREFIXES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
)


# ---------------------------------------------------------------------------
# Per-client token usage tracking
# ---------------------------------------------------------------------------
@dataclass
class TokenUsage:
    """Tracks cumulative token usage for a single client."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    last_request_at: float = 0

    def record(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.request_count += 1
        self.last_request_at = time.time()

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
            "last_request_at": self.last_request_at,
        }


# Per-client token usage store (in-memory; Step 9 exposes via Admin API)
# Key: client_id
_token_usage: dict[str, TokenUsage] = {}


def get_token_usage(client_id: str) -> TokenUsage:
    """Get or create a token usage tracker for a client."""
    if client_id not in _token_usage:
        _token_usage[client_id] = TokenUsage()
    return _token_usage[client_id]


def all_token_usage() -> dict[str, dict]:
    """Return all token usage data (for admin API)."""
    return {k: v.to_dict() for k, v in _token_usage.items()}


# ---------------------------------------------------------------------------
# Token usage quota limits per tier
# ---------------------------------------------------------------------------
TOKEN_QUOTAS: dict[str, int] = {
    "free": 100_000,       # 100K tokens/month
    "pro": 10_000_000,     # 10M tokens/month
    "admin": 999_999_999,  # effectively unlimited
}


# ---------------------------------------------------------------------------
# Helper: extract model name from request body
# ---------------------------------------------------------------------------
async def _extract_model(request: Request) -> Optional[str]:
    """Parse the 'model' field from a JSON request body."""
    try:
        body = await request.body()
        if not body:
            return None
        data = json.loads(body)
        return data.get("model")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def resolve_model_backend(model: str) -> Optional[str]:
    """Look up the backend URL for a given model name."""
    return MODEL_ROUTING_TABLE.get(model)


# ---------------------------------------------------------------------------
# LLM middleware
# ---------------------------------------------------------------------------
class LLMGatewayMiddleware(BaseHTTPMiddleware):
    """
    LLM-aware request processing:

    1. For LLM endpoint paths, parse the ``model`` field from the body
       and override the routing target.
    2. After the response, extract ``usage`` from the response body
       and track prompt/completion tokens per client.
    3. Enforce per-tier token quotas.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Only activate for LLM endpoints
        is_llm = any(path.startswith(p) for p in LLM_PATH_PREFIXES)
        if not is_llm:
            return await call_next(request)

        # --- Model-based routing ---
        if request.method == "POST":
            model = await _extract_model(request)
            if model:
                backend = resolve_model_backend(model)
                if backend:
                    # Store the model-resolved backend on request.state
                    # so the proxy route can use it instead of prefix routing
                    request.state.llm_backend = backend
                    request.state.llm_model = model
                    logger.info("LLM routing: model=%s -> %s", model, backend)
                else:
                    return JSONResponse(
                        status_code=404,
                        content={
                            "error": {
                                "message": f"Model '{model}' is not available",
                                "type": "invalid_request_error",
                                "code": "model_not_found",
                            }
                        },
                    )

            # --- Token quota check ---
            client = getattr(request.state, "client", None)
            if client:
                usage = get_token_usage(client.client_id)
                quota = TOKEN_QUOTAS.get(client.tier, TOKEN_QUOTAS["free"])
                if usage.total_tokens >= quota:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": {
                                "message": "Token quota exceeded",
                                "type": "rate_limit_error",
                                "code": "token_quota_exceeded",
                                "usage": usage.to_dict(),
                                "quota": quota,
                            }
                        },
                    )

        # --- Forward the request ---
        response = await call_next(request)

        # --- Track token usage from response ---
        if request.method == "POST" and 200 <= response.status_code < 300:
            try:
                body = b""
                async for chunk in response.body_iterator:
                    if isinstance(chunk, str):
                        body += chunk.encode("utf-8")
                    else:
                        body += chunk

                resp_data = json.loads(body)
                usage_data = resp_data.get("usage", {})
                prompt_tokens = usage_data.get("prompt_tokens", 0)
                completion_tokens = usage_data.get("completion_tokens", 0)

                client = getattr(request.state, "client", None)
                if client and (prompt_tokens or completion_tokens):
                    tracker = get_token_usage(client.client_id)
                    tracker.record(prompt_tokens, completion_tokens)
                    logger.info(
                        "Token usage: client=%s model=%s prompt=%d completion=%d total_cumulative=%d",
                        client.client_id,
                        getattr(request.state, "llm_model", "unknown"),
                        prompt_tokens,
                        completion_tokens,
                        tracker.total_tokens,
                    )

                # Re-create response with the consumed body
                response = Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            except (json.JSONDecodeError, Exception):
                # If we can't parse the response, just pass it through
                pass

        return response
