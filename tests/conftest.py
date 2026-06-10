"""Pytest bootstrap.

Loads the repo-root ``.env`` (if present) at import time — before pytest
evaluates skip markers — so integration tests that gate on DATABASE_URL /
SUPABASE_* see those vars. This is a no-op when ``.env`` or python-dotenv is
absent, keeping the offline unit suite green with no extra dependencies.
"""
from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv not installed (offline suite) — nothing to do.
    load_dotenv = None

if load_dotenv is not None:
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path)
