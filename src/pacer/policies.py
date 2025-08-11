"""Rate limiting policies for FastAPI Pacer."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request


@dataclass(frozen=True)
class Rate:
    """
    Individual rate limit configuration.

    Args:
        permits: Number of requests allowed
        per: Time period (e.g., "1s", "10s", "1m", "1h", "1d")
        burst: Additional burst capacity (default: 0)
    """
    permits: int
    per: str
    burst: int = 0

    def __post_init__(self) -> None:
        if self.permits <= 0:
            raise ValueError("permits must be positive")
        if self.burst < 0:
            raise ValueError("burst must be non-negative")
        if not self._parse_duration(self.per):
            raise ValueError(f"Invalid duration format: {self.per}")

    @staticmethod
    def _parse_duration(duration: str) -> int | None:
        """Parse duration string to milliseconds."""
        pattern = r'^(\d+(?:\.\d+)?)(s|m|h|d)$'
        match = re.match(pattern, duration)
        if not match:
            return None

        value, unit = match.groups()
        value = float(value)

        multipliers = {
            's': 1000,         # seconds to ms
            'm': 60 * 1000,    # minutes to ms
            'h': 3600 * 1000,  # hours to ms
            'd': 86400 * 1000, # days to ms
        }

        return int(value * multipliers[unit])

    @property
    def period_ms(self) -> int:
        """Get period in milliseconds."""
        ms = self._parse_duration(self.per)
        if ms is None:
            raise ValueError(f"Invalid duration: {self.per}")
        return ms

    @property
    def emission_interval_ms(self) -> int:
        """Calculate emission interval T = period/permits."""
        return self.period_ms // self.permits

    @property
    def burst_capacity_ms(self) -> int:
        """Calculate burst capacity in milliseconds."""
        return self.burst * self.emission_interval_ms

    @property
    def ttl_ms(self) -> int:
        """Calculate TTL for Redis keys (must be >= burst window)."""
        # TTL should be at least the burst window plus the period
        return max(self.period_ms + self.burst_capacity_ms, self.period_ms * 2)


# Type for selector functions
Selector = Callable[["Request"], str]


@dataclass(frozen=True)
class Policy:
    """
    Rate limiting policy with support for multiple rates.

    Args:
        rates: List of rate limits (1-3 rates, all must pass)
        key: Selector function or string shorthand ("ip", "api_key", "user")
        name: Policy name for debugging and tracing
        max_rates: Maximum number of rates allowed (default: 3)
    """
    rates: list[Rate] = field(default_factory=lambda: [Rate(10, "1s", burst=10)])
    key: str | Selector = "ip"
    name: str = "default"
    max_rates: int = field(default=3, repr=False)

    def __post_init__(self) -> None:
        # Validate rates
        if not self.rates:
            raise ValueError("Policy must have at least one rate")
        if len(self.rates) > self.max_rates:
            raise ValueError(f"Policy cannot have more than {self.max_rates} rates")

        # Validate key
        if isinstance(self.key, str):
            if self.key not in ("ip", "api_key", "user", "org"):
                raise ValueError(f"Invalid key type: {self.key}")

    def generate_keys(self, app: str, scope: str, route: str, principal: str) -> list[str]:
        """
        Generate Redis keys for all rates in this policy.

        All keys share the same hash tag for Redis cluster compatibility.
        Each rate gets a unique suffix to maintain separate TAT values.
        """
        keys = []
        # Use hash tag for Redis cluster to colocate all keys
        base_key = f"{app}:{scope}:{{{route}}}:{principal}"

        for i, rate in enumerate(self.rates):
            # Add rate-specific suffix to maintain separate TAT
            rate_key = f"{base_key}:r{i}:{rate.permits}/{rate.per}"
            keys.append(rate_key)

        return keys

    @property
    def max_ttl_ms(self) -> int:
        """Get the maximum TTL across all rates."""
        return max(rate.ttl_ms for rate in self.rates)

    def describe(self) -> str:
        """Get human-readable description of the policy."""
        rate_strs = []
        for rate in self.rates:
            if rate.burst > 0:
                rate_strs.append(f"{rate.permits}/{rate.per} (burst={rate.burst})")
            else:
                rate_strs.append(f"{rate.permits}/{rate.per}")
        return f"Policy({self.name}): {', '.join(rate_strs)}"
