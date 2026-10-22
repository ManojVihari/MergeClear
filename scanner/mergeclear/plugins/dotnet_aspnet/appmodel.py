"""
The ASP.NET Core application structure: the DI container, options binding, HTTP client configuration, authentication and
authorization setup, middleware, exception handling, and every endpoint (controller actions and Minimal API routes, with
the `MapGroup` prefixes and conventions that apply to them).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .catalog import AUTH_SCHEMES, CONTROLLER_BASES, HTTP_ATTRIBUTES, MINIMAL_VERBS
from .csindex import (
    Attr, ConstEvaluator, CsIndex, FileInfo, MethodInfo, ParamInfo, TypeInfo, TypeRef, UNKNOWN, argument_nodes, first, invocation_parts,
    line_of, parse_type, positional, status_from_member, strip_await, text, unparen, walk,
)

logger = logging.getLogger(__name__)

MAX_INTERPRET_DEPTH = 8
BUILDER_TYPES = {"IEndpointRouteBuilder", "WebApplication", "RouteGroupBuilder", "IApplicationBuilder", "IEndpointConventionBuilder", "RouteHandlerBuilder"}
LIFETIMES = {"AddScoped": "scoped", "AddTransient": "transient", "AddSingleton": "singleton", "TryAddScoped": "scoped", "TryAddTransient": "transient",
             "TryAddSingleton": "singleton", "AddKeyedScoped": "scoped", "AddKeyedTransient": "transient", "AddKeyedSingleton": "singleton"}
MIDDLEWARE = {
    "UseAuthentication": "authentication", "UseAuthorization": "authorization", "UseCors": "cors", "UseHttpsRedirection": "https-redirect",
    "UseExceptionHandler": "exception-handler", "UseDeveloperExceptionPage": "developer-exception-page", "UseRateLimiter": "rate-limiter",
    "UseResponseCompression": "compression", "UseSerilogRequestLogging": "request-logging", "UseHsts": "hsts", "UseStaticFiles": "static-files",
    "UseForwardedHeaders": "forwarded-headers", "UseOutputCache": "output-cache", "UseResponseCaching": "response-caching", "UseSession": "session",
    "UseWebSockets": "websockets", "UseHealthChecks": "health-checks", "UseSwagger": "swagger", "UseSwaggerUI": "swagger-ui", "UseStatusCodePages": "status-pages",
    "UseRequestLocalization": "localization", "UseAntiforgery": "antiforgery", "UseMiddleware": "custom", "UseRouting": "routing",
    "UseHttpLogging": "http-logging", "UseMetricServer": "prometheus", "UseHttpMetrics": "prometheus", "UseProblemDetails": "problem-details",
}


@dataclass(eq=False)
class ServiceRegistration:
    service: Optional[TypeInfo]
    service_ref: TypeRef
    impl: Optional[TypeInfo]
    impl_ref: Optional[TypeRef]
    lifetime: str
    file: FileInfo
    line: int
    open_generic: bool = False


@dataclass(eq=False)
class HttpClientConfig:
    name: Optional[str]
    client: Optional[TypeInfo]
    base_url: Optional[str]
    file: FileInfo
    line: int
    resilience: List[str] = field(default_factory=list)


@dataclass(eq=False)
class Builder:
    """A route builder in the abstract interpretation: the application, a `MapGroup`, or a mapped route."""
    prefix: str = ""
    tags: List[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)                     # conventions: auth, produces, filters, rate_limit, cache, versions
    parent: Optional["Builder"] = None
    route: Optional["Route"] = None
    kind: str = "app"                                            # app | group | route | controllers
    span: Optional[Tuple[str, int, int]] = None                  # (file, first line, last line) of the statement that created it

    def chain(self) -> List["Builder"]:
        out, current = [], self
        while current is not None:
            out.append(current)
            current = current.parent
        return list(reversed(out))


@dataclass(eq=False)
class Route:
    kind: str                                                    # controller | minimal | hub
    methods: List[str]
    path: str
    handler: Optional[MethodInfo]
    owner: Optional[TypeInfo]                                    # controller / endpoint class
    file: FileInfo
    line: int
    builder: Optional[Builder] = None
    meta: dict = field(default_factory=dict)
    template_params: List[Tuple[str, str]] = field(default_factory=list)      # [(name, constraint type)]
    name: Optional[str] = None
    hub: Optional[TypeInfo] = None
    lambda_node: object = None


@dataclass
class ExceptionMapping:
    exception: str
    status: Optional[int]
    handler: str
    source: str
    file: str = ""
    line: int = 0


class AppModel:

    def __init__(self, index: CsIndex, const: ConstEvaluator):
        self.index = index
        self.const = const
        self.services: List[ServiceRegistration] = []
        self.http_clients: List[HttpClientConfig] = []
        self.options_sections: Dict[str, str] = {}               # options type qualified name -> configuration section
        self.dbcontexts: Dict[str, dict] = {}                    # context qualified name -> {provider, connection}
        self.auth_schemes: List[dict] = []
        self.policies: Dict[str, dict] = {}
        self.fallback_policy: Optional[dict] = None
        self.middleware: List[dict] = []
        self.hosted: List[TypeInfo] = []
        self.masstransit: List[dict] = []
        self.infra: List[dict] = []                              # connection strings etc. found in registration code: {kind, ref, file}
        self.cors: List[dict] = []
        self.rate_limit_policies: List[str] = []
        self.json_pascal_case = False
        self.routes: List[Route] = []
        self.exception_mappings: List[ExceptionMapping] = []
        self.versions: Dict[str, str] = {}
        self.conventional_pattern = "{controller=Home}/{action=Index}/{id?}"
        self.has_map_controllers = False
        self.path_base: Optional[str] = None
        self.controller_conventions: List[dict] = []             # app.MapControllers().RequireAuthorization()
        self._lambdas: Dict[Tuple[str, int], MethodInfo] = {}
        self._interpreted: set = set()
        self._seen_routes: set = set()

    # ------------------------------------------------------------------
    def build(self):
        for file in self.index.files.values():
            self._scan_file(file)
        self._index_exception_handlers()
        self._discover_minimal_apis()
        self._discover_controllers()

    # ------------------------------------------------------------------
    # configuration scanning
    # ------------------------------------------------------------------

    def _scan_file(self, file: FileInfo):
        for t in file.types:
            for m in t.methods + t.ctors:
                for root in m.body:
                    self._scan_nodes(root, file, t)
        if file.top_level is not None:
            for root in file.top_level.body:
                self._scan_nodes(root, file, None)

    def _scan_nodes(self, root, file: FileInfo, owner: Optional[TypeInfo]):
        for node in walk(root):
            if node.type == "invocation_expression":
                parts = invocation_parts(node)
                if parts is None:
                    continue
                receiver, name, type_args, args = parts
                self._scan_invocation(node, receiver, name, type_args, args, file, owner)
            elif node.type == "assignment_expression":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is not None and text(left).endswith("PropertyNamingPolicy") and right is not None and right.type == "null_literal":
                    self.json_pascal_case = True
                if left is not None and text(left).endswith("FallbackPolicy"):
                    self.fallback_policy = self._policy_from_expression(right, file, owner)

    def _scan_invocation(self, node, receiver, name, type_args, args, file, owner):
        if name in LIFETIMES:
            self._register_service(node, name, type_args, args, file, owner)
        elif name in ("AddHttpClient", "AddRefitClient", "AddTypedClient"):
            self._register_http_client(node, name, type_args, args, file, owner)
        elif name in ("AddDbContext", "AddDbContextPool", "AddDbContextFactory", "AddPooledDbContextFactory") and type_args:
            self._register_dbcontext(node, type_args, args, file, owner)
        elif name == "AddHostedService" and type_args:
            target = self.index.resolve(type_args[0].name, file, owner.namespace if owner else "", owner)
            if target is not None:
                self.hosted.append(target)
        elif name in ("Configure", "ConfigureOptions", "AddOptions", "Bind") and (type_args or name == "Bind"):
            self._register_options(node, name, type_args, args, file, owner)
        elif name in ("Get", "GetSection") and type_args and receiver is not None and "GetSection" in text(receiver):
            section = re.search(r'GetSection\(\s*"([^"]+)"\s*\)', text(receiver))
            target = self.index.resolve(type_args[0].name, file, owner.namespace if owner else "", owner)
            if section and target is not None:
                self.options_sections.setdefault(target.qualified, section.group(1))
        elif name in AUTH_SCHEMES:
            scheme = {"scheme": AUTH_SCHEMES[name], "method": name, "file": file.rel, "line": line_of(node)}
            if args:
                label = self.const.string(args[0][1], file, owner)
                if label:
                    scheme["name"] = label
            self.auth_schemes.append(scheme)
        elif name == "AddAuthentication" and args:
            label = self.const.string(args[0][1], file, owner)
            if label:
                self.auth_schemes.append({"scheme": label.lower(), "method": name, "name": label, "file": file.rel, "line": line_of(node)})
        elif name == "AddPolicy" and len(args) >= 2:
            self._register_policy(args, file, owner)
        elif name in ("SetFallbackPolicy",) and args:
            self.fallback_policy = self._policy_from_expression(args[0][1], file, owner)
        elif name == "AddConsumer" and type_args or name in ("AddConsumers", "AddSagaStateMachine") and type_args:
            consumer = self.index.resolve(type_args[0].name, file, owner.namespace if owner else "", owner) if type_args else None
            if consumer is not None:
                self.masstransit.append({"consumer": consumer, "file": file.rel, "line": line_of(node)})
        elif name in ("ReceiveEndpoint", "ConfigureConsumer") and args:
            queue = self.const.string(args[0][1], file, owner) if name == "ReceiveEndpoint" else None
            if queue:
                self._masstransit_endpoint(node, queue, file, owner)
        elif name in ("UsingRabbitMq", "UsingAzureServiceBus", "UsingAmazonSqs", "UsingInMemory", "UsingActiveMq", "UsingGrpc"):
            self.infra.append({"kind": name[5:].lower(), "ref": None, "file": file.rel, "line": line_of(node), "broker": True})
        elif name in ("Host",) and args and receiver is not None and text(receiver).strip() in ("cfg", "configurator", "x", "bus"):
            host = self.const.string(args[0][1], file, owner)
            if host:
                self.infra.append({"kind": "rabbitmq-host", "ref": host, "file": file.rel, "line": line_of(node)})
        elif name in MIDDLEWARE and (receiver is not None):
            entry = {"name": MIDDLEWARE[name], "call": name, "file": file.rel, "line": line_of(node)}
            if name == "UseMiddleware" and type_args:
                entry["type"] = type_args[0].name
            self.middleware.append(entry)
        elif name == "AddCors" or name == "AddPolicy" and receiver is not None and "cors" in text(receiver).lower():
            self.cors.append({"file": file.rel, "line": line_of(node)})
        elif name == "AddRateLimiter":
            self.rate_limit_policies.append("rate-limiter")
        elif name == "UsePathBase" and args:
            self.path_base = self.const.string(args[0][1], file, owner)
        elif name == "MapControllerRoute" or name == "MapDefaultControllerRoute":
            pattern = next((self.const.string(e, file, owner) for n, e in args if n == "pattern"), None) or \
                (self.const.string(args[1][1], file, owner) if len(args) > 1 else None)
            if pattern:
                self.conventional_pattern = pattern
        elif name in ("UseNpgsql", "UseSqlServer", "UseMySql", "UseSqlite", "UseOracle", "UseCosmos", "UseInMemoryDatabase", "UseMongoDB", "UseNpgsqlStorage",
                      "UsePostgreSqlStorage", "UseSqlServerStorage", "UseRedisStorage", "UseMemoryStorage", "UseMongoStorage"):
            self.infra.append({"kind": name, "ref": self._first_string(args, file, owner), "file": file.rel, "line": line_of(node)})
        elif name in ("AddNpgSql", "AddSqlServer", "AddRedis", "AddMongoDb", "AddRabbitMQ", "AddKafka", "AddElasticsearch", "AddMySql", "AddAzureServiceBusQueue",
                      "AddAzureServiceBusTopic", "AddUrlGroup", "AddOracle"):
            self.infra.append({"kind": name, "ref": self._first_string(args, file, owner), "file": file.rel, "line": line_of(node)})
        elif name in ("AddStackExchangeRedisCache", "AddStackExchangeRedisOutputCache", "AddRedisOutputCache", "AddDistributedMemoryCache",
                      "AddMemoryCache", "AddOutputCache", "AddHybridCache", "AddResponseCaching", "AddDistributedSqlServerCache", "AddDistributedPostgreSqlCache"):
            ref = None
            for lam in (e for _, e in args if e.type == "lambda_expression"):
                for inner in walk(lam):
                    if inner.type == "assignment_expression" and text(inner.child_by_field_name("left")).endswith("Configuration"):
                        ref = self.const.string(inner.child_by_field_name("right"), file, owner)
            self.infra.append({"kind": name, "ref": ref or self._first_string(args, file, owner), "file": file.rel, "line": line_of(node)})

    def _first_string(self, args, file, owner) -> Optional[str]:
        for _, expression in args:
            value = self.const.string(expression, file, owner)
            if value:
                return value
        return None

    # ---- DI ----

    def _register_service(self, node, method: str, type_args, args, file, owner):
        namespace = owner.namespace if owner else ""
        refs = list(type_args)
        impl_ref = None
        keyed = method.startswith("AddKeyed")
        pos = positional(args)
        # AddScoped(typeof(IFoo), typeof(Foo))
        typeofs = [self.const.value(e, file, owner) for e in pos if unparen(e).type == "typeof_expression"]
        if not refs and typeofs:
            refs = [self._ref_from_typeof(t[1]) for t in typeofs if isinstance(t, tuple)]
        if not refs:
            return
        service_ref = refs[0]
        if len(refs) > 1:
            impl_ref = refs[1]
        elif len(pos) > (1 if keyed else 0):
            lam = next((e for e in pos if e.type in ("lambda_expression", "anonymous_method_expression")), None)
            if lam is not None:
                creation = next((n for n in walk(lam) if n.type in ("object_creation_expression", "implicit_object_creation_expression")), None)
                if creation is not None and creation.child_by_field_name("type") is not None:
                    impl_ref = parse_type(creation.child_by_field_name("type"))
        service = self.index.resolve(service_ref.name, file, namespace, owner)
        impl = self.index.resolve(impl_ref.name, file, namespace, owner) if impl_ref else service
        if service is None and impl is None:
            return
        self.services.append(ServiceRegistration(service, service_ref, impl, impl_ref or service_ref, LIFETIMES[method], file, line_of(node),
                                                 open_generic=bool(typeofs) and ("<>" in text(pos[0]) or "<," in text(pos[0]))))

    @staticmethod
    def _ref_from_typeof(value: str) -> TypeRef:
        match = re.match(r"^([\w.]+)", value)
        return TypeRef((match.group(1) if match else value).rsplit(".", 1)[-1])

    def _register_http_client(self, node, method: str, type_args, args, file, owner):
        namespace = owner.namespace if owner else ""
        client = self.index.resolve(type_args[-1].name if len(type_args) > 1 and method != "AddRefitClient" else type_args[0].name, file, namespace, owner) \
            if type_args else None
        if method == "AddHttpClient" and len(type_args) == 2:
            client = self.index.resolve(type_args[1].name, file, namespace, owner)
        name = self.const.string(args[0][1], file, owner) if args and args[0][1].type in ("string_literal", "interpolated_string_expression") else None
        # the whole fluent statement: AddHttpClient<T>(c => c.BaseAddress = ...).AddPolicyHandler(...)
        statement = node
        while statement.parent is not None and statement.parent.type in ("member_access_expression", "invocation_expression", "await_expression"):
            statement = statement.parent
        base_url = None
        resilience = []
        for inner in walk(statement):
            if inner.type == "assignment_expression" and text(inner.child_by_field_name("left")).endswith("BaseAddress"):
                right = inner.child_by_field_name("right")
                creation = right if right is not None and right.type in ("object_creation_expression", "implicit_object_creation_expression") else None
                target = argument_nodes(creation)[0][1] if creation is not None and argument_nodes(creation) else right
                base_url = self.const.string(target, file, owner)
            elif inner.type == "invocation_expression":
                parts = invocation_parts(inner)
                if parts and parts[1] in ("AddPolicyHandler", "AddTransientHttpErrorPolicy", "AddResilienceHandler", "AddStandardResilienceHandler",
                                          "AddPolicyHandlerFromRegistry"):
                    resilience.append(parts[1])
                if parts and parts[1] == "ConfigureHttpClient":
                    for lam in (e for _, e in parts[3] if e.type == "lambda_expression"):
                        for deep in walk(lam):
                            if deep.type == "assignment_expression" and text(deep.child_by_field_name("left")).endswith("BaseAddress"):
                                right = deep.child_by_field_name("right")
                                creation = right if right is not None and right.type in ("object_creation_expression", "implicit_object_creation_expression") else None
                                target = argument_nodes(creation)[0][1] if creation is not None and argument_nodes(creation) else right
                                base_url = base_url or self.const.string(target, file, owner)
        if client is None and name is None:
            return
        self.http_clients.append(HttpClientConfig(name, client, base_url, file, line_of(node), resilience))

    def _register_dbcontext(self, node, type_args, args, file, owner):
        context = self.index.resolve(type_args[0].name, file, owner.namespace if owner else "", owner)
        if context is None:
            return
        info = self.dbcontexts.setdefault(context.qualified, {"provider": None, "connection": None})
        for inner in walk(node):
            if inner.type != "invocation_expression":
                continue
            parts = invocation_parts(inner)
            if parts and parts[1].startswith("Use") and parts[1] in ("UseNpgsql", "UseSqlServer", "UseMySql", "UseSqlite", "UseOracle", "UseCosmos",
                                                                       "UseInMemoryDatabase", "UseMongoDB"):
                info["provider"] = {"UseNpgsql": "postgresql", "UseSqlServer": "sqlserver", "UseMySql": "mysql", "UseSqlite": "sqlite", "UseOracle": "oracle",
                                    "UseCosmos": "cosmosdb", "UseInMemoryDatabase": "inmemory", "UseMongoDB": "mongodb"}[parts[1]]
                info["connection"] = self._first_string(parts[3], file, owner)

    def _register_options(self, node, method: str, type_args, args, file, owner):
        namespace = owner.namespace if owner else ""
        section = None
        statement = node
        while statement.parent is not None and statement.parent.type in ("member_access_expression", "invocation_expression"):
            statement = statement.parent
        for inner in walk(statement):
            if inner.type == "invocation_expression":
                parts = invocation_parts(inner)
                if parts and parts[1] in ("GetSection", "GetRequiredSection") and parts[3]:
                    section = self.const.string(parts[3][0][1], file, owner)
                    break
        if section is None:
            return
        target_ref = type_args[0] if type_args else None
        if target_ref is None:
            return
        target = self.index.resolve(target_ref.name, file, namespace, owner)
        if target is not None:
            self.options_sections.setdefault(target.qualified, section)

    def _register_policy(self, args, file, owner):
        name = self.const.string(args[0][1], file, owner)
        if not name:
            return
        self.policies[name] = self._policy_from_expression(args[1][1], file, owner) or {"authenticated": True}

    def _policy_from_expression(self, node, file, owner) -> Optional[dict]:
        node = unparen(node)
        if node is None:
            return None
        policy = {"roles": [], "claims": [], "authenticated": False, "requirements": []}
        for inner in walk(node):
            if inner.type != "invocation_expression":
                continue
            parts = invocation_parts(inner)
            if parts is None:
                continue
            _, name, type_args, args = parts
            if name == "RequireRole":
                for _, expression in args:
                    policy["roles"].extend(self.const.strings(expression, file, owner))
            elif name == "RequireClaim" and args:
                claim = self.const.string(args[0][1], file, owner)
                values = [v for _, e in args[1:] for v in self.const.strings(e, file, owner)]
                policy["claims"].append(claim + (f"={','.join(values)}" if values else ""))
            elif name == "RequireAuthenticatedUser":
                policy["authenticated"] = True
            elif name in ("AddRequirements", "Requirements"):
                policy["requirements"].append("custom")
            elif name == "RequireAssertion":
                policy["requirements"].append("assertion")
            elif name == "RequireUserName" and args:
                policy["claims"].append("name=" + (self.const.string(args[0][1], file, owner) or ""))
        if not any(policy.values()):
            return {"authenticated": True, "roles": [], "claims": [], "requirements": []}
        return policy

    def _masstransit_endpoint(self, node, queue, file, owner):
        for inner in walk(node):
            if inner.type == "invocation_expression":
                parts = invocation_parts(inner)
                if parts and parts[1] == "ConfigureConsumer" and parts[2]:
                    consumer = self.index.resolve(parts[2][0].name, file, owner.namespace if owner else "", owner)
                    if consumer is not None:
                        self.masstransit.append({"consumer": consumer, "queue": queue, "file": file.rel, "line": line_of(inner)})

    # ------------------------------------------------------------------
    # DI lookups
    # ------------------------------------------------------------------

    def implementations(self, service: TypeInfo, ref: Optional[TypeRef] = None) -> List[Tuple[TypeInfo, Optional[TypeRef]]]:
        """Classes that answer for a service type: container registrations first, then every implementing class."""
        out: List[Tuple[TypeInfo, Optional[TypeRef]]] = []
        for r in self.services:
            if r.service is service and r.impl is not None and r.impl is not service:
                out.append((r.impl, r.impl_ref if not r.open_generic else ref))
            elif r.service is service and r.impl is service and service.kind != "interface" and "abstract" not in service.modifiers:
                out.append((service, ref))
        if out:
            return out
        if service.kind == "interface" or "abstract" in service.modifiers:
            return [(s, ref) for s in self.index.subtypes(service) if s.kind in ("class", "record") and "abstract" not in s.modifiers]
        return [(service, ref)]

    def http_client_for(self, client_type: Optional[TypeInfo], name: Optional[str] = None) -> Optional[HttpClientConfig]:
        for c in self.http_clients:
            if client_type is not None and c.client is client_type:
                return c
        for c in self.http_clients:
            if name is not None and c.name == name:
                return c
        return None

    # ------------------------------------------------------------------
    # Minimal APIs: abstract interpretation of the registration code
    # ------------------------------------------------------------------

    def _discover_minimal_apis(self):
        entry_points: List[MethodInfo] = []
        for file in self.index.files.values():
            if file.top_level is not None:
                entry_points.append(file.top_level)
        for t in self.index.types.values():
            for m in t.methods:
                if m.name in ("Main", "Configure", "ConfigureWebApplication", "Run", "MapEndpoints_"):
                    entry_points.append(m)
        root = Builder(kind="app")
        for m in entry_points:
            self._interpret(m, {}, root, 0, None)

        # registration methods nobody calls (reflection-based modules, Carter, IEndpoint): treat their builder parameter as the root
        for t in list(self.index.types.values()):
            for m in t.methods:
                if m.id in self._interpreted or not m.params:
                    continue
                first_param = m.params[0]
                if first_param.type.name in BUILDER_TYPES and (m.body):
                    self._interpret(m, {first_param.name: root}, root, 0, None)

    def _interpret(self, method: MethodInfo, env: Dict[str, Builder], root: Builder, depth: int, receiver: Optional[Builder]):
        """Walk a method that registers endpoints; `env` binds its builder parameters. Returns the builder it returns, if any."""
        if depth > MAX_INTERPRET_DEPTH:
            return None
        key = (method.id, tuple(sorted((k, id(v)) for k, v in env.items())))
        if key in self._interpreted:
            return None
        self._interpreted.add(key)
        self._interpreted.add(method.id)
        scope = dict(env)
        returned: Optional[Builder] = None
        for body in method.body:
            for node in walk(body):
                kind = node.type
                if kind == "variable_declarator":
                    value = next((c for c in node.named_children if c.type not in ("identifier", "bracketed_argument_list")), None)
                    if value is not None:
                        result = self._evaluate(value, scope, root, method, depth)
                        if isinstance(result, Builder):
                            scope[text(node.child_by_field_name("name"))] = result
                elif kind == "assignment_expression":
                    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                    if left is not None and left.type == "identifier" and right is not None:
                        result = self._evaluate(right, scope, root, method, depth)
                        if isinstance(result, Builder):
                            scope[text(left)] = result
                elif kind == "expression_statement" and node.named_children:
                    self._evaluate(node.named_children[0], scope, root, method, depth)
                elif kind == "return_statement" and node.named_children:
                    result = self._evaluate(node.named_children[0], scope, root, method, depth)
                    if isinstance(result, Builder):
                        returned = result
                elif kind == "arrow_expression_clause" and node.named_children:
                    result = self._evaluate(node.named_children[0], scope, root, method, depth)
                    if isinstance(result, Builder):
                        returned = result
        return returned

    def _evaluate(self, node, scope, root, method, depth):
        """Value of an expression in the registration code: a Builder (or None). Mapping calls register routes as a side effect."""
        node = strip_await(node)
        if node is None:
            return None
        if node.type == "identifier":
            return scope.get(text(node))
        if node.type == "parenthesized_expression" or node.type == "cast_expression":
            return self._evaluate(node.named_children[-1], scope, root, method, depth)
        if node.type == "conditional_expression":
            return self._evaluate(node.named_children[1], scope, root, method, depth)
        if node.type == "lambda_expression":
            return None
        if node.type != "invocation_expression":
            return None
        parts = invocation_parts(node)
        if parts is None:
            return None
        receiver_node, name, type_args, args = parts
        owner = method.owner
        file = method.file

        # `app.UseEndpoints(endpoints => { endpoints.MapGet(...) })`, `app.Map("/x", branch => {...})`
        lambda_args = any(e.type in ("lambda_expression", "anonymous_method_expression") for _, e in args)
        string_first = bool(args) and args[0][1].type in ("string_literal", "interpolated_string_expression", "verbatim_string_literal", "raw_string_literal") \
            or bool(args) and args[0][0] in ("pattern", "path")
        if name in ("UseEndpoints", "UseWhen", "MapWhen") or (name == "Map" and lambda_args and not string_first):
            if receiver_node is None:
                return None
            builder = self._receiver(receiver_node, scope, root, method, depth)
            for _, expression in args:
                if expression.type == "lambda_expression":
                    parameter = self._lambda_parameter(expression)
                    inner_scope = dict(scope)
                    if parameter:
                        inner_scope[parameter] = builder if name == "UseEndpoints" else Builder(prefix="", parent=builder, kind="group")
                    body = expression.child_by_field_name("body")
                    self._interpret_lambda_body(body, inner_scope, root, method, depth + 1)
            return builder

        receiver = self._receiver(receiver_node, scope, root, method, depth) if receiver_node is not None else None

        if (name in MINIMAL_VERBS and not (name == "Map" and not string_first)) or name in ("MapGroup", "MapControllers", "MapHub", "MapHealthChecks",
                                                                                             "MapDefaultControllerRoute", "MapControllerRoute"):
            builder = receiver if receiver is not None else root
            return self._map(node, name, type_args, args, builder, scope, root, method, depth)

        # a call into project code: `app.MapOrderEndpoints()` / `OrderEndpoints.Map(app)` / `RegisterRoutes(group)`
        target = self._registration_method(receiver_node, name, args, method, scope)
        if target is None and isinstance(receiver, Builder):
            # conventions chained on a group or a route
            self._convention(receiver, name, type_args, args, file, owner)
            return receiver
        if target is not None:
            env = {}
            call_args = [e for n, e in args if n is None]
            params = list(target.params)
            if params and "this" in params[0].modifiers:
                # extension method: the receiver is the first parameter
                if isinstance(receiver, Builder) or receiver_node is not None:
                    bound = receiver if isinstance(receiver, Builder) else self._receiver(receiver_node, scope, root, method, depth)
                    env[params[0].name] = bound
                params = params[1:]
            for p, a in zip(params, call_args):
                value = self._evaluate(a, scope, root, method, depth)
                if isinstance(value, Builder):
                    env[p.name] = value
                elif p.type.name in BUILDER_TYPES:
                    env[p.name] = root
            for p in params:
                if p.type.name in BUILDER_TYPES and p.name not in env:
                    env[p.name] = root
            return self._interpret(target, env, root, depth + 1, receiver)
        return receiver if isinstance(receiver, Builder) else None

    def _interpret_lambda_body(self, body, scope, root, method: MethodInfo, depth: int):
        if body is None:
            return
        for node in walk(body):
            kind = node.type
            if kind == "variable_declarator":
                value = next((c for c in node.named_children if c.type not in ("identifier", "bracketed_argument_list")), None)
                if value is not None:
                    result = self._evaluate(value, scope, root, method, depth)
                    if isinstance(result, Builder):
                        scope[text(node.child_by_field_name("name"))] = result
            elif kind == "expression_statement" and node.named_children:
                self._evaluate(node.named_children[0], scope, root, method, depth)
        if body.type == "invocation_expression":
            self._evaluate(body, scope, root, method, depth)

    @staticmethod
    def _lambda_parameter(lam) -> Optional[str]:
        parameters = lam.child_by_field_name("parameters")
        if parameters is None:
            parameter = first(lam, "identifier")
            return text(parameter) if parameter is not None else None
        if parameters.type in ("implicit_parameter", "identifier"):
            return text(parameters)
        first_param = first(parameters, "parameter")
        if first_param is not None:
            return text(first_param.child_by_field_name("name"))
        ident = first(parameters, "identifier")
        return text(ident) if ident is not None else None

    def _receiver(self, node, scope, root, method, depth):
        node = strip_await(node)
        if node is None:
            return root
        if node.type == "identifier":
            return scope.get(text(node), root)
        if node.type == "member_access_expression":
            return self._receiver(node.child_by_field_name("expression"), scope, root, method, depth) \
                if text(node.child_by_field_name("name")) in ("Services",) else root
        value = self._evaluate(node, scope, root, method, depth)
        return value if isinstance(value, Builder) else root

    def _registration_method(self, receiver_node, name, args, method: MethodInfo, scope) -> Optional[MethodInfo]:
        """The project method a registration call goes to."""
        if receiver_node is None:
            if method.owner is not None:
                found = self.index.find_method(method.owner, name, len(args))
                if found is not None and (found.body):
                    return found
            for static in method.file.static_usings:
                t = self.index.types.get(static)
                found = self.index.find_method(t, name, len(args)) if t is not None else None
                if found is not None:
                    return found
        else:
            if receiver_node.type in ("identifier", "member_access_expression", "qualified_name"):
                head = text(receiver_node).rsplit(".", 1)[-1]
                t = self.index.resolve(head, method.file, method.owner.namespace if method.owner else "", method.owner)
                if t is not None and head not in scope:
                    found = self.index.find_method(t, name, len(args))
                    if found is not None:
                        return found
        candidates = [m for m in self.index.extensions.get(name, []) if m.params and m.params[0].type.name in BUILDER_TYPES]
        if candidates:
            return candidates[0]
        # an instance whose class declares the method: `new OrderModule().Map(app)` is rare; Carter-style modules are interpreted as unreferenced roots
        return None

    # ---- mapping calls ----

    def _map(self, node, name, type_args, args, builder: Builder, scope, root, method: MethodInfo, depth: int):
        file, owner = method.file, method.owner
        statement = node
        while statement.parent is not None and statement.parent.type in ("member_access_expression", "invocation_expression", "await_expression"):
            statement = statement.parent
        span = (file.rel, line_of(node), statement.end_point[0] + 1)
        if name == "MapGroup":
            prefix = self.const.string(args[0][1], file, owner) if args else ""
            group = Builder(prefix=prefix or "", parent=builder, kind="group", span=(file.rel, line_of(node), node.end_point[0] + 1))
            return group
        if name == "MapControllers" or name == "MapDefaultControllerRoute":
            self.has_map_controllers = True
            marker = Builder(parent=builder, kind="controllers")
            self.controller_conventions.append({"builder": marker})
            return marker
        if name == "MapControllerRoute":
            return builder
        if name == "MapHub":
            hub = self.index.resolve(type_args[0].name, file, owner.namespace if owner else "", owner) if type_args else None
            path = self.const.string(args[0][1], file, owner) if args else None
            if hub is not None and path is not None:
                route = Route("hub", ["WEBSOCKET"], path, None, hub, file, line_of(node), builder=Builder(parent=builder, kind="route"), hub=hub)
                route.builder.route = route
                self._add_route(route)
                return route.builder
            return builder
        if name == "MapHealthChecks":
            path = self.const.string(args[0][1], file, owner) if args else "/health"
            route = Route("minimal", ["GET"], path or "/health", None, owner, file, line_of(node), builder=Builder(parent=builder, kind="route"))
            route.name = "health-checks"
            route.builder.route = route
            route.meta["synthetic"] = "MapHealthChecks"
            self._add_route(route)
            return route.builder

        # MapGet("/x", handler) / MapMethods("/x", new[] {"GET","POST"}, handler) / Map("/x", handler)
        pos = [(n, e) for n, e in args]
        path_expr = next((e for n, e in pos if n in ("pattern", "path", "route")), None) or (pos[0][1] if pos and pos[0][0] is None else None)
        if name == "MapFallback" and path_expr is not None and path_expr.type in ("lambda_expression", "identifier") and len(pos) == 1:
            path_expr, handler_expr = None, pos[0][1]
        else:
            handler_expr = next((e for n, e in pos if n in ("handler", "requestDelegate")), None)
            remaining = [e for n, e in pos if n is None and e is not path_expr]
            methods_expr = None
            if name == "MapMethods" and remaining:
                methods_expr = remaining[0]
                remaining = remaining[1:]
            if handler_expr is None and remaining:
                handler_expr = remaining[-1]
        path = self.const.string(path_expr, file, owner) if path_expr is not None else ("{*path}" if name == "MapFallback" else "")
        if path is None:
            path = ""
        methods = [MINIMAL_VERBS[name]] if MINIMAL_VERBS.get(name) else []
        if name == "MapMethods" and methods_expr is not None:
            methods = [m.upper() for m in self.const.strings(methods_expr, file, owner)] or ["GET"]
        if not methods:
            methods = ["ANY"]
        handler = self._handler(handler_expr, method, scope)
        route_builder = Builder(parent=builder, kind="route", span=span)
        for m in methods:
            route = Route("minimal", [m], path, handler, handler.owner if handler is not None else owner, file, line_of(node), builder=route_builder,
                          lambda_node=handler_expr if handler is not None and handler.id.startswith("lambda:") else None)
            route_builder.route = route
            self._add_route(route)
        return route_builder

    def _add_route(self, route: Route):
        key = (route.handler.id if route.handler else (route.file.rel, route.line), tuple(route.methods), route.path, id(route.builder.parent) if route.builder else 0)
        if key in self._seen_routes:
            return
        self._seen_routes.add(key)
        self.routes.append(route)

    def _handler(self, expression, method: MethodInfo, scope) -> Optional[MethodInfo]:
        """The method a Minimal API route runs: a lambda (synthesized), a method group, or a local function."""
        expression = unparen(expression)
        if expression is None:
            return None
        if expression.type == "cast_expression":
            expression = unparen(expression.named_children[-1])
        if expression.type in ("lambda_expression", "anonymous_method_expression"):
            key = (method.file.rel, expression.start_byte)
            cached = self._lambdas.get(key)
            if cached is not None:
                return cached
            parameters = expression.child_by_field_name("parameters")
            plist = []
            if parameters is not None and parameters.type == "implicit_parameter":
                plist = [ParamInfo(text(parameters), TypeRef("object"))]
            elif parameters is not None:
                from .csindex import parse_parameters
                plist = parse_parameters(parameters, method.file)
            else:
                ident = first(expression, "identifier")
                if ident is not None:
                    plist = [ParamInfo(text(ident), TypeRef("object"))]
            body = expression.child_by_field_name("body")
            from .csindex import parse_attributes
            attrs = parse_attributes(expression, method.file)
            declared = expression.child_by_field_name("type")
            synthetic = MethodInfo(
                id=f"lambda:{method.file.rel}:{expression.start_byte}", name=f"lambda@{line_of(expression)}", owner=method.owner, node=expression,
                file=method.file, attrs=attrs, params=plist, returns=parse_type(declared) if declared is not None else TypeRef("IResult"),
                body=[body] if body is not None else [],
                is_async=any(text(c) == "async" for c in expression.children if c.type == "modifier") or text(expression).lstrip().startswith("async"),
                line_start=line_of(expression), line_end=expression.end_point[0] + 1,
            )
            self._lambdas[key] = synthetic
            self.index.methods[synthetic.id] = synthetic
            return synthetic
        name = text(expression)
        if "." in name:
            type_name, _, member = name.rpartition(".")
            t = self.index.resolve(type_name.rsplit(".", 1)[-1], method.file, method.owner.namespace if method.owner else "", method.owner)
            if t is not None:
                return self.index.find_method(t, member)
            return None
        if method.owner is not None:
            found = self.index.find_method(method.owner, name)
            if found is not None:
                return found
        # local function in the registration method
        for body in method.body:
            for n in walk(body):
                if n.type == "local_function_statement" and text(n.child_by_field_name("name")) == name:
                    from .csindex import parse_parameters, parse_attributes
                    plist = parse_parameters(n.child_by_field_name("parameters") or first(n, "parameter_list"), method.file)
                    local_body = n.child_by_field_name("body")
                    return MethodInfo(
                        id=f"local:{method.file.rel}:{n.start_byte}", name=name, owner=method.owner, node=n, file=method.file,
                        attrs=parse_attributes(n, method.file), params=plist, returns=parse_type(n.child_by_field_name("returns") or n.child_by_field_name("type")),
                        body=[local_body] if local_body is not None else [], line_start=line_of(n), line_end=n.end_point[0] + 1)
        for static in method.file.static_usings:
            t = self.index.types.get(static)
            found = self.index.find_method(t, name) if t is not None else None
            if found is not None:
                return found
        return None

    # ---- conventions ----

    def _convention(self, builder: Builder, name, type_args, args, file, owner):
        meta = builder.meta
        if name == "WithTags":
            for _, e in args:
                builder.tags.extend(self.const.strings(e, file, owner))
        elif name in ("WithName", "WithDisplayName") and args:
            meta["name"] = self.const.string(args[0][1], file, owner)
        elif name == "WithSummary" and args:
            meta["summary"] = self.const.string(args[0][1], file, owner)
        elif name == "WithDescription" and args:
            meta["description"] = self.const.string(args[0][1], file, owner)
        elif name == "WithOpenApi":
            for _, e in args:
                if e.type == "lambda_expression":
                    for inner in walk(e):
                        if inner.type == "assignment_expression":
                            left = text(inner.child_by_field_name("left"))
                            if left.endswith(".Summary"):
                                meta["summary"] = self.const.string(inner.child_by_field_name("right"), file, owner)
                            elif left.endswith(".Description"):
                                meta["description"] = self.const.string(inner.child_by_field_name("right"), file, owner)
        elif name in ("RequireAuthorization", "RequireAuthorisation"):
            entry = {"policies": [], "roles": [], "level": builder.kind}
            for label, e in args:
                if e.type in ("object_creation_expression",):
                    for inner in walk(e):
                        if inner.type == "assignment_expression":
                            left = text(inner.child_by_field_name("left"))
                            if left == "Roles":
                                entry["roles"].extend(r.strip() for r in (self.const.string(inner.child_by_field_name("right"), file, owner) or "").split(","))
                            elif left == "Policy":
                                value = self.const.string(inner.child_by_field_name("right"), file, owner)
                                if value:
                                    entry["policies"].append(value)
                else:
                    entry["policies"].extend(self.const.strings(e, file, owner))
            meta.setdefault("authorize", []).append(entry)
        elif name == "AllowAnonymous":
            meta["anonymous"] = True
        elif name == "Produces" and (type_args or args):
            status = next((self.const.integer(e, file, owner) for n, e in args if n in (None, "statusCode")), None) or 200
            if args and self.const.integer(args[0][1], file, owner) is None:
                status = 200
            meta.setdefault("produces", []).append((status, type_args[0] if type_args else None))
        elif name == "ProducesProblem" and args:
            meta.setdefault("produces", []).append((self.const.integer(args[0][1], file, owner) or 500, TypeRef("ProblemDetails")))
        elif name == "ProducesValidationProblem":
            meta.setdefault("produces", []).append((self.const.integer(args[0][1], file, owner) if args else 400, TypeRef("HttpValidationProblemDetails")))
        elif name == "Accepts" and (type_args or args):
            content = [self.const.string(e, file, owner) for _, e in args if e.type == "string_literal"]
            meta["accepts"] = (type_args[0] if type_args else None, [c for c in content if c])
        elif name in ("RequireRateLimiting", "DisableRateLimiting"):
            meta["rate_limit"] = self.const.string(args[0][1], file, owner) if args else "disabled"
        elif name in ("CacheOutput",):
            meta["cache"] = True
        elif name in ("AddEndpointFilter", "AddEndpointFilterFactory"):
            meta.setdefault("filters", []).append(type_args[0].name if type_args else "filter")
        elif name in ("HasApiVersion", "MapToApiVersion", "WithApiVersionSet"):
            if args:
                meta["version"] = self.const.string(args[0][1], file, owner) or text(args[0][1])
        elif name == "ExcludeFromDescription":
            meta["hidden"] = True

    # ------------------------------------------------------------------
    # controllers
    # ------------------------------------------------------------------

    def is_controller(self, t: TypeInfo) -> bool:
        if t.kind != "class" or "abstract" in t.modifiers or t.attr("NonController"):
            return False
        if t.attr("ApiController") or any(a.attr("ApiController") for a in self.index.ancestors(t)):
            return True
        bases = set(self.index.base_names(t))
        return bool(bases & CONTROLLER_BASES) or (t.name.endswith("Controller") and "ControllerBase" in bases | CONTROLLER_BASES)

    def controllers(self) -> List[TypeInfo]:
        return [t for t in self.index.types.values() if self.is_controller(t)]

    def inherited_attrs(self, t: TypeInfo, *names) -> List[Tuple[Attr, TypeInfo]]:
        out = []
        for a in self.index.ancestors(t):
            for attr in a.attrs:
                if attr.name in names:
                    out.append((attr, a))
        return out

    def _discover_controllers(self):
        for controller in self.controllers():
            self._controller_routes(controller)

    def _controller_routes(self, controller: TypeInfo):
        class_routes = [self.const.string(a.args[0], a.file, owner) if a.args else "" for a, owner in self.inherited_attrs(controller, "Route")][:1] \
            if self.inherited_attrs(controller, "Route") else []
        # a [Route] on the class itself wins over an inherited one
        own = [a for a in controller.attrs if a.name == "Route"]
        if own:
            class_routes = [self.const.string(a.args[0], a.file, controller) or "" for a in own]
        versions = [self.const.string(a.args[0], a.file, controller) for a, _ in self.inherited_attrs(controller, "ApiVersion") if a.args]
        area = next((self.const.string(a.args[0], a.file, controller) for a, _ in self.inherited_attrs(controller, "Area") if a.args), None)
        controller_name = controller.name[:-10] if controller.name.endswith("Controller") else controller.name
        api = bool(controller.attr("ApiController") or any(a.attr("ApiController") for a in self.index.ancestors(controller)))

        seen_methods = set()
        for owner_type in self.index.ancestors(controller):
            if owner_type is not controller and not self.is_controller_base(owner_type):
                continue
            for m in owner_type.methods:
                if m.name in seen_methods and "override" in m.modifiers:
                    continue
                if not self._is_action(m, owner_type):
                    continue
                seen_methods.add(m.name)
                self._action_routes(controller, owner_type, m, class_routes, controller_name, area, versions, api)

    def is_controller_base(self, t: TypeInfo) -> bool:
        return t.name not in CONTROLLER_BASES and t.kind == "class"

    @staticmethod
    def _is_action(m: MethodInfo, owner: TypeInfo) -> bool:
        if "public" not in m.modifiers or "static" in m.modifiers or m.is_ctor or m.attr("NonAction"):
            return False
        if m.name in ("Dispose", "OnActionExecuting", "OnActionExecuted", "OnActionExecutionAsync") or m.name.startswith("get_"):
            return False
        if "abstract" in m.modifiers and not m.attrs:
            return False
        return True

    def _action_routes(self, controller: TypeInfo, owner_type: TypeInfo, m: MethodInfo, class_routes, controller_name, area, versions, api):
        verb_attrs = [(HTTP_ATTRIBUTES[a.name], a) for a in m.attrs if a.name in HTTP_ATTRIBUTES]
        route_attrs = [a for a in m.attrs if a.name == "Route"]
        accept_verbs = [a for a in m.attrs if a.name == "AcceptVerbs"]
        file = m.file
        if not verb_attrs and not route_attrs and not accept_verbs:
            if api or class_routes:
                if not class_routes:
                    return
                # [Route("api/[controller]")] on the class makes every public action reachable as GET without a verb attribute
                if not api:
                    verb_attrs = [("GET", Attr("HttpGet", None))]
                else:
                    return
            else:
                self._conventional_route(controller, owner_type, m, controller_name, area)
                return

        if not class_routes and not route_attrs and verb_attrs and not any(attr and attr.args for _, attr in verb_attrs):
            # [HttpPost] on a class that uses conventional routing: the verb picks the method, the route pattern the URL
            self._conventional_route(controller, owner_type, m, controller_name, area, verbs=[verb for verb, _ in verb_attrs])
            return

        templates: List[Tuple[str, str, Optional[str]]] = []          # (method, template, route name)
        if verb_attrs:
            for verb, attr in verb_attrs:
                template = self.const.string(attr.args[0], attr.file, owner_type) if attr and attr.args else None
                name = self.const.string(attr.named["Name"], attr.file, owner_type) if attr and "Name" in attr.named else None
                if template is None and route_attrs:
                    template = self.const.string(route_attrs[0].args[0], file, owner_type) if route_attrs[0].args else ""
                templates.append((verb, template or "", name))
        elif accept_verbs:
            verbs = [self.const.string(a, file, owner_type) or "GET" for a in accept_verbs[0].args]
            template = next((self.const.string(r.args[0], file, owner_type) for r in route_attrs if r.args), "")
            for v in verbs or ["GET"]:
                templates.append((v.upper(), template, None))
        else:
            for r in route_attrs:
                templates.append(("ANY", self.const.string(r.args[0], file, owner_type) if r.args else "", None))

        for verb, template, route_name in templates:
            bases = [""] if not class_routes else class_routes
            if template.startswith("/") or template.startswith("~/"):
                bases = [""]
                template = template.lstrip("~").lstrip("/")
            for base in bases:
                path = "/" + "/".join(p.strip("/") for p in (base, template) if p and p.strip("/"))
                path = self._substitute_tokens(path, controller_name, m.name, area, versions)
                route = Route("controller", [verb], path, m, controller, file, m.line_start, name=route_name)
                route.meta["declaring"] = owner_type
                self._add_route(route)

    def _conventional_route(self, controller: TypeInfo, owner_type: TypeInfo, m: MethodInfo, controller_name: str, area, verbs=None):
        returns = m.returns.name
        if returns not in ("IActionResult", "ActionResult", "ViewResult", "Task", "string", "JsonResult", "ContentResult", "IViewComponentResult",
                           "FileResult", "RedirectResult", "PartialViewResult") and not (m.returns.args and m.returns.args[0].name in (
                               "IActionResult", "ActionResult", "ViewResult", "JsonResult", "string")):
            return
        pattern = self.conventional_pattern
        action = m.name[:-5] if m.name.endswith("Async") else m.name
        path = pattern
        path = re.sub(r"\{controller(=[^}?]*)?\}", controller_name, path)
        path = re.sub(r"\{action(=[^}?]*)?\}", action, path)
        if area:
            path = f"{area}/{path}"
        has_id = any(p.name == "id" for p in m.params)
        path = re.sub(r"/?\{id\??\}", "/{id}" if has_id else "", path)
        path = "/" + path.strip("/")
        if action == "Index":
            path = path.rsplit("/", 1)[0] or "/"
            if path == f"/{controller_name}" and controller_name == "Home":
                path = "/"
        for verb in verbs or ["GET"]:
            route = Route("controller", [verb], path or "/", m, controller, m.file, m.line_start)
            route.meta["conventional"] = True
            route.meta["declaring"] = owner_type
            self._add_route(route)

    def _substitute_tokens(self, path: str, controller: str, action: str, area, versions) -> str:
        action = action[:-5] if action.endswith("Async") else action
        path = path.replace("[controller]", controller).replace("[action]", action)
        path = path.replace("[area]", area or "")
        version = (versions[0] if versions else None)
        if version:
            clean = re.sub(r"\.0$", "", version)
            path = re.sub(r"\{version(:apiVersion)?\}", clean, path)
        return re.sub(r"/{2,}", "/", path)

    # exception handling
    # ------------------------------------------------------------------

    def _index_exception_handlers(self):
        for t in self.index.types.values():
            names = set(self.index.base_names(t))
            handler_like = bool(names & {"IExceptionHandler", "IExceptionFilter", "ExceptionFilterAttribute", "IAsyncExceptionFilter", "IProblemDetailsWriter"}) \
                or re.search(r"(ExceptionHandler|ExceptionMiddleware|ErrorHandler|ErrorHandling|GlobalException)", t.name) \
                or any(m.name in ("InvokeAsync", "Invoke") and m.params and m.params[0].type.name == "HttpContext" for m in t.methods) \
                and any("catch" in text(root) for m in t.methods for root in m.body)
            if handler_like:
                for m in t.methods:
                    for root in m.body:
                        self._read_exception_arms(root, m.file, t, f"{t.name}.{m.name}")
        # inline: app.Use(async (ctx, next) => { try { ... } catch (X) {...} }) and app.UseExceptionHandler(...)
        for file in self.index.files.values():
            roots = []
            if file.top_level is not None:
                roots.extend(file.top_level.body)
            for t in file.types:
                for m in t.methods:
                    if m.name in ("Configure", "Main", "ConfigureWebApplication", "UseExceptionHandling"):
                        roots.extend(m.body)
            for root in roots:
                for node in walk(root):
                    if node.type != "invocation_expression":
                        continue
                    parts = invocation_parts(node)
                    if parts and parts[1] in ("Use", "UseExceptionHandler", "UseMiddleware", "Map") and any(e.type == "lambda_expression" for _, e in parts[3]):
                        for _, e in parts[3]:
                            if e.type == "lambda_expression":
                                self._read_exception_arms(e, file, None, f"Program.{parts[1]}")
                    elif parts and parts[1] == "Map" and parts[2]:           # Hellang ProblemDetails: options.Map<NotFoundException>(ex => ...)
                        exc = parts[2][0].name
                        status = self._find_status(node)
                        self._add_mapping(exc, status, "ProblemDetails.Map", file, line_of(node))

    def _read_exception_arms(self, root, file: FileInfo, owner: Optional[TypeInfo], handler: str):
        for node in walk(root):
            kind = node.type
            if kind == "catch_clause":
                declaration = first(node, "catch_declaration")
                body = node.child_by_field_name("body") or first(node, "block")
                if declaration is not None and body is not None:
                    exc = text(declaration.child_by_field_name("type") or declaration.named_children[0])
                    status = self._find_status(body)
                    self._add_mapping(exc.rsplit(".", 1)[-1], status, handler, file, line_of(node))
            elif kind == "switch_expression_arm":
                pattern, value = self._arm_parts(node)
                status = self._find_status(value)
                for exc in self._pattern_types(pattern):
                    self._add_mapping(exc, status, handler, file, line_of(node))
            elif kind == "switch_section":
                labels = [c for c in node.named_children if c.type == "case_switch_label"]
                body = [c for c in node.named_children if c.type not in ("case_switch_label", "default_switch_label")]
                status = None
                for b in body:
                    status = status or self._find_status(b)
                for label in labels:
                    for exc in self._pattern_types(label):
                        self._add_mapping(exc, status, handler, file, line_of(node))
            elif kind == "if_statement":
                condition = node.child_by_field_name("condition")
                consequence = node.child_by_field_name("consequence")
                if condition is not None and consequence is not None and ("is " in text(condition) or "typeof" in text(condition)):
                    status = self._find_status(consequence)
                    for exc in self._pattern_types(condition):
                        self._add_mapping(exc, status, handler, file, line_of(node))

    @staticmethod
    def _arm_parts(arm):
        children = arm.named_children
        pattern = children[0] if children else None
        value = children[-1] if len(children) > 1 else None
        return pattern, value

    def _pattern_types(self, node) -> List[str]:
        names = []
        if node is None:
            return names
        for n in walk(node):
            if n.type == "declaration_pattern":
                t = n.child_by_field_name("type") or (n.named_children[0] if n.named_children else None)
                if t is not None:
                    names.append(text(t).rsplit(".", 1)[-1])
            elif n.type == "type_pattern":
                names.append(text(n.named_children[0]).rsplit(".", 1)[-1])
            elif n.type == "constant_pattern" and n.named_children and n.named_children[0].type in ("identifier", "member_access_expression"):
                names.append(text(n.named_children[0]).rsplit(".", 1)[-1])
            elif n.type == "typeof_expression":
                names.append(text(n.named_children[-1]).rsplit(".", 1)[-1])
            elif n.type == "recursive_pattern":
                t = first(n, "identifier", "generic_name")
                if t is not None:
                    names.append(text(t))
            elif n.type == "is_pattern_expression":
                pass
            elif n.type == "binary_expression" and " is " in text(n):
                right = n.child_by_field_name("right")
                if right is not None and right.type in ("identifier", "generic_name"):
                    names.append(text(right))
        return [n for n in dict.fromkeys(names) if n and n[0].isupper()]

    def _find_status(self, node) -> Optional[int]:
        """First HTTP status in a handler fragment: `StatusCode = 404`, `Results.NotFound()`, `HttpStatusCode.Conflict`, `Problem(statusCode: 422)`."""
        if node is None:
            return None
        from .catalog import RESULT_HELPERS
        for n in walk(node):
            kind = n.type
            if kind == "integer_literal" and n.start_byte == node.start_byte:          # `Type => 404`
                value = self._status_value(n)
                if value and 100 <= value <= 599:
                    return value
            elif kind == "assignment_expression":
                left = text(n.child_by_field_name("left"))
                if left.endswith(("StatusCode", ".Status", "Status")):
                    value = self._status_value(n.child_by_field_name("right"))
                    if value:
                        return value
            elif kind == "invocation_expression":
                parts = invocation_parts(n)
                if parts is None:
                    continue
                receiver, name, _, args = parts
                if name in ("StatusCode", "Problem", "WriteAsJsonAsync", "SetStatusCode") or name in RESULT_HELPERS:
                    for label, e in args:
                        if label in (None, "statusCode", "status"):
                            value = self._status_value(e)
                            if value:
                                return value
                    if name in RESULT_HELPERS and RESULT_HELPERS[name] and name not in ("Ok", "File", "Content", "Json", "Text"):
                        return RESULT_HELPERS[name]
            elif kind == "member_access_expression":
                value = status_from_member(text(n))
                if value:
                    return value
            elif kind == "initializer_expression":
                for a in n.named_children:
                    if a.type == "assignment_expression" and text(a.child_by_field_name("left")) == "Status":
                        value = self._status_value(a.child_by_field_name("right"))
                        if value:
                            return value
        return None

    def _status_value(self, node) -> Optional[int]:
        value = self.const.value(node, None) if node is not None else UNKNOWN
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
        if node is not None:
            return status_from_member(text(unparen(node)).replace("(int)", "").strip("() "))
        return None

    def _add_mapping(self, exc: str, status: Optional[int], handler: str, file: FileInfo, line: int):
        if not exc or exc in ("Exception_",):
            return
        self.exception_mappings.append(ExceptionMapping(exc, status, handler, "exception handler", file.rel, line))

    def class_status(self, cls: TypeInfo) -> Optional[int]:
        """Status an exception class carries itself: `base(404, ...)`, `StatusCode = 404`."""
        for t in self.index.ancestors(cls):
            for c in t.ctors:
                initializer = first(c.node, "constructor_initializer")
                if initializer is not None:
                    for _, expression in argument_nodes(initializer):
                        value = self._status_value(expression)
                        if value:
                            return value
            for m in t.members:
                if m.kind == "property" and re.search(r"status", m.name, re.I) and m.init is not None:
                    value = self._status_value(m.init)
                    if value:
                        return value
        return None

    def exception_mapping_for(self, cls: Optional[TypeInfo], name: str) -> Optional[ExceptionMapping]:
        names = [name]
        if cls is not None:
            names = [a.name for a in self.index.ancestors(cls)] + [b for a in self.index.ancestors(cls) for b in [x.name for x in a.bases]]
        for candidate in names:
            for mapping in self.exception_mappings:
                if mapping.exception == candidate and mapping.status:
                    return mapping
        return None
