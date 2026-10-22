from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import admin, files, health, orders
from app.core.config import settings
from app.exceptions import OrderNotFound, PaymentDeclined


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Shop Service", version="1.4.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["https://shop.example.com"], allow_credentials=True)


@app.exception_handler(PaymentDeclined)
async def payment_declined_handler(request: Request, exc: PaymentDeclined):
    return JSONResponse(status_code=402, content={"detail": exc.reason})


@app.exception_handler(OrderNotFound)
async def order_not_found_handler(request: Request, exc: OrderNotFound):
    return JSONResponse(status_code=404, content={"detail": "missing"})


app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(admin.router, prefix=settings.API_V1_STR)
app.include_router(files.router, prefix=settings.API_V1_STR)
app.include_router(health.router)
