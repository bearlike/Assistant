"""Pydantic v2 mirrors of the frontend wiki API wire types.

Every model here corresponds 1-to-1 with a TypeScript interface or type alias
declared in ``apps/mewbo_console/src/components/wiki/api/types.ts``.

Conventions:
- ``model_config = ConfigDict(extra="forbid", populate_by_name=True)``
- Python attributes are snake_case; camelCase wire names use ``Field(alias=...)``.
- Discriminated unions are wrapped in ``RootModel`` for ``model_validate`` access.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

# ── Shared config ──────────────────────────────────────────────────────────────

_CFG = ConfigDict(extra="forbid", populate_by_name=True)

PlatformId = Literal["github", "gitlab", "bitbucket", "gitea", "azure", "git"]


# ── Project ────────────────────────────────────────────────────────────────────


class Project(BaseModel):
    """Landing-card model for a wiki project.

    Slug is fully qualified — ``host/owner/repo`` — so the identity is
    unambiguous across self-hosted and enterprise instances. Legacy
    two-segment slugs (``owner/repo``) are accepted for backward read
    compatibility; ``host`` is then ``None``.
    """

    model_config = _CFG

    slug: str
    source: PlatformId
    lang: str
    indexed_at: str = Field(alias="indexedAt")
    pages: int
    primary: bool | None = None
    desc: str
    landing_page_id: str | None = Field(default=None, alias="landingPageId")
    repo_url: str | None = Field(default=None, alias="repoUrl")
    # DNS host the repo lives on (github.com, git.hurricane.home, …).
    # First-class so enterprise instances need no fallback heuristics.
    host: str | None = None
    # Git snapshot the wiki was generated from. Populated by ``finalize``
    # from the IndexingJob — historical projects predating these fields
    # render without them (the FE atomic class hides absent values).
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    commit_short: str | None = Field(default=None, alias="commitShort")
    # True when the cloned repo carried a ``.mewbo/wiki.json`` or
    # ``.devin/wiki.json`` grounder file at finalize time. Sole driver of
    # the "Maintainer Edited" badge — defaults to False so legacy projects
    # without the field correctly read as un-edited.
    maintainer_edited: bool = Field(default=False, alias="maintainerEdited")


# ── Platform ───────────────────────────────────────────────────────────────────


class Platform(BaseModel):
    """Git-hosting platform descriptor (used in wizard)."""

    model_config = _CFG

    id: PlatformId
    name: str
    mono: str
    color: str
    short: str
    hosts: list[str]
    token_label: str = Field(alias="tokenLabel")
    token_scope: str = Field(alias="tokenScope")
    token_url: str | None = Field(alias="tokenUrl")
    token_steps: list[str] = Field(alias="tokenSteps")


# ── Language ───────────────────────────────────────────────────────────────────


class Language(BaseModel):
    """Language option shown in the wizard."""

    model_config = _CFG

    id: str
    label: str
    subtle: str | None = None


# ── Nav / TOC entries ──────────────────────────────────────────────────────────


class NavEntry(BaseModel):
    """Sidebar navigation entry."""

    model_config = _CFG

    id: str
    label: str
    lvl: Literal[1, 2, 3]
    parent: str | None = None


class TocEntry(BaseModel):
    """In-page table-of-contents entry."""

    model_config = _CFG

    id: str
    label: str
    lvl: Literal[1, 2, 3]


# ── InlineNode (recursive) ─────────────────────────────────────────────────────
# TypeScript: string | InlineNode[] | {code:string} | {link:string;text:string}
#             | {kind:"src";path:string;lines?:string}
#
# RootModel so the wire shape is a bare value (not wrapped in an object).
# model_rebuild() resolves the forward reference after class definition.


class InlineNode(RootModel[str | list["InlineNode"] | dict]):
    """Recursive inline rich-text node.

    Valid root values:

    - ``str`` — plain text
    - ``list[InlineNode]`` — sequence of inline nodes
    - ``{"code": str}`` — inline code span
    - ``{"link": str, "text": str}`` — hyperlink
    - ``{"kind": "src", "path": str, "lines"?: str}`` — source reference
    """


InlineNode.model_rebuild()


# ── Block variants (discriminated on "kind") ───────────────────────────────────


class PBlock(BaseModel):
    """Paragraph block."""

    model_config = _CFG
    kind: Literal["p"]
    text: InlineNode


class H2Block(BaseModel):
    """Level-2 heading block."""

    model_config = _CFG
    kind: Literal["h2"]
    id: str | None = None
    text: str


class H3Block(BaseModel):
    """Level-3 heading block."""

    model_config = _CFG
    kind: Literal["h3"]
    id: str | None = None
    text: str


class HrBlock(BaseModel):
    """Horizontal-rule block."""

    model_config = _CFG
    kind: Literal["hr"]


class UlBlock(BaseModel):
    """Unordered-list block."""

    model_config = _CFG
    kind: Literal["ul"]
    items: list[InlineNode]


class AccordionBlock(BaseModel):
    """Accordion (collapsible) block."""

    model_config = _CFG
    kind: Literal["accordion"]
    title: str
    items: list[str]


class SourcesBlock(BaseModel):
    """Cited sources block."""

    model_config = _CFG
    kind: Literal["sources"]
    items: list[str]


class TableBlock(BaseModel):
    """Table block."""

    model_config = _CFG
    kind: Literal["table"]
    head: list[str]
    rows: list[list[InlineNode]]


class DiagramBlock(BaseModel):
    """Mermaid diagram reference block."""

    model_config = _CFG
    kind: Literal["diagram"]
    id: str


_BlockAnnotated = Annotated[
    PBlock
    | H2Block
    | H3Block
    | HrBlock
    | UlBlock
    | AccordionBlock
    | SourcesBlock
    | TableBlock
    | DiagramBlock,
    Field(discriminator="kind"),
]


class BlockUnion(RootModel[_BlockAnnotated]):
    """Discriminated union of all block kinds; use ``BlockUnion.model_validate``."""


# ── WikiPage ───────────────────────────────────────────────────────────────────


class SourceRef(BaseModel):
    """Source-file reference with optional line range."""

    model_config = _CFG
    path: str
    lines: str | None = None


class Frontmatter(BaseModel):
    """Parsed frontmatter from a wiki page."""

    model_config = _CFG
    title: str
    slug: str
    relevant_sources: list[SourceRef] | None = Field(
        default=None, alias="relevantSources"
    )
    sources: list[SourceRef] | None = None


class WikiPage(BaseModel):
    """Full wiki page including body, TOC, and sidebar nav."""

    model_config = _CFG

    id: str
    title: str
    frontmatter: Frontmatter
    body: str
    toc: list[TocEntry]
    nav: list[NavEntry]


# ── WizardSubmission ───────────────────────────────────────────────────────────

FilterMode = Literal["exclude", "include"]
DepthMode = Literal["comprehensive", "concise"]


class WizardSubmission(BaseModel):
    """Wizard POST body for triggering a new indexing job."""

    model_config = _CFG

    repo_url: str = Field(alias="repoUrl")
    slug: str
    platform: PlatformId
    token: str | None = None
    depth: DepthMode
    language: str
    model: str
    filter_mode: FilterMode = Field(alias="filterMode")
    dirs: list[str]
    files: list[str]


# ── IndexingJob ────────────────────────────────────────────────────────────────

IndexingStatus = Literal[
    "queued", "scanning", "finalizing", "complete", "cancelled", "failed"
]

# Fine-grained progress phase (defined alongside ``IndexingStatus`` so
# ``IndexingJob`` can reference it).
IndexingPhase = Literal["clone", "scan", "graph", "plan", "pages", "finalize"]


class IndexingJob(BaseModel):
    """Snapshot of an in-progress or finished indexing job."""

    model_config = _CFG

    job_id: str = Field(alias="jobId")
    slug: str
    status: IndexingStatus
    scanned_count: int = Field(alias="scannedCount")
    total_count: int = Field(alias="totalCount")
    current_file: str | None = Field(alias="currentFile")
    landing_page_id: str | None = Field(default=None, alias="landingPageId")
    # Platform of record (gitea, github, …). Hydrated from the wizard
    # submission; lets the FE compose canonical URLs without a round-trip.
    platform: PlatformId | None = None
    # DNS host the repo lives on — first-class for enterprise/self-hosted.
    host: str | None = None
    # LLM model authoring this wiki — surfaced for user transparency.
    model: str | None = None
    # ── Phase-weighted progress ────────────────────────────────────────
    # The coarse 6-state ``status`` is the lifecycle bucket; ``phase`` is
    # the fine-grained progress state. Both the landing card and the
    # indexing page read these to render a single honest progress bar.
    phase: IndexingPhase | None = None
    total_pages: int | None = Field(default=None, alias="totalPages")
    pages_submitted: int = Field(default=0, alias="pagesSubmitted")
    # ISO timestamp at which the current ``phase`` started. Used by the
    # FE to extrapolate an ETA inside the active phase.
    phase_started_at: str | None = Field(default=None, alias="phaseStartedAt")
    # Git snapshot resolved at clone time. ``finalize`` reads these off
    # the snapshot when persisting the Project record — no extra args
    # threaded through the tool chain.
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    # forward ref to WikiError — resolved by IndexingJob.model_rebuild() below
    error: WikiError | None = None


# ── WikiError ──────────────────────────────────────────────────────────────────

WikiErrorCode = Literal[
    "not_found",
    "forbidden",
    "repo_access",
    "quota_exceeded",
    "rate_limited",
    "validation",
    "cancelled",
    "internal",
    "network",
]


class WikiError(BaseModel):
    """Typed error returned by wiki API endpoints and streamed events."""

    model_config = _CFG

    code: WikiErrorCode
    message: str
    hint: str | None = None
    fields: dict[str, str] | None = None
    retry_after: float | None = Field(default=None, alias="retryAfter")


# Resolve forward reference now that WikiError is defined.
IndexingJob.model_rebuild()


# ── IndexingEvent discriminated union ──────────────────────────────────────────


class QueuedEvent(BaseModel):
    """Emitted when a job is accepted into the queue."""

    model_config = _CFG
    type: Literal["queued"]
    job_id: str = Field(alias="jobId")
    slug: str
    total_count: int = Field(alias="totalCount")


class ScanningEvent(BaseModel):
    """Emitted just before a file is analysed."""

    model_config = _CFG
    type: Literal["scanning"]
    file: str
    index: int
    total_count: int = Field(alias="totalCount")


class ScannedEvent(BaseModel):
    """Emitted after a file has been analysed."""

    model_config = _CFG
    type: Literal["scanned"]
    file: str
    index: int
    total_count: int = Field(alias="totalCount")


class FinalizingEvent(BaseModel):
    """Emitted when all files are scanned and final pages are being written."""

    model_config = _CFG
    type: Literal["finalizing"]
    scanned_count: int = Field(alias="scannedCount")
    total_count: int = Field(alias="totalCount")


class CompleteEvent(BaseModel):
    """Terminal event: indexing succeeded."""

    model_config = _CFG
    type: Literal["complete"]
    landing_page_id: str = Field(alias="landingPageId")
    page_count: int = Field(alias="pageCount")


class CancelledEvent(BaseModel):
    """Terminal event: indexing was cancelled."""

    model_config = _CFG
    type: Literal["cancelled"]


class ErrorEvent(BaseModel):
    """Terminal event: indexing failed with an error."""

    model_config = _CFG
    type: Literal["error"]
    error: WikiError


class HeartbeatEvent(BaseModel):
    """Keep-alive event; consumers must ignore it."""

    model_config = _CFG
    type: Literal["heartbeat"]


# ── Honest progress events ────────────────────────────────────────────────
#
# The legacy scanned-file counter jumped to 96% the moment the indexer hit
# the (much longer) page-generation phase, then stalled for users with no
# visibility into what was actually happening. The events below are the
# server-side state machine the FE renders honestly:
#
#   clone → scan → graph → plan → pages → finalize
#
# - `phase` marks the transition between coarse phases.
# - `plan_committed` lands the total page count so the page bar is real.
# - `page_committed` advances per-page as `wiki_submit_page` lands a page.
# - `log` is a free-form milestone line for the timeline.
# (``IndexingPhase`` is defined alongside ``IndexingStatus`` above.)


class PhaseEvent(BaseModel):
    """Coarse-phase transition; drives the phase-weighted progress bar."""

    model_config = _CFG
    type: Literal["phase"]
    name: IndexingPhase


class PlanCommittedEvent(BaseModel):
    """Plan has landed — drives the denominator of the page-write bar."""

    model_config = _CFG
    type: Literal["plan_committed"]
    total_pages: int = Field(alias="totalPages")


class PageCommittedEvent(BaseModel):
    """One page just landed; ``index`` is 0-based."""

    model_config = _CFG
    type: Literal["page_committed"]
    page_id: str = Field(alias="pageId")
    index: int
    total_pages: int = Field(alias="totalPages")


LogLevel = Literal["info", "warn", "error"]


class LogEvent(BaseModel):
    """Free-form milestone line shown in the indexing timeline."""

    model_config = _CFG
    type: Literal["log"]
    level: LogLevel
    text: str


_IndexingEventAnnotated = Annotated[
    QueuedEvent
    | ScanningEvent
    | ScannedEvent
    | FinalizingEvent
    | CompleteEvent
    | CancelledEvent
    | ErrorEvent
    | HeartbeatEvent
    | PhaseEvent
    | PlanCommittedEvent
    | PageCommittedEvent
    | LogEvent,
    Field(discriminator="type"),
]


class IndexingEventUnion(RootModel[_IndexingEventAnnotated]):
    """Discriminated union of all indexing SSE events."""


# ── QaAnswer ───────────────────────────────────────────────────────────────────


class QaAnswer(BaseModel):
    """Complete Q&A answer returned after streaming finishes."""

    model_config = _CFG

    answer_id: str = Field(alias="answerId")
    from_page_id: str = Field(alias="fromPageId")
    summary_sources: list[str] = Field(alias="summarySources")
    model: str
    blocks: list[BlockUnion]
    # Project slug that owns this answer. Persisted so ``resolve_qa_ctx``
    # can recover it after a process restart or any read-back path —
    # previously this field was ``exclude=True`` to keep it off the wire,
    # which also kept it out of the store and left ``slug=""`` on every
    # ctx lookup, breaking ``wiki_search_pages`` (empty BM25 corpus). The
    # FE TS type silently ignores the extra field.
    slug: str = Field(default="")


# ── QaEvent discriminated union ────────────────────────────────────────────────


class MetaEvent(BaseModel):
    """First QA event carrying answer ID and chosen model."""

    model_config = _CFG
    type: Literal["meta"]
    answer_id: str = Field(alias="answerId")
    model: str
    from_page_id: str = Field(alias="fromPageId")


class SummaryReadyEvent(BaseModel):
    """Emitted once summary sources are known."""

    model_config = _CFG
    type: Literal["summary_ready"]
    sources: list[str]


class BlockOpenEvent(BaseModel):
    """Emitted when a new block starts streaming."""

    model_config = _CFG
    type: Literal["block_open"]
    index: int
    block: BlockUnion


class BlockDeltaEvent(BaseModel):
    """Emitted for each text chunk appended to the current block."""

    model_config = _CFG
    type: Literal["block_delta"]
    index: int
    text_append: str = Field(alias="textAppend")


class BlockCloseEvent(BaseModel):
    """Emitted when the current block is finalised."""

    model_config = _CFG
    type: Literal["block_close"]
    index: int


class QaCompleteEvent(BaseModel):
    """Terminal QA event: answer generation succeeded."""

    model_config = _CFG
    type: Literal["complete"]
    total_blocks: int = Field(alias="totalBlocks")


class QaCancelledEvent(BaseModel):
    """Terminal QA event: answer generation was cancelled."""

    model_config = _CFG
    type: Literal["cancelled"]


class QaErrorEvent(BaseModel):
    """Terminal QA event: answer generation failed."""

    model_config = _CFG
    type: Literal["error"]
    error: WikiError


class QaHeartbeatEvent(BaseModel):
    """QA keep-alive event; consumers must ignore it."""

    model_config = _CFG
    type: Literal["heartbeat"]


_QaEventAnnotated = Annotated[
    MetaEvent
    | SummaryReadyEvent
    | BlockOpenEvent
    | BlockDeltaEvent
    | BlockCloseEvent
    | QaCompleteEvent
    | QaCancelledEvent
    | QaErrorEvent
    | QaHeartbeatEvent,
    Field(discriminator="type"),
]


class QaEventUnion(RootModel[_QaEventAnnotated]):
    """Discriminated union of all Q&A SSE events."""


# ── Internal types (not in types.ts) ──────────────────────────────────────────


class PagePlan(BaseModel):
    """Planned wiki page — used by the indexing pipeline before writing."""

    model_config = _CFG

    id: str
    title: str
    description: str = ""
    importance: Literal["high", "medium", "low"] = "medium"
    relevant_files: list[str] = Field(default_factory=list, alias="relevantFiles")
    related_pages: list[str] = Field(default_factory=list, alias="relatedPages")
    parent: str | None = None


GraphNodeType = Literal["File", "Module", "Class", "Function", "Method", "Interface"]
GraphEdgeType = Literal["CONTAINS", "IMPORTS", "CALLS", "EXTENDS", "REFERENCES"]


class GraphNode(BaseModel):
    """Code graph node produced by tree-sitter analysis."""

    model_config = _CFG

    slug: str
    node_id: str
    type: GraphNodeType
    name: str
    file: str
    range: tuple[int, int]
    docstring: str | None = None


class GraphEdge(BaseModel):
    """Directed edge in the code graph."""

    model_config = _CFG

    slug: str
    source: str  # node_id
    target: str  # node_id
    type: GraphEdgeType


class Embedding(BaseModel):
    """Dense embedding vector for a graph node."""

    model_config = _CFG

    slug: str
    node_id: str
    vector: list[float]
    model: str  # embedding model id
    dim: int
