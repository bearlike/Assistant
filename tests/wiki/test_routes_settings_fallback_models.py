"""Backend half plus the refresh round-trip: the per-project fallback ladder on
``ProjectSettingsPatch`` / ``ProjectSettings``.

``fallback_models: list[str] | None`` is a PINNED cross-package contract —
same name/type on ``WizardSubmission`` (mewbo_graph), ``ProjectSettings``
(mewbo_graph, landed by the graph agent), and ``ProjectSettingsPatch``
(this file). No shape translation at any hop, so a value set via PATCH here
round-trips through ``ProjectSettings.model_copy`` without any dict/model
conversion — unlike an earlier nested-shape draft, which would have needed one.

New file (not appended to the existing ``tests/wiki/test_routes_settings.py``)
so the pre-existing suite for that route stays untouched. Route-level, using
the same ``client``/``store`` fixture shape as its sibling file.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

API_KEY = "test-key-123"
SLUG = "org/repo"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-stub"
    rt.start_async.return_value = True
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, store, runtime_stub):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app.test_client(), store

    routes_mod._runtime = None


def _seed_git_project(store, slug: str = SLUG):
    from mewbo_graph.wiki.types import Project

    store.create_project(Project(
        slug=slug, source="github", lang="en",
        indexedAt="2026-01-01T00:00:00Z", pages=1, desc="x",
        repoUrl=f"https://github.com/{slug}",
    ))


def _headers():
    return {"X-Api-Key": API_KEY}


def test_fallback_models_is_editable_for_a_git_project(client, store):
    c, st = client
    _seed_git_project(st)
    resp = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["editable"]["fallbackModels"] is True
    # No override yet on a freshly onboarded project.
    assert body["fallbackModels"] is None


def test_fallback_models_not_editable_for_a_catalog_project(client, store):
    from mewbo_graph.wiki.types import Project

    c, st = client
    st.create_project(Project(
        slug=SLUG, source="git", lang="en", indexedAt="2026-01-01T00:00:00Z",
        pages=1, desc="a catalog", repoUrl=None,
    ))
    resp = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "catalog"
    assert body["editable"]["fallbackModels"] is False


def test_patch_rejects_fallback_models_on_a_catalog_project_410(client, store):
    from mewbo_graph.wiki.types import Project

    c, st = client
    st.create_project(Project(
        slug=SLUG, source="git", lang="en", indexedAt="2026-01-01T00:00:00Z",
        pages=1, desc="a catalog", repoUrl=None,
    ))
    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4"]},
        headers=_headers(),
    )
    assert resp.status_code == 410


def test_patch_sets_and_reads_back_the_ladder(client, store):
    c, st = client
    _seed_git_project(st)
    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4", "gemini-2.5-pro"]},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["fallbackModels"] == ["openai/gpt-5.4", "gemini-2.5-pro"]

    # Persisted, not just echoed — a fresh GET agrees.
    follow_up = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers())
    assert follow_up.get_json()["fallbackModels"] == ["openai/gpt-5.4", "gemini-2.5-pro"]


def test_patch_blank_ladder_entries_are_dropped(client, store):
    c, st = client
    _seed_git_project(st)
    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4", "  ", ""]},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["fallbackModels"] == ["openai/gpt-5.4"]


def test_patch_omitted_ladder_leaves_existing_value_untouched(client, store):
    c, st = client
    _seed_git_project(st)
    c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4"]},
        headers=_headers(),
    )
    # A later patch that doesn't mention fallbackModels at all must not clear it.
    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"desc": "new desc"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.get_json()["fallbackModels"] == ["openai/gpt-5.4"]


def test_patch_explicit_null_clears_the_ladder(client, store):
    c, st = client
    _seed_git_project(st)
    c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4"]},
        headers=_headers(),
    )
    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"fallbackModels": None}, headers=_headers())
    assert resp.status_code == 200
    assert resp.get_json()["fallbackModels"] is None


# ── The ladder survives a refresh (settings record → reconstructed
# submission → WikiIndexingJob.start → runtime.start_async) ──────────────────


def test_fallback_models_survives_a_refresh(client, store, runtime_stub):
    """PATCHing a ladder onto the settings record, then refreshing the project,
    must thread that SAME ladder into the new job's indexer session — the
    concrete regression this closes (refresh reconstructs its
    submission from ProjectSettings.to_submission(), which previously had
    nowhere to carry a ladder from)."""
    from mewbo_api.wiki.jobs import WikiIndexingJob

    c, st = client
    _seed_git_project(st)
    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"fallbackModels": ["openai/gpt-5.4", "gemini-2.5-pro"]},
        headers=_headers(),
    )
    assert resp.status_code == 200

    WikiIndexingJob.refresh(SLUG, runtime=runtime_stub, hook_manager=None)

    kw = runtime_stub.start_async.call_args.kwargs
    assert kw["fallback_models"] == ("openai/gpt-5.4", "gemini-2.5-pro")
