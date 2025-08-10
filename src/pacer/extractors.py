import ipaddress
import logging
from collections.abc import Callable

from starlette.requests import Request

logger = logging.getLogger(__name__)

Extractor = Callable[[Request], str]


def extract_ip(
    trusted_proxies: list[str] | None = None,
    real_ip_header: str = "X-Forwarded-For",
) -> Extractor:
    """
    Create an IP address extractor with proxy chain support.

    Args:
        trusted_proxies: List of trusted proxy IPs or CIDR ranges
        real_ip_header: Header to check for real IP (default: X-Forwarded-For)

    Returns:
        Extractor function that returns the client's IP address
    """
    # Parse trusted proxies into IP networks
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if trusted_proxies:
        for proxy in trusted_proxies:
            try:
                # Handle both single IPs and CIDR notation
                if '/' in proxy:
                    trusted_networks.append(ipaddress.ip_network(proxy))
                else:
                    # Single IP, convert to /32 or /128 network
                    ip = ipaddress.ip_address(proxy)
                    if isinstance(ip, ipaddress.IPv4Address):
                        trusted_networks.append(ipaddress.ip_network(f"{proxy}/32"))
                    else:
                        trusted_networks.append(ipaddress.ip_network(f"{proxy}/128"))
            except ValueError as e:
                logger.warning(f"Invalid trusted proxy: {proxy}: {e}")

    def _extract(request: Request) -> str:
        # Get the immediate client IP
        client_host = request.client.host if request.client else None

        if not client_host:
            # Fallback to localhost if no client info
            return "127.0.0.1"

        # Check if request is from a trusted proxy
        is_trusted = False
        if trusted_networks:
            try:
                client_ip = ipaddress.ip_address(client_host)
                is_trusted = any(client_ip in network for network in trusted_networks)
            except ValueError:
                # Invalid IP, treat as untrusted
                pass

        # If from trusted proxy, try to get real IP from headers
        if is_trusted:
            # Check X-Forwarded-For header
            forwarded_for = request.headers.get(real_ip_header)
            if forwarded_for:
                # X-Forwarded-For can contain multiple IPs: "client, proxy1, proxy2"
                # We want the leftmost public IP
                ips = [ip.strip() for ip in forwarded_for.split(',')]
                for ip in ips:
                    try:
                        parsed_ip = ipaddress.ip_address(ip)
                        # Return first public IP
                        if not parsed_ip.is_private and not parsed_ip.is_loopback:
                            return ip
                        # If all are private, return the first one
                        if ip == ips[0]:
                            return ip
                    except ValueError:
                        continue

            # Check Forwarded header (RFC 7239)
            forwarded = request.headers.get("Forwarded")
            if forwarded:
                # Parse Forwarded header: for=192.0.2.60;proto=http;by=203.0.113.43
                for param in forwarded.split(';'):
                    if param.strip().startswith('for='):
                        ip = param.split('=')[1].strip()
                        # Remove port if present
                        if ':' in ip and not ip.startswith('['):
                            ip = ip.split(':')[0]
                        # Remove brackets from IPv6
                        ip = ip.strip('[]')
                        return ip

        # Return direct client IP
        return client_host

    return _extract


def extract_api_key(header_name: str = "X-API-Key") -> Extractor:
    """
    Create an API key extractor from headers.

    Args:
        header_name: Name of the header containing the API key

    Returns:
        Extractor function that returns the API key or client IP as fallback
    """
    ip_extractor = extract_ip()

    def _extract(request: Request) -> str:
        api_key = request.headers.get(header_name)
        if api_key:
            return f"api_key:{api_key}"
        # Fallback to IP if no API key
        return ip_extractor(request)

    return _extract


def extract_user_id(
    user_id_func: Callable[[Request], str | None],
    fallback_to_ip: bool = True,
) -> Extractor:
    """
    Create a user ID extractor using a custom function.

    Args:
        user_id_func: Function to extract user ID from request
        fallback_to_ip: Whether to fall back to IP if no user ID

    Returns:
        Extractor function that returns the user ID
    """
    ip_extractor = extract_ip() if fallback_to_ip else None

    def _extract(request: Request) -> str:
        user_id = user_id_func(request)
        if user_id:
            return f"user:{user_id}"
        # Fallback to IP if configured
        if ip_extractor:
            return ip_extractor(request)
        # No fallback, use anonymous
        return "anonymous"

    return _extract


def extract_combined(*extractors: Extractor) -> Extractor:
    """
    Combine multiple extractors, using the first non-empty result.

    Args:
        *extractors: Variable number of extractor functions

    Returns:
        Extractor function that tries each extractor in order
    """
    def _extract(request: Request) -> str:
        for extractor in extractors:
            try:
                result = extractor(request)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Extractor failed: {e}")
                continue
        # Default fallback
        return "unknown"

    return _extract
