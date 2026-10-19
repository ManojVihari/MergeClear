"""
A FastAPI repository scanned by the real scanner and uploaded to the server: documentation, versions, impact
analysis and the architecture pages, alone and together with a Spring repository.
"""
import json
import shutil
from pathlib import Path

import pytest
from test_server import analyze, client, isolated_storage  # noqa: F401  (fixtures and helpers of the server tests)

from app.services import architecture_view, docs_store
from mergeclear.core.scanner import Scanner

FIXTURES = Path(__file__).resolve().parents[2] / "scanner" / "tests" / "fixtures"
SPRING_SCAN = Path(__file__).parent / "fixtures" / "spring_shop_scan.json"


def scan(path, commit, repository):
    payload = json.loads(json.dumps(Scanner().scan_full(str(path), commit)))
    payload["repository"] = repository
    return payload


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "fastapi_shop"
    shutil.copytree(FIXTURES / "fastapi_shop", root)
    return root


@pytest.fixture
def two_versions(client, isolated_storage, shop):  # noqa: F811
    analyze(client, scan(shop, "abc1234", "fastapi-shop"))

    schemas = shop / "app/schemas/order.py"
    schemas.write_text(schemas.read_text().replace("quantity: int = Field(ge=1)", "quantity: int = Field(ge=5)\n    warehouse: str"))
    analyze(client, scan(shop, "def5678", "fastapi-shop"))
    return client


def test_fastapi_scan_is_documented_and_architecture_stored(client, isolated_storage, shop):  # noqa: F811
    analyze(client, scan(shop, "abc1234", "fastapi-shop"))

    assert sorted(docs_store.list_apis("fastapi-shop")) == [
        "admin.export_invoice", "files.upload", "health.health", "health.orders_ws",
        "orders.cancel_order", "orders.create_order", "orders.list_orders", "orders.read_order",
    ]
    doc = docs_store.read_version("fastapi-shop", "orders.create_order", 1)
    assert "- **Path:** `/api/v1/orders`" in doc
    assert "| 402 | PAYMENT_REQUIRED | PaymentDeclined (main.payment_declined_handler) |" in doc
    assert "| lines | list[LineItem] | Yes | min, max, required |" in doc
    assert "**Messaging** (kafka): publish to `orders.created.v1`" in doc
    assert "**HTTP call** (httpx): POST `https://api.payments.example.com/v1/charges/{order_id}`" in doc
    assert "`get_current_user`" in doc and "**authenticated**" in doc

    stored = client.get("/api/architecture", params={"repo": "fastapi-shop"}).json()
    assert stored["commit"] == "abc1234"
    assert stored["architecture"]["fastapi"]["summary"]["endpoints"] == 8
    assert "spring" not in stored["architecture"]

    route = docs_store.get_route("fastapi-shop", "orders.create_order", 1)
    assert route["handler"] == "orders.create_order" and route["framework"] == "fastapi"


def test_unchanged_scan_creates_no_new_versions(client, isolated_storage, shop):  # noqa: F811
    analyze(client, scan(shop, "abc1234", "fastapi-shop"))
    analyze(client, scan(shop, "def5678", "fastapi-shop"))

    assert docs_store.list_versions("fastapi-shop", "orders.create_order") == [1]


def test_renaming_a_dependency_or_rewording_a_parameter_is_not_a_new_version():
    from app.models.schema import Route
    from app.services.signature_service import SignatureService

    route = scan(FIXTURES / "fastapi_misc", "abc", "misc")["routes"]
    listing = next(r for r in route if r["path"] == "/v1/products")
    assert any(p.get("via") for p in listing["params"])

    reworded = dict(listing, params=[dict(p, via="renamed", description="new text") for p in listing["params"]])
    signature = SignatureService()
    assert signature.generate(Route(**listing)) == signature.generate(Route(**reworded))

    stricter = dict(listing, params=[dict(p, required=True) for p in listing["params"]])
    assert signature.generate(Route(**listing)) != signature.generate(Route(**stricter))


def test_changed_contract_makes_a_new_version_and_an_impact_analysis(two_versions):
    assert docs_store.list_versions("fastapi-shop", "orders.create_order") == [1, 2]
    assert docs_store.list_versions("fastapi-shop", "health.health") == [1]

    impact = two_versions.get("/api/impact-analysis", params={
        "repo": "fastapi-shop", "api": "orders.create_order", "v1": 1, "v2": 2}).json()

    assert impact["source"] == "scanner"
    changes = {(c["severity"], c["type"], c["detail"]) for c in impact["breaking_changes"]["changes"]}
    assert ("breaking", "VALIDATION_CHANGED", "`lines.quantity` minimum: 1 → 5") in changes
    assert ("breaking", "REQUEST_FIELD_ADDED", "`lines.warehouse` (str) is required") in changes
    assert impact["breaking_changes"]["impact_level"] == "high"
    shared = {(s["kind"], s["name"]) for s in impact["shared_resources"]}
    assert ("table", "orders") in shared and ("topic", "shop.send_receipt") in shared      # the Celery task has a consumer


def test_architecture_pages_render(two_versions):
    page = two_versions.get("/ui/fastapi-shop/architecture")

    assert page.status_code == 200
    assert "fastapi-shop architecture" in page.text
    for expected in ("orders.created.v1", "api.payments.example.com", "app/api/routes/orders.py", "audit_log", "Python"):
        assert expected in page.text, expected

    layered = two_versions.get("/api/architecture/layered", params={"repo": "fastapi-shop"}).json()
    columns = {c["title"]: {n["label"] for n in c["nodes"]} for c in layered["columns"]}
    assert "Shop Service" in columns["Services"]
    assert any("postgresql" in label for label in columns["Data & infrastructure"])
    components = two_versions.get("/api/architecture/layered", params={"repo": "fastapi-shop", "module": ""}).json()
    labels = {n["label"] for c in components["columns"] for n in c["nodes"]}
    assert {"router", "OrderService"} <= labels

    system = two_versions.get("/api/dependency-graph", params={"repo": "fastapi-shop"}).json()
    links = {(l["source"], l["target"], l["kind"]) for l in system["links"]}
    assert ("module:", "broker:kafka", "connects") in links or ("module:", "broker:kafka", "publishes") in links
    assert ("module:", "external:api.payments.example.com", "http") in links

    deps = two_versions.get("/ui/fastapi-shop/orders.create_order/dependencies", params={"v1": 1, "v2": 2})
    assert deps.status_code == 200 and "Hold before merging" in deps.text

    assert two_versions.get("/ui/fastapi-shop/orders.create_order").status_code == 200
    assert two_versions.get("/ui/fastapi-shop").status_code == 200


def test_a_repository_with_both_frameworks_shows_one_application(client, isolated_storage, shop):  # noqa: F811
    spring = json.loads(SPRING_SCAN.read_text())
    python = scan(shop, "abc1234", "mixed")
    spring["repository"] = "mixed"
    mixed = dict(python, frameworks=["spring", "fastapi"], routes=spring["routes"] + python["routes"],
                 architecture={**spring["architecture"], **python["architecture"]})

    analyze(client, mixed)

    document = {"architecture": mixed["architecture"]}
    model = architecture_view.application_model(document)
    assert model["summary"]["endpoints"] == 4 + 8
    assert model["summary"]["frameworks"] == ["fastapi", "spring"]
    tables = {t["name"] for t in model["data_model"]["tables"]}
    assert {"orders", "audit_log", "order_line"} <= tables
    orders = next(t for t in model["data_model"]["tables"] if t["name"] == "orders")
    assert "OrderServiceImpl.create" in orders["accessed_by"] and "CRUDBase.create" in orders["accessed_by"]
    topics = {(d["technology"], d["destination"]) for d in model["integrations"]["messaging"]["destinations"]}
    assert ("kafka", "orders.created.v1") in topics

    page = client.get("/ui/mixed/architecture")
    assert page.status_code == 200
    assert "OrderController" in page.text and "app/api/routes/orders.py" in page.text
    assert client.get("/api/architecture/layered", params={"repo": "mixed"}).status_code == 200
    assert architecture_view.application_model({"architecture": {"other": {"x": 1}}}) is None
