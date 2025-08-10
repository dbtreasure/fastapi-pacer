from unittest.mock import Mock

from starlette.requests import Request

from pacer.extractors import (
    extract_api_key,
    extract_combined,
    extract_ip,
    extract_user_id,
)


class TestExtractIP:
    """Test IP extraction with proxy support."""

    def test_direct_client_ip(self):
        """Test extracting IP from direct client connection."""
        extractor = extract_ip()

        request = Mock(spec=Request)
        request.client = Mock(host="192.168.1.100")
        request.headers = {}

        assert extractor(request) == "192.168.1.100"

    def test_trusted_proxy_with_x_forwarded_for(self):
        """Test extracting IP from X-Forwarded-For with trusted proxy."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")  # Trusted proxy
        request.headers = {"X-Forwarded-For": "203.0.113.42, 10.0.0.1"}

        assert extractor(request) == "203.0.113.42"

    def test_untrusted_proxy_ignores_headers(self):
        """Test that untrusted proxy headers are ignored."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="192.168.1.100")  # Not in trusted list
        request.headers = {"X-Forwarded-For": "203.0.113.42"}

        # Should return direct client IP, ignoring header
        assert extractor(request) == "192.168.1.100"

    def test_trusted_proxy_cidr_range(self):
        """Test trusted proxy with CIDR notation."""
        extractor = extract_ip(trusted_proxies=["10.0.0.0/8"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.5.1.1")  # Within trusted range
        request.headers = {"X-Forwarded-For": "203.0.113.42"}

        assert extractor(request) == "203.0.113.42"

    def test_multiple_ips_in_x_forwarded_for(self):
        """Test handling multiple IPs in X-Forwarded-For."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")
        request.headers = {"X-Forwarded-For": "203.0.113.42, 198.51.100.10, 10.0.0.1"}

        # Should return the leftmost public IP
        assert extractor(request) == "203.0.113.42"

    def test_private_ips_in_x_forwarded_for(self):
        """Test handling private IPs in X-Forwarded-For."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")
        request.headers = {"X-Forwarded-For": "192.168.1.100, 10.0.0.2, 10.0.0.1"}

        # All are private, should return the first one
        assert extractor(request) == "192.168.1.100"

    def test_forwarded_header_rfc7239(self):
        """Test parsing RFC 7239 Forwarded header."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")
        request.headers = {"Forwarded": "for=198.51.100.17;proto=http;by=10.0.0.1"}

        assert extractor(request) == "198.51.100.17"

    def test_forwarded_header_with_port(self):
        """Test Forwarded header with port number."""
        extractor = extract_ip(trusted_proxies=["10.0.0.1"])

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")
        request.headers = {"Forwarded": "for=198.51.100.17:4711;proto=http"}

        assert extractor(request) == "198.51.100.17"

    def test_ipv6_address(self):
        """Test handling IPv6 addresses."""
        extractor = extract_ip(trusted_proxies=["::1"])

        request = Mock(spec=Request)
        request.client = Mock(host="::1")
        request.headers = {"X-Forwarded-For": "2001:db8::1"}

        assert extractor(request) == "2001:db8::1"

    def test_no_client_info(self):
        """Test fallback when no client info available."""
        extractor = extract_ip()

        request = Mock(spec=Request)
        request.client = None
        request.headers = {}

        assert extractor(request) == "127.0.0.1"

    def test_custom_header_name(self):
        """Test using custom header for real IP."""
        extractor = extract_ip(
            trusted_proxies=["10.0.0.1"],
            real_ip_header="X-Real-IP"
        )

        request = Mock(spec=Request)
        request.client = Mock(host="10.0.0.1")
        request.headers = {"X-Real-IP": "203.0.113.42"}

        assert extractor(request) == "203.0.113.42"


class TestExtractAPIKey:
    """Test API key extraction."""

    def test_api_key_from_header(self):
        """Test extracting API key from header."""
        extractor = extract_api_key()

        request = Mock(spec=Request)
        request.headers = {"X-API-Key": "secret-key-123"}

        assert extractor(request) == "api_key:secret-key-123"

    def test_custom_header_name(self):
        """Test using custom header name."""
        extractor = extract_api_key(header_name="Authorization")

        request = Mock(spec=Request)
        request.headers = {"Authorization": "Bearer token123"}

        assert extractor(request) == "api_key:Bearer token123"

    def test_fallback_to_ip(self):
        """Test fallback to IP when no API key present."""
        extractor = extract_api_key()

        request = Mock(spec=Request)
        request.client = Mock(host="192.168.1.100")
        request.headers = {}  # No API key

        assert extractor(request) == "192.168.1.100"


class TestExtractUserID:
    """Test user ID extraction."""

    def test_user_id_extraction(self):
        """Test extracting user ID with custom function."""
        def get_user_id(request):
            return request.state.user_id if hasattr(request.state, 'user_id') else None

        extractor = extract_user_id(get_user_id)

        request = Mock(spec=Request)
        request.state = Mock(user_id="user123")

        assert extractor(request) == "user:user123"

    def test_no_user_id_fallback_to_ip(self):
        """Test fallback to IP when no user ID."""
        def get_user_id(request):
            return None

        extractor = extract_user_id(get_user_id, fallback_to_ip=True)

        request = Mock(spec=Request)
        request.client = Mock(host="192.168.1.100")
        request.headers = {}

        assert extractor(request) == "192.168.1.100"

    def test_no_user_id_no_fallback(self):
        """Test anonymous when no user ID and no fallback."""
        def get_user_id(request):
            return None

        extractor = extract_user_id(get_user_id, fallback_to_ip=False)

        request = Mock(spec=Request)

        assert extractor(request) == "anonymous"


class TestExtractCombined:
    """Test combined extractors."""

    def test_first_successful_extractor(self):
        """Test that first successful extractor is used."""
        def extractor1(request):
            return ""  # Empty result

        def extractor2(request):
            return "result2"

        def extractor3(request):
            return "result3"

        combined = extract_combined(extractor1, extractor2, extractor3)

        request = Mock(spec=Request)
        assert combined(request) == "result2"

    def test_all_extractors_fail(self):
        """Test fallback when all extractors fail."""
        def failing_extractor(request):
            raise ValueError("Extraction failed")

        combined = extract_combined(failing_extractor, failing_extractor)

        request = Mock(spec=Request)
        assert combined(request) == "unknown"

    def test_mixed_success_and_failure(self):
        """Test handling mix of successful and failing extractors."""
        def failing_extractor(request):
            raise ValueError("Failed")

        def successful_extractor(request):
            return "success"

        combined = extract_combined(failing_extractor, successful_extractor)

        request = Mock(spec=Request)
        assert combined(request) == "success"
