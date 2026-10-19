"""
Views over the application model stored by the scanner (architecture_store):
graphs for the UI and the relationships of a single endpoint.
"""
import re
from typing import Dict, List, Optional

STRUCTURAL_EDGES = {"contains", "injects", "calls"}


def application_model(document: Optional[dict]) -> Optional[dict]:
    """
    The application model inside a stored architecture document.

    Scanners store one model per framework ({"spring": ..., "fastapi": ...}); a repository using several is shown
    as one application.
    """
    if not document:
        return None
    models = [m for m in ((document.get("architecture") or {}).get(f) for f in ("spring", "fastapi", "aspnet")) if m]
    if not models:
        # a framework this server does not know yet: show the first model that looks like an application model
        models = [m for m in (document.get("architecture") or {}).values() if isinstance(m, dict) and "graph" in m]
    if not models:
        return None
    return models[0] if len(models) == 1 else merge_models(models)


spring_model = application_model            # name used before FastAPI support


def _extend_unique(target: list, items: list, key=None):
    seen = {key(x) if key else repr(x) for x in target}
    for item in items:
        k = key(item) if key else repr(item)
        if k not in seen:
            seen.add(k)
            target.append(item)


def merge_models(models: List[dict]) -> dict:
    """Combine the application models of several frameworks into one."""
    first, rest = models[0], models[1:]
    merged = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in first.items()}

    for model in rest:
        _extend_unique(merged.setdefault("modules", []), model.get("modules", []), key=lambda m: m.get("path"))
        _extend_unique(merged.setdefault("components", []), model.get("components", []), key=lambda c: c.get("qualified_name"))
        merged.setdefault("endpoints", []).extend(model.get("endpoints", []))
        _extend_unique(merged.setdefault("exception_handling", []), model.get("exception_handling", []))

        data, other = merged.setdefault("data_model", {}), model.get("data_model") or {}
        for part in ("entities", "repositories"):
            _extend_unique(data.setdefault(part, []), other.get(part, []), key=lambda x: x.get("qualified_name"))
        tables = {t["name"]: dict(t) for t in data.get("tables", [])}
        for t in other.get("tables", []):
            if t["name"] in tables:
                mine = tables[t["name"]]
                for field in ("entities", "operations", "accessed_by", "modules"):
                    mine[field] = sorted(set(mine.get(field, [])) | set(t.get(field, [])))
            else:
                tables[t["name"]] = dict(t)
        data["tables"] = [tables[n] for n in sorted(tables)]

        integ, other_integ = merged.setdefault("integrations", {}), model.get("integrations") or {}
        for part, value in other_integ.items():
            if isinstance(value, list):
                _extend_unique(integ.setdefault(part, []), value)
            elif isinstance(value, dict):
                mine = integ.setdefault(part, {})
                for sub, items in value.items():
                    if isinstance(items, list):
                        _extend_unique(mine.setdefault(sub, []), items)

        sec, other_sec = merged.setdefault("security", {}), model.get("security") or {}
        sec["enabled"] = bool(sec.get("enabled") or other_sec.get("enabled"))
        for part in ("technologies", "mechanisms", "configured_in"):
            sec[part] = sorted(set(sec.get(part, [])) | set(other_sec.get(part, [])))
        _extend_unique(sec.setdefault("url_rules", []), other_sec.get("url_rules", []))
        sec["endpoints_with_method_security"] = sec.get("endpoints_with_method_security", 0) + other_sec.get("endpoints_with_method_security", 0)
        sec["csrf_disabled"] = bool(sec.get("csrf_disabled")) and bool(other_sec.get("csrf_disabled", True))

        graph, other_graph = merged.setdefault("graph", {"nodes": [], "edges": []}), model.get("graph") or {}
        _extend_unique(graph.setdefault("nodes", []), other_graph.get("nodes", []), key=lambda n: n["id"])
        _extend_unique(graph.setdefault("edges", []), other_graph.get("edges", []), key=lambda e: (e["from"], e["to"], e["kind"]))

        summary, other_summary = merged.setdefault("summary", {}), model.get("summary") or {}
        for part in ("modules", "endpoints", "entities", "repositories", "feign_clients", "message_listeners", "scheduled_jobs", "types"):
            summary[part] = (summary.get(part) or 0) + (other_summary.get(part) or 0)
        components = dict(summary.get("components") or {})
        for name, count in (other_summary.get("components") or {}).items():
            components[name] = components.get(name, 0) + count
        summary["components"] = components
        technologies = {k: set(v) for k, v in (summary.get("technologies") or {}).items()}
        for category, techs in (other_summary.get("technologies") or {}).items():
            technologies.setdefault(category, set()).update(techs)
        summary["technologies"] = {k: sorted(v) for k, v in sorted(technologies.items())}
        apps = list(summary.get("spring_boot_apps") or [])
        _extend_unique(apps, other_summary.get("spring_boot_apps") or [])
        summary["spring_boot_apps"] = apps
        summary["frameworks"] = sorted(set(summary.get("frameworks") or ["spring"]) | set(other_summary.get("frameworks") or []))
        summary["architecture_style"] = "microservices" if len(apps) > 1 else summary.get("architecture_style") or "monolith"

    # destinations are derived: rebuild them from the combined producers and consumers
    messaging = (merged.get("integrations") or {}).get("messaging")
    if isinstance(messaging, dict) and rest_has_messaging(models):
        topics: Dict[tuple, dict] = {}
        for p in messaging.get("producers", []):
            key = (p.get("technology"), p.get("destination"))
            topics.setdefault(key, {"technology": key[0], "destination": key[1], "producers": [], "consumers": []})["producers"].append(p.get("source"))
        for c in messaging.get("consumers", []):
            for dest in c.get("destinations") or [None]:
                key = (c.get("technology"), dest)
                topics.setdefault(key, {"technology": key[0], "destination": dest, "producers": [], "consumers": []})["consumers"].append(c.get("handler"))
        messaging["destinations"] = sorted(
            ({**v, "producers": sorted(set(filter(None, v["producers"]))), "consumers": sorted(set(filter(None, v["consumers"])))}
             for v in topics.values()), key=lambda v: (str(v["technology"]), str(v["destination"])))
    return merged


def rest_has_messaging(models: List[dict]) -> bool:
    return any(isinstance((m.get("integrations") or {}).get("messaging"), dict) for m in models)


# ============================
# GRAPHS
# ============================

def _merge_edge(edges: Dict[tuple, dict], src, dst, kind, details):
    if src == dst:
        return
    key = (src, dst, kind)
    edge = edges.setdefault(key, {"source": src, "target": dst, "kind": kind, "details": []})
    for d in details or []:
        if d and d not in edge["details"] and len(edge["details"]) < 30:
            edge["details"].append(d)


def system_view(model: dict) -> dict:
    """Modules and external systems; component edges are lifted to their module."""
    graph = model.get("graph") or {}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}

    def owner(node_id):
        node = nodes.get(node_id, {})
        if node.get("type") == "component":
            return f"module:{node.get('module', '')}"
        return node_id

    edges: Dict[tuple, dict] = {}
    for e in graph.get("edges", []):
        if e["kind"] in STRUCTURAL_EDGES:
            continue
        _merge_edge(edges, owner(e["from"]), owner(e["to"]), e["kind"], e.get("details"))

    used = {e["source"] for e in edges.values()} | {e["target"] for e in edges.values()}
    view_nodes = [
        _view_node(n) for n in nodes.values()
        if n.get("type") != "component" and (n["id"] in used or n.get("type") == "module")
    ]
    return {"nodes": view_nodes, "links": list(edges.values())}


def component_view(model: dict, module: Optional[str] = None) -> dict:
    """Components (optionally of one module) with the systems they touch."""
    graph = model.get("graph") or {}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}

    def in_scope(node_id):
        node = nodes.get(node_id, {})
        return node.get("type") == "component" and (module is None or node.get("module") == module)

    edges: Dict[tuple, dict] = {}
    for e in graph.get("edges", []):
        if e["kind"] == "contains":
            continue
        if in_scope(e["from"]) or in_scope(e["to"]):
            # keep component <-> component edges only inside the scope
            if e["kind"] in ("injects", "calls") and not (in_scope(e["from"]) and in_scope(e["to"])):
                continue
            _merge_edge(edges, e["from"], e["to"], e["kind"], e.get("details"))

    used = {e["source"] for e in edges.values()} | {e["target"] for e in edges.values()}
    used |= {n for n in nodes if in_scope(n)}
    return {
        "nodes": [_view_node(nodes[n]) for n in sorted(used) if n in nodes],
        "links": list(edges.values()),
    }


def _view_node(node: dict) -> dict:
    label = node.get("name") or node.get("technology") or node["id"].split(":", 1)[-1]
    if node.get("type") == "datastore" and node.get("host"):
        label = f"{node.get('technology')} @ {node['host']}"
    elif node.get("type") in ("cache", "broker", "storage", "email", "search") and node.get("technology"):
        label = node["technology"] + (f" @ {node['host']}" if node.get("host") else "")
    return {**node, "label": label}


# ============================
# ENDPOINT RELATIONSHIPS
# ============================

def find_endpoint(model: dict, api: str, route: Optional[dict] = None) -> Optional[dict]:
    """Endpoint summary of an API (docs are keyed by handler method name)."""
    # docs are named Controller.method (scanner 2.0) or method (older docs)
    candidates = [
        e for e in model.get("endpoints", [])
        if api in (e.get("doc_name"), e["handler"], e["handler"].rsplit(".", 1)[-1])
    ]

    if route:
        exact = [e for e in candidates if e.get("handler") == route.get("handler")]
        exact = exact or [e for e in candidates if (e["method"], e["path"]) == (route.get("method"), route.get("path"))]
        candidates = exact or candidates

    return candidates[0] if candidates else None


def _module_name(model, module_path):
    for m in model.get("modules", []):
        if m.get("path") == module_path:
            return (m.get("runtime") or {}).get("application_name") or m.get("name") or module_path
    return module_path


def _context_path(model, module_path):
    for m in model.get("modules", []):
        if m.get("path") == module_path:
            return (m.get("runtime") or {}).get("context_path") or ""
    return ""


def _path_regex(path):
    """/owners/{id}/pets -> regex matching concrete or templated variants."""
    parts = []
    for segment in path.strip("/").split("/"):
        if re.fullmatch(r"\{[^}]+\}", segment):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    return re.compile("^/" + "/".join(parts) + "/?$")


def _url_path(detail: str) -> Optional[str]:
    """'GET http://svc/owners/{id}?x=1' -> '/owners/{id}'"""
    url = detail.split(" ", 1)[-1] if " " in detail else detail
    url = re.sub(r"^\w+://[^/]+", "", url)
    url = url.split("?", 1)[0]
    return url if url.startswith("/") else None


def _gateway_matches(predicate: str, path: str) -> bool:
    pattern = predicate.split("=", 1)[-1] if "=" in predicate else predicate
    regex = re.escape(pattern.strip()).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.match(regex, path) is not None


def consumers(model: dict, endpoint: dict) -> List[dict]:
    """Who calls this endpoint: other services (HTTP/Feign) and gateways."""
    graph = model.get("graph") or {}
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    module_id = f"module:{endpoint['module']}"
    context = _context_path(model, endpoint["module"])
    regexes = [_path_regex(context + endpoint["path"]), _path_regex(endpoint["path"])]
    found = []

    for e in graph.get("edges", []):
        if e["to"] != module_id:
            continue

        source = nodes.get(e["from"], {})
        caller = source.get("name") or e["from"]
        caller_module = source.get("module") if source.get("type") == "component" else source.get("path")

        if e["kind"] == "http":
            for detail in e.get("details") or []:
                method = detail.split(" ", 1)[0] if " " in detail else None
                path = _url_path(detail)
                if path and any(r.match(path) for r in regexes) and method in (None, endpoint["method"], "ANY"):
                    found.append({
                        "kind": "service_call",
                        "caller": caller,
                        "caller_type": source.get("type"),
                        "caller_module": _module_name(model, caller_module) if caller_module is not None else None,
                        "detail": detail,
                    })

        elif e["kind"] == "routes":
            full_path = context + endpoint["path"]
            for detail in e.get("details") or []:
                if any(_gateway_matches(p.strip(), full_path) for p in detail.split(",")):
                    found.append({
                        "kind": "gateway_route",
                        "caller": caller,
                        "caller_type": source.get("type"),
                        "caller_module": source.get("name"),
                        "detail": detail,
                    })

    # the scanner also lifts component calls to module -> module edges; keep the precise caller
    precise = {(f["caller_module"], f["detail"]) for f in found if f["caller_type"] == "component"}
    unique = []
    for f in found:
        if f["caller_type"] == "module" and f["kind"] == "service_call" and (f["caller_module"], f["detail"]) in precise:
            continue
        if f not in unique:
            unique.append(f)
    return unique


def shared_resources(model: dict, endpoint: dict) -> List[dict]:
    """Tables, caches and destinations this endpoint shares with other endpoints and listeners."""
    others = [e for e in model.get("endpoints", []) if e["handler"] != endpoint["handler"] or e["path"] != endpoint["path"]]
    shared = []

    for kind, key in (("table", "tables"), ("cache", "caches")):
        for name in endpoint.get(key) or []:
            users = sorted({f"{e['method']} {e['path']} ({e['handler']})" for e in others if name in (e.get(key) or [])})
            if users:
                shared.append({"kind": kind, "name": name, "also_used_by": users})

    destinations = (model.get("integrations") or {}).get("messaging", {}).get("destinations", [])
    for name in endpoint.get("publishes") or []:
        for d in destinations:
            if str(d.get("destination")) == name and d.get("consumers"):
                shared.append({"kind": "topic", "name": name, "also_used_by": d["consumers"],
                               "technology": d.get("technology")})

    return shared


# ============================
# LAYERED DIAGRAMS
# ============================
# Left-to-right columns instead of a force layout: who receives traffic on the
# left, the systems everything ends up in on the right. Positions are stable,
# lines only go forward, and node order is chosen to reduce crossings.

# per-module persistence frameworks are an implementation detail of the service
ACCESS_FRAMEWORKS = {"mybatis", "mybatis-plus", "jpa", "hibernate", "jdbc", "jdbctemplate",
                     "spring-data-jpa", "spring-data-jdbc", "r2dbc", "jooq",
                     "sqlalchemy", "sqlmodel", "tortoise", "peewee", "databases", "beanie", "mongoengine", "odmantic", "ormar", "prisma",
                     "efcore", "dapper", "sql", "marten", "ravendb", "nhibernate"}

CALL_KINDS = {"http", "routes", "calls", "injects", "feign"}
MESSAGING_KINDS = {"publishes", "subscribes", "consumes", "listens", "delivers", "sends", "triggers"}
# a broker every service merely connects to is a platform bus (e.g. Spring Cloud Bus), not a dependency to draw
BUS_SHARE = 0.5
DATA_TYPES = {"datastore", "cache", "storage", "search"}
MESSAGING_TYPES = {"broker", "email"}

TYPE_LABELS = {
    "module": "Service", "datastore": "Database", "cache": "Cache", "storage": "Storage", "search": "Search",
    "broker": "Message broker", "email": "Email", "external_service": "External service",
    "infrastructure": "Platform", "component": "Component",
}
STEREOTYPE_LABELS = {
    "rest_controller": "API controller / router", "controller": "Controller", "service": "Service", "application": "Application",
    "configuration_properties": "Settings",
    "repository": "Repository", "feign_client": "HTTP client", "component": "Component",
    "listener": "Listener", "scheduler": "Scheduled job", "mapper": "Mapper",
}


def _link_category(kinds, target_type) -> str:
    if target_type == "external_service":
        return "external"
    if kinds & MESSAGING_KINDS or target_type in MESSAGING_TYPES:
        return "messaging"
    if kinds & CALL_KINDS and target_type in ("module", "component"):
        return "call"
    return "data"


def _display_kinds(kinds) -> List[str]:
    shown = sorted(k for k in kinds if k not in ("connects", "injects"))
    if not shown and "injects" in kinds:
        shown = ["uses"]
    return shown or sorted(kinds)


def _order_columns(columns: List[List[str]], links: List[dict]) -> List[List[str]]:
    """Barycenter ordering: put each node next to the nodes it is linked with."""
    neighbors: Dict[str, set] = {}
    for link in links:
        neighbors.setdefault(link["source"], set()).add(link["target"])
        neighbors.setdefault(link["target"], set()).add(link["source"])

    def sweep(cols, forward):
        indexes = range(1, len(cols)) if forward else range(len(cols) - 2, -1, -1)
        for i in indexes:
            ref = cols[i - 1] if forward else cols[i + 1]
            pos = {n: k for k, n in enumerate(ref)}
            current = {n: k for k, n in enumerate(cols[i])}

            def key(n):
                linked = [pos[m] for m in neighbors.get(n, ()) if m in pos]
                return (sum(linked) / len(linked)) if linked else current[n] + 0.5

            cols[i] = sorted(cols[i], key=lambda n: (key(n), current[n]))
        return cols

    columns = [list(c) for c in columns]
    for _ in range(3):
        columns = sweep(columns, True)
        columns = sweep(columns, False)
    return columns


def _layered(nodes: Dict[str, dict], links: List[dict], column_of, titles: List[str], groups_of=None) -> dict:
    count = len(titles)
    raw = [[] for _ in range(count)]
    for node_id in sorted(nodes, key=lambda n: nodes[n]["label"].lower()):
        raw[column_of(node_id)].append(node_id)

    ordered = _order_columns(raw, links)
    linked = {l["source"] for l in links} | {l["target"] for l in links}
    ordered = [sorted(col, key=lambda n: n not in linked) for col in ordered]
    columns = []
    for title, ids in zip(titles, ordered):
        if not ids:
            continue
        if groups_of:
            # keep groups together (e.g. brokers before external services), order inside kept
            ids = sorted(ids, key=lambda n: (groups_of(n)[0], n not in linked))
        column = {"title": title, "nodes": []}
        for node_id in ids:
            node = nodes[node_id]
            group = groups_of(node_id)[1] if groups_of else None
            column["nodes"].append({**node, "group": group})
        columns.append(column)
    return {"columns": columns, "links": links}


def _merge_links(raw_edges, nodes) -> List[dict]:
    merged: Dict[tuple, dict] = {}
    for src, dst, kind, details in raw_edges:
        if src == dst or src not in nodes or dst not in nodes:
            continue
        if nodes[src]["type"] in MESSAGING_TYPES and nodes[dst]["type"] in ("module", "component"):
            # a broker delivering to a listener: draw it from the consumer, like every other dependency
            src, dst, kind = dst, src, "consumes"
        link = merged.setdefault((src, dst), {"source": src, "target": dst, "kinds": set(), "details": []})
        link["kinds"].add(kind)
        for d in details or []:
            if d and d not in link["details"] and len(link["details"]) < 12:
                link["details"].append(d)
    out = []
    for link in merged.values():
        kinds = link["kinds"]
        out.append({**link, "kinds": _display_kinds(kinds),
                    "category": _link_category(kinds, nodes[link["target"]]["type"])})
    return out


def _system_node(node: dict, model: dict) -> dict:
    view = _view_node(node)
    out = {"id": node["id"], "type": node["type"], "label": view["label"], "type_label": TYPE_LABELS.get(node["type"], node["type"]),
           "meta": [], "badges": []}
    if node["type"] == "module":
        module = next((m for m in model.get("modules", []) if m.get("path") == node.get("path")), {})
        out["path"] = node.get("path")
        out["label"] = (module.get("runtime") or {}).get("application_name") or node.get("name") or out["label"]
        if node.get("port"):
            out["meta"].append(f"port {node['port']}")
        if module.get("endpoint_count"):
            out["meta"].append(f"{module['endpoint_count']} endpoint{'s' if module['endpoint_count'] != 1 else ''}")
        if not node.get("spring_boot_app", True):
            out["badges"].append("library")
    else:
        if node.get("technology") and node["technology"] != out["label"].split(" @ ")[0]:
            out["meta"].append(node["technology"])
        if node.get("database"):
            out["meta"].append(f"db {node['database']}")
        if node["id"] == "external:unresolved":
            out["label"] = "Unresolved URLs"
            out["muted"] = True
            out["meta"].append("URL built at runtime")
    return out


def layered_system_view(model: dict) -> dict:
    """System diagram: gateways -> services -> data -> messaging & external; platform listed apart."""
    graph = model.get("graph") or {}
    raw_nodes = {n["id"]: n for n in graph.get("nodes", [])}
    module_ids = {n["id"] for n in raw_nodes.values() if n.get("type") == "module"}
    module_by_path = {n.get("path"): n["id"] for n in raw_nodes.values() if n.get("type") == "module"}

    alias: Dict[str, str] = {}
    access: Dict[str, set] = {}
    merged_store: Dict[str, dict] = {}
    for n in raw_nodes.values():
        if n.get("type") == "component":
            alias[n["id"]] = module_by_path.get(n.get("module"), f"module:{n.get('module')}")
        elif n["id"].startswith("store:") and n.get("module") is not None:
            tech = (n.get("technology") or "").lower()
            if tech in ACCESS_FRAMEWORKS:
                owner = module_by_path.get(n["module"]) or f"module:{n['module']}"
                alias[n["id"]] = owner
                access.setdefault(owner, set()).add(n.get("technology"))
            else:
                key = f"store-tech:{tech}"
                alias[n["id"]] = key
                merged_store.setdefault(key, {"id": key, "type": n.get("type") or "datastore", "technology": n.get("technology")})

    def resolve(node_id):
        return alias.get(node_id, node_id)

    platform: Dict[str, dict] = {}
    raw_edges = []
    for e in graph.get("edges", []):
        if e["kind"] == "contains":
            continue
        src, dst = resolve(e["from"]), resolve(e["to"])
        if src == dst:
            continue
        target = raw_nodes.get(dst) or merged_store.get(dst) or {}
        if target.get("type") == "infrastructure":
            entry = platform.setdefault(dst, {"id": dst, "label": _view_node(target)["label"], "technology": target.get("technology"),
                                              "url": target.get("url"), "used_by": set(), "kinds": set()})
            source = raw_nodes.get(src, {})
            entry["used_by"].add(source.get("name") or src.split(":", 1)[-1])
            entry["kinds"].add(e["kind"])
            continue
        if e["kind"] in ("injects", "calls") and not (src in module_ids and dst in module_ids):
            continue
        raw_edges.append((src, dst, e["kind"], e.get("details")))

    nodes: Dict[str, dict] = {}
    for node_id in module_ids:
        node = _system_node(raw_nodes[node_id], model)
        if access.get(node_id):
            node["meta"].append(" · ".join(sorted(access[node_id])))
        nodes[node_id] = node
    used = {s for s, *_ in raw_edges} | {d for _, d, *_ in raw_edges}
    for node_id in used - module_ids:
        source = raw_nodes.get(node_id) or merged_store.get(node_id)
        if source and source.get("type") not in ("component", "infrastructure", "module"):
            nodes[node_id] = _system_node(source, model)

    links = _merge_links(raw_edges, nodes)

    for broker in [n for n in nodes if nodes[n]["type"] == "broker"]:
        touching = [l for l in links if broker in (l["source"], l["target"])]
        bus = [l for l in touching if l["kinds"] == ["connects"]]
        if len(bus) >= 3 and len(bus) >= BUS_SHARE * len(module_ids):
            platform[broker] = {"id": broker, "label": nodes[broker]["label"] + " (message bus)", "technology": nodes[broker]["label"],
                                "url": None, "used_by": {nodes[l["source"]]["label"] for l in bus}, "kinds": {"connects"}}
            links = [l for l in links if l not in bus]
            if len(bus) == len(touching):
                del nodes[broker]

    apps = {m for m in module_ids if "library" not in nodes[m]["badges"]}
    libraries = module_ids - apps if apps else set()
    for lib in libraries:
        nodes[lib]["type_label"] = "Library"
        nodes[lib]["badges"] = [b for b in nodes[lib]["badges"] if b != "library"]
        for l in links:
            if l["target"] == lib and l["category"] == "call":
                l["category"], l["kinds"] = "library", ["uses library"]

    gateways = {l["source"] for l in links if "routes" in l["kinds"] and l["source"] in apps}
    titles = ["Entry points", "Services", "Data & infrastructure"]
    infra_groups = {"datastore": (0, "Databases"), "search": (0, "Databases"), "cache": (1, "Caches"), "storage": (2, "Storage"),
                    "broker": (3, "Messaging"), "email": (3, "Messaging"), "external_service": (4, "External services")}

    def column_of(node_id):
        if node_id in gateways:
            return 0
        return 1 if nodes[node_id]["type"] == "module" else 2

    def groups_of(node_id):
        if node_id in libraries:
            return (1, "Shared libraries")
        return infra_groups.get(nodes[node_id]["type"], (0, None))

    if gateways:
        for g in gateways:
            nodes[g]["badges"].append("gateway")

    view = _layered(nodes, links, column_of, titles, groups_of)
    view["counts"] = {"services": len(apps or module_ids), "libraries": len(libraries)}
    view["platform"] = sorted(
        ({**p, "used_by": sorted(p["used_by"]), "kinds": sorted(p["kinds"])} for p in platform.values()),
        key=lambda p: (-len(p["used_by"]), p["label"]))
    return view


def layered_component_view(model: dict, module: str) -> dict:
    """One module: entry components -> services -> repositories & clients -> systems they touch."""
    graph = model.get("graph") or {}
    raw_nodes = {n["id"]: n for n in graph.get("nodes", [])}
    components = {n["id"]: n for n in raw_nodes.values() if n.get("type") == "component" and n.get("module") == module}
    hidden_layers = {"config"}
    shown = {i for i, n in components.items() if n.get("layer") not in hidden_layers and n.get("stereotype") != "controller_advice"}

    raw_edges, others = [], set()
    for e in graph.get("edges", []):
        if e["kind"] == "contains":
            continue
        src, dst = e["from"], e["to"]
        if src not in shown and dst not in shown:
            continue
        if e["kind"] in ("injects", "calls") and not (src in shown and dst in shown):
            continue
        target = raw_nodes.get(dst, {})
        if target.get("type") == "infrastructure":
            continue
        for end in (src, dst):
            if end not in shown and end in raw_nodes:
                others.add(end)
        raw_edges.append((src, dst, e["kind"], e.get("details")))

    nodes: Dict[str, dict] = {}
    for node_id in shown:
        c = components[node_id]
        nodes[node_id] = {"id": node_id, "type": "component", "label": c.get("name"),
                          "type_label": STEREOTYPE_LABELS.get(c.get("stereotype"), (c.get("stereotype") or "component").replace("_", " ")),
                          "layer": c.get("layer"), "meta": [], "badges": []}
    for node_id in others:
        nodes[node_id] = _system_node(raw_nodes[node_id], model)
        raw = raw_nodes[node_id]
        if (raw.get("technology") or "").lower() in ACCESS_FRAMEWORKS:
            # name the database behind the persistence framework (profiles look like "dev: mysql")
            databases = sorted({p.split(":", 1)[-1].strip() for p in raw.get("profiles") or [] if ":" in p})
            if databases:
                nodes[node_id]["label"] = f"{' / '.join(databases)} via {raw['technology']}"

    links = _merge_links(raw_edges, nodes)
    layer_rank = {"web": 0, "messaging": 0, "scheduling": 0, "service": 1, "data": 2, "integration": 2}

    titles = ["Entry points", "Services", "Repositories & clients", "Systems"]

    def column_of(node_id):
        node = nodes[node_id]
        if node["type"] != "component":
            return 3
        return min(layer_rank.get(node["layer"], 1), 2)

    def groups_of(node_id):
        node_type = nodes[node_id]["type"]
        order = {"module": (0, "Other services"), "datastore": (1, "Data"), "cache": (1, "Data"), "storage": (1, "Data"),
                 "search": (1, "Data"), "broker": (2, "Messaging"), "email": (2, "Messaging"), "external_service": (3, "External")}
        return order.get(node_type, (0, None))

    view = _layered(nodes, links, column_of, titles, groups_of)
    view["hidden"] = len(components) - len(shown)
    return view
