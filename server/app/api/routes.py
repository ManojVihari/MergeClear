import difflib
import html
import re
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from app.config import LLM_ENABLED, TEMPLATES_DIR
from app.models.schema import AnalyzeRequest
from app import auth, jobs, limits
from app.api import auth_routes
from app.services import docs_store, ui_data
from app.services.html_safety import clean_html
from app.services.architecture_view import application_model, layered_component_view, layered_system_view
from app.services.architecture_store import has_architecture, load_architecture
from app.services.qa_plan_service import QAPlanService
from app.services.llm_service import summarize_changes, search_apis_rag, answer_question_based_on_docs
from app.services.qa_plan_generator import generate_full_qa_plan
from app.services.dependency_analyzer import (
    analyze_endpoint_impact, build_architecture_graph, build_dependency_graph, get_impact_analysis
)
from app.services.test_templates import (
    get_predefined_templates, list_templates, create_template,
    get_template_recommendations
)
import markdown
import markupsafe
from bs4 import BeautifulSoup

router = APIRouter()
qa_plan_service = QAPlanService()
templates = Jinja2Templates(directory=TEMPLATES_DIR)
auth_routes.templates = templates


def _ago(moment):
    """Stored timestamp -> "3 minutes ago" ("" for None)."""
    if not moment:
        return ""
    return ui_data.relative_time(ui_data._timestamp(moment))


templates.env.filters["ago"] = _ago


def _inline_code(text):
    """Escape text and render `backticked` parts as <code>."""
    escaped = str(markupsafe.escape(text or ""))
    return markupsafe.Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped))


templates.env.filters["inline_code"] = _inline_code
# docs come from repository code and LLM output: never `| safe`, always `| clean`
templates.env.filters["clean"] = clean_html

DOC_MARKDOWN_EXTENSIONS = [
    "tables",
    "fenced_code",
    "toc",
    "codehilite",
    "attr_list",
    "md_in_html"
]


def _nav_context(repo, api=None, tab=None, version=None):
    """Data for the shared layout: API sidebar, repo switcher, API header and tabs."""
    context = {
        "apis": ui_data.api_items(repo),
        "repos": docs_store.list_repos(),
        "has_architecture": has_architecture(repo),
        "tab": tab,
    }
    if api is not None:
        versions = docs_store.list_versions(repo, api)
        context.update({
            "api_title": docs_store.get_title(repo, api),
            "latest_version": versions[-1] if versions else None,
            "current_route": ui_data.current_route(repo, api, version),
        })
    return context


def _not_found(request, message, back=None, status=404, title="Not found"):
    return templates.TemplateResponse(
        request, "message.html",
        {"title": title, "message": message, "back": back or "/ui", "nav": None},
        status_code=status,
    )


def _impact(repo, api, v1, v2, v1_content, v2_content):
    """Scanner-based impact analysis when structured data exists, docs-based otherwise."""
    impact = analyze_endpoint_impact(
        repo, api, v1, v2,
        docs_store.get_route(repo, api, v1) if v1 else None,
        docs_store.get_route(repo, api, v2),
        load_architecture(repo),
    )
    if impact is None:
        impact = get_impact_analysis(repo, api, v1_content, v2_content)
        impact["source"] = "docs"
    return impact


def _default_versions(versions, v1, v2):
    """v2 defaults to the latest version, v1 to the one before it."""
    if v2 is None:
        v2 = versions[-1]

    if v1 is None and len(versions) > 1:
        v1 = versions[-2]

    return v1, v2


def _deny(request: Request, admin: bool = False, csrf: bool = False) -> Optional[JSONResponse]:
    """403 when the signed-in user (or API key) may not do this; None when allowed."""
    user = request.state.user
    if admin and not (user and user.is_admin):
        return JSONResponse({"error": "this needs the admin role"}, status_code=403)
    if csrf and not auth.csrf_ok(user, request.headers.get("x-csrf-token")):
        return JSONResponse({"error": "missing or invalid CSRF token: reload the page"}, status_code=403)
    return None


@router.post("/analyze", status_code=202)
async def analyze(request: AnalyzeRequest, http_request: Request):
    """
    Receive a scan report from `mergeclear scan --push` / `mergeclear push` and
    queue it for documentation. Answers at once with the job to follow.
    """
    denied = _deny(http_request, admin=True, csrf=True)      # API keys need no CSRF token
    if denied:
        return denied
    wait = limits.uploads.check(http_request.state.user.id)
    if wait:
        return JSONResponse({"error": f"too many uploads from this account (limit {limits.uploads.per_minute} a minute): "
                                      f"retry in {wait} s"}, status_code=429, headers={"Retry-After": str(wait)})

    project = _upload_project(request, http_request.state.user)
    if not project:
        return JSONResponse({"error": "no project name: pass --project <name> (or set MERGECLEAR_PROJECT) in the "
                                      "pipeline, or use an API key created with a default project"}, status_code=400)
    if not docs_store.is_safe_name(project):
        return JSONResponse({"error": f"invalid project name {project!r}: no / or \\, at most 200 characters"}, status_code=400)

    payload = {**request.model_dump(mode="json"), "repository": project, "project": project}
    job, created = await run_in_threadpool(jobs.enqueue, "analyze", payload, project, request.commit)
    return JSONResponse({
        "status": "queued" if created else "already_queued",
        "project": project,
        "job_id": job["id"],
        "job_url": f"/api/jobs/{job['id']}",
    }, status_code=202)


def _upload_project(request: AnalyzeRequest, user) -> Optional[str]:
    """Explicit project, else the API key's default project; older scanners name it `repository`."""
    key_project = getattr(user, "key_project", None)
    if "project" in request.model_fields_set:          # scanner 0.3+: null means "use the key's project"
        return (request.project or "").strip() or key_project
    return (request.repository or "").strip() or key_project


@router.get("/api/jobs")
def list_jobs(repo: str = None, active: bool = None, status: str = None, limit: int = 20):
    """Recent background jobs, newest first (active=true: queued and running only)."""
    return {"jobs": jobs.list_jobs(repo=repo, active=active, status=status, limit=limit)}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": f"no job {job_id}"}, status_code=404)
    return job


@router.get("/api/architecture")
def get_architecture(repo: str, commit: str = None):
    """Application model (modules, components, data, integrations, graph) from the latest scan."""
    document = load_architecture(repo, commit)

    if document is None:
        return JSONResponse({"error": f"No architecture stored for '{repo}'"}, status_code=404)

    return JSONResponse(document)


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """Product landing page."""
    repos = docs_store.list_repos()
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "nav": "landing",
            "repo_count": len(repos),
            "api_count": sum(len(docs_store.list_apis(repo)) for repo in repos),
        }
    )


@router.get("/ui", response_class=HTMLResponse)
def ui_home(request: Request):
    """Dashboard: search, repositories and recent changes."""
    repos = docs_store.list_repos()
    items = {repo: ui_data.api_items(repo) for repo in repos}
    summaries = [ui_data.repo_summary(repo, items[repo]) for repo in repos]
    summaries = [s for s in summaries if s["api_count"]]
    summaries.sort(key=lambda s: s["updated"] or 0, reverse=True)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "nav": "dashboard",
            "repos": summaries,
            "totals": {
                "repos": len(summaries),
                "apis": sum(s["api_count"] for s in summaries),
                "versions": sum(s["version_count"] for s in summaries),
                "architectures": sum(1 for s in summaries if s["has_architecture"]),
            },
            "recent": ui_data.recent_changes([s["name"] for s in summaries], 8, items),
            "jobs": ui_data.jobs_panel(),
        }
    )


@router.get("/ui/all", response_class=HTMLResponse)
def ui_all_apis(request: Request, q: str = "", repo: str = ""):
    """Every API of every repository, filterable."""
    repos = docs_store.list_repos()
    items = []
    for r in repos:
        for item in ui_data.api_items(r):
            items.append({**item, "repo": r})
    items.sort(key=lambda i: (i["repo"].lower(), i["group"].lower(), i["title"].lower()))

    return templates.TemplateResponse(
        request,
        "all_apis.html",
        {"nav": "apis", "items": items, "repos": repos, "q": q, "selected_repo": repo}
    )


def render_md(md_text):
    return markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code"]
    )

def html_to_blocks(html):
    soup = BeautifulSoup(html, "html.parser")

    blocks = []
    for el in soup.find_all(["h1", "h2", "h3", "p", "li", "pre", "table"]):
        blocks.append(str(el))  # keep HTML intact

    return blocks

def generate_diff(content1, content2):
    diff = list(difflib.ndiff(content1, content2))

    rows = []
    left_line = 1
    right_line = 1

    buffer_removed = []

    i = 0
    while i < len(diff):
        tag = diff[i][:2]
        text = diff[i][2:]

        # SAME
        if tag == "  ":
            rows.append(("same", left_line, text, right_line, text))
            left_line += 1
            right_line += 1
            i += 1

        # REMOVED → store in buffer
        elif tag == "- ":
            buffer_removed.append((left_line, text))
            left_line += 1
            i += 1

        # ADDED → match with buffer
        elif tag == "+ ":
            if buffer_removed:
                lnum, old_text = buffer_removed.pop(0)

                # 🔥 word highlight
          # detect table diff
                if old_text.strip().startswith("<table") and text.strip().startswith("<table"):
                    old_h, new_h = highlight_table_diff(old_text, text)

                # normal text diff
                else:
                    old_h, new_h = highlight_words(old_text, text)

                rows.append(("change", lnum, old_h, right_line, new_h))
            else:
                rows.append(("add", "", "", right_line, text))

            right_line += 1
            i += 1

        else:
            i += 1

    # leftover removed lines
    for lnum, old_text in buffer_removed:
        rows.append(("remove", lnum, old_text, "", ""))

    return rows


def group_diff_rows(rows, context=2):
    """Split diff rows into sections; long unchanged runs become collapsible."""
    sections, run = [], []

    def flush(last):
        if not run:
            return
        first = not sections
        head = [] if first else run[:context]
        tail = [] if last else run[-context:]
        hidden = run[len(head):len(run) - len(tail)]
        if len(hidden) <= 2:
            sections.append({"rows": run[:], "hidden": False})
        else:
            if head:
                sections.append({"rows": head, "hidden": False})
            sections.append({"rows": hidden, "hidden": True})
            if tail:
                sections.append({"rows": tail, "hidden": False})
        run.clear()

    for row in rows:
        if row[0] == "same":
            run.append(row)
            continue
        flush(last=False)
        sections.append({"rows": [row], "hidden": False})
    flush(last=True)
    return sections

def highlight_table_diff(old_html, new_html):
    old_soup = BeautifulSoup(old_html, "html.parser")
    new_soup = BeautifulSoup(new_html, "html.parser")

    old_cells = old_soup.find_all("td")
    new_cells = new_soup.find_all("td")

    for i in range(min(len(old_cells), len(new_cells))):
        old_text = old_cells[i].get_text(strip=True)
        new_text = new_cells[i].get_text(strip=True)

        if old_text != new_text:
            old_cells[i].string = ""
            new_cells[i].string = ""

            old_cells[i].append(
                BeautifulSoup(
                    f"<span class='bg-red-200 px-1 rounded'>{html.escape(old_text)}</span>",
                    "html.parser"
                )
            )

            new_cells[i].append(
                BeautifulSoup(
                    f"<span class='bg-green-200 px-1 rounded'>{html.escape(new_text)}</span>",
                    "html.parser"
                )
            )

    return str(old_soup), str(new_soup)

def highlight_words(old, new):
    result_old = []
    result_new = []

    diff = difflib.ndiff(old.split(), new.split())

    for d in diff:
        if d.startswith("- "):
            result_old.append(f"<span class='bg-red-200'>{d[2:]}</span>")
        elif d.startswith("+ "):
            result_new.append(f"<span class='bg-green-200'>{d[2:]}</span>")
        elif d.startswith("  "):
            word = d[2:]
            result_old.append(word)
            result_new.append(word)

    return " ".join(result_old), " ".join(result_new)


@router.get("/ui/{repo}/{api}/diff", response_class=HTMLResponse)
def api_diff(request: Request, repo: str, api: str, v1: int, v2: int):

    md1 = docs_store.read_version(repo, api, v1)
    md2 = docs_store.read_version(repo, api, v2)

    if md1 is None or md2 is None:
        return _not_found(request, f"Version v{v1} or v{v2} of this API does not exist.", f"/ui/{repo}/{api}/history")

    # Generate LLM summary of changes
    summary = summarize_changes(md1, md2, api)

    content1 = html_to_blocks(render_md(md1))
    content2 = html_to_blocks(render_md(md2))

    diff_rows = generate_diff(content1, content2)

    return templates.TemplateResponse(
        request,
        "diff.html",
        {
            "repo": repo,
            "api": api,
            "v1": v1,
            "v2": v2,
            "rows": diff_rows,
            "sections": group_diff_rows(diff_rows),
            "summary": summary,
            **_nav_context(repo, api, "history", v2)
        }
    )


def _render_doc_page(request, repo, api, version, md_content):
    md = markdown.Markdown(extensions=DOC_MARKDOWN_EXTENSIONS)

    html_content = md.convert(md_content)
    toc = getattr(md, 'toc', '')

    return templates.TemplateResponse(
        request,
        "view.html",
        {
            "repo": repo,
            "api": api,
            "version": int(version) if str(version).isdigit() else version,
            "versions": docs_store.list_versions(repo, api),
            "content": html_content,
            "toc": toc,
            **_nav_context(repo, api, "docs", int(version) if str(version).isdigit() else None)
        }
    )


@router.get("/ui/{repo}/architecture", response_class=HTMLResponse)
def architecture_page(request: Request, repo: str):
    """Application model of a repository: system graph, modules, components, data, messaging."""
    document = load_architecture(repo)
    model = application_model(document)

    if model is None:
        return _not_found(
            request,
            f"No architecture stored for '{repo}'. Run the scanner (2.0+) or scripts/document_local_repo.py on it.",
            f"/ui/{repo}" if docs_store.is_safe_name(repo) and repo in docs_store.list_repos() else "/",
        )

    return templates.TemplateResponse(
        request,
        "architecture.html",
        {
            "repo": repo,
            "commit": document.get("commit"),
            "model": model,
            "system_view": layered_system_view(model),
            "titles": docs_store.get_titles(repo),
            "nav": None,
            **_nav_context(repo)
        }
    )


@router.get("/ui/{repo}/{api}/view/{version}", response_class=HTMLResponse)
def view_doc(request: Request, repo: str, api: str, version: str):

    md_content = docs_store.read_version(repo, api, version)

    if md_content is None:
        return _not_found(request, f"Version v{version} of this API does not exist.", f"/ui/{repo}/{api}/history")

    return _render_doc_page(request, repo, api, version, md_content)


@router.get("/ui/{repo}/{api}", response_class=HTMLResponse)
def view_latest(request: Request, repo: str, api: str):

    if not docs_store.api_exists(repo, api):
        return _not_found(request, f"There is no API '{api}' in '{repo}'.", f"/ui/{repo}" if docs_store.is_safe_name(repo) else "/")

    versions = docs_store.list_versions(repo, api)

    if not versions:
        return _not_found(request, "This API has no documented versions yet.", f"/ui/{repo}")

    latest_version = versions[-1]
    md_content = docs_store.read_version(repo, api, latest_version)

    return _render_doc_page(request, repo, api, latest_version, md_content)


@router.get("/ui/{repo}/{api}/history", response_class=HTMLResponse)
def api_versions(request: Request, repo: str, api: str):

    if not docs_store.api_exists(repo, api):
        return _not_found(request, f"There is no API '{api}' in '{repo}'.")

    versions = ui_data.version_rows(repo, api)

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "repo": repo,
            "api": api,
            "versions": versions,
            **_nav_context(repo, api, "history")
        }
    )


@router.get("/ui/search", response_class=HTMLResponse)
def ui_search(request: Request):
    """Display the LLM search page"""
    return templates.TemplateResponse(request, "llm_search.html", {"nav": "search", "q": request.query_params.get("q", "")})


@router.get("/ui/{repo}", response_class=HTMLResponse)
def repo_home(request: Request, repo: str):

    if not docs_store.is_safe_name(repo):
        return _not_found(request, f"There is no repository '{repo}'.")

    items = ui_data.api_items(repo)
    panel = ui_data.jobs_panel(repo)

    # a first upload that is still being documented shows its progress instead of a 404
    if not items and not panel["active"] and not panel["failed"]:
        return _not_found(request, f"There is no repository '{repo}'.")

    groups = {}
    for item in items:
        groups.setdefault(item["group"], []).append(item)

    return templates.TemplateResponse(
        request,
        "repo.html",
        {
            "nav": None,
            "repo": repo,
            "summary": ui_data.repo_summary(repo, items),
            "groups": groups,
            "items": items,
            "recent": ui_data.recent_changes([repo], 6, {repo: items}),
            "jobs": panel,
        }
    )


@router.post("/api/search")
async def search_apis(request: Request):
    """
    Search APIs and answer user questions based on available documentation.
    Acts as a conversational KT provider/assistant.
    
    Request body:
    {
        "query": "user string query about APIs"
    }
    
    Returns:
    {
        "success": true/false,
        "answer": "Conversational answer based on docs or error message",
        "results": [{"repo": "...", "api": "...", "version": 1}],
        "matchedCount": number of matching APIs
    }
    """
    try:
        denied = _deny(request, csrf=True)
        if denied:
            return denied
        body = await request.json()
        query = body.get("query", "").strip()
        
        if not query:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Query cannot be empty",
                    "answer": "Please enter a question or search term.",
                    "results": [],
                    "matchedCount": 0
                },
                status_code=400
            )
        
        # LLM calls block, keep them off the event loop
        matching_apis = await run_in_threadpool(search_apis_rag, query)
        
        # Generate a conversational answer based on actual documentation
        answer_result = await run_in_threadpool(answer_question_based_on_docs, query, matching_apis)
        
        return JSONResponse({
            "success": answer_result["success"],
            "answer": answer_result["answer"] or answer_result["message"],
            "message": answer_result["message"],
            "results": matching_apis,
            "matchedCount": len(matching_apis)
        })
        
    except Exception as e:
        print(f"Error in search_apis: {e}")
        return JSONResponse(
            {
                "success": False,
                "error": str(e),
                "answer": "Something went wrong on our end. Please try again later.",
                "results": [],
                "matchedCount": 0
            },
            status_code=500
        )


@router.get("/ui/{repo}/{api}/qa-plan", response_class=HTMLResponse)
def qa_plan_view(request: Request, repo: str, api: str, v1: int = None, v2: int = None,
                 force: bool = False, generate: bool = False):
    """
    Generate and display a comprehensive QA plan for an API.
    QA plans are cached to avoid regenerating on every page load.
    If v1 and v2 are provided, includes regression testing.
    
    Args:
        force: If True, bypass cache and regenerate QA plan
    """
    if not docs_store.api_exists(repo, api):
        return _not_found(request, f"There is no API '{api}' in '{repo}'.")
    if force and not request.state.user.is_admin:
        return _not_found(request, "Only admins can regenerate QA plans.", f"/ui/{repo}/{api}/qa-plan",
                          status=403, title="Not allowed")

    versions = docs_store.list_versions(repo, api)

    if not versions:
        return _not_found(request, "This API has no documented versions yet.")

    v1, v2 = _default_versions(versions, v1, v2)

    # Check if QA plan is cached (unless force=True to bypass cache)
    cached_plan = None if force else qa_plan_service.get_qa_plan(repo, api, v2)
    
    if v2 not in versions:
        return _not_found(request, f"Version v{v2} of this API does not exist.", f"/ui/{repo}/{api}/history")

    if not cached_plan and not generate:
        # Generating can take a while with an LLM: answer at once with a progress
        # page that builds the plan through /api/qa-plan and then reloads.
        return templates.TemplateResponse(
            request,
            "qa_plan_loading.html",
            {
                "repo": repo,
                "api": api,
                "v1": v1,
                "v2": v2,
                "force": force,
                "llm_enabled": LLM_ENABLED,
                **_nav_context(repo, api, "qa", v2)
            }
        )

    if cached_plan:
        qa_plan = cached_plan.get("plan", {})
        plan_generated_at = cached_plan.get("generated_at")
    else:
        doc_v2 = docs_store.read_version(repo, api, v2)

        # v1 doc enables regression testing
        doc_v1 = docs_store.read_version(repo, api, v1) if v1 else None

        qa_plan = generate_full_qa_plan(api, doc_v2, doc_v1)
        qa_plan_service.save_qa_plan(repo, api, v2, qa_plan)

        plan_generated_at = datetime.utcnow().isoformat()

    return templates.TemplateResponse(
        request,
        "qa_plan.html",
        {
            "repo": repo,
            "api": api,
            "api_title": docs_store.get_title(repo, api),
            "v1": v1,
            "v2": v2,
            "qa_plan": qa_plan,
            "plan_generated_at": plan_generated_at,
            "is_cached": cached_plan is not None,
            **_nav_context(repo, api, "qa", v2)
        }
    )


@router.get("/api/qa-plan")
def generate_qa_plan_api(request: Request, repo: str, api: str, v1: int = None, v2: int = None, force: bool = False):
    """
    API endpoint to generate QA plan as JSON.
    Useful for integrating with CI/CD pipelines.
    
    Args:
        force: If True, bypass cache and regenerate QA plan
    """
    if force:
        denied = _deny(request, admin=True)
        if denied:
            return denied
    if not docs_store.api_exists(repo, api):
        return JSONResponse(
            {"error": f"API '{api}' not found in repository '{repo}'"},
            status_code=404
        )

    versions = docs_store.list_versions(repo, api)

    if not versions:
        return JSONResponse(
            {"error": "No versions found for this API"},
            status_code=404
        )

    v1, v2 = _default_versions(versions, v1, v2)

    if v2 not in versions:
        return JSONResponse(
            {"error": f"Version v{v2} not found"},
            status_code=404
        )

    # Check cache (unless force=True)
    cached_plan = None if force else qa_plan_service.get_qa_plan(repo, api, v2)
    
    if cached_plan:
        qa_plan = cached_plan.get("plan", {})
    else:
        doc_v2 = docs_store.read_version(repo, api, v2)
        doc_v1 = docs_store.read_version(repo, api, v1) if v1 else None

        qa_plan = generate_full_qa_plan(api, doc_v2, doc_v1)
        qa_plan_service.save_qa_plan(repo, api, v2, qa_plan)
    
    return JSONResponse(qa_plan)


# ============================================================================
# DEPENDENCY ANALYSIS ENDPOINTS
# ============================================================================

@router.get("/api/architecture/layered")
def layered_architecture_api(repo: str, module: str = None):
    """Column layout of the system (or of one module's components) for the architecture page."""
    if not docs_store.is_safe_name(repo):
        return JSONResponse({"error": "invalid repository"}, status_code=400)
    model = application_model(load_architecture(repo))
    if model is None:
        return JSONResponse({"error": f"No architecture stored for '{repo}'"}, status_code=404)
    view = layered_component_view(model, module) if module is not None else layered_system_view(model)
    return JSONResponse(view)


@router.get("/api/dependency-graph")
def get_dependency_graph_api(repo: str, view: str = "system", module: str = None):
    """
    Dependency graph of a repository.

    Uses the scanner's application model when one is stored (view=system:
    modules and external systems, view=components: beans, optionally of one
    module); falls back to relationships guessed from the docs.
    """
    if not docs_store.is_safe_name(repo):
        return JSONResponse({"nodes": [], "links": []})

    graph = build_architecture_graph(load_architecture(repo), view, module)
    if graph is not None:
        return JSONResponse({**graph, "repo": repo, "source": "scanner", "view": view})

    graph = build_dependency_graph(repo)
    return JSONResponse({**graph, "source": "docs"})


@router.get("/api/impact-analysis")
def get_impact_analysis_api(repo: str, api: str, v1: int, v2: int):
    """
    Get breaking change impact analysis for an API upgrade.
    
    Query params:
        repo: Repository name
        api: API name
        v1: Version 1
        v2: Version 2
    """
    if not docs_store.api_exists(repo, api):
        return JSONResponse({"error": f"API '{api}' not found in repository '{repo}'"}, status_code=404)

    versions = docs_store.list_versions(repo, api)
    missing = [v for v in (v1, v2) if v not in versions]
    if missing:
        return JSONResponse({"error": f"Version(s) {missing} not found", "versions": versions}, status_code=404)

    v1_content = docs_store.read_version(repo, api, v1) or ""
    v2_content = docs_store.read_version(repo, api, v2) or ""

    return JSONResponse(_impact(repo, api, v1, v2, v1_content, v2_content))


# ============================================================================
# TEST TEMPLATES ENDPOINTS
# ============================================================================

@router.get("/api/templates/predefined")
def get_predefined_templates_api():
    """Get all predefined test templates."""
    return JSONResponse(get_predefined_templates())


@router.get("/api/templates/recommendations")
def get_template_recommendations_api(api: str):
    """Get recommended templates for an API."""
    return JSONResponse(get_template_recommendations(api))


@router.get("/api/templates/list")
def list_templates_api(repo: str, category: str = None):
    """
    List custom templates for a repository.
    
    Query params:
        repo: Repository name
        category: Optional category filter
    """
    return JSONResponse(list_templates(repo, category))


@router.post("/api/templates/create")
def create_template_api(
    request: Request,
    repo: str,
    name: str,
    category: str,
    description: str,
    test_cases: List[str]
):
    """
    Create a new custom test template.
    
    Body params:
        repo: Repository name
        name: Template name
        category: Test category
        description: Template description
        test_cases: List of test case descriptions
    """
    denied = _deny(request, admin=True, csrf=True)
    if denied:
        return denied
    success = create_template(repo, name, category, description, test_cases)
    
    return JSONResponse({
        "success": success,
        "message": f"Template '{name}' created successfully" if success else "Failed to create template"
    })


@router.get("/ui/{repo}/{api}/dependencies", response_class=HTMLResponse)
def dependencies_view(request: Request, repo: str, api: str, v1: int = None, v2: int = None):
    """
    Display dependency analysis and impact graph for API changes.
    """
    if not docs_store.api_exists(repo, api):
        return _not_found(request, f"There is no API '{api}' in '{repo}'.")

    versions = docs_store.list_versions(repo, api)

    if not versions:
        return _not_found(request, "This API has no documented versions yet.")

    v1, v2 = _default_versions(versions, v1, v2)

    v2_content = docs_store.read_version(repo, api, v2)

    if v2_content is None:
        return _not_found(request, f"Version v{v2} of this API does not exist.", f"/ui/{repo}/{api}/history")

    v1_content = (docs_store.read_version(repo, api, v1) if v1 else None) or ""

    impact_data = _impact(repo, api, v1, v2, v1_content, v2_content)

    return templates.TemplateResponse(
        request,
        "dependencies.html",
        {
            "repo": repo,
            "api": api,
            "api_title": docs_store.get_title(repo, api),
            "v1": v1,
            "v2": v2,
            "impact_data": impact_data,
            "impact_view": ui_data.impact_view(impact_data),
            **_nav_context(repo, api, "dependencies", v2)
        }
    )


@router.get("/ui/{repo}/{api}/templates", response_class=HTMLResponse)
def templates_view(request: Request, repo: str, api: str):
    """
    Display and manage test templates for an API.
    """
    if not docs_store.api_exists(repo, api):
        return _not_found(request, f"There is no API '{api}' in '{repo}'.")

    return templates.TemplateResponse(
        request,
        "templates.html",
        {
            "repo": repo,
            "api": api,
            "api_title": docs_store.get_title(repo, api),
            # the title describes what the API does better than its code name
            "recommendations": get_template_recommendations(f"{docs_store.get_title(repo, api)} {api}"),
            "custom_templates": list_templates(repo),
            "predefined_templates": get_predefined_templates(),
            **_nav_context(repo, api, "templates")
        }
    )
