"""Simple Redis storage backend without cluster support."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ConnectionError, NoScriptError, RedisError, ResponseError, TimeoutError

logger = logging.getLogger(__name__)


class SimpleRedisStorage:
    """Simple Redis storage backend for rate limiting with GCRA.

    This is the recommended storage backend for most applications.
    For Redis Cluster support, use RedisClusterStorage instead.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        connect_timeout_ms: int = 1000,
        command_timeout_ms: int = 100,
        max_retries: int = 1,
    ):
        self.redis_url = redis_url
        self.connect_timeout = connect_timeout_ms / 1000.0
        self.command_timeout = command_timeout_ms / 1000.0
        self.max_retries = max_retries

        self._client: redis.Redis | None = None
        self._script_sha: str | None = None
        self._script_content: str | None = None
        self._connected = False

    async def connect(self) -> None:
        """Establish connection to Redis and load scripts."""
        try:
            self._client = redis.from_url(
                self.redis_url,
                socket_connect_timeout=self.connect_timeout,
                socket_timeout=self.command_timeout,
                decode_responses=False,
                max_connections=50,  # Increase connection pool size
                health_check_interval=0,  # Disable health checks for performance
            )

            # Test connection
            await self._client.ping()
            self._connected = True

            # Load GCRA script
            await self._load_script()

            logger.info("Connected to Redis successfully")

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.aclose()
            self._connected = False
            logger.info("Disconnected from Redis")

    async def _load_script(self) -> None:
        """Load GCRA Lua script into Redis."""
        if not self._client:
            raise RuntimeError("Redis client not connected")

        # Load script from file
        script_path = Path(__file__).parent / "lua" / "gcra.lua"
        with open(script_path) as f:
            self._script_content = f.read()

        # Load script and get SHA
        self._script_sha = await self._client.script_load(self._script_content)
        logger.debug(f"Loaded GCRA script with SHA: {self._script_sha}")

    async def check_rate_limit(
        self,
        key: str,
        emission_interval_ms: int,
        burst_capacity_ms: int,
        ttl_ms: int,
    ) -> tuple[bool, int, int, int]:
        """
        Check rate limit using GCRA algorithm.

        Args:
            key: Redis key for the rate limit
            emission_interval_ms: Time between permitted requests
            burst_capacity_ms: Burst capacity in milliseconds
            ttl_ms: TTL for the Redis key

        Returns:
            Tuple of (allowed, retry_after_ms, reset_ms, remaining)
        """
        if not self._connected or not self._client:
            raise RuntimeError("Redis storage not connected")

        now_ms = int(time.time() * 1000)

        for attempt in range(self.max_retries + 1):
            try:
                # Try EVALSHA first, fall back to EVAL if script not loaded
                try:
                    result = await self._execute_script_sha(
                        key, emission_interval_ms, burst_capacity_ms, now_ms, ttl_ms
                    )
                except NoScriptError:
                    logger.debug("Script not in cache, reloading")
                    await self._load_script()
                    result = await self._execute_script_sha(
                        key, emission_interval_ms, burst_capacity_ms, now_ms, ttl_ms
                    )

                # Parse result
                allowed = bool(result[0])
                retry_after_ms = int(result[1])
                reset_ms = int(result[2])
                remaining = int(result[3])

                return allowed, retry_after_ms, reset_ms, remaining

            except (ConnectionError, TimeoutError) as e:
                if attempt < self.max_retries:
                    logger.warning(f"Redis operation failed (attempt {attempt + 1}), retrying: {e}")
                    await asyncio.sleep(0.01 * (2 ** attempt))  # Exponential backoff
                else:
                    logger.error(f"Redis operation failed after {self.max_retries + 1} attempts")
                    raise
            except ResponseError as e:
                logger.error(f"Redis script error: {e}")
                raise

        # This should never be reached due to the raises above, but satisfies type checker
        raise RuntimeError("Unexpected error in rate limit check")

    async def _execute_script_sha(
        self,
        key: str,
        emission_interval_ms: int,
        burst_capacity_ms: int,
        now_ms: int,
        ttl_ms: int,
    ) -> list[Any]:
        """Execute GCRA script using EVALSHA."""
        if not self._client or not self._script_sha:
            raise RuntimeError("Script not loaded")

        result = await self._client.evalsha(  # type: ignore[misc]
            self._script_sha,
            1,  # number of keys
            key,  # KEYS[1]
            str(emission_interval_ms),  # ARGV[1]
            str(burst_capacity_ms),  # ARGV[2]
            str(now_ms),  # ARGV[3]
            str(ttl_ms),  # ARGV[4]
        )
        return result  # type: ignore[no-any-return]

    async def is_healthy(self) -> bool:
        """Check if Redis connection is healthy."""
        if not self._connected or not self._client:
            return False

        try:
            await asyncio.wait_for(self._client.ping(), timeout=0.1)
            return True
        except (RedisError, asyncio.TimeoutError):
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        if not self._client:
            return {"connected": False}

        try:
            info = await self._client.info("stats")
            return {
                "connected": self._connected,
                "total_connections_received": info.get("total_connections_received", 0),
                "total_commands_processed": info.get("total_commands_processed", 0),
                "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec", 0),
            }
        except RedisError:
            return {"connected": False}
