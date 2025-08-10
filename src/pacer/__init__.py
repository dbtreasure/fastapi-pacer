from pacer.dependencies import limit
from pacer.limiter import Limiter
from pacer.middleware import LimiterMiddleware
from pacer.policies import Rate
from pacer.storage import RedisStorage  # Backward compatibility
from pacer.storage_simple import SimpleRedisStorage  # Recommended

__version__ = "0.1.0"

__all__ = [
    "Limiter",
    "LimiterMiddleware",
    "limit",
    "Rate",
    "RedisStorage",  # Backward compatibility
    "SimpleRedisStorage",  # Recommended for new code
]
