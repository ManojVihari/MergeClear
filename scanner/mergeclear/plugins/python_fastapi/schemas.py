"""
Models and schemas: classifies classes (Pydantic model, ORM entity, enum, settings, exception, ...), turns
type annotations into the JSON-like schemas the contract comparison reads, and describes ORM entities.
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .catalog import (
    DATACLASS_DECORATORS, ENTITY_BASES, ENUM_BASES, EXCEPTION_BASES, MODEL_BASES, SETTINGS_BASES, SQLMODEL_BASE,
)
from .pyindex import (
    ClassInfo, ConstEvaluator, ModuleInfo, PythonIndex, UNKNOWN, call_parts, dotted, keyword_args, positional_args,
    string_value, strip_type, subscript_parts, text, walk,
)
from .typeexpr import TypeExpr, TypeParser, generic_parameters

MAX_SCHEMA_DEPTH = 4

CONSTRAINT_RULES = {
    "min_length": "min", "max_length": "max", "min_items": "min", "max_items": "max", "ge": "minimum", "le": "maximum",
    "gt": "exclusiveMinimum", "lt": "exclusiveMaximum", "multiple_of": "multipleOf", "pattern": "pattern", "regex": "pattern",
    "max_digits": "maxDigits", "decimal_places": "decimalPlaces", "strict": "strict", "min_": "min", "max_": "max",
}
SIMPLE_VALIDATION = {
    "EmailStr": {"email": True}, "NameEmail": {"email": True}, "HttpUrl": {"url": True}, "AnyUrl": {"url": True},
    "AnyHttpUrl": {"url": True}, "FileUrl": {"url": True}, "WebsocketUrl": {"url": True},
    "PositiveInt": {"exclusiveMinimum": 0}, "PositiveFloat": {"exclusiveMinimum": 0},
    "NegativeInt": {"exclusiveMaximum": 0}, "NegativeFloat": {"exclusiveMaximum": 0},
    "NonNegativeInt": {"minimum": 0}, "NonNegativeFloat": {"minimum": 0}, "NonPositiveInt": {"maximum": 0},
    "NonPositiveFloat": {"maximum": 0}, "PastDate": {"past": True}, "FutureDate": {"future": True},
    "PastDatetime": {"past": True}, "FutureDatetime": {"future": True}, "IPvAnyAddress": {"ip": True}, "IPv4Address": {"ip": True},
    "IPv6Address": {"ip": True}, "UUID1": {"uuid": 1}, "UUID3": {"uuid": 3}, "UUID4": {"uuid": 4}, "UUID5": {"uuid": 5},
    "StrictStr": {"strict": True}, "StrictInt": {"strict": True}, "StrictFloat": {"strict": True}, "StrictBool": {"strict": True},
    "SecretStr": {"secret": True}, "PostgresDsn": {"dsn": True}, "RedisDsn": {"dsn": True}, "MongoDsn": {"dsn": True},
}
ANNOTATED_TYPES = {"Gt": "exclusiveMinimum", "Ge": "minimum", "Lt": "exclusiveMaximum", "Le": "maximum", "MultipleOf": "multipleOf",
                   "MinLen": "min", "MaxLen": "max"}

ENTITY_FIELD_FACTORIES = {"Column", "mapped_column", "relationship", "Relationship", "Field", "ForeignKey", "composite", "synonym",
                          "column_property", "association_proxy", "deferred", "query_expression"}
COLUMN_TYPE_NAMES = {"Integer", "String", "Boolean", "Float", "Text", "DateTime", "Date", "Time", "Numeric", "JSON", "JSONB", "UUID",
                     "BigInteger", "SmallInteger", "LargeBinary", "Enum", "ARRAY", "Interval", "Unicode", "UnicodeText"}


@dataclass(eq=False)
class CoreTable:
    """A SQLAlchemy Core `Table("users", metadata, ...)` bound to a variable."""
    name: str                         # the variable
    table: str
    module: ModuleInfo
    line: int = 0


class ModelRegistry:
    """Class kinds and ORM entities of a code base."""

    def __init__(self, index: PythonIndex, const: ConstEvaluator):
        self.index = index
        self.const = const
        self.types = TypeParser(index, const)
        self.pydantic_v2 = False
        self._kind_cache: Dict[str, str] = {}
        self._entity_cache: Dict[str, Optional[dict]] = {}
        self._enum_cache: Dict[str, list] = {}
        self._validators: Dict[str, Dict[str, bool]] = {}

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------

    def kind(self, cls: ClassInfo) -> str:
        """enum | settings | entity | model | dataclass | exception | plain"""
        cached = self._kind_cache.get(cls.id)
        if cached is not None:
            return cached
        self._kind_cache[cls.id] = "plain"           # recursion guard
        result = self._classify(cls)
        self._kind_cache[cls.id] = result
        return result

    def _classify(self, cls: ClassInfo) -> str:
        externals = self.index.external_bases(cls)
        mro = self.index.mro(cls)

        if any(ENUM_BASES.match(e) for e in externals):
            return "enum"
        if any(SETTINGS_BASES.match(e) for e in externals):
            return "settings"
        if any(self._decorated_dataclass(c) for c in mro):
            return "dataclass"

        for c in mro:
            if any(SQLMODEL_BASE.match(e) for e in self.index.class_bases(c)[1]):
                table = self.const.boolean(c.keywords.get("table"), c.module) if "table" in c.keywords else False
                if table or any(self.const.boolean(k.keywords.get("table"), k.module) for k in mro if "table" in k.keywords):
                    return "entity"           # SQLModel(table=True) always maps a table
        stores = [store for pattern, store in ENTITY_BASES for e in externals if pattern.match(e)]
        if stores:
            # only SQLAlchemy declarative bases are also used for abstract bases and mixins
            if stores[0] != "sqlalchemy":
                return "entity"
            return "entity" if self._has_table(cls) else "plain"
        if any(MODEL_BASES.match(e) for e in externals):
            return "model"
        if any(EXCEPTION_BASES.search(e) for e in externals) or any(EXCEPTION_BASES.search(c.name) and not c.fields for c in mro):
            return "exception"
        if self._is_namedtuple_or_typeddict(cls):
            return "model"
        # an unresolved library base called ...Model / ...Schema is almost always a pydantic model
        if any(re.search(r"(BaseModel|Schema|CamelModel)$", e.rsplit(".", 1)[-1]) for e in externals):
            return "model"
        return "plain"

    def _decorated_dataclass(self, cls: ClassInfo) -> bool:
        for d in cls.decorators:
            name = d.name
            qualified = self.index.external_name(d.node, cls.module) or name
            if DATACLASS_DECORATORS.match(qualified) or DATACLASS_DECORATORS.match(name) or name.rsplit(".", 1)[-1] in ("dataclass", "define"):
                return True
        return False

    def _is_namedtuple_or_typeddict(self, cls: ClassInfo) -> bool:
        for base in cls.bases:
            name = self.index.external_name(base, cls.module) or dotted(base) or ""
            if name.rsplit(".", 1)[-1] in ("NamedTuple", "TypedDict"):
                return True
        return False

    def _has_table(self, cls: ClassInfo) -> bool:
        mro = self.index.mro(cls)
        for c in mro:
            if "__abstract__" in c.class_vars and self.const.boolean(c.class_vars["__abstract__"], c.module) and c is cls:
                return False
        if any("__tablename__" in c.class_vars or "__table__" in c.class_vars for c in mro):
            return True
        if any(m.name == "__tablename__" for c in mro for m in c.methods.values()):
            return True
        return any(self._is_column_field(f) for f in cls.fields)

    def _is_column_field(self, f) -> bool:
        if f.default is not None:
            func, _ = call_parts(f.default)
            if func is not None and (dotted(func) or "").rsplit(".", 1)[-1] in ("Column", "mapped_column", "relationship"):
                return True
        if f.annotation is not None:
            head, _ = subscript_parts(f.annotation)
            if head is not None and (dotted(head) or "").rsplit(".", 1)[-1] in ("Mapped", "WriteOnlyMapped", "DynamicMapped"):
                return True
        return False

    def is_model_like(self, cls: ClassInfo) -> bool:
        return self.kind(cls) in ("model", "dataclass", "entity", "settings") or (self.kind(cls) == "plain" and bool(cls.fields)
                                                                                   and any(f.annotation is not None for f in cls.fields))

    def is_entity(self, cls: ClassInfo) -> bool:
        return self.kind(cls) == "entity"

    def entity_store(self, cls: ClassInfo) -> str:
        externals = self.index.external_bases(cls)
        for pattern, store in ENTITY_BASES:
            if any(pattern.match(e) for e in externals):
                return store
        if any(SQLMODEL_BASE.match(e) for e in externals):
            return "sqlalchemy"
        return "sqlalchemy"

    # ------------------------------------------------------------------
    # enums
    # ------------------------------------------------------------------

    def enum_values(self, cls: ClassInfo) -> list:
        cached = self._enum_cache.get(cls.id)
        if cached is not None:
            return cached
        values = []
        for c in reversed(self.index.mro(cls)):
            for f in c.fields:
                if f.name.startswith("_") or f.default is None or f.annotation is not None:
                    continue
                node = strip_type(f.default)
                if node.type in ("lambda", "function_definition"):
                    continue
                value = self.const.value(node, c.module)
                if value is UNKNOWN or isinstance(value, (list, dict)):
                    value = f.name
                values.append(value)
        self._enum_cache[cls.id] = values
        return values

    # ------------------------------------------------------------------
    # validators (@field_validator / @validator)
    # ------------------------------------------------------------------

    def validated_fields(self, cls: ClassInfo) -> Set[str]:
        names: Set[str] = set()
        for c in self.index.mro(cls):
            for method in c.methods.values():
                for d in method.decorators:
                    if d.name.rsplit(".", 1)[-1] in ("field_validator", "validator") and d.call is not None:
                        for arg in positional_args(call_parts(d.call)[1]):
                            value = string_value(arg)
                            if value:
                                names.add(value)
        return names

    # ------------------------------------------------------------------
    # field metadata
    # ------------------------------------------------------------------

    def field_call(self, default, module: ModuleInfo):
        """(kind, kwargs, positional) when a default is Field(...) / Column(...) / Query(...) style call."""
        func, args = call_parts(default) if default is not None else (None, None)
        if func is None:
            return None, {}, []
        name = (dotted(func) or "").rsplit(".", 1)[-1]
        return name, keyword_args(args), positional_args(args)

    def constraint_rules(self, kwargs: dict, module: ModuleInfo, te: Optional[TypeExpr] = None) -> dict:
        rules = {}
        for key, rule in CONSTRAINT_RULES.items():
            if key in kwargs:
                value = self.const.value(kwargs[key], module)
                if value is not UNKNOWN and value is not None:
                    rules[rule] = value
        return rules

    def meta_rules(self, te: TypeExpr) -> dict:
        rules = dict(te.constraints)
        out = {}
        for key, rule in CONSTRAINT_RULES.items():
            if key in rules and rules[key] is not None:
                out[rule] = rules[key]
        for m in te.metas:
            func, args = call_parts(m.node)
            if func is None:
                continue
            name = (dotted(func) or "").rsplit(".", 1)[-1]
            if name in ("Field", "Query", "Path", "Body", "Header", "Cookie", "Form", "File", "StringConstraints", "constr"):
                out.update(self.constraint_rules(keyword_args(args), m.module, te))
            elif name in ANNOTATED_TYPES:
                pos = positional_args(args)
                if pos:
                    value = self.const.value(pos[0], m.module)
                    if value is not UNKNOWN:
                        out[ANNOTATED_TYPES[name]] = value
        return out

    def type_rules(self, te: TypeExpr) -> dict:
        """Validation implied by the type itself (EmailStr, PositiveInt, ...)."""
        inner = te.unwrap_optional()
        rules = {}
        if inner.kind == "simple" and inner.name in SIMPLE_VALIDATION:
            rules.update(SIMPLE_VALIDATION[inner.name])
        rules.update(self.meta_rules(te))
        rules.update(self.meta_rules(inner) if inner is not te else {})
        return rules

    # ------------------------------------------------------------------
    # fields
    # ------------------------------------------------------------------

    def is_required(self, default, te: TypeExpr, module: ModuleInfo) -> bool:
        if default is None:
            if te.kind == "optional" and not self.pydantic_v2:
                return False
            return True
        name, kwargs, positional = self.field_call(default, module)
        if name in ("Field", "Query", "Path", "Body", "Header", "Cookie", "Form", "File", "FieldInfo", "field"):
            if "default" in kwargs:
                return text(kwargs["default"]) == "..."
            if "default_factory" in kwargs or "factory" in kwargs:
                return False
            if positional:
                return text(positional[0]) == "..."
            return True
        if text(default) == "...":
            return True
        return False

    def class_fields(self, cls: ClassInfo) -> List[tuple]:
        """[(FieldInfo, owner ClassInfo)] of a model: base classes first, overridden names once."""
        out: Dict[str, tuple] = {}
        for owner in reversed(self.index.mro(cls)):
            for f in owner.fields:
                if f.annotation is None or f.name.startswith("_") or f.name in ("model_config", "Config", "__tablename__"):
                    continue
                head = strip_type(f.annotation)
                origin = (dotted(subscript_parts(head)[0] if subscript_parts(head)[0] is not None else head) or "").rsplit(".", 1)[-1]
                if origin in ("ClassVar",):
                    continue
                out[f.name] = (f, owner)
        return list(out.values())

    def alias_of(self, default, te: TypeExpr, module: ModuleInfo) -> Optional[str]:
        sources = [(default, module)] + [(m.node, m.module) for m in te.metas]
        for node, mod in sources:
            name, kwargs, _ = self.field_call(node, mod)
            if name in ("Field", "FieldInfo") and kwargs:
                for key in ("alias", "validation_alias"):
                    if key in kwargs:
                        value = self.const.string(kwargs[key], mod)
                        if value:
                            return value
        return None

    def is_excluded(self, default, te: TypeExpr, module: ModuleInfo) -> bool:
        sources = [(default, module)] + [(m.node, m.module) for m in te.metas]
        for node, mod in sources:
            name, kwargs, _ = self.field_call(node, mod)
            if name in ("Field", "FieldInfo") and "exclude" in kwargs and self.const.boolean(kwargs["exclude"], mod):
                return True
        return False

    def default_of(self, default, module: ModuleInfo):
        name, kwargs, positional = self.field_call(default, module)
        node = default
        if name in ("Field", "Query", "Path", "Body", "Header", "Cookie", "Form", "File", "field"):
            node = kwargs.get("default") or (positional[0] if positional else None)
        if node is None or text(node) == "...":
            return None
        value = self.const.value(node, module)
        return None if value is UNKNOWN else value

    # ------------------------------------------------------------------
    # ORM entities and tables
    # ------------------------------------------------------------------

    def table_name(self, entity) -> Optional[str]:
        if isinstance(entity, CoreTable):
            return entity.table
        info = self.entity_info(entity)
        return info["table"] if info else None

    def core_table(self, var, module: ModuleInfo) -> Optional[CoreTable]:
        """`users = Table("users", metadata, ...)` -> CoreTable."""
        if var is None or var.value is None:
            return None
        value = strip_type(var.value)
        func, args = call_parts(value)
        if func is None:
            return None
        qualified = self.index.external_name(func, module) or ""
        if not qualified.endswith(".Table") and (dotted(func) or "").rsplit(".", 1)[-1] != "Table":
            return None
        pos = positional_args(args)
        table = self.const.string(pos[0], module) if pos else None
        return CoreTable(var.name, table, module, var.line) if table else None

    def _nested_setting(self, cls: ClassInfo, holder: str, key: str):
        for c in self.index.mro(cls):
            inner = c.nested.get(holder)
            if inner is not None and key in inner.class_vars:
                return self.const.string(inner.class_vars[key], inner.module)
        return None

    def entity_info(self, cls: ClassInfo) -> Optional[dict]:
        if cls.id in self._entity_cache:
            return self._entity_cache[cls.id]
        self._entity_cache[cls.id] = None
        if self.kind(cls) != "entity":
            return None
        store = self.entity_store(cls)
        table, inferred = None, False
        mro = self.index.mro(cls)
        for c in mro:
            if "__tablename__" in c.class_vars:
                table = self.const.string(c.class_vars["__tablename__"], c.module)
                if table:
                    break
        if table is None:
            for c in mro:
                method = c.methods.get("__tablename__")
                if method is not None:
                    for node in walk(method.node):
                        if node.type == "return_statement" and node.named_children:
                            table = self.const.string(node.named_children[0], c.module)
                            break
                    if table is None:
                        table, inferred = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.name).lower(), True
                    break
        if table is None:
            for c in mro:
                value = c.class_vars.get("__table__")
                if value is not None:
                    pos = positional_args(call_parts(value)[1])
                    table = self.const.string(pos[0], c.module) if pos else None
                    if table:
                        break
        if table is None:
            table = (self._nested_setting(cls, "Meta", "table") or self._nested_setting(cls, "Meta", "table_name")
                     or self._nested_setting(cls, "Meta", "tablename") or self._nested_setting(cls, "Meta", "db_table")
                     or self._nested_setting(cls, "Settings", "name"))
        if table is None:
            table, inferred = cls.name.lower(), True

        fields, relationships = [], []
        for owner in reversed(mro):
            if owner.module is None:
                continue
            for f in owner.fields:
                if f.name.startswith("_") or f.name in ("metadata", "registry", "query", "model_config", "Config", "Meta", "Settings", "objects"):
                    continue
                described = self._describe_field(f, owner, cls)
                if described is None:
                    continue
                entry, relationship = described
                fields = [x for x in fields if x["name"] != entry["name"]] + [entry]
                if relationship:
                    relationships = [x for x in relationships if x["field"] != relationship["field"]] + [relationship]

        internal, _ = self.index.class_bases(cls)
        return self._store_entity(cls, {
            "name": cls.name, "qualified_name": cls.id, "module": cls.module.project, "file": cls.module.rel,
            "kind": "Document" if store in ("beanie", "mongoengine", "odmantic") else "Entity", "store": store, "table": table,
            "table_inferred": inferred, "jpa_name": None, "extends": [b.name for b in internal if self.kind(b) == "entity" or b.fields],
            "fields": fields, "relationships": relationships,
        })

    def _store_entity(self, cls, info):
        self._entity_cache[cls.id] = info
        return info

    def _describe_field(self, f, owner: ClassInfo, entity: ClassInfo):
        func_name, kwargs, positional = self.field_call(f.default, owner.module)
        annotation = self.types.parse(f.annotation, owner.module) if f.annotation is not None else None
        dotted_func = dotted(call_parts(f.default)[0]) if f.default is not None and call_parts(f.default)[0] is not None else ""
        is_column = func_name in ("Column", "mapped_column", "Field") or (func_name or "").endswith("Field") or bool(
            f.annotation is not None and "Mapped" in text(f.annotation))
        is_relationship = func_name in ("relationship", "Relationship", "ForeignKeyField", "ManyToManyField", "OneToOneField",
                                        "ReferenceField", "Link", "BackLink") or (dotted_func or "").endswith(
            ("ForeignKeyField", "ManyToManyField", "OneToOneField", "ReferenceField"))
        if not (is_column or is_relationship) and (f.annotation is None or (f.default is not None and func_name is not None)):
            return None            # a class variable, not a mapped attribute

        column_type = self.types.render(annotation) if annotation is not None else None
        if column_type is None and positional:
            candidate = dotted(call_parts(positional[0])[0] if call_parts(positional[0])[0] is not None else positional[0]) or ""
            if candidate.rsplit(".", 1)[-1] in COLUMN_TYPE_NAMES or candidate[:1].isupper():
                column_type = candidate.rsplit(".", 1)[-1]
        if column_type is None and dotted_func:
            column_type = dotted_func.rsplit(".", 1)[-1]

        entry = {"name": f.name, "type": column_type or "Any"}
        primary = kwargs.get("primary_key") or kwargs.get("pk") or kwargs.get("primary_key_field")
        if primary is not None and self.const.boolean(primary, owner.module):
            entry["id"] = True
            auto = kwargs.get("autoincrement")
            if auto is None or self.const.boolean(auto, owner.module) is not False:
                if column_type in ("Integer", "int", "BigInteger", "Mapped[int]") or (annotation is not None and annotation.name == "int"):
                    entry["generated"] = "IDENTITY"
        if "foreign_key" in kwargs:
            reference = self.const.string(kwargs["foreign_key"], owner.module)
            if reference:
                entry["foreign_key"] = reference
        nullable = kwargs.get("nullable")
        if nullable is not None and self.const.boolean(nullable, owner.module) is False:
            entry["nullable"] = False
        elif annotation is not None and annotation.kind != "optional" and func_name in ("mapped_column", None) and "Mapped" in text(f.annotation or ""):
            entry["nullable"] = False
        if func_name == "Column" and positional:
            column = self.const.string(positional[0], owner.module)
            if column:
                entry["column"] = column
        elif "name" in kwargs:
            column = self.const.string(kwargs["name"], owner.module)
            if column:
                entry["column"] = column
        for node in ([f.default] if f.default is not None else []):
            for n in walk(node):
                if n.type == "call" and (dotted(call_parts(n)[0]) or "").rsplit(".", 1)[-1] == "ForeignKey":
                    pos = positional_args(call_parts(n)[1])
                    reference = self.const.string(pos[0], owner.module) if pos else None
                    if reference:
                        entry["foreign_key"] = reference

        relationship = None
        if is_relationship:
            target = None
            if positional:
                target = self.const.string(positional[0], owner.module) or (dotted(positional[0]) or "").rsplit(".", 1)[-1]
            if target is None and annotation is not None:
                item = annotation.unwrap_optional()
                if item.kind in ("list", "set") and item.args:
                    item = item.args[0]
                target = item.name
            many = annotation is not None and annotation.unwrap_optional().kind in ("list", "set")
            if "secondary" in kwargs or func_name == "ManyToManyField":
                kind = "ManyToMany"
            elif "uselist" in kwargs and self.const.boolean(kwargs["uselist"], owner.module) is False:
                kind = "OneToOne"
            else:
                kind = "OneToMany" if many else "ManyToOne"
            entry["relationship"] = kind
            relationship = {"field": f.name, "target": target, "type": kind}
            for key, name in (("back_populates", "mapped_by"), ("backref", "mapped_by")):
                if key in kwargs:
                    value = self.const.string(kwargs[key], owner.module)
                    if value:
                        relationship[name] = value
            if "cascade" in kwargs:
                value = self.const.string(kwargs["cascade"], owner.module)
                if value:
                    relationship["cascade"] = value
            if "sa_relationship_kwargs" in kwargs:
                relationship["cascade"] = relationship.get("cascade") or "configured"
        return entry, relationship


class SchemaBuilder:
    """Schemas of types, in the shape the Spring scanner produces."""

    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self.index = registry.index
        self.types = registry.types

    # ------------------------------------------------------------------
    def parse(self, node, module: ModuleInfo, bindings=None) -> TypeExpr:
        return self.types.parse(node, module, bindings=bindings)

    def render(self, te: TypeExpr) -> str:
        return self.types.render(te)

    def is_simple(self, te: TypeExpr) -> bool:
        te = te.unwrap_optional()
        if te.kind in ("simple", "any", "none", "literal", "unknown"):
            return True
        if te.kind == "class" and te.cls is not None:
            return self.registry.kind(te.cls) == "enum" or not self.registry.is_model_like(te.cls)
        return False

    def collection_item(self, te: TypeExpr) -> Optional[TypeExpr]:
        te = te.unwrap_optional()
        if te.kind in ("list", "set", "tuple") and te.args:
            return te.args[0]
        return None

    # ------------------------------------------------------------------
    def schema(self, te: TypeExpr, referenced: Set[str], depth=0, seen=frozenset(), mode="request") -> dict:
        te = te.unwrap_optional()
        kind = te.kind

        if kind in ("list", "set", "tuple"):
            item = te.args[0] if te.args else TypeExpr("any", "Any")
            return {"type": "array", "items": self.schema(item, referenced, depth, seen, mode)}
        if kind == "dict":
            value = te.args[1] if len(te.args) > 1 else TypeExpr("any", "Any")
            return {"type": "map", "values": self.schema(value, referenced, depth, seen, mode)}
        if kind == "union":
            return {"type": "union", "anyOf": [self.schema(a, referenced, depth, seen, mode) for a in te.args]}
        if kind == "literal":
            return {"type": "enum", "values": list(te.literal)}
        if kind in ("simple", "any", "none", "unknown"):
            return {"type": self.render(te)}
        if kind == "class" and te.cls is not None:
            cls = te.cls
            if self.registry.kind(cls) == "enum":
                referenced.add(cls.id)
                return {"type": "enum", "name": cls.name, "values": self.registry.enum_values(cls)}
            if not self.registry.is_model_like(cls):
                return {"type": self.render(te)}
            referenced.add(cls.id)
            if depth >= MAX_SCHEMA_DEPTH or cls.id in seen:
                return {"type": "object", "name": cls.name, "ref": True}
            return {"type": "object", "name": cls.name,
                    "fields": self.schema_fields(te, referenced, depth + 1, seen | {cls.id}, mode)}
        return {"type": self.render(te)}

    def class_bindings(self, te: TypeExpr) -> Optional[dict]:
        """{TypeVar name: TypeExpr} for a generic model such as Page[Item]."""
        if te.cls is None or not te.args:
            return None
        params = generic_parameters(self.index, te.cls)
        if not params:
            return None
        return {p: a for p, a in zip(params, te.args)}

    def schema_fields(self, te_or_cls, referenced: Set[str], depth=0, seen=frozenset(), mode="request") -> Dict[str, dict]:
        """{field: {type, validation?, schema?, enum?}} of a model class (inherited fields included)."""
        te = te_or_cls if isinstance(te_or_cls, TypeExpr) else TypeExpr("class", te_or_cls.name, cls=te_or_cls, module=te_or_cls.module)
        te = te.unwrap_optional()
        cls = te.cls
        if cls is None:
            return {}
        reg = self.registry
        if reg.kind(cls) == "enum":
            return {}
        referenced.add(cls.id)
        bindings = self.class_bindings(te)
        validated = reg.validated_fields(cls)
        fields: Dict[str, dict] = {}

        for f, owner in reg.class_fields(cls):
            ftype = self.types.parse(f.annotation, owner.module, bindings=bindings)
            if mode == "response" and reg.is_excluded(f.default, ftype, owner.module):
                continue
            name = reg.alias_of(f.default, ftype, owner.module) or f.name
            entry = {"type": self.render(ftype)}

            rules = dict(reg.type_rules(ftype))
            fname, kwargs, _ = reg.field_call(f.default, owner.module)
            if fname in ("Field", "FieldInfo", "field") and kwargs:
                rules.update(reg.constraint_rules(kwargs, owner.module, ftype))
            if mode == "request" and reg.is_required(f.default, ftype, owner.module) and reg.kind(cls) != "entity":
                rules["required"] = True
            if f.name in validated or "*" in validated:
                rules["custom"] = True
            if rules:
                entry["validation"] = rules

            inner = ftype.unwrap_optional()
            if inner.kind == "class" and inner.cls is not None and reg.kind(inner.cls) == "enum":
                entry["enum"] = reg.enum_values(inner.cls)
                referenced.add(inner.cls.id)
            elif inner.kind == "literal":
                entry["enum"] = list(inner.literal)
            elif depth < MAX_SCHEMA_DEPTH and not self.is_simple(inner):
                nested = self.schema(inner, referenced, depth, seen | {cls.id}, mode)
                if nested.get("type") in ("object", "array", "map", "enum", "union"):
                    entry["schema"] = nested
            fields[name] = entry

        return fields
