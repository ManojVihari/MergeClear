"""
An ASP.NET Core solution scanned by the real scanner and uploaded to the server: documentation, versions, impact
analysis and the architecture pages, alone and together with Spring and FastAPI repositories.
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


def table_row(doc, field):
    """(type, required, {rules}) of a field's row; the stored JSON may reorder keys (Postgres jsonb), so rules are a set."""
    line = next(l for l in doc.splitlines() if l.startswith(f"| {field} |"))
    _, _, type_, required, rules, _ = [c.strip() for c in line.split("|")]
    return type_, required, set(rules.split(", "))


def scan(path, commit, repository):
    payload = json.loads(json.dumps(Scanner().scan_full(str(path), commit)))
    payload["repository"] = repository
    return payload


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "dotnet_shop"
    shutil.copytree(FIXTURES / "dotnet_shop", root)
    return root


@pytest.fixture
def two_versions(client, isolated_storage, shop):  # noqa: F811
    analyze(client, scan(shop, "abc1234", "dotnet-shop"))
    dtos = shop / "src/Shop.Api/Models/Dtos.cs"
    dtos.write_text(dtos.read_text().replace("[Range(1, 100)] int Quantity", "[Range(1, 100)] int Quantity, [Required] string Warehouse"))
    analyze(client, scan(shop, "def5678", "dotnet-shop"))
    return client


def test_dotnet_scan_is_documented_and_architecture_stored(client, isolated_storage, shop):  # noqa: F811
    analyze(client, scan(shop, "abc1234", "dotnet-shop"))

    apis = docs_store.list_apis("dotnet-shop")
    assert "OrdersController.Create" in apis and "Program.GET_health" in apis and len(apis) == 10
    doc = docs_store.read_version("dotnet-shop", "OrdersController.Create", 1)
    assert "- **Path:** `/api/Orders`" in doc
    assert "| 402 | PAYMENT_REQUIRED | PaymentDeclinedException (ExceptionMiddleware.InvokeAsync) |" in doc
    assert table_row(doc, "customerEmail") == ("string", "Yes", {"required", "email", "notEmpty", "max"})
    assert "**Messaging** (masstransit): publish to `OrderCreated`" in doc
    assert "**HTTP call** (httpclient): POST `https://api.payments.example.com/v1/charges/{orderId}`" in doc
    assert "**Cache** (distributed-cache): WRITE `order:{order.Id}`" in doc
    assert "`@Authorize` (class)" in doc and "**authenticated**" in doc

    stored = client.get("/api/architecture", params={"repo": "dotnet-shop"}).json()
    assert stored["architecture"]["aspnet"]["summary"]["endpoints"] == 10
    route = docs_store.get_route("dotnet-shop", "OrdersController.Create", 1)
    assert route["framework"] == "aspnet" and route["handler"] == "OrdersController.Create"


def test_changed_contract_makes_a_new_version_and_an_impact_analysis(two_versions):
    assert docs_store.list_versions("dotnet-shop", "OrdersController.Create") == [1, 2]
    assert docs_store.list_versions("dotnet-shop", "Program.GET_health") == [1]

    impact = two_versions.get("/api/impact-analysis", params={
        "repo": "dotnet-shop", "api": "OrdersController.Create", "v1": 1, "v2": 2}).json()

    assert impact["source"] == "scanner"
    changes = {(c["severity"], c["type"], c["detail"]) for c in impact["breaking_changes"]["changes"]}
    assert ("breaking", "REQUEST_FIELD_ADDED", "`lines.warehouse` (string) is required") in changes
    assert impact["breaking_changes"]["impact_level"] == "high"
    shared = {(s["kind"], s["name"]) for s in impact["shared_resources"]}
    assert ("table", "orders") in shared and ("topic", "OrderPaid") not in shared


def test_architecture_pages_render(two_versions):
    page = two_versions.get("/ui/dotnet-shop/architecture")

    assert page.status_code == 200
    for expected in ("dotnet-shop architecture", "OrderCreated", "api.payments.example.com", "OrdersController", "audit_log", ".NET net8.0", "ASP.NET Core"):
        assert expected in page.text, expected

    layered = two_versions.get("/api/architecture/layered", params={"repo": "dotnet-shop"}).json()
    columns = {c["title"]: {n["label"] for n in c["nodes"]} for c in layered["columns"]}
    assert "Shop.Api" in columns["Services"]
    assert any("postgresql" in label for label in columns["Data & infrastructure"])

    system = two_versions.get("/api/dependency-graph", params={"repo": "dotnet-shop"}).json()
    links = {(l["source"], l["target"], l["kind"]) for l in system["links"]}
    assert ("module:src/Shop.Api", "external:api.payments.example.com", "http") in links
    assert two_versions.get("/ui/dotnet-shop/OrdersController.Create/dependencies", params={"v1": 1, "v2": 2}).status_code == 200


def test_three_frameworks_in_one_repository_show_one_application(client, isolated_storage, shop):  # noqa: F811
    spring = json.loads(SPRING_SCAN.read_text())
    python = scan(FIXTURES / "fastapi_shop", "abc1234", "poly")
    dotnet = scan(shop, "abc1234", "poly")
    architecture = {**spring["architecture"], **python["architecture"], **dotnet["architecture"]}
    combined = dict(dotnet, frameworks=["spring", "fastapi", "aspnet"], routes=spring["routes"] + python["routes"] + dotnet["routes"], architecture=architecture)

    analyze(client, combined)

    model = architecture_view.application_model({"architecture": architecture})
    assert model["summary"]["endpoints"] == 4 + 8 + 10
    assert model["summary"]["frameworks"] == ["aspnet", "fastapi", "spring"]
    assert {"Shop.Api", "Shop Service"} <= set(model["summary"]["spring_boot_apps"])
    orders = next(t for t in model["data_model"]["tables"] if t["name"] == "orders")
    assert {"OrderServiceImpl.create", "CRUDBase.create", "EfRepository.AddAsync"} <= set(orders["accessed_by"])
    assert client.get("/ui/poly/architecture").status_code == 200
