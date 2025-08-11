"""Test rate limiting with concurrent requests."""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from pacer import Limiter, Policy, Rate, limit


class TestConcurrency:
    """Test rate limiting behavior under concurrent load."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_same_identity(self):
        """Test that rate limits are enforced correctly for concurrent requests from same identity."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # Policy: 5 requests per 10 seconds with burst of 2
        # This allows 1 + 2 = 3 immediate requests
        policy = Policy(
            rates=[Rate(5, "10s", burst=2)],
            key="ip",
            name="concurrent_test"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown") 
        async def shutdown():
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(policy, limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Make 10 concurrent requests from same IP
            async def make_request():
                return await client.get("/test")

            # Launch all requests concurrently
            tasks = [make_request() for _ in range(10)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Count successful and rate-limited responses
            success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            rate_limited_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 429)

            # Should have exactly 3 successful (1 + 2 burst) and 7 rate limited
            assert success_count == 3, f"Expected 3 successful requests, got {success_count}"
            assert rate_limited_count == 7, f"Expected 7 rate-limited requests, got {rate_limited_count}"

    @pytest.mark.asyncio
    async def test_concurrent_requests_different_identities(self):
        """Test that rate limits are isolated between different identities."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # Policy: 2 requests per 5 seconds with burst of 1
        # This allows 2 immediate requests per key
        policy = Policy(
            rates=[Rate(2, "5s", burst=1)],
            key="api_key",
            name="identity_test"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(policy, limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Make concurrent requests from 3 different API keys
            async def make_requests_for_key(api_key: str, count: int):
                results = []
                for _ in range(count):
                    response = await client.get("/test", headers={"X-API-Key": api_key})
                    results.append(response.status_code)
                return results

            # Each API key should get 2 successful requests
            tasks = [
                make_requests_for_key("key1", 3),  # Should get 2 success, 1 rate limited
                make_requests_for_key("key2", 3),  # Should get 2 success, 1 rate limited
                make_requests_for_key("key3", 3),  # Should get 2 success, 1 rate limited
            ]
            
            results = await asyncio.gather(*tasks)

            # Verify each API key got exactly 2 successful requests
            for key_results in results:
                success_count = sum(1 for status in key_results if status == 200)
                rate_limited_count = sum(1 for status in key_results if status == 429)
                assert success_count == 2, f"Expected 2 successful requests per key, got {success_count}"
                assert rate_limited_count == 1, f"Expected 1 rate-limited request per key, got {rate_limited_count}"

    @pytest.mark.asyncio
    async def test_burst_recovery_with_concurrent_requests(self):
        """Test that burst capacity recovers correctly under concurrent load."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # Policy: 10 requests per second with burst of 5
        # This allows 1 + 5 = 6 immediate requests
        policy = Policy(
            rates=[Rate(10, "1s", burst=5)],
            key="ip",
            name="burst_recovery"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(policy, limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Phase 1: Exhaust burst with concurrent requests
            tasks = [client.get("/test") for _ in range(15)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            phase1_success = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            assert phase1_success == 6, f"Expected 6 requests to succeed (1 + 5 burst), got {phase1_success}"

            # Phase 2: Immediate requests should be rate limited
            response = await client.get("/test")
            assert response.status_code == 429, "Should be rate limited after burst exhausted"

            # Phase 3: Wait for partial recovery (200ms = 2 permits)
            await asyncio.sleep(0.2)
            
            # Should be able to make 2 more requests
            tasks = [client.get("/test") for _ in range(3)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            phase3_success = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            assert phase3_success == 2, f"Expected 2 successful after recovery, got {phase3_success}"

    @pytest.mark.asyncio
    async def test_multi_rate_concurrent_requests(self):
        """Test multi-rate policies under concurrent load."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # Multi-rate policy: Most restrictive should win
        policy = Policy(
            rates=[
                Rate(100, "1m", burst=10),  # 100/min with burst 10
                Rate(10, "10s", burst=2),   # 10/10s with burst 2 (most restrictive)
                Rate(1000, "1h", burst=50), # 1000/hour with burst 50
            ],
            key="ip",
            name="multi_rate"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(policy, limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Make 15 concurrent requests
            tasks = [client.get("/test") for _ in range(15)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Should be limited by the 10/10s + 2 burst = 1 + 2 = 3 total immediate
            success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            rate_limited_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 429)

            assert success_count == 3, f"Expected 3 successful (1 + 2 burst), got {success_count}"
            assert rate_limited_count == 12, f"Expected 12 rate-limited, got {rate_limited_count}"

    @pytest.mark.asyncio
    async def test_race_condition_handling(self):
        """Test that the rate limiter correctly handles race conditions."""
        # This tests that the Lua script atomicity prevents race conditions
        limiter = Limiter(redis_url="redis://localhost:6379")
        await limiter.startup()

        try:
            # Very restrictive policy to make race conditions more likely
            policy = Policy(
                rates=[Rate(1, "1s", burst=0)],  # Only 1 request per second, no burst
                key="ip",
                name="race_test"
            )

            # Create multiple mock requests with same IP
            requests = []
            for _ in range(10):
                request = MagicMock()
                request.client.host = "192.168.1.1"
                request.headers = {}
                request.url.path = "/test"
                request.method = "GET"
                requests.append(request)

            # Launch all checks concurrently (simulating race condition)
            tasks = [limiter.check_policy(req, policy) for req in requests]
            results = await asyncio.gather(*tasks)

            # Exactly one should succeed, rest should be rate limited
            allowed_count = sum(1 for r in results if r.allowed)
            denied_count = sum(1 for r in results if not r.allowed)

            assert allowed_count == 1, f"Expected exactly 1 allowed request, got {allowed_count}"
            assert denied_count == 9, f"Expected 9 denied requests, got {denied_count}"

        finally:
            await limiter.shutdown()

    @pytest.mark.asyncio
    async def test_concurrent_different_endpoints(self):
        """Test rate limiting across different endpoints with same policy."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # Shared policy for multiple endpoints
        shared_policy = Policy(
            rates=[Rate(5, "5s", burst=2)],
            key="ip",
            name="shared"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            await limiter.shutdown()

        @app.get("/endpoint1", dependencies=[Depends(limit(shared_policy, limiter=limiter))])
        async def endpoint1():
            return {"endpoint": 1}

        @app.get("/endpoint2", dependencies=[Depends(limit(shared_policy, limiter=limiter))])
        async def endpoint2():
            return {"endpoint": 2}

        @app.get("/endpoint3", dependencies=[Depends(limit(shared_policy, limiter=limiter))])
        async def endpoint3():
            return {"endpoint": 3}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Make concurrent requests to different endpoints
            tasks = []
            for _ in range(3):
                tasks.append(client.get("/endpoint1"))
                tasks.append(client.get("/endpoint2"))
                tasks.append(client.get("/endpoint3"))
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Each endpoint has its own rate limit counter
            # So all 9 requests should succeed (3 per endpoint, each has limit of 5+2)
            success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            assert success_count == 9, f"Expected all 9 requests to succeed (separate limits per endpoint), got {success_count}"

    @pytest.mark.asyncio
    async def test_high_concurrency_stress(self):
        """Stress test with high concurrency to ensure stability."""
        app = FastAPI()
        limiter = Limiter(redis_url="redis://localhost:6379")

        # High-volume policy: 100/sec with burst 50
        # This allows 1 + 50 = 51 immediate requests
        policy = Policy(
            rates=[Rate(100, "1s", burst=50)],
            key="ip",
            name="stress_test"
        )

        @app.on_event("startup")
        async def startup():
            await limiter.startup()

        @app.on_event("shutdown")
        async def shutdown():
            await limiter.shutdown()

        @app.get("/test", dependencies=[Depends(limit(policy, limiter=limiter))])
        async def test_endpoint():
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Launch 200 concurrent requests
            tasks = [client.get("/test") for _ in range(200)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Should get exactly 51 successful (1 + 50 burst)
            success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 200)
            rate_limited_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 429)
            error_count = sum(1 for r in responses if isinstance(r, Exception))

            assert error_count == 0, f"Should have no errors, got {error_count}"
            # Allow 51 or 52 due to timing
            assert 51 <= success_count <= 52, f"Expected 51-52 successful requests, got {success_count}"
            assert 148 <= rate_limited_count <= 149, f"Expected 148-149 rate-limited requests, got {rate_limited_count}"