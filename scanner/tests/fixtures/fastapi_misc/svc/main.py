from fastapi import FastAPI, WebSocket

from svc.infra import clients
from svc.routers import catalog


def build_v2() -> FastAPI:
    sub = FastAPI(title="catalog v2")

    @sub.get("/ping")
    def ping_v2():
        return {"pong": 2}

    return sub


def create_app() -> FastAPI:
    app = FastAPI(title="Catalog", root_path="/catalog")

    @app.on_event("startup")
    async def start():
        clients.scheduler.start()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()

    for router in (catalog.router,):
        app.include_router(router, prefix="/v1", tags=["catalog"])

    app.add_api_route("/healthz", lambda: {"ok": True}, methods=["GET", "HEAD"])
    app.mount("/v2", build_v2())
    return app


app = create_app()
