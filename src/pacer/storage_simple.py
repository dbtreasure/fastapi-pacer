"""Simple Redis storage backend for multi-rate policies."""

import logging
import time
from pathlib import Path

import redis.asyncio as redis
from redis.exceptions import ConnectionError, NoScriptError, ResponseError, TimeoutError

from pacer.policies import Policy

logger = logging.getLogger(__name__)


class SimpleRedisStorage:
    """
    Simple Redis storage backend for rate limiting with multi-rate GCRA.
    
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
                max_connections=50,
                health_check_interval=0,  # Disable health checks for performance
            )

            # Test connection
            await self._client.ping()

            # Load Lua script
            script_path = Path(__file__).parent / "lua" / "gcra_multi.lua"
            with open(script_path) as f:
                self._script_content = f.read()

            # Register script and get SHA
            self._script_sha = await self._client.script_load(self._script_content)

            self._connected = True
            logger.info("Connected to Redis and loaded multi-rate GCRA script")

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during Redis connection: {e}")
            raise

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._connected = False
            logger.info("Disconnected from Redis")

    async def check_policy(
        self,
        keys: list[str],
        policy: Policy,
        now_ms: int | None = None,
    ) -> tuple[bool, int, int, int, int]:
        """
        Check if request is allowed under the policy.
        
        Args:
            keys: Redis keys for each rate in the policy
            policy: The policy to check against
            now_ms: Current time in milliseconds (for testing)
        
        Returns:
            Tuple of (allowed, retry_after_ms, reset_ms, remaining, matched_rate_index)
        """
        if not self._connected:
            raise RuntimeError("Storage not connected. Call connect() first.")

        if now_ms is None:
            now_ms = int(time.time() * 1000)

        # Build arguments for Lua script
        args = [
            str(now_ms),
            str(policy.max_ttl_ms),
            str(len(policy.rates)),
        ]

        # Add emission interval and burst capacity for each rate
        for rate in policy.rates:
            args.extend([
                str(rate.emission_interval_ms),
                str(rate.burst_capacity_ms),
            ])

        # Pad with empty args if less than 3 rates
        while len(args) < 9:  # 3 base + 2*3 rate args
            args.append("0")

        # Pad keys to always have 3
        padded_keys = keys + [""] * (3 - len(keys))

        try:
            # Execute script with EVALSHA
            if not self._client or not self._script_sha:
                raise RuntimeError("Storage not properly initialized")
                
            result = await self._client.evalsha(
                self._script_sha,
                3,  # Always pass 3 keys (some may be empty)
                *padded_keys[:3],  # Always pass 3 key slots
                *args,
            )

            # Parse result
            if result and len(result) >= 5:
                allowed = bool(result[0])
                retry_after_ms = int(result[1])
                reset_ms = int(result[2])
                remaining = int(result[3])
                matched_rate_index = int(result[4]) - 1  # Convert to 0-based

                return allowed, retry_after_ms, reset_ms, remaining, matched_rate_index
            else:
                raise ValueError(f"Invalid result from Lua script: {result}")

        except NoScriptError:
            # Script not in cache, reload it
            logger.warning("Lua script not in cache, reloading...")
            if self._script_content and self._client:
                self._script_sha = await self._client.script_load(self._script_content)
                # Retry the command
                return await self.check_policy(keys, policy, now_ms)
            else:
                raise RuntimeError("Lua script not loaded") from None

        except (ConnectionError, TimeoutError, ResponseError) as e:
            logger.error(f"Redis error during rate limit check: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during rate limit check: {e}")
            raise

    @property
    def redis(self) -> redis.Redis | None:
        """Get the Redis client (for testing/admin purposes)."""
        return self._client
