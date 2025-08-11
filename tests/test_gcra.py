"""Test GCRA algorithm implementation."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pacer import Limiter, Policy, Rate
from pacer.storage_simple import SimpleRedisStorage


class TestGCRAAlgorithm:
    """Test GCRA algorithm logic."""

    def test_gcra_math_first_request(self):
        """Test GCRA calculation for first request."""
        # Given
        emission_interval = 100  # 100ms between requests
        burst_capacity = 200  # 200ms burst (2 requests)
        now = 1000  # Current time in ms

        # When first request arrives, TAT should be initialized to now
        # and request should be allowed
        tat = now  # First request
        allow_at = tat - burst_capacity  # 1000 - 200 = 800

        # Should allow since now (1000) >= allow_at (800)
        assert now >= allow_at

        # New TAT = max(tat, now) + emission_interval = 1000 + 100 = 1100
        new_tat = max(tat, now) + emission_interval
        assert new_tat == 1100

    def test_gcra_math_burst_requests(self):
        """Test GCRA with burst of requests."""
        emission_interval = 100
        burst_capacity = 200  # Allows 2 extra requests

        # First request at t=1000
        now1 = 1000
        tat1 = now1
        new_tat1 = max(tat1, now1) + emission_interval
        assert new_tat1 == 1100

        # Second request immediately at t=1001 (within burst)
        now2 = 1001
        tat2 = new_tat1  # 1100
        allow_at2 = tat2 - burst_capacity  # 1100 - 200 = 900
        assert now2 >= allow_at2  # 1001 >= 900, allowed
        new_tat2 = max(tat2, now2) + emission_interval  # max(1100, 1001) + 100 = 1200
        assert new_tat2 == 1200

        # Third request immediately at t=1002 (within burst)
        now3 = 1002
        tat3 = new_tat2  # 1200
        allow_at3 = tat3 - burst_capacity  # 1200 - 200 = 1000
        assert now3 >= allow_at3  # 1002 >= 1000, allowed
        new_tat3 = max(tat3, now3) + emission_interval  # max(1200, 1002) + 100 = 1300
        assert new_tat3 == 1300

        # Fourth request immediately at t=1003 (exceeds burst)
        now4 = 1003
        tat4 = new_tat3  # 1300
        allow_at4 = tat4 - burst_capacity  # 1300 - 200 = 1100
        assert now4 < allow_at4  # 1003 < 1100, DENIED

        # Retry after should be allow_at4 - now4 = 1100 - 1003 = 97ms
        retry_after = allow_at4 - now4
        assert retry_after == 97

    def test_gcra_math_steady_rate(self):
        """Test GCRA at steady rate matching emission interval."""
        emission_interval = 100
        burst_capacity = 0  # No burst allowed

        # Requests at exact emission interval should all be allowed
        times = [1000, 1100, 1200, 1300, 1400]
        tat = times[0]

        for now in times:
            allow_at = tat - burst_capacity
            assert now >= allow_at  # All should be allowed
            tat = max(tat, now) + emission_interval

    def test_gcra_math_after_idle_period(self):
        """Test GCRA after idle period."""
        emission_interval = 100
        burst_capacity = 200

        # First request
        now1 = 1000
        tat1 = now1 + emission_interval  # 1100

        # Long idle period, then request at t=5000
        now2 = 5000
        allow_at2 = tat1 - burst_capacity  # 1100 - 200 = 900
        assert now2 >= allow_at2  # 5000 >= 900, allowed

        # TAT should be reset to current time since we're past the old TAT
        new_tat2 = max(tat1, now2) + emission_interval  # max(1100, 5000) + 100 = 5100
        assert new_tat2 == 5100

    def test_remaining_capacity_calculation(self):
        """Test calculation of remaining request capacity."""
        emission_interval = 100
        burst_capacity = 300  # Allows 3 burst requests

        # After first request at t=1000, TAT=1100
        now = 1000
        tat = 1100

        # Burst available = burst_capacity - (tat - now) = 300 - 100 = 200
        burst_available = burst_capacity - (tat - now)
        assert burst_available == 200

        # Remaining requests = burst_available / emission_interval = 200 / 100 = 2
        remaining = burst_available // emission_interval
        assert remaining == 2


class TestGCRABehavior:
    """Test real-world GCRA rate limiting scenarios using Policy API."""

    @pytest.mark.asyncio
    async def test_burst_then_steady_state(self):
        """Test that burst is consumed then rate limiting kicks in."""
        limiter = Limiter(redis_url="redis://localhost:6379")
        await limiter.startup()

        try:
            # 10 req/sec with burst of 5
            policy = Policy(
                rates=[Rate(permits=10, per="1s", burst=5)],
                key="ip",
                name="burst_test"
            )
            
            # Clear any existing key
            key = policy.generate_keys("pacer", "route", "/test", "192.168.1.1")[0]
            if limiter.storage.redis:
                await limiter.storage.redis.delete(key)

            request = MagicMock()
            request.client.host = "192.168.1.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"

            # Burst: First 6 requests should be allowed immediately
            for i in range(6):
                result = await limiter.check_policy(request, policy)
                assert result.allowed, f"Request {i+1} should be allowed (burst)"
                assert result.remaining >= 0

            # 7th request should be denied (burst exhausted)
            result = await limiter.check_policy(request, policy)
            assert not result.allowed, "7th request should be denied"
            assert result.retry_after_ms > 0, "Should have retry_after set"
            assert result.remaining == 0

            # Wait for one emission interval (100ms) then try again
            await asyncio.sleep(0.11)

            # Should allow one more request
            result = await limiter.check_policy(request, policy)
            assert result.allowed, "Should allow after waiting emission interval"

        finally:
            await limiter.shutdown()

    @pytest.mark.asyncio
    async def test_multiple_users_isolated(self):
        """Test that different users have isolated rate limits."""
        limiter = Limiter(redis_url="redis://localhost:6379")
        await limiter.startup()

        try:
            # 2 req/sec with burst of 1 (allows 2 immediate requests)
            policy = Policy(
                rates=[Rate(permits=2, per="1s", burst=1)],
                key="ip",
                name="isolation_test"
            )

            # Clear any existing keys
            key1 = policy.generate_keys("pacer", "route", "/test", "user1")[0]
            key2 = policy.generate_keys("pacer", "route", "/test", "user2")[0]
            if limiter.storage.redis:
                await limiter.storage.redis.delete(key1)
                await limiter.storage.redis.delete(key2)

            # Create request objects for two different users
            request1 = MagicMock()
            request1.client.host = "user1"
            request1.headers = {}
            request1.url.path = "/test"
            request1.method = "GET"

            request2 = MagicMock()
            request2.client.host = "user2"
            request2.headers = {}
            request2.url.path = "/test"
            request2.method = "GET"

            # User 1 makes 2 requests (uses regular + burst)
            for i in range(2):
                result = await limiter.check_policy(request1, policy)
                assert result.allowed, f"User1 request {i+1} should be allowed"

            # User 1's 3rd request should be denied
            result = await limiter.check_policy(request1, policy)
            assert not result.allowed, "User1's 3rd request should be denied"

            # User 2 should still be able to make requests
            for i in range(2):
                result = await limiter.check_policy(request2, policy)
                assert result.allowed, f"User2 request {i+1} should be allowed"

        finally:
            await limiter.shutdown()

    @pytest.mark.asyncio
    async def test_rate_limit_recovery_after_idle(self):
        """Test that rate limit recovers after idle period."""
        limiter = Limiter(redis_url="redis://localhost:6379")
        await limiter.startup()

        try:
            # 5 req/sec with burst of 2
            policy = Policy(
                rates=[Rate(permits=5, per="1s", burst=2)],
                key="ip",
                name="recovery_test"
            )

            # Clear any existing key
            key = policy.generate_keys("pacer", "route", "/test", "192.168.1.1")[0]
            if limiter.storage.redis:
                await limiter.storage.redis.delete(key)

            request = MagicMock()
            request.client.host = "192.168.1.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"

            # Use up all burst (3 requests)
            for i in range(3):
                result = await limiter.check_policy(request, policy)
                assert result.allowed, f"Request {i+1} should be allowed"

            # Next should be denied
            result = await limiter.check_policy(request, policy)
            assert not result.allowed, "Should be denied after burst exhausted"

            # Wait for 2 seconds (idle period)
            await asyncio.sleep(2)

            # Should recover full burst capacity after idle
            for i in range(3):
                result = await limiter.check_policy(request, policy)
                assert result.allowed, f"Request {i+1} after idle should be allowed"

        finally:
            await limiter.shutdown()

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        """Test that keys are properly expired with TTL."""
        limiter = Limiter(redis_url="redis://localhost:6379")
        await limiter.startup()

        try:
            # Very short rate limit for TTL testing  
            policy = Policy(
                rates=[Rate(permits=2, per="1s", burst=0)],
                key="ip",
                name="ttl_test"
            )

            request = MagicMock()
            request.client.host = "192.168.1.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"

            # Make a request
            result = await limiter.check_policy(request, policy)
            assert result.allowed, "First request should be allowed"

            # The key format is app_name:route_scope:scope:principal:rate_index:rate_desc
            # From the limiter.check_policy call, it would be: pacer:route:{/test}:192.168.1.1:r0:2/1s
            key = f"{limiter.app_name}:{limiter.route_scope}:{{/test}}:192.168.1.1:r0:2/1s"
            exists = await limiter.storage.redis.exists(key)
            assert exists, "Key should exist after first request"

            # TTL should be 2 * period = 2s
            ttl = await limiter.storage.redis.ttl(key)
            assert ttl > 0, "Key should have TTL set"
            assert ttl <= 2, "TTL should be around 2 seconds"

            # Wait for TTL to expire (add buffer)
            await asyncio.sleep(2.5)

            # Key should be gone
            exists = await limiter.storage.redis.exists(key)
            assert not exists, "Key should be expired after TTL"

            # Should be able to make requests again (fresh start)
            result = await limiter.check_policy(request, policy)
            assert result.allowed, "Should allow after key expired"

        finally:
            await limiter.shutdown()