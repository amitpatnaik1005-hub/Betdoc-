"""The 422 every request validation error answers with. Developed for Amit Ashok Kumar Patnaik.

FastAPI's default handler echoes each failing ``input`` back, and a body carrying a non-finite number (``NaN``,
``Infinity``) cannot be written as JSON: the error response itself would crash. The inputs are dropped (the caller
sent them; the location and message say what was wrong), so every malformed request gets a clean 422. Installed on
the application in ``app/main.py``, and on the test apps that mount routers without it (Group 110).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def install(app: FastAPI) -> FastAPI:
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    return app
