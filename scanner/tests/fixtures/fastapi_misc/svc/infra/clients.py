import asyncpg
import motor.motor_asyncio
from aio_pika import connect_robust
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tortoise import fields
from tortoise.models import Model

mongo = motor.motor_asyncio.AsyncIOMotorClient("mongodb://mongo.internal:27017")
db = mongo["catalog"]
scheduler = AsyncIOScheduler()


class Product(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "products"


async def pool():
    return await asyncpg.create_pool("postgresql://u:p@pg.internal/catalog")


async def consume_prices():
    consumer = AIOKafkaConsumer("price-updates", bootstrap_servers="kafka:9092")
    await consumer.start()


async def publish_event(body: bytes):
    connection = await connect_robust("amqp://rabbit.internal/")
    channel = await connection.channel()
    await channel.default_exchange.publish(body, routing_key="catalog.events")


def nightly_cleanup():
    pass


scheduler.add_job(nightly_cleanup, "cron", hour=2)
