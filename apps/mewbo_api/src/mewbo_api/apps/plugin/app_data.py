#!/usr/bin/env python3
"""The ``app_data`` SessionTool — the maintainer's scoped, validated data plane.

The maintainer session (tagged ``app:<app_id>``) is re-woken by a pipeline
trigger to ingest / transform data; ``app_data`` is how it writes and reads its
app's collections. It is the ONLY agent-side ingress to ``app_data``,
so it owns three guarantees the store below it cannot:

1. **Scope.** A call may only touch an app whose ``maintainer_session_id`` is THIS
   session. A foreign or unknown ``app_id`` reads ``not_found`` uniformly — no
   existence leak (the ``schedule_trigger`` precedent).
2. **Schema.** An ``upsert`` document is validated against the target
   collection's declared JSON Schema (``CollectionSpec.validate_doc``) before it
   is persisted — a bad doc is a clean error the agent self-corrects on, never a
   silent malformed write.
3. **Provenance.** A write increments the pipeline's OPEN ledger entry
   (``get_open`` -> ``record_write`` -> ``save``), keeping the ``PipelineRun``
   ``docs_written`` count — the freshness signal + repair-loop input — honest.

Terminal-free (the ``schedule_trigger`` / ``update_todos`` shape): reading and
writing app data is normal work, never a run exit. Errors return the shared
``{"error": {"code", "message"}}`` envelope that the loop's
``_session_tool_error_envelope`` reclassifies as a FAILED step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import jsonschema
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_api.apps.plugin.runtime import (
    AppDataStore,
    AppStore,
    PipelineRunStore,
    session_tags_for,
)
from mewbo_api.apps.staging import AppStagingArea
from mewbo_api.apps.store import (
    CollectionCapExceeded,
    get_app_data_store,
    get_app_store,
    get_pipeline_run_store,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mewbo_core.classes import ActionStep
    from mewbo_core.contracts.types import Event

    from mewbo_api.apps.models import AppSpec, CollectionSpec

logging = get_logger(name="apps.plugin.app_data")

APP_DATA_TOOL_ID = "app_data"

# Bound a single query so a runaway `limit` can't pull the whole collection into
# the model's context.
_MAX_QUERY_LIMIT = 1000

# The tool's OWN ceiling on a serialized `query` result — strictly below the
# loop's generic per-session-tool cap (`DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS`,
# 200_000) so the loop's undifferentiated `content_str[:max_chars]` cut can
# never fire on this path and slice a document in half. The headroom (20_000
# chars) is not a real reservation against that cap — it exists so this tool's
# OWN envelope math is the one deciding what gets cut, by whole documents, with
# the drop reported honestly instead of an amputated Python repr the loop
# hands back unparseable.
_MAX_QUERY_RESULT_CHARS = 180_000


# ------------------------------------------------------------------
# Tool args
# ------------------------------------------------------------------


class AppDataArgs(BaseModel):
    """Read or write your app's data — the ONLY way an app's data changes.

    `operation="upsert"` writes one document (`key` + `doc`) into a collection,
    validated against that collection's JSON Schema. `operation="query"` reads
    documents back (optional `filter`, `limit`). `operation="delete"` removes one
    document by `key`. You may only touch the app you maintain. Set `pipeline` on
    a write so the run's provenance ledger counts it.

    A `query` result carries TWO independent bounds and reports each honestly
    rather than letting one masquerade as the other. `limit` bounds which
    documents get FETCHED — `more_available` on the result says whether the
    collection held more matches than `limit` let through, so `count: 1000`
    against `limit: 1000` never reads as "that's everything" the way it used
    to. Separately, a character ceiling bounds whether the documents already
    fetched FIT in the response — when they don't, whole documents are dropped
    from the tail (never cut mid-record), reported via
    `output_truncated`/`documents_omitted`. `count` is always how many
    documents actually came back. Only `more_available is False AND
    output_truncated is False` means you have the complete matching set; plan
    pagination (a narrower `filter`, or a follow-up `query` past what you
    already have) off either flag, never off `limit` alone.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["upsert", "query", "delete"] = Field(
        description="`upsert` writes one doc; `query` reads docs; `delete` removes one doc.",
    )
    app_id: str = Field(description="The id of the app you maintain.")
    collection: str = Field(
        description="Which declared collection to read or write (one of the app's collections).",
    )
    key: str | None = Field(
        default=None,
        description="`upsert`/`delete` only: the document's stable key (idempotent upsert key).",
    )
    doc: dict[str, Any] | None = Field(
        default=None,
        description="`upsert` only: the document body, validated against the collection schema.",
    )
    filter: dict[str, Any] | None = Field(
        default=None,
        description="`query` only: an optional equality filter over document fields.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=_MAX_QUERY_LIMIT,
        description=(
            "`query` only: max documents to return (1-1000). Two things can leave "
            "you short of that: the collection may hold more matches than `limit` "
            "(see `more_available` on the result), and the response is additionally "
            "capped by total serialized size (see `output_truncated`/"
            "`documents_omitted`). Narrow `filter` rather than assuming `limit` "
            "alone bounds what you have."
        ),
    )
    sort: str | None = Field(
        default=None,
        description=(
            "`query` only (optional): a `doc` field to sort by, `-`-prefixed for "
            "descending. Omit for newest-first by update time."
        ),
    )
    pipeline: str | None = Field(
        default=None,
        description=(
            "`upsert` only (optional): the pipeline this write belongs to, so its "
            "provenance run counts the write. Use the pipeline name from your wake prompt."
        ),
    )

    @model_validator(mode="after")
    def _check_operation_fields(self) -> AppDataArgs:
        """Require the per-operation fields (fail closed at the boundary)."""
        if self.operation == "upsert":
            if not (self.key and self.key.strip()):
                raise ValueError("operation=upsert requires a non-empty `key`")
            if self.doc is None:
                raise ValueError("operation=upsert requires `doc`")
        elif self.operation == "delete" and not (self.key and self.key.strip()):
            raise ValueError("operation=delete requires a non-empty `key`")
        return self


APP_DATA_SCHEMA: dict[str, object] = pydantic_to_openai_tool(AppDataArgs, name=APP_DATA_TOOL_ID)


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class AppDataTool:
    """Handles ``app_data`` — scoped upsert/query/delete over one app's collections.

    Satisfies the :class:`~mewbo_core.tooling.session_tools.SessionTool` Protocol; the
    three store collaborators resolve from workstream A's process-wide store
    factories (``get_app_store`` / ``get_app_data_store`` / ``get_pipeline_run_store``)
    when not passed explicitly (the plugin-manifest build path). A test injects
    fakes directly through the constructor.
    """

    tool_id: str = APP_DATA_TOOL_ID
    schema: dict[str, object] = APP_DATA_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES
    # The SessionTool declaration law (tooling/CLAUDE.md): declared here so the
    # loop's cap and _query's own bound agree — `_query` never produces a
    # payload past this, so the loop's generic cut is structurally unreachable.
    max_result_chars: int = _MAX_QUERY_RESULT_CHARS

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        app_store: AppStore | None = None,
        data_store: AppDataStore | None = None,
        run_store: PipelineRunStore | None = None,
        tags_reader: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        """Bind the session id + the three store collaborators and the tag reader.

        Args:
            session_id: The bound session — the scope boundary (only the app the
                SERVER bound this session to may be touched).
            event_logger: Reserved for parity with the plugin build path (this
                tool emits no transcript event; the ledger is the record).
            app_store: Manifest read store (scope check + collection lookup).
            data_store: The app-ID-keyed data plane.
            run_store: The provenance ledger store.
            tags_reader: Reads this session's server-stamped tags — the second
                binding tier. Injected rather than reached for, because the
                default (``session_tags_for``) resolves a process-wide session
                store: a test could otherwise never drive the tag tier THROUGH
                this tool, which is why the tier could drift out of three of the
                four app tools without a single test noticing.
            A ``None`` store (the plugin path) is resolved from A's process-wide
            store factory at handle time.
        """
        self._session_id = session_id
        self._event_logger = event_logger
        self._app_store = app_store
        self._data_store = data_store
        self._run_store = run_store
        self._tags_reader = tags_reader or session_tags_for

    def should_terminate_run(self) -> bool:
        """Never terminates — reading/writing app data is normal work."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate, authorize against this session's app, then dispatch the op."""
        try:
            args = AppDataArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err("validation", str(exc))

        app_store, data_store, run_store = self._resolve_stores()
        if app_store is None or data_store is None or run_store is None:
            return self._err("unavailable", "the apps runtime is not configured")

        app = self._resolve_app(app_store, args.app_id)
        if app is None:
            # Uniform not_found for missing AND foreign — no existence leak.
            return self._err(
                "not_found",
                f"no app {args.app_id!r} bound to this session — the server binds an "
                "app by its owner/maintainer session or by a stamped app tag. Call "
                "get_app(operation='get') to see which app (if any) this session is "
                "bound to, and pass that app_id.",
            )

        collection = self._collection(app, args.collection)
        if collection is None:
            declared = [c.name for c in app.collections]
            return self._err(
                "validation",
                f"unknown collection {args.collection!r}; declared: {declared}",
            )

        if args.operation == "upsert":
            return self._upsert(
                args, collection, data_store, run_store,
                max_docs=app.policies.max_docs_per_collection,
            )
        if args.operation == "query":
            return self._query(args, data_store)
        return self._delete(args, data_store)

    # -- operations ---------------------------------------------------------

    def _upsert(
        self,
        args: AppDataArgs,
        collection: CollectionSpec,
        data_store: AppDataStore,
        run_store: PipelineRunStore,
        *,
        max_docs: int,
    ) -> MockSpeaker:
        """Persist the doc (store validates + caps), then count it on the ledger.

        *max_docs* is the app's ``policies.max_docs_per_collection``: a NEW key at
        the cap is refused with a clean agent-visible error, while an update to an
        existing key always goes through (the store owns the insert-vs-update
        distinction — see :class:`CollectionCapExceeded`).
        """
        assert args.key is not None and args.doc is not None  # noqa: S101 — guarded by validator
        try:
            # Passing collection_spec makes the store the single validation owner
            # (A's _validate raises jsonschema.ValidationError, the same type the
            # model's CollectionSpec.validate_doc raises); max_docs makes it the
            # single cap owner (it alone knows new-vs-existing + the live count).
            data_store.upsert(
                args.app_id, args.collection, args.key, args.doc,
                collection_spec=collection, max_docs=max_docs,
            )
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            return self._err("schema", f"doc field '{path}': {exc.message}")
        except CollectionCapExceeded as exc:
            return self._err("cap", str(exc))
        self._record_write(args, run_store)
        return self._ok(
            {"operation": "upsert", "app_id": args.app_id, "collection": args.collection,
             "key": args.key}
        )

    def _query(self, args: AppDataArgs, data_store: AppDataStore) -> MockSpeaker:
        """Read documents back — key/doc/updated_at per row (updated_at as ISO).

        Two independent bounds, and they fail differently. ``limit`` bounds
        WHICH documents get fetched from the store — this method asks for
        ``limit + 1`` (mirroring ``AppsRoutesController.read_data``'s own
        ``limit + 1`` probe) purely to learn whether the store held more
        matching documents than ``limit`` allowed through, without widening
        what a caller may ask for (``AppDataArgs.limit`` still REFUSES
        anything over :data:`_MAX_QUERY_LIMIT` at the boundary). That answer
        is ``more_available``. Separately, the char ceiling
        (:data:`_MAX_QUERY_RESULT_CHARS`, strictly under the loop's generic
        per-tool cap) bounds whether the documents THIS call already fetched
        (up to ``limit`` of them) fit in the serialized payload — a large one
        is trimmed by WHOLE documents dropped from the tail, never cut
        mid-record the way the loop's undifferentiated character slice would,
        and that drop is reported via ``output_truncated``/
        ``documents_omitted``. A caller reading ``more_available=False`` and
        ``output_truncated=False`` together has the true, complete matching
        set; either flag alone means more exists somewhere. ``count`` stays
        ``len(documents)`` throughout — what actually came back.
        """
        docs = data_store.query(
            args.app_id, args.collection, filter=args.filter, limit=args.limit + 1,
            sort=args.sort,
        )
        more_available = len(docs) > args.limit
        docs = docs[: args.limit]
        rows = [
            {"key": d.key, "doc": d.doc, "updated_at": d.updated_at.isoformat()}
            for d in docs
        ]
        kept, omitted = self._fit_query_rows(args.app_id, args.collection, rows)
        return self._ok(
            {
                "operation": "query",
                "app_id": args.app_id,
                "collection": args.collection,
                "count": len(kept),
                "documents": kept,
                "more_available": more_available,
                "output_truncated": omitted > 0,
                "documents_omitted": omitted,
                "result_char_limit": _MAX_QUERY_RESULT_CHARS,
            }
        )

    @staticmethod
    def _fit_query_rows(
        app_id: str, collection: str, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Greedily keep whole rows from the head within :data:`_MAX_QUERY_RESULT_CHARS`.

        Exact, not estimated, and O(n) over the row count — never a re-serialize
        of the whole growing payload per row (that would be O(n^2) over a
        200KB-class payload). ``str(dict)``'s ``documents`` value serializes as
        ``"[" + ", ".join(repr(d) for d in rows) + "]"``, so each row's
        contribution to the total length is exactly its own ``len(str(row))``
        plus a 2-char ``", "`` separator for every row after the first — the
        envelope (everything except ``documents``) is measured ONCE, with
        ``documents: []``.

        The envelope measurement uses the WORST-CASE width for the fields whose
        final values depend on how many rows get kept: ``count`` and
        ``documents_omitted`` are measured at the pre-drop row total (the final
        values can only have equal or fewer digits, since both are <= that
        total), and ``output_truncated``/``more_available`` are measured as
        ``False`` (their longer spelling — this method doesn't know the real
        ``more_available`` value and doesn't need to, since ``False`` upper-bounds
        either outcome). That reservation is therefore always >= the real envelope
        length, so the actual serialized result — envelope plus kept rows —
        never exceeds the cap.
        """
        total = len(rows)
        worst_case_envelope = {
            "operation": "query",
            "app_id": app_id,
            "collection": collection,
            "count": total,
            "documents": [],
            "more_available": False,
            "output_truncated": False,
            "documents_omitted": total,
            "result_char_limit": _MAX_QUERY_RESULT_CHARS,
        }
        budget = _MAX_QUERY_RESULT_CHARS - len(str(worst_case_envelope))

        kept: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            row_len = len(str(row))
            added = row_len if not kept else row_len + 2  # ", " separator
            if used + added > budget:
                break
            used += added
            kept.append(row)
        return kept, total - len(kept)

    def _delete(self, args: AppDataArgs, data_store: AppDataStore) -> MockSpeaker:
        """Remove one document by key; report whether it existed."""
        assert args.key is not None  # noqa: S101 — guarded by validator
        deleted = data_store.delete(args.app_id, args.collection, args.key)
        return self._ok(
            {"operation": "delete", "app_id": args.app_id, "collection": args.collection,
             "key": args.key, "deleted": deleted}
        )

    def _record_write(self, args: AppDataArgs, run_store: PipelineRunStore) -> None:
        """Increment the pipeline's OPEN ledger entry — best-effort provenance.

        Only when the write names a ``pipeline`` AND that pipeline has an open
        (``running``) run: a manual/repair write outside a pipeline leaves the
        ledger untouched rather than inventing a run. ``record_write`` is safe on
        the returned run (``get_open`` only yields ``running`` entries).
        """
        if not args.pipeline:
            return
        run = run_store.get_open(args.app_id, args.pipeline)
        if run is None:
            return
        run.record_write(args.collection)
        run_store.save(run)

    # -- helpers ------------------------------------------------------------

    def _resolve_app(self, app_store: AppStore, app_id: str) -> AppSpec | None:
        """The app the SERVER bound this session to, iff it is *app_id*; else ``None``.

        Delegates to :meth:`~mewbo_api.apps.staging.AppStagingArea.app_for_session`
        — the two id FIELDS (``maintainer_session_id`` / ``owner_session_id``)
        first, then the server-stamped ``app:<id>`` TAG — so this tool, ``get_app``,
        ``run_pipeline`` and ``submit_app`` give ONE answer about which app a
        session may act on.

        This previously compared ``maintainer_session_id`` alone, which was
        narrower than every other tool in the suite in TWO ways. A tag-bound
        composer session could stage the app and ship a new live version of it
        while being told the app did not exist here; and a pre-submit BUILDER
        session (``owner_session_id`` set, no maintainer yet) could not read back
        the documents its own pipeline had just written.

        :meth:`~mewbo_api.apps.staging.AppStagingArea.binds` and not
        ``app_for_session``: this tool TAKES an ``app_id``, so it never has to
        discover the binding, and the discovery scan would gate a per-document
        read on a walk of every stored app — the performance contract's "a detail
        surface must never be gated on a collection query". Both encode the one
        membership rule; they differ only in whether the caller already has the
        app.

        Cost: ``O(one app)`` — one keyed get plus an ``O(1)`` membership test.
        """
        app = app_store.get(app_id)
        if app is None:
            return None
        binder = AppStagingArea(session_id=self._session_id)
        if not binder.binds(app, session_tags=self._tags_reader(self._session_id)):
            return None
        return app

    def _resolve_stores(
        self,
    ) -> tuple[AppStore | None, AppDataStore | None, PipelineRunStore | None]:
        """Explicit constructor injection wins; else A's process-wide store factories.

        The factories (``mewbo_api.apps.store``) are shared singletons A built for
        exactly this consumer, so no wiring seam is needed. ``or`` short-circuits,
        so a test's injected fake is never overridden by a factory call. A factory
        failure degrades to a clean ``unavailable`` error rather than a crash.
        """
        try:
            return (
                self._app_store or get_app_store(),
                self._data_store or get_app_data_store(),
                self._run_store or get_pipeline_run_store(),
            )
        except Exception as exc:  # noqa: BLE001 — a store-init failure must not 500 the agent
            logging.error("app_data: store resolution failed: {}", exc)
            return None, None, None

    @staticmethod
    def _collection(app: AppSpec, name: str) -> CollectionSpec | None:
        """The named collection on *app*, or ``None`` if undeclared."""
        return next((c for c in app.collections if c.name == name), None)

    @staticmethod
    def _err(code: str, message: str) -> MockSpeaker:
        """Structured-error envelope (``str({"error": {...}})`` — the shared shape).

        ``str(dict)`` not ``json.dumps`` — the loop's envelope parser is
        ``ast.literal_eval``, which reads Python repr, not JSON literals.
        """
        return MockSpeaker(content=str({"error": {"code": code, "message": message}}))

    @staticmethod
    def _ok(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload — same Python-repr shape as :meth:`_err`."""
        return MockSpeaker(content=str(payload))


__all__ = [
    "APP_DATA_SCHEMA",
    "AppDataArgs",
    "AppDataTool",
]
