from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.order import Order, OrderLine


class CRUDOrder(CRUDBase[Order]):
    def mark_paid(self, db: Session, order_id: int) -> None:
        db.execute(update(Order).where(Order.id == order_id).values(status="PAID"))
        db.commit()

    def lines_for(self, db: Session, order_id: int):
        return db.execute(select(OrderLine).where(OrderLine.order_id == order_id)).scalars().all()


order = CRUDOrder(Order)
