import logging
from functools import wraps
from typing import Callable, Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from pacer.limiter import Limiter
from pacer.policies import Rate

logger = logging.getLogger(__name__)

# Global limiter instance (to be set by user)
_global_limiter: Limiter | None = None


def set_limiter(limiter: Limiter) -> None:
    """Set the global limiter instance for dependency injection."""
    global _global_limiter
    _global_limiter = limiter


def get_limiter() -> Limiter:
    """Get the global limiter instance."""
    if _global_limiter is None:
        raise RuntimeError(
            "Limiter not configured. Call set_limiter() or use limit() with explicit limiter."
        )
    return _global_limiter


def limit(
    policy: Rate | None = None,
    limiter: Limiter | None = None,
) -> Callable[[Request, Response], Any]:
    """
    FastAPI dependency for per-route rate limiting.

    Args:
        policy: Rate limit policy for this route
        limiter: Limiter instance (uses global if None)

    Returns:
        Dependency function for FastAPI

    Usage:
        @app.get("/api/items", dependencies=[Depends(limit(Rate(100, "1m")))])
        async def get_items():
            return {"items": []}
    """
    async def rate_limit_dependency(
        request: Request,
        response: Response,
    ) -> None:
        """Check rate limit for the current request."""
        # Get limiter instance
        limiter_instance = limiter or get_limiter()

        try:
            # Check rate limit
            result = await limiter_instance.check_rate_limit(
                request=request,
                policy=policy,
            )

            # Add headers to response
            limiter_instance.add_headers(response, result, policy)

            if not result.allowed:
                # Rate limit exceeded
                logger.info(f"Rate limit exceeded for route {request.url.path}")

                # Raise HTTPException with 429 status
                raise HTTPException(
                    status_code=429,
                    detail={
                        "detail": "rate_limited",
                        "retry_after_ms": result.retry_after_ms,
                    },
                    headers={
                        "Retry-After": str(result.retry_after_seconds),
                        "RateLimit-Limit": str((policy or limiter_instance.default_policy).permits),
                        "RateLimit-Remaining": str(max(0, result.remaining)),
                        "RateLimit-Reset": str(result.reset_timestamp),
                    },
                )

        except HTTPException:
            # Re-raise HTTPException
            raise
        except Exception as e:
            logger.error(f"Rate limit check failed: {e}")

            # Handle based on fail mode
            if limiter_instance.fail_mode == "closed":
                # Deny request on error
                raise HTTPException(
                    status_code=503,
                    detail="Service temporarily unavailable",
                    headers={"Retry-After": "5"},
                ) from e
            # else: fail open - allow request to proceed

    return rate_limit_dependency


class RateLimitDecorator:
    """
    Decorator for rate limiting FastAPI routes.

    This is an alternative to using dependencies=[Depends(limit(...))]

    Usage:
        @app.get("/api/items")
        @rate_limit(Rate(100, "1m"))
        async def get_items():
            return {"items": []}
    """

    def __init__(self, policy: Rate | None = None, limiter: Limiter | None = None):
        self.policy = policy
        self.limiter = limiter

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            # Get limiter instance
            limiter_instance = self.limiter or get_limiter()

            # Check rate limit
            result = await limiter_instance.check_rate_limit(
                request=request,
                policy=self.policy,
            )

            if not result.allowed:
                # Rate limit exceeded
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": "rate_limited",
                        "retry_after_ms": result.retry_after_ms,
                    },
                )

                # Add headers
                limiter_instance.add_headers(response, result, self.policy)

                return response

            # Call the original function
            response = await func(request, *args, **kwargs)

            # Add headers to successful response
            if isinstance(response, Response):
                limiter_instance.add_headers(response, result, self.policy)

            return response

        return wrapper


# Convenience decorator factory
def rate_limit(
    policy: Rate | None = None,
    limiter: Limiter | None = None,
) -> RateLimitDecorator:
    """
    Decorator factory for rate limiting.

    Usage:
        @app.get("/api/items")
        @rate_limit(Rate(100, "1m"))
        async def get_items():
            return {"items": []}
    """
    return RateLimitDecorator(policy=policy, limiter=limiter)
