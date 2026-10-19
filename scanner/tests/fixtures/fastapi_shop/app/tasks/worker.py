from celery import Celery

from app.core.config import settings

celery_app = Celery("shop", broker="amqp://rabbit.internal")


@celery_app.task(name="shop.send_receipt")
def send_receipt(order_id: int):
    return order_id
