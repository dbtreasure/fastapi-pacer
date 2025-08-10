# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI Pacer is a production-ready rate limiter using the GCRA (Generic Cell Rate Algorithm) with Redis backend. The implementation prioritizes "grug-first" principles - simple, correct code that can grow to best-in-class.

## Development Commands

### Package Management
This project uses `uv` as the package manager. Always use `uv`, not pip or poetry.

```bash
# Install dependencies
uv pip install -e ".[dev]"

# Run tests
uv run pytest                          # All tests
uv run pytest tests/test_policies.py   # Specific test file
uv run pytest -k test_ttl              # Tests matching pattern
uv run pytest -v                       # Verbose output
uv run pytest --cov=src/pacer          # With coverage

# Linting and formatting
uv run ruff check .                    # Check linting issues
uv run ruff check . --fix              # Auto-fix linting issues
uv run ruff format src tests           # Format code

# Type checking
uv run mypy src                        # MyPy type checking
uvx ty check .                         # Alternative type checker (pyright)

# Run example app
uvicorn examples.simple_app:app --reload
```

### Docker and Integration Testing

```bash
# Start Redis for testing
docker run -d -p 6379:6379 redis:latest

# Run integration test services (6 configurations on ports 8001-8006)
docker-compose -f examples/docker-compose.test.yml up -d

# Run integration tests
uv run pytest tests/test_integration.py -v

# Clean up
docker-compose -f examples/docker-compose.test.yml down
```

## Architecture

### Core Algorithm: GCRA (Generic Cell Rate Algorithm)

The rate limiter uses GCRA implemented as a Lua script (`src/pacer/lua/gcra.lua`) executed atomically in Redis. Key concepts:
- **TAT (Theoretical Arrival Time)**: Tracks when the next request would be allowed at steady state
- **Emission Interval**: Time between permitted requests (period/permits)
- **Burst Capacity**: Additional capacity for burst requests beyond steady rate
- **Single Redis RTT**: All decisions made atomically in one round trip

### Component Architecture

```
Request Flow:
1. Request → Identity Extractor (IP/API Key/User ID)
2. → Limiter.check_rate_limit() 
3. → RedisStorage.check_rate_limit()
4. → Lua script execution (atomic)
5. → Allow/Deny decision with headers
```

**Key Components:**
- `policies.py`: Rate policy with duration parsing, key generation with Redis cluster hash tags
- `storage.py`: Redis connection management, EVALSHA execution with automatic script reloading
- `limiter.py`: Core orchestrator managing policies, storage, metrics, fail-open/closed behavior
- `extractors.py`: Identity extraction strategies (IP with proxy support, API key, user ID)
- `middleware.py`: ASGI middleware for global rate limiting
- `dependencies.py`: FastAPI dependency injection for per-route limits

### Integration Points

Two ways to integrate with FastAPI:
1. **Middleware** (global): `app.add_middleware(LimiterMiddleware, limiter=limiter, policy=rate)`
2. **Dependency** (per-route): `@app.get("/", dependencies=[Depends(limit(rate))])`

### Redis Key Structure

Keys use format: `{prefix}:{scope}:{hash_tag}:{identity}`
- Hash tags `{...}` ensure cluster compatibility
- TTL auto-expires keys after `2 * period` or `period + burst_capacity`

### Fail Modes

- **Fail-open** (default): Allow requests on Redis errors
- **Fail-closed**: Deny requests on Redis errors (return 503)

## Testing Strategy

- **Unit tests**: Algorithm logic, policy parsing, extractors
- **Integration tests**: 6 service configurations testing different rate limits
- **Property-based tests**: Hypothesis state machines for GCRA invariants
- **TTL tests**: Verify Redis key expiration and refresh

## Important Implementation Details

1. **Redis Commands**: All integers must be converted to strings for Redis commands
2. **Type Annotations**: Use `Callable[..., Any]` not bare `callable`
3. **Mutable Defaults**: Use `dict | None = None` not `dict = {}`
4. **Redis Close**: Use `aclose()` not deprecated `close()`
5. **Response Format**: 429 responses have nested detail: `{"detail": {"detail": "rate_limited", "retry_after_ms": 5000}}`
6. **Immutable Policies**: Rate objects use `@dataclass(frozen=True)`

## Common Issues and Solutions

1. **NoScriptError**: Script automatically reloads if not in Redis cache
2. **Cluster Mode**: Keys use hash tags for proper slot distribution
3. **Test Failures**: Ensure Redis is running on localhost:6379
4. **Import Sorting**: Let ruff auto-fix with `uv run ruff check . --fix`