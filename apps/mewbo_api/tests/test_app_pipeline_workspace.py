"""Which directory a code pipeline's ``ctx`` is scoped to.

``_resolve_app_workspace_cwd`` had no test at all, which is part of why its
fallback could be the wrong directory indefinitely: the failure it produced was
a run that read nothing and reported success, so nothing downstream complained.

The rule under test: a resolvable project wins, and everything else falls back
to the app's STAGING directory rather than the maintainer's session temp dir.

Stubs: ``_resolve_session_cwd`` — the transcript/catalog read is the I/O
boundary here, and the branch being pinned is what happens with and without an
answer from it.
"""

# mypy: ignore-errors

from datetime import datetime, timezone

import pytest
from mewbo_api import backend
from mewbo_api.apps.models import AppFrontend, AppSpec, WorkspaceRef

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
MAINTAINER = "maintainer-session-1"


def _app(app_id="app-demo000001", *, maintainer=MAINTAINER):
    return AppSpec(
        app_id=app_id,
        title="Demo",
        owner_session_id="owner-session-1",
        maintainer_session_id=maintainer,
        workspace_ref=WorkspaceRef(kind="own", key=app_id),
        frontend=AppFrontend(entrypoint="app.py", files={"app.py": "x = 1\n"}),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def apps_root(tmp_path, monkeypatch):
    """Pin the staging root so nothing touches the real ``/tmp/mewbo/apps``."""
    root = tmp_path / "apps"
    monkeypatch.setenv("MEWBO_APPS_ROOT", str(root))
    return root


def test_falls_back_to_the_apps_staging_directory(apps_root, monkeypatch):
    """No resolvable project ⇒ the app's staging dir, NOT the session temp dir.

    This is the case that shipped broken. A two-stage app's agentic capture
    writes into the staging directory — the place `get_app`/`stage`
    materializes and the only place the app's files demonstrably are — while the
    code pipeline read the session temp dir, which nothing populates. Every glob
    matched nothing and the run closed `succeeded`.
    """
    monkeypatch.setattr(backend, "_resolve_session_cwd", lambda _s: None)

    resolved = backend._resolve_app_workspace_cwd(_app())

    assert resolved == str(apps_root / MAINTAINER / "app-demo000001")
    # The specific wrong answer this replaced, named so a regression is obvious.
    assert "/sessions/" not in resolved


def test_a_resolvable_project_still_wins(apps_root, monkeypatch):
    """A `shared` app anchored to a real project keeps that project's cwd."""
    monkeypatch.setattr(backend, "_resolve_session_cwd", lambda _s: "/projects/beacon")

    assert backend._resolve_app_workspace_cwd(_app()) == "/projects/beacon"


def test_a_draft_with_no_maintainer_resolves_nothing(apps_root, monkeypatch):
    """No maintainer ⇒ ``None``: the runner treats the workspace as empty.

    Deliberately not a staging path — a still-building draft has no session to
    scope one to, and inventing a directory would reach outside that scope.
    """
    monkeypatch.setattr(backend, "_resolve_session_cwd", lambda _s: None)

    assert backend._resolve_app_workspace_cwd(_app(maintainer=None)) is None


def test_the_directory_is_not_created_or_materialized(apps_root, monkeypatch):
    """Resolving names a path; it never writes one.

    Materializing the bundle per run would overwrite a freshly captured file
    with the older copy stored in the manifest — which is precisely the data a
    two-stage pipeline exists to refresh. A missing directory globs empty, which
    is the honest degradation.
    """
    monkeypatch.setattr(backend, "_resolve_session_cwd", lambda _s: None)

    resolved = backend._resolve_app_workspace_cwd(_app())

    from pathlib import Path

    assert not Path(resolved).exists()
