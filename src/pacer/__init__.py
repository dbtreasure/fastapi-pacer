"""FastAPI Pacer - Production-ready rate limiting with GCRA."""

from pacer.dependencies import limit, rate_limit, set_limiter
from pacer.limiter import Limiter, LimiterMetrics, RateLimitResult
from pacer.middleware import LimiterMiddleware
from pacer.otel import OTelHooks, create_otel_hooks
from pacer.policies import Policy, Rate
from pacer.selectors import compose, key_api_key, key_ip, key_org, key_user
from pacer.storage import RedisStorage  # Backward compatibility
from pacer.storage_simple import SimpleRedisStorage  # Recommended

__version__ = "0.2.0"

__all__ = [
    # Core components
    "Limiter",
    "LimiterMiddleware",
    "LimiterMetrics",
    "RateLimitResult",
    # Policies and rates
    "Policy",
    "Rate",
    # Dependencies
    "limit",
    "rate_limit",
    "set_limiter",
    # Selectors
    "key_ip",
    "key_api_key",
    "key_user",
    "key_org",
    "compose",
    # Storage backends
    "SimpleRedisStorage",
    "RedisStorage",  # Backward compatibility
    # Observability
    "OTelHooks",
    "create_otel_hooks",
]
