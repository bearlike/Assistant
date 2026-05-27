"""Shared fixtures for the Mewbo API route-test suite.

Lifts the copy-pasted Flask-test-client + master-token auth-header pattern
(per-file ``_auth()`` helpers / inline ``{"X-API-KEY": ...}`` dicts) into one
canonical home so route tests consume fixtures instead of redefining them.
"""

import pytest
from mewbo_api import backend


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Master-token auth header accepted by every API-key-gated route."""
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


@pytest.fixture()
def client():
    """A Flask test client bound to the API app."""
    return backend.app.test_client()
