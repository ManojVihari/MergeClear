"""
Models and schemas: classifies types (DTO, EF Core entity, DbContext, enum, exception, ...), turns types into the JSON-like
schemas the contract comparison reads (DataAnnotations and FluentValidation rules included) and describes EF Core entities.
"""
import re
from typing import Dict, List, Optional, Set, Tuple

from .catalog import COLLECTION_TYPES, MAP_TYPES, SIMPLE_TYPES, WRAPPER_TYPES
from .csindex import (
    Attr, ConstEvaluator, CsIndex, MemberInfo, TypeInfo, TypeRef, UNKNOWN, argument_nodes, invocation_parts, text, unparen, walk,
)

MAX_SCHEMA_DEPTH = 4
VALUE_TYPES = {"int", "long", "short", "byte", "sbyte", "uint", "ulong", "ushort", "bool", "char", "float", "double", "decimal", "Guid", "DateTime",
               "DateTimeOffset", "DateOnly", "TimeOnly", "TimeSpan", "BigInteger", "Half", "Ulid"}
DBCONTEXT_BASES = {"DbContext", "IdentityDbContext", "IdentityUserContext", "ApiAuthorizationDbContext"}
EXCEPTION_SUFFIX = re.compile(r"(Exception|Error)$")
NAMES_FOR_ID = re.compile(r"^(id|key)$", re.I)


def camel(name: str) -> str:
    """System.Text.Json / Newtonsoft default property naming."""
    if not name:
        return name
    if name.isupper() and len(name) <= 3:
        return name.lower()
    head = re.match(r"^[A-Z]+(?=[A-Z][a-z])", name)
    if head:
        return head.group(0).lower() + name[len(head.group(0)):]
    return name[0].lower() + name[1:]


class ModelRegistry:
    """Type kinds, EF Core entities and validation rules of a code base."""

    def __init__(self, index: CsIndex, const: ConstEvaluator):
        self.index = index
        self.const = const
        self.nullable_enabled: Dict[str, bool] = {}               # project folder -> <Nullable>enable</Nullable>
        self.pascal_case = False                                    # JsonNamingPolicy = null / UseMemberCasing
        self._kind: Dict[str, str] = {}
        self._entities: Optional[Dict[str, dict]] = None
        self._contexts: Optional[List[TypeInfo]] = None
        self._fluent: Dict[str, dict] = {}
        self._validators: Optional[Dict[str, Dict[str, List[dict]]]] = None

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------

    def kind(self, t: TypeInfo) -> str:
        cached = self._kind.get(t.qualified)
        if cached:
            return cached
        result = self._classify(t)
        self._kind[t.qualified] = result
        return result

    def _classify(self, t: TypeInfo) -> str:
        if t.kind == "enum":
            return "enum"
        names = self.index.base_names(t)
        if DBCONTEXT_BASES & set(names) or t.name in DBCONTEXT_BASES:
            return "dbcontext"
        if EXCEPTION_SUFFIX.search(t.name) and ("Exception" in names or any(n.endswith("Exception") for n in names) or t.name.endswith("Exception")):
            return "exception"
        if t.qualified in self.entities():
            return "entity"
        if t.kind == "interface":
            return "interface"
        return "model" if (t.kind in ("record", "struct") or any(m.kind == "property" for m in t.members)) else "class"

    def is_model_like(self, t: TypeInfo) -> bool:
        return self.kind(t) in ("model", "entity") and not t.is_static

    def is_value_type(self, ref: TypeRef, t: Optional[TypeInfo]) -> bool:
        if ref.array:
            return False
        if ref.name in VALUE_TYPES:
            return True
        return t is not None and (t.kind in ("enum", "struct") or (t.kind == "record" and "struct" in t.modifiers))

    # ------------------------------------------------------------------
    # DbContexts and entities
    # ------------------------------------------------------------------

    def contexts(self) -> List[TypeInfo]:
        if self._contexts is None:
            self._contexts = [t for t in self.index.types.values() if t.kind == "class" and (DBCONTEXT_BASES & set(self.index.base_names(t)))]
        return self._contexts

    def dbsets(self, context: TypeInfo) -> Dict[str, TypeInfo]:
        """{property name: entity type} of a DbContext (inherited sets included)."""
        out = {}
        for a in self.index.ancestors(context):
            for m in a.members:
                if m.kind == "property" and m.type.name in ("DbSet", "DbQuery") and m.type.args:
                    entity = self.index.resolve(m.type.args[0].name, a.file, a.namespace, a)
                    if entity is not None:
                        out[m.name] = entity
        return out

    def entities(self) -> Dict[str, dict]:
        if self._entities is not None:
            return self._entities
        self._entities = {}
        self._scan_fluent_configuration()
        found: Dict[str, Tuple[TypeInfo, Optional[str]]] = {}
        for context in self.contexts():
            for prop, entity in self.dbsets(context).items():
                found.setdefault(entity.qualified, (entity, prop))
        for qualified, config in self._fluent.items():
            entity = self.index.types.get(qualified)
            if entity is not None:
                found.setdefault(qualified, (entity, None))
        for t in self.index.types.values():
            if t.kind in ("class", "record") and t.attr("Table") and t.qualified not in found:
                found[t.qualified] = (t, None)
        for qualified, (entity, prop) in found.items():
            self._entities[qualified] = {"type": entity, "set": prop}
        return self._entities

    def _scan_fluent_configuration(self):
        """modelBuilder.Entity<T>().ToTable("x").Property(...) and IEntityTypeConfiguration<T> classes."""
        for t in self.index.types.values():
            builders = []
            for m in t.methods:
                if m.name == "OnModelCreating":
                    builders.append((m, None))
                elif m.name == "Configure" and any(b.name == "IEntityTypeConfiguration" for b in t.bases):
                    base = next(b for b in t.bases if b.name == "IEntityTypeConfiguration")
                    entity = self.index.resolve(base.args[0].name, t.file, t.namespace, t) if base.args else None
                    builders.append((m, entity))
            for method, entity in builders:
                for root in method.body:
                    for node in walk(root):
                        if node.type != "invocation_expression":
                            continue
                        parts = invocation_parts(node)
                        if parts is None:
                            continue
                        receiver, name, type_args, args = parts
                        if name == "Entity" and type_args:
                            target = self.index.resolve(type_args[0].name, t.file, t.namespace, t)
                            if target is not None:
                                self._record_fluent(target, node, method.file, t)
                        elif name == "ToTable" and entity is not None and receiver is not None and text(receiver).strip() in ("builder", "b", "entity"):
                            self._fluent.setdefault(entity.qualified, {})["table"] = self.const.string(args[0][1], method.file, t) if args else None
                        elif entity is not None and name in ("Property", "HasKey", "HasMany", "HasOne", "HasIndex") and receiver is not None:
                            self._record_property_rule(entity, node, method.file, t)

    def _record_fluent(self, entity: TypeInfo, node, file, context):
        config = self._fluent.setdefault(entity.qualified, {})
        # climb the fluent chain after Entity<T>(): .ToTable("x").HasKey(...)
        current = node
        for _ in range(10):
            parent = current.parent
            if parent is None or parent.type != "member_access_expression":
                break
            grand = parent.parent
            if grand is None or grand.type != "invocation_expression":
                break
            parts = invocation_parts(grand)
            if parts and parts[1] == "ToTable":
                args = parts[3]
                config["table"] = self.const.string(args[0][1], file, context) if args else None
            current = grand
        # Entity<T>(e => { e.ToTable("x"); e.Property(...) })
        arguments = argument_nodes(node)
        for _, expression in arguments:
            if expression.type == "lambda_expression":
                for inner in walk(expression):
                    if inner.type != "invocation_expression":
                        continue
                    parts = invocation_parts(inner)
                    if parts is None:
                        continue
                    if parts[1] == "ToTable":
                        config["table"] = self.const.string(parts[3][0][1], file, context) if parts[3] else None
                    elif parts[1] in ("Property", "HasKey", "HasMany", "HasOne", "HasIndex"):
                        self._record_property_rule(entity, inner, file, context)

    def _record_property_rule(self, entity: TypeInfo, node, file, context):
        parts = invocation_parts(node)
        if parts is None:
            return
        receiver, name, _, args = parts
        config = self._fluent.setdefault(entity.qualified, {})
        if name == "Property" and args:
            prop = self._lambda_member(args[0][1])
            if prop:
                rules = config.setdefault("properties", {}).setdefault(prop, {})
                current = node
                for _ in range(10):
                    parent = current.parent
                    if parent is None or parent.type != "member_access_expression":
                        break
                    grand = parent.parent
                    if grand is None or grand.type != "invocation_expression":
                        break
                    chained = invocation_parts(grand)
                    if chained:
                        method, chained_args = chained[1], chained[3]
                        if method == "IsRequired":
                            rules["required"] = True
                        elif method == "HasMaxLength" and chained_args:
                            value = self.const.integer(chained_args[0][1], file, context)
                            if value:
                                rules["max"] = value
                        elif method == "HasColumnName" and chained_args:
                            rules["column"] = self.const.string(chained_args[0][1], file, context)
                        elif method == "HasPrecision" and chained_args:
                            rules["precision"] = self.const.integer(chained_args[0][1], file, context)
                    current = grand
        elif name == "HasKey" and args:
            config["key"] = [m for m in [self._lambda_member(args[0][1])] if m]
        elif name in ("HasMany", "HasOne"):
            prop = self._lambda_member(args[0][1]) if args else None
            if prop:
                rel = config.setdefault("relationships", {})
                rel[prop] = "OneToMany" if name == "HasMany" else "ManyToOne"
                # .WithMany() on the other side makes a many-to-many; WithOne() on HasOne a one-to-one
                chain = text(node.parent.parent) if node.parent is not None and node.parent.parent is not None else ""
                if name == "HasMany" and ".WithMany" in chain:
                    rel[prop] = "ManyToMany"
                if name == "HasOne" and ".WithOne" in chain:
                    rel[prop] = "OneToOne"

    @staticmethod
    def _lambda_member(node) -> Optional[str]:
        node = unparen(node)
        if node is None:
            return None
        if node.type == "string_literal":
            return text(node).strip('"')
        if node.type == "lambda_expression":
            body = node.child_by_field_name("body")
            body = unparen(body)
            if body is not None and body.type == "member_access_expression":
                return text(body.child_by_field_name("name"))
        return None

    def table_name(self, entity: TypeInfo) -> Optional[str]:
        info = self.entity_info(entity)
        return info["table"] if info else None

    def entity_info(self, t: TypeInfo) -> Optional[dict]:
        meta = self.entities().get(t.qualified)
        if meta is None:
            return None
        cached = meta.get("info")
        if cached is not None:
            return cached
        config = self._fluent.get(t.qualified, {})
        attr = t.attr("Table")
        table, schema, inferred = None, None, False
        if attr is not None:
            table = self.const.string(attr.args[0], t.file, t) if attr.args else None
            if "Schema" in attr.named:
                schema = self.const.string(attr.named["Schema"], t.file, t)
        if table is None:
            table = config.get("table")
        if table is None:
            table, inferred = meta.get("set") or t.name, True

        fields, relationships = [], []
        properties = config.get("properties", {})
        entity_names = {q for q in self.entities()}
        for a in reversed(self.index.ancestors(t)):
            for m in a.members:
                if m.kind != "property" or m.is_static or m.attr("NotMapped"):
                    continue
                rules = properties.get(m.name, {})
                target, many = self._navigation(m, a, entity_names)
                entry = {"name": m.name, "type": str(m.type)}
                if target is not None:
                    kind = config.get("relationships", {}).get(m.name) or ("OneToMany" if many else "ManyToOne")
                    entry["relationship"] = kind
                    relationship = {"field": m.name, "target": target.name, "type": kind}
                    foreign = m.attr("ForeignKey")
                    if foreign is not None and foreign.args:
                        relationship["foreign_key"] = self.const.string(foreign.args[0], a.file, a)
                    inverse = m.attr("InverseProperty")
                    if inverse is not None and inverse.args:
                        relationship["mapped_by"] = self.const.string(inverse.args[0], a.file, a)
                    relationships.append(relationship)
                    fields.append(entry)
                    continue
                if m.attr("Key") or (m.name in config.get("key", [])) or NAMES_FOR_ID.match(m.name) or m.name == f"{t.name}Id":
                    entry["id"] = True
                    if not m.attr("DatabaseGenerated") and m.type.name in ("int", "long", "short"):
                        entry["generated"] = "IDENTITY"
                column = m.attr("Column")
                column_name = rules.get("column") or (self.const.string(column.args[0], a.file, a) if column is not None and column.args else None)
                if column_name:
                    entry["column"] = column_name
                if m.attr("Required") or rules.get("required") or (not m.type.nullable and not self.is_value_type(m.type, self.index.resolve(
                        m.type.name, a.file, a.namespace, a)) and self.nullable_for(a) and m.type.name not in COLLECTION_TYPES):
                    entry["nullable"] = False
                max_length = m.attr("MaxLength", "StringLength")
                limit = rules.get("max") or (self.const.integer(max_length.args[0], a.file, a) if max_length is not None and max_length.args else None)
                if limit:
                    entry["max_length"] = limit
                foreign = m.attr("ForeignKey")
                if foreign is not None and foreign.args:
                    entry["foreign_key"] = self.const.string(foreign.args[0], a.file, a)
                fields.append(entry)

        context = next((c for c in self.contexts() if t in self.dbsets(c).values()), None)
        meta["info"] = {
            "name": t.name, "qualified_name": t.qualified, "module": t.file.project, "file": t.file.rel, "kind": "Entity", "store": "efcore",
            "table": table, "schema": schema, "table_inferred": inferred, "jpa_name": None, "context": context.name if context else None,
            "extends": [b.name for b in self.index.bases(t) if b.kind == "class"], "fields": fields, "relationships": relationships,
        }
        return meta["info"]

    def _navigation(self, m: MemberInfo, owner: TypeInfo, entity_names: Set[str]) -> Tuple[Optional[TypeInfo], bool]:
        ref = m.type
        many = ref.array > 0 or ref.name in COLLECTION_TYPES
        element = ref.args[0] if many and ref.args else (ref.element() if ref.array else ref)
        target = self.index.resolve(element.name, owner.file, owner.namespace, owner)
        if target is not None and (target.qualified in entity_names or target.attr("Table") is not None) and target.kind != "enum":
            return target, many
        return None, False

    def nullable_for(self, t: TypeInfo) -> bool:
        return self.nullable_enabled.get(t.file.project, False)

    # ------------------------------------------------------------------
    # validation rules
    # ------------------------------------------------------------------

    def attribute_rules(self, attrs: List[Attr], file, context: Optional[TypeInfo]) -> dict:
        rules = {}
        for a in attrs:
            name = a.name
            if name in ("Required", "BindRequired", "JsonRequired"):
                rules["required"] = True
            elif name == "StringLength" and a.args:
                maximum = self.const.integer(a.args[0], file, context)
                if maximum is not None:
                    rules["max"] = maximum
                if "MinimumLength" in a.named:
                    minimum = self.const.integer(a.named["MinimumLength"], file, context)
                    if minimum:
                        rules["min"] = minimum
            elif name in ("MaxLength", "MinLength") and a.args:
                value = self.const.integer(a.args[0], file, context)
                if value is not None:
                    rules["max" if name == "MaxLength" else "min"] = value
            elif name == "Length" and len(a.args) >= 2:
                low, high = self.const.integer(a.args[0], file, context), self.const.integer(a.args[1], file, context)
                if low is not None:
                    rules["min"] = low
                if high is not None:
                    rules["max"] = high
            elif name == "Range" and len(a.args) >= 2:
                low, high = self.const.value(a.args[0], file, context), self.const.value(a.args[1], file, context)
                if isinstance(low, (int, float)):
                    rules["minimum"] = low
                if isinstance(high, (int, float)):
                    rules["maximum"] = high
            elif name == "RegularExpression" and a.args:
                pattern = self.const.string(a.args[0], file, context)
                if pattern:
                    rules["pattern"] = pattern
            elif name == "EmailAddress":
                rules["email"] = True
            elif name == "Url":
                rules["url"] = True
            elif name == "Phone":
                rules["phone"] = True
            elif name == "CreditCard":
                rules["creditCard"] = True
            elif name == "Compare" and a.args:
                rules["compare"] = self.const.string(a.args[0], file, context) or True
            elif name in ("AllowedValues",) and a.args:
                rules["allowed"] = True
            elif name in ("Base64String", "Ulid"):
                rules[name[0].lower() + name[1:]] = True
            elif name in ("Range_",):
                pass
            elif name not in KNOWN_NON_VALIDATION and a.node is not None and self._is_validation_attribute(a):
                rules["custom"] = True
        return rules

    def _is_validation_attribute(self, a: Attr) -> bool:
        target = self.index.resolve(a.name + "Attribute", a.file) or self.index.resolve(a.name, a.file)
        return target is not None and any(n in ("ValidationAttribute", "ValidateNever") or n.endswith("ValidationAttribute")
                                          for n in self.index.base_names(target))

    def fluent_rules(self, t: TypeInfo) -> Dict[str, List[dict]]:
        """FluentValidation rules per property of a model: {property: [{rule, value}]}."""
        if self._validators is None:
            self._validators = {}
            for v in self.index.types.values():
                base = next((b for b in v.bases if b.name == "AbstractValidator" and b.args), None)
                if base is None:
                    continue
                model = self.index.resolve(base.args[0].name, v.file, v.namespace, v)
                if model is None:
                    continue
                rules = self._validators.setdefault(model.qualified, {})
                for m in v.ctors + v.methods:
                    for root in m.body:
                        self._read_validator(root, rules, v)
        return self._validators.get(t.qualified, {})

    def _read_validator(self, root, rules, owner: TypeInfo):
        for node in walk(root):
            if node.type != "invocation_expression":
                continue
            parts = invocation_parts(node)
            if parts is None or parts[1] != "RuleFor" or not parts[3]:
                continue
            prop = self._lambda_member(parts[3][0][1])
            if not prop:
                continue
            # climb the chain after RuleFor(...): .NotEmpty().MaximumLength(50)
            current = node
            for _ in range(20):
                parent = current.parent
                if parent is None or parent.type != "member_access_expression":
                    break
                grand = parent.parent
                if grand is None or grand.type != "invocation_expression":
                    break
                chained = invocation_parts(grand)
                if chained:
                    rule = self._fluent_rule(chained[1], chained[3], owner)
                    if rule:
                        rules.setdefault(prop, []).append(rule)
                current = grand

    def _fluent_rule(self, method: str, args, owner: TypeInfo) -> Optional[dict]:
        def number(i):
            value = self.const.value(args[i][1], owner.file, owner) if len(args) > i else UNKNOWN
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        if method == "NotEmpty":
            return {"required": True, "notEmpty": True}
        if method == "NotNull":
            return {"required": True}
        if method == "MaximumLength":
            return {"max": number(0)}
        if method == "MinimumLength":
            return {"min": number(0)}
        if method == "Length":
            return {"min": number(0), "max": number(1)}
        if method == "EmailAddress":
            return {"email": True}
        if method in ("InclusiveBetween", "Between"):
            return {"minimum": number(0), "maximum": number(1)}
        if method == "ExclusiveBetween":
            return {"exclusiveMinimum": number(0), "exclusiveMaximum": number(1)}
        if method == "GreaterThan":
            return {"exclusiveMinimum": number(0)}
        if method == "GreaterThanOrEqualTo":
            return {"minimum": number(0)}
        if method == "LessThan":
            return {"exclusiveMaximum": number(0)}
        if method == "LessThanOrEqualTo":
            return {"maximum": number(0)}
        if method == "Matches":
            pattern = self.const.string(args[0][1], owner.file, owner) if args else None
            return {"pattern": pattern} if pattern else {"custom": True}
        if method == "Must" or method == "Custom":
            return {"custom": True}
        if method == "IsInEnum":
            return {"enum": True}
        if method in ("CreditCard",):
            return {"creditCard": True}
        if method in ("Equal", "NotEqual"):
            return {"custom": True}
        return None


KNOWN_NON_VALIDATION = {
    "Key", "Column", "Table", "NotMapped", "ForeignKey", "InverseProperty", "DatabaseGenerated", "Timestamp", "ConcurrencyCheck", "Index",
    "JsonPropertyName", "JsonIgnore", "JsonProperty", "JsonConverter", "JsonInclude", "JsonExtensionData", "Display", "DisplayName", "Description",
    "DefaultValue", "DataType", "FromBody", "FromQuery", "FromRoute", "FromHeader", "FromForm", "FromServices", "Obsolete", "SwaggerSchema",
    "SwaggerIgnore", "EnumMember", "XmlElement", "XmlIgnore", "DataMember", "IgnoreDataMember", "BindNever", "Bind", "SuppressMessage",
    "Owned", "Keyless", "ComplexType", "EditorBrowsable", "ModelBinder", "Precision", "Unicode", "DefaultValueSql", "SwaggerRequestBody",
    "DebuggerDisplay", "Serializable", "Flags", "CallerMemberName", "NotNull", "MaybeNull", "AllowNull", "DisallowNull",
}


class SchemaBuilder:
    """Schemas of types, in the shape the other scanners produce."""

    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self.index = registry.index
        self.const = registry.const

    # ------------------------------------------------------------------
    def unwrap(self, ref: TypeRef) -> TypeRef:
        while ref.name in WRAPPER_TYPES and ref.args and not ref.array:
            ref = ref.args[0]
        if ref.name == "Nullable" and ref.args:
            return ref.args[0].with_nullable()
        return ref

    def element_of(self, ref: TypeRef) -> Optional[TypeRef]:
        if ref.array:
            return ref.element()
        if ref.name in COLLECTION_TYPES:
            return ref.args[0] if ref.args else TypeRef("object")
        return None

    def schema(self, ref: TypeRef, file, namespace, context, referenced: Set[str], depth=0, seen=frozenset(), mode="request",
               bindings: Optional[dict] = None) -> dict:
        ref = self.unwrap(ref)
        if bindings and ref.name in bindings:
            ref = bindings[ref.name]
        element = self.element_of(ref)
        if element is not None:
            return {"type": "array", "items": self.schema(element, file, namespace, context, referenced, depth, seen, mode, bindings)}
        if ref.name in MAP_TYPES:
            value = ref.args[-1] if ref.args else TypeRef("object")
            return {"type": "map", "values": self.schema(value, file, namespace, context, referenced, depth, seen, mode, bindings)}
        if ref.name in SIMPLE_TYPES:
            return {"type": str(ref)}
        target = self.index.resolve(ref.name, file, namespace, context)
        if target is None:
            return {"type": str(ref)}
        referenced.add(target.qualified)
        if target.kind == "enum":
            return {"type": "enum", "name": target.name, "values": [name for name, _ in target.enum_members]}
        if not self.registry.is_model_like(target):
            return {"type": str(ref)}
        if depth >= MAX_SCHEMA_DEPTH or target.qualified in seen:
            return {"type": "object", "name": target.name, "ref": True}
        return {"type": "object", "name": target.name,
                "fields": self.schema_fields(target, ref, referenced, depth + 1, seen | {target.qualified}, mode, bindings)}

    @staticmethod
    def substitute(ref: TypeRef, bound: Optional[dict]) -> TypeRef:
        """`List<T>` with T bound to OrderDto -> `List<OrderDto>`."""
        if not bound:
            return ref
        if ref.name in bound and not ref.args and not ref.array:
            replacement = bound[ref.name]
            return replacement.with_nullable(True) if ref.nullable else replacement
        if ref.args:
            return TypeRef(ref.name, tuple(SchemaBuilder.substitute(a, bound) for a in ref.args), ref.nullable, ref.array, ref.qualifier, ref.tuple_items)
        if ref.array and ref.name in bound:
            return TypeRef(bound[ref.name].name, bound[ref.name].args, ref.nullable, ref.array, ref.qualifier)
        return ref

    def type_bindings(self, target: TypeInfo, ref: TypeRef, outer: Optional[dict]) -> Optional[dict]:
        if not target.type_params or not ref.args:
            return outer
        mapping = dict(outer or {})
        for name, arg in zip(target.type_params, ref.args):
            mapping[name] = outer[arg.name] if outer and arg.name in outer else arg
        return mapping

    def schema_fields(self, target: TypeInfo, ref: Optional[TypeRef], referenced: Set[str], depth=0, seen=frozenset(), mode="request",
                      bindings: Optional[dict] = None) -> Dict[str, dict]:
        """{field: {type, validation?, schema?, enum?}} of a DTO (inherited properties included)."""
        if target.kind == "enum":
            return {}
        reg = self.registry
        referenced.add(target.qualified)
        bound = self.type_bindings(target, ref, bindings) if ref is not None else bindings
        fluent = reg.fluent_rules(target)
        fields: Dict[str, dict] = {}

        for owner in reversed(self.index.ancestors(target)):
            if owner.kind == "interface":
                continue
            nullable_context = reg.nullable_for(owner)
            for m in owner.members:
                if m.kind != "property" or m.is_static or not self._is_public(m):
                    continue
                if m.attr("JsonIgnore") and not m.attr("JsonIgnore").named.get("Condition"):
                    continue
                if owner.kind == "record" and m.primary and m.attr("JsonIgnore"):
                    continue
                member_ref = self.substitute(m.type, bound)
                json_name = m.attr("JsonPropertyName", "JsonProperty")
                name = (self.const.string(json_name.args[0], owner.file, owner) if json_name is not None and json_name.args else None) or \
                    (m.name if reg.pascal_case else camel(m.name))
                entry = {"type": str(member_ref)}

                rules = reg.attribute_rules(m.attrs, owner.file, owner)
                if "required" in m.modifiers:
                    rules["required"] = True
                inner = self.unwrap(member_ref)
                member_type = reg.index.resolve(inner.name, owner.file, owner.namespace, owner)
                if mode == "request" and nullable_context and not member_ref.nullable and not m.init and "required" not in rules \
                        and not reg.is_value_type(inner, member_type) and inner.name not in COLLECTION_TYPES | MAP_TYPES and not inner.array \
                        and m.has_setter and m.primary is False and inner.name not in ("object", "dynamic"):
                    rules["required"] = True
                for rule in fluent.get(m.name, []):
                    rules.update({k: v for k, v in rule.items() if v is not None})
                if mode == "response":
                    rules.pop("required", None)
                if rules:
                    entry["validation"] = rules

                if member_type is not None and member_type.kind == "enum":
                    entry["enum"] = [n for n, _ in member_type.enum_members]
                    referenced.add(member_type.qualified)
                elif depth < MAX_SCHEMA_DEPTH and not self._scalar(inner, owner):
                    nested = self.schema(member_ref, owner.file, owner.namespace, owner, referenced, depth, seen | {target.qualified}, mode, bound)
                    if nested.get("type") in ("object", "array", "map", "enum"):
                        entry["schema"] = nested
                fields[name] = entry
        return fields

    def _scalar(self, ref: TypeRef, owner: TypeInfo) -> bool:
        if ref.name in SIMPLE_TYPES and not ref.array:
            return True
        if ref.array and ref.name == "byte":
            return True
        if ref.name in COLLECTION_TYPES or ref.name in MAP_TYPES or ref.array:
            return False
        target = self.index.resolve(ref.name, owner.file, owner.namespace, owner)
        return target is None or target.kind == "enum" or not self.registry.is_model_like(target)

    @staticmethod
    def _is_public(m: MemberInfo) -> bool:
        if "private" in m.modifiers or "protected" in m.modifiers or "internal" in m.modifiers and "public" not in m.modifiers:
            return False
        return True

    def is_scalar_ref(self, ref: TypeRef, file, namespace, context) -> bool:
        ref = self.unwrap(ref)
        if ref.name in SIMPLE_TYPES and not ref.array:
            return True
        if ref.name in COLLECTION_TYPES or ref.name in MAP_TYPES or ref.array or ref.tuple_items:
            return False
        target = self.index.resolve(ref.name, file, namespace, context)
        if target is None:
            return True
        return target.kind == "enum" or (target.kind == "struct" and ref.name not in ("KeyValuePair",))

    def collection_of_scalars(self, ref: TypeRef, file, namespace, context) -> bool:
        ref = self.unwrap(ref)
        element = self.element_of(ref)
        return element is not None and self.is_scalar_ref(element, file, namespace, context)


def parse_type_text(source: str) -> TypeRef:
    """TypeRef from a few characters of type syntax (typeof arguments given as text)."""
    match = re.match(r"^([\w.]+)(<(.*)>)?(\?)?$", source.strip())
    if not match:
        return TypeRef(source.strip())
    args = tuple(parse_type_text(a) for a in re.split(r",\s*(?![^<]*>)", match.group(3))) if match.group(3) else ()
    return TypeRef(match.group(1).rsplit(".", 1)[-1], args, bool(match.group(4)))

