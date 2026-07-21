#!/usr/bin/env python3
"""The ``app_data`` SessionTool — the maintainer's scoped, validated data plane.

The maintainer session (tagged ``app:<app_id>``) is re-woken by a pipeline
trigger to ingest / transform data; ``app_data`` is how it writes and reads its
app's collections. It is the ONLY agent-side ingress to ``app_data`` (spec §2.5),
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
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_api.apps.plugin.runtime import (
    AppDataStore,
    AppStore,
    PipelineRunStore,
)
from mewbo_api.apps.store import (
    CollectionCapExceeded,
    get_app_data_store,
    get_app_store,
    get_pipeline_run_store,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

    from mewbo_api.apps.models import AppSpec, CollectionSpec

logging = get_logger(name="apps.plugin.app_data")

APP_DATA_TOOL_ID = "app_data"

# Bound a single query so a runaway `limit` can't pull the whole collection into
# the model's context.
_MAX_QUERY_LIMIT = 1000


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
        description="`query` only: max documents to return (1-1000).",
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

    Satisfies the :class:`~mewbo_core.session_tools.SessionTool` Protocol; the
    three store collaborators resolve from workstream A's process-wide store
    factories (``get_app_store`` / ``get_app_data_store`` / ``get_pipeline_run_store``)
    when not passed explicitly (the plugin-manifest build path). A test injects
    fakes directly through the constructor.
    """

    tool_id: str = APP_DATA_TOOL_ID
    schema: dict[str, object] = APP_DATA_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        app_store: AppStore | None = None,
        data_store: AppDataStore | None = None,
        run_store: PipelineRunStore | None = None,
    ) -> None:
        """Bind the maintainer session id + the three store collaborators.

        Args:
            session_id: The maintainer session — the scope boundary (only apps
                whose ``maintainer_session_id`` equals this may be touched).
            event_logger: Reserved for parity with the plugin build path (this
                tool emits no transcript event; the ledger is the record).
            app_store: Manifest read store (scope check + collection lookup).
            data_store: The app-ID-keyed data plane.
            run_store: The provenance ledger store.
            A ``None`` store (the plugin path) is resolved from A's process-wide
            store factory at handle time.
        """
        self._session_id = session_id
        self._event_logger = event_logger
        self._app_store = app_store
        self._data_store = data_store
        self._run_store = run_store

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

        app = app_store.get(args.app_id)
        if app is None or app.maintainer_session_id != self._session_id:
            # Uniform not_found for missing AND foreign — no existence leak.
            return self._err("not_found", f"no app {args.app_id!r} bound to this session")

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
        """Read documents back — key/doc/updated_at per row (updated_at as ISO)."""
        docs = data_store.query(
            args.app_id, args.collection, filter=args.filter, limit=args.limit, sort=args.sort
        )
        rows = [
            {"key": d.key, "doc": d.doc, "updated_at": d.updated_at.isoformat()}
            for d in docs
        ]
        return self._ok(
            {"operation": "query", "app_id": args.app_id, "collection": args.collection,
             "count": len(rows), "documents": rows}
        )

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
