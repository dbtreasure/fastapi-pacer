"""Tests for multi-rate limiting functionality."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pacer import Limiter, Policy, Rate
from pacer.storage_simple import SimpleRedisStorage


@pytest.mark.asyncio
class TestMultiRate:
    """Test multi-rate limiting."""

    async def test_single_rate_policy(self):
        """Test policy with single rate."""
        policy = Policy(
            rates=[Rate(10, "1s", burst=5)],
            key="ip",
            name="single",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        # Mock storage
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (True, 0, 1000, 9, 0)
            
            request = MagicMock()
            request.client.host = "127.0.0.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"
            
            result = await limiter.check_policy(request, policy)
            
            assert result.allowed is True
            assert result.remaining == 9
            assert result.matched_rate_index == 0
            
            # Verify correct keys were generated
            mock_check.assert_called_once()
            keys = mock_check.call_args[1]['keys']
            assert len(keys) == 1
            assert "r0:10/1s" in keys[0]

    async def test_multi_rate_policy_all_pass(self):
        """Test multi-rate policy where all rates pass."""
        policy = Policy(
            rates=[
                Rate(100, "1m"),
                Rate(10, "1s"),
                Rate(1000, "1h"),
            ],
            key="ip",
            name="multi",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            # All rates pass, second rate (10/1s) is most restrictive
            mock_check.return_value = (True, 0, 1000, 5, 1)
            
            request = MagicMock()
            request.client.host = "127.0.0.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"
            
            result = await limiter.check_policy(request, policy)
            
            assert result.allowed is True
            assert result.remaining == 5
            assert result.matched_rate_index == 1  # Second rate
            
            # Verify 3 keys were generated
            keys = mock_check.call_args[1]['keys']
            assert len(keys) == 3
            assert "r0:100/1m" in keys[0]
            assert "r1:10/1s" in keys[1]
            assert "r2:1000/1h" in keys[2]

    async def test_multi_rate_policy_one_denies(self):
        """Test multi-rate policy where one rate denies."""
        policy = Policy(
            rates=[
                Rate(100, "1m"),
                Rate(10, "1s"),  # This one will deny
                Rate(1000, "1h"),
            ],
            key="ip",
            name="multi",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            # Second rate denies
            mock_check.return_value = (False, 500, 1000, 0, 1)
            
            request = MagicMock()
            request.client.host = "127.0.0.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"
            
            result = await limiter.check_policy(request, policy)
            
            assert result.allowed is False
            assert result.retry_after_ms == 500
            assert result.remaining == 0
            assert result.matched_rate_index == 1  # Second rate denied

    async def test_multi_rate_different_selectors(self):
        """Test policies with different selectors."""
        ip_policy = Policy(
            rates=[Rate(100, "1m")],
            key="ip",
            name="by_ip",
        )
        
        api_key_policy = Policy(
            rates=[Rate(1000, "1m")],
            key="api_key", 
            name="by_api_key",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (True, 0, 1000, 99, 0)
            
            request = MagicMock()
            request.client.host = "192.168.1.1"
            request.headers = {"X-API-Key": "test-key"}
            request.url.path = "/test"
            request.method = "GET"
            request.query_params = {}
            
            # Check IP-based policy
            result = await limiter.check_policy(request, ip_policy)
            keys = mock_check.call_args[1]['keys']
            assert "192.168.1.1" in keys[0]
            
            # Check API key-based policy
            result = await limiter.check_policy(request, api_key_policy)
            keys = mock_check.call_args[1]['keys']
            # API key should be hashed
            assert "192.168.1.1" not in keys[0]
            assert "test-key" not in keys[0]

    async def test_composed_selector(self):
        """Test policy with composed selector."""
        from pacer.selectors import compose, key_ip, key_user
        
        policy = Policy(
            rates=[Rate(50, "1m")],
            key=compose(key_user, key_ip),
            name="user_ip",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (True, 0, 1000, 49, 0)
            
            request = MagicMock()
            request.client.host = "192.168.1.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"
            request.state = MagicMock()
            request.state.user_id = "user123"
            
            result = await limiter.check_policy(request, policy)
            
            # Check that key contains both user and IP
            keys = mock_check.call_args[1]['keys']
            assert "user123:192.168.1.1" in keys[0]

    async def test_custom_selector_function(self):
        """Test policy with custom selector function."""
        def tenant_selector(request):
            return request.headers.get("X-Tenant-ID", "default")
        
        policy = Policy(
            rates=[Rate(200, "1m")],
            key=tenant_selector,
            name="by_tenant",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379")
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (True, 0, 1000, 199, 0)
            
            request = MagicMock()
            request.headers = {"X-Tenant-ID": "tenant456"}
            request.url.path = "/test"
            request.method = "GET"
            
            result = await limiter.check_policy(request, policy)
            
            # Check that tenant ID is in the key
            keys = mock_check.call_args[1]['keys']
            assert "tenant456" in keys[0]

    async def test_headers_with_multi_rate(self):
        """Test that headers reflect the most restrictive rate."""
        policy = Policy(
            rates=[
                Rate(1000, "1h"),  # 1000/hour
                Rate(100, "1m"),   # 100/minute (most restrictive)
                Rate(50, "30s"),   # 50/30sec
            ],
            key="ip",
            name="multi",
        )
        
        limiter = Limiter(redis_url="redis://localhost:6379", expose_headers=True)
        
        with patch.object(limiter.storage, 'check_policy', new_callable=AsyncMock) as mock_check:
            # Second rate (100/1m) is most restrictive with 10 remaining
            mock_check.return_value = (True, 0, 60000, 10, 1)
            
            request = MagicMock()
            request.client.host = "127.0.0.1"
            request.headers = {}
            request.url.path = "/test"
            request.method = "GET"
            
            result = await limiter.check_policy(request, policy)
            
            # Create mock response to add headers
            response = MagicMock()
            response.headers = {}
            
            limiter.add_headers(response, result, policy)
            
            # Headers should reflect the matched rate (100/1m)
            assert response.headers["RateLimit-Limit"] == "100"
            assert response.headers["RateLimit-Remaining"] == "10"