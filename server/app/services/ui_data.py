"""
Read-only view models for the UI: API lists with method/path, repository
summaries and recent changes. Everything comes from the docs folder, the
version files and the stored architecture model.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.services import architecture_view, docs_store
from app.services.architecture_store import has_architecture, load_architecture
from mergeclear import contracts


def _timestamp(value) -> Optional[float]:
    """Epoch seconds of a stored timestamp (SQLite returns them without a timezone: they are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def relative_time(timestamp: Optional[float]) -> str:
    if not timestamp:
        return ""
    seconds = max(0, datetime.now(timezone.utc).timestamp() - timestamp)
    for unit, size in (("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def absolute_time(timestamp: Optional[float]) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _group_of(route: dict, api: str) -> str:
    controller = (route or {}).get("controller") or ""
    if controller:
        return controller.rsplit(".", 1)[-1]
    handler = (route or {}).get("handler") or ""
    if "." in handler:
        return handler.split(".", 1)[0]
    return "APIs"


def api_items(repo: str) -> List[dict]:
    """Every documented API of a repo with title, method, path and freshness."""
    titles = docs_store.get_titles(repo)
    items = []

    for entry in docs_store.latest_versions(repo):
        api = entry["api"]
        route = entry.get("route") or {}
        updated = _timestamp(entry.get("created_at"))
        items.append({
            "name": api,
            "title": titles.get(api, api),
            "method": route.get("method"),
            "path": route.get("path"),
            "context_path": route.get("context_path"),
            "handler": route.get("handler"),
            "module": route.get("module"),
            "group": _group_of(route, api),
            "latest_version": entry["version"],
            "versions": entry["versions"],
            "commit": entry.get("commit_hash"),
            "updated": updated,
            "updated_label": relative_time(updated),
            "updated_title": absolute_time(updated),
        })

    return sorted(items, key=lambda i: (i["group"].lower(), i["title"].lower()))


def current_route(repo: str, api: str, version: Optional[int] = None) -> dict:
    versions = docs_store.list_versions(repo, api)
    if not versions:
        return {}
    return docs_store.get_route(repo, api, version or versions[-1]) or {}


def version_rows(repo: str, api: str) -> List[dict]:
    """Versions newest first with commit, title and time."""
    entries = docs_store.version_entries(repo, api)
    rows = []

    for i, entry in enumerate(entries):
        updated = _timestamp(entry.get("created_at"))
        route = entry.get("route") or {}
        rows.append({
            "version": entry["version"],
            "previous": entries[i - 1]["version"] if i > 0 else None,
            "commit": entry.get("commit_hash"),
            "title": entry.get("title"),
            "change_reasons": route.get("change_reasons") or [],
            "breaking_changes": route.get("breaking_changes") or [],
            "updated_label": relative_time(updated),
            "updated_title": absolute_time(updated),
            "is_latest": i == len(entries) - 1,
        })

    return list(reversed(rows))


def repo_summary(repo: str, items: Optional[List[dict]] = None) -> dict:
    items = items if items is not None else api_items(repo)
    updated = max((i["updated"] for i in items if i["updated"]), default=None)
    summary = {
        "name": repo,
        "api_count": len(items),
        "version_count": sum(i["versions"] for i in items),
        "changed_apis": sum(1 for i in items if i["versions"] > 1),
        "updated": updated,
        "updated_label": relative_time(updated),
        "updated_title": absolute_time(updated),
        "has_architecture": has_architecture(repo),
        "architecture": None,
    }

    if summary["has_architecture"]:
        document = load_architecture(repo) or {}
        model = architecture_view.application_model(document) or {}
        s = model.get("summary") or {}
        technologies = s.get("technologies") or {}
        summary["architecture"] = {
            "commit": document.get("commit"),
            "style": s.get("architecture_style"),
            "modules": s.get("modules"),
            "endpoints": s.get("endpoints"),
            "entities": s.get("entities"),
            "technologies": [t for cat in ("web", "database", "cache", "messaging", "http-client", "security", "service-discovery")
                             for t in technologies.get(cat, [])][:8],
        }

    return summary


def recent_changes(repos: List[str], limit: int = 8, items_by_repo: Optional[Dict[str, List[dict]]] = None) -> List[dict]:
    """Latest documented versions across repositories, newest first."""
    changes = []
    for repo in repos:
        items = (items_by_repo or {}).get(repo) or api_items(repo)
        for item in items:
            changes.append({
                "repo": repo,
                "api": item["name"],
                "title": item["title"],
                "method": item["method"],
                "version": item["latest_version"],
                "previous": item["latest_version"] - 1 if item["latest_version"] > 1 else None,
                "updated": item["updated"],
                "updated_label": item["updated_label"],
                "updated_title": item["updated_title"],
            })
    changes.sort(key=lambda c: c["updated"] or 0, reverse=True)
    return changes[:limit]


# ---------- impact page ----------

_CHANGE_AREAS = (
    ("ENDPOINT", "Endpoint"), ("REQUEST", "Request body"), ("RESPONSE", "Response"), ("PARAM", "Parameters"),
    ("VALIDATION", "Validation"), ("SECURITY", "Security"), ("ERROR_STATUS", "Status codes"), ("SUCCESS_STATUS", "Status codes"),
)
_SEVERITY_ORDER = {"breaking": 0, "minor": 1, "additive": 2}
# same verdicts as `mergeclear check`
_VERDICT_TEXT = {
    contracts.HOLD: ("Hold before merging", "danger", "Breaking changes: existing clients fail until they are updated."),
    contracts.REVIEW: ("Review before merging", "warn", "Changes clients may notice, such as new error codes or new downstream systems."),
    contracts.CLEAR: ("Clear to merge", "ok", "No change existing clients could notice."),
}
# documentation-only analysis (no scanner data) only has an impact level
_LEVEL_VERDICT = {"high": contracts.HOLD, "medium": contracts.HOLD, "low": contracts.REVIEW, "none": contracts.CLEAR}
_GROUP_LABELS = {"databases": "Databases", "caches": "Caches", "messaging": "Messaging",
                 "external_apis": "External services", "other": "Other"}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def change_area(change_type: str) -> str:
    for prefix, label in _CHANGE_AREAS:
        if (change_type or "").startswith(prefix):
            return label
    return "Contract"


def impact_view(impact: dict) -> dict:
    """Plain-language pieces of an impact analysis for the dependencies page."""
    bc = impact.get("breaking_changes") or {}
    changes = sorted(bc.get("changes") or [], key=lambda c: _SEVERITY_ORDER.get(c.get("severity"), 3))
    level = bc.get("impact_level") or "none"
    if impact.get("source") == "scanner":
        verdict = contracts.verdict(changes, impact.get("downstream_changes"))
    else:
        verdict = _LEVEL_VERDICT.get(level, contracts.CLEAR)
    title, tone, explanation = _VERDICT_TEXT[verdict]
    if verdict == contracts.CLEAR and not changes and not impact.get("downstream_changes"):
        explanation = "Nothing in the API contract or its dependencies changed."
    if verdict == contracts.HOLD and impact.get("source") == "scanner" and not (impact.get("consumers") or impact.get("shared_resources")):
        explanation += " No callers were found in the scanned code; external clients may still exist."

    affected = []
    for c in impact.get("consumers") or []:
        affected.append({"name": c.get("caller"), "module": c.get("caller_module"),
                         "how": "Routes requests to this API" if c.get("kind") == "gateway_route" else "Calls this API",
                         "detail": c.get("detail"), "kind": "caller"})
    sharing: Dict[str, List[str]] = {}
    for sr in impact.get("shared_resources") or []:
        for other in sr.get("also_used_by") or []:
            sharing.setdefault(other, []).append(f"{sr.get('kind')} {sr.get('name')}")
    for other, resources in sharing.items():
        name, _, handler = other.partition(" (")
        affected.append({"name": name, "module": handler.rstrip(")"), "how": "Shares " + ", ".join(resources),
                         "detail": "", "kind": "shares"})

    breaking = sum(1 for c in changes if c.get("severity") == "breaking")
    facts = []
    if changes:
        facts.append(_plural(breaking, "breaking change") + (f" of {len(changes)}" if breaking != len(changes) else ""))
    callers = sum(1 for a in affected if a["kind"] == "caller")
    if callers:
        facts.append(_plural(callers, "caller") + " in the scanned code")
    sharers = len(sharing)
    if sharers:
        facts.append(_plural(sharers, "endpoint") + " sharing data")
    delta = impact.get("downstream_changes") or {}
    new_deps = sum(len(d.get("added", [])) for d in delta.values())
    removed_deps = sum(len(d.get("removed", [])) for d in delta.values())
    if new_deps or removed_deps:
        facts.append(", ".join(x for x in (f"{new_deps} new" if new_deps else "", f"{removed_deps} removed" if removed_deps else "") if x)
                     + " dependenc" + ("y" if new_deps + removed_deps == 1 else "ies"))

    dependencies = [{"group": g, "label": _GROUP_LABELS.get(g, g), "entries": items}
                    for g, items in (impact.get("dependencies") or {}).items()]
    if not dependencies:
        # docs-only / older data: plain strings
        dependencies = [{"group": g, "label": _GROUP_LABELS.get(g, g),
                         "entries": [{"operation": "", "name": s, "technology": "", "status": "same"} for s in items]}
                        for g, items in (impact.get("downstream") or {}).items() if items]

    return {
        "level": level, "verdict": verdict, "title": title, "tone": tone, "explanation": explanation, "facts": facts,
        "changes": [{**c, "area": change_area(c.get("type"))} for c in changes],
        "breaking": breaking,
        "affected": affected,
        "dependencies": dependencies,
        "dependency_count": sum(len([i for i in d["entries"] if i["status"] != "removed"]) for d in dependencies),
    }


# ---------- background jobs ----------

def _job_view(job: dict) -> dict:
    from datetime import datetime as _dt

    def ts(value):
        return _dt.fromisoformat(value).timestamp() if value else None

    total, done = job.get("progress_total") or 0, job.get("progress_done") or 0
    if job["status"] == "running":
        label = "Documenting"
    elif job["status"] == "queued":
        label = "Retrying soon" if job.get("attempts") else "Queued"
    elif job["status"] == "failed":
        label = "Failed"
    else:
        label = "Done"
    error = (job.get("error") or "").strip().splitlines()
    return {
        **job,
        "label": label,
        "commit_short": (job.get("commit") or "")[:12],
        "percent": int(done * 100 / total) if total else 0,
        "error_line": error[0] if error else "",
        "when": relative_time(ts(job.get("finished_at") or job.get("started_at") or job.get("created_at"))),
    }


def jobs_panel(repo: Optional[str] = None) -> dict:
    """Queued/running jobs and failures of the last day, for the dashboard."""
    from app import jobs

    active = [_job_view(j) for j in reversed(jobs.list_jobs(repo=repo, active=True, limit=20))]
    day_ago = datetime.now(timezone.utc).timestamp() - 86400
    failed = [_job_view(j) for j in jobs.list_jobs(repo=repo, status="failed", limit=5)
              if j.get("finished_at") and datetime.fromisoformat(j["finished_at"]).timestamp() > day_ago]
    return {"active": active, "failed": failed, "repo": repo}
