"""Offline unit tests for get_current_owner — verifier mocked, no network."""
from __future__ import annotations

from uuid import UUID

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from promptdict.api import auth

VALID_UUID = "11111111-1111-1111-1111-111111111111"


def test_valid_token_returns_uuid(monkeypatch):
    monkeypatch.setattr(auth, "verify_token", lambda token: VALID_UUID)
    result = auth.get_current_owner(authorization="Bearer good-token")
    assert result == UUID(VALID_UUID)


def test_missing_header_is_401():
    with pytest.raises(HTTPException) as exc:
        auth.get_current_owner(authorization=None)
    assert exc.value.status_code == 401


def test_non_bearer_header_is_401():
    with pytest.raises(HTTPException) as exc:
        auth.get_current_owner(authorization="Token abc")
    assert exc.value.status_code == 401


def test_empty_bearer_is_401():
    with pytest.raises(HTTPException) as exc:
        auth.get_current_owner(authorization="Bearer    ")
    assert exc.value.status_code == 401


def test_invalid_token_is_401(monkeypatch):
    def boom(token):
        raise ValueError("invalid token")

    monkeypatch.setattr(auth, "verify_token", boom)
    with pytest.raises(HTTPException) as exc:
        auth.get_current_owner(authorization="Bearer bad-token")
    assert exc.value.status_code == 401


def test_non_uuid_subject_is_401(monkeypatch):
    monkeypatch.setattr(auth, "verify_token", lambda token: "not-a-uuid")
    with pytest.raises(HTTPException) as exc:
        auth.get_current_owner(authorization="Bearer good-token")
    assert exc.value.status_code == 401
