"""Route tests for /v1/wiki/* — Flask test client coverage.

Uses a temp JsonWikiStore and a stub runtime so no real DB is needed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────


def _seed_project(store, slug: str = "org/repo", indexed_at: str = "2026-01-01T00:00:00Z"):
    from mewbo_api.wiki.types import Project

    proj = Project(
        slug=slug,
        source="github",
        lang="en",
        indexed_at=indexed_at,
        pages=5,
        desc="Test repo",
    )
    store.create_project(proj)
    return proj


def _seed_page(store, slug: str = "org/repo", page_id: str = "overview"):
    from mewbo_api.wiki.types import Frontmatter, NavEntry, TocEntry, WikiPage

    page = WikiPage(
        id=page_id,
        title="Overview",
        frontmatter=Frontmatter(title="Overview", slug=page_id),
        body="# Overview\n\nContent.",
        toc=[TocEntry(id=page_id, label="Overview", lvl=1)],
        nav=[NavEntry(id=page_id, label="Overview", lvl=1)],
    )
    store.save_page(slug, page)
    return page


def _seed_job(store, job_id: str = "job-001", slug: str = "org/repo"):
    from mewbo_api.wiki.types import IndexingJob

    job = IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=10,
        current_file=None,
    )
    store.create_job(job)
    return job


def _seed_qa(store, answer_id: str = "ans-001"):
    from mewbo_api.wiki.types import QaAnswer

    ans = QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=["src/main.py"],
        model="anthropic/claude-sonnet-4-5",
        blocks=[],
    )
    store.save_qa(ans)
    return ans


# ── Fixtures ───────────────────────────────────────────────────────────────────

API_KEY = "test-key-123"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_api.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime_stub(store):
    """Full runtime stub for routes that call WikiIndexingJob.start/cancel."""
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-stub"
    rt.start_async.return_value = True
    rt.cancel.return_value = True
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def wiki_app(tmp_path: Path, monkeypatch, store, runtime_stub):
    """Flask test app with wiki routes mounted and a temp JsonWikiStore."""
    # backend reads MASTER_API_TOKEN at import time; if another test imported it
    # earlier in the run, setenv is too late. Force the resolved attribute so
    # auth works regardless of collection/import order.
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app, store

    # Reset the module-level _runtime so later tests that import routes
    # don't see the MagicMock stub (e.g. test_tool_finalize._get_submission).
    routes_mod._runtime = None


@pytest.fixture()
def client(wiki_app):
    flask_app, store = wiki_app
    return flask_app.test_client(), store


@pytest.fixture()
def valid_submission():
    """A dict matching WizardSubmission wire shape."""
    return {
        "repoUrl": "https://github.com/bearlike/Assistant",
        "slug": "bearlike/Assistant",
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    }


# ── Auth tests ─────────────────────────────────────────────────────────────────


PROTECTED_ENDPOINTS = [
    ("GET", "/v1/wiki/projects"),
    ("DELETE", "/v1/wiki/projects/org%2Frepo"),
    ("GET", "/v1/wiki/projects/org%2Frepo/pages/overview"),
    ("GET", "/v1/wiki/platforms"),
    ("GET", "/v1/wiki/languages"),
    ("GET", "/v1/wiki/index/job-001"),
    ("GET", "/v1/wiki/qa/ans-001"),
]


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_auth_required(client, method, path):
    """Every endpoint returns 401 without X-Api-Key."""
    c, _ = client
    resp = c.open(path, method=method)
    assert resp.status_code == 401


# ── Projects ───────────────────────────────────────────────────────────────────


def test_list_projects_empty(client):
    """GET /v1/wiki/projects with empty store → 200 []."""
    c, _ = client
    resp = c.get("/v1/wiki/projects", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_projects(client):
    """Seed 2 projects → returns them sorted by indexed_at desc with camelCase keys."""
    c, store = client
    _seed_project(store, slug="org/a", indexed_at="2026-01-01T00:00:00Z")
    _seed_project(store, slug="org/b", indexed_at="2026-06-01T00:00:00Z")

    resp = c.get("/v1/wiki/projects", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 2
    # Sorted desc by indexedAt — org/b is newer
    assert data[0]["slug"] == "org/b"
    assert data[1]["slug"] == "org/a"
    # camelCase wire keys present
    assert "indexedAt" in data[0]


def test_get_page_ok(client):
    """Seed a WikiPage → GET returns 200 with full shape."""
    c, store = client
    _seed_project(store)
    _seed_page(store, slug="org/repo", page_id="overview")

    resp = c.get(
        "/v1/wiki/projects/org%2Frepo/pages/overview",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == "overview"
    assert data["title"] == "Overview"
    assert "frontmatter" in data
    assert "body" in data
    assert "toc" in data
    assert "nav" in data


def test_get_page_not_found(client):
    """404 with WikiError {code: 'not_found'} for absent page."""
    c, store = client
    _seed_project(store)

    resp = c.get(
        "/v1/wiki/projects/org%2Frepo/pages/missing",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["code"] == "not_found"


def test_delete_project_idempotent(client):
    """First DELETE → {deleted: true}; second → {deleted: false}."""
    c, store = client
    _seed_project(store)

    resp1 = c.delete("/v1/wiki/projects/org%2Frepo", headers={"X-Api-Key": API_KEY})
    assert resp1.status_code == 200
    assert resp1.get_json()["deleted"] is True

    resp2 = c.delete("/v1/wiki/projects/org%2Frepo", headers={"X-Api-Key": API_KEY})
    assert resp2.status_code == 200
    assert resp2.get_json()["deleted"] is False


# ── Catalogues ─────────────────────────────────────────────────────────────────


def test_platforms(client):
    """GET /v1/wiki/platforms → 200 with non-empty list, camelCase keys."""
    c, _ = client
    resp = c.get("/v1/wiki/platforms", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 6
    platform = data[0]
    assert "tokenLabel" in platform
    assert "tokenSteps" in platform
    assert "tokenScope" in platform
    assert "id" in platform
    assert "name" in platform
    assert "mono" in platform
    assert "color" in platform


def test_languages(client):
    """GET /v1/wiki/languages → 200 with non-empty list."""
    c, _ = client
    resp = c.get("/v1/wiki/languages", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 13
    lang = data[0]
    assert "id" in lang
    assert "label" in lang


def test_models_endpoint_removed(client):
    """/v1/wiki/models no longer exists — picker uses shared /api/models (DRY)."""
    c, _ = client
    resp = c.get("/v1/wiki/models", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 404


def test_platforms_and_languages_present(client):
    """Catalogues return 200 with camelCase keys matching frontend fixtures."""
    c, _ = client
    for path in ["/v1/wiki/platforms", "/v1/wiki/languages"]:
        resp = c.get(path, headers={"X-Api-Key": API_KEY})
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) > 0


# ── Indexing job snapshots ──────────────────────────────────────────────────────


def test_get_indexing_job_snapshot(client):
    """Seed an IndexingJob → GET returns 200 with camelCase keys; 404 for unknown id."""
    c, store = client
    _seed_job(store, job_id="job-001")

    resp = c.get("/v1/wiki/index/job-001", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["jobId"] == "job-001"
    assert data["status"] == "queued"
    assert "scannedCount" in data
    assert "totalCount" in data

    resp404 = c.get("/v1/wiki/index/no-such-job", headers={"X-Api-Key": API_KEY})
    assert resp404.status_code == 404
    assert resp404.get_json()["code"] == "not_found"


def test_get_qa_snapshot(client):
    """Seed a QaAnswer → GET returns 200; 404 for unknown id."""
    c, store = client
    _seed_qa(store, answer_id="ans-001")

    resp = c.get("/v1/wiki/qa/ans-001", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["answerId"] == "ans-001"

    resp404 = c.get("/v1/wiki/qa/no-such-ans", headers={"X-Api-Key": API_KEY})
    assert resp404.status_code == 404
    assert resp404.get_json()["code"] == "not_found"


# ── Slug with slashes ──────────────────────────────────────────────────────────


def test_invalid_slug_404(client):
    """GET page with percent-encoded slug — routes correctly; 404 for absent slug."""
    c, store = client
    # org/repo encoded as org%2Frepo — should reach the page handler, not 404 on routing
    resp = c.get(
        "/v1/wiki/projects/org%2Frepo/pages/does-not-exist",
        headers={"X-Api-Key": API_KEY},
    )
    # 404 from the store (page missing), NOT a Flask routing 404
    assert resp.status_code == 404
    data = resp.get_json()
    # Should be a WikiError, not a generic Flask 404
    assert "code" in data
    assert data["code"] == "not_found"


# ── Indexing POST/DELETE/stream ─────────────────────────────────────────────


def test_post_index_returns_queued_snapshot(client, store, runtime_stub, valid_submission):
    """POST /v1/wiki/index creates a job, returns IndexingJob with status=queued."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/index",
        json=valid_submission,
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"
    assert body["jobId"]
    assert body["slug"] == valid_submission["slug"]
    # job persisted
    assert store.get_job(body["jobId"]) is not None


def test_post_index_invalid_submission_returns_400(client):
    """Missing required fields → 400 with WikiError validation."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/index",
        json={"slug": "x/y"},  # missing nearly everything
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_delete_index_idempotent(client, store, runtime_stub, valid_submission):
    """First DELETE cancels; second is a no-op but still 200."""
    c, _ = client
    create = c.post("/v1/wiki/index", json=valid_submission, headers={"X-Api-Key": API_KEY})
    job_id = create.get_json()["jobId"]
    # First cancel
    resp = c.delete(f"/v1/wiki/index/{job_id}", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "cancelled"
    # Second cancel — still 200, still cancelled
    resp = c.delete(f"/v1/wiki/index/{job_id}", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 200


def test_delete_unknown_index_returns_404(client):
    c, _ = client
    resp = c.delete("/v1/wiki/index/nope", headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 404


# ── Refresh endpoint ────────────────────────────────────────────────────────


def test_refresh_creates_new_job(client, store, runtime_stub, valid_submission):
    """Pre-seed a project + a prior job → POST refresh → {queued: true}; new job created."""
    c, _ = client
    # Seed the project first
    _seed_project(store, slug="bearlike/Assistant")
    # Seed a prior job with a submission
    _seed_job(store, job_id="prior-job", slug="bearlike/Assistant")
    store.save_job_submission("prior-job", {
        "repoUrl": "https://github.com/bearlike/Assistant",
        "slug": "bearlike/Assistant",
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    })

    resp = c.post(
        "/v1/wiki/projects/bearlike%2FAssistant/refresh",
        json={},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"queued": True}
    # A new job should have been created (start_async called)
    runtime_stub.start_async.assert_called()


def test_refresh_unknown_slug_returns_404(client):
    """POST refresh for missing project → 404."""
    c, _ = client
    resp = c.post(
        "/v1/wiki/projects/no%2Fsuch/refresh",
        json={},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


# ── Validation hardening + rate-limit ──────────────────────────────────────────


def test_post_index_validation_populates_fields(client):
    """POST /v1/wiki/index with invalid fields → 400 with 'fields' map populated."""
    c, _ = client
    # Test with a missing required field to guarantee a validation failure
    # (Pydantic doesn't validate URL format, so use a truly missing field).
    resp2 = c.post(
        "/v1/wiki/index",
        json={"slug": "x/y"},  # missing repoUrl, platform, etc.
        headers={"X-Api-Key": API_KEY},
    )
    assert resp2.status_code == 400
    body = resp2.get_json()
    assert body["code"] == "validation"
    # 'fields' should be populated with the failing field names
    assert body.get("fields") is not None
    fields = body["fields"]
    assert isinstance(fields, dict)
    assert len(fields) > 0


def test_post_index_rate_limit(client, monkeypatch):
    """POST /index hits rate limit → 429 with Retry-After: 60."""
    import mewbo_api.wiki.routes as routes_mod

    c, _ = client

    # Reset the shared counter so we start fresh for this test.
    routes_mod._rate_limit_counters.clear()

    # Patch the rate limit ceiling to 3 so we can trigger it cheaply.
    monkeypatch.setattr(routes_mod, "_get_rate_limit", lambda: 3)

    valid = {
        "repoUrl": "https://github.com/bearlike/Assistant",
        "slug": "bearlike/Assistant",
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    }

    # First 3 requests should succeed (202)
    for _ in range(3):
        resp = c.post("/v1/wiki/index", json=valid, headers={"X-Api-Key": API_KEY})
        assert resp.status_code == 202, f"Expected 202 but got {resp.status_code}"

    # 4th request should be rate-limited
    resp = c.post("/v1/wiki/index", json=valid, headers={"X-Api-Key": API_KEY})
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["code"] == "rate_limited"
    assert "Retry-After" in resp.headers
    assert resp.headers["Retry-After"] == "60.0"
