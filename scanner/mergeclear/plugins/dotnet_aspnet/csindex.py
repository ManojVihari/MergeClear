"""
C# source index: types, members, attributes, usings and type resolution across files, plus constant evaluation.
Nothing here knows about ASP.NET; it is the foundation the analyzer builds on.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

UNKNOWN = object()
MAX_DEPTH = 8

TYPE_KINDS = {
    "class_declaration": "class", "struct_declaration": "struct", "record_declaration": "record",
    "record_struct_declaration": "record", "interface_declaration": "interface", "enum_declaration": "enum",
}
CANONICAL_TYPES = {
    "String": "string", "Int32": "int", "Int64": "long", "Int16": "short", "UInt32": "uint", "UInt64": "ulong", "Boolean": "bool",
    "Double": "double", "Single": "float", "Decimal": "decimal", "Byte": "byte", "SByte": "sbyte", "Char": "char", "Object": "object",
}
HTTP_STATUS = {
    "Continue": 100, "OK": 200, "Created": 201, "Accepted": 202, "NonAuthoritativeInformation": 203, "NoContent": 204, "ResetContent": 205,
    "PartialContent": 206, "MultiStatus": 207, "MultipleChoices": 300, "Moved": 301, "MovedPermanently": 301, "Found": 302, "Redirect": 302,
    "SeeOther": 303, "NotModified": 304, "TemporaryRedirect": 307, "PermanentRedirect": 308, "BadRequest": 400, "Unauthorized": 401,
    "PaymentRequired": 402, "Forbidden": 403, "NotFound": 404, "MethodNotAllowed": 405, "NotAcceptable": 406, "RequestTimeout": 408,
    "Conflict": 409, "Gone": 410, "LengthRequired": 411, "PreconditionFailed": 412, "RequestEntityTooLarge": 413, "PayloadTooLarge": 413,
    "UnsupportedMediaType": 415, "ExpectationFailed": 417, "UnprocessableEntity": 422, "UnprocessableContent": 422, "Locked": 423,
    "FailedDependency": 424, "UpgradeRequired": 426, "PreconditionRequired": 428, "TooManyRequests": 429,
    "InternalServerError": 500, "NotImplemented": 501, "BadGateway": 502, "ServiceUnavailable": 503, "GatewayTimeout": 504,
}


# ============================================================================
# SYNTAX HELPERS
# ============================================================================

def text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def line_of(node) -> int:
    return node.start_point[0] + 1


def walk(node) -> Iterator:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def named(node, kind: str) -> list:
    return [c for c in node.named_children if c.type == kind]


def first(node, *kinds):
    return next((c for c in node.named_children if c.type in kinds), None)


def unparen(node):
    while node is not None and node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    return node


def strip_await(node):
    node = unparen(node)
    while node is not None and node.type == "await_expression" and node.named_children:
        node = unparen(node.named_children[0])
    return node


def argument_nodes(call) -> List[Tuple[Optional[str], object]]:
    """[(name or None, expression node)] of an invocation / object creation."""
    args = call.child_by_field_name("arguments") if call is not None else None
    if args is None:
        args = first(call, "argument_list") if call is not None else None
    out = []
    if args is None:
        return out
    for a in args.named_children:
        if a.type != "argument":
            continue
        label = first(a, "name_colon")
        expression = next((c for c in a.named_children if c.type != "name_colon"), None)
        if expression is not None:
            out.append((text(label.named_children[0]) if label is not None and label.named_children else None, expression))
    return out


def positional(args) -> list:
    return [e for n, e in args if n is None]


def keyword(args) -> dict:
    return {n: e for n, e in args if n is not None}


def invocation_parts(node):
    """(receiver node or None, method name, generic type args, argument pairs) of `a.b<T>(x)` / `b(x)`."""
    node = strip_await(node)
    if node is None or node.type != "invocation_expression":
        return None
    function = node.child_by_field_name("function")
    args = argument_nodes(node)
    if function is None:
        return None
    if function.type == "member_access_expression":
        name_node = function.child_by_field_name("name")
        receiver = function.child_by_field_name("expression")
    elif function.type in ("identifier", "generic_name"):
        name_node, receiver = function, None
    elif function.type == "member_binding_expression":          # a?.b()
        name_node, receiver = function.child_by_field_name("name"), None
    else:
        return None
    if name_node is None:
        return None
    if name_node.type == "generic_name":
        ident = first(name_node, "identifier")
        type_args = [parse_type(c) for c in (first(name_node, "type_argument_list").named_children if first(name_node, "type_argument_list") else [])]
        return receiver, text(ident), type_args, args
    return receiver, text(name_node), [], args


# ============================================================================
# TYPES
# ============================================================================

@dataclass(frozen=True)
class TypeRef:
    name: str                                   # simple name: List, int, OrderDto
    args: Tuple["TypeRef", ...] = ()
    nullable: bool = False
    array: int = 0
    qualifier: str = ""                         # System.Collections.Generic
    tuple_items: Tuple["TypeRef", ...] = ()

    def __str__(self) -> str:
        base = self.name
        if self.tuple_items:
            base = "(" + ", ".join(str(t) for t in self.tuple_items) + ")"
        elif self.args:
            base += "<" + ", ".join(str(a) for a in self.args) + ">"
        return base + "[]" * self.array + ("?" if self.nullable else "")

    def with_nullable(self, value=True) -> "TypeRef":
        return TypeRef(self.name, self.args, value, self.array, self.qualifier, self.tuple_items)

    def element(self) -> "TypeRef":
        return TypeRef(self.name, self.args, False, max(self.array - 1, 0), self.qualifier, self.tuple_items)


VOID = TypeRef("void")
OBJECT = TypeRef("object")


def parse_type(node) -> TypeRef:
    if node is None:
        return OBJECT
    kind = node.type
    if kind in ("predefined_type", "identifier", "implicit_type", "void_keyword"):
        name = text(node)
        return TypeRef(CANONICAL_TYPES.get(name, name))
    if kind == "generic_name":
        ident = first(node, "identifier")
        arguments = first(node, "type_argument_list")
        return TypeRef(text(ident), tuple(parse_type(c) for c in (arguments.named_children if arguments is not None else [])))
    if kind == "qualified_name":
        right = parse_type(node.child_by_field_name("name"))
        left = text(node.child_by_field_name("qualifier"))
        return TypeRef(right.name, right.args, right.nullable, right.array, left)
    if kind == "alias_qualified_name":
        return parse_type(node.child_by_field_name("name"))
    if kind == "nullable_type":
        return parse_type(node.child_by_field_name("type") or node.named_children[0]).with_nullable()
    if kind == "array_type":
        inner = parse_type(node.child_by_field_name("type") or node.named_children[0])
        rank = first(node, "array_rank_specifier")
        return TypeRef(inner.name, inner.args, inner.nullable, inner.array + (1 if rank is not None else 1), inner.qualifier)
    if kind == "tuple_type":
        items = tuple(parse_type(c.child_by_field_name("type") or c.named_children[0]) for c in node.named_children if c.type == "tuple_element")
        return TypeRef("ValueTuple", (), False, 0, "", items)
    if kind in ("ref_type", "scoped_type") and node.named_children:
        return parse_type(node.named_children[-1])
    return TypeRef(text(node)[:40] or "object")


# ============================================================================
# MODEL
# ============================================================================

@dataclass(eq=False)
class FileInfo:
    rel: str
    root: object
    code: bytes
    project: str = ""
    usings: List[str] = field(default_factory=list)               # imported namespaces (global usings included)
    aliases: Dict[str, str] = field(default_factory=dict)
    static_usings: List[str] = field(default_factory=list)
    types: List["TypeInfo"] = field(default_factory=list)
    top_level: Optional["MethodInfo"] = None                       # Program.cs top-level statements


@dataclass(eq=False)
class Attr:
    name: str                                                      # without the Attribute suffix
    node: object
    args: list = field(default_factory=list)                       # positional argument expressions
    named: Dict[str, object] = field(default_factory=dict)         # Name = value
    target: Optional[str] = None                                   # property: / field: / return:
    file: Optional[FileInfo] = None

    @property
    def line(self) -> int:
        return line_of(self.node)


@dataclass(eq=False)
class ParamInfo:
    name: str
    type: TypeRef
    attrs: List[Attr] = field(default_factory=list)
    default: object = None
    modifiers: Tuple[str, ...] = ()
    node: object = None

    def attr(self, *names) -> Optional[Attr]:
        return next((a for a in self.attrs if a.name in names), None)


@dataclass(eq=False)
class MethodInfo:
    id: str
    name: str
    owner: Optional["TypeInfo"]
    node: object
    file: FileInfo
    attrs: List[Attr] = field(default_factory=list)
    modifiers: Tuple[str, ...] = ()
    params: List[ParamInfo] = field(default_factory=list)
    returns: TypeRef = VOID
    type_params: List[str] = field(default_factory=list)
    body: List[object] = field(default_factory=list)               # roots to walk: [block] / [expression] / global statements
    is_async: bool = False
    is_ctor: bool = False
    line_start: int = 0
    line_end: int = 0
    doc: Optional[str] = None

    def attr(self, *names) -> Optional[Attr]:
        return next((a for a in self.attrs if a.name in names), None)

    @property
    def label(self) -> str:
        return f"{self.owner.name}.{self.name}" if self.owner else self.name


@dataclass(eq=False)
class MemberInfo:
    """A property or a field."""
    name: str
    type: TypeRef
    owner: "TypeInfo"
    node: object
    attrs: List[Attr] = field(default_factory=list)
    modifiers: Tuple[str, ...] = ()
    init: object = None
    kind: str = "property"                                         # property | field
    has_setter: bool = True
    line: int = 0
    primary: bool = False                                          # positional record parameter / primary constructor parameter
    doc: Optional[str] = None

    def attr(self, *names) -> Optional[Attr]:
        return next((a for a in self.attrs if a.name in names), None)

    @property
    def is_static(self) -> bool:
        return "static" in self.modifiers

    @property
    def is_const(self) -> bool:
        return "const" in self.modifiers


@dataclass(eq=False)
class TypeInfo:
    name: str
    qualified: str
    namespace: str
    kind: str                                                      # class | struct | record | interface | enum
    node: object
    file: FileInfo
    outer: Optional["TypeInfo"] = None
    modifiers: Tuple[str, ...] = ()
    attrs: List[Attr] = field(default_factory=list)
    bases: List[TypeRef] = field(default_factory=list)
    type_params: List[str] = field(default_factory=list)
    methods: List[MethodInfo] = field(default_factory=list)
    ctors: List[MethodInfo] = field(default_factory=list)
    members: List[MemberInfo] = field(default_factory=list)
    primary: List[ParamInfo] = field(default_factory=list)
    enum_members: List[Tuple[str, object]] = field(default_factory=list)
    nested: List["TypeInfo"] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    doc: Optional[str] = None
    parts: List[object] = field(default_factory=list)              # nodes of every partial declaration

    @property
    def id(self) -> str:
        return self.qualified

    def attr(self, *names) -> Optional[Attr]:
        return next((a for a in self.attrs if a.name in names), None)

    def member(self, name: str) -> Optional[MemberInfo]:
        return next((m for m in self.members if m.name == name), None)

    @property
    def is_static(self) -> bool:
        return "static" in self.modifiers

    @property
    def is_abstract(self) -> bool:
        return self.kind == "interface" or "abstract" in self.modifiers


# ============================================================================
# PARSING
# ============================================================================

def doc_comment(node) -> Optional[str]:
    """`/// <summary>` text above a declaration."""
    lines = []
    sibling = node.prev_sibling
    while sibling is not None and sibling.type == "comment" and text(sibling).lstrip().startswith("///"):
        lines.insert(0, text(sibling).lstrip()[3:].strip())
        sibling = sibling.prev_sibling
    if not lines:
        return None
    joined = " ".join(lines)
    match = re.search(r"<summary>(.*?)</summary>", joined, re.S)
    body = match.group(1) if match else joined
    cleaned = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", cleaned).strip() or None


def parse_attributes(owner_node, file: FileInfo) -> List[Attr]:
    out = []
    for lst in named(owner_node, "attribute_list"):
        target = None
        specifier = first(lst, "attribute_target_specifier")
        if specifier is not None:
            target = text(specifier.named_children[0]) if specifier.named_children else text(specifier).rstrip(":").strip()
        for attribute in named(lst, "attribute"):
            name_node = attribute.child_by_field_name("name")
            name = text(name_node).rsplit(".", 1)[-1]
            if name.endswith("Attribute") and len(name) > 9:
                name = name[:-9]
            attr = Attr(name, attribute, target=target, file=file)
            arguments = first(attribute, "attribute_argument_list")
            if arguments is not None:
                for a in named(arguments, "attribute_argument"):
                    label = a.child_by_field_name("name")
                    colon = first(a, "name_colon")
                    value = next((c for c in a.named_children if c.type not in ("name_equals", "name_colon") and (label is None or c.start_byte != label.start_byte)), None)
                    if label is not None and value is not None:
                        attr.named[text(label)] = value
                    elif colon is not None and value is not None:
                        attr.named[text(colon.named_children[0])] = value
                    else:
                        equals = first(a, "name_equals")
                        if equals is not None and value is not None:
                            attr.named[text(equals.named_children[0])] = value
                        elif value is not None:
                            attr.args.append(value)
            out.append(attr)
    return out


def modifiers_of(node) -> Tuple[str, ...]:
    return tuple(text(c) for c in node.children if c.type == "modifier")


def parse_parameters(plist, file: FileInfo) -> List[ParamInfo]:
    out = []
    if plist is None:
        return out
    for p in named(plist, "parameter"):
        type_node = p.child_by_field_name("type")
        default = None
        seen_equals = False
        for c in p.children:
            if c.type == "=":
                seen_equals = True
            elif seen_equals and c.is_named:
                default = c
                break
        name_node = p.child_by_field_name("name")
        out.append(ParamInfo(text(name_node), parse_type(type_node), parse_attributes(p, file), default, modifiers_of(p), p))
    return out


class CsIndex:

    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.files: Dict[str, FileInfo] = {}
        self.types: Dict[str, TypeInfo] = {}
        self.by_name: Dict[str, List[TypeInfo]] = {}
        self.extensions: Dict[str, List[MethodInfo]] = {}
        self.methods: Dict[str, MethodInfo] = {}
        self._global_usings: List[str] = []
        self._subtypes: Dict[str, List[TypeInfo]] = {}
        self._resolve_cache: Dict[tuple, Optional[TypeInfo]] = {}

    # ----------------------------
    # building
    # ----------------------------

    def add_file(self, rel: str, code: bytes, root, project: str = "") -> FileInfo:
        file = FileInfo(rel=rel, root=root, code=code, project=project)
        self.files[rel] = file
        self._parse_compilation_unit(file, root)
        return file

    def _parse_compilation_unit(self, file: FileInfo, root):
        statements = []
        namespace = ""                                    # `namespace A.B;` applies to everything that follows it
        for child in root.named_children:
            kind = child.type
            if kind == "using_directive":
                self._parse_using(file, child)
            elif kind == "file_scoped_namespace_declaration":
                namespace = text(child.child_by_field_name("name"))
            elif kind == "namespace_declaration":
                self._parse_namespace(file, child, "")
            elif kind in TYPE_KINDS:
                self._parse_type(file, child, namespace, None)
            elif kind == "global_statement":
                statements.append(child)
        if statements:
            top = MethodInfo(
                id=f"{file.rel}#top-level", name="<Main>", owner=None, node=root, file=file, body=statements,
                line_start=line_of(statements[0]), line_end=statements[-1].end_point[0] + 1, is_async=True,
            )
            file.top_level = top
            self.methods[top.id] = top

    def _parse_using(self, file: FileInfo, node):
        is_global = any(c.type == "global" for c in node.children) or text(node).startswith("global ")
        is_static = any(c.type == "static" for c in node.children)
        alias = node.child_by_field_name("name")
        target = next((c for c in node.named_children if c.type in ("identifier", "qualified_name") and c is not alias), None)
        if target is None:
            return
        name = text(target)
        if alias is not None and any(c.type == "=" for c in node.children):
            file.aliases[text(alias)] = name
        elif is_static:
            file.static_usings.append(name)
        else:
            file.usings.append(name)
            if is_global:
                self._global_usings.append(name)

    def _parse_namespace(self, file: FileInfo, node, parent: str):
        name = text(node.child_by_field_name("name"))
        namespace = f"{parent}.{name}" if parent else name
        body = node.child_by_field_name("body") or node
        for child in body.named_children:
            kind = child.type
            if kind in TYPE_KINDS:
                self._parse_type(file, child, namespace, None)
            elif kind in ("namespace_declaration", "file_scoped_namespace_declaration"):
                self._parse_namespace(file, child, namespace)
            elif kind == "using_directive":
                self._parse_using(file, child)

    def _parse_type(self, file: FileInfo, node, namespace: str, outer: Optional[TypeInfo]) -> TypeInfo:
        kind = TYPE_KINDS[node.type]
        name = text(node.child_by_field_name("name"))
        qualified = f"{outer.qualified}.{name}" if outer else (f"{namespace}.{name}" if namespace else name)

        existing = self.types.get(qualified)
        info = existing or TypeInfo(
            name=name, qualified=qualified, namespace=namespace, kind=kind, node=node, file=file, outer=outer,
            line_start=line_of(node), line_end=node.end_point[0] + 1, doc=doc_comment(node),
        )
        info.parts.append(node)
        info.modifiers = tuple(dict.fromkeys(info.modifiers + modifiers_of(node)))
        info.attrs.extend(parse_attributes(node, file))
        if existing is None:
            self.types[qualified] = info
            self.by_name.setdefault(name, []).append(info)
            file.types.append(info)
            if outer is not None:
                outer.nested.append(info)
        else:
            info.line_end = max(info.line_end, node.end_point[0] + 1)

        type_params = first(node, "type_parameter_list")
        if type_params is not None:
            info.type_params = [text(first(p, "identifier")) for p in named(type_params, "type_parameter")]
        base_list = first(node, "base_list")
        if base_list is not None:
            for b in base_list.named_children:
                if b.type in ("identifier", "generic_name", "qualified_name", "primary_constructor_base_type"):
                    target = b if b.type != "primary_constructor_base_type" else (b.named_children[0] if b.named_children else b)
                    info.bases.append(parse_type(target))
        plist = first(node, "parameter_list")
        if plist is not None:
            info.primary = parse_parameters(plist, file)
            for p in info.primary:
                if kind == "record":
                    info.members.append(MemberInfo(p.name, p.type, info, p.node, [a for a in p.attrs], (), None, "property", True,
                                                   line_of(p.node), True))
                else:
                    info.members.append(MemberInfo(p.name, p.type, info, p.node, [], ("private", "readonly"), None, "field", False,
                                                   line_of(p.node), True))

        body = node.child_by_field_name("body") or first(node, "declaration_list", "enum_member_declaration_list")
        if body is not None:
            self._parse_members(file, info, body)
        return info

    def _parse_members(self, file: FileInfo, info: TypeInfo, body):
        for m in body.named_children:
            kind = m.type
            if kind in TYPE_KINDS:
                self._parse_type(file, m, info.namespace, info)
            elif kind == "method_declaration":
                self._add_method(file, info, m, False)
            elif kind == "constructor_declaration":
                self._add_method(file, info, m, True)
            elif kind == "property_declaration":
                self._add_property(file, info, m)
            elif kind == "field_declaration":
                self._add_field(file, info, m)
            elif kind == "enum_member_declaration":
                value = next((c for c in m.named_children if c.type not in ("identifier", "attribute_list")), None)
                info.enum_members.append((text(m.child_by_field_name("name")), value))
            elif kind == "indexer_declaration":
                pass

    def _add_method(self, file: FileInfo, info: TypeInfo, node, ctor: bool):
        name = text(node.child_by_field_name("name")) if not ctor else info.name
        parameters = parse_parameters(node.child_by_field_name("parameters") or first(node, "parameter_list"), file)
        body_node = node.child_by_field_name("body")
        expression = first(node, "arrow_expression_clause")
        roots = [body_node] if body_node is not None else ([expression] if expression is not None else [])
        type_params_node = first(node, "type_parameter_list")
        returns = parse_type(node.child_by_field_name("returns")) if not ctor else VOID
        method = MethodInfo(
            id=f"{info.qualified}.{name}({len(parameters)})#{line_of(node)}", name=name, owner=info, node=node, file=file,
            attrs=parse_attributes(node, file), modifiers=modifiers_of(node), params=parameters, returns=returns,
            type_params=[text(first(p, "identifier")) for p in named(type_params_node, "type_parameter")] if type_params_node is not None else [],
            body=roots, is_async="async" in modifiers_of(node), is_ctor=ctor, line_start=line_of(node), line_end=node.end_point[0] + 1,
            doc=doc_comment(node),
        )
        (info.ctors if ctor else info.methods).append(method)
        self.methods[method.id] = method
        if not ctor and parameters and "this" in parameters[0].modifiers and info.is_static:
            self.extensions.setdefault(name, []).append(method)

    def _add_property(self, file: FileInfo, info: TypeInfo, node):
        accessors = node.child_by_field_name("accessors")
        has_setter = True
        if accessors is not None:
            kinds = [text(a.child_by_field_name("name")) if a.child_by_field_name("name") is not None else text(a).split()[0]
                     for a in named(accessors, "accessor_declaration")]
            has_setter = any(k in ("set", "init") for k in kinds)
        elif first(node, "arrow_expression_clause") is not None:
            has_setter = False
        init = node.child_by_field_name("value")
        info.members.append(MemberInfo(
            text(node.child_by_field_name("name")), parse_type(node.child_by_field_name("type")), info, node,
            parse_attributes(node, file), modifiers_of(node), init, "property", has_setter, line_of(node), False, doc_comment(node)))

    def _add_field(self, file: FileInfo, info: TypeInfo, node):
        declaration = first(node, "variable_declaration")
        if declaration is None:
            return
        type_ref = parse_type(declaration.child_by_field_name("type"))
        attributes = parse_attributes(node, file)
        for declarator in named(declaration, "variable_declarator"):
            init = next((c for c in declarator.named_children if c.type not in ("identifier", "bracketed_argument_list")), None)
            info.members.append(MemberInfo(text(declarator.child_by_field_name("name")), type_ref, info, node, attributes,
                                           modifiers_of(node), init, "field", True, line_of(node), False, doc_comment(node)))

    def finalize(self):
        self._resolve_cache.clear()
        self._subtypes.clear()
        for t in self.types.values():
            for base in t.bases:
                target = self.resolve(base.name, t.file, t.namespace, t)
                if target is not None and target is not t:
                    self._subtypes.setdefault(target.qualified, []).append(t)

    # ----------------------------
    # resolution
    # ----------------------------

    def resolve(self, name: str, file: Optional[FileInfo], namespace: str = "", context: Optional[TypeInfo] = None) -> Optional[TypeInfo]:
        """The project type a simple name refers to, preferring the caller's namespaces and imports."""
        if not name:
            return None
        key = (name, file.rel if file else None, namespace, context.qualified if context else None)
        if key in self._resolve_cache:
            return self._resolve_cache[key]
        found = self._resolve(name, file, namespace, context)
        self._resolve_cache[key] = found
        return found

    def _resolve(self, name, file, namespace, context):
        if "." in name:
            qualified = self.types.get(name)
            if qualified is not None:
                return qualified
            name = name.rsplit(".", 1)[-1]
        # nested types of the context type and its ancestors
        current = context
        while current is not None:
            for n in current.nested:
                if n.name == name:
                    return n
            current = current.outer
        if file is not None and name in file.aliases:
            target = self.types.get(file.aliases[name])
            if target is not None:
                return target
        candidates = list(self.by_name.get(name, []))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        scope = []
        ns = namespace
        while ns:
            scope.append(ns)
            ns = ns.rpartition(".")[0]
        imports = (file.usings if file else []) + self._global_usings
        for wanted in scope + imports:
            for t in candidates:
                if t.namespace == wanted:
                    return t
        same_file = [t for t in candidates if file is not None and t.file is file]
        if same_file:
            return same_file[0]
        same_project = [t for t in candidates if file is not None and t.file.project == file.project]
        return (same_project or candidates)[0]

    def bases(self, t: TypeInfo) -> List[TypeInfo]:
        out = []
        for b in t.bases:
            target = self.resolve(b.name, t.file, t.namespace, t)
            if target is not None and target is not t:
                out.append(target)
        return out

    def ancestors(self, t: TypeInfo) -> List[TypeInfo]:
        """The type followed by its project ancestors (classes and interfaces), breadth first."""
        out, queue = [], [t]
        while queue and len(out) < 60:
            current = queue.pop(0)
            if current in out:
                continue
            out.append(current)
            queue.extend(self.bases(current))
        return out

    def base_names(self, t: TypeInfo) -> List[str]:
        """Names of every base type up the chain, including those from libraries (ControllerBase, DbContext...)."""
        names = []
        for a in self.ancestors(t):
            names.extend(b.name for b in a.bases)
        return names

    def is_a(self, t: TypeInfo, *names: str) -> bool:
        wanted = set(names)
        return t.name in wanted or any(n in wanted for n in self.base_names(t))

    def subtypes(self, t: TypeInfo, transitive=True) -> List[TypeInfo]:
        out, queue = [], list(self._subtypes.get(t.qualified, []))
        while queue and len(out) < 300:
            current = queue.pop(0)
            if current in out:
                continue
            out.append(current)
            if transitive:
                queue.extend(self._subtypes.get(current.qualified, []))
        return out

    def find_method(self, t: TypeInfo, name: str, arity: Optional[int] = None) -> Optional[MethodInfo]:
        best = None
        for a in self.ancestors(t):
            for m in a.methods:
                if m.name != name:
                    continue
                required = len([p for p in m.params if p.default is None and "params" not in p.modifiers])
                if arity is None or required <= arity <= len(m.params) or any("params" in p.modifiers for p in m.params):
                    return m
                best = best or m
        return best

    def find_member(self, t: TypeInfo, name: str) -> Optional[MemberInfo]:
        for a in self.ancestors(t):
            m = a.member(name)
            if m is not None:
                return m
        return None

    def type_of_file(self, rel: str) -> List[TypeInfo]:
        file = self.files.get(rel)
        return file.types if file else []


# ============================================================================
# CONSTANTS
# ============================================================================

STATUS_CODE_NAME = re.compile(r"^Status(\d{3})")


def status_from_member(path: str) -> Optional[int]:
    """`StatusCodes.Status404NotFound` / `HttpStatusCode.NotFound` / `HttpStatusCode.Created` -> int."""
    parts = path.split(".")
    last = parts[-1]
    match = STATUS_CODE_NAME.match(last)
    if match:
        return int(match.group(1))
    if len(parts) >= 2 and parts[-2] == "HttpStatusCode":
        return HTTP_STATUS.get(last)
    return None


INTERPOLATION_PUNCTUATION = {"interpolation_alignment_clause", "interpolation_format_clause", "interpolation_brace", "interpolation_start",
                             "interpolation_quote", "string_literal_start", "string_literal_end"}


class ConstEvaluator:
    """Best-effort evaluation of constants: strings (interpolated, concatenated), numbers, status codes, config lookups."""

    CONFIG_NAME = re.compile(r"(config|configuration|settings|options)", re.I)

    def __init__(self, index: CsIndex):
        self.index = index

    def value(self, node, file: Optional[FileInfo], context: Optional[TypeInfo] = None, depth=0, env=None):
        node = unparen(node)
        if node is None or depth > MAX_DEPTH:
            return UNKNOWN
        kind = node.type

        if kind in ("string_literal", "verbatim_string_literal", "raw_string_literal", "character_literal"):
            return self._literal(node)
        if kind == "interpolated_string_expression":
            return self._interpolated(node, file, context, depth, env)
        if kind == "integer_literal":
            try:
                return int(re.sub(r"[uUlL_]", "", text(node)), 0)
            except ValueError:
                return UNKNOWN
        if kind == "real_literal":
            try:
                return float(re.sub(r"[fFdDmM_]", "", text(node)))
            except ValueError:
                return UNKNOWN
        if kind == "boolean_literal":
            return text(node) == "true"
        if kind == "null_literal":
            return None
        if kind == "binary_expression":
            return self._binary(node, file, context, depth, env)
        if kind == "prefix_unary_expression":
            inner = self.value(node.named_children[-1], file, context, depth + 1, env)
            if text(node).startswith("-") and isinstance(inner, (int, float)):
                return -inner
            return UNKNOWN
        if kind == "cast_expression":
            return self.value(node.child_by_field_name("value") or node.named_children[-1], file, context, depth + 1, env)
        if kind == "postfix_unary_expression" and node.named_children:            # value!
            return self.value(node.named_children[0], file, context, depth + 1, env)
        if kind in ("identifier", "member_access_expression", "qualified_name"):
            return self._reference(node, file, context, depth, env)
        if kind == "invocation_expression":
            return self._invocation(node, file, context, depth, env)
        if kind == "element_access_expression":
            return self._config_element(node)
        if kind == "typeof_expression":
            target = node.child_by_field_name("type") or node.named_children[-1]
            return ("typeof", str(parse_type(target)))
        if kind == "conditional_expression":
            return self.value(node.named_children[1], file, context, depth + 1, env)
        if kind in ("array_creation_expression", "implicit_array_creation_expression", "collection_expression", "initializer_expression",
                    "stackalloc_array_creation_expression"):
            container = first(node, "initializer_expression") or node
            return [self.value(c, file, context, depth + 1, env) for c in walk_children(container)]
        return UNKNOWN

    def _literal(self, node):
        raw = text(node)
        if node.type == "verbatim_string_literal":
            return raw[2:-1].replace('""', '"')
        if node.type == "raw_string_literal":
            return re.sub(r'^"""+\s*|\s*"""+$', "", raw)
        if node.type == "character_literal":
            return raw.strip("'")
        content = "".join(text(c) for c in node.children if c.type in ("string_literal_content", "escape_sequence"))
        content = content.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
        return content

    def _interpolated(self, node, file, context, depth, env):
        out = []
        for child in node.children:
            if child.type in ("string_content", "interpolated_string_text", "string_literal_content"):
                out.append(text(child))
            elif child.type == "escape_sequence":
                out.append(text(child))
            elif child.type == "interpolation":
                inner = next((c for c in child.named_children if c.type not in INTERPOLATION_PUNCTUATION), None)
                resolved = self.value(inner, file, context, depth + 1, env) if inner is not None else UNKNOWN
                out.append("{" + text(inner) + "}" if resolved is UNKNOWN or isinstance(resolved, (list, tuple)) or resolved is None
                           else str(resolved))
        return "".join(out).replace('\\"', '"')

    def _binary(self, node, file, context, depth, env):
        left = self.value(node.child_by_field_name("left"), file, context, depth + 1, env)
        right = self.value(node.child_by_field_name("right"), file, context, depth + 1, env)
        operator = text(node.child_by_field_name("operator"))
        if operator == "+":
            if isinstance(left, str) or isinstance(right, str):
                l_text = left if isinstance(left, str) else "{" + text(node.child_by_field_name("left")) + "}" if left is UNKNOWN else str(left)
                r_text = right if isinstance(right, str) else "{" + text(node.child_by_field_name("right")) + "}" if right is UNKNOWN else str(right)
                return l_text + r_text
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
        if operator == "*" and isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left * right
        return UNKNOWN

    def _reference(self, node, file, context, depth, env):
        path = text(node)
        if env and path in env:
            return env[path]
        status = status_from_member(path)
        if status is not None:
            return status
        parts = path.split(".")
        # a member of the context type: `Prefix`, `this.Prefix`, `Routes.Orders`
        owner, member = None, None
        if len(parts) == 1 and context is not None:
            member = self.index.find_member(context, parts[0])
            owner = member.owner if member else None
        elif len(parts) >= 2:
            target = self.index.resolve(parts[-2], file, context.namespace if context else "", context)
            if target is not None:
                if target.kind == "enum":
                    for name, value in target.enum_members:
                        if name == parts[-1]:
                            return self._enum_value(target, name)
                member = self.index.find_member(target, parts[-1])
                owner = member.owner if member else None
            elif parts[0] in ("this", "base") and context is not None:
                member = self.index.find_member(context, parts[-1])
                owner = member.owner if member else None
        if member is not None and member.init is not None and (member.is_const or member.is_static or "readonly" in member.modifiers):
            return self.value(member.init, owner.file, owner, depth + 1)
        if member is not None and member.kind == "property" and member.init is not None and member.is_static:
            return self.value(member.init, owner.file, owner, depth + 1)
        return UNKNOWN

    def _enum_value(self, enum: TypeInfo, name: str):
        counter = -1
        for member, value in enum.enum_members:
            if value is not None:
                v = self.value(value, enum.file, enum)
                counter = v if isinstance(v, int) else counter + 1
            else:
                counter += 1
            if member == name:
                return counter
        return UNKNOWN

    def _invocation(self, node, file, context, depth, env):
        parts = invocation_parts(node)
        if parts is None:
            return UNKNOWN
        receiver, name, type_args, args = parts
        if name == "nameof" and args:
            return text(args[0][1]).rsplit(".", 1)[-1]
        if name in ("GetValue", "GetConnectionString", "GetSection", "Get", "GetRequiredSection") and receiver is not None \
                and self.CONFIG_NAME.search(text(receiver)):
            key = self.value(args[0][1], file, context, depth + 1, env) if args else None
            if isinstance(key, str):
                return "${ConnectionStrings:" + key + "}" if name == "GetConnectionString" else "${" + key + "}"
        if name == "ToString" and receiver is not None:
            return self.value(receiver, file, context, depth + 1, env)
        if name in ("Combine", "Join") and receiver is not None and text(receiver).endswith(("Path", "Url", "UriHelper")):
            items = [self.value(e, file, context, depth + 1, env) for _, e in args]
            if all(isinstance(i, str) for i in items):
                return "/".join(i.strip("/") if n else i.rstrip("/") for n, i in enumerate(items))
        if name in ("Replace", "Trim", "TrimEnd", "TrimStart", "ToLower", "ToLowerInvariant") and receiver is not None:
            return self.value(receiver, file, context, depth + 1, env)
        return UNKNOWN

    def _config_element(self, node):
        """`_config["Section:Key"]` / `configuration["Key"]`."""
        target = node.child_by_field_name("expression")
        subscript = first(node, "bracketed_argument_list")
        if target is None or subscript is None or not self.CONFIG_NAME.search(text(target)):
            return UNKNOWN
        inner = subscript.named_children[0] if subscript.named_children else None
        key = self.value(inner.named_children[0] if inner is not None and inner.named_children else inner, None)
        if isinstance(key, str):
            section = re.search(r'GetSection\("([^"]+)"\)', text(target))
            return "${" + (f"{section.group(1)}:" if section else "") + key + "}"
        return UNKNOWN

    # ----------------------------
    def string(self, node, file, context=None, env=None) -> Optional[str]:
        value = self.value(node, file, context, env=env)
        return value if isinstance(value, str) else None

    def integer(self, node, file, context=None) -> Optional[int]:
        value = self.value(node, file, context)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def boolean(self, node, file, context=None) -> Optional[bool]:
        value = self.value(node, file, context)
        return value if isinstance(value, bool) else None

    def strings(self, node, file, context=None) -> List[str]:
        value = self.value(node, file, context)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        return []


def walk_children(node) -> list:
    return [c for c in node.named_children if c.type not in ("comment", "array_rank_specifier", "array_type")]


def is_test_source(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        any(p.endswith((".Tests", ".Test", ".UnitTests", ".IntegrationTests", ".Specs", "Tests", ".Benchmarks")) or p in ("tests", "test")
            for p in parts[:-1])
        or name.endswith(("Tests.cs", "Test.cs", "Spec.cs", "Specs.cs"))
        or "/obj/" in "/" + rel or "/bin/" in "/" + rel
    )
