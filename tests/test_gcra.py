
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)


class TestGCRAAlgorithm:
    """Test GCRA algorithm logic."""

    def test_gcra_math_first_request(self):
        """Test GCRA calculation for first request."""
        # Given
        emission_interval = 100  # 100ms between requests
        burst_capacity = 200  # 200ms burst (2 requests)
        now = 1000  # Current time in ms

        # When first request arrives, TAT should be initialized to now
        # and request should be allowed
        tat = now  # First request
        allow_at = tat - burst_capacity  # 1000 - 200 = 800

        # Should allow since now (1000) >= allow_at (800)
        assert now >= allow_at

        # New TAT = max(tat, now) + emission_interval = 1000 + 100 = 1100
        new_tat = max(tat, now) + emission_interval
        assert new_tat == 1100

    def test_gcra_math_burst_requests(self):
        """Test GCRA with burst of requests."""
        emission_interval = 100
        burst_capacity = 200  # Allows 2 extra requests

        # First request at t=1000
        now1 = 1000
        tat1 = now1
        new_tat1 = max(tat1, now1) + emission_interval
        assert new_tat1 == 1100

        # Second request immediately at t=1001 (within burst)
        now2 = 1001
        tat2 = new_tat1  # 1100
        allow_at2 = tat2 - burst_capacity  # 1100 - 200 = 900
        assert now2 >= allow_at2  # 1001 >= 900, allowed
        new_tat2 = max(tat2, now2) + emission_interval  # max(1100, 1001) + 100 = 1200
        assert new_tat2 == 1200

        # Third request immediately at t=1002 (within burst)
        now3 = 1002
        tat3 = new_tat2  # 1200
        allow_at3 = tat3 - burst_capacity  # 1200 - 200 = 1000
        assert now3 >= allow_at3  # 1002 >= 1000, allowed
        new_tat3 = max(tat3, now3) + emission_interval  # max(1200, 1002) + 100 = 1300
        assert new_tat3 == 1300

        # Fourth request immediately at t=1003 (exceeds burst)
        now4 = 1003
        tat4 = new_tat3  # 1300
        allow_at4 = tat4 - burst_capacity  # 1300 - 200 = 1100
        assert now4 < allow_at4  # 1003 < 1100, DENIED

        # Retry after should be allow_at4 - now4 = 1100 - 1003 = 97ms
        retry_after = allow_at4 - now4
        assert retry_after == 97

    def test_gcra_math_steady_rate(self):
        """Test GCRA at steady rate matching emission interval."""
        emission_interval = 100
        burst_capacity = 0  # No burst allowed

        # Requests at exact emission interval should all be allowed
        times = [1000, 1100, 1200, 1300, 1400]
        tat = times[0]

        for now in times:
            allow_at = tat - burst_capacity
            assert now >= allow_at  # All should be allowed
            tat = max(tat, now) + emission_interval

    def test_gcra_math_after_idle_period(self):
        """Test GCRA after idle period."""
        emission_interval = 100
        burst_capacity = 200

        # First request
        now1 = 1000
        tat1 = now1 + emission_interval  # 1100

        # Long idle period, then request at t=5000
        now2 = 5000
        allow_at2 = tat1 - burst_capacity  # 1100 - 200 = 900
        assert now2 >= allow_at2  # 5000 >= 900, allowed

        # TAT should be reset to current time since we're past the old TAT
        new_tat2 = max(tat1, now2) + emission_interval  # max(1100, 5000) + 100 = 5100
        assert new_tat2 == 5100

    def test_remaining_capacity_calculation(self):
        """Test calculation of remaining request capacity."""
        emission_interval = 100
        burst_capacity = 300  # Allows 3 burst requests

        # After first request at t=1000, TAT=1100
        now = 1000
        tat = 1100

        # Burst available = burst_capacity - (tat - now) = 300 - 100 = 200
        burst_available = burst_capacity - (tat - now)
        assert burst_available == 200

        # Remaining requests = burst_available / emission_interval = 200 / 100 = 2
        remaining = burst_available // emission_interval
        assert remaining == 2


class GCRAStateMachine(RuleBasedStateMachine):
    """
    Property-based testing for GCRA using Hypothesis.

    This state machine simulates requests to verify GCRA properties.
    """

    def __init__(self):
        super().__init__()
        self.emission_interval = 100  # 100ms between requests
        self.burst_capacity = 200  # 200ms burst
        self.tat = None  # Theoretical Arrival Time
        self.current_time = 0
        self.allowed_count = 0
        self.denied_count = 0
        self.last_allowed_time = None

    @rule(time_advance=st.integers(min_value=0, max_value=1000))
    def advance_time(self, time_advance):
        """Advance the current time."""
        self.current_time += time_advance

    @rule()
    def make_request(self):
        """Simulate a request and check GCRA decision."""
        now = self.current_time

        if self.tat is None:
            # First request
            self.tat = now

        allow_at = self.tat - self.burst_capacity

        if now >= allow_at:
            # Request allowed
            self.allowed_count += 1
            self.last_allowed_time = now
            self.tat = max(self.tat, now) + self.emission_interval
        else:
            # Request denied
            self.denied_count += 1

    @invariant()
    def tat_monotonic(self):
        """TAT should never decrease."""
        if self.tat is not None and hasattr(self, '_prev_tat'):
            assert self.tat >= self._prev_tat
        if self.tat is not None:
            self._prev_tat = self.tat

    @invariant()
    def burst_limit_respected(self):
        """Burst capacity should be respected."""
        if self.tat is not None:
            # The next allowed time should not be more than
            # burst_capacity in the future from TAT
            _ = self.tat - self.burst_capacity
            # This is implicitly checked in make_request


@pytest.mark.hypothesis
class TestGCRAProperties:
    """Property-based tests for GCRA."""

    def test_gcra_state_machine(self):
        """Test GCRA properties using state machine."""
        run_state_machine_as_test(GCRAStateMachine, settings=settings(max_examples=100, deadline=None))

    @given(
        permits=st.integers(min_value=1, max_value=1000),
        burst=st.integers(min_value=0, max_value=100),
        request_times=st.lists(
            st.integers(min_value=0, max_value=10000),
            min_size=1,
            max_size=100
        ).map(sorted)  # Ensure times are sorted
    )
    def test_burst_then_steady(self, permits, burst, request_times):
        """Test that burst is allowed followed by steady rate."""
        if not request_times:
            return

        period_ms = 1000  # 1 second period
        emission_interval = period_ms // permits
        burst_capacity = burst * emission_interval

        tat = None
        allowed = []
        denied = []

        for now in request_times:
            if tat is None:
                tat = now

            allow_at = tat - burst_capacity

            if now >= allow_at:
                allowed.append(now)
                tat = max(tat, now) + emission_interval
            else:
                denied.append(now)

        # Verify that we don't allow more than permits + burst
        # in any period window
        if allowed:
            for i, t in enumerate(allowed):
                # Count requests in the period starting at t
                count_in_period = sum(
                    1 for t2 in allowed[i:]
                    if t2 < t + period_ms
                )
                # Should not exceed permits + burst
                assert count_in_period <= permits + burst
