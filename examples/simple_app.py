"""
Simple FastAPI application demonstrating rate limiting with pacer.

Run with:
    uvicorn examples.simple_app:app --reload

Requires Redis running locally:
    docker run -d -p 6379:6379 redis:latest
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from pacer import Limiter, LimiterMiddleware, Rate, limit
from pacer.dependencies import set_limiter
from pacer.extractors import extract_api_key, extract_ip

# Get Redis URL from environment or use default
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Initialize limiter
limiter = Limiter(
    redis_url=REDIS_URL,
    default_policy=Rate(permits=10, per="1m", burst=5),
    extractor=extract_ip(trusted_proxies=["127.0.0.1", "::1"]),
    fail_mode="open",
)

# Set global limiter for dependency injection
set_limiter(limiter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    await limiter.startup()
    yield
    # Shutdown
    await limiter.shutdown()


# Create FastAPI app
app = FastAPI(
    title="FastAPI Rate Limiter Example",
    description="Example API with rate limiting using pacer",
    lifespan=lifespan,
)

# Add global rate limiting middleware
app.add_middleware(
    LimiterMiddleware,
    limiter=limiter,
    policy=Rate(permits=100, per="1m", burst=20),  # Global limit
    exclude_paths=["/health", "/metrics", "/docs", "/openapi.json"],
)


# Routes with different rate limits

@app.get("/")
async def root():
    """Root endpoint with default rate limit from middleware."""
    return {"message": "Hello World", "rate_limit": "100 requests per minute"}


@app.get("/health")
async def health_check():
    """Health check endpoint (excluded from rate limiting)."""
    is_healthy = await limiter.is_healthy()
    if is_healthy:
        return {"status": "healthy", "redis": "connected"}
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "redis": "disconnected"}
        )


@app.get("/metrics")
async def metrics():
    """Metrics endpoint (excluded from rate limiting)."""
    return limiter.get_metrics()


@app.get(
    "/api/limited",
    dependencies=[Depends(limit(Rate(permits=5, per="1m", burst=2)))]
)
async def limited_endpoint():
    """Endpoint with strict rate limit (5 requests per minute)."""
    return {
        "message": "This endpoint is strictly rate limited",
        "rate_limit": "5 requests per minute with burst of 2"
    }


@app.get(
    "/api/generous",
    dependencies=[Depends(limit(Rate(permits=1000, per="1m", burst=100)))]
)
async def generous_endpoint():
    """Endpoint with generous rate limit."""
    return {
        "message": "This endpoint has a generous rate limit",
        "rate_limit": "1000 requests per minute with burst of 100"
    }


@app.post(
    "/api/create",
    dependencies=[Depends(limit(Rate(permits=10, per="1m", burst=0)))]
)
async def create_resource(data: dict):
    """POST endpoint with no burst allowed."""
    # Simulate some processing
    await asyncio.sleep(0.1)
    return {
        "message": "Resource created",
        "data": data,
        "rate_limit": "10 requests per minute, no burst"
    }


# API key based rate limiting example

api_key_limiter = Limiter(
    redis_url=REDIS_URL,
    default_policy=Rate(permits=100, per="1m", burst=10),
    extractor=extract_api_key(header_name="X-API-Key"),
    fail_mode="open",
)


@app.get(
    "/api/with-key",
    dependencies=[Depends(limit(Rate(permits=50, per="1m"), limiter=api_key_limiter))]
)
async def api_key_endpoint(api_key: str | None = None):
    """
    Endpoint with API key based rate limiting.

    Pass API key in X-API-Key header for per-key rate limiting.
    Without API key, falls back to IP-based limiting.
    """
    return {
        "message": "API key based rate limiting",
        "api_key_provided": api_key is not None,
        "rate_limit": "50 requests per minute per API key"
    }


# Burst testing endpoint

@app.get(
    "/test/burst",
    dependencies=[Depends(limit(Rate(permits=10, per="10s", burst=5)))]
)
async def burst_test():
    """
    Endpoint for testing burst capability.

    Allows 10 requests per 10 seconds with burst of 5.
    This means you can make 5 requests immediately, then 1 per second.
    """
    import time
    return {
        "timestamp": time.time(),
        "message": "Burst test endpoint",
        "rate_limit": "10 requests per 10 seconds, burst of 5"
    }


# Multiple rate limits example

@app.get("/api/multi-limit")
async def multi_limit_endpoint():
    """
    Example of checking multiple rate limits programmatically.
    """

    # This would normally come from the request context
    # For demo purposes, we're showing the concept

    # Check user-specific limit
    # result1 = await limiter.check_rate_limit(request, Rate(permits=100, per="1h"))

    # Check endpoint-specific limit
    # result2 = await limiter.check_rate_limit(request, Rate(permits=10, per="1m"))

    return {
        "message": "Multiple rate limits can be applied",
        "note": "See code comments for implementation details"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
