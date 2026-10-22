"""
The FastAPI application structure: apps, routers, which handlers are registered where, how routers are
included into each other, and so the effective URL, dependencies and tags of every route.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .catalog import APP_CLASSES, MIDDLEWARE_NAMES, ROUTER_CLASSES, ROUTE_DECORATORS
from .pyindex import (
    ConstEvaluator, FuncInfo, ModuleInfo, PythonIndex, UNKNOWN, call_parts, dotted, keyword_args, line_of,
    positional_args, strip_type, text, walk,
)
from .typeexpr import Meta

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class RouterInfo:
    id: str
    kind: str                         # app | router
    module: ModuleInfo
    var: str
    line: int
    node: object                      # the constructor call
    scope: Optional[FuncInfo] = None  # factory function that creates it
    prefix: str = ""
    tags: List[str] = field(default_factory=list)
    dependencies: List[Meta] = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    root_path: Optional[str] = None
    routes: List["Registration"] = field(default_factory=list)
    includes: List["Include"] = field(default_factory=list)
    parents: List["Include"] = field(default_factory=list)
    end_line: int = 0
    title: Optional[str] = None
    version: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.module.short}.{self.var}"


@dataclass(eq=False)
class Include:
    parent: RouterInfo
    child: RouterInfo
    prefix: str
    tags: List[str]
    dependencies: List[Meta]
    module: ModuleInfo
    line: int
    kind: str = "include"             # include | mount
    end_line: int = 0
    responses: object = None          # `responses=` node given to include_router


@dataclass(eq=False)
class Registration:
    router: Optional[RouterInfo]
    func: FuncInfo
    methods: List[str]
    path: str
    kwargs: dict                      # decorator keyword arguments (nodes)
    module: ModuleInfo                # module the decorator / add_api_route call is in
    line: int
    kind: str = "http"                # http | websocket
    via: str = "decorator"            # decorator | add_api_route


@dataclass
class Placement:
    prefix: str
    app: Optional[RouterInfo]
    dependencies: List[Tuple[str, Meta]]       # (level, Depends call)
    tags: List[str]
    chain: List[RouterInfo]
    includes: List["Include"] = field(default_factory=list)
    responses: List[Tuple[object, ModuleInfo]] = field(default_factory=list)       # `responses=` dicts from app / include / router


@dataclass(eq=False)
class ExceptionHandler:
    exception: object                 # node of the exception class (or status code)
    module: ModuleInfo
    func: Optional[FuncInfo]
    app: Optional[RouterInfo]
    line: int


@dataclass(eq=False)
class MiddlewareInfo:
    name: str
    kind: str
    module: ModuleInfo
    line: int
    options: dict = field(default_factory=dict)
    app: Optional[RouterInfo] = None


@dataclass(eq=False)
class Lifecycle:
    event: str                        # startup | shutdown | lifespan
    func: FuncInfo
    module: ModuleInfo
    line: int


class AppModel:

    def __init__(self, index: PythonIndex, const: ConstEvaluator):
        self.index = index
        self.const = const
        self.routers: Dict[Tuple[str, str], RouterInfo] = {}       # (module name, var key) -> router
        self.router_list: List[RouterInfo] = []
        self.registrations: List[Registration] = []
        self.includes: List[Include] = []
        self.exception_handlers: List[ExceptionHandler] = []
        self.middleware: List[MiddlewareInfo] = []
        self.lifecycle: List[Lifecycle] = []
        self.static_mounts: List[dict] = []
        self.placements: Dict[Registration, List[Placement]] = {}
        self._func_at: Dict[Tuple[str, int], FuncInfo] = {}

    # ------------------------------------------------------------------
    def build(self):
        for module in self.index.modules.values():
            for f in module.all_functions:
                self._func_at[(module.name, f.node.start_byte)] = f
        for module in self.index.modules.values():
            self._find_routers(module)
        for module in self.index.modules.values():
            self._find_registrations(module)
        for module in self.index.modules.values():
            self._find_calls(module)
        self._place()

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def router_kind(self, call_node, module: ModuleInfo) -> Optional[str]:
        func, _ = call_parts(call_node)
        if func is None:
            return None
        symbol = self.index.resolve_expr(func, module)
        if symbol.kind == "external" and symbol.qualified:
            if APP_CLASSES.match(symbol.qualified):
                return "app"
            if ROUTER_CLASSES.match(symbol.qualified):
                return "router"
        elif symbol.kind == "class" and not symbol.rest:
            for name in self.index.external_bases(symbol.obj):
                if APP_CLASSES.match(name):
                    return "app"
                if ROUTER_CLASSES.match(name):
                    return "router"
        return None

    def _find_routers(self, module: ModuleInfo):
        # module level
        for name, var in module.variables.items():
            if var.value is None:
                continue
            value = strip_type(var.value)
            if value.type != "call":
                continue
            kind = self.router_kind(value, module)
            if kind:
                self._add_router(kind, module, name, value, None, var.line)

        # factories: `def create_app(): app = FastAPI() ...`, and `self.router = APIRouter()` in classes
        for f in module.all_functions:
            if f.nested:
                continue
            for node in walk(f.node):
                if node.type != "assignment":
                    continue
                left, right = node.child_by_field_name("left"), strip_type(node.child_by_field_name("right"))
                if left is None or right is None or right.type != "call":
                    continue
                kind = self.router_kind(right, module)
                if not kind:
                    continue
                key = text(left) if left.type == "identifier" else (text(left) if text(left).startswith("self.") else None)
                if key:
                    self._add_router(kind, module, key, right, f, line_of(node))

    def _add_router(self, kind, module, key, call_node, scope: Optional[FuncInfo], line):
        scope_key = f"{scope.qualname}:" if scope else ""
        router_id = f"{module.name}:{scope_key}{key}"
        _, args = call_parts(call_node)
        kwargs = keyword_args(args)
        info = RouterInfo(id=router_id, kind=kind, module=module, var=key, line=line, node=call_node, scope=scope, kwargs=kwargs,
                          end_line=call_node.end_point[0] + 1)
        info.prefix = self.const.string(kwargs["prefix"], module) or "" if "prefix" in kwargs else ""
        info.tags = self.const.strings(kwargs["tags"], module) if "tags" in kwargs else []
        info.dependencies = self._depends_list(kwargs.get("dependencies"), module)
        if "root_path" in kwargs:
            info.root_path = self.const.string(kwargs["root_path"], module)
        if "title" in kwargs:
            info.title = self.const.string(kwargs["title"], module)
        if "version" in kwargs:
            info.version = self.const.string(kwargs["version"], module)
        self.routers[(module.name, f"{scope_key}{key}")] = info
        self.router_list.append(info)
        return info

    @staticmethod
    def _depends_list(node, module) -> List[Meta]:
        node = strip_type(node)
        if node is None or node.type not in ("list", "tuple"):
            return []
        return [Meta(c, module) for c in node.named_children if c.type == "call"]

    # ------------------------------------------------------------------
    # resolving a router expression
    # ------------------------------------------------------------------

    def enclosing_function(self, node, module: ModuleInfo) -> Optional[FuncInfo]:
        current = node.parent
        while current is not None:
            if current.type == "function_definition":
                found = self._func_at.get((module.name, current.start_byte))
                if found is not None:
                    return found if not found.nested else found
            current = current.parent
        return None

    def lookup_router(self, node, module: ModuleInfo, scope: Optional[FuncInfo]) -> Optional[RouterInfo]:
        node = strip_type(node)
        if node is None:
            return None
        if node.type == "call":                         # include_router(make_router())
            func, _ = call_parts(node)
            symbol = self.index.resolve_expr(func, module)
            if symbol.kind == "function" and not symbol.rest:
                return self._returned_router(symbol.obj)
            return None
        name = dotted(node)
        if name is None:
            return None

        # factory-local first (including the factory this nested handler lives in)
        scopes = []
        current = scope
        while current is not None:
            scopes.append(current)
            current = None
        for s in scopes:
            parent_name = s.qualname.rsplit(".", 1)[0] if s.nested else s.qualname
            for candidate in (s.qualname, parent_name):
                router = self.routers.get((module.name, f"{candidate}:{name}"))
                if router is not None:
                    return router
        if name.startswith("self.") and scope is not None and scope.owner is not None:
            for c in self.index.mro(scope.owner):
                for key, router in self.routers.items():
                    if key[0] == c.module.name and key[1].endswith(f":{name}") and router.scope is not None and router.scope.owner is c:
                        return router
        router = self.routers.get((module.name, name))
        if router is not None:
            return router

        symbol = self.index.resolve_name(name, module)
        if symbol.kind == "variable" and not symbol.rest:
            router = self.routers.get((symbol.module.name, symbol.obj.name))
            if router is not None:
                return router
            value = strip_type(symbol.obj.value) if symbol.obj.value is not None else None
            if value is not None and value.type == "call":          # router = make_router()
                return self.lookup_router(value, symbol.module, None)
        if symbol.kind == "module" and symbol.rest:                 # include_router(items.router) where rest was not a variable
            return None
        return None

    def _returned_router(self, func: FuncInfo) -> Optional[RouterInfo]:
        for node in walk(func.node):
            if node.type == "return_statement" and node.named_children:
                value = strip_type(node.named_children[0])
                if value.type == "identifier":
                    router = self.routers.get((func.module.name, f"{func.qualname}:{text(value)}"))
                    if router is not None:
                        return router
                    router = self.routers.get((func.module.name, text(value)))
                    if router is not None:
                        return router
        return None

    # ------------------------------------------------------------------
    # decorator-registered handlers
    # ------------------------------------------------------------------

    def _find_registrations(self, module: ModuleInfo):
        source_has_fastapi = b"fastapi" in module.code or b"starlette" in module.code
        for func in module.all_functions:
            scope = func if func.nested else None
            if func.nested:
                parent = func.qualname.rsplit(".", 1)[0]
                scope = self._func_at_name(module, parent)
            elif func.owner is not None:
                scope = func
            for d in func.decorators:
                if d.call is None:
                    continue
                call_func, args = call_parts(d.call)
                if call_func is None or call_func.type != "attribute":
                    continue
                verb = text(call_func.child_by_field_name("attribute"))
                if verb not in ROUTE_DECORATORS:
                    continue
                obj = call_func.child_by_field_name("object")
                router = self.lookup_router(obj, module, scope)
                if router is None:
                    if not source_has_fastapi or verb in ("route",):
                        continue
                    pos = positional_args(args)
                    kwargs = keyword_args(args)
                    path_node = pos[0] if pos else kwargs.get("path")
                    path = self.const.string(path_node, module) if path_node is not None else None
                    if path is None or (path and not path.startswith("/")):
                        continue
                    router = self._synthetic_router(module, dotted(obj) or "app")
                self._register_decorator(router, func, verb, args, module, d)

    def _func_at_name(self, module: ModuleInfo, qualname: str) -> Optional[FuncInfo]:
        for f in module.all_functions:
            if f.qualname == qualname:
                return f
        return None

    def _synthetic_router(self, module: ModuleInfo, name: str) -> RouterInfo:
        key = (module.name, f"unresolved:{name}")
        if key not in self.routers:
            info = RouterInfo(id=f"{module.name}:unresolved:{name}", kind="router", module=module, var=name, line=1, node=None)
            self.routers[key] = info
            self.router_list.append(info)
        return self.routers[key]

    def _register_decorator(self, router: RouterInfo, func: FuncInfo, verb: str, args, module: ModuleInfo, decorator):
        pos, kwargs = positional_args(args), keyword_args(args)
        path_node = pos[0] if pos else kwargs.get("path")
        path = self.const.string(path_node, module) if path_node is not None else ""
        path = path if path is not None else ""
        if verb == "websocket":
            methods, kind = ["WEBSOCKET"], "websocket"
        elif verb in ("api_route", "route"):
            methods = [m.upper() for m in self.const.strings(kwargs.get("methods"), module)] or ["GET"]
            kind = "http"
        else:
            methods, kind = [verb.upper()], "http"
        reg = Registration(router, func, methods, path, kwargs, module, line_of(decorator.node), kind, "decorator")
        router.routes.append(reg)
        self.registrations.append(reg)

    # ------------------------------------------------------------------
    # calls: include_router, mount, add_api_route, exception handlers, middleware
    # ------------------------------------------------------------------

    def _find_calls(self, module: ModuleInfo):
        for node in walk(module.root):
            if node.type != "call":
                continue
            func, args = call_parts(node)
            if func is None or func.type != "attribute":
                continue
            attr = text(func.child_by_field_name("attribute"))
            if attr not in ("include_router", "mount", "add_api_route", "add_api_websocket_route", "add_route", "add_websocket_route",
                            "add_exception_handler", "add_middleware", "add_event_handler"):
                continue
            obj = func.child_by_field_name("object")
            scope = self.enclosing_function(node, module)
            if scope is not None and scope.nested:
                scope = self._func_at_name(module, scope.qualname.rsplit(".", 1)[0]) or scope
            receiver = self.lookup_router(obj, module, scope)
            if receiver is None:
                continue

            if attr == "include_router":
                self._include(node, args, receiver, module, scope)
            elif attr == "mount":
                self._mount(node, args, receiver, module, scope)
            elif attr in ("add_api_route", "add_api_websocket_route", "add_route", "add_websocket_route"):
                self._add_api_route(node, args, receiver, module, scope, attr)
            elif attr == "add_exception_handler":
                pos = positional_args(args)
                if len(pos) >= 2:
                    self.exception_handlers.append(ExceptionHandler(pos[0], module, self._function_ref(pos[1], module, scope), receiver, line_of(node)))
            elif attr == "add_middleware":
                pos = positional_args(args)
                if pos:
                    self._middleware(pos[0], keyword_args(args), receiver, module, line_of(node))
            elif attr == "add_event_handler":
                pos = positional_args(args)
                if len(pos) >= 2:
                    handler = self._function_ref(pos[1], module, scope)
                    event = self.const.string(pos[0], module) or "startup"
                    if handler is not None:
                        self.lifecycle.append(Lifecycle(event, handler, module, line_of(node)))

        # decorators on handlers: exception handlers, middleware, events
        for func in module.all_functions:
            for d in func.decorators:
                if d.call is None:
                    continue
                call_func, args = call_parts(d.call)
                if call_func is None or call_func.type != "attribute":
                    continue
                attr = text(call_func.child_by_field_name("attribute"))
                if attr not in ("exception_handler", "middleware", "on_event"):
                    continue
                scope = func if not func.nested else self._func_at_name(module, func.qualname.rsplit(".", 1)[0])
                receiver = self.lookup_router(call_func.child_by_field_name("object"), module, scope)
                if receiver is None:
                    continue
                pos = positional_args(args)
                if attr == "exception_handler" and pos:
                    self.exception_handlers.append(ExceptionHandler(pos[0], module, func, receiver, func.line_start))
                elif attr == "middleware":
                    kind = (self.const.string(pos[0], module) if pos else None) or "http"
                    self.middleware.append(MiddlewareInfo(func.name, f"http:{kind}", module, func.line_start, {}, receiver))
                elif attr == "on_event" and pos:
                    self.lifecycle.append(Lifecycle(self.const.string(pos[0], module) or "startup", func, module, func.line_start))

        # FastAPI(lifespan=..., exception_handlers={...})
        for router in self.router_list:
            if router.module is not module or router.kind != "app":
                continue
            lifespan = router.kwargs.get("lifespan")
            if lifespan is not None:
                handler = self._function_ref(lifespan, module, router.scope)
                if handler is not None:
                    self.lifecycle.append(Lifecycle("lifespan", handler, module, router.line))
            handlers = strip_type(router.kwargs.get("exception_handlers"))
            if handlers is not None and handlers.type == "dictionary":
                for pair in handlers.named_children:
                    if pair.type == "pair":
                        self.exception_handlers.append(ExceptionHandler(
                            pair.child_by_field_name("key"), module,
                            self._function_ref(pair.child_by_field_name("value"), module, router.scope), router, router.line))
            for mw in self._depends_list(router.kwargs.get("middleware"), module):
                func_node, args = call_parts(mw.node)
                inner = positional_args(args)
                if inner:
                    self._middleware(inner[0], keyword_args(args), router, module, line_of(mw.node))

    def _middleware(self, node, options: dict, app, module, line):
        name = (dotted(node) or "").rsplit(".", 1)[-1] or text(node)
        kind = MIDDLEWARE_NAMES.get(name, "custom")
        values = {}
        for key, value_node in options.items():
            value = self.const.value(value_node, module)
            if value is not UNKNOWN and isinstance(value, (str, int, float, bool, list)):
                values[key] = value
        self.middleware.append(MiddlewareInfo(name, kind, module, line, values, app))

    def _function_ref(self, node, module: ModuleInfo, scope: Optional[FuncInfo]) -> Optional[FuncInfo]:
        node = strip_type(node)
        if node is None:
            return None
        name = dotted(node)
        if not name:
            return None
        if name.startswith("self.") and scope is not None and scope.owner is not None:
            return self.index.find_method(scope.owner, name[5:])
        symbol = self.index.resolve_name(name, module)
        if symbol.kind == "function" and not symbol.rest:
            return symbol.obj
        if symbol.kind == "class" and symbol.rest and "." not in symbol.rest:
            return self.index.find_method(symbol.obj, symbol.rest)
        if scope is not None:                                   # nested function inside a factory
            for f in module.all_functions:
                if f.nested and f.name == name and f.qualname.startswith(scope.qualname + "."):
                    return f
        return None

    def _include(self, node, args, receiver: RouterInfo, module, scope):
        pos, kwargs = positional_args(args), keyword_args(args)
        target = pos[0] if pos else kwargs.get("router")
        if target is None:
            return
        prefix = self.const.string(kwargs["prefix"], module) or "" if "prefix" in kwargs else ""
        tags = self.const.strings(kwargs["tags"], module) if "tags" in kwargs else []
        deps = self._depends_list(kwargs.get("dependencies"), module)
        for child_node in self._expand_loop(target, module):
            child = self.lookup_router(child_node, module, scope)
            if child is None or child is receiver:
                continue
            inc = Include(receiver, child, prefix, tags, deps, module, line_of(node), "include", node.end_point[0] + 1,
                          kwargs.get("responses"))
            receiver.includes.append(inc)
            child.parents.append(inc)
            self.includes.append(inc)

    def _mount(self, node, args, receiver: RouterInfo, module, scope):
        pos, kwargs = positional_args(args), keyword_args(args)
        if len(pos) < 2 and "app" not in kwargs:
            return
        path = self.const.string(pos[0] if pos else kwargs.get("path"), module) or ""
        target = pos[1] if len(pos) > 1 else kwargs.get("app")
        child = self.lookup_router(target, module, scope)
        if child is not None and child is not receiver:
            inc = Include(receiver, child, path, [], [], module, line_of(node), "mount", node.end_point[0] + 1)
            receiver.includes.append(inc)
            child.parents.append(inc)
            self.includes.append(inc)
            return
        target_call = strip_type(target)
        func_node, _ = call_parts(target_call) if target_call is not None else (None, None)
        name = (dotted(func_node) or "").rsplit(".", 1)[-1] if func_node is not None else ""
        self.static_mounts.append({
            "path": path, "kind": "static" if name == "StaticFiles" else (name or "application"),
            "module": module.name, "line": line_of(node),
        })

    def _expand_loop(self, node, module) -> list:
        """`for r in [a.router, b.router]: app.include_router(r)` -> the list elements."""
        node = strip_type(node)
        if node is None or node.type != "identifier":
            return [node]
        current = node.parent
        while current is not None:
            if current.type == "for_statement":
                left = current.child_by_field_name("left")
                if left is not None and text(left) == text(node):
                    iterable = strip_type(current.child_by_field_name("right"))
                    if iterable is not None and iterable.type in ("list", "tuple"):
                        return list(iterable.named_children)
                    if iterable is not None and iterable.type == "identifier":
                        var = module.variables.get(text(iterable))
                        if var is not None and var.value is not None and strip_type(var.value).type in ("list", "tuple"):
                            return list(strip_type(var.value).named_children)
                    break
            current = current.parent
        return [node]

    def _add_api_route(self, node, args, receiver: RouterInfo, module, scope, attr):
        pos, kwargs = positional_args(args), keyword_args(args)
        path_node = pos[0] if pos else kwargs.get("path")
        endpoint_node = pos[1] if len(pos) > 1 else kwargs.get("endpoint")
        path = self.const.string(path_node, module) if path_node is not None else ""
        endpoint = self._function_ref(endpoint_node, module, scope) if endpoint_node is not None else None
        if endpoint is None:
            return
        websocket = "websocket" in attr
        methods = ["WEBSOCKET"] if websocket else [m.upper() for m in self.const.strings(kwargs.get("methods"), module)] or ["GET"]
        reg = Registration(receiver, endpoint, methods, path or "", kwargs, module, line_of(node), "websocket" if websocket else "http",
                           "add_api_route")
        receiver.routes.append(reg)
        self.registrations.append(reg)

    # ------------------------------------------------------------------
    # placement: effective prefix / dependencies per route
    # ------------------------------------------------------------------

    def _place(self):
        roots = [r for r in self.router_list if not r.parents]
        for root in roots:
            self._descend(root, "", [], [], root if root.kind == "app" else None, [root], [], [], set())
        # routes of routers that sit in an include cycle (never reached from a root)
        for reg in self.registrations:
            if reg not in self.placements and reg.router is not None:
                self.placements[reg] = [Placement(reg.router.prefix, None, [], list(reg.router.tags), [reg.router])]

    def _descend(self, router: RouterInfo, prefix_acc: str, deps_acc: list, tags_acc: list, app: Optional[RouterInfo], chain: list,
                 included: list, responses: list, seen: set):
        if router.id in seen:
            return
        seen = seen | {router.id}
        own_deps = [("app" if router.kind == "app" else "router", d) for d in router.dependencies]
        own_responses = responses + ([(router.kwargs["responses"], router.module)] if "responses" in router.kwargs else [])
        for reg in router.routes:
            placement = Placement(prefix_acc + (router.prefix if router.kind == "router" else ""), app, deps_acc + own_deps,
                                  tags_acc + router.tags, chain, included, own_responses)
            self.placements.setdefault(reg, []).append(placement)

        for inc in router.includes:
            child_deps = deps_acc + own_deps + [("include", d) for d in inc.dependencies]
            child_app = inc.child if inc.child.kind == "app" and inc.kind == "mount" else app
            child_prefix = prefix_acc + (router.prefix if router.kind == "router" else "") + inc.prefix
            if inc.kind == "mount" and inc.child.kind == "app":
                child_deps = []             # a mounted application is independent: parent dependencies do not apply
            inherited = own_responses + ([(inc.responses, inc.module)] if inc.responses is not None and inc.kind == "include" else [])
            self._descend(inc.child, child_prefix, child_deps, tags_acc + router.tags + inc.tags, child_app, chain + [inc.child],
                          included + [inc], inherited if inc.kind == "include" else [], seen)

    # ------------------------------------------------------------------
    # helpers for consumers
    # ------------------------------------------------------------------

    def apps(self) -> List[RouterInfo]:
        return [r for r in self.router_list if r.kind == "app"]

    def registrations_of(self, func: FuncInfo) -> List[Registration]:
        return [r for r in self.registrations if r.func is func]
