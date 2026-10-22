"""
Project level facts: which folders are separate Python projects (modules), their dependency manifests,
environment configuration (.env files, Settings defaults, docker-compose) and the infrastructure derived from it.
"""
import logging
import os
import re
from typing import Dict, List, Optional

from mergeclear.core.files import SKIP_DIRS

from mergeclear.core.compose import load_compose, parse_url  # noqa: F401  (re-exported: the extractor imports them from here)

from .catalog import IGNORED_CATEGORIES, INFRA_KEYS, is_secret_name, mask_url, normalize_package, package_info

logger = logging.getLogger(__name__)

PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt", "requirements.in", "poetry.lock", "uv.lock")
ENV_FILE = re.compile(r"^(\.env(\.[\w.-]+)?|[\w.-]+\.env)$")


# ============================================================================
# FILE DISCOVERY
# ============================================================================

def find_project_dirs(repo_path: str) -> List[str]:
    """Relative folders holding a Python project; "" (the root) is always one."""
    found = {""}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(root, repo_path).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        if rel.count("/") >= 4:
            dirs[:] = []
            continue
        if any(f in PROJECT_MARKERS for f in files):
            found.add(rel)
    return sorted(found)


def module_for(rel: str, project_dirs: List[str]) -> str:
    best = ""
    for d in project_dirs:
        if d and (rel == d or rel.startswith(d + "/")) and len(d) > len(best):
            best = d
    return best


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ============================================================================
# TOML (tomllib when available, a small reader otherwise)
# ============================================================================

def load_toml(content: str) -> dict:
    try:
        import tomllib                                  # Python 3.11+
        return tomllib.loads(content)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("toml parse failed: %s", e)
        return {}
    return _mini_toml(content)


def _bare(line: str) -> str:
    """A line without its string literals (so brackets inside "pkg[extra]" are not structure)."""
    return re.sub(r""""[^"]*"|'[^']*'""", '""', line)


def _mini_toml(content: str) -> dict:
    """Just enough TOML for dependency tables: sections, key = "string" | [list] | scalar."""
    root: dict = {}
    current = root
    pending_key, pending = None, []
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not re.search(r"""["'].*#.*["']""", raw) else raw.rstrip()
        if pending_key is not None:
            pending.append(line)
            if "]" in _bare(line):
                current[pending_key] = re.findall(r"""["']([^"']+)["']""", " ".join(pending))
                pending_key, pending = None, []
            continue
        header = re.match(r"^\s*\[\[?([^\]]+)\]\]?\s*$", line)
        if header:
            current = root
            for part in header.group(1).strip().split("."):
                current = current.setdefault(part.strip().strip('"'), {})
            continue
        match = re.match(r"""^\s*([\w."-]+)\s*=\s*(.*)$""", line)
        if not match:
            continue
        key, value = match.group(1).strip('"'), match.group(2).strip()
        if value.startswith("[") and "]" not in _bare(value):
            pending_key, pending = key, [value]
        elif value.startswith("["):
            current[key] = re.findall(r"""["']([^"']+)["']""", value)
        elif value.startswith(("'", '"')):
            current[key] = value.strip("\"'")
        elif value.startswith("{"):
            current[key] = {m.group(1): m.group(2) for m in re.finditer(r"""(\w+)\s*=\s*["']([^"']+)["']""", value)}
        else:
            current[key] = value
    return root


# ============================================================================
# DEPENDENCIES
# ============================================================================

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def _requirement(spec: str) -> Optional[dict]:
    spec = spec.split(";", 1)[0].split("#", 1)[0].strip()
    if not spec or spec.startswith(("-", "git+", "http", ".", "/")):
        egg = re.search(r"#egg=([\w.-]+)", spec)
        if not egg:
            return None
        return {"name": egg.group(1), "version": None}
    match = _REQ_LINE.match(spec)
    if not match:
        return None
    version = match.group(3).strip() or None
    return {"name": match.group(1), "version": version}


def _dependency(name: str, version: Optional[str], scope: str) -> dict:
    info = package_info(name)
    return {
        "group": None, "artifact": normalize_package(name), "version": version, "scope": scope,
        "category": info[0] if info else None, "technology": info[1] if info else None,
    }


def _scope_for(filename: str) -> str:
    return "test" if re.search(r"(test|dev|lint|docs?|ci|typing|mypy)", filename, re.I) else "compile"


def parse_requirements(repo_path: str, directory: str) -> List[dict]:
    out, base = [], os.path.join(repo_path, directory)
    seen = set()

    def read_file(path, scope, depth=0):
        if path in seen or depth > 4 or not os.path.isfile(path):
            return
        seen.add(path)
        for line in _read(path).splitlines():
            line = line.strip()
            include = re.match(r"^-r\s+(\S+)", line) or re.match(r"^--requirement[= ]+(\S+)", line)
            if include:
                read_file(os.path.normpath(os.path.join(os.path.dirname(path), include.group(1))), scope, depth + 1)
                continue
            req = _requirement(line)
            if req:
                out.append(_dependency(req["name"], req["version"], scope))

    candidates = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if re.match(r"^requirements([-_.][\w.-]*)?\.(txt|in)$", name):
                candidates.append(os.path.join(base, name))
        req_dir = os.path.join(base, "requirements")
        if os.path.isdir(req_dir):
            candidates.extend(os.path.join(req_dir, n) for n in sorted(os.listdir(req_dir)) if n.endswith((".txt", ".in")))
    locked = {c for c in candidates if c.endswith(".txt") and os.path.exists(c[:-4] + ".in")}
    for path in candidates:
        if path in locked:
            continue                                    # the .in file lists the intent, the .txt its lock
        read_file(path, _scope_for(os.path.basename(path)))
    return out


def parse_pyproject(content: str) -> dict:
    data = load_toml(content)
    project = data.get("project") or {}
    poetry = (data.get("tool") or {}).get("poetry") or {}
    deps: List[dict] = []
    for spec in project.get("dependencies") or []:
        req = _requirement(spec)
        if req:
            deps.append(_dependency(req["name"], req["version"], "compile"))
    for group, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs if isinstance(specs, list) else []:
            req = _requirement(spec)
            if req:
                deps.append(_dependency(req["name"], req["version"], "test" if re.search(r"(dev|test|lint|doc)", group) else "optional"))
    for group, specs in (data.get("dependency-groups") or {}).items():
        for spec in specs if isinstance(specs, list) else []:
            if isinstance(spec, str):
                req = _requirement(spec)
                if req:
                    deps.append(_dependency(req["name"], req["version"], "test" if re.search(r"(dev|test|lint|doc)", group) else "compile"))
    for name, spec in (poetry.get("dependencies") or {}).items():
        if name.lower() != "python":
            deps.append(_dependency(name, spec if isinstance(spec, str) else (spec or {}).get("version") if isinstance(spec, dict) else None,
                                    "compile"))
    for group_name, group in (poetry.get("group") or {}).items():
        for name, spec in ((group or {}).get("dependencies") or {}).items():
            deps.append(_dependency(name, spec if isinstance(spec, str) else None, "test" if re.search(r"(dev|test|lint|doc)", group_name) else "compile"))
    for name, spec in (poetry.get("dev-dependencies") or {}).items():
        deps.append(_dependency(name, spec if isinstance(spec, str) else None, "test"))
    backend = ((data.get("build-system") or {}).get("build-backend") or "")
    tool = data.get("tool") or {}
    build_tool = "poetry" if poetry else "uv" if "uv" in tool else "pdm" if "pdm" in tool else "hatch" if "hatch" in backend or "hatch" in tool \
        else "flit" if "flit" in backend else "setuptools" if backend or "setuptools" in tool else "pip"
    requires = project.get("requires-python") or (poetry.get("dependencies") or {}).get("python")
    return {
        "name": project.get("name") or poetry.get("name"), "version": project.get("version") or poetry.get("version"),
        "description": project.get("description") or poetry.get("description"), "dependencies": deps, "build_tool": build_tool,
        "python": requires if isinstance(requires, str) else None,
        "scripts": list((project.get("scripts") or poetry.get("scripts") or {}).keys()) if isinstance(project.get("scripts") or poetry.get("scripts") or {}, dict) else [],
    }


def parse_pipfile(content: str) -> List[dict]:
    data = load_toml(content)
    out = []
    for name, spec in (data.get("packages") or {}).items():
        out.append(_dependency(name, spec if isinstance(spec, str) and spec != "*" else None, "compile"))
    for name, spec in (data.get("dev-packages") or {}).items():
        out.append(_dependency(name, spec if isinstance(spec, str) and spec != "*" else None, "test"))
    return out


def parse_setup_py(content: str) -> List[dict]:
    out = []
    match = re.search(r"install_requires\s*=\s*\[(.*?)\]", content, re.S)
    if match:
        for spec in re.findall(r"""["']([^"']+)["']""", match.group(1)):
            req = _requirement(spec)
            if req:
                out.append(_dependency(req["name"], req["version"], "compile"))
    return out


# ============================================================================
# ENVIRONMENT
# ============================================================================

def parse_env_file(content: str) -> Dict[str, str]:
    out = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(2).strip()
        if value and value[0] in "\"'" and value.count(value[0]) >= 2:
            value = value[1:value.rindex(value[0])]
        else:
            value = value.split(" #", 1)[0].strip()
        out[match.group(1)] = value
    return out


class ModuleConfig:
    """Configuration of one project: defaults (Settings classes, .env), per-file profiles, compose environment."""

    def __init__(self):
        self.default: Dict[str, str] = {}
        self.profiles: Dict[str, Dict[str, str]] = {}
        self.files: List[str] = []
        self.compose_service: Optional[dict] = None

    def get(self, *keys: str) -> Optional[str]:
        lowered = {k.lower(): v for k, v in self.default.items()}
        for key in keys:
            if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
                return lowered[key.lower()]
        return None


def load_env_files(repo_path: str, directory: str) -> ModuleConfig:
    config = ModuleConfig()
    base = os.path.join(repo_path, directory)
    if not os.path.isdir(base):
        return config
    order = {".env.example": 0, ".env.sample": 0, ".env.template": 0, ".env.dist": 0, ".env.local.example": 0}
    names = [n for n in sorted(os.listdir(base)) if ENV_FILE.match(n) and os.path.isfile(os.path.join(base, n))]
    for name in sorted(names, key=lambda n: (order.get(n, 1 if n == ".env" else 2), n)):
        values = parse_env_file(_read(os.path.join(base, name)))
        if not values:
            continue
        rel = (directory + "/" + name).lstrip("/")
        config.files.append(rel)
        if name in order or name == ".env":
            config.default.update(values)
        else:
            config.profiles[re.sub(r"^\.env\.?|\.env$", "", name) or name] = values
    return config


# ============================================================================
# INFRASTRUCTURE
# ============================================================================

def derive_infrastructure(config: ModuleConfig, compose: Optional[dict], app_name: Optional[str], root_path: Optional[str]) -> dict:
    """Runtime picture of a module from its configuration: data sources, cache, brokers, identity provider."""
    values: Dict[str, str] = dict(config.default)
    if compose:
        values.update({k: v for k, v in compose.get("environment", {}).items() if v})
    runtime: dict = {}
    datasources: List[dict] = []

    def add_datasource(entry, profile):
        entry = {k: v for k, v in entry.items() if v is not None}
        if entry not in datasources and not any(d.get("type") == entry.get("type") and d.get("host") == entry.get("host")
                                               and d.get("database") == entry.get("database") for d in datasources):
            datasources.append({**entry, "profile": profile})

    def classify(key: str, value: str, profile: str):
        parsed = parse_url(value)
        for pattern, kind in INFRA_KEYS:
            if not pattern.search(key):
                continue
            if kind == "database":
                if parsed and parsed["type"]:
                    add_datasource({"type": parsed["type"], "host": parsed["host"], "port": parsed["port"], "database": parsed["database"]}, profile)
            elif kind == "mongodb":
                host = parsed["host"] if parsed else value
                runtime.setdefault("mongodb", {k: v for k, v in {"host": host, "database": parsed.get("database") if parsed else None}.items() if v})
            elif kind == "redis":
                host = parsed["host"] if parsed else value
                runtime.setdefault("redis", {k: v for k, v in {"host": host, "port": parsed.get("port") if parsed else None}.items() if v})
            elif kind == "celery-broker":
                if parsed and parsed["type"] == "redis":
                    runtime.setdefault("redis", {"host": parsed["host"]} if parsed["host"] else {})
                elif parsed and parsed["type"] == "rabbitmq":
                    runtime.setdefault("rabbitmq", {"host": parsed["host"]} if parsed["host"] else {})
            elif kind == "kafka":
                runtime.setdefault("kafka", {"bootstrap_servers": value})
            elif kind == "rabbitmq":
                runtime.setdefault("rabbitmq", {"host": parsed["host"] if parsed else value})
            elif kind == "elasticsearch":
                runtime.setdefault("elasticsearch", {"host": parsed["host"] if parsed else value})
            elif kind == "smtp":
                runtime.setdefault("smtp", {"host": value})
            elif kind == "s3":
                runtime.setdefault("storage", {"type": "s3", "endpoint": value})
            elif kind == "nats":
                runtime.setdefault("nats", {"url": value})
            return
        if parsed and parsed["type"] and not any(p.search(key) for p, _ in INFRA_KEYS):
            if parsed["type"] in ("postgresql", "mysql", "sqlite", "oracle", "sqlserver", "clickhouse"):
                add_datasource({"type": parsed["type"], "host": parsed["host"], "port": parsed["port"], "database": parsed["database"]}, profile)

    for key, value in values.items():
        if value and not value.startswith("${"):
            classify(key, value, "default")
    for profile, profile_values in config.profiles.items():
        for key, value in profile_values.items():
            if value and not value.startswith("${"):
                classify(key, value, profile)

    # host / port / name given as separate settings (POSTGRES_SERVER, POSTGRES_DB, ...)
    for prefix, kind in (("POSTGRES", "postgresql"), ("POSTGRESQL", "postgresql"), ("PG", "postgresql"), ("MYSQL", "mysql"),
                         ("MARIADB", "mysql"), ("DB", None), ("DATABASE", None)):
        host = config.get(f"{prefix}_SERVER", f"{prefix}_HOST", f"{prefix}_HOSTNAME")
        name = config.get(f"{prefix}_DB", f"{prefix}_DATABASE", f"{prefix}_NAME")
        if host and (name or kind):
            dialect = kind or _dialect_from(config.get("DB_ENGINE", "DB_TYPE", "DATABASE_TYPE", "DB_DIALECT") or "") or "sql"
            add_datasource({"type": dialect, "host": host, "port": _int(config.get(f"{prefix}_PORT")), "database": name}, "default")
    for prefix, key in (("REDIS", "redis"), ("MONGO", "mongodb"), ("MONGODB", "mongodb"), ("RABBITMQ", "rabbitmq"), ("RABBIT", "rabbitmq"),
                        ("ELASTICSEARCH", "elasticsearch"), ("KAFKA", "kafka")):
        host = config.get(f"{prefix}_SERVER", f"{prefix}_HOST", f"{prefix}_HOSTNAME")
        if host and key not in runtime:
            runtime[key] = {"host": host} if key != "kafka" else {"bootstrap_servers": host}

    if datasources:
        runtime["datasources"] = datasources
    name = app_name or config.get("APP_NAME", "PROJECT_NAME", "SERVICE_NAME", "APPLICATION_NAME")
    if name:
        runtime["application_name"] = name
    port = _int(config.get("PORT", "APP_PORT", "SERVER_PORT", "API_PORT", "UVICORN_PORT"))
    if port:
        runtime["server_port"] = port
    if root_path or config.get("ROOT_PATH", "API_ROOT_PATH"):
        runtime["context_path"] = root_path or config.get("ROOT_PATH", "API_ROOT_PATH")
    provider = config.get("OIDC_ISSUER", "OAUTH_ISSUER", "KEYCLOAK_URL", "AUTH0_DOMAIN", "JWKS_URL", "OIDC_DISCOVERY_URL", "OAUTH2_ISSUER")
    if provider:
        runtime["identity_provider"] = provider
    return runtime


def _dialect_from(value: str) -> Optional[str]:
    value = value.lower()
    for token, dialect in (("postgres", "postgresql"), ("mysql", "mysql"), ("maria", "mysql"), ("sqlite", "sqlite"), ("oracle", "oracle"),
                           ("mssql", "sqlserver")):
        if token in value:
            return dialect
    return None


def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def masked(values: Dict[str, str]) -> Dict[str, str]:
    return {k: ("***" if is_secret_name(k) and v else mask_url(v)) for k, v in sorted(values.items())}


def public_dependencies(deps: List[dict]) -> List[dict]:
    return [d for d in deps if d.get("category") not in IGNORED_CATEGORIES or d.get("scope") != "test"]


# ============================================================================
# PROJECTS
# ============================================================================

class ProjectInfo:
    """One Python project (module): manifests, dependencies and configuration."""

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.build_tool = "pip"
        self.version: Optional[str] = None
        self.python: Optional[str] = None
        self.description: Optional[str] = None
        self.dependencies: List[dict] = []
        self.config = ModuleConfig()
        self.compose: Optional[dict] = None
        self.manifests: List[str] = []

    def dependency(self, name: str) -> Optional[dict]:
        wanted = normalize_package(name)
        return next((d for d in self.dependencies if d["artifact"] == wanted), None)


def load_project(repo_path: str, directory: str, compose_services: List[dict], fallback_name: str) -> ProjectInfo:
    base = os.path.join(repo_path, directory)
    project = ProjectInfo(directory, os.path.basename(directory) if directory else fallback_name)
    dependencies: List[dict] = []

    pyproject = os.path.join(base, "pyproject.toml")
    if os.path.isfile(pyproject):
        parsed = parse_pyproject(_read(pyproject))
        project.manifests.append((directory + "/pyproject.toml").lstrip("/"))
        project.build_tool = parsed["build_tool"]
        project.version = parsed["version"]
        project.python = parsed["python"]
        project.description = parsed["description"]
        if parsed["name"]:
            project.name = parsed["name"]
        dependencies.extend(parsed["dependencies"])
    pipfile = os.path.join(base, "Pipfile")
    if os.path.isfile(pipfile):
        project.manifests.append((directory + "/Pipfile").lstrip("/"))
        dependencies.extend(parse_pipfile(_read(pipfile)))
        if not os.path.isfile(pyproject):
            project.build_tool = "pipenv"
    setup_py = os.path.join(base, "setup.py")
    if os.path.isfile(setup_py):
        project.manifests.append((directory + "/setup.py").lstrip("/"))
        dependencies.extend(parse_setup_py(_read(setup_py)))
        if not os.path.isfile(pyproject):
            project.build_tool = "setuptools"
    requirements = parse_requirements(repo_path, directory)
    if requirements:
        project.manifests.append((directory + "/requirements.txt").lstrip("/"))
    dependencies.extend(requirements)
    if os.path.isfile(os.path.join(base, "uv.lock")) and project.build_tool in ("pip", "setuptools"):
        project.build_tool = "uv"

    seen = {}
    for d in dependencies:
        key = d["artifact"]
        if key not in seen or (seen[key]["scope"] != "compile" and d["scope"] == "compile") or (not seen[key]["version"] and d["version"]):
            seen[key] = d
    project.dependencies = sorted(seen.values(), key=lambda d: (d["scope"] != "compile", d["artifact"]))
    project.config = load_env_files(repo_path, directory)
    project.compose = next((s for s in compose_services if s.get("build_context") == directory and not s.get("infra")), None)
    if project.compose is not None:
        project.config.compose_service = project.compose
    return project
