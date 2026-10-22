"""
Type annotations as structured values: `Optional[List[Item]]`, `Annotated[int, Query(ge=1)]`, aliases,
generics and string forward references, rendered in one canonical form so that modernising the syntax
(`List[int]` -> `list[int]`, `Optional[X]` -> `X | None`) is not seen as a contract change.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .pyindex import (
    ConstEvaluator, ClassInfo, ModuleInfo, PythonIndex, UNKNOWN, call_parts, dotted, keyword_args, positional_args,
    string_value, strip_type, subscript_parts, text,
)

SIMPLE_NAMES = {
    "str", "int", "float", "bool", "bytes", "bytearray", "complex", "object", "Any", "datetime", "date", "time", "timedelta", "UUID",
    "Decimal", "Path", "EmailStr", "NameEmail", "HttpUrl", "AnyUrl", "AnyHttpUrl", "Url", "SecretStr", "SecretBytes", "IPv4Address",
    "IPv6Address", "IPvAnyAddress", "PositiveInt", "NegativeInt", "NonNegativeInt", "NonPositiveInt", "PositiveFloat", "NegativeFloat",
    "NonNegativeFloat", "NonPositiveFloat", "StrictStr", "StrictInt", "StrictFloat", "StrictBool", "StrictBytes", "Json", "FilePath",
    "DirectoryPath", "PastDate", "FutureDate", "PastDatetime", "FutureDatetime", "UploadFile", "File", "Pattern", "ObjectId",
    "PydanticObjectId", "PostgresDsn", "RedisDsn", "MongoDsn", "AmqpDsn", "KafkaDsn", "FileUrl", "WebsocketUrl", "SecretPath",
    "Color", "PaymentCardNumber", "ByteSize", "IPv4Network", "IPv6Network", "IPv4Interface", "IPv6Interface", "NoneType", "range",
    "frozenset_", "bson_ObjectId", "ULID", "PhoneNumber", "Base64Bytes", "Base64Str", "Base64UrlBytes", "Base64UrlStr", "UUID1",
    "UUID3", "UUID4", "UUID5", "Timestamp",
}
LIST_ORIGINS = {"list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection", "Deque", "deque", "AsyncIterable",
                "AsyncIterator", "Generator", "AsyncGenerator", "Set", "set", "FrozenSet", "frozenset", "AbstractSet", "MutableSet",
                "conlist", "Tuple_", "Array"}
SET_ORIGINS = {"Set", "set", "FrozenSet", "frozenset", "AbstractSet", "MutableSet"}
TUPLE_ORIGINS = {"Tuple", "tuple"}
DICT_ORIGINS = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict", "defaultdict", "Counter", "TypedDict_"}
WRAPPER_ORIGINS = {"Type", "type", "ClassVar", "Final", "Required", "NotRequired", "ReadOnly", "InitVar", "Awaitable", "Coroutine", "Mapped",
                   "WriteOnlyMapped", "DynamicMapped", "Annotated_", "Depends_", "Optional_", "Strict", "Lazy"}
GENERATOR_ORIGINS = {"Generator", "AsyncGenerator", "Iterator", "AsyncIterator", "Iterable", "AsyncIterable"}
RESPONSE_WRAPPERS = {"Response", "JSONResponse", "ORJSONResponse", "UJSONResponse", "HTMLResponse", "PlainTextResponse",
                     "StreamingResponse", "FileResponse", "RedirectResponse"}
CONSTRAINED = {"conint": "int", "confloat": "float", "constr": "str", "conbytes": "bytes", "condecimal": "Decimal", "conlist": "list",
               "conset": "set", "confrozenset": "frozenset", "condate": "date"}


@dataclass
class Meta:
    node: object
    module: ModuleInfo


@dataclass
class TypeExpr:
    kind: str                      # simple | class | list | set | tuple | dict | optional | union | literal | none | any | unknown
    name: str = ""
    args: List["TypeExpr"] = field(default_factory=list)
    cls: Optional[ClassInfo] = None
    metas: List[Meta] = field(default_factory=list)
    literal: list = field(default_factory=list)
    constraints: dict = field(default_factory=dict)
    module: Optional[ModuleInfo] = None
    qualified: str = ""            # dotted library name (sqlalchemy.orm.Session) for types from packages

    @property
    def nullable(self) -> bool:
        return self.kind == "optional"

    def unwrap_optional(self) -> "TypeExpr":
        current = self
        while current.kind == "optional" and current.args:
            inner = current.args[0]
            inner.metas = current.metas + inner.metas
            current = inner
        return current


class TypeParser:

    def __init__(self, index: PythonIndex, const: ConstEvaluator):
        self.index = index
        self.const = const

    # ------------------------------------------------------------------
    def parse(self, node, module: ModuleInfo, depth=0, bindings=None) -> TypeExpr:
        """TypeExpr of an annotation node. `bindings` substitutes generic type variables {name: TypeExpr}."""
        node = strip_type(node)
        if node is None or depth > 12:
            return TypeExpr("unknown", "Any", module=module)
        kind = node.type

        if kind == "none":
            return TypeExpr("none", "None", module=module)
        if kind in ("string", "concatenated_string"):
            value = string_value(node)
            if value:
                parsed = self.index.parse_annotation(value)
                if parsed is not None:
                    return self.parse(parsed, module, depth + 1, bindings)
            return TypeExpr("unknown", value or "str", module=module)
        if kind == "ellipsis":
            return TypeExpr("simple", "...", module=module)
        if kind in ("integer", "float", "true", "false"):
            return TypeExpr("literal", "Literal", literal=[self.const.value(node, module)], module=module)

        if kind in ("binary_operator", "union_type"):
            op = node.child_by_field_name("operator")
            if kind == "union_type" or (op is not None and text(op) == "|"):
                return self._union(self._flatten_union(node), module, depth, bindings)

        if kind in ("subscript", "generic_type"):
            return self._subscript(node, module, depth, bindings)

        if kind == "call":
            return self._call_annotation(node, module, depth, bindings)

        if kind in ("identifier", "attribute"):
            return self._name(node, module, depth, bindings)

        if kind == "list":      # Callable[[int], str] parameter lists
            return TypeExpr("unknown", "list", module=module)

        return TypeExpr("unknown", text(node)[:40], module=module)

    # ------------------------------------------------------------------
    def _flatten_union(self, node) -> list:
        node = strip_type(node)
        if node.type == "union_type" or (node.type == "binary_operator" and text(node.child_by_field_name("operator")) == "|"):
            parts = []
            children = [c for c in node.named_children if c.type != "comment"]
            if node.type == "binary_operator":
                children = [node.child_by_field_name("left"), node.child_by_field_name("right")]
            for c in children:
                parts.extend(self._flatten_union(c))
            return parts
        return [node]

    def _union(self, parts, module, depth, bindings) -> TypeExpr:
        members = [self.parse(p, module, depth + 1, bindings) for p in parts]
        non_none = [m for m in members if m.kind != "none"]
        has_none = len(non_none) != len(members)
        if len(non_none) == 1:
            inner = non_none[0]
            return TypeExpr("optional", args=[inner], module=module) if has_none else inner
        if not non_none:
            return TypeExpr("none", "None", module=module)
        union = TypeExpr("union", args=non_none, module=module)
        return TypeExpr("optional", args=[union], module=module) if has_none else union

    def _name(self, node, module, depth, bindings) -> TypeExpr:
        name = dotted(node) or text(node)
        last = name.rsplit(".", 1)[-1]
        if bindings and name in bindings:
            return bindings[name]
        symbol = self.index.resolve_name(name, module)

        if symbol.kind == "class" and not symbol.rest:
            return TypeExpr("class", symbol.obj.name, cls=symbol.obj, module=symbol.module)

        if symbol.kind == "variable" and not symbol.rest:
            value = symbol.obj.value
            if value is not None and self._is_alias_value(value, symbol.module):
                return self.parse(value, symbol.module, depth + 1, bindings)
            return TypeExpr("unknown", last, module=module)

        qualified = symbol.qualified if symbol.kind == "external" else None
        if qualified is None and symbol.kind == "external":
            qualified = name
        origin = (qualified or name).rsplit(".", 1)[-1]

        if origin in ("Any", "object") or origin == "Any":
            return TypeExpr("any", "Any", module=module)
        if origin == "NoneType":
            return TypeExpr("none", "None", module=module)
        if origin in ("List", "list", "Sequence", "Iterable", "Collection", "Set", "set", "FrozenSet", "frozenset", "Tuple", "tuple",
                      "Deque", "deque"):
            kind = "set" if origin in SET_ORIGINS else "tuple" if origin in TUPLE_ORIGINS else "list"
            return TypeExpr(kind, origin.lower(), args=[TypeExpr("any", "Any", module=module)], module=module)
        if origin in DICT_ORIGINS:
            return TypeExpr("dict", "dict", args=[TypeExpr("simple", "str", module=module), TypeExpr("any", "Any", module=module)], module=module)
        if origin == "Optional":
            return TypeExpr("optional", args=[TypeExpr("any", "Any", module=module)], module=module)
        if origin in CONSTRAINED:
            return TypeExpr("simple", CONSTRAINED[origin], module=module)
        return TypeExpr("simple", self._canonical_simple(qualified or name, origin), module=module, qualified=qualified or "")

    @staticmethod
    def _canonical_simple(qualified: str, origin: str) -> str:
        if qualified in ("datetime.datetime",):
            return "datetime"
        return origin

    def _is_alias_value(self, value, module) -> bool:
        """`Name = Annotated[...] / list[X] / Optional[X] / X | Y` are type aliases; other assignments are not."""
        value = strip_type(value)
        if value.type in ("subscript", "generic_type", "union_type"):
            return True
        if value.type == "binary_operator" and text(value.child_by_field_name("operator")) == "|":
            return True
        if value.type in ("identifier", "attribute"):
            symbol = self.index.resolve_name(dotted(value) or "", module)
            return symbol.kind in ("class",) or (symbol.kind == "external" and (symbol.qualified or "").rsplit(".", 1)[-1] in SIMPLE_NAMES)
        return False

    def _call_annotation(self, node, module, depth, bindings) -> TypeExpr:
        func, args = call_parts(node)
        name = (dotted(func) or "").rsplit(".", 1)[-1]
        if name in CONSTRAINED:
            kw = keyword_args(args)
            constraints = {}
            for key, value_node in kw.items():
                value = self.const.value(value_node, module)
                if value is not UNKNOWN:
                    constraints[key] = value
            pos = positional_args(args)
            base = TypeExpr("simple", CONSTRAINED[name], constraints=constraints, module=module)
            if name in ("conlist", "conset", "confrozenset") and pos:
                item = self.parse(pos[0], module, depth + 1, bindings)
                return TypeExpr("list" if name == "conlist" else "set", name, args=[item], constraints=constraints, module=module)
            return base
        return TypeExpr("unknown", name or text(node)[:30], module=module)

    def _subscript(self, node, module, depth, bindings) -> TypeExpr:
        head, items = subscript_parts(node)
        if head is None:
            return TypeExpr("unknown", text(node)[:40], module=module)
        head_name = dotted(head) or ""
        symbol = self.index.resolve_name(head_name, module) if head_name else None
        origin = head_name.rsplit(".", 1)[-1]
        if symbol is not None and symbol.kind == "external" and symbol.qualified:
            origin = symbol.qualified.rsplit(".", 1)[-1]

        # a project generic class: Page[Item]
        if symbol is not None and symbol.kind == "class" and not symbol.rest:
            cls = symbol.obj
            args = [self.parse(i, module, depth + 1, bindings) for i in items]
            return TypeExpr("class", cls.name, args=args, cls=cls, module=symbol.module)
        if symbol is not None and symbol.kind == "variable" and not symbol.rest and symbol.obj.value is not None:
            # alias of a generic: ItemPage = Page[Item][...]
            return self.parse(symbol.obj.value, symbol.module, depth + 1, bindings)

        if origin == "Annotated" and items:
            inner = self.parse(items[0], module, depth + 1, bindings)
            inner = TypeExpr(**{**inner.__dict__})        # do not mutate cached aliases
            inner.metas = inner.metas + [Meta(i, module) for i in items[1:]]
            return inner
        if origin == "Optional" and items:
            return TypeExpr("optional", args=[self.parse(items[0], module, depth + 1, bindings)], module=module)
        if origin == "Union":
            return self._union(items, module, depth, bindings)
        if origin == "Literal":
            values = [self.const.value(i, module) for i in items]
            return TypeExpr("literal", "Literal", literal=[v for v in values if v is not UNKNOWN], module=module)
        if origin in WRAPPER_ORIGINS and items:
            return self.parse(items[0], module, depth + 1, bindings)
        if origin in GENERATOR_ORIGINS and items and origin not in ("Iterable", "AsyncIterable"):
            return self.parse(items[0], module, depth + 1, bindings)
        if origin in TUPLE_ORIGINS:
            real = [i for i in items if text(i) != "..."]
            args = [self.parse(i, module, depth + 1, bindings) for i in real]
            homogeneous = len(items) == 2 and text(items[1]) == "..."
            return TypeExpr("tuple", "tuple", args=args[:1] if homogeneous else args, module=module,
                            constraints={"homogeneous": homogeneous})
        if origin in LIST_ORIGINS and items:
            kind = "set" if origin in SET_ORIGINS else "list"
            return TypeExpr(kind, origin.lower(), args=[self.parse(items[0], module, depth + 1, bindings)], module=module)
        if origin in DICT_ORIGINS and items:
            args = [self.parse(i, module, depth + 1, bindings) for i in items[:2]]
            while len(args) < 2:
                args.append(TypeExpr("any", "Any", module=module))
            return TypeExpr("dict", "dict", args=args, module=module)
        if origin in CONSTRAINED:
            return TypeExpr("simple", CONSTRAINED[origin], module=module)
        if origin in ("Json",):
            return TypeExpr("simple", "Json", module=module)
        args = [self.parse(i, module, depth + 1, bindings) for i in items]
        qualified = symbol.qualified if symbol is not None and symbol.kind == "external" and symbol.qualified else ""
        return TypeExpr("simple" if origin in SIMPLE_NAMES else "unknown", origin, args=args, module=module, qualified=qualified)

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def render(self, te: TypeExpr) -> str:
        kind = te.kind
        if kind == "optional":
            inner = self.render(te.args[0]) if te.args else "Any"
            return f"{inner} | None"
        if kind == "union":
            return " | ".join(self.render(a) for a in te.args)
        if kind == "list":
            return f"list[{self.render(te.args[0])}]" if te.args else "list"
        if kind == "set":
            return f"set[{self.render(te.args[0])}]" if te.args else "set"
        if kind == "tuple":
            if te.constraints.get("homogeneous") and te.args:
                return f"tuple[{self.render(te.args[0])}, ...]"
            return f"tuple[{', '.join(self.render(a) for a in te.args)}]" if te.args else "tuple"
        if kind == "dict":
            return f"dict[{self.render(te.args[0])}, {self.render(te.args[1])}]" if len(te.args) == 2 else "dict"
        if kind == "literal":
            return "Literal[" + ", ".join(repr(v) for v in te.literal) + "]"
        if kind == "none":
            return "None"
        if kind == "any":
            return "Any"
        if kind == "class" and te.args:
            return f"{te.name}[{', '.join(self.render(a) for a in te.args)}]"
        return te.name or "Any"


def generic_parameters(index: PythonIndex, cls: ClassInfo) -> List[str]:
    """Names of the type variables a class is generic over: `class Page(BaseModel, Generic[T])`."""
    names = []
    for base in cls.bases:
        head, items = subscript_parts(base)
        if head is None:
            continue
        origin = (dotted(head) or "").rsplit(".", 1)[-1]
        if origin == "Generic":
            names.extend(text(i) for i in items)
        elif origin not in ("Annotated",):
            # class Page(BaseModel[T]) / class Child(Page[T]) with free variables
            names.extend(text(i) for i in items if re.fullmatch(r"[A-Z]\w{0,20}", text(i)) and text(i) not in SIMPLE_NAMES)
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen
