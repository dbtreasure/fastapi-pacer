#!/usr/bin/env python3
"""
Test script for rate limiting behavior.

Usage:
    uv run python examples/test_rate_limits.py
"""

import asyncio

import httpx


async def test_endpoint(
    client: httpx.AsyncClient,
    endpoint: str,
    num_requests: int,
    delay: float = 0
) -> dict:
    """Test an endpoint with multiple requests."""
    results = {
        "endpoint": endpoint,
        "total_requests": num_requests,
        "successful": 0,
        "rate_limited": 0,
        "responses": []
    }

    for i in range(num_requests):
        try:
            response = await client.get(endpoint)
            results["responses"].append({
                "request": i + 1,
                "status": response.status_code,
                "headers": {
                    "RateLimit-Remaining": response.headers.get("RateLimit-Remaining"),
                    "Retry-After": response.headers.get("Retry-After"),
                }
            })

            if response.status_code == 200:
                results["successful"] += 1
            elif response.status_code == 429:
                results["rate_limited"] += 1

        except Exception as e:
            results["responses"].append({
                "request": i + 1,
                "error": str(e)
            })

        if delay > 0:
            await asyncio.sleep(delay)

    return results


async def test_burst_behavior():
    """Test burst capability of the rate limiter."""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print("=" * 60)
        print("Testing Burst Behavior")
        print("=" * 60)

        # Test burst endpoint (10 per 10s, burst of 5)
        print("\n1. Testing /test/burst (10 per 10s, burst of 5)")
        print("   Sending 8 requests immediately...")

        results = await test_endpoint(client, "/test/burst", 8)

        print(f"   Successful: {results['successful']}")
        print(f"   Rate Limited: {results['rate_limited']}")
        print("   Expected: 5 successful (burst), 3 rate limited")

        # Show individual results
        for resp in results["responses"][:8]:
            status = "✓" if resp["status"] == 200 else "✗"
            remaining = resp["headers"].get("RateLimit-Remaining", "N/A")
            print(f"   Request {resp['request']}: {status} (remaining: {remaining})")

        print("\n   Waiting 2 seconds...")
        await asyncio.sleep(2)

        print("   Sending 2 more requests...")
        results2 = await test_endpoint(client, "/test/burst", 2)

        print(f"   Successful: {results2['successful']}")
        print(f"   Rate Limited: {results2['rate_limited']}")
        print("   Expected: 2 successful (refilled at 1 per second)")


async def test_strict_limit():
    """Test strict rate limiting."""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print("\n" + "=" * 60)
        print("Testing Strict Rate Limit")
        print("=" * 60)

        print("\n2. Testing /api/limited (5 per minute, burst of 2)")
        print("   Sending 8 requests immediately...")

        results = await test_endpoint(client, "/api/limited", 8)

        print(f"   Successful: {results['successful']}")
        print(f"   Rate Limited: {results['rate_limited']}")
        print("   Expected: ~3 successful (2 burst + 1), 5 rate limited")

        for resp in results["responses"]:
            status = "✓" if resp["status"] == 200 else "✗"
            retry = resp["headers"].get("Retry-After", "N/A")
            print(f"   Request {resp['request']}: {status} (retry-after: {retry}s)")


async def test_api_key_limiting():
    """Test API key based rate limiting."""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print("\n" + "=" * 60)
        print("Testing API Key Rate Limiting")
        print("=" * 60)

        print("\n3. Testing /api/with-key with different API keys")

        # Test with API key 1
        headers1 = {"X-API-Key": "key-alice"}
        print("   Sending 3 requests with API key 'key-alice'...")
        for i in range(3):
            response = await client.get("/api/with-key", headers=headers1)
            print(f"   Request {i+1}: {response.status_code}")

        # Test with API key 2
        headers2 = {"X-API-Key": "key-bob"}
        print("   Sending 3 requests with API key 'key-bob'...")
        for i in range(3):
            response = await client.get("/api/with-key", headers=headers2)
            print(f"   Request {i+1}: {response.status_code}")

        # Test without API key (falls back to IP)
        print("   Sending 3 requests without API key (IP-based)...")
        for i in range(3):
            response = await client.get("/api/with-key")
            print(f"   Request {i+1}: {response.status_code}")


async def test_health_endpoint():
    """Test that health endpoint is not rate limited."""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print("\n" + "=" * 60)
        print("Testing Excluded Endpoints")
        print("=" * 60)

        print("\n4. Testing /health (should not be rate limited)")
        print("   Sending 10 requests...")

        results = await test_endpoint(client, "/health", 10)

        print(f"   Successful: {results['successful']}")
        print(f"   Rate Limited: {results['rate_limited']}")
        print("   Expected: 10 successful (no rate limit)")


async def main():
    """Run all tests."""
    print("\nFastAPI Pacer - Rate Limiter Test Suite")
    print("Make sure the example app is running on http://localhost:8000")
    print()

    try:
        # Check if server is running
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8000/health")
            if response.status_code != 200:
                print("❌ Server is not healthy")
                return
            print("✓ Server is running and healthy")
    except httpx.ConnectError:
        print("❌ Cannot connect to server at http://localhost:8000")
        print("   Run: uv run uvicorn examples.simple_app:app --reload")
        return

    # Run tests
    await test_burst_behavior()
    await test_strict_limit()
    await test_api_key_limiting()
    await test_health_endpoint()

    print("\n" + "=" * 60)
    print("Test Suite Complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
