"""
FastAPI support: routes, contracts, dependencies, integrations and the application model, checked against
three fixture applications (an order service, the SQLModel project template and a grab bag of unusual patterns)
plus small inline sources for individual features.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from mergeclear import contracts
from mergeclear.core.scanner import Scanner
from mergeclear.plugins.python_fastapi.extractor import FastAPIExtractor
from mergeclear.plugins.python_fastapi.project import _mini_toml, load_toml, parse_pyproject

FIXTURES = Path(__file__).parent / "fixtures"
_cache = {}


def scan(name):
    """(routes by (method, path), architecture) of a fixture application."""
    if name not in _cache:
        extractor = FastAPIExtractor()
        routes = extractor.all_routes(str(FIXTURES / name))
        _cache[name] = ({(r["method"], r["path"]): r for r in routes}, extractor.architecture, extractor)
    return _cache[name]


def scan_source(tmp_path, files):
    """Routes of an application written inline: {path: source}."""
    for rel, source in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))
    routes = FastAPIExtractor().all_routes(str(tmp_path))
    return {(r["method"], r["path"]): r for r in routes}


def codes(route):
    return {s["code"] for s in route["status_codes"]}


def param(route, name, location=None):
    matches = [p for p in route["params"] if p["name"] == name and (location is None or p["in"] == location)]
    assert matches, f"no parameter {name} in {[p['name'] for p in route['params']]}"
    return matches[0]


# ============================================================================
# ORDER SERVICE
# ============================================================================

def test_routes_get_the_full_path_through_nested_prefixes_and_includes():
    routes, _, _ = scan("fastapi_shop")

    assert set(routes) == {
        ("POST", "/api/v1/orders"), ("GET", "/api/v1/orders/{order_id}"), ("GET", "/api/v1/orders"),
        ("DELETE", "/api/v1/orders/{order_id}"), ("POST", "/api/v1/admin/invoices/{order_id}"), ("POST", "/api/v1/files"),
        ("GET", "/health"), ("WEBSOCKET", "/ws/orders"),
    }
    create = routes[("POST", "/api/v1/orders")]
    assert create["handler"] == "orders.create_order"
    assert create["controller"] == "app.api.routes.orders"
    assert create["summary"] == "Place an order"
    assert create["description"] == "Places a new order."
    assert create["tags"] == ["orders"]
    assert create["doc_name"] == "orders.create_order"
    assert routes[("GET", "/health")]["include_in_schema"] is False


def test_request_body_schema_carries_pydantic_constraints():
    create = scan("fastapi_shop")[0][("POST", "/api/v1/orders")]
    body = create["request_body"]

    assert body["type"] == "CreateOrderRequest" and body["required"] and body["validated"]
    fields = body["schema"]
    assert fields["customer_email"] == {"type": "EmailStr", "validation": {"email": True, "required": True}}
    assert fields["lines"]["validation"] == {"min": 1, "max": 20, "required": True}
    assert fields["lines"]["schema"]["items"]["fields"]["quantity"]["validation"] == {"minimum": 1, "required": True}
    assert fields["payment_method"] == {"type": "PaymentMethod", "enum": ["CARD", "PAYPAL"]}
    assert fields["note"]["type"] == "str | None"
    assert fields["note"]["validation"] == {"max": 500, "custom": True}      # @field_validator("note")
    assert create["consumes"] == ["application/json"]


def test_parameters_headers_aliases_and_annotated_constraints():
    routes, _, _ = scan("fastapi_shop")

    header = param(routes[("POST", "/api/v1/orders")], "x-request-id", "header")
    assert header["required"] is True

    listing = routes[("GET", "/api/v1/orders")]
    assert param(listing, "skip") == {"name": "skip", "type": "int", "in": "query", "required": False, "default": 0}
    assert param(listing, "limit")["validation"] == {"maximum": 100}
    assert param(listing, "status", "query")["type"] == "str | None"          # Query(alias="status")
    assert not any(p["name"] == "status_filter" for p in listing["params"])

    assert param(routes[("GET", "/api/v1/orders/{order_id}")], "order_id")["in"] == "path"


def test_response_model_hides_excluded_fields_and_resolves_generics():
    routes, _, _ = scan("fastapi_shop")

    single = routes[("GET", "/api/v1/orders/{order_id}")]["response"]
    assert single["body_type"] == "OrderResponse"
    assert set(single["schema"]) == {"id", "status", "customer_email", "secret_hash"} - {"secret_hash"}

    page = routes[("GET", "/api/v1/orders")]["response"]
    assert page["body_type"] == "Page[OrderResponse]"
    assert page["schema"]["items"]["type"] == "list[OrderResponse]"          # Page[T] bound to OrderResponse
    assert page["schema"]["items"]["schema"]["items"]["name"] == "OrderResponse"

    assert routes[("DELETE", "/api/v1/orders/{order_id}")]["response"]["schema"] is None      # 204


def test_status_codes_come_from_decorator_handlers_exception_classes_and_validation():
    routes, _, _ = scan("fastapi_shop")
    create = routes[("POST", "/api/v1/orders")]

    assert codes(create) == {201, 401, 402, 409, 422}
    by_source = {(s["code"], s["source"]) for s in create["status_codes"]}
    assert (402, "PaymentDeclined (main.payment_declined_handler)") in by_source        # @app.exception_handler
    assert (409, "InventoryConflict (HTTPException)") in by_source                      # super().__init__(status_code=409)
    assert (201, "status_code") in by_source and not any(c == 200 for c in codes(create))
    assert {"PAYMENT_REQUIRED", "CONFLICT", "UNAUTHORIZED"} <= {e for e in create["errors"] if isinstance(e, str)}

    errors = [e for e in create["errors"] if isinstance(e, dict)][0]
    assert errors["type"] == "VALIDATION_ERROR" and errors["status"] == 422
    assert {"name": "customer_email", "rule": "email"} in errors["fields"]

    read = routes[("GET", "/api/v1/orders/{order_id}")]
    assert {(s["code"], s["source"]) for s in read["status_codes"]} >= {
        (404, "HTTPException"), (404, "OrderNotFound (main.order_not_found_handler)"), (404, "responses")}
    assert read["responses"] == [{"code": 404, "description": "Not found"}]


def test_exceptions_are_mapped_to_the_handler_that_answers_them():
    create = scan("fastapi_shop")[0][("POST", "/api/v1/orders")]
    exceptions = {e["exception"]: e for e in create["exceptions"]}

    assert exceptions["PaymentDeclined"]["status"] == 402
    assert exceptions["PaymentDeclined"]["mapped_by"] == "main.payment_declined_handler"
    assert exceptions["PaymentDeclined"]["thrown_in"] == "OrderService.create"
    assert exceptions["InventoryConflict"]["status"] == 409


def test_security_from_dependencies_and_roles():
    routes, _, _ = scan("fastapi_shop")

    create = routes[("POST", "/api/v1/orders")]["security"]
    assert create["annotations"] == [{"annotation": "Depends", "expression": "get_current_user", "level": "method", "scheme": "oauth2-password"}]
    assert create["url_rule"]["access"] == "authenticated"
    assert 401 in codes(routes[("POST", "/api/v1/orders")])

    admin = routes[("POST", "/api/v1/admin/invoices/{order_id}")]["security"]      # APIRouter(dependencies=[Depends(require_role("admin"))])
    assert admin["annotations"][0]["level"] == "router"
    assert admin["url_rule"]["access"] == "hasRole" and admin["url_rule"]["roles"] == ["admin"]

    cancel = routes[("DELETE", "/api/v1/orders/{order_id}")]["security"]           # dependencies=[...] on the decorator
    assert cancel["url_rule"]["roles"] == ["admin"]
    assert routes[("GET", "/health")]["security"] is None


def test_integrations_follow_the_call_chain_across_modules_and_classes():
    create = scan("fastapi_shop")[0][("POST", "/api/v1/orders")]
    integrations = create["integrations"]

    databases = {(f["operation"], tuple(f["table"]), f["source"]) for f in integrations["databases"]}
    assert ("WRITE", ("audit_log",), "OrderService.create") in databases
    assert ("WRITE", ("orders",), "CRUDBase.create") in databases        # self.model bound to Order by CRUDOrder(Order)

    assert [(f["technology"], f["operation"], f["key"]) for f in integrations["caches"]] == [("redis", "WRITE", "order:{order.id}")]
    destinations = {(f["technology"], f["destination"]) for f in integrations["messaging"]}
    assert destinations == {("kafka", "orders.created.v1"), ("celery", "shop.send_receipt")}

    external = {(f["http_method"], f["url"], f["host"]) for f in integrations["external_apis"]}
    assert ("POST", "https://api.payments.example.com/v1/charges/{order_id}", "api.payments.example.com") in external     # base_url + path
    assert ("GET", "http://inventory-service/stock/{sku}", "inventory-service") in external                           # settings default
    assert any(f.get("target_service") == "inventory-service" for f in integrations["external_apis"])
    assert integrations["transactions"]

    assert [op["call"] for op in create["db_ops"]][0].startswith("Session.")


def test_call_graph_includes_dependencies_and_services():
    create = scan("fastapi_shop")[0][("POST", "/api/v1/orders")]
    graph = create["call_graph"]

    assert {"security.get_current_user", "deps.get_order_service", "OrderService.create"} <= set(graph["direct"])
    assert {"CRUDBase.create", "PaymentClient.charge", "payments.stock_level", "session.get_db"} <= set(graph["full"])
    assert graph["tree"]["orders.create_order"]["security.get_current_user"] == {"security.decode_token": {}}
    assert create["impact"] == graph["full"]
    # exception and model constructors are not part of the flow
    assert not any("__init__" in label and ("Conflict" in label or "Declined" in label) for label in graph["full"])


def test_background_tasks_and_other_dependency_shapes():
    routes, _, _ = scan("fastapi_shop")
    cancel = routes[("DELETE", "/api/v1/orders/{order_id}")]

    assert [(f["technology"], f["target"]) for f in cancel["integrations"]["async"]] == [("background-task", "orders.notify")]
    assert "orders.notify" in cancel["call_graph"]["full"]
    assert cancel["integrations"]["databases"][0]["table"] == ["orders"]
    assert any(f["operation"] == "DELETE" for f in cancel["integrations"]["databases"])

    upload = routes[("POST", "/api/v1/files")]
    assert param(upload, "file")["in"] == "multipart" and param(upload, "description")["in"] == "form"
    assert upload["consumes"] == ["multipart/form-data"]
    assert routes[("WEBSOCKET", "/ws/orders")]["protocol"] == "websocket"


def test_architecture_model_of_the_order_service():
    _, architecture, _ = scan("fastapi_shop")
    summary = architecture["summary"]

    assert summary["endpoints"] == 8 and summary["entities"] == 3 and summary["pydantic"] == "v2"
    assert summary["spring_boot_apps"] == ["Shop Service"] and summary["message_listeners"] == 1
    assert summary["middleware"] == ["cors"]

    (module,) = architecture["modules"]
    assert module["name"] == "shop-service" and module["version"] == "1.4.0" and module["main_class"] == "app.main:app"
    assert module["runtime"]["datasources"] == [{"type": "postgresql", "host": "db.internal", "database": "shop", "profile": "default"}]
    assert module["runtime"]["redis"]["host"] == "cache.internal"
    assert module["runtime"]["rabbitmq"]["host"] == "rabbit.internal"
    assert module["runtime"]["kafka"]["bootstrap_servers"] == "kafka.internal:9092"
    assert "database: SQLAlchemy" in module["technologies"] and "messaging: Celery" in module["technologies"]
    assert module["properties"]["SECRET_KEY"] == "***"

    entities = {e["name"]: e for e in architecture["data_model"]["entities"]}
    assert entities["Order"]["table"] == "orders" and entities["Order"]["relationships"][0]["type"] == "OneToMany"
    assert entities["OrderLine"]["fields"][1]["foreign_key"] == "orders.id"
    tables = {t["name"]: t for t in architecture["data_model"]["tables"]}
    assert tables["orders"]["operations"] == ["DELETE", "READ", "WRITE"]
    assert "CRUDBase.create" in tables["orders"]["accessed_by"]

    components = {c["qualified_name"]: c for c in architecture["components"]}
    assert components["app.services.order_service.OrderService"]["stereotype"] == "service"
    assert {d["field"] for d in components["app.services.order_service.OrderService"]["dependencies"]} == {"db", "payments"}
    assert components["app.crud.order.CRUDOrder"]["stereotype"] == "repository"
    assert components["app.api.routes.orders.router"]["stereotype"] == "rest_controller"
    assert components["app.main.app"]["stereotype"] == "application"

    destinations = {(d["technology"], d["destination"]): d for d in architecture["integrations"]["messaging"]["destinations"]}
    assert destinations[("celery", "shop.send_receipt")]["consumers"] == ["worker.send_receipt"]
    assert "OrderService.create" in destinations[("kafka", "orders.created.v1")]["producers"]

    assert {h["exception"] for h in architecture["exception_handling"]} >= {"PaymentDeclined", "OrderNotFound", "InventoryConflict"}
    assert architecture["security"]["mechanisms"] == ["jwt", "oauth2-password", "password-hashing"]
    assert architecture["security"]["cors"]["allow_origins"] == ["https://shop.example.com"]


def test_architecture_graph_connects_components_to_the_systems_they_use():
    graph = scan("fastapi_shop")[1]["graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}

    assert nodes["datastore:postgresql:db.internal/shop"]["type"] == "datastore"
    assert nodes["cache:redis:cache.internal"]["technology"] == "redis"
    assert "broker:kafka" in nodes and "broker:celery" in nodes
    assert "external:inventory-service" in nodes and "external:api.payments.example.com" in nodes

    service = "component:app.services.order_service.OrderService"
    assert (service, "broker:kafka", "publishes") in edges
    assert ("component:app.clients.payments", "external:inventory-service", "http") in edges
    assert ("component:app.crud.order.CRUDOrder", "datastore:postgresql:db.internal/shop", "write") in edges
    assert ("component:app.api.routes.orders.router", service, "calls") in edges
    assert ("component:app.api.routes.orders.router", "component:app.api.deps", "injects") in edges
    assert ("module:", "component:app.main.app", "contains") in edges
    assert any(k == "delivers" and t.endswith("worker") for _, t, k in edges)


# ============================================================================
# SQLMODEL PROJECT TEMPLATE
# ============================================================================

def test_template_app_uses_annotated_aliases_and_nested_routers():
    routes, architecture, _ = scan("fastapi_template")

    assert ("GET", "/api/v1/items/") in routes and ("DELETE", "/api/v1/users/{user_id}") in routes
    read_items = routes[("GET", "/api/v1/items/")]
    assert [p["name"] for p in read_items["params"]] == ["skip", "limit"]          # SessionDep / CurrentUser are not parameters
    assert read_items["response"]["body_type"] == "ItemsPublic"
    assert read_items["security"]["annotations"][0]["expression"] == "get_current_user"

    table_reads = {(f["call"], tuple(f["table"])) for f in read_items["integrations"]["databases"]}
    assert ("Session.exec", ("item",)) in table_reads and ("Session.get", ("user",)) in table_reads
    assert not any(tuple(f["table"]) == ("item", "user") for f in read_items["integrations"]["databases"])      # current_user.id is not a table

    create = routes[("POST", "/api/v1/items/")]
    assert create["request_body"]["type"] == "ItemCreate"
    assert create["request_body"]["schema"]["title"]["validation"] == {"min": 1, "max": 255, "required": True}      # inherited from ItemBase
    assert {(f["operation"], tuple(f["table"])) for f in create["integrations"]["databases"]} >= {("WRITE", ("item",))}

    superuser = routes[("GET", "/api/v1/users/")]["security"]
    assert [a["expression"] for a in superuser["annotations"]] == ["get_current_active_superuser"]
    assert {403, 404} <= codes(routes[("GET", "/api/v1/users/")])

    entities = {e["name"]: e for e in architecture["data_model"]["entities"]}
    assert entities["User"]["table"] == "user" and entities["User"]["table_inferred"] is True
    assert entities["Item"]["fields"][3]["foreign_key"] == "user.id"
    assert {r["type"] for r in entities["User"]["relationships"]} == {"OneToMany"}
    assert architecture["summary"]["entities"] == 2


def test_oauth2_password_request_form_dependency_is_a_form():
    login = scan("fastapi_template")[0][("POST", "/api/v1/login/access-token")]

    assert {p["name"] for p in login["params"] if p["in"] == "form"} == {
        "grant_type", "username", "password", "scope", "client_id", "client_secret"}
    assert param(login, "username")["required"] is True
    assert login["consumes"] == ["application/x-www-form-urlencoded"]
    assert login["security"] is None                      # the login form is not protected by a scheme
    assert 400 in codes(login)


def test_module_names_are_relative_to_the_source_root_of_the_project():
    routes, architecture, extractor = scan("fastapi_template")

    assert routes[("GET", "/api/v1/items/")]["module"] == "backend"
    assert routes[("GET", "/api/v1/items/")]["handler"] == "items.read_items"
    assert [m["path"] for m in architecture["modules"]] == ["backend"]
    # `from app.models import Item` inside backend/app resolves to backend/app/models.py, not to anything named `app`
    assert extractor.index.resolve_module("app.models", extractor.index.by_rel["backend/app/crud.py"]).rel == "backend/app/models.py"


# ============================================================================
# UNUSUAL PATTERNS
# ============================================================================

def test_query_models_class_and_instance_dependencies_and_headers():
    routes, _, _ = scan("fastapi_misc")
    listing = routes[("GET", "/v1/products")]

    assert param(listing, "limit") == {"name": "limit", "type": "int", "in": "query", "required": False, "default": 10,
                                      "validation": {"minimum": 1, "maximum": 50}}
    assert param(listing, "page")["via"] == "Pagination" and param(listing, "size")["default"] == 20     # Depends() on a class: __init__
    assert param(listing, "x-tenant-id", "header")["required"] is True and param(listing, "session", "cookie")["required"] is False

    price = routes[("PUT", "/v1/products/{pid}/price")]
    assert not any(p["name"] == "allowed" for p in price["params"])           # RoleChecker(["admin"]): __call__, not __init__
    assert price["security"]["annotations"][0]["scheme"] == "api-key-header"
    assert price["security"]["url_rule"]["roles"] == ["admin"]


def test_body_embed_scopes_response_classes_and_streaming():
    routes, _, _ = scan("fastapi_misc")

    price = routes[("PUT", "/v1/products/{pid}/price")]
    assert price["request_body"]["type"] == "object" and set(price["request_body"]["schema"]) == {"price", "reason"}
    assert price["request_body"]["schema"]["price"]["validation"] == {"required": True}

    export = routes[("GET", "/v1/export")]
    assert export["security"]["annotations"][0]["scopes"] == ["catalog:read", "catalog:export"]
    assert export["security"]["url_rule"]["roles"] == ["catalog:export", "catalog:read"]
    assert export["response"]["streaming"] is True

    page = routes[("GET", "/v1/products/{pid}/page")]
    assert page["response"]["view"] is True and page["produces"] == ["text/html"]
    old = routes[("GET", "/v1/old")]
    assert old["deprecated"] is True and codes(old) == {307}


def test_factories_mounts_websockets_and_root_path():
    routes, architecture, _ = scan("fastapi_misc")

    assert ("GET", "/v2/ping") in routes                            # app.mount("/v2", build_v2())
    assert routes[("GET", "/v2/ping")]["handler"] == "main.build_v2.ping_v2"
    assert ("WEBSOCKET", "/ws") in routes                           # decorated function inside create_app()
    assert routes[("GET", "/v1/products")]["context_path"] == "/catalog"
    assert routes[("GET", "/v1/products")]["tags"] == ["catalog"]   # include_router(..., tags=["catalog"]) in a for loop
    assert architecture["modules"][0]["runtime"]["application_name"] == "Catalog"      # the root app, not the mounted one


def test_documents_stores_and_messaging_outside_sqlalchemy():
    routes, architecture, _ = scan("fastapi_misc")

    listing = routes[("GET", "/v1/products")]["integrations"]
    assert [(f["technology"], f["operation"], f["table"]) for f in listing["databases"]] == [("mongodb", "READ", ["products"])]
    assert listing["caches"][0]["technology"] == "fastapi-cache" and listing["caches"][0]["ttl"] == 60

    page = routes[("GET", "/v1/products/{pid}/page")]["integrations"]["databases"][0]
    assert (page["technology"], page["call"], page["table"]) == ("tortoise", "Product.get", ["products"])      # Meta.table
    delete = routes[("DELETE", "/v1/products/{pid}")]["integrations"]["databases"][0]
    assert delete["operation"] == "DELETE"                                                                    # Product.filter(...).delete()

    price = routes[("PUT", "/v1/products/{pid}/price")]["integrations"]
    assert [(f["technology"], f["limit"]) for f in price["resilience"]] == [("rate-limit", "5/minute")]
    assert {(f["technology"], f["destination"]) for f in price["messaging"]} == {("rabbitmq", "catalog.events")}
    assert routes[("GET", "/v1/export")]["integrations"]["resilience"][0]["technology"] == "retry"

    jobs = architecture["integrations"]["scheduled_jobs"]
    assert jobs[0]["handler"] == "clients.nightly_cleanup" and jobs[0]["schedule"] == {"trigger": "cron", "hour": 2}
    listener = architecture["integrations"]["messaging"]["consumers"][0]
    assert (listener["technology"], listener["destinations"]) == ("kafka", ["price-updates"])
    assert architecture["data_model"]["entities"][0]["store"] == "tortoise"
    assert architecture["summary"]["pydantic"] == "v1"


def test_class_based_views_add_api_route_and_loops(tmp_path):
    routes = scan_source(tmp_path, {
        "app.py": """
            from fastapi import APIRouter, Depends, FastAPI, Header
            from fastapi_utils.cbv import cbv

            def auth(x_token: str = Header(...)):
                return x_token

            router = APIRouter(prefix="/items")

            @cbv(router)
            class ItemView:
                token: str = Depends(auth)

                @router.get("/{item_id}")
                def get(self, item_id: int, verbose: bool = False):
                    return {}

            other = APIRouter()

            def ping():
                return "pong"

            other.add_api_route("/ping", ping, methods=["GET", "HEAD"], status_code=202)

            app = FastAPI()
            for r in (router, other):
                app.include_router(r, prefix="/api")
        """,
    })

    assert set(routes) == {("GET", "/api/items/{item_id}"), ("GET", "/api/ping"), ("HEAD", "/api/ping")}
    view = routes[("GET", "/api/items/{item_id}")]
    assert {(p["name"], p["in"]) for p in view["params"]} == {("item_id", "path"), ("verbose", "query"), ("x-token", "header")}
    assert codes(routes[("GET", "/api/ping")]) == {202}
    # the same handler registered for two methods gets distinct stable doc names
    assert routes[("GET", "/api/ping")]["doc_name"] != routes[("HEAD", "/api/ping")]["doc_name"]


def test_async_sessions_classic_declarative_base_and_inherited_responses(tmp_path):
    routes = scan_source(tmp_path, {
        "src/__init__.py": "",
        "src/db.py": """
            from sqlalchemy import Column, ForeignKey, Integer
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
            from sqlalchemy.ext.declarative import declarative_base

            Base = declarative_base()
            SessionLocal = async_sessionmaker(create_async_engine("postgresql+asyncpg://u:p@db/app"), class_=AsyncSession)

            async def get_session():
                async with SessionLocal() as session:
                    yield session

            class User(Base):
                __tablename__ = "users"
                id = Column(Integer, primary_key=True)

            class Post(Base):
                __tablename__ = "posts"
                id = Column(Integer, primary_key=True)
                author_id = Column(Integer, ForeignKey("users.id"))
        """,
        "src/api/__init__.py": """
            from fastapi import APIRouter

            from .users import router as users_router

            api_router = APIRouter(prefix="/v1")
            api_router.include_router(users_router, responses={409: {"description": "conflict"}})
        """,
        "src/api/users.py": """
            from fastapi import APIRouter, Depends, HTTPException
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import AsyncSession

            from src.db import User, get_session

            router = APIRouter(prefix="/users", responses={404: {"description": "missing"}})

            @router.get("/{user_id}")
            async def get_user(user_id: int, db: AsyncSession = Depends(get_session)):
                result = await db.execute(select(User).where(User.id == user_id))
                return result.scalars().first()

            @router.get("/stats", responses={404: {"description": "none"}, "5XX": {"description": "down"}})
            async def stats(db: AsyncSession = Depends(get_session)):
                await db.execute("SELECT count(*) FROM posts p JOIN users u ON u.id = p.author_id")
                await db.execute("UPDATE users SET email = lower(email)")
        """,
        "src/main.py": """
            from fastapi import FastAPI

            from src.api import api_router

            app = FastAPI()
            app.include_router(api_router, prefix="/api")
            broken = (
        """,
    })

    user = routes[("GET", "/api/v1/users/{user_id}")]
    assert [(f["operation"], f["table"]) for f in user["integrations"]["databases"]] == [("READ", ["users"])]
    assert codes(user) == {200, 404, 409, 422}                  # responses= of the router and of include_router
    assert {r["code"]: r["description"] for r in user["responses"]} == {404: "missing", 409: "conflict"}

    stats = routes[("GET", "/api/v1/users/stats")]
    assert [(f["operation"], tuple(f["table"])) for f in stats["integrations"]["databases"]] == [
        ("READ", ("posts", "users")), ("WRITE", ("users",))]
    assert {r["code"]: r["description"] for r in stats["responses"]}[404] == "none"       # the route's own entry wins
    assert 500 in codes(stats)                                  # "5XX"


def test_router_included_twice_is_one_endpoint_with_both_paths(tmp_path):
    routes = scan_source(tmp_path, {
        "app.py": """
            from fastapi import APIRouter, FastAPI

            router = APIRouter()

            @router.get("/status")
            def status():
                return {}

            app = FastAPI()
            app.include_router(router, prefix="/v1")
            app.include_router(router, prefix="/v2")
        """,
    })

    route = routes[("GET", "/v1/status")]
    assert route["paths"] == ["/v1/status", "/v2/status"]


def test_services_in_a_monorepo_keep_their_own_modules(tmp_path):
    for service in ("orders", "billing"):
        files = {
            f"{service}/pyproject.toml": f'[project]\nname = "{service}"\ndependencies = ["fastapi"]\n',
            f"{service}/app/__init__.py": "",
            f"{service}/app/db.py": f"def load():\n    return '{service}'\n",
            f"{service}/app/main.py": f"""
                from fastapi import FastAPI
                from app.db import load

                app = FastAPI(title="{service}")

                @app.get("/{service}")
                def read():
                    return load()
            """,
        }
        for rel, source in files.items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(source))

    extractor = FastAPIExtractor()
    routes = {r["path"]: r for r in extractor.all_routes(str(tmp_path))}

    assert routes["/orders"]["module"] == "orders" and routes["/billing"]["module"] == "billing"
    assert routes["/orders"]["call_graph"]["direct"] == ["db.load"]
    # each service's `app.db` is its own file
    callee = extractor.analyzer.analysis(extractor.index.modules["orders.app.main"].functions["read"]).calls[0].callee
    assert callee.module.rel == "orders/app/db.py"
    callee = extractor.analyzer.analysis(extractor.index.modules["billing.app.main"].functions["read"]).calls[0].callee
    assert callee.module.rel == "billing/app/db.py"
    assert [m["path"] for m in extractor.architecture["modules"]] == ["billing", "orders"]


def test_http_calls_between_services_of_one_repository(tmp_path):
    for service, source in {
        "gateway": """
            import httpx
            from fastapi import FastAPI

            app = FastAPI()

            @app.get("/orders")
            async def orders():
                async with httpx.AsyncClient() as client:
                    return (await client.get("http://orders-service:8000/orders")).json()
        """,
        "orders-service": """
            from fastapi import FastAPI

            app = FastAPI()

            @app.get("/orders")
            def orders():
                return []
        """,
    }.items():
        (tmp_path / service / "app").mkdir(parents=True)
        (tmp_path / service / "pyproject.toml").write_text(f'[project]\nname = "{service}"\ndependencies = ["fastapi"]\n')
        (tmp_path / service / "app" / "main.py").write_text(textwrap.dedent(source))

    extractor = FastAPIExtractor()
    routes = {(r["module"], r["path"]): r for r in extractor.all_routes(str(tmp_path))}
    call = routes[("gateway", "/orders")]["integrations"]["external_apis"][0]

    assert call["target_module"] == "orders-service"
    edges = {(e["from"], e["to"], e["kind"]) for e in extractor.architecture["graph"]["edges"]}
    assert ("module:gateway", "module:orders-service", "http") in edges


def test_recursive_and_mutually_dependent_code_does_not_hang(tmp_path):
    routes = scan_source(tmp_path, {
        "app.py": """
            from fastapi import Depends, FastAPI

            app = FastAPI()

            def a(b=Depends(lambda: 1)):
                return b()

            def one(n: int):
                return two(n - 1)

            def two(n: int):
                return one(n - 1)

            @app.get("/loop")
            def loop(n: int, x=Depends(a)):
                return one(n)
        """,
    })

    assert sorted(routes[("GET", "/loop")]["call_graph"]["full"]) == ["app.a", "app.one", "app.two"]


def test_scan_without_fastapi_finds_nothing(tmp_path):
    (tmp_path / "script.py").write_text("print('hello')\n")

    assert Scanner().scan_full(str(tmp_path))["frameworks"] == []


# ============================================================================
# CONFIGURATION AND MANIFESTS
# ============================================================================

def test_dependency_manifests_and_toml_fallback():
    content = (FIXTURES / "fastapi_shop" / "pyproject.toml").read_text()

    full = parse_pyproject(content)
    names = {d["artifact"] for d in full["dependencies"]}
    assert {"fastapi", "sqlalchemy", "celery", "python-jose", "pytest"} <= names
    assert next(d for d in full["dependencies"] if d["artifact"] == "pytest")["scope"] == "test"
    assert next(d for d in full["dependencies"] if d["artifact"] == "sqlalchemy")["technology"] == "SQLAlchemy"

    # Python 3.10 has no tomllib: the small reader must find the same dependencies
    fallback = _mini_toml(content)
    assert fallback["project"]["name"] == "shop-service"
    assert load_toml(content)["project"]["dependencies"] == fallback["project"]["dependencies"]


def test_settings_defaults_and_env_files_become_configuration(tmp_path):
    routes = scan_source(tmp_path, {
        ".env": "PAYMENTS_URL=http://payments.internal:9000\n",
        "app.py": """
            import httpx
            from fastapi import FastAPI
            from pydantic_settings import BaseSettings

            class Settings(BaseSettings):
                PAYMENTS_URL: str = "http://localhost:1"
                API_PREFIX: str = "/api"

            settings = Settings()
            app = FastAPI()

            @app.get(settings.API_PREFIX + "/pay")
            def pay():
                return httpx.get(f"{settings.PAYMENTS_URL}/charge").json()
        """,
    })

    route = routes[("GET", "/api/pay")]
    call = route["integrations"]["external_apis"][0]
    assert call["http_method"] == "GET" and call["client"] == "httpx"
    assert call["url"] == "http://localhost:1/charge"            # the class default; the .env value is configuration, not code


# ============================================================================
# CHANGES: IMPACT, BREAKING CHANGES, VERDICTS
# ============================================================================

def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def shop_repo(tmp_path):
    repo = tmp_path / "shop"
    shutil.copytree(FIXTURES / "fastapi_shop", repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def edit(repo, rel, old, new, message):
    path = repo / rel
    assert old in path.read_text(), f"{old!r} not in {rel}"
    path.write_text(path.read_text().replace(old, new))
    git(repo, "commit", "-qam", message)


def impacted(repo):
    return {(r["method"], r["path"]): r for r in Scanner().scan(str(repo), "HEAD")["routes"]}


def test_a_changed_model_impacts_its_endpoints_and_reports_the_breaking_change(shop_repo):
    edit(shop_repo, "app/schemas/order.py", "quantity: int = Field(ge=1)", "quantity: int = Field(ge=5)", "stricter")

    routes = impacted(shop_repo)

    assert set(routes) == {("POST", "/api/v1/orders")}
    create = routes[("POST", "/api/v1/orders")]
    assert create["change_reasons"] == ["changed model: LineItem"]
    assert create["breaking_changes"] == [
        {"type": "VALIDATION_CHANGED", "field": "quantity", "rule": "minimum", "old": 1, "new": 5, "dto": "LineItem"}]


def test_a_changed_service_method_impacts_only_the_endpoints_that_reach_it(shop_repo):
    edit(shop_repo, "app/services/order_service.py", '"CREATE"', '"CREATED"', "service")

    routes = impacted(shop_repo)

    assert set(routes) == {("POST", "/api/v1/orders")}
    assert routes[("POST", "/api/v1/orders")]["change_reasons"] == ["changed: OrderService.create"]


def test_a_change_in_a_shared_base_class_reaches_the_endpoints_bound_to_it(shop_repo):
    edit(shop_repo, "app/crud/base.py", "limit: int = 100", "limit: int = 50", "paging")

    assert set(impacted(shop_repo)) == {("GET", "/api/v1/orders")}


def test_a_changed_dependency_impacts_everything_that_depends_on_it(shop_repo):
    edit(shop_repo, "app/core/security.py", "HS256", "HS512", "alg")

    assert set(impacted(shop_repo)) == {
        ("POST", "/api/v1/orders"), ("POST", "/api/v1/admin/invoices/{order_id}"), ("DELETE", "/api/v1/orders/{order_id}")}


def test_router_prefix_and_include_changes_impact_the_routes_they_move(shop_repo):
    edit(shop_repo, "app/api/routes/orders.py", 'prefix="/orders"', 'prefix="/purchases"', "prefix")
    routes = impacted(shop_repo)
    assert {p for _, p in routes} == {"/api/v1/purchases", "/api/v1/purchases/{order_id}"}
    assert all(r["change_reasons"] == ["changed router configuration: app.api.routes.orders"] for r in routes.values())

    edit(shop_repo, "app/main.py", "app.include_router(health.router)", "app.include_router(health.router, prefix='/x')", "include")
    assert set(impacted(shop_repo)) == {("GET", "/x/health"), ("WEBSOCKET", "/x/ws/orders")}


def test_a_changed_entity_impacts_the_endpoints_that_use_its_table(shop_repo):
    edit(shop_repo, "app/models/order.py", "String(500)", "String(900)", "wider note")

    routes = impacted(shop_repo)

    assert ("POST", "/api/v1/orders") in routes and ("GET", "/api/v1/orders") in routes
    assert routes[("GET", "/api/v1/orders")]["change_reasons"] == ["uses changed entity: Order"]
    assert ("GET", "/health") not in routes


def test_root_commit_reports_every_endpoint(shop_repo):
    result = Scanner().scan(str(shop_repo), "HEAD")      # first commit has no parent: every file counts as changed

    assert len(result["routes"]) == 8 and result["frameworks"] == ["fastapi"]


def test_verdict_between_two_versions_of_a_fastapi_app(shop_repo):
    base = Scanner().scan_full(str(shop_repo), "base")

    schemas = shop_repo / "app/schemas/order.py"
    schemas.write_text(schemas.read_text().replace("quantity: int = Field(ge=1)", "quantity: int = Field(ge=5)\n    warehouse: str"))
    routes = shop_repo / "app/api/routes/orders.py"
    routes.write_text(routes.read_text() + textwrap.dedent('''

        @router.get("/{order_id}/lines")
        def order_lines(order_id: int, db: SessionDep):
            return crud.order.lines_for(db, order_id)
    '''))
    head = Scanner().scan_full(str(shop_repo), "head")

    report = contracts.compare_reports(json.loads(json.dumps(base)), json.loads(json.dumps(head)))

    assert report["verdict"] == "hold"
    by_name = {e["name"]: e for e in report["endpoints"]}
    create = by_name["orders.create_order"]
    assert {(c["type"], c["severity"]) for c in create["changes"]} == {
        ("VALIDATION_CHANGED", "breaking"), ("REQUEST_FIELD_ADDED", "breaking")}
    assert by_name["orders.order_lines"]["status"] == "added" and by_name["orders.order_lines"]["verdict"] == "clear"
    assert "orders.read_order" not in by_name


def test_unchanged_code_is_clear(shop_repo):
    first = json.loads(json.dumps(Scanner().scan_full(str(shop_repo), "a")))
    second = json.loads(json.dumps(Scanner().scan_full(str(shop_repo), "b")))

    assert contracts.compare_reports(first, second)["verdict"] == "clear"


def test_modernising_type_syntax_is_not_a_contract_change(tmp_path):
    old = scan_source(tmp_path / "old", {"app.py": """
        from typing import List, Optional
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/x")
        def x(tags: Optional[List[int]] = None) -> List[str]:
            return []
    """})
    new = scan_source(tmp_path / "new", {"app.py": """
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/x")
        def x(tags: list[int] | None = None) -> list[str]:
            return []
    """})

    before = param(old[("GET", "/x")], "tags")["type"]
    after = param(new[("GET", "/x")], "tags")["type"]
    assert before == after == "list[int] | None"
    assert contracts.contract_changes(old[("GET", "/x")], new[("GET", "/x")]) == []
