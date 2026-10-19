"""
Project level facts: which folders are .NET projects (modules), their .csproj packages and settings, appsettings*.json
configuration, launch settings, docker-compose, and the infrastructure derived from connection strings.
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from urllib.parse import urlparse

from mergeclear.core.compose import load_compose, parse_url  # noqa: F401  (re-exported for the extractor)
from mergeclear.core.files import SKIP_DIRS

from .catalog import IGNORED_CATEGORIES, is_secret_name, package_info

logger = logging.getLogger(__name__)

DOTNET_SKIP = SKIP_DIRS | {"bin", "obj", "packages", ".vs", "TestResults", "artifacts", "wwwroot_"}


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def iter_cs_files(repo_path: str):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in DOTNET_SKIP and not d.startswith(".")]
        for f in files:
            if f.endswith(".cs") and not f.endswith((".g.cs", ".designer.cs", ".Designer.cs", ".AssemblyInfo.cs", ".cshtml.g.cs")):
                yield os.path.join(root, f)


def find_project_dirs(repo_path: str) -> List[str]:
    """Relative folders holding a .csproj; the repository root is always a candidate."""
    found = {""}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in DOTNET_SKIP and not d.startswith(".")]
        rel = os.path.relpath(root, repo_path).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        if any(f.endswith(".csproj") for f in files):
            found.add(rel)
    return sorted(found)


def module_for(rel: str, project_dirs: List[str]) -> str:
    best = ""
    for d in project_dirs:
        if d and (rel == d or rel.startswith(d + "/")) and len(d) > len(best):
            best = d
    return best


# ============================================================================
# CONFIGURATION (appsettings)
# ============================================================================

def flatten(value, prefix="", out=None) -> Dict[str, str]:
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(v, f"{prefix}:{k}" if prefix else str(k), out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            flatten(v, f"{prefix}:{i}", out)
    elif value is not None:
        out[prefix] = str(value).lower() if isinstance(value, bool) else str(value)
    return out


def strip_json_comments(content: str) -> str:
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
    content = re.sub(r"(?m)^\s*//.*$", "", content)
    content = re.sub(r'(?m)(?<=[,{\[\s])//[^"\n]*$', "", content)
    return re.sub(r",(\s*[}\]])", r"\1", content)


def load_json(path: str) -> dict:
    content = _read(path)
    for candidate in (content, strip_json_comments(content)):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except ValueError:
            continue
    return {}


class AppConfig:
    """appsettings.json (+ environment files) of one project."""

    def __init__(self):
        self.default: Dict[str, str] = {}
        self.profiles: Dict[str, Dict[str, str]] = {}
        self.files: List[str] = []
        self.launch: Dict[str, str] = {}
        self.compose_service: Optional[dict] = None

    def get(self, *keys: str) -> Optional[str]:
        lowered = {k.lower(): v for k, v in self.default.items()}
        for key in keys:
            if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
                return lowered[key.lower()]
        return None

    def matching(self, pattern: str) -> Dict[str, str]:
        regex = re.compile(pattern, re.I)
        return {k: v for k, v in self.default.items() if regex.search(k)}


def load_app_config(repo_path: str, directory: str) -> AppConfig:
    config = AppConfig()
    base = os.path.join(repo_path, directory)
    if not os.path.isdir(base):
        return config
    for name in sorted(os.listdir(base)):
        match = re.fullmatch(r"appsettings(?:\.([\w-]+))?\.json", name)
        if not match or not os.path.isfile(os.path.join(base, name)):
            continue
        values = flatten(load_json(os.path.join(base, name)))
        rel = (directory + "/" + name).lstrip("/")
        config.files.append(rel)
        if match.group(1) is None:
            config.default.update(values)
        else:
            config.profiles[match.group(1)] = values
    launch = load_json(os.path.join(base, "Properties", "launchSettings.json"))
    for profile in (launch.get("profiles") or {}).values():
        if isinstance(profile, dict):
            url = profile.get("applicationUrl")
            if url and "applicationUrl" not in config.launch:
                config.launch["applicationUrl"] = url
            for k, v in (profile.get("environmentVariables") or {}).items():
                config.launch.setdefault(k, str(v))
    if (directory + "/Properties/launchSettings.json").lstrip("/") and os.path.isfile(os.path.join(base, "Properties", "launchSettings.json")):
        config.files.append((directory + "/Properties/launchSettings.json").lstrip("/"))
    return config


# ============================================================================
# CSPROJ
# ============================================================================

class ProjectInfo:

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.csproj: Optional[str] = None
        self.sdk: str = ""
        self.frameworks: List[str] = []
        self.version: Optional[str] = None
        self.nullable = False
        self.assembly_name: Optional[str] = None
        self.root_namespace: Optional[str] = None
        self.dependencies: List[dict] = []
        self.project_references: List[str] = []
        self.config = AppConfig()
        self.compose: Optional[dict] = None
        self.is_web = False
        self.output_type = ""

    def dependency(self, name: str) -> Optional[dict]:
        wanted = name.lower()
        return next((d for d in self.dependencies if d["artifact"].lower() == wanted), None)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def central_versions(repo_path: str, directory: str) -> Dict[str, str]:
    """`<PackageVersion Include= Version=>` from Directory.Packages.props in the project folder or any parent."""
    out: Dict[str, str] = {}
    current = directory
    chain = []
    while True:
        chain.append(current)
        if not current:
            break
        current = current.rpartition("/")[0]
    for d in reversed(chain):
        path = os.path.join(repo_path, d, "Directory.Packages.props")
        if os.path.isfile(path):
            try:
                root = ET.fromstring(_read(path))
            except ET.ParseError:
                continue
            for el in root.iter():
                if _local(el.tag) == "PackageVersion" and el.get("Include"):
                    out[el.get("Include")] = el.get("Version") or ""
    return out


def _dependency(name: str, version: Optional[str], scope: str = "compile") -> dict:
    info = package_info(name)
    category = info[0] if info else None
    if category == "test":
        scope = "test"
    return {"group": None, "artifact": name, "version": version or None, "scope": scope, "category": category, "technology": info[1] if info else None}


def load_project(repo_path: str, directory: str, compose_services: List[dict], fallback_name: str) -> ProjectInfo:
    base = os.path.join(repo_path, directory)
    project = ProjectInfo(directory, os.path.basename(directory) if directory else fallback_name)
    csprojs = sorted(f for f in os.listdir(base) if f.endswith(".csproj")) if os.path.isdir(base) else []
    central = central_versions(repo_path, directory)
    for csproj in csprojs[:1]:
        project.csproj = (directory + "/" + csproj).lstrip("/")
        project.name = csproj[:-7]
        try:
            root = ET.fromstring(_read(os.path.join(base, csproj)))
        except ET.ParseError:
            root = None
        if root is None:
            continue
        project.sdk = root.get("Sdk") or ""
        project.is_web = "Web" in project.sdk
        for el in root.iter():
            tag = _local(el.tag)
            value = (el.text or "").strip()
            if tag == "TargetFramework" and value:
                project.frameworks = [value]
            elif tag == "TargetFrameworks" and value:
                project.frameworks = [v for v in value.split(";") if v]
            elif tag == "Nullable":
                project.nullable = value.lower() == "enable"
            elif tag in ("Version", "VersionPrefix") and value and not project.version:
                project.version = value
            elif tag == "AssemblyName" and value:
                project.assembly_name = value
            elif tag == "RootNamespace" and value:
                project.root_namespace = value
            elif tag == "OutputType":
                project.output_type = value
            elif tag == "PackageReference":
                name = el.get("Include") or el.get("Update")
                if not name:
                    continue
                version = el.get("Version") or next((_c.text for _c in el if _local(_c.tag) == "Version"), None) or central.get(name)
                project.dependencies.append(_dependency(name, version))
                if name == "Microsoft.AspNetCore.App":
                    project.is_web = True
            elif tag == "FrameworkReference" and el.get("Include") == "Microsoft.AspNetCore.App":
                project.is_web = True
            elif tag == "ProjectReference" and el.get("Include"):
                project.project_references.append(os.path.basename(el.get("Include").replace("\\", "/")).replace(".csproj", ""))
    project.config = load_app_config(repo_path, directory)
    project.compose = next((s for s in compose_services if s.get("build_context") == directory and not s.get("infra")), None)
    if project.compose is None:
        project.compose = next((s for s in compose_services if not s.get("infra") and s.get("name", "").lower() in (project.name.lower(), os.path.basename(directory).lower())), None)
    if project.compose is not None:
        project.config.compose_service = project.compose
    return project


# ============================================================================
# CONNECTION STRINGS AND INFRASTRUCTURE
# ============================================================================

def parse_connection_string(value: str) -> dict:
    """host / port / database of an ADO.NET, Npgsql, MongoDB or Redis style connection string."""
    out: dict = {}
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        out.update({"scheme": scheme, "host": parsed.hostname, "port": parsed.port, "database": parsed.path.lstrip("/") or None})
        return out
    pairs = {}
    for part in value.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            pairs[k.strip().lower()] = v.strip()
    host = next((pairs[k] for k in ("server", "host", "data source", "address", "addr", "endpoint", "hostname") if k in pairs), None)
    if host:
        host = re.sub(r"^(tcp|np):", "", host, flags=re.I)
        port = None
        match = re.match(r"^(.*?)[,:](\d+)$", host)
        if match:
            host, port = match.group(1), int(match.group(2))
        out["host"] = host
        out["port"] = port or (int(pairs["port"]) if pairs.get("port", "").isdigit() else None)
    database = next((pairs[k] for k in ("database", "initial catalog", "db", "dbname") if k in pairs), None)
    if database:
        out["database"] = database
    if "host" in pairs and ("username" in pairs or "user id" in pairs) and "initial catalog" not in pairs:
        out["scheme_hint"] = "postgresql"
    elif "initial catalog" in pairs or "trusted_connection" in pairs or "integrated security" in pairs or "trustservercertificate" in pairs:
        out["scheme_hint"] = "sqlserver"
    elif "uid" in pairs and "server" in pairs:
        out["scheme_hint"] = "mysql"
    elif "filename" in pairs or (pairs.get("data source", "").endswith((".db", ".sqlite", ".sqlite3")) or pairs.get("data source", "") == ":memory:"):
        out["scheme_hint"] = "sqlite"
        out.pop("host", None)
        out["port"] = None
        out["database"] = pairs.get("filename") or pairs.get("data source")
    elif pairs.get("host") and "," in value and "=" not in value:
        out["scheme_hint"] = "redis"
    elif "accountendpoint" in pairs:
        out["scheme_hint"] = "cosmosdb"
    return out


def database_type(key: str, parsed: dict, provider: Optional[str]) -> Optional[str]:
    if provider:
        return provider
    scheme = (parsed.get("scheme") or "").lower()
    mapping = {"postgres": "postgresql", "postgresql": "postgresql", "mysql": "mysql", "mongodb": "mongodb", "mongodb+srv": "mongodb", "sqlserver": "sqlserver",
               "redis": "redis", "rediss": "redis", "amqp": "rabbitmq", "amqps": "rabbitmq"}
    if scheme in mapping:
        return mapping[scheme]
    if parsed.get("scheme_hint"):
        return parsed["scheme_hint"]
    lowered = key.lower()
    for token, kind in (("postgres", "postgresql"), ("npgsql", "postgresql"), ("mysql", "mysql"), ("sqlite", "sqlite"), ("sqlserver", "sqlserver"), ("mssql", "sqlserver"),
                        ("mongo", "mongodb"), ("redis", "redis"), ("cosmos", "cosmosdb"), ("oracle", "oracle")):
        if token in lowered:
            return kind
    return None


def derive_infrastructure(project: ProjectInfo, providers: Dict[str, str], app_name: Optional[str]) -> dict:
    """Runtime picture of a project from appsettings: data sources, cache, brokers, identity provider, ports."""
    config = project.config
    values = dict(config.default)
    if config.compose_service:
        values.update({k: v for k, v in config.compose_service.get("environment", {}).items() if v})
        values = {**values, **{k.replace("__", ":"): v for k, v in values.items() if "__" in k}}
    runtime: dict = {}
    datasources: List[dict] = []
    seen = set()

    def add_datasource(entry, profile):
        key = (entry.get("type"), entry.get("host"), entry.get("database"))
        if key in seen:
            return
        seen.add(key)
        datasources.append({**{k: v for k, v in entry.items() if v is not None}, "profile": profile})

    def classify(key: str, value: str, profile: str):
        lowered = key.lower()
        if not value or value.startswith("${"):
            return
        looks_like_connection = ("connectionstring" in lowered or lowered.startswith("connectionstrings:") or "=" in value and ";" in value or "://" in value)
        parsed = parse_connection_string(value) if looks_like_connection else {}
        if not parsed.get("host") and not parsed.get("database") and not looks_like_connection:
            parsed = {}
        kind = database_type(key, parsed, providers.get(key.split(":")[-1])) if parsed else None
        if lowered.startswith("connectionstrings:") or "connectionstring" in lowered:
            if parsed and kind in ("postgresql", "mysql", "sqlserver", "sqlite", "oracle"):
                add_datasource({"type": kind, "host": parsed.get("host"), "port": parsed.get("port"), "database": parsed.get("database"), "name": key.split(":")[-1]}, profile)
                return
            if kind == "mongodb":
                runtime.setdefault("mongodb", {k: v for k, v in {"host": parsed.get("host"), "database": parsed.get("database")}.items() if v})
                return
            if kind == "redis" or re.search(r"redis|cache", lowered):
                runtime.setdefault("redis", {"host": parsed.get("host") or value.split(",")[0].split(":")[0]})
                return
            if kind == "rabbitmq" or "rabbit" in lowered:
                runtime.setdefault("rabbitmq", {"host": parsed.get("host")} if parsed.get("host") else {})
                return
            if kind == "cosmosdb":
                runtime.setdefault("cosmosdb", {"host": parsed.get("host")} if parsed.get("host") else {})
                return
            if parsed.get("host") and not kind:
                add_datasource({"type": "sql", "host": parsed.get("host"), "port": parsed.get("port"), "database": parsed.get("database"), "name": key.split(":")[-1]}, profile)
                return
        if re.search(r"redis", lowered) and not runtime.get("redis"):
            host = parsed.get("host") or re.sub(r"[:,].*$", "", value)
            if host and re.fullmatch(r"[\w.\-]+", host):
                runtime["redis"] = {"host": host}
        elif re.search(r"(kafka).*(bootstrap|server|broker)|bootstrapservers", lowered):
            runtime.setdefault("kafka", {"bootstrap_servers": value})
        elif re.search(r"rabbit(mq)?:(host|hostname|uri|url|connection)|amqp", lowered):
            host = parsed.get("host") or value
            runtime.setdefault("rabbitmq", {"host": host})
        elif re.search(r"(elastic|opensearch).*(uri|url|node|host)", lowered):
            runtime.setdefault("elasticsearch", {"host": parsed.get("host") or value})
        elif re.search(r"(smtp|mail).*(host|server)", lowered):
            runtime.setdefault("smtp", {"host": value})
        elif re.search(r"(servicebus|eventhub)", lowered) and "connection" in lowered:
            runtime.setdefault("service_bus", {"name": key})
        elif re.search(r"(authority|issuer)$", lowered) and value.startswith("http"):
            runtime.setdefault("identity_provider", value)
        elif re.search(r"mongo.*(uri|url|connection|host)", lowered):
            runtime.setdefault("mongodb", {"host": parsed.get("host") or value, **({"database": parsed["database"]} if parsed.get("database") else {})})
        elif re.search(r"(s3|minio|blob|storage).*(endpoint|url|service)", lowered):
            runtime.setdefault("storage", {"endpoint": value})

    for key, value in values.items():
        classify(key, value, "default")
    for profile, profile_values in config.profiles.items():
        for key, value in profile_values.items():
            classify(key, value, profile)

    # a DbContext whose connection string is not in appsettings still tells the database type
    if datasources:
        runtime["datasources"] = datasources
    name = app_name or project.assembly_name or project.name
    if name:
        runtime["application_name"] = name
    port = None
    urls = config.launch.get("applicationUrl") or values.get("Kestrel:Endpoints:Http:Url") or values.get("urls") or config.launch.get("ASPNETCORE_URLS")
    if urls:
        match = re.search(r":(\d{2,5})\b", urls.split(";")[0])
        port = int(match.group(1)) if match else None
    if port:
        runtime["server_port"] = port
    authority = config.get("Authentication:Authority", "Jwt:Authority", "Authority", "OpenIdConnect:Authority", "IdentityServer:Authority", "Auth0:Domain", "Keycloak:Authority")
    if authority and "identity_provider" not in runtime:
        runtime["identity_provider"] = authority
    instance, tenant = config.get("AzureAd:Instance"), config.get("AzureAd:TenantId")
    if instance and "identity_provider" not in runtime:
        runtime["identity_provider"] = f"{instance.rstrip('/')}/{tenant or ''}".rstrip("/")
    return runtime


def masked(values: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for k, v in sorted(values.items()):
        if is_secret_name(k) or re.search(r"password\s*=|pwd\s*=|accountkey\s*=|sharedaccesskey\s*=", v, re.I):
            out[k] = re.sub(r"(?i)(password|pwd|accountkey|sharedaccesskey)\s*=\s*[^;]+", r"\1=***", v) if "=" in v and ";" in v else "***"
        else:
            out[k] = v
    return out


def public_dependencies(deps: List[dict]) -> List[dict]:
    return [d for d in deps if d.get("category") not in IGNORED_CATEGORIES or d.get("scope") != "test"]
