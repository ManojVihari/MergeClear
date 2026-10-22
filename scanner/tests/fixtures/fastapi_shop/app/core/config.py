from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Shop"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "dev-secret"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_DB: str = "shop"
    REDIS_URL: str = "redis://localhost:6379/0"
    PAYMENTS_BASE_URL: str = "https://api.payments.example.com"
    INVENTORY_URL: str = "http://inventory-service"
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    ORDER_TOPIC: str = "orders.created.v1"


settings = Settings()
