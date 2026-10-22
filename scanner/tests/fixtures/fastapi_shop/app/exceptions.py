from fastapi import HTTPException


class OrderNotFound(Exception):
    pass


class PaymentDeclined(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class InventoryConflict(HTTPException):
    def __init__(self, sku: str):
        super().__init__(status_code=409, detail=f"Out of stock: {sku}")
