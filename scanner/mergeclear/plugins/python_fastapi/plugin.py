import logging
import re

from mergeclear.core.files import iter_source_files
from mergeclear.plugins.base_plugin import BasePlugin
from .extractor import FastAPIExtractor

logger = logging.getLogger(__name__)

FASTAPI_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+fastapi(?:\.[\w.]+)?[ \t]+import|import[ \t]+fastapi\b)", re.M)


class Plugin(BasePlugin):

    name = "fastapi"

    def __init__(self):
        self.extractor = FastAPIExtractor()

    def detect(self, repo_path):
        logger.debug("Running FastAPI detection...")

        for full in iter_source_files(repo_path, ".py"):
            try:
                with open(full, "r", encoding="utf-8") as file:
                    content = file.read()
            except (OSError, UnicodeDecodeError) as e:
                logger.debug("Error reading %s: %s", full, e)
                continue

            if FASTAPI_IMPORT.search(content):
                logger.debug("Detected FastAPI in: %s", full)
                return True

        return False
