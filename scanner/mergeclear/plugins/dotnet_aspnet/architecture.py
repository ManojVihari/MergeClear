"""
Application-wide model of an ASP.NET Core code base: modules (projects), components, data model (EF Core), integrations,
security and an architecture graph (nodes + edges) in the shape the other scanners produce, so the server renders all alike.
"""
import re
from collections import defaultdict
from typing import Dict, List, Optional

from .analyzer import Analyzer, dedupe
from .appmodel import AppModel
from .catalog import IGNORED_CATEGORIES
from .csindex import CsIndex, MethodInfo, TypeInfo
from .endpoints import EndpointExtractor
from .project import ProjectInfo, derive_infrastructure, masked, public_dependencies
from .schemas import ModelRegistry

LAYERS = {
    "rest_controller": "web", "application": "config", "configuration": "config", "configuration_properties": "config", "service": "service",
    "component": "service", "repository": "data", "mapper": "data", "feign_client": "integration", "listener": "messaging", "scheduler": "scheduling",
}
NAME_HINTS = [
    ("repository", re.compile(r"(Repository|Repo|Dao|DAO|Store|UnitOfWork|DbContext|Context|Persistence|Queries|QueryService)$")),
    ("feign_client", re.compile(r"(Client|Gateway|Adapter|Proxy|Connector|Sdk|SDK|Integration|ApiClient)$")),
    ("listener", re.compile(r"(Consumer|Listener|Subscriber|Worker|MessageHandler)$")),
    ("scheduler", re.compile(r"(Job|Scheduler|BackgroundService|HostedService)$")),
    ("mapper", re.compile(r"(Mapper|Converter|Profile|Assembler|Mapping)$")),
    ("service", re.compile(r"(Service|Manager|Handler|UseCase|Interactor|Processor|Facade|Orchestrator|Engine|Command|Query)$")),
]
RELATIONAL = {"efcore", "sqlserver", "postgresql", "mysql", "sqlite", "oracle", "dapper", "sql", "databases", "inmemory"}
DOCUMENT = {"mongodb", "cosmosdb", "dynamodb", "marten", "ravendb"}
STATIC_ENDPOINT_PARAM = {"IEndpointRouteBuilder", "WebApplication", "RouteGroupBuilder", "IApplicationBuilder"}


class ArchitectureBuilder:

    def __init__(self, analyzer: Analyzer, app: AppModel, registry: ModelRegistry, endpoints: List[dict], extractor: EndpointExtractor,
                 projects: Dict[str, ProjectInfo], compose: List[dict], index: CsIndex):
        self.a = analyzer
        self.app = app
        self.registry = registry
        self.endpoints = endpoints
        self.ex = extractor
        self.projects = projects
        self.compose = compose
        self.index = index
        self.components: Dict[str, dict] = {}
        self.method_component: Dict[str, str] = {}
        self._facts: Optional[List[tuple]] = None
        self._runtimes: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    def build(self) -> dict:
        self._collect_components()
        modules = self._modules()
        components = sorted(self.components.values(), key=lambda c: c["qualified_name"])
        return {
            "summary": self._summary(modules, components),
            "modules": modules,
            "components": components,
            "data_model": {"entities": self._entities(), "repositories": self._repositories(), "tables": self._tables()},
            "endpoints": [self._endpoint_summary(e) for e in self.endpoints],
            "integrations": self._integrations(),
            "security": self._security(),
            "exception_handling": self._exception_handling(),
            "graph": self._graph(modules, components),
        }

    def all_facts(self) -> List[tuple]:
        if self._facts is None:
            out, seen = [], set()
            for analysis in self.a._analyses.values():
                for fact in analysis.facts:
                    key = (analysis.method.id, tuple(sorted((k, str(v)) for k, v in fact.items())))
                    if key not in seen:
                        seen.add(key)
                        out.append((analysis.method, fact))
            self._facts = out
        return self._facts

    def facts_of(self, kind: str) -> List[dict]:
        return dedupe([f for _, f in self.all_facts() if f.get("kind") == kind])

    # ------------------------------------------------------------------
    # modules
    # ------------------------------------------------------------------

    def _providers(self, path: str) -> Dict[str, str]:
        out = {}
        for ctx_name, info in self.app.dbcontexts.items():
            ctx = self.index.types.get(ctx_name)
            if ctx is not None and ctx.file.project == path and info.get("provider") and info.get("connection"):
                m = re.search(r"ConnectionStrings:([\w.\-]+)", info["connection"])
                if m:
                    out[m.group(1)] = info["provider"]
        return out

    def host_project(self, path: str) -> str:
        """The web application a class library runs in (its configuration is the one that applies), else the project itself."""
        project = self.projects.get(path)
        if project is None or project.is_web or any(r.file.project == path for r in self.app.routes):
            return path
        names = {project.name, project.assembly_name}
        seen = set(names)
        frontier = set(names)
        while frontier:
            nxt = set()
            for p_path, p in self.projects.items():
                if frontier & set(p.project_references) and p.name not in seen:
                    if p.is_web or any(r.file.project == p_path for r in self.app.routes):
                        return p_path
                    nxt.add(p.name)
                    seen.add(p.name)
            frontier = nxt
        return path

    def runtime(self, path: str) -> dict:
        if path not in self._runtimes:
            project = self.projects.get(path)
            self._runtimes[path] = derive_infrastructure(project, self._providers(path), None) if project else {}
            runtime = self._runtimes[path]
            library = self.host_project(path) != path
            if library:
                for key in ("datasources", "redis", "rabbitmq", "kafka", "mongodb", "elasticsearch", "identity_provider", "smtp", "service_bus"):
                    runtime.pop(key, None)
            # a DbContext with a provider but whose connection string is not in appsettings still names a database type
            if project and not library and not runtime.get("datasources"):
                for ctx_name, info in self.app.dbcontexts.items():
                    ctx = self.index.types.get(ctx_name)
                    if ctx is not None and ctx.file.project == path and info.get("provider") and info["provider"] != "inmemory":
                        runtime.setdefault("datasources", []).append({"type": info["provider"], "profile": "default", "name": ctx.name})
        return self._runtimes[path]

    def _web_projects(self) -> List[str]:
        return [p for p, project in self.projects.items() if project.is_web or any(r.file.project == p for r in self.app.routes)]

    def _modules(self) -> List[dict]:
        counts = defaultdict(lambda: defaultdict(int))
        for c in self.components.values():
            counts[c["module"]][c["stereotype"]] += 1
        endpoints_per_module = defaultdict(int)
        for e in self.endpoints:
            endpoints_per_module[e["module"]] += 1
        sources = defaultdict(int)
        for f in self.index.files.values():
            sources[f.project] += 1
        web = set(self._web_projects())

        out = []
        for path, project in sorted(self.projects.items()):
            if not sources.get(path):
                continue
            runtime = self.runtime(path)
            deps = public_dependencies(project.dependencies)
            technologies = sorted({f"{d['category']}: {d['technology']}" for d in deps if d.get("category") and d["category"] not in IGNORED_CATEGORIES})
            aspnet = project.dependency("Microsoft.AspNetCore.App")
            application = path in web
            module = {
                "name": project.name, "path": path, "artifact": project.assembly_name or project.name, "build_tool": "dotnet", "version": project.version,
                "packaging": project.output_type or None, "language": "csharp", "target_frameworks": project.frameworks,
                "dotnet_version": project.frameworks[0] if project.frameworks else None, "sdk": project.sdk or None,
                "aspnet_version": aspnet["version"] if aspnet else None, "nullable": project.nullable,
                "dependencies": [d for d in deps if d.get("scope") != "test"], "technologies": technologies,
                "runtime": runtime, "config_files": project.config.files, "properties": masked(project.config.default),
                "profile_properties": {p: masked(v) for p, v in project.config.profiles.items()},
                "main_class": self._program(path), "is_spring_boot_app": application, "is_application": application,
                "type_counts": dict(counts.get(path, {})), "endpoint_count": endpoints_per_module.get(path, 0),
                "client_base_urls": self._client_base_urls(path), "project_references": project.project_references,
            }
            if not module["type_counts"] and not module["config_files"] and not application and not module["endpoint_count"]:
                module["aggregator"] = True
            out.append(module)
        return out

    def _program(self, path: str) -> Optional[str]:
        for f in self.index.files.values():
            if f.project == path and (f.top_level is not None or any(t.name == "Program" for t in f.types)):
                return f"{f.rel}"
        return None

    def _client_base_urls(self, path: str) -> List[dict]:
        out = []
        for c in self.app.http_clients:
            if c.file.project == path and c.base_url:
                out.append({"base_url": self.a.resolve_placeholders(c.file, c.base_url), "bean": c.client.name if c.client else c.name,
                            "client": "httpclient", "defined_in": c.file.rel})
        return out

    # ------------------------------------------------------------------
    # components
    # ------------------------------------------------------------------

    def _stereotype(self, t: TypeInfo, kinds: set, listener: bool, scheduled: bool) -> str:
        if self.app.is_controller(t):
            return "rest_controller"
        if self.registry.kind(t) == "dbcontext":
            return "repository"
        if listener:
            return "listener"
        if scheduled:
            return "scheduler"
        if t.qualified in self.app.options_sections:
            return "configuration_properties"
        for stereotype, pattern in NAME_HINTS:
            if pattern.search(t.name):
                if stereotype == "feign_client" and "http" not in kinds and "database" in kinds:
                    return "repository"
                return stereotype
        if t.is_static and any(m.params and m.params[0].type.name in STATIC_ENDPOINT_PARAM for m in t.methods):
            return "rest_controller"
        if "database" in kinds and not kinds & {"http", "messaging"}:
            return "repository"
        if "http" in kinds and "database" not in kinds:
            return "feign_client"
        return "component" if kinds else "service"

    @staticmethod
    def _is_test(rel: str) -> bool:
        return any(p.endswith((".Tests", ".Test", ".UnitTests", ".IntegrationTests")) or p in ("tests", "test") for p in rel.split("/")[:-1])

    def _collect_components(self):
        listener_methods = {l["method_id"] for l in self.a.listeners if l.get("method_id")}
        scheduled_methods = {j["method_id"] for j in self.a.scheduled if j.get("method_id")}
        used = set(self.ex.chain_methods) | listener_methods | scheduled_methods
        registered = {r.impl.qualified for r in self.app.services if r.impl is not None}

        for t in self.index.types.values():
            if self._is_test(t.file.rel) or t.outer is not None:
                continue
            kind = self.registry.kind(t)
            if kind in ("enum", "entity", "exception", "interface"):
                continue
            if kind == "model" and t.qualified not in self.app.options_sections:
                continue
            methods = [m for m in t.methods if "public" in m.modifiers or t.kind == "interface"]
            is_controller = self.app.is_controller(t)
            kinds = {f.get("kind") for m, f in self.all_facts() if m.owner is t}
            is_listener = any(m.id in listener_methods for m in t.methods)
            is_scheduled = any(m.id in scheduled_methods for m in t.methods)
            options = t.qualified in self.app.options_sections
            relevant = is_controller or kind == "dbcontext" or options or bool(kinds) or is_listener or is_scheduled or t.qualified in registered \
                or any(m.id in used for m in t.methods + t.ctors) or (t.is_static and any(m.params and m.params[0].type.name in STATIC_ENDPOINT_PARAM for m in t.methods))
            if not relevant:
                continue
            if not methods and not options and kind != "dbcontext":
                continue
            stereotype = self._stereotype(t, kinds, is_listener, is_scheduled)
            self._add_type(t, stereotype, methods)

        # the application itself (Program / Startup / top-level statements)
        for path, project in self.projects.items():
            for f in self.index.files.values():
                if f.project != path or self._is_test(f.rel):
                    continue
                if f.top_level is not None or any(t.name in ("Program", "Startup") for t in f.types):
                    name = "Program"
                    qn = f"{project.name}.Program" if f.top_level is not None and not any(t.name == "Program" for t in f.types) else None
                    if qn is None:
                        continue
                    self.components[qn] = {
                        "name": name, "qualified_name": qn, "module": path, "file": f.rel, "line": 1, "kind": "class", "stereotype": "application",
                        "layer": "config", "annotations": ["WebApplication"], "description": None, "dependencies": [], "methods": [],
                    }
                    self.method_component[f.top_level.id] = qn
                    for m in self.app._lambdas.values():
                        if m.owner is None and m.file is f:
                            self.method_component[m.id] = qn

        self._fill_details()

    def _add_type(self, t: TypeInfo, stereotype: str, methods: List[MethodInfo]):
        qn = t.qualified
        self.components[qn] = {
            "name": t.name, "qualified_name": qn, "module": t.file.project, "file": t.file.rel, "line": t.line_start, "kind": t.kind,
            "stereotype": stereotype, "layer": LAYERS.get(stereotype, "service"), "annotations": [a.name for a in t.attrs],
            "description": t.doc, "extends": [b.name for b in t.bases if self.index.resolve(b.name, t.file, t.namespace, t) is not None and
                                              self.index.resolve(b.name, t.file, t.namespace, t).kind == "class"] or None,
            "implements": [b.name for b in t.bases if self.index.resolve(b.name, t.file, t.namespace, t) is not None and
                           self.index.resolve(b.name, t.file, t.namespace, t).kind == "interface"] or None,
            "dependencies": [],
            "methods": [{"name": m.name, "line": m.line_start, "params": [str(p.type) for p in m.params], "returns": str(m.returns)} for m in methods][:100],
        }
        if stereotype == "configuration_properties":
            self.components[qn]["config_properties"] = [self.app.options_sections.get(qn)]
        for m in t.methods + t.ctors:
            self.method_component[m.id] = qn

    def _fill_details(self):
        integrations: Dict[str, set] = defaultdict(set)
        for m, fact in self.all_facts():
            qn = self.method_component.get(m.id)
            if qn and fact.get("kind") not in ("transaction",):
                integrations[qn].add(f"{fact['kind']}:{fact.get('technology') or fact.get('client')}")
        for qn, comp in self.components.items():
            if integrations.get(qn):
                comp["integrations"] = sorted(integrations[qn])
            t = self.index.types.get(qn)
            if t is not None:
                comp["dependencies"] = self._dependencies(t)

    def _dependencies(self, t: TypeInfo) -> List[dict]:
        deps = []
        ctor_params = list(t.primary)
        for c in t.ctors:
            ctor_params.extend(c.params)
        seen = set()
        for p in ctor_params:
            if p.name in seen:
                continue
            seen.add(p.name)
            deps.append(self._dependency(p.name, p.type, "constructor", t))
        for m in t.members:
            if m.kind == "field" and not m.primary and "static" not in m.modifiers and m.attr("Inject"):
                deps.append(self._dependency(m.name, m.type, "field", t))
        return dedupe([d for d in deps if d.get("target") or d.get("client")])

    def _dependency(self, name: str, ref, injection: str, context: TypeInfo) -> dict:
        entry = {"field": name, "type": str(ref), "injection": injection}
        target = self.index.resolve(ref.name, context.file, context.namespace, context)
        if target is not None:
            entry["target"] = target.qualified
            entry["target_stereotype"] = self.components.get(target.qualified, {}).get("stereotype") or ("interface" if target.kind == "interface" else "component")
            impls = [i.qualified for i, _ in self.app.implementations(target) if i is not target and i.qualified in self.components]
            if target.kind == "interface" and impls:
                entry["implementations"] = impls
            elif target.qualified not in self.components and not impls:
                entry.pop("target")
        else:
            from .catalog import LIBRARY_TYPES
            lib = LIBRARY_TYPES.get(ref.name)
            if lib is not None:
                entry["client"] = lib[0].tech
        return entry

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def _entities(self) -> List[dict]:
        out = []
        for qn in self.registry.entities():
            entity = self.index.types.get(qn)
            if entity is not None and not self._is_test(entity.file.rel):
                info = self.registry.entity_info(entity)
                if info:
                    out.append(info)
        return sorted(out, key=lambda e: e["name"])

    def _repositories(self) -> List[dict]:
        out = []
        for qn, comp in sorted(self.components.items()):
            if comp["stereotype"] != "repository":
                continue
            facts = [f for m, f in self.all_facts() if self.method_component.get(m.id) == qn and f.get("kind") == "database"]
            t = self.index.types.get(qn)
            entities = []
            if t is not None and self.registry.kind(t) == "dbcontext":
                entities = sorted({e.name for e in self.registry.dbsets(t).values()})
            for f in facts:
                for name in f.get("entities") or ([f["entity"]] if f.get("entity") else []):
                    if name not in entities:
                        entities.append(name)
            queries = [{"method": f.get("source", "").split(".")[-1], "native": True, "operation": f.get("operation"), "query": f["query"],
                        "tables": f.get("table") or []} for f in facts if f.get("query")]
            stores = sorted({f.get("technology") for f in facts if f.get("technology")})
            out.append({
                "name": comp["name"], "qualified_name": qn, "module": comp["module"], "file": comp["file"],
                "base": (comp.get("extends") or [None])[0], "entity": entities[0] if entities else None, "entities": entities or None,
                "entity_qualified_name": next((q for q in self.registry.entities() if q.rsplit(".", 1)[-1] == entities[0]), None) if entities else None,
                "store": "efcore" if t is not None and self.registry.kind(t) == "dbcontext" else (stores[0] if stores else None),
                "custom_queries": dedupe(queries),
            })
        return out

    def _tables(self) -> List[dict]:
        tables: Dict[str, dict] = {}

        def entry(name, store):
            return tables.setdefault(name, {"name": name, "store": store, "entities": [], "operations": set(), "accessed_by": set(), "modules": set()})

        for e in self._entities():
            t = entry(e["table"], e["store"])
            t["entities"].append(e["name"])
            t["modules"].add(e["module"])
        for m, f in self.all_facts():
            if f.get("kind") != "database":
                continue
            for table in f.get("table") or []:
                t = entry(table, f.get("technology"))
                if f.get("operation"):
                    t["operations"].add(f["operation"])
                t["accessed_by"].add(f.get("source"))
                t["modules"].add(f.get("module"))
        return [{**v, "operations": sorted(v["operations"]), "accessed_by": sorted(filter(None, v["accessed_by"])),
                 "modules": sorted(m for m in v["modules"] if m is not None)} for _, v in sorted(tables.items())]

    # ------------------------------------------------------------------
    # integrations / security / exceptions
    # ------------------------------------------------------------------

    def _integrations(self) -> dict:
        producers = self.facts_of("messaging")
        consumers = [dict(l) for l in dedupe(self.a.listeners)]
        topics: Dict[tuple, dict] = {}
        for p in producers:
            key = (p.get("technology"), p.get("destination"))
            topics.setdefault(key, {"technology": key[0], "destination": key[1], "producers": [], "consumers": []})["producers"].append(p.get("source"))
        for c in consumers:
            for dest in c.get("destinations") or [None]:
                key = (c.get("technology"), dest)
                topics.setdefault(key, {"technology": key[0], "destination": dest, "producers": [], "consumers": []})["consumers"].append(c.get("handler"))
        events = self.facts_of("event")
        return {
            "databases": self.facts_of("database"),
            "caches": self.facts_of("cache"),
            "messaging": {
                "producers": producers, "consumers": consumers,
                "destinations": sorted(({**v, "producers": sorted(set(filter(None, v["producers"]))), "consumers": sorted(set(filter(None, v["consumers"])))}
                                        for v in topics.values()), key=lambda v: (str(v["technology"]), str(v["destination"]))),
            },
            "external_apis": self.facts_of("http"),
            "feign_clients": [],
            "events": {"published": events, "listeners": []},
            "scheduled_jobs": dedupe(self.a.scheduled),
            "email": self.facts_of("email"),
            "storage": self.facts_of("storage"),
            "search": self.facts_of("search"),
            "resilience": self.facts_of("resilience"),
            "async": self.facts_of("async"),
        }

    def _security(self) -> dict:
        technologies, mechanisms = set(), set()
        for project in self.projects.values():
            for d in project.dependencies:
                if d.get("category") == "security":
                    technologies.add(d["technology"])
        for s in self.app.auth_schemes:
            mechanisms.add(s["scheme"])
        rules: Dict[tuple, dict] = {}
        method_level = 0
        for e in self.endpoints:
            security = e.get("security")
            if not security:
                continue
            method_level += 1
            rule = security.get("url_rule") or {}
            key = (rule.get("access"), tuple(rule.get("roles") or []), tuple(rule.get("policy") or []), e["module"])
            entry = rules.setdefault(key, {"access": rule.get("access"), "defined_in": rule.get("defined_in"), "http_method": None, "module": e["module"],
                                           "patterns": [], "roles": rule.get("roles") or [], "policy": rule.get("policy")})
            if e["path"] not in entry["patterns"] and len(entry["patterns"]) < 40:
                entry["patterns"].append(e["path"])
        if self.app.fallback_policy:
            rules[("fallback",)] = {"access": "authenticated", "defined_in": "FallbackPolicy", "http_method": None, "module": "", "patterns": ["/**"],
                                    "roles": self.app.fallback_policy.get("roles", [])}
        middleware = [dict(m) for m in self.app.middleware]
        names = {d["artifact"].lower() for p in self.projects.values() for d in p.dependencies}
        if any(n.startswith("microsoft.aspnetcore.identity") for n in names):
            mechanisms.add("aspnet-identity")
        return {
            "enabled": bool(rules or mechanisms or technologies), "technologies": sorted(technologies), "mechanisms": sorted(mechanisms),
            "csrf_disabled": False, "url_rules": sorted(rules.values(), key=lambda r: (str(r["access"]), r["patterns"][0] if r["patterns"] else "")),
            "configured_in": sorted({s["file"] for s in self.app.auth_schemes}), "endpoints_with_method_security": method_level,
            "policies": {k: v for k, v in self.app.policies.items()}, "middleware": middleware,
            "cors": bool(self.app.cors) or any(m["name"] == "cors" for m in middleware),
        }

    def _exception_handling(self) -> List[dict]:
        out, seen = [], set()
        for m in self.app.exception_mappings:
            key = (m.exception, m.status)
            if key in seen:
                continue
            seen.add(key)
            out.append({"exception": m.exception, "status": m.status, "handler": m.handler, "source": m.source, "global": True, "reason": None})
        for t in self.index.types.values():
            if self.registry.kind(t) == "exception" and not self._is_test(t.file.rel):
                status = self.app.class_status(t)
                if status and (t.name, status) not in seen:
                    out.append({"exception": t.name, "status": status, "handler": None, "source": "exception class", "reason": None})
        return sorted(out, key=lambda x: x["exception"])

    def _endpoint_summary(self, e: dict) -> dict:
        integrations = e.get("integrations") or {}
        return {
            "method": e["method"], "path": e["path"], "handler": e["handler"], "doc_name": e.get("doc_name"), "module": e["module"],
            "summary": e.get("summary") or (e.get("description") or "")[:200] or None,
            "request_body": (e.get("request_body") or {}).get("type"), "response": (e.get("response") or {}).get("body_type"),
            "status_codes": sorted({s["code"] for s in e.get("status_codes", [])}), "secured": bool(e.get("security")),
            "tables": sorted({t for f in integrations.get("databases", []) for t in (f.get("table") or [])}),
            "caches": sorted({c for f in integrations.get("caches", []) for c in (f.get("cache_names") or [f.get("key") or f.get("technology")]) if c}),
            "publishes": sorted({str(f.get("destination")) for f in integrations.get("messaging", []) if f.get("direction") == "publish"}),
            "calls_external": sorted({f.get("target_service") or f.get("host") or f.get("url") or "?" for f in integrations.get("external_apis", [])}),
        }

    def _summary(self, modules, components) -> dict:
        stereotypes = defaultdict(int)
        for c in components:
            stereotypes[c["stereotype"]] += 1
        technologies = defaultdict(set)
        for m in modules:
            for d in m.get("dependencies", []):
                if d.get("category") and d["category"] not in IGNORED_CATEGORIES:
                    technologies[d["category"]].add(d["technology"])
        apps = [m for m in modules if m["is_application"]]
        names = [(m["runtime"].get("application_name") or m["name"]) for m in apps]
        return {
            "modules": len([m for m in modules if not m.get("aggregator")]), "spring_boot_apps": names, "applications": names, "frameworks": ["aspnet"],
            "types": len(self.index.types), "components": dict(stereotypes), "endpoints": len(self.endpoints), "entities": len(self.registry.entities()),
            "repositories": stereotypes.get("repository", 0), "feign_clients": stereotypes.get("feign_client", 0),
            "message_listeners": len(dedupe(self.a.listeners)), "scheduled_jobs": len(dedupe(self.a.scheduled)),
            "technologies": {k: sorted(v) for k, v in sorted(technologies.items())},
            "middleware": sorted({m["name"] for m in self.app.middleware}),
            "styles": sorted({e.get("style") for e in self.endpoints if e.get("style")}),
            "architecture_style": "microservices" if len(apps) > 1 else "monolith",
        }

    # ------------------------------------------------------------------
    # graph
    # ------------------------------------------------------------------

    def _graph(self, modules, components) -> dict:
        nodes: Dict[str, dict] = {}
        edges: Dict[tuple, dict] = {}
        module_names = {m["path"]: (m["runtime"].get("application_name") or m["name"]) for m in modules}

        def node(node_id, **attrs):
            if node_id not in nodes:
                nodes[node_id] = {"id": node_id, **attrs}
            return node_id

        def edge(src, dst, kind, detail=None):
            if src == dst or src is None or dst is None:
                return
            e = edges.setdefault((src, dst, kind), {"from": src, "to": dst, "kind": kind, "details": []})
            if detail and detail not in e["details"] and len(e["details"]) < 50:
                e["details"].append(detail)

        for m in modules:
            if m.get("aggregator"):
                continue
            node(f"module:{m['path']}", type="module", name=module_names[m["path"]], path=m["path"], spring_boot_app=m["is_application"], port=m["runtime"].get("server_port"))

        component_ids = {}
        for c in components:
            cid = node(f"component:{c['qualified_name']}", type="component", name=c["name"], stereotype=c["stereotype"], layer=c["layer"], module=c["module"])
            component_ids[c["qualified_name"]] = cid
            edge(f"module:{c['module']}", cid, "contains")
            for dep in c.get("dependencies", []):
                for target in dep.get("implementations") or [dep.get("target")]:
                    if target and target in self.components:
                        edge(cid, f"component:{target}", "injects", dep["field"])

        for analysis in self.a._analyses.values():
            src = self.method_component.get(analysis.method.id)
            if src not in component_ids:
                continue
            for e in analysis.calls:
                dst = self.method_component.get(e.callee.id)
                if dst in component_ids:
                    edge(component_ids[src], component_ids[dst], "calls", self.a.label(e.callee))

        configured_store = {}
        for m in modules:
            mid = f"module:{m['path']}"
            runtime = m["runtime"]
            for ds in runtime.get("datasources", []):
                nid = node(f"datastore:{ds.get('type')}:{ds.get('host', 'embedded')}/{ds.get('database', '')}", type="datastore", technology=ds.get("type"),
                           host=ds.get("host"), database=ds.get("database"))
                edge(mid, nid, "connects", ds.get("profile"))
                if ds.get("profile") == "default" or len(runtime["datasources"]) == 1:
                    configured_store[(m["path"], "relational")] = nid
            if runtime.get("mongodb"):
                mongo = runtime["mongodb"]
                nid = node(f"datastore:mongodb:{mongo.get('host', 'default')}/{mongo.get('database', '')}", type="datastore", technology="mongodb",
                           host=mongo.get("host"), database=mongo.get("database"))
                edge(mid, nid, "connects")
                configured_store[(m["path"], "mongodb")] = nid
            if runtime.get("redis"):
                nid = node(f"cache:redis:{runtime['redis'].get('host', 'default')}", type="cache", technology="redis", host=runtime["redis"].get("host"))
                edge(mid, nid, "connects")
            for broker in ("kafka", "rabbitmq", "nats"):
                if runtime.get(broker):
                    edge(mid, node(f"broker:{broker}", type="broker", technology=broker), "connects")
            if runtime.get("service_bus"):
                edge(mid, node("broker:azure-service-bus", type="broker", technology="azure-service-bus"), "connects")
            if runtime.get("elasticsearch"):
                edge(mid, node("search:elasticsearch", type="search", technology="elasticsearch", host=runtime["elasticsearch"].get("host")), "connects")
            if runtime.get("identity_provider"):
                edge(mid, node("infra:identity-provider", type="infrastructure", technology="oauth2", url=runtime["identity_provider"]), "authenticates")
            self._compose_infra(m, mid, runtime, node, edge)

        for method, f in self.all_facts():
            module_path = method.file.project
            host_path = self.host_project(module_path)
            src = component_ids.get(self.method_component.get(method.id) or "") or f"module:{module_path}"
            kind = f.get("kind")
            if kind == "database":
                tech = f.get("technology")
                family = "mongodb" if tech in DOCUMENT else "relational" if tech in RELATIONAL else tech
                profiles = [f"{d.get('profile')}: {d.get('type')}" for d in self.runtime(host_path).get("datasources", [])] or None
                nid = configured_store.get((host_path, family)) or node(f"store:{host_path}:{tech}", type="datastore", technology=tech,
                                                                          module=host_path, profiles=profiles)
                for table in f.get("table") or [None]:
                    edge(src, nid, str(f.get("operation", "uses")).lower(), table)
            elif kind == "cache":
                tech = f.get("technology")
                nid = node(f"cache:{module_path}:{tech}", type="cache", technology=tech, module=module_path)
                edge(src, nid, str(f.get("operation", "uses")).lower(), ", ".join(f.get("cache_names") or []) or f.get("key"))
            elif kind == "messaging":
                edge(src, node(f"broker:{f.get('technology')}", type="broker", technology=f.get("technology")), "publishes", f.get("destination"))
            elif kind == "http":
                target_module = self.ex.resolve_service(f.get("host"), f.get("target_service"))
                if target_module is not None:
                    dst = f"module:{target_module}"
                else:
                    target = f.get("target_service") or f.get("host") or f.get("config_ref") or "unresolved"
                    dst = node(f"external:{target}", type="external_service", name=target)
                edge(src, dst, "http", " ".join(filter(None, [f.get("http_method"), f.get("url")])))
            elif kind in ("email", "storage", "search"):
                nid = node(f"{kind}:{f.get('technology')}", type=kind, technology=f.get("technology"))
                edge(src, nid, str(f.get("operation", "uses")).lower() if f.get("operation") else "uses", f.get("bucket"))

        for listener in dedupe(self.a.listeners):
            src = component_ids.get(self.method_component.get(listener.get("method_id") or "", ""))
            nid = node(f"broker:{listener['technology']}", type="broker", technology=listener["technology"])
            for dest in listener.get("destinations") or [None]:
                edge(nid, src or f"module:{listener['module']}", "delivers", dest)
        for job in dedupe(self.a.scheduled):
            src = component_ids.get(self.method_component.get(job.get("method_id") or "", ""))
            if src:
                edge(node("infra:scheduler", type="infrastructure", technology=job.get("technology") or "scheduler"), src, "triggers", job["handler"])

        module_edges = {}
        for (src, dst, kind), e in edges.items():
            if kind in ("contains", "injects", "calls"):
                continue
            src_module = nodes.get(src, {}).get("module")
            if src.startswith("component:") and dst.startswith("module:") and src_module is not None:
                key = (f"module:{src_module}", dst, kind)
                module_edges.setdefault(key, {"from": key[0], "to": dst, "kind": kind, "details": []})
                module_edges[key]["details"].extend(d for d in e["details"] if d not in module_edges[key]["details"])
        all_edges = list(edges.values()) + [e for k, e in module_edges.items() if k not in edges]
        return {"nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": sorted(all_edges, key=lambda e: (e["from"], e["to"], e["kind"]))}

    def _compose_infra(self, module, module_id, runtime, node, edge):
        project = self.projects.get(module["path"])
        service = project.compose if project else None
        if not service:
            return
        by_name = {s["name"]: s for s in self.compose}
        has = {"postgresql": bool(runtime.get("datasources")), "mysql": bool(runtime.get("datasources")), "mongodb": "mongodb" in runtime,
               "redis": "redis" in runtime, "rabbitmq": "rabbitmq" in runtime, "kafka": "kafka" in runtime}
        for name in service.get("depends_on", []):
            dep = by_name.get(name)
            infra = dep.get("infra") if dep else None
            if not infra or has.get(infra["technology"]):
                continue
            tech, kind = infra["technology"], infra["kind"]
            if kind == "datastore":
                nid = node(f"datastore:{tech}:{name}/", type="datastore", technology=tech, host=name)
            elif kind == "cache":
                nid = node(f"cache:{tech}:{name}", type="cache", technology=tech, host=name)
            elif kind == "broker":
                nid = node(f"broker:{tech}", type="broker", technology=tech)
            elif kind == "infrastructure":
                nid = node(f"infra:{tech}:{name}", type="infrastructure", technology=tech)
            else:
                nid = node(f"{kind}:{tech}", type=kind, technology=tech)
            edge(module_id, nid, "connects", "docker-compose")
