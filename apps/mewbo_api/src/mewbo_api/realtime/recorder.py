"""Write-behind session recorder for the realtime fast/draft paths (#78).

``/v1/structured/fast`` and ``/v1/draft/stream`` were sessionless by design —
zero session record, transcript, or Langfuse trace. #78 reclassified that as a
defect: those surfaces must be session-full like every other entry point, WITHOUT
regressing the latency path (draft p95 TTFT < 1.5s).

:class:`RealtimeSessionRecorder` is the one atomic class both routes use to make
that true. It owns the two halves of "session-full but fast":

* **In-process trace.** It derives :class:`~mewbo_core.session_provenance.TraceProvenance`
  from the tags + context it is ABOUT to write (no store read — the session
  doesn't exist yet) and hands the route a ``langfuse_session_context`` opened on
  a pre-minted ``session_id``. The LLM call runs inside that context, so the
  generation lands in a filterable, session-grouped Langfuse trace. This is the
  only thing that must wrap the model call.
* **Write-behind persistence.** Every durable store write (tag + transcript
  events) is deferred to :meth:`persist`, which the route calls AFTER the
  response/stream has been sent. The first token never waits on a store write.

Why this lives in the app, not core: it needs a concrete ``SessionRuntime`` (the
session store) — app glue, per the layering DAG. Core stays graph-/store-free;
the routes inject the runtime.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.components import langfuse_session_context
from mewbo_core.session_provenance import TraceProvenance

logging = get_logger(name="api.realtime.recorder")

# Provenance tag PREFIXES for the two realtime surfaces. Kept parallel to
# ``structured_response.STRUCTURED_RUN_TAG`` ("structured:run") so the three
# structured-family surfaces share the ``structured`` product while staying
# individually filterable by ``session_type`` (the 2nd ``:``-segment).
#
# The PER-SESSION tag is ``<prefix>:<session_id>`` (see ``tag``) — never the
# bare prefix. The tags collection is keyed BY TAG, so a constant tag would make
# every run overwrite one shared doc (the latest run steals it, every prior run
# silently loses its tag and reclassifies to the ``user`` origin fallback). The
# extra id segment is transparent to the parsers: ``SessionOrigin.classify``
# prefix-matches ``structured:``/``draft:`` and ``TraceProvenance._facets_from_tags``
# reads the 2nd segment (``fast``/``stream``) as ``session_type`` regardless of
# any trailing id segment.
FAST_STRUCTURED_TAG = "structured:fast"
DRAFT_STREAM_TAG = "draft:stream"

# Cap a query used as a session title so a giant prompt never bloats the title.
_TITLE_CAP = 120


@dataclass
class RealtimeSessionRecorder:
    """Session-backs ONE realtime fast/draft request with write-behind persistence.

    Construct per request via the :meth:`for_fast` / :meth:`for_draft` builders,
    which set the right tag + ``session_type``. The pre-minted ``session_id`` is
    available immediately (no I/O) so the route can open the trace and return a
    handle; persistence is deferred to :meth:`persist`.

    Args:
        runtime: The session runtime (session store seam) — DI'd by the route.
        query: The user's natural-language query (the single inbound turn).
        base_tag: Provenance tag PREFIX (``structured:fast`` / ``draft:stream``);
            the durable tag is :attr:`tag` = ``<base_tag>:<session_id>``.
        surface: Originating client surface (``X-Mewbo-Surface``; default ``api``).
        workspace: Optional grounding workspace slug (recorded as context).
        model: Optional model override (recorded as context when set).
        session_id: Pre-minted session id; auto-generated when omitted.
    """

    runtime: Any
    query: str
    base_tag: str
    surface: str = "api"
    workspace: str | None = None
    model: str | None = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def tag(self) -> str:
        """The UNIQUE per-session provenance tag (``<base_tag>:<session_id>``).

        Unique so two concurrent runs never collide on (and steal) one
        tag-keyed doc; the parsers prefix-match the base, so the trailing id is
        transparent to origin/provenance classification (see the module note).
        """
        return f"{self.base_tag}:{self.session_id}"

    @classmethod
    def for_fast(cls, runtime: Any, query: str, **kwargs: Any) -> RealtimeSessionRecorder:
        """Recorder for ``POST /v1/structured/fast`` (tag ``structured:fast:<id>``)."""
        return cls(runtime=runtime, query=query, base_tag=FAST_STRUCTURED_TAG, **kwargs)

    @classmethod
    def for_draft(cls, runtime: Any, query: str, **kwargs: Any) -> RealtimeSessionRecorder:
        """Recorder for ``POST /v1/draft/stream`` (tag ``draft:stream:<id>``)."""
        return cls(runtime=runtime, query=query, base_tag=DRAFT_STREAM_TAG, **kwargs)

    # -- trace ---------------------------------------------------------------

    def _context(self) -> dict[str, object]:
        """The context payload this run advertises (also feeds the trace)."""
        ctx: dict[str, object] = {"source_platform": self.surface}
        if self.workspace:
            ctx["structured_workspace"] = self.workspace
        if self.model:
            ctx["model"] = self.model
        return ctx

    @contextmanager
    def trace(self) -> Iterator[str]:
        """Open the Langfuse session context for the LLM call; yields the session id.

        Provenance is derived from the tags + context this run is ABOUT to
        persist — the session record doesn't exist yet, so we cannot read the
        store. That's identical to the data ``Orchestrator.run`` would read after
        the write-behind, so the trace facets match a fully-persisted session.
        Degrades to a bare yield when Langfuse is disabled (the context manager
        is a graceful no-op).
        """
        provenance = TraceProvenance.derive(
            tags=[self.tag],
            context=self._context(),
            surface=self.surface,
        )
        with langfuse_session_context(
            self.session_id,
            source_platform=self.surface,
            tags=list(provenance.tags),
            metadata=provenance.metadata,
        ):
            yield self.session_id

    # -- write-behind persistence -------------------------------------------

    def persist(
        self,
        *,
        output: object | None = None,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        """Durably record the session AFTER the response was sent (write-behind).

        Materialises the session RECORD first (``ensure_session`` — idempotent),
        then writes the origin tag, the run's context, the inbound user turn, and
        the single outbound turn — ``structured_output`` for fast (the same event
        the agentic ``/v1/structured`` path emits, so ``GET /v1/structured/<run_id>``
        semantics carry over) and an assistant ``text`` turn for draft. Without the
        ``ensure_session`` call the Mongo driver's ``list_sessions`` (which reads
        the ``sessions`` collection, not ``events``) never sees the id, so the
        transcript is invisible on every read surface. The terminal ``completion``
        records ``error`` honestly when *error* is set (a stream that died
        mid-flight must NOT summarize as ``completed``). Best-effort: a persistence
        failure is logged and swallowed — it must never surface to a caller who
        already got a 200 response/stream.
        """
        try:
            # Materialise the RECORD before any transcript write so the session is
            # listed/visible, not an orphan (Mongo: append_event writes only the
            # events collection). Idempotent — safe if persist runs twice.
            self.runtime.ensure_session(self.session_id)
            self.runtime.tag_session(self.session_id, self.tag)
            for key, value in self._context().items():
                self.runtime.append_context_event(self.session_id, {key: value})
            self.runtime.append_event(
                self.session_id,
                {"type": "user", "payload": {"text": self.query[:_TITLE_CAP]}},
            )
            if output is not None:
                self.runtime.append_event(
                    self.session_id,
                    {"type": "structured_output", "payload": output},
                )
            if text is not None:
                self.runtime.append_event(
                    self.session_id,
                    {"type": "assistant", "payload": {"text": text}},
                )
            # Terminal completion → ``summarize_session`` reports a real status
            # (not a dangling ``idle``). ``error`` makes a mid-stream failure
            # summarize as ``failed``, never a false ``completed``.
            completion = (
                {"done": False, "done_reason": "error", "reason": error}
                if error is not None
                else {"done": True, "done_reason": "completed"}
            )
            self.runtime.append_event(
                self.session_id, {"type": "completion", "payload": completion}
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort, post-response
            logging.warning(
                "RealtimeSessionRecorder.persist failed for session {}",
                self.session_id,
                exc_info=True,
            )

    def persist_async(self, **kwargs: object) -> None:
        """Fire :meth:`persist` on a daemon thread (off the response hot path).

        Keeps the durable writes off the connection-close path for both the fast
        response (built before persistence) and the draft stream (persisted from
        the generator tail). Accepts the same keywords as :meth:`persist`.
        """
        threading.Thread(
            target=lambda: self.persist(**kwargs),  # type: ignore[arg-type]
            daemon=True,
            name=f"realtime-persist-{self.session_id[:8]}",
        ).start()


__all__ = [
    "DRAFT_STREAM_TAG",
    "FAST_STRUCTURED_TAG",
    "RealtimeSessionRecorder",
]
