import logging
import subprocess
import os
from mergeclear.core.files import iter_source_files
from mergeclear.core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)

SCANNER_VERSION = "2.0"
SOURCE_EXTENSIONS = (".py", ".java", ".cs")


class Scanner:

    def __init__(self):
        self.plugin_manager = PluginManager()

    def scan(self, repo_path, commit):

        try:
            changed_files = self.get_changed_files(repo_path, commit)
        except Exception as e:
            logger.error("Failed to get changed files: %s", e)
            changed_files = []

        if not changed_files:
            logger.info("No changed files detected")
            return self._empty_result(repo_path, commit)

        return self._run_plugins(repo_path, commit, changed_files, full=False)

    def scan_since(self, repo_path, ref, label=None):
        """Endpoints touched by every change between `ref` and the working tree (e.g. a branch vs main)."""
        files = self._git(repo_path, "diff", "--name-only", f"{ref}...HEAD")
        files += self._git(repo_path, "diff", "--name-only", "HEAD")      # uncommitted edits too
        changed = sorted({f for f in files if f.endswith(SOURCE_EXTENSIONS)})
        logger.info("%d source files changed since %s", len(changed), ref)
        if not changed:
            return self._empty_result(repo_path, label)
        return self._run_plugins(repo_path, label, changed, full=False)

    def scan_full(self, repo_path, label="local"):
        """
        Every endpoint of a local folder, no git needed (local testing).

        `label` is reported as the commit.
        """
        changed_files = [
            os.path.relpath(path, repo_path).replace(os.sep, "/")
            for ext in SOURCE_EXTENSIONS
            for path in iter_source_files(repo_path, ext)
        ]
        logger.info("Full scan: %d source files", len(changed_files))
        return self._run_plugins(repo_path, label, changed_files, full=True)

    def _run_plugins(self, repo_path, commit, changed_files, full):
        plugins = self.plugin_manager.load_plugins()

        all_routes = []
        detected_frameworks = []
        architecture = {}

        for plugin in plugins:
            try:
                if plugin.detect(repo_path):
                    detected_frameworks.append(plugin.name)

                    all_routes_of = getattr(plugin.extractor, "all_routes", None)
                    if full and all_routes_of is not None:
                        routes = all_routes_of(repo_path)
                    else:
                        routes = plugin.extractor.process_repository(
                            repo_path,
                            changed_files,
                            commit
                        )

                    all_routes.extend(routes)

                    describe = getattr(plugin.extractor, "describe_application", None)
                    if describe is not None:
                        architecture[plugin.name] = describe()

            except Exception as e:
                logger.exception("Plugin %s failed: %s", plugin.name, e)

        if not detected_frameworks:
            logger.warning("No frameworks detected")

        return {
            "scanner_version": SCANNER_VERSION,
            "repository": os.path.basename(os.path.abspath(repo_path)),
            "commit": commit,
            "scan_mode": "full" if full else "changes",
            "frameworks": detected_frameworks,
            "routes": all_routes,
            "architecture": architecture
        }

    def get_changed_files(self, repo_path, commit):
        """
        Source files changed by `commit` compared to its first parent.

        Falls back to every tracked source file when the parent is not
        available (first commit, or a shallow CI clone).
        """
        try:
            files = self._git(repo_path, "diff", "--name-only", f"{commit}^", commit)
        except subprocess.CalledProcessError:
            logger.info("No parent commit for %s; scanning all tracked files", commit)
            files = self._git(repo_path, "ls-files")

        filtered = [f for f in files if f.endswith(SOURCE_EXTENSIONS)]

        logger.info("Found %d changed source files", len(filtered))

        return filtered

    def _git(self, repo_path, *args):
        result = subprocess.check_output(
            ["git", *args],
            cwd=repo_path,
            stderr=subprocess.STDOUT
        )
        return result.decode().splitlines()

    def _empty_result(self, repo_path, commit):
        return {
            "scanner_version": SCANNER_VERSION,
            "repository": os.path.basename(os.path.abspath(repo_path)),
            "commit": commit,
            "scan_mode": "changes",
            "frameworks": [],
            "routes": [],
            "architecture": {}
        }
