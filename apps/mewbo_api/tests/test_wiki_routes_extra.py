"""Extra contract tests for apps/mewbo_api/src/mewbo_api/wiki/routes.py.

Exercises uncovered branches:
- /v1/wiki/defaults endpoint (all combos of configured / unconfigured keys)
- /v1/wiki/projects/<slug>/graph (200 with KG, 404 for unknown slug, node_limit param)
- /v1/wiki/jobs/active (hydration, empty)
- /v1/wiki/index/<id>/stream (SSE primer emitted, 404 for unknown job, Last-Event-ID resume)
- /v1/wiki/qa (validation, internal error)
- /v1/wiki/qa/<id>/stream (200, 404, Last-Event-ID)
- /v1/wiki/qa/<id>/delete (200 cancel, 404)
- /v1/wiki/projects/<slug>/insights (201/200/400 paths, project not found)
- _hydrate_platform (missing fields backfilled from submission, no-op when complete)
- _pydantic_fields (extracts loc→msg from Pydantic ValidationError)
- _require_auth fallback path (no backend importable → env var check)
- _get_rate_limit falls back to default on exception
- _check_rate_limit increments correctly
- auth required on every new endpoint
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/wiki/test_routes.py)
# ---------------------------------------------------------------------------

API_KEY = "test-wiki-key-extra"


def _seed_project(store, slug: str = "org/repo", indexed_at: str = "2026-01-01T00:00:00Z"):
    from mewbo_graph.wiki.types import Project

    proj = Project(
        slug=slug,
        source="github",
        lang="en",
        indexed_at=indexed_at,
        pages=3,
        desc="Extra test repo",
    )
    store.create_project(proj)
    return proj


def _seed_page(store, slug: str = "org/repo", page_id: str = "overview"):
    from mewbo_graph.wiki.types import Frontmatter, NavEntry, TocEntry, WikiPage

    page = WikiPage(
        id=page_id,
        title="Overview",
        frontmatter=Frontmatter(title="Overview", slug=page_id),
        body="# Overview\n\nContent here.",
        toc=[TocEntry(id=page_id, label="Overview", lvl=1)],
        nav=[NavEntry(id=page_id, label="Overview", lvl=1)],
    )
    store.save_page(slug, page)
    return page


def _seed_job(store, job_id: str = "job-ext-001", slug: str = "org/repo", status: str = "queued"):
    from mewbo_graph.wiki.types import IndexingJob

    job = IndexingJob(
        job_id=job_id,
        slug=slug,
        status=status,
        scanned_count=0,
        total_count=5,
        current_file=None,
    )
    store.create_job(job)
    return job


def _seed_qa(store, answer_id: str = "ans-ext-001"):
    from mewbo_graph.wiki.types import QaAnswer

    ans = QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=["src/main.py"],
        model="anthropic/claude-sonnet-4-6",
        blocks=[],
    )
    store.save_qa(ans)
    return ans


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki-extra")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-ext-stub"
    rt.start_async.return_value = True
    rt.cancel.return_value = True
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def wiki_app(tmp_path: Path, monkeypatch, store, runtime_stub):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    flask_app = Flask(__name__ + "_extra")
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app, store, runtime_stub

    routes_mod._runtime = None


@pytest.fixture()
def client(wiki_app):
    flask_app, store, runtime_stub = wiki_app
    return flask_app.test_client(), store, runtime_stub


def _h() -> dict:
    return {"X-Api-Key": API_KEY}


# ---------------------------------------------------------------------------
# Auth guard: new endpoints
# ---------------------------------------------------------------------------


NEW_AUTH_ENDPOINTS = [
    ("GET", "/v1/wiki/defaults"),
    ("GET", "/v1/wiki/projects/org%2Frepo/graph"),
    ("GET", "/v1/wiki/jobs/active"),
    ("POST", "/v1/wiki/qa"),
    ("DELETE", "/v1/wiki/qa/ans-ext-001"),
    ("POST", "/v1/wiki/qa/ans-ext-001/stream"),
    ("POST", "/v1/wiki/projects/org%2Frepo/insights"),
]


@pytest.mark.parametrize("method,path", NEW_AUTH_ENDPOINTS)
def test_new_endpoint_auth_required(client, method, path):
    c, _, _ = client
    resp = c.open(path, method=method)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /v1/wiki/defaults
# ---------------------------------------------------------------------------


class TestWikiDefaults:
    def test_defaults_empty_when_nothing_configured(self, client) -> None:
        c, _, _ = client
        with patch("mewbo_core.config.get_config_value", return_value=""):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_defaults_model_set(self, client) -> None:
        c, _, _ = client

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_model"):
                return "anthropic/claude-sonnet-4-6"
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_fake_get):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        data = resp.get_json()
        assert data.get("model") == "anthropic/claude-sonnet-4-6"

    def test_defaults_depth_comprehensive(self, client) -> None:
        c, _, _ = client

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_depth"):
                return "comprehensive"
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_fake_get):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        data = resp.get_json()
        assert data.get("depth") == "comprehensive"

    def test_defaults_invalid_depth_omitted(self, client) -> None:
        c, _, _ = client

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_depth"):
                return "invalid-depth"
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_fake_get):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        data = resp.get_json()
        assert "depth" not in data

    def test_defaults_qa_model_falls_back_to_model(self, client) -> None:
        c, _, _ = client

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_model"):
                return "my-model"
            if keys == ("wiki", "default_qa_model"):
                return ""  # not configured
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_fake_get):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        data = resp.get_json()
        assert data.get("model") == "my-model"
        assert data.get("qaModel") == "my-model"

    def test_defaults_qa_model_separate(self, client) -> None:
        c, _, _ = client

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_model"):
                return "indexer-model"
            if keys == ("wiki", "default_qa_model"):
                return "qa-model"
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_fake_get):
            resp = c.get("/v1/wiki/defaults", headers=_h())
        data = resp.get_json()
        assert data["model"] == "indexer-model"
        assert data["qaModel"] == "qa-model"


# ---------------------------------------------------------------------------
# /v1/wiki/projects/<slug>/graph
# ---------------------------------------------------------------------------


class TestProjectGraph:
    def test_graph_404_for_unknown_project(self, client) -> None:
        c, _, _ = client
        resp = c.get("/v1/wiki/projects/no%2Fsuch/graph", headers=_h())
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "not_found"

    def test_graph_200_with_empty_graph(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        resp = c.get("/v1/wiki/projects/org%2Frepo/graph", headers=_h())
        assert resp.status_code == 200
        data = resp.get_json()
        # Cytoscape wire shape: {nodes: [...], edges: [...]}
        assert "nodes" in data
        assert "edges" in data

    def test_graph_node_limit_param(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        resp = c.get("/v1/wiki/projects/org%2Frepo/graph?limit=10", headers=_h())
        assert resp.status_code == 200

    def test_graph_invalid_limit_param_ignored(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        resp = c.get("/v1/wiki/projects/org%2Frepo/graph?limit=notanumber", headers=_h())
        assert resp.status_code == 200  # bad limit silently treated as None


# ---------------------------------------------------------------------------
# /v1/wiki/jobs/active
# ---------------------------------------------------------------------------


class TestActiveJobs:
    def test_active_jobs_empty(self, client) -> None:
        c, _, _ = client
        resp = c.get("/v1/wiki/jobs/active", headers=_h())
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_active_jobs_returns_non_terminal(self, client) -> None:
        c, store, _ = client
        _seed_job(store, "j-queue", "org/repo", "queued")
        _seed_job(store, "j-scan", "org/repo", "scanning")
        _seed_job(store, "j-final", "org/repo", "finalizing")
        _seed_job(store, "j-done", "org/repo", "complete")  # terminal → excluded

        resp = c.get("/v1/wiki/jobs/active", headers=_h())
        assert resp.status_code == 200
        data = resp.get_json()
        job_ids = {j["jobId"] for j in data}
        assert "j-queue" in job_ids
        assert "j-scan" in job_ids
        assert "j-final" in job_ids
        assert "j-done" not in job_ids

    def test_active_jobs_hydrates_platform(self, client) -> None:
        """Jobs missing platform get it backfilled from submission."""
        c, store, _ = client
        _seed_job(store, "j-hydrate", "org/repo", "queued")
        store.save_job_submission(
            "j-hydrate",
            {
                "repoUrl": "https://github.com/org/repo",
                "slug": "org/repo",
                "platform": "github",
                "model": "anthropic/claude-sonnet-4-6",
            },
        )
        resp = c.get("/v1/wiki/jobs/active", headers=_h())
        assert resp.status_code == 200
        data = resp.get_json()
        j = next((x for x in data if x["jobId"] == "j-hydrate"), None)
        assert j is not None
        assert j.get("platform") == "github"
        assert j.get("model") == "anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# /v1/wiki/index/<id>/stream
# ---------------------------------------------------------------------------


class TestIndexStream:
    def test_stream_404_unknown_job(self, client) -> None:
        c, _, _ = client
        resp = c.get("/v1/wiki/index/no-such-job/stream", headers=_h())
        assert resp.status_code == 404

    def test_stream_200_yields_sse_primer(self, client) -> None:
        c, store, _ = client
        _seed_job(store, "j-stream", "org/repo", "complete")
        # Terminate immediately — store has no events so generator exits after idle
        # Set a 0-cycle idle limit so the test doesn't hang

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.get("/v1/wiki/index/j-stream/stream", headers=_h())
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")
        # SSE primer is a 2KB padded comment frame
        body = resp.get_data(as_text=True)
        assert ":" in body  # SSE comment character

    def test_stream_last_event_id_header_used(self, client) -> None:
        c, store, _ = client
        _seed_job(store, "j-resume", "org/repo", "complete")

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.get(
                "/v1/wiki/index/j-resume/stream",
                headers={**_h(), "Last-Event-ID": "5"},
            )
        assert resp.status_code == 200

    def test_stream_after_idx_param_wins_over_header(self, client) -> None:
        c, store, _ = client
        _seed_job(store, "j-after", "org/repo", "complete")

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.get(
                "/v1/wiki/index/j-after/stream?after_idx=3",
                headers={**_h(), "Last-Event-ID": "10"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /v1/wiki/qa POST
# ---------------------------------------------------------------------------


class TestQaPost:
    def test_qa_validation_missing_fields(self, client) -> None:
        c, _, _ = client
        resp = c.post("/v1/wiki/qa", json={}, headers=_h())
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "validation"
        # Fields map populated with missing keys — model is NO LONGER required
        # (server defaults it), and the public param name is ``project``.
        assert "question" in data.get("fields", {})
        assert "model" not in data.get("fields", {})
        assert "project" in data.get("fields", {})
        assert "slug" not in data.get("fields", {})

    def test_qa_validation_message_names_project_not_slug(self, client) -> None:
        """The user-facing 400 speaks the PUBLIC param name (project)."""
        c, _, _ = client
        resp = c.post("/v1/wiki/qa", json={"question": "q?"}, headers=_h())
        assert resp.status_code == 400
        data = resp.get_json()
        assert "project" in data["message"]
        assert "slug" not in data["message"]

    def test_qa_model_optional_defaults_from_config(self, client) -> None:
        """No model in the body → server defaults it via _resolve_qa_model."""
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        def _fake_get(*keys, default=""):
            if keys == ("wiki", "default_qa_model"):
                return "cfg-qa-model"
            return default

        fake_answer = MagicMock()
        fake_answer.answer_id = "ans-defaulted"

        with (
            patch("mewbo_core.config.get_config_value", side_effect=_fake_get),
            patch("mewbo_api.wiki.jobs.WikiQaSession.start", return_value=fake_answer) as start,
            patch.dict(
                os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}
            ),
        ):
            resp = c.post(
                "/v1/wiki/qa",
                json={"question": "what is it?", "project": "org/repo"},
                headers=_h(),
            )
        assert resp.status_code == 200
        assert start.call_args.kwargs["model"] == "cfg-qa-model"

    def test_qa_accepts_project_alias(self, client) -> None:
        """The body may use ``project`` (public) instead of ``slug`` (internal)."""
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        fake_answer = MagicMock()
        fake_answer.answer_id = "ans-alias"

        with (
            patch("mewbo_api.wiki.jobs.WikiQaSession.start", return_value=fake_answer) as start,
            patch.dict(
                os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}
            ),
        ):
            resp = c.post(
                "/v1/wiki/qa",
                json={"question": "q?", "project": "org/repo", "model": "m"},
                headers=_h(),
            )
        assert resp.status_code == 200
        # ``project`` maps to the internal ``slug`` plumbing unchanged.
        assert start.call_args.kwargs["slug"] == "org/repo"

    def test_resolve_qa_model_chain(self) -> None:
        """_resolve_qa_model honors qa → wiki.default → llm.default order."""
        from mewbo_api.wiki.routes import _resolve_qa_model

        def _qa(*keys, default=""):
            if keys == ("wiki", "default_qa_model"):
                return "qa"
            if keys == ("wiki", "default_model"):
                return "wiki"
            if keys == ("llm", "default_model"):
                return "llm"
            return default

        def _wiki_only(*keys, default=""):
            if keys == ("wiki", "default_model"):
                return "wiki"
            if keys == ("llm", "default_model"):
                return "llm"
            return default

        def _llm_only(*keys, default=""):
            if keys == ("llm", "default_model"):
                return "llm"
            return default

        with patch("mewbo_core.config.get_config_value", side_effect=_qa):
            assert _resolve_qa_model() == "qa"
        with patch("mewbo_core.config.get_config_value", side_effect=_wiki_only):
            assert _resolve_qa_model() == "wiki"
        with patch("mewbo_core.config.get_config_value", side_effect=_llm_only):
            assert _resolve_qa_model() == "llm"

    def test_qa_internal_error_returns_500(self, client) -> None:
        # Patch at the exact seam the route calls: WikiQaSession.start in routes.py.
        # The route catches any Exception and calls wiki_error_response with
        # code="internal", which maps to HTTP 500 (see wiki/errors.py).
        c, _, runtime_stub = client
        with patch(
            "mewbo_api.wiki.routes.WikiQaSession.start",
            side_effect=RuntimeError("boom"),
        ):
            resp = c.post(
                "/v1/wiki/qa",
                json={"question": "what?", "model": "m", "slug": "s/l"},
                headers=_h(),
            )
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["code"] == "internal"

    def test_qa_starts_sse_stream(self, client) -> None:
        """Happy path: WikiQaSession.start returns → SSE stream begins."""
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        fake_answer = MagicMock()
        fake_answer.answer_id = "ans-new-001"

        with patch("mewbo_api.wiki.jobs.WikiQaSession.start", return_value=fake_answer):
            with patch.dict(
                os.environ,
                {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"},
            ):
                resp = c.post(
                    "/v1/wiki/qa",
                    json={"question": "what is it?", "model": "some-model", "slug": "org/repo"},
                    headers=_h(),
                )
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")


# ---------------------------------------------------------------------------
# /v1/wiki/qa/<id> DELETE
# ---------------------------------------------------------------------------


class TestQaDelete:
    def test_delete_qa_404_unknown(self, client) -> None:
        c, _, _ = client
        resp = c.delete("/v1/wiki/qa/no-such-ans", headers=_h())
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "not_found"

    def test_delete_qa_cancels(self, client) -> None:
        c, store, _ = client
        _seed_qa(store, "ans-del-001")
        resp = c.delete("/v1/wiki/qa/ans-del-001", headers=_h())
        assert resp.status_code == 200
        data = resp.get_json()
        # QA cancelled → status should reflect that
        assert "answerId" in data


# ---------------------------------------------------------------------------
# /v1/wiki/qa/<id>/stream POST
# ---------------------------------------------------------------------------


class TestQaStream:
    def test_qa_stream_404_unknown(self, client) -> None:
        c, _, _ = client
        resp = c.post("/v1/wiki/qa/no-such-ans/stream", headers=_h())
        assert resp.status_code == 404

    def test_qa_stream_200_yields_primer(self, client) -> None:
        c, store, _ = client
        _seed_qa(store, "ans-stream-001")

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.post("/v1/wiki/qa/ans-stream-001/stream", headers=_h())
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")

    def test_qa_stream_last_event_id(self, client) -> None:
        c, store, _ = client
        _seed_qa(store, "ans-resume-001")

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.post(
                "/v1/wiki/qa/ans-resume-001/stream",
                headers={**_h(), "Last-Event-ID": "3"},
            )
        assert resp.status_code == 200

    def test_qa_stream_invalid_after_idx_defaults(self, client) -> None:
        c, store, _ = client
        _seed_qa(store, "ans-bad-001")

        with patch.dict(os.environ, {"MEWBO_WIKI_SSE_MAX_IDLE": "0", "MEWBO_WIKI_SSE_SLEEP": "0"}):
            resp = c.post(
                "/v1/wiki/qa/ans-bad-001/stream?after_idx=notanint",
                headers=_h(),
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /v1/wiki/projects/<slug>/insights
# ---------------------------------------------------------------------------


class TestInsights:
    def test_insights_404_unknown_project(self, client) -> None:
        c, _, _ = client
        resp = c.post(
            "/v1/wiki/projects/no%2Fsuch/insights",
            json={"content": "some claim"},
            headers=_h(),
        )
        assert resp.status_code == 404

    def test_insights_400_missing_content_and_raw(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")
        resp = c.post(
            "/v1/wiki/projects/org%2Frepo/insights",
            json={},
            headers=_h(),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "validation"

    def test_insights_400_invalid_kind(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")
        resp = c.post(
            "/v1/wiki/projects/org%2Frepo/insights",
            json={"content": "a claim", "kind": "unknown-kind"},
            headers=_h(),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "validation"
        assert "kind" in data.get("fields", {})

    def test_insights_201_on_successful_ingest(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        fake_result = MagicMock()
        fake_result.ok = True
        fake_result.model_dump.return_value = {"ok": True, "stored": 1, "rejected": 0}

        with patch("mewbo_api.wiki.routes._make_insight_llm", return_value=None):
            with patch(
                "mewbo_graph.wiki.memory.InsightIngestor.from_store",
            ) as mock_from_store:
                mock_ingestor = MagicMock()
                mock_ingestor.ingest.return_value = fake_result
                mock_from_store.return_value = mock_ingestor
                resp = c.post(
                    "/v1/wiki/projects/org%2Frepo/insights",
                    json={"content": "The loader is async-first."},
                    headers=_h(),
                )
        assert resp.status_code == 201

    def test_insights_200_on_rejected_ingest(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        fake_result = MagicMock()
        fake_result.ok = False
        fake_result.model_dump.return_value = {"ok": False, "stored": 0, "rejected": 1}

        with patch("mewbo_api.wiki.routes._make_insight_llm", return_value=None):
            with patch(
                "mewbo_graph.wiki.memory.InsightIngestor.from_store",
            ) as mock_from_store:
                mock_ingestor = MagicMock()
                mock_ingestor.ingest.return_value = fake_result
                mock_from_store.return_value = mock_ingestor
                resp = c.post(
                    "/v1/wiki/projects/org%2Frepo/insights",
                    json={"content": "duplicate claim"},
                    headers=_h(),
                )
        # 200 ok:false — not a client error
        assert resp.status_code == 200

    def test_insights_500_on_ingest_exception(self, client) -> None:
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        with patch("mewbo_api.wiki.routes._make_insight_llm", return_value=None):
            with patch(
                "mewbo_graph.wiki.memory.InsightIngestor.from_store",
            ) as mock_from_store:
                mock_ingestor = MagicMock()
                mock_ingestor.ingest.side_effect = RuntimeError("internal failure")
                mock_from_store.return_value = mock_ingestor
                resp = c.post(
                    "/v1/wiki/projects/org%2Frepo/insights",
                    json={"content": "something"},
                    headers=_h(),
                )
        assert resp.status_code == 500
        assert resp.get_json()["code"] == "internal"

    def test_insights_raw_path(self, client) -> None:
        """Passing raw= triggers condenser path (condenser=None when no LLM)."""
        c, store, _ = client
        _seed_project(store, slug="org/repo")

        fake_result = MagicMock()
        fake_result.ok = True
        fake_result.model_dump.return_value = {"ok": True}

        with patch("mewbo_api.wiki.routes._make_insight_llm", return_value=None):
            with patch(
                "mewbo_graph.wiki.memory.InsightIngestor.from_store",
            ) as mock_from_store:
                mock_ingestor = MagicMock()
                mock_ingestor.ingest.return_value = fake_result
                mock_from_store.return_value = mock_ingestor
                resp = c.post(
                    "/v1/wiki/projects/org%2Frepo/insights",
                    json={"raw": "Long free text to be condensed."},
                    headers=_h(),
                )
        assert resp.status_code == 201
        # condense flag should be True for raw path
        call_kwargs = mock_ingestor.ingest.call_args[1]
        assert call_kwargs.get("condense") is True


# ---------------------------------------------------------------------------
# _hydrate_platform
# ---------------------------------------------------------------------------


class TestHydratePlatform:
    def test_noop_when_all_fields_present(self, client) -> None:
        from mewbo_api.wiki.routes import _hydrate_platform
        from mewbo_graph.wiki.types import IndexingJob

        job = IndexingJob(
            job_id="j-full",
            slug="org/repo",
            status="queued",
            scanned_count=0,
            total_count=0,
            current_file=None,
            platform="github",
            host="github.com",
            model="anthropic/claude",
        )
        result = _hydrate_platform(job)
        assert result is job  # no copy made

    def test_backfills_platform_from_submission(self, wiki_app) -> None:
        flask_app, store, _ = wiki_app
        from mewbo_api.wiki.routes import _hydrate_platform
        from mewbo_graph.wiki.types import IndexingJob

        job = IndexingJob(
            job_id="j-bare",
            slug="org/repo",
            status="queued",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
        store.create_job(job)
        store.save_job_submission(
            "j-bare",
            {
                "platform": "gitea",
                "model": "anthropic/claude-3",
                "repoUrl": "https://git.example.com/org/repo",
            },
        )
        result = _hydrate_platform(job)
        assert result.platform == "gitea"
        assert result.model == "anthropic/claude-3"
        assert result.host == "git.example.com"

    def test_handles_missing_submission_gracefully(self, wiki_app) -> None:
        flask_app, store, _ = wiki_app
        from mewbo_api.wiki.routes import _hydrate_platform
        from mewbo_graph.wiki.types import IndexingJob

        job = IndexingJob(
            job_id="j-nosubmission",
            slug="org/repo",
            status="queued",
            scanned_count=0,
            total_count=0,
            current_file=None,
        )
        # No submission saved → returns job unchanged (no patch)
        result = _hydrate_platform(job)
        assert result.platform is None


# ---------------------------------------------------------------------------
# _pydantic_fields helper
# ---------------------------------------------------------------------------


def test_pydantic_fields_extracts_locations() -> None:
    from mewbo_api.wiki.routes import _pydantic_fields
    from mewbo_graph.wiki.types import WizardSubmission

    try:
        WizardSubmission.model_validate({"slug": "x/y"})
        assert False, "should have raised"
    except Exception as exc:
        fields = _pydantic_fields(exc)
        assert isinstance(fields, dict)
        assert len(fields) > 0


def test_pydantic_fields_returns_empty_for_non_validation_error() -> None:
    from mewbo_api.wiki.routes import _pydantic_fields

    result = _pydantic_fields(RuntimeError("boom"))
    assert result == {}


# ---------------------------------------------------------------------------
# _get_rate_limit
# ---------------------------------------------------------------------------


def test_get_rate_limit_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from mewbo_api.wiki.routes import _get_rate_limit

    with patch("mewbo_core.config.get_config_value", side_effect=Exception("no config")):
        limit = _get_rate_limit()
    assert limit == 10  # _DEFAULT_RATE_LIMIT


def test_check_rate_limit_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    import mewbo_api.wiki.routes as routes_mod

    routes_mod._rate_limit_counters.clear()
    monkeypatch.setattr(routes_mod, "_get_rate_limit", lambda: 5)

    # Should allow first 5 then block
    results = [routes_mod._check_rate_limit("1.2.3.4") for _ in range(7)]
    assert results[:5] == [True] * 5
    assert results[5] is False
    assert results[6] is False


# ---------------------------------------------------------------------------
# /v1/wiki/index/<id> GET (hydrate path triggered)
# ---------------------------------------------------------------------------


def test_get_job_snapshot_hydrates_platform(client) -> None:
    c, store, _ = client
    _seed_job(store, "j-hydr-snap", "org/repo", "queued")
    store.save_job_submission(
        "j-hydr-snap",
        {
            "platform": "github",
            "model": "anthropic/claude-3",
            "repoUrl": "https://github.com/org/repo",
        },
    )
    resp = c.get("/v1/wiki/index/j-hydr-snap", headers=_h())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("platform") == "github"
    assert data.get("host") == "github.com"


# ---------------------------------------------------------------------------
# WikiQaSession step-budget ceiling (#62)
# ---------------------------------------------------------------------------


class TestWikiQaSessionStepBudget:
    """#62: the QA fan-out is started with a HARD session-wide step ceiling so an
    unbounded probe fan-out can't run away (~1.1M tokens / 110 steps observed)."""

    def test_start_passes_session_step_budget(self, runtime_stub) -> None:
        """Real path through WikiQaSession.start; only the runtime boundary is
        stubbed. Assert start_async is invoked with the documented ceiling."""
        from mewbo_api.wiki.jobs import QA_SESSION_STEP_BUDGET, WikiQaSession

        answer = WikiQaSession.start(
            slug="org/repo",
            question="How does auth work?",
            from_page_id="overview",
            model="anthropic/claude-3",
            runtime=runtime_stub,
        )

        # The QA answer round-trips through the real store.
        assert answer.slug == "org/repo"

        runtime_stub.start_async.assert_called_once()
        _, kwargs = runtime_stub.start_async.call_args
        assert kwargs["session_step_budget"] == QA_SESSION_STEP_BUDGET
        # Sanity: the ceiling is a generous-but-finite cost backstop, never 0
        # (0 == unbounded in start_async).
        assert QA_SESSION_STEP_BUDGET > 0
