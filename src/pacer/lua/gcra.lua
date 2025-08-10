-- GCRA (Generic Cell Rate Algorithm) implementation for rate limiting
-- Keys: rate_key
-- Args: emission_interval_ms, burst_capacity_ms, now_ms, ttl_ms
-- Returns: {allowed (0|1), retry_after_ms, reset_ms, remaining_estimate}

local rate_key = KEYS[1]
local emission_interval = tonumber(ARGV[1])
local burst_capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

-- Get current TAT (Theoretical Arrival Time)
local tat = redis.call('GET', rate_key)
if tat then
    tat = tonumber(tat)
else
    -- First request, initialize TAT
    tat = now
end

-- Calculate the next allowed time
-- Allow request if: now >= TAT - burst_capacity
local allow_at = tat - burst_capacity

-- Decision logic
local allowed = 0
local new_tat = tat
local retry_after = 0

if now >= allow_at then
    -- Request is allowed
    allowed = 1
    -- Update TAT: max(TAT, now) + emission_interval
    new_tat = math.max(tat, now) + emission_interval
    -- Set new TAT with TTL
    redis.call('SET', rate_key, new_tat, 'PX', ttl)
else
    -- Request is denied
    retry_after = math.ceil(allow_at - now)
end

-- Calculate remaining capacity estimate
-- This is approximate: how many requests could be made right now
local remaining = 0
if allowed == 1 then
    -- After this request, how much burst is left?
    local burst_available = burst_capacity - (new_tat - now)
    remaining = math.max(0, math.floor(burst_available / emission_interval))
else
    remaining = 0
end

-- Calculate reset time (when full burst will be available again)
local reset_at = new_tat - emission_interval + burst_capacity

return {allowed, retry_after, reset_at, remaining}