from fastapi import APIRouter, WebSocket

from app.api.deps import WsUser
from app.core.events import relay_channel

router = APIRouter(prefix="/omni", tags=["omni"])


@router.websocket("/ws/stream")
async def omni_ws_proxy(websocket: WebSocket, user: WsUser) -> None:  # noqa: ARG001 - auth gate
    """Forward the Omni live channel (normalised provider payloads) to the browser."""
    settings = websocket.app.state.settings
    await relay_channel(websocket, getattr(websocket.app.state, "redis", None), settings.omni_live_channel)
