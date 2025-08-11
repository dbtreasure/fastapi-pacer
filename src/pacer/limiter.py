"""Core rate limiter implementation."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pacer.policies import Policy, Rate
from pacer.selectors import get_selector
from pacer.storage_simple import SimpleRedisStorage

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

# Hook type definitions for observability
OnDecisionHook = Callable[["Request", Policy, "RateLimitResult", float], None]
OnErrorHook = Callable[["Request", Policy, Exception, float], None]


def _noop_on_decision(request: "Request", policy: Policy, result: "RateLimitResult", duration_ms: float) -> None:
    """Default no-op hook for rate limit decisions."""
    pass


def _noop_on_error(request: "Request", policy: Policy, error: Exception, duration_ms: float) -> None:
    """Default no-op hook for errors."""
    pass


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""
    allowed: bool
    retry_after_ms: int
    reset_ms: int
    remaining: int
    matched_rate_index: int = 0

    @property
    def retry_after_seconds(self) -> int:
        """Get retry_after in seconds (for Retry-After header)."""
        return max(1, self.retry_after_ms // 1000)

    @property
    def reset_timestamp(self) -> int:
        """Get reset time as Unix timestamp."""
        return int(time.time()) + (self.reset_ms // 1000)

    @property
    def reset_seconds(self) -> int:
        """Get reset time as delta-seconds from now (for RateLimit-Reset header)."""
        return max(1, self.reset_ms // 1000)


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
    FastAPI rate limiter with multi-rate GCRA algorithm.

    Args:
        redis_url: Redis connection URL
        default_policy: Default rate limit policy (optional)
        fail_mode: "open" (allow on error) or "closed" (deny on error)
        app_name: Application name for key prefixing
        route_scope: Scope for route keys ("route", "method", "app")
        expose_headers: Whether to expose rate limit headers
        connect_timeout_ms: Redis connection timeout in milliseconds
        command_timeout_ms: Redis command timeout in milliseconds
        legacy_timestamp_header: Whether to add X-RateLimit-Reset with Unix timestamp
        expose_policy_header: Whether to add X-RateLimit-Policy header for debugging
        on_decision: Hook called after rate limit decision
        on_error: Hook called on errors
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        default_policy: Policy | None = None,
        fail_mode: str = "open",
        app_name: str = "fastapi",
        route_scope: str = "route",
        expose_headers: bool = True,
        connect_timeout_ms: int = 1000,
        command_timeout_ms: int = 100,
        legacy_timestamp_header: bool = False,
        expose_policy_header: bool = False,
        on_decision: OnDecisionHook | None = None,
        on_error: OnErrorHook | None = None,
    ):
        # Validate inputs
        if fail_mode not in ("open", "closed"):
            raise ValueError("fail_mode must be 'open' or 'closed'")
        if route_scope not in ("route", "method", "app"):
            raise ValueError("route_scope must be 'route', 'method', or 'app'")

        self.default_policy = default_policy
        self.fail_mode = fail_mode
        self.app_name = app_name
        self.route_scope = route_scope
        self.expose_headers = expose_headers
        self.legacy_timestamp_header = legacy_timestamp_header
        self.expose_policy_header = expose_policy_header

        # Observability hooks (default to no-op)
        self.on_decision = on_decision or _noop_on_decision
        self.on_error = on_error or _noop_on_error

        # Initialize storage
        self.storage = SimpleRedisStorage(
            redis_url=redis_url,
            connect_timeout_ms=connect_timeout_ms,
            command_timeout_ms=command_timeout_ms,
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

    async def check_policy(
        self,
        request: "Request",
        policy: Policy | None = None,
        scope_override: str | None = None,
    ) -> RateLimitResult:
        """
        Check if a request is within rate limits using a policy.

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
        if policy is None:
            if self.default_policy is None:
                raise ValueError("No policy provided and no default policy configured")
            policy = self.default_policy

        # Get selector function for extracting identity
        selector = get_selector(policy.key)

        # Extract principal (identity)
        principal = selector(request)

        # Generate scope key
        scope = scope_override or self._get_scope(request)

        # Generate Redis keys for all rates in the policy
        keys = policy.generate_keys(self.app_name, self.route_scope, scope, principal)

        # Track timing for observability
        start_time = time.time()

        try:
            # Check policy with all rates
            allowed, retry_after_ms, reset_ms, remaining, matched_rate_index = await self.storage.check_policy(
                keys=keys,
                policy=policy,
            )

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Update metrics
            if allowed:
                self.metrics.requests_allowed += 1
            else:
                self.metrics.requests_blocked += 1
                logger.info(f"Rate limit exceeded for {principal} on {scope} (policy: {policy.name})")

            result = RateLimitResult(
                allowed=allowed,
                retry_after_ms=retry_after_ms,
                reset_ms=reset_ms,
                remaining=remaining,
                matched_rate_index=matched_rate_index,
            )

            # Call decision hook for observability
            try:
                self.on_decision(request, policy, result, duration_ms)
            except Exception as hook_error:
                logger.warning(f"Decision hook failed: {hook_error}")

            return result

        except Exception as e:
            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Handle Redis errors
            logger.error(f"Rate limit check failed: {e}")
            self.metrics.redis_errors += 1

            # Call error hook for observability
            try:
                self.on_error(request, policy, e, duration_ms)
            except Exception as hook_error:
                logger.warning(f"Error hook failed: {hook_error}")

            # Apply fail mode
            if self.fail_mode == "open":
                # Allow request on error
                # Use the most permissive rate for remaining
                max_permits = max(rate.permits for rate in policy.rates)
                return RateLimitResult(
                    allowed=True,
                    retry_after_ms=0,
                    reset_ms=0,
                    remaining=max_permits,
                    matched_rate_index=0,
                )
            else:
                # Deny request on error
                return RateLimitResult(
                    allowed=False,
                    retry_after_ms=1000,  # Default 1 second
                    reset_ms=1000,
                    remaining=0,
                    matched_rate_index=0,
                )

    def _get_scope(self, request: "Request") -> str:
        """Generate scope identifier from request."""
        if self.route_scope == "app":
            return "global"
        elif self.route_scope == "method":
            return f"{request.method}:{request.url.path}"
        else:  # route
            return request.url.path

    def add_headers(
        self,
        response: "Response",
        result: RateLimitResult,
        policy: Policy | None = None,
    ) -> None:
        """
        Add rate limit headers to response.

        Args:
            response: Response object to add headers to
            result: Rate limit check result
            policy: Policy used (for limit header)
        """
        if not self.expose_headers:
            return

        if policy is None:
            if self.default_policy is None:
                return
            policy = self.default_policy

        # Get the matched rate for headers
        matched_rate = policy.rates[result.matched_rate_index]

        # Add standard headers using the matched rate
        response.headers["RateLimit-Limit"] = str(matched_rate.permits)
        response.headers["RateLimit-Remaining"] = str(max(0, result.remaining))

        # RateLimit-Reset: Use delta-seconds (spec compliant)
        response.headers["RateLimit-Reset"] = str(result.reset_seconds)

        # Optional: Add X-RateLimit-Reset with Unix timestamp for compatibility
        if self.legacy_timestamp_header:
            response.headers["X-RateLimit-Reset"] = str(result.reset_timestamp)

        # Optional: Add X-RateLimit-Policy header for debugging
        if self.expose_policy_header:
            response.headers["X-RateLimit-Policy"] = policy.describe()

        # Add Retry-After header if rate limited
        if not result.allowed:
            response.headers["Retry-After"] = str(result.retry_after_seconds)

    async def is_healthy(self) -> bool:
        """Check if limiter is healthy."""
        if not self._connected:
            return False
        try:
            # Try a simple ping
            if self.storage.redis:
                await self.storage.redis.ping()
                return True
            return False
        except Exception:
            return False

    def get_metrics(self) -> dict[str, Any]:
        """Get limiter metrics."""
        return {
            "limiter": self.metrics.to_dict(),
            "connected": self._connected,
        }

    # Backward compatibility: check_rate_limit wraps check_policy
    async def check_rate_limit(
        self,
        request: "Request",
        policy: Rate | Policy | None = None,
        scope_override: str | None = None,
    ) -> RateLimitResult:
        """
        Check if a request is within rate limits.
        
        This method maintains backward compatibility with Rate objects
        while supporting the new Policy API.

        Args:
            request: The incoming request
            policy: Rate limit policy (Rate or Policy object)
            scope_override: Override the scope for this check

        Returns:
            RateLimitResult with decision and metadata
        """
        # Convert Rate to Policy if needed
        if isinstance(policy, Rate):
            policy = Policy(rates=[policy], key="ip", name="legacy")

        return await self.check_policy(request, policy, scope_override)
