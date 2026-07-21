#!/usr/bin/env python3
"""Agentic-Search seed bundle + the ``SearchSeeder`` atomic class.

The console's *Mewbo Search* surface renders from three durable stores:

* the **agentic_search** store — workspaces (+ their ``past_queries`` chips) and
  durable :class:`RunRecord` snapshots (the results / answer / trace a deep-link
  replays);
* the **SCG structure** store (``mewbo_graph.scg``) — the Source Capability
  Graph a workspace's *capability graph* view draws (source-hub + capability
  nodes + edges), scoped to the workspace's ``sources``;
* the **wiki memory** store (``mewbo_graph.wiki``) — the connector *memory*
  layer (``corpus="connector"``) whose anchored notes complete the graph's
  "N memory notes" band.

A :class:`SearchBundle` is the deterministic description of that world, authored
as OFFSETS from a frozen ``T0`` so re-seeding is byte-identical (an absolute
wall-clock ``created_at``/``ran_at`` would drift the console's "2d ago" labels
on every capture). The bundle REUSES the real wire models verbatim
(``mewbo_api.agentic_search.schemas`` — ``Workspace``/``RunRecord``/
``SearchResult``/``TraceAgent``/``AnswerSynthesis`` …) for the timeless content
and wraps only the timestamp-bearing fields in T0-relative offsets, so there is
no second copy of the search wire schema here (DRY).

:class:`SearchSeeder` is the one atomic class: the store(s), the parsed bundle,
and the frozen ``t0`` are injected as FIELDS (so a test drives the real
``seed()`` against mongomock-backed stores with a fixed clock and never patches
time); ``seed()`` writes THROUGH the existing store contracts (``save_workspace``
verbatim upsert, ``create_run`` + the terminal event log, SCG ``upsert_*``), and
a re-run converges to identical state. NO live search, NO LLM, NO network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from mewbo_api.agentic_search import events
from mewbo_api.agentic_search.schemas import (
    AnswerSynthesis,
    PastQuery,
    RelatedPerson,
    RunPayload,
    RunRecord,
    RunStatsWire,
    RunStatus,
    SearchResult,
    SearchTierLiteral,
    TraceAgent,
    Workspace,
)
from mewbo_api.agentic_search.store import AgenticSearchStoreBase
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Placeholder session tag stamped on every seeded run's ``session_id`` — the
# same ``agentic_search:run:<id>`` shape the real runner uses, so the deep-link
# snapshot is self-sufficient without a real backing session existing.
_SESSION_TAG = "agentic_search:run:{run_id}"

# The MCP-tool-list source shape the SCG provider parses. One tool → one
# ``capability`` node keyed ``<source_id>#<name>`` (mcp_tool_list provider).
_SCG_SOURCE_TYPE = "mcp_tool_list"


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class SeedPastQuery(BaseModel):
    """One recent-query chip on a workspace card.

    ``run_id`` deep-links the chip to a seeded run (the console renders a
    History icon and replays via ``GET /runs/<id>``); a chip with no ``run_id``
    degrades to a fresh search. ``ran_at_offset`` rebases to the canonical
    ``ran_at`` ISO (the console computes its own "2d ago" label from it).
    """

    model_config = ConfigDict(extra="forbid")

    q: str
    when: str = "just now"
    results: int = 0
    ran_at_offset: int | None = Field(
        default=None, description="ran_at relative to T0, in seconds (negative = past)."
    )
    run_id: str | None = None
    status: RunStatus | None = None

    def to_past_query(self, t0: datetime) -> PastQuery:
        """Shape into the wire :class:`PastQuery`, rebasing ``ran_at``."""
        ran_at = (
            None
            if self.ran_at_offset is None
            else (t0 + timedelta(seconds=self.ran_at_offset)).isoformat()
        )
        return PastQuery(
            q=self.q,
            when=self.when,
            results=self.results,
            ran_at=ran_at,
            run_id=self.run_id,
            status=self.status,
        )


class SeedWorkspace(BaseModel):
    """A saved multi-source search workspace, with a T0-relative creation time.

    ``sources`` are the connector ids the workspace fans across; the console's
    card resolves each to a brand glyph through the ``GET /sources`` catalog, so
    every id here must be catalog-resolvable (a fixture id or a configured MCP
    server) or its chip renders blank. The same ``sources`` scope the workspace's
    capability-graph view.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    desc: str = ""
    sources: list[str] = Field(default_factory=list)
    instructions: str = ""
    created: str = Field(
        default="", description="Human 'created' label; derived from T0 when blank."
    )
    created_offset: int = Field(
        default=0, description="created_at relative to T0, in seconds (negative = past)."
    )
    updated_offset: int | None = Field(
        default=None, description="updated_at relative to T0; defaults to created_offset."
    )
    past_queries: list[SeedPastQuery] = Field(default_factory=list)

    def to_workspace(self, t0: datetime) -> Workspace:
        """Shape into the wire :class:`Workspace`, rebasing created/updated/ran_at."""
        updated = self.created_offset if self.updated_offset is None else self.updated_offset
        created_at = t0 + timedelta(seconds=self.created_offset)
        created_label = self.created or created_at.strftime("%b %d, %Y")
        return Workspace(
            id=self.id,
            name=self.name,
            desc=self.desc,
            sources=list(self.sources),
            instructions=self.instructions,
            created=created_label,
            created_at=created_at.isoformat(),
            updated_at=(t0 + timedelta(seconds=updated)).isoformat(),
            past_queries=[pq.to_past_query(t0) for pq in self.past_queries],
        )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class SeedRun(BaseModel):
    """One completed search run: the durable snapshot + its replayable stream.

    Carries the real wire content models verbatim (``answer``/``results``/
    ``trace``/``related_*``/``stats``) plus T0-relative run timestamps. The
    seeder shapes this into a terminal :class:`RunRecord` (status ``completed``)
    and emits the same normalized event log the echo runner writes, so a
    ``/search?run=<id>`` deep-link replays exactly like a real finished run.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workspace_id: str
    query: str
    tier: SearchTierLiteral = "auto"
    model: str
    created_offset: int = Field(
        description="created_at relative to T0, in seconds (negative = past)."
    )
    started_offset: int | None = Field(
        default=None, description="started_at relative to T0; defaults to created_offset."
    )
    completed_offset: int | None = Field(
        default=None,
        description="completed_at relative to T0; defaults to created + ceil(total_ms).",
    )
    total_ms: int = 0
    source_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    answer: AnswerSynthesis
    results: list[SearchResult] = Field(default_factory=list)
    trace: list[TraceAgent] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)
    related_people: list[RelatedPerson] = Field(default_factory=list)
    stats: RunStatsWire | None = None

    @property
    def session_id(self) -> str:
        """The placeholder ``agentic_search:run:<id>`` tag (no real session needed)."""
        return _SESSION_TAG.format(run_id=self.run_id)

    def to_payload(self) -> RunPayload:
        """Build the console-facing :class:`RunPayload` snapshot (no timestamps)."""
        return RunPayload(
            run_id=self.run_id,
            session_id=self.session_id,
            query=self.query,
            workspace_id=self.workspace_id,
            status="completed",
            tier=self.tier,
            model=self.model,
            total_ms=self.total_ms,
            answer=self.answer,
            results=list(self.results),
            trace=list(self.trace),
            related_questions=list(self.related_questions),
            related_people=list(self.related_people),
            stats=self.stats,
        )

    def to_record(self, t0: datetime) -> RunRecord:
        """Build the durable, terminal :class:`RunRecord` (rebasing timestamps)."""
        started = self.created_offset if self.started_offset is None else self.started_offset
        completed = (
            self.created_offset + max(1, -(-self.total_ms // 1000))
            if self.completed_offset is None
            else self.completed_offset
        )
        return RunRecord(
            run_id=self.run_id,
            session_id=self.session_id,
            workspace_id=self.workspace_id,
            query=self.query,
            status="completed",
            tier=self.tier,
            model=self.model,
            created_at=(t0 + timedelta(seconds=self.created_offset)).isoformat(),
            started_at=(t0 + timedelta(seconds=started)).isoformat(),
            completed_at=(t0 + timedelta(seconds=completed)).isoformat(),
            total_ms=self.total_ms,
            source_ids=list(self.source_ids),
            allowed_tools=list(self.allowed_tools),
            payload=self.to_payload(),
        )

    def stream_events(self) -> list[dict[str, Any]]:
        """Project the normalized SSE event log a deep-link replays.

        Mirrors ``EchoSearchRunner`` — ``run_started`` → per-lane
        ``agent_start``/``agent_line``/``agent_done`` → ``result`` per hit →
        ``answer_ready`` → ``related_questions`` → terminal ``run_done``. The
        ``agent_*`` dicts are hand-built (not the basic ``events`` builders) so
        they carry the lane's instrument fields (``kind``/``model``/``steps``/
        ``duration_ms``/tokens/``returned_count``) the console folds — without
        them a replayed lane loses everything the snapshot's ``TraceAgent`` has.
        """
        out: list[dict[str, Any]] = [
            events.run_started(
                run_id=self.run_id,
                session_id=self.session_id,
                workspace_id=self.workspace_id,
                query=self.query,
                sources=list(self.source_ids),
            )
        ]
        for lane in self.trace:
            out.append(self._agent_start(lane))
            for line in lane.lines:
                out.append(events.agent_line(agent_id=lane.agent_id, line=line))
            out.append(self._agent_done(lane))
        for hit in self.results:
            out.append(events.result(item=hit))
        out.append(events.answer_ready(answer=self.answer))
        if self.related_questions:
            out.append(events.related_questions(questions=list(self.related_questions)))
        out.append(events.run_done(status="completed", total_ms=self.total_ms))
        return out

    @staticmethod
    def _agent_start(lane: TraceAgent) -> dict[str, Any]:
        """An ``agent_start`` frame carrying the lane's kind + driving model."""
        return {
            "type": "agent_start",
            "agent_id": lane.agent_id,
            "source_id": lane.source_id,
            "name": lane.name,
            "slot": lane.slot,
            "kind": lane.kind,
            "model": lane.model,
        }

    @staticmethod
    def _agent_done(lane: TraceAgent) -> dict[str, Any]:
        """An ``agent_done`` frame carrying the lane's per-lane instrument totals."""
        return {
            "type": "agent_done",
            "agent_id": lane.agent_id,
            "results_count": lane.results_count,
            "returned_count": lane.returned_count,
            "empty": lane.results_count == 0,
            "result": lane.result,
            "steps": lane.steps,
            "duration_ms": lane.duration_ms,
            "input_tokens": lane.input_tokens,
            "output_tokens": lane.output_tokens,
        }


# ---------------------------------------------------------------------------
# Source Capability Graph (SCG) — the workspace's capability-graph view
# ---------------------------------------------------------------------------


class SeedScgTool(BaseModel):
    """One MCP tool → one ``capability`` node (via the real SCG provider).

    Expanded through ``McpToolListStructureProvider`` so the seeded nodes/edges
    are byte-identical to a live-mapped MCP source (source-hub + one capability
    per tool + a ``HAS_ENTITY`` edge; input/output fields ride as bindings).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    input_fields: list[str] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)

    def to_raw(self) -> dict[str, Any]:
        """Return the ``{name, description, inputSchema, outputSchema}`` tool dict."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "properties": {f: {"type": "string"} for f in self.input_fields},
                "required": list(self.required),
            },
            "outputSchema": {"properties": {f: {"type": "string"} for f in self.output_fields}},
        }


class SeedScgSource(BaseModel):
    """One mapped connector source-hub and its capability tools."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    tools: list[SeedScgTool] = Field(default_factory=list)


class SeedScgMemory(BaseModel):
    """One connector *memory* note — a reachability fact anchored into the graph.

    Written to the wiki store (``corpus="connector"``) with a live ``ANCHORS``
    edge onto an in-scope capability ``source_key`` (``<source_id>#<tool>``), so
    the graph view keeps it in the memory layer. Polarity rides the labels as
    ``scg:positive`` / ``scg:dead_end``; ``relates_to`` links note↔note.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=200)
    anchor: str = Field(description="An in-scope capability source_key, '<source_id>#<tool>'.")
    polarity: Literal["positive", "dead_end"] = "positive"
    kind: Literal["propositional", "prescriptive"] = "propositional"
    relates_to: list[int] = Field(
        default_factory=list, description="Indices of sibling memory notes to RELATES-link."
    )
    created_offset: int = Field(
        default=0, description="created_at relative to T0, in seconds (negative = past)."
    )


class SeedScg(BaseModel):
    """A workspace's deterministic capability graph (structure + memory layers)."""

    model_config = ConfigDict(extra="forbid")

    memory_workspace: str = Field(description="Workspace id stamped as the ws:<id> memory label.")
    sources: list[SeedScgSource] = Field(default_factory=list)
    memory: list[SeedScgMemory] = Field(default_factory=list)

    @model_validator(mode="after")
    def _anchors_resolve(self) -> SeedScg:
        """Every memory anchor must point at a capability this graph seeds."""
        keys = {
            f"{src.source_id}#{tool.name}" for src in self.sources for tool in src.tools
        }
        for note in self.memory:
            if note.anchor not in keys:
                raise ValueError(
                    f"memory note anchor {note.anchor!r} is not a seeded capability source_key"
                )
        return self


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


class SearchBundle(BaseModel):
    """The whole deterministic agentic-search world: workspaces + runs + SCG."""

    model_config = ConfigDict(extra="forbid")

    workspaces: list[SeedWorkspace]
    runs: list[SeedRun] = Field(default_factory=list)
    scg: SeedScg | None = None

    @model_validator(mode="after")
    def _validate_refs(self) -> SearchBundle:
        """Ids are unique; runs + past-query chips reference real workspaces/runs."""
        ws_ids = [w.id for w in self.workspaces]
        if len(set(ws_ids)) != len(ws_ids):
            raise ValueError("duplicate workspace id in bundle")
        run_ids = [r.run_id for r in self.runs]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("duplicate run id in bundle")
        known_ws = set(ws_ids)
        known_runs = set(run_ids)
        for run in self.runs:
            if run.workspace_id not in known_ws:
                raise ValueError(
                    f"run {run.run_id!r} references unknown workspace {run.workspace_id!r}"
                )
        for ws in self.workspaces:
            for pq in ws.past_queries:
                if pq.run_id is not None and pq.run_id not in known_runs:
                    raise ValueError(
                        f"workspace {ws.id!r} past query references unknown run {pq.run_id!r}"
                    )
        if self.scg is not None and self.scg.memory_workspace not in known_ws:
            raise ValueError(
                f"scg.memory_workspace {self.scg.memory_workspace!r} is not a bundle workspace"
            )
        return self


# ---------------------------------------------------------------------------
# Report + seeder
# ---------------------------------------------------------------------------


@dataclass
class SearchSeedReport:
    """What a :meth:`SearchSeeder.seed` run wrote — an in-process return value.

    A plain dataclass (not Pydantic): it crosses no trust boundary.
    """

    workspaces: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    scg_sources: list[str] = field(default_factory=list)
    scg_nodes: int = 0
    scg_edges: int = 0
    memory_notes: int = 0


class SearchSeeder:
    """Idempotently materialise a :class:`SearchBundle` into the search world.

    Collaborators are injected as FIELDS — the agentic_search store, the parsed
    bundle, the frozen ``t0``, and the OPTIONAL SCG + wiki-memory stores — so a
    test drives the real ``seed()`` against mongomock-backed stores with a fixed
    clock and never patches time. ``seed`` writes THROUGH the store contracts and
    a re-run converges to identical state: ``save_workspace`` is a verbatim
    upsert; a run's terminal snapshot is create-or-update while its append-only
    event log is written exactly once (a re-emit would collide on the unique
    ``(run_id, idx)`` index — the run already existing is the "already seeded"
    signal, mirroring the session seeder's clear-before-write); every SCG /
    memory write is a content-addressed ``upsert_*`` (idempotent by construction).

    ``scg_store`` / ``wiki_store`` stay ``None`` on a graph-less install — the
    workspaces + runs still seed fully; only the capability-graph view degrades
    to the empty (unmapped-ghost) state.
    """

    def __init__(
        self,
        *,
        search_store: AgenticSearchStoreBase,
        bundle: SearchBundle,
        t0: datetime,
        scg_store: Any = None,
        wiki_store: Any = None,
    ) -> None:
        """Store the injected collaborators and the frozen ``t0`` clock."""
        self._search = search_store
        self._bundle = bundle
        self._t0 = t0
        self._scg = scg_store
        self._wiki = wiki_store

    def seed(self) -> SearchSeedReport:
        """Write every workspace, run, and (if a store is injected) the SCG."""
        report = SearchSeedReport()
        for ws in self._bundle.workspaces:
            self._search.save_workspace(ws.to_workspace(self._t0))  # verbatim upsert
            report.workspaces.append(ws.id)
        for run in self._bundle.runs:
            self._seed_run(run)
            report.runs.append(run.run_id)
        if self._scg is not None and self._bundle.scg is not None:
            self._seed_scg(self._bundle.scg, report)
        return report

    def _seed_run(self, run: SeedRun) -> None:
        """Persist one run's terminal snapshot + its replayable event log.

        The snapshot is idempotent (create when absent, else refresh in place);
        the append-only event log is written ONLY on first seed so a re-run never
        double-appends onto the unique-idx run event stream.
        """
        record = run.to_record(self._t0)
        if self._search.get_run(run.run_id) is None:
            self._search.create_run(record)
            for event in run.stream_events():
                self._search.append_run_event(run.run_id, event)
        else:
            self._search.update_run(
                run.run_id,
                status=record.status,
                started_at=record.started_at,
                completed_at=record.completed_at,
                total_ms=record.total_ms,
                payload=record.payload,
            )

    def _seed_scg(self, spec: SeedScg, report: SearchSeedReport) -> None:
        """Expand + upsert the SCG structure layer, then the memory layer.

        Nodes/edges are built by the REAL ``McpToolListStructureProvider`` from a
        per-source ``SourceDescriptor``, so a seeded source is byte-identical to a
        live-mapped one. Import is lazy + guarded: ``mewbo_graph.scg`` is optional
        (a caller only injects ``scg_store`` when it is installed).
        """
        from mewbo_graph.scg.providers.mcp_tool_list import McpToolListStructureProvider
        from mewbo_graph.scg.types import ScgEdge, ScgNode, SourceDescriptor

        provider = McpToolListStructureProvider()
        nodes: list[ScgNode] = []
        edges: list[ScgEdge] = []
        for src in spec.sources:
            descriptor = SourceDescriptor(
                source_id=src.source_id,
                source_type=_SCG_SOURCE_TYPE,
                raw={"tools": [t.to_raw() for t in src.tools]},
            )
            self._scg.upsert_source(descriptor)
            structure = provider.build_structure(descriptor)
            nodes.extend(structure.nodes)
            edges.extend(structure.edges)
            report.scg_sources.append(src.source_id)
        self._scg.upsert_nodes(nodes)
        self._scg.upsert_edges(edges)
        report.scg_nodes = len(nodes)
        report.scg_edges = len(edges)
        if self._wiki is not None and spec.memory:
            report.memory_notes = self._seed_memory(spec)

    def _seed_memory(self, spec: SeedScg) -> int:
        """Write the connector memory notes + their ANCHORS/RELATES edges.

        Each note is anchored (live ``ANCHORS``, ``invalid_at=None``) onto an
        in-scope capability ``source_key`` so the graph view keeps it in the
        memory layer; ``relates_to`` becomes note↔note ``RELATES`` edges. Content
        is addressed by ``sha1(slug|content)`` so re-seeds converge.
        """
        from mewbo_graph.scg.memory_bridge import (
            CONNECTOR_CORPUS,
            CONNECTOR_SLUG,
            polarity_label,
        )
        from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode, MemoryProvenance

        ws_label = f"ws:{spec.memory_workspace}"
        notes: list[MemoryNode] = []
        for note in spec.memory:
            created = (self._t0 + timedelta(seconds=note.created_offset)).isoformat()
            notes.append(
                MemoryNode(
                    slug=CONNECTOR_SLUG,
                    content=note.content,
                    kind=note.kind,
                    corpus=CONNECTOR_CORPUS,
                    labels=[polarity_label(note.polarity), ws_label],
                    provenance=MemoryProvenance(
                        author_agent="demo-seeder",
                        source="on_demand",
                        created_at=created,
                    ),
                )
            )
        self._wiki.upsert_memory_nodes(CONNECTOR_SLUG, notes)

        edges: list[MemoryEdge] = []
        for note, model in zip(spec.memory, notes):
            valid_at = (self._t0 + timedelta(seconds=note.created_offset)).isoformat()
            edges.append(
                MemoryEdge(
                    slug=CONNECTOR_SLUG,
                    source=model.node_id,
                    target=note.anchor,
                    type="ANCHORS",
                    valid_at=valid_at,
                )
            )
            for idx in note.relates_to:
                edges.append(
                    MemoryEdge(
                        slug=CONNECTOR_SLUG,
                        source=model.node_id,
                        target=notes[idx].node_id,
                        type="RELATES",
                        valid_at=valid_at,
                    )
                )
        self._wiki.upsert_memory_edges(CONNECTOR_SLUG, edges)
        return len(notes)


__all__ = [
    "SeedPastQuery",
    "SeedWorkspace",
    "SeedRun",
    "SeedScgTool",
    "SeedScgSource",
    "SeedScgMemory",
    "SeedScg",
    "SearchBundle",
    "SearchSeedReport",
    "SearchSeeder",
]
