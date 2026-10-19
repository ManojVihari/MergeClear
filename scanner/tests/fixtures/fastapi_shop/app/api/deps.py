from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.clients.payments import PaymentClient
from app.core.security import get_current_user
from app.db.session import get_db
from app.services.order_service import OrderService

SessionDep = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[dict, Depends(get_current_user)]


def get_order_service(db: SessionDep) -> OrderService:
    return OrderService(db, PaymentClient())
