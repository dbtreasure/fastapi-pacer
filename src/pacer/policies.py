import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    """
    Rate limit policy using GCRA algorithm.

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

    def key_for(self, app: str, scope: str, route: str, principal: str) -> str:
        """Generate Redis key for this rate limit."""
        # Use hash tag for Redis cluster to colocate by route
        return f"{app}:{scope}:{{{route}}}:{principal}"
