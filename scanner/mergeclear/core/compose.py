"""docker-compose files and connection URLs, shared by the framework plugins."""
import os
import re
from typing import List, Optional
from urllib.parse import urlparse

from .files import SKIP_DIRS

URL_SCHEMES = {
    "postgresql": "postgresql", "postgres": "postgresql", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite", "oracle": "oracle",
    "mssql": "sqlserver", "sqlserver": "sqlserver", "mongodb": "mongodb", "redis": "redis", "rediss": "redis", "amqp": "rabbitmq",
    "amqps": "rabbitmq", "kafka": "kafka", "nats": "nats", "elasticsearch": "elasticsearch", "memcached": "memcached",
    "clickhouse": "clickhouse", "cockroachdb": "postgresql", "snowflake": "snowflake", "bigquery": "bigquery", "sqs": "sqs",
}


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


COMPOSE_FILE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$")
SERVICE_IMAGES = [(re.compile(p, re.I), kind, tech) for p, kind, tech in [
    (r"(^|/)postgres|timescale|postgis|supabase/postgres|cockroach", "datastore", "postgresql"),
    (r"(^|/)(mysql|mariadb)", "datastore", "mysql"),
    (r"(^|/)mongo", "datastore", "mongodb"),
    (r"(^|/)(redis|valkey|keydb|dragonfly)", "cache", "redis"),
    (r"(^|/)memcached", "cache", "memcached"),
    (r"rabbitmq", "broker", "rabbitmq"),
    (r"kafka|redpanda", "broker", "kafka"),
    (r"(^|/)nats", "broker", "nats"),
    (r"elasticsearch|opensearch", "search", "elasticsearch"),
    (r"(^|/)(minio|localstack)", "storage", "s3"),
    (r"mailhog|mailpit|maildev|smtp", "email", "smtp"),
    (r"keycloak", "infrastructure", "oauth2"),
]]



def load_compose(repo_path: str) -> List[dict]:
    """Services of the repository's docker-compose files."""
    try:
        import yaml
    except ImportError:
        return []
    services = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if os.path.relpath(root, repo_path).count(os.sep) >= 3:
            dirs[:] = []
        for name in files:
            if not COMPOSE_FILE.match(name):
                continue
            rel_dir = os.path.relpath(root, repo_path).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            try:
                document = yaml.safe_load(_read(os.path.join(root, name))) or {}
            except yaml.YAMLError:
                continue
            for service_name, spec in (document.get("services") or {}).items():
                if not isinstance(spec, dict):
                    continue
                build = spec.get("build")
                context = build.get("context") if isinstance(build, dict) else build if isinstance(build, str) else None
                environment = spec.get("environment") or {}
                if isinstance(environment, list):
                    environment = {k: v for k, _, v in (str(e).partition("=") for e in environment)}
                depends = spec.get("depends_on") or []
                if isinstance(depends, dict):
                    depends = list(depends.keys())
                env_files = spec.get("env_file") or []
                services.append({
                    "name": service_name, "image": spec.get("image"), "compose_file": (rel_dir + "/" + name).lstrip("/"),
                    "build_context": os.path.normpath(os.path.join(rel_dir, context)).replace(os.sep, "/") if context else None,
                    "environment": {str(k): "" if v is None else str(v) for k, v in environment.items()},
                    "depends_on": [str(d) for d in depends], "ports": [str(p) for p in spec.get("ports") or []],
                    "env_file": [env_files] if isinstance(env_files, str) else [str(e) for e in env_files if isinstance(e, (str,))],
                })
    for s in services:
        if s["build_context"] == ".":
            s["build_context"] = ""
        image = s.get("image") or ""
        for pattern, kind, tech in SERVICE_IMAGES:
            if image and pattern.search(image) or (not image and not s["build_context"] and pattern.search(s["name"])):
                s["infra"] = {"kind": kind, "technology": tech}
                break
    return services


def parse_url(value: str) -> Optional[dict]:
    if not value or "://" not in value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    scheme = parsed.scheme.split("+")[0].lower()
    return {"scheme": scheme, "host": parsed.hostname, "port": parsed.port, "database": parsed.path.lstrip("/") or None,
            "type": URL_SCHEMES.get(scheme)}
