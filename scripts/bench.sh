#!/bin/bash
# Benchmark script for FastAPI Pacer
# Measures rate limiter overhead by comparing unlimited vs limited endpoints

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
WORKERS=${WORKERS:-2}
PORT=${PORT:-8000}
DURATION=${DURATION:-30}
CONNECTIONS=${CONNECTIONS:-50}
QPS=${QPS:-1000}
REDIS_URL=${REDIS_URL:-"redis://localhost:6379"}

echo -e "${GREEN}FastAPI Pacer Benchmark${NC}"
echo "================================"
echo "Workers: $WORKERS"
echo "Duration: ${DURATION}s"
echo "Connections: $CONNECTIONS"
echo "Target QPS: $QPS"
echo "Redis URL: $REDIS_URL"
echo ""

# Check dependencies
if ! command -v uv &> /dev/null; then
    echo -e "${RED}Error: uv not found. Install from: https://github.com/astral-sh/uv${NC}"
    exit 1
fi

# Check if uvicorn is available in the project
if ! uv run python -c "import uvicorn" 2>/dev/null; then
    echo -e "${RED}Error: uvicorn not found in project. Install with: uv pip install uvicorn${NC}"
    exit 1
fi

if ! command -v hey &> /dev/null && ! command -v wrk &> /dev/null; then
    echo -e "${YELLOW}Installing hey for benchmarking...${NC}"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install hey 2>/dev/null || {
            echo -e "${RED}Error: Could not install hey. Install manually: brew install hey${NC}"
            exit 1
        }
    else
        go install github.com/rakyll/hey@latest 2>/dev/null || {
            echo -e "${RED}Error: Could not install hey. Install Go and run: go install github.com/rakyll/hey@latest${NC}"
            exit 1
        }
    fi
fi

# Check Redis connectivity
echo -e "${YELLOW}Checking Redis connection...${NC}"
# Try different methods to check Redis
if command -v redis-cli &> /dev/null; then
    if ! redis-cli -u "$REDIS_URL" ping > /dev/null 2>&1; then
        echo -e "${RED}Error: Cannot connect to Redis at $REDIS_URL${NC}"
        echo "Start Redis with: docker run -d -p 6379:6379 redis:latest"
        exit 1
    fi
elif command -v nc &> /dev/null; then
    # Use netcat to check if port is open
    REDIS_HOST=$(echo "$REDIS_URL" | sed 's|redis://||' | cut -d: -f1)
    REDIS_PORT=$(echo "$REDIS_URL" | sed 's|redis://||' | cut -d: -f2 | cut -d/ -f1)
    if ! nc -z "$REDIS_HOST" "$REDIS_PORT" 2>/dev/null; then
        echo -e "${RED}Error: Cannot connect to Redis at $REDIS_HOST:$REDIS_PORT${NC}"
        echo "Start Redis with: docker run -d -p 6379:6379 redis:latest"
        exit 1
    fi
elif command -v uv &> /dev/null; then
    # Use uv run python to check Redis (uses project environment)
    if ! uv run python -c "import redis; r = redis.from_url('$REDIS_URL'); r.ping()" 2>/dev/null; then
        echo -e "${RED}Error: Cannot connect to Redis at $REDIS_URL${NC}"
        echo "Start Redis with: docker run -d -p 6379:6379 redis:latest"
        exit 1
    fi
else
    echo -e "${YELLOW}Warning: Cannot verify Redis connection (redis-cli not installed)${NC}"
    echo "Proceeding anyway..."
fi
echo -e "${GREEN}✓ Redis connected${NC}"
echo ""

# Kill any existing uvicorn processes
pkill -f "uvicorn.*bench_app" 2>/dev/null || true
sleep 1

# Start the benchmark app
echo -e "${YELLOW}Starting benchmark app with $WORKERS workers...${NC}"
cd "$(dirname "$0")/.."  # Go to project root
REDIS_URL="$REDIS_URL" uv run uvicorn scripts.bench_app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers "$WORKERS" \
    --log-level error \
    --no-access-log &

APP_PID=$!
sleep 3

# Wait for app to be ready
echo -e "${YELLOW}Waiting for app to start...${NC}"
for i in {1..10}; do
    if curl -s "http://localhost:$PORT/health" > /dev/null; then
        echo -e "${GREEN}✓ App ready${NC}"
        break
    fi
    sleep 1
done

# Warm up
echo -e "${YELLOW}Warming up...${NC}"
hey -q "$QPS" -c 10 -z 5s "http://localhost:$PORT/limited" > /dev/null 2>&1 || true
echo ""

# Function to run benchmark and extract stats
run_benchmark() {
    local endpoint=$1
    local label=$2
    
    echo -e "${GREEN}Benchmarking $label${NC}"
    echo "------------------------"
    
    if command -v hey &> /dev/null; then
        # Use hey
        result=$(hey -q "$QPS" -c "$CONNECTIONS" -z "${DURATION}s" "http://localhost:$PORT/$endpoint" 2>&1)
        
        # Extract metrics
        echo "$result" | grep -E "Requests/sec:|Latencies \[mean"
        
        # Get percentiles (hey outputs them in a specific format)
        p50=$(echo "$result" | grep "50% in" | awk '{print $3, $4}')
        p90=$(echo "$result" | grep "90% in" | awk '{print $3, $4}')
        p99=$(echo "$result" | grep "99% in" | awk '{print $3, $4}')
        
        echo "Latencies:"
        echo "  P50: $p50"
        echo "  P90: $p90"
        echo "  P99: $p99"
        
        # Extract for comparison
        if [[ $endpoint == "unlimited" ]]; then
            BASELINE_P50=$p50
            BASELINE_P90=$p90
            BASELINE_P99=$p99
        else
            LIMITED_P50=$p50
            LIMITED_P90=$p90
            LIMITED_P99=$p99
        fi
    else
        # Fallback to wrk if available
        wrk -t "$WORKERS" -c "$CONNECTIONS" -d "${DURATION}s" "http://localhost:$PORT/$endpoint"
    fi
    
    echo ""
}

# Run benchmarks
echo -e "${GREEN}Starting benchmarks...${NC}"
echo "================================"
echo ""

# Baseline (no rate limiting)
run_benchmark "unlimited" "Baseline (no rate limiting)"

# With rate limiting
run_benchmark "limited" "With rate limiting"

# Calculate overhead
echo -e "${GREEN}Rate Limiter Overhead${NC}"
echo "================================"

if command -v hey &> /dev/null && [ -n "$BASELINE_P99" ] && [ -n "$LIMITED_P99" ]; then
    # Extract numeric value from "X.XXXX secs" format
    baseline_val=$(echo "$BASELINE_P99" | awk '{print $1}')
    limited_val=$(echo "$LIMITED_P99" | awk '{print $1}')
    
    # Convert to microseconds for calculation
    if [ -n "$baseline_val" ] && [ -n "$limited_val" ]; then
        baseline_us=$(echo "$baseline_val * 1000000" | bc 2>/dev/null || echo "0")
        limited_us=$(echo "$limited_val * 1000000" | bc 2>/dev/null || echo "0")
        overhead_us=$(echo "$limited_us - $baseline_us" | bc 2>/dev/null || echo "N/A")
    
        if [ "$overhead_us" != "N/A" ]; then
            echo "P99 Overhead: ${overhead_us}μs"
            
            # Check if overhead is reasonable (< 10ms is good for network RTT)
            if (( $(echo "$overhead_us < 10000" | bc -l) )); then
                echo -e "${GREEN}✓ Reasonable overhead for Redis network RTT${NC}"
            else
                echo -e "${YELLOW}⚠ Higher than expected overhead (>10ms)${NC}"
            fi
            
            # Show overhead in milliseconds for clarity
            overhead_ms=$(echo "scale=2; $overhead_us / 1000" | bc)
            echo "P99 Overhead: ${overhead_ms}ms"
        fi
    fi
fi

# Get Redis stats (if redis-cli available)
if command -v redis-cli &> /dev/null; then
    echo ""
    echo -e "${GREEN}Redis Operations${NC}"
    echo "------------------------"
    redis-cli -u "$REDIS_URL" --stat -i 1 -r 3 2>/dev/null | tail -3 || true
fi

# Cleanup
echo ""
echo -e "${YELLOW}Cleaning up...${NC}"
kill $APP_PID 2>/dev/null || true
wait $APP_PID 2>/dev/null || true

echo -e "${GREEN}✓ Benchmark complete${NC}"