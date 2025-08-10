import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from pacer.extractors import Extractor, extract_ip
from pacer.policies import Rate
from pacer.storage import RedisStorage

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""
    allowed: bool
    retry_after_ms: int
    reset_ms: int
    remaining: int

    @property
    def retry_after_seconds(self) -> int:
        """Get retry_after in seconds (for Retry-After header)."""
        return max(1, self.retry_after_ms // 1000)

    @property
    def reset_timestamp(self) -> int:
        """Get reset time as Unix timestamp."""
        return int(time.time()) + (self.reset_ms // 1000)


@dataclass
class LimiterMetrics:
    """Metrics for rate limiter."""
    requests_allowed: int = 0
    requests_blocked: int = 0
    redis_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "requests_allowed": self.requests_allowed,
            "requests_blocked": self.requests_blocked,
            "redis_errors": self.redis_errors,
        }


class Limiter:
    """
    FastAPI rate limiter with GCRA algorithm.

    Args:
        redis_url: Redis connection URL
        default_policy: Default rate limit policy
        extractor: Function to extract identity from request
        fail_mode: "open" (allow on error) or "closed" (deny on error)
        app_name: Application name for key prefixing
        route_scope: Scope for route keys ("route", "method", "app")
        expose_headers: Whether to expose rate limit headers
        trust_proxies: List of trusted proxy IPs/CIDRs
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        default_policy: Rate | None = None,
        extractor: Extractor | None = None,
        fail_mode: str = "open",
        app_name: str = "fastapi",
        route_scope: str = "route",
        expose_headers: bool = True,
        trust_proxies: list[str] | None = None,
        connect_timeout_ms: int = 1000,
        command_timeout_ms: int = 100,
        cluster_mode: bool = False,
    ):
        # Validate inputs
        if fail_mode not in ("open", "closed"):
            raise ValueError("fail_mode must be 'open' or 'closed'")
        if route_scope not in ("route", "method", "app"):
            raise ValueError("route_scope must be 'route', 'method', or 'app'")

        self.default_policy = default_policy or Rate(permits=10, per="1s", burst=10)
        self.extractor = extractor or extract_ip(trusted_proxies=trust_proxies)
        self.fail_mode = fail_mode
        self.app_name = app_name
        self.route_scope = route_scope
        self.expose_headers = expose_headers

        # Initialize storage
        self.storage = RedisStorage(
            redis_url=redis_url,
            connect_timeout_ms=connect_timeout_ms,
            command_timeout_ms=command_timeout_ms,
            cluster_mode=cluster_mode,
        )

        # Metrics
        self.metrics = LimiterMetrics()

        # Connection state
        self._connected = False
        self._connection_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Initialize limiter on application startup."""
        async with self._connection_lock:
            if not self._connected:
                try:
                    await self.storage.connect()
                    self._connected = True
                    logger.info("Rate limiter initialized successfully")
                except Exception as e:
                    logger.error(f"Failed to initialize rate limiter: {e}")
                    if self.fail_mode == "closed":
                        raise

    async def shutdown(self) -> None:
        """Cleanup limiter on application shutdown."""
        async with self._connection_lock:
            if self._connected:
                await self.storage.disconnect()
                self._connected = False
                logger.info("Rate limiter shut down")

    async def check_rate_limit(
        self,
        request: Request,
        policy: Rate | None = None,
        scope_override: str | None = None,
    ) -> RateLimitResult:
        """
        Check if a request is within rate limits.

        Args:
            request: The incoming request
            policy: Rate limit policy (uses default if None)
            scope_override: Override the scope for this check

        Returns:
            RateLimitResult with decision and metadata
        """
        # Ensure we're connected
        if not self._connected:
            await self.startup()

        # Use provided policy or default
        policy = policy or self.default_policy

        # Extract principal (identity)
        principal = self.extractor(request)

        # Generate scope key
        scope = scope_override or self._get_scope(request)

        # Generate Redis key
        key = policy.key_for(self.app_name, self.route_scope, scope, principal)

        try:
            # Check rate limit
            allowed, retry_after_ms, reset_ms, remaining = await self.storage.check_rate_limit(
                key=key,
                emission_interval_ms=policy.emission_interval_ms,
                burst_capacity_ms=policy.burst_capacity_ms,
                ttl_ms=policy.ttl_ms,
            )

            # Update metrics
            if allowed:
                self.metrics.requests_allowed += 1
            else:
                self.metrics.requests_blocked += 1
                logger.info(f"Rate limit exceeded for {principal} on {scope}")

            return RateLimitResult(
                allowed=allowed,
                retry_after_ms=retry_after_ms,
                reset_ms=reset_ms,
                remaining=remaining,
            )

        except Exception as e:
            # Handle Redis errors
            logger.error(f"Rate limit check failed: {e}")
            self.metrics.redis_errors += 1

            # Apply fail mode
            if self.fail_mode == "open":
                # Allow request on error
                return RateLimitResult(
                    allowed=True,
                    retry_after_ms=0,
                    reset_ms=0,
                    remaining=policy.permits,
                )
            else:
                # Deny request on error
                return RateLimitResult(
                    allowed=False,
                    retry_after_ms=1000,  # Default 1 second
                    reset_ms=1000,
                    remaining=0,
                )

    def _get_scope(self, request: Request) -> str:
        """Generate scope identifier from request."""
        if self.route_scope == "app":
            return "global"
        elif self.route_scope == "method":
            return f"{request.method}:{request.url.path}"
        else:  # route
            return request.url.path

    def add_headers(
        self,
        response: Response,
        result: RateLimitResult,
        policy: Rate | None = None,
    ) -> None:
        """
        Add rate limit headers to response.

        Args:
            response: Response object to add headers to
            result: Rate limit check result
            policy: Rate policy used (for limit header)
        """
        if not self.expose_headers:
            return

        policy = policy or self.default_policy

        # Add standard headers
        response.headers["RateLimit-Limit"] = str(policy.permits)
        response.headers["RateLimit-Remaining"] = str(max(0, result.remaining))
        response.headers["RateLimit-Reset"] = str(result.reset_timestamp)

        # Add Retry-After header if rate limited
        if not result.allowed:
            response.headers["Retry-After"] = str(result.retry_after_seconds)

    async def is_healthy(self) -> bool:
        """Check if limiter is healthy."""
        if not self._connected:
            return False
        return await self.storage.is_healthy()

    def get_metrics(self) -> dict[str, Any]:
        """Get limiter metrics."""
        return {
            "limiter": self.metrics.to_dict(),
            "connected": self._connected,
        }
