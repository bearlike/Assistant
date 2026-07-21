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


@pytest.fixture(autouse=True)
def _reset_schedule_trigger_provider():
    """Reset the process-wide schedule_trigger provider between tests.

    Mirror of the fixture in the root ``tests/conftest.py`` (which does not
    reach this directory): importing ``backend`` above already ran
    ``init_triggers`` and set the process-wide provider, so without a reset
    ``schedule_trigger`` leaks into every un-scoped session this suite builds
    and reorders events under random collection order (bit
    ``test_commands_api`` first). A test that wants the provider registers it
    in its own body.
    """
    from mewbo_core.triggers import session_tool as _st

    saved = _st._TRIGGER_TOOL_PROVIDER
    _st._TRIGGER_TOOL_PROVIDER = None
    yield
    _st._TRIGGER_TOOL_PROVIDER = saved
