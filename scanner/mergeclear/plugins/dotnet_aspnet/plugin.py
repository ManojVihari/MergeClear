import logging
import re

from mergeclear.plugins.base_plugin import BasePlugin

from .extractor import AspNetExtractor
from .project import iter_cs_files

logger = logging.getLogger(__name__)

ASPNET_MARKERS = re.compile(
    r"\bControllerBase\b|\[ApiController\]|\.Map(Get|Post|Put|Delete|Patch|Group|Controllers|Methods|Fallback|Hub)\s*\(|WebApplication\.Create|"
    r"\bIEndpointRouteBuilder\b|\bUseEndpoints\s*\(|\bMicrosoft\.AspNetCore\b|:\s*Controller\b|\bAddControllers\s*\("
)


class Plugin(BasePlugin):

    name = "aspnet"

    def __init__(self):
        self.extractor = AspNetExtractor()

    def detect(self, repo_path):
        logger.debug("Running ASP.NET Core detection...")

        for full in iter_cs_files(repo_path):
            try:
                with open(full, "r", encoding="utf-8-sig", errors="replace") as file:
                    content = file.read()
            except OSError as e:
                logger.debug("Error reading %s: %s", full, e)
                continue

            if ASPNET_MARKERS.search(content):
                logger.debug("Detected ASP.NET Core in: %s", full)
                return True

        return False
