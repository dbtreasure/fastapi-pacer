import logging
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from pacer.limiter import Limiter
from pacer.policies import Rate

logger = logging.getLogger(__name__)


class LimiterMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware for global rate limiting.

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
        super().__init__(app)
        self.limiter = limiter
        self.policy = policy
        self.exclude_paths = set(exclude_paths or [])
        self.exclude_methods = set(exclude_methods or ["OPTIONS", "HEAD"])

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        """Process request through rate limiter."""
        # Check if path or method is excluded
        if self._is_excluded(request):
            response = await call_next(request)
            return response  # type: ignore[no-any-return]

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

                return response

            # Request allowed - proceed
            response = await call_next(request)

            # Add rate limit headers to successful response
            self.limiter.add_headers(response, result, self.policy)

            return response  # type: ignore[no-any-return]

        except Exception as e:
            logger.error(f"Rate limiter middleware error: {e}")

            # Handle based on fail mode
            if self.limiter.fail_mode == "open":
                # Allow request to proceed
                response = await call_next(request)
                return response  # type: ignore[no-any-return]
            else:
                # Return 503 Service Unavailable
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Service temporarily unavailable"},
                    headers={"Retry-After": "5"},  # 5 seconds
                )

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
