"""FastAPI auth dependency — validates Supabase ES256 JWTs via JWKS.

Ported from shirim/api/auth/dependencies.py.
"""
import logging
import ssl

import certifi
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from ..config import DEV_BYPASS_AUTH, DEV_USER_EMAIL, SUPABASE_URL
from .models import User

log = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

_jwks_client: PyJWKClient | None = None
_dev_user_cache: User | None = None

# Fallback used only if DEV_BYPASS_AUTH is true but the admin lookup fails
# (e.g. Supabase unreachable, DEV_USER_EMAIL unset or not in auth.users).
# Same UUID as shirim so any rows it creates line up with the existing dev user
# in the shared Supabase project.
FALLBACK_DEV_USER = User(
    id="2395ebd0-4e36-4f39-94ec-d86030f4e700",
    email="dev@test.com",
    name="Dev User (fallback)",
)


def _resolve_dev_user() -> User:
    """Fetch a real Supabase user (matching DEV_USER_EMAIL) via the admin client.

    Cached after the first successful lookup. Falls back to FALLBACK_DEV_USER
    on any error so dev-bypass mode never breaks because of a transient
    Supabase failure.
    """
    global _dev_user_cache
    if _dev_user_cache is not None:
        return _dev_user_cache

    if not DEV_USER_EMAIL:
        log.warning(
            "[auth] DEV_BYPASS_AUTH=true but DEV_USER_EMAIL is empty — "
            "using hardcoded fallback dev user"
        )
        _dev_user_cache = FALLBACK_DEV_USER
        return _dev_user_cache

    try:
        # Imported lazily so importing this module never hard-requires supabase.
        from ..supabase_client import get_client

        client = get_client()
        # supabase-py paginates list_users; we walk pages until we find the match.
        page = 1
        while True:
            resp = client.auth.admin.list_users(page=page, per_page=200)
            users = resp if isinstance(resp, list) else getattr(resp, "users", [])
            if not users:
                break
            for u in users:
                if (getattr(u, "email", "") or "").lower() == DEV_USER_EMAIL:
                    meta = getattr(u, "user_metadata", None) or {}
                    _dev_user_cache = User(
                        id=u.id,
                        email=u.email,
                        name=meta.get("full_name") or meta.get("name"),
                        avatar_url=meta.get("avatar_url"),
                    )
                    log.info(
                        "[auth] dev bypass resolved to real user %s (%s)",
                        u.id,
                        u.email,
                    )
                    return _dev_user_cache
            if len(users) < 200:
                break
            page += 1

        log.warning(
            "[auth] DEV_USER_EMAIL=%s not found in Supabase auth.users — "
            "using hardcoded fallback",
            DEV_USER_EMAIL,
        )
    except Exception as e:
        log.warning(
            "[auth] admin user lookup failed (%s: %s) — using hardcoded fallback",
            type(e).__name__,
            e,
        )

    _dev_user_cache = FALLBACK_DEV_USER
    return _dev_user_cache


def get_jwks_client() -> PyJWKClient | None:
    """Lazy singleton JWKS client for ES256 verification."""
    global _jwks_client
    if _jwks_client is None and SUPABASE_URL:
        jwks_url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        _jwks_client = PyJWKClient(jwks_url, ssl_context=ssl_context)
    return _jwks_client


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> User:
    """FastAPI dependency — extract + validate the Bearer token, return User.

    Set DEV_BYPASS_AUTH=true in .env to bypass auth during local testing.
    """
    if DEV_BYPASS_AUTH:
        return _resolve_dev_user()

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    jwks_client = get_jwks_client()
    if jwks_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_URL not configured",
        )

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        log.info("[auth] token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        log.info("[auth] invalid token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        log.warning("[auth] unexpected jwt error %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    email = payload.get("email") or ""

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user ID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_metadata = payload.get("user_metadata") or {}
    return User(
        id=user_id,
        email=email,
        name=user_metadata.get("full_name") or user_metadata.get("name"),
        avatar_url=user_metadata.get("avatar_url"),
        access_token=token,
    )
