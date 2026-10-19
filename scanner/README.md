# mergeclear CLI

Static analysis for API contracts and dependencies. It parses a repository
with tree-sitter (no compilation, no running the app), writes a report, and
compares reports to give a change a verdict: **Clear**, **Review** or **Hold**.
It only talks to git and the file system, so it runs the same on a laptop, in
any CI system, in a git hook or a cron job.

```bash
pipx install "git+https://github.com/ManojVihari/RepoPilot.git@v0.3.0#subdirectory=scanner"   # or: pipx install ./scanner

mergeclear check --base origin/main                      # this checkout vs main: verdict + exit code
mergeclear scan --out report.json                        # every endpoint of the current folder
mergeclear scan --since origin/main --out changes.json   # only endpoints touched since a ref
mergeclear scan --project shop --push https://mergeclear.internal   # send to a server (MERGECLEAR_API_KEY)
mergeclear push report.json --server https://...         # upload a saved report
mergeclear diff base.json head.json --format markdown    # compare two reports
mergeclear check head.json --against base.json --fail-on review
```

Exit codes: `0` ok / clear, `1` check failed, `2` error. Logs go to stderr, so
stdout stays clean JSON or Markdown. `-v` shows the scanner's own progress.
See the [main README](../README.md) for settings and pipeline recipes.

`--commit SHA` scans the endpoints a single commit touched (it diffs
`<commit>^..<commit>`; without a parent, as on a first commit or a shallow
clone, every tracked file counts as changed). Removed endpoints are only
detected when both compared reports are full scans.

For local testing, `scripts/document_local_repo.py` scans a folder and
generates the server's docs, versions and architecture model in-process,
without a running server.

## Output

```jsonc
{
  "scanner_version": "2.0",
  "scanner": { "name": "mergeclear", "version": "0.3.0" },
  "repository": "shop",
  "commit": "abc123...",
  "branch": "main",                // null on detached checkouts unless --branch is given
  "dirty": false,                  // uncommitted edits were scanned
  "scan_mode": "full",             // or "changes" (--commit / --since)
  "scanned_at": "2026-10-09T10:00:00+00:00",
  "frameworks": ["spring"],
  "routes": [ /* every endpoint, or those a change touched; see below */ ],
  "architecture": { "spring": { /* application model, see below */ } }
}
```

### `routes[]` - endpoints (Spring and FastAPI)

A full scan lists every endpoint. With `--commit`/`--since` only endpoints
affected by the change are listed, and impact is precise: git diff
hunks are mapped to the methods they touch, then followed backwards through
the resolved call graph (interface calls dispatch to their implementations,
`@EventListener`s are linked to `publishEvent`). Changes to DTOs, entities and
repositories impact every endpoint that uses them.

| Field | Content |
|---|---|
| `function`, `method`, `path`, `paths`, `context_path` | Handler name, HTTP method (`ANY` for `@RequestMapping` without method), path(s), `server.servlet.context-path` |
| `handler`, `controller`, `module`, `file`, `line` | Where the endpoint lives |
| `summary`, `description`, `deprecated` | `@Operation`/`@ApiOperation` and Javadoc |
| `params[]` | `name`, `type`, `in` (`path`/`query`/`header`/`cookie`/`body`/`model`/`multipart`), `required`, `default`, `validation`, `schema` (DTO fields, nested) |
| `request_body` | `type`, `required`, `validated` (`@Valid`), `schema` |
| `response` | `type` (declared), `body_type` (unwrapped from `ResponseEntity`/`Optional`/`Mono`...), `schema`, `view` for MVC pages |
| `consumes`, `produces` | From the mapping annotation |
| `status_codes[]` | `{code, reason, source}` from `@ResponseStatus`, `ResponseEntity` builders, `ResponseStatusException`, `@ExceptionHandler` mappings, bean validation |
| `exceptions[]` | Exceptions thrown anywhere in the call chain with the status they map to (`mapped_by`: handler, `@ResponseStatus`, Spring default or `unhandled` → 500) |
| `security` | Method annotations (`@PreAuthorize`, `@Secured`, `@RolesAllowed`) and the first matching URL rule of the `SecurityFilterChain` / `HttpSecurity` config |
| `integrations` | Everything reached through the call chain, grouped: `databases`, `caches`, `messaging`, `external_apis`, `events`, `storage`, `email`, `search`, `transactions`, `resilience`, `async`. Each fact names its `source` method |
| `call_graph` | `direct` and `full` (resolved `Class.method`), `tree` (nested, depth 6) |
| `change_reasons` | Why the endpoint is impacted (`changed: OrderService.create`, `changed model: LineItem`, ...) |
| `breaking_changes[]` | Contract changes of changed DTOs: `FIELD_ADDED` (with `required`), `FIELD_REMOVED`, `TYPE_CHANGED`, `VALIDATION_ADDED/REMOVED/CHANGED` |
| `errors`, `calls`, `db_ops`, `impact`, `referenced_types` | Compact forms kept for existing consumers |

Integration facts, by kind:

| Kind | Detected from | Key fields |
|---|---|---|
| `database` | Spring Data repositories (JPA, Mongo, R2DBC, Elasticsearch, Cassandra, Neo4j...), `@Query`, MyBatis mappers (XML and `@Select`...), `JdbcTemplate`/`JdbcClient`, `EntityManager`, `MongoTemplate` | `technology`, `operation` (READ/WRITE/DELETE), `table[]`, `entity`, `query`, `call` |
| `cache` | `@Cacheable`/`@CachePut`/`@CacheEvict`/`@Caching`, `RedisTemplate` & co, `CacheManager`, Caffeine | `operation`, `cache_names`, `key`, `command` |
| `messaging` | `KafkaTemplate`, `RabbitTemplate`/`AmqpTemplate`, `JmsTemplate`, `StreamBridge` (+ bindings from config), SQS, SNS, Pub/Sub, Redis pub/sub, `@SendTo`, functional stream beans | `direction`, `technology`, `destination`, `exchange`/`routing_key`, `payload_type` |
| `http` | `RestTemplate`, `WebClient`, `RestClient` (base URL from `@Bean` builders), Feign clients, `java.net.http`, OkHttp, Apache HttpClient, `DiscoveryClient` lookups | `client`, `http_method`, `url`, `base_url`, `host`, `target_service`/`target_module` (when it is another module of the repo) |
| `event` | `ApplicationEventPublisher.publishEvent` | `event_type` (listeners are linked into the call graph) |
| `storage`, `email`, `search` | S3/GCS/Azure clients, `JavaMailSender`, Elasticsearch clients | `bucket`, `operation`, `call` |
| `transaction`, `resilience`, `async` | `@Transactional`, `@CircuitBreaker`/`@Retry`/..., `@Async` | `read_only`, `name`, `fallback` |

String values (URLs, topics, keys, SQL) are resolved through constants,
`@Value` fields, local variables, enum constants and the module configuration;
unresolved parts appear as `{name}` placeholders.

### `architecture.spring` - application model

| Key | Content |
|---|---|
| `summary` | Counts, Spring Boot apps, technology stack by category, `monolith`/`microservices` |
| `modules[]` | Maven/Gradle module: coordinates, Java/Spring Boot/Spring Cloud versions, classified `dependencies`, `technologies`, `runtime` (app name, port, context path, datasources, MongoDB, Redis, Kafka, RabbitMQ, discovery, config server, identity provider, gateway/Zuul routes, stream bindings, external URLs), `properties` and `profile_properties` (secrets masked), `main_class`, `client_base_urls` |
| `components[]` | Spring beans with `stereotype`/`layer`, annotations, Javadoc, injected `dependencies` (field/constructor/setter, interface → implementations), `config_properties`, public methods, `@Bean` definitions, integrations used |
| `data_model` | `entities` (table/collection, fields, ids, columns, relationships), `repositories` (store, entity, id type, custom queries with operation and tables, MyBatis mapper XML), `tables` (operations and accessing methods) |
| `endpoints[]` | Every endpoint (not only impacted ones), compact: method, path, request/response types, status codes, tables, caches, published destinations, external calls |
| `integrations` | App-wide facts: databases, caches, messaging (`producers`, `consumers`, `destinations` pairing producers with consumers), external APIs, Feign clients, events, scheduled jobs, storage, email, search, resilience |
| `security` | Technologies, mechanisms (JWT, OAuth2 resource server, form login...), CSRF, URL rules |
| `exception_handling` | Exception → status mappings (`@ExceptionHandler`, `@ResponseStatus`) |
| `graph` | `nodes` (modules, components, datastores, caches, brokers, external services, infrastructure) and `edges` (`contains`, `injects`, `calls`, `read`/`write`, `publishes`, `delivers`, `http`, `routes`, `connects`, `registers`, `reads_config`, `triggers`) ready to render |

Test sources (`src/test`, `src/it`) are excluded. Nothing is executed: values
only known at runtime (environment variables without defaults, values built
from method parameters) stay as placeholders.

## FastAPI

`mergeclear` reads FastAPI applications with the same depth as Spring ones. It
follows the structure of the code, not just the decorators: apps, `APIRouter`s,
`include_router(..., prefix=)` chains (also in loops and factories such as
`create_app()`), `app.mount(...)` sub-applications, `add_api_route`, class-based
views and `root_path`. A router included twice yields one endpoint with both `paths`.

What a route reports, in the same fields as Spring:

| Field | FastAPI source |
|---|---|
| `path`, `paths`, `context_path`, `tags` | decorator path + router `prefix` + every `include_router` prefix; `FastAPI(root_path=)` |
| `params[]` | the signature **and** every dependency's: `Query`/`Path`/`Header`/`Cookie`/`Body`/`Form`/`File`, `Annotated[..., Query(ge=1)]` and aliases such as `SessionDep`, Pydantic models as body or as `Query()`/`Form()` groups, `OAuth2PasswordRequestForm`, constraints, defaults, header names (`x_token` → `x-token`, `convert_underscores`) |
| `request_body`, `response` | Pydantic v1/v2 models, SQLModel, dataclasses and `TypedDict`: `Field(...)` constraints, `EmailStr`/`PositiveInt`/`conint`, `@field_validator`, enums and `Literal`, nested models, `list[...]`/`dict[...]`, generics (`Page[Item]`), inheritance, `response_model` (and `_exclude`/`_include`, `Field(exclude=True)`), return annotations, `response_class`, streaming |
| `status_codes[]`, `exceptions[]` | `status_code=`, `responses={...}`, `raise HTTPException(...)` anywhere in the call chain, `HTTPException` subclasses, `@app.exception_handler(...)`/`add_exception_handler` (status read from the handler), `JSONResponse(status_code=)`, 422 request validation, 401 from the security scheme |
| `security` | `Depends`/`Security` chains down to `OAuth2PasswordBearer`, `HTTPBearer`, `APIKeyHeader`, ... at app, router, include and route level; scopes and role arguments (`Depends(require_role("admin"))`) |
| `integrations`, `call_graph` | the call chain through functions, methods, `Depends` targets, class and callable-class dependencies, dependency factories, `BackgroundTasks.add_task`, `asyncio`/thread-pool hand-offs, and base-class methods bound to their model (`CRUDBase(Item)`) |

Integration detection (the inferred type of `db`, `redis_client`, `self.repo`... decides what a call is):

| Kind | Libraries |
|---|---|
| `database` | SQLAlchemy (ORM sessions, async, Core, `text()`), SQLModel, raw drivers (`asyncpg`, `psycopg`, `sqlite3`, `aiomysql`, `databases`), MongoDB (`pymongo`, `motor`, Beanie, MongoEngine), Tortoise, Peewee, Ormar, DynamoDB; tables from `__tablename__`, `Table(...)`, SQLModel, `Meta`/`Settings` classes and SQL text |
| `cache` | Redis (`redis`, `redis.asyncio`, `aioredis`), Memcached, `fastapi-cache`, `aiocache` |
| `messaging` | Kafka (`aiokafka`, `kafka-python`, `confluent-kafka`), RabbitMQ (`pika`, `aio-pika`), Celery/RQ/arq/Dramatiq tasks, FastStream, NATS, SQS/SNS/EventBridge/Kinesis, Pub/Sub, MQTT; consumers, `@task`s and schedulers (APScheduler, `repeat_every`, Celery beat) become listeners and jobs |
| `http` | `httpx`, `requests`, `aiohttp`, `urllib` (base URL of clients, `settings.X` and `.env` values resolved) |
| `storage`, `email`, `search` | S3/GCS/Azure/MinIO, SMTP/SES/SendGrid/fastapi-mail, Elasticsearch/OpenSearch/Meilisearch |
| `resilience`, `async`, `transaction` | `tenacity`/`backoff`/circuit breakers/`slowapi`, background tasks and executors, `commit`/`begin` |

`architecture.fastapi` has the same keys as `architecture.spring`: `modules[]` (one per
project folder with a `pyproject.toml`, `requirements*.txt`, `Pipfile` or `setup.py`; runtime derived
from `.env` files, `Settings` classes and `docker-compose`), `components[]` (routers, services,
repositories, clients, listeners, plain-function modules), `data_model` (entities, tables,
relationships), `integrations`, `security` (mechanisms, CORS and other middleware),
`exception_handling` and `graph`.

With `--commit`/`--since`, impact works as for Spring: changed lines are mapped to functions
and classes, followed through the call graph, and a change to a Pydantic model, an ORM entity, a
dependency, a router `prefix` or an `include_router` line reports the endpoints it affects with
`change_reasons` and `breaking_changes`. Tests (`tests/`, `test_*.py`, `conftest.py`) and
migrations are excluded. Nothing is imported or run.
