"""Selector functions for extracting identity from requests."""

import hashlib
import ipaddress
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


def key_ip(request: "Request") -> str:
    """
    Extract client IP address from request.

    Handles proxy headers in order of preference:
    1. CF-Connecting-IP (Cloudflare)
    2. X-Real-IP (common proxy header)
    3. X-Forwarded-For (standard proxy header, uses first IP)
    4. Falls back to request.client.host
    """
    # Try Cloudflare header first (most reliable when present)
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return _normalize_ip(cf_ip)

    # Try X-Real-IP (commonly set by nginx and others)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return _normalize_ip(real_ip)

    # Try X-Forwarded-For (standard proxy header)
    # Format: "client, proxy1, proxy2, ..."
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # Take the first IP (original client)
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return _normalize_ip(first_ip)

    # Fall back to direct connection
    if request.client and request.client.host:
        return _normalize_ip(request.client.host)

    # Should never happen in practice
    logger.warning("Could not extract IP from request, using fallback")
    return "unknown"


def key_api_key(request: "Request") -> str:
    """
    Extract API key from request headers.

    Looks for API key in order:
    1. X-API-Key header (standard)
    2. Authorization: Bearer <token> header
    3. api_key query parameter (less secure, but common)
    """
    # Try X-API-Key header
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return _hash_key(api_key)

    # Try Authorization Bearer token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            return _hash_key(token)

    # Try query parameter (less secure)
    api_key_param = request.query_params.get("api_key")
    if api_key_param:
        return _hash_key(api_key_param)

    # No API key found
    logger.debug("No API key found in request")
    return "no_api_key"


def key_user(request: "Request") -> str:
    """
    Extract user ID from request.

    Expects user ID to be set by authentication middleware as:
    - request.state.user_id
    - request.state.user.id
    - request.user.id (for some frameworks)
    """
    # Try request.state.user_id (common pattern)
    if hasattr(request.state, "user_id"):
        user_id = request.state.user_id
        if user_id:
            return str(user_id)

    # Try request.state.user.id (object pattern)
    if hasattr(request.state, "user"):
        user = request.state.user
        if user and hasattr(user, "id"):
            return str(user.id)

    # Try request.user.id (some frameworks)
    if hasattr(request, "user"):
        user = request.user
        if user and hasattr(user, "id"):
            return str(user.id)

    # No user found
    logger.debug("No user ID found in request")
    return "anonymous"


def key_org(request: "Request") -> str:
    """
    Extract organization ID from request.

    Expects org ID to be set by authentication middleware as:
    - request.state.org_id
    - request.state.organization_id
    - request.state.org.id
    """
    # Try request.state.org_id
    if hasattr(request.state, "org_id"):
        org_id = request.state.org_id
        if org_id:
            return str(org_id)

    # Try request.state.organization_id
    if hasattr(request.state, "organization_id"):
        org_id = request.state.organization_id
        if org_id:
            return str(org_id)

    # Try request.state.org.id (object pattern)
    if hasattr(request.state, "org"):
        org = request.state.org
        if org and hasattr(org, "id"):
            return str(org.id)

    # No org found
    logger.debug("No organization ID found in request")
    return "no_org"


def compose(*selectors: Callable[["Request"], str]) -> Callable[["Request"], str]:
    """
    Compose multiple selectors into a single selector.

    The composed selector concatenates results with ':' separator.
    This allows rate limiting by multiple dimensions.

    Example:
        # Rate limit by both user and IP
        policy = Policy(
            rates=[Rate(100, "1m")],
            key=compose(key_user, key_ip)
        )
    """
    def composed_selector(request: "Request") -> str:
        parts = []
        for selector in selectors:
            try:
                part = selector(request)
                if part:
                    parts.append(part)
            except Exception as e:
                logger.warning(f"Selector {selector.__name__} failed: {e}")
                parts.append("error")

        return ":".join(parts) if parts else "unknown"

    # Set a useful name for debugging
    selector_names = [s.__name__ for s in selectors]
    composed_selector.__name__ = f"compose({','.join(selector_names)})"

    return composed_selector


def _normalize_ip(ip_str: str) -> str:
    """
    Normalize IP address to standard format.

    - Removes IPv6 zone IDs
    - Validates IP format
    - Returns consistent representation
    """
    try:
        # Remove port if present (e.g., "1.2.3.4:5678")
        if ":" in ip_str and not ip_str.startswith("["):
            # IPv4 with port
            ip_str = ip_str.rsplit(":", 1)[0]
        elif ip_str.startswith("[") and "]" in ip_str:
            # IPv6 with port like "[::1]:8000"
            ip_str = ip_str[1:ip_str.index("]")]

        # Parse and normalize
        ip = ipaddress.ip_address(ip_str)
        return str(ip)
    except ValueError:
        logger.warning(f"Invalid IP address: {ip_str}")
        return ip_str  # Return as-is if invalid


def _hash_key(key: str) -> str:
    """
    Hash sensitive keys for storage.

    Uses SHA-256 truncated to 16 bytes (32 hex chars).
    This provides good distribution while keeping keys short.
    """
    if not key:
        return "empty"

    # Use SHA-256 and truncate to 16 bytes
    hash_obj = hashlib.sha256(key.encode("utf-8"))
    return hash_obj.hexdigest()[:32]


# Built-in selector registry for string shortcuts
BUILTIN_SELECTORS = {
    "ip": key_ip,
    "api_key": key_api_key,
    "user": key_user,
    "org": key_org,
}


def get_selector(key: str | Callable[["Request"], str]) -> Callable[["Request"], str]:
    """
    Get a selector function from string name or callable.

    Args:
        key: Either a string name ("ip", "api_key", "user", "org") or a callable

    Returns:
        Selector function
    """
    if callable(key):
        return key

    if isinstance(key, str):
        selector = BUILTIN_SELECTORS.get(key)
        if selector:
            return selector
        raise ValueError(f"Unknown selector: {key}. Available: {list(BUILTIN_SELECTORS.keys())}")

    raise TypeError(f"Selector must be string or callable, got {type(key)}")
