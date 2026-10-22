"""
ASP.NET Core extractor: builds the application model of a repository and reports the endpoints a change impacts.
"""
import logging
import os
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Parser
from tree_sitter_c_sharp import language as csharp_language

from mergeclear.core.compose import load_compose

from .analyzer import Analyzer, dedupe
from .appmodel import AppModel
from .architecture import ArchitectureBuilder
from .csindex import ConstEvaluator, CsIndex, is_test_source
from .endpoints import EndpointExtractor
from .project import ProjectInfo, find_project_dirs, iter_cs_files, load_project, module_for
from .schemas import ModelRegistry, SchemaBuilder

logger = logging.getLogger(__name__)


class AspNetExtractor:

    def __init__(self):
        self.parser = Parser()
        self.parser.language = Language(csharp_language())
        self._reset("HEAD")

    def _reset(self, commit):
        self._commit = commit
        self._repo_path = None
        self.index: Optional[CsIndex] = None
        self.analyzer: Optional[Analyzer] = None
        self.app: Optional[AppModel] = None
        self.registry: Optional[ModelRegistry] = None
        self.endpoint_extractor: Optional[EndpointExtractor] = None
        self.endpoints: List[dict] = []
        self.architecture: Optional[dict] = None
        self.projects: Dict[str, ProjectInfo] = {}

    # ============================
    # MODEL
    # ============================

    def build_model(self, repo_path):
        if self._repo_path == repo_path and self.analyzer is not None:
            return
        self._repo_path = repo_path

        project_dirs = find_project_dirs(repo_path)
        compose = load_compose(repo_path)
        fallback = os.path.basename(os.path.abspath(repo_path))
        self.projects = {d: load_project(repo_path, d, compose, fallback) for d in project_dirs}

        index = CsIndex(repo_path)
        for full in iter_cs_files(repo_path):
            rel = os.path.relpath(full, repo_path).replace(os.sep, "/")
            if is_test_source(rel):
                continue
            try:
                with open(full, "rb") as f:
                    code = f.read()
                tree = self.parser.parse(code)
                index.add_file(rel, code, tree.root_node, module_for(rel, project_dirs))
            except Exception as e:
                logger.debug("Failed to index %s: %s", rel, e)
        index.finalize()
        self.index = index

        const = ConstEvaluator(index)
        registry = ModelRegistry(index, const)
        registry.nullable_enabled = {path: p.nullable for path, p in self.projects.items()}
        self.registry = registry
        schemas = SchemaBuilder(registry)

        app = AppModel(index, const)
        app.build()
        registry.pascal_case = app.json_pascal_case
        self.app = app

        analyzer = Analyzer(index, const, registry, app, self._resolve_placeholders)
        analyzer.analyze_all()
        self.analyzer = analyzer

        services = self._service_names()
        extractor = EndpointExtractor(analyzer, app, schemas, self.projects, lambda host, service: self._resolve_service(services, host, service))
        self.endpoint_extractor = extractor
        self.endpoints = extractor.extract_all()

        self.architecture = ArchitectureBuilder(analyzer, app, registry, self.endpoints, extractor, self.projects, compose, index).build()
        logger.info("ASP.NET Core model: %d types, %d endpoints, %d projects", len(index.types), len(self.endpoints), len(self.projects))

    def _resolve_placeholders(self, file, value: str) -> str:
        project = self.projects.get(file.project)
        if project is None or "${" not in value:
            return value

        def replace(match):
            found = project.config.get(match.group(1))
            if found is None:
                for profile in ("Development", "Production"):
                    found = project.config.profiles.get(profile, {}).get(match.group(1))
                    if found:
                        break
            return found if found is not None else match.group(0)

        return re.sub(r"\$\{([A-Za-z0-9_.:\-]+)\}", replace, value)

    @staticmethod
    def _norm(name: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())

    def _service_names(self) -> Dict[str, str]:
        names: Dict[str, str] = {}
        for path, project in self.projects.items():
            if not any(f.project == path for f in self.index.files.values()):
                continue
            for name in (project.name, project.assembly_name, os.path.basename(path)):
                if name:
                    names.setdefault(self._norm(name), path)
            if project.compose:
                names.setdefault(self._norm(project.compose["name"]), path)
        return names

    def _resolve_service(self, services: Dict[str, str], host: Optional[str], service: Optional[str]) -> Optional[str]:
        if len([p for p in self.projects if any(f.project == p for f in self.index.files.values())]) < 2:
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

        cs_files = [f.replace(os.sep, "/") for f in changed_files if f.endswith(".cs")]
        changed_methods, changed_types, hunks = self._changed_symbols(repo_path, cs_files, commit)
        impacted = self._impacted_endpoints(changed_methods, changed_types, hunks)
        breaking = self._breaking_changes(repo_path, cs_files, changed_types, commit)

        routes = []
        for endpoint in self.endpoints:
            key = self.endpoint_extractor.handler_key(endpoint)
            reasons = set(impacted.get(key, set()))
            if not reasons:
                continue
            route = dict(endpoint)
            route["change_reasons"] = sorted(reasons)
            changes = []
            for type_name in endpoint.get("referenced_types", []):
                changes.extend(breaking.get(type_name, []))
            if changes:
                route["breaking_changes"] = dedupe(changes)
            routes.append(route)
        logger.info("Impacted endpoints: %d of %d", len(routes), len(self.endpoints))
        return routes

    # ============================
    # CHANGE DETECTION
    # ============================

    def _changed_lines(self, repo_path, files, commit) -> Optional[Dict[str, List[Tuple[int, int]]]]:
        if not files:
            return {}
        try:
            diff = subprocess.check_output(["git", "diff", "-U0", f"{commit}^", commit, "--", *files], cwd=repo_path,
                                           stderr=subprocess.DEVNULL).decode(errors="replace")
        except (subprocess.CalledProcessError, OSError):
            return None
        changed: Dict[str, List[Tuple[int, int]]] = {}
        current = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                current = path[2:] if path.startswith("b/") else None
            elif line.startswith("@@") and current:
                match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
                if match:
                    start = int(match.group(1))
                    count = int(match.group(2)) if match.group(2) is not None else 1
                    changed.setdefault(current, []).append((start, start + max(count, 1) - 1))
        return changed

    def _changed_symbols(self, repo_path, cs_files, commit):
        changed_lines = self._changed_lines(repo_path, cs_files, commit)
        methods: Set[str] = set()
        types: Set[str] = set()
        hunks_by_file: Dict[str, List[Tuple[int, int]]] = {}
        for rel in cs_files:
            file = self.index.files.get(rel)
            if file is None:
                continue
            hunks = [(1, 10 ** 9)] if changed_lines is None else changed_lines.get(rel, [])
            hunks_by_file[rel] = list(hunks)
            for start, end in hunks:
                for t in file.types:
                    if end < t.line_start or start > t.line_end:
                        continue
                    own = t.methods + t.ctors
                    hit = [m for m in own if m.file is file and not (end < m.line_start or start > m.line_end)]
                    methods.update(m.id for m in hit)
                    nested = any(n.file is file and not (end < n.line_start or start > n.line_end) for n in t.nested)
                    member_hit = any(not (end < m.line - 0 or start > m.line + 0) for m in t.members) and not hit
                    if changed_lines is None or (not hit and not nested) or member_hit:
                        # class-level change (attributes, bases, properties, fields): the whole type changed
                        types.add(t.qualified)
                        methods.update(m.id for m in own)
                if file.top_level is not None and not (end < file.top_level.line_start or start > file.top_level.line_end):
                    methods.add(file.top_level.id)
                for lam in self.app._lambdas.values():
                    if lam.file is file and not (end < lam.line_start or start > lam.line_end):
                        methods.add(lam.id)
        return methods, types, hunks_by_file

    def _impacted_endpoints(self, changed_methods: Set[str], changed_types: Set[str], hunks: Dict[str, List[Tuple[int, int]]]) -> Dict[str, Set[str]]:
        ex = self.endpoint_extractor
        a = self.analyzer
        entity_changed = {self.registry.entities()[q]["type"].name for q in changed_types if q in self.registry.entities()}
        impacted: Dict[str, Set[str]] = {}
        for e in self.endpoints:
            key = ex.handler_key(e)
            reasons: Set[str] = set()
            for mid in ex.chains.get(key, set()) & changed_methods:
                method = ex.chain_methods.get(mid)
                reasons.add(f"changed: {a.label(method)}" if method else f"changed: {mid}")
            for q in changed_types & set(e.get("referenced_types", [])):
                reasons.add(f"changed model: {q.rsplit('.', 1)[-1]}")
            used = {n for f in (e.get("integrations") or {}).get("databases", []) for n in (f.get("entities") or [f.get("entity")]) if n}
            for name in used & entity_changed:
                reasons.add(f"uses changed entity: {name}")
            for rel, first, last in ex.route_spans.get(key, []):
                if any(not (end < first or start > last) for start, end in hunks.get(rel, [])):
                    reasons.add(f"changed route registration: {rel}")
            if reasons:
                impacted[key] = reasons
        return impacted

    # ============================
    # BREAKING CHANGES
    # ============================

    def get_old_file_content(self, repo_path, rel_path, commit=None):
        try:
            return subprocess.check_output(["git", "show", f"{commit or self._commit}^:{rel_path}"], cwd=repo_path, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError):
            return None

    def _breaking_changes(self, repo_path, cs_files, changed_types, commit) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {}
        wanted = [rel for rel in cs_files if rel in self.index.files and any(t.qualified in changed_types for t in self.index.files[rel].types)]
        if not wanted:
            return out
        old = CsIndex(repo_path)
        replaced = False
        for rel, file in self.index.files.items():
            code = file.code
            if rel in wanted:
                code = self.get_old_file_content(repo_path, rel, commit)
                if not code:
                    continue
                replaced = True
            old.add_file(rel, code, self.parser.parse(code).root_node, file.project)
        old.finalize()
        if not replaced:
            return out
        old_registry = ModelRegistry(old, ConstEvaluator(old))
        old_registry.nullable_enabled = self.registry.nullable_enabled
        old_registry.pascal_case = self.registry.pascal_case
        old_schemas, new_schemas = SchemaBuilder(old_registry), SchemaBuilder(self.registry)
        for rel in wanted:
            for new_type in self.index.files[rel].types:
                if new_type.qualified not in changed_types or not self.registry.is_model_like(new_type):
                    continue
                old_type = old.types.get(new_type.qualified)
                if old_type is None:
                    continue
                changes = self.detect_breaking_changes(old_schemas.schema_fields(old_type, None, set()), new_schemas.schema_fields(new_type, None, set()))
                for c in changes:
                    c["dto"] = new_type.name
                if changes:
                    out[new_type.qualified] = changes
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
                breaking.append({"type": "FIELD_ADDED", "field": field, "new_type": new_details.get("type"), "required": bool(new_validations.get("required"))})
                continue
            if old_details.get("type") != new_details.get("type"):
                breaking.append({"type": "TYPE_CHANGED", "field": field, "old_type": old_details.get("type"), "new_type": new_details.get("type")})
            old_validations = old_details.get("validation") or {}
            for rule in old_validations:
                if rule not in new_validations:
                    breaking.append({"type": "VALIDATION_REMOVED", "field": field, "rule": rule})
            for rule in new_validations:
                if rule not in old_validations:
                    breaking.append({"type": "VALIDATION_ADDED", "field": field, "rule": rule})
                elif old_validations[rule] != new_validations[rule]:
                    breaking.append({"type": "VALIDATION_CHANGED", "field": field, "rule": rule, "old": old_validations[rule], "new": new_validations[rule]})
        return breaking
