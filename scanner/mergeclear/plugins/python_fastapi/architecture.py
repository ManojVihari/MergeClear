"""
Application-wide model of a FastAPI code base: modules, components, data model, integrations, security and an
architecture graph (nodes + edges) in the shape the Spring scanner produces, so the server renders both alike.
"""
import re
from collections import defaultdict
from typing import Dict, List, Optional

from .analyzer import Analyzer, dedupe
from .appmodel import AppModel, RouterInfo
from .catalog import IGNORED_CATEGORIES
from .endpoints import EndpointExtractor
from .project import ProjectInfo, derive_infrastructure, masked, public_dependencies
from .pyindex import ClassInfo, FuncInfo, PythonIndex
from .schemas import ModelRegistry

LAYERS = {
    "rest_controller": "web", "application": "config", "configuration": "config", "configuration_properties": "config",
    "service": "service", "component": "service", "repository": "data", "mapper": "data", "feign_client": "integration",
    "listener": "messaging", "scheduler": "scheduling",
}
NAME_HINTS = [
    ("repository", re.compile(r"(Repository|Repo|CRUD|Crud|DAO|Dao|Store|Storage|Dal|Queries|Persistence|Database)$")),
    ("feign_client", re.compile(r"(Client|Gateway|Adapter|Proxy|Connector|Sdk|SDK|Integration|ApiClient)$")),
    ("listener", re.compile(r"(Consumer|Listener|Subscriber|Worker|Handler)$")),
    ("mapper", re.compile(r"(Mapper|Converter|Serializer|Transformer|Assembler)$")),
    ("service", re.compile(r"(Service|Manager|UseCase|Interactor|Processor|Facade|Logic|Controller|Orchestrator|Engine)$")),
]
PATH_HINTS = [
    ("repository", ("crud", "repositories", "repository", "repos", "dao", "daos", "persistence", "queries")),
    ("feign_client", ("clients", "client", "integrations", "integration", "adapters", "gateways", "external", "providers")),
    ("listener", ("consumers", "listeners", "subscribers", "workers", "worker", "tasks", "jobs")),
    ("service", ("services", "service", "usecases", "use_cases", "domain", "business", "logic")),
    ("configuration", ("config", "settings")),
]
PLAIN_PATHS = {"deps", "dependencies", "auth", "security", "utils", "helpers", "common", "shared", "lib", "main"}
RELATIONAL = {"sqlalchemy", "sqlmodel", "sqlite", "postgresql", "mysql", "sqlserver", "oracle", "databases", "tortoise", "peewee", "ormar",
              "pony", "django", "prisma", "clickhouse", "aiosqlite"}
DOCUMENT = {"mongodb", "beanie", "mongoengine", "odmantic"}


class ArchitectureBuilder:

    def __init__(self, analyzer: Analyzer, appmodel: AppModel, registry: ModelRegistry, endpoints: List[dict], extractor: EndpointExtractor,
                 projects: Dict[str, ProjectInfo], compose: List[dict], index: PythonIndex, pydantic_v2: bool):
        self.a = analyzer
        self.app = appmodel
        self.registry = registry
        self.endpoints = endpoints
        self.ex = extractor
        self.projects = projects
        self.compose = compose
        self.index = index
        self.pydantic_v2 = pydantic_v2
        self.components: Dict[str, dict] = {}
        self.func_component: Dict[str, str] = {}
        self.router_component: Dict[str, str] = {}
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
            "data_model": {
                "entities": sorted((e for e in self._entities()), key=lambda e: e["name"]),
                "repositories": self._repositories(),
                "tables": self._tables(),
            },
            "endpoints": [self._endpoint_summary(e) for e in self.endpoints],
            "integrations": self._integrations(),
            "security": self._security(),
            "exception_handling": self._exception_handling(),
            "graph": self._graph(modules, components),
        }

    # ------------------------------------------------------------------
    # facts of every analysed function (bound variants share their facts)
    # ------------------------------------------------------------------

    def all_facts(self) -> List[tuple]:
        if self._facts is None:
            out, seen = [], set()
            for analysis in self.a._analyses.values():
                for fact in analysis.facts:
                    key = (analysis.func.id, tuple(sorted((k, str(v)) for k, v in fact.items())))
                    if key not in seen:
                        seen.add(key)
                        out.append((analysis.func, fact))
            self._facts = out
        return self._facts

    def facts_of(self, kind: str) -> List[dict]:
        return dedupe([f for _, f in self.all_facts() if f.get("kind") == kind])

    # ------------------------------------------------------------------
    # modules
    # ------------------------------------------------------------------

    def runtime(self, project_path: str) -> dict:
        if project_path not in self._runtimes:
            project = self.projects.get(project_path)
            if project is None:
                self._runtimes[project_path] = {}
            else:
                app = self._main_app(project_path)
                self._runtimes[project_path] = derive_infrastructure(
                    project.config, project.compose, (app.title if app else None), app.root_path if app else None)
        return self._runtimes[project_path]

    def _root_apps(self, project_path: str) -> List[RouterInfo]:
        """Applications of a project that are not mounted into another one."""
        apps = [r for r in self.app.apps() if r.module.project == project_path]
        roots = [r for r in apps if not r.parents]
        return roots or apps

    def _main_app(self, project_path: str) -> Optional[RouterInfo]:
        apps = self._root_apps(project_path)
        return apps[0] if apps else None

    def _modules(self) -> List[dict]:
        counts = defaultdict(lambda: defaultdict(int))
        for c in self.components.values():
            counts[c["module"]][c["stereotype"]] += 1
        endpoints_per_module = defaultdict(int)
        for e in self.endpoints:
            endpoints_per_module[e["module"]] += 1
        sources_per_module = defaultdict(int)
        for m in self.index.modules.values():
            sources_per_module[m.project] += 1

        out = []
        for path, project in sorted(self.projects.items()):
            apps = self._root_apps(path)
            if not sources_per_module.get(path) and not apps:
                continue
            runtime = self.runtime(path)
            deps = public_dependencies(project.dependencies)
            technologies = sorted({f"{d['category']}: {d['technology']}" for d in deps
                                   if d.get("category") and d["category"] not in IGNORED_CATEGORIES | {"runtime"}})
            fastapi = project.dependency("fastapi")
            main = apps[0] if apps else None
            module = {
                "name": project.name, "path": path, "artifact": project.name, "build_tool": project.build_tool, "version": project.version,
                "packaging": None, "language": "python", "python_version": project.python,
                "fastapi_version": fastapi["version"] if fastapi else None,
                "pydantic": "v2" if self.pydantic_v2 else "v1", "description": project.description,
                "dependencies": [d for d in deps if d.get("scope") != "test"],
                "technologies": technologies,
                "runtime": runtime,
                "config_files": project.config.files,
                "properties": masked(project.config.default),
                "profile_properties": {p: masked(v) for p, v in project.config.profiles.items()},
                "main_class": f"{main.module.name}:{main.var}" if main else None,
                "is_spring_boot_app": main is not None,           # kept for consumers written against the Spring model
                "is_application": main is not None,
                "type_counts": dict(counts.get(path, {})),
                "endpoint_count": endpoints_per_module.get(path, 0),
                "client_base_urls": self._client_base_urls(path),
            }
            if not module["type_counts"] and not module["config_files"] and not main and not module["endpoint_count"]:
                module["aggregator"] = True
            out.append(module)
        return out

    def _client_base_urls(self, project_path: str) -> List[dict]:
        out = []
        for module in self.index.modules.values():
            if module.project != project_path:
                continue
            for name, var in module.variables.items():
                tag = self.a.var_type(name, module)
                base = getattr(tag, "get", lambda *_: None)("base_url") if tag is not None and getattr(tag, "family", "") == "http" else None
                if base:
                    out.append({"base_url": self.a.resolve_placeholders(module, base), "bean": name, "client": tag.tech,
                                "defined_in": f"{module.short}.{name}"})
        return out

    # ------------------------------------------------------------------
    # components
    # ------------------------------------------------------------------

    def _stereotype(self, name: str, rel: str, kinds: set, listener: bool, scheduled: bool, kind: str) -> str:
        if listener:
            return "listener"
        if scheduled:
            return "scheduler"
        parts = [p.lower() for p in rel[:-3].split("/")]
        if kind == "class":
            for stereotype, pattern in NAME_HINTS:
                if pattern.search(name):
                    if stereotype == "repository" or stereotype == "feign_client" and "http" not in kinds and "database" in kinds:
                        return "repository" if "database" in kinds or stereotype == "repository" else "service"
                    if stereotype == "feign_client" and "http" not in kinds and not any(p in PATH_HINTS[1][1] for p in parts):
                        continue
                    return stereotype
        else:
            name_parts = set(parts)
            if parts[-1] in PLAIN_PATHS:
                return "component"
            for stereotype, words in PATH_HINTS:
                if name_parts & set(words):
                    if stereotype == "configuration" and kinds:
                        continue
                    return stereotype
        if "database" in kinds and not kinds & {"http", "messaging"}:
            return "repository"
        if "http" in kinds and "database" not in kinds:
            return "feign_client"
        if "messaging" in kinds and "database" not in kinds:
            return "service"
        return "component"

    def _component_name_for_router(self, router: RouterInfo) -> str:
        var = router.var.replace("self.", "")
        scope = f"{router.scope.name}." if router.scope is not None and router.scope.owner is None else ""
        return f"{router.module.name}.{scope}{var}"

    def _collect_components(self):
        handler_router: Dict[str, RouterInfo] = {}
        for router in self.app.router_list:
            for reg in router.routes:
                handler_router.setdefault(reg.func.id, router)

        listener_funcs = {l["method_id"] for l in self.a.listeners if l.get("method_id")}
        scheduled_funcs = {j["method_id"] for j in self.a.scheduled if j.get("method_id")}
        used = set(self.ex.chain_funcs) | listener_funcs | scheduled_funcs
        fact_funcs = {f.id for f, _ in self.all_facts()}

        # applications and routers
        for router in self.app.router_list:
            if router.node is None and not router.routes:
                continue
            if router.kind == "router" and not router.routes and not router.includes:
                continue
            qn = self._component_name_for_router(router)
            stereotype = "application" if router.kind == "app" else "rest_controller"
            methods = []
            for reg in router.routes:
                methods.append({"name": reg.func.name, "line": reg.func.line_start, "params": [p.name for p in reg.func.params
                                                                                               if p.name not in ("self", "cls")],
                                "returns": self._returns(reg.func)})
            self.components[qn] = {
                "name": router.var.replace("self.", ""), "qualified_name": qn, "module": router.module.project, "file": router.module.rel,
                "line": router.line, "kind": "router" if router.kind == "router" else "application", "stereotype": stereotype,
                "layer": LAYERS[stereotype], "annotations": ["FastAPI" if router.kind == "app" else "APIRouter"],
                "description": (router.title or router.module.doc or "").strip().split("\n")[0] or None,
                "dependencies": [], "methods": methods[:100],
            }
            self.router_component[router.id] = qn
            for reg in router.routes:
                self.func_component.setdefault(reg.func.id, qn)

        # classes
        for module in self.index.modules.values():
            if self._is_test(module.rel):
                continue
            for cls in module.all_classes:
                if cls.outer is not None:
                    continue
                kind = self.registry.kind(cls)
                if kind in ("enum", "entity", "exception", "model"):
                    continue
                methods = [m for m in cls.methods.values() if not (m.name.startswith("__") and m.name.endswith("__"))]
                if kind == "settings":
                    self._add_class(cls, "configuration_properties", set(), methods)
                    continue
                if kind == "dataclass" and not methods:
                    continue
                if not methods and not cls.name.endswith(("Client", "Service", "Repository")):
                    continue
                funcs = list(cls.methods.values())
                kinds = {f.get("kind") for fn, f in self.all_facts() if fn.owner is cls}
                is_listener = any(fn.id in listener_funcs for fn in funcs)
                is_scheduled = any(fn.id in scheduled_funcs for fn in funcs)
                relevant = bool(kinds) or any(fn.id in used for fn in funcs) or is_listener or is_scheduled or any(
                    NAME_HINTS_MATCH(cls.name) for _ in [0])
                if not relevant:
                    continue
                stereotype = self._stereotype(cls.name, module.rel, kinds, is_listener, is_scheduled, "class")
                self._add_class(cls, stereotype, kinds, methods)

        # modules made of plain functions (crud.py, services.py, tasks.py, dependencies.py)
        for module in self.index.modules.values():
            if self._is_test(module.rel):
                continue
            funcs = [f for f in module.functions.values() if f.id not in handler_router and not f.nested]
            if not funcs:
                continue
            hits = [f for f in funcs if f.id in used or f.id in fact_funcs or f.id in listener_funcs or f.id in scheduled_funcs]
            if not hits:
                continue
            kinds = {f.get("kind") for fn, f in self.all_facts() if fn.module is module and fn.owner is None}
            is_listener = any(f.id in listener_funcs for f in funcs)
            is_scheduled = any(f.id in scheduled_funcs for f in funcs)
            stereotype = self._stereotype(module.short, module.rel, kinds, is_listener, is_scheduled, "module")
            qn = module.name
            self.components[qn] = {
                "name": module.short, "qualified_name": qn, "module": module.project, "file": module.rel, "line": 1, "kind": "module",
                "stereotype": stereotype, "layer": LAYERS.get(stereotype, "service"), "annotations": [],
                "description": (module.doc or "").strip().split("\n")[0] or None, "dependencies": [],
                "methods": [{"name": f.name, "line": f.line_start, "params": [p.name for p in f.params if p.name not in ("self", "cls")],
                             "returns": self._returns(f)} for f in funcs if not f.name.startswith("_")][:100],
            }
            for f in funcs:
                self.func_component[f.id] = qn

        self._fill_component_details(handler_router)

    @staticmethod
    def _is_test(rel: str) -> bool:
        parts = rel.split("/")
        return parts[-1].startswith("test_") or parts[-1].endswith("_test.py") or parts[-1] == "conftest.py" or any(
            p in ("tests", "test") for p in parts[:-1])

    def _returns(self, func: FuncInfo) -> Optional[str]:
        if func.returns is None:
            return None
        return self.registry.types.render(self.registry.types.parse(func.returns, func.module))

    def _add_class(self, cls: ClassInfo, stereotype: str, kinds: set, methods: List[FuncInfo]):
        qn = cls.id
        internal, external = self.index.class_bases(cls)
        self.components[qn] = {
            "name": cls.name, "qualified_name": qn, "module": cls.module.project, "file": cls.module.rel, "line": cls.line_start,
            "kind": "class", "stereotype": stereotype, "layer": LAYERS.get(stereotype, "service"),
            "annotations": [d.name.rsplit(".", 1)[-1] for d in cls.decorators], "description": (cls.doc or "").strip().split("\n")[0] or None,
            "extends": [b.name for b in internal] + [e.rsplit(".", 1)[-1] for e in external] or None,
            "dependencies": [],
            "methods": [{"name": m.name, "line": m.line_start, "params": [p.name for p in m.params if p.name not in ("self", "cls")],
                         "returns": self._returns(m)} for m in methods if not m.name.startswith("_")][:100],
        }
        for m in cls.methods.values():
            self.func_component[m.id] = qn

    def _fill_component_details(self, handler_router):
        integrations: Dict[str, set] = defaultdict(set)
        for fn, fact in self.all_facts():
            qn = self.func_component.get(fn.id)
            if qn and fact.get("kind") not in ("transaction",):
                integrations[qn].add(f"{fact['kind']}:{fact.get('technology') or fact.get('client')}")
        for qn, comp in self.components.items():
            if integrations.get(qn):
                comp["integrations"] = sorted(integrations[qn])

        # injection: constructor parameters / class fields, and Depends() of routes
        for qn, comp in self.components.items():
            if comp["kind"] != "class":
                continue
            cls = next((c for m in self.index.modules.values() for c in m.all_classes if c.id == qn), None)
            if cls is not None:
                comp["dependencies"] = self._class_dependencies(cls)

        by_router: Dict[str, List[dict]] = defaultdict(list)
        for key, deps in self.ex.dep_nodes.items():
            func_id = key.split("|")[0]
            router = handler_router.get(func_id)
            if router is None:
                continue
            owner_qn = self.router_component.get(router.id)
            for dep in deps:
                target = self._dependency_target(dep)
                if target is None or owner_qn is None:
                    continue
                entry = {"field": dep.label or "dependency", "type": dep.label, "injection": f"depends:{dep.level}"}
                entry.update(target)
                by_router[owner_qn].append(entry)
        for qn, deps in by_router.items():
            self.components[qn]["dependencies"] = dedupe(deps)

        # applications depend on the routers they include
        for router in self.app.router_list:
            owner = self.router_component.get(router.id)
            if owner is None:
                continue
            for inc in router.includes:
                child = self.router_component.get(inc.child.id)
                if child:
                    self.components[owner]["dependencies"].append(
                        {"field": inc.child.var, "type": "APIRouter", "injection": inc.kind, "target": child,
                         "target_stereotype": "rest_controller", "prefix": inc.prefix or None})
            self.components[owner]["dependencies"] = dedupe(self.components[owner]["dependencies"])

    def _dependency_target(self, dep) -> Optional[dict]:
        if dep.cls is not None:
            qn = dep.cls.id
        elif dep.func is not None:
            qn = self.func_component.get(dep.func.id)
        else:
            return None
        if not qn or qn not in self.components:
            return None
        return {"target": qn, "target_stereotype": self.components[qn]["stereotype"]}

    def _class_dependencies(self, cls: ClassInfo) -> List[dict]:
        deps = []
        for name in ("__init__",):
            init = cls.methods.get(name)
            if init is None:
                continue
            for p in init.params:
                if p.name in ("self", "cls") or p.annotation is None or p.kind != "normal":
                    continue
                te = self.registry.types.parse(p.annotation, cls.module)
                deps.append(self._dependency_entry(p.name, te, "constructor"))
        for f in cls.fields:
            if f.annotation is not None and f.default is None:
                te = self.registry.types.parse(f.annotation, cls.module)
                entry = self._dependency_entry(f.name, te, "field")
                if entry.get("target") or entry.get("client"):
                    deps.append(entry)
        return dedupe([d for d in deps if d.get("target") or d.get("client")])

    def _dependency_entry(self, name: str, te, injection: str) -> dict:
        entry = {"field": name, "type": self.registry.types.render(te), "injection": injection}
        inner = te.unwrap_optional()
        if inner.kind == "class" and inner.cls is not None:
            qn = inner.cls.id
            if qn in self.components:
                entry["target"] = qn
                entry["target_stereotype"] = self.components[qn]["stereotype"]
                impls = [s.id for s in self.index.subclasses(inner.cls) if s.id in self.components]
                if impls:
                    entry["implementations"] = impls
        else:
            tag = self.a.te_type(inner)
            if tag is not None and getattr(tag, "family", None):
                entry["client"] = tag.tech
        return entry

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def _entities(self) -> List[dict]:
        out = []
        for module in self.index.modules.values():
            if self._is_test(module.rel):
                continue
            for cls in module.all_classes:
                if cls.outer is None and self.registry.kind(cls) == "entity":
                    info = self.registry.entity_info(cls)
                    if info:
                        out.append(info)
        return out

    def _repositories(self) -> List[dict]:
        out = []
        entities_by_name = {e["name"]: e for e in self._entities()}
        for qn, comp in sorted(self.components.items()):
            if comp["stereotype"] != "repository":
                continue
            facts = [f for fn, f in self.all_facts() if self.func_component.get(fn.id) == qn and f.get("kind") == "database"]
            entity_names = []
            for f in facts:
                for name in (f.get("entities") or ([f["entity"]] if f.get("entity") else [])):
                    if name not in entity_names:
                        entity_names.append(name)
            for binding_cls in self._bound_entities(qn):
                if binding_cls.name not in entity_names:
                    entity_names.append(binding_cls.name)
            entity = entities_by_name.get(entity_names[0]) if entity_names else None
            queries = []
            for f in facts:
                if f.get("query"):
                    queries.append({"method": f.get("source", "").split(".")[-1], "native": True, "operation": f.get("operation"),
                                    "query": f["query"], "tables": f.get("table") or []})
            stores = sorted({f.get("technology") for f in facts if f.get("technology")})
            out.append({
                "name": comp["name"], "qualified_name": qn, "module": comp["module"], "file": comp["file"],
                "base": next((b for b in (comp.get("extends") or []) if b not in ("Generic", "ABC", "object", "Protocol")), None), "entity": entity["name"] if entity else (entity_names[0] if entity_names else None),
                "entity_qualified_name": entity["qualified_name"] if entity else None,
                "store": entity["store"] if entity else (stores[0] if stores else None), "custom_queries": dedupe(queries),
            })
        return out

    def _bound_entities(self, qn: str) -> List[ClassInfo]:
        found = []
        for inst in self.a._instances.values():
            if inst.cls.id == qn or qn in [c.id for c in self.index.mro(inst.cls)]:
                if inst.bind:
                    for target in inst.bind.attrs.values():
                        if self.registry.is_entity(target) and target not in found:
                            found.append(target)
        return found

    def _tables(self) -> List[dict]:
        tables: Dict[str, dict] = {}

        def entry(name, store):
            return tables.setdefault(name, {"name": name, "store": store, "entities": [], "operations": set(), "accessed_by": set(),
                                            "modules": set()})

        for e in self._entities():
            t = entry(e["table"], e["store"])
            t["entities"].append(e["name"])
            t["modules"].add(e["module"])
        for fn, f in self.all_facts():
            if f.get("kind") != "database":
                continue
            for table in f.get("table") or []:
                t = entry(table, f.get("technology"))
                if f.get("operation"):
                    t["operations"].add(f["operation"])
                t["accessed_by"].add(f.get("source"))
                t["modules"].add(f.get("module"))
        return [
            {**v, "operations": sorted(v["operations"]), "accessed_by": sorted(filter(None, v["accessed_by"])),
             "modules": sorted(m for m in v["modules"] if m is not None)}
            for _, v in sorted(tables.items())
        ]

    # ------------------------------------------------------------------
    # integrations / security / exceptions
    # ------------------------------------------------------------------

    def _integrations(self) -> dict:
        producers = self.facts_of("messaging")
        consumers = []
        for l in dedupe(self.a.listeners):
            consumers.append(dict(l))

        topics: Dict[tuple, dict] = {}
        for p in producers:
            key = (p.get("technology"), p.get("destination"))
            topics.setdefault(key, {"technology": key[0], "destination": key[1], "producers": [], "consumers": []})
            topics[key]["producers"].append(p.get("source"))
        for c in consumers:
            for dest in c.get("destinations") or [None]:
                key = (c.get("technology"), dest)
                topics.setdefault(key, {"technology": key[0], "destination": dest, "producers": [], "consumers": []})
                topics[key]["consumers"].append(c.get("handler"))

        return {
            "databases": self.facts_of("database"),
            "caches": self.facts_of("cache"),
            "messaging": {
                "producers": producers, "consumers": consumers,
                "destinations": sorted(
                    ({**v, "producers": sorted(set(filter(None, v["producers"]))), "consumers": sorted(set(filter(None, v["consumers"])))}
                     for v in topics.values()),
                    key=lambda v: (str(v["technology"]), str(v["destination"]))),
            },
            "external_apis": self.facts_of("http"),
            "feign_clients": [],
            "events": {"published": [], "listeners": []},
            "scheduled_jobs": dedupe(self.a.scheduled),
            "email": self.facts_of("email"),
            "storage": self.facts_of("storage"),
            "search": self.facts_of("search"),
            "resilience": self.facts_of("resilience"),
            "async": self.facts_of("async"),
            "background_work": self.facts_of("async"),
        }

    def _security(self) -> dict:
        technologies, mechanisms = set(), set()
        for project in self.projects.values():
            for d in project.dependencies:
                if d.get("category") == "security":
                    technologies.add(d["technology"])
        rules: Dict[tuple, dict] = {}
        method_level = 0
        for e in self.endpoints:
            security = e.get("security")
            if not security:
                continue
            method_level += 1
            for annotation in security["annotations"]:
                if annotation.get("scheme"):
                    mechanisms.add(annotation["scheme"])
            rule = security.get("url_rule") or {}
            key = (rule.get("defined_in"), rule.get("access"), tuple(rule.get("roles") or []), e["module"])
            entry = rules.setdefault(key, {"access": rule.get("access"), "defined_in": rule.get("defined_in"), "http_method": None,
                                           "module": e["module"], "patterns": [], "roles": rule.get("roles") or []})
            if e["path"] not in entry["patterns"] and len(entry["patterns"]) < 40:
                entry["patterns"].append(e["path"])
        names = {d["artifact"] for p in self.projects.values() for d in p.dependencies}
        if names & {"python-jose", "pyjwt", "authlib", "fastapi-jwt-auth", "authx"}:
            mechanisms.add("jwt")
        if "passlib" in names or "bcrypt" in names:
            mechanisms.add("password-hashing")
        middleware = []
        for mw in self.app.middleware:
            middleware.append({"name": mw.name, "kind": mw.kind, "options": mw.options, "file": mw.module.rel, "line": mw.line})
        return {
            "enabled": bool(rules or mechanisms or technologies),
            "technologies": sorted(technologies),
            "mechanisms": sorted(mechanisms),
            "csrf_disabled": False,
            "url_rules": sorted(rules.values(), key=lambda r: (str(r["defined_in"]), r["patterns"][0] if r["patterns"] else "")),
            "configured_in": sorted({r["defined_in"] for r in rules.values() if r["defined_in"]}),
            "endpoints_with_method_security": method_level,
            "middleware": middleware,
            "cors": next((m["options"] for m in middleware if m["kind"] == "cors"), None),
        }

    def _exception_handling(self) -> List[dict]:
        out = []
        for key, handler in self.a.exception_handlers.items():
            name = key.rsplit(".", 1)[-1]
            if key.startswith("status:"):
                name = f"HTTP {key.split(':', 1)[1]}"
            out.append({"exception": name, "status": handler.get("status"), "handler": handler.get("handler"),
                        "handler_id": handler.get("handler_id"), "source": handler.get("source"), "global": True, "reason": None})
        handled = {o["exception"] for o in out}
        for module in self.index.modules.values():
            for cls in module.all_classes:
                if cls.outer is None and self.registry.kind(cls) == "exception" and not self._is_test(module.rel):
                    status = self.a.exception_status(cls)
                    if status and cls.name not in handled:
                        out.append({"exception": cls.name, "status": status, "handler": None, "source": "HTTPException subclass",
                                    "reason": None})
        return sorted(out, key=lambda x: x["exception"])

    # ------------------------------------------------------------------
    # endpoint summary
    # ------------------------------------------------------------------

    def _endpoint_summary(self, e: dict) -> dict:
        integrations = e.get("integrations") or {}
        return {
            "method": e["method"], "path": e["path"], "handler": e["handler"], "doc_name": e.get("doc_name"), "module": e["module"],
            "summary": e.get("summary") or (e.get("description") or "")[:200] or None,
            "request_body": (e.get("request_body") or {}).get("type"),
            "response": (e.get("response") or {}).get("body_type"),
            "status_codes": sorted({s["code"] for s in e.get("status_codes", [])}),
            "secured": bool(e.get("security")),
            "tables": sorted({t for f in integrations.get("databases", []) for t in (f.get("table") or [])}),
            "caches": sorted({c for f in integrations.get("caches", []) for c in (f.get("cache_names") or [f.get("key") or f.get("technology")]) if c}),
            "publishes": sorted({str(f.get("destination")) for f in integrations.get("messaging", []) if f.get("direction") == "publish"}),
            "calls_external": sorted({f.get("target_service") or f.get("host") or f.get("url") or "?" for f in integrations.get("external_apis", [])}),
        }

    # ------------------------------------------------------------------
    # summary
    # ------------------------------------------------------------------

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
        middleware = sorted({m.kind for m in self.app.middleware})
        return {
            "modules": len([m for m in modules if not m.get("aggregator")]),
            "spring_boot_apps": names,
            "applications": names,
            "frameworks": ["fastapi"],
            "types": sum(len(m.all_classes) for m in self.index.modules.values()),
            "components": dict(stereotypes),
            "endpoints": len(self.endpoints),
            "entities": len(self._entities()),
            "repositories": stereotypes.get("repository", 0),
            "feign_clients": stereotypes.get("feign_client", 0),
            "message_listeners": len([l for l in dedupe(self.a.listeners)]),
            "scheduled_jobs": len(dedupe(self.a.scheduled)),
            "technologies": {k: sorted(v) for k, v in sorted(technologies.items())},
            "middleware": middleware,
            "pydantic": "v2" if self.pydantic_v2 else "v1",
            "architecture_style": "microservices" if len(apps) > 1 else "monolith",
        }

    # ------------------------------------------------------------------
    # graph
    # ------------------------------------------------------------------

    def _component_of(self, func: FuncInfo) -> Optional[str]:
        return self.func_component.get(func.id)

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
            key = (src, dst, kind)
            e = edges.setdefault(key, {"from": src, "to": dst, "kind": kind, "details": []})
            if detail and detail not in e["details"] and len(e["details"]) < 50:
                e["details"].append(detail)

        for m in modules:
            if m.get("aggregator"):
                continue
            node(f"module:{m['path']}", type="module", name=module_names[m["path"]], path=m["path"], spring_boot_app=m["is_application"],
                 port=m["runtime"].get("server_port"))

        component_ids = {}
        for c in components:
            cid = node(f"component:{c['qualified_name']}", type="component", name=c["name"], stereotype=c["stereotype"], layer=c["layer"],
                       module=c["module"])
            component_ids[c["qualified_name"]] = cid
            edge(f"module:{c['module']}", cid, "contains")
            for dep in c.get("dependencies", []):
                for target in dep.get("implementations") or [dep.get("target")]:
                    if target and target in self.components:
                        edge(cid, f"component:{target}", "injects", dep["field"])

        # component -> component calls
        for analysis in self.a._analyses.values():
            src = self._component_of(analysis.func)
            if src not in component_ids:
                continue
            for e in analysis.calls:
                dst = self._component_of(e.callee)
                if dst in component_ids:
                    edge(component_ids[src], component_ids[dst], "calls", self.a.label(e.callee))

        configured_store = {}
        for m in modules:
            mid = f"module:{m['path']}"
            runtime = m["runtime"]
            for ds in runtime.get("datasources", []):
                nid = node(f"datastore:{ds.get('type')}:{ds.get('host', 'embedded')}/{ds.get('database', '')}", type="datastore",
                           technology=ds.get("type"), host=ds.get("host"), database=ds.get("database"))
                edge(mid, nid, "connects", ds.get("profile"))
                if ds.get("profile") == "default" or len(runtime["datasources"]) == 1:
                    configured_store[(m["path"], "relational")] = nid
            if runtime.get("mongodb"):
                mongo = runtime["mongodb"]
                nid = node(f"datastore:mongodb:{mongo.get('host', 'default')}/{mongo.get('database', '')}", type="datastore",
                           technology="mongodb", host=mongo.get("host"), database=mongo.get("database"))
                edge(mid, nid, "connects")
                configured_store[(m["path"], "mongodb")] = nid
            if runtime.get("redis"):
                nid = node(f"cache:redis:{runtime['redis'].get('host', 'default')}", type="cache", technology="redis",
                           host=runtime["redis"].get("host"))
                edge(mid, nid, "connects")
            for broker in ("kafka", "rabbitmq", "nats"):
                if runtime.get(broker):
                    nid = node(f"broker:{broker}", type="broker", technology=broker)
                    edge(mid, nid, "connects")
            if runtime.get("elasticsearch"):
                nid = node("search:elasticsearch", type="search", technology="elasticsearch", host=runtime["elasticsearch"].get("host"))
                edge(mid, nid, "connects")
            if runtime.get("identity_provider"):
                nid = node("infra:identity-provider", type="infrastructure", technology="oauth2", url=runtime["identity_provider"])
                edge(mid, nid, "authenticates")
            self._compose_infra(m, mid, runtime, node, edge)

        # facts -> external systems
        for func, f in self.all_facts():
            module_path = func.module.project
            src = component_ids.get(self._component_of(func) or "") or f"module:{module_path}"
            kind = f.get("kind")
            if kind == "database":
                tech = f.get("technology")
                family = "mongodb" if tech in DOCUMENT else "relational" if tech in RELATIONAL else tech
                profiles = [f"{d.get('profile')}: {d.get('type')}" for d in self.runtime(module_path).get("datasources", [])] or None
                nid = configured_store.get((module_path, family)) or node(
                    f"store:{module_path}:{tech}", type="datastore", technology=tech, module=module_path, profiles=profiles)
                for table in f.get("table") or [None]:
                    edge(src, nid, str(f.get("operation", "uses")).lower(), table)
            elif kind == "cache":
                tech = f.get("technology")
                nid = node(f"cache:{module_path}:{tech}", type="cache", technology=tech, module=module_path)
                edge(src, nid, str(f.get("operation", "uses")).lower(), ", ".join(f.get("cache_names") or []) or f.get("key"))
            elif kind == "messaging":
                nid = node(f"broker:{f.get('technology')}", type="broker", technology=f.get("technology"))
                edge(src, nid, "publishes", f.get("destination"))
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
            func_id = listener.get("method_id")
            src = component_ids.get(self.func_component.get(func_id, "")) if func_id else None
            nid = node(f"broker:{listener['technology']}", type="broker", technology=listener["technology"])
            for dest in listener.get("destinations") or [None]:
                edge(nid, src or f"module:{listener['module']}", "delivers", dest)

        for job in dedupe(self.a.scheduled):
            src = component_ids.get(self.func_component.get(job.get("method_id") or "", ""))
            if src:
                nid = node("infra:scheduler", type="infrastructure", technology=job.get("technology") or "scheduler")
                edge(nid, src, "triggers", job["handler"])

        # module -> module edges derived from component edges
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
        return {
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(all_edges, key=lambda e: (e["from"], e["to"], e["kind"])),
        }

    def _compose_infra(self, module, module_id, runtime, node, edge):
        """Infrastructure only known from docker-compose `depends_on` (no URL in the configuration)."""
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
            if not infra:
                continue
            tech, kind = infra["technology"], infra["kind"]
            if has.get(tech):
                continue
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


def NAME_HINTS_MATCH(name: str) -> bool:
    return any(pattern.search(name) for _, pattern in NAME_HINTS)
