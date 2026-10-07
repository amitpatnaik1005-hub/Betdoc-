import asyncio
import time
from fastapi import APIRouter
from sqlalchemy import text
from app.api.deps import DbSession
from app.core.rate_limit import limiter
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()

@router.get("/")
async def deep_health_check(db: DbSession):
    start_time = time.time()
    
    async def check_db():
        try:
            await db.execute(text("SELECT 1"))
            return "ok"
        except Exception:
            return "failed"
            
    async def check_redis():
        try:
            if limiter._redis:
                await limiter._redis.ping()
                return "ok"
            return "uninitialized"
        except Exception:
            return "failed"

    db_status, redis_status = await asyncio.gather(
        check_db(),
        check_redis(),
        return_exceptions=True
    )
    
    return {
        "status": "ok" if db_status == "ok" and redis_status == "ok" else "degraded",
        "project": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT,
        "dependencies": {
            "postgres": db_status,
            "redis": redis_status
        },
        "response_time_ms": round((time.time() - start_time) * 1000, 2)
    }
