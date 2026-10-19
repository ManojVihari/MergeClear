"""
FastAPI extractor: builds the application model of a repository and reports the endpoints a change impacts.
"""
import logging
import os
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Parser
from tree_sitter_python import language as python_language

from mergeclear.core.files import iter_source_files
from mergeclear.core.treesitter import reachable

from .analyzer import Analyzer, dedupe
from .appmodel import AppModel
from .architecture import ArchitectureBuilder
from .endpoints import EndpointExtractor
from .project import ProjectInfo, find_project_dirs, load_compose, load_project, module_for
from .pyindex import ConstEvaluator, PythonIndex, UNKNOWN, call_parts, is_test_source, keyword_args
from .schemas import ModelRegistry, SchemaBuilder

logger = logging.getLogger(__name__)

SKIPPED_SEGMENTS = {"migrations", "alembic_versions", "node_modules", "site-packages"}
PYDANTIC_V2_MARKERS = (b"field_validator", b"model_validator", b"ConfigDict", b"model_config", b"computed_field", b"pydantic_settings",
                       b"model_dump", b"SettingsConfigDict", b"model_validate", b"TypeAdapter")


def _skip_source(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIPPED_SEGMENTS for p in parts[:-1]):
        return True
    return "alembic" in parts and "versions" in parts


class FastAPIExtractor:

    def __init__(self):
        self.parser = Parser()
        self.parser.language = Language(python_language())
        self._reset("HEAD")

    def _reset(self, commit):
        self._commit = commit
        self._repo_path = None
        self.index: Optional[PythonIndex] = None
        self.analyzer: Optional[Analyzer] = None
        self.appmodel: Optional[AppModel] = None
        self.registry: Optional[ModelRegistry] = None
        self.endpoint_extractor: Optional[EndpointExtractor] = None
        self.endpoints: List[dict] = []
        self.architecture: Optional[dict] = None
        self.projects: Dict[str, ProjectInfo] = {}

    # ============================
    # MODEL
    # ============================

    def build_model(self, repo_path):
        """Parse the sources, manifests and configuration and analyze the whole application."""
        if self._repo_path == repo_path and self.analyzer is not None:
            return
        self._repo_path = repo_path

        project_dirs = find_project_dirs(repo_path)
        compose = load_compose(repo_path)
        fallback = os.path.basename(os.path.abspath(repo_path))
        self.projects = {d: load_project(repo_path, d, compose, fallback) for d in project_dirs}

        index = PythonIndex(repo_path)
        index.parser = self.parser
        v2_marker = False
        for full in iter_source_files(repo_path, ".py"):
            rel = os.path.relpath(full, repo_path).replace(os.sep, "/")
            if is_test_source(rel) or _skip_source(rel):
                continue
            try:
                with open(full, "rb") as f:
                    code = f.read()
                tree = self.parser.parse(code)
                index.add_file(rel, code, tree.root_node, module_for(rel, project_dirs))
                v2_marker = v2_marker or any(m in code for m in PYDANTIC_V2_MARKERS)
            except Exception as e:
                logger.debug("Failed to index %s: %s", rel, e)
        index.finalize()
        self.index = index

        const = ConstEvaluator(index)
        registry = ModelRegistry(index, const)
        registry.pydantic_v2 = v2_marker or self._declares_pydantic_v2()
        self.registry = registry
        schemas = SchemaBuilder(registry)

        self._load_settings_defaults(const, registry)

        appmodel = AppModel(index, const)
        appmodel.build()
        self.appmodel = appmodel

        analyzer = Analyzer(index, const, registry, appmodel, self._resolve_placeholders)
        analyzer.analyze_all()
        self.analyzer = analyzer

        services = self._service_names(appmodel)
        extractor = EndpointExtractor(analyzer, appmodel, schemas, lambda host, service: self._resolve_service(services, host, service))
        self.endpoint_extractor = extractor
        self.endpoints = extractor.extract_all()

        self.architecture = ArchitectureBuilder(
            analyzer, appmodel, registry, self.endpoints, extractor, self.projects, compose, index, registry.pydantic_v2).build()

        logger.info(
            "FastAPI model: %d modules, %d classes, %d endpoints, %d projects",
            len(index.modules), sum(len(m.all_classes) for m in index.modules.values()), len(self.endpoints), len(self.projects),
        )

    def _declares_pydantic_v2(self) -> bool:
        for project in self.projects.values():
            dep = project.dependency("pydantic")
            if dep and dep.get("version") and re.search(r"(>=|==|~=|\^|>)\s*v?2", dep["version"]):
                return True
            if project.dependency("pydantic-settings"):
                return True
        return False

    def _load_settings_defaults(self, const: ConstEvaluator, registry: ModelRegistry):
        """Defaults of Settings classes join the configuration (environment files win)."""
        for module in self.index.modules.values():
            project = self.projects.get(module.project)
            if project is None:
                continue
            for cls in module.all_classes:
                if cls.outer is not None or registry.kind(cls) != "settings":
                    continue
                prefix = ""
                config_node = cls.class_vars.get("model_config")
                if config_node is not None:
                    kw = keyword_args(call_parts(config_node)[1]) if call_parts(config_node)[0] is not None else {}
                    prefix = const.string(kw["env_prefix"], module) if "env_prefix" in kw else ""
                inner = cls.nested.get("Config")
                if inner is not None and "env_prefix" in inner.class_vars:
                    prefix = const.string(inner.class_vars["env_prefix"], inner.module) or ""
                for f, owner in registry.class_fields(cls):
                    value = const.class_field_default(cls, f.name)
                    if value is UNKNOWN or value is None or isinstance(value, (list, dict)):
                        continue
                    if isinstance(value, str) and value.startswith("${"):
                        continue
                    project.config.default.setdefault(f"{prefix or ''}{f.name}".upper(), str(value))

    def _resolve_placeholders(self, module, value: str) -> str:
        project = self.projects.get(module.project)
        if project is None or "${" not in value:
            return value

        def replace(match):
            found = project.config.get(match.group(1))
            return found if found is not None else match.group(0)

        return re.sub(r"\$\{([A-Za-z0-9_.-]+)\}", replace, value)

    @staticmethod
    def _norm(name: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())

    def _service_names(self, appmodel: AppModel) -> Dict[str, str]:
        """normalized service name -> project path, for HTTP calls between services of one repository."""
        names: Dict[str, str] = {}
        for path, project in self.projects.items():
            if not any(m.project == path for m in self.index.modules.values()):
                continue
            for name in (project.name, os.path.basename(path)):
                if name:
                    names.setdefault(self._norm(name), path)
            if project.compose:
                names.setdefault(self._norm(project.compose["name"]), path)
        for app in appmodel.apps():
            if app.title:
                names.setdefault(self._norm(app.title), app.module.project)
        return names

    def _resolve_service(self, services: Dict[str, str], host: Optional[str], service: Optional[str]) -> Optional[str]:
        if len(self.projects) < 2:
            return None
        for candidate in (service, host):
            if candidate:
                key = self._norm(candidate.split(".")[0])
                if key in services:
                    return services[key]
        return None

    # ============================
    # PUBLIC API
    # ============================

    def all_routes(self, repo_path):
        """Every endpoint of the repository (full documentation, no diff)."""
        self._reset("HEAD")
        self.build_model(repo_path)
        return [dict(e) for e in self.endpoints]

    def describe_application(self, repo_path=None):
        if repo_path is not None:
            self.build_model(repo_path)
        return self.architecture

    # ============================
    # MAIN PROCESS
    # ============================

    def process_repository(self, repo_path, changed_files, commit="HEAD"):
        self._reset(commit)
        self.build_model(repo_path)

        py_files = [f.replace(os.sep, "/") for f in changed_files if f.endswith(".py") and not _skip_source(f)]
        changed_funcs, changed_classes, changed_hunks = self._changed_symbols(repo_path, py_files, commit)

        logger.debug("Changed functions: %s", sorted(changed_funcs))
        logger.debug("Changed classes: %s", sorted(changed_classes))

        impacted = self._impacted_endpoints(changed_funcs, changed_classes, changed_hunks)
        breaking = self._breaking_changes(repo_path, py_files, changed_classes, commit)

        routes = []
        for endpoint in self.endpoints:
            key = (endpoint["controller"], endpoint["function"], endpoint["line"], endpoint["method"], endpoint["path"])
            if key not in impacted:
                continue
            route = dict(endpoint)
            route["change_reasons"] = sorted(impacted[key])

            changes = []
            for class_id in endpoint.get("referenced_types", []):
                changes.extend(breaking.get(class_id, []))
            if changes:
                route["breaking_changes"] = dedupe(changes)
            routes.append(route)

        logger.info("Impacted endpoints: %d of %d", len(routes), len(self.endpoints))
        return routes

    # ============================
    # CHANGE DETECTION
    # ============================

    def _changed_lines(self, repo_path, files, commit) -> Optional[Dict[str, List[Tuple[int, int]]]]:
        """{file: [(start, end)]} of lines changed by commit; None if the parent is unavailable."""
        if not files:
            return {}
        try:
            diff = subprocess.check_output(
                ["git", "diff", "-U0", f"{commit}^", commit, "--", *files],
                cwd=repo_path, stderr=subprocess.DEVNULL,
            ).decode(errors="replace")
        except (subprocess.CalledProcessError, OSError):
            return None

        changed: Dict[str, List[Tuple[int, int]]] = {}
        current = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                current = path[2:] if path.startswith("b/") else None
            elif line.startswith("@@") and current:
                match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if match:
                    start = int(match.group(3))
                    count = int(match.group(4)) if match.group(4) is not None else 1
                    # deleted lines have no new lines: they touch the line where they were removed
                    changed.setdefault(current, []).append((start, start + max(count, 1) - 1))
        return changed

    def _changed_symbols(self, repo_path, py_files, commit):
        """(function ids, class ids, {module name: changed line ranges}) touched by the change."""
        changed_lines = self._changed_lines(repo_path, py_files, commit)
        funcs: Set[str] = set()
        classes: Set[str] = set()
        hunks_by_module: Dict[str, List[Tuple[int, int]]] = {}

        for rel in py_files:
            module = self.index.by_rel.get(rel)
            if module is None:
                continue
            hunks = [(1, 10 ** 9)] if changed_lines is None else changed_lines.get(rel, [])
            hunks_by_module[module.name] = list(hunks)
            for start, end in hunks:
                for cls in module.all_classes:
                    if end < cls.line_start or start > cls.line_end:
                        continue
                    hit_methods = [m for m in cls.methods.values() if not (end < m.line_start or start > m.line_end)]
                    in_nested = any(not (end < n.line_start or start > n.line_end) for n in cls.nested.values())
                    funcs.update(m.id for m in hit_methods)
                    if changed_lines is None or (not hit_methods and not in_nested):
                        # class-level change (fields, bases, decorators): the whole class changed
                        classes.add(cls.id)
                        funcs.update(m.id for m in cls.methods.values())
                for func in module.all_functions:
                    if func.owner is not None:
                        continue
                    if end < func.line_start or start > func.line_end:
                        continue
                    funcs.add(func.id)
        return funcs, classes, hunks_by_module

    def _impacted_endpoints(self, changed_funcs: Set[str], changed_classes: Set[str],
                            changed_hunks: Dict[str, List[Tuple[int, int]]]) -> Dict[tuple, Set[str]]:
        a = self.analyzer
        ex = self.endpoint_extractor
        entity_names = {
            self.registry.entity_info(c)["name"]: c.id
            for m in self.index.modules.values() for c in m.all_classes
            if c.outer is None and self.registry.kind(c) == "entity" and self.registry.entity_info(c)
        }
        changed_entity_names = {n for n, cid in entity_names.items() if cid in changed_classes}

        impacted: Dict[tuple, Set[str]] = {}
        for e in self.endpoints:
            key = (e["controller"], e["function"], e["line"], e["method"], e["path"])
            reasons: Set[str] = set()
            chain = ex.chains.get(f"{ex.handler_id(e)}", set())
            for fid in chain & changed_funcs:
                func = next((f for f in [ex.chain_funcs.get(fid)] if f is not None), None)
                reasons.add(f"changed: {a.label(func)}" if func else f"changed: {fid}")
            for class_id in changed_classes & set(e.get("referenced_types", [])):
                reasons.add(f"changed model: {class_id.rsplit('.', 1)[-1]}")
            used_entities = {n for f in (e.get("integrations") or {}).get("databases", []) for n in (f.get("entities") or [f.get("entity")]) if n}
            for name in used_entities & changed_entity_names:
                reasons.add(f"uses changed entity: {name}")
            for module_name, first, last in ex.router_ranges.get(ex.handler_id(e), []):
                if any(not (end < first or start > last) for start, end in changed_hunks.get(module_name, [])):
                    reasons.add(f"changed router configuration: {module_name}")
            if reasons:
                impacted[key] = reasons
        return impacted

    # ============================
    # BREAKING CHANGES
    # ============================

    def get_old_file_content(self, repo_path, rel_path, commit=None):
        try:
            return subprocess.check_output(
                ["git", "show", f"{commit or self._commit}^:{rel_path}"],
                cwd=repo_path, stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            return None

    def _breaking_changes(self, repo_path, py_files, changed_classes, commit) -> Dict[str, List[dict]]:
        """Contract changes of changed model classes: {class id: [change]}."""
        out: Dict[str, List[dict]] = {}
        wanted = [rel for rel in py_files if any(c.id in changed_classes for c in (self.index.by_rel[rel].all_classes if rel in self.index.by_rel else []))]
        if not wanted:
            return out

        old_index = PythonIndex(repo_path)
        old_index.parser = self.parser
        old_index.modules = dict(self.index.modules)
        old_index.by_rel = dict(self.index.by_rel)
        replaced = False
        for rel in wanted:
            current = self.index.by_rel[rel]
            old_code = self.get_old_file_content(repo_path, rel, commit)
            if not old_code:
                old_index.modules.pop(current.name, None)
                continue
            tree = self.parser.parse(old_code)
            old_index.add_file(rel, old_code, tree.root_node, current.project)
            replaced = True
        old_index.finalize()
        if not replaced:
            return out

        old_registry = ModelRegistry(old_index, ConstEvaluator(old_index))
        old_registry.pydantic_v2 = self.registry.pydantic_v2
        old_schemas, new_schemas = SchemaBuilder(old_registry), SchemaBuilder(self.registry)

        for rel in wanted:
            new_module = self.index.by_rel[rel]
            old_module = old_index.by_rel.get(rel)
            if old_module is None:
                continue
            for new_cls in new_module.all_classes:
                if new_cls.id not in changed_classes or not self.registry.is_model_like(new_cls):
                    continue
                old_cls = next((c for c in old_module.all_classes if c.id == new_cls.id), None)
                if old_cls is None:
                    continue
                changes = self.detect_breaking_changes(
                    old_schemas.schema_fields(old_cls, set()), new_schemas.schema_fields(new_cls, set()))
                for c in changes:
                    c["dto"] = new_cls.name
                if changes:
                    out[new_cls.id] = changes
        return out

    def detect_breaking_changes(self, old_schema, new_schema):
        breaking = []

        for field, old_details in old_schema.items():
            if field not in new_schema:
                breaking.append({"type": "FIELD_REMOVED", "field": field, "old_type": old_details.get("type")})

        for field, new_details in new_schema.items():
            old_details = old_schema.get(field)
            new_validations = new_details.get("validation") or {}

            if old_details is None:
                breaking.append({"type": "FIELD_ADDED", "field": field, "new_type": new_details.get("type"),
                                 "required": bool(new_validations.get("required"))})
                continue

            if old_details.get("type") != new_details.get("type"):
                breaking.append({"type": "TYPE_CHANGED", "field": field,
                                 "old_type": old_details.get("type"), "new_type": new_details.get("type")})

            old_validations = old_details.get("validation") or {}
            for rule in old_validations:
                if rule not in new_validations:
                    breaking.append({"type": "VALIDATION_REMOVED", "field": field, "rule": rule})
            for rule in new_validations:
                if rule not in old_validations:
                    breaking.append({"type": "VALIDATION_ADDED", "field": field, "rule": rule})
                elif old_validations[rule] != new_validations[rule]:
                    breaking.append({"type": "VALIDATION_CHANGED", "field": field, "rule": rule,
                                     "old": old_validations[rule], "new": new_validations[rule]})
        return breaking

    # ============================
    # CONVENIENCE
    # ============================

    def extract_model_schema(self, repo_path, model_name):
        """Flat field schema of a Pydantic model (built from the full model)."""
        self.build_model(repo_path)
        for module in self.index.modules.values():
            cls = module.classes.get(model_name)
            if cls is not None:
                return SchemaBuilder(self.registry).schema_fields(cls, set())
        return {}

    def expand_impact(self, func, graph):
        return reachable(func, graph, include_start_on_cycle=False)

