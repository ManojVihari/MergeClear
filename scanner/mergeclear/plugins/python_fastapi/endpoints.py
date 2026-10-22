"""
REST endpoint extraction: URL, parameters (including those of every dependency), request and response
schemas, status codes, security and everything an endpoint reaches downstream.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .analyzer import Analyzer, Binding, MAX_LIST, dedupe, walk_body
from .appmodel import AppModel, Placement, Registration
from .catalog import (
    AUTH_NAME_HINT, DEPENDS_FUNCTIONS, FORM_DEPENDENCIES, INJECTED_TYPES, PARAM_FUNCTIONS, RESPONSE_CLASSES, ROLE_NAME_HINT, SECURITY_SCHEMES, STATUS_NAME,
)
from .pyindex import (
    ClassInfo, FuncInfo, ModuleInfo, call_parts, dotted, keyword_args, positional_args, string_value, strip_type, text, walk,
)
from .schemas import SchemaBuilder
from .typeexpr import Meta, TypeExpr

MAX_DEPENDENCY_DEPTH = 6
PATH_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")
AUTH_INPUT_NAME = re.compile(r"(token|authorization|api[-_]?key|session|cookie|jwt|bearer|credential)", re.I)
AUTH_BODY_HINT = re.compile(r"\b(jwt|decode|verify_token|verify_password|authenticate|has_permission|check_permission|get_current_user)\b")


@dataclass(eq=False)
class DepNode:
    kind: str                           # function | class | scheme | external | unknown
    label: str
    level: str
    node: object = None                 # the Depends(...) call
    module: Optional[ModuleInfo] = None
    func: Optional[FuncInfo] = None     # function to run
    extra_funcs: List[FuncInfo] = field(default_factory=list)   # factory / __init__ that also run
    cls: Optional[ClassInfo] = None
    scheme: Optional[str] = None
    scheme_detail: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    literals: List[str] = field(default_factory=list)
    params: List[dict] = field(default_factory=list)
    children: List["DepNode"] = field(default_factory=list)
    bind: Optional[Binding] = None

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class EndpointExtractor:

    def __init__(self, analyzer: Analyzer, appmodel: AppModel, schemas: SchemaBuilder,
                 resolve_service: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]] = None):
        self.a = analyzer
        self.app = appmodel
        self.schemas = schemas
        self.registry = schemas.registry
        self.index = analyzer.index
        self.const = analyzer.const
        self.types = schemas.types
        self.resolve_service = resolve_service or (lambda host, service: None)
        self.dep_nodes: Dict[str, List[DepNode]] = {}          # "func id|method|path" -> dependencies of that endpoint
        self.chain_funcs: Dict[str, FuncInfo] = {}             # every function some endpoint reaches
        self.chains: Dict[str, Set[str]] = {}                  # endpoint key -> ids of the functions it reaches
        self.router_ranges: Dict[str, List[Tuple[str, int, int]]] = {}   # endpoint key -> (module, first line, last line) of its routers / includes

    # ------------------------------------------------------------------
    def extract_all(self) -> List[dict]:
        endpoints = []
        for reg in self.app.registrations:
            if reg.func.module.rel and self._is_test(reg.func.module.rel):
                continue
            endpoints.extend(self.extract_registration(reg))

        # docs are stored per handler; overloaded handlers and handlers with several methods get a stable name
        counts = Counter((e["module"], e["handler"]) for e in endpoints)
        for e in endpoints:
            if counts[(e["module"], e["handler"])] > 1:
                slug = re.sub(r"[^A-Za-z0-9]+", "_", e["path"]).strip("_") or "root"
                e["doc_name"] = f"{e['handler']}-{e['method']}-{slug}"
            else:
                e["doc_name"] = e["handler"]
        endpoints.sort(key=lambda e: (e["file"], e["line"], e["method"]))
        return endpoints

    @staticmethod
    def handler_id(endpoint: dict) -> str:
        """Key of an endpoint in chains / dep_nodes / router_modules."""
        qualname = endpoint["handler"].split(".", 1)[1]
        return f"{endpoint['controller']}.{qualname}|{endpoint['method']}|{endpoint['path']}"

    @staticmethod
    def _is_test(rel: str) -> bool:
        parts = rel.split("/")
        return parts[-1].startswith("test_") or parts[-1].endswith("_test.py") or any(p in ("tests", "test") for p in parts[:-1])

    def extract_registration(self, reg: Registration) -> List[dict]:
        placements = self.app.placements.get(reg) or [Placement(reg.router.prefix if reg.router else "", None, [], [], [])]
        paths, seen = [], set()
        for placement in placements:
            full = self._join(placement.prefix, reg.path)
            if full not in seen:
                seen.add(full)
                paths.append(full)
        primary = placements[0]
        return [self.build_endpoint(reg, method, paths, primary) for method in reg.methods]

    @staticmethod
    def _join(*parts) -> str:
        joined = "".join(p for p in parts if p)
        joined = re.sub(r"/{2,}", "/", joined)
        if not joined.startswith("/"):
            joined = "/" + joined
        return joined

    # ------------------------------------------------------------------
    # endpoint
    # ------------------------------------------------------------------

    def build_endpoint(self, reg: Registration, method: str, paths: List[str], placement: Placement) -> dict:
        func = reg.func
        module = func.module
        referenced: Set[str] = set()
        kwargs = reg.kwargs
        path_params = {m.group(1) for p in paths[:1] for m in PATH_PARAM.finditer(p)}
        websocket = reg.kind == "websocket"

        # ---- dependencies: router / app / include / decorator / class level, then the signature ----
        dependencies: List[DepNode] = []
        seen: Set[str] = set()
        for level, meta in placement.dependencies:
            dependencies.append(self.resolve_dependency(meta.node, meta.module, level, None, seen, 0))
        decorator_deps = [Meta(c, reg.module) for c in self._list_nodes(kwargs.get("dependencies"))]
        for meta in decorator_deps:
            dependencies.append(self.resolve_dependency(meta.node, meta.module, "route", None, seen, 0))
        if func.owner is not None and any(d.name.rsplit(".", 1)[-1] == "cbv" for d in func.owner.decorators):
            for f in func.owner.fields:
                if f.default is not None and (dotted(call_parts(f.default)[0]) or "").rsplit(".", 1)[-1] in DEPENDS_FUNCTIONS:
                    dependencies.append(self.resolve_dependency(f.default, func.owner.module, "class", None, seen, 0))

        own_params, signature_deps = self.signature(func, path_params, referenced, seen, 0)
        dependencies.extend(signature_deps)

        params: List[dict] = []
        seen_keys: Set[Tuple[str, str]] = set()

        def add(entry):
            key = (entry["in"], entry["name"])
            if key not in seen_keys:
                seen_keys.add(key)
                params.append(entry)

        for entry in own_params:
            add(entry)
        for dep in dependencies:
            for node in dep.walk():
                for entry in node.params:
                    add({**entry, "via": node.label})

        # path parameters that the signature never mentions are still part of the contract
        for name in sorted(path_params):
            if ("path", name) not in seen_keys and not any(p["name"] == name for p in params if p["in"] == "path"):
                add({"name": name, "type": "str", "in": "path", "required": True, "default": None})

        body_params = [p for p in params if p["in"] == "body"]
        request_body = self._request_body(body_params)
        response = self.response(reg, func, referenced)

        # ---- call chain ----
        extra = self._dependency_functions(dependencies)
        reachable = self.a.reachable(func, None, extra)
        chain = [(func, None)] + [r for r in reachable]
        for f, b in extra:
            if (f, b) not in chain:
                chain.append((f, b))
        chain = chain[:MAX_LIST * 2]

        key = f"{func.id}|{method}|{paths[0]}"
        self.dep_nodes[key] = dependencies
        self.chains[key] = {f.id for f, _ in chain}
        ranges = [(r.module.name, r.line, r.end_line) for r in placement.chain if r.node is not None]
        ranges += [(inc.module.name, inc.line, inc.end_line) for inc in placement.includes]
        self.router_ranges[key] = ranges
        for f, _ in chain:
            self.chain_funcs.setdefault(f.id, f)

        integrations = self._integrations(chain)
        exceptions = self._exceptions(chain)
        security = self._security(dependencies, kwargs, module, method, paths[0])
        documented = self._documented_responses(kwargs, reg.module, referenced, placement.responses)
        status_codes = self._status_codes(reg, func, chain, exceptions, params, security, documented)

        errors = sorted({STATUS_NAME.get(s["code"], str(s["code"])) for s in status_codes if s["code"] >= 400})
        errors = list(errors) + self._validation_errors(params)

        direct_edges = [(e.callee, e.bind) for e in self.a.analysis(func).calls]
        direct_nodes = [(d.func, d.bind) for d in dependencies if d.func is not None] + direct_edges
        direct = list(dict.fromkeys(self.a.label(f) for f, _ in direct_nodes))
        full = list(dict.fromkeys(self.a.label(f) for f, _ in chain[1:]))[:MAX_LIST]
        handler_label = func.label
        tree = {handler_label: {**{self.a.label(d.func): self.a.call_tree(d.func, d.bind, 1) for d in dependencies if d.func},
                                **self.a.call_tree(func, None)}}

        db_ops = [
            {"type": f.get("operation"), "call": f.get("call") or f.get("source"), "entity": f.get("entity"), "table": f.get("table"),
             "store": f.get("technology")}
            for f in integrations.get("databases", [])
        ]

        summary = self.const.string(kwargs["summary"], reg.module) if "summary" in kwargs else None
        description = self.const.string(kwargs["description"], reg.module) if "description" in kwargs else None
        doc = description or func.doc
        deprecated = bool(self.const.boolean(kwargs["deprecated"], reg.module)) if "deprecated" in kwargs else False
        decorator_tags = self.const.strings(kwargs["tags"], reg.module) if "tags" in kwargs else []
        tags = list(dict.fromkeys(placement.tags + decorator_tags))
        operation_id = self.const.string(kwargs["operation_id"], reg.module) if "operation_id" in kwargs else None
        include_in_schema = self.const.boolean(kwargs["include_in_schema"], reg.module) if "include_in_schema" in kwargs else None
        response_class = self._response_class(kwargs, reg.module)
        consumes = self._consumes(params)
        produces = [response_class[1]] if response_class and response_class[1] and response_class[0] != "JSONResponse" else []

        context_path = None
        if placement.app is not None and placement.app.root_path:
            context_path = placement.app.root_path

        return {
            # ---- core fields (same contract as the Spring scanner) ----
            "function": func.name,
            "method": method,
            "path": paths[0],
            "params": params,
            "errors": errors,
            "calls": sorted(set(self.a.analysis(func).names + [d.func.name for d in dependencies if d.func])),
            "db_ops": dedupe(db_ops),
            "response": response,
            "call_graph": {"direct": direct, "full": full, "tree": tree},
            "impact": full,
            # ---- location ----
            "handler": f"{module.short}.{func.qualname}",
            "controller": module.name,
            "module": module.project,
            "file": module.rel,
            "line": func.line_start,
            "paths": paths,
            "context_path": context_path,
            "summary": summary,
            "description": doc,
            "deprecated": deprecated,
            "framework": "fastapi",
            "tags": tags,
            "operation_id": operation_id,
            "protocol": "websocket" if websocket else "http",
            "include_in_schema": include_in_schema,
            # ---- contract ----
            "consumes": consumes,
            "produces": produces,
            "request_body": request_body,
            "status_codes": status_codes,
            "exceptions": exceptions,
            "security": security,
            "transactional": any(f.get("kind") == "transaction" for fn, b in chain for f in self.a.analysis(fn, b).facts) or None,
            "dependencies": [self._dependency_summary(d) for d in dependencies if d.kind != "unknown" or d.label],
            "responses": documented,
            # ---- downstream ----
            "integrations": integrations,
            "referenced_types": sorted(referenced),
        }

    @staticmethod
    def _list_nodes(node) -> list:
        node = strip_type(node)
        if node is None or node.type not in ("list", "tuple"):
            return []
        return [c for c in node.named_children if c.type == "call"]

    # ------------------------------------------------------------------
    # parameters
    # ------------------------------------------------------------------

    def _marker(self, p, te: TypeExpr, module: ModuleInfo):
        """(kind, call node, module) of Query()/Depends()/... given as default or inside Annotated."""
        candidates = []
        if p.default is not None:
            candidates.append((strip_type(p.default), module))
        candidates.extend((strip_type(m.node), m.module) for m in te.metas)
        for node, mod in candidates:
            if node is None or node.type != "call":
                continue
            func, _ = call_parts(node)
            name = (dotted(func) or "").rsplit(".", 1)[-1]
            if name in PARAM_FUNCTIONS or name in DEPENDS_FUNCTIONS:
                qualified = self.index.external_name(func, mod)
                if qualified is None or qualified.split(".")[0] in ("fastapi", "starlette", "fastapi_utils"):
                    return name, node, mod
        return None, None, None

    def signature(self, func: FuncInfo, path_params: Set[str], referenced: Set[str], seen: Set[str], depth: int) -> Tuple[List[dict], List[DepNode]]:
        """(parameters, dependencies) declared by a function signature."""
        params: List[dict] = []
        deps: List[DepNode] = []
        for p in func.params:
            if p.name in ("self", "cls") or p.kind != "normal":
                continue
            te = self.types.parse(p.annotation, func.module) if p.annotation is not None else TypeExpr("any", "Any", module=func.module)
            inner = te.unwrap_optional()
            if inner.qualified and INJECTED_TYPES.match(inner.qualified):
                continue
            if inner.kind == "class" and inner.cls is not None and self.registry.kind(inner.cls) == "settings":
                continue
            marker, marker_node, marker_module = self._marker(p, te, func.module)
            if marker in DEPENDS_FUNCTIONS:
                deps.append(self.resolve_dependency(marker_node, marker_module, "param", te, seen, depth, param_name=p.name))
                continue
            entry = self.parameter(p, te, marker, marker_node, marker_module, func, path_params, referenced)
            if entry is None:
                continue
            if isinstance(entry, list):
                params.extend(entry)
            else:
                params.append(entry)
        return params, deps

    def parameter(self, p, te: TypeExpr, marker: Optional[str], marker_node, marker_module, func: FuncInfo, path_params: Set[str],
                  referenced: Set[str]):
        module = func.module
        inner = te.unwrap_optional()
        marker_kwargs = keyword_args(call_parts(marker_node)[1]) if marker_node is not None else {}

        # ---- location ----
        location = {"Query": "query", "Path": "path", "Header": "header", "Cookie": "cookie", "Body": "body", "Form": "form",
                    "File": "multipart"}.get(marker or "")
        is_model = inner.kind == "class" and inner.cls is not None and self.registry.is_model_like(inner.cls)
        is_upload = inner.qualified.endswith("UploadFile") or inner.name == "UploadFile" or (
            inner.kind in ("list", "set") and inner.args and inner.args[0].name == "UploadFile")
        if location is None:
            if p.name in path_params:
                location = "path"
            elif is_upload:
                location = "multipart"
            elif is_model or inner.kind in ("dict", "list", "set", "tuple"):
                location = "body"                # a bare list[int] without Query() is a request body too
            elif inner.kind == "class" and inner.cls is not None and self.registry.kind(inner.cls) != "enum":
                location = "body"
            else:
                location = "query"

        # a Pydantic model declared with Query() / Header() / Form() is a group of parameters
        if marker in ("Query", "Header", "Cookie", "Form") and is_model:
            fields = self.schemas.schema_fields(inner, referenced)
            out = []
            for f, owner in self.registry.class_fields(inner.cls):
                name = self.registry.alias_of(f.default, self.types.parse(f.annotation, owner.module), owner.module) or f.name
                details = fields.get(name)
                if details is None:
                    continue
                entry = {"name": name, "type": details["type"], "in": location,
                         "required": bool((details.get("validation") or {}).get("required")),
                         "default": self.registry.default_of(f.default, owner.module)}
                validation = {k: v for k, v in (details.get("validation") or {}).items() if k != "required"}
                if validation:
                    entry["validation"] = validation
                out.append(entry)
            return out

        # ---- name ----
        name = p.name
        alias = marker_kwargs.get("alias")
        if alias is not None:
            name = self.const.string(alias, marker_module or module) or name
        elif location == "header" and marker_kwargs.get("convert_underscores") is None:
            name = name.replace("_", "-")
        elif location == "header" and self.const.boolean(marker_kwargs.get("convert_underscores"), module) is not False:
            name = name.replace("_", "-")

        required = True if location == "path" else self.registry.is_required(strip_type(p.default) if p.default is not None else None, te, module) \
            if p.default is not None else True
        if marker is not None and p.default is None and location != "path":
            required = not (te.kind == "optional" and not self.registry.pydantic_v2)
        if location == "path":
            required = True
        default_node = strip_type(p.default) if p.default is not None else None
        default = self.registry.default_of(default_node, module) if default_node is not None else None

        entry = {"name": name, "type": self.schemas.render(te), "in": location, "required": bool(required), "default": default}
        rules = {k: v for k, v in self.registry.type_rules(te).items() if k != "required"}
        if marker_kwargs:
            rules.update(self.registry.constraint_rules(marker_kwargs, marker_module or module, te))
        if rules:
            entry["validation"] = rules
        for key in ("description", "title"):
            if key in marker_kwargs:
                value = self.const.string(marker_kwargs[key], marker_module or module)
                if value and key == "description":
                    entry["description"] = value
        if "deprecated" in marker_kwargs and self.const.boolean(marker_kwargs["deprecated"], marker_module or module):
            entry["deprecated"] = True

        if location in ("body", "form") and not self.schemas.is_simple(inner):
            entry["validated"] = True
            fields = self.schemas.schema_fields(inner, referenced) if inner.kind == "class" else {}
            if fields:
                entry["schema"] = fields
            else:
                item = self.schemas.collection_item(inner)
                if item is not None:
                    entry["schema"] = {"[]": self.schemas.schema(inner, referenced)}
                elif inner.kind == "dict":
                    entry["schema"] = {"{}": self.schemas.schema(inner, referenced)}
        elif location in ("body",) and inner.kind in ("list", "set", "tuple", "dict"):
            entry["validated"] = True
        if location == "body" and marker_kwargs.get("embed") is not None and self.const.boolean(marker_kwargs["embed"], module):
            entry["embed"] = True
        if inner.kind == "class" and inner.cls is not None:
            referenced.add(inner.cls.id)
        return entry

    # ------------------------------------------------------------------
    def _request_body(self, body_params: List[dict]) -> Optional[dict]:
        if not body_params:
            return None
        if len(body_params) == 1 and not body_params[0].get("embed"):
            p = body_params[0]
            return {"type": p["type"], "required": p["required"], "validated": p.get("validated", False), "schema": p.get("schema")}
        fields = {}
        for p in body_params:
            field = {"type": p["type"]}
            if p.get("schema"):
                field["schema"] = {"type": "object", "fields": p["schema"]} if not set(p["schema"]) <= {"[]", "{}"} else list(p["schema"].values())[0]
            if p["required"]:
                field["validation"] = {"required": True}
            fields[p["name"]] = field
        return {"type": "object", "required": any(p["required"] for p in body_params), "validated": True, "schema": fields}

    @staticmethod
    def _consumes(params: List[dict]) -> List[str]:
        locations = {p["in"] for p in params}
        if "multipart" in locations:
            return ["multipart/form-data"]
        if "form" in locations:
            return ["application/x-www-form-urlencoded"]
        if "body" in locations:
            return ["application/json"]
        return []

    def _validation_errors(self, params):
        field_errors = []
        for p in params:
            if p.get("in") not in ("body", "form") or not p.get("validated"):
                continue
            for field_name, details in (p.get("schema") or {}).items():
                if not isinstance(details, dict):
                    continue
                for rule in (details.get("validation") or {}):
                    field_errors.append({"name": field_name, "rule": rule})
        if field_errors:
            return [{"type": "VALIDATION_ERROR", "status": 422, "fields": field_errors}]
        return []

    # ------------------------------------------------------------------
    # dependencies
    # ------------------------------------------------------------------

    def resolve_dependency(self, node, module: ModuleInfo, level: str, annotation: Optional[TypeExpr], seen: Set[str], depth: int,
                           param_name: Optional[str] = None) -> DepNode:
        node = strip_type(node)
        func_node, args = call_parts(node)
        pos, kwargs = positional_args(args), keyword_args(args)
        target = pos[0] if pos else kwargs.get("dependency")
        function_name = (dotted(func_node) or "Depends").rsplit(".", 1)[-1]
        scopes = self.const.strings(kwargs.get("scopes"), module) if function_name == "Security" and "scopes" in kwargs else []
        if function_name == "Security" and pos[1:]:
            scopes = scopes or self.const.strings(pos[1], module)

        dep = DepNode("unknown", "", level, node, module, scopes=scopes)

        if target is None:                                  # Depends(): the annotation is the dependency
            if annotation is not None and annotation.unwrap_optional().cls is not None:
                cls = annotation.unwrap_optional().cls
                return self._class_dependency(dep, cls, cls.name, None, seen, depth)
            inner = annotation.unwrap_optional() if annotation is not None else None
            if inner is not None and inner.name in FORM_DEPENDENCIES and (inner.qualified or "").startswith("fastapi"):
                dep.kind, dep.label = "external", inner.name
                dep.params = [{"name": n, "type": t, "in": "form", "required": r, "default": None} for n, t, r in FORM_DEPENDENCIES[inner.name]]
                return dep
            dep.label = (inner.name if inner is not None and inner.name else None) or param_name or ""
            return dep

        target = strip_type(target)
        label = self._clean(text(target))
        dep.label = label

        if target.type == "call":
            return self._call_dependency(dep, target, module, seen, depth)

        symbol = self.index.resolve_expr(target, module)
        if symbol.kind == "function" and not symbol.rest:
            dep.label = symbol.obj.name
            return self._function_dependency(dep, symbol.obj, seen, depth)
        if symbol.kind == "class" and not symbol.rest:
            return self._class_dependency(dep, symbol.obj, symbol.obj.name, None, seen, depth)
        if symbol.kind == "variable":
            value = strip_type(symbol.obj.value) if symbol.obj.value is not None else None
            if value is not None and value.type == "call":
                qualified = self.a.qualified_of(value, symbol.module) or ""
                scheme = SECURITY_SCHEMES.get(qualified.rsplit(".", 1)[-1])
                if scheme and qualified.split(".")[0] in ("fastapi", "starlette"):
                    dep.kind, dep.scheme, dep.label = "scheme", scheme, symbol.obj.name
                    dep.scheme_detail = qualified.rsplit(".", 1)[-1]
                    return dep
                held = self.a.var_type(symbol.obj.name, symbol.module)
                inst_cls = getattr(held, "cls", None)
                if inst_cls is not None:
                    return self._class_dependency(dep, inst_cls, symbol.obj.name, getattr(held, "bind", None), seen, depth, instance=True)
            dep.label = symbol.obj.name
            return dep
        if symbol.kind == "external":
            qualified = symbol.qualified or label
            scheme = SECURITY_SCHEMES.get(qualified.rsplit(".", 1)[-1])
            dep.kind = "scheme" if scheme else "external"
            dep.scheme, dep.scheme_detail = scheme, qualified.rsplit(".", 1)[-1] if scheme else None
            return dep
        return dep

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def _call_dependency(self, dep: DepNode, call, module: ModuleInfo, seen: Set[str], depth: int) -> DepNode:
        func_node, args = call_parts(call)
        literals = []
        for a in positional_args(args) + list(keyword_args(args).values()):
            value = self.const.value(a, module)
            if isinstance(value, str):
                literals.append(value)
            elif isinstance(value, list):
                literals.extend(v for v in value if isinstance(v, str))
        dep.literals = literals
        qualified = self.a.qualified_of(call, module) or ""
        scheme = SECURITY_SCHEMES.get(qualified.rsplit(".", 1)[-1])
        if scheme and qualified.split(".")[0] in ("fastapi", "starlette"):
            dep.kind, dep.scheme, dep.scheme_detail = "scheme", scheme, qualified.rsplit(".", 1)[-1]
            return dep
        symbol = self.index.resolve_expr(func_node, module)
        if symbol.kind == "class" and not symbol.rest:
            return self._class_dependency(dep, symbol.obj, self._clean(text(call)), None, seen, depth, instance=True)
        if symbol.kind == "function" and not symbol.rest:
            factory = symbol.obj
            inner = None
            for n in walk(factory.node):
                if n.type == "return_statement" and n.named_children and n.named_children[0].type == "identifier":
                    inner = self.index.nested_function(factory, text(n.named_children[0]))
                    if inner is not None:
                        break
            if inner is not None:
                dep.extra_funcs.append(factory)
                return self._function_dependency(dep, inner, seen, depth)
            return self._function_dependency(dep, factory, seen, depth)
        dep.kind = "external"
        return dep

    def _function_dependency(self, dep: DepNode, func: FuncInfo, seen: Set[str], depth: int) -> DepNode:
        dep.kind, dep.func = "function", func
        if depth >= MAX_DEPENDENCY_DEPTH or func.id in seen:
            return dep
        sub_seen = seen | {func.id}
        params, children = self.signature(func, set(), set(), sub_seen, depth + 1)
        dep.params = params
        dep.children = children
        # dependencies often need the security scheme through a parameter default: `token = Depends(oauth2_scheme)`
        return dep

    def _class_dependency(self, dep: DepNode, cls: ClassInfo, label: str, bind: Optional[Binding], seen: Set[str], depth: int,
                          instance: bool = False) -> DepNode:
        """
        A class as a dependency. FastAPI calls a class reference (`Depends(Pagination)`), so its `__init__` parameters
        are the request parameters; for an instance (`Depends(RoleChecker(["admin"]))`) they are those of `__call__`.
        """
        dep.kind, dep.cls, dep.bind = "class", cls, bind
        dep.label = label
        call_method = self.index.find_method(cls, "__call__")
        init = self.index.find_method(cls, "__init__")
        dep.func = call_method if instance and call_method is not None else (init or call_method)
        if instance and call_method is not None and init is not None:
            dep.extra_funcs.append(init)
        if depth >= MAX_DEPENDENCY_DEPTH or cls.id in seen:
            return dep
        sub_seen = seen | {cls.id}
        params, children = [], []
        for method in ((call_method,) if instance and call_method is not None else (init,) if init is not None else (call_method,)):
            if method is not None:
                p, c = self.signature(method, set(), set(), sub_seen, depth + 1)
                params.extend(p)
                children.extend(c)
        # a dependency class may be a pydantic model or a dataclass: its fields are parameters
        if not params and self.registry.is_model_like(cls) and not (init and init.params):
            for f, owner in self.registry.class_fields(cls):
                te = self.types.parse(f.annotation, owner.module)
                params.append({"name": f.name, "type": self.schemas.render(te), "in": "query",
                               "required": self.registry.is_required(f.default, te, owner.module), "default": None})
        dep.params, dep.children = params, children
        return dep

    @staticmethod
    def _dependency_functions(dependencies: List[DepNode]) -> List[Tuple[FuncInfo, Optional[Binding]]]:
        """Functions that run before the handler (dependencies at every level, factories, __init__), in order."""
        extra = []
        for dep in dependencies:
            for node in dep.walk():
                for f in [node.func] + node.extra_funcs:
                    if f is not None:
                        extra.append((f, node.bind if f is node.func else None))
        unique, seen = [], set()
        for f, b in extra:
            key = (f.id, b.key if b else None)
            if key not in seen:
                seen.add(key)
                unique.append((f, b))
        return unique

    @staticmethod
    def _dependency_summary(dep: DepNode) -> dict:
        out = {"name": dep.label, "level": dep.level, "kind": dep.kind}
        if dep.scheme:
            out["scheme"] = dep.scheme
        if dep.scopes:
            out["scopes"] = dep.scopes
        return out

    # ------------------------------------------------------------------
    # security
    # ------------------------------------------------------------------

    def _security(self, dependencies: List[DepNode], kwargs, module: ModuleInfo, method: str, path: str) -> Optional[dict]:
        annotations = []
        roles: List[str] = []
        defined_in = None
        for dep in dependencies:
            scheme = next((n for n in dep.walk() if n.kind == "scheme"), None)
            qualifies = scheme is not None
            if not qualifies and dep.kind in ("function", "class") and AUTH_NAME_HINT.search(dep.label):
                qualifies = any(AUTH_INPUT_NAME.search(p["name"]) and p["in"] in ("header", "cookie", "query") for n in dep.walk()
                                for p in n.params) or any(self._body_mentions_auth(n) for n in dep.walk())
            if not qualifies and dep.kind == "external" and AUTH_NAME_HINT.search(dep.label) and dep.label not in FORM_DEPENDENCIES:
                qualifies = True
            if not qualifies:
                continue
            level = {"app": "app", "include": "router", "router": "router", "route": "method", "param": "method", "class": "class"}.get(
                dep.level, "method")
            entry = {"annotation": "Security" if dep.scopes else "Depends", "expression": dep.label, "level": level}
            if scheme is not None:
                entry["scheme"] = scheme.scheme
            if dep.scopes:
                entry["scopes"] = dep.scopes
            annotations.append(entry)
            defined_in = defined_in or dep.label
            roles.extend(dep.scopes)
            if ROLE_NAME_HINT.search(dep.label) and dep.literals:
                roles.extend(dep.literals)
        if not annotations:
            return None
        roles = sorted(dict.fromkeys(roles))
        url_rule = {"access": "hasRole" if roles else "authenticated", "roles": roles, "defined_in": defined_in, "patterns": [path],
                    "http_method": method, "source": "dependency"}
        return {"annotations": dedupe(annotations), "url_rule": url_rule}

    @staticmethod
    def _body_mentions_auth(dep: DepNode) -> bool:
        if dep.func is None:
            return False
        return bool(AUTH_BODY_HINT.search(text(dep.func.node.child_by_field_name("body"))))

    # ------------------------------------------------------------------
    # response
    # ------------------------------------------------------------------

    def _response_class(self, kwargs, module):
        node = kwargs.get("response_class")
        if node is None:
            return None
        name = (dotted(node) or "").rsplit(".", 1)[-1]
        return name, RESPONSE_CLASSES.get(name)

    def response(self, reg: Registration, func: FuncInfo, referenced: Set[str]) -> dict:
        kwargs = reg.kwargs
        declared: Optional[TypeExpr] = None
        model_node = kwargs.get("response_model")
        disabled = model_node is not None and text(strip_type(model_node)) == "None"
        if model_node is not None and not disabled:
            declared = self.types.parse(model_node, reg.module)
        elif func.returns is not None and not disabled:
            declared = self.types.parse(func.returns, func.module)

        response_class = self._response_class(kwargs, reg.module)
        response: dict = {"type": None, "body_type": None, "schema": None}
        if reg.kind == "websocket":
            return response

        if declared is None:
            inferred = self._inferred_return(func)
            if inferred is None:
                if response_class and response_class[1] == "text/html":
                    response["view"] = True
                return response
            declared = inferred

        inner = declared.unwrap_optional()
        response["type"] = self.schemas.render(declared)
        response["body_type"] = self.schemas.render(inner)

        qualified = inner.qualified or ""
        response_name = inner.name if inner.kind in ("simple", "unknown") else ""
        if (response_name in RESPONSE_CLASSES) or qualified.startswith(("fastapi.responses", "starlette.responses")):
            response["schema"] = None
            if response_name in ("HTMLResponse",):
                response["view"] = True
            if response_name in ("StreamingResponse", "FileResponse"):
                response["streaming"] = True
            return response
        if inner.kind == "none" or inner.name == "None":
            return response

        status = self.const.integer(kwargs["status_code"], reg.module) if "status_code" in kwargs else None
        if status in (204, 304):
            response["schema"] = None
            return response

        if self.schemas.is_simple(inner) and inner.kind != "class":
            response["schema"] = {"type": self.schemas.render(inner)}
        else:
            schema = self._response_schema(inner, kwargs, reg.module, referenced)
            response["schema"] = schema.get("fields", schema) if schema.get("type") == "object" else schema
        if response_class and response_class[1] == "text/html":
            response["view"] = True
        if response_class and response_class[0] in ("StreamingResponse", "FileResponse"):
            response["streaming"] = True
        return response

    def _response_schema(self, te: TypeExpr, kwargs, module: ModuleInfo, referenced: Set[str]) -> dict:
        schema = self.schemas.schema(te, referenced, mode="response")
        include = self.const.value(kwargs["response_model_include"], module) if "response_model_include" in kwargs else None
        exclude = self.const.value(kwargs["response_model_exclude"], module) if "response_model_exclude" in kwargs else None
        target = schema
        if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            target = schema["items"]
        if target.get("type") == "object" and "fields" in target:
            fields = dict(target["fields"])
            if isinstance(exclude, list) and exclude:
                fields = {k: v for k, v in fields.items() if k not in exclude}
            if isinstance(include, list) and include:
                fields = {k: v for k, v in fields.items() if k in include}
            target["fields"] = fields
        return schema

    def _inferred_return(self, func: FuncInfo) -> Optional[TypeExpr]:
        """`return ItemOut(...)` / `return StreamingResponse(...)` when nothing is declared."""
        for node in walk_body(func.node.child_by_field_name("body")):
            if node.type == "return_statement" and node.named_children:
                value = strip_type(node.named_children[0])
                if value is not None and value.type == "call":
                    fn, _ = call_parts(value)
                    symbol = self.index.resolve_expr(fn, func.module)
                    if symbol.kind == "class" and not symbol.rest and self.registry.is_model_like(symbol.obj):
                        return TypeExpr("class", symbol.obj.name, cls=symbol.obj, module=symbol.module)
                    if symbol.kind == "external" and (symbol.qualified or "").split(".")[0] in ("fastapi", "starlette") and \
                            symbol.qualified.rsplit(".", 1)[-1] in RESPONSE_CLASSES:
                        name = symbol.qualified.rsplit(".", 1)[-1]
                        return TypeExpr("simple", name, qualified=symbol.qualified, module=func.module)
        return None

    def _documented_responses(self, kwargs, module: ModuleInfo, referenced: Set[str], inherited=()) -> Optional[list]:
        """`responses={404: {...}}` of the app, includes, router and route (the closest wins per status code)."""
        by_code: Dict[object, dict] = {}
        sources = list(inherited) + ([(kwargs["responses"], module)] if "responses" in kwargs else [])
        for source, source_module in sources:
            node = strip_type(source)
            if node is None or node.type != "dictionary":
                continue
            for pair in node.named_children:
                if pair.type != "pair":
                    continue
                code_value = self.const.value(pair.child_by_field_name("key"), source_module)
                value = strip_type(pair.child_by_field_name("value"))
                entry = {"code": code_value if isinstance(code_value, (int, str)) else None}
                if value is not None and value.type == "dictionary":
                    for item in value.named_children:
                        if item.type != "pair":
                            continue
                        key = string_value(item.child_by_field_name("key"))
                        if key == "description":
                            entry["description"] = self.const.string(item.child_by_field_name("value"), source_module)
                        elif key == "model":
                            te = self.types.parse(item.child_by_field_name("value"), source_module)
                            entry["model"] = self.schemas.render(te)
                            schema = self.schemas.schema(te, referenced, mode="response")
                            entry["schema"] = schema.get("fields", schema) if schema.get("type") == "object" else schema
                by_code[entry["code"]] = entry
        return list(by_code.values()) or None

    # ------------------------------------------------------------------
    # status codes, exceptions
    # ------------------------------------------------------------------

    def _status_codes(self, reg: Registration, func: FuncInfo, chain, exceptions, params, security, documented):
        codes = []
        kwargs = reg.kwargs
        if reg.kind == "websocket":
            return []
        if "status_code" in kwargs:
            code = self.const.integer(kwargs["status_code"], reg.module)
            if code:
                codes.append({"code": code, "source": "status_code"})

        for i, (f, b) in enumerate(chain):
            for s in self.a.analysis(f, b).statuses:
                if i == 0 or s["code"] >= 400:
                    source = s["source"] if i == 0 else f"{s['source']} in {self.a.label(f)}"
                    codes.append({"code": s["code"], "source": source})

        for exception in exceptions:
            # an HTTPException raised in the chain is already listed with the statuses of its function
            if exception.get("status") and exception["exception"] != "HTTPException":
                codes.append({"code": exception["status"], "source": f"{exception['exception']} ({exception.get('mapped_by', 'mapping')})"})

        for entry in documented or []:
            code = entry["code"]
            if isinstance(code, int):
                codes.append({"code": code, "source": "responses"})
            elif isinstance(code, str) and re.fullmatch(r"[1-5]XX", code, re.I):
                codes.append({"code": int(code[0]) * 100, "source": f"responses {code}"})

        if any(p["in"] in ("path", "query", "header", "cookie", "body", "form", "multipart") for p in params):
            codes.append({"code": 422, "source": "request validation"})

        if security:
            scheme = next((a.get("scheme") for a in security["annotations"] if a.get("scheme")), None)
            if scheme:
                codes.append({"code": 401, "source": f"{scheme} security"})

        if not any(200 <= c["code"] < 300 for c in codes) and "status_code" not in kwargs:
            codes.append({"code": 200, "source": "default"})

        for c in codes:
            c["reason"] = STATUS_NAME.get(c["code"])
        return sorted(dedupe(codes), key=lambda c: (c["code"], c["source"]))

    def _exceptions(self, chain) -> List[dict]:
        out = []
        for f, b in chain:
            for t in self.a.analysis(f, b).thrown:
                mapping = self.a.exception_mapping(t["cls"], t["external"], t["status"])
                status = t["status"] or mapping.get("status")
                handler = mapping.get("handler")
                if status is None and mapping.get("source") == "unhandled":
                    status = 500
                if t["external"] and not t["cls"] and t["name"] != "HTTPException" and mapping.get("source") == "unhandled":
                    status = 500
                out.append({
                    "exception": t["name"],
                    "thrown_in": self.a.label(f),
                    "status": status,
                    "mapped_by": handler or mapping.get("source"),
                })
        return dedupe(out)[:MAX_LIST]

    # ------------------------------------------------------------------
    # integrations
    # ------------------------------------------------------------------

    def _integrations(self, chain) -> dict:
        groups = {
            "database": "databases", "cache": "caches", "messaging": "messaging", "http": "external_apis", "event": "events",
            "email": "email", "storage": "storage", "search": "search", "transaction": "transactions", "async": "async",
            "resilience": "resilience",
        }
        out: Dict[str, list] = {}
        for f, b in chain:
            for fact in self.a.analysis(f, b).facts:
                group = groups.get(fact.get("kind"), "other")
                cleaned = {k: v for k, v in fact.items() if k not in ("module", "file")}
                if fact.get("kind") == "http":
                    target = self.resolve_service(fact.get("host"), fact.get("target_service"))
                    if target is not None:
                        cleaned["target_module"] = target
                out.setdefault(group, []).append(cleaned)
        return {k: dedupe(v)[:MAX_LIST] for k, v in out.items()}
