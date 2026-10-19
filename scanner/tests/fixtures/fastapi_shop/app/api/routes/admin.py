from fastapi import APIRouter, Depends

from app.core.security import require_role

router = APIRouter(prefix="/admin", dependencies=[Depends(require_role("admin"))])


@router.post("/invoices/{order_id}")
def export_invoice(order_id: int):
    from app.services.order_service import OrderService
    return {"order_id": order_id, "service": OrderService.__name__}
