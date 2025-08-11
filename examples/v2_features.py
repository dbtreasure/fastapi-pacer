#!/usr/bin/env python3
"""
Example demonstrating v0.2.0 features of FastAPI Pacer.

New features:
- Multi-rate policies (all rates must pass)
- Flexible selector system for identity extraction
- OpenTelemetry integration hooks
- Policy-based API (cleaner than Rate-only)
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from pacer import (
    Limiter,
    LimiterMiddleware,
    Policy,
    Rate,
    compose,
    create_otel_hooks,
    key_ip,
    key_user,
    limit,
    set_limiter,
)

# Initialize limiter with OTel hooks
on_decision, on_error = create_otel_hooks("example-api")

limiter = Limiter(
    redis_url="redis://localhost:6379",
    on_decision=on_decision,
    on_error=on_error,
    expose_policy_header=True,  # Show policy in headers for debugging
)

# Set global limiter for dependency injection
set_limiter(limiter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage limiter lifecycle."""
    await limiter.startup()
    yield
    await limiter.shutdown()


app = FastAPI(
    title="FastAPI Pacer v0.2.0 Demo",
    description="Demonstrating multi-rate policies and selectors",
    lifespan=lifespan,
)

# Example 1: Global middleware with multi-rate policy
global_policy = Policy(
    rates=[
        Rate(1000, "1m"),  # 1000 requests per minute
        Rate(100, "10s"),  # 100 requests per 10 seconds (burst protection)
    ],
    key="ip",
    name="global",
)

app.add_middleware(
    LimiterMiddleware,
    limiter=limiter,
    policy=global_policy,
    exclude_paths=["/health", "/metrics"],
)


# Example 2: Per-route rate limiting with API key selector
api_policy = Policy(
    rates=[Rate(100, "1m", burst=20)],
    key="api_key",
    name="api_endpoint",
)


@app.get("/api/data", dependencies=[Depends(limit(api_policy))])
async def get_data(x_api_key: Annotated[str | None, Header()] = None):
    """
    API endpoint with rate limiting by API key.
    
    Try with: curl -H "X-API-Key: test123" http://localhost:8000/api/data
    """
    return {"message": "API data", "api_key": x_api_key is not None}


# Example 3: Multi-rate policy for critical endpoints
critical_policy = Policy(
    rates=[
        Rate(10, "1m"),    # 10 per minute
        Rate(2, "10s"),    # 2 per 10 seconds
        Rate(100, "1h"),   # 100 per hour
    ],
    key="ip",
    name="critical",
)


@app.post("/api/critical", dependencies=[Depends(limit(critical_policy))])
async def critical_operation():
    """
    Critical endpoint with strict multi-rate limiting.
    All three rates must pass for the request to be allowed.
    """
    return {"message": "Critical operation performed"}


# Example 4: User-based rate limiting with authentication
class AuthMiddleware:
    """Mock authentication middleware that sets user_id."""

    async def __call__(self, request: Request, call_next):
        # In real app, extract from JWT or session
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            # Mock: extract user ID from token
            request.state.user_id = "user_" + auth_header[7:][:10]
        else:
            request.state.user_id = None

        response = await call_next(request)
        return response


# Add auth middleware before rate limiting
app.middleware("http")(AuthMiddleware())

user_policy = Policy(
    rates=[Rate(50, "1m", burst=10)],
    key="user",
    name="user_endpoint",
)


@app.get("/api/user/profile", dependencies=[Depends(limit(user_policy))])
async def get_user_profile(request: Request):
    """
    User endpoint with per-user rate limiting.
    
    Try with: curl -H "Authorization: Bearer abc123" http://localhost:8000/api/user/profile
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    return {"user_id": user_id, "profile": "User profile data"}


# Example 5: Composed selector (user + IP combination)
composed_policy = Policy(
    rates=[Rate(30, "1m")],
    key=compose(key_user, key_ip),
    name="composed",
)


@app.post("/api/user/action", dependencies=[Depends(limit(composed_policy))])
async def user_action(request: Request):
    """
    Rate limit by combination of user AND IP address.
    This prevents a single user from making too many requests even from different IPs.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    return {"message": f"Action performed by {user_id}"}


# Example 6: Custom selector function
def key_by_endpoint(request: Request) -> str:
    """Custom selector that rate limits by endpoint + method."""
    return f"{request.method}:{request.url.path}"


custom_policy = Policy(
    rates=[Rate(200, "1m")],
    key=key_by_endpoint,
    name="by_endpoint",
)


@app.get("/api/custom", dependencies=[Depends(limit(custom_policy))])
async def custom_endpoint():
    """
    Uses custom selector to rate limit by endpoint.
    All requests to this endpoint share the same rate limit.
    """
    return {"message": "Custom selector example"}


# Example 7: Different policies for different HTTP methods
read_policy = Policy(
    rates=[Rate(100, "1m")],
    key="ip",
    name="read_ops",
)

write_policy = Policy(
    rates=[Rate(10, "1m", burst=2)],
    key="ip",
    name="write_ops",
)


@app.get("/api/resource", dependencies=[Depends(limit(read_policy))])
async def read_resource():
    """More permissive rate limit for read operations."""
    return {"operation": "read", "data": "Resource data"}


@app.post("/api/resource", dependencies=[Depends(limit(write_policy))])
async def write_resource():
    """Stricter rate limit for write operations."""
    return {"operation": "write", "status": "created"}


# Health check endpoint (excluded from global middleware)
@app.get("/health")
async def health_check():
    """Health check endpoint - not rate limited."""
    is_healthy = await limiter.is_healthy()
    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "redis": "connected" if is_healthy else "disconnected",
    }


# Metrics endpoint showing rate limiter stats
@app.get("/metrics")
async def get_metrics():
    """Get rate limiter metrics."""
    return limiter.get_metrics()


if __name__ == "__main__":
    import uvicorn

    print("Starting FastAPI Pacer v0.2.0 demo...")
    print("Features demonstrated:")
    print("- Multi-rate policies (multiple rates must all pass)")
    print("- Flexible selectors (IP, API key, user, composed)")
    print("- Custom selector functions")
    print("- Per-route and global rate limiting")
    print("- OpenTelemetry integration")
    print()
    print("Try these commands:")
    print("  curl http://localhost:8000/api/data")
    print("  curl -H 'X-API-Key: test123' http://localhost:8000/api/data")
    print("  curl -H 'Authorization: Bearer abc123' http://localhost:8000/api/user/profile")
    print("  curl -X POST http://localhost:8000/api/critical")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000)
