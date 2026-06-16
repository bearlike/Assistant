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

# The per-run search-tier budget knob (decomposition depth + probe fan-out).
# Lowercase on the wire; defaults to ``scg`` config ``default_tier``.
SearchTierLiteral = Literal["fast", "auto", "deep"]
SEARCH_TIERS: frozenset[str] = frozenset({"fast", "auto", "deep"})

# Coarse map-source (SCG indexing) lifecycle — the durable status bucket of a
# map job: ``queued → running → completed|failed``. Fine-grained pipeline
# progress lives on ``phase`` (``MapJobPhase``), never here.
MapJobStatus = Literal["queued", "running", "completed", "failed"]

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

    ``available`` / ``unavailable_reason`` let the console grey-out a configured
    source whose tool discovery failed instead of silently dropping it (the
    catalog is live-first; a source that is neither configured nor a demo
    fixture is omitted). ``tool_ids`` is the seam to tool scoping — the
    orchestration team maps a selected source to the concrete tool ids the run
    is allowed to call.
    ``source_type`` is the SCG descriptor kind a map job should use (live MCP
    servers advertise ``mcp_tool_list``; the console defaults absent values).
    """

    id: str
    name: str
    color: str = "#ffffff"
    bg: str = "#191919"
    glyph: str = "?"
    desc: str = ""
    source_type: str | None = None
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
# Virtual MCP config (DB-persisted, per workspace) — #75
# ---------------------------------------------------------------------------


class McpServerDef(BaseModel):
    """One resolved MCP server in a workspace's virtual MCP config.

    The persisted source-of-truth for what a run on this workspace may reach: a
    server *name* plus the resolved transport coordinates. ``headers`` / ``env``
    are the ONLY secret-bearing fields — they are stored behind the
    :class:`WorkspaceMcpConfig` encode seam and **always redacted outward**
    (:meth:`redacted`); the wire/graph/event surfaces never see their values.

    Not ``extra="forbid"`` on purpose: the merged ``.mcp.json`` server def is an
    open shape (transport-specific keys vary across MCP transports), so an
    unknown key is preserved as opaque ``extra`` rather than rejected — but only
    the recognised secret fields are ever redacted.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    transport: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Return an outward projection with every secret value masked.

        ``headers`` / ``env`` keys are preserved (the SHAPE of the auth is not a
        secret — that you send an ``Authorization`` header is fine to surface),
        but each value is replaced with ``"***"`` so a token never appears in a
        descriptor, run event, or wire payload (the ``ScgNode.auth_scope``
        stance). The redacted dict is safe to log / emit / persist anywhere.
        """
        blob = self.model_dump(mode="json")
        blob["headers"] = {k: "***" for k in self.headers}
        blob["env"] = {k: "***" for k in self.env}
        return blob

    def auth_scope(self) -> str | None:
        """A redacted one-line auth descriptor (e.g. ``"header:Authorization"``).

        Mirrors :attr:`ScgNode.auth_scope` — names *which* auth a server carries
        without ever revealing the credential, so the SCG/run surfaces can show
        "authenticated" without a secret.
        """
        scopes = [f"header:{k}" for k in self.headers] + [f"env:{k}" for k in self.env]
        return ", ".join(sorted(scopes)) or None


class WorkspaceMcpConfigRecord(_Wire):
    """The durable virtual MCP config for ONE workspace (#75).

    Persisted in the agentic_search store namespace (JSON file / Mongo
    collection, the :class:`CredentialStore` dual-backend pattern). ``servers``
    is the resolved selection — server name → :class:`McpServerDef` — built from
    ``Workspace.sources`` ∩ the merged MCP config at save/attach time. The
    secret-bearing fields are stored behind the :class:`WorkspaceMcpConfig`
    encode seam; only the redacted projection is ever returned outward.
    """

    workspace_id: str
    servers: list[McpServerDef] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now_iso)
    # Fingerprint of the workspace prose (``instructions`` + ``desc``) that last
    # drove a map-time enrich. Server-internal map-lifecycle bookkeeping — the
    # NL-context sibling of ``SourceDescriptor.schema_version`` (the tool-list
    # ManifestHash). Empty until the first enrich-bearing save; a change gates an
    # idempotent re-enrich of the workspace's mapped sources (#83). Never a
    # secret, never echoed outward.
    nl_fingerprint: str = ""

    def server_names(self) -> list[str]:
        """The resolved server names this workspace grants (selection order)."""
        return [s.name for s in self.servers]

    def redacted(self) -> dict[str, Any]:
        """An outward projection — every server's secrets masked (safe to emit)."""
        return {
            "workspace_id": self.workspace_id,
            "updated_at": self.updated_at,
            "servers": [s.redacted() for s in self.servers],
        }


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
    # How sure the emitting agent is this hit answers the query (0..1) —
    # carried verbatim from an ``scg_results`` entry (agent-emitted cards
    # only). ``None`` when the emitter offered no defensible confidence.
    confidence: float | None = None
    title: str
    url: str = ""
    snippet: str = ""
    author: str = ""
    timestamp: str = ""
    # Free-form, agent-emitted scalar metadata carried verbatim from an
    # ``scg_results`` entry's ``meta`` (e.g. ``{"stars": 1200, "language":
    # "Go"}``). SCALARS ONLY — the projection drops any non-scalar value
    # silently so a connector blob can never ride this field. ``None`` when the
    # emitter supplied none.
    meta: dict[str, str | int | float | bool] | None = None
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
    # The probe's compressed terminal evidence (the ``EVIDENCE (pathway: …)`` /
    # ``NO DATA …`` block it returned) — projected from the ``sub_agent`` stop
    # event's ``summary`` so the console's per-lane response panel can show what
    # each probe actually found, not just that it finished. "" until terminal.
    result: str = ""
    # Per-lane provenance (additive, populated at settle from the transcript):
    # ``kind`` is the agent KIND ("coordinator" | "scg-path-probe" | …),
    # distinct from ``name`` (the display label) — the EVIDENCE flagged that
    # ``name`` was carrying the MODEL string; the kind is the honest taxonomy.
    # ``model`` names the LLM the lane ran on. ``steps`` / ``duration_ms`` /
    # ``input_tokens`` / ``output_tokens`` are derived from the lane's
    # ``llm_call_*`` + ``sub_agent`` stop aggregates (``None`` when underivable).
    # ``results_count`` is the TRUE per-lane KEPT card count (after cross-emitter
    # dedup); ``returned_count`` is how many it RAW-emitted before dedup. Their
    # delta is the count the lane contributed that collapsed into another lane's
    # card — surfaced as "N filtered" so the trace reads how much each tool
    # really contributed (the old hardcoded 0 was blind to a 3-card probe).
    kind: str = ""
    model: str | None = None
    steps: int | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    results_count: int = 0
    returned_count: int = 0


# ---------------------------------------------------------------------------
# Answer synthesis
# ---------------------------------------------------------------------------


class AnswerBullet(_Wire):
    """A synthesis bullet with the result ids it cites."""

    text: str
    cites: list[str] = Field(default_factory=list)


class AnswerSynthesis(_Wire):
    """The cited answer block rendered by ``AnswerCard``.

    Provenance fields are populated at settle from REAL probe signals, never
    invented (``OrchestratedSearchRunner._synthesis_metrics``):

    * ``sources_count`` — the number of probes that returned data (an
      ``EVIDENCE`` block, not a ``NO DATA`` dead-end). Each probe walks one
      qualified pathway, so it is the breadth of grounding behind the answer,
      not a fixed fixture value.
    * ``confidence`` — a DEFINED heuristic: ``data-bearing probes / probes run``
      (a probe is data-bearing iff its evidence isn't a ``NO DATA`` dead-end).
      ``0.0`` means "no probe ran" (e.g. a synthesis with an empty trace) — the
      console suppresses the chip rather than render an unearned ``0%``.

    The echo runner keeps its fixture values; only the orchestrated runner
    derives these from the live trace.
    """

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


class RunStatsWire(_Wire):
    """Honest, derived run statistics — the "show the work" instrument block.

    Populated at settle from REAL session events (``RunStats`` discipline:
    NEVER fabricate — a value that can't be derived stays ``None``, never a
    misleading 0). ``probes`` is the spawned probe-lane count; ``tool_calls``
    the total ``tool_result`` events; ``input_tokens`` / ``output_tokens`` the
    cross-lane token totals. ``setup_ms`` is the pre-turn wall clock
    (``created_at`` → first user/llm event — the MCP-handshake gap the old
    "73s total" hid); ``search_ms`` is ``total_ms − setup_ms``. The two ``_ms``
    fields are ``None`` when the bracketing event is unavailable (e.g. a
    fake-runtime transcript with no ``llm_call_*``), so the console suppresses
    them rather than render a fabricated 0.
    """

    probes: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    setup_ms: int | None = None
    search_ms: int | None = None


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
    tier: SearchTierLiteral = "auto"
    # Explicit per-run model override (a LiteLLM name); None = the tier's
    # configured model. Echoed so the deep-link snapshot stays self-sufficient.
    model: str | None = None
    total_ms: int = 0
    answer: AnswerSynthesis = Field(default_factory=AnswerSynthesis)
    results: list[SearchResult] = Field(default_factory=list)
    trace: list[TraceAgent] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)
    related_people: list[RelatedPerson] = Field(default_factory=list)
    # Honest derived run stats (probes / tool_calls / tokens / setup·search ms),
    # populated at settle from session events. ``None`` until a real settle ran
    # (an in-flight or echo run carries no stats).
    stats: RunStatsWire | None = None
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
    tier: SearchTierLiteral = "auto"
    # Explicit per-run model override; the runner reads it at drive time
    # (``run.model or ScgConfig.model_for_tier(run.tier)``).
    model: str | None = None
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
        "agent_done",    # {agent_id, results_count, returned_count, empty}
        "result",        # {result: SearchResult}
        "answer_delta",  # {text}  — streamed synthesis tokens (typewriter)
        "answer_ready",  # {answer: AnswerSynthesis}
        "related_questions",  # {questions: [str]}  — parallel follow-up call
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
    "SearchTierLiteral",
    "SEARCH_TIERS",
    "MapJobStatus",
    "MapJobPhase",
    "MapJobRecord",
    "SEARCH_EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "SourceCatalogEntry",
    "PastQuery",
    "WorkspaceInput",
    "Workspace",
    "McpServerDef",
    "WorkspaceMcpConfigRecord",
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
    "RunStatsWire",
    "RunPayload",
    "RunRecord",
    "utc_now_iso",
    "clean_for_model",
]
