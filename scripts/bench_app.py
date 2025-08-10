#!/usr/bin/env python3
"""Minimal FastAPI app for benchmarking rate limiter overhead."""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from pacer import Limiter, LimiterMiddleware, Rate, limit

# Get Redis URL from environment
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Initialize limiter with aggressive settings for benchmarking
limiter = Limiter(
    redis_url=REDIS_URL,
    default_policy=Rate(permits=10000, per="1s", burst=1000),  # High limits
    fail_mode="open",
    expose_headers=True,
    connect_timeout_ms=500,
    command_timeout_ms=50,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    await limiter.startup()
    yield
    await limiter.shutdown()


# Create app
app = FastAPI(
    title="Benchmark App",
    lifespan=lifespan,
)

# Add global middleware
app.add_middleware(
    LimiterMiddleware,
    limiter=limiter,
    policy=Rate(permits=10000, per="1s", burst=1000),
)


@app.get("/unlimited")
async def unlimited():
    """Endpoint without rate limiting (baseline)."""
    return {"status": "ok", "limited": False}


@app.get("/limited", dependencies=[Depends(limit(Rate(10000, "1s", burst=1000)))])
async def limited():
    """Endpoint with rate limiting (measure overhead)."""
    return {"status": "ok", "limited": True}


@app.get("/health")
async def health():
    """Health check endpoint."""
    is_healthy = await limiter.is_healthy()
    status = 200 if is_healthy else 503
    return JSONResponse(
        {"healthy": is_healthy, "metrics": limiter.get_metrics()},
        status_code=status,
    )


if __name__ == "__main__":
    import uvicorn

    # Run with multiple workers for benchmarking
    uvicorn.run(
        "bench_app:app",
        host="0.0.0.0",
        port=8000,
        workers=1,  # Will be overridden by script
        log_level="error",  # Reduce logging overhead
    )
