"""FastAPI dependency injection for rate limiting."""

import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response

from pacer.policies import Policy, Rate

if TYPE_CHECKING:
    from pacer.limiter import Limiter

logger = logging.getLogger(__name__)

# Global limiter instance (to be set by user)
_global_limiter: "Limiter | None" = None


def set_limiter(limiter: "Limiter") -> None:
    """Set the global limiter instance for dependency injection."""
    global _global_limiter
    _global_limiter = limiter


def get_limiter() -> "Limiter":
    """Get the global limiter instance."""
    if _global_limiter is None:
        raise RuntimeError(
            "Limiter not configured. Call set_limiter() or use limit() with explicit limiter."
        )
    return _global_limiter


def limit(
    policy: Policy | Rate | None = None,
    limiter: "Limiter | None" = None,
) -> Callable[[Request, Response], Any]:
    """
    FastAPI dependency for per-route rate limiting.

    Args:
        policy: Rate limit policy (Policy or Rate for backward compat)
        limiter: Limiter instance (uses global if None)

    Returns:
        Dependency function for FastAPI

    Usage:
        # With Policy
        @app.get("/api/items", dependencies=[Depends(limit(Policy(rates=[Rate(100, "1m")])))])
        async def get_items():
            return {"items": []}

        # With Rate (backward compat)
        @app.get("/api/items", dependencies=[Depends(limit(Rate(100, "1m")))])
        async def get_items():
            return {"items": []}
    """
    # Convert Rate to Policy if needed for backward compatibility
    if isinstance(policy, Rate):
        policy = Policy(rates=[policy], key="ip", name="dependency")

    async def rate_limit_dependency(
        request: Request,
        response: Response,
    ) -> None:
        """Check rate limit for the current request."""
        # Get limiter instance
        limiter_instance = limiter or get_limiter()

        try:
            # Check rate limit
            result = await limiter_instance.check_policy(
                request=request,
                policy=policy,
            )

            # Add headers to response
            limiter_instance.add_headers(response, result, policy)

            if not result.allowed:
                # Rate limit exceeded
                logger.info(f"Rate limit exceeded for route {request.url.path}")

                # Get the matched rate for the error message
                if policy:
                    matched_rate = policy.rates[result.matched_rate_index]
                    limit_value = matched_rate.permits
                else:
                    limit_value = 0

                # Raise HTTPException with 429 status
                raise HTTPException(
                    status_code=429,
                    detail={
                        "detail": "rate_limited",
                        "retry_after_ms": result.retry_after_ms,
                    },
                    headers={
                        "Retry-After": str(result.retry_after_seconds),
                        "RateLimit-Limit": str(limit_value),
                        "RateLimit-Remaining": str(max(0, result.remaining)),
                        "RateLimit-Reset": str(result.reset_seconds),
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
        @rate_limit(Policy(rates=[Rate(100, "1m")]))
        async def get_items():
            return {"items": []}
    """

    def __init__(self, policy: Policy | Rate | None = None, limiter: "Limiter | None" = None):
        # Convert Rate to Policy if needed for backward compatibility
        if isinstance(policy, Rate):
            self.policy: Policy | None = Policy(rates=[policy], key="ip", name="decorator")
        else:
            self.policy = policy
        self.limiter = limiter

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            # Get limiter instance
            limiter_instance = self.limiter or get_limiter()

            # Check rate limit
            result = await limiter_instance.check_policy(
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
    policy: Policy | Rate | None = None,
    limiter: "Limiter | None" = None,
) -> RateLimitDecorator:
    """
    Decorator factory for rate limiting.

    Usage:
        # With Policy
        @app.get("/api/items")
        @rate_limit(Policy(rates=[Rate(100, "1m")]))
        async def get_items():
            return {"items": []}

        # With Rate (backward compat)
        @app.get("/api/items")
        @rate_limit(Rate(100, "1m"))
        async def get_items():
            return {"items": []}
    """
    return RateLimitDecorator(policy=policy, limiter=limiter)
