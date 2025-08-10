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
if ! command -v uvicorn &> /dev/null; then
    echo -e "${RED}Error: uvicorn not found. Install with: pip install uvicorn${NC}"
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
if ! redis-cli -u "$REDIS_URL" ping > /dev/null 2>&1; then
    echo -e "${RED}Error: Cannot connect to Redis at $REDIS_URL${NC}"
    echo "Start Redis with: docker run -d -p 6379:6379 redis:latest"
    exit 1
fi
echo -e "${GREEN}✓ Redis connected${NC}"
echo ""

# Kill any existing uvicorn processes
pkill -f "uvicorn.*bench_app" 2>/dev/null || true
sleep 1

# Start the benchmark app
echo -e "${YELLOW}Starting benchmark app with $WORKERS workers...${NC}"
cd "$(dirname "$0")"
REDIS_URL="$REDIS_URL" uvicorn bench_app:app \
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
        
        # Get percentiles
        p50=$(echo "$result" | grep "50%" | awk '{print $2}')
        p90=$(echo "$result" | grep "90%" | awk '{print $2}')
        p99=$(echo "$result" | grep "99%" | awk '{print $2}')
        
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
    # Convert to microseconds for calculation
    baseline_us=$(echo "$BASELINE_P99" | sed 's/s$//' | awk '{print $1 * 1000000}')
    limited_us=$(echo "$LIMITED_P99" | sed 's/s$//' | awk '{print $1 * 1000000}')
    overhead_us=$(echo "$limited_us - $baseline_us" | bc 2>/dev/null || echo "N/A")
    
    if [ "$overhead_us" != "N/A" ]; then
        echo "P99 Overhead: ${overhead_us}μs"
        
        # Check if we meet the <150μs target
        if (( $(echo "$overhead_us < 150" | bc -l) )); then
            echo -e "${GREEN}✓ Meets <150μs P99 target${NC}"
        else
            echo -e "${YELLOW}⚠ Exceeds 150μs P99 target${NC}"
        fi
    fi
fi

# Get Redis stats
echo ""
echo -e "${GREEN}Redis Operations${NC}"
echo "------------------------"
redis-cli -u "$REDIS_URL" --stat -i 1 -r 3 2>/dev/null | tail -3 || true

# Cleanup
echo ""
echo -e "${YELLOW}Cleaning up...${NC}"
kill $APP_PID 2>/dev/null || true
wait $APP_PID 2>/dev/null || true

echo -e "${GREEN}✓ Benchmark complete${NC}"