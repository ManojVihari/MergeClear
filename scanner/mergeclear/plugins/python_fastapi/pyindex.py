"""
Python source index: modules, classes, functions, imports and module-level
variables, with name resolution across modules and constant evaluation.

Nothing here knows about FastAPI; it is the foundation the analyzer builds on.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

UNKNOWN = object()
MAX_RESOLVE_DEPTH = 8


# ============================================================================
# SYNTAX HELPERS
# ============================================================================

def text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def strip_type(node):
    """tree-sitter wraps annotations in a `type` node."""
    while node is not None and node.type in ("type", "parenthesized_expression") and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def dotted(node) -> Optional[str]:
    """`a.b.c` for a pure name chain, else None."""
    node = strip_type(node)
    if node is None:
        return None
    if node.type == "identifier":
        return text(node)
    if node.type == "attribute":
        left = dotted(node.child_by_field_name("object"))
        attr = node.child_by_field_name("attribute")
        return f"{left}.{text(attr)}" if left and attr is not None else None
    return None


def call_parts(node):
    """(function node, argument_list node) of a call, else (None, None)."""
    node = strip_type(node)
    if node is not None and node.type == "await":
        node = strip_type(node.named_children[0]) if node.named_children else None
    if node is None or node.type != "call":
        return None, None
    return node.child_by_field_name("function"), node.child_by_field_name("arguments")


def unwrap_await(node):
    node = strip_type(node)
    while node is not None and node.type == "await" and node.named_children:
        node = strip_type(node.named_children[0])
    return node


def positional_args(args_node) -> list:
    if args_node is None or args_node.type != "argument_list":
        return []
    return [c for c in args_node.named_children
            if c.type not in ("keyword_argument", "comment", "list_splat", "dictionary_splat")]


def keyword_args(args_node) -> dict:
    out = {}
    if args_node is None or args_node.type != "argument_list":
        return out
    for c in args_node.named_children:
        if c.type == "keyword_argument":
            out[text(c.child_by_field_name("name"))] = c.child_by_field_name("value")
    return out


def call_args(call_node) -> Tuple[list, dict]:
    _, args = call_parts(call_node)
    return positional_args(args), keyword_args(args)


def subscript_parts(node):
    """(value node, [index nodes]) for `X[a, b]` in both annotation and expression syntax."""
    node = strip_type(node)
    if node is None:
        return None, []
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        items = [strip_type(c) for c in node.children_by_field_name("subscript")]
        if len(items) == 1 and items[0].type == "tuple":
            items = [strip_type(c) for c in items[0].named_children]
        return value, items
    if node.type == "generic_type":
        value = node.named_children[0]
        params = [c for c in node.named_children if c.type == "type_parameter"]
        items = [strip_type(c) for c in params[0].named_children] if params else []
        return value, items
    return None, []


def string_value(node) -> Optional[str]:
    """Literal value of a plain (non-f) string, concatenation included."""
    node = strip_type(node)
    if node is None:
        return None
    if node.type == "concatenated_string":
        parts = [string_value(c) for c in node.named_children]
        return None if any(p is None for p in parts) else "".join(parts)
    if node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.children):
        return None
    prefix = text(node.children[0])[:-1].lower() if node.children else ""
    if "b" in prefix:
        return None
    return "".join(text(c) for c in node.children if c.type == "string_content")


def docstring_of(body) -> Optional[str]:
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
        value = string_value(first.named_children[0])
        if value is not None:
            return re.sub(r"\n\s+", "\n", value.strip())
    return None


def walk(node) -> Iterator:
    """Every node below `node` (depth first, node included)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def walk_calls(node) -> Iterator:
    for n in walk(node):
        if n.type == "call":
            yield n


def line_of(node) -> int:
    return node.start_point[0] + 1


# ============================================================================
# MODEL
# ============================================================================

@dataclass
class ImportRef:
    module: str                      # absolute dotted module
    name: Optional[str] = None       # imported attribute (from x import name), None for `import x`
    line: int = 0


@dataclass
class ParamInfo:
    name: str
    annotation: object = None        # tree-sitter node
    default: object = None
    kind: str = "normal"             # normal | varargs | kwargs


@dataclass
class DecoratorInfo:
    node: object                     # expression after the @
    name: str                        # dotted name of the decorator (or of the called function)
    call: object = None              # the call node when decorated with arguments


@dataclass
class VarInfo:
    name: str
    value: object                    # tree-sitter node (None for a bare annotation)
    annotation: object = None
    line: int = 0


@dataclass
class FieldInfo:
    name: str
    annotation: object
    default: object
    line: int


@dataclass(eq=False)
class FuncInfo:
    id: str
    name: str
    qualname: str
    module: "ModuleInfo"
    node: object
    owner: Optional["ClassInfo"] = None
    decorators: List[DecoratorInfo] = field(default_factory=list)
    params: List[ParamInfo] = field(default_factory=list)
    returns: object = None
    is_async: bool = False
    line_start: int = 0
    line_end: int = 0
    doc: Optional[str] = None
    nested: bool = False             # a decorated function defined inside another function

    @property
    def label(self) -> str:
        return f"{self.owner.name}.{self.name}" if self.owner else f"{self.module.short}.{self.name}"


@dataclass(eq=False)
class ClassInfo:
    name: str
    qualname: str
    module: "ModuleInfo"
    node: object
    bases: list = field(default_factory=list)          # base expression nodes
    keywords: dict = field(default_factory=dict)       # class X(Base, table=True)
    decorators: List[DecoratorInfo] = field(default_factory=list)
    fields: List[FieldInfo] = field(default_factory=list)
    methods: Dict[str, FuncInfo] = field(default_factory=dict)
    nested: Dict[str, "ClassInfo"] = field(default_factory=dict)
    class_vars: Dict[str, object] = field(default_factory=dict)
    doc: Optional[str] = None
    line_start: int = 0
    line_end: int = 0
    outer: Optional["ClassInfo"] = None

    @property
    def id(self) -> str:
        return f"{self.module.name}.{self.qualname}"


@dataclass(eq=False)
class ModuleInfo:
    name: str                        # dotted module name
    rel: str                         # path relative to the repository
    is_package: bool
    code: bytes
    root: object
    project: str = ""                # path of the project module (pyproject folder) it belongs to
    imports: Dict[str, ImportRef] = field(default_factory=dict)
    star_imports: List[str] = field(default_factory=list)
    classes: Dict[str, ClassInfo] = field(default_factory=dict)
    functions: Dict[str, FuncInfo] = field(default_factory=dict)
    variables: Dict[str, VarInfo] = field(default_factory=dict)
    all_functions: List[FuncInfo] = field(default_factory=list)   # incl. methods and nested handlers
    all_classes: List[ClassInfo] = field(default_factory=list)    # incl. nested classes
    doc: Optional[str] = None

    @property
    def short(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    @property
    def package(self) -> str:
        return self.name if self.is_package else self.name.rpartition(".")[0]


@dataclass
class Symbol:
    kind: str                        # class | function | variable | module | external
    obj: object = None               # ClassInfo / FuncInfo / VarInfo / ModuleInfo
    module: Optional[ModuleInfo] = None
    qualified: Optional[str] = None  # external dotted name (kind == external)
    rest: str = ""                   # unresolved attribute tail, e.g. "get" for `crud.get`


# ============================================================================
# PARSING
# ============================================================================

def iter_flat_statements(block) -> Iterator:
    """Statements of a block, looking into if / try / with / for so conditional definitions are seen."""
    if block is None:
        return
    for stmt in block.named_children:
        yield stmt
        if stmt.type in ("if_statement", "try_statement", "with_statement", "for_statement", "while_statement", "else_clause",
                         "elif_clause", "except_clause", "finally_clause", "match_statement", "case_clause"):
            for child in stmt.named_children:
                if child.type == "block":
                    yield from iter_flat_statements(child)
                elif child.type in ("else_clause", "elif_clause", "except_clause", "finally_clause", "case_clause"):
                    yield from iter_flat_statements(child.child_by_field_name("body") or child)


def parse_decorators(decorated) -> List[DecoratorInfo]:
    out = []
    for d in decorated.children:
        if d.type != "decorator":
            continue
        expr = strip_type(d.named_children[0]) if d.named_children else None
        if expr is None:
            continue
        func, _ = call_parts(expr)
        out.append(DecoratorInfo(expr, dotted(func if func is not None else expr) or "", expr if func is not None else None))
    return out


def parse_params(params_node) -> List[ParamInfo]:
    out = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type == "identifier":
            out.append(ParamInfo(text(p)))
        elif p.type == "typed_parameter":
            ident = next((c for c in p.named_children if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern")), None)
            kind = "varargs" if ident is not None and ident.type == "list_splat_pattern" else \
                "kwargs" if ident is not None and ident.type == "dictionary_splat_pattern" else "normal"
            name = text(ident).lstrip("*") if ident is not None else ""
            out.append(ParamInfo(name, p.child_by_field_name("type"), None, kind))
        elif p.type == "default_parameter":
            out.append(ParamInfo(text(p.child_by_field_name("name")), None, p.child_by_field_name("value")))
        elif p.type == "typed_default_parameter":
            out.append(ParamInfo(text(p.child_by_field_name("name")), p.child_by_field_name("type"), p.child_by_field_name("value")))
        elif p.type == "list_splat_pattern":
            out.append(ParamInfo(text(p).lstrip("*"), kind="varargs"))
        elif p.type == "dictionary_splat_pattern":
            out.append(ParamInfo(text(p).lstrip("*"), kind="kwargs"))
    return out


class PythonIndex:

    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.modules: Dict[str, ModuleInfo] = {}
        self.by_rel: Dict[str, ModuleInfo] = {}
        self._suffix_cache: Dict[tuple, Optional[ModuleInfo]] = {}
        self._roots: Optional[set] = None
        self._subclasses: Dict[str, List[ClassInfo]] = {}
        self._resolve_cache: Dict[tuple, Symbol] = {}
        self.parser = None                                # set by the extractor; used for string annotations
        self._annotation_cache: Dict[str, object] = {}
        self._trees: list = []                            # keeps parsed forward references alive

    def parse_annotation(self, source: str):
        """Node of a string annotation (`"Optional[Item]"`), None if it does not parse."""
        if source in self._annotation_cache:
            return self._annotation_cache[source]
        node = None
        if self.parser is not None and len(source) < 400:
            tree = self.parser.parse(f"_: {source.strip()}\n".encode())
            self._trees.append(tree)
            statements = tree.root_node.named_children
            if statements and not tree.root_node.has_error and statements[0].type == "expression_statement":
                assignment = statements[0].named_children[0] if statements[0].named_children else None
                if assignment is not None and assignment.type == "assignment":
                    node = strip_type(assignment.child_by_field_name("type"))
        self._annotation_cache[source] = node
        return node

    # ----------------------------
    # building
    # ----------------------------

    @staticmethod
    def module_name_for(rel: str) -> Tuple[str, bool]:
        parts = rel[:-3].split("/")
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        return ".".join(parts), is_package

    def add_file(self, rel: str, code: bytes, root, project: str = "") -> ModuleInfo:
        name, is_package = self.module_name_for(rel)
        module = ModuleInfo(name=name or "__init__", rel=rel, is_package=is_package, code=code, root=root, project=project)
        self._parse_module(module)
        self.modules[module.name] = module
        self.by_rel[rel] = module
        return module

    def finalize(self):
        self._suffix_cache.clear()
        self._resolve_cache.clear()
        self._roots = None
        self._subclasses.clear()
        for module in self.modules.values():
            # resolved lazily below because star imports need every module present
            for cls in module.all_classes:
                for base in cls.bases:
                    target = self.resolve_expr(base, module)
                    if target.kind == "class" and not target.rest:
                        self._subclasses.setdefault(target.obj.id, []).append(cls)

    def _parse_module(self, module: ModuleInfo):
        root = module.root
        module.doc = docstring_of(root)

        for stmt in iter_flat_statements(root):
            if stmt.type == "import_statement":
                self._parse_import(module, stmt)
            elif stmt.type == "import_from_statement":
                self._parse_import_from(module, stmt)
            elif stmt.type == "class_definition":
                self._parse_class(module, stmt, [], None)
            elif stmt.type == "function_definition":
                self._parse_function(module, stmt, [], None, "")
            elif stmt.type == "decorated_definition":
                definition = stmt.child_by_field_name("definition")
                decorators = parse_decorators(stmt)
                if definition.type == "class_definition":
                    self._parse_class(module, definition, decorators, None)
                elif definition.type == "function_definition":
                    self._parse_function(module, definition, decorators, None, "")
            elif stmt.type == "expression_statement" and stmt.named_children:
                inner = stmt.named_children[0]
                if inner.type == "assignment":
                    self._parse_assignment(module.variables, inner)
                elif inner.type == "augmented_assignment":
                    pass
            elif stmt.type == "type_alias_statement":
                pass

    def _parse_assignment(self, target: dict, assignment):
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        right = assignment.child_by_field_name("right")
        # `a = b = value`
        while right is not None and right.type == "assignment":
            target[text(right.child_by_field_name("left"))] = VarInfo(
                text(right.child_by_field_name("left")), right.child_by_field_name("right"), None, line_of(right))
            right = right.child_by_field_name("right")
        annotation = assignment.child_by_field_name("type")
        target[text(left)] = VarInfo(text(left), right, annotation, line_of(assignment))

    def _parse_import(self, module: ModuleInfo, stmt):
        for name_node in stmt.children_by_field_name("name"):
            if name_node.type == "aliased_import":
                target = text(name_node.child_by_field_name("name"))
                alias = text(name_node.child_by_field_name("alias"))
                module.imports[alias] = ImportRef(target, None, line_of(stmt))
            else:
                target = text(name_node)
                module.imports[target.split(".")[0]] = ImportRef(target.split(".")[0], None, line_of(stmt))
                module.imports.setdefault(target, ImportRef(target, None, line_of(stmt)))

    def _parse_import_from(self, module: ModuleInfo, stmt):
        source = stmt.child_by_field_name("module_name")
        base = self._absolute_module(module, source)
        if base is None:
            return
        if any(c.type == "wildcard_import" for c in stmt.children):
            module.star_imports.append(base)
            return
        for name_node in stmt.children_by_field_name("name"):
            if name_node.type == "aliased_import":
                name = text(name_node.child_by_field_name("name"))
                alias = text(name_node.child_by_field_name("alias"))
            else:
                name = alias = text(name_node)
            module.imports[alias] = ImportRef(base, name, line_of(stmt))

    @staticmethod
    def _absolute_module(module: ModuleInfo, node) -> Optional[str]:
        if node is None:
            return None
        if node.type != "relative_import":
            return text(node)
        prefix = next((c for c in node.children if c.type == "import_prefix"), None)
        dots = text(prefix).count(".") if prefix is not None else 0
        tail = next((text(c) for c in node.children if c.type == "dotted_name"), "")
        package = module.package.split(".") if module.package else []
        if dots > 1:
            package = package[:len(package) - (dots - 1)] if len(package) >= dots - 1 else []
        return ".".join(p for p in package + ([tail] if tail else []) if p)

    def _parse_function(self, module, node, decorators, owner: Optional[ClassInfo], parent_qual: str, nested=False) -> FuncInfo:
        name = text(node.child_by_field_name("name"))
        qualname = f"{owner.qualname}.{name}" if owner else (f"{parent_qual}.{name}" if parent_qual else name)
        body = node.child_by_field_name("body")
        func = FuncInfo(
            id=f"{module.name}.{qualname}", name=name, qualname=qualname, module=module, node=node, owner=owner,
            decorators=decorators, params=parse_params(node.child_by_field_name("parameters")),
            returns=node.child_by_field_name("return_type"), is_async=any(c.type == "async" for c in node.children),
            line_start=line_of(node.parent) if node.parent is not None and node.parent.type == "decorated_definition" else line_of(node),
            line_end=node.end_point[0] + 1, doc=docstring_of(body), nested=nested,
        )
        module.all_functions.append(func)
        if owner is not None:
            owner.methods.setdefault(name, func)
        elif not nested:
            module.functions[name] = func

        # decorated functions defined inside functions are route handlers of app factories
        if body is not None and owner is None:
            for stmt in iter_flat_statements(body):
                if stmt.type == "decorated_definition":
                    definition = stmt.child_by_field_name("definition")
                    if definition.type == "function_definition":
                        self._parse_function(module, definition, parse_decorators(stmt), None, qualname, nested=True)
        return func

    def _parse_class(self, module, node, decorators, outer: Optional[ClassInfo]) -> ClassInfo:
        name = text(node.child_by_field_name("name"))
        supers = node.child_by_field_name("superclasses")
        bases, keywords = [], {}
        if supers is not None:
            bases = positional_args(supers)
            keywords = keyword_args(supers)
        body = node.child_by_field_name("body")
        cls = ClassInfo(
            name=name, qualname=f"{outer.qualname}.{name}" if outer else name, module=module, node=node, bases=bases,
            keywords=keywords, decorators=decorators, doc=docstring_of(body),
            line_start=line_of(node.parent) if node.parent is not None and node.parent.type == "decorated_definition" else line_of(node),
            line_end=node.end_point[0] + 1, outer=outer,
        )
        module.all_classes.append(cls)
        if outer is None:
            module.classes[name] = cls
        else:
            outer.nested[name] = cls

        for stmt in iter_flat_statements(body):
            if stmt.type == "function_definition":
                self._parse_function(module, stmt, [], cls, "")
            elif stmt.type == "decorated_definition":
                definition = stmt.child_by_field_name("definition")
                inner_decorators = parse_decorators(stmt)
                if definition.type == "function_definition":
                    self._parse_function(module, definition, inner_decorators, cls, "")
                elif definition.type == "class_definition":
                    self._parse_class(module, definition, inner_decorators, cls)
            elif stmt.type == "class_definition":
                self._parse_class(module, stmt, [], cls)
            elif stmt.type == "expression_statement" and stmt.named_children:
                inner = stmt.named_children[0]
                if inner.type == "assignment":
                    left = inner.child_by_field_name("left")
                    if left is not None and left.type == "identifier":
                        fname = text(left)
                        annotation = inner.child_by_field_name("type")
                        value = inner.child_by_field_name("right")
                        cls.class_vars[fname] = value
                        cls.fields.append(FieldInfo(fname, annotation, value, line_of(inner)))
        return cls

    def nested_function(self, parent: FuncInfo, name: str) -> Optional[FuncInfo]:
        """A function defined inside another (the checker a dependency factory returns)."""
        qualname = f"{parent.qualname}.{name}"
        for f in parent.module.all_functions:
            if f.nested and f.qualname == qualname:
                return f
        for node in walk(parent.node.child_by_field_name("body")):
            if node.type == "function_definition" and text(node.child_by_field_name("name")) == name:
                decorators = parse_decorators(node.parent) if node.parent is not None and node.parent.type == "decorated_definition" else []
                return self._parse_function(parent.module, node, decorators, None, parent.qualname, nested=True)
        return None

    # ----------------------------
    # lookup
    # ----------------------------

    def _compute_roots(self):
        """Dotted prefixes that act as import roots: the repository, project folders, `src/`, parents of top-level packages."""
        roots = {""}
        for module in self.modules.values():
            if module.project:
                roots.add(module.project.replace("/", "."))
                roots.add((module.project + "/src").replace("/", "."))
            roots.add("src")
            directory = module.rel.rpartition("/")[0]
            top = directory
            while top and f"{top}/__init__.py" in self.by_rel:
                parent = top.rpartition("/")[0]
                if parent and f"{parent}/__init__.py" in self.by_rel:
                    top = parent
                else:
                    break
            if top and f"{top}/__init__.py" in self.by_rel:
                roots.add(top.rpartition("/")[0].replace("/", "."))
        self._roots = roots

    def resolve_module(self, dotted_name: str, origin: Optional[ModuleInfo] = None) -> Optional[ModuleInfo]:
        """
        Module by absolute import name.

        Imports are relative to a source root, not to the repository: `app.crud` is `backend/app/crud.py` when
        `backend` is a project folder. Modules of the importing project win over same-named modules elsewhere.
        """
        key = (dotted_name, origin.project if origin is not None else None)
        if key in self._suffix_cache:
            return self._suffix_cache[key]
        if self._roots is None:
            self._compute_roots()
        candidates = []
        if dotted_name in self.modules:
            candidates.append(self.modules[dotted_name])
        suffix = "." + dotted_name
        for name, module in self.modules.items():
            if name.endswith(suffix) and name[:-len(suffix)] in self._roots and module not in candidates:
                candidates.append(module)
        found = None
        if candidates:
            if origin is not None:
                same = [m for m in candidates if m.project == origin.project]
                candidates = same or candidates
            found = sorted(candidates, key=lambda m: m.name.count("."))[0]
        self._suffix_cache[key] = found
        return found

    def lookup(self, module: ModuleInfo, name: str, depth=0, seen=None) -> Optional[Symbol]:
        """What a bare name refers to inside a module."""
        if depth > MAX_RESOLVE_DEPTH:
            return None
        seen = seen or set()
        if (module.name, name) in seen:
            return None
        seen = seen | {(module.name, name)}

        if name in module.classes:
            return Symbol("class", module.classes[name], module)
        if name in module.functions:
            return Symbol("function", module.functions[name], module)
        if name in module.variables:
            return Symbol("variable", module.variables[name], module)

        ref = module.imports.get(name)
        if ref is not None:
            return self._follow_import(ref, depth, seen, module)

        for star in module.star_imports:
            target = self.resolve_module(star, module)
            if target is not None and target is not module:
                found = self.lookup(target, name, depth + 1, seen)
                if found is not None:
                    return found
        return None

    def _follow_import(self, ref: ImportRef, depth, seen, origin: Optional[ModuleInfo] = None) -> Symbol:
        if ref.name is None:
            target = self.resolve_module(ref.module, origin)
            if target is not None:
                return Symbol("module", target, target)
            return Symbol("external", qualified=ref.module)

        source = self.resolve_module(ref.module, origin)
        if source is not None:
            found = self.lookup(source, ref.name, depth + 1, seen)
            if found is not None:
                return found
        submodule = self.resolve_module(f"{ref.module}.{ref.name}", origin)
        if submodule is not None:
            return Symbol("module", submodule, submodule)
        return Symbol("external", qualified=f"{ref.module}.{ref.name}")

    def resolve_name(self, dotted_name: str, module: ModuleInfo) -> Symbol:
        """Resolve `a.b.c` from a module's scope: through imports, modules, classes. `rest` is the unresolved tail."""
        key = (module.name, dotted_name)
        cached = self._resolve_cache.get(key)
        if cached is not None:
            return cached
        result = self._resolve_name(dotted_name, module)
        self._resolve_cache[key] = result
        return result

    def _resolve_name(self, dotted_name: str, module: ModuleInfo) -> Symbol:
        parts = dotted_name.split(".")
        # `import a.b.c` registers the whole dotted path
        for cut in range(len(parts), 0, -1):
            head = ".".join(parts[:cut])
            if head in module.imports and cut > 1:
                ref = module.imports[head]
                return self._walk_symbol(self._follow_import(ref, 0, set(), module), parts[cut:])
        symbol = self.lookup(module, parts[0])
        if symbol is None:
            return Symbol("external", qualified=None, rest=dotted_name)
        return self._walk_symbol(symbol, parts[1:])

    def _walk_symbol(self, symbol: Symbol, rest: List[str]) -> Symbol:
        while rest:
            if symbol.kind == "module":
                nxt = self.lookup(symbol.obj, rest[0])
                if nxt is None:
                    sub = self.resolve_module(f"{symbol.obj.name}.{rest[0]}", symbol.obj)
                    if sub is None:
                        symbol.rest = ".".join(rest)
                        return symbol
                    nxt = Symbol("module", sub, sub)
                symbol, rest = nxt, rest[1:]
            elif symbol.kind == "external":
                base = symbol.qualified
                joined = ".".join(rest)
                return Symbol("external", qualified=f"{base}.{joined}" if base else None, rest=symbol.rest)
            elif symbol.kind == "class":
                inner = symbol.obj.nested.get(rest[0])
                if inner is None:
                    symbol.rest = ".".join(rest)
                    return symbol
                symbol, rest = Symbol("class", inner, symbol.module), rest[1:]
            else:
                symbol.rest = ".".join(rest)
                return symbol
        return symbol

    def resolve_expr(self, node, module: ModuleInfo) -> Symbol:
        """Resolve a name/attribute expression (calls and subscripts are looked through)."""
        node = unwrap_await(strip_type(node))
        if node is None:
            return Symbol("external")
        if node.type == "call":
            func, _ = call_parts(node)
            return self.resolve_expr(func, module)
        if node.type in ("subscript", "generic_type"):
            value, _ = subscript_parts(node)
            return self.resolve_expr(value, module)
        name = dotted(node)
        if name is None:
            return Symbol("external")
        return self.resolve_name(name, module)

    def external_name(self, node, module: ModuleInfo) -> Optional[str]:
        """Dotted library name an expression refers to (`fastapi.APIRouter`), None for project symbols."""
        symbol = self.resolve_expr(node, module)
        if symbol.kind == "external":
            return symbol.qualified
        return None

    # ----------------------------
    # classes
    # ----------------------------

    def class_bases(self, cls: ClassInfo) -> Tuple[List[ClassInfo], List[str]]:
        """(project base classes, library base names) of a class."""
        internal, external = [], []
        for base in cls.bases:
            symbol = self.resolve_expr(base, cls.module)
            if symbol.kind == "class" and not symbol.rest:
                internal.append(symbol.obj)
            elif symbol.kind == "external" and symbol.qualified:
                external.append(symbol.qualified)
            elif symbol.kind == "variable":
                # `Base = declarative_base()`
                value = symbol.obj.value
                name = self.external_name(value, symbol.module) if value is not None else None
                external.append(name or symbol.obj.name)
            else:
                name = dotted(strip_type(base) if strip_type(base).type != "subscript" else subscript_parts(base)[0])
                if name:
                    external.append(name)
        return internal, external

    def mro(self, cls: ClassInfo) -> List[ClassInfo]:
        """Class followed by its project ancestors (breadth first, no duplicates)."""
        out, queue = [], [cls]
        while queue and len(out) < 50:
            current = queue.pop(0)
            if current in out:
                continue
            out.append(current)
            queue.extend(self.class_bases(current)[0])
        return out

    def external_bases(self, cls: ClassInfo) -> List[str]:
        names = []
        for c in self.mro(cls):
            names.extend(self.class_bases(c)[1])
        return names

    def subclasses(self, cls: ClassInfo, transitive=True) -> List[ClassInfo]:
        out, queue = [], list(self._subclasses.get(cls.id, []))
        while queue and len(out) < 200:
            current = queue.pop(0)
            if current in out:
                continue
            out.append(current)
            if transitive:
                queue.extend(self._subclasses.get(current.id, []))
        return out

    def find_method(self, cls: ClassInfo, name: str) -> Optional[FuncInfo]:
        for c in self.mro(cls):
            if name in c.methods:
                return c.methods[name]
        return None

    def find_class_var(self, cls: ClassInfo, name: str):
        for c in self.mro(cls):
            if name in c.class_vars:
                return c, c.class_vars[name]
        return None, None


# ============================================================================
# CONSTANT EVALUATION
# ============================================================================

HTTP_STATUS_NAMES = {
    "CONTINUE": 100, "SWITCHING_PROTOCOLS": 101, "OK": 200, "CREATED": 201, "ACCEPTED": 202,
    "NON_AUTHORITATIVE_INFORMATION": 203, "NO_CONTENT": 204, "RESET_CONTENT": 205, "PARTIAL_CONTENT": 206,
    "MULTI_STATUS": 207, "MULTIPLE_CHOICES": 300, "MOVED_PERMANENTLY": 301, "FOUND": 302, "SEE_OTHER": 303,
    "NOT_MODIFIED": 304, "TEMPORARY_REDIRECT": 307, "PERMANENT_REDIRECT": 308, "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401, "PAYMENT_REQUIRED": 402, "FORBIDDEN": 403, "NOT_FOUND": 404, "METHOD_NOT_ALLOWED": 405,
    "NOT_ACCEPTABLE": 406, "PROXY_AUTHENTICATION_REQUIRED": 407, "REQUEST_TIMEOUT": 408, "CONFLICT": 409,
    "GONE": 410, "LENGTH_REQUIRED": 411, "PRECONDITION_FAILED": 412, "REQUEST_ENTITY_TOO_LARGE": 413,
    "CONTENT_TOO_LARGE": 413, "PAYLOAD_TOO_LARGE": 413, "REQUEST_URI_TOO_LONG": 414, "UNSUPPORTED_MEDIA_TYPE": 415,
    "REQUESTED_RANGE_NOT_SATISFIABLE": 416, "EXPECTATION_FAILED": 417, "IM_A_TEAPOT": 418,
    "UNPROCESSABLE_ENTITY": 422, "UNPROCESSABLE_CONTENT": 422, "LOCKED": 423, "FAILED_DEPENDENCY": 424,
    "TOO_EARLY": 425, "UPGRADE_REQUIRED": 426, "PRECONDITION_REQUIRED": 428, "TOO_MANY_REQUESTS": 429,
    "REQUEST_HEADER_FIELDS_TOO_LARGE": 431, "UNAVAILABLE_FOR_LEGAL_REASONS": 451, "INTERNAL_SERVER_ERROR": 500,
    "NOT_IMPLEMENTED": 501, "BAD_GATEWAY": 502, "SERVICE_UNAVAILABLE": 503, "GATEWAY_TIMEOUT": 504,
    "HTTP_VERSION_NOT_SUPPORTED": 505, "INSUFFICIENT_STORAGE": 507, "NETWORK_AUTHENTICATION_REQUIRED": 511,
}


def status_from_name(name: str) -> Optional[int]:
    """`status.HTTP_404_NOT_FOUND` / `HTTPStatus.NOT_FOUND` / `HTTP_404_NOT_FOUND` -> 404."""
    last = name.rsplit(".", 1)[-1]
    match = re.fullmatch(r"HTTP_(\d{3})_[A-Z_]+", last)
    if match:
        return int(match.group(1))
    if "HTTPStatus" in name or "http.HTTPStatus" in name:
        return HTTP_STATUS_NAMES.get(last)
    return None


class ConstEvaluator:
    """Best-effort evaluation of constants: strings, numbers, lists, dicts, f-strings, settings and env defaults."""

    ENV_CALLS = {"os.getenv", "os.environ.get", "getenv", "environ.get", "config", "env", "env.str", "env.int", "env.bool",
                 "decouple.config", "environs.Env.str"}

    def __init__(self, index: PythonIndex):
        self.index = index

    def value(self, node, module: ModuleInfo, depth=0):
        """Python value of `node`, or UNKNOWN. Unresolvable pieces of f-strings stay as `{expr}` / `${ENV}`."""
        node = strip_type(node)
        if node is None or depth > MAX_RESOLVE_DEPTH:
            return UNKNOWN
        kind = node.type

        if kind in ("string", "concatenated_string"):
            return self._string(node, module, depth)
        if kind == "integer":
            try:
                return int(text(node).replace("_", ""), 0)
            except ValueError:
                return UNKNOWN
        if kind == "float":
            try:
                return float(text(node).replace("_", ""))
            except ValueError:
                return UNKNOWN
        if kind == "true":
            return True
        if kind == "false":
            return False
        if kind == "none":
            return None
        if kind in ("list", "tuple", "set"):
            return [self.value(c, module, depth + 1) for c in node.named_children]
        if kind == "dictionary":
            out = {}
            for pair in node.named_children:
                if pair.type != "pair":
                    continue
                k = self.value(pair.child_by_field_name("key"), module, depth + 1)
                if k is not UNKNOWN and isinstance(k, (str, int, float, bool)):
                    out[k] = self.value(pair.child_by_field_name("value"), module, depth + 1)
            return out
        if kind == "unary_operator":
            operand = self.value(node.child_by_field_name("argument"), module, depth + 1)
            if text(node).startswith("-") and isinstance(operand, (int, float)):
                return -operand
            return UNKNOWN
        if kind == "binary_operator":
            return self._binary(node, module, depth)
        if kind in ("identifier", "attribute"):
            return self._reference(node, module, depth)
        if kind == "call":
            return self._call(node, module, depth)
        if kind == "conditional_expression":
            return self.value(node.named_children[0], module, depth + 1)
        return UNKNOWN

    def _string(self, node, module, depth):
        if node.type == "concatenated_string":
            parts = [self.value(c, module, depth + 1) for c in node.named_children]
            return "".join(str(p) if p is not UNKNOWN else "{?}" for p in parts)
        out = []
        for child in node.children:
            if child.type == "string_content":
                out.append(text(child))
            elif child.type == "interpolation":
                expr = child.child_by_field_name("expression")
                resolved = self.value(expr, module, depth + 1)
                if resolved is UNKNOWN or isinstance(resolved, (list, dict)):
                    out.append("{" + text(expr) + "}")
                else:
                    out.append(str(resolved))
        return "".join(out)

    def _binary(self, node, module, depth):
        left = self.value(node.child_by_field_name("left"), module, depth + 1)
        right = self.value(node.child_by_field_name("right"), module, depth + 1)
        op = text(node.child_by_field_name("operator"))
        if op == "+":
            if isinstance(left, str) or isinstance(right, str):
                if left is UNKNOWN or right is UNKNOWN:
                    l_text = left if isinstance(left, str) else "{" + text(node.child_by_field_name("left")) + "}"
                    r_text = right if isinstance(right, str) else "{" + text(node.child_by_field_name("right")) + "}"
                    return l_text + r_text
                if isinstance(left, str) and isinstance(right, str):
                    return left + right
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
        elif op == "/" and isinstance(left, str) and isinstance(right, str):          # pathlib style
            return f"{left.rstrip('/')}/{right.lstrip('/')}"
        elif op == "*" and isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left * right
        return UNKNOWN

    def _reference(self, node, module, depth):
        name = dotted(node)
        if name is None:
            return UNKNOWN
        status = status_from_name(name) if "." in name or name.startswith("HTTP_") else None
        if status is not None:
            return status

        symbol = self.index.resolve_name(name, module)
        if symbol.kind == "variable" and not symbol.rest:
            var = symbol.obj
            return self.value(var.value, symbol.module, depth + 1) if var.value is not None else UNKNOWN
        if symbol.kind == "variable" and symbol.rest:
            return self._attribute_of_instance(symbol, depth)
        if symbol.kind == "class" and symbol.rest and "." not in symbol.rest:
            owner, value = self.index.find_class_var(symbol.obj, symbol.rest)
            if value is not None:
                return self.value(value, owner.module, depth + 1)
        if symbol.kind == "external" and symbol.qualified:
            status = status_from_name(symbol.qualified)
            if status is not None:
                return status
        if symbol.kind == "module" and symbol.rest and "." not in symbol.rest:
            var = symbol.obj.variables.get(symbol.rest)
            if var is not None and var.value is not None:
                return self.value(var.value, symbol.obj, depth + 1)
        return UNKNOWN

    def _attribute_of_instance(self, symbol: Symbol, depth):
        """`settings.API_PREFIX` where settings = Settings()."""
        cls = self.instance_class(symbol.obj.value, symbol.module, depth + 1)
        if cls is None or "." in symbol.rest:
            return UNKNOWN
        return self.class_field_default(cls, symbol.rest, depth)

    def class_field_default(self, cls: ClassInfo, name: str, depth=0):
        for c in self.index.mro(cls):
            for f in c.fields:
                if f.name == name and f.default is not None:
                    return self._field_default(f.default, c.module, name, depth)
        return UNKNOWN

    def _field_default(self, default, module, name, depth):
        func, args = call_parts(default)
        if func is not None and (dotted(func) or "").rsplit(".", 1)[-1] in ("Field", "field"):
            pos, kw = positional_args(args), keyword_args(args)
            node = kw.get("default") or (pos[0] if pos and text(pos[0]) != "..." else None)
            if node is None:
                env = kw.get("env") or kw.get("validation_alias") or kw.get("alias")
                key = string_value(env) if env is not None else name.upper()
                return "${" + (key or name.upper()) + "}"
            return self.value(node, module, depth + 1)
        return self.value(default, module, depth + 1)

    def instance_class(self, node, module: ModuleInfo, depth=0) -> Optional[ClassInfo]:
        """Project class an expression instantiates (directly, via a variable or a factory function)."""
        node = unwrap_await(strip_type(node))
        if node is None or depth > MAX_RESOLVE_DEPTH:
            return None
        if node.type == "call":
            func, _ = call_parts(node)
            symbol = self.index.resolve_expr(func, module)
            if symbol.kind == "class" and not symbol.rest:
                return symbol.obj
            if symbol.kind == "function" and not symbol.rest:
                for ret in (n for n in walk(symbol.obj.node) if n.type == "return_statement" and n.named_children):
                    cls = self.instance_class(ret.named_children[0], symbol.module, depth + 1)
                    if cls is not None:
                        return cls
                annotated = self.index.resolve_expr(symbol.obj.returns, symbol.module) if symbol.obj.returns is not None else None
                if annotated is not None and annotated.kind == "class":
                    return annotated.obj
            return None
        if node.type in ("identifier", "attribute"):
            symbol = self.index.resolve_expr(node, module)
            if symbol.kind == "variable" and not symbol.rest and symbol.obj.value is not None:
                return self.instance_class(symbol.obj.value, symbol.module, depth + 1)
        return None

    def _call(self, node, module, depth):
        func, args = call_parts(node)
        name = dotted(func) or ""
        symbol = self.index.resolve_expr(func, module)
        qualified = symbol.qualified if symbol.kind == "external" else None
        pos, kw = positional_args(args), keyword_args(args)

        if name in self.ENV_CALLS or (qualified or "").rsplit(".", 1)[-1] in ("getenv",) or qualified in ("os.environ.get",):
            key = string_value(pos[0]) if pos else None
            default = kw.get("default") if "default" in kw else (pos[1] if len(pos) > 1 else None)
            if default is not None:
                resolved = self.value(default, module, depth + 1)
                if resolved is not UNKNOWN:
                    return resolved
            return "${" + key + "}" if key else UNKNOWN
        if name.rsplit(".", 1)[-1] == "format" and isinstance(func, type(node)) and func.type == "attribute":
            template = self.value(func.child_by_field_name("object"), module, depth + 1)
            if isinstance(template, str):
                values = [self.value(p, module, depth + 1) for p in pos]
                try:
                    return re.sub(r"\{\}", lambda m: str(values.pop(0)) if values and values[0] is not UNKNOWN else "{}", template)
                except IndexError:
                    return template
        if name in ("str", "int", "float") and pos:
            return self.value(pos[0], module, depth + 1)
        if name.rsplit(".", 1)[-1] in ("Path", "PurePath", "join", "urljoin") and pos:
            parts = [self.value(p, module, depth + 1) for p in pos]
            if all(isinstance(p, str) for p in parts):
                return "/".join(p.strip("/") if i else p.rstrip("/") for i, p in enumerate(parts))
        # a field default: Field(default=...)
        if name.rsplit(".", 1)[-1] in ("Field", "field", "Query", "Header", "Body", "Path", "Cookie", "Form"):
            default = kw.get("default") or (pos[0] if pos else None)
            if default is not None and text(default) != "...":
                return self.value(default, module, depth + 1)
        return UNKNOWN

    # ----------------------------
    # convenience
    # ----------------------------

    def string(self, node, module: ModuleInfo) -> Optional[str]:
        value = self.value(node, module)
        return value if isinstance(value, str) else None

    def strings(self, node, module: ModuleInfo) -> List[str]:
        value = self.value(node, module)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        return []

    def integer(self, node, module: ModuleInfo) -> Optional[int]:
        value = self.value(node, module)
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) else None

    def boolean(self, node, module: ModuleInfo) -> Optional[bool]:
        value = self.value(node, module)
        return value if isinstance(value, bool) else None


def is_test_source(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
        or any(p in ("tests", "test", "testing") for p in parts[:-1])
    )


def project_of(rel: str, project_dirs: List[str]) -> str:
    """Deepest project folder (pyproject / requirements) containing a file; "" is the repository root."""
    best = ""
    for d in project_dirs:
        if d and (rel == d or rel.startswith(d + "/")) and len(d) > len(best):
            best = d
    return best


def clean_rel(path: str, repo_path: str) -> str:
    return os.path.relpath(path, repo_path).replace(os.sep, "/")
