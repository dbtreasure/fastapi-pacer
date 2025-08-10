"""Storage backend for rate limiting.

This module provides backward compatibility. New code should import from:
- storage_simple.SimpleRedisStorage for regular Redis
- storage_cluster.RedisClusterStorage for Redis Cluster (if needed)
"""

import logging
from typing import Any, Union

from pacer.storage_simple import SimpleRedisStorage

logger = logging.getLogger(__name__)


class RedisStorage:
    """Redis storage backend for rate limiting with GCRA.

    This is a compatibility wrapper that uses SimpleRedisStorage by default.
    If cluster_mode=True is passed, it will import and use RedisClusterStorage.

    For new code, directly import SimpleRedisStorage or RedisClusterStorage.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        connect_timeout_ms: int = 1000,
        command_timeout_ms: int = 100,
        max_retries: int = 1,
        cluster_mode: bool = False,
    ):
        if cluster_mode:
            # Only import cluster storage if needed (keeps it optional)
            try:
                from pacer.storage_cluster import RedisClusterStorage
                logger.info("Using Redis Cluster storage backend")
                self._storage: Union[SimpleRedisStorage, Any] = RedisClusterStorage(
                    redis_url=redis_url,
                    connect_timeout_ms=connect_timeout_ms,
                    command_timeout_ms=command_timeout_ms,
                    max_retries=max_retries,
                )
            except ImportError as e:
                raise ImportError(
                    "Redis Cluster support requires redis-py with cluster support. "
                    "Install with: pip install redis[cluster]"
                ) from e
        else:
            logger.info("Using simple Redis storage backend")
            self._storage = SimpleRedisStorage(
                redis_url=redis_url,
                connect_timeout_ms=connect_timeout_ms,
                command_timeout_ms=command_timeout_ms,
                max_retries=max_retries,
            )

        # Expose internal storage attributes for compatibility
        self._client: Any = None
        self._connected = False

    async def connect(self) -> None:
        """Establish connection to Redis and load scripts."""
        await self._storage.connect()
        self._client = self._storage._client
        self._connected = self._storage._connected

    async def disconnect(self) -> None:
        """Close Redis connection."""
        await self._storage.disconnect()
        self._client = None
        self._connected = False

    async def check_rate_limit(
        self,
        key: str,
        emission_interval_ms: int,
        burst_capacity_ms: int,
        ttl_ms: int,
    ) -> tuple[bool, int, int, int]:
        """Check rate limit using GCRA algorithm."""
        return await self._storage.check_rate_limit(
            key, emission_interval_ms, burst_capacity_ms, ttl_ms
        )

    async def is_healthy(self) -> bool:
        """Check if Redis connection is healthy."""
        return await self._storage.is_healthy()

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        return await self._storage.get_stats()
