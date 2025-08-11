#!/usr/bin/env python3
"""Test actual rate limiter overhead in isolation."""

import asyncio
import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pacer import Limiter, Policy, Rate, limit
from pacer.dependencies import set_limiter


async def measure_overhead():
    """Measure rate limiter overhead without network/multi-process complexity."""

    # Setup limiter
    limiter = Limiter(
        redis_url="redis://localhost:6379",
        default_policy=Policy(rates=[Rate(10000, "1s", burst=1000)], key="ip", name="overhead_test"),
        fail_mode="open",
        expose_headers=False,
        connect_timeout_ms=100,
        command_timeout_ms=10,
    )

    await limiter.startup()
    set_limiter(limiter)

    # Create test app
    app = FastAPI()

    @app.get("/unlimited")
    async def unlimited():
        return {"status": "ok"}

    @app.get("/limited", dependencies=[Depends(limit(Rate(10000, "1s", burst=1000)))])
    async def limited():
        return {"status": "ok"}

    # Create test client
    client = TestClient(app)

    # Warm up
    print("Warming up...")
    for _ in range(100):
        client.get("/unlimited")
        client.get("/limited")

    # Measure baseline (no rate limiting)
    print("\nMeasuring baseline (no rate limiting)...")
    baseline_latencies = []
    for _ in range(500):
        start = time.perf_counter()
        response = client.get("/unlimited")
        end = time.perf_counter()
        assert response.status_code == 200
        baseline_latencies.append((end - start) * 1000)  # ms

    # Measure with rate limiting
    print("Measuring with rate limiting...")
    limited_latencies = []
    for _ in range(500):
        start = time.perf_counter()
        response = client.get("/limited")
        end = time.perf_counter()
        assert response.status_code == 200
        limited_latencies.append((end - start) * 1000)  # ms

    # Calculate statistics
    baseline_latencies.sort()
    limited_latencies.sort()

    baseline_p50 = baseline_latencies[int(len(baseline_latencies) * 0.50)]
    baseline_p90 = baseline_latencies[int(len(baseline_latencies) * 0.90)]
    baseline_p99 = baseline_latencies[int(len(baseline_latencies) * 0.99)]

    limited_p50 = limited_latencies[int(len(limited_latencies) * 0.50)]
    limited_p90 = limited_latencies[int(len(limited_latencies) * 0.90)]
    limited_p99 = limited_latencies[int(len(limited_latencies) * 0.99)]

    overhead_p50 = (limited_p50 - baseline_p50) * 1000  # Convert to μs
    overhead_p90 = (limited_p90 - baseline_p90) * 1000
    overhead_p99 = (limited_p99 - baseline_p99) * 1000

    print("\n" + "="*50)
    print("RESULTS")
    print("="*50)

    print("\nBaseline (no rate limiting) - ms:")
    print(f"  P50: {baseline_p50:.3f}")
    print(f"  P90: {baseline_p90:.3f}")
    print(f"  P99: {baseline_p99:.3f}")

    print("\nWith rate limiting - ms:")
    print(f"  P50: {limited_p50:.3f}")
    print(f"  P90: {limited_p90:.3f}")
    print(f"  P99: {limited_p99:.3f}")

    print("\nRate Limiter Overhead - μs:")
    print(f"  P50: {overhead_p50:.0f}μs")
    print(f"  P90: {overhead_p90:.0f}μs")
    print(f"  P99: {overhead_p99:.0f}μs")

    if overhead_p99 < 150:
        print("\n✓ Meets <150μs P99 target!")
    else:
        print(f"\n⚠ Exceeds 150μs P99 target (by {overhead_p99 - 150:.0f}μs)")

    await limiter.shutdown()


if __name__ == "__main__":
    asyncio.run(measure_overhead())
