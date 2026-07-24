from __future__ import annotations

import hashlib
import secrets

from starlette.authentication import AuthCredentials, AuthenticationBackend, BaseUser
from starlette.requests import HTTPConnection

from context_pager.config import settings
from context_pager.deps import Dependencies


class APIKeyUser(BaseUser):
    def __init__(self, tenant_id: str, api_key_prefix: str):
        self.tenant_id = tenant_id
        self.api_key_prefix = api_key_prefix

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return f"tenant:{self.tenant_id}"


class AuthBackend(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection):
        auth_header = conn.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None

        api_key = auth_header[7:].strip()
        if not api_key.startswith(settings.api_key_prefix):
            return None

        prefix = api_key[:8]
        pool = await Dependencies.pg_pool()
        async with pool.acquire() as pg_conn:
            row = await pg_conn.fetchrow(
                "SELECT id, tenant_id, hashed_api_key FROM users WHERE api_key_prefix = $1",
                prefix,
            )
            if not row:
                return None

            expected_hash = hashlib.sha256(api_key.encode()).hexdigest()
            if not secrets.compare_digest(row["hashed_api_key"], expected_hash):
                return None

            # Set tenant_id on the request state so downstream middleware + tools
            # can read it via contextvar (copied in tenant_middleware).
            conn.state.tenant_id = row["tenant_id"]

            return AuthCredentials(["authenticated"]), APIKeyUser(
                row["tenant_id"], prefix
            )