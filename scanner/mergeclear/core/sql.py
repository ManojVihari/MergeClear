"""SQL text helpers shared by the framework plugins."""
import re
from typing import List, Optional, Tuple


_SQL_FIRST = {"SELECT": "READ", "WITH": "READ", "SHOW": "READ", "DESCRIBE": "READ", "EXPLAIN": "READ", "VALUES": "READ",
              "INSERT": "WRITE", "UPDATE": "WRITE", "MERGE": "WRITE", "UPSERT": "WRITE", "REPLACE": "WRITE", "COPY": "WRITE",
              "DELETE": "DELETE", "TRUNCATE": "DELETE", "DROP": "DELETE", "CALL": "CALL", "EXEC": "CALL", "EXECUTE": "CALL",
              "CREATE": "WRITE", "ALTER": "WRITE"}
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([`\"\[]?[\w.]+[`\"\]]?)",
    re.I,
)
_SQL_KEYWORDS = {"select", "set", "values", "lateral", "unnest", "generate_series", "only", "dual", "table", "from", "where", "join", "as"}


def looks_like_sql(value: str) -> bool:
    return bool(re.match(r"\s*(select|insert|update|delete|with|merge|replace|truncate|create|alter|drop|call|exec|execute|upsert|copy|show)\b",
                         value, re.I)) and len(value) > 6


def parse_sql(sql: str) -> Tuple[Optional[str], List[str]]:
    """(operation, tables) of a SQL statement."""
    compact = re.sub(r"\s+", " ", sql or "").strip()
    if not compact:
        return None, []
    first = compact.split(" ", 1)[0].upper().rstrip(";(")
    operation = _SQL_FIRST.get(first)
    tables = []
    for match in _TABLE_REF.finditer(compact):
        name = match.group(1).strip('`"[]')
        if name and name.lower() not in _SQL_KEYWORDS and not name.startswith(("(", ":", "%", "$", "{")) and name not in tables:
            tables.append(name)
    return operation, tables



