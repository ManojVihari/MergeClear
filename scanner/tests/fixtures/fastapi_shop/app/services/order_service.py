import json

import boto3
import redis
from aiokafka import AIOKafkaProducer
from sqlalchemy.orm import Session

from app import crud
from app.clients.payments import PaymentClient, stock_level
from app.core.config import settings
from app.exceptions import InventoryConflict, OrderNotFound, PaymentDeclined
from app.models.audit import AuditLog
from app.schemas.order import CreateOrderRequest
from app.tasks.worker import send_receipt

cache = redis.Redis.from_url(settings.REDIS_URL)
s3 = boto3.client("s3")


class OrderService:
    def __init__(self, db: Session, payments: PaymentClient):
        self.db = db
        self.payments = payments
        self.producer = AIOKafkaProducer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)

    async def create(self, request: CreateOrderRequest):
        for line in request.lines:
            if await stock_level(line.sku) < line.quantity:
                raise InventoryConflict(line.sku)
        order = crud.order.create(self.db, request)
        if not await self.payments.charge(order.id, 10.0):
            raise PaymentDeclined("card")
        self.db.add(AuditLog(order_id=order.id, action="CREATE"))
        self.db.commit()
        cache.set(f"order:{order.id}", json.dumps({"id": order.id}))
        await self.producer.send_and_wait(settings.ORDER_TOPIC, b"created")
        send_receipt.delay(order.id)
        return order

    def get(self, order_id: int):
        cached = cache.get(f"order:{order_id}")
        if cached:
            return json.loads(cached)
        order = crud.order.get(self.db, order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order

    def export_invoice(self, order_id: int):
        s3.put_object(Bucket="shop-invoices", Key=f"{order_id}.pdf", Body=b"")
