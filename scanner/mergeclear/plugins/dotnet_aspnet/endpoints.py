"""
REST endpoint extraction: URL, parameters and where they bind from, request and response schemas, status codes, security
and everything an endpoint reaches downstream - for MVC controllers and Minimal API routes alike.
"""
import re
from collections import Counter
from typing import Callable, Dict, List, Optional, Set, Tuple

from .analyzer import Analyzer, MAX_LIST, dedupe
from .appmodel import AppModel, Route
from .catalog import (
    COLLECTION_TYPES, INJECTED_TYPES, MAP_TYPES, RESULT_TYPES, SIMPLE_TYPES, STATUS_NAME, TYPED_RESULT_STATUS, UPLOAD_TYPES,
)
from .csindex import MethodInfo, ParamInfo, TypeInfo, TypeRef, first, text
from .schemas import ModelRegistry, SchemaBuilder, parse_type_text

PATH_PARAM = re.compile(r"\{\*{0,2}([A-Za-z_][\w]*)(?::([^}=?]+))?(?:=[^}]*)?(\?)?\}")
SERVICE_SUFFIX = re.compile(r"(Service|Repository|Handler|Manager|Client|Provider|Factory|Context|Mediator|Logger|Store|Gateway|Validator|Mapper|Accessor|Options|Settings)$")


class EndpointExtractor:

    def __init__(self, analyzer: Analyzer, app: AppModel, schemas: SchemaBuilder, projects,
                 resolve_service: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]] = None):
        self.a = analyzer
        self.app = app
        self.schemas = schemas
        self.registry: ModelRegistry = schemas.registry
        self.index = analyzer.index
        self.const = analyzer.const
        self.projects = projects
        self.resolve_service = resolve_service or (lambda host, service: None)
        self.chains: Dict[str, Set[str]] = {}
        self.chain_methods: Dict[str, MethodInfo] = {}
        self.route_spans: Dict[str, List[Tuple[str, int, int]]] = {}

    # ------------------------------------------------------------------
    def extract_all(self) -> List[dict]:
        endpoints = []
        for route in self.app.routes:
            if route.meta.get("hidden") or (route.builder is not None and any(b.meta.get("hidden") for b in route.builder.chain())):
                continue
            built = self.build_endpoint(route)
            if built is not None:
                endpoints.append(built)
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
    def handler_key(endpoint: dict) -> str:
        return f"{endpoint['handler']}|{endpoint['method']}|{endpoint['path']}"

    # ------------------------------------------------------------------
    # naming and paths
    # ------------------------------------------------------------------

    def full_template(self, route: Route) -> str:
        parts = []
        if route.builder is not None:
            for b in route.builder.chain():
                if b.prefix:
                    parts.append(b.prefix)
        if route.kind == "controller":
            parts = []
        parts.append(route.path)
        joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
        return "/" + joined

    @staticmethod
    def normalize(path: str) -> Tuple[str, List[Tuple[str, str, bool]]]:
        """(path without constraints, [(parameter, type constraint, optional)])."""
        found = []

        def replace(match):
            found.append((match.group(1), (match.group(2) or "").split(":")[0], bool(match.group(3))))
            return "{" + match.group(1) + "}"

        path = PATH_PARAM.sub(replace, path)
        path = re.sub(r"/{2,}", "/", path)
        if len(path) > 1:
            path = path.rstrip("/")
        return path or "/", found

    def label(self, route: Route, path: str) -> str:
        handler = route.handler
        if handler is not None and not handler.id.startswith(("lambda:", "local:")):
            return f"{handler.owner.name}.{handler.name}" if handler.owner else handler.name
        owner = (handler.owner.name if handler is not None and handler.owner is not None else None) or route.file.rel.rsplit("/", 1)[-1].replace(".cs", "")
        if route.hub is not None:
            return f"{route.hub.name}.Hub"
        method = route.methods[0]
        slug = re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"\{(\w+)\}", r"\1", path)).strip("_") or "root"
        return f"{owner}.{method}_{slug}"

    # ------------------------------------------------------------------
    # endpoint
    # ------------------------------------------------------------------

    def build_endpoint(self, route: Route) -> Optional[dict]:
        handler = route.handler
        file = route.file
        template = self.full_template(route)
        path, template_params = self.normalize(template)
        method = route.methods[0]
        referenced: Set[str] = set()
        is_api = bool(route.owner and route.kind == "controller" and (route.owner.attr("ApiController") or any(
            t.attr("ApiController") for t in self.index.ancestors(route.owner))))
        builder_meta = self._builder_meta(route)
        project = file.project

        params: List[dict] = []
        if handler is not None:
            params = self.parameters(handler, route, template_params, referenced, is_api, method)
        elif route.hub is not None:
            params = []
        body_params = [p for p in params if p["in"] == "body"]
        request_body = self._request_body(body_params)

        chain: List[Tuple[MethodInfo, Optional[object]]] = []
        extra: List[Tuple[MethodInfo, Optional[object]]] = []
        analysis = None
        if handler is not None:
            extra = [(m, None) for m in self._filter_methods(route, builder_meta)]
            reachable = self.a.reachable(handler, None, extra)
            chain = [(handler, None)] + reachable
            analysis = self.a.analysis(handler)
        integrations = self._integrations(chain)
        exceptions = self._exceptions(chain)
        response = self.response(route, handler, builder_meta, analysis, referenced)
        security = self._security(route, builder_meta, path, method)
        documented = self._documented_responses(route, builder_meta, referenced)
        status_codes = self._status_codes(route, handler, chain, exceptions, params, security, documented, is_api, builder_meta, response)

        errors = sorted({STATUS_NAME.get(s["code"], str(s["code"])) for s in status_codes if s["code"] >= 400})
        errors = list(errors) + self._validation_errors(params)

        handler_label = self.label(route, path)
        direct = []
        full = []
        tree = {}
        if handler is not None:
            direct = list(dict.fromkeys(self.a.label(e.callee) for e in self.a.analysis(handler).calls))
            full = list(dict.fromkeys(self.a.label(m) for m, _ in chain[1:]))[:MAX_LIST]
            tree = {handler_label: self.a.call_tree(handler, None)}
        db_ops = [
            {"type": f.get("operation"), "call": f.get("call") or f.get("source"), "entity": f.get("entity"), "table": f.get("table"), "store": f.get("technology")}
            for f in integrations.get("databases", [])
        ]

        summary, description, name = self._documentation(route, handler, builder_meta)
        deprecated = bool(handler is not None and handler.attr("Obsolete")) or bool(route.owner is not None and route.owner.attr("Obsolete"))
        tags = self._tags(route, builder_meta)
        consumes = self._consumes(route, params, builder_meta)
        produces = self._produces(route, builder_meta)
        key = f"{handler_label}|{method}|{path}"
        self.chains[key] = {m.id for m, _ in chain}
        self.route_spans[key] = [b.span for b in (route.builder.chain() if route.builder is not None else []) if b.span]
        for m, _ in chain:
            self.chain_methods.setdefault(m.id, m)

        return {
            # ---- core fields (same contract as the other scanners) ----
            "function": handler.name if handler is not None and not handler.id.startswith(("lambda:", "local:")) else handler_label.rsplit(".", 1)[-1],
            "method": method,
            "path": path,
            "params": params,
            "errors": errors,
            "calls": sorted(set(self.a.analysis(handler).names)) if handler is not None else [],
            "db_ops": dedupe(db_ops),
            "response": response,
            "call_graph": {"direct": direct, "full": full, "tree": tree},
            "impact": full,
            # ---- location ----
            "handler": handler_label,
            "controller": (route.owner.qualified if route.owner else (route.hub.qualified if route.hub else "Program")),
            "module": project,
            "file": file.rel,
            "line": route.line,
            "paths": [path],
            "context_path": self.app.path_base if hasattr(self.app, "path_base") else None,
            "summary": summary,
            "description": description,
            "deprecated": deprecated,
            "framework": "aspnet",
            "tags": tags,
            "operation_id": name,
            "protocol": "websocket" if route.kind == "hub" else "http",
            "style": "controller" if route.kind == "controller" else "minimal-api" if route.kind == "minimal" else "signalr-hub",
            # ---- contract ----
            "consumes": consumes,
            "produces": produces,
            "request_body": request_body,
            "status_codes": status_codes,
            "exceptions": exceptions,
            "security": security,
            "transactional": any(f.get("kind") == "transaction" for m, b in chain for f in self.a.analysis(m, b).facts) or None,
            "dependencies": self._injected_services(route, handler),
            "responses": documented,
            # ---- downstream ----
            "integrations": integrations,
            "referenced_types": sorted(referenced),
        }

    # ------------------------------------------------------------------
    def _builder_meta(self, route: Route) -> dict:
        """Conventions applied to a Minimal API route by its groups and by itself, merged top-down."""
        merged: dict = {"authorize": [], "produces": [], "filters": [], "tags": []}
        chains = route.builder.chain() if route.builder is not None else []
        if route.kind == "controller":
            for conv in self.app.controller_conventions:
                chains = conv["builder"].chain()
                break
        for b in chains:
            m = b.meta
            for entry in m.get("authorize", []):
                merged["authorize"].append({**entry, "level": "group" if b.kind == "group" else "route" if b.kind == "route" else "global"})
            if m.get("anonymous"):
                merged["anonymous"] = True
                merged["anonymous_level"] = b.kind
            merged["produces"].extend(m.get("produces", []))
            merged["filters"].extend(m.get("filters", []))
            merged["tags"].extend(b.tags)
            for key in ("name", "summary", "description", "accepts", "rate_limit", "cache", "version", "hidden"):
                if key in m:
                    merged[key] = m[key]
        return merged

    def _filter_methods(self, route: Route, meta: dict) -> List[MethodInfo]:
        """Filters that run around the handler: [ServiceFilter(typeof(X))], AddEndpointFilter<X>()."""
        out = []
        attrs = []
        if route.handler is not None:
            attrs.extend(route.handler.attrs)
        if route.owner is not None:
            attrs.extend(a for t in self.index.ancestors(route.owner) for a in t.attrs)
        names = list(meta.get("filters", []))
        for a in attrs:
            if a.name in ("ServiceFilter", "TypeFilter") and a.args:
                value = self.const.value(a.args[0], a.file, route.owner)
                if isinstance(value, tuple):
                    names.append(parse_type_text(value[1]).name)
        for name in names:
            t = self.index.resolve(name, route.file, route.owner.namespace if route.owner else "", route.owner)
            if t is None:
                continue
            for candidate in ("OnActionExecuting", "OnActionExecutionAsync", "InvokeAsync", "OnResultExecuting", "OnAuthorizationAsync", "OnException"):
                m = self.index.find_method(t, candidate)
                if m is not None:
                    out.append(m)
        return out

    def _documentation(self, route: Route, handler: Optional[MethodInfo], meta: dict):
        summary = meta.get("summary")
        description = meta.get("description")
        name = meta.get("name") or route.name
        if handler is not None:
            for attr in handler.attrs:
                if attr.name == "SwaggerOperation":
                    summary = summary or (self.const.string(attr.named["Summary"], attr.file, route.owner) if "Summary" in attr.named else None)
                    description = description or (self.const.string(attr.named["Description"], attr.file, route.owner) if "Description" in attr.named else None)
                    name = name or (self.const.string(attr.named["OperationId"], attr.file, route.owner) if "OperationId" in attr.named else None)
                elif attr.name == "EndpointSummary" and attr.args:
                    summary = summary or self.const.string(attr.args[0], attr.file, route.owner)
                elif attr.name == "EndpointDescription" and attr.args:
                    description = description or self.const.string(attr.args[0], attr.file, route.owner)
            description = description or handler.doc
        return summary, description, name

    def _tags(self, route: Route, meta: dict) -> List[str]:
        tags = list(meta.get("tags", []))
        if route.handler is not None:
            for a in route.handler.attrs:
                if a.name == "Tags":
                    tags.extend(v for arg in a.args for v in self.const.strings(arg, a.file, route.owner))
        if route.kind == "controller" and route.owner is not None:
            tags.append(route.owner.name[:-10] if route.owner.name.endswith("Controller") else route.owner.name)
        return list(dict.fromkeys(tags))

    def _injected_services(self, route: Route, handler: Optional[MethodInfo]) -> List[dict]:
        out = []
        owner = route.owner
        if owner is not None and route.kind == "controller":
            for m in owner.members:
                if m.primary or (m.kind == "field" and "readonly" in m.modifiers):
                    out.append({"name": str(m.type), "field": m.name, "kind": "constructor"})
        if handler is not None:
            for p in handler.params:
                if self._is_service(p, route):
                    out.append({"name": str(p.type), "field": p.name, "kind": "parameter"})
        return out

    # ------------------------------------------------------------------
    # parameters
    # ------------------------------------------------------------------

    def _is_service(self, p: ParamInfo, route: Route) -> bool:
        if p.attr("FromServices", "FromKeyedServices"):
            return True
        if p.attr("FromBody", "FromQuery", "FromRoute", "FromHeader", "FromForm", "AsParameters"):
            return False
        ref = self.schemas.unwrap(p.type)
        if ref.name in INJECTED_TYPES or ref.name in ("CancellationToken",):
            return True
        target = self.index.resolve(ref.name, route.file, route.owner.namespace if route.owner else "", route.owner)
        if target is not None:
            kind = self.registry.kind(target)
            if kind in ("model", "entity", "enum", "exception"):
                return False
            if target.kind == "interface" or SERVICE_SUFFIX.search(target.name) or any(r.service is target for r in self.app.services):
                return True
            if kind == "dbcontext":
                return True
        else:
            if SERVICE_SUFFIX.search(ref.name) and ref.name not in SIMPLE_TYPES:
                return True
            if ref.name.startswith("I") and len(ref.name) > 1 and ref.name[1].isupper() and ref.name not in COLLECTION_TYPES | MAP_TYPES | {"IFormFile", "IFormFileCollection", "IFormCollection"}:
                return True
        return False

    def parameters(self, handler: MethodInfo, route: Route, template_params, referenced: Set[str], is_api: bool, method: str) -> List[dict]:
        template = {name: (constraint, optional) for name, constraint, optional in template_params}
        out: List[dict] = []
        minimal = route.kind == "minimal"
        for p in handler.params:
            if self._is_service(p, route):
                continue
            entry = self._parameter(p, handler, route, template, referenced, is_api, minimal, method)
            if entry is None:
                continue
            if isinstance(entry, list):
                out.extend(entry)
            else:
                out.append(entry)
        # route parameters the handler never mentions are still part of the contract
        names = {e["name"] for e in out if e["in"] == "path"}
        for name, (constraint, optional) in template.items():
            if name not in names and name not in {p.name for p in handler.params}:
                out.append({"name": name, "type": self._constraint_type(constraint), "in": "path", "required": True, "default": None})
        return out

    @staticmethod
    def _constraint_type(constraint: str) -> str:
        return {"int": "int", "long": "long", "guid": "Guid", "bool": "bool", "datetime": "DateTime", "decimal": "decimal", "double": "double", "float": "float",
                "alpha": "string", "minlength": "string", "maxlength": "string", "regex": "string"}.get((constraint or "").lower(), "string")

    def _parameter(self, p: ParamInfo, handler: MethodInfo, route: Route, template, referenced: Set[str], is_api: bool, minimal: bool, method: str):
        file = handler.file
        owner = handler.owner
        namespace = owner.namespace if owner else ""
        ref = p.type
        inner = self.schemas.unwrap(ref)
        target = self.index.resolve(inner.name, file, namespace, owner)

        def attr_name(attr):
            if attr is not None:
                for key in ("Name", "name"):
                    if key in attr.named:
                        return self.const.string(attr.named[key], attr.file, owner)
                if attr.args and attr.name in ("FromQuery", "FromRoute", "FromHeader", "FromForm"):
                    value = self.const.string(attr.args[0], attr.file, owner)
                    return value
            return None

        if p.attr("AsParameters") and target is not None:
            return self._expand_parameters(target, route, template, referenced, minimal, method)

        location = None
        explicit = None
        for attr_name_, loc in (("FromBody", "body"), ("FromQuery", "query"), ("FromRoute", "path"), ("FromHeader", "header"), ("FromForm", "form"),
                                ("FromKeyedServices", None)):
            attr = p.attr(attr_name_)
            if attr is not None:
                location, explicit = loc, attr_name(attr)
                break
        name = explicit or p.name

        is_upload = inner.name in UPLOAD_TYPES or (inner.name in COLLECTION_TYPES and inner.args and inner.args[0].name == "IFormFile")
        scalar = self.schemas.is_scalar_ref(inner, file, namespace, owner)
        scalar_collection = self.schemas.collection_of_scalars(inner, file, namespace, owner)
        if location is None:
            if p.name in template:
                location = "path"
            elif is_upload:
                location = "multipart"
            elif inner.name == "IFormCollection":
                location = "form"
            elif scalar or scalar_collection:
                location = "query"
            elif inner.name in MAP_TYPES and minimal and method in ("GET", "DELETE", "HEAD", "OPTIONS"):
                location = "query"
            elif is_api or (minimal and method not in ("GET", "DELETE", "HEAD", "OPTIONS")):
                location = "body"
            elif minimal:
                location = "query"
            else:
                location = "model"
        if location == "form" and is_upload:
            location = "multipart"

        if location in ("query", "header", "form") and not scalar and not scalar_collection and target is not None and self.registry.is_model_like(target):
            return self._expand_parameters(target, route, template, referenced, minimal, method, default_location=location)

        has_default = p.default is not None
        nullable = ref.nullable or inner.nullable
        value_type = self.registry.is_value_type(inner, target)
        bind_required = bool(p.attr("BindRequired", "Required"))
        required = True
        if location == "path":
            required = not (p.name in template and template[p.name][1]) and not nullable
        elif has_default or nullable:
            required = False
        elif minimal:
            required = True
        elif bind_required:
            required = True
        elif location in ("body",):
            required = not nullable
        elif value_type:
            required = False
        else:
            required = bool(self.registry.nullable_enabled.get(file.project)) and not nullable and not scalar_collection or False

        entry = {"name": name, "type": str(ref), "in": location, "required": bool(required), "default": self._default(p, file, owner)}
        rules = self.registry.attribute_rules(p.attrs, file, owner)
        rules.pop("required", None)
        if rules:
            entry["validation"] = rules
        if location in ("body", "form", "model") and not scalar and not scalar_collection:
            fluent = target is not None and bool(self.registry.fluent_rules(target))
            entry["validated"] = bool(is_api or fluent)
            element = self.schemas.element_of(inner)
            if target is not None and self.registry.is_model_like(target):
                fields = self.schemas.schema_fields(target, inner, referenced)
                if fields:
                    entry["schema"] = fields
            elif element is not None:
                entry["schema"] = {"[]": self.schemas.schema(inner, file, namespace, owner, referenced)}
            elif inner.name in MAP_TYPES:
                entry["schema"] = {"{}": self.schemas.schema(inner, file, namespace, owner, referenced)}
        if target is not None:
            referenced.add(target.qualified)
        return entry

    def _default(self, p: ParamInfo, file, owner):
        if p.default is None:
            return None
        value = self.const.value(p.default, file, owner)
        from .csindex import UNKNOWN
        if value is UNKNOWN:
            return None
        return value if isinstance(value, (str, int, float, bool)) or value is None else None

    def _expand_parameters(self, target: TypeInfo, route: Route, template, referenced, minimal, method, default_location: Optional[str] = None):
        out = []
        for a in reversed(self.index.ancestors(target)):
            for m in a.members:
                if m.kind != "property" or m.is_static or "private" in m.modifiers:
                    continue
                inner = self.schemas.unwrap(m.type)
                loc = default_location or "query"
                name = m.name
                for attr_name_, l in (("FromQuery", "query"), ("FromRoute", "path"), ("FromHeader", "header"), ("FromForm", "form"), ("FromBody", "body")):
                    attr = m.attr(attr_name_)
                    if attr is not None:
                        loc = l
                        if "Name" in attr.named:
                            name = self.const.string(attr.named["Name"], attr.file, a) or name
                        break
                else:
                    if m.name in template or m.name.lower() in template:
                        loc = "path"
                    elif default_location is None and not self.schemas.is_scalar_ref(inner, a.file, a.namespace, a) and \
                            not self.schemas.collection_of_scalars(inner, a.file, a.namespace, a):
                        loc = "body"
                entry = {"name": name, "type": str(m.type), "in": loc, "required": loc == "path" or "required" in m.modifiers or bool(m.attr("Required")),
                         "default": None}
                rules = self.registry.attribute_rules(m.attrs, a.file, a)
                rules.pop("required", None)
                if rules:
                    entry["validation"] = rules
                out.append(entry)
        referenced.add(target.qualified)
        return out

    def _request_body(self, body_params: List[dict]) -> Optional[dict]:
        if not body_params:
            return None
        p = body_params[0]
        return {"type": p["type"], "required": p["required"], "validated": p.get("validated", False), "schema": p.get("schema")}

    def _validation_errors(self, params):
        field_errors = []
        for p in params:
            if p.get("in") not in ("body", "form", "model") or not p.get("validated"):
                continue
            for field_name, details in (p.get("schema") or {}).items():
                if not isinstance(details, dict):
                    continue
                for rule in (details.get("validation") or {}):
                    field_errors.append({"name": field_name, "rule": rule})
        if field_errors:
            return [{"type": "VALIDATION_ERROR", "status": 400, "fields": field_errors}]
        return []

    def _consumes(self, route: Route, params, meta) -> List[str]:
        declared = []
        sources = []
        if route.handler is not None:
            sources.extend(route.handler.attrs)
        if route.owner is not None:
            sources.extend(a for t in self.index.ancestors(route.owner) for a in t.attrs)
        for a in sources:
            if a.name == "Consumes":
                declared.extend(v for arg in a.args for v in self.const.strings(arg, a.file, route.owner))
        if declared:
            return list(dict.fromkeys(declared))
        if meta.get("accepts") and meta["accepts"][1]:
            return list(meta["accepts"][1])
        locations = {p["in"] for p in params}
        if "multipart" in locations:
            return ["multipart/form-data"]
        if "form" in locations:
            return ["application/x-www-form-urlencoded"]
        if "body" in locations:
            return ["application/json"]
        return []

    def _produces(self, route: Route, meta) -> List[str]:
        out = []
        sources = []
        if route.handler is not None:
            sources.extend(route.handler.attrs)
        if route.owner is not None:
            sources.extend(a for t in self.index.ancestors(route.owner) for a in t.attrs)
        for a in sources:
            if a.name == "Produces":
                out.extend(v for arg in a.args for v in self.const.strings(arg, a.file, route.owner) if "/" in v)
        return list(dict.fromkeys(v for v in out if v != "application/json"))

    # ------------------------------------------------------------------
    # response
    # ------------------------------------------------------------------

    def _declared_responses(self, route: Route, meta) -> List[Tuple[int, Optional[TypeRef], Optional[str]]]:
        """[ProducesResponseType], [SwaggerResponse] and `.Produces<T>()`: (status, type, description)."""
        out = []
        sources = []
        if route.handler is not None:
            sources.extend(route.handler.attrs)
        if route.owner is not None:
            sources.extend(a for t in reversed(self.index.ancestors(route.owner)) for a in t.attrs)
        for a in sources:
            if a.name in ("ProducesResponseType", "ProducesDefaultResponseType", "SwaggerResponse", "ProducesErrorResponseType"):
                status, type_ref, description = None, None, None
                generic = self._attribute_type_args(a)
                if generic:
                    type_ref = generic[0]
                for arg in a.args:
                    value = self.const.value(arg, a.file, route.owner)
                    if isinstance(value, tuple) and value and value[0] == "typeof":
                        type_ref = parse_type_text(value[1])
                    elif isinstance(value, int) and not isinstance(value, bool):
                        status = value
                    elif isinstance(value, str) and a.name == "SwaggerResponse":
                        description = value
                    else:
                        member = self._status_member(arg)
                        if member:
                            status = member
                if "Type" in a.named:
                    value = self.const.value(a.named["Type"], a.file, route.owner)
                    if isinstance(value, tuple):
                        type_ref = parse_type_text(value[1])
                if "StatusCode" in a.named:
                    status = self.const.integer(a.named["StatusCode"], a.file, route.owner) or status
                if a.name == "ProducesDefaultResponseType":
                    status = 500
                if status:
                    out.append((status, type_ref, description))
        for status, type_ref in meta.get("produces", []):
            out.append((status, type_ref, None))
        return out

    def _attribute_type_args(self, attr) -> List[TypeRef]:
        node = attr.node
        if node is None:
            return []
        name = node.child_by_field_name("name")
        if name is not None and name.type == "generic_name":
            arguments = first(name, "type_argument_list")
            return [parse_type_text(text(c)) for c in (arguments.named_children if arguments is not None else [])]
        return []

    def _status_member(self, node) -> Optional[int]:
        from .csindex import status_from_member
        return status_from_member(text(node).replace("(int)", "").strip("() "))

    def _typed_results(self, ref: TypeRef) -> List[Tuple[int, Optional[TypeRef]]]:
        """Results<Ok<T>, NotFound, BadRequest<ProblemDetails>> -> [(200, T), (404, None), (400, ProblemDetails)]."""
        out = []
        members = ref.args if ref.name == "Results" else [ref]
        for m in members:
            status = TYPED_RESULT_STATUS.get(m.name)
            if status:
                out.append((status, m.args[0] if m.args else None))
        return out

    def response(self, route: Route, handler: Optional[MethodInfo], meta: dict, analysis, referenced: Set[str]) -> dict:
        response = {"type": None, "body_type": None, "schema": None}
        if route.kind == "hub" or handler is None:
            return response
        file, owner = handler.file, handler.owner
        namespace = owner.namespace if owner else ""
        declared_ref = handler.returns
        unwrapped = declared_ref
        while unwrapped.name in ("Task", "ValueTask") and unwrapped.args:
            unwrapped = unwrapped.args[0]
        response["type"] = str(declared_ref)

        body_ref: Optional[TypeRef] = None
        produced = self._declared_responses(route, meta)
        success = [(s, t) for s, t, _ in produced if 200 <= s < 300 and t is not None]

        if unwrapped.name == "ActionResult" and unwrapped.args:
            body_ref = unwrapped.args[0]
        elif unwrapped.name == "Results" and unwrapped.args:
            for status, type_ref in self._typed_results(unwrapped):
                if 200 <= status < 300 and type_ref is not None:
                    body_ref = type_ref
                    break
        elif unwrapped.name in ("Ok", "Created", "Accepted") and unwrapped.args:
            body_ref = unwrapped.args[0]
        elif unwrapped.name not in RESULT_TYPES and unwrapped.name not in ("void", "Task", "ValueTask", "IResult"):
            body_ref = unwrapped
        if success:
            body_ref = success[0][1]
        if body_ref is None and analysis is not None:
            for r in analysis.returns:
                if 200 <= r["status"] < 300 and r.get("body") is not None and r["body"].ref.name not in ("object", "var"):
                    body_ref = r["body"].ref
                    break
        if unwrapped.name in ("FileResult", "FileContentResult", "FileStreamResult", "Stream", "PhysicalFileResult") or (
                analysis is not None and any(r["helper"] in ("File", "PhysicalFile", "Stream") for r in analysis.returns)):
            response["streaming"] = True
        if unwrapped.name in ("ViewResult", "PartialViewResult") or (analysis is not None and any(r["helper"] in ("View", "PartialView") for r in analysis.returns)):
            response["view"] = True
        if unwrapped.name == "IAsyncEnumerable":
            response["streaming"] = True

        if body_ref is None or body_ref.name in ("void", "Task", "IActionResult", "IResult", "ActionResult", "object") and not body_ref.args:
            return response
        body = self.schemas.unwrap(body_ref)
        response["body_type"] = str(body)
        statuses = {s for s, _, _ in produced} | ({r["status"] for r in analysis.returns} if analysis is not None else set())
        if 204 in statuses and not any(200 <= s < 300 and s != 204 for s in statuses):
            response["schema"] = None
            return response
        if self.schemas.is_scalar_ref(body, file, namespace, owner):
            response["schema"] = {"type": str(body)}
        else:
            schema = self.schemas.schema(body, file, namespace, owner, referenced, mode="response")
            response["schema"] = schema.get("fields", schema) if schema.get("type") == "object" else schema
        return response

    def _documented_responses(self, route: Route, meta: dict, referenced: Set[str]) -> Optional[list]:
        out = []
        file = route.file
        owner = route.owner
        for status, type_ref, description in self._declared_responses(route, meta):
            entry = {"code": status}
            if description:
                entry["description"] = description
            if type_ref is not None:
                entry["model"] = str(type_ref)
                if not self.schemas.is_scalar_ref(type_ref, file, owner.namespace if owner else "", owner):
                    schema = self.schemas.schema(type_ref, file, owner.namespace if owner else "", owner, referenced, mode="response")
                    entry["schema"] = schema.get("fields", schema) if schema.get("type") == "object" else schema
            out.append(entry)
        by_code = {}
        for e in out:
            by_code[e["code"]] = e
        return list(by_code.values()) or None

    # ------------------------------------------------------------------
    # status codes and exceptions
    # ------------------------------------------------------------------

    def _status_codes(self, route: Route, handler: Optional[MethodInfo], chain, exceptions, params, security, documented, is_api, meta, response):
        codes = []
        if route.kind == "hub":
            return []
        if handler is not None:
            unwrapped = handler.returns
            while unwrapped.name in ("Task", "ValueTask") and unwrapped.args:
                unwrapped = unwrapped.args[0]
            for status, _ in self._typed_results(unwrapped):
                codes.append({"code": status, "source": f"{unwrapped.name} return type"})
        for entry in documented or []:
            codes.append({"code": entry["code"], "source": "ProducesResponseType"})
        for i, (m, b) in enumerate(chain):
            for s in self.a.analysis(m, b).statuses:
                if i == 0 or s["code"] >= 400:
                    source = s["source"] if i == 0 else f"{s['source']} in {self.a.label(m)}"
                    codes.append({"code": s["code"], "source": source})
        for exception in exceptions:
            if exception.get("status") and exception.get("mapped_by") != "unhandled":
                codes.append({"code": exception["status"], "source": f"{exception['exception']} ({exception.get('mapped_by', 'mapping')})"})
            elif exception.get("status") == 500:
                codes.append({"code": 500, "source": f"{exception['exception']} (unhandled)"})
        if is_api and any(p["in"] in ("body", "form", "query", "path", "model", "header") for p in params):
            codes.append({"code": 400, "source": "ApiController model validation"})
        elif any(p.get("validated") for p in params):
            codes.append({"code": 400, "source": "model validation"})
        if security:
            rule = security.get("url_rule") or {}
            if rule.get("access") != "permitAll":
                codes.append({"code": 401, "source": "Authorize"})
                if rule.get("roles") or rule.get("policy"):
                    codes.append({"code": 403, "source": "Authorize"})
        if meta.get("rate_limit") and meta["rate_limit"] != "disabled" or (handler is not None and handler.attr("EnableRateLimiting")):
            codes.append({"code": 429, "source": "rate limiting"})
        if not any(200 <= c["code"] < 300 for c in codes):
            if not (handler is None):
                codes.append({"code": 200, "source": "default"})
            else:
                codes.append({"code": 200, "source": "default"})
        for c in codes:
            c["reason"] = STATUS_NAME.get(c["code"])
        return sorted(dedupe(codes), key=lambda c: (c["code"], c["source"]))

    def _exceptions(self, chain) -> List[dict]:
        out = []
        for m, b in chain:
            for t in self.a.analysis(m, b).thrown:
                status, handled_by = self.a.exception_status(t["info"], t["name"])
                if t["status"]:
                    status = t["status"]
                out.append({"exception": t["name"], "thrown_in": self.a.label(m), "status": status, "mapped_by": handled_by})
        return dedupe(out)[:MAX_LIST]

    # ------------------------------------------------------------------
    # security
    # ------------------------------------------------------------------

    def _security(self, route: Route, meta: dict, path: str, method: str) -> Optional[dict]:
        annotations = []
        roles: List[str] = []
        policies: List[str] = []
        claims: List[str] = []
        anonymous = bool(meta.get("anonymous"))
        entries = []   # (level, roles, policies)
        sources = []
        if route.kind == "controller" and route.owner is not None:
            for t in reversed(self.index.ancestors(route.owner)):
                sources.append(("class", t.attrs, t))
            if route.handler is not None:
                sources.append(("method", route.handler.attrs, route.owner))
        elif route.handler is not None:
            sources.append(("route", route.handler.attrs, route.owner))
        for level, attrs, t in sources:
            for a in attrs:
                if a.name == "AllowAnonymous":
                    anonymous = True
                    annotations.append({"annotation": "AllowAnonymous", "expression": None, "level": level})
                elif a.name == "Authorize":
                    r = self.const.string(a.named["Roles"], a.file, t) if "Roles" in a.named else None
                    pol = self.const.string(a.named["Policy"], a.file, t) if "Policy" in a.named else (self.const.string(a.args[0], a.file, t) if a.args else None)
                    scheme = self.const.string(a.named["AuthenticationSchemes"], a.file, t) if "AuthenticationSchemes" in a.named else None
                    expression = "; ".join(x for x in (f"Roles={r}" if r else None, f"Policy={pol}" if pol else None, f"Schemes={scheme}" if scheme else None) if x)
                    annotations.append({"annotation": "Authorize", "expression": expression or None, "level": level})
                    entries.append((level, [x.strip() for x in (r or "").split(",") if x.strip()], [pol] if pol else []))
        for entry in meta.get("authorize", []):
            expression = "; ".join(x for x in (f"Roles={','.join(entry['roles'])}" if entry["roles"] else None,
                                               f"Policy={','.join(entry['policies'])}" if entry["policies"] else None) if x)
            annotations.append({"annotation": "RequireAuthorization", "expression": expression or None, "level": entry.get("level", "group")})
            entries.append((entry.get("level", "group"), [r for r in entry["roles"] if r], entry["policies"]))
        for level, r, pol in entries:
            roles.extend(r)
            policies.extend(pol)
        for name in policies:
            policy = self.app.policies.get(name)
            if policy:
                roles.extend(policy.get("roles", []))
                claims.extend(policy.get("claims", []))
        fallback = None
        if not annotations and not anonymous and self.app.fallback_policy is not None:
            fallback = self.app.fallback_policy
            annotations.append({"annotation": "FallbackPolicy", "expression": None, "level": "global"})
            roles.extend(fallback.get("roles", []))
        if not annotations:
            return None
        if anonymous:
            rule = {"access": "permitAll", "roles": [], "defined_in": next((a["level"] for a in annotations if a["annotation"] == "AllowAnonymous"), "route"),
                    "patterns": [path], "http_method": method}
            return {"annotations": dedupe(annotations), "url_rule": rule}
        roles = sorted(dict.fromkeys(roles))
        access = "hasRole" if roles else "policy" if policies else "authenticated"
        rule = {"access": access, "roles": roles, "policy": sorted(dict.fromkeys(policies)) or None, "claims": sorted(dict.fromkeys(claims)) or None,
                "defined_in": annotations[0]["level"], "patterns": [path], "http_method": method, "source": "authorization"}
        return {"annotations": dedupe(annotations), "url_rule": {k: v for k, v in rule.items() if v is not None}}

    # ------------------------------------------------------------------
    # integrations
    # ------------------------------------------------------------------

    def _integrations(self, chain) -> dict:
        groups = {
            "database": "databases", "cache": "caches", "messaging": "messaging", "http": "external_apis", "event": "events", "email": "email",
            "storage": "storage", "search": "search", "transaction": "transactions", "async": "async", "resilience": "resilience",
        }
        out: Dict[str, list] = {}
        for m, b in chain:
            for fact in self.a.analysis(m, b).facts:
                group = groups.get(fact.get("kind"), "other")
                cleaned = {k: v for k, v in fact.items() if k not in ("module", "file")}
                if fact.get("kind") == "http":
                    target = self.resolve_service(fact.get("host"), fact.get("target_service"))
                    if target is not None:
                        cleaned["target_module"] = target
                out.setdefault(group, []).append(cleaned)
        return {k: dedupe(v)[:MAX_LIST] for k, v in out.items()}

