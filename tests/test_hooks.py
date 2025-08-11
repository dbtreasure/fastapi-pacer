"""Test observability hooks functionality."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from pacer import Limiter, LimiterMiddleware, Policy, Rate, limit


class TestObservabilityHooks:
    """Test that hooks are called correctly."""

    @pytest.mark.asyncio
    async def test_on_decision_hook_called_with_dependency(self):
        """Test on_decision hook is called with correct data via dependency injection."""
        hook_calls = []

        def capture_decision(request, policy, result, duration_ms):
            hook_calls.append({
                "policy": policy.name if policy else None,
                "allowed": result.allowed,
                "duration_ms": duration_ms,
                "path": request.url.path,
            })

        app = FastAPI()
        limiter = Limiter(
            redis_url="redis://localhost:6379",
            on_decision=capture_decision,
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            if limiter.storage.redis:
                await limiter.storage.redis.flushdb()
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(Policy(rates=[Rate(10, "1m")], key="ip", name="test"), limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/test")
            assert response.status_code == 200

            # Verify hook was called
            assert len(hook_calls) == 1
            call = hook_calls[0]
            assert call["allowed"] is True
            assert call["duration_ms"] > 0
            assert "/test" in call["path"]
            assert call["policy"] in ["test", "middleware"]  # Policy name should match

    @pytest.mark.asyncio
    async def test_on_decision_hook_called_with_middleware(self):
        """Test on_decision hook is called with correct data via middleware."""
        hook_calls = []

        def capture_decision(request, policy, result, duration_ms):
            hook_calls.append({
                "policy": policy.name if policy else None,
                "allowed": result.allowed,
                "duration_ms": duration_ms,
                "path": request.url.path,
            })

        app = FastAPI()
        limiter = Limiter(
            redis_url="redis://localhost:6379",
            on_decision=capture_decision,
        )

        app.add_middleware(
            LimiterMiddleware,
            limiter=limiter,
            policy=Policy(rates=[Rate(permits=10, per="1m")], key="ip", name="middleware"),
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            if limiter.storage.redis:
                await limiter.storage.redis.flushdb()
            await limiter.shutdown()

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/test")
            assert response.status_code == 200

            # Verify hook was called
            assert len(hook_calls) == 1
            call = hook_calls[0]
            assert call["allowed"] is True
            assert call["duration_ms"] > 0
            assert "/test" in call["path"]
            assert call["policy"] in ["test", "middleware"]  # Policy name should match

    @pytest.mark.asyncio
    async def test_on_decision_hook_called_on_rate_limit(self):
        """Test on_decision hook is called when request is rate limited."""
        hook_calls = []

        def capture_decision(request, policy, result, duration_ms):
            hook_calls.append({
                "allowed": result.allowed,
                "retry_after_ms": result.retry_after_ms,
            })

        app = FastAPI()
        limiter = Limiter(
            redis_url="redis://localhost:6379",
            on_decision=capture_decision,
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            if limiter.storage.redis:
                await limiter.storage.redis.flushdb()
            await limiter.shutdown()

        # Very low limit to trigger rate limiting
        @app.get("/test", dependencies=[Depends(limit(Policy(rates=[Rate(1, "60s", burst=0)], key="ip", name="test"), limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # First request should succeed
            response = await client.get("/test")
            assert response.status_code == 200
            assert hook_calls[0]["allowed"] is True

            # Second request should be rate limited
            response = await client.get("/test")
            assert response.status_code == 429
            assert len(hook_calls) == 2
            assert hook_calls[1]["allowed"] is False
            assert hook_calls[1]["retry_after_ms"] > 0

    @pytest.mark.asyncio
    async def test_on_error_hook_called(self):
        """Test on_error hook is called on Redis errors."""
        error_calls = []

        def capture_error(request, policy, error, duration_ms):
            error_calls.append({
                "error_type": type(error).__name__,
                "duration_ms": duration_ms,
            })

        app = FastAPI()
        # Use invalid Redis URL to trigger error
        limiter = Limiter(
            redis_url="redis://invalid-host:6379",
            on_error=capture_error,
            fail_mode="open",  # Allow request despite error
            connect_timeout_ms=100,  # Short timeout to fail fast
        )

        @app.get("/test", dependencies=[Depends(limit(Policy(rates=[Rate(10, "1m")], key="ip", name="test"), limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/test")
            # Should succeed due to fail-open
            assert response.status_code == 200

            # Verify error hook was called
            assert len(error_calls) >= 1
            assert error_calls[0]["duration_ms"] >= 0
