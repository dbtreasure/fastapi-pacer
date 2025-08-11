# FastAPI Pacer

A production-ready, high-performance rate limiter for FastAPI applications using the GCRA (Generic Cell Rate Algorithm) and Redis.

## Features

- **GCRA Algorithm**: Smooth rate limiting with configurable burst capacity
- **Multi-Rate Policies**: Apply multiple rate limits simultaneously (v0.2.0+)
- **FastAPI Native**: Drop-in middleware and dependency injection support
- **Redis Backend**: Distributed rate limiting across multiple instances
- **Atomic Operations**: Single Redis RTT per decision using Lua scripting
- **Flexible Identity Extraction**: IP-based, API key, user ID, or custom selectors
- **Standard Headers**: RFC 6585 compliant with `429 Too Many Requests`
- **Resilience**: Configurable fail-open/fail-closed behavior
- **High Performance**: 1-7ms P99 overhead (benchmarked with Redis RTT)
- **Type Safe**: Full type hints with mypy and pyright support
- **Well Tested**: 72+ tests including property-based testing with Hypothesis
- **OpenTelemetry**: Built-in observability with metrics and tracing (v0.2.0+)

## Installation

Using [uv](https://github.com/astral-sh/uv):
```bash
uv add fastapi-pacer
```

Using pip:
```bash
pip install fastapi-pacer
```

## Quick Start

```python
from fastapi import FastAPI, Depends
from pacer import Limiter, LimiterMiddleware, Policy, Rate, limit

# Initialize limiter
limiter = Limiter(
    redis_url="redis://localhost:6379",
)

app = FastAPI()

# Add global middleware with a policy
app.add_middleware(
    LimiterMiddleware,
    limiter=limiter,
    policy=Policy(
        rates=[Rate(100, "1m")],
        key="ip",  # Rate limit by IP address
        name="global"
    ),
)

# Per-route rate limiting with burst
@app.get("/api/items", dependencies=[Depends(limit(
    Policy(
        rates=[Rate(100, "1m", burst=50)],
        key="ip",
        name="items_endpoint"
    )
))])
async def get_items():
    return {"items": []}
```

## Configuration

### Rate Policies (v0.2.0+)

```python
from pacer import Policy, Rate

# Single rate policy
policy = Policy(
    rates=[Rate(10, "1s")],
    key="ip",  # Rate limit by IP address
    name="basic"
)

# Multi-rate policy (all rates must pass)
policy = Policy(
    rates=[
        Rate(1000, "1h"),  # 1000 per hour
        Rate(100, "1m"),   # 100 per minute  
        Rate(10, "1s"),    # 10 per second
    ],
    key="api_key",  # Rate limit by API key
    name="tiered"
)

# With burst capacity
policy = Policy(
    rates=[Rate(100, "1m", burst=20)],
    key="user",
    name="burst_enabled"
)

# Supported time units: "1s", "10s", "1m", "5m", "1h", "1d"
```

### Identity Extraction (v0.2.0+)

```python
from pacer import Policy, Rate, compose, key_ip, key_user

# Built-in selectors
policy = Policy(
    rates=[Rate(100, "1m")],
    key="ip",  # Built-in: "ip", "api_key", "user", "org"
    name="by_ip"
)

# Composed selectors (combine multiple identities)
policy = Policy(
    rates=[Rate(50, "1m")],
    key=compose(key_user, key_ip),  # Rate limit by user AND IP
    name="user_ip_combo"
)

# Custom selector function
def custom_selector(request):
    return request.headers.get("X-Tenant-ID", "default")

policy = Policy(
    rates=[Rate(200, "1m")],
    key=custom_selector,
    name="by_tenant"
)
```

### Storage Backends

For most applications, the default simple Redis storage is recommended:

```python
from pacer import Limiter

# Simple Redis (recommended)
limiter = Limiter(
    redis_url="redis://localhost:6379",
    cluster_mode=False  # default
)
```

For Redis Cluster deployments only:

```python
# Redis Cluster (adds complexity)
limiter = Limiter(
    redis_url="redis://cluster-node:6379",
    cluster_mode=True  # enables cluster support
)
```

### Fail Modes

```python
# Fail-open (default): Allow requests on Redis errors
limiter = Limiter(fail_mode="open")

# Fail-closed: Deny requests on Redis errors
limiter = Limiter(fail_mode="closed")
```

## Response Headers and Error Format

### Rate Limit Headers

All responses include rate limit headers (when `expose_headers=True`):

**Note**: Optional headers use the `X-` prefix to indicate they are non-standard debug/compatibility headers.

```http
HTTP/1.1 200 OK
RateLimit-Limit: 100
RateLimit-Remaining: 99
RateLimit-Reset: 60
```

- `RateLimit-Limit`: Maximum requests allowed in the time window
- `RateLimit-Remaining`: Requests remaining in current window
- `RateLimit-Reset`: Seconds until the limit resets (delta-seconds per spec)
- `X-RateLimit-Reset`: Unix timestamp of reset time (optional, with `legacy_timestamp_header=True`)
- `X-RateLimit-Policy`: Active rate limit policy for debugging (optional, with `expose_policy_header=True`)

### 429 Rate Limited Response

When rate limited, the response includes:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 5
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 60
Content-Type: application/json

{
  "detail": "rate_limited",
  "retry_after_ms": 5000
}
```

### 503 Service Unavailable (Fail-Closed Mode)

When Redis is unavailable and `fail_mode="closed"`:

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 5
Content-Type: application/json

{
  "detail": "Service temporarily unavailable"
}
```

## What's New in v0.2.0

### Multi-Rate Policies
Apply multiple rate limits that all must pass:
```python
policy = Policy(
    rates=[
        Rate(1000, "1h"),  # Hourly limit
        Rate(100, "1m"),   # Minute limit (prevents bursts)
        Rate(10, "1s"),    # Second limit (smooths traffic)
    ],
    key="api_key",
    name="tiered_limits"
)
```

### Flexible Selectors
Choose how to identify clients:
```python
# Built-in selectors
Policy(rates=[...], key="ip")       # By IP address
Policy(rates=[...], key="api_key")  # By API key
Policy(rates=[...], key="user")     # By user ID
Policy(rates=[...], key="org")      # By organization

# Compose multiple selectors
from pacer import compose, key_user, key_ip
Policy(rates=[...], key=compose(key_user, key_ip))
```

### OpenTelemetry Integration
```python
from pacer import create_otel_hooks

on_decision, on_error = create_otel_hooks("my-service")
limiter = Limiter(
    redis_url="redis://localhost:6379",
    on_decision=on_decision,
    on_error=on_error,
)
```

## Advanced Usage

### Multiple Rate Limits

Apply different limits to different endpoints:

```python
# Public endpoint with higher limits
@app.get("/public", dependencies=[Depends(limit(
    Policy(rates=[Rate(1000, "1h")], key="ip", name="public")
))])
async def public_endpoint():
    return {"access": "public"}

# Private endpoint with strict limits
@app.post("/private", dependencies=[Depends(limit(
    Policy(
        rates=[Rate(10, "1m", burst=0)],
        key="api_key",
        name="private"
    )
))])
async def private_endpoint():
    return {"access": "restricted"}
```

### Exclude Paths

```python
app.add_middleware(
    LimiterMiddleware,
    limiter=limiter,
    exclude_paths=["/health", "/metrics", "/docs*"],
    exclude_methods=["OPTIONS", "HEAD"],
)
```

### Redis Cluster

```python
limiter = Limiter(
    redis_url="redis://cluster-endpoint:6379",
    cluster_mode=True,
)
```

## GCRA Algorithm

The Generic Cell Rate Algorithm provides smooth rate limiting with burst support:

- **Emission Interval (T)**: Time between permitted requests (`period/permits`)
- **Burst Capacity (τ)**: Additional capacity for burst requests
- **TAT (Theoretical Arrival Time)**: Next time a request would be allowed at steady state

Benefits over token bucket:
- No background token refill process needed
- O(1) memory per key
- Precise control over burst behavior
- Natural decay of burst capacity over time

For a detailed explanation with visual timeline, see [docs/ALGORITHM.md](docs/ALGORITHM.md).

## Architecture

```
Request → Identity Extractor → GCRA Check (Redis+Lua) → Allow/Deny
                                    ↓
                              Single RTT
                              Atomic Decision
```

## Development

### Setup

Using [uv](https://github.com/astral-sh/uv) (recommended):
```bash
# Clone the repository
git clone https://github.com/dan/fastapi-pacer
cd fastapi-pacer

# Install in development mode with all dependencies
uv pip install -e ".[dev]"

# Or using uv sync (if you have a uv.lock file)
uv sync
```

Using pip:
```bash
# Install dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Start Redis for integration tests
docker run -d -p 6379:6379 redis:latest

# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test files
uv run pytest tests/test_policy.py

# Run with coverage
uv run pytest --cov=src/pacer --cov-report=term-missing

# Run type checking
uv run mypy src
uvx ty check .  # Alternative type checker

# Format code
uv run ruff format src tests

# Lint code
uv run ruff check src tests
```

### Integration Testing

```bash
# Start all test services (6 different configurations)
docker-compose -f examples/docker-compose.test.yml up -d

# Run integration tests
uv run pytest tests/test_integration.py -v

# Test individual service configurations
curl http://localhost:8001/config  # basic: 10/min, burst=5
curl http://localhost:8002/config  # strict: 5/min, no burst
curl http://localhost:8003/config  # burst: 10/10s, burst=5
curl http://localhost:8004/config  # highvolume: 1000/min, burst=100
curl http://localhost:8005/config  # middleware: global + per-route
curl http://localhost:8006/config  # fast: 100/1s, burst=10

# Clean up
docker-compose -f examples/docker-compose.test.yml down
```

### Performance Benchmarking

Run the included benchmark script to measure rate limiter overhead:

```bash
# Start Redis
docker run -d -p 6379:6379 redis:latest

# Run benchmark (measures overhead vs baseline)
./scripts/bench.sh

# Sample output:
# Baseline P99: 0.0234 secs
# With rate limiting P99: 0.0289 secs
# Overhead: 5.5ms
```

The benchmark uses [hey](https://github.com/rakyll/hey) to measure:
- Baseline latency (no rate limiting)
- Rate-limited latency
- Calculates overhead in milliseconds

Typical overhead: 1-7ms P99 (dominated by Redis network RTT)

### Docker Development

```bash
# Run the example app with Redis
docker-compose -f examples/docker-compose.yml up

# Access the app at http://localhost:8000
# API docs at http://localhost:8000/docs
```

## Documentation

- [Algorithm Details](docs/ALGORITHM.md) - Deep dive into GCRA with visual timeline
- [Security Guide](docs/SECURITY.md) - Production security considerations
- [Performance Analysis](docs/PERFORMANCE_ANALYSIS.md) - Benchmark results and industry comparison
- [Complexity Audit](COMPLEXITY_AUDIT.md) - Grug-brain simplification decisions
- [Improvements Plan](IMPROVEMENTS_PLAN.md) - Roadmap for enhancements

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions welcome! Please read our contributing guidelines and submit PRs to our GitHub repository.

## Roadmap

- v0.1: GCRA, middleware, dependency injection ✅
- v0.2: Multiple rates per policy, flexible selectors, OpenTelemetry (current)
- v0.3: Concurrency limiting, WebSocket support
- v0.4: Policy DSL with hot-reload
- v0.5: Token leasing, quotas, admin endpoints

## Support

- Documentation: https://github.com/dan/fastapi-pacer
- Issues: https://github.com/dan/fastapi-pacer/issues
- Discussions: https://github.com/dan/fastapi-pacer/discussions
