"""Tests for Policy-based rate limiting."""

import pytest

from pacer.policies import Policy, Rate


class TestPolicy:
    """Test Policy class."""

    def test_single_rate_policy(self):
        """Test policy with single rate."""
        policy = Policy(
            rates=[Rate(100, "1m", burst=10)],
            key="ip",
            name="single_rate",
        )

        assert len(policy.rates) == 1
        assert policy.name == "single_rate"
        assert policy.key == "ip"
        assert policy.describe() == "Policy(single_rate): 100/1m (burst=10)"

    def test_multi_rate_policy(self):
        """Test policy with multiple rates."""
        policy = Policy(
            rates=[
                Rate(1000, "1h"),
                Rate(100, "1m", burst=20),
                Rate(10, "1s", burst=5),
            ],
            key="api_key",
            name="multi_rate",
        )

        assert len(policy.rates) == 3
        assert policy.describe() == "Policy(multi_rate): 1000/1h, 100/1m (burst=20), 10/1s (burst=5)"

    def test_policy_max_rates(self):
        """Test policy enforces maximum rates."""
        with pytest.raises(ValueError, match="Policy cannot have more than 3 rates"):
            Policy(
                rates=[
                    Rate(100, "1m"),
                    Rate(200, "2m"),
                    Rate(300, "3m"),
                    Rate(400, "4m"),  # 4th rate should fail
                ],
                key="ip",
                name="too_many",
            )

    def test_policy_key_generation(self):
        """Test Redis key generation."""
        policy = Policy(
            rates=[Rate(10, "1s"), Rate(100, "1m")],
            key="ip",
            name="test",
        )

        keys = policy.generate_keys("myapp", "route", "/api/test", "127.0.0.1")

        assert len(keys) == 2
        assert keys[0] == "myapp:route:{/api/test}:127.0.0.1:r0:10/1s"
        assert keys[1] == "myapp:route:{/api/test}:127.0.0.1:r1:100/1m"

    def test_policy_ttl_calculation(self):
        """Test TTL calculation for policy."""
        policy = Policy(
            rates=[
                Rate(10, "1s"),    # TTL = 2s
                Rate(100, "1m"),   # TTL = 120s
                Rate(1000, "1h"),  # TTL = 7200s
            ],
            key="ip",
            name="ttl_test",
        )

        assert policy.max_ttl_ms == 7200000  # 2 hours in ms

    def test_custom_selector(self):
        """Test policy with custom selector function."""
        def custom_selector(request):
            return "custom_id"

        policy = Policy(
            rates=[Rate(50, "10s")],
            key=custom_selector,
            name="custom",
        )

        assert callable(policy.key)
        assert policy.describe() == "Policy(custom): 50/10s"
