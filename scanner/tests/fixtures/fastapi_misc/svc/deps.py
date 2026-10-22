from fastapi import Depends, Header, Cookie, Security
from fastapi.security import APIKeyHeader, SecurityScopes

api_key = APIKeyHeader(name="X-API-Key")


class Pagination:
    def __init__(self, page: int = 1, size: int = 20):
        self.page = page
        self.size = size


class RoleChecker:
    def __init__(self, allowed: list):
        self.allowed = allowed

    def __call__(self, key: str = Depends(api_key)):
        return key


def tenant(x_tenant_id: str = Header(...), session: str = Cookie(None)):
    return x_tenant_id


def scoped(security_scopes: SecurityScopes, key: str = Depends(api_key)):
    return key
