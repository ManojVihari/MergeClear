from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query, Security
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi_cache.decorator import cache
from pydantic import BaseModel, Field
from slowapi import Limiter
from tenacity import retry, stop_after_attempt

from svc.deps import Pagination, RoleChecker, scoped, tenant
from svc.infra import clients
from svc.infra.clients import Product

limiter = Limiter(key_func=lambda r: "x")
router = APIRouter()


class Filters(BaseModel):
    q: Optional[str] = None
    limit: int = Field(10, ge=1, le=50)


class Price(BaseModel):
    amount: float = Field(..., gt=0)
    currency: str = "EUR"


@router.get("/products", response_model=List[Price])
@cache(expire=60)
async def list_products(filters: Filters = Query(), page: Pagination = Depends(), tenant_id: str = Depends(tenant)):
    docs = await clients.db.products.find({}).to_list(100)
    return docs


@router.get("/products/{pid}/page", response_class=HTMLResponse)
async def product_page(pid: int):
    row = await Product.get(id=pid)
    return "<html></html>"


@router.put("/products/{pid}/price", dependencies=[Depends(RoleChecker(["admin"]))])
@limiter.limit("5/minute")
async def set_price(pid: int, price: Price = Body(..., embed=True), reason: str = Body(None)):
    await clients.db.products.update_one({"_id": pid}, {"$set": {"price": price.amount}})
    await clients.publish_event(b"price")
    return {"ok": True}


@router.get("/export")
@retry(stop=stop_after_attempt(3))
def export(user: str = Security(scoped, scopes=["catalog:read", "catalog:export"])):
    async def rows():
        yield b"x"
    return StreamingResponse(rows())


@router.get("/old", deprecated=True, status_code=307)
def old():
    return RedirectResponse("/products", status_code=307)


@router.delete("/products/{pid}", status_code=204)
async def delete_product(pid: int):
    await Product.filter(id=pid).delete()
