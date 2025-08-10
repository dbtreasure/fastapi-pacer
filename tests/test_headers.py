"""Test rate limit headers functionality."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from pacer import Limiter, Rate, limit
from pacer.dependencies import get_limiter


@pytest.fixture
async def app_with_policy_header():
    """Create test app with policy header enabled."""
    app = FastAPI()

    limiter = Limiter(
        redis_url="redis://localhost:6379",
        expose_headers=True,
        expose_policy_header=True,  # Enable policy header
    )

    # Store limiter for dependency injection
    app.state.limiter = limiter

    @app.on_event("startup")
    async def startup():
        await limiter.startup()

    @app.on_event("shutdown")
    async def shutdown():
        # Clear all keys to avoid interference between tests
        if limiter.storage.redis:
            await limiter.storage.redis.flushdb()
        await limiter.shutdown()

    # Override dependency
    def get_test_limiter():
        return app.state.limiter

    app.dependency_overrides[get_limiter] = get_test_limiter

    @app.get("/", dependencies=[Depends(limit(Rate(100, "1m", burst=10), limiter=limiter))])
    async def root():
        return {"message": "ok"}

    @app.get("/noburst", dependencies=[Depends(limit(Rate(50, "10s", burst=0), limiter=limiter))])
    async def noburst():
        return {"message": "ok"}

    @app.get("/strict", dependencies=[Depends(limit(Rate(1, "60s", burst=0), limiter=limiter))])
    async def strict():
        return {"message": "ok"}

    return app


@pytest.fixture
async def app_without_policy_header():
    """Create test app without policy header."""
    app = FastAPI()

    limiter = Limiter(
        redis_url="redis://localhost:6379",
        expose_headers=True,
        expose_policy_header=False,  # Disable policy header (default)
    )

    app.state.limiter = limiter

    @app.on_event("startup")
    async def startup():
        await limiter.startup()

    @app.on_event("shutdown")
    async def shutdown():
        # Clear all keys to avoid interference between tests
        if limiter.storage.redis:
            await limiter.storage.redis.flushdb()
        await limiter.shutdown()

    # Override dependency
    def get_test_limiter():
        return app.state.limiter

    app.dependency_overrides[get_limiter] = get_test_limiter

    @app.get("/", dependencies=[Depends(limit(Rate(100, "1m", burst=10), limiter=limiter))])
    async def root():
        return {"message": "ok"}

    return app


@pytest.fixture
async def app_with_legacy_header():
    """Create test app with legacy timestamp header."""
    app = FastAPI()

    limiter = Limiter(
        redis_url="redis://localhost:6379",
        expose_headers=True,
        legacy_timestamp_header=True,  # Enable legacy header
    )

    app.state.limiter = limiter

    @app.on_event("startup")
    async def startup():
        await limiter.startup()

    @app.on_event("shutdown")
    async def shutdown():
        # Clear all keys to avoid interference between tests
        if limiter.storage.redis:
            await limiter.storage.redis.flushdb()
        await limiter.shutdown()

    # Override dependency
    def get_test_limiter():
        return app.state.limiter

    app.dependency_overrides[get_limiter] = get_test_limiter

    @app.get("/", dependencies=[Depends(limit(Rate(100, "1m"), limiter=limiter))])
    async def root():
        return {"message": "ok"}

    return app


class TestRateLimitHeaders:
    """Test rate limit headers including new policy header."""

    @pytest.mark.asyncio
    async def test_standard_headers(self, app_without_policy_header):
        """Test standard rate limit headers are included."""
        async with AsyncClient(transport=ASGITransport(app=app_without_policy_header), base_url="http://test") as client:
            response = await client.get("/")

            assert response.status_code == 200
            assert "RateLimit-Limit" in response.headers
            assert response.headers["RateLimit-Limit"] == "100"
            assert "RateLimit-Remaining" in response.headers
            assert "RateLimit-Reset" in response.headers

            # Should be delta-seconds, not timestamp
            reset_value = int(response.headers["RateLimit-Reset"])
            assert reset_value <= 60  # Should be seconds, not timestamp

    @pytest.mark.asyncio
    async def test_legacy_timestamp_header(self, app_with_legacy_header):
        """Test optional Unix timestamp header."""
        async with AsyncClient(transport=ASGITransport(app=app_with_legacy_header), base_url="http://test") as client:
            # Use a unique header to avoid rate limit conflict
            response = await client.get("/", headers={"X-Test-ID": "legacy-test"})

            assert response.status_code == 200
            assert "X-RateLimit-Reset" in response.headers
            # Should be Unix timestamp (large number)
            reset_timestamp = int(response.headers["X-RateLimit-Reset"])
            assert reset_timestamp > 1700000000  # After Nov 2023

    @pytest.mark.asyncio
    async def test_no_legacy_header_by_default(self, app_without_policy_header):
        """Test that X-RateLimit-Reset is NOT included by default."""
        async with AsyncClient(transport=ASGITransport(app=app_without_policy_header), base_url="http://test") as client:
            response = await client.get("/")

            assert response.status_code == 200
            # Standard header should be present
            assert "RateLimit-Reset" in response.headers
            # Legacy header should NOT be present
            assert "X-RateLimit-Reset" not in response.headers

    @pytest.mark.asyncio
    async def test_policy_header_disabled_by_default(self, app_without_policy_header):
        """Test that policy header is not included by default."""
        async with AsyncClient(transport=ASGITransport(app=app_without_policy_header), base_url="http://test") as client:
            response = await client.get("/")

            assert response.status_code == 200
            assert "X-RateLimit-Policy" not in response.headers

    @pytest.mark.asyncio
    async def test_policy_header_with_burst(self, app_with_policy_header):
        """Test policy header includes burst when enabled."""
        async with AsyncClient(transport=ASGITransport(app=app_with_policy_header), base_url="http://test") as client:
            response = await client.get("/")

            assert response.status_code == 200
            assert "X-RateLimit-Policy" in response.headers
            assert response.headers["X-RateLimit-Policy"] == "100;w=1m;burst=10"

    @pytest.mark.asyncio
    async def test_policy_header_without_burst(self, app_with_policy_header):
        """Test policy header without burst."""
        async with AsyncClient(transport=ASGITransport(app=app_with_policy_header), base_url="http://test") as client:
            response = await client.get("/noburst")

            assert response.status_code == 200
            assert "X-RateLimit-Policy" in response.headers
            assert response.headers["X-RateLimit-Policy"] == "50;w=10s"

    @pytest.mark.asyncio
    async def test_retry_after_on_rate_limit(self, app_with_policy_header):
        """Test Retry-After header is included on 429 responses."""
        async with AsyncClient(transport=ASGITransport(app=app_with_policy_header), base_url="http://test") as client:
            # Use strict endpoint with 1 request per 60s
            # First request should succeed
            response = await client.get("/strict")
            assert response.status_code == 200

            # Second request should be rate limited
            response = await client.get("/strict")
            assert response.status_code == 429
            assert "Retry-After" in response.headers
            retry_after = int(response.headers["Retry-After"])
            assert retry_after > 0 and retry_after <= 60
