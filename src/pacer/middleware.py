import logging
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pacer.limiter import Limiter
from pacer.policies import Rate

logger = logging.getLogger(__name__)


class LimiterMiddleware:
    """
    Pure ASGI middleware for global rate limiting.
    
    This implementation avoids BaseHTTPMiddleware overhead by working
    directly with ASGI, providing better performance.

    Args:
        app: ASGI application
        limiter: Configured Limiter instance
        policy: Optional rate policy (uses limiter's default if None)
        exclude_paths: List of paths to exclude from rate limiting
        exclude_methods: List of HTTP methods to exclude
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: Limiter,
        policy: Rate | None = None,
        exclude_paths: list[str] | None = None,
        exclude_methods: list[str] | None = None,
    ):
        self.app = app
        self.limiter = limiter
        self.policy = policy
        self.exclude_paths = set(exclude_paths or [])
        self.exclude_methods = set(exclude_methods or ["OPTIONS", "HEAD"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI application entrypoint."""
        # Only handle HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Create request object for compatibility
        request = Request(scope, receive)
        
        # Check if path or method is excluded
        if self._is_excluded(request):
            await self.app(scope, receive, send)
            return

        try:
            # Check rate limit
            result = await self.limiter.check_rate_limit(
                request=request,
                policy=self.policy,
            )

            if not result.allowed:
                # Rate limit exceeded - return 429
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": "rate_limited",
                        "retry_after_ms": result.retry_after_ms,
                    },
                )
                
                # Add rate limit headers
                self.limiter.add_headers(response, result, self.policy)
                
                # Send response directly
                await response(scope, receive, send)
                return

            # Request allowed - proceed with wrapped send to add headers
            async def send_wrapper(message: Message) -> None:
                # Intercept response start to add headers
                if message["type"] == "http.response.start":
                    # Create mutable headers
                    headers = MutableHeaders(scope=message)
                    
                    # Add rate limit headers
                    if self.limiter.expose_headers:
                        policy = self.policy or self.limiter.default_policy
                        headers["RateLimit-Limit"] = str(policy.permits)
                        headers["RateLimit-Remaining"] = str(max(0, result.remaining))
                        headers["RateLimit-Reset"] = str(result.reset_seconds)
                        
                        # Optional headers
                        if self.limiter.legacy_timestamp_header:
                            headers["X-RateLimit-Reset"] = str(result.reset_timestamp)
                        
                        if self.limiter.expose_policy_header:
                            policy_str = f"{policy.permits};w={policy.per}"
                            if policy.burst > 0:
                                policy_str += f";burst={policy.burst}"
                            headers["X-RateLimit-Policy"] = policy_str
                
                # Send the message
                await send(message)

            # Call the app with wrapped send
            await self.app(scope, receive, send_wrapper)

        except Exception as e:
            logger.error(f"Rate limiter middleware error: {e}")

            # Handle based on fail mode
            if self.limiter.fail_mode == "open":
                # Allow request to proceed
                await self.app(scope, receive, send)
            else:
                # Return 503 Service Unavailable
                response = JSONResponse(
                    status_code=503,
                    content={"detail": "Service temporarily unavailable"},
                    headers={"Retry-After": "5"},  # 5 seconds
                )
                await response(scope, receive, send)

    def _is_excluded(self, request: Request) -> bool:
        """Check if request should be excluded from rate limiting."""
        # Check method exclusion
        if request.method in self.exclude_methods:
            return True

        # Check path exclusion
        path = request.url.path
        if path in self.exclude_paths:
            return True

        # Check path prefix exclusion
        for exclude_path in self.exclude_paths:
            if exclude_path.endswith('*') and path.startswith(exclude_path[:-1]):
                return True

        return False