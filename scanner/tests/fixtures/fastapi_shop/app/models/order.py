import enum
from typing import List, Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OrderStatus(str, enum.Enum):
    NEW = "NEW"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_email: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.NEW)
    note: Mapped[Optional[str]] = mapped_column(String(500))
    lines: Mapped[List["OrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderLine(Base):
    __tablename__ = "order_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    sku: Mapped[str]
    quantity: Mapped[int]
    order: Mapped["Order"] = relationship(back_populates="lines")
