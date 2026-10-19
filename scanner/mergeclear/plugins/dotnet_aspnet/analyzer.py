"""
Method analysis: for every method, what it calls inside the project (through DI, interfaces, generics and MediatR), which
external systems it touches (databases, caches, brokers, HTTP APIs, storage, email, search), which HTTP statuses it produces
and which exceptions it throws. Types are inferred just far enough to know what `_context`, `_cache` or `_repo` is.
"""
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from mergeclear.core.sql import looks_like_sql, parse_sql
from mergeclear.core.tags import Tag

from .appmodel import AppModel
from .catalog import (
    CACHE_THROUGH, COMMAND_EXECUTE, COSMOS_DELETE, COSMOS_READ, COSMOS_WRITE, DAPPER_METHODS, DISTRIBUTED_DELETE,
    DISTRIBUTED_READ, DISTRIBUTED_WRITE, EF_COMMIT, EF_DELETE, EF_RAW_READ, EF_RAW_WRITE, EF_READ, EF_TX, EF_WRITE, EMAIL_METHODS,
    HTTP_CLIENT_METHODS, KAFKA_CONSUMER, LIBRARY_TYPES, MONGO_DELETE, MONGO_READ, MONGO_WRITE, PUBLISH_METHODS, REDIS_DELETE,
    REDIS_PUBLISH, REDIS_READ, REDIS_WRITE, REFIT_ATTRIBUTES, RESILIENCE_ATTRIBUTES, RESULT_HELPERS, RESULT_RECEIVERS,
    SEARCH_DELETE, SEARCH_READ, SEARCH_WRITE, STORAGE_DELETE, STORAGE_READ, STORAGE_WRITE,
)
from .csindex import (
    INTERPOLATION_PUNCTUATION, ConstEvaluator, CsIndex, FileInfo, MethodInfo, TypeInfo, TypeRef, UNKNOWN, argument_nodes, first, invocation_parts, keyword, line_of, parse_type, positional, status_from_member, strip_await, text, unparen, walk,
)
from .schemas import ModelRegistry

logger = logging.getLogger(__name__)

CONTEXT_TAG = Tag("db", "efcore", "context")
MAX_CLOSURE = 400
MAX_LIST = 200
CREATION_STATUS = {
    "OkObjectResult": 200, "OkResult": 200, "JsonResult": 200, "ContentResult": 200, "FileContentResult": 200, "FileStreamResult": 200,
    "PhysicalFileResult": 200, "NotFoundResult": 404, "NotFoundObjectResult": 404, "BadRequestResult": 400, "BadRequestObjectResult": 400,
    "UnauthorizedResult": 401, "UnauthorizedObjectResult": 401, "ForbidResult": 403, "ConflictResult": 409, "ConflictObjectResult": 409,
    "NoContentResult": 204, "CreatedResult": 201, "CreatedAtActionResult": 201, "CreatedAtRouteResult": 201, "AcceptedResult": 202,
    "AcceptedAtActionResult": 202, "UnprocessableEntityResult": 422, "UnprocessableEntityObjectResult": 422, "RedirectResult": 302,
}


def dedupe(items):
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


@dataclass(eq=False)
class Ty:
    """What an expression is: a project type (with generic arguments), a library type (a Tag), or nothing known."""
    ref: TypeRef
    info: Optional[TypeInfo] = None
    tag: Optional[Tag] = None
    args: Tuple["Ty", ...] = ()
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        base = self.info.qualified if self.info else self.ref.name
        return base + ("<" + ",".join(a.key() for a in self.args) + ">" if self.args else "")

    def with_extra(self, **kw) -> "Ty":
        merged = dict(self.extra)
        merged.update({k: v for k, v in kw.items() if v is not None})
        return Ty(self.ref, self.info, self.tag, self.args, merged)


@dataclass(eq=False)
class Binding:
    """Type arguments in force while a generic method / type is analysed (`T` -> Order)."""
    key: str
    map: Dict[str, Ty] = field(default_factory=dict)


@dataclass
class CallEdge:
    callee: MethodInfo
    binding: Optional[Binding]
    line: int


@dataclass(eq=False)
class MethodAnalysis:
    method: MethodInfo
    binding: Optional[Binding] = None
    calls: List[CallEdge] = field(default_factory=list)
    names: List[str] = field(default_factory=list)
    facts: List[dict] = field(default_factory=list)
    statuses: List[dict] = field(default_factory=list)
    thrown: List[dict] = field(default_factory=list)
    returns: List[dict] = field(default_factory=list)            # result helpers used: {status, body, helper, line}
    instantiated: List[TypeInfo] = field(default_factory=list)
    field_types: Dict[str, Ty] = field(default_factory=dict)
    base_url: Optional[str] = None


def node_id(method: MethodInfo, binding: Optional[Binding] = None) -> str:
    return method.id if binding is None else f"{method.id}@{binding.key}"


class Analyzer:

    def __init__(self, index: CsIndex, const: ConstEvaluator, registry: ModelRegistry, app: AppModel,
                 resolve_placeholders: Optional[Callable[[FileInfo, str], str]] = None):
        self.index = index
        self.const = const
        self.registry = registry
        self.app = app
        self.resolve_placeholders = resolve_placeholders or (lambda file, value: value)
        self._analyses: Dict[Tuple[str, Optional[str]], MethodAnalysis] = {}
        self._in_progress: set = set()
        self.listeners: List[dict] = []
        self.scheduled: List[dict] = []
        self._message_handlers: Optional[Dict[str, List[Tuple[TypeInfo, MethodInfo, str]]]] = None
        self._refit: Dict[str, Dict[str, Tuple[str, str]]] = {}
        self._class_base_url: Dict[str, str] = {}

    # ------------------------------------------------------------------
    def analyze_all(self):
        self._index_listeners()
        for method in list(self.index.methods.values()):
            if method.owner is None and method.name == "<Main>" or method.owner is not None:
                self.analysis(method)
        for method in list(self.app._lambdas.values()):
            self.analysis(method)

    def analysis(self, method: MethodInfo, binding: Optional[Binding] = None) -> MethodAnalysis:
        key = (method.id, binding.key if binding else None)
        cached = self._analyses.get(key)
        if cached is not None:
            return cached
        if key in self._in_progress:
            return MethodAnalysis(method, binding)
        self._in_progress.add(key)
        try:
            result = MethodWalker(self, method, binding).run()
        finally:
            self._in_progress.discard(key)
        self._analyses[key] = result
        return result

    def label(self, method: MethodInfo) -> str:
        if method.id.startswith("lambda:"):
            return f"{method.owner.name if method.owner else 'Program'}.{method.name}"
        return method.label

    def reachable(self, method: MethodInfo, binding: Optional[Binding] = None, extra_roots=()) -> List[Tuple[MethodInfo, Optional[Binding]]]:
        seen = {node_id(method, binding)}
        order: List[Tuple[MethodInfo, Optional[Binding]]] = []
        queue = deque((e.callee, e.binding) for e in self.analysis(method, binding).calls)
        queue.extend(extra_roots)
        while queue and len(order) < MAX_CLOSURE:
            m, b = queue.popleft()
            nid = node_id(m, b)
            if nid in seen:
                continue
            seen.add(nid)
            order.append((m, b))
            queue.extend((e.callee, e.binding) for e in self.analysis(m, b).calls)
        return order

    def call_tree(self, method: MethodInfo, binding: Optional[Binding] = None, depth=0, seen=None, max_depth=6) -> dict:
        seen = seen or set()
        nid = node_id(method, binding)
        if depth >= max_depth or nid in seen:
            return {}
        seen = seen | {nid}
        return {self.label(e.callee): self.call_tree(e.callee, e.binding, depth + 1, seen, max_depth) for e in self.analysis(method, binding).calls}

    # ------------------------------------------------------------------
    # message handlers (MediatR, MassTransit) and background workers
    # ------------------------------------------------------------------

    def message_handlers(self) -> Dict[str, List[Tuple[TypeInfo, MethodInfo, str]]]:
        """{message type name: [(handler class, Handle method, kind)]} for MediatR requests / notifications and MassTransit consumers."""
        if self._message_handlers is None:
            out: Dict[str, List[Tuple[TypeInfo, MethodInfo, str]]] = {}
            for t in self.index.types.values():
                for base in t.bases:
                    kind = {"IRequestHandler": "request", "INotificationHandler": "notification", "IStreamRequestHandler": "stream",
                            "IConsumer": "consumer", "IHandleMessages": "nservicebus", "ICommandHandler": "request", "IQueryHandler": "request",
                            "IEventHandler": "notification", "IHandler": "request"}.get(base.name)
                    if kind is None or not base.args:
                        continue
                    if base.name == "IConsumer" and len(base.args) != 1:
                        continue
                    message = base.args[0].name
                    handle = {"consumer": "Consume", "nservicebus": "Handle"}.get(kind, "Handle")
                    method = self.index.find_method(t, handle) or self.index.find_method(t, "HandleAsync") or self.index.find_method(t, "Execute")
                    if method is not None:
                        out.setdefault(message, []).append((t, method, kind))
            self._message_handlers = out
        return self._message_handlers

    def _index_listeners(self):
        seen = set()
        for message, handlers in self.message_handlers().items():
            for t, method, kind in handlers:
                if kind in ("consumer", "nservicebus"):
                    mt = next((m for m in self.app.masstransit if m["consumer"] is t), None)
                    self.listeners.append({
                        "handler": f"{t.name}.{method.name}", "method_id": method.id, "module": t.file.project, "file": t.file.rel,
                        "line": method.line_start, "technology": "masstransit" if kind == "consumer" else "nservicebus",
                        "destinations": [message] if not mt or not mt.get("queue") else [mt["queue"]], "message": message, "kind": "consumer",
                    })
                    seen.add(t.qualified)
        for t in self.index.types.values():
            bases = set(self.index.base_names(t))
            if "BackgroundService" in bases or "IHostedService" in bases or "IHostedLifecycleService" in bases:
                method = self.index.find_method(t, "ExecuteAsync") or self.index.find_method(t, "StartAsync")
                if method is not None:
                    self.scheduled.append({
                        "handler": f"{t.name}.{method.name}", "method_id": method.id, "module": t.file.project, "file": t.file.rel,
                        "line": method.line_start, "technology": "hosted-service", "schedule": {"kind": "background service"},
                    })
            if "IJob" in bases:
                method = self.index.find_method(t, "Execute")
                if method is not None:
                    self.scheduled.append({
                        "handler": f"{t.name}.{method.name}", "method_id": method.id, "module": t.file.project, "file": t.file.rel,
                        "line": method.line_start, "technology": "quartz", "schedule": {"kind": "quartz job"},
                    })

    # ------------------------------------------------------------------
    # refit interfaces
    # ------------------------------------------------------------------

    def refit_methods(self, t: TypeInfo) -> Dict[str, Tuple[str, str]]:
        """{method: (http method, path)} when the interface is a Refit client."""
        cached = self._refit.get(t.qualified)
        if cached is not None:
            return cached
        out = {}
        for m in t.methods:
            for a in m.attrs:
                if a.name in REFIT_ATTRIBUTES:
                    path = self.const.string(a.args[0], a.file, t) if a.args else ""
                    out[m.name] = (REFIT_ATTRIBUTES[a.name], path or "")
        self._refit[t.qualified] = out
        return out

    def class_base_url(self, t: TypeInfo) -> Optional[str]:
        """BaseAddress assigned inside a client class, or configured with AddHttpClient<T>."""
        if t.qualified in self._class_base_url:
            return self._class_base_url[t.qualified]
        value = None
        config = self.app.http_client_for(t)
        if config is not None and config.base_url:
            value = config.base_url
        if value is None:
            for m in t.ctors + t.methods:
                for root in m.body:
                    for n in walk(root):
                        if n.type == "assignment_expression" and text(n.child_by_field_name("left")).endswith("BaseAddress"):
                            right = n.child_by_field_name("right")
                            creation = right if right is not None and right.type in ("object_creation_expression", "implicit_object_creation_expression") else None
                            target = argument_nodes(creation)[0][1] if creation is not None and argument_nodes(creation) else right
                            value = self.const.string(target, t.file, t)
        self._class_base_url[t.qualified] = value
        return value

    # ------------------------------------------------------------------
    # exceptions
    # ------------------------------------------------------------------

    def exception_status(self, exc_type: Optional[TypeInfo], name: str) -> Tuple[Optional[int], str]:
        """(status, who answers) for an exception type."""
        mapping = self.app.exception_mapping_for(exc_type, name)
        if mapping is not None:
            return mapping.status, mapping.handler
        if exc_type is not None:
            own = self.app.class_status(exc_type)
            if own:
                return own, exc_type.name
        generic = next((m for m in self.app.exception_mappings if m.exception in ("Exception", "SystemException") and m.status), None)
        if generic is not None:
            return generic.status, generic.handler
        return 500, "unhandled"


# ============================================================================
# EXPRESSION TYPES + FUNCTION WALKER
# ============================================================================

class MethodWalker:

    def __init__(self, analyzer: Analyzer, method: MethodInfo, binding: Optional[Binding]):
        self.A = analyzer
        self.index = analyzer.index
        self.const = analyzer.const
        self.registry = analyzer.registry
        self.app = analyzer.app
        self.method = method
        self.binding = binding
        self.file = method.file
        self.owner = method.owner
        self.namespace = method.owner.namespace if method.owner else ""
        self.env: Dict[str, Ty] = {}
        self.assigned: Dict[str, object] = {}
        self.result = MethodAnalysis(method, binding)
        self._seen_edges: set = set()
        self._handled: set = set()
        self._local_functions: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # types
    # ------------------------------------------------------------------

    def ty_from_ref(self, ref: TypeRef, file: Optional[FileInfo] = None, namespace: Optional[str] = None, owner: Optional[TypeInfo] = None,
                    bindings: Optional[Dict[str, Ty]] = None) -> Ty:
        file = file or self.file
        namespace = self.namespace if namespace is None else namespace
        owner = owner or self.owner
        bindings = bindings if bindings is not None else (self.binding.map if self.binding else {})
        if ref.name in bindings and not ref.array:
            return bindings[ref.name]
        method_params = self.method.type_params
        if ref.name in method_params and ref.name in bindings:
            return bindings[ref.name]
        args = tuple(self.ty_from_ref(a, file, namespace, owner, bindings) for a in ref.args)
        if ref.array:
            return Ty(ref, None, None, (self.ty_from_ref(ref.element(), file, namespace, owner, bindings),))
        if ref.name in ("Task", "ValueTask", "Lazy", "Nullable") and args:
            return args[0]
        info = self.index.resolve(ref.name, file, namespace, owner)
        if info is not None:
            return Ty(ref, info, None, args)
        entry = LIBRARY_TYPES.get(ref.name)
        if ref.name == "IConsumer" and len(ref.args) == 2:
            return Ty(ref, None, KAFKA_CONSUMER, args)
        if entry is not None:
            return Ty(ref, None, entry[0], args)
        if ref.name in ("IOptions", "IOptionsSnapshot", "IOptionsMonitor") and args:
            return Ty(ref, None, Tag("framework", "options", "options"), args)
        return Ty(ref, None, None, args)

    def ty(self, node, depth=0) -> Optional[Ty]:
        node = strip_await(node)
        if node is None or depth > 8:
            return None
        kind = node.type
        if kind == "identifier":
            name = text(node)
            if name in self.env:
                return self.env[name]
            return self._member_ty(self.owner, name)
        if kind == "this_expression":
            return Ty(TypeRef(self.owner.name), self.owner) if self.owner else None
        if kind == "base_expression":
            return self._base_ty()
        if kind in ("object_creation_expression", "implicit_object_creation_expression"):
            type_node = node.child_by_field_name("type")
            if type_node is None:
                return None
            ref = parse_type(type_node)
            result = self.ty_from_ref(ref)
            if result.tag is not None and result.tag.family == "http":
                return result
            return result
        if kind == "member_access_expression":
            return self._member_access_ty(node, depth)
        if kind == "invocation_expression":
            return self._invocation_ty(node, depth)
        if kind == "cast_expression":
            return self.ty_from_ref(parse_type(node.child_by_field_name("type")))
        if kind == "as_expression" or (kind == "binary_expression" and text(node.child_by_field_name("operator")) == "as"):
            right = node.child_by_field_name("right")
            return self.ty_from_ref(parse_type(right)) if right is not None else None
        if kind == "conditional_expression":
            return self.ty(node.named_children[1], depth + 1)
        if kind == "binary_expression" and text(node.child_by_field_name("operator")) == "??":
            return self.ty(node.child_by_field_name("left"), depth + 1)
        if kind == "element_access_expression":
            base = self.ty(node.child_by_field_name("expression"), depth + 1)
            if base is not None and base.args:
                return base.args[-1]
            return None
        if kind == "parenthesized_expression" and node.named_children:
            return self.ty(node.named_children[0], depth + 1)
        if kind in ("postfix_unary_expression",) and node.named_children:
            return self.ty(node.named_children[0], depth + 1)
        if kind == "conditional_access_expression":
            return self.ty(node.named_children[-1], depth + 1) if node.named_children else None
        return None

    def _base_ty(self) -> Optional[Ty]:
        if self.owner is None:
            return None
        for b in self.owner.bases:
            info = self.index.resolve(b.name, self.file, self.namespace, self.owner)
            if info is not None and info.kind != "interface":
                return Ty(b, info)
        return None

    def _member_ty(self, owner: Optional[TypeInfo], name: str) -> Optional[Ty]:
        if owner is None:
            return None
        if name in self.result.field_types:
            return self.result.field_types[name]
        member = self.index.find_member(owner, name)
        if member is None:
            return None
        declared = self.ty_from_ref(member.type, member.owner.file, member.owner.namespace, member.owner)
        refined = self._refined_field_type(member, declared)
        return refined or declared

    def _refined_field_type(self, member, declared: Ty) -> Optional[Ty]:
        """A field initialised in a constructor with something more specific (`_http = factory.CreateClient("x")`)."""
        if member.owner is None or declared.tag is None or declared.tag.family != "http":
            return None
        base = self.A.class_base_url(member.owner)
        if base:
            return declared.with_extra(base_url=base)
        return None

    def _member_access_ty(self, node, depth) -> Optional[Ty]:
        receiver_node = node.child_by_field_name("expression")
        name_node = node.child_by_field_name("name")
        name = text(name_node.named_children[0]) if name_node is not None and name_node.type == "generic_name" else text(name_node)
        # a type name used as a receiver: enum member, static member
        base = self.ty(receiver_node, depth + 1)
        if base is None:
            return None
        if base.info is not None:
            if name == "Value" and base.info.name in ("Lazy",):
                return base.args[0] if base.args else None
            member = self.index.find_member(base.info, name)
            if member is not None:
                result = self.ty_from_ref(member.type, member.owner.file, member.owner.namespace, member.owner, self._bindings_for(base, member.owner))
                refined = self._refined_field_type(member, result)
                return refined or result
            if self._is_dbcontext(base.info):
                return self._tag_member(Ty(base.ref, None, CONTEXT_TAG), name)
            if base.info.qualified in self.app.options_sections or base.extra.get("section"):
                return None
        if base.tag is not None:
            return self._tag_member(base, name)
        return None

    def _bindings_for(self, receiver: Ty, declaring: TypeInfo) -> Dict[str, Ty]:
        mapping = dict(self.binding.map) if self.binding else {}
        if receiver.info is not None and receiver.info.type_params and receiver.args:
            for p, a in zip(receiver.info.type_params, receiver.args):
                mapping[p] = a
        return mapping

    def _tag_member(self, base: Ty, name: str) -> Optional[Ty]:
        tag = base.tag
        if tag.role == "options" and name == "Value" and base.args:
            target = base.args[0]
            section = self.app.options_sections.get(target.info.qualified) if target.info else None
            return target.with_extra(section=section)
        if tag.role == "context" and name == "Database":
            return Ty(TypeRef("DatabaseFacade"), None, Tag("db", "efcore", "database"))
        if tag.tech == "rabbitmq" and name == "DefaultExchange":
            return Ty(TypeRef("exchange"), None, Tag("broker", "rabbitmq", "channel"))
        return None

    def _invocation_ty(self, node, depth) -> Optional[Ty]:
        parts = self._parts(node)
        if parts is None:
            return None
        receiver, name, type_args, args = parts
        receiver_ty = self.ty(receiver, depth + 1) if receiver is not None else None
        if receiver is None:
            target = self.index.find_method(self.owner, name, len(args)) if self.owner else None
            if target is not None:
                return self._return_ty(target, receiver_ty, type_args)
            return None
        if receiver_ty is None:
            static = self._static_type(receiver)
            if static is not None:
                target = self.index.find_method(static, name, len(args))
                return self._return_ty(target, None, type_args) if target else None
            return None
        if receiver_ty.info is not None:
            for impl, impl_ref in self._impls(receiver_ty):
                target = self.index.find_method(impl, name, len(args))
                if target is not None:
                    return self._return_ty(target, receiver_ty, type_args)
            if self._is_dbcontext(receiver_ty.info):
                return self._library_return(receiver_ty, CONTEXT_TAG, name, type_args, args, node)
            return None
        tag = receiver_ty.tag
        if tag is None:
            return None
        return self._library_return(receiver_ty, tag, name, type_args, args, node)

    def _return_ty(self, target: MethodInfo, receiver: Optional[Ty], type_args) -> Optional[Ty]:
        mapping = dict(self.binding.map) if self.binding else {}
        if receiver is not None and receiver.info is not None and receiver.info.type_params and receiver.args:
            mapping.update(dict(zip(receiver.info.type_params, receiver.args)))
        for p, a in zip(target.type_params, type_args):
            mapping[p] = self.ty_from_ref(a)
        return self.ty_from_ref(target.returns, target.file, target.owner.namespace if target.owner else "", target.owner, mapping)

    def _library_return(self, receiver: Ty, tag: Tag, name: str, type_args, args, node) -> Optional[Ty]:
        file, owner = self.file, self.owner
        if name in ("GetRequiredService", "GetService", "GetRequiredKeyedService", "GetKeyedService") and type_args:
            return self.ty_from_ref(type_args[0])
        if tag.family == "http" and tag.role == "factory" and name == "CreateClient":
            client_name = self.const.string(args[0][1], file, owner) if args else None
            config = self.app.http_client_for(None, client_name)
            return Ty(TypeRef("HttpClient"), None, Tag("http", "httpclient", "client"), (), {"base_url": config.base_url if config else None, "client_name": client_name})
        if tag.role == "context" and name == "Set" and type_args:
            return Ty(TypeRef("DbSet", (type_args[0],)), None, Tag("db", "efcore", "dbset"), (self.ty_from_ref(type_args[0]),))
        if tag.role in ("dbset", "query") and name in EF_READ | {"Local"}:
            entity = receiver.args[0] if receiver.args else None
            if entity is not None and name in ("ToList", "ToListAsync", "ToArray", "ToArrayAsync", "AsEnumerable", "ToHashSetAsync", "ToPagedList", "ToPagedListAsync"):
                return Ty(TypeRef("List", (entity.ref,)), None, None, (entity,))
            if entity is not None and name in ("First", "FirstAsync", "FirstOrDefault", "FirstOrDefaultAsync", "Single", "SingleAsync", "SingleOrDefault",
                                               "SingleOrDefaultAsync", "Find", "FindAsync", "Last", "LastAsync", "LastOrDefault", "LastOrDefaultAsync", "ElementAt"):
                return entity
            if name in ("Count", "CountAsync", "LongCount", "LongCountAsync", "Sum", "SumAsync", "Average", "AverageAsync", "Min", "Max", "Any", "AnyAsync",
                        "All", "AllAsync", "Contains"):
                return None
            return receiver
        if tag.tech == "mongodb":
            if name == "GetCollection":
                entity = self.ty_from_ref(type_args[0]) if type_args else None
                collection = self.const.string(args[0][1], file, owner) if args else None
                return Ty(TypeRef("IMongoCollection"), None, Tag("db", "mongodb", "collection"), (entity,) if entity else (), {"collection": collection})
            if name == "GetDatabase":
                return Ty(TypeRef("IMongoDatabase"), None, Tag("db", "mongodb", "database"))
        if tag.tech == "cosmosdb" and name in ("GetContainer", "GetDatabase"):
            container = self.const.string(args[-1][1], file, owner) if args else None
            return Ty(TypeRef("Container"), None, Tag("db", "cosmosdb", "container"), (), {"collection": container})
        if tag.role == "multiplexer" and name == "GetDatabase":
            return Ty(TypeRef("IDatabase"), None, Tag("cache", "redis", "client"))
        if tag.role == "multiplexer" and name == "GetSubscriber":
            return Ty(TypeRef("ISubscriber"), None, Tag("cache", "redis", "subscriber"))
        if tag.tech == "azure-service-bus" and name in ("CreateSender", "CreateProcessor", "CreateReceiver"):
            queue = self.const.string(args[0][1], file, owner) if args else None
            role = "producer" if name == "CreateSender" else "consumer"
            return Ty(TypeRef("ServiceBusSender"), None, Tag("broker", "azure-service-bus", role), (), {"queue": queue})
        if tag.tech == "rabbitmq" and name in ("CreateModel", "CreateChannel", "CreateChannelAsync", "CreateModelAsync"):
            return Ty(TypeRef("IModel"), None, Tag("broker", "rabbitmq", "channel"))
        if tag.family == "storage" and name in ("GetBlobContainerClient", "GetBlobClient", "GetContainerClient"):
            bucket = self.const.string(args[0][1], file, owner) if args else receiver.extra.get("bucket")
            return receiver.with_extra(bucket=bucket)
        if tag.family == "db" and tag.role == "connection" and name in ("CreateCommand", "BeginTransaction"):
            return Ty(TypeRef("DbCommand"), None, Tag("db", tag.tech, "command" if name == "CreateCommand" else "transaction"))
        if tag.family == "db" and tag.role == "connection" and name in ("CreateConnection", "OpenConnection", "OpenConnectionAsync"):
            return receiver
        if tag.family == "http" and name in ("CreateClient",):
            return Ty(TypeRef("HttpClient"), None, Tag("http", "httpclient", "client"))
        if tag.role == "options" and name == "Get" and receiver.args:
            return receiver.args[0]
        return None

    def _is_dbcontext(self, info: TypeInfo) -> bool:
        return self.registry.kind(info) == "dbcontext"

    def _static_type(self, node) -> Optional[TypeInfo]:
        node = unparen(node)
        if node is None or node.type not in ("identifier", "member_access_expression", "qualified_name"):
            return None
        name = text(node)
        if node.type == "identifier" and name in self.env:
            return None
        head = name.rsplit(".", 1)[-1]
        if node.type == "identifier" and self._member_ty(self.owner, name) is not None:
            return None
        return self.index.resolve(head, self.file, self.namespace, self.owner) if head[:1].isupper() else None

    def _impls(self, receiver: Ty) -> List[Tuple[TypeInfo, Optional[TypeRef]]]:
        if receiver.info is None:
            return []
        out = self.app.implementations(receiver.info, receiver.ref)
        return out or [(receiver.info, receiver.ref)]

    def _parts(self, node):
        parts = invocation_parts(node)
        if parts is not None:
            return parts
        function = node.child_by_field_name("function")
        if function is not None and function.type == "member_binding_expression":
            parent = node.parent
            if parent is not None and parent.type == "conditional_access_expression":
                receiver = parent.child_by_field_name("condition") or (parent.named_children[0] if parent.named_children else None)
                name_node = function.child_by_field_name("name")
                if name_node is not None:
                    type_args = []
                    if name_node.type == "generic_name":
                        arguments = first(name_node, "type_argument_list")
                        type_args = [parse_type(c) for c in (arguments.named_children if arguments is not None else [])]
                        return receiver, text(first(name_node, "identifier")), type_args, argument_nodes(node)
                    return receiver, text(name_node), [], argument_nodes(node)
        return None

    # ------------------------------------------------------------------
    # walking
    # ------------------------------------------------------------------

    def run(self) -> MethodAnalysis:
        for p in self.method.params:
            self.env[p.name] = self.ty_from_ref(p.type)
        self._attribute_facts()
        for root in self.method.body:
            for node in walk(root):
                kind = node.type
                if kind == "variable_declarator":
                    self._declarator(node)
                elif kind == "foreach_statement":
                    self._foreach(node)
                elif kind == "assignment_expression":
                    self._assignment(node)
                elif kind in ("declaration_pattern", "recursive_pattern", "var_pattern"):
                    self._pattern(node)
                elif kind == "invocation_expression":
                    self._call(node)
                elif kind in ("object_creation_expression", "implicit_object_creation_expression"):
                    self._creation(node)
                elif kind == "member_access_expression":
                    self._member_access(node)
                elif kind in ("throw_statement", "throw_expression"):
                    self._throw(node)
                elif kind == "local_function_statement":
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self._local_functions[text(name)] = node
        return self.result

    # ---- declarations ----

    def _declarator(self, node):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(name_node)
        init = next((c for c in node.named_children if c.type not in ("identifier", "bracketed_argument_list")), None)
        declaration = node.parent
        declared_type = declaration.child_by_field_name("type") if declaration is not None else None
        if init is not None:
            self.assigned[name] = init
        if declared_type is not None and declared_type.type != "implicit_type":
            resolved = self.ty_from_ref(parse_type(declared_type))
            if (resolved.tag is None and resolved.info is None) and init is not None:
                inferred = self.ty(init)
                if inferred is not None:
                    resolved = inferred
            self.env[name] = resolved
        elif init is not None:
            inferred = self.ty(init)
            if inferred is not None:
                self.env[name] = inferred

    def _foreach(self, node):
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        type_node = node.child_by_field_name("type")
        if type_node is not None and type_node.type != "implicit_type":
            self.env[text(left)] = self.ty_from_ref(parse_type(type_node))
            return
        iterable = self.ty(node.child_by_field_name("right"))
        if iterable is not None and iterable.args:
            self.env[text(left)] = iterable.args[0]

    def _assignment(self, node):
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None:
            return
        target = text(left)
        if target.endswith(".BaseAddress") or target == "BaseAddress":
            creation = unparen(right)
            expression = argument_nodes(creation)[0][1] if creation is not None and creation.type in (
                "object_creation_expression", "implicit_object_creation_expression") and argument_nodes(creation) else right
            url = self.string_of(expression)
            owner_name = target.rsplit(".", 1)[0]
            if url:
                if owner_name in self.env:
                    self.env[owner_name] = self.env[owner_name].with_extra(base_url=url)
                self.result.base_url = url
        if target.endswith(".CommandText"):
            sql = self.string_of(right)
            if sql:
                self._sql_fact(None, sql, node, "CommandText")
        if left.type == "identifier":
            name = text(left)
            self.assigned[name] = right
            inferred = self.ty(right)
            if inferred is not None:
                self.env[name] = inferred
        elif left.type == "member_access_expression" and text(left.child_by_field_name("expression")) == "this":
            name = text(left.child_by_field_name("name"))
            inferred = self.ty(right)
            if inferred is not None:
                declared = self._member_ty(self.owner, name)
                if declared is None or (declared.info is None and declared.tag is None) or inferred.tag is not None:
                    self.result.field_types[name] = inferred
        if left.type == "identifier" and self.owner is not None and text(left) not in self.env:
            member = self.index.find_member(self.owner, text(left))
            inferred = self.ty(right)
            if member is not None and inferred is not None and (inferred.tag is not None or inferred.extra):
                self.result.field_types[text(left)] = inferred

    def _pattern(self, node):
        designation = first(node, "identifier")
        type_node = node.child_by_field_name("type") or (node.named_children[0] if node.named_children else None)
        if designation is not None and type_node is not None and designation is not type_node and type_node.type != "var_pattern":
            self.env[text(designation)] = self.ty_from_ref(parse_type(type_node))

    # ------------------------------------------------------------------
    # facts and edges
    # ------------------------------------------------------------------

    def fact(self, kind: str, node, **fields) -> dict:
        fact = {"kind": kind, **fields, "source": self.A.label(self.method), "line": line_of(node) if node is not None else self.method.line_start,
                "file": self.file.rel, "module": self.file.project}
        self.result.facts.append({k: v for k, v in fact.items() if v is not None})
        return fact

    def edge(self, callee: MethodInfo, binding: Optional[Binding], node):
        key = (node_id(callee, binding), line_of(node))
        if key in self._seen_edges or (callee is self.method and binding is self.binding):
            return
        self._seen_edges.add(key)
        self.result.calls.append(CallEdge(callee, binding, line_of(node)))
        self.result.names.append(callee.name)

    def _attribute_facts(self):
        attrs = list(self.method.attrs)
        owner_attrs = list(self.owner.attrs) if self.owner is not None else []
        for a in attrs + owner_attrs:
            kind = RESILIENCE_ATTRIBUTES.get(a.name)
            if kind is None:
                continue
            level = "class" if a in owner_attrs else "method"
            if kind in ("output-cache", "response-cache"):
                self.fact("cache", a.node, technology=kind, operation="READ_THROUGH", cache_names=[self.method.name], level=level)
            elif kind in ("cors",):
                continue
            else:
                self.fact("resilience", a.node, technology=kind, name=(self.const.string(a.args[0], a.file, self.owner) if a.args else self.method.name), level=level)

    # ---- calls ----

    def _call(self, node):
        parts = self._parts(node)
        if parts is None:
            return
        receiver, name, type_args, args = parts
        pos = positional(args)
        named_args = keyword(args)

        if receiver is None:
            self._unqualified_call(node, name, type_args, args, pos, named_args)
            return

        receiver_ty = self.ty(receiver)
        static = self._static_type(receiver) if receiver_ty is None else None

        if static is None:
            head = text(receiver).rsplit(".", 1)[-1]
            if head in RESULT_RECEIVERS:
                self._result_helper(node, name, args, head)
                return
            if receiver_ty is None and head[:1].isupper() and self._library_static(node, head, name, type_args, args, pos):
                return
        if receiver_ty is None and static is None:
            return self._extension_or_unknown(node, receiver, name, args)

        if static is not None:
            target = self.index.find_method(static, name, len(args))
            if target is not None:
                self.edge(target, self._method_binding(target, None, type_args), node)
            return

        if receiver_ty.tag is not None and receiver_ty.info is None:
            self._library_call(node, receiver_ty, receiver_ty.tag, name, type_args, args, pos, named_args, receiver)
            return

        if receiver_ty.info is not None:
            if self._is_dbcontext(receiver_ty.info) and self.index.find_method(receiver_ty.info, name, len(args)) is None:
                self._library_call(node, receiver_ty, CONTEXT_TAG, name, type_args, args, pos, named_args, receiver)
                return
            self._project_call(node, receiver_ty, name, type_args, args)
            return

        self._extension_or_unknown(node, receiver, name, args)

    def _unqualified_call(self, node, name, type_args, args, pos, named_args):
        # local function
        if name in self._local_functions:
            return
        if self.owner is not None:
            owner_is_controller = self.index.is_a(self.owner, "ControllerBase", "Controller") or self.app.is_controller(self.owner)
            target = self.index.find_method(self.owner, name, len(args))
            if target is None and owner_is_controller and name in RESULT_HELPERS:
                self._result_helper(node, name, args, None)
                return
            if target is not None:
                self.edge(target, self._method_binding(target, Ty(TypeRef(self.owner.name), self.owner, None, tuple(self.binding.map.values()) if self.binding else ()),
                                                       type_args), node)
                return
            # inherited from a library base class (ControllerBase.Ok, Task.Run via using static)
            if name in RESULT_HELPERS and owner_is_controller:
                self._result_helper(node, name, args, None)
                return
        for static in self.file.static_usings:
            t = self.index.types.get(static)
            target = self.index.find_method(t, name, len(args)) if t is not None else None
            if target is not None:
                self.edge(target, None, node)
                return
        if name in RESULT_HELPERS and name in ("Ok", "NotFound", "BadRequest", "Created", "NoContent", "Accepted", "Conflict", "Unauthorized", "Forbid",
                                                "StatusCode", "Problem", "ValidationProblem", "UnprocessableEntity", "CreatedAtAction", "CreatedAtRoute",
                                                "AcceptedAtAction", "File", "Content", "Json", "Redirect", "RedirectToAction"):
            if self.owner is not None and (self.app.is_controller(self.owner) or self.index.is_a(self.owner, "ControllerBase", "Controller", "ControllerBase")):
                self._result_helper(node, name, args, None)

    def _project_call(self, node, receiver: Ty, name, type_args, args):
        info = receiver.info
        # a Refit client: calling the interface method is an HTTP call
        if info.kind == "interface":
            refit = self.A.refit_methods(info)
            if name in refit:
                http_method, path = refit[name]
                config = next((c for c in self.app.http_clients if c.client is info), None)
                self._http_fact("refit", http_method, path, node, config.base_url if config else None)
                return
        # MediatR-style dispatch on project mediator abstractions is handled through the library tag; here: ordinary methods
        found = False
        for impl, impl_ref in self._impls(receiver):
            target = self.index.find_method(impl, name, len(args))
            if target is None:
                continue
            found = True
            self.edge(target, self._method_binding(target, Ty(impl_ref or receiver.ref, impl, None, receiver.args), type_args, impl), node)
            if target.is_ctor:
                continue
        if not found:
            self._extension_or_unknown(node, None, name, args, receiver)

    def _method_binding(self, target: MethodInfo, receiver: Optional[Ty], type_args, impl: Optional[TypeInfo] = None) -> Optional[Binding]:
        """Generic arguments in force inside `target`: those of its type (through the inheritance chain) and of the call."""
        mapping: Dict[str, Ty] = {}
        declaring = target.owner
        if declaring is not None and receiver is not None:
            chain = self._base_chain_map(impl or receiver.info, receiver, declaring)
            mapping.update(chain)
        for p, a in zip(target.type_params, type_args):
            mapping[p] = self.ty_from_ref(a)
        if not mapping:
            return None
        key = ",".join(f"{k}={v.key()}" for k, v in sorted(mapping.items()))
        return Binding(key, mapping)

    def _base_chain_map(self, impl: Optional[TypeInfo], receiver: Ty, declaring: TypeInfo) -> Dict[str, Ty]:
        """Type arguments of `declaring` as seen from `impl` (class OrderRepo : Repository<Order>)."""
        if impl is None:
            return {}
        start = {p: a for p, a in zip(impl.type_params, receiver.args)} if receiver.args else {}
        if impl is declaring:
            return start
        queue = [(impl, start)]
        seen = set()
        while queue:
            current, mapping = queue.pop(0)
            if current.qualified in seen:
                continue
            seen.add(current.qualified)
            for base in current.bases:
                target = self.index.resolve(base.name, current.file, current.namespace, current)
                if target is None:
                    continue
                args = [self._resolve_with(a, current, mapping) for a in base.args]
                next_map = {p: a for p, a in zip(target.type_params, args)}
                if target is declaring:
                    return next_map
                queue.append((target, next_map))
        return {}

    def _resolve_with(self, ref: TypeRef, context: TypeInfo, mapping: Dict[str, Ty]) -> Ty:
        if ref.name in mapping:
            return mapping[ref.name]
        return self.ty_from_ref(ref, context.file, context.namespace, context, mapping)

    def _extension_or_unknown(self, node, receiver_node, name, args, receiver_ty: Optional[Ty] = None):
        candidates = self.index.extensions.get(name, [])
        for m in candidates:
            if len(m.params) - 1 == len(args) or any(p.default is not None for p in m.params):
                self.edge(m, None, node)
                return

    # ------------------------------------------------------------------
    # static library calls
    # ------------------------------------------------------------------

    def _library_static(self, node, head, name, type_args, args, pos) -> bool:
        if head == "Task" and name in ("Run", "Factory", "StartNew"):
            self.fact("async", node, technology="task", target=None)
            return True
        if head in ("BackgroundJob",) and name in ("Enqueue", "Schedule", "ContinueWith"):
            self._hangfire_enqueue(node, args, "hangfire", type_args)
            return True
        if head == "RecurringJob" and name in ("AddOrUpdate", "AddOrUpdateAsync"):
            self._hangfire_recurring(node, args, type_args)
            return True
        if head == "Parallel":
            self.fact("async", node, technology="parallel", target=None)
            return True
        return False

    @staticmethod
    def _lambda_name(lam) -> Optional[str]:
        parameters = lam.child_by_field_name("parameters")
        if parameters is None:
            ident = first(lam, "identifier")
            return text(ident) if ident is not None else None
        if parameters.type in ("implicit_parameter", "identifier"):
            return text(parameters)
        param = first(parameters, "parameter")
        if param is not None:
            return text(param.child_by_field_name("name"))
        ident = first(parameters, "identifier")
        return text(ident) if ident is not None else None

    def _hangfire_enqueue(self, node, args, tech, type_args=()):
        target_label = None
        for _, expression in args:
            if expression.type == "lambda_expression":
                body = expression.child_by_field_name("body")
                parts = invocation_parts(body) if body is not None else None
                if parts and parts[0] is not None:
                    receiver_ty = self.ty(parts[0])
                    if receiver_ty is None and type_args and text(parts[0]) == self._lambda_name(expression):
                        receiver_ty = self.ty_from_ref(type_args[0])
                    if receiver_ty is not None and receiver_ty.info is not None:
                        for impl, impl_ref in self._impls(receiver_ty):
                            target = self.index.find_method(impl, parts[1], len(parts[3]))
                            if target is not None:
                                self.edge(target, None, node)
                                target_label = self.A.label(target)
                    elif receiver_ty is None:
                        static = self._static_type(parts[0])
                        target = self.index.find_method(static, parts[1], len(parts[3])) if static else None
                        if target is not None:
                            self.edge(target, None, node)
                            target_label = self.A.label(target)
        self.fact("messaging", node, technology=tech, direction="publish", destination=target_label or "job", call="BackgroundJob.Enqueue")

    def _hangfire_recurring(self, node, args, type_args=()):
        job_id = self.const.string(args[0][1], self.file, self.owner) if args else None
        cron = next((self.const.string(e, self.file, self.owner) for _, e in args if e.type == "string_literal" and e is not args[0][1]), None)
        target = None
        for _, expression in args:
            if expression.type == "lambda_expression":
                body = expression.child_by_field_name("body")
                parts = invocation_parts(body) if body is not None else None
                if parts and parts[0] is not None:
                    receiver_ty = self.ty(parts[0])
                    if receiver_ty is None and type_args and text(parts[0]) == self._lambda_name(expression):
                        receiver_ty = self.ty_from_ref(type_args[0])
                    if receiver_ty is not None and receiver_ty.info is not None:
                        for impl, _ in self._impls(receiver_ty):
                            target = self.index.find_method(impl, parts[1], len(parts[3]))
                            if target is not None:
                                break
        if target is not None:
            self.A.scheduled.append({
                "handler": self.A.label(target), "method_id": target.id, "module": self.file.project, "file": self.file.rel, "line": line_of(node),
                "technology": "hangfire", "schedule": {k: v for k, v in {"id": job_id, "cron": cron}.items() if v},
            })

    # ------------------------------------------------------------------
    # result helpers (status codes)
    # ------------------------------------------------------------------

    def _result_helper(self, node, name, args, receiver_name):
        status = RESULT_HELPERS.get(name)
        pos = positional(args)
        named_args = keyword(args)
        if name in ("StatusCode",):
            status = self.const.integer(pos[0], self.file, self.owner) if pos else None
            status = status or self._status_value(pos[0] if pos else None)
        elif name == "Problem":
            status = self._status_value(named_args.get("statusCode")) or (self.const.integer(pos[2], self.file, self.owner) if len(pos) > 2 else None) or 500
        elif name == "ValidationProblem":
            status = 400
        if name in ("Json", "Text", "Content", "File", "Stream", "Bytes") and "statusCode" in named_args:
            status = self._status_value(named_args["statusCode"]) or status
        if status is None:
            return
        body = None
        if name in ("Ok", "Accepted", "Json", "AcceptedAtAction", "AcceptedAtRoute") and pos:
            body = self.ty(pos[0] if name != "AcceptedAtAction" else pos[-1])
        elif name in ("Created", "CreatedAtAction", "CreatedAtRoute") and len(pos) >= 2:
            body = self.ty(pos[-1])
        elif name == "StatusCode" and len(pos) >= 2:
            body = self.ty(pos[1])
        self.result.returns.append({"status": status, "body": body, "helper": name, "line": line_of(node), "typed": receiver_name == "TypedResults"})
        self.result.statuses.append({"code": status, "source": f"{receiver_name or 'ControllerBase'}.{name}", "line": line_of(node)})

    def _status_value(self, node) -> Optional[int]:
        if node is None:
            return None
        value = self.const.value(node, self.file, self.owner)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return status_from_member(text(unparen(node)).replace("(int)", "").strip("() "))

    # ------------------------------------------------------------------
    # object creation
    # ------------------------------------------------------------------

    def _creation(self, node):
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return
        ref = parse_type(type_node)
        name = ref.name
        args = argument_nodes(node)
        pos = positional(args)
        if name in CREATION_STATUS:
            status = CREATION_STATUS[name]
            if name in ("StatusCodeResult", "ObjectResult"):
                status = self._status_value(pos[0] if pos else None) or status
            body = self.ty(pos[-1]) if pos and name.endswith(("ObjectResult", "CreatedResult", "JsonResult")) else None
            self.result.returns.append({"status": status, "body": body, "helper": name, "line": line_of(node), "typed": False})
            self.result.statuses.append({"code": status, "source": f"new {name}", "line": line_of(node)})
            return
        if name in ("StatusCodeResult", "ObjectResult"):
            status = self._status_value(pos[0] if pos else None)
            initializer = first(node, "initializer_expression")
            if status is None and initializer is not None:
                for a in initializer.named_children:
                    if a.type == "assignment_expression" and text(a.child_by_field_name("left")) == "StatusCode":
                        status = self._status_value(a.child_by_field_name("right"))
            if status:
                body = self.ty(pos[0]) if name == "ObjectResult" and pos else None
                self.result.returns.append({"status": status, "body": body, "helper": name, "line": line_of(node), "typed": False})
                self.result.statuses.append({"code": status, "source": f"new {name}", "line": line_of(node)})
            return
        if name in ("SqlCommand", "NpgsqlCommand", "MySqlCommand", "OracleCommand", "SqliteCommand", "OleDbCommand") and pos:
            sql = self.string_of(pos[0])
            if sql:
                tech = {"SqlCommand": "sqlserver", "NpgsqlCommand": "postgresql", "MySqlCommand": "mysql", "OracleCommand": "oracle",
                        "SqliteCommand": "sqlite"}.get(name, "sql")
                self._sql_fact(tech, sql, node, f"new {name}")
            return
        info = self.index.resolve(name, self.file, self.namespace, self.owner)
        if info is None:
            return
        self.result.instantiated.append(info)
        if self.registry.kind(info) in ("model", "entity", "exception", "enum", "interface"):
            return
        for impl, _ in [(info, ref)]:
            for ctor in impl.ctors:
                if len(ctor.params) == len(pos) or not ctor.params:
                    self.edge(ctor, self._method_binding(ctor, Ty(ref, info, None, tuple(self.ty_from_ref(a) for a in ref.args)), []), node)
                    break

    # ------------------------------------------------------------------
    # exceptions
    # ------------------------------------------------------------------

    def _throw(self, node):
        expression = next((c for c in node.named_children), None)
        if expression is None:
            return
        expression = unparen(expression)
        if expression.type in ("object_creation_expression", "implicit_object_creation_expression"):
            ref = parse_type(expression.child_by_field_name("type"))
        elif expression.type == "identifier" and text(expression) in self.env:
            ref = self.env[text(expression)].ref
        else:
            return
        info = self.index.resolve(ref.name, self.file, self.namespace, self.owner)
        explicit = None
        args = positional(argument_nodes(expression)) if expression.type != "identifier" else []
        for a in args:
            value = self._status_value(a)
            if value and 100 <= value <= 599:
                explicit = value
        self.result.thrown.append({"name": ref.name, "info": info, "status": explicit, "line": line_of(node)})

    # ------------------------------------------------------------------
    # member access (DbSet roots)
    # ------------------------------------------------------------------

    def _member_access(self, node):
        if node.start_byte in self._handled:
            return
        receiver = node.child_by_field_name("expression")
        name_node = node.child_by_field_name("name")
        if receiver is None or name_node is None or name_node.type == "generic_name":
            return
        resulting = self._member_access_ty(node, 0)
        if resulting is None or resulting.tag is None:
            return
        if resulting.tag.role == "dbset" and resulting.args:
            self._handled.add(node.start_byte)
            self._ef_fact(node, resulting.args[0], self._chain_methods(node))
        elif resulting.tag.role == "dbset":
            self._handled.add(node.start_byte)

    @staticmethod
    def _chain_methods(node) -> List[Tuple[str, object]]:
        """Methods chained after an expression: `Orders.Where(..).Include(..).ToListAsync()` -> [(Where, call), ...]."""
        names = []
        current = node
        for _ in range(20):
            parent = current.parent
            while parent is not None and parent.type in ("await_expression", "parenthesized_expression"):
                current, parent = parent, parent.parent
            if parent is None or parent.type != "member_access_expression" or parent.child_by_field_name("expression") != current:
                break
            name_node = parent.child_by_field_name("name")
            method = text(first(name_node, "identifier")) if name_node is not None and name_node.type == "generic_name" else text(name_node)
            grand = parent.parent
            if grand is not None and grand.type == "invocation_expression" and grand.child_by_field_name("function") == parent:
                names.append((method, grand))
                current = grand
            else:
                names.append((method, None))
                break
        return names

    # ------------------------------------------------------------------
    # library calls by family
    # ------------------------------------------------------------------

    def _library_call(self, node, receiver: Ty, tag: Tag, name, type_args, args, pos, named_args, receiver_node):
        family = tag.family
        if family == "db":
            self._db_call(node, receiver, tag, name, type_args, args, pos, named_args)
        elif family == "cache":
            self._cache_call(node, receiver, tag, name, type_args, args, pos)
        elif family == "broker":
            self._broker_call(node, receiver, tag, name, type_args, args, pos, named_args)
        elif family == "http":
            self._http_client_call(node, receiver, tag, name, type_args, args, pos, named_args)
        elif family == "storage":
            self._storage_call(node, receiver, tag, name, args, pos, named_args)
        elif family == "email":
            if name in EMAIL_METHODS or name.startswith("Send"):
                self.fact("email", node, technology=tag.tech, operation="WRITE", call=f"{tag.tech}.{name}")
        elif family == "search":
            operation = "DELETE" if name in SEARCH_DELETE else "WRITE" if name in SEARCH_WRITE else "READ" if name in SEARCH_READ else None
            if operation:
                index_name = self.string_of(named_args["index"]) if "index" in named_args else (self.string_of(pos[0]) if pos and pos[0].type == "string_literal" else None)
                self.fact("search", node, technology=tag.tech, operation=operation, call=f"{tag.tech}.{name}", index=index_name)
        elif family == "framework" and tag.tech in ("mediatr", "wolverine", "dispatcher"):
            self._mediator_call(node, name, type_args, args, pos)

    # ---- databases ----

    def _entity_of(self, ty: Optional[Ty]) -> Optional[TypeInfo]:
        if ty is None:
            return None
        if ty.info is not None and ty.info.qualified in self.registry.entities():
            return ty.info
        if ty.args:
            return self._entity_of(ty.args[0])
        return None

    def _ef_fact(self, node, entity_ty: Ty, chain, operation_override: Optional[str] = None, sql: Optional[str] = None):
        entity = self._entity_of(entity_ty) or (entity_ty.info if entity_ty.info is not None else None)
        methods = [m for m, _ in chain]
        operation = operation_override
        if operation is None:
            operation = "DELETE" if any(m in EF_DELETE for m in methods) else "WRITE" if any(m in EF_WRITE for m in methods) else "READ"
        table = self.registry.table_name(entity) if entity is not None and entity.qualified in self.registry.entities() else None
        raw = next((c for m, c in chain if m in EF_RAW_READ and c is not None), None)
        raw_sql = None
        if raw is not None:
            raw_args = argument_nodes(raw)
            raw_sql = self.string_of(raw_args[0][1]) if raw_args else None
        tables = [table] if table else []
        if raw_sql and looks_like_sql(raw_sql):
            tables = parse_sql(raw_sql)[1] or tables
        context = None
        self.fact("database", node, technology="efcore", operation=operation, call=f"DbSet<{entity.name if entity else str(entity_ty.ref)}>.{methods[-1] if methods else 'Query'}",
                  entity=entity.name if entity else None, table=tables or None, query=raw_sql[:300] if raw_sql else None, context=context)

    def _db_call(self, node, receiver: Ty, tag: Tag, name, type_args, args, pos, named_args):
        role, tech = tag.role, tag.tech
        if role == "context":
            if name == "Set" and type_args:
                entity = self.ty_from_ref(type_args[0])
                self._ef_fact(node, entity, self._chain_methods(node))
            elif name in EF_COMMIT:
                self.fact("transaction", node, technology="efcore", operation="COMMIT")
            elif name in ("Add", "AddAsync", "Update", "Attach", "Remove", "AddRange", "AddRangeAsync", "UpdateRange", "RemoveRange") and pos:
                entity_ty = self.ty(pos[0])
                entity = self._entity_of(entity_ty)
                if entity is not None:
                    operation = "DELETE" if name.startswith("Remove") else "WRITE"
                    self._ef_fact(node, entity_ty, [(name, node)], operation)
            elif name in EF_TX:
                self.fact("transaction", node, technology="efcore", operation="BEGIN")
            elif name in EF_RAW_READ | EF_RAW_WRITE:
                self._raw_sql_call(node, name, args)
            return
        if role == "database":
            if name in EF_RAW_WRITE | EF_RAW_READ:
                self._raw_sql_call(node, name, args)
            elif name in EF_TX or name in ("CommitTransaction", "RollbackTransaction", "CommitTransactionAsync"):
                self.fact("transaction", node, technology="efcore", operation="BEGIN" if name in EF_TX else "COMMIT")
            return
        if role in ("dbset", "query"):
            entity_ty = receiver.args[0] if receiver.args else receiver
            chain = [(name, node)] + self._chain_methods(node)
            if node.start_byte not in self._handled:
                self._ef_fact(node, entity_ty, chain)
            return
        if tech == "mongodb":
            self._mongo_call(node, receiver, name, args)
            return
        if tech == "cosmosdb":
            operation = "DELETE" if name in COSMOS_DELETE else "WRITE" if name in COSMOS_WRITE else "READ" if name in COSMOS_READ else None
            if operation:
                self.fact("database", node, technology="cosmosdb", operation=operation, call=f"Container.{name}",
                          table=[receiver.extra["collection"]] if receiver.extra.get("collection") else None)
            return
        if tech in ("dynamodb", "marten", "ravendb", "nhibernate", "azure-table"):
            operation = "DELETE" if "Delete" in name or name == "Remove" else "WRITE" if any(w in name for w in ("Save", "Put", "Store", "Insert", "Update", "Add")) else \
                "READ" if any(r in name for r in ("Load", "Get", "Query", "Find", "Scan", "Read")) else None
            if operation:
                self.fact("database", node, technology=tech, operation=operation, call=f"{tech}.{name}")
            return
        # ADO.NET and Dapper on a connection / command
        if role in ("connection", "transaction") and (name in DAPPER_METHODS) and not (name in ("Get", "GetAsync", "GetAll", "GetAllAsync", "Insert", "Update", "Delete",
                                                                                          "InsertAsync", "UpdateAsync", "DeleteAsync") and not pos):
            sql_node = named_args.get("sql") or (pos[0] if pos else None)
            sql = self.string_of(sql_node) if sql_node is not None else None
            if sql and looks_like_sql(sql):
                self._sql_fact(tech, sql, node, f"Dapper.{name}")
            elif name in ("Insert", "InsertAsync", "Update", "UpdateAsync", "Delete", "DeleteAsync", "Get", "GetAsync", "GetAll", "GetAllAsync"):
                entity = self._entity_of(self.ty(pos[0])) if pos else None
                self.fact("database", node, technology="dapper", operation="DELETE" if name.startswith("Delete") else "WRITE" if name.startswith(("Insert", "Update")) else "READ",
                          call=f"Dapper.{name}", entity=entity.name if entity else None)
            return
        if role == "command" and name in COMMAND_EXECUTE:
            return                                                  # the SQL was captured where the command was created
        if name in ("Commit", "Rollback", "CommitAsync", "RollbackAsync"):
            self.fact("transaction", node, technology=tech, operation=name.replace("Async", "").upper())

    def _raw_sql_call(self, node, name, args):
        sql = self.string_of(args[0][1]) if args else None
        if sql and looks_like_sql(sql):
            operation, tables = parse_sql(sql)
            self.fact("database", node, technology="efcore", operation=operation or ("WRITE" if name in EF_RAW_WRITE else "READ"), call=f"Database.{name}",
                      table=tables or None, query=sql[:300])
        else:
            self.fact("database", node, technology="efcore", operation="WRITE" if name in EF_RAW_WRITE else "READ", call=f"Database.{name}")

    def _sql_fact(self, tech: Optional[str], sql: str, node, call: str):
        operation, tables = parse_sql(sql)
        self.fact("database", node, technology=tech or "sql", operation=operation or "READ", call=call, table=tables or None, query=sql[:300])

    def _mongo_call(self, node, receiver: Ty, name, args):
        operation = "DELETE" if name in MONGO_DELETE else "WRITE" if name in MONGO_WRITE else "READ" if name in MONGO_READ else None
        if operation is None:
            return
        entity = receiver.args[0] if receiver.args and receiver.args[0] is not None else None
        collection = receiver.extra.get("collection") or (entity.ref.name if entity else None)
        self.fact("database", node, technology="mongodb", operation=operation, call=f"IMongoCollection.{name}", table=[collection] if collection else None,
                  entity=entity.ref.name if entity else None)

    # ---- caches ----

    def _cache_call(self, node, receiver: Ty, tag: Tag, name, type_args, args, pos):
        key = self.string_of(pos[0]) if pos and pos[0].type in ("string_literal", "interpolated_string_expression", "identifier", "binary_expression") else None
        if tag.tech == "redis" and tag.role == "multiplexer":
            return
        if tag.tech == "redis" and tag.role == "subscriber":
            if name in ("Subscribe", "SubscribeAsync"):
                self._listener(Tag("broker", "redis", "consumer"), [key] if key else [], node)
            elif name in REDIS_PUBLISH:
                self.fact("messaging", node, technology="redis", direction="publish", destination=key, call=f"redis.{name}")
            return
        if tag.tech == "redis":
            if name in REDIS_PUBLISH:
                self.fact("messaging", node, technology="redis", direction="publish", destination=key, call=f"redis.{name}")
                return
            operation = "DELETE" if name in REDIS_DELETE else "WRITE" if name in REDIS_WRITE else "READ" if name in REDIS_READ else None
            if operation is None:
                return
            self.fact("cache", node, technology="redis", operation=operation, command=name, key=key)
            return
        if name in CACHE_THROUGH:
            self.fact("cache", node, technology=tag.tech, operation="READ_THROUGH", command=name, key=key)
            return
        operation = "DELETE" if name in DISTRIBUTED_DELETE else "WRITE" if name in DISTRIBUTED_WRITE else "READ" if name in DISTRIBUTED_READ else None
        if operation:
            self.fact("cache", node, technology=tag.tech, operation=operation, command=name, key=key)

    # ---- brokers ----

    def _message_type_name(self, expression) -> Optional[str]:
        if expression is None:
            return None
        ty = self.ty(expression)
        if ty is not None:
            return ty.ref.name
        return None

    def _broker_call(self, node, receiver: Ty, tag: Tag, name, type_args, args, pos, named_args):
        tech, role = tag.tech, tag.role
        if tech == "hangfire":
            if role == "queue" and name in ("Enqueue", "Schedule", "ContinueWith"):
                self._hangfire_enqueue(node, args, "hangfire")
            elif role == "scheduler" and name in ("AddOrUpdate", "AddOrUpdateAsync"):
                self._hangfire_recurring(node, args)
            return
        if tech == "quartz":
            if name in ("ScheduleJob", "AddJob", "TriggerJob"):
                self.fact("async", node, technology="quartz", target=None)
            return
        if tech == "masstransit":
            if name in ("Publish", "PublishBatch"):
                message = type_args[0].name if type_args else self._message_type_name(pos[0] if pos else None)
                self.fact("messaging", node, technology="masstransit", direction="publish", destination=message, call=f"IPublishEndpoint.{name}", payload_type=message)
            elif name in ("Send", "SendBatch", "GetSendEndpoint"):
                message = type_args[0].name if type_args else self._message_type_name(pos[0] if pos and name != "GetSendEndpoint" else None)
                address = self.string_of(pos[0]) if name == "GetSendEndpoint" and pos else None
                if address is None and name == "GetSendEndpoint" and pos:
                    creation = unparen(pos[0])
                    inner = argument_nodes(creation) if creation is not None and creation.type in ("object_creation_expression", "implicit_object_creation_expression") else []
                    address = self.string_of(inner[0][1]) if inner else None
                destination = re.sub(r"^queue:|^exchange:", "", address) if address else message
                self.fact("messaging", node, technology="masstransit", direction="publish", destination=destination, call=f"ISendEndpoint.{name}", payload_type=message)
            elif name in ("GetResponse", "Request"):
                message = type_args[0].name if type_args else None
                self.fact("messaging", node, technology="masstransit", direction="request", destination=receiver.args[0].ref.name if receiver.args else message,
                          call="IRequestClient.GetResponse")
            return
        if tech == "nservicebus":
            if name in ("Send", "Publish", "SendLocal"):
                message = type_args[0].name if type_args else self._message_type_name(pos[0] if pos else None)
                self.fact("messaging", node, technology="nservicebus", direction="publish", destination=message, call=f"IMessageSession.{name}", payload_type=message)
            return
        methods = PUBLISH_METHODS.get(tech, set())
        if role == "consumer" and name in ("Subscribe", "StartProcessingAsync", "ReceiveMessageAsync", "ReceiveMessagesAsync", "Consume"):
            topics = []
            for p in pos[:1]:
                p = unparen(p)
                if p.type in ("array_creation_expression", "collection_expression", "implicit_array_creation_expression", "initializer_expression"):
                    topics.extend(self.const.strings(p, self.file, self.owner))
                else:
                    value = self.string_of(p)
                    if value:
                        topics.append(value)
            if receiver.extra.get("queue"):
                topics.append(receiver.extra["queue"])
            self._listener(tag, topics, node)
            return
        if tech == "rabbitmq" and name in ("BasicConsume", "BasicConsumeAsync"):
            queue = self.string_of(named_args["queue"]) if "queue" in named_args else (self.string_of(pos[0]) if pos else None)
            self._listener(tag, [queue] if queue else [], node)
            return
        if name not in methods:
            return
        destination = None
        if tech == "kafka":
            first_arg = pos[0] if pos else named_args.get("topic")
            destination = self.string_of(first_arg) if first_arg is not None and first_arg.type != "object_creation_expression" else None
            if destination is None and first_arg is not None and first_arg.type == "object_creation_expression":
                inner = argument_nodes(first_arg)
                destination = self.string_of(inner[0][1]) if inner else None
        elif tech == "rabbitmq":
            routing = named_args.get("routingKey") or (pos[1] if len(pos) > 1 else None)
            exchange = named_args.get("exchange") or (pos[0] if pos else None)
            destination = (self.string_of(routing) if routing is not None else None) or (self.string_of(exchange) if exchange is not None else None)
        elif tech == "azure-service-bus":
            destination = receiver.extra.get("queue")
        elif tech == "sqs":
            url = self._request_property(args, "QueueUrl") or (self.string_of(pos[0]) if pos else None)
            destination = url.rstrip("/").rsplit("/", 1)[-1] if url else None
        elif tech == "sns":
            arn = self._request_property(args, "TopicArn") or (self.string_of(pos[0]) if pos else None)
            destination = arn.rsplit(":", 1)[-1] if arn else None
        elif tech == "dapr":
            destination = self.string_of(pos[1]) if len(pos) > 1 else None
        else:
            destination = self.string_of(pos[0]) if pos else None
        self.fact("messaging", node, technology=tech, direction="publish", destination=destination, call=f"{tech}.{name}")

    def _request_property(self, args, prop: str) -> Optional[str]:
        """`new SendMessageRequest { QueueUrl = "..." }` passed to a publish call."""
        for _, expression in args:
            value = unparen(expression)
            if value is None:
                continue
            if value.type == "identifier" and text(value) in self.assigned:
                value = unparen(self.assigned[text(value)])
            for n in walk(value):
                if n.type == "assignment_expression" and text(n.child_by_field_name("left")) == prop:
                    return self.string_of(n.child_by_field_name("right"))
        return None

    def _listener(self, tag: Tag, destinations: List[str], node):
        entry = {
            "handler": self.A.label(self.method), "method_id": self.method.id, "module": self.file.project, "file": self.file.rel,
            "line": self.method.line_start, "technology": tag.tech, "destinations": [d for d in destinations if d], "kind": "consumer",
        }
        if entry not in self.A.listeners:
            self.A.listeners.append(entry)

    # ---- mediator ----

    def _mediator_call(self, node, name, type_args, args, pos):
        if name not in ("Send", "Publish", "CreateStream", "SendAsync", "InvokeAsync", "PublishAsync", "Invoke"):
            return
        message_ty = self.ty(pos[0]) if pos else None
        message = message_ty.ref.name if message_ty is not None else (type_args[0].name if type_args else None)
        if message is None:
            return
        handlers = self.A.message_handlers().get(message, [])
        dispatched = False
        for handler_type, handle, kind in handlers:
            if kind in ("request", "stream") and name in ("Send", "CreateStream", "SendAsync", "InvokeAsync", "Invoke") or kind == "notification" and name in ("Publish", "PublishAsync"):
                self.edge(handle, None, node)
                dispatched = True
        if name in ("Publish", "PublishAsync"):
            self.fact("event", node, technology="mediatr", direction="publish", event_type=message, handled=dispatched)

    # ---- HTTP ----

    def _http_client_call(self, node, receiver: Ty, tag: Tag, name, type_args, args, pos, named_args):
        if tag.role == "factory":
            return
        if name not in HTTP_CLIENT_METHODS and not (tag.tech in ("restsharp", "webclient")):
            return
        base = receiver.extra.get("base_url")
        if base is None and self.owner is not None:
            base = self.A.class_base_url(self.owner)
        verb = None
        for prefix, http in (("Get", "GET"), ("Post", "POST"), ("Put", "PUT"), ("Patch", "PATCH"), ("Delete", "DELETE"), ("Head", "HEAD"),
                             ("Download", "GET"), ("Upload", "POST"), ("ExecuteGet", "GET"), ("ExecutePost", "POST")):
            if name.startswith(prefix):
                verb = http
                break
        url_node = pos[0] if pos else named_args.get("requestUri") or named_args.get("url")
        if name in ("SendAsync", "Send", "ExecuteAsync"):
            request = unparen(url_node) if url_node is not None else None
            if request is not None and request.type == "identifier" and text(request) in self.assigned:
                request = unparen(self.assigned[text(request)])
            if request is not None and request.type in ("object_creation_expression", "implicit_object_creation_expression"):
                inner = argument_nodes(request)
                verb_node = inner[0][1] if inner else None
                verb = (text(verb_node).rsplit(".", 1)[-1].upper() if verb_node is not None and "HttpMethod" in text(verb_node) else None) or "ANY"
                url_node = inner[1][1] if len(inner) > 1 else None
            else:
                url_node = None
        url = self.string_of(url_node) if url_node is not None else None
        self._http_fact(tag.tech, verb or "ANY", url, node, base, response_type=type_args[0].name if type_args else None)

    def _http_fact(self, client: str, http_method: str, url: Optional[str], node, base: Optional[str], response_type=None):
        if url:
            url = self.A.resolve_placeholders(self.file, url)
        if base:
            base = self.A.resolve_placeholders(self.file, base)
        full = url
        if base and (url is None or not re.match(r"^\w+://", url or "")):
            full = (base.rstrip("/") + "/" + (url or "").lstrip("/")) if url else base
        info = self._split_url(full)
        self.fact("http", node, client=client, http_method=http_method, url=full, host=info["host"], base_url=base, target_service=info["service"],
                  config_ref=info["config"], response_type=response_type)

    @staticmethod
    def _split_url(url: Optional[str]) -> dict:
        out = {"host": None, "service": None, "config": None}
        if not url:
            return out
        placeholder = re.match(r"^\$\{([^}]+)\}", url) or re.match(r"^\{([^}]+)\}", url)
        if placeholder:
            out["config"] = placeholder.group(1)
            return out
        match = re.match(r"^\w+://([^/?#]+)", url)
        if not match:
            return out
        host = match.group(1).rsplit("@", 1)[-1]
        if "{" in host or "$" in host:
            out["config"] = host
            return out
        host = host.split(":")[0]
        out["host"] = host
        if "." not in host and host not in ("localhost", "127", "0"):
            out["service"] = host
        return out

    # ---- storage ----

    def _storage_call(self, node, receiver: Ty, tag: Tag, name, args, pos, named_args):
        operation = "DELETE" if name in STORAGE_DELETE else "WRITE" if name in STORAGE_WRITE else "READ" if name in STORAGE_READ else None
        if operation is None:
            return
        bucket = receiver.extra.get("bucket")
        bucket = self._request_property(args, "BucketName") or bucket
        if bucket is None and tag.tech in ("s3", "minio", "gcs") and pos and pos[0].type == "string_literal":
            bucket = self.string_of(pos[0])
        self.fact("storage", node, technology=tag.tech, operation=operation, call=f"{tag.tech}.{name}", bucket=bucket)

    # ------------------------------------------------------------------
    # strings
    # ------------------------------------------------------------------

    def string_of(self, node) -> Optional[str]:
        """String value of an expression; locals are looked through, options become `${Section:Key}`, unknown parts stay as {expr}."""
        node = unparen(strip_await(node))
        if node is None:
            return None
        if node.type == "identifier" and text(node) in self.assigned and self.assigned[text(node)] is not node:
            value = self.string_of(self.assigned[text(node)])
            if value is not None:
                return value
        if node.type == "member_access_expression":
            placeholder = self._options_placeholder(node)
            if placeholder:
                return placeholder
        if node.type == "interpolated_string_expression":
            out = []
            for child in node.children:
                if child.type in ("string_content", "interpolated_string_text", "string_literal_content", "escape_sequence"):
                    out.append(text(child))
                elif child.type == "interpolation":
                    inner = next((c for c in child.named_children if c.type not in INTERPOLATION_PUNCTUATION), None)
                    value = self.string_of(inner) if inner is not None else None
                    out.append(value if value is not None else "{" + (text(inner) if inner is not None else "") + "}")
            return "".join(out)
        if node.type == "binary_expression" and text(node.child_by_field_name("operator")) == "+":
            left = self.string_of(node.child_by_field_name("left"))
            right = self.string_of(node.child_by_field_name("right"))
            if left is not None or right is not None:
                return (left if left is not None else "{" + text(node.child_by_field_name("left")) + "}") + \
                    (right if right is not None else "{" + text(node.child_by_field_name("right")) + "}")
        if node.type in ("object_creation_expression", "implicit_object_creation_expression"):
            inner = argument_nodes(node)
            if inner and text(node.child_by_field_name("type")) in ("Uri", "System.Uri"):
                return self.string_of(inner[0][1])
        value = self.const.value(node, self.file, self.owner, env=None)
        if isinstance(value, str):
            return value
        if value is UNKNOWN and node.type in ("identifier", "member_access_expression"):
            return "{" + text(node) + "}"
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    def _options_placeholder(self, node) -> Optional[str]:
        """`_options.Value.BaseUrl` / `_settings.BaseUrl` where the class is bound to a configuration section."""
        receiver = node.child_by_field_name("expression")
        name = text(node.child_by_field_name("name"))
        base = self.ty(receiver)
        if base is None or base.info is None:
            return None
        section = base.extra.get("section") or self.app.options_sections.get(base.info.qualified)
        if section:
            return "${" + section + ":" + name + "}"
        return None
