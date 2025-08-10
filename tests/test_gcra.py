
import asyncio

import pytest

from pacer.policies import Rate
from pacer.storage import RedisStorage


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
    """Test real-world GCRA rate limiting scenarios."""

    @pytest.mark.asyncio
    async def test_burst_then_steady_state(self):
        """Test that burst is consumed then rate limiting kicks in."""
        storage = RedisStorage(redis_url="redis://localhost:6379")
        await storage.connect()

        try:
            # 10 req/sec with burst of 5
            rate = Rate(permits=10, per="1s", burst=5)
            key = "test:burst:scenario:user1"

            # Clear any existing key
            if storage._client:
                await storage._client.delete(key)

            # Burst: First 6 requests should be allowed immediately
            for i in range(6):
                allowed, retry_after, _, remaining = await storage.check_rate_limit(
                    key=key,
                    emission_interval_ms=rate.emission_interval_ms,
                    burst_capacity_ms=rate.burst_capacity_ms,
                    ttl_ms=rate.ttl_ms,
                )
                assert allowed, f"Request {i+1} should be allowed (burst)"
                assert remaining >= 0

            # 7th request should be denied (burst exhausted)
            allowed, retry_after, _, remaining = await storage.check_rate_limit(
                key=key,
                emission_interval_ms=rate.emission_interval_ms,
                burst_capacity_ms=rate.burst_capacity_ms,
                ttl_ms=rate.ttl_ms,
            )
            assert not allowed, "7th request should be denied"
            assert retry_after > 0, "Should have retry_after set"
            assert remaining == 0

            # Wait for one emission interval (100ms) then try again
            await asyncio.sleep(0.11)

            # Should allow one more request
            allowed, _, _, _ = await storage.check_rate_limit(
                key=key,
                emission_interval_ms=rate.emission_interval_ms,
                burst_capacity_ms=rate.burst_capacity_ms,
                ttl_ms=rate.ttl_ms,
            )
            assert allowed, "Should allow after waiting emission interval"

        finally:
            await storage.disconnect()

    @pytest.mark.asyncio
    async def test_multiple_users_isolated(self):
        """Test that different users have isolated rate limits."""
        storage = RedisStorage(redis_url="redis://localhost:6379")
        await storage.connect()

        try:
            # 2 req/sec with burst of 1 (allows 2 immediate requests)
            rate = Rate(permits=2, per="1s", burst=1)

            # Clear any existing keys
            if storage._client:
                await storage._client.delete("test:isolated:endpoint:user1")
                await storage._client.delete("test:isolated:endpoint:user2")

            # User 1 makes 2 requests (uses regular + burst)
            for i in range(2):
                allowed, _, _, _ = await storage.check_rate_limit(
                    key="test:isolated:endpoint:user1",
                    emission_interval_ms=rate.emission_interval_ms,
                    burst_capacity_ms=rate.burst_capacity_ms,
                    ttl_ms=rate.ttl_ms,
                )
                assert allowed, f"User1 request {i+1} should be allowed"

            # User 1's 3rd request should be denied
            allowed, _, _, _ = await storage.check_rate_limit(
                key="test:isolated:endpoint:user1",
                emission_interval_ms=rate.emission_interval_ms,
                burst_capacity_ms=rate.burst_capacity_ms,
                ttl_ms=rate.ttl_ms,
            )
            assert not allowed, "User1's 3rd request should be denied"

            # User 2 should still be able to make requests
            for i in range(2):
                allowed, _, _, _ = await storage.check_rate_limit(
                    key="test:isolated:endpoint:user2",
                    emission_interval_ms=rate.emission_interval_ms,
                    burst_capacity_ms=rate.burst_capacity_ms,
                    ttl_ms=rate.ttl_ms,
                )
                assert allowed, f"User2 request {i+1} should be allowed"

        finally:
            await storage.disconnect()

    @pytest.mark.asyncio
    async def test_rate_limit_recovery_after_idle(self):
        """Test that rate limit recovers after idle period."""
        storage = RedisStorage(redis_url="redis://localhost:6379")
        await storage.connect()

        try:
            # 5 req/sec with burst of 2
            rate = Rate(permits=5, per="1s", burst=2)
            key = "test:recovery:endpoint:user1"

            # Clear any existing key
            if storage._client:
                await storage._client.delete(key)

            # Use up all burst (3 requests)
            for i in range(3):
                allowed, _, _, _ = await storage.check_rate_limit(
                    key=key,
                    emission_interval_ms=rate.emission_interval_ms,
                    burst_capacity_ms=rate.burst_capacity_ms,
                    ttl_ms=rate.ttl_ms,
                )
                assert allowed, f"Request {i+1} should be allowed"

            # Next should be denied
            allowed, _, _, _ = await storage.check_rate_limit(
                key=key,
                emission_interval_ms=rate.emission_interval_ms,
                burst_capacity_ms=rate.burst_capacity_ms,
                ttl_ms=rate.ttl_ms,
            )
            assert not allowed, "Should be rate limited"

            # Wait for full recovery (1 second + burst time)
            await asyncio.sleep(1.5)

            # Should be able to burst again
            for i in range(3):
                allowed, _, _, _ = await storage.check_rate_limit(
                    key=key,
                    emission_interval_ms=rate.emission_interval_ms,
                    burst_capacity_ms=rate.burst_capacity_ms,
                    ttl_ms=rate.ttl_ms,
                )
                assert allowed, f"Request {i+1} after idle should be allowed"

        finally:
            await storage.disconnect()


class TestRedisTTL:
    """Test Redis TTL functionality."""

    @pytest.mark.asyncio
    async def test_key_expires_with_ttl(self):
        """Test that Redis keys expire according to TTL."""
        # Create storage and connect
        storage = RedisStorage(redis_url="redis://localhost:6379")
        await storage.connect()

        try:
            # Create a rate with short TTL (2 seconds)
            rate = Rate(permits=10, per="1s", burst=0)
            key = rate.key_for("test", "ttl", "endpoint", "test-user")

            # Make a request to set the key
            emission_interval_ms = rate.emission_interval_ms
            burst_capacity_ms = rate.burst_capacity_ms
            ttl_ms = 2000  # 2 seconds TTL

            allowed, _, _, _ = await storage.check_rate_limit(
                key=key,
                emission_interval_ms=emission_interval_ms,
                burst_capacity_ms=burst_capacity_ms,
                ttl_ms=ttl_ms,
            )
            assert allowed

            # Verify key exists
            client = storage._client
            assert client is not None
            exists = await client.exists(key)
            assert exists == 1

            # Check TTL is set correctly (should be close to 2000ms)
            ttl_remaining = await client.pttl(key)
            assert 1500 <= ttl_remaining <= 2000

            # Wait for key to expire
            await asyncio.sleep(2.5)

            # Verify key has expired
            exists = await client.exists(key)
            assert exists == 0

        finally:
            await storage.disconnect()

    @pytest.mark.asyncio
    async def test_ttl_refreshes_on_new_request(self):
        """Test that TTL is refreshed when a new request is made."""
        storage = RedisStorage(redis_url="redis://localhost:6379")
        await storage.connect()

        try:
            rate = Rate(permits=10, per="1s", burst=0)
            key = rate.key_for("test", "ttl", "refresh", "test-user")

            emission_interval_ms = rate.emission_interval_ms
            burst_capacity_ms = rate.burst_capacity_ms
            ttl_ms = 3000  # 3 seconds TTL

            # First request
            await storage.check_rate_limit(
                key=key,
                emission_interval_ms=emission_interval_ms,
                burst_capacity_ms=burst_capacity_ms,
                ttl_ms=ttl_ms,
            )

            # Wait 1.5 seconds
            await asyncio.sleep(1.5)

            # Check TTL before second request
            client = storage._client
            assert client is not None
            ttl_before = await client.pttl(key)
            assert 1000 <= ttl_before <= 1600

            # Second request should refresh TTL
            await storage.check_rate_limit(
                key=key,
                emission_interval_ms=emission_interval_ms,
                burst_capacity_ms=burst_capacity_ms,
                ttl_ms=ttl_ms,
            )

            # Check TTL after second request
            ttl_after = await client.pttl(key)
            assert 2500 <= ttl_after <= 3000
            assert ttl_after > ttl_before

        finally:
            await storage.disconnect()
