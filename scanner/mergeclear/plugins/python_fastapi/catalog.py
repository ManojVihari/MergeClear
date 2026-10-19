"""
Knowledge about libraries: what a class from a package is (database session,
Redis client, Kafka producer, ...), which methods read or write, which
packages are which technology. Pure data plus tiny helpers.
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .pyindex import HTTP_STATUS_NAMES

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "options", "head", "trace")
ROUTE_DECORATORS = set(HTTP_VERBS) | {"api_route", "websocket", "route"}

STATUS_NAME = {code: name for name, code in HTTP_STATUS_NAMES.items() if name not in ("CONTENT_TOO_LARGE", "PAYLOAD_TOO_LARGE", "UNPROCESSABLE_CONTENT", "REQUEST_ENTITY_TOO_LARGE")}
STATUS_NAME.update({413: "REQUEST_ENTITY_TOO_LARGE", 422: "UNPROCESSABLE_ENTITY"})

# ============================================================================
# FASTAPI / STARLETTE
# ============================================================================

APP_CLASSES = re.compile(r"^(fastapi(\.applications)?\.FastAPI|starlette\.applications\.Starlette)$")
ROUTER_CLASSES = re.compile(
    r"^(fastapi(\.routing)?\.APIRouter|fastapi_utils\.inferring_router\.InferringRouter|fastapi_restful\.inferring_router\.InferringRouter"
    r"|fastapi_versioning\.\w+|starlette\.routing\.Router)$"
)

PARAM_FUNCTIONS = {"Query": "query", "Path": "path", "Header": "header", "Cookie": "cookie", "Body": "body", "Form": "form",
                   "File": "file"}
DEPENDS_FUNCTIONS = ("Depends", "Security")

# parameters FastAPI injects itself; they are not part of the HTTP contract
INJECTED_TYPES = re.compile(
    r"^(fastapi|starlette)(\.\w+)*\.(Request|Response|WebSocket|HTTPConnection|BackgroundTasks|SecurityScopes|"
    r"WebSocketDisconnect|UJSONResponse|ORJSONResponse|JSONResponse|HTMLResponse|PlainTextResponse|StreamingResponse|FileResponse|"
    r"RedirectResponse)$"
)

RESPONSE_CLASSES = {
    "JSONResponse": "application/json", "UJSONResponse": "application/json", "ORJSONResponse": "application/json",
    "HTMLResponse": "text/html", "PlainTextResponse": "text/plain", "StreamingResponse": "application/octet-stream",
    "FileResponse": "application/octet-stream", "RedirectResponse": None, "Response": None,
}

SECURITY_SCHEMES = {
    "OAuth2PasswordBearer": "oauth2-password", "OAuth2AuthorizationCodeBearer": "oauth2-authorization-code",
    "OAuth2PasswordRequestForm": "oauth2-password", "OAuth2": "oauth2", "HTTPBearer": "bearer", "HTTPBasic": "basic",
    "HTTPDigest": "digest", "APIKeyHeader": "api-key-header", "APIKeyQuery": "api-key-query", "APIKeyCookie": "api-key-cookie",
    "OpenIdConnect": "openid-connect",
}

# library classes used as `Depends()`: the request parameters they read
FORM_DEPENDENCIES = {
    "OAuth2PasswordRequestForm": [("grant_type", "str | None", False), ("username", "str", True), ("password", "str", True),
                                  ("scope", "str", False), ("client_id", "str | None", False), ("client_secret", "str | None", False)],
    "OAuth2PasswordRequestFormStrict": [("grant_type", "str", True), ("username", "str", True), ("password", "str", True),
                                        ("scope", "str", False), ("client_id", "str | None", False), ("client_secret", "str | None", False)],
}

MIDDLEWARE_NAMES = {
    "CORSMiddleware": "cors", "TrustedHostMiddleware": "trusted-host", "GZipMiddleware": "gzip",
    "HTTPSRedirectMiddleware": "https-redirect", "SessionMiddleware": "session", "AuthenticationMiddleware": "authentication",
    "SlowAPIMiddleware": "rate-limit", "ProxyHeadersMiddleware": "proxy-headers", "SentryAsgiMiddleware": "sentry",
    "PrometheusMiddleware": "prometheus", "RateLimitMiddleware": "rate-limit",
}

AUTH_NAME_HINT = re.compile(r"(auth|token|user|login|jwt|credential|permission|role|scope|api_?key|principal|identity|admin|superuser|current)", re.I)
ROLE_NAME_HINT = re.compile(r"(role|permission|scope|admin|superuser|require|has_|allowed|group)", re.I)


# ============================================================================
# LIBRARY TYPES
# ============================================================================

@dataclass(frozen=True)
class Tag:
    """What a variable or expression is, as far as the scanner can tell."""
    family: str                     # db | cache | broker | http | storage | email | search | framework
    tech: str                       # sqlalchemy, redis, kafka, ...
    role: str = ""                  # session | engine | connection | producer | consumer | client | ...
    info: Tuple[Tuple[str, str], ...] = ()

    def get(self, key, default=None):
        return dict(self.info).get(key, default)

    def with_info(self, **kw) -> "Tag":
        merged = dict(self.info)
        merged.update({k: v for k, v in kw.items() if v is not None})
        return Tag(self.family, self.tech, self.role, tuple(sorted(merged.items())))


# (regex on the dotted library name, tag). First match wins.
TYPE_RULES: List[Tuple[re.Pattern, Tag]] = [(re.compile(p), t) for p, t in [
    # ---- relational / ORM sessions ----
    (r"^sqlalchemy\.orm(\.session)?\.Session$", Tag("db", "sqlalchemy", "session")),
    (r"^sqlalchemy\.ext\.asyncio(\.session)?\.AsyncSession$", Tag("db", "sqlalchemy", "session")),
    (r"^sqlmodel(\.orm\.session|\.ext\.asyncio\.session)?\.(Session|AsyncSession)$", Tag("db", "sqlalchemy", "session")),
    (r"^sqlalchemy\.orm(\.scoping)?\.scoped_session$|^sqlalchemy\.ext\.asyncio\.(scoping\.)?async_scoped_session$", Tag("db", "sqlalchemy", "session")),
    (r"^sqlalchemy\.orm(\.session)?\.sessionmaker$|^sqlalchemy\.ext\.asyncio(\.session)?\.async_sessionmaker$", Tag("db", "sqlalchemy", "session_factory")),
    (r"^sqlalchemy(\.engine)?\.create_engine$|^sqlalchemy\.ext\.asyncio(\.engine)?\.create_async_engine$|^sqlmodel\.create_engine$", Tag("db", "sqlalchemy", "engine")),
    (r"^sqlalchemy\.(engine\.)?(Engine|Connection)$|^sqlalchemy\.ext\.asyncio\.(AsyncEngine|AsyncConnection)$", Tag("db", "sqlalchemy", "engine")),
    # ---- raw drivers ----
    (r"^sqlite3\.connect$|^sqlite3\.Connection$", Tag("db", "sqlite", "connection")),
    (r"^aiosqlite\.(connect|Connection)$", Tag("db", "sqlite", "connection")),
    (r"^psycopg2?(\.extensions)?\.(connect|connection)$|^psycopg(_pool)?\.(Connection|AsyncConnection|ConnectionPool|AsyncConnectionPool)$|^psycopg\.AsyncConnection\.connect$", Tag("db", "postgresql", "connection")),
    (r"^asyncpg\.(connect|create_pool|Connection|Pool)$|^asyncpg\.connection\.Connection$|^asyncpg\.pool\.Pool$", Tag("db", "postgresql", "connection")),
    (r"^(pymysql|aiomysql|mysql\.connector|MySQLdb|asyncmy)\.(connect|create_pool|Connection|Pool)$", Tag("db", "mysql", "connection")),
    (r"^(oracledb|cx_Oracle)\.(connect|create_pool)$", Tag("db", "oracle", "connection")),
    (r"^(pyodbc|pymssql|aioodbc)\.(connect|create_pool)$", Tag("db", "sqlserver", "connection")),
    (r"^databases\.Database$", Tag("db", "databases", "connection")),
    (r"^clickhouse_(driver|connect)\..*", Tag("db", "clickhouse", "connection")),
    # ---- document / graph / wide-column ----
    (r"^(pymongo|motor\.motor_asyncio|motor\.motor_tornado|motor)(\.\w+)*\.(MongoClient|AsyncIOMotorClient|Database|Collection|AsyncIOMotorDatabase|AsyncIOMotorCollection|AsyncMongoClient)$", Tag("db", "mongodb", "client")),
    (r"^neo4j\.(GraphDatabase|AsyncGraphDatabase)(\.driver)?$", Tag("db", "neo4j", "connection")),
    (r"^(cassandra\.cluster|acsylla)\.\w+$", Tag("db", "cassandra", "connection")),
    (r"^(pynamodb|aiodynamo)\..*", Tag("db", "dynamodb", "connection")),
    (r"^prisma\.(Prisma|client\.Prisma)$", Tag("db", "prisma", "client")),
    # ---- caches ----
    (r"^(redis|aioredis)(\.asyncio)?(\.client)?(\.cluster)?\.(Redis|StrictRedis|RedisCluster|from_url|create_redis|create_redis_pool|create_pool|ConnectionPool)$", Tag("cache", "redis", "client")),
    (r"^redis\.asyncio\.(Redis|StrictRedis|from_url)$", Tag("cache", "redis", "client")),
    (r"^redis\.(asyncio\.)?client\.(PubSub)$", Tag("cache", "redis", "pubsub")),
    (r"^(fakeredis|fakeredis\.aioredis)\.\w+$", Tag("cache", "redis", "client")),
    (r"^(pymemcache\.client\.base|aiomcache)\.\w*Client$|^pymemcache\.client\.\w+\.Client$", Tag("cache", "memcached", "client")),
    (r"^(aiocache)\.(Cache|caches|RedisCache|SimpleMemoryCache)$", Tag("cache", "aiocache", "client")),
    # ---- messaging ----
    (r"^aiokafka\.(AIOKafkaProducer|producer\.AIOKafkaProducer)$|^kafka\.(KafkaProducer|producer\.KafkaProducer)$|^confluent_kafka\.(Producer|SerializingProducer)$|^confluent_kafka\.avro\.AvroProducer$", Tag("broker", "kafka", "producer")),
    (r"^aiokafka\.(AIOKafkaConsumer|consumer\.AIOKafkaConsumer)$|^kafka\.(KafkaConsumer|consumer\.KafkaConsumer)$|^confluent_kafka\.(Consumer|DeserializingConsumer)$", Tag("broker", "kafka", "consumer")),
    (r"^faststream\.kafka(\.\w+)*\.(KafkaBroker|KafkaRouter)$", Tag("broker", "kafka", "broker")),
    (r"^faststream\.(rabbit)(\.\w+)*\.(RabbitBroker|RabbitRouter)$", Tag("broker", "rabbitmq", "broker")),
    (r"^faststream\.nats(\.\w+)*\.(NatsBroker|NatsRouter)$", Tag("broker", "nats", "broker")),
    (r"^faststream\.redis(\.\w+)*\.(RedisBroker|RedisRouter)$", Tag("broker", "redis-streams", "broker")),
    (r"^faststream\.confluent(\.\w+)*\.(KafkaBroker|KafkaRouter)$", Tag("broker", "kafka", "broker")),
    (r"^pika\.(BlockingConnection|SelectConnection|AsyncioConnection|TornadoConnection)$|^pika\.adapters(\.\w+)*\.\w*Connection$", Tag("broker", "rabbitmq", "connection")),
    (r"^pika\.(channel\.)?(Channel|BlockingChannel)$|^pika\.adapters\.blocking_connection\.BlockingChannel$", Tag("broker", "rabbitmq", "channel")),
    (r"^aio_pika\.(connect|connect_robust|Connection|RobustConnection)$", Tag("broker", "rabbitmq", "connection")),
    (r"^aio_pika(\.\w+)*\.(Channel|RobustChannel)$", Tag("broker", "rabbitmq", "channel")),
    (r"^aio_pika(\.\w+)*\.(Exchange|RobustExchange)$", Tag("broker", "rabbitmq", "exchange")),
    (r"^kombu\.(Connection|Producer|Exchange|Queue)$", Tag("broker", "rabbitmq", "connection")),
    (r"^nats\.(connect|aio\.client\.Client|NATS)$|^nats\.aio\.client\.Client$", Tag("broker", "nats", "connection")),
    (r"^google\.cloud\.pubsub(_v1)?(\.\w+)*\.PublisherClient$", Tag("broker", "pubsub", "producer")),
    (r"^google\.cloud\.pubsub(_v1)?(\.\w+)*\.SubscriberClient$", Tag("broker", "pubsub", "consumer")),
    (r"^azure\.servicebus(\.aio)?\.(ServiceBusClient|ServiceBusSender)$", Tag("broker", "azure-service-bus", "producer")),
    (r"^paho\.mqtt\.client\.Client$|^(asyncio_mqtt|aiomqtt)\.Client$", Tag("broker", "mqtt", "producer")),
    (r"^celery\.(Celery|app\.base\.Celery)$", Tag("broker", "celery", "app")),
    (r"^rq\.(Queue|queue\.Queue)$", Tag("broker", "rq", "queue")),
    (r"^arq\.(create_pool|connections\.ArqRedis|ArqRedis)$", Tag("broker", "arq", "queue")),
    (r"^taskiq\.\w+Broker$|^taskiq_\w+(\.\w+)*\.\w+Broker$", Tag("broker", "taskiq", "app")),
    (r"^dramatiq\.(actor|Actor)$", Tag("broker", "dramatiq", "actor")),
    # ---- HTTP clients ----
    (r"^httpx\.(Client|AsyncClient)$", Tag("http", "httpx", "client")),
    (r"^requests(\.sessions)?\.Session$|^requests_cache\.\w*Session$", Tag("http", "requests", "client")),
    (r"^aiohttp(\.client)?\.ClientSession$", Tag("http", "aiohttp", "client")),
    (r"^urllib3\.(PoolManager|HTTPConnectionPool|HTTPSConnectionPool)$", Tag("http", "urllib3", "client")),
    (r"^(grequests|treq)\.\w+$", Tag("http", "http", "client")),
    # ---- object storage ----
    (r"^minio\.Minio$", Tag("storage", "minio", "client")),
    (r"^google\.cloud\.storage(\.\w+)*\.(Client|Bucket|Blob)$", Tag("storage", "gcs", "client")),
    (r"^azure\.storage\.blob(\.aio)?\.(BlobServiceClient|ContainerClient|BlobClient)$", Tag("storage", "azure-blob", "client")),
    # ---- email ----
    (r"^smtplib\.(SMTP|SMTP_SSL)$|^aiosmtplib\.SMTP$", Tag("email", "smtp", "client")),
    (r"^fastapi_mail\.FastMail$", Tag("email", "fastapi-mail", "client")),
    (r"^sendgrid(\.\w+)*\.SendGridAPIClient$", Tag("email", "sendgrid", "client")),
    (r"^yagmail\.SMTP$|^resend\.Emails$|^mailchimp_transactional\.Client$", Tag("email", "email", "client")),
    # ---- search ----
    (r"^elasticsearch\.(Elasticsearch|AsyncElasticsearch)$|^elasticsearch\._(a)?sync\.client\.\w*Elasticsearch$", Tag("search", "elasticsearch", "client")),
    (r"^opensearchpy\.(OpenSearch|AsyncOpenSearch)$", Tag("search", "opensearch", "client")),
    (r"^meilisearch(_python_sdk)?\.(Client|AsyncClient)$|^typesense\.Client$|^algoliasearch(\.\w+)*\.\w*Client$", Tag("search", "search", "client")),
    # ---- framework helpers ----
    (r"^(fastapi|starlette)(\.\w+)*\.BackgroundTasks$", Tag("framework", "fastapi", "background")),
]]

# boto3.client("x") / boto3.resource("x") / session.client("x") by service
AWS_SERVICE_TAGS = {
    "s3": Tag("storage", "s3", "client"), "sqs": Tag("broker", "sqs", "producer"), "sns": Tag("broker", "sns", "producer"),
    "events": Tag("broker", "eventbridge", "producer"), "kinesis": Tag("broker", "kinesis", "producer"),
    "firehose": Tag("broker", "kinesis", "producer"), "ses": Tag("email", "ses", "client"), "sesv2": Tag("email", "ses", "client"),
    "dynamodb": Tag("db", "dynamodb", "client"), "es": Tag("search", "elasticsearch", "client"),
    "opensearch": Tag("search", "opensearch", "client"), "stepfunctions": Tag("broker", "step-functions", "producer"),
    "lambda": Tag("http", "lambda", "client"), "secretsmanager": Tag("framework", "secrets", "client"),
}
AWS_FACTORIES = re.compile(r"^(boto3|aioboto3|boto3\.session\.Session|boto3\.Session|aiobotocore\.session)(\.\w+)*\.(client|resource)$")
AWS_SESSIONS = re.compile(r"^(boto3|aioboto3)\.(session\.)?(Session)$|^aiobotocore\.session\.get_session$")

# library functions that return something we recognise
FUNCTION_RESULTS = {
    "sqlalchemy.orm.declarative_base": Tag("orm", "sqlalchemy", "base"),
    "sqlalchemy.ext.declarative.declarative_base": Tag("orm", "sqlalchemy", "base"),
    "sqlalchemy.ext.automap.automap_base": Tag("orm", "sqlalchemy", "base"),
    "sqlalchemy.orm.DeclarativeBase": Tag("orm", "sqlalchemy", "base"),
}

ENTITY_BASES = [(re.compile(p), store) for p, store in [
    (r"^sqlalchemy\.orm\.(DeclarativeBase|DeclarativeBaseNoMeta|DeclarativeMeta)$|^sqlalchemy\.ext\.declarative\.(declarative_base|DeclarativeMeta)$|^sqlalchemy\.orm\.declarative_base$|^sqlalchemy\.ext\.automap\.automap_base$|^sqlalchemy\.orm\.MappedAsDataclass$", "sqlalchemy"),
    (r"^flask_sqlalchemy\.\w+$|^fastapi_sqlalchemy\.\w+$", "sqlalchemy"),
    (r"^tortoise\.models\.Model$|^tortoise\.Model$", "tortoise"),
    (r"^beanie\.(Document|odm\.documents\.Document)$|^beanie\.odm\.documents\.Document$", "beanie"),
    (r"^mongoengine\.(Document|EmbeddedDocument|DynamicDocument)$", "mongoengine"),
    (r"^odmantic\.Model$", "odmantic"),
    (r"^ormar\.Model$", "ormar"),
    (r"^peewee\.Model$", "peewee"),
    (r"^django\.db\.models\.Model$", "django"),
    (r"^pony\.orm\.core\.Entity$", "pony"),
    (r"^pynamodb\.models\.Model$", "dynamodb"),
]]
SQLMODEL_BASE = re.compile(r"^sqlmodel(\.main)?\.SQLModel$")

MODEL_BASES = re.compile(
    r"^(pydantic(\.v1)?(\.main)?\.BaseModel|pydantic\.generics\.GenericModel|pydantic\.v1\.generics\.GenericModel|sqlmodel(\.main)?\.SQLModel|"
    r"pydantic_settings(\.main)?\.BaseSettings|pydantic(\.v1)?(\.env_settings)?\.BaseSettings|pydantic\.RootModel|"
    r"fastapi_camelcase\.CamelModel|fastapi_utils\.camelcase\.CamelModel|typing(_extensions)?\.TypedDict|typing\.NamedTuple|"
    r"fastapi_users\.schemas\.\w+|fastapi_users\.models\.\w+|ninja\.Schema)$"
)
SETTINGS_BASES = re.compile(r"^(pydantic_settings(\.main)?\.BaseSettings|pydantic(\.v1)?(\.env_settings)?\.BaseSettings|dynaconf\.\w+|starlette\.config\.Config)$")
ENUM_BASES = re.compile(r"^(enum\.)?(Enum|IntEnum|StrEnum|Flag|IntFlag)$|^(enum|aenum)\.\w+$|^fastapi_utils\.enums\.\w+$")
DATACLASS_DECORATORS = re.compile(r"^(dataclasses\.)?dataclass$|^pydantic\.dataclasses\.dataclass$|^attr\.s$|^attrs\.define$|^attr\.define$")
EXCEPTION_BASES = re.compile(r"(Exception|Error|HTTPException|Warning)$")
HTTP_EXCEPTIONS = re.compile(r"^(fastapi|starlette)(\.exceptions)?\.HTTPException$|^starlette\.exceptions\.HTTPException$|^fastapi\.exceptions\.HTTPException$")
VALIDATION_EXCEPTIONS = re.compile(r"^fastapi(\.exceptions)?\.RequestValidationError$|^pydantic(\.error_wrappers)?\.ValidationError$")

# ============================================================================
# METHODS
# ============================================================================

ORM_READ = {"query", "get", "scalars", "scalar", "scalar_one", "scalar_one_or_none", "first", "all", "one", "one_or_none", "fetchall",
            "fetchone", "fetchmany", "refresh", "stream", "stream_scalars", "exec", "get_one", "count", "get_or_404"}
ORM_WRITE = {"add", "add_all", "merge", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings", "flush", "save", "upsert",
             "update", "insert"}
ORM_DELETE = {"delete", "expunge", "truncate"}
ORM_TX = {"commit", "rollback", "begin", "begin_nested"}
SQL_EXECUTE = {"execute", "executemany", "executescript", "exec_driver_sql", "fetch", "fetchrow", "fetchval", "fetch_all", "fetch_one",
               "fetch_val", "execute_many", "run_sync", "copy_records_to_table", "copy_from", "copy_to"}
SQL_BUILDERS = {"select": "READ", "insert": "WRITE", "update": "WRITE", "delete": "DELETE"}

MONGO_READ = {"find", "find_one", "aggregate", "count_documents", "distinct", "estimated_document_count", "get", "count", "to_list"}
MONGO_WRITE = {"insert_one", "insert_many", "update_one", "update_many", "replace_one", "find_one_and_update", "find_one_and_replace",
               "bulk_write", "save", "create_index", "insert", "update"}
MONGO_DELETE = {"delete_one", "delete_many", "drop", "find_one_and_delete", "remove"}

# entity class methods (Tortoise, Beanie, Mongoengine, Peewee, Ormar, Django)
ACTIVE_RECORD_READ = {"get", "get_or_none", "filter", "all", "find", "find_one", "find_all", "first", "select", "objects", "exists",
                      "count", "aggregate", "get_or_create", "values", "values_list", "annotate", "fetch_related", "prefetch_related",
                      "select_related", "get_by_id", "order_by", "limit", "paginate", "exclude", "in_bulk"}
ACTIVE_RECORD_WRITE = {"create", "save", "insert", "update", "bulk_create", "bulk_update", "upsert", "update_or_create", "insert_one",
                       "insert_many", "replace", "set", "inc", "get_or_create", "create_or_update", "add"}
ACTIVE_RECORD_DELETE = {"delete", "delete_many", "delete_one", "remove", "truncate"}

REDIS_READ = {"get", "mget", "hget", "hgetall", "hmget", "hkeys", "hvals", "hexists", "lrange", "lindex", "llen", "smembers", "sismember",
              "scard", "zrange", "zrevrange", "zrangebyscore", "zscore", "zcard", "exists", "ttl", "pttl", "keys", "scan", "scan_iter",
              "type", "strlen", "getrange", "xrange", "xread", "xrevrange", "json", "ft"}
REDIS_WRITE = {"set", "setex", "setnx", "psetex", "mset", "hset", "hmset", "hsetnx", "lpush", "rpush", "lset", "ltrim", "sadd", "zadd",
               "incr", "incrby", "incrbyfloat", "decr", "decrby", "hincrby", "expire", "pexpire", "expireat", "persist", "append",
               "setrange", "getset", "getdel", "rename", "copy", "lpop", "rpop", "spop", "zincrby", "zpopmin", "zpopmax", "setbit"}
REDIS_DELETE = {"delete", "unlink", "hdel", "srem", "lrem", "zrem", "flushdb", "flushall", "xdel", "xtrim", "zremrangebyscore"}
REDIS_PUBLISH = {"publish", "xadd", "spublish"}

PRODUCER_METHODS = {
    "kafka": {"send", "send_and_wait", "send_batch", "produce", "publish"},
    "rabbitmq": {"basic_publish", "publish", "send", "send_message", "default_exchange"},
    "nats": {"publish", "request", "js_publish"},
    "sqs": {"send_message", "send_message_batch"},
    "sns": {"publish", "publish_batch"},
    "eventbridge": {"put_events"},
    "kinesis": {"put_record", "put_records", "put_record_batch"},
    "pubsub": {"publish"},
    "azure-service-bus": {"send_messages", "send_message"},
    "mqtt": {"publish"},
    "step-functions": {"start_execution", "start_sync_execution"},
    "redis-streams": {"publish"},
}
TASK_DISPATCH_METHODS = {"delay", "apply_async", "send_task", "signature", "si", "s", "apply", "enqueue", "enqueue_call", "enqueue_job",
                         "send", "send_with_options", "kiq", "kick"}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send", "fetch", "urlopen"}
HTTP_MODULE_FUNCTIONS = re.compile(r"^(requests|httpx|aiohttp|grequests|treq|urllib3)\.(get|post|put|patch|delete|head|options|request|stream)$")

STORAGE_READ = {"get_object", "download_file", "download_fileobj", "get_object_tagging", "list_objects", "list_objects_v2", "head_object",
                "generate_presigned_url", "generate_presigned_post", "download_as_bytes", "download_as_text", "download_blob",
                "list_blobs", "fget_object", "presigned_get_object", "stat_object", "download_to_filename", "exists", "open"}
STORAGE_WRITE = {"put_object", "upload_file", "upload_fileobj", "upload_from_file", "upload_from_string", "upload_from_filename",
                 "upload_blob", "fput_object", "copy_object", "copy", "create_bucket", "put_object_tagging", "presigned_put_object",
                 "put_bucket_policy", "create_multipart_upload"}
STORAGE_DELETE = {"delete_object", "delete_objects", "remove_object", "remove_objects", "delete_blob", "delete", "delete_bucket"}

EMAIL_METHODS = {"sendmail", "send_message", "send", "send_mail", "send_email", "send_raw_email", "send_templated_email", "sendmail_async"}
SEARCH_READ = {"search", "get", "mget", "count", "msearch", "scroll", "exists", "suggest", "multi_search"}
SEARCH_WRITE = {"index", "bulk", "update", "create", "add_documents", "upsert", "update_by_query", "add_document", "save_object", "save_objects"}
SEARCH_DELETE = {"delete", "delete_by_query", "delete_index", "delete_document", "delete_all_documents"}

ASYNC_LAUNCHERS = {
    "asyncio.create_task": "asyncio", "asyncio.ensure_future": "asyncio", "asyncio.to_thread": "thread", "asyncio.run_coroutine_threadsafe": "asyncio",
    "starlette.concurrency.run_in_threadpool": "threadpool", "fastapi.concurrency.run_in_threadpool": "threadpool",
    "anyio.to_thread.run_sync": "thread", "anyio.from_thread.run": "thread", "threading.Thread": "thread",
    "concurrent.futures.ThreadPoolExecutor.submit": "thread", "concurrent.futures.ProcessPoolExecutor.submit": "process",
    "multiprocessing.Process": "process",
}
ASYNC_METHODS = {"run_in_executor", "add_task", "submit", "apply_async"}

RESILIENCE_DECORATORS = [(re.compile(p), name) for p, name in [
    (r"^tenacity\.(retry|AsyncRetrying|Retrying)$|^retrying\.retry$|^retry\.retry(_call)?$|^retry_async\.retry$", "retry"),
    (r"^backoff\.(on_exception|on_predicate)$", "backoff"),
    (r"^circuitbreaker\.circuit$|^pybreaker\.CircuitBreaker$|^aiobreaker\.CircuitBreaker$", "circuit-breaker"),
    (r"^slowapi\.Limiter\.(limit|shared_limit)$|^fastapi_limiter\.depends\.RateLimiter$", "rate-limit"),
    (r"^async_timeout\.timeout$|^asyncio\.wait_for$|^stopit\.\w+$|^timeout_decorator\.timeout$", "timeout"),
    (r"^aiolimiter\.AsyncLimiter$|^ratelimit\.(limits|sleep_and_retry)$", "rate-limit"),
]]
CACHE_DECORATORS = [(re.compile(p), name) for p, name in [
    (r"^fastapi_cache\.decorator\.cache$|^fastapi_cache\.cache$", "fastapi-cache"),
    (r"^aiocache\.(cached|multi_cached)$", "aiocache"),
    (r"^cachetools\.(cached|cachedmethod)$", "cachetools"),
    (r"^async_lru\.alru_cache$|^functools\.(lru_cache|cache)$", None),       # in-process memoisation: not an integration
    (r"^cashews\.\w+(\.cache)?$|^cashews\.cache$", "cashews"),
    (r"^fastapi_redis_cache\.\w+$", "redis"),
]]
SCHEDULER_PATTERNS = {
    "apscheduler": re.compile(r"^apscheduler\.schedulers(\.\w+)*\.\w*Scheduler$"),
    "fastapi-utils": re.compile(r"^fastapi_utils\.tasks\.repeat_every$"),
    "fastapi-crons": re.compile(r"^fastapi_crons\.Crons$"),
    "aiocron": re.compile(r"^aiocron\.crontab$"),
    "schedule": re.compile(r"^schedule\.(every|Scheduler)$"),
    "rocketry": re.compile(r"^rocketry\.Rocketry$"),
}

# ============================================================================
# SQL
# ============================================================================

_SQL_FIRST = {"SELECT": "READ", "WITH": "READ", "SHOW": "READ", "DESCRIBE": "READ", "EXPLAIN": "READ", "VALUES": "READ",
              "INSERT": "WRITE", "UPDATE": "WRITE", "MERGE": "WRITE", "UPSERT": "WRITE", "REPLACE": "WRITE", "COPY": "WRITE",
              "DELETE": "DELETE", "TRUNCATE": "DELETE", "DROP": "DELETE", "CALL": "CALL", "EXEC": "CALL", "EXECUTE": "CALL",
              "CREATE": "WRITE", "ALTER": "WRITE"}
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([`\"\[]?[\w.]+[`\"\]]?)",
    re.I,
)
_SQL_KEYWORDS = {"select", "set", "values", "lateral", "unnest", "generate_series", "only", "dual", "table", "from", "where", "join", "as"}


def looks_like_sql(value: str) -> bool:
    return bool(re.match(r"\s*(select|insert|update|delete|with|merge|replace|truncate|create|alter|drop|call|exec|execute|upsert|copy|show)\b",
                         value, re.I)) and len(value) > 6


def parse_sql(sql: str) -> Tuple[Optional[str], List[str]]:
    """(operation, tables) of a SQL statement."""
    compact = re.sub(r"\s+", " ", sql or "").strip()
    if not compact:
        return None, []
    first = compact.split(" ", 1)[0].upper().rstrip(";(")
    operation = _SQL_FIRST.get(first)
    tables = []
    for match in _TABLE_REF.finditer(compact):
        name = match.group(1).strip('`"[]')
        if name and name.lower() not in _SQL_KEYWORDS and not name.startswith(("(", ":", "%", "$", "{")) and name not in tables:
            tables.append(name)
    return operation, tables


# ============================================================================
# DEPENDENCIES (module summary)
# ============================================================================

# package -> (category, technology)
PACKAGES: Dict[str, Tuple[str, str]] = {
    "fastapi": ("web", "FastAPI"), "starlette": ("web", "Starlette"), "flask": ("web", "Flask"), "django": ("web", "Django"),
    "uvicorn": ("runtime", "Uvicorn"), "gunicorn": ("runtime", "Gunicorn"), "hypercorn": ("runtime", "Hypercorn"),
    "granian": ("runtime", "Granian"), "websockets": ("web", "WebSockets"), "sse-starlette": ("web", "Server-sent events"),
    "strawberry-graphql": ("web", "GraphQL (Strawberry)"), "graphene": ("web", "GraphQL (Graphene)"), "ariadne": ("web", "GraphQL (Ariadne)"),
    "grpcio": ("web", "gRPC"), "fastapi-versioning": ("web", "API versioning"),
    "pydantic": ("validation", "Pydantic"), "pydantic-settings": ("config", "Pydantic Settings"), "python-dotenv": ("config", "dotenv"),
    "dynaconf": ("config", "Dynaconf"), "python-decouple": ("config", "decouple"),
    "sqlalchemy": ("database", "SQLAlchemy"), "sqlmodel": ("database", "SQLModel"), "alembic": ("database", "Alembic migrations"),
    "psycopg2": ("database", "PostgreSQL"), "psycopg2-binary": ("database", "PostgreSQL"), "psycopg": ("database", "PostgreSQL"),
    "psycopg-binary": ("database", "PostgreSQL"), "asyncpg": ("database", "PostgreSQL"), "pg8000": ("database", "PostgreSQL"),
    "pymysql": ("database", "MySQL"), "mysqlclient": ("database", "MySQL"), "aiomysql": ("database", "MySQL"), "asyncmy": ("database", "MySQL"),
    "mysql-connector-python": ("database", "MySQL"), "aiosqlite": ("database", "SQLite"), "oracledb": ("database", "Oracle"),
    "cx-oracle": ("database", "Oracle"), "pyodbc": ("database", "SQL Server"), "pymssql": ("database", "SQL Server"),
    "databases": ("database", "databases (async SQL)"), "tortoise-orm": ("database", "Tortoise ORM"), "peewee": ("database", "Peewee"),
    "ormar": ("database", "Ormar"), "pony": ("database", "Pony ORM"), "piccolo": ("database", "Piccolo"), "prisma": ("database", "Prisma"),
    "pymongo": ("database", "MongoDB"), "motor": ("database", "MongoDB"), "beanie": ("database", "Beanie (MongoDB)"),
    "mongoengine": ("database", "MongoEngine"), "odmantic": ("database", "ODMantic (MongoDB)"), "cassandra-driver": ("database", "Cassandra"),
    "neo4j": ("database", "Neo4j"), "boto3-dynamodb": ("database", "DynamoDB"), "pynamodb": ("database", "DynamoDB"),
    "clickhouse-driver": ("database", "ClickHouse"), "clickhouse-connect": ("database", "ClickHouse"), "duckdb": ("database", "DuckDB"),
    "redis": ("cache", "Redis"), "aioredis": ("cache", "Redis"), "fastapi-cache2": ("cache", "fastapi-cache"), "fastapi-cache": ("cache", "fastapi-cache"),
    "aiocache": ("cache", "aiocache"), "cachetools": ("cache", "cachetools"), "pymemcache": ("cache", "Memcached"), "cashews": ("cache", "cashews"),
    "celery": ("messaging", "Celery"), "kafka-python": ("messaging", "Kafka"), "aiokafka": ("messaging", "Kafka"),
    "confluent-kafka": ("messaging", "Kafka"), "pika": ("messaging", "RabbitMQ"), "aio-pika": ("messaging", "RabbitMQ"),
    "kombu": ("messaging", "Kombu"), "faststream": ("messaging", "FastStream"), "nats-py": ("messaging", "NATS"),
    "rq": ("messaging", "RQ"), "arq": ("messaging", "arq"), "dramatiq": ("messaging", "Dramatiq"), "taskiq": ("messaging", "Taskiq"),
    "google-cloud-pubsub": ("messaging", "Google Pub/Sub"), "azure-servicebus": ("messaging", "Azure Service Bus"),
    "paho-mqtt": ("messaging", "MQTT"), "aiomqtt": ("messaging", "MQTT"), "gmqtt": ("messaging", "MQTT"),
    "boto3": ("cloud", "AWS SDK"), "aioboto3": ("cloud", "AWS SDK"), "botocore": ("cloud", "AWS SDK"), "minio": ("storage", "MinIO"),
    "google-cloud-storage": ("storage", "Google Cloud Storage"), "azure-storage-blob": ("storage", "Azure Blob Storage"),
    "aiofiles": ("storage", "aiofiles"), "python-multipart": ("web", "Multipart forms"),
    "httpx": ("http-client", "HTTPX"), "requests": ("http-client", "Requests"), "aiohttp": ("http-client", "aiohttp"),
    "urllib3": ("http-client", "urllib3"), "grpclib": ("http-client", "gRPC"), "zeep": ("http-client", "SOAP"),
    "elasticsearch": ("search", "Elasticsearch"), "opensearch-py": ("search", "OpenSearch"), "meilisearch": ("search", "Meilisearch"),
    "typesense": ("search", "Typesense"), "algoliasearch": ("search", "Algolia"),
    "aiosmtplib": ("email", "SMTP"), "fastapi-mail": ("email", "fastapi-mail"), "sendgrid": ("email", "SendGrid"),
    "emails": ("email", "emails"), "yagmail": ("email", "SMTP"), "resend": ("email", "Resend"),
    "python-jose": ("security", "JWT (python-jose)"), "pyjwt": ("security", "JWT (PyJWT)"), "authlib": ("security", "Authlib (OAuth)"),
    "passlib": ("security", "Passlib"), "bcrypt": ("security", "bcrypt"), "argon2-cffi": ("security", "Argon2"),
    "fastapi-users": ("security", "fastapi-users"), "fastapi-jwt-auth": ("security", "JWT"), "fastapi-login": ("security", "fastapi-login"),
    "fastapi-security": ("security", "fastapi-security"), "fastapi-azure-auth": ("security", "Azure AD"), "python-keycloak": ("security", "Keycloak"),
    "fastapi-keycloak": ("security", "Keycloak"), "oauthlib": ("security", "OAuth"), "cryptography": ("security", "cryptography"),
    "slowapi": ("security", "Rate limiting (slowapi)"), "fastapi-limiter": ("security", "Rate limiting"), "itsdangerous": ("security", "itsdangerous"),
    "authx": ("security", "AuthX"), "casbin": ("security", "Casbin"), "fastapi-permissions": ("security", "fastapi-permissions"),
    "apscheduler": ("scheduling", "APScheduler"), "fastapi-utils": ("scheduling", "fastapi-utils"), "aiocron": ("scheduling", "aiocron"),
    "fastapi-crons": ("scheduling", "fastapi-crons"), "rocketry": ("scheduling", "Rocketry"), "schedule": ("scheduling", "schedule"),
    "tenacity": ("resilience", "tenacity"), "backoff": ("resilience", "backoff"), "circuitbreaker": ("resilience", "circuit breaker"),
    "pybreaker": ("resilience", "circuit breaker"),
    "opentelemetry-api": ("observability", "OpenTelemetry"), "opentelemetry-sdk": ("observability", "OpenTelemetry"),
    "prometheus-client": ("observability", "Prometheus"), "prometheus-fastapi-instrumentator": ("observability", "Prometheus"),
    "sentry-sdk": ("observability", "Sentry"), "structlog": ("observability", "structlog"), "loguru": ("observability", "loguru"),
    "ddtrace": ("observability", "Datadog"), "newrelic": ("observability", "New Relic"),
    "pytest": ("test", "pytest"), "pytest-asyncio": ("test", "pytest-asyncio"), "pytest-cov": ("test", "pytest-cov"), "httpretty": ("test", "httpretty"),
    "anyio": ("test", "anyio"), "faker": ("test", "Faker"), "factory-boy": ("test", "factory_boy"), "mypy": ("test", "mypy"),
    "ruff": ("test", "ruff"), "black": ("test", "black"), "flake8": ("test", "flake8"), "pre-commit": ("test", "pre-commit"), "coverage": ("test", "coverage"),
    "jinja2": ("web", "Jinja2 templates"), "orjson": ("web", "orjson"), "ujson": ("web", "ujson"), "pandas": ("data", "pandas"), "numpy": ("data", "NumPy"),
    "scikit-learn": ("ml", "scikit-learn"), "torch": ("ml", "PyTorch"), "tensorflow": ("ml", "TensorFlow"), "transformers": ("ml", "Transformers"),
    "openai": ("ml", "OpenAI"), "anthropic": ("ml", "Anthropic"), "langchain": ("ml", "LangChain"),
}

IGNORED_CATEGORIES = {"test"}


def normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


_NORMALIZED_PACKAGES = {normalize_package(k): v for k, v in PACKAGES.items()}


def package_info(name: str) -> Optional[Tuple[str, str]]:
    return _NORMALIZED_PACKAGES.get(normalize_package(name))


# env var / setting names -> infrastructure kind
INFRA_KEYS = [(re.compile(p, re.I), kind) for p, kind in [
    (r"(^|_)(database|db|sqlalchemy|postgres|postgresql|pg|mysql|sql)_?(url|uri|dsn|connection)(_string)?$|^sqlalchemy_database_(url|uri)$", "database"),
    (r"(^|_)(mongo|mongodb)_?(url|uri|dsn|host)$", "mongodb"),
    (r"(^|_)redis_?(url|uri|dsn|host)$|^cache_?(url|uri)$", "redis"),
    (r"(^|_)(celery_)?(broker_?url|result_backend)$", "celery-broker"),
    (r"(^|_)(kafka)_?(bootstrap_?servers|brokers?|servers?)$", "kafka"),
    (r"(^|_)(rabbit(mq)?|amqp)_?(url|uri|host|dsn)$", "rabbitmq"),
    (r"(^|_)(elasticsearch|elastic|opensearch)_?(url|uri|host|hosts)$", "elasticsearch"),
    (r"(^|_)smtp_?(host|server)$|^mail_server$", "smtp"),
    (r"(^|_)(s3|minio)_?(endpoint|bucket|url)(_name)?$|^aws_s3_bucket$", "s3"),
    (r"(^|_)nats_?(url|servers?)$", "nats"),
]]
URL_SCHEMES = {
    "postgresql": "postgresql", "postgres": "postgresql", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite", "oracle": "oracle",
    "mssql": "sqlserver", "mongodb": "mongodb", "redis": "redis", "rediss": "redis", "amqp": "rabbitmq", "amqps": "rabbitmq",
    "kafka": "kafka", "nats": "nats", "elasticsearch": "elasticsearch", "memcached": "memcached", "clickhouse": "clickhouse",
    "cockroachdb": "postgresql", "snowflake": "snowflake", "bigquery": "bigquery", "sqs": "sqs",
}
SECRET_KEY = re.compile(r"(secret|password|passwd|pwd|token|api_?key|private|credential|signing|auth_key|dsn)", re.I)


def mask_url(value: str) -> str:
    return re.sub(r"(://[^:/@\s]+:)[^@/\s]+@", r"\1***@", value)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_KEY.search(name))


KNOWN_EXCEPTION_STATUS = {"RequestValidationError": 422, "WebSocketRequestValidationError": 1008}
