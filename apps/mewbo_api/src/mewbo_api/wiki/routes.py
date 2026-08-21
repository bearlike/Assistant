"""Wiki HTTP routes — Flask Blueprint.

This module is imported only when wiki extras are installed (gated by
init_wiki). Routes are registered via ``register(app, runtime)`` which
is called from ``wiki/__init__.py``.
"""
from __future__ import annotations

import collections
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal, cast

from flask import Blueprint, Response, jsonify, request, stream_with_context
from mewbo_core.common import get_logger
from mewbo_core.contracts.progress import ProgressLedger
from mewbo_graph.wiki.resume import ResumeCountError
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import (
    IndexingJob,
    RefreshMode,
    WikiError,
    WikiPage,
    WizardSubmission,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_api.auth.guard_registry import guard

from .catalogues import LANGUAGES, PLATFORMS
from .errors import (
    documentation_unavailable_response,
    register_error_handler,
    wiki_error_response,
)
from .events import WikiQaSseGenerator, WikiSseGenerator
from .jobs import (
    QaSessionEndHook,
    WikiIndexingJob,
    WikiIndexingSessionEndHook,
    WikiJobTerminationCascade,
    WikiMaintainerSession,
    WikiQaSession,
)
from .resume import WikiResume
from .settings import WikiProjectSettings

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

# ---------------------------------------------------------------------------
# In-process TTL cache for GET .../freshness (a ``git ls-remote`` + platform
# compare API call per check — cheap but not free-to-poll). Keyed by slug;
# ``?force=1`` busts a cached entry, and ``refresh_project``/``delete_project``
# evict the slug's entry so a re-index or removal never serves a stale badge.
# A negative result (remote unreachable) is cached like any other body for the
# full TTL, so a dead remote can't re-block every request. Module-level (not
# per-blueprint-instance) to mirror the rate-limiter convention above.
# ---------------------------------------------------------------------------

_freshness_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_FRESHNESS_TTL_SECONDS = 300.0


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


def _store() -> WikiStoreBase:
    return _runtime.wiki_store


def _resolve_qa_model(mode: str = "deep") -> str:
    """Resolve the Q&A model id from config (the one canonical chain).

    Order: (``wiki.default_qa_fast_model`` when *mode* is ``"fast"``) →
    ``wiki.default_qa_model`` → ``wiki.default_model`` → ``llm.default_model``.
    Q&A typically wants a smaller/faster model than indexing, so its own key
    wins; it then degrades to the shared wiki default and finally the global
    LLM default. Returns ``""`` when nothing is configured. DRY: the single
    source for this chain — reused by ``post_qa`` (request default, the only
    caller that ever passes ``mode="fast"``), ``get_wiki_defaults`` (picker
    pre-select), and ``_make_insight_llm`` (condense/dedup model) — the latter
    two call with no argument and so resolve the plain (non-fast) chain.
    """
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        fast_model = (
            get_config_value("wiki", "default_qa_fast_model", default="")
            if mode == "fast"
            else ""
        )
        return str(
            fast_model
            or get_config_value("wiki", "default_qa_model", default="")
            or get_config_value("wiki", "default_model", default="")
            or get_config_value("llm", "default_model", default="")
        )
    except Exception:
        return ""


# Every value ``WikiRefreshConfig.default_mode`` advertises, mapped onto the
# engine's two-member ``RefreshMode``. It is TOTAL over that config literal on
# purpose: a knob the schema itself offers must not be handled by the
# unrecognised-value fallback below, or an operator sets a mode the schema told
# them existed and silently gets a different one.
#
# ``"incremental"`` is an ALIAS for ``auto``, not a third strategy. ``auto`` IS
# the incremental one — it takes the scoped delta pass wherever reuse can be
# justified and falls back to a full rebuild where it cannot — and there is
# deliberately no mode that demands a scoped pass unconditionally (see
# ``RefreshMode``: ``scoped`` is an OUTCOME, not something a caller may request).
# So ``auto`` is the whole of what "incremental" can honestly mean here, and
# mapping it to ``full`` would hand an operator asking for the cheap path the
# expensive one.
_CONFIG_REFRESH_MODES: dict[str, RefreshMode] = {
    "auto": "auto",
    "full": "full",
    "incremental": "auto",
}


def _configured_refresh_mode() -> RefreshMode:
    """Resolve the operator's default refresh strategy from config.

    Reads ``wiki.refresh.default_mode`` — the strategy a refresh takes when the
    CALLER named none. It sits beside ``_resolve_qa_model`` for the same reason
    that one does: config is I/O, so it is read here at the edge and handed to
    the wire model as an argument rather than reached for from inside it.

    A value outside ``_CONFIG_REFRESH_MODES`` degrades to ``auto`` — the shipped
    default, and the one that can never do LESS than a full rebuild. Reaching
    that branch means config validation was bypassed or the
    literal gained a member this map was not taught, so it is a genuine unknown
    rather than a knob the schema advertises.
    """
    try:
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        configured = str(
            get_config_value("wiki", "refresh", "default_mode", default="auto") or "auto"
        )
    except Exception:
        return "auto"
    return _CONFIG_REFRESH_MODES.get(configured, "auto")


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
        from mewbo_core.llm.llm import build_chat_model  # noqa: PLC0415

        model = get_config_value("wiki", "memory", "model", default="") or _resolve_qa_model()
        if not model:
            return None
        return build_chat_model(str(model))
    except Exception:
        return None


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


def _job_wire(job: IndexingJob) -> dict[str, Any]:
    """Serialise *job* for the wire — hydrated fields plus its backing session id.

    The ONE job→wire seam, shared by the per-job snapshot and the active-jobs
    list, so the two endpoints can never disagree about a job's shape (the
    console types both with a single ``IndexingJob`` interface, so a field
    stamped by only one of them would make that type lie).

    ``sessionId`` is stamped HERE rather than carried on :class:`IndexingJob`
    because the job→session binding lives on its own store surface
    (``attach_job_session``), deliberately outside the job schema — putting it
    on the snapshot too would mean two writers of one fact, and a resume would
    have to remember to update both. A graph-only index is sessionless, so the
    key is simply absent; that is what lets a progress surface offer "watch the
    indexer" only when there is a session to watch. Best-effort like the
    platform backfill above: a store hiccup must not fail a progress poll.

    ``isActive`` projects the model's own liveness question onto the wire so the
    console reads ONE answer instead of re-deriving it from a status list of its
    own — the drift that let a job read as dead on one surface and still
    indexing on another. It is derived, never stored (the snapshot is dumped
    ``exclude_none=True``, and stamping a plain bool here also guarantees the
    key is always present, so absence never has to be read as false).
    """
    data = _hydrate_platform(job).model_dump(mode="json", by_alias=True, exclude_none=True)
    data["isActive"] = job.is_active
    try:
        session_id = _store().get_job_session(job.job_id)
    except Exception:
        session_id = None
    if session_id:
        data["sessionId"] = session_id
    return data


class BranchListRequest(BaseModel):
    """``POST /v1/wiki/branches`` request body (transport-only — never persisted).

    Lives api-side because it is wire transport, not a domain model that travels
    with the store (see the wiki CLAUDE.md API/library boundary). ``token`` is the
    same never-persisted secret the wizard submits for a private clone.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo_url: str = Field(alias="repoUrl")
    slug: str | None = None
    token: str | None = None


class ResumeIndexRequest(BaseModel):
    """``POST /v1/wiki/index/<job_id>/resume`` request body — every field optional.

    Transport-only, never persisted as-is (mirrors ``BranchListRequest`` above).
    ``restart`` reaches ``ResumePlan.for_restart()`` — the
    deliberate "rebuild from scratch" intent (as opposed to the default
    checkpoint resume, which skips already-done phases). ``extra="forbid"`` so a
    client typo doesn't silently no-op into the default resume behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    restart: bool = False


class MaintainerSessionRequest(BaseModel):
    """``POST /v1/wiki/projects/<slug>/session`` request body — entirely optional.

    Transport-only, never persisted (mirrors ``ResumeIndexRequest`` above). The
    body is optional on the wire and the "open" affordance on the project card
    sends none, so an absent or empty body must validate to the default.

    ``new_session`` names what it does: mint a session that is NOT the project's
    canonical maintainer. The default is the get-or-create the "open" button
    depends on — a link into the ongoing conversation as much as a way to start
    one — while a composer submitting a turn against this project asked for a NEW
    conversation and must never be handed someone else's transcript to grow.
    ``extra="forbid"`` so a client that misspells the field gets a 400 rather
    than the silent reuse this exists to prevent.

    Aliased ``newSession`` because this Blueprint's wire is camelCase
    (``repoUrl``, ``sessionId``); ``populate_by_name`` keeps the snake spelling
    working too, so a non-console caller is not tripped by the convention.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    new_session: bool = Field(default=False, alias="newSession")


class RefreshProjectRequest(BaseModel):
    """``POST /v1/wiki/projects/<slug>/refresh`` request body — entirely optional.

    Transport-only, never persisted as-is (mirrors ``ResumeIndexRequest`` above).
    The body is optional on the wire and most callers send none, so an absent or
    empty body must keep working — it validates to the default.

    ``mode`` is OPTIONAL, and its absence is a distinct state from any value it
    could carry: an omitted mode means "whatever this deployment is configured
    to do" (``wiki.refresh.default_mode``), while a present one is a caller
    making a choice that always wins. Collapsing the two — defaulting the field
    to ``"auto"`` at validation — would make the operator setting unreachable,
    because by the time the route reads the model it could no longer tell a
    caller that asked for ``auto`` from one that asked for nothing.

    The shipped default of that setting is ``auto``, which is a PRODUCT
    decision, not a conservative one: the console's Refresh button
    takes the cheap scoped path, and ``auto`` already falls back to a full
    rebuild wherever reuse cannot be justified (see ``RefreshDecision.decide``),
    so it can never do LESS than a full rebuild. ``extra="forbid"`` plus the
    closed ``RefreshMode`` literal make a typo'd field or an unknown mode a
    clean 400 naming the field, rather than a silent fall-through to a default
    — the whole failure this model exists to prevent is a caller believing it
    asked for one path and being given the other.
    """

    model_config = ConfigDict(extra="forbid")

    mode: RefreshMode | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        """An explicit ``"mode": null`` is a 400, exactly as it always was.

        Omitted and null are DIFFERENT states here — the same rule the settings
        PATCH applies — and only the first means "consult the configured
        default". A field validator sees a value only when the key was actually
        supplied (defaults are not validated), which is what lets one rule
        separate the two without a second sentinel type.
        """
        if value is None:
            raise ValueError("mode must be 'auto' or 'full'; omit the field to use the default")
        return value

    def resolve_mode(self, configured_default: RefreshMode) -> RefreshMode:
        """Return the mode this request runs under.

        An explicitly requested mode always wins; only its absence consults the
        deployment's configured default. The default arrives as an ARGUMENT
        because reading config is I/O and a wire model must never reach for it —
        the same rule that keeps the clock out of ``TriggerSpec``.
        """
        return self.mode or configured_default


class WikiPageIndexQuery(BaseModel):
    """``GET /v1/wiki/projects/<slug>/pages`` query contract.

    Bounds live HERE, at the query, so the answer is always complete and small
    rather than a severed prefix of a large one — a caller that wants the rest
    asks for the next ``offset``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title_contains: str | None = Field(
        default=None,
        alias="titleContains",
        description="Case-insensitive substring filter on page titles.",
    )
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    def page(self, pages: Iterable[WikiPage]) -> dict[str, Any]:
        """Filter, sort and slice *pages* into one wire page of ``{id, title}``.

        The store read arrives as an ARGUMENT — this model never reaches for it,
        so the whole contract is testable against a plain list.
        """
        needle = (self.title_contains or "").strip().lower()
        rows = [
            {"id": p.id, "title": p.title or p.id}
            for p in pages
            if not needle or needle in (p.title or p.id).lower()
        ]
        rows.sort(key=lambda r: r["title"].lower())
        window = rows[self.offset : self.offset + self.limit]
        next_offset = self.offset + len(window)
        truncated = next_offset < len(rows)
        return {
            "pages": window,
            "count": len(window),
            "total": len(rows),
            "truncated": truncated,
            "nextOffset": next_offset if truncated else None,
        }


class WikiQaRequest(BaseModel):
    """``POST /v1/wiki/qa`` request body.

    Every field defaults rather than being required at the Pydantic layer:
    ``post_qa`` does its OWN required-field checks by hand (missing
    ``question``/``project`` → a ``validation`` error naming
    those fields, not a raw Pydantic 422), so this model's only two jobs are
    ``extra="forbid"`` rejecting an unknown field, and validating ``mode``
    against a closed ``Literal`` so an unrecognised value is a clean 400 naming
    the field.

    The ``answerId`` continuation branch never reads ``mode`` at all — a
    follow-up keeps the session's existing mode (see ``jobs.py``).
    """

    model_config = ConfigDict(extra="forbid")

    question: str = ""
    project: str = ""
    slug: str = ""
    fromPageId: str = ""
    model: str = ""
    answerId: str = ""
    mode: Literal["fast", "deep"] = "fast"


def _query_args() -> dict[str, str]:
    """Return the request's query params as a plain dict for model validation.

    Lives at module level, not on a wire model, because reading ``flask.request``
    is I/O and a model must never import it (the same rule that keeps the clock
    out of ``TriggerSpec``). ``api_key`` is dropped: the key guard accepts it
    as a query param for SSE consumers that cannot set headers, so it is auth
    TRANSPORT rather than a query argument — leaving it in would make every
    ``extra="forbid"`` query model reject an authenticated request.
    """
    return {k: v for k, v in request.args.items() if k != "api_key"}


def register(app, runtime, hook_manager=None) -> None:
    """Mount /v1/wiki/* routes on the given Flask app + attach runtime ref.

    When a ``hook_manager`` is supplied, two ``on_session_end`` hooks are
    registered (idempotent across repeated ``register`` calls):

    - :class:`QaSessionEndHook`: reconciles QA answers whose run ends without
      the terminal ``wiki_emit_answer`` call (one-shot nudge + models_used stamp).
    - :class:`WikiIndexingSessionEndHook`: marks non-terminal indexing jobs
      ``interrupted`` when their session ends — defense-in-depth so infra
      failures (tool-internal network / IO errors) hand off to ``JobRecovery``
      on next restart.

    A third callback, :class:`WikiJobTerminationCascade`, is registered on the
    runtime's ``on_terminate`` seam (the same one the trigger store cascades
    through) whenever the runtime exposes it. Session END and session
    TERMINATION are different events with opposite intents — see that class for
    why one hands the job to recovery and the other settles it — so they cannot
    share a registration. It is registered unconditionally because the callback
    is idempotent (a second run finds the job no longer active) and the runtime
    exposes no readable registry to dedupe against.
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
    on_terminate = getattr(runtime, "register_on_terminate", None)
    if callable(on_terminate):
        on_terminate(WikiJobTerminationCascade(runtime))
    register_error_handler(app)
    app.register_blueprint(_build_blueprint(), url_prefix="/v1/wiki")


def _build_blueprint() -> Blueprint:
    bp = Blueprint("wiki", __name__)

    @bp.route("/projects", methods=["GET"])
    @guard.requires("wiki.read")
    def list_projects():
        projects = _store().list_projects()
        return jsonify([p.model_dump(mode="json", by_alias=True) for p in projects])

    @bp.route("/projects/<path:slug>", methods=["DELETE"])
    @guard.requires("wiki.admin")
    def delete_project(slug: str):
        from mewbo_graph.wiki.credentials import (  # noqa: PLC0415
            CredentialScope,
            CredentialStore,
        )

        deleted = _store().delete_project(slug)
        # Exact-match delete on the repo scope ONLY — a host-scoped credential
        # is shared across every repo on that host (``CredentialScope.covers``),
        # so deleting one project must never cascade and take down auth for its
        # siblings.
        repo_scope = CredentialScope.coerce(slug)
        if repo_scope is not None:
            CredentialStore.delete(_store(), repo_scope)
        # Drop the editable settings record for the same reason the freshness
        # entry is evicted below: a re-created project with this slug must not
        # inherit the dead one's configuration (its model/ref/scope/graph-only).
        _store().delete_project_settings(slug)
        # Reap everything else the slug ever wrote. Removing only the three
        # records above would strand the pages, the code graph, the entity and
        # memory layers, the manifest, every job with its event log, and the Q&A
        # history: they are reachable only through a slug whose
        # project row no longer exists, and the one bulk reaper that does exist
        # (``supersede_graph_artifacts``) fires solely from a COMPLETED index
        # for that slug — which a deleted project can never run again. Such rows
        # would not merely leak, they would be unreachable by construction.
        # Synchronous rather than deferred: a background reaper would be a
        # second lifecycle to reason about, and the delete is already the slow,
        # rare, explicitly-confirmed action in this surface.
        reaped = _store().reap_slug(slug)
        # Evict any cached freshness so a re-created project with the same slug
        # never inherits the deleted one's stale badge.
        _freshness_cache.pop(slug, None)
        return jsonify({"deleted": deleted, "reaped": reaped})

    @bp.route("/projects/<path:slug>/settings", methods=["GET"])
    @guard.requires("wiki.read")
    def get_project_settings(slug: str):
        """Return the editable settings + credential status for a project.

        The settings a re-index would actually run with — the slug-keyed record if
        one exists, else reconstructed from the newest job submission, else derived
        from the Project. ``credential`` reports only WHETHER a git credential is on
        file and under which scope; a value is never echoed. A catalog (non-git)
        project returns the reduced ``kind: "catalog"`` shape.
        """
        return jsonify(WikiProjectSettings(_store()).read(slug))

    @bp.route("/projects/<path:slug>", methods=["PATCH"])
    @guard.requires("wiki.admin")
    def patch_project(slug: str):
        """Update a project's settings. Body is partial; omitted fields persist.

        Writable: ``model``, ``ref``, ``depth``, ``language``, ``filterMode``,
        ``dirs``, ``files``, ``graphOnly``, ``desc``, and a same-repo re-normalising
        ``repoUrl``/``platform``. A ``token``, a ``slug`` rename, or any system-owned
        field is rejected (400) rather than silently ignored — credentials go through
        the ONE registry at ``/v1/git/credentials/<scope>``.

        **This does not start a re-index.** Everything but ``desc`` takes effect the
        next time the project is indexed (the user drives that with Refresh), which
        is also why a settings edit doesn't touch the per-IP indexing rate limiter.

        One consequence worth knowing: turning ``graphOnly`` ON means the next index
        runs the deterministic zero-LLM path, which DROPS this project's existing
        documentation pages (they would otherwise linger unreachable behind the
        graph-only doc-read guard). Turning it back off regenerates them.
        """
        body = request.get_json(silent=True) or {}
        return jsonify(WikiProjectSettings(_store()).patch(slug, body))

    @bp.route("/projects/<path:slug>/pages/<string:page_id>", methods=["GET"])
    @guard.requires("wiki.read")
    def get_page(slug: str, page_id: str):
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

    @bp.route("/projects/<path:slug>/pages", methods=["GET"])
    @guard.requires("wiki.read")
    def list_project_pages(slug: str):
        """Return the page INDEX for *slug* — ``{id, title}`` rows, paginated.

        The page-content route above reads ONE page by id; nothing exposed a way
        to discover those ids. Bounded at the QUERY layer rather than by cutting
        a response: ``limit``/``offset`` (and an optional case-insensitive
        ``titleContains``) return a complete, small page of rows plus
        ``truncated`` + ``nextOffset``, so a caller pages forward instead of
        parsing a severed prefix.

        Not doc-guarded: ``list_pages`` is the roster, not page CONTENT (a
        graph-only project simply has none and returns an empty list) — the 409
        guard lives on ``get_page``.
        """
        store = _store()
        if store.get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        try:
            query = WikiPageIndexQuery.model_validate(_query_args())
        except Exception as exc:  # noqa: BLE001 — Pydantic validation → clean 400
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="invalid page-index query",
                    fields=_pydantic_fields(exc) or None,
                )
            )
        return jsonify(query.page(store.list_pages(slug)))

    @bp.route("/projects/<path:slug>/graph/neighbors", methods=["GET"])
    @guard.requires("wiki.read")
    def get_graph_neighbors(slug: str):
        """Traverse the code graph outward from one node — no model call, no run.

        Query params ARE :class:`WikiGraphNeighborsArgs` (the same model the
        in-session ``wiki_graph_neighbors`` tool parses), so ``hops``/``limit``
        bounds, the direction/edge-kind vocabularies and ``extra="forbid"`` have
        exactly one definition. Restating them here is how the two surfaces would
        drift apart the first time either bound changed.
        """
        store = _store()
        if store.get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        from mewbo_graph.plugins.wiki.graph_neighbors import (  # noqa: PLC0415
            WikiGraphNeighbors,
            WikiGraphNeighborsArgs,
        )

        try:
            args = WikiGraphNeighborsArgs.model_validate(_query_args())
        except Exception as exc:  # noqa: BLE001 — Pydantic validation → clean 400
            return wiki_error_response(
                WikiError(
                    code="validation",
                    message="invalid traversal query",
                    fields=_pydantic_fields(exc) or None,
                )
            )
        return jsonify(WikiGraphNeighbors(slug=slug, store=store).traverse(args))

    @bp.route("/projects/<path:slug>/graph", methods=["GET"])
    @guard.requires("wiki.read")
    def get_project_graph(slug: str):
        """Return the persisted knowledge graph for *slug* (Cytoscape shape).

        Returns 404 when the project is unknown so the FE can render a
        sensible "not indexed" empty state.
        """
        if _store().get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        try:
            node_limit_raw = request.args.get("limit", default=None)
            node_limit = int(node_limit_raw) if node_limit_raw else None
        except (TypeError, ValueError):
            node_limit = None
        # ``?hierarchy=1`` (truthy) synthesises a Folder scaffold + per-node
        # parentId so the FE can cluster/collapse by directory. Default off →
        # the wire carries the plain AST-only payload.
        hierarchy = request.args.get("hierarchy", "").lower() in {"1", "true", "yes"}
        from mewbo_graph.wiki.graph import KnowledgeGraphView  # noqa: PLC0415

        view = KnowledgeGraphView.for_slug(
            _store(), slug, node_limit=node_limit, hierarchy=hierarchy
        )
        return jsonify(view.to_wire())

    @bp.route("/projects/<path:slug>/source", methods=["GET"])
    @guard.requires("wiki.read")
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

    @bp.route("/projects/<path:slug>/freshness", methods=["GET"])
    @guard.requires("wiki.read")
    def get_project_freshness(slug: str):
        """Compare the indexed commit against the remote HEAD; ``?force=1`` busts the cache.

        Indexed sha comes off the ``Project`` record, falling back to the latest
        ``complete`` job's recorded commit for older projects that predate the
        field. Results are cached for :data:`_FRESHNESS_TTL_SECONDS` per slug (a
        check is a ``git ls-remote`` plus a platform compare-API call — cheap, but
        not free to poll on every card render); ``force=1`` bypasses a cached read
        and always recomputes (still refreshing the cache for the next caller).
        """
        store = _store()
        project = store.get_project(slug)
        if project is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )

        force = request.args.get("force", "").lower() in {"1", "true", "yes"}
        now = time.monotonic()
        cached = _freshness_cache.get(slug)
        if not force and cached is not None and (now - cached[0]) < _FRESHNESS_TTL_SECONDS:
            return jsonify(cached[1])

        indexed_sha = project.commit_sha
        if not indexed_sha:
            # Fallback for projects that predate ``Project.commit_sha``:
            # the newest ``complete`` job's own commit, via the ONE
            # latest-job lookup (never job_id — see ``store.latest_job``).
            latest_complete = store.latest_job(slug, statuses={"complete"})
            if latest_complete is not None and latest_complete.commit_sha:
                indexed_sha = latest_complete.commit_sha
        if not indexed_sha:
            return wiki_error_response(
                WikiError(
                    code="not_found",
                    message=f"project {slug} has no indexed commit to compare",
                )
            )

        from mewbo_graph.plugins.wiki.freshness import RepoFreshness  # noqa: PLC0415

        repo_url = project.repo_url or f"https://{slug}"
        result = RepoFreshness.check(
            repo_url,
            indexed_sha,
            ref=project.branch,
            platform=project.source,
            store=store,
            slug=slug,
        )
        body = {
            "indexedSha": indexed_sha,
            "remoteSha": result.remote_sha,
            "behindBy": result.behind_by,
            "upToDate": result.up_to_date,
            "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _freshness_cache[slug] = (now, body)
        return jsonify(body)

    @bp.route("/platforms", methods=["GET"])
    @guard.requires("wiki.read")
    def list_platforms():
        return jsonify([p.model_dump(mode="json", by_alias=True) for p in PLATFORMS])

    @bp.route("/languages", methods=["GET"])
    @guard.requires("wiki.read")
    def list_languages():
        return jsonify([la.model_dump(mode="json", by_alias=True) for la in LANGUAGES])

    @bp.route("/defaults", methods=["GET"])
    @guard.requires("wiki.read")
    def get_wiki_defaults():
        """Return wiki-specific defaults the picker should pre-select.

        Cost: ``O(1)`` — bounded config reads, with no store or proxy call.

        Each key is independent: set ``wiki.default_model`` (indexing),
        ``wiki.default_qa_model`` (Q&A — typically a smaller/faster
        model than indexing), ``wiki.default_depth``, or
        ``wiki.default_language`` in app.json to pin that field. ``embeddingModel``
        is the deployment default for project vectors; it deliberately names no
        vendor list because the proxy, not the server, determines which embedding
        models it supports. ``qaModel`` falls back to ``wiki.default_model`` when
        not separately set so a single ``default_model`` still works for both
        phases.
        """
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        out: dict[str, str] = {}
        model = get_config_value("wiki", "default_model", default="")
        # ``qaModel`` shares the one canonical Q&A chain (qa → wiki default →
        # llm default) so the picker pre-selects exactly what ``post_qa`` defaults.
        qa_model = _resolve_qa_model()
        depth = get_config_value("wiki", "default_depth", default="")
        language = get_config_value("wiki", "default_language", default="")
        embedding_model = get_config_value(
            "wiki", "embedding", "model", default="openai/text-embedding-3-small"
        )
        if model:
            out["model"] = model
        if qa_model:
            out["qaModel"] = qa_model
        if depth in ("comprehensive", "concise"):
            out["depth"] = depth
        if language:
            out["language"] = language
        if embedding_model:
            out["embeddingModel"] = str(embedding_model)
        return jsonify(out)

    @bp.route("/index/<string:job_id>", methods=["GET"])
    @guard.requires("wiki.read")
    def get_job_snapshot(job_id: str):
        job = _store().get_job(job_id)
        if job is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        return jsonify(_job_wire(job))

    @bp.route("/index/<string:job_id>/progress", methods=["GET"])
    @guard.requires("wiki.read")
    def get_job_progress(job_id: str):
        """Return the whole declared-and-observed progress context for one job.

        The response combines the job identity (``jobId``, ``slug``, ``phase``,
        ``status``, ``isActive``) with :meth:`ProgressLedger.export`, so a
        poller can learn the operation's declared outline, current position and
        remaining work from one object. A pre-ledger job returns the same valid
        empty ledger shape rather than a distinct absence case.

        Cost: ``O(one record)`` — reads one job and projects a ledger bounded by
        declared steps (tens), never by repository units.
        """
        job = _store().get_job(job_id)
        if job is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        ledger = job.progress or ProgressLedger()
        return jsonify({
            "jobId": job.job_id,
            "slug": job.slug,
            "phase": job.phase,
            "status": job.status,
            "isActive": job.is_active,
            **ledger.export(datetime.now(timezone.utc)),
        })

    @bp.route("/jobs/active", methods=["GET"])
    @guard.requires("wiki.read")
    def list_active_jobs():
        """Return all non-terminal jobs (queued/scanning/finalizing/interrupted).

        Powers the landing-page "Indexing now" surface; each job goes through
        the same ``_job_wire`` seam the per-job snapshot uses, so platform is
        hydrated (the FE composes canonical URLs without an extra hop) and the
        two endpoints serve one shape. Liveness is the job's OWN question
        (``IndexingJob.is_active``) — the same one ``_job_wire`` puts on the
        wire as ``isActive``, so this list and every consumer of that flag can
        never disagree about which statuses count as in-progress.
        """
        return jsonify([
            _job_wire(job) for job in _store().list_jobs() if job.is_active
        ])

    @bp.route("/jobs/recoverable", methods=["GET"])
    @guard.requires("wiki.read")
    def list_recoverable_jobs():
        """Return non-complete jobs that carry checkpoint artifacts worth resuming.

        Powers the console "Resume indexing" surface. A job qualifies when
        ``POST .../resume`` would actually accept it (``IndexingJob.is_resumable``
        — the same predicate ``WikiResume.resume`` guards on, so this listing can
        never advertise a job resume then refuses; notably ``cancelled`` is
        EXCLUDED — a deliberate user stop is not a resume candidate), has NOT been
        superseded by a later completed index for the same slug, AND has reached
        at least the graph or committed a plan / written pages — i.e. resuming it
        would actually save work (else it is a from-scratch re-index, not a
        resume). The payload is the smallest the FE needs per job; the ``recoverable``
        hint summarises what would be reused.
        """
        from mewbo_graph.wiki.resume import ResumePlan  # noqa: PLC0415

        store = _store()
        out = []
        for job in store.list_jobs():
            if not job.is_resumable:
                continue
            # Is this terminal signal still true, or did a later index for the
            # same slug already finish? The job's own status can't answer that
            # — only the store's authority on "what's the newest completed
            # attempt" can (never job_id; see ``store.latest_job``). A job with
            # no ``phase_started_at`` (never even cloned) sorts oldest and is
            # therefore always superseded by any completed index.
            latest_complete = store.latest_job(job.slug, statuses={"complete"})
            if latest_complete is not None and (
                (latest_complete.phase_started_at or "")
                >= (job.phase_started_at or "")
            ):
                continue
            plan = ResumePlan.build(store, job)
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

    @bp.route("/sessions/<string:session_id>", methods=["GET"])
    @guard.requires("wiki.read")
    def get_session_link(session_id: str):
        """Resolve a Mewbo session id to the wiki project it belongs to.

        Reverse lookup over the two forward mappings the indexing/QA
        session-end hooks already maintain (``attach_job_session`` /
        ``attach_qa_session`` — see ``WikiIndexingSessionEndHook`` /
        ``QaSessionEndHook``): pure passthrough, no new domain logic.
        Indexing is checked first — a session backs at most one of the two.

        Backs the console's session-header "Open wiki" jump: a wiki-origin
        session's context only ever advertises the ``wiki`` capability
        (``jobs.py``'s ``{"client_capabilities": ["wiki"]}``), never the
        project slug, so the console has no other way to resolve one from a
        bare session id.

        Returns ``{"slug": <project slug>, "kind": "indexing" | "qa"}``, or a
        404 ``not_found`` :class:`WikiError` when *session_id* isn't a wiki
        session at all.

        The ``indexing`` branch also carries ``jobId`` + ``active``, because
        "which project" does not answer the question a user watching a RUNNING
        index actually has. Progress — phase, the node/page counters, ETA —
        lives only on the indexing screen, which is addressed by job id; the
        session transcript has no progress rendering of its own, so a session
        that could only resolve a slug stranded its viewer on a project front
        door (and on a FIRST index, on the gallery, since the project has no
        landing page yet). ``active`` is ``IndexingJob.is_active``, the same
        predicate ``_job_wire`` puts on the wire as ``isActive`` — it is what
        lets the console send a live run to the progress bar while a finished
        one still resolves to the project, which is the right destination once
        there is something to read.

        The ``qa`` branch additionally carries the answer's own coordinates —
        ``answerId``/``fromPageId``/``question`` — because the two kinds have
        different destinations: an indexing session belongs to a PROJECT (the
        console resolves its landing page), while a Q&A session belongs to one
        ANSWER, which is addressable on its own (``?answer=<id>``). They come
        off the answer already loaded here, so this costs no extra lookup; the
        alternative is a console that knows a session is Q&A-backed and routes
        it to the project's front door anyway. ``fromPageId`` is what keeps the
        deep link honest — the console route falls back to a hardcoded ``core``
        page id, which a project need not have, so omitting it would caption
        the answer with a page it was never generated from and leave the
        up-link pointing at one that may not resolve.
        """
        store = _store()
        job_id = store.find_job_by_session(session_id)
        if job_id:
            job = store.get_job(job_id)
            if job is not None:
                return jsonify({
                    "slug": job.slug,
                    "kind": "indexing",
                    "jobId": job_id,
                    "active": job.is_active,
                })
        answer_id = store.find_qa_by_session(session_id)
        if answer_id:
            answer = store.get_qa(answer_id)
            if answer is not None:
                return jsonify({
                    "slug": answer.slug,
                    "kind": "qa",
                    "answerId": answer.answer_id,
                    "fromPageId": answer.from_page_id,
                    "question": answer.question,
                })
        return wiki_error_response(
            WikiError(
                code="not_found",
                message=f"session {session_id} is not linked to a wiki project",
            )
        )

    @bp.route("/qa/<string:answer_id>", methods=["GET"])
    @guard.requires("wiki.read")
    def get_qa_snapshot(answer_id: str):
        """Replay a persisted answer — the idempotent ``?answer=<id>`` source.

        ``sessionId`` is stamped at READ time from ``store.get_qa_session``,
        exactly as ``_job_wire`` does for an indexing job and for the same
        reason: that binding already lives on its own store surface
        (``attach_qa_session``), deliberately outside :class:`QaAnswer`, so
        carrying it on the model too would make two writers of one fact and
        every non-destructive turn update would have to remember both. An
        answer with no backing session omits the key rather than sending an
        empty string, so the console reads absence as "nothing to watch" and
        renders no affordance instead of a dead one. Best-effort like the job
        seam: a store hiccup must degrade the jump, not fail the replay.
        """
        ans = _store().get_qa(answer_id)
        if ans is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"answer {answer_id} not found")
            )
        # Resolve ``graph:<node_id>`` refs to readable labels (entity hashes →
        # ``name (type)`` / ``file#Symbol``) for BOTH provenance panels: the
        # retrieval-details trail (accessedSources) and the cited panel
        # (summarySources — which now folds in the file/graph the answer grounded on,
        # so its graph refs need the same humanising). NON-destructive: the
        # stored snapshot keeps raw ids (the memory depositor anchors off them) — only
        # the wire is humanised.
        from mewbo_graph.wiki.qa import AccessedSourceResolver  # noqa: PLC0415

        data = ans.model_dump(mode="json", by_alias=True)
        data["accessedSources"] = AccessedSourceResolver.resolve_refs(
            _store(), ans.slug, ans.accessed_sources
        )
        data["summarySources"] = AccessedSourceResolver.resolve_refs(
            _store(), ans.slug, ans.summary_sources
        )
        try:
            session_id = _store().get_qa_session(answer_id)
        except Exception:
            session_id = None
        if session_id:
            data["sessionId"] = session_id
        return jsonify(data)

    @bp.route("/index", methods=["POST"])
    @guard.requires("wiki.write")
    def post_index():
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
        # Graph-only (zero-LLM) indexing is a developer-mode feature: honour
        # ``graphOnly`` ONLY when ``runtime.developer_mode`` is on; otherwise
        # force it False so an unprivileged caller can never opt into the
        # no-docs path (the wire field is accepted but ignored).
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        if submission.graph_only and not get_config_value(
            "runtime", "developer_mode", default=False
        ):
            submission = submission.model_copy(update={"graph_only": False})
        try:
            # The REAL hook manager, not ``None``: ``Orchestrator`` turns a
            # missing one into a FRESH EMPTY ``HookManager``, so an index
            # started here would run without ``WikiIndexingSessionEndHook`` and
            # a session that died mid-phase would never hand its job to
            # recovery. Every start path threads the registered instance.
            job = WikiIndexingJob.start(
                submission, runtime=_runtime, hook_manager=_hook_manager
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

    @bp.route("/branches", methods=["POST"])
    @guard.requires("wiki.write")
    def post_branches():
        """List a remote repo's branches so the wizard can pick a ref to index.

        An explicit body ``token`` is EXCLUSIVE: the wizard is testing a specific,
        not-yet-saved credential, so ONLY that token is tried and an auth-class
        rejection is surfaced (400 ``validation``) instead of being masked by a
        stored/ambient/anonymous fallback that happens to work — masking here would
        let the wizard "succeed" and then durably persist the untested-bad token
        at the first index. When NO explicit token is sent, resolution falls back to the
        SAME canonical chain every other consumer uses (``resolve_chain``):
        repo-scoped store → host-scoped store → the ambient (built-in) git
        credential → anonymous, each tried in order with an auth-class failure
        (``is_auth_failure``) advancing to the next so a revoked/wrong stored
        credential doesn't shadow a valid fallback. A non-auth failure (network,
        timeout, ...) propagates immediately as the standard ``repo_access``
        envelope — those never succeed on retry. Returns ``{branches, defaultBranch}``.
        """
        try:
            req = BranchListRequest.model_validate(request.get_json(silent=True) or {})
        except Exception as exc:
            fields = _pydantic_fields(exc)
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=fields or None)
            )

        from mewbo_graph.plugins.wiki.branches import (  # noqa: PLC0415
            BranchListError,
            RemoteBranchLister,
        )
        from mewbo_graph.wiki.credentials import (  # noqa: PLC0415
            CredentialScope,
            is_auth_failure,
            resolve_chain,
        )

        def _list_heads(
            token: str | None, ssh_key: str | None, username: str | None = None
        ) -> dict[str, Any]:
            """One branch-list attempt; returns the JSON reply body or raises.

            *username* threads a stored credential's own username (GitLab oauth2 /
            deploy-token style) into the injected URL exactly as clone/freshness/
            validate do — else a username-bearing credential would auth everywhere
            but here, 401-ing the branch-list step on those platforms.
            """
            remote = RemoteBranchLister(
                url=req.repo_url, token=token, ssh_key=ssh_key, username=username
            ).list_heads()
            return {"branches": remote.branches, "defaultBranch": remote.default_branch}

        # An explicit body token is EXCLUSIVE — try ONLY it, never fall through to
        # the store/ambient/anonymous chain. A stored/ambient success masking a
        # rejected typed token would let the wizard "succeed" here and then durably
        # persist the untested-bad token at the first index; an auth-class rejection is
        # a 400 (bad user input), not the generic repo_access envelope.
        if req.token:
            try:
                return jsonify(_list_heads(req.token, None))
            except BranchListError as exc:
                if is_auth_failure(str(exc)):
                    return wiki_error_response(WikiError(
                        code="validation",
                        message="the provided token was rejected by the remote",
                        fields={"token": "rejected"},
                    ))
                return wiki_error_response(WikiError(code="repo_access", message=str(exc)))

        # No explicit token — resolve via the canonical chain. No job/slug context
        # yet before the first index, so fall back to the URL's bare HOST scope so a
        # host-scoped credential (and the host-keyed ambient lookup) still resolves
        # even when the wizard hasn't chosen a slug yet. An unparseable slug/URL
        # coerces to no scope at all — the chain then yields anonymous alone, which
        # is exactly the public-repo path.
        url_scope = CredentialScope.coerce(req.repo_url)
        scope = CredentialScope.coerce(req.slug) or (
            url_scope.host_scope() if url_scope else None
        )
        last_exc: BranchListError | None = None
        for candidate in resolve_chain(_store(), scope):
            try:
                return jsonify(
                    _list_heads(candidate.token, candidate.ssh_key, candidate.username)
                )
            except BranchListError as exc:
                last_exc = exc
                if is_auth_failure(str(exc)):
                    continue
                return wiki_error_response(WikiError(code="repo_access", message=str(exc)))
        return wiki_error_response(
            WikiError(
                code="repo_access",
                message=str(last_exc) if last_exc else "unable to list remote branches",
            )
        )

    @bp.route("/index/<string:job_id>", methods=["DELETE"])
    @guard.requires("wiki.admin")
    def delete_index(job_id: str):
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
        return jsonify(_job_wire(snapshot))

    @bp.route("/index/<string:job_id>/resume", methods=["POST"])
    @guard.requires("wiki.write")
    def resume_index(job_id: str):
        """Checkpoint-aware resume of an interrupted index (reuses the SAME job_id).

        Re-clones at the recorded commit + skips the expensive idempotent phases
        whose store artifacts already exist (graph / enrich / plan), writing only
        the remaining pages. User-initiated, so it resets the per-slug auto-recovery
        cap. An optional ``{"restart": true}`` body forces the distinct REBUILD
        intent (``ResumePlan.for_restart()``) instead of the default checkpoint
        resume — every phase re-runs, still reusing the same job_id + recorded
        commit. Returns ``{job_id, session_id, status}`` mirroring start/refresh.
        """
        if _store().get_job(job_id) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        try:
            req = ResumeIndexRequest.model_validate(request.get_json(silent=True) or {})
        except Exception as exc:
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=_pydantic_fields(exc) or None)
            )
        try:
            result = WikiResume.resume(
                _store(), _runtime, job_id, hook_manager=_hook_manager, restart=req.restart
            )
        except KeyError:
            return wiki_error_response(
                WikiError(code="not_found", message=f"job {job_id} not found")
            )
        except ValueError as exc:
            return wiki_error_response(
                WikiError(code="validation", message=str(exc))
            )
        except ResumeCountError as exc:
            # A transient store-read failure the resume decision depends on —
            # refuse rather than silently rebuild (ResumeCountError IS a
            # RuntimeError, so this branch MUST precede the in-flight one below).
            return wiki_error_response(
                WikiError(code="network", message=str(exc)), status=503
            )
        except RuntimeError as exc:
            # A resume already in flight for this job (double-submit / a stacked
            # manual resume racing an automatic recovery) — a conflict, not a
            # malformed request. Mirrors POST /qa's follow_up in-flight mapping.
            return wiki_error_response(
                WikiError(code="validation", message=str(exc)), status=409
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
    @guard.requires("wiki.read")
    def stream_index(job_id: str):
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
    @guard.requires("wiki.read")
    def post_qa():
        body = request.get_json(silent=True) or {}
        try:
            parsed = WikiQaRequest.model_validate(body)
        except ValidationError as exc:
            return wiki_error_response(WikiError(
                code="validation",
                message=str(exc),
                fields=_pydantic_fields(exc),
            ))
        question = parsed.question.strip()
        # Optional continuation: an existing answer's session is re-engaged
        # with this question appended as a new turn instead of starting a
        # fresh session. Absent ⇒ today's behaviour, unchanged.
        # Continuation only needs ``question`` — ``project``/``fromPageId``/
        # ``model``/``mode`` ride the existing answer (mode is NEVER taken
        # from the request here — a follow-up keeps the session's existing
        # mode, per ``jobs.py``), so it validates independently of the
        # new-session ``{question, project}`` combined check below.
        answer_id = parsed.answerId.strip()
        if answer_id:
            if not question:
                return wiki_error_response(WikiError(
                    code="validation",
                    message="question is required",
                    fields={"question": "required"},
                ))
            prior = _store().get_qa(answer_id)
            if prior is None:
                return wiki_error_response(
                    WikiError(code="not_found", message=f"answer {answer_id} not found")
                )
            try:
                answer = WikiQaSession.follow_up(
                    answer_id,
                    question,
                    runtime=_runtime,
                    hook_manager=_hook_manager,
                )
            except LookupError as exc:
                return wiki_error_response(WikiError(code="not_found", message=str(exc)))
            except RuntimeError as exc:
                # The session already has a run in flight (double-submit /
                # client retry racing the still-streaming prior turn) — a
                # conflict, not a malformed request.
                return wiki_error_response(
                    WikiError(code="validation", message=str(exc)), status=409
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
        from_page_id = parsed.fromPageId.strip()
        mode = parsed.mode
        # ``model`` is optional — default it from config (qa → wiki → llm,
        # fast-mode preferring its own key first) so the MCP ``ask_wiki`` tool
        # can omit it entirely.
        model = parsed.model.strip() or _resolve_qa_model(mode)
        # Public param is ``project``; ``slug`` is the internal name. Accept
        # either in the body but report validation against the public ``project``.
        slug = (parsed.project or parsed.slug).strip()
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
        # A graph-only (developer-mode) project has NO documentation — reject Q&A
        # at the route (mirrors the ``get_page`` check). Without this, the QA probe's
        # ``wiki_read_page`` raises ``DocumentationUnavailableError`` INSIDE a
        # SessionTool, where the Flask errorhandler never fires and the orchestrator
        # flails instead of returning a clean 409.
        project = _store().get_project(slug)
        if project is not None and getattr(project, "graph_only", False):
            return documentation_unavailable_response(slug)
        try:
            answer = WikiQaSession.start(
                slug=slug,
                question=question,
                from_page_id=from_page_id,
                model=model,
                mode=mode,
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
    @guard.requires("wiki.write")
    def delete_qa(answer_id: str):
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
    @guard.requires("wiki.read")
    def stream_qa(answer_id: str):
        """Replay-from-start SSE for shared QA URLs."""
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
    @guard.requires("wiki.write")
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
        ingestor = InsightIngestor.from_store(
            _store(), slug=slug, llm=llm, condenser=condenser
        )
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
    @guard.requires("wiki.write")
    def post_documents(slug: str):
        """Programmatically ingest catalog documents into a NON-git project.

        Body: ``{documents: [{id, title, text, metadata?}]}``. Creates the
        project for *slug* if it does not yet exist (no git URL required) and
        writes each record as a wiki page + an embedded graph node, so the
        existing :class:`HybridRetriever` / Q&A ground over it unchanged.
        Synchronous (a modest batch is a deterministic write — no agent loop).
        Returns the :class:`CatalogIngestReport` (201 when ≥1 doc ingested).
        """
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
    @guard.requires("wiki.write")
    def refresh_project(slug: str):
        """Re-index an existing project (on-demand only), scoped where it is safe.

        Optional body ``{"mode": "auto"|"full"}``. An absent/empty body — or a
        body that omits ``mode`` — runs the deployment's configured
        ``wiki.refresh.default_mode`` (shipped as ``auto``, which tries the
        cheap scoped delta pass and falls back to a full rebuild wherever reuse
        cannot be justified). An explicitly requested mode always wins over that
        setting. An unknown mode or an unrecognised field is a 400 naming it,
        never a silent fall-through to the default.

        Returns ``{queued, jobId, refresh}`` where ``refresh`` is the
        :class:`RefreshDecision` that was actually taken — a full rebuild names
        its reason, which is what a console renders beside the scope preview.

        **Cost: `O(1)` plus one fingerprint probe.** The indexing work itself is
        `O(repo)` and runs on a background thread or an agent session, so the
        handler only enqueues it. The one thing it does NOT get for free is the
        refresh decision: it reads the already-loaded project record and calls
        the no-clone fingerprint probe (config + installed package metadata + a
        ``shutil.which``), which is bounded and touches no network and no
        repository — but it IS local I/O on the request path, so it is named
        here rather than rounded down to `O(1)`.
        """
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
        # carries the real clone URL refresh restores). ``is_catalog`` is the ONE
        # definition of that test, shared with the settings façade.
        if WikiProjectSettings(_store()).is_catalog(slug, project.repo_url):
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
            req = RefreshProjectRequest.model_validate(request.get_json(silent=True) or {})
        except Exception as exc:
            return wiki_error_response(
                WikiError(
                    code="validation", message=str(exc), fields=_pydantic_fields(exc) or None
                )
            )
        try:
            # Same reason as the index route above: a hookless refresh runs
            # without the session-end reconciler that hands a stranded job to
            # recovery.
            job = WikiIndexingJob.refresh(
                slug,
                mode=req.resolve_mode(_configured_refresh_mode()),
                runtime=_runtime,
                hook_manager=_hook_manager,
            )
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        # A refresh re-indexes at the latest HEAD, so any cached freshness for
        # this slug is now stale — evict it so the next poll recomputes instead
        # of showing "behind by N" against the commit we just started rebuilding.
        _freshness_cache.pop(slug, None)
        # Additive: ``queued`` keeps its meaning for every caller, and the two
        # extra keys answer what it alone cannot — which job to watch, and
        # whether it will be cheap. ``refresh`` is
        # stamped on both arms of ``WikiIndexingJob.refresh``; the ``None`` arm
        # keeps the KEY present rather than letting its absence have to be read
        # as a third meaning.
        decision = job.refresh_decision
        return jsonify({
            "queued": True,
            "jobId": job.job_id,
            "refresh": (
                decision.model_dump(mode="json", by_alias=True) if decision else None
            ),
        })

    @bp.route("/projects/<path:slug>/session", methods=["POST"])
    @guard.requires("wiki.write")
    def open_maintainer_session(slug: str):
        """Get-or-create the maintainer session for an indexed project.

        Returns ``{"sessionId": ..., "created": bool}``. ``created`` is false
        when the project already had a live maintainer session — the affordance
        is a "take me to it" link as much as a "start one" button, and a caller
        that renders the two differently needs to know which happened.

        **Idempotent by construction**, so clicking twice cannot leave two
        sessions behind: the session is keyed by a slug-derived tag, and the tag
        collection maps one tag to one session. A session that was permanently
        TERMINATED is replaced rather than resurrected, since termination is
        one-way; the caller sees ``created: true`` for that, which is what it is.

        An optional body ``{"newSession": true}`` asks for a session that is NOT the
        canonical maintainer, always minting and always reporting
        ``created: true``. That is the composer's shape — a caller starting a new
        conversation about this project — while the default get-or-create is the
        project card's. Both are correct for their own caller, which is why the
        one endpoint serves both rather than the composer reusing a transcript
        it never opened.

        No run is started and no indexing job is created. The session is created
        bound to the project — tool ceiling, playbook and slug written to its
        context — and the first message drives the first turn through
        ``POST /api/sessions/<id>/query`` like any other session.

        404 when the slug names no indexed project: a maintainer's whole tool
        surface reads pages, graph and a checkout that only an index produces,
        so a session against an unindexed slug would open and then be unable to
        do anything.

        **Cost: `O(one record)`** — a project read, a tag read, and the session
        create plus its two appends.
        """
        store = _store()
        if store.get_project(slug) is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"project {slug} not found")
            )
        try:
            req = MaintainerSessionRequest.model_validate(request.get_json(silent=True) or {})
        except Exception as exc:
            return wiki_error_response(
                WikiError(
                    code="validation", message=str(exc), fields=_pydantic_fields(exc) or None
                )
            )
        try:
            session_id, created = WikiMaintainerSession.open(
                slug, runtime=_runtime, store=store, fresh=req.new_session
            )
        except Exception as exc:
            return wiki_error_response(WikiError(code="internal", message=str(exc)))
        return jsonify({"sessionId": session_id, "created": created})

    return bp
