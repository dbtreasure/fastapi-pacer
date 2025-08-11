"""Test configuration and fixtures."""

import pytest
import redis.asyncio as redis


@pytest.fixture(autouse=True)
async def clear_redis():
    """Clear Redis before each test to ensure isolation."""
    client = redis.from_url("redis://localhost:6379")
    try:
        await client.flushdb()
    except Exception:
        pass  # Redis might not be available
    finally:
        await client.aclose()
    yield
    # Optional: clean up after test too
    try:
        await client.flushdb()
    except Exception:
        pass
