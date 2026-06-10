"""Configuration — environment-backed settings.

Settings come from the process environment. When python-dotenv (part of the
``cloud`` extra) is installed, a local ``.env`` is loaded first as a convenience.
The core package stays stdlib-only: if python-dotenv isn't present, ``.env`` is
simply ignored and env vars are read directly.

No secrets live in code. See ``.env.example`` for the full list of variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv() -> None:
    """Hydrate os.environ from a local .env if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


@dataclass(frozen=True)
class Settings:
    supabase_url: str | None
    supabase_anon_key: str | None
    supabase_service_role_key: str | None
    mistral_api_key: str | None
    database_url: str | None

    def require(self, field_name: str) -> str:
        """Return a setting that must be present, else raise a clear error."""
        value = getattr(self, field_name)
        if not value:
            env_name = field_name.upper()
            raise RuntimeError(
                f"Missing required setting {env_name}. Set it in your environment "
                f"or .env (see .env.example)."
            )
        return value


def load_settings() -> Settings:
    """Load settings from the environment (and .env, if python-dotenv is present)."""
    _load_dotenv()
    return Settings(
        supabase_url=os.environ.get("SUPABASE_URL"),
        supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY"),
        supabase_service_role_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
        mistral_api_key=os.environ.get("MISTRAL_API_KEY"),
        database_url=os.environ.get("DATABASE_URL"),
    )
