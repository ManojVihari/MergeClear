"""
Function analysis: for every function, what it calls inside the project, which external systems it touches
(databases, caches, brokers, HTTP APIs, storage, email, search), which HTTP statuses it can produce and which
exceptions it raises. Types are inferred just far enough to know what `db`, `redis_client` or `self.repo` is.
"""
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .appmodel import AppModel
from .catalog import (
    ACTIVE_RECORD_DELETE, ACTIVE_RECORD_READ, ACTIVE_RECORD_WRITE, ASYNC_LAUNCHERS, AWS_FACTORIES, AWS_SERVICE_TAGS, AWS_SESSIONS,
    CACHE_DECORATORS, EMAIL_METHODS, FUNCTION_RESULTS, HTTP_EXCEPTIONS, HTTP_METHODS, HTTP_MODULE_FUNCTIONS, MONGO_DELETE, MONGO_READ,
    MONGO_WRITE, ORM_DELETE, ORM_READ, ORM_TX, ORM_WRITE, PRODUCER_METHODS, REDIS_DELETE, REDIS_PUBLISH, REDIS_READ, REDIS_WRITE,
    RESILIENCE_DECORATORS, RESPONSE_CLASSES, ROUTE_DECORATORS, SCHEDULER_PATTERNS, SEARCH_DELETE, SEARCH_READ, SEARCH_WRITE, SQL_BUILDERS, SQL_EXECUTE,
    STORAGE_DELETE, STORAGE_READ, STORAGE_WRITE, TASK_DISPATCH_METHODS, TYPE_RULES, Tag, looks_like_sql, parse_sql,
)
from .pyindex import (
    ClassInfo, ConstEvaluator, FuncInfo, ModuleInfo, PythonIndex, Symbol, UNKNOWN, call_parts, dotted, keyword_args, line_of,
    positional_args, string_value, strip_type, text, unwrap_await, walk,
)
from .schemas import ModelRegistry
from .typeexpr import TypeExpr

logger = logging.getLogger(__name__)

MAX_CLOSURE = 400
MAX_LIST = 200
CONSTRUCTOR_SUFFIXES = ("from_url", "connect", "create", "from_dsn", "from_env", "from_config", "create_pool", "from_settings", "from_client")


def dedupe(items):
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


@dataclass(eq=False)
class Binding:
    """Classes handed to a constructor (`CRUDItem(Item)`): lets `self.model` inside shared base classes resolve."""
    key: str
    attrs: Dict[str, ClassInfo] = field(default_factory=dict)


@dataclass(eq=False)
class Inst:
    """An instance of a project class."""
    cls: ClassInfo
    bind: Optional[Binding] = None


@dataclass
class CallEdge:
    callee: FuncInfo
    bind: Optional[Binding]
    line: int


@dataclass(eq=False)
class FuncAnalysis:
    func: FuncInfo
    bind: Optional[Binding] = None
    calls: List[CallEdge] = field(default_factory=list)
    names: List[str] = field(default_factory=list)
    facts: List[dict] = field(default_factory=list)
    statuses: List[dict] = field(default_factory=list)
    thrown: List[dict] = field(default_factory=list)
    attr_types: Dict[str, object] = field(default_factory=dict)
    return_types: List[object] = field(default_factory=list)
    instantiated: List[ClassInfo] = field(default_factory=list)


def walk_body(root):
    """Nodes of a function body in source order; nested `def`s are separate functions and run only when called."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(node.children):
            if child.type != "function_definition":
                stack.append(child)


def node_id(func: FuncInfo, bind: Optional[Binding] = None) -> str:
    return func.id if bind is None else f"{func.id}@{bind.key}"


class Analyzer:

    def __init__(self, index: PythonIndex, const: ConstEvaluator, registry: ModelRegistry, appmodel: AppModel,
                 resolve_placeholders: Optional[Callable[[ModuleInfo, str], str]] = None):
        self.index = index
        self.const = const
        self.registry = registry
        self.app = appmodel
        self.resolve_placeholders = resolve_placeholders or (lambda module, value: value)
        self._analyses: Dict[Tuple[str, Optional[str]], FuncAnalysis] = {}
        self._in_progress: set = set()
        self._var_types: Dict[Tuple[str, str], object] = {}
        self._return_cache: Dict[str, object] = {}
        self.listeners: List[dict] = []
        self.scheduled: List[dict] = []
        self.tasks: Dict[str, dict] = {}              # function id -> background task (Celery, Dramatiq, ...)
        self._instances: Dict[str, Inst] = {}
        self._handler_status: Dict[str, Optional[int]] = {}
        self.exception_handlers: Dict[str, dict] = {}   # exception key -> {status, handler, source}

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def analyze_all(self):
        self._index_exception_handlers()
        self._index_tasks()
        for module in self.index.modules.values():
            for func in module.all_functions:
                self.analysis(func)
        self._index_module_level_effects()

    # ------------------------------------------------------------------
    # public helpers
    # ------------------------------------------------------------------

    def analysis(self, func: FuncInfo, bind: Optional[Binding] = None) -> FuncAnalysis:
        key = (func.id, bind.key if bind else None)
        cached = self._analyses.get(key)
        if cached is not None:
            return cached
        if key in self._in_progress:
            return FuncAnalysis(func, bind)
        self._in_progress.add(key)
        try:
            result = FunctionWalker(self, func, bind).run()
        finally:
            self._in_progress.discard(key)
        self._analyses[key] = result
        return result

    def label(self, func: FuncInfo) -> str:
        return func.label

    def reachable(self, func: FuncInfo, bind: Optional[Binding] = None, extra_roots=()) -> List[Tuple[FuncInfo, Optional[Binding]]]:
        """Functions reachable from `func` (BFS, excluding itself), following project calls."""
        seen = {node_id(func, bind)}
        order: List[Tuple[FuncInfo, Optional[Binding]]] = []
        queue = deque()
        for edge in self.analysis(func, bind).calls:
            queue.append((edge.callee, edge.bind))
        for f, b in extra_roots:
            queue.append((f, b))
        while queue and len(order) < MAX_CLOSURE:
            f, b = queue.popleft()
            nid = node_id(f, b)
            if nid in seen:
                continue
            seen.add(nid)
            order.append((f, b))
            for edge in self.analysis(f, b).calls:
                queue.append((edge.callee, edge.bind))
        return order

    def call_tree(self, func: FuncInfo, bind: Optional[Binding] = None, depth=0, seen=None, max_depth=6) -> dict:
        seen = seen or set()
        nid = node_id(func, bind)
        if depth >= max_depth or nid in seen:
            return {}
        seen = seen | {nid}
        out = {}
        for edge in self.analysis(func, bind).calls:
            out[self.label(edge.callee)] = self.call_tree(edge.callee, edge.bind, depth + 1, seen, max_depth)
        return out

    # ------------------------------------------------------------------
    # type inference shared by walkers
    # ------------------------------------------------------------------

    def tag_for_qualified(self, qualified: Optional[str]) -> Optional[Tag]:
        if not qualified:
            return None
        candidates = [qualified]
        head, _, tail = qualified.rpartition(".")
        if tail in CONSTRUCTOR_SUFFIXES and head:
            candidates.append(head)
        for candidate in candidates:
            for pattern, tag in TYPE_RULES:
                if pattern.match(candidate):
                    return tag
        return None

    def te_type(self, te: TypeExpr, bind=None):
        """Inst / Tag for a parsed annotation."""
        te = te.unwrap_optional()
        if te.kind == "class" and te.cls is not None:
            return Inst(te.cls)
        if te.kind in ("simple", "unknown") and te.qualified:
            return self.tag_for_qualified(te.qualified)
        if te.kind == "union":
            for member in te.args:
                found = self.te_type(member)
                if found is not None:
                    return found
        return None

    def var_type(self, name: str, module: ModuleInfo, depth=0):
        """Type of a module-level variable."""
        key = (module.name, name)
        if key in self._var_types:
            return self._var_types[key]
        var = module.variables.get(name)
        if var is None or depth > 6:
            return None
        self._var_types[key] = None                       # recursion guard
        result = None
        if var.annotation is not None:
            result = self.te_type(self.registry.types.parse(var.annotation, module))
        if result is None and var.value is not None:
            result = ExpressionTyper(self, module).type_of(var.value)
        self._var_types[key] = result
        return result

    def func_return_type(self, func: FuncInfo):
        cached = self._return_cache.get(func.id)
        if cached is not None or func.id in self._return_cache:
            return cached
        self._return_cache[func.id] = None
        result = None
        if func.returns is not None:
            result = self.te_type(self.registry.types.parse(func.returns, func.module))
        if result is None:
            for rt in self.analysis(func).return_types:
                if rt is not None:
                    result = rt
                    break
        self._return_cache[func.id] = result
        return result

    def make_instance(self, cls: ClassInfo, call_node, module: ModuleInfo) -> Inst:
        key = f"{module.name}:{call_node.start_byte}"
        cached = self._instances.get(key)
        if cached is not None:
            return cached
        attrs: Dict[str, ClassInfo] = {}
        init = self.index.find_method(cls, "__init__")
        if init is not None:
            pos, kwargs = positional_args(call_parts(call_node)[1]), keyword_args(call_parts(call_node)[1])
            params = [p for p in init.params if p.name not in ("self", "cls")]
            bound: Dict[str, ClassInfo] = {}
            for p, arg in zip(params, pos):
                target = self._class_of_expr(arg, module)
                if target is not None:
                    bound[p.name] = target
            for name, arg in kwargs.items():
                target = self._class_of_expr(arg, module)
                if target is not None:
                    bound[name] = target
            if bound:
                for node in walk(init.node):
                    if node.type == "assignment":
                        left, right = node.child_by_field_name("left"), strip_type(node.child_by_field_name("right"))
                        if left is not None and text(left).startswith("self.") and right is not None and text(right) in bound:
                            attrs[text(left)[5:]] = bound[text(right)]
        inst = Inst(cls, Binding(key, attrs) if attrs else None)
        self._instances[key] = inst
        return inst

    def _class_of_expr(self, node, module: ModuleInfo) -> Optional[ClassInfo]:
        node = strip_type(node)
        if node is None or node.type not in ("identifier", "attribute"):
            return None
        symbol = self.index.resolve_expr(node, module)
        if symbol.kind == "class" and not symbol.rest:
            return symbol.obj
        return None

    def qualified_of(self, node, module: ModuleInfo) -> Optional[str]:
        """Library name of an expression, looking through variables created by library constructors."""
        node = strip_type(node)
        if node is None:
            return None
        func, _ = call_parts(node)
        target = func if func is not None else node
        symbol = self.index.resolve_expr(target, module)
        if symbol.kind == "external":
            return symbol.qualified
        if symbol.kind == "variable" and symbol.obj.value is not None:
            value = strip_type(symbol.obj.value)
            inner = self.qualified_of(value, symbol.module) if value.type == "call" else None
            if inner:
                return f"{inner}.{symbol.rest}" if symbol.rest else inner
        return None

    # ------------------------------------------------------------------
    # exceptions
    # ------------------------------------------------------------------

    def _index_exception_handlers(self):
        for h in self.app.exception_handlers:
            key = self._exception_key(h.exception, h.module)
            if key is None:
                continue
            status = self.handler_status(h.func) if h.func is not None else None
            self.exception_handlers[key] = {
                "status": status, "handler": h.func.label if h.func else None,
                "source": "@exception_handler", "handler_id": h.func.id if h.func else None,
            }

    def _exception_key(self, node, module: ModuleInfo) -> Optional[str]:
        node = strip_type(node)
        if node is None:
            return None
        if node.type == "integer":
            return f"status:{text(node)}"
        symbol = self.index.resolve_expr(node, module)
        if symbol.kind == "class" and not symbol.rest:
            return symbol.obj.id
        if symbol.kind == "external":
            return symbol.qualified or dotted(node)
        name = dotted(node)
        value = self.const.value(node, module)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"status:{value}"
        return name

    def handler_status(self, func: Optional[FuncInfo]) -> Optional[int]:
        if func is None:
            return None
        if func.id in self._handler_status:
            return self._handler_status[func.id]
        status = None
        for node in walk(func.node):
            if node.type == "call":
                kw = keyword_args(call_parts(node)[1])
                if "status_code" in kw:
                    status = self.const.integer(kw["status_code"], func.module)
                    if status:
                        break
            elif node.type == "assignment":
                left = node.child_by_field_name("left")
                if left is not None and text(left).endswith(".status_code"):
                    status = self.const.integer(node.child_by_field_name("right"), func.module)
                    if status:
                        break
        self._handler_status[func.id] = status
        return status

    def exception_status(self, cls: ClassInfo) -> Optional[int]:
        """Status a project exception class carries by itself (HTTPException subclasses)."""
        for c in self.index.mro(cls):
            value = c.class_vars.get("status_code")
            if value is not None:
                status = self.const.integer(value, c.module)
                if status:
                    return status
            init = c.methods.get("__init__")
            if init is not None:
                for p in init.params:
                    if p.name == "status_code" and p.default is not None:
                        status = self.const.integer(p.default, c.module)
                        if status:
                            return status
                for node in walk(init.node):
                    if node.type == "call":
                        func, args = call_parts(node)
                        if func is not None and text(func).endswith("__init__"):
                            kw = keyword_args(args)
                            pos = positional_args(args)
                            status_node = kw.get("status_code") or (pos[0] if pos else None)
                            status = self.const.integer(status_node, c.module) if status_node is not None else None
                            if status:
                                return status
        return None

    def is_http_exception(self, cls: ClassInfo) -> bool:
        return any(HTTP_EXCEPTIONS.match(e) for e in self.index.external_bases(cls))

    def exception_mapping(self, cls: Optional[ClassInfo], external: Optional[str], thrown_status: Optional[int] = None) -> dict:
        """How an exception is answered: status, who handles it."""
        if cls is not None:
            for c in self.index.mro(cls):
                found = self.exception_handlers.get(c.id)
                if found:
                    return found
            for e in self.index.external_bases(cls):
                found = self.exception_handlers.get(e)
                if found:
                    return found
            status = thrown_status or self.exception_status(cls)
            if status or self.is_http_exception(cls):
                handled = self.exception_handlers.get("fastapi.exceptions.HTTPException") or self.exception_handlers.get(
                    "starlette.exceptions.HTTPException")
                if handled:
                    return {**handled, "status": handled.get("status") or status}
                return {"status": status, "handler": None, "source": "HTTPException"}
            return {"status": None, "handler": None, "source": "unhandled"}
        if external:
            found = self.exception_handlers.get(external)
            if found:
                return found
            if HTTP_EXCEPTIONS.match(external):
                return {"status": thrown_status, "handler": None, "source": "HTTPException"}
        return {"status": None, "handler": None, "source": "unhandled"}

    # ------------------------------------------------------------------
    # tasks, schedulers and other registrations
    # ------------------------------------------------------------------

    def _index_tasks(self):
        for module in self.index.modules.values():
            for func in module.all_functions:
                for d in func.decorators:
                    info = self._task_decorator(d, func)
                    if info:
                        self.tasks[func.id] = info
                        self.listeners.append({
                            "handler": func.label, "method_id": func.id, "module": module.project, "file": module.rel,
                            "line": func.line_start, "technology": info["technology"], "destinations": [info["name"]],
                            "kind": "task",
                        })
                        break

    def _task_decorator(self, d, func: FuncInfo) -> Optional[dict]:
        target = d.call if d.call is not None else d.node
        qn = self.qualified_of(target, func.module) or ""
        call_func = call_parts(target)[0] if d.call is not None else target
        last = (dotted(call_func) or "").rsplit(".", 1)[-1]
        technology = None
        if qn == "celery.shared_task" or qn.endswith("celery.Celery.task") or qn.endswith("celery.app.base.Celery.task"):
            technology = "celery"
        elif qn.startswith("dramatiq.") and last == "actor":
            technology = "dramatiq"
        elif qn.startswith("taskiq") and last == "task":
            technology = "taskiq"
        elif last in ("task", "shared_task") and call_func is not None and call_func.type == "attribute":
            holder = self.qualified_of(call_func.child_by_field_name("object"), func.module) or ""
            if holder.startswith("celery") or "Celery" in holder:
                technology = "celery"
        elif qn.startswith("arq"):
            technology = None
        if technology is None:
            return None
        kw = keyword_args(call_parts(d.call)[1]) if d.call is not None else {}
        name = self.const.string(kw["name"], func.module) if "name" in kw else None
        queue = self.const.string(kw["queue"], func.module) if "queue" in kw else None
        return {"technology": technology, "name": name or f"{func.module.name}.{func.name}", "queue": queue}

    def _index_module_level_effects(self):
        """Scheduler registrations: add_job(fn, ...), beat_schedule, @repeat_every."""
        for module in self.index.modules.values():
            for node in walk(module.root):
                if node.type == "call":
                    self._scheduler_call(node, module)
                elif node.type == "assignment":
                    left = node.child_by_field_name("left")
                    if left is not None and text(left).endswith("beat_schedule"):
                        self._beat_schedule(node.child_by_field_name("right"), module)
            for func in module.all_functions:
                for d in func.decorators:
                    qn = self.qualified_of(d.call if d.call is not None else d.node, module) or ""
                    last = (dotted(call_parts(d.call)[0] if d.call is not None else d.node) or "").rsplit(".", 1)[-1]
                    if SCHEDULER_PATTERNS["fastapi-utils"].match(qn) or last == "repeat_every":
                        kw = keyword_args(call_parts(d.call)[1]) if d.call is not None else {}
                        self._add_scheduled(func, "fastapi-utils", {k: self.const.value(v, module) for k, v in kw.items()
                                                                     if self.const.value(v, module) is not UNKNOWN})
                    elif last in ("scheduled_job", "scheduled") and d.call is not None:
                        holder = self.qualified_of(call_parts(d.call)[0].child_by_field_name("object"), module) or ""
                        if SCHEDULER_PATTERNS["apscheduler"].match(holder) or "Scheduler" in holder:
                            self._add_scheduled(func, "apscheduler", self._schedule_args(d.call, module))
                    elif SCHEDULER_PATTERNS["aiocron"].match(qn) and d.call is not None:
                        pos = positional_args(call_parts(d.call)[1])
                        self._add_scheduled(func, "aiocron", {"cron": self.const.string(pos[0], module)} if pos else {})

    def _schedule_args(self, call, module) -> dict:
        pos, kw = positional_args(call_parts(call)[1]), keyword_args(call_parts(call)[1])
        schedule = {}
        trigger = None
        for p in pos:
            value = self.const.string(p, module)
            if value in ("interval", "cron", "date"):
                trigger = value
        if trigger is None and "trigger" in kw:
            trigger = self.const.string(kw["trigger"], module)
        if trigger:
            schedule["trigger"] = trigger
        for key, value_node in kw.items():
            if key in ("id", "name", "args", "kwargs", "func", "trigger", "replace_existing", "misfire_grace_time", "coalesce", "jobstore",
                       "executor", "max_instances"):
                continue
            value = self.const.value(value_node, module)
            if value is not UNKNOWN and isinstance(value, (str, int, float, bool)):
                schedule[key] = value
        return schedule

    def _scheduler_call(self, node, module: ModuleInfo):
        func, args = call_parts(node)
        if func is None or func.type != "attribute":
            return
        attr = text(func.child_by_field_name("attribute"))
        if attr == "add_job":
            holder = self.qualified_of(func.child_by_field_name("object"), module) or ""
            if SCHEDULER_PATTERNS["apscheduler"].match(holder) or "Scheduler" in holder:
                pos = positional_args(args)
                target = self.app._function_ref(pos[0], module, self.app.enclosing_function(node, module)) if pos else None
                if target is not None:
                    self._add_scheduled(target, "apscheduler", self._schedule_args(node, module))
        elif attr == "do":
            chain = text(func.child_by_field_name("object"))
            if chain.startswith("schedule.every") or ".every(" in chain:
                pos = positional_args(args)
                target = self.app._function_ref(pos[0], module, self.app.enclosing_function(node, module)) if pos else None
                if target is not None:
                    self._add_scheduled(target, "schedule", {"every": re.sub(r"\s+", " ", chain.replace("schedule.", ""))})

    def _beat_schedule(self, node, module: ModuleInfo):
        node = strip_type(node)
        if node is None or node.type != "dictionary":
            return
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            entry = strip_type(pair.child_by_field_name("value"))
            if entry is None or entry.type != "dictionary":
                continue
            values = {}
            for item in entry.named_children:
                if item.type == "pair":
                    values[text(item.child_by_field_name("key")).strip("\"'")] = item.child_by_field_name("value")
            task_name = self.const.string(values["task"], module) if "task" in values else None
            schedule = values.get("schedule")
            self.scheduled.append({
                "handler": task_name or self.const.string(pair.child_by_field_name("key"), module) or "task",
                "method_id": next((fid for fid, t in self.tasks.items() if t["name"] == task_name), None),
                "module": module.project, "file": module.rel, "line": line_of(pair), "technology": "celery-beat",
                "schedule": {"schedule": re.sub(r"\s+", " ", text(schedule))[:80]} if schedule is not None else {},
            })

    def _add_scheduled(self, func: FuncInfo, technology: str, schedule: dict):
        self.scheduled.append({
            "handler": func.label, "method_id": func.id, "module": func.module.project, "file": func.module.rel,
            "line": func.line_start, "technology": technology, "schedule": {k: v for k, v in schedule.items() if v is not None},
        })


# ============================================================================
# EXPRESSION TYPES
# ============================================================================

class ExpressionTyper:
    """Infers what an expression evaluates to (an Inst, a Tag or nothing). Subclassed by the function walker."""

    def __init__(self, analyzer: Analyzer, module: ModuleInfo):
        self.A = analyzer
        self.index = analyzer.index
        self.const = analyzer.const
        self.module = module
        self.env: Dict[str, object] = {}
        self.assigned: Dict[str, object] = {}
        self.bind: Optional[Binding] = None
        self.func: Optional[FuncInfo] = None
        self.self_attrs: Dict[str, object] = {}

    # ------------------------------------------------------------------
    def type_of(self, node, depth=0):
        node = unwrap_await(strip_type(node))
        if node is None or depth > 8:
            return None
        kind = node.type
        if kind == "identifier":
            name = text(node)
            if name in self.env:
                return self.env[name]
            if name in ("self", "cls") and self.func is not None and self.func.owner is not None:
                return Inst(self.func.owner, self.bind)
            return self._global_type(name)
        if kind == "attribute":
            return self._attribute_type(node, depth)
        if kind == "call":
            return self._call_type(node, depth)
        if kind == "subscript":
            base = self.type_of(node.child_by_field_name("value"), depth + 1)
            if isinstance(base, Tag) and base.tech in ("mongodb", "dynamodb"):
                key = string_value(node.children_by_field_name("subscript")[0]) if node.children_by_field_name("subscript") else None
                return base.with_info(collection=key)
            return base if isinstance(base, Tag) and base.family in ("db",) else None
        if kind in ("conditional_expression",):
            return self.type_of(node.named_children[0], depth + 1)
        if kind == "boolean_operator":
            return self.type_of(node.child_by_field_name("left"), depth + 1)
        return None

    def _global_type(self, name: str):
        symbol = self.index.lookup(self.module, name)
        if symbol is None:
            return None
        if symbol.kind == "variable":
            return self.A.var_type(symbol.obj.name, symbol.module)
        return None

    def _attribute_type(self, node, depth):
        obj, attr = node.child_by_field_name("object"), text(node.child_by_field_name("attribute"))
        base = self.type_of(obj, depth + 1)
        if isinstance(base, Inst):
            return self._class_attr_type(base, attr)
        if isinstance(base, Tag):
            return self.A_attr_of_tag(base, attr)
        name = dotted(node)
        if name:
            symbol = self.index.resolve_name(name, self.module)
            if symbol.kind == "variable" and not symbol.rest:
                return self.A.var_type(symbol.obj.name, symbol.module)
        return None

    @staticmethod
    def A_attr_of_tag(tag: Tag, attr: str):
        if tag.tech == "rabbitmq" and attr == "default_exchange":
            return Tag("broker", "rabbitmq", "exchange")
        if tag.tech == "mongodb":
            return tag.with_info(collection=attr)
        return None

    def _class_attr_type(self, inst: Inst, attr: str):
        if self.func is not None and inst.cls is self.func.owner and attr in self.self_attrs:
            return self.self_attrs[attr]
        for c in self.index.mro(inst.cls):
            for f in c.fields:
                if f.name == attr and f.annotation is not None:
                    return self.A.te_type(self.A.registry.types.parse(f.annotation, c.module))
        # assigned in some method of the class (usually __init__)
        for c in self.index.mro(inst.cls):
            for name in ("__init__", "setup", "__post_init__", "init", "start", "connect"):
                method = c.methods.get(name)
                if method is not None:
                    found = self.A.analysis(method, inst.bind).attr_types.get(attr)
                    if found is not None:
                        return found
            for method in c.methods.values():
                if method.name in ("__init__",):
                    continue
                found = self.A.analysis(method, inst.bind).attr_types.get(attr)
                if found is not None:
                    return found
        return None

    # ------------------------------------------------------------------
    def _call_type(self, node, depth):
        func, args = call_parts(node)
        if func is None:
            return None
        if func.type == "attribute":
            obj = func.child_by_field_name("object")
            method = text(func.child_by_field_name("attribute"))
            base = self.type_of(obj, depth + 1)
            if isinstance(base, Inst):
                target = self.index.find_method(base.cls, method)
                return self.A.func_return_type(target) if target is not None else None
            if isinstance(base, Tag):
                return self._method_result(base, method, node)
        if func.type == "identifier" and text(func) in self.env:
            held = self.env[text(func)]
            if isinstance(held, Tag) and held.role == "session_factory":
                return Tag(held.family, held.tech, "session")
        symbol = self.index.resolve_expr(func, self.module)
        if symbol.kind == "class" and not symbol.rest:
            return self.A.make_instance(symbol.obj, node, self.module)
        if symbol.kind == "function" and not symbol.rest:
            return self.A.func_return_type(symbol.obj)
        if symbol.kind == "variable":
            held = self.A.var_type(symbol.obj.name, symbol.module)
            if symbol.rest == "" and isinstance(held, Tag) and held.role == "session_factory":
                return Tag(held.family, held.tech, "session")
            if symbol.rest and isinstance(held, Tag):
                return self._method_result(held, symbol.rest.rsplit(".", 1)[-1], node)
            if symbol.rest and isinstance(held, Inst):
                target = self.index.find_method(held.cls, symbol.rest)
                return self.A.func_return_type(target) if target is not None else None
        if symbol.kind == "external" and symbol.qualified:
            return self._library_result(symbol.qualified, node)
        return None

    def _library_result(self, qualified: str, node):
        if qualified in FUNCTION_RESULTS:
            return FUNCTION_RESULTS[qualified]
        if AWS_FACTORIES.match(qualified):
            return self._aws_service(node)
        if AWS_SESSIONS.match(qualified):
            return Tag("framework", "aws", "session")
        tag = self.A.tag_for_qualified(qualified)
        if tag is None:
            return None
        pos, kwargs = positional_args(call_parts(node)[1]), keyword_args(call_parts(node)[1])
        if tag.family == "http":
            base = kwargs.get("base_url") or kwargs.get("base_uri")
            if base is not None:
                value = self.string_of(base)
                if value:
                    tag = tag.with_info(base_url=value)
        if tag.family == "broker" and tag.role in ("consumer",):
            topics = [self.string_of(p) for p in pos if self.string_of(p)]
            if topics:
                tag = tag.with_info(topics=",".join(topics))
        return tag

    def _aws_service(self, node):
        pos = positional_args(call_parts(node)[1])
        kwargs = keyword_args(call_parts(node)[1])
        service = self.const.string(pos[0], self.module) if pos else self.const.string(kwargs.get("service_name"), self.module) \
            if "service_name" in kwargs else None
        tag = AWS_SERVICE_TAGS.get(service or "")
        return tag if tag is not None else Tag("framework", "aws", "client", (("service", service or ""),))

    def _method_result(self, tag: Tag, method: str, node):
        if tag.family == "db":
            if method in ("connect", "begin", "raw_connection", "cursor", "acquire", "transaction", "execution_options", "get_connection",
                          "connection") and tag.role in ("engine", "connection"):
                return Tag("db", tag.tech, "connection")
            if method in ("begin", "begin_nested") and tag.role == "session_factory":
                return Tag("db", tag.tech, "session")
            if tag.tech == "mongodb" and method in ("get_database", "get_collection", "__getitem__", "database", "collection"):
                pos = positional_args(call_parts(node)[1])
                return tag.with_info(collection=self.string_of(pos[0]) if pos else None)
            if tag.tech == "dynamodb" and method == "Table":
                pos = positional_args(call_parts(node)[1])
                return tag.with_info(table=self.string_of(pos[0]) if pos else None)
        if tag.family == "cache" and tag.tech == "redis":
            if method == "pubsub":
                return Tag("cache", "redis", "pubsub")
            if method in ("pipeline", "client"):
                return tag
        if tag.family == "broker" and tag.tech == "rabbitmq":
            if method == "channel":
                return Tag("broker", "rabbitmq", "channel")
            if method in ("declare_exchange", "get_exchange", "exchange"):
                return Tag("broker", "rabbitmq", "exchange")
        if tag.family == "framework" and tag.tech == "aws" and method in ("client", "resource"):
            return self._aws_service(node)
        if tag.family == "storage" and method in ("Bucket", "bucket", "get_bucket", "blob", "Object", "get_blob_client", "get_container_client",
                                                  "get_bucket", "container"):
            pos = positional_args(call_parts(node)[1])
            return tag.with_info(bucket=self.string_of(pos[0]) if pos else tag.get("bucket"))
        return None

    def string_of(self, node) -> Optional[str]:
        """String value of an expression; local variables are looked through, unknown pieces stay as {expr}."""
        node = strip_type(node)
        if node is None:
            return None
        if node.type == "identifier" and text(node) in self.assigned:
            value = self.string_of(self.assigned[text(node)])
            if value is not None:
                return value
        value = self.const.value(node, self.module)
        if isinstance(value, str):
            return value
        if node.type in ("string", "concatenated_string"):
            return None
        if value is UNKNOWN and node.type in ("identifier", "attribute"):
            return "{" + text(node) + "}"
        return None


# ============================================================================
# FUNCTION WALKER
# ============================================================================

class FunctionWalker(ExpressionTyper):

    def __init__(self, analyzer: Analyzer, func: FuncInfo, bind: Optional[Binding]):
        super().__init__(analyzer, func.module)
        self.func = func
        self.bind = bind
        self.result = FuncAnalysis(func, bind)
        self.self_attrs = self.result.attr_types
        self._seen_edges: set = set()

    # ------------------------------------------------------------------
    def run(self) -> FuncAnalysis:
        self._seed_environment()
        self._decorator_facts()
        body = self.func.node.child_by_field_name("body")
        if body is not None:
            for node in walk_body(body):
                kind = node.type
                if kind == "assignment":
                    self._assignment(node)
                elif kind == "with_item":
                    self._with_item(node)
                elif kind == "call":
                    self._call(node)
                elif kind == "raise_statement":
                    self._raise(node)
                elif kind in ("return_statement", "yield"):
                    if node.named_children:
                        self.result.return_types.append(self.type_of(node.named_children[0]))
        return self.result

    # ------------------------------------------------------------------
    # environment
    # ------------------------------------------------------------------

    def _seed_environment(self):
        for p in self.func.params:
            if p.name in ("self", "cls"):
                continue
            found = None
            if p.annotation is not None:
                te = self.A.registry.types.parse(p.annotation, self.module)
                found = self.A.te_type(te)
                if found is None:
                    for meta in te.metas:
                        found = self._dependency_type(meta.node, meta.module) or found
            if found is None and p.default is not None:
                found = self._dependency_type(p.default, self.module)
            if found is not None:
                self.env[p.name] = found

    def _dependency_type(self, node, module: ModuleInfo):
        func, args = call_parts(node)
        if func is None or (dotted(func) or "").rsplit(".", 1)[-1] not in ("Depends", "Security"):
            return None
        pos = positional_args(args)
        kw = keyword_args(args)
        target = pos[0] if pos else kw.get("dependency")
        if target is None:
            return None
        symbol = self.index.resolve_expr(target, module)
        if symbol.kind == "function" and not symbol.rest:
            return self.A.func_return_type(symbol.obj)
        if symbol.kind == "class" and not symbol.rest:
            return Inst(symbol.obj)
        if symbol.kind == "variable":
            return self.A.var_type(symbol.obj.name, symbol.module)
        return None

    def _assignment(self, node):
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None:
            return
        value_type = self.type_of(right)
        annotation = node.child_by_field_name("type")
        if value_type is None and annotation is not None:
            value_type = self.A.te_type(self.A.registry.types.parse(annotation, self.module))
        if left.type == "identifier":
            self.assigned[text(left)] = strip_type(right)
            if value_type is not None:
                self.env[text(left)] = value_type
        elif left.type == "attribute":
            target = text(left)
            if target.startswith("self.") and "." not in target[5:]:
                if value_type is None and strip_type(right).type == "identifier":
                    value_type = self.env.get(text(strip_type(right)))
                if value_type is not None:
                    self.result.attr_types[target[5:]] = value_type
            if target.endswith(".status_code"):
                code = self.const.integer(right, self.module)
                if code:
                    self.result.statuses.append({"code": code, "source": "response.status_code", "line": line_of(node)})

    def _with_item(self, node):
        value = node.child_by_field_name("value")
        if value is None or value.type != "as_pattern":
            return
        expression = value.named_children[0] if value.named_children else None
        alias = value.child_by_field_name("alias")
        if expression is None or alias is None:
            return
        name = text(alias)
        found = self.type_of(expression)
        if found is not None:
            self.env[name] = found
        self.assigned[name] = strip_type(expression)

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------

    def fact(self, kind: str, node, **fields) -> dict:
        fact = {"kind": kind, **fields, "source": self.func.label, "line": line_of(node) if node is not None else self.func.line_start,
                "file": self.module.rel, "module": self.module.project}
        self.result.facts.append({k: v for k, v in fact.items() if v is not None})
        return fact

    def _decorator_facts(self):
        for d in self.func.decorators:
            target = d.call if d.call is not None else d.node
            call_func = call_parts(target)[0] if d.call is not None else target
            if call_func is not None and call_func.type == "attribute" and \
                    text(call_func.child_by_field_name("attribute")) in ROUTE_DECORATORS | {"exception_handler", "middleware", "on_event"}:
                continue
            qn = self.A.qualified_of(target, self.module) or ""
            kw = keyword_args(call_parts(d.call)[1]) if d.call is not None else {}
            pos = positional_args(call_parts(d.call)[1]) if d.call is not None else []
            for pattern, name in CACHE_DECORATORS:
                if pattern.match(qn):
                    if name is None:
                        break
                    expire = self.const.value(kw["expire"], self.module) if "expire" in kw else None
                    namespace = self.const.string(kw["namespace"], self.module) if "namespace" in kw else None
                    self.fact("cache", d.node, technology=name, operation="READ_THROUGH", cache_names=[namespace or self.func.name],
                              ttl=expire if isinstance(expire, (int, float)) else None)
                    break
            for pattern, name in RESILIENCE_DECORATORS:
                if pattern.match(qn):
                    options = {}
                    if name == "rate-limit" and pos:
                        limit = self.const.string(pos[0], self.module)
                        options["limit"] = limit
                    self.fact("resilience", d.node, technology=name, name=self.func.name, **options)
                    break
            last = (dotted(call_func) or "").rsplit(".", 1)[-1] if call_func is not None else ""
            if last in ("subscriber", "publisher") and call_func.type == "attribute":
                holder = self.type_of(call_func.child_by_field_name("object"))
                if isinstance(holder, Tag) and holder.family == "broker":
                    destination = self._broker_destination(holder, pos, kw)
                    if last == "subscriber":
                        self.A.listeners.append({
                            "handler": self.func.label, "method_id": self.func.id, "module": self.module.project, "file": self.module.rel,
                            "line": self.func.line_start, "technology": holder.tech, "destinations": [destination] if destination else [],
                            "kind": "subscriber",
                        })
                    else:
                        self.fact("messaging", d.node, technology=holder.tech, direction="publish", destination=destination, via="@publisher")

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------

    def _edge(self, callee: FuncInfo, bind: Optional[Binding], node):
        key = (node_id(callee, bind), line_of(node))
        if key in self._seen_edges or callee is self.func and bind is self.bind:
            return
        self._seen_edges.add(key)
        self.result.calls.append(CallEdge(callee, bind, line_of(node)))
        self.result.names.append(callee.name)

    def _method_edges(self, inst: Inst, method: str, node):
        target = self.index.find_method(inst.cls, method)
        if target is None:
            return False
        self._edge(target, inst.bind, node)
        # an abstract / protocol method dispatches to the implementations
        if self._is_abstract(target):
            for sub in self.index.subclasses(inst.cls):
                override = sub.methods.get(method)
                if override is not None:
                    self._edge(override, None, node)
        return True

    @staticmethod
    def _is_abstract(func: FuncInfo) -> bool:
        if any(d.name.rsplit(".", 1)[-1] == "abstractmethod" for d in func.decorators):
            return True
        body = func.node.child_by_field_name("body")
        statements = [c for c in body.named_children if c.type != "comment"] if body is not None else []
        statements = [s for s in statements if not (s.type == "expression_statement" and s.named_children and s.named_children[0].type == "string")]
        if len(statements) == 1:
            only = statements[0]
            if only.type == "pass_statement" or text(only) == "...":
                return True
            if only.type == "raise_statement" and "NotImplementedError" in text(only):
                return True
        return False

    def _call(self, node):
        func, args = call_parts(node)
        if func is None:
            return
        pos, kwargs = positional_args(args), keyword_args(args)

        # ---- method call on something we know the type of ----
        if func.type == "attribute":
            method = text(func.child_by_field_name("attribute"))
            receiver = func.child_by_field_name("object")
            recv_type = self.type_of(receiver)
            if isinstance(recv_type, Inst):
                if self._method_edges(recv_type, method, node):
                    return
            elif isinstance(recv_type, Tag):
                if self._tag_call(recv_type, method, node, pos, kwargs, receiver):
                    return
            if self._background_call(func, method, node, pos, kwargs):
                return

        symbol = self.index.resolve_expr(func, self.module)

        if func.type == "identifier" and symbol.kind == "external" and not symbol.qualified:
            local = self._local_function(text(func))
            if local is not None:
                self._edge(local, None, node)
                return

        if symbol.kind == "function":
            if not symbol.rest:
                self._edge(symbol.obj, None, node)
                return
            if symbol.rest in TASK_DISPATCH_METHODS or symbol.rest.split(".")[0] in TASK_DISPATCH_METHODS:
                self._task_dispatch(symbol.obj, node, pos, kwargs)
                return
        elif symbol.kind == "class":
            self._class_call(symbol, node, pos, kwargs)
            return
        elif symbol.kind == "variable" and symbol.rest:
            core = self.A.registry.core_table(symbol.obj, symbol.module)
            if core is not None:
                first = symbol.rest.split(".")[0]
                operation = {"select": "READ", "insert": "WRITE", "update": "WRITE", "delete": "DELETE"}.get(first)
                if operation:
                    self.fact("database", node, technology="sqlalchemy", operation=operation, call=f"Table.{first}", table=[core.table])
                return
            held = self.A.var_type(symbol.obj.name, symbol.module)
            method = symbol.rest.rsplit(".", 1)[-1]
            if isinstance(held, Inst):
                self._method_edges(held, symbol.rest.split(".")[0], node)
                return
            if isinstance(held, Tag):
                self._tag_call(held, method, node, pos, kwargs, func.child_by_field_name("object") if func.type == "attribute" else None)
                return
        elif symbol.kind == "external" and symbol.qualified:
            self._library_call(symbol.qualified, node, pos, kwargs)
            return

        # Celery tasks referenced through the Celery app: app.send_task("name")
        if func.type == "attribute" and text(func.child_by_field_name("attribute")) in TASK_DISPATCH_METHODS:
            holder = self.type_of(func.child_by_field_name("object"))
            if isinstance(holder, Tag) and holder.tech == "celery":
                self._tag_call(holder, text(func.child_by_field_name("attribute")), node, pos, kwargs, func.child_by_field_name("object"))

    def _local_function(self, name: str) -> Optional[FuncInfo]:
        """A function defined inside the current one and called from it."""
        if not hasattr(self, "_local_names"):
            body = self.func.node.child_by_field_name("body")
            self._local_names = {text(n.child_by_field_name("name")) for n in walk(body)
                                 if n.type == "function_definition"} if body is not None else set()
        if name not in self._local_names:
            return None
        return self.index.nested_function(self.func, name)

    def _class_call(self, symbol: Symbol, node, pos, kwargs):
        cls = symbol.obj
        if not symbol.rest:
            inst = self.A.make_instance(cls, node, self.module)
            self.result.instantiated.append(cls)
            if self.A.registry.kind(cls) in ("entity", "exception", "model", "enum", "settings"):
                return
            for name in ("__init__", "__post_init__"):
                method = self.index.find_method(cls, name)
                if method is not None and method.module is not None:
                    self._edge(method, inst.bind, node)
            return
        method = symbol.rest.split(".")[0]
        if self.A.registry.is_entity(cls):
            self._active_record_call(cls, symbol.rest.split(".")[-1], node)
            return
        target = self.index.find_method(cls, method)
        if target is not None:
            self._edge(target, None, node)

    # ------------------------------------------------------------------
    # library calls by qualified name
    # ------------------------------------------------------------------

    def _library_call(self, qualified: str, node, pos, kwargs):
        last = qualified.rsplit(".", 1)[-1]
        if last in RESPONSE_CLASSES and qualified.split(".")[0] in ("fastapi", "starlette"):
            status_node = kwargs.get("status_code") or (pos[1] if last == "Response" and len(pos) > 1 else None)
            code = self.const.integer(status_node, self.module) if status_node is not None else None
            if code:
                self.result.statuses.append({"code": code, "source": f"{last} returned", "line": line_of(node)})
            return
        if HTTP_MODULE_FUNCTIONS.match(qualified):
            self._http_fact(qualified.split(".")[0], qualified.rsplit(".", 1)[-1], node, pos, kwargs, None)
            return
        if qualified in ("urllib.request.urlopen", "urllib.request.Request"):
            self._http_fact("urllib", "get", node, pos, kwargs, None)
            return
        if qualified in ASYNC_LAUNCHERS:
            self._launch(qualified, node, pos, kwargs)
            return
        if qualified.endswith("run_in_threadpool") or qualified.endswith("to_thread.run_sync"):
            self._launch("starlette.concurrency.run_in_threadpool", node, pos, kwargs)
            return
        if qualified in ("aiosmtplib.send",):
            self.fact("email", node, technology="smtp", operation="WRITE")
            return
        if qualified.startswith("sqlalchemy") or qualified.startswith("sqlmodel"):
            return
        tag = self.A.tag_for_qualified(qualified)
        if tag is not None and tag.family == "broker" and tag.role == "consumer":
            topics = [self.string_of(p) for p in pos if self.string_of(p)]
            self._listener(tag, topics)
        if qualified.endswith(".CeleryBeat"):
            return

    def _launch(self, qualified: str, node, pos, kwargs):
        mechanism = ASYNC_LAUNCHERS.get(qualified, "threadpool")
        target = None
        candidates = list(pos) + ([kwargs["target"]] if "target" in kwargs else [])
        for c in candidates[:2]:
            ref = self.app_function_ref(c)
            if ref is not None:
                target = ref
                break
        if qualified == "asyncio.create_task" and pos:
            func, _ = call_parts(pos[0])
            if func is not None:
                ref = self.app_function_ref(func)
                if ref is not None:
                    target = ref
        self.fact("async", node, technology=mechanism, target=target.label if target else None)
        if target is not None:
            self._edge(target, None, node)

    def app_function_ref(self, node) -> Optional[FuncInfo]:
        node = strip_type(node)
        if node is None:
            return None
        name = dotted(node)
        if not name:
            return None
        if name.startswith("self."):
            owner = self.func.owner
            if owner is not None:
                return self.index.find_method(owner, name[5:].split(".")[0])
            return None
        symbol = self.index.resolve_name(name, self.module)
        if symbol.kind == "function" and not symbol.rest:
            return symbol.obj
        if symbol.kind == "variable" and symbol.rest:
            held = self.A.var_type(symbol.obj.name, symbol.module)
            if isinstance(held, Inst):
                return self.index.find_method(held.cls, symbol.rest.split(".")[0])
        if symbol.kind == "class" and symbol.rest and "." not in symbol.rest:
            return self.index.find_method(symbol.obj, symbol.rest)
        return None

    def _background_call(self, func_node, method, node, pos, kwargs) -> bool:
        receiver = func_node.child_by_field_name("object")
        receiver_type = self.type_of(receiver)
        is_background = isinstance(receiver_type, Tag) and receiver_type.role == "background"
        if method == "add_task" and (is_background or re.search(r"(background|tasks?)$", text(receiver), re.I)):
            target = self.app_function_ref(pos[0]) if pos else None
            self.fact("async", node, technology="background-task", target=target.label if target else None)
            if target is not None:
                self._edge(target, None, node)
            return True
        if method in ("run_in_executor",) and len(pos) >= 2:
            target = self.app_function_ref(pos[1])
            self.fact("async", node, technology="thread", target=target.label if target else None)
            if target is not None:
                self._edge(target, None, node)
            return True
        if method == "submit" and pos and isinstance(receiver_type, Tag) is False and re.search(r"(executor|pool)", text(receiver), re.I):
            target = self.app_function_ref(pos[0])
            self.fact("async", node, technology="thread", target=target.label if target else None)
            if target is not None:
                self._edge(target, None, node)
            return True
        return False

    # ------------------------------------------------------------------
    # tag (library object) calls
    # ------------------------------------------------------------------

    def _tag_call(self, tag: Tag, method: str, node, pos, kwargs, receiver) -> bool:
        family = tag.family
        if family == "db":
            return self._db_call(tag, method, node, pos, kwargs)
        if family == "cache":
            return self._cache_call(tag, method, node, pos, kwargs)
        if family == "broker":
            return self._broker_call(tag, method, node, pos, kwargs, receiver)
        if family == "http":
            if method in HTTP_METHODS:
                self._http_fact(tag.tech, method, node, pos, kwargs, tag)
                return True
            return False
        if family == "storage":
            return self._storage_call(tag, method, node, pos, kwargs)
        if family == "email":
            if method in EMAIL_METHODS or method.startswith("send"):
                self.fact("email", node, technology=tag.tech, operation="WRITE", call=f"{tag.tech}.{method}")
                return True
            return False
        if family == "search":
            operation = "DELETE" if method in SEARCH_DELETE else "WRITE" if method in SEARCH_WRITE else "READ" if method in SEARCH_READ else None
            if operation:
                index_name = self.string_of(kwargs["index"]) if "index" in kwargs else (self.string_of(pos[0]) if pos else None)
                self.fact("search", node, technology=tag.tech, operation=operation, call=f"{tag.tech}.{method}", index=index_name)
                return True
            return False
        if family == "framework" and tag.tech == "aws":
            return False
        return False

    # ---- databases ----------------------------------------------------

    def _chain_methods(self, call) -> List[str]:
        """Methods chained after a call: `db.query(X).filter(..).delete()` -> ['filter', 'delete']."""
        names = []
        current = call
        for _ in range(12):
            parent = current.parent
            while parent is not None and parent.type in ("await", "parenthesized_expression"):
                current, parent = parent, parent.parent
            if parent is None or parent.type != "attribute" or parent.child_by_field_name("object") != current:
                break
            names.append(text(parent.child_by_field_name("attribute")))
            grand = parent.parent
            if grand is not None and grand.type == "call" and grand.child_by_field_name("function") == parent:
                current = grand
            else:
                break
        return names

    def entities_in(self, nodes, instances=False, _seen=None) -> list:
        """
        ORM entities (and Core tables) referenced in expressions; variables are looked through once.

        `instances` also counts objects passed directly (`db.add(item)`), not those only read from (`item.id == 1`).
        """
        found: list = []
        seen = _seen if _seen is not None else set()
        for root in nodes:
            if instances:
                direct = [root] if root.type == "identifier" else [c for c in root.named_children if c.type == "identifier"] \
                    if root.type in ("list", "tuple") else []
                for d in direct:
                    held = self.env.get(text(d))
                    if isinstance(held, Inst) and self.A.registry.is_entity(held.cls):
                        found.append(held.cls)
            for n in walk(root):
                if n.type == "identifier":
                    name = text(n)
                    symbol = self.index.lookup(self.module, name)
                    if symbol is not None and symbol.kind == "class" and self.A.registry.is_entity(symbol.obj):
                        found.append(symbol.obj)
                    elif symbol is not None and symbol.kind == "variable" and self.A.registry.core_table(symbol.obj, symbol.module):
                        found.append(self.A.registry.core_table(symbol.obj, symbol.module))
                    elif name in self.assigned and name not in seen and n is not self.assigned.get(name):
                        seen.add(name)
                        found.extend(self.entities_in([self.assigned[name]], False, seen))
                elif n.type == "attribute":
                    name = dotted(n)
                    if name and name.startswith("self.") and self.bind and name[5:] in self.bind.attrs:
                        found.append(self.bind.attrs[name[5:]])
                    elif name:
                        symbol = self.index.resolve_name(name, self.module)
                        if symbol.kind == "class" and self.A.registry.is_entity(symbol.obj):
                            found.append(symbol.obj)
        out = []
        for c in found:
            if c not in out:
                out.append(c)
        return out

    def _statement_info(self, node, depth=0) -> Tuple[Optional[str], List[str], Optional[str]]:
        """(operation, tables, sql text) of an argument to execute(): builders, text(), raw SQL, or a variable holding one."""
        node = unwrap_await(strip_type(node))
        if node is None or depth > 3:
            return None, [], None
        if node.type == "identifier" and text(node) in self.assigned and self.assigned[text(node)] is not node:
            return self._statement_info(self.assigned[text(node)], depth + 1)
        # root of a builder chain: select(Item).where(...).limit(1)
        root = node
        while root.type == "call" and call_parts(root)[0] is not None and call_parts(root)[0].type == "attribute":
            root = strip_type(call_parts(root)[0].child_by_field_name("object"))
            if root is None:
                break
        if root is not None and root.type == "call":
            func, _ = call_parts(root)
            name = (dotted(func) or "").rsplit(".", 1)[-1]
            if name in SQL_BUILDERS:
                return SQL_BUILDERS[name], [], None
            if name in ("text", "sql", "SQL", "literal_sql"):
                inner = positional_args(call_parts(root)[1])
                if inner:
                    sql = self.string_of(inner[0])
                    if sql:
                        operation, tables = parse_sql(sql)
                        return operation, tables, sql
        value = self.string_of(node)
        if value and looks_like_sql(value):
            operation, tables = parse_sql(value)
            return operation, tables, value
        return None, [], None

    def _entity_fields(self, entities: List[ClassInfo]):
        names = [self._entity_table(e) for e in entities]
        return [n for n in names if n]

    def _entity_table(self, cls) -> Optional[str]:
        return self.A.registry.table_name(cls)

    def _db_call(self, tag: Tag, method: str, node, pos, kwargs, ) -> bool:
        tech = tag.tech
        type_name = {"session": "Session", "engine": "Connection", "connection": "Connection", "session_factory": "Session",
                     "client": tag.tech}.get(tag.role, tag.role)
        call_name = f"{type_name}.{method}"
        chained = self._chain_methods(node)

        if tag.role in ("session", "session_factory") and tech == "sqlalchemy":
            if method in ORM_TX:
                self.fact("transaction", node, technology=tech, operation=method.upper())
                return True
            operation, tables, sql = None, [], None
            if method in SQL_EXECUTE | {"scalars", "scalar", "exec", "stream", "stream_scalars", "execute"} and pos:
                operation, tables, sql = self._statement_info(pos[0])
                if operation is None and method in ("execute", "executemany", "exec_driver_sql"):
                    operation = "WRITE" if tables or sql else "READ"
            if operation is None:
                operation = "DELETE" if method in ORM_DELETE else "WRITE" if method in ORM_WRITE else "READ" if method in ORM_READ else None
            if operation is None:
                return False
            if "delete" in chained and method in ("query", "execute"):
                operation = "DELETE"
            elif any(c in chained for c in ("update", "insert", "values", "returning")) and operation == "READ" and "delete" not in chained:
                operation = "WRITE" if method in ("query",) and "update" in chained else operation
            entities = self.entities_in(pos + list(kwargs.values()), instances=method in ORM_WRITE | ORM_DELETE | {"refresh", "get"})
            table_names = list(dict.fromkeys(self._entity_fields(entities) + tables))
            if not entities and not table_names and method in ("commit", "flush", "refresh", "rollback", "close", "all", "first", "one"):
                return False
            self.fact("database", node, technology=tech, operation=operation, call=call_name,
                      entity=entities[0].name if entities else None, entities=[e.name for e in entities] or None,
                      table=table_names or None, query=(sql[:300] if sql else None))
            return True

        if tech == "mongodb":
            operation = "DELETE" if method in MONGO_DELETE else "WRITE" if method in MONGO_WRITE else "READ" if method in MONGO_READ else None
            if operation is None:
                return False
            collection = tag.get("collection")
            self.fact("database", node, technology="mongodb", operation=operation, call=call_name, table=[collection] if collection else None)
            return True

        if tech == "dynamodb":
            operation = {"put_item": "WRITE", "update_item": "WRITE", "batch_writer": "WRITE", "delete_item": "DELETE", "get_item": "READ",
                         "query": "READ", "scan": "READ", "batch_get_item": "READ"}.get(method)
            if operation is None:
                return False
            table = tag.get("table")
            self.fact("database", node, technology="dynamodb", operation=operation, call=call_name, table=[table] if table else None)
            return True

        # raw drivers and engines
        if method in ORM_TX or method in ("commit", "rollback"):
            self.fact("transaction", node, technology=tech, operation=method.upper())
            return True
        if method in SQL_EXECUTE or method in ("run", "query", "execute_query", "execute_write", "execute_read"):
            if not pos and "query" not in kwargs and "sql" not in kwargs:
                return False
            operation, tables, sql = self._statement_info(pos[0] if pos else kwargs.get("query") or kwargs.get("sql"))
            if operation is None:
                operation = "READ" if method.startswith("fetch") else "WRITE"
            self.fact("database", node, technology=tech, operation=operation, call=call_name, table=tables or None,
                      query=sql[:300] if sql else None)
            return True
        return False

    def _active_record_call(self, cls: ClassInfo, method: str, node):
        reg = self.A.registry
        operation = "DELETE" if method in ACTIVE_RECORD_DELETE else "WRITE" if method in ACTIVE_RECORD_WRITE else \
            "READ" if method in ACTIVE_RECORD_READ else None
        if operation is None:
            return
        chained = self._chain_methods(node)
        if "delete" in chained:
            operation = "DELETE"
        elif "update" in chained and operation == "READ":
            operation = "WRITE"
        table = self._entity_table(cls)
        self.fact("database", node, technology=reg.entity_store(cls), operation=operation, call=f"{cls.name}.{method}",
                  entity=cls.name, table=[table] if table else None)

    # ---- caches -------------------------------------------------------

    def _cache_call(self, tag: Tag, method: str, node, pos, kwargs) -> bool:
        if tag.role == "pubsub":
            if method in ("subscribe", "psubscribe"):
                self._listener(Tag("broker", "redis", "consumer"), [self.string_of(p) for p in pos if self.string_of(p)])
                return True
            return False
        if method in REDIS_PUBLISH:
            destination = self.string_of(pos[0]) if pos else None
            self.fact("messaging", node, technology="redis", direction="publish", destination=destination, call=f"redis.{method}")
            return True
        operation = "DELETE" if method in REDIS_DELETE else "WRITE" if method in REDIS_WRITE else "READ" if method in REDIS_READ else None
        if operation is None:
            return False
        key = self.string_of(pos[0]) if pos else None
        self.fact("cache", node, technology=tag.tech, operation=operation, command=method, key=key)
        return True

    # ---- brokers ------------------------------------------------------

    def _broker_destination(self, tag: Tag, pos, kwargs) -> Optional[str]:
        tech = tag.tech
        if tech == "rabbitmq":
            for key in ("routing_key", "queue"):
                if key in kwargs:
                    return self.string_of(kwargs[key])
            for key in ("exchange",):
                if key in kwargs:
                    return self.string_of(kwargs[key])
            return self.string_of(pos[0]) if pos else None
        if tech in ("kafka", "nats", "pubsub", "mqtt", "redis-streams", "redis"):
            for key in ("topic", "subject", "channel", "stream"):
                if key in kwargs:
                    return self.string_of(kwargs[key])
            return self.string_of(pos[0]) if pos else None
        if tech == "sqs":
            url = self.string_of(kwargs["QueueUrl"]) if "QueueUrl" in kwargs else None
            return url.rstrip("/").rsplit("/", 1)[-1] if url else None
        if tech == "sns":
            arn = self.string_of(kwargs["TopicArn"]) if "TopicArn" in kwargs else None
            return arn.rsplit(":", 1)[-1] if arn else None
        if tech == "kinesis":
            return self.string_of(kwargs["StreamName"]) if "StreamName" in kwargs else None
        if tech == "eventbridge":
            return "eventbridge"
        for key in ("topic", "queue", "destination", "name"):
            if key in kwargs:
                return self.string_of(kwargs[key])
        return self.string_of(pos[0]) if pos else None

    def _broker_call(self, tag: Tag, method: str, node, pos, kwargs, receiver) -> bool:
        tech = tag.tech
        if tech in ("celery",):
            if method == "send_task":
                name = self.string_of(pos[0]) if pos else None
                self.fact("messaging", node, technology="celery", direction="publish", destination=name, call="Celery.send_task")
                return True
            return False
        if tech in ("rq", "arq", "dramatiq", "taskiq"):
            if method in TASK_DISPATCH_METHODS:
                target = self.app_function_ref(pos[0]) if pos else None
                name = (target.label if target else None) or (self.string_of(pos[0]) if pos else None)
                self.fact("messaging", node, technology=tech, direction="publish", destination=name, call=f"{tech}.{method}")
                if target is not None:
                    self._edge(target, None, node)
                return True
            return False
        if tag.role in ("consumer",):
            if method == "subscribe":
                topics = []
                for p in pos[:1]:
                    value = strip_type(p)
                    if value.type in ("list", "tuple", "set"):
                        topics.extend(self.string_of(c) for c in value.named_children)
                    else:
                        topics.append(self.string_of(p))
                self._listener(tag, [t for t in topics if t])
                return True
            return False
        if tech == "rabbitmq" and method in ("basic_consume", "consume", "iterator", "queue_bind", "get"):
            if method == "basic_consume":
                queue = self.string_of(kwargs["queue"]) if "queue" in kwargs else (self.string_of(pos[0]) if pos else None)
                self._listener(tag, [queue] if queue else [])
                return True
            return False
        if tech in PRODUCER_METHODS and method in PRODUCER_METHODS[tech] or method in ("publish", "send", "send_and_wait", "produce") and tech in PRODUCER_METHODS:
            destination = self._broker_destination(tag, pos, kwargs)
            self.fact("messaging", node, technology=tech, direction="publish", destination=destination, call=f"{tech}.{method}")
            return True
        if tag.role == "broker" and method == "publish":
            destination = self._broker_destination(tag, pos[1:] if pos else pos, kwargs)
            self.fact("messaging", node, technology=tech, direction="publish", destination=destination, call=f"{tech}.publish")
            return True
        return False

    def _listener(self, tag: Tag, destinations: List[str]):
        entry = {
            "handler": self.func.label, "method_id": self.func.id, "module": self.module.project, "file": self.module.rel,
            "line": self.func.line_start, "technology": tag.tech, "destinations": [d for d in destinations if d], "kind": "consumer",
        }
        if entry not in self.A.listeners:
            self.A.listeners.append(entry)

    def _task_dispatch(self, task_func: FuncInfo, node, pos, kwargs):
        task = self.A.tasks.get(task_func.id)
        if task is None:
            return
        self.fact("messaging", node, technology=task["technology"], direction="publish", destination=task["name"],
                  queue=task["queue"], call=f"{task_func.name}.delay")

    # ---- HTTP ---------------------------------------------------------

    def _http_fact(self, client: str, method: str, node, pos, kwargs, tag: Optional[Tag]):
        http_method = method.upper()
        url_node = None
        if method in ("request", "stream", "send", "fetch"):
            if pos and (self.string_of(pos[0]) or "").upper() in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                http_method = (self.string_of(pos[0]) or "GET").upper()
                url_node = pos[1] if len(pos) > 1 else kwargs.get("url")
            else:
                http_method = (self.string_of(kwargs["method"]) or "GET").upper() if "method" in kwargs else "ANY"
                url_node = pos[0] if pos else kwargs.get("url")
        else:
            url_node = pos[0] if pos else kwargs.get("url")
        url = self.string_of(url_node) if url_node is not None else None
        base_url = tag.get("base_url") if tag is not None else None
        if url is None and base_url is None:
            url = None
        full = url
        if base_url and (url is None or not re.match(r"^\w+://", url or "")):
            full = (base_url.rstrip("/") + "/" + (url or "").lstrip("/")) if url else base_url
        if full:
            full = self.A.resolve_placeholders(self.module, full)
        info = self._split_url(full)
        self.fact("http", node, client=client, http_method=http_method, url=full, host=info["host"], base_url=base_url,
                  target_service=info["service"], config_ref=info["config"], response_type=None)

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

    # ---- storage ------------------------------------------------------

    def _storage_call(self, tag: Tag, method: str, node, pos, kwargs) -> bool:
        operation = "DELETE" if method in STORAGE_DELETE else "WRITE" if method in STORAGE_WRITE else "READ" if method in STORAGE_READ else None
        if operation is None:
            return False
        bucket = tag.get("bucket")
        if "Bucket" in kwargs:
            bucket = self.string_of(kwargs["Bucket"]) or bucket
        elif "bucket_name" in kwargs:
            bucket = self.string_of(kwargs["bucket_name"]) or bucket
        elif tag.tech == "minio" and pos:
            bucket = self.string_of(pos[0]) or bucket
        elif method in ("upload_file", "download_file", "upload_fileobj", "download_fileobj") and len(pos) >= 2 and tag.tech == "s3":
            bucket = self.string_of(pos[1]) or bucket
        self.fact("storage", node, technology=tag.tech, operation=operation, call=f"{tag.tech}.{method}", bucket=bucket)
        return True

    # ------------------------------------------------------------------
    # exceptions and responses
    # ------------------------------------------------------------------

    def _raise(self, node):
        if not node.named_children:
            return
        expression = strip_type(node.named_children[0])
        if expression is None:
            return
        func, args = call_parts(expression)
        target = func if func is not None else expression
        symbol = self.index.resolve_expr(target, self.module)
        pos, kwargs = (positional_args(args), keyword_args(args)) if args is not None else ([], {})
        name = (dotted(target) or text(target)).rsplit(".", 1)[-1]
        line = line_of(node)

        if expression.type == "identifier" and symbol.kind != "class":
            held = self.env.get(text(expression))
            if isinstance(held, Inst):
                symbol = Symbol("class", held.cls, held.cls.module)
            else:
                return

        if symbol.kind == "external":
            qualified = symbol.qualified or name
            status = None
            if HTTP_EXCEPTIONS.match(qualified) or name == "HTTPException":
                status_node = kwargs.get("status_code") or (pos[0] if pos else None)
                status = self.const.integer(status_node, self.module) if status_node is not None else None
                if status:
                    self.result.statuses.append({"code": status, "source": "HTTPException", "line": line})
                self.result.thrown.append({"name": "HTTPException", "cls": None, "external": qualified, "status": status, "line": line})
            else:
                self.result.thrown.append({"name": name, "cls": None, "external": qualified, "status": None, "line": line})
        elif symbol.kind == "class" and not symbol.rest:
            status = None
            status_node = kwargs.get("status_code") or (pos[0] if pos and self.A.is_http_exception(symbol.obj) else None)
            if status_node is not None:
                status = self.const.integer(status_node, self.module)
            self.result.thrown.append({"name": symbol.obj.name, "cls": symbol.obj, "external": None, "status": status, "line": line})
