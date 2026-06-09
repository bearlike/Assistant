"""Route tests for GET /v1/wiki/projects/<slug>/source — cited-sources excerpt.

Mocks only the on-disk clone boundary (``resolve_qa_clone_dir``); the route, the
``WikiSourceAccess`` path-safety/decode reuse, and the JSON wire shape are all
exercised through the real Flask test client.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

API_KEY = "test-key-123"


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _seed_project(store, slug: str = "git.hurricane.home/bearlike/Assistant"):
    from mewbo_graph.wiki.types import IndexingJob, Project

    proj = Project(
        slug=slug,
        source="gitea",
        lang="en",
        indexed_at="2026-01-01T00:00:00Z",
        pages=1,
        desc="Test repo",
    )
    store.create_project(proj)
    # A completed job is what ``resolve_qa_clone_dir`` walks to find the on-disk
    # clone — seed one so the route resolves the clone the same way production
    # does (via ``_clone_dir_for``), instead of stubbing ``resolve_qa_clone_dir``
    # itself (that name is also bound in ``source_tools`` and a monkeypatch of it
    # leaks across files — the classic full-suite ordering failure).
    store.create_job(
        IndexingJob(
            job_id="job-source-test",  # slash-free: list_jobs keys jobs by dir name
            slug=slug,
            status="complete",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
    )
    return proj


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def clone_dir(tmp_path: Path):
    """A fake on-disk clone with a single nested source file."""
    root = tmp_path / "clone"
    (root / "src").mkdir(parents=True)
    body = "\n".join(f"line {i}" for i in range(1, 11))  # 10 lines, no trailing NL
    (root / "src" / "main.py").write_text(body, encoding="utf-8")
    # A secret OUTSIDE the clone root, to prove traversal is blocked.
    (tmp_path / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return root


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    return rt


@pytest.fixture()
def wiki_app(monkeypatch, store, runtime_stub, clone_dir):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    # Stub only the lowest I/O seam — the job-id → on-disk path map — so the
    # production ``resolve_qa_clone_dir`` still runs (walks the seeded completed
    # job) but lands on our fake clone. ``_clone_dir_for`` has a single binding
    # (module-local lookup inside ``_ctx``), so monkeypatch restores it cleanly;
    # patching ``resolve_qa_clone_dir`` would leak into ``source_tools``.
    monkeypatch.setattr(
        "mewbo_graph.plugins.wiki._ctx._clone_dir_for",
        lambda job_id: clone_dir,
    )

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app, store

    routes_mod._runtime = None


@pytest.fixture()
def client(wiki_app):
    flask_app, store = wiki_app
    return flask_app.test_client(), store


SLUG = "git.hurricane.home/bearlike/Assistant"
SLUG_ENC = "git.hurricane.home%2Fbearlike%2FAssistant"


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_source_requires_auth(client):
    c, _ = client
    resp = c.get(f"/v1/wiki/projects/{SLUG_ENC}/source?path=src/main.py")
    assert resp.status_code == 401


def test_source_valid_range(client, store):
    """Valid path + 1-based range returns the slice + correct line metadata."""
    c, _ = client
    _seed_project(store, slug=SLUG)

    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source?path=src/main.py&start=3&end=5",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["path"] == "src/main.py"
    assert data["startLine"] == 3
    assert data["endLine"] == 5
    assert data["totalLines"] == 10
    assert data["content"] == "line 3\nline 4\nline 5"


def test_source_whole_file_nulls_range(client, store):
    """Omitting start/end returns the whole file with null start/endLine."""
    c, _ = client
    _seed_project(store, slug=SLUG)

    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source?path=src/main.py",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["startLine"] is None
    assert data["endLine"] is None
    assert data["totalLines"] == 10
    assert data["content"].splitlines() == [f"line {i}" for i in range(1, 11)]


def test_source_path_traversal_rejected(client, store):
    """A ``../../`` path escaping the clone root is forbidden (no arbitrary read)."""
    c, _ = client
    _seed_project(store, slug=SLUG)

    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source?path=../secret.txt",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["code"] == "forbidden"
    # The secret content must never leak into the body.
    assert "TOP SECRET" not in resp.get_data(as_text=True)


def test_source_absolute_path_rejected(client, store):
    """An absolute path is rejected the same way as a traversal."""
    c, _ = client
    _seed_project(store, slug=SLUG)

    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source?path=/etc/passwd",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "forbidden"


def test_source_unknown_slug_404(client):
    """Unknown project slug → 404 (no clone lookup attempted)."""
    c, _ = client
    resp = c.get(
        "/v1/wiki/projects/no%2Fsuch%2Frepo/source?path=src/main.py",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"


def test_source_missing_path_param_400(client, store):
    c, _ = client
    _seed_project(store, slug=SLUG)
    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


def test_source_file_not_found_404(client, store):
    c, _ = client
    _seed_project(store, slug=SLUG)
    resp = c.get(
        f"/v1/wiki/projects/{SLUG_ENC}/source?path=src/nope.py",
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "not_found"
