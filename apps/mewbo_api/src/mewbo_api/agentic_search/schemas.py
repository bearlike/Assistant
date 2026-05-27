"""Typed wire + storage contracts for Agentic Search ("Mewbo Search").

This is the keystone of the integration: every other module in this package
references these models, and the orchestration team implements *against* them
(see ``runner.py`` for the seam). All wire field names are snake_case to match
the rest of the API and the original prototype's ``data.js``.

Two families live here:

* **Entity / wire models** — ``SourceCatalogEntry``, ``Workspace``,
  ``SearchResult``, ``AnswerSynthesis``, ``RunPayload`` … — the shapes the
  console consumes.
* **Run lifecycle models** — ``RunStatus`` and ``RunRecord`` — the durable
  record persisted in the ``agentic_search_runs`` store; the SSE event stream
  (see :mod:`mewbo_api.agentic_search.events`) is the live projection of the
  same data.

The SSE event protocol itself is wire-as-dicts (built by ``events.py``) so the
append-only event log stays transport-agnostic; the canonical event-type set is
documented on :data:`SEARCH_EVENT_TYPES`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump when the wire shape changes incompatibly. Stamped onto every RunRecord +
# emitted in the ``run_started`` event so the console can guard on it.
OUTPUT_CONTRACT_VERSION = "1.0"

# Coarse run lifecycle — mirrors the session/agent terminal-state vocabulary.
RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Result kinds the console knows how to render (filter rail in ResultsPanel).
ResultKindLiteral = Literal["docs", "code", "threads", "design", "tickets", "web"]

# Coarse map-source (SCG indexing) lifecycle — the durable status bucket of a
# map job. Mirrors ``RunStatus`` but tracks the indexing pipeline, not a search.
MapJobStatus = Literal["queued", "mapping", "linking", "finalizing", "complete", "failed"]

# Fine-grained SCG map phase (parallels the wiki's six-phase model). ``phase``
# is the live progress state; ``status`` above is the coarse lifecycle bucket.
MapJobPhase = Literal["connect", "introspect", "parse", "link", "finalize"]


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp (the canonical time field format)."""
    return datetime.now(timezone.utc).isoformat()


def _today_label() -> str:
    """Human label like ``'Jun 05, 2026'`` — back-compat with the prototype."""
    return datetime.now(timezone.utc).strftime("%b %d, %Y")


class _Wire(BaseModel):
    """Base for outward-facing models: forbid unknown keys, allow snake_case."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Source catalog
# ---------------------------------------------------------------------------


class SourceCatalogEntry(_Wire):
    """One MCP-style connector the search agent can fan out across.

    ``available`` / ``unavailable_reason`` let the console grey-out a persisted
    workspace source that is no longer configured instead of silently dropping
    it. ``tool_ids`` is the seam to tool scoping — the orchestration team maps a
    selected source to the concrete tool ids the run is allowed to call.
    """

    id: str
    name: str
    color: str = "#ffffff"
    bg: str = "#191919"
    glyph: str = "?"
    desc: str = ""
    available: bool = True
    unavailable_reason: str | None = None
    tool_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class PastQuery(_Wire):
    """One entry in a workspace's recent-query history.

    ``ran_at`` (ISO) is canonical; ``when`` is a coarse human label kept for
    back-compat — the console computes its own relative label from ``ran_at``.
    ``run_id`` deep-links the history entry to its run snapshot.
    """

    q: str
    when: str = "just now"
    results: int = 0
    ran_at: str | None = None
    run_id: str | None = None
    status: RunStatus | None = None


class WorkspaceInput(_Wire):
    """Validated create/update payload from the console."""

    name: str = Field(min_length=1)
    desc: str = ""
    sources: list[str] = Field(default_factory=list)
    instructions: str = ""


class Workspace(_Wire):
    """A saved multi-source search workspace.

    ``created`` is the legacy display label; ``created_at`` / ``updated_at`` are
    the canonical ISO timestamps. Both are emitted so an un-migrated console
    keeps rendering while a migrated one prefers the ISO fields.
    """

    id: str
    name: str
    desc: str = ""
    sources: list[str] = Field(default_factory=list)
    instructions: str = ""
    created: str = Field(default_factory=_today_label)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    past_queries: list[PastQuery] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalized search results
# ---------------------------------------------------------------------------


class ResultRef(_Wire):
    """A secondary reference attached to a result card."""

    title: str
    url: str
    kind: str = "doc"


class ResultInsight(_Wire):
    """An agent-authored insight callout on a result."""

    label: str
    body: str


class ResultImage(_Wire):
    """Decorative image/preview metadata (defer — present only with real data)."""

    alt: str
    gradient: str


class ResultEmbed(_Wire):
    """Rich embed metadata (figma/slides) — decorative, optional."""

    kind: Literal["figma", "slides"]
    title: str


class SearchResult(_Wire):
    """A single normalized hit from one source.

    ``finish_delay_ms`` was the prototype's fake-reveal driver and is now
    optional/deprecated — real arrival ordering comes from the SSE ``result``
    events, not a baked client-side timer.
    """

    id: str
    source: str
    kind: ResultKindLiteral
    relevance: float = 0.0
    title: str
    url: str = ""
    snippet: str = ""
    author: str = ""
    timestamp: str = ""
    insight: ResultInsight | None = None
    refs: list[ResultRef] = Field(default_factory=list)
    image: ResultImage | None = None
    embed: ResultEmbed | None = None
    # Deprecated decorative timing — kept optional for prototype data parity.
    finish_delay_ms: int | None = None


# ---------------------------------------------------------------------------
# Agent trace
# ---------------------------------------------------------------------------


class TraceLine(_Wire):
    """One line in a per-source agent trace. ``t_ms`` is ms since run start."""

    t_ms: int
    glyph: str = "·"
    text: str
    done: bool = False
    empty: bool = False


class TraceAgent(_Wire):
    """A per-source sub-agent's trace. ``slot`` maps to the ``--agent-N`` token."""

    id: str
    agent_id: str
    name: str
    source_id: str
    slot: int = 0
    lines: list[TraceLine] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Answer synthesis
# ---------------------------------------------------------------------------


class AnswerBullet(_Wire):
    """A synthesis bullet with the result ids it cites."""

    text: str
    cites: list[str] = Field(default_factory=list)


class AnswerSynthesis(_Wire):
    """The cited answer block rendered by ``AnswerCard``."""

    tldr: str = ""
    bullets: list[AnswerBullet] = Field(default_factory=list)
    confidence: float = 0.0
    sources_count: int = 0


class RelatedPerson(_Wire):
    """A related-person chip in the right rail (decorative, optional)."""

    name: str
    role: str
    initials: str
    color: int = 0


# ---------------------------------------------------------------------------
# Run payload + record
# ---------------------------------------------------------------------------


class RunPayload(_Wire):
    """The normalized, console-facing snapshot of a run's results.

    Accumulated by the runner/adapter as the run progresses and persisted onto
    the :class:`RunRecord`. ``GET /runs/{id}`` returns this; the SSE stream
    rebuilds the same shape incrementally on the client.
    """

    run_id: str
    session_id: str
    query: str
    workspace_id: str
    status: RunStatus = "completed"
    total_ms: int = 0
    answer: AnswerSynthesis = Field(default_factory=AnswerSynthesis)
    results: list[SearchResult] = Field(default_factory=list)
    trace: list[TraceAgent] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)
    related_people: list[RelatedPerson] = Field(default_factory=list)
    error: str | None = None


class RunRecord(_Wire):
    """Durable record in the ``agentic_search_runs`` store.

    Separate from session transcripts (the run is *backed by* a session but the
    normalized projection lives here for fast snapshot reads + survives session
    GC). The orchestration team writes ``payload`` via the adapter; the console
    never reads this model directly — it reads ``payload`` through the routes.
    """

    run_id: str
    session_id: str
    workspace_id: str
    query: str
    status: RunStatus = "queued"
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    total_ms: int = 0
    error: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    output_contract_version: str = OUTPUT_CONTRACT_VERSION
    payload: RunPayload | None = None


# ---------------------------------------------------------------------------
# Map-source (SCG indexing) job record
# ---------------------------------------------------------------------------


class MapJobRecord(_Wire):
    """Durable record of a map-source (SCG indexing) job (spec #19 §16.2).

    The map job lives in the *agentic_search* store — NOT the SCG structure
    store — so it reuses the run-event-log + ``RunSseGenerator`` plumbing the
    search runs already ride. Mirrors :class:`RunRecord`: a coarse ``status``
    lifecycle bucket plus a fine-grained ``phase`` (the live progress state),
    written together by ``MapJobProgress.emit_phase`` so the snapshot and the
    event stream can never drift apart (the wiki ``emit_phase`` invariant).

    ``error`` is a small redacted ``{code, message}`` dict (NEVER a token or
    credential — same security stance as :class:`RunRecord`).
    """

    job_id: str
    source_id: str
    source_type: str
    status: MapJobStatus = "queued"
    # Fine-grained progress phase; ``None`` until the first emit_phase.
    phase: MapJobPhase | None = None
    # ISO timestamp at which the current ``phase`` started (ETA extrapolation).
    phase_started_at: str | None = None
    node_count: int = 0
    edge_count: int = 0
    # Redacted error descriptor only — never a secret (mirrors RunRecord).
    error: dict[str, str] | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# SSE event protocol (documented; events travel as dicts — see events.py)
# ---------------------------------------------------------------------------

# The canonical event-type vocabulary the normalized run stream emits. The
# console's reducer switches on these. ``heartbeat`` is transport-only and
# ignored client-side. Terminal types end the stream.
SEARCH_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",   # {run_id, session_id, workspace_id, query, sources}
        "agent_start",   # {agent_id, source_id, name, slot}
        "agent_line",    # {agent_id, line: TraceLine}
        "agent_done",    # {agent_id, results_count, empty}
        "result",        # {result: SearchResult}
        "answer_delta",  # {text}  — streamed synthesis tokens (typewriter)
        "answer_ready",  # {answer: AnswerSynthesis}
        "run_done",      # {status, total_ms}                 (terminal)
        "error",         # {error: {code, message, hint?}}    (terminal)
        "cancelled",     # {}                                 (terminal)
    }
)

# Event types that terminate the SSE stream.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"run_done", "error", "cancelled"})


def clean_for_model(doc: dict[str, Any], model_cls: type[BaseModel]) -> dict[str, Any]:
    """Whitelist *doc* to the fields declared on *model_cls*.

    Mongo / event-log docs carry bookkeeping keys (``_id``, ``idx``,
    ``event_count``) the ``extra="forbid"`` wire models reject. Mirrors the
    wiki store's ``_clean_for_model`` so loads stay lenient while the models
    stay strict.
    """
    allowed = set(model_cls.model_fields.keys())
    return {k: v for k, v in doc.items() if k in allowed and not k.startswith("_")}


__all__ = [
    "OUTPUT_CONTRACT_VERSION",
    "RunStatus",
    "TERMINAL_RUN_STATUSES",
    "MapJobStatus",
    "MapJobPhase",
    "MapJobRecord",
    "SEARCH_EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "SourceCatalogEntry",
    "PastQuery",
    "WorkspaceInput",
    "Workspace",
    "ResultRef",
    "ResultInsight",
    "ResultImage",
    "ResultEmbed",
    "SearchResult",
    "TraceLine",
    "TraceAgent",
    "AnswerBullet",
    "AnswerSynthesis",
    "RelatedPerson",
    "RunPayload",
    "RunRecord",
    "utc_now_iso",
    "clean_for_model",
]
