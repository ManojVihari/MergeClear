import httpx

from app.core.config import settings


class PaymentClient:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.PAYMENTS_BASE_URL)

    async def charge(self, order_id: int, amount: float) -> bool:
        response = await self.client.post(f"/v1/charges/{order_id}", json={"amount": amount})
        return response.status_code == 200


async def stock_level(sku: str) -> int:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{settings.INVENTORY_URL}/stock/{sku}")
        return r.json()["quantity"]
