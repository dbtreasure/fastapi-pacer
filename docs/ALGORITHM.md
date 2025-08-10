# GCRA Algorithm Documentation

## Overview

FastAPI Pacer uses the **Generic Cell Rate Algorithm (GCRA)**, a sophisticated rate limiting algorithm that provides smooth traffic shaping with burst support. Unlike token bucket algorithms, GCRA uses a single state variable (TAT - Theoretical Arrival Time) to make decisions, enabling atomic operations in distributed systems.

## Core Concepts

### TAT (Theoretical Arrival Time)
The TAT represents when the next request would theoretically arrive if requests came at the exact allowed rate. It's the key to GCRA's elegance - all decisions are based on this single timestamp.

### Emission Interval (T)
The time between requests at the allowed rate: `T = period / permits`
- Example: 10 requests per second → T = 1000ms / 10 = 100ms

### Burst Capacity (τ)
Additional capacity beyond the steady rate: `τ = burst * T`
- Example: burst=5 with T=100ms → τ = 500ms

## The Algorithm

```
When request arrives at time `now`:
1. If TAT doesn't exist: TAT = now (first request)
2. Check if: now >= TAT - τ (can we accept?)
3. If YES: 
   - Allow request
   - Update: TAT = max(TAT, now) + T
4. If NO:
   - Deny request
   - retry_after = TAT - τ - now
```

## Visual Timeline

```
Time →  0    100   200   300   400   500   600   700   800   900  1000
        ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
        
Rate: 5 req/sec (T=200ms), Burst: 2 (τ=400ms)

Request at t=0:
  TAT = 0 (first request)
  Allow? 0 >= 0 - 400 → YES ✓
  New TAT = max(0, 0) + 200 = 200
  
        [R1]
        ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
        ↑                   
       t=0              TAT=200

Request at t=50 (burst):
  Allow? 50 >= 200 - 400 → 50 >= -200 → YES ✓
  New TAT = max(200, 50) + 200 = 400
  
        [R1]  [R2]
        ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
        ↑     ↑                         
       t=0   t=50                   TAT=400

Request at t=100 (burst):
  Allow? 100 >= 400 - 400 → 100 >= 0 → YES ✓
  New TAT = max(400, 100) + 200 = 600
  
        [R1]  [R2]  [R3]
        ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
        ↑     ↑     ↑                               
       t=0   t=50  t=100                        TAT=600

Request at t=150 (exceeds burst):
  Allow? 150 >= 600 - 400 → 150 >= 200 → NO ✗
  retry_after = 200 - 150 = 50ms
  
        [R1]  [R2]  [R3]  [X]
        ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
        ↑     ↑     ↑     ↑                         
       t=0   t=50  t=100 t=150                  TAT=600 (unchanged)
                          ↑
                    Must wait until t=200
```

## Remaining Capacity Calculation

The "remaining" capacity tells clients how many more requests they can make immediately without being rate limited.

### Calculation Method

```lua
-- After request is allowed:
burst_available = burst_capacity - (new_tat - now)
remaining = max(0, floor(burst_available / emission_interval))
```

### Example

```
After allowing request at t=100:
- new_tat = 600
- burst_capacity = 400
- emission_interval = 200

burst_available = 400 - (600 - 100) = 400 - 500 = -100
remaining = max(0, floor(-100 / 200)) = 0

This correctly shows no immediate capacity remaining.
```

### Important Notes

- **Post-decision calculation**: The remaining count is calculated AFTER the current request is processed
- **Approximate value**: Due to concurrent requests, this is an estimate
- **Burst-aware**: Accounts for both steady-state and burst capacity

## TTL (Time To Live) Calculation

Redis keys need appropriate TTLs to:
1. Clean up after inactive clients
2. Persist long enough for burst recovery
3. Minimize memory usage

### TTL Formula

```python
TTL = max(period + burst_capacity, 2 * period)
```

### Why This Formula?

**Case 1: With burst (burst > 0)**
- A client using full burst needs `period + burst_capacity` time to recover
- Example: period=1s, burst=5, T=100ms
  - Burst capacity = 500ms
  - TTL = max(1000 + 500, 2000) = 2000ms
  - This ensures the key persists through the full recovery window

**Case 2: No burst (burst = 0)**
- TTL = 2 * period ensures keys don't expire between regular requests
- Provides buffer for network latency and processing delays

**Case 3: Large burst**
- If burst_capacity > period, the first term dominates
- Ensures keys persist long enough for burst recovery

### TTL Refresh

The TTL is refreshed on every request (allowed or denied) to keep the key alive for active clients while letting inactive clients' keys expire.

## Why GCRA Over Token Bucket?

### Advantages of GCRA

1. **Single State Variable**
   - Token bucket needs: tokens + last_refill_time
   - GCRA needs only: TAT
   - Simpler atomic operations in Redis

2. **No Floating Point Math**
   - Token bucket: Fractional tokens during refill
   - GCRA: Integer milliseconds only

3. **Predictable Behavior**
   - Smooth traffic shaping
   - No token accumulation issues
   - Clear burst boundaries

4. **Better for Distributed Systems**
   - Single Redis key update
   - Atomic EVALSHA operation
   - No read-modify-write races

### Comparison Table

| Aspect | Token Bucket | GCRA |
|--------|--------------|------|
| State Variables | 2 (tokens, timestamp) | 1 (TAT) |
| Math Complexity | Floating point | Integer only |
| Redis Operations | Multiple | Single EVALSHA |
| Burst Handling | Token accumulation | Clean burst window |
| Recovery Time | Calculate tokens | Simple TAT check |
| Atomicity | Harder | Natural |

### When to Use GCRA

GCRA is ideal when:
- You need distributed rate limiting
- Atomic operations are important
- You want predictable burst behavior
- Simplicity matters

### Trade-offs

- GCRA assumes uniform "cell" sizes (all requests equal weight)
- Token bucket can handle variable costs per request
- For weighted requests, token bucket may be more suitable

## Implementation Details

### Lua Script Atomicity

The entire GCRA decision happens in a single Lua script:
```lua
-- All these operations are atomic:
1. Read current TAT
2. Calculate decision
3. Update TAT if allowed
4. Set TTL
5. Return result
```

This eliminates race conditions that plague naive implementations.

### Precision Considerations

- All times in milliseconds (integer)
- Minimum emission interval: 1ms
- Maximum reasonable period: days
- Overflow protection: TAT capped at reasonable future time

### Edge Cases Handled

1. **First Request**: TAT initialized to current time
2. **Clock Skew**: max(TAT, now) prevents TAT regression
3. **Idle Recovery**: Old TAT naturally allows burst after idle
4. **Key Expiration**: TTL ensures cleanup
5. **Concurrent Requests**: Atomic script prevents races

## Performance Characteristics

- **Time Complexity**: O(1) - Single Redis operation
- **Space Complexity**: O(N) - One key per identity
- **Network Calls**: 1 RTT per decision
- **CPU Usage**: Minimal (simple integer math)

## Summary

GCRA provides an elegant solution for distributed rate limiting with:
- Single state variable (TAT)
- Atomic operations via Lua
- Predictable burst behavior
- Automatic cleanup via TTL
- Production-proven algorithm (used in ATM networks)

The algorithm's simplicity and atomicity make it ideal for high-performance distributed systems where consistency and predictability are crucial.