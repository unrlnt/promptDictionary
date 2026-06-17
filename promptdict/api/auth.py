"""Request authentication: derive owner_id from a verified Supabase JWT.

The owner id comes ONLY from a server-side-verified access token — never from the
request body or query. We verify by calling supabase-py ``auth.get_user(token)``,
which validates the token against the Supabase auth server. This works both before
and after JWT signing-key rotation.

(Local JWKS verification — fetching the project's public keys and verifying the JWT
signature in-process, no network per request — is a later optimization, viable once
the asymmetric JWT signing keys are rotated in.)
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Header, HTTPException, status

_client = None


def _supabase():
    """Lazily build a server-side Supabase client (cached). The key is only used as
    the API key header; the user's token is what gets verified."""
    global _client
    if _client is None:
        from supabase import create_client

        from ..config import load_settings

        settings = load_settings()
        _client = create_client(settings.require("supabase_url"),
                                settings.supabase_secret)
    return _client


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid bearer token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_token(token: str) -> str:
    """Verify a Supabase access token and return the user id (str). Raises on any
    failure. Patch this in tests to avoid a real network call."""
    response = _supabase().auth.get_user(token)
    user = getattr(response, "user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    if not user_id:
        raise _unauthorized()
    return str(user_id)


def get_current_owner(authorization: str | None = Header(default=None)) -> UUID:
    """FastAPI dependency: validate the ``Authorization: Bearer <token>`` header and
    return the verified user's id as a UUID. The ONLY source of owner_id."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    token = authorization[len("bearer "):].strip()
    if not token:
        raise _unauthorized()
    try:
        user_id = verify_token(token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — any verification failure is a 401
        raise _unauthorized() from exc
    try:
        return UUID(user_id)
    except (ValueError, TypeError) as exc:
        raise _unauthorized() from exc
