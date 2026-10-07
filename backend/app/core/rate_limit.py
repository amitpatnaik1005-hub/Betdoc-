import time
from typing import Optional
from redis.asyncio import Redis
from fastapi import Request
from fastapi.responses import JSONResponse
from app.core.config import get_settings

settings = get_settings()

LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = 1

local bucket = redis.call('HMGET', key, 'tokens', 'last_update')
local tokens = tonumber(bucket[1])
local last_update = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_update = now
else
    local time_passed = math.max(0, now - last_update)
    tokens = math.min(capacity, tokens + (time_passed * refill_rate))
end

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'last_update', now)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate))
    return 1
else
    return 0
end
"""

class RateLimiter:
    def __init__(self):
        self._redis: Optional[Redis] = None
        self._script = None

    def initialize(self, redis_pool: Redis):
        self._redis = redis_pool

    async def check_rate_limit(self, key: str, capacity: int, refill_rate: float) -> bool:
        """Returns True if allowed, False if rate limited"""
        if not self._redis:
            return True # Fail open if Redis is down/missing to prevent total outage

        if not self._script:
            self._script = self._redis.register_script(LUA_TOKEN_BUCKET)

        now = time.time()
        result = await self._script(
            keys=[f"rate_limit:{key}"],
            args=[capacity, refill_rate, now]
        )
        return result == 1

limiter = RateLimiter()

async def rate_limit_middleware(request: Request, call_next):
    # Nginx handles Edge protection; this handles Application/Tenant logic
    # Uvicorn's UvicornWorker uses X-Forwarded-For because of --forwarded-allow-ips="*"
    ip = request.client.host if request.client else "unknown"
    
    # 60 RPM = 1 req/sec refill, capacity 60
    allowed = await limiter.check_rate_limit(
        key=f"global:{ip}",
        capacity=settings.RATE_LIMIT_GLOBAL_RPM,
        refill_rate=settings.RATE_LIMIT_GLOBAL_RPM / 60.0
    )
    
    if not allowed:
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})
        
    return await call_next(request)
