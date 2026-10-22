from enum import Enum
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class PaymentMethod(str, Enum):
    CARD = "CARD"
    PAYPAL = "PAYPAL"


class LineItem(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class CreateOrderRequest(BaseModel):
    customer_email: EmailStr
    lines: List[LineItem] = Field(min_length=1, max_length=20)
    payment_method: PaymentMethod = PaymentMethod.CARD
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value):
        return value.strip() if value else value


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    customer_email: str
    secret_hash: Optional[str] = Field(default=None, exclude=True)


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int = 1
