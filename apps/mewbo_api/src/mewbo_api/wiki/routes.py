"""Wiki HTTP routes — Flask Blueprint.

This module is imported only when wiki extras are installed (gated by
init_wiki). Routes are registered via ``register(app, runtime)`` which
is called from ``wiki/__init__.py``.
"""
from __future__ import annotations

import collections
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .catalogues import LANGUAGES, PLATFORMS
from .errors import register_error_handler, wiki_error_response
from .events import WikiQaSseGenerator, WikiSseGenerator
from .jobs import WikiIndexingJob, WikiQaSession
from .store import WikiStoreBase
from .types import IndexingJob, WikiError, WizardSubmission

_runtime: Any = None  # populated by register()

# ---------------------------------------------------------------------------
# In-process rate limiter for POST /v1/wiki/index
# Keyed by (remote_addr, hour_bucket); value = request count in that window.
# ---------------------------------------------------------------------------

_rate_limit_counters: dict[tuple[str, int], int] = collections.defaultdict(int)
_RATE_LIMIT_CONFIG_KEY = ("wiki", "rate_limit", "indexing_per_hour")
_DEFAULT_RATE_LIMIT = 10


def _get_rate_limit() -> int:
    """Return the configured max indexing requests per hour per IP."""
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        return int(get_config_value(*_RATE_LIMIT_CONFIG_KEY, default=_DEFAULT_RATE_LIMIT))
    except Exception:
        return _DEFAULT_RATE_LIMIT


def _check_rate_limit(remote_addr: str) -> bool:
    """Return True if request is within rate limit; False if exceeded.

    Uses an in-process counter keyed by (remote_addr, hour_bucket).
    The hour bucket is ``int(time.time() // 3600)``.
    """
    hour = int(time.time() // 3600)
    key = (remote_addr, hour)
    _rate_limit_counters[key] += 1
    return _rate_limit_counters[key] <= _get_rate_limit()


def _pydantic_fields(exc: Exception) -> dict[str, str]:
    """Extract field→message map from a Pydantic v2 ValidationError."""
    try:
        from pydantic import ValidationError  # noqa: PLC0415

        if isinstance(exc, ValidationError):
            fields: dict[str, str] = {}
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                fields[loc or "root"] = err.get("msg", "invalid")
            return fields
    except Exception:
        pass
    return {}


def _require_auth():
    """Validate the API key; mirrors backend._require_api_key.

    Lazily imports from backend so the import side-effects only happen
    when the first request arrives, not at module load time.  This keeps
    test fixtures that build a bare Flask app from pulling in the full
    API server.
    """
    try:
        from mewbo_api.backend import _require_api_key

        return _require_api_key()
    except ImportError:
        # Fallback when backend is not importable (e.g. unit-test stubs
        # that build a Flask app without the full mewbo_api.backend).
        import os

        master_token = os.environ.get("MASTER_API_TOKEN", "msk-strong-password")
        api_token = request.headers.get("X-API-Key") or request.args.get("api_key")
        if api_token is None:
            return {"message": "API token is not provided."}, 401
        if api_token != master_token:
            return {"message": "Unauthorized"}, 401
        return None


def _store() -> WikiStoreBase:
    return _runtime.wiki_store


def _hydrate_platform(job: IndexingJob) -> IndexingJob:
    """Backfill ``job.platform``/``job.host``/``job.model`` from the submission.

    Older jobs may lack one or more of these fields. The wizard submission
    is the canonical source — platform and model are explicit; host comes
    from the repo URL's DNS. Patch in whichever is missing.
    """
    if (
        job.platform is not None
        and job.host is not None
        and job.model is not None
    ):
        return job
    try:
        sub = _store().get_job_submission(job.job_id)
    except Exception:
        sub = None
    if not sub:
        return job
    patch: dict[str, str] = {}
    if job.platform is None and sub.get("platform"):
        patch["platform"] = sub["platform"]
    if job.model is None and sub.get("model"):
        patch["model"] = sub["model"]
    if job.host is None and sub.get("repoUrl"):
        try:
            from urllib.parse import urlparse  # noqa: PLC0415

            host = urlparse(sub["repoUrl"]).hostname
            if host:
                patch["host"] = host
        except Exception:
            pass
    if not patch:
        return job
    return job.model_copy(update=patch)


def register(app, runtime) -> None:
    """Mount /v1/wiki/* routes on the given Flask app + attach runtime ref."""
    global _runtime
    _runtime = runtime
    register_error_handler(app)
    app.register_blueprint(_build_blueprint(), url_prefix="/v1/wiki")


def _build_blueprint() -> Blueprint:
    bp = Blueprint("wiki", __name__)

    @bp.route("/projects", methods=["GET"])
    def list_projects():
        auth = _require_auth()
        if auth:
            return auth
        projects = _store().list_projects()
        return jsonify([p.model_dump(mode="json", by_alias=True) for p in projects])

    @bp.route("/projects/<path:slug>", methods=["DELETE"])
    def delete_project(slug: str):
        auth = _require_auth()
        if auth:
            return auth
        deleted = _store().delete_project(slug)
        return jsonify({"deleted": deleted})

    @bp.route("/projects/<path:slug>/pages/<string:page_id>", methods=["GET"])
    def get_page(slug: str, page_id: str):
        auth = _require_auth()
        if auth:
            return auth
        store = _store()
        page = store.get_page(slug, page_id)
        if page is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"page {page_id} not found")
            )
        # ``nav`` and ``toc`` are derived per-request from the project's
        # page list + this page's markdown headings. The persisted
        # ``WikiPage`` keeps them empty (see ``submit_page._build_wiki_page``)
        # so the derivation can change without re-indexing.
        from .nav_toc import derive_nav, derive_toc  # noqa: PLC0415

        project = store.get_project(slug)
        landing_id = getattr(project, "landing_page_id", None)
        nav = derive_nav(store.list_pages(slug))
        if landing_id:
            # Promote the landing page to the top so the user has a
            # stable entry point regardless of title-alpha sort.
            nav = (
                [n for n in nav if n.id == landing_id]
                + [n for n in nav if n.id != landing_id]
            )
        toc = derive_toc(page.body)
        enriched = page.model_copy(update={"nav": nav, "toc": toc})
        return jsonify(enriched.model_dump(mode="json", by_alias=True))

    @bp.route("/projects/<path:slug>/graph", methods=["GET"])
    def get_project_graph(slug: str):
        """Return the persisted knowledge graph for *slug* (Cytoscape shape).

        Returns 404 when the project is unknown so the FE can render a
        sensible "not indexed" empty state.
        """
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        try:
            node_limit_raw = request.args.get("limit", default=None)
            node_limit = int(node_limit_raw) if node_limit_raw else None
        except (TypeError, ValueError):
            node_limit = None
        from .graph import KnowledgeGraphView  # noqa: PLC0415

        view = KnowledgeGraphView.for_slug(_store(), slug, node_limit=node_limit)
        return jsonify(view.to_wire())

    @bp.route("/platforms", methods=["GET"])
    def list_platforms():
        auth = _require_auth()
        if auth:
            return auth
        return jsonify([p.model_dump(mode="json", by_alias=True) for p in PLATFORMS])

    @bp.route("/languages", methods=["GET"])
    def list_languages():
        auth = _require_auth()
        if auth:
            return auth
        return jsonify([la.model_dump(mode="json", by_alias=True) for la in LANGUAGES])

    @bp.route("/defaults", methods=["GET"])
    def get_wiki_defaults():
        """Return wiki-specific defaults the picker should pre-select.

        Each key is independent: set ``wiki.default_model`` (indexing),
        ``wiki.default_qa_model`` (Q&A — typically a smaller/faster
        model than indexing), ``wiki.default_depth``, or
        ``wiki.default_language`` in app.json to pin that field. Unset
        keys fall back to whatever the FE already does (e.g.
        ``/api/models``'s global default). ``qaModel`` falls back to
        ``wiki.default_model`` when not separately set so a single
        ``default_model`` still works for both phases.
        """
        auth = _require_auth()
        if auth:
            return auth
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        out: dict[str, str] = {}
        model = get_config_value("wiki", "default_model", default="")
        qa_model = (
            get_config_value("wiki", "default_qa_model", default="")
            or model
        )
        depth = get_config_value("wiki", "default_depth", default="")
        language = get_config_value("wiki", "default_language", default="")
        if model:
            out["model"] = model
        if qa_model:
            out["qaModel"] = qa_model
        if depth in ("comprehensive", "concise"):
            out["depth"] = depth
        if language:
            out["language"] = language
        return jsonify(out)

    @bp.route("/index/<string:job_id>", methods=["GET"])
    def get_job_snapshot(job_id: str):
        auth = _require_auth()
        if auth:
            return auth
        job = _store().get_job(job_id)
        if job is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        job = _hydrate_platform(job)
        return jsonify(job.model_dump(mode="json", by_alias=True, exclude_none=True))

    @bp.route("/jobs/active", methods=["GET"])
    def list_active_jobs():
        """Return all non-terminal jobs (queued/scanning/finalizing).

        Powers the landing-page "Indexing now" surface; platform is
        hydrated so the FE composes canonical URLs without an extra hop.
        """
        auth = _require_auth()
        if auth:
            return auth
        ACTIVE = {"queued", "scanning", "finalizing"}
        out = []
        for job in _store().list_jobs():
            if job.status not in ACTIVE:
                continue
            job = _hydrate_platform(job)
            out.append(job.model_dump(mode="json", by_alias=True, exclude_none=True))
        return jsonify(out)

    @bp.route("/qa/<string:answer_id>", methods=["GET"])
    def get_qa_snapshot(answer_id: str):
        auth = _require_auth()
        if auth:
            return auth
        ans = _store().get_qa(answer_id)
        if ans is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"answer {answer_id} not found")
            )
        return jsonify(ans.model_dump(mode="json", by_alias=True))

    @bp.route("/index", methods=["POST"])
    def post_index():
        auth = _require_auth()
        if auth:
            return auth
        # Per-IP rate-limit check.
        remote = request.remote_addr or "unknown"
        if not _check_rate_limit(remote):
            err = WikiError(
                code="rate_limited",
                message="Too many indexing requests; try again later.",
                retry_after=60.0,
            )
            return wiki_error_response(err, status=429)
        try:
            submission = WizardSubmission.model_validate(
                request.get_json(silent=True) or {}
            )
        except Exception as exc:
            fields = _pydantic_fields(exc)
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=fields or None)
            )
        try:
            job = WikiIndexingJob.start(
                submission, runtime=_runtime, hook_manager=None
            )
        except Exception as exc:
            return wiki_error_response(
                WikiError(code="internal", message=str(exc))
            )
        resp = jsonify(
            job.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        resp.status_code = 202
        return resp

    @bp.route("/index/<string:job_id>", methods=["DELETE"])
    def delete_index(job_id: str):
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_job(job_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        WikiIndexingJob.cancel(job_id, runtime=_runtime)
        snapshot = _store().get_job(job_id)
        if snapshot is None:
            # Should not happen — job existed just above; guard for mypy.
            return wiki_error_response(
                WikiError(code="internal", message=f"job {job_id} vanished after cancel")
            )
        return jsonify(
            snapshot.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    @bp.route("/index/<string:job_id>/stream", methods=["GET"])
    def stream_index(job_id: str):
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_job(job_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        # EventSource auto-reconnect sends back the last received event id
        # via the ``Last-Event-ID`` header — honour it so a dropped/recycled
        # SSE connection picks up from the same point instead of replaying
        # the entire transcript. ``after_idx`` query param still wins for
        # explicit callers (curl, tests).
        raw_after = request.args.get("after_idx")
        if raw_after is None:
            raw_after = request.headers.get("Last-Event-ID")
        try:
            after_idx = int(raw_after) if raw_after is not None else -1
        except (ValueError, TypeError):
            after_idx = -1
        gen = WikiSseGenerator(store=_store(), job_id=job_id, after_idx=after_idx)
        return Response(
            stream_with_context(gen.generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.route("/qa", methods=["POST"])
    def post_qa():
        auth = _require_auth()
        if auth:
            return auth
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        from_page_id = (body.get("fromPageId") or "").strip()
        model = (body.get("model") or "").strip()
        slug = (body.get("slug") or "").strip()
        missing = {k: "required" for k in ("question", "model", "slug") if not body.get(k)}
        if missing:
            return wiki_error_response(WikiError(
                code="validation",
                message="question, model, and slug are required",
                fields=missing,
            ))
        try:
            answer = WikiQaSession.start(
                slug=slug,
                question=question,
                from_page_id=from_page_id,
                model=model,
                runtime=_runtime,
            )
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        gen = WikiQaSseGenerator(store=_store(), answer_id=answer.answer_id)
        return Response(
            stream_with_context(gen.generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.route("/qa/<string:answer_id>", methods=["DELETE"])
    def delete_qa(answer_id: str):
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_qa(answer_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"answer {answer_id} not found")
            )
        WikiQaSession.cancel(answer_id, runtime=_runtime)
        snap = _store().get_qa(answer_id)
        if snap is None:
            return wiki_error_response(
                WikiError(code="internal", message=f"answer {answer_id} vanished after cancel")
            )
        return jsonify(snap.model_dump(mode="json", by_alias=True))

    @bp.route("/qa/<string:answer_id>/stream", methods=["POST"])
    def stream_qa(answer_id: str):
        """Replay-from-start SSE for shared QA URLs."""
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_qa(answer_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"answer {answer_id} not found")
            )
        raw_after = request.args.get("after_idx")
        if raw_after is None:
            raw_after = request.headers.get("Last-Event-ID")
        try:
            after_idx = int(raw_after) if raw_after is not None else -1
        except (ValueError, TypeError):
            after_idx = -1
        gen = WikiQaSseGenerator(store=_store(), answer_id=answer_id, after_idx=after_idx)
        return Response(
            stream_with_context(gen.generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.route("/projects/<path:slug>/refresh", methods=["POST"])
    def refresh_project(slug: str):
        """Re-trigger indexing for an existing project. No email collection."""
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        try:
            WikiIndexingJob.refresh(slug, runtime=_runtime, hook_manager=None)
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        return jsonify({"queued": True})

    return bp
