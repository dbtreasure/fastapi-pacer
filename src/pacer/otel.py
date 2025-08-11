"""OpenTelemetry integration for FastAPI Pacer."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request

    from pacer.limiter import RateLimitResult
    from pacer.policies import Policy

logger = logging.getLogger(__name__)

# Try to import OpenTelemetry, but don't fail if not available
try:
    from opentelemetry import metrics, trace
    from opentelemetry.trace import Status, StatusCode

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    logger.debug("OpenTelemetry not available, metrics and tracing disabled")


class OTelHooks:
    """
    OpenTelemetry hooks for rate limiter observability.

    Provides metrics and tracing for rate limit decisions and errors.
    If OpenTelemetry is not installed, all methods are no-ops.

    Usage:
        otel_hooks = OTelHooks(service_name="my-api")

        limiter = Limiter(
            redis_url="redis://localhost:6379",
            on_decision=otel_hooks.on_decision,
            on_error=otel_hooks.on_error,
        )
    """

    def __init__(
        self,
        service_name: str = "fastapi-pacer",
        meter_name: str | None = None,
        tracer_name: str | None = None,
        include_request_attrs: bool = True,
        include_policy_attrs: bool = True,
    ):
        """
        Initialize OpenTelemetry hooks.

        Args:
            service_name: Service name for metrics and traces
            meter_name: Custom meter name (defaults to service_name)
            tracer_name: Custom tracer name (defaults to service_name)
            include_request_attrs: Include request attributes in telemetry
            include_policy_attrs: Include policy attributes in telemetry
        """
        self.service_name = service_name
        self.meter_name = meter_name or service_name
        self.tracer_name = tracer_name or service_name
        self.include_request_attrs = include_request_attrs
        self.include_policy_attrs = include_policy_attrs

        # Type annotations for meter and tracer
        self._meter: Any = None
        self._tracer: Any = None

        if OTEL_AVAILABLE:
            # Get meter for metrics
            self._meter = metrics.get_meter(self.meter_name)

            # Create metrics
            self._requests_total = self._meter.create_counter(
                name="rate_limit_requests_total",
                description="Total number of rate limit checks",
                unit="1",
            )

            self._requests_allowed = self._meter.create_counter(
                name="rate_limit_requests_allowed_total",
                description="Number of requests allowed by rate limiter",
                unit="1",
            )

            self._requests_denied = self._meter.create_counter(
                name="rate_limit_requests_denied_total",
                description="Number of requests denied by rate limiter",
                unit="1",
            )

            self._check_duration = self._meter.create_histogram(
                name="rate_limit_check_duration_ms",
                description="Duration of rate limit checks in milliseconds",
                unit="ms",
            )

            self._errors_total = self._meter.create_counter(
                name="rate_limit_errors_total",
                description="Total number of rate limit errors",
                unit="1",
            )

            # Get tracer for distributed tracing
            self._tracer = trace.get_tracer(self.tracer_name)

    def on_decision(
        self,
        request: "Request",
        policy: "Policy",
        result: "RateLimitResult",
        duration_ms: float,
    ) -> None:
        """
        Hook called after rate limit decision.

        Records metrics and spans for the rate limit check.
        """
        if not OTEL_AVAILABLE:
            return

        # Build attributes
        attrs = self._build_attributes(request, policy, result)

        # Record metrics
        self._requests_total.add(1, attrs)

        if result.allowed:
            self._requests_allowed.add(1, attrs)
        else:
            self._requests_denied.add(1, attrs)

        self._check_duration.record(duration_ms, attrs)

        # Add span event to current trace if active
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            event_attrs: dict[str, Any] = {
                "rate_limit.allowed": result.allowed,
                "rate_limit.remaining": result.remaining,
                "rate_limit.duration_ms": duration_ms,
            }

            if self.include_policy_attrs:
                event_attrs["rate_limit.policy"] = policy.name
                event_attrs["rate_limit.matched_rate"] = result.matched_rate_index

            if not result.allowed:
                event_attrs["rate_limit.retry_after_ms"] = result.retry_after_ms

            current_span.add_event(
                name="rate_limit_check",
                attributes=event_attrs,
            )

    def on_error(
        self,
        request: "Request",
        policy: "Policy",
        error: Exception,
        duration_ms: float,
    ) -> None:
        """
        Hook called on rate limit errors.

        Records error metrics and spans.
        """
        if not OTEL_AVAILABLE:
            return

        # Build attributes
        attrs = self._build_error_attributes(request, policy, error)

        # Record error metric
        self._errors_total.add(1, attrs)

        # Record duration even for errors
        self._check_duration.record(duration_ms, attrs)

        # Add error to current trace if active
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            current_span.record_exception(error)
            current_span.set_status(Status(StatusCode.ERROR, str(error)))
            current_span.add_event(
                name="rate_limit_error",
                attributes={
                    "error.type": type(error).__name__,
                    "error.message": str(error),
                    "rate_limit.duration_ms": duration_ms,
                },
            )

    def _build_attributes(
        self,
        request: "Request",
        policy: "Policy",
        result: "RateLimitResult",
    ) -> dict[str, Any]:
        """Build attributes for metrics and traces."""
        attrs = {
            "service": self.service_name,
            "allowed": str(result.allowed).lower(),
        }

        if self.include_request_attrs:
            attrs.update({
                "method": request.method,
                "path": request.url.path,
                "route": request.url.path,  # Can be overridden by route pattern
            })

        if self.include_policy_attrs:
            attrs.update({
                "policy": policy.name,
                "matched_rate": f"{result.matched_rate_index}",
            })

        return attrs

    def _build_error_attributes(
        self,
        request: "Request",
        policy: "Policy",
        error: Exception,
    ) -> dict[str, Any]:
        """Build attributes for error metrics."""
        attrs = {
            "service": self.service_name,
            "error_type": type(error).__name__,
        }

        if self.include_request_attrs:
            attrs.update({
                "method": request.method,
                "path": request.url.path,
            })

        if self.include_policy_attrs:
            attrs["policy"] = policy.name

        return attrs


def create_otel_hooks(
    service_name: str = "fastapi-pacer",
    **kwargs: Any,
) -> tuple[Any, Any]:
    """
    Create OpenTelemetry hooks for rate limiter.

    Returns a tuple of (on_decision, on_error) hooks.
    If OpenTelemetry is not available, returns no-op functions.

    Usage:
        on_decision, on_error = create_otel_hooks("my-api")

        limiter = Limiter(
            redis_url="redis://localhost:6379",
            on_decision=on_decision,
            on_error=on_error,
        )
    """
    if OTEL_AVAILABLE:
        hooks = OTelHooks(service_name=service_name, **kwargs)
        return hooks.on_decision, hooks.on_error
    else:
        # Return no-op functions
        def noop_decision(request: Any, policy: Any, result: Any, duration_ms: float) -> None:
            pass

        def noop_error(request: Any, policy: Any, error: Exception, duration_ms: float) -> None:
            pass

        return noop_decision, noop_error
