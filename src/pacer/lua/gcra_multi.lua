-- Multi-rate GCRA implementation for rate limiting
-- Supports up to 3 rates, all must pass for request to be allowed
-- 
-- KEYS: rate_key_1, rate_key_2, rate_key_3 (up to 3)
-- ARGV: 
--   [1] = now_ms
--   [2] = ttl_ms (max TTL across all rates)
--   [3] = num_rates (1-3)
--   [4,5,6] = emission_interval_ms for rate 1
--   [5,6,7] = burst_capacity_ms for rate 1
--   (pattern repeats for rate 2 and 3)
--
-- Returns: {allowed (0|1), retry_after_ms, reset_delta_ms, remaining, matched_rate_index}

local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local num_rates = tonumber(ARGV[3])

-- Result tracking
local global_allowed = 1
local min_retry_after = 0
local min_reset_delta = math.huge
local min_remaining = math.huge
local matched_rate_index = 0

-- Process each rate
for i = 1, num_rates do
    local key_index = i
    local argv_base = 3 + ((i - 1) * 2)  -- Skip now, ttl, num_rates
    
    local rate_key = KEYS[key_index]
    local emission_interval = tonumber(ARGV[argv_base + 1])
    local burst_capacity = tonumber(ARGV[argv_base + 2])
    
    -- Skip if key is empty (padding)
    if rate_key == "" then
        break
    end
    
    -- Get current TAT for this rate
    local tat = redis.call('GET', rate_key)
    if tat then
        tat = tonumber(tat)
    else
        -- First request for this rate
        tat = now
    end
    
    -- Calculate if request is allowed for this rate
    local allow_at = tat - burst_capacity
    local rate_allowed = 0
    local new_tat = tat
    local retry_after = 0
    
    if now >= allow_at then
        -- This rate allows the request
        rate_allowed = 1
        new_tat = math.max(tat, now) + emission_interval
        -- Update TAT for this rate
        redis.call('SET', rate_key, new_tat, 'PX', ttl)
    else
        -- This rate denies the request
        retry_after = math.ceil(allow_at - now)
        -- Don't update TAT on deny
    end
    
    -- Calculate remaining capacity for this rate
    local remaining = 0
    if rate_allowed == 1 then
        local burst_available = burst_capacity - (new_tat - now)
        remaining = math.max(0, math.floor(burst_available / emission_interval))
    end
    
    -- Calculate reset time for this rate
    local reset_at = new_tat - emission_interval + burst_capacity
    local reset_delta = reset_at - now
    
    -- Update global results (most restrictive wins)
    if rate_allowed == 0 then
        global_allowed = 0
        -- If this is the first deny, or has sooner retry_after
        if matched_rate_index == 0 or retry_after < min_retry_after then
            min_retry_after = retry_after
            matched_rate_index = i
        end
    end
    
    -- Track minimum remaining and reset (most restrictive)
    if remaining < min_remaining then
        min_remaining = remaining
        if global_allowed == 1 then
            matched_rate_index = i  -- This is the tightest allowing rate
        end
    end
    
    if reset_delta < min_reset_delta then
        min_reset_delta = reset_delta
    end
end

-- If no rate was specifically matched (all have same remaining), use first
if matched_rate_index == 0 then
    matched_rate_index = 1
end

return {global_allowed, min_retry_after, min_reset_delta, min_remaining, matched_rate_index}