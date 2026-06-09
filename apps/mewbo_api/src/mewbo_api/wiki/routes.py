"""Wiki HTTP routes — Flask Blueprint.

This module is imported only when wiki extras are installed (gated by
init_wiki). Routes are registered via ``register(app, runtime)`` which
is called from ``wiki/__init__.py``.
"""
from __future__ import annotations

import collections
import time
from typing import Any, Literal, cast

from flask import Blueprint, Response, jsonify, request, stream_with_context
from mewbo_core.common import get_logger
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import IndexingJob, WikiError, WizardSubmission

from .catalogues import LANGUAGES, PLATFORMS
from .errors import register_error_handler, wiki_error_response
from .events import WikiQaSseGenerator, WikiSseGenerator
from .jobs import QaSessionEndHook, WikiIndexingJob, WikiIndexingSessionEndHook, WikiQaSession
from .resume import WikiResume

logging = get_logger(name="api.wiki.routes")

_runtime: Any = None  # populated by register()
_hook_manager: Any = None  # populated by register(); drives QA session-end finalize

# ---------------------------------------------------------------------------
# In-process rate limiter for POST /v1/wiki/index
# Keyed by (remote_addr, hour_bucket); value = request count in that window.
# ---------------------------------------------------------------------------

_rate_limit_counters: dict[tuple[str, int], int] = collections.defaultdict(int)
_RATE_LIMIT_CONFIG_KEY = ("wiki", "rate_limit", "indexing_per_hour")
_DEFAULT_RATE_LIMIT = 10

# Cap on lines returned when GET .../source is asked for a whole file (no
# start/end). ``totalLines`` still reports the true count so the FE can flag a
# truncated view. Keeps a giant file from blowing up the cited-sources panel.
_SOURCE_MAX_LINES = 2_000


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


def _resolve_qa_model() -> str:
    """Resolve the Q&A model id from config (the one canonical chain).

    Order: ``wiki.default_qa_model`` → ``wiki.default_model`` → ``llm.default_model``.
    Q&A typically wants a smaller/faster model than indexing, so its own key wins;
    it then degrades to the shared wiki default and finally the global LLM default.
    Returns ``""`` when nothing is configured. DRY: the single source for this
    chain — reused by ``post_qa`` (request default), ``get_wiki_defaults`` (picker
    pre-select), and ``_make_insight_llm`` (condense/dedup model).
    """
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        return str(
            get_config_value("wiki", "default_qa_model", default="")
            or get_config_value("wiki", "default_model", default="")
            or get_config_value("llm", "default_model", default="")
        )
    except Exception:
        return ""


def _make_insight_llm() -> Any | None:
    """Build the chat model for condense + dedup tier-3 on the human path.

    Resolves a model from ``wiki.memory.model`` → :func:`_resolve_qa_model`
    (``wiki.default_qa_model`` → ``wiki.default_model`` → ``llm.default_model``).
    Returns None (condense/LLM-dedup off) when no model is configured or the
    model can't be built — the content path still works with exact + fuzzy
    dedup. Isolated so tests can stub it.
    """
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415
        from mewbo_core.llm import build_chat_model  # noqa: PLC0415

        model = get_config_value("wiki", "memory", "model", default="") or _resolve_qa_model()
        if not model:
            return None
        return build_chat_model(str(model))
    except Exception:
        return None


def _has_git_submission(slug: str) -> bool:
    """True iff *slug* has a stored job submission carrying a real clone URL.

    Mirrors ``WikiIndexingJob.refresh``'s submission discovery: if any prior
    job's persisted submission validates as a ``WizardSubmission`` with a
    non-empty ``repo_url``, refresh can reconstruct a real git clone — so the
    project is git-backed, NOT a catalog. Used by the refresh route to tell a
    catalog project (no URL, nothing to reconstruct) from a git project whose
    Project record merely omits ``repo_url``.
    """
    try:
        store = _store()
        for job in store.list_jobs(slug=slug):
            sub = store.get_job_submission(job.job_id)
            if sub and str(sub.get("repoUrl") or "").strip():
                return True
    except Exception:
        # On any read hiccup, don't block a refresh (the git pipeline will
        # surface its own error if the URL is genuinely missing).
        return True
    return False


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


def register(app, runtime, hook_manager=None) -> None:
    """Mount /v1/wiki/* routes on the given Flask app + attach runtime ref.

    When a ``hook_manager`` is supplied, two ``on_session_end`` hooks are
    registered (idempotent across repeated ``register`` calls):

    - :class:`QaSessionEndHook`: reconciles QA answers whose run halts before
      the terminal ``wiki_emit_block`` sources block (models_used stamp).
    - :class:`WikiIndexingSessionEndHook`: marks non-terminal indexing jobs
      ``interrupted`` when their session ends — defense-in-depth so infra
      failures (tool-internal network / IO errors) hand off to ``JobRecovery``
      on next restart (Gitea #56).
    """
    global _runtime, _hook_manager
    _runtime = runtime
    _hook_manager = hook_manager
    if hook_manager is not None:
        existing = hook_manager.on_session_end
        if not any(isinstance(h, QaSessionEndHook) for h in existing):
            existing.append(QaSessionEndHook(runtime))
        if not any(isinstance(h, WikiIndexingSessionEndHook) for h in existing):
            existing.append(WikiIndexingSessionEndHook(runtime))
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
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

        deleted = _store().delete_project(slug)
        CredentialStore.delete(_store(), slug)
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
        from mewbo_graph.wiki.graph import KnowledgeGraphView  # noqa: PLC0415

        view = KnowledgeGraphView.for_slug(_store(), slug, node_limit=node_limit)
        return jsonify(view.to_wire())

    @bp.route("/projects/<path:slug>/source", methods=["GET"])
    def get_project_source(slug: str):
        """Return a source-file excerpt (with 1-based line numbers) for *slug*.

        Backs the Q&A "cited sources viewer": the FE parses each
        ``path#L<start>-<end>`` citation and lazily fetches the excerpt here.
        Query params: ``path`` (required, repo-relative), ``start`` / ``end``
        (optional, 1-based inclusive). Omitting the range returns the whole
        file, capped at :data:`_SOURCE_MAX_LINES` lines (``totalLines`` still
        reports the true count so the FE can show "truncated").

        Reads from the most-recent completed indexing clone on disk via
        :func:`resolve_qa_clone_dir`; path-safety (no ``..`` escape outside the
        clone root) and decoding are delegated to :class:`WikiSourceAccess`.
        """
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )

        rel_path = (request.args.get("path") or "").strip()
        if not rel_path:
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="path query param is required",
                    fields={"path": "required"},
                )
            )
        try:
            raw_start = request.args.get("start")
            raw_end = request.args.get("end")
            start = int(raw_start) if raw_start is not None else None
            end = int(raw_end) if raw_end is not None else None
        except (TypeError, ValueError):
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="start and end must be integers",
                    fields={"start": "invalid"},
                )
            )
        if (start is not None and start < 1) or (end is not None and end < 1):
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="start and end are 1-based and must be >= 1",
                    fields={"start": "invalid"},
                )
            )

        from mewbo_graph.plugins.wiki._ctx import resolve_qa_clone_dir  # noqa: PLC0415
        from mewbo_graph.plugins.wiki.source_tools import WikiSourceAccess  # noqa: PLC0415

        clone_dir = resolve_qa_clone_dir(slug, _store())
        if clone_dir is None:
            return wiki_error_response(
                WikiError(
                    code="not_found",
                    message="no completed indexing clone is available for this project",
                )
            )

        # Reuse the QA tool's path-safety + decode statics — ``_safe_path`` is the
        # load-bearing traversal guard (rejects absolute / ``..`` / symlink-escape
        # paths). Both are pure, clone-dir-parameterised, so no WikiSourceAccess
        # instance/ctx is needed.
        target = WikiSourceAccess._safe_path(rel_path, clone_dir)
        if target is None:
            return wiki_error_response(
                WikiError(
                    code="forbidden",
                    message="path escapes the repository root",
                    fields={"path": "forbidden"},
                )
            )
        if not target.is_file():
            return wiki_error_response(
                WikiError(code="not_found", message=f"file not found: {rel_path}")
            )
        try:
            text = WikiSourceAccess._decode(target.read_bytes())
        except OSError as exc:
            return wiki_error_response(
                WikiError(code="internal", message=f"read failed: {exc}")
            )

        lines = text.splitlines()
        total = len(lines)
        whole_file = start is None and end is None
        slice_start = max(0, (start or 1) - 1)
        slice_end = min(total, end if end is not None else total)
        if whole_file:
            slice_end = min(total, _SOURCE_MAX_LINES)
        clip = lines[slice_start:slice_end]
        return jsonify({
            "path": rel_path,
            "startLine": (slice_start + 1) if clip and not whole_file else None,
            "endLine": slice_end if clip and not whole_file else None,
            "totalLines": total,
            "content": "\n".join(clip),
        })

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
        # ``qaModel`` shares the one canonical Q&A chain (qa → wiki default →
        # llm default) so the picker pre-selects exactly what ``post_qa`` defaults.
        qa_model = _resolve_qa_model()
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
        """Return all non-terminal jobs (queued/scanning/finalizing/interrupted).

        Powers the landing-page "Indexing now" surface; platform is
        hydrated so the FE composes canonical URLs without an extra hop.
        ``interrupted`` is an in-progress state (a restart-stranded job
        awaiting recovery), so it belongs here, not in the terminal set.
        """
        auth = _require_auth()
        if auth:
            return auth
        ACTIVE = {"queued", "scanning", "finalizing", "interrupted"}
        out = []
        for job in _store().list_jobs():
            if job.status not in ACTIVE:
                continue
            job = _hydrate_platform(job)
            out.append(job.model_dump(mode="json", by_alias=True, exclude_none=True))
        return jsonify(out)

    @bp.route("/jobs/recoverable", methods=["GET"])
    def list_recoverable_jobs():
        """Return non-complete jobs that carry checkpoint artifacts worth resuming.

        Powers the console "Resume indexing" surface. A job qualifies when it is
        in a recoverable status (failed / interrupted / cancelled) AND has reached
        at least the graph or committed a plan / written pages — i.e. resuming it
        would actually save work (else it is a from-scratch re-index, not a
        resume). The payload is the smallest the FE needs per job; the ``recoverable``
        hint summarises what would be reused.
        """
        from mewbo_graph.wiki.resume import ResumePlan  # noqa: PLC0415

        auth = _require_auth()
        if auth:
            return auth
        RECOVERABLE_STATUS = {"failed", "interrupted", "cancelled"}
        out = []
        for job in _store().list_jobs():
            if job.status not in RECOVERABLE_STATUS:
                continue
            plan = ResumePlan.build(_store(), job)
            # Nothing reusable ⇒ a resume would just rebuild from scratch; don't
            # advertise it as a checkpoint-resume candidate.
            if plan.is_noop():
                continue
            job = _hydrate_platform(job)
            out.append({
                "jobId": job.job_id,
                "slug": job.slug,
                "status": job.status,
                "phase": job.phase,
                "error": job.error.model_dump(mode="json", by_alias=True, exclude_none=True)
                if job.error is not None
                else None,
                "pagesSubmitted": job.pages_submitted,
                "totalPages": job.total_pages,
                "updatedAt": job.phase_started_at,
                "recoverable": {
                    "skip": sorted(plan.skip),
                    "pagesDone": len(plan.pages_done),
                    "pagesRemaining": len(plan.pages_remaining),
                    "nodeCount": plan.node_count,
                },
            })
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

    @bp.route("/index/<string:job_id>/resume", methods=["POST"])
    def resume_index(job_id: str):
        """Checkpoint-aware resume of an interrupted index (reuses the SAME job_id).

        Re-clones at the recorded commit + skips the expensive idempotent phases
        whose store artifacts already exist (graph / enrich / plan), writing only
        the remaining pages. User-initiated, so it resets the per-slug auto-recovery
        cap. Returns ``{job_id, session_id, status}`` mirroring start/refresh.
        """
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_job(job_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        try:
            result = WikiResume.resume(
                _store(), _runtime, job_id, hook_manager=_hook_manager
            )
        except KeyError:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        except ValueError as exc:
            return wiki_error_response(
                WikiError(code="validation", message=str(exc))
            )
        except Exception as exc:
            return wiki_error_response(
                WikiError(code="internal", message=str(exc))
            )
        resp = jsonify({
            "jobId": result["job_id"],
            "sessionId": result["session_id"],
            "status": result["status"],
        })
        resp.status_code = 202
        return resp

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
        # ``model`` is optional — default it from config (qa → wiki → llm) so the
        # MCP ``ask_wiki`` tool can omit it entirely.
        model = (body.get("model") or "").strip() or _resolve_qa_model()
        # Public param is ``project``; ``slug`` is the internal name. Accept
        # either in the body but report validation against the public ``project``.
        slug = (body.get("project") or body.get("slug") or "").strip()
        missing: dict[str, str] = {}
        if not question:
            missing["question"] = "required"
        if not slug:
            missing["project"] = "required"
        if missing:
            return wiki_error_response(WikiError(
                code="validation",
                message="question and project are required",
                fields=missing,
            ))
        try:
            answer = WikiQaSession.start(
                slug=slug,
                question=question,
                from_page_id=from_page_id,
                model=model,
                runtime=_runtime,
                hook_manager=_hook_manager,
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

    @bp.route("/projects/<path:slug>/insights", methods=["POST"])
    def post_insight(slug: str):
        """Ingest a suggested memory insight for *slug* (human/external agent).

        Body: ``{content?, raw?, anchors?, links?, kind?, labels?, condense?}``.
        Either ``content`` (one atomic claim) or ``raw`` (free text to
        condense) is required. The shared ``InsightIngestor`` validates,
        condenses (raw path), auto-anchors to the tree-sitter graph, dedups,
        and safely merges. Returns the per-claim ``IngestResult`` — 201 when
        at least one claim was stored, 200 (``ok: false``) when a well-formed
        request was processed but every claim was rejected (a normal advisory
        outcome, not a client error).
        """
        auth = _require_auth()
        if auth:
            return auth
        if _store().get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        body = request.get_json(silent=True) or {}
        content = str(body.get("content") or "").strip()
        raw = str(body.get("raw") or "").strip()
        condense = bool(body.get("condense", False))
        kind = body.get("kind") or "propositional"
        if not content and not raw:
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="content or raw is required",
                    fields={"content": "required"},
                )
            )
        if kind not in ("propositional", "prescriptive"):
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="kind must be 'propositional' or 'prescriptive'",
                    fields={"kind": "invalid"},
                )
            )
        # kind is already constrained to the two-member set by the guard above.
        kind_lit = cast(Literal["propositional", "prescriptive"], kind)

        from mewbo_graph.wiki.memory import InsightCondenser, InsightIngestor  # noqa: PLC0415

        llm = _make_insight_llm()
        want_condense = bool(condense or raw)
        condenser = InsightCondenser(llm) if (llm is not None and want_condense) else None
        ingestor = InsightIngestor.from_store(_store(), llm=llm, condenser=condenser)
        try:
            result = ingestor.ingest(
                slug,
                content or None,
                raw=raw or None,
                anchors=list(body.get("anchors") or []),
                links=list(body.get("links") or []),
                kind=kind_lit,
                labels=list(body.get("labels") or []),
                condense=want_condense,
                source="on_demand",
                author_agent="rest",
            )
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        resp = jsonify(result.model_dump(mode="json"))
        resp.status_code = 201 if result.ok else 200
        return resp

    @bp.route("/projects/<path:slug>/documents", methods=["POST"])
    def post_documents(slug: str):
        """Programmatically ingest catalog documents into a NON-git project.

        Body: ``{documents: [{id, title, text, metadata?}]}``. Creates the
        project for *slug* if it does not yet exist (no git URL required) and
        writes each record as a wiki page + an embedded graph node, so the
        existing :class:`HybridRetriever` / Q&A ground over it unchanged.
        Synchronous (a modest batch is a deterministic write — no agent loop).
        Returns the :class:`CatalogIngestReport` (201 when ≥1 doc ingested).
        """
        auth = _require_auth()
        if auth:
            return auth
        body = request.get_json(silent=True) or {}
        raw_docs = body.get("documents")
        if not isinstance(raw_docs, list) or not raw_docs:
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="documents must be a non-empty list",
                    fields={"documents": "required"},
                )
            )
        from mewbo_graph.wiki.catalog import CatalogIngestor  # noqa: PLC0415
        from mewbo_graph.wiki.types import CatalogDocument  # noqa: PLC0415

        try:
            documents = [CatalogDocument.model_validate(d) for d in raw_docs]
        except Exception as exc:
            fields = _pydantic_fields(exc)
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=fields or None)
            )
        try:
            report = CatalogIngestor(store=_store()).ingest(slug, documents)
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        resp = jsonify(report.model_dump(mode="json", by_alias=True))
        resp.status_code = 201
        return resp

    @bp.route("/projects/<path:slug>/refresh", methods=["POST"])
    def refresh_project(slug: str):
        """Re-trigger a full re-index for an existing project (on-demand only)."""
        auth = _require_auth()
        if auth:
            return auth
        project = _store().get_project(slug)
        if project is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        # A catalog (non-git) project has no clone URL AND no git submission to
        # reconstruct one from. ``WikiIndexingJob.refresh`` would then synthesize
        # a bogus submission (``repoUrl = slug``) → ``git clone <slug>`` fails.
        # Reject here instead of letting the git pipeline thrash; catalog content
        # is re-populated via ``POST .../documents``, not the refresh path. A git
        # project whose Project record simply lacks ``repo_url`` still has a
        # stored submission, so it is NOT treated as a catalog (that submission
        # carries the real clone URL refresh restores).
        if project.repo_url is None and not _has_git_submission(slug):
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message=(
                        "project has no repository — re-ingest catalog documents "
                        "via POST /v1/wiki/projects/<slug>/documents instead of refresh"
                    ),
                )
            )
        try:
            WikiIndexingJob.refresh(slug, runtime=_runtime, hook_manager=None)
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        return jsonify({"queued": True})

    return bp
