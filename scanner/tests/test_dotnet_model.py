"""
ASP.NET Core support: controllers and Minimal APIs, parameters and schemas, status codes, security, DI-aware call chains,
EF Core / Dapper / Redis / MassTransit / MediatR / HttpClient integrations and the application model, checked against two
fixture solutions plus small inline sources for individual features.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from mergeclear import contracts
from mergeclear.core.scanner import Scanner
from mergeclear.plugins.dotnet_aspnet.extractor import AspNetExtractor
from mergeclear.plugins.dotnet_aspnet.project import parse_connection_string

FIXTURES = Path(__file__).parent / "fixtures"
_cache = {}


def scan(name):
    """(routes by (method, path), architecture, extractor) of a fixture solution."""
    if name not in _cache:
        extractor = AspNetExtractor()
        routes = extractor.all_routes(str(FIXTURES / name))
        _cache[name] = ({(r["method"], r["path"]): r for r in routes}, extractor.architecture, extractor)
    return _cache[name]


def scan_source(tmp_path, files):
    for rel, source in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))
    extractor = AspNetExtractor()
    routes = extractor.all_routes(str(tmp_path))
    return {(r["method"], r["path"]): r for r in routes}, extractor


def codes(route):
    return {s["code"] for s in route["status_codes"]}


def param(route, name, location=None):
    matches = [p for p in route["params"] if p["name"] == name and (location is None or p["in"] == location)]
    assert matches, f"no parameter {name} in {[p['name'] for p in route['params']]}"
    return matches[0]


# ============================================================================
# SHOP SOLUTION: controllers, EF Core, MediatR, MassTransit
# ============================================================================

def test_controller_and_minimal_api_routes_get_their_full_paths():
    routes, _, _ = scan("dotnet_shop")

    assert set(routes) == {
        ("POST", "/api/Orders"), ("GET", "/api/Orders/{id}"), ("GET", "/api/Orders"), ("DELETE", "/api/Orders/{id}"),
        ("POST", "/api/Orders/{id}/pay"), ("POST", "/api/Orders/import"),
        ("GET", "/api/v2/orders/{id}"), ("POST", "/api/v2/orders"), ("DELETE", "/api/v2/orders/{id}"), ("GET", "/health"),
    }
    create = routes[("POST", "/api/Orders")]
    assert create["handler"] == "OrdersController.Create"
    assert create["controller"] == "Shop.Api.Controllers.OrdersController"
    assert create["description"] == "Places a new order."
    assert create["style"] == "controller" and routes[("GET", "/api/v2/orders/{id}")]["style"] == "minimal-api"
    assert routes[("GET", "/api/v2/orders/{id}")]["tags"] == ["OrdersV2"]            # MapGroup("/orders").WithTags(...)
    assert routes[("GET", "/api/v2/orders/{id}")]["handler"] == "OrderEndpoints.GET_api_v2_orders_id"
    assert routes[("GET", "/api/v2/orders/{id}")]["operation_id"] == "GetOrderV2"


def test_request_body_schema_merges_data_annotations_and_fluent_validation():
    body = scan("dotnet_shop")[0][("POST", "/api/Orders")]["request_body"]
    fields = body["schema"]

    assert body["type"] == "CreateOrderRequest" and body["required"] and body["validated"]
    assert fields["customerEmail"]["validation"] == {"required": True, "email": True, "notEmpty": True, "max": 120}      # [EmailAddress] + RuleFor(...).NotEmpty().MaximumLength(120)
    assert fields["lines"]["validation"] == {"required": True, "min": 1, "max": 20}
    item = fields["lines"]["schema"]["items"]["fields"]
    assert item["sku"]["validation"] == {"required": True, "max": 40, "min": 2}                # [StringLength(40, MinimumLength = 2)]
    assert item["quantity"]["validation"] == {"minimum": 1, "maximum": 100}                    # [Range(1, 100)]
    assert fields["paymentMethod"] == {"type": "PaymentMethod", "enum": ["Card", "Paypal"]}
    assert fields["note"]["type"] == "string?" and fields["note"]["validation"] == {"max": 500}


def test_parameters_bind_from_the_right_place():
    routes, _, _ = scan("dotnet_shop")

    create = routes[("POST", "/api/Orders")]
    assert param(create, "X-Request-Id", "header")["required"] is True                          # [FromHeader(Name = "X-Request-Id")]
    assert [p["name"] for p in create["params"]] == ["request", "X-Request-Id"]                  # services and CancellationToken are not parameters

    listing = routes[("GET", "/api/Orders")]
    assert [(p["name"], p["in"]) for p in listing["params"]] == [("Page", "query"), ("PageSize", "query"), ("status", "query")]
    assert param(listing, "Page")["validation"] == {"minimum": 1, "maximum": 1000}               # [FromQuery] PageQuery expands to its properties
    assert param(listing, "status")["required"] is False                                         # [FromQuery(Name = "status")] string?

    assert param(routes[("GET", "/api/Orders/{id}")], "id") == {"name": "id", "type": "int", "in": "path", "required": True, "default": None}
    upload = routes[("POST", "/api/Orders/import")]
    assert param(upload, "file")["in"] == "multipart" and param(upload, "source")["in"] == "form"
    assert upload["consumes"] == ["multipart/form-data"]


def test_response_types_unwrap_task_actionresult_and_typed_results():
    routes, _, _ = scan("dotnet_shop")

    get = routes[("GET", "/api/Orders/{id}")]["response"]
    assert get["body_type"] == "OrderDto"
    assert set(get["schema"]) == {"id", "status", "customerEmail", "lines"}                       # InternalNotes is [JsonIgnore]; names are camelCase
    assert get["schema"]["status"]["enum"] == ["New", "Paid", "Cancelled"]

    page = routes[("GET", "/api/Orders")]["response"]
    assert page["body_type"] == "PagedResult<OrderDto>"
    assert page["schema"]["items"]["type"] == "List<OrderDto>"                                    # PagedResult<T> bound to OrderDto

    created = routes[("POST", "/api/Orders")]["response"]["body_type"]                           # IActionResult: from [ProducesResponseType] / CreatedAtAction
    assert created == "OrderDto"
    typed = routes[("POST", "/api/v2/orders")]
    assert typed["response"]["body_type"] == "OrderDto"                                          # Task<Results<Created<OrderDto>, BadRequest>>
    assert {201, 400} <= codes(typed)


def test_status_codes_come_from_helpers_attributes_and_exception_middleware():
    routes, _, _ = scan("dotnet_shop")
    create = routes[("POST", "/api/Orders")]

    sources = {(s["code"], s["source"]) for s in create["status_codes"]}
    assert (201, "ControllerBase.CreatedAtAction") in sources and (201, "ProducesResponseType") in sources
    assert (400, "ApiController model validation") in sources
    assert (402, "PaymentDeclinedException (ExceptionMiddleware.InvokeAsync)") in sources         # catch (PaymentDeclinedException) { StatusCode = 402 }
    assert (409, "InvalidOperationException (ExceptionMiddleware.InvokeAsync)") in sources         # StatusCodes.Status409Conflict
    assert not any(c == 200 for c in codes(create))
    assert {"PAYMENT_REQUIRED", "CONFLICT", "UNAUTHORIZED", "BAD_REQUEST"} <= {e for e in create["errors"] if isinstance(e, str)}
    validation = [e for e in create["errors"] if isinstance(e, dict)][0]
    assert validation["type"] == "VALIDATION_ERROR" and {"name": "customerEmail", "rule": "email"} in validation["fields"]

    get = routes[("GET", "/api/Orders/{id}")]
    assert {(404, "ControllerBase.NotFound"), (404, "NotFoundException (ExceptionMiddleware.InvokeAsync)")} <= {
        (s["code"], s["source"]) for s in get["status_codes"]}
    assert {e["exception"]: e["status"] for e in get["exceptions"]} == {"NotFoundException": 404}


def test_authorization_attributes_policies_and_group_conventions():
    routes, _, _ = scan("dotnet_shop")

    controller = routes[("GET", "/api/Orders/{id}")]["security"]
    assert controller["annotations"] == [{"annotation": "Authorize", "expression": None, "level": "class"}]
    assert controller["url_rule"]["access"] == "authenticated"

    anonymous = routes[("GET", "/api/Orders")]["security"]                                        # [AllowAnonymous] beats the class-level [Authorize]
    assert anonymous["url_rule"]["access"] == "permitAll"
    assert 401 not in codes(routes[("GET", "/api/Orders")])

    cancel = routes[("DELETE", "/api/Orders/{id}")]["security"]
    assert cancel["url_rule"]["access"] == "hasRole" and cancel["url_rule"]["roles"] == ["Admin", "Support"]
    assert {401, 403} <= codes(routes[("DELETE", "/api/Orders/{id}")])

    group = routes[("GET", "/api/v2/orders/{id}")]["security"]                                    # .MapGroup(...).RequireAuthorization()
    assert group["annotations"][0] == {"annotation": "RequireAuthorization", "expression": None, "level": "group"}
    policy = routes[("DELETE", "/api/v2/orders/{id}")]["security"]["url_rule"]                  # .RequireAuthorization("AdminOnly") -> RequireRole("Admin")
    assert policy["policy"] == ["AdminOnly"] and policy["roles"] == ["Admin"]

    assert routes[("GET", "/health")]["security"] is None


def test_calls_resolve_through_di_registrations_and_generic_repositories():
    create = scan("dotnet_shop")[0][("POST", "/api/Orders")]
    graph = create["call_graph"]

    assert graph["direct"] == ["OrderService.CreateAsync"]                                        # IOrderService -> OrderService (AddScoped)
    assert {"EfRepository.AddAsync", "PaymentClient.ChargeAsync"} <= set(graph["full"])           # IRepository<>/IPaymentClient -> implementations
    assert graph["tree"]["OrdersController.Create"]["OrderService.CreateAsync"].keys() >= {"EfRepository.AddAsync", "PaymentClient.ChargeAsync"}


def test_integrations_efcore_tables_http_cache_and_messaging():
    create = scan("dotnet_shop")[0][("POST", "/api/Orders")]
    integrations = create["integrations"]

    databases = {(f["operation"], tuple(f["table"]), f["entity"]) for f in integrations["databases"]}
    assert ("WRITE", ("orders",), "Order") in databases                       # IRepository<Order> -> EfRepository<T>: Set<T>() with T = Order
    assert ("WRITE", ("audit_log",), "AuditLog") in databases                 # IRepository<AuditLog>: the same class, another binding
    assert integrations["transactions"][0]["operation"] == "COMMIT"

    assert [(f["technology"], f["operation"], f["key"]) for f in integrations["caches"]] == [("distributed-cache", "WRITE", "order:{order.Id}")]
    assert [(f["technology"], f["destination"]) for f in integrations["messaging"]] == [("masstransit", "OrderCreated")]

    external = {(f["http_method"], f["url"], f["host"]) for f in integrations["external_apis"]}
    assert ("POST", "https://api.payments.example.com/v1/charges/{orderId}", "api.payments.example.com") in external        # AddHttpClient<..>(BaseAddress = config)
    assert ("GET", "http://inventory-service/stock/{line.Sku}", "inventory-service") in external                            # IOptions<InventoryOptions> -> appsettings
    assert any(f.get("target_service") == "inventory-service" for f in integrations["external_apis"])


def test_mediatr_requests_are_followed_into_their_handlers():
    pay = scan("dotnet_shop")[0][("POST", "/api/Orders/{id}/pay")]

    assert "PayOrderHandler.Handle" in pay["call_graph"]["full"]
    assert [(f["technology"], f["destination"]) for f in pay["integrations"]["messaging"]] == [("masstransit", "OrderPaid")]
    assert {409, 200} <= codes(pay) and pay["response"]["body_type"] == "bool"


def test_query_chains_and_raw_sql_find_entities_and_tables():
    routes, _, _ = scan("dotnet_shop")
    listing = routes[("GET", "/api/Orders")]["integrations"]["databases"]
    cancel = routes[("DELETE", "/api/Orders/{id}")]["integrations"]["databases"]

    assert [(f["operation"], f["table"]) for f in listing] == [("READ", ["orders"])]
    assert {f["operation"] for f in cancel} == {"READ", "DELETE"}


def test_architecture_model_of_the_shop():
    _, architecture, _ = scan("dotnet_shop")
    summary = architecture["summary"]

    assert summary["endpoints"] == 10 and summary["entities"] == 3 and summary["frameworks"] == ["aspnet"]
    assert summary["styles"] == ["controller", "minimal-api"] and summary["spring_boot_apps"] == ["Shop.Api"]
    api = next(m for m in architecture["modules"] if m["path"] == "src/Shop.Api")
    assert api["is_application"] and api["dotnet_version"] == "net8.0" and api["nullable"] is True and api["version"] == "2.3.0"
    assert api["runtime"]["server_port"] == 5080
    assert api["runtime"]["datasources"][0] == {"type": "postgresql", "host": "db.internal", "port": 5432, "database": "shop", "name": "Shop", "profile": "default"}
    assert api["runtime"]["redis"] == {"host": "cache.internal"} and api["runtime"]["rabbitmq"] == {"host": "rabbit.internal"}
    assert api["properties"]["ConnectionStrings:Shop"].endswith("Password=***")
    assert {"messaging: RabbitMQ", "cqrs: MediatR", "validation: FluentValidation"} <= set(api["technologies"])
    assert "EF Core" in architecture["summary"]["technologies"]["database"]
    library = next(m for m in architecture["modules"] if m["path"] == "src/Shop.Data")
    assert "datasources" not in library["runtime"]                      # a class library runs with its host's configuration

    entities = {e["name"]: e for e in architecture["data_model"]["entities"]}
    assert entities["Order"]["table"] == "orders" and entities["Order"]["relationships"] == [{"field": "Lines", "target": "OrderLine", "type": "OneToMany"}]
    assert entities["OrderLine"]["table"] == "order_lines"                                       # [Table("order_lines")]
    assert entities["OrderLine"]["fields"][0] == {"name": "LineId", "type": "int", "id": True, "generated": "IDENTITY"}
    assert entities["AuditLog"]["table"] == "audit_log"                                          # Entity<AuditLog>(e => e.ToTable(...))
    tables = {t["name"]: t for t in architecture["data_model"]["tables"]}
    assert tables["orders"]["operations"] == ["DELETE", "READ", "WRITE"] and "EfRepository.AddAsync" in tables["orders"]["accessed_by"]

    components = {c["qualified_name"]: c for c in architecture["components"]}
    assert components["Shop.Api.Controllers.OrdersController"]["stereotype"] == "rest_controller"
    assert components["Shop.Api.Services.OrderService"]["dependencies"][0]["implementations"] == ["Shop.Data.Repositories.EfRepository"]
    assert components["Shop.Data.ShopDbContext"]["stereotype"] == "repository"
    assert components["Shop.Api.Messaging.OrderPaidConsumer"]["stereotype"] == "listener"
    assert components["Shop.Api.Options.InventoryOptions"]["stereotype"] == "configuration_properties"

    messaging = architecture["integrations"]["messaging"]
    destinations = {(d["technology"], d["destination"]): d for d in messaging["destinations"]}
    assert destinations[("masstransit", "OrderPaid")]["consumers"] == ["OrderPaidConsumer.Consume"]
    assert destinations[("masstransit", "OrderPaid")]["producers"] == ["PayOrderHandler.Handle"]
    assert {h["exception"] for h in architecture["exception_handling"]} == {"NotFoundException", "PaymentDeclinedException", "InvalidOperationException"}
    assert architecture["security"]["mechanisms"] == ["jwt-bearer"] and "AdminOnly" in architecture["security"]["policies"]


def test_architecture_graph_uses_the_host_applications_database():
    graph = scan("dotnet_shop")[1]["graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}

    assert nodes["datastore:postgresql:db.internal/shop"]["type"] == "datastore"
    assert ("component:Shop.Data.Repositories.EfRepository", "datastore:postgresql:db.internal/shop", "write") in edges
    assert ("module:src/Shop.Api", "datastore:postgresql:db.internal/shop", "connects") in edges
    assert not any(n.startswith("datastore:postgresql:embedded") for n in nodes)
    assert ("component:Shop.Api.Services.OrderService", "broker:masstransit", "publishes") in edges
    assert ("broker:masstransit", "component:Shop.Api.Messaging.OrderPaidConsumer", "delivers") in edges
    assert ("component:Shop.Api.Services.OrderService", "external:inventory-service", "http") in edges
    assert ("component:Shop.Api.Controllers.OrdersController", "component:Shop.Api.Services.OrderService", "calls") in edges


# ============================================================================
# MINIMAL API SOLUTION
# ============================================================================

def test_minimal_apis_with_extension_groups_as_parameters_and_hubs():
    routes, architecture, _ = scan("dotnet_minimal")

    assert set(routes) == {("GET", "/todos"), ("GET", "/todos/{id}"), ("POST", "/todos"), ("GET", "/todos/report"), ("DELETE", "/todos/{id}"),
                           ("WEBSOCKET", "/chat"), ("GET", "/ping")}
    listing = routes[("GET", "/todos")]
    assert [(p["name"], p["in"]) for p in listing["params"]] == [("Done", "query"), ("q", "query"), ("X-Tenant", "header")]      # [AsParameters]
    assert listing["response"]["body_type"] == "List<Todo>"
    assert [(f["technology"], f["operation"], f["key"]) for f in listing["integrations"]["caches"]] == [("redis", "READ", "todos:all"), ("redis", "WRITE", "todos:all")]
    assert listing["integrations"]["databases"][0]["table"] == ["Todos"]                                  # DbSet property name is the default table

    one = routes[("GET", "/todos/{id}")]
    assert one["response"]["body_type"] == "Todo" and codes(one) == {200, 404}                            # async Task<Results<Ok<Todo>, NotFound>> (...) =>
    assert param(one, "id")["required"] is True

    create = routes[("POST", "/todos")]
    assert param(create, "input")["in"] == "body" and create["request_body"]["schema"]["title"]["validation"] == {"required": True, "max": 100}   # [property: ...]
    assert codes(create) == {201}

    assert routes[("WEBSOCKET", "/chat")]["protocol"] == "websocket"
    assert architecture["summary"]["message_listeners"] == 0


def test_dapper_raw_sql_kafka_hangfire_and_background_services():
    routes, architecture, _ = scan("dotnet_minimal")

    report = routes[("GET", "/todos/report")]["integrations"]["databases"][0]
    assert (report["operation"], report["table"]) == ("READ", ["todos", "reports"])
    delete = routes[("DELETE", "/todos/{id}")]
    assert delete["integrations"]["databases"][0]["operation"] == "DELETE" and delete["integrations"]["databases"][0]["table"] == ["todos"]
    assert delete["security"]["url_rule"]["access"] == "authenticated" and 401 in codes(delete)

    producer = architecture["integrations"]["messaging"]["producers"][0]
    assert (producer["technology"], producer["destination"], producer["source"]) == ("kafka", "todo-events", "Notifier.NotifyAsync")
    jobs = {j["handler"]: j for j in architecture["integrations"]["scheduled_jobs"]}
    assert jobs["Cleanup.Run"]["schedule"] == {"id": "cleanup", "cron": "0 3 * * *"}
    assert jobs["WeatherSync.ExecuteAsync"]["technology"] == "hosted-service"


def test_central_package_versions_connection_strings_and_commented_appsettings():
    _, architecture, _ = scan("dotnet_minimal")
    (module,) = architecture["modules"]

    versions = {d["artifact"]: d["version"] for d in module["dependencies"]}
    assert versions["Dapper"] == "2.1.35" and versions["StackExchange.Redis"] == "2.8.0"          # Directory.Packages.props
    runtime = module["runtime"]
    assert {"type": "sqlite", "database": "app.db", "name": "Default", "profile": "default"} in runtime["datasources"]
    assert {"type": "sqlserver", "host": "sql.internal", "port": 1433, "database": "reports", "name": "Reporting", "profile": "default"} in runtime["datasources"]
    assert runtime["kafka"]["bootstrap_servers"] == "kafka.internal:9092" and runtime["redis"]["host"] == "redis.internal"


def test_connection_string_parsing():
    assert parse_connection_string("Host=db;Port=5432;Database=shop;Username=u")["scheme_hint"] == "postgresql"
    assert parse_connection_string("Server=tcp:sql.example.com,1433;Initial Catalog=x;")["port"] == 1433
    assert parse_connection_string("mongodb://m1:27017/catalog")["database"] == "catalog"
    assert parse_connection_string("cache:6379,abortConnect=false") == {}


# ============================================================================
# INLINE SOURCES
# ============================================================================

def test_route_templates_tokens_versions_and_conventional_routing(tmp_path):
    routes, _ = scan_source(tmp_path, {
        "Controllers.cs": """
            namespace Demo;

            [ApiController]
            [ApiVersion("2.0")]
            [Route("api/v{version:apiVersion}/[controller]")]
            public class WidgetsController : ControllerBase
            {
                [HttpGet("{id:guid}/parts/{*rest}")]
                public IActionResult Parts(Guid id, string rest) => Ok();

                [HttpPost("/absolute/[action]")]
                public IActionResult Build() => Accepted();

                [HttpGet, HttpHead]
                public IActionResult Index() => Ok();
            }

            public class HomeController : Controller
            {
                public IActionResult Index() => View();
                public IActionResult Details(int id) => View();
                [HttpPost]
                public IActionResult Save(string name) => Ok();
            }
        """,
        "Program.cs": """
            var app = WebApplication.CreateBuilder(args).Build();
            app.MapControllerRoute(name: "default", pattern: "{controller=Home}/{action=Index}/{id?}");
            app.Run();
        """,
    })

    assert ("GET", "/api/v2/Widgets/{id}/parts/{rest}") in routes
    assert param(routes[("GET", "/api/v2/Widgets/{id}/parts/{rest}")], "id")["type"] == "Guid"
    assert ("POST", "/absolute/Build") in routes
    assert {("GET", "/api/v2/Widgets"), ("HEAD", "/api/v2/Widgets")} <= set(routes)
    assert ("GET", "/") in routes and ("GET", "/Home/Details/{id}") in routes                    # conventional routing for MVC controllers
    assert ("POST", "/Home/Save") in routes
    assert param(routes[("GET", "/Home/Details/{id}")], "id")["in"] == "path"


def test_minimal_api_groups_nested_variables_and_startup_style(tmp_path):
    routes, _ = scan_source(tmp_path, {
        "Program.cs": """
            var app = WebApplication.CreateBuilder(args).Build();
            var api = app.MapGroup("/api");
            var v1 = api.MapGroup("/v1").WithTags("V1").RequireAuthorization();
            var users = v1.MapGroup("/users");
            users.MapGet("/", () => Results.Ok());
            users.MapPost("/{id}", (int id) => Results.NoContent()).AllowAnonymous();
            api.MapMethods("/multi", new[] { "GET", "POST" }, () => "x");
            app.Run();
        """,
        "Startup.cs": """
            public class Startup
            {
                public void Configure(IApplicationBuilder app)
                {
                    app.UseRouting();
                    app.UseEndpoints(endpoints =>
                    {
                        endpoints.MapControllers();
                        endpoints.MapGet("/legacy", async context => await context.Response.WriteAsync("hi"));
                    });
                }
            }
        """,
        "Modules.cs": """
            public static class Orphan
            {
                public static void Register(IEndpointRouteBuilder routes)
                {
                    routes.MapGet("/found-by-reflection", () => "ok");
                }
            }
        """,
    })

    assert {("GET", "/api/v1/users"), ("POST", "/api/v1/users/{id}"), ("GET", "/api/multi"), ("POST", "/api/multi"), ("GET", "/legacy"),
            ("GET", "/found-by-reflection")} <= set(routes)
    assert routes[("GET", "/api/v1/users")]["tags"] == ["V1"] and routes[("GET", "/api/v1/users")]["security"]["url_rule"]["access"] == "authenticated"
    assert routes[("POST", "/api/v1/users/{id}")]["security"]["url_rule"]["access"] == "permitAll"      # .AllowAnonymous() overrides the group


def test_exception_handler_with_a_switch_expression_and_exception_classes(tmp_path):
    routes, extractor = scan_source(tmp_path, {
        "Errors.cs": """
            public class AppException : Exception
            {
                public int StatusCode { get; }
                public AppException(int status, string message) : base(message) { StatusCode = status; }
            }
            public class TeapotException : AppException
            {
                public TeapotException() : base(418, "teapot") { }
            }
            public class GlobalExceptionHandler : IExceptionHandler
            {
                public async ValueTask<bool> TryHandleAsync(HttpContext context, Exception exception, CancellationToken ct)
                {
                    var status = exception switch
                    {
                        KeyNotFoundException => 404,
                        ArgumentException => 400,
                        _ => 500,
                    };
                    context.Response.StatusCode = status;
                    return true;
                }
            }
        """,
        "Api.cs": """
            [ApiController]
            [Route("things")]
            public class ThingsController : ControllerBase
            {
                [HttpGet("{id}")]
                public IActionResult Get(int id)
                {
                    if (id == 1) throw new KeyNotFoundException();
                    if (id == 2) throw new TeapotException();
                    throw new ArgumentException("bad");
                }
            }
        """,
    })

    route = routes[("GET", "/things/{id}")]
    statuses = {e["exception"]: (e["status"], e["mapped_by"]) for e in route["exceptions"]}
    assert statuses["KeyNotFoundException"][0] == 404 and statuses["ArgumentException"][0] == 400
    assert statuses["TeapotException"] == (418, "TeapotException")                                # base(418, ...) of the exception class
    assert {404, 400, 418} <= codes(route)


def test_generic_base_class_methods_see_the_type_arguments_of_their_subclass(tmp_path):
    routes, _ = scan_source(tmp_path, {
        "Data.cs": """
            public class Item { public int Id { get; set; } }
            public class Customer { public int Id { get; set; } }
            public class AppDb : DbContext
            {
                public DbSet<Item> Items { get; set; }
                public DbSet<Customer> Customers { get; set; }
            }
            public abstract class Repo<T> where T : class
            {
                protected readonly AppDb Db;
                protected Repo(AppDb db) { Db = db; }
                public Task<List<T>> All() => Db.Set<T>().ToListAsync();
            }
            public class ItemRepo : Repo<Item>
            {
                public ItemRepo(AppDb db) : base(db) { }
            }
            public class CustomerRepo : Repo<Customer>
            {
                public CustomerRepo(AppDb db) : base(db) { }
            }
        """,
        "Api.cs": """
            [ApiController, Route("x")]
            public class XController : ControllerBase
            {
                private readonly ItemRepo _items;
                private readonly CustomerRepo _customers;
                public XController(ItemRepo items, CustomerRepo customers) { _items = items; _customers = customers; }
                [HttpGet("items")] public async Task<IActionResult> Items() => Ok(await _items.All());
                [HttpGet("customers")] public async Task<IActionResult> Customers() => Ok(await _customers.All());
            }
        """,
    })

    assert [f["table"] for f in routes[("GET", "/x/items")]["integrations"]["databases"]] == [["Items"]]
    assert [f["table"] for f in routes[("GET", "/x/customers")]["integrations"]["databases"]] == [["Customers"]]


def test_partial_classes_file_scoped_namespaces_and_nested_types(tmp_path):
    routes, extractor = scan_source(tmp_path, {
        "A.cs": """
            namespace Demo.Api;

            [ApiController]
            [Route("p")]
            public partial class PartController : ControllerBase
            {
                [HttpGet("a")] public IActionResult A() => Ok(new Outer.Inner { Name = "x" });
            }

            public class Outer { public class Inner { public string Name { get; set; } = ""; } }
        """,
        "B.cs": """
            namespace Demo.Api;

            public partial class PartController
            {
                [HttpGet("b")] public IActionResult B() => NoContent();
            }
        """,
    })

    assert {("GET", "/p/a"), ("GET", "/p/b")} <= set(routes)
    assert routes[("GET", "/p/a")]["controller"] == "Demo.Api.PartController"
    assert routes[("GET", "/p/a")]["file"] == "A.cs" and routes[("GET", "/p/b")]["file"] == "B.cs"       # each action keeps its own file


def test_json_naming_options_and_pascal_case(tmp_path):
    routes, _ = scan_source(tmp_path, {
        "Api.cs": """
            public record Money(decimal Amount, [property: JsonPropertyName("ccy")] string Currency);
            public class Wrapper { public Money Total { get; init; } = default!; public List<Money> Parts { get; init; } = new(); }

            [ApiController, Route("w")]
            public class WController : ControllerBase
            {
                [HttpGet] public ActionResult<Wrapper> Get() => new Wrapper();
            }
        """,
        "Program.cs": """
            var builder = WebApplication.CreateBuilder(args);
            builder.Services.AddControllers().AddJsonOptions(o => o.JsonSerializerOptions.PropertyNamingPolicy = null);
        """,
    })

    schema = routes[("GET", "/w")]["response"]["schema"]
    assert set(schema) == {"Total", "Parts"}                                                      # PropertyNamingPolicy = null keeps PascalCase
    assert set(schema["Total"]["schema"]["fields"]) == {"Amount", "ccy"}                          # [JsonPropertyName] always wins


def test_scan_without_aspnet_finds_nothing(tmp_path):
    (tmp_path / "Lib.cs").write_text("public class Plain { public int X() => 1; }\n")

    assert Scanner().scan_full(str(tmp_path))["frameworks"] == []


# ============================================================================
# CHANGES: IMPACT, BREAKING CHANGES, VERDICTS
# ============================================================================

def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def shop_repo(tmp_path):
    repo = tmp_path / "shop"
    shutil.copytree(FIXTURES / "dotnet_shop", repo)
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


def test_a_changed_dto_impacts_its_endpoints_and_reports_the_breaking_change(shop_repo):
    edit(shop_repo, "src/Shop.Api/Models/Dtos.cs", "[Range(1, 100)] int Quantity", "[Range(1, 10)] int Quantity", "stricter")

    routes = impacted(shop_repo)

    assert set(routes) == {("POST", "/api/Orders"), ("POST", "/api/v2/orders"), ("GET", "/api/Orders/{id}"), ("GET", "/api/v2/orders/{id}"),
                           ("GET", "/api/Orders")}
    create = routes[("POST", "/api/Orders")]
    assert "changed model: LineItemDto" in create["change_reasons"]
    assert {"type": "VALIDATION_CHANGED", "field": "quantity", "rule": "maximum", "old": 100, "new": 10, "dto": "LineItemDto"} in create["breaking_changes"]


def test_a_changed_service_method_impacts_only_what_reaches_it(shop_repo):
    edit(shop_repo, "src/Shop.Api/Services/OrderService.cs", '"CREATE"', '"CREATED"', "audit")

    routes = impacted(shop_repo)

    assert set(routes) == {("POST", "/api/Orders"), ("POST", "/api/v2/orders")}
    assert routes[("POST", "/api/Orders")]["change_reasons"] == ["changed: OrderService.CreateAsync"]


def test_a_change_in_a_generic_repository_reaches_every_service_that_uses_it(shop_repo):
    edit(shop_repo, "src/Shop.Data/Repositories/Repositories.cs", "AsNoTracking().ToListAsync()", "ToListAsync()", "tracking")

    assert set(impacted(shop_repo)) == {("GET", "/api/Orders")}


def test_a_changed_attribute_or_route_registration_impacts_the_routes_it_moves(shop_repo):
    edit(shop_repo, "src/Shop.Api/Controllers/OrdersController.cs", '[Route("api/[controller]")]', '[Route("v3/[controller]")]', "route")
    routes = impacted(shop_repo)
    assert {p for _, p in routes} == {"/v3/Orders", "/v3/Orders/{id}", "/v3/Orders/{id}/pay", "/v3/Orders/import"}

    edit(shop_repo, "src/Shop.Api/Program.cs", 'app.MapGroup("/api/v2")', 'app.MapGroup("/api/v9")', "group")
    routes = impacted(shop_repo)
    assert {p for _, p in routes} == {"/api/v9/orders", "/api/v9/orders/{id}"}
    assert all(r["change_reasons"] == ["changed route registration: src/Shop.Api/Program.cs"] for r in routes.values())


def test_a_changed_entity_impacts_the_endpoints_that_use_its_table(shop_repo):
    edit(shop_repo, "src/Shop.Data/Entities/Order.cs", "public string? Note { get; set; }", "public string? Note { get; set; }\n    public decimal Total { get; set; }", "total")

    routes = impacted(shop_repo)

    assert ("POST", "/api/Orders") in routes and ("GET", "/api/Orders") in routes
    assert "uses changed entity: Order" in routes[("GET", "/api/Orders")]["change_reasons"]
    assert ("GET", "/health") not in routes


def test_verdict_between_two_versions_of_an_aspnet_app(shop_repo):
    base = Scanner().scan_full(str(shop_repo), "base")

    dtos = shop_repo / "src/Shop.Api/Models/Dtos.cs"
    dtos.write_text(dtos.read_text().replace("[Range(1, 100)] int Quantity", "[Range(1, 100)] int Quantity, [Required] string Warehouse"))
    controller = shop_repo / "src/Shop.Api/Controllers/OrdersController.cs"
    controller.write_text(controller.read_text().replace("    [HttpPost(\"import\")]", "    [HttpGet(\"{id:int}/lines\")]\n    public IActionResult Lines(int id) => Ok();\n\n    [HttpPost(\"import\")]"))
    head = Scanner().scan_full(str(shop_repo), "head")

    report = contracts.compare_reports(json.loads(json.dumps(base)), json.loads(json.dumps(head)))

    assert report["verdict"] == "hold"
    by_name = {e["name"]: e for e in report["endpoints"]}
    assert ("REQUEST_FIELD_ADDED", "breaking") in {(c["type"], c["severity"]) for c in by_name["OrdersController.Create"]["changes"]}
    assert by_name["OrdersController.Lines"]["status"] == "added"


def test_unchanged_code_is_clear(shop_repo):
    first = json.loads(json.dumps(Scanner().scan_full(str(shop_repo), "a")))
    second = json.loads(json.dumps(Scanner().scan_full(str(shop_repo), "b")))

    assert contracts.compare_reports(first, second)["verdict"] == "clear"
