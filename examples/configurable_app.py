"""
Configurable FastAPI application for testing different rate limiting scenarios.

Environment variables:
    SERVICE_NAME: Name of the service (for identification)
    REDIS_URL: Redis connection URL
    RATE_PERMITS: Number of permits (default: 10)
    RATE_PERIOD: Time period (default: "1m")
    RATE_BURST: Burst capacity (default: 5)
    FAIL_MODE: "open" or "closed" (default: "open")
    USE_MIDDLEWARE: "true" to enable global middleware (default: "false")
    MIDDLEWARE_PERMITS: Permits for middleware (default: 100)
    MIDDLEWARE_PERIOD: Period for middleware (default: "1m")
    MIDDLEWARE_BURST: Burst for middleware (default: 20)
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from pacer import Limiter, LimiterMiddleware, Rate, limit
from pacer.dependencies import set_limiter
from pacer.extractors import extract_api_key, extract_ip

# Configuration from environment
SERVICE_NAME = os.getenv("SERVICE_NAME", "default")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Rate configuration
RATE_PERMITS = int(os.getenv("RATE_PERMITS", "10"))
RATE_PERIOD = os.getenv("RATE_PERIOD", "1m")
RATE_BURST = int(os.getenv("RATE_BURST", "5"))
FAIL_MODE = os.getenv("FAIL_MODE", "open")

# Middleware configuration
USE_MIDDLEWARE = os.getenv("USE_MIDDLEWARE", "false").lower() == "true"
MIDDLEWARE_PERMITS = int(os.getenv("MIDDLEWARE_PERMITS", "100"))
MIDDLEWARE_PERIOD = os.getenv("MIDDLEWARE_PERIOD", "1m")
MIDDLEWARE_BURST = int(os.getenv("MIDDLEWARE_BURST", "20"))

# Create rate policies
default_rate = Rate(permits=RATE_PERMITS, per=RATE_PERIOD, burst=RATE_BURST)
middleware_rate = Rate(permits=MIDDLEWARE_PERMITS, per=MIDDLEWARE_PERIOD, burst=MIDDLEWARE_BURST)

# Initialize limiter
limiter = Limiter(
    redis_url=REDIS_URL,
    default_policy=default_rate,
    extractor=extract_ip(trusted_proxies=["127.0.0.1", "::1", "172.0.0.0/8"]),
    fail_mode=FAIL_MODE,
    app_name=SERVICE_NAME,  # Use service name as app name for key prefixing
)

# Set global limiter for dependency injection
set_limiter(limiter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    await limiter.startup()
    print(f"Service '{SERVICE_NAME}' started with rate limit: {RATE_PERMITS} per {RATE_PERIOD}, burst={RATE_BURST}")
    if USE_MIDDLEWARE:
        print(f"  Middleware enabled: {MIDDLEWARE_PERMITS} per {MIDDLEWARE_PERIOD}, burst={MIDDLEWARE_BURST}")
    yield
    # Shutdown
    await limiter.shutdown()


# Create FastAPI app
app = FastAPI(
    title=f"Rate Limiter Test Service - {SERVICE_NAME}",
    description=f"Service configured with {RATE_PERMITS} requests per {RATE_PERIOD}",
    lifespan=lifespan,
)

# Conditionally add middleware
if USE_MIDDLEWARE:
    app.add_middleware(
        LimiterMiddleware,
        limiter=limiter,
        policy=middleware_rate,
        exclude_paths=["/health", "/config", "/metrics"],
    )


@app.get("/")
async def root():
    """Root endpoint to test rate limiting."""
    return {
        "service": SERVICE_NAME,
        "message": "Request successful",
        "rate_limit": f"{RATE_PERMITS} per {RATE_PERIOD}, burst={RATE_BURST}",
    }


@app.get("/health")
async def health():
    """Health check endpoint (excluded from rate limiting)."""
    is_healthy = await limiter.is_healthy()
    if is_healthy:
        return {"status": "healthy", "service": SERVICE_NAME, "redis": "connected"}
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": SERVICE_NAME, "redis": "disconnected"}
        )


@app.get("/config")
async def config():
    """Show current configuration (excluded from rate limiting)."""
    return {
        "service": SERVICE_NAME,
        "rate_config": {
            "permits": RATE_PERMITS,
            "period": RATE_PERIOD,
            "burst": RATE_BURST,
            "fail_mode": FAIL_MODE,
        },
        "middleware": {
            "enabled": USE_MIDDLEWARE,
            "permits": MIDDLEWARE_PERMITS if USE_MIDDLEWARE else None,
            "period": MIDDLEWARE_PERIOD if USE_MIDDLEWARE else None,
            "burst": MIDDLEWARE_BURST if USE_MIDDLEWARE else None,
        },
    }


@app.get("/metrics")
async def metrics():
    """Metrics endpoint (excluded from rate limiting)."""
    metrics_data = limiter.get_metrics()
    metrics_data["service"] = SERVICE_NAME
    return metrics_data


@app.get(
    "/limited",
    dependencies=[Depends(limit())]  # Uses default policy from limiter
)
async def limited_endpoint():
    """Endpoint with rate limiting using default policy."""
    return {
        "service": SERVICE_NAME,
        "endpoint": "limited",
        "message": "Request successful",
        "rate_limit": f"{RATE_PERMITS} per {RATE_PERIOD}, burst={RATE_BURST}",
    }


@app.get(
    "/custom",
    dependencies=[Depends(limit(Rate(permits=5, per="10s", burst=2)))]
)
async def custom_limit():
    """Endpoint with custom rate limit (5 per 10s, burst of 2)."""
    return {
        "service": SERVICE_NAME,
        "endpoint": "custom",
        "message": "Request successful",
        "rate_limit": "5 per 10s, burst=2",
    }


# API key based endpoint
api_key_limiter = Limiter(
    redis_url=REDIS_URL,
    default_policy=Rate(permits=20, per="1m", burst=5),
    extractor=extract_api_key(header_name="X-API-Key"),
    fail_mode=FAIL_MODE,
    app_name=f"{SERVICE_NAME}_apikey",
)


@app.get(
    "/api-key",
    dependencies=[Depends(limit(limiter=api_key_limiter))]
)
async def api_key_endpoint():
    """Endpoint with API key based rate limiting."""
    return {
        "service": SERVICE_NAME,
        "endpoint": "api-key",
        "message": "Request successful",
        "rate_limit": "20 per 1m with API key, burst=5",
    }


@app.post(
    "/post",
    dependencies=[Depends(limit(Rate(permits=3, per="1m", burst=0)))]
)
async def post_endpoint(data: dict | None = None):
    """POST endpoint with strict limit and no burst."""
    if data is None:
        data = {}
    return {
        "service": SERVICE_NAME,
        "endpoint": "post",
        "message": "Data received",
        "data": data,
        "rate_limit": "3 per 1m, no burst",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
