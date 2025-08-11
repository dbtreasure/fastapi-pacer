"""Tests for selector functions."""

import pytest
from unittest.mock import MagicMock

from pacer.selectors import (
    BUILTIN_SELECTORS,
    compose,
    get_selector,
    key_api_key,
    key_ip,
    key_org,
    key_user,
)


class TestSelectors:
    """Test selector functions."""

    def test_key_ip_direct(self):
        """Test IP extraction from direct connection."""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"
        
        assert key_ip(request) == "192.168.1.100"

    def test_key_ip_cloudflare(self):
        """Test IP extraction with Cloudflare header."""
        request = MagicMock()
        request.headers = {"CF-Connecting-IP": "203.0.113.1"}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        
        assert key_ip(request) == "203.0.113.1"

    def test_key_ip_x_real_ip(self):
        """Test IP extraction with X-Real-IP header."""
        request = MagicMock()
        request.headers = {"X-Real-IP": "198.51.100.1"}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        
        assert key_ip(request) == "198.51.100.1"

    def test_key_ip_x_forwarded_for(self):
        """Test IP extraction with X-Forwarded-For header."""
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 10.0.0.2"}
        request.client = MagicMock()
        request.client.host = "10.0.0.3"
        
        assert key_ip(request) == "203.0.113.5"

    def test_key_api_key_header(self):
        """Test API key extraction from X-API-Key header."""
        request = MagicMock()
        request.headers = {"X-API-Key": "test-api-key-123"}
        request.query_params = {}
        
        result = key_api_key(request)
        assert len(result) == 32  # SHA-256 truncated to 32 chars
        assert result != "test-api-key-123"  # Should be hashed

    def test_key_api_key_bearer(self):
        """Test API key extraction from Authorization Bearer."""
        request = MagicMock()
        request.headers = {"Authorization": "Bearer secret-token-456"}
        request.query_params = {}
        
        result = key_api_key(request)
        assert len(result) == 32
        assert result != "secret-token-456"

    def test_key_api_key_query_param(self):
        """Test API key extraction from query parameter."""
        request = MagicMock()
        request.headers = {}
        request.query_params = {"api_key": "query-key-789"}
        
        result = key_api_key(request)
        assert len(result) == 32
        assert result != "query-key-789"

    def test_key_api_key_not_found(self):
        """Test API key when not present."""
        request = MagicMock()
        request.headers = {}
        request.query_params = {}
        
        assert key_api_key(request) == "no_api_key"

    def test_key_user(self):
        """Test user ID extraction."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user_id = "user123"
        
        assert key_user(request) == "user123"

    def test_key_user_object(self):
        """Test user ID extraction from user object."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = MagicMock()
        request.state.user.id = 456
        # Need to not have user_id attribute for this test
        del request.state.user_id
        
        assert key_user(request) == "456"

    def test_key_user_anonymous(self):
        """Test user ID when not authenticated."""
        request = MagicMock()
        # Create a state object without user_id or user attributes
        state = MagicMock(spec=[])  # Empty spec means no attributes
        request.state = state
        request.user = None
        
        assert key_user(request) == "anonymous"

    def test_key_org(self):
        """Test organization ID extraction."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.org_id = "org789"
        
        assert key_org(request) == "org789"

    def test_compose_selectors(self):
        """Test composing multiple selectors."""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        request.state = MagicMock()
        request.state.user_id = "user123"
        
        composed = compose(key_user, key_ip)
        result = composed(request)
        
        assert result == "user123:192.168.1.1"

    def test_compose_with_errors(self):
        """Test compose handles selector errors gracefully."""
        def failing_selector(request):
            raise ValueError("Test error")
        
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        
        composed = compose(failing_selector, key_ip)
        result = composed(request)
        
        assert result == "error:192.168.1.1"

    def test_get_selector_string(self):
        """Test getting selector by string name."""
        selector = get_selector("ip")
        assert selector == key_ip
        
        selector = get_selector("api_key")
        assert selector == key_api_key
        
        selector = get_selector("user")
        assert selector == key_user
        
        selector = get_selector("org")
        assert selector == key_org

    def test_get_selector_callable(self):
        """Test getting selector with callable."""
        def custom(request):
            return "custom"
        
        selector = get_selector(custom)
        assert selector == custom

    def test_get_selector_invalid(self):
        """Test get_selector with invalid input."""
        with pytest.raises(ValueError, match="Unknown selector"):
            get_selector("invalid")
        
        with pytest.raises(TypeError, match="must be string or callable"):
            get_selector(123)