from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status

from app import crud
from app.api.deps import CurrentUser, SessionDep, get_order_service
from app.core.security import require_role
from app.exceptions import OrderNotFound
from app.schemas.order import CreateOrderRequest, OrderResponse, Page
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED, summary="Place an order")
async def create_order(
    request: CreateOrderRequest,
    user: CurrentUser,
    service: OrderService = Depends(get_order_service),
    x_request_id: Annotated[str, Header()] = ...,
):
    """Places a new order."""
    return await service.create(request)


@router.get("/{order_id}", response_model=OrderResponse, responses={404: {"description": "Not found"}})
def read_order(order_id: int, service: OrderService = Depends(get_order_service)):
    try:
        return service.get(order_id)
    except OrderNotFound:
        raise HTTPException(status_code=404, detail="Order not found")


@router.get("", response_model=Page[OrderResponse])
def list_orders(
    db: SessionDep,
    skip: int = 0,
    limit: Annotated[int, Query(le=100)] = 20,
    status_filter: str | None = Query(default=None, alias="status"),
):
    items = crud.order.get_multi(db, skip=skip, limit=limit)
    return {"items": items, "total": len(items)}


@router.delete("/{order_id}", status_code=204, dependencies=[Depends(require_role("admin"))])
def cancel_order(order_id: int, db: SessionDep, background: BackgroundTasks):
    crud.order.remove(db, order_id)
    background.add_task(notify, order_id)


def notify(order_id: int) -> None:
    pass
