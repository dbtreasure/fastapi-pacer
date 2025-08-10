# FastAPI Pacer v0.1.1 Improvements Plan

Based on the review assessment, here's the implementation plan for high-leverage improvements while maintaining grug-brain simplicity.

## Priority 1: Quick Fixes (High ROI, Low Complexity)

### 1. Fix RateLimit-Reset Header Semantics ⚡
**Issue:** Currently emitting Unix timestamp, spec expects delta-seconds
**Fix:** 
- Change `RateLimit-Reset` to delta-seconds from now
- Add `X-RateLimit-Reset` with Unix timestamp (optional, for compatibility)
- Keep `Retry-After` as-is for 429 responses

**Files to modify:**
- `src/pacer/limiter.py` - Update `add_headers()` method
- Tests to verify new header format

**Complexity:** Simple - just math change in one place

### 2. Add OTel Hook Points 🔌
**Issue:** No extensibility for observability
**Fix:** Add simple callback hooks that default to no-op
```python
# Simple hook interface
def on_rate_limit_decision(request, key, result, timing_ms):
    pass  # no-op by default

# In Limiter.__init__
self.on_decision = on_rate_limit_decision
```

**Files to modify:**
- `src/pacer/limiter.py` - Add hook points
- Document hooks in README

**Complexity:** Simple - just callback pattern, no dependencies

### 3. Create Benchmark Script 📊 ✅ DONE
**Issue:** Performance claims without reproducible evidence
**Fix:** Created `scripts/bench.sh` that:
- Starts uvicorn with N workers
- Runs `hey` against both unlimited and rate-limited endpoints
- Reports p50/p90/p99 latencies and overhead
- Calculates overhead in microseconds and milliseconds

**Reality Check:** 
- Initial claim of <150μs was unrealistic for network operations
- Actual overhead: 1-7ms P99 (depends on Redis latency)
- This is normal and expected for Redis network RTT
- Updated documentation to reflect realistic performance

## Priority 2: Documentation Improvements

### 4. Document Remaining Capacity Calculation 📝
**Issue:** Unclear what "remaining" means in GCRA context
**Fix:** Add clear documentation explaining:
- Post-decision remaining capacity
- How it's derived from TAT and burst window
- Include examples with timeline

**Files to modify:**
- `docs/ALGORITHM.md` (new) - Detailed GCRA explanation
- Update docstrings in `lua/gcra.lua`

### 5. Document TTL Math 🧮
**Issue:** TTL calculation reasoning not explained
**Fix:** Document why `TTL >= max(period + burst_capacity, 2 * period)`
- Ensures keys persist through burst window
- Prevents premature expiration during recovery

**Files to modify:**
- Add to `docs/ALGORITHM.md`
- Update `policies.py` docstring

### 6. Add GCRA Diagram 📈
**Issue:** Text-only explanation of complex algorithm
**Fix:** Create ASCII or simple SVG diagram showing:
- TAT timeline
- Burst window
- Allow/deny decision points

## Priority 3: Nice-to-Have Enhancements

### 7. Add RateLimit-Policy Header 🏷️
**Issue:** No echo of active policy for debugging
**Fix:** Optional header showing `permits;w=period;burst=N`
```
X-RateLimit-Policy: 100;w=60s;burst=10
```

**Files to modify:**
- `src/pacer/limiter.py` - Add to `add_headers()` if flag enabled
- Make it opt-in via config flag

**Complexity:** Simple, optional feature

### 8. Document Proxy Security Considerations 🔒
**Issue:** Proxy parsing is permissive
**Fix:** Add security documentation:
- "First public IP" rule explanation
- Option for fixed hop count in zero-trust environments
- Example configurations for common scenarios

**Files to modify:**
- `docs/SECURITY.md` (new)
- Update extractors.py docstrings

## Implementation Order

### Phase 1: Quick Fixes (1-2 hours)
1. ✅ Fix RateLimit-Reset header (15 min)
2. ✅ Add OTel hooks (30 min)
3. ✅ Create benchmark script (45 min)

### Phase 2: Documentation (2-3 hours)
4. ✅ Document remaining calculation
5. ✅ Document TTL math
6. ✅ Add GCRA diagram

### Phase 3: Enhancements (1 hour)
7. ✅ Add optional Policy header
8. ✅ Document proxy security

## Grug-Brain Principles to Maintain

- **No new dependencies** - OTel hooks are just callbacks
- **Keep it optional** - New features are opt-in
- **Simple > clever** - Header math stays readable
- **Document the why** - Explain decisions, not just code
- **Benchmark = truth** - Prove performance with numbers

## Success Criteria

- [ ] All existing tests pass
- [ ] New headers match spec semantics
- [ ] Benchmark shows <150μs p99 overhead
- [ ] Hooks allow future OTel without changes
- [ ] Documentation explains all non-obvious decisions

## Non-Goals (Complexity to Avoid)

- ❌ Don't add OTel dependency now
- ❌ Don't over-engineer hook system
- ❌ Don't change core algorithm
- ❌ Don't break backward compatibility

## Estimated Timeline

- **Phase 1:** Ship immediately (high ROI)
- **Phase 2:** Ship within a day
- **Phase 3:** Ship when convenient

This plan maintains simplicity while addressing all review feedback. Each change is isolated and testable.