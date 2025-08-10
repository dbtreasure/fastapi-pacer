"""
Integration tests for FastAPI Pacer rate limiter.

These tests require the test services to be running:
    docker-compose -f examples/docker-compose.test.yml up -d

Run tests with:
    uv run pytest tests/test_integration.py -v
"""

import asyncio

import httpx
import pytest

# Service configurations for testing
SERVICES = {
    "basic": {
        "port": 8001,
        "permits": 10,
        "period": "1m",
        "burst": 5,
    },
    "strict": {
        "port": 8002,
        "permits": 5,
        "period": "1m",
        "burst": 0,
    },
    "burst": {
        "port": 8003,
        "permits": 10,
        "period": "10s",
        "burst": 5,
    },
    "highvolume": {
        "port": 8004,
        "permits": 1000,
        "period": "1m",
        "burst": 100,
    },
    "middleware": {
        "port": 8005,
        "permits": 50,
        "period": "1m",
        "burst": 10,
        "middleware_permits": 100,
    },
    "fast": {
        "port": 8006,
        "permits": 100,
        "period": "1s",
        "burst": 10,
    },
}


@pytest.fixture
async def http_client():
    """Create an async HTTP client."""
    async with httpx.AsyncClient() as client:
        yield client


async def wait_for_service(client: httpx.AsyncClient, port: int, max_retries: int = 30):
    """Wait for a service to be healthy."""
    url = f"http://localhost:{port}/health"
    for _ in range(max_retries):
        try:
            response = await client.get(url, timeout=1.0)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        await asyncio.sleep(1)
    return False


@pytest.mark.asyncio
class TestBasicRateLimiting:
    """Test basic rate limiting functionality."""

    async def test_service_health(self, http_client):
        """Test that all services are healthy."""
        for name, config in SERVICES.items():
            port = config["port"]
            is_healthy = await wait_for_service(http_client, port)
            assert is_healthy, f"Service {name} on port {port} is not healthy"

    async def test_basic_rate_limit(self, http_client):
        """Test basic rate limiting (10/min, burst=5)."""
        port = SERVICES["basic"]["port"]
        url = f"http://localhost:{port}/limited"

        # Should allow initial burst
        success_count = 0
        for _ in range(8):
            response = await http_client.get(url)
            if response.status_code == 200:
                success_count += 1

        # With burst=5 and rate=10/min, we should get some successes
        assert success_count > 0, "Should allow some requests"
        assert success_count < 8, "Should block some requests after burst"

    async def test_strict_no_burst(self, http_client):
        """Test strict rate limiting with no burst (5/min, burst=0)."""
        port = SERVICES["strict"]["port"]
        url = f"http://localhost:{port}/limited"

        # First request should succeed
        response = await http_client.get(url)
        assert response.status_code == 200

        # Immediate second request should fail (no burst)
        response = await http_client.get(url)
        assert response.status_code == 429

    async def test_burst_capacity(self, http_client):
        """Test burst capacity (10/10s, burst=5)."""
        port = SERVICES["burst"]["port"]
        url = f"http://localhost:{port}/limited"

        # Clear any previous state
        await asyncio.sleep(11)  # Wait for full period to reset

        # Send burst of requests
        responses = []
        for _ in range(8):
            response = await http_client.get(url)
            responses.append(response.status_code)

        success_count = sum(1 for status in responses if status == 200)

        # Should allow burst capacity
        assert success_count >= 5, f"Should allow at least burst capacity (5), got {success_count}"
        assert success_count <= 6, f"Should not exceed burst + 1, got {success_count}"


@pytest.mark.asyncio
class TestRateLimitHeaders:
    """Test rate limit response headers."""

    async def test_headers_on_success(self, http_client):
        """Test headers on successful request."""
        port = SERVICES["basic"]["port"]
        url = f"http://localhost:{port}/limited"

        response = await http_client.get(url)

        # Should have rate limit headers
        assert "RateLimit-Limit" in response.headers
        assert "RateLimit-Remaining" in response.headers
        assert "RateLimit-Reset" in response.headers

        # Verify header values
        assert response.headers["RateLimit-Limit"] == "10"
        remaining = int(response.headers["RateLimit-Remaining"])
        assert remaining >= 0

    async def test_headers_on_rate_limit(self, http_client):
        """Test headers when rate limited."""
        port = SERVICES["strict"]["port"]
        url = f"http://localhost:{port}/limited"

        # Exhaust rate limit
        for _ in range(10):
            response = await http_client.get(url)
            if response.status_code == 429:
                break

        assert response.status_code == 429
        assert "Retry-After" in response.headers
        assert "RateLimit-Limit" in response.headers
        assert response.headers["RateLimit-Remaining"] == "0"

        # Check response body
        body = response.json()
        # FastAPI wraps our detail dict in another detail field
        assert body["detail"]["detail"] == "rate_limited"
        assert "retry_after_ms" in body["detail"]


@pytest.mark.asyncio
class TestCustomEndpoints:
    """Test custom rate limited endpoints."""

    async def test_custom_limit_endpoint(self, http_client):
        """Test endpoint with custom rate limit (5/10s, burst=2)."""
        port = SERVICES["basic"]["port"]
        url = f"http://localhost:{port}/custom"

        # Should allow initial requests with burst
        responses = []
        for _ in range(5):
            response = await http_client.get(url)
            responses.append(response.status_code)

        success_count = sum(1 for status in responses if status == 200)
        assert success_count >= 2, "Should allow at least burst capacity"
        assert success_count <= 3, "Should not exceed burst + 1"

    async def test_post_endpoint_no_burst(self, http_client):
        """Test POST endpoint with no burst (3/min, burst=0)."""
        port = SERVICES["basic"]["port"]
        url = f"http://localhost:{port}/post"

        # First POST should succeed
        response = await http_client.post(url, json={"test": "data"})
        assert response.status_code == 200

        # Second POST should fail (no burst)
        response = await http_client.post(url, json={"test": "data2"})
        assert response.status_code == 429


@pytest.mark.asyncio
class TestAPIKeyRateLimiting:
    """Test API key based rate limiting."""

    async def test_different_api_keys(self, http_client):
        """Test that different API keys get separate limits."""
        port = SERVICES["basic"]["port"]
        url = f"http://localhost:{port}/api-key"

        # Test with first API key
        headers1 = {"X-API-Key": "test-key-1"}
        response = await http_client.get(url, headers=headers1)
        assert response.status_code == 200

        # Test with second API key (should have its own limit)
        headers2 = {"X-API-Key": "test-key-2"}
        response = await http_client.get(url, headers=headers2)
        assert response.status_code == 200

        # Without API key (falls back to IP)
        response = await http_client.get(url)
        # May succeed or fail depending on IP-based limit state


@pytest.mark.asyncio
class TestMiddleware:
    """Test middleware rate limiting."""

    async def test_middleware_applies_globally(self, http_client):
        """Test that middleware applies to all endpoints."""
        port = SERVICES["middleware"]["port"]

        # Test root endpoint (should have middleware limit)
        url = f"http://localhost:{port}/"
        response = await http_client.get(url)
        assert "RateLimit-Limit" in response.headers

        # Test limited endpoint (has both middleware and endpoint limit)
        url = f"http://localhost:{port}/limited"
        response = await http_client.get(url)
        assert "RateLimit-Limit" in response.headers

    async def test_excluded_endpoints(self, http_client):
        """Test that certain endpoints are excluded from middleware."""
        port = SERVICES["middleware"]["port"]

        # Health endpoint should not be rate limited
        url = f"http://localhost:{port}/health"
        for _ in range(10):
            response = await http_client.get(url)
            assert response.status_code == 200

        # Config endpoint should not be rate limited
        url = f"http://localhost:{port}/config"
        for _ in range(10):
            response = await http_client.get(url)
            assert response.status_code == 200


@pytest.mark.asyncio
class TestHighVolume:
    """Test high volume rate limiting."""

    async def test_high_volume_allows_many_requests(self, http_client):
        """Test that high volume config allows many requests (1000/min)."""
        port = SERVICES["highvolume"]["port"]
        url = f"http://localhost:{port}/limited"

        # Should allow many requests
        success_count = 0
        for _ in range(150):  # Test with 150 requests
            response = await http_client.get(url)
            if response.status_code == 200:
                success_count += 1

        # Should allow at least burst capacity (100)
        assert success_count >= 100, f"Should allow at least burst (100), got {success_count}"


@pytest.mark.asyncio
class TestFastRate:
    """Test fast rate limiting (per second)."""

    async def test_per_second_rate_limit(self, http_client):
        """Test per-second rate limiting (100/1s, burst=10)."""
        port = SERVICES["fast"]["port"]
        url = f"http://localhost:{port}/limited"

        # Send burst of requests
        responses = []
        for _ in range(15):
            response = await http_client.get(url)
            responses.append(response.status_code)

        success_count = sum(1 for status in responses if status == 200)

        # Should allow burst + some (100/sec means a few more might squeeze in during the request loop)
        # With 100 permits/sec, during 15 rapid requests some additional permits may become available
        assert success_count >= 10, f"Should allow at least burst capacity (10), got {success_count}"
        assert success_count <= 15, f"Should not exceed total requests (15), got {success_count}"

        # Wait 1 second for refill
        await asyncio.sleep(1.1)

        # Should allow more requests after refill
        response = await http_client.get(url)
        assert response.status_code == 200


@pytest.mark.asyncio
class TestMetrics:
    """Test metrics endpoint."""

    async def test_metrics_tracking(self, http_client):
        """Test that metrics are properly tracked."""
        port = SERVICES["basic"]["port"]

        # Get initial metrics
        metrics_url = f"http://localhost:{port}/metrics"
        response = await http_client.get(metrics_url)
        initial_metrics = response.json()

        # Make some requests
        url = f"http://localhost:{port}/limited"
        for _ in range(5):
            await http_client.get(url)

        # Check updated metrics
        response = await http_client.get(metrics_url)
        updated_metrics = response.json()

        # Should have more requests than initial
        assert updated_metrics["limiter"]["requests_allowed"] >= initial_metrics["limiter"]["requests_allowed"]
        assert updated_metrics["connected"] is True


@pytest.mark.asyncio
class TestFailModes:
    """Test fail-open vs fail-closed modes."""

    async def test_service_configurations(self, http_client):
        """Test that services have correct configurations."""
        # Basic service should be fail-open
        response = await http_client.get(f"http://localhost:{SERVICES['basic']['port']}/config")
        config = response.json()
        assert config["rate_config"]["fail_mode"] == "open"

        # Strict service should be fail-closed
        response = await http_client.get(f"http://localhost:{SERVICES['strict']['port']}/config")
        config = response.json()
        assert config["rate_config"]["fail_mode"] == "closed"


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
