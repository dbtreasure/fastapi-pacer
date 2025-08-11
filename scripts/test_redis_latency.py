#!/usr/bin/env python3
"""Test raw Redis latency to understand baseline overhead."""

import asyncio
import statistics
import time

import redis.asyncio as redis


async def test_redis_latency():
    """Measure raw Redis EVALSHA latency."""

    # Connect to Redis
    client = await redis.from_url(
        "redis://localhost:6379",
        socket_connect_timeout=0.1,
        socket_timeout=0.01,
        max_connections=50,
        health_check_interval=0,
    )

    # Load a simple Lua script
    script = """
    local key = KEYS[1]
    local value = ARGV[1]
    redis.call('SET', key, value, 'PX', 1000)
    return redis.call('GET', key)
    """

    script_sha = await client.script_load(script)
    print(f"Script SHA: {script_sha}")

    # Warm up
    for i in range(100):
        await client.evalsha(script_sha, 1, f"test:key:{i}", "value")

    # Measure latencies
    latencies = []
    iterations = 1000

    print(f"\nMeasuring {iterations} Redis EVALSHA operations...")

    for i in range(iterations):
        start = time.perf_counter()
        await client.evalsha(script_sha, 1, f"test:key:{i % 100}", f"value{i}")
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # Convert to ms

    # Calculate statistics
    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p90 = latencies[int(len(latencies) * 0.90)]
    p99 = latencies[int(len(latencies) * 0.99)]
    mean = statistics.mean(latencies)

    print("\nRedis EVALSHA Latency (ms):")
    print(f"  Mean: {mean:.3f}")
    print(f"  P50:  {p50:.3f}")
    print(f"  P90:  {p90:.3f}")
    print(f"  P99:  {p99:.3f}")
    print(f"  Min:  {min(latencies):.3f}")
    print(f"  Max:  {max(latencies):.3f}")

    # Convert to microseconds for comparison
    print("\nIn microseconds:")
    print(f"  P99: {p99 * 1000:.0f}μs")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(test_redis_latency())
