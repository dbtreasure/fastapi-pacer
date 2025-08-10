import pytest

from pacer.policies import Rate


class TestRate:
    """Test Rate policy class."""

    def test_valid_rate_creation(self):
        """Test creating valid rate policies."""
        rate = Rate(permits=10, per="1s")
        assert rate.permits == 10
        assert rate.per == "1s"
        assert rate.burst == 0

        rate_with_burst = Rate(permits=100, per="1m", burst=50)
        assert rate_with_burst.permits == 100
        assert rate_with_burst.per == "1m"
        assert rate_with_burst.burst == 50

    def test_duration_parsing(self):
        """Test parsing different duration formats."""
        test_cases = [
            ("1s", 1000),
            ("10s", 10000),
            ("1m", 60000),
            ("5m", 300000),
            ("1h", 3600000),
            ("2h", 7200000),
            ("1d", 86400000),
            ("0.5s", 500),
            ("1.5m", 90000),
        ]

        for duration, expected_ms in test_cases:
            rate = Rate(permits=1, per=duration)
            assert rate.period_ms == expected_ms

    def test_invalid_duration_format(self):
        """Test invalid duration formats."""
        invalid_durations = [
            "1",  # No unit
            "s",  # No value
            "1x",  # Invalid unit
            "1ms",  # Unsupported unit
            "-1s",  # Negative value
            "1s1m",  # Multiple units
        ]

        for duration in invalid_durations:
            with pytest.raises(ValueError, match="Invalid duration"):
                Rate(permits=1, per=duration)

    def test_invalid_permits(self):
        """Test invalid permit values."""
        with pytest.raises(ValueError, match="permits must be positive"):
            Rate(permits=0, per="1s")

        with pytest.raises(ValueError, match="permits must be positive"):
            Rate(permits=-1, per="1s")

    def test_invalid_burst(self):
        """Test invalid burst values."""
        with pytest.raises(ValueError, match="burst must be non-negative"):
            Rate(permits=10, per="1s", burst=-1)

    def test_emission_interval_calculation(self):
        """Test emission interval calculation."""
        rate = Rate(permits=10, per="1s")
        assert rate.emission_interval_ms == 100  # 1000ms / 10 = 100ms

        rate = Rate(permits=60, per="1m")
        assert rate.emission_interval_ms == 1000  # 60000ms / 60 = 1000ms

        rate = Rate(permits=100, per="10s")
        assert rate.emission_interval_ms == 100  # 10000ms / 100 = 100ms

    def test_burst_capacity_calculation(self):
        """Test burst capacity calculation."""
        rate = Rate(permits=10, per="1s", burst=5)
        assert rate.burst_capacity_ms == 500  # 5 * 100ms = 500ms

        rate = Rate(permits=60, per="1m", burst=10)
        assert rate.burst_capacity_ms == 10000  # 10 * 1000ms = 10000ms

        rate_no_burst = Rate(permits=10, per="1s", burst=0)
        assert rate_no_burst.burst_capacity_ms == 0

    def test_ttl_calculation(self):
        """Test TTL calculation for Redis keys."""
        rate = Rate(permits=10, per="1s", burst=5)
        # TTL should be at least period + burst_capacity
        assert rate.ttl_ms >= 1000 + 500

        rate_no_burst = Rate(permits=10, per="1s", burst=0)
        # TTL should be at least 2 * period when no burst
        assert rate_no_burst.ttl_ms >= 2000

    def test_key_generation(self):
        """Test Redis key generation."""
        rate = Rate(permits=10, per="1s")

        key = rate.key_for("myapp", "route", "/api/users", "192.168.1.1")
        assert key == "myapp:route:{/api/users}:192.168.1.1"

        # Test with different components
        key = rate.key_for("app2", "method", "GET:/api/items", "user:123")
        assert key == "app2:method:{GET:/api/items}:user:123"

        # Verify hash tag is present for Redis cluster
        assert "{" in key and "}" in key

    def test_rate_immutability(self):
        """Test that Rate objects are immutable."""
        rate = Rate(permits=10, per="1s", burst=5)

        # Should not be able to modify attributes
        with pytest.raises(AttributeError):
            rate.permits = 20  # type: ignore[misc]

        with pytest.raises(AttributeError):
            rate.per = "2s"  # type: ignore[misc]

        with pytest.raises(AttributeError):
            rate.burst = 10  # type: ignore[misc]
