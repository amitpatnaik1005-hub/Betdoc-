"""The slip scanner (Group 126): a bookmaker's bet slip read from a screenshot, a camera photo or pasted text, vetted by
the Consultant's engines and re-calibrated. Mounted under ``/slip-scanner`` for a signed-in user.
Developed for Amit Ashok Kumar Patnaik.

* ``POST /scan-and-rate``: JSON ``{image_base64 | slip_text, bookmaker, stake_inr}`` (one of the image or the text)
* ``POST /upload-and-rate``: multipart ``file`` (PNG, JPEG, WEBP, BMP, GIF), ``bookmaker``, ``stake_inr``

The image is read on this machine and never stored; nothing is recorded, placed or sent to a bookmaker. A slip opens in
the Consultant (``consultant_legs``) to be sealed. Refusals answer ``{"detail": {"code", "message"}}``: 413 too large,
415 not an image the scanner reads, 422 nothing to read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.schemas.slip_scanner import DEVELOPER_CREDIT, ScanBook, ScanSlipRequest, ScanSlipResponse
from app.services.vision import slip_scanner_service as scanner

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


async def _engineered_for(response: Response) -> None:
    response.headers["X-Engineered-For"] = DEVELOPER_CREDIT


router = APIRouter(prefix="/slip-scanner", tags=["Slip Scanner (Group 126)"], dependencies=[Depends(_engineered_for)])


def _fail(exc: scanner.ScanRefusal) -> HTTPException:
    return HTTPException(exc.status_code, {"code": exc.reason, "message": exc.message}, headers={"X-Engineered-For": DEVELOPER_CREDIT})


async def _scan(request: Request, user: Any, sessions: async_sessionmaker[AsyncSession], settings: Settings, *, image: bytes | None, text: str | None,
                bookmaker: str, stake: float | None) -> dict[str, Any]:
    try:
        return await scanner.scan(sessions, getattr(request.app.state, "redis", None), settings, user.id, image=image, text=text, book=bookmaker,
                                  stake=None if stake is None else Decimal(str(stake)).quantize(scanner.CENT), now=datetime.now(UTC))
    except scanner.ScanRefusal as exc:
        raise _fail(exc) from exc


@router.post("/scan-and-rate", response_model=ScanSlipResponse, summary="Read a slip (base64 screenshot or text), vet it and re-calibrate it")
async def scan_and_rate(body: ScanSlipRequest, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    try:
        image = None if body.image_base64 is None else scanner.decode_base64(body.image_base64, settings)
    except scanner.ScanRefusal as exc:
        raise _fail(exc) from exc
    return await _scan(request, user, sessions, settings, image=image, text=body.slip_text, bookmaker=body.bookmaker, stake=body.stake_inr)


@router.post("/upload-and-rate", response_model=ScanSlipResponse, summary="Read an uploaded slip screenshot or photo, vet it and re-calibrate it")
async def upload_and_rate(
    request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings,
    file: Annotated[UploadFile, File(description="the slip: a PNG, JPEG or WEBP screenshot, or a camera photo")],
    bookmaker: Annotated[ScanBook, Form(description="auto: the book the slip names, else the best retail price")] = "auto",
    stake_inr: Annotated[float | None, Form(gt=0, le=1e8, description="omitted: the stake the slip shows, if any")] = None,
) -> dict[str, Any]:
    try:
        raw = await file.read(settings.SCANNER_MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    return await _scan(request, user, sessions, settings, image=raw, text=None, bookmaker=bookmaker, stake=stake_inr)
