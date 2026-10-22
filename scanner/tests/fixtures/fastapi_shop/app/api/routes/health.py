from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@router.websocket("/ws/orders")
async def orders_ws(websocket: WebSocket):
    await websocket.accept()
