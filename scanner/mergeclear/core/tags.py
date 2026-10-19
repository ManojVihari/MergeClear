"""What a variable or expression is, as far as a scanner can tell (shared by the framework plugins)."""
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Tag:
    """What a variable or expression is, as far as the scanner can tell."""
    family: str                     # db | cache | broker | http | storage | email | search | framework
    tech: str                       # sqlalchemy, redis, kafka, ...
    role: str = ""                  # session | engine | connection | producer | consumer | client | ...
    info: Tuple[Tuple[str, str], ...] = ()

    def get(self, key, default=None):
        return dict(self.info).get(key, default)

    def with_info(self, **kw) -> "Tag":
        merged = dict(self.info)
        merged.update({k: v for k, v in kw.items() if v is not None})
        return Tag(self.family, self.tech, self.role, tuple(sorted(merged.items())))
