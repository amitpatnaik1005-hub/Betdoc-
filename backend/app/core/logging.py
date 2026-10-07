import logging
import json
import traceback
from datetime import datetime
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uuid

class JSONLogFormatter(logging.Formatter):
    """
    Law 6: No Leaked Stack Traces (JSON logs get tracebacks; HTTP 500s get opaque correlation IDs).
    """
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Inject request correlation ID if bound to the context
        if hasattr(record, "correlation_id"):
            log_obj["correlation_id"] = record.correlation_id

        if record.exc_info:
            log_obj["exception"] = "".join(traceback.format_exception(*record.exc_info))
            
        return json.dumps(log_obj)

def setup_json_logging(log_level: int = logging.INFO):
    """Hijacks standard library, Uvicorn, and Gunicorn loggers."""
    formatter = JSONLogFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers = [handler]

    # Hijack framework loggers
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "gunicorn", "gunicorn.access", "gunicorn.error", "fastapi"):
        logger = logging.getLogger(logger_name)
        logger.handlers = [handler]
        logger.propagate = False

class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        except Exception as e:
            # Catch 500s globally, log traceback internally, return opaque ID
            logger = logging.getLogger("fastapi")
            logger.error("Unhandled server exception", exc_info=True, extra={"correlation_id": correlation_id})
            return Response(
                content=json.dumps({"detail": "Internal Server Error", "correlation_id": correlation_id}),
                status_code=500,
                media_type="application/json"
            )
