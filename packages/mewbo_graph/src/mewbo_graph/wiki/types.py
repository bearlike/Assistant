"""Pydantic v2 mirrors of the frontend wiki API wire types.

Every model here corresponds 1-to-1 with a TypeScript interface or type alias
declared in ``apps/mewbo_console/src/components/wiki/api/types.ts``.

Conventions:
- ``model_config = ConfigDict(extra="forbid", populate_by_name=True)``
- Python attributes are snake_case; camelCase wire names use ``Field(alias=...)``.
- Discriminated unions are wrapped in ``RootModel`` for ``model_validate`` access.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    field_validator,
    model_validator,
)

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
    # DNS host the repo lives on (github.com, a self-hosted git.example.com, …).
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
    # True when the project was indexed in graph-only (developer) mode: the AST
    # code graph was built with NO documentation pages and NO LLM. Stamped at
    # finalize by ``GraphOnlyIndexer``; drives the console's "No documentation
    # available" empty state and makes the doc-content read seam raise
    # ``DocumentationUnavailableError``. Defaults False so ordinary/legacy
    # projects correctly read as documented.
    graph_only: bool = Field(default=False, alias="graphOnly")


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
    """Wizard POST body for triggering a new indexing job.

    ``repo_url`` is optional: a NON-git "catalog" workspace (programmatic
    document ingestion via ``CatalogIngestor`` / ``POST .../documents``) has no
    clone URL. The git indexing pipeline still requires it — its own validation
    rejects a clone with an empty URL — but the model itself no longer forces
    one so the same submission shape carries a repo-less catalog project.
    """

    model_config = _CFG

    repo_url: str | None = Field(default=None, alias="repoUrl")
    slug: str
    platform: PlatformId
    token: str | None = None
    depth: DepthMode
    language: str
    model: str
    filter_mode: FilterMode = Field(alias="filterMode")
    dirs: list[str]
    files: list[str]
    # Request a DETERMINISTIC, zero-LLM index: clone → scan → AST graph →
    # finalize, SKIPPING enrich/plan/pages. Produces a populated graph + zero
    # pages (developer mode). Honoured ONLY when ``runtime.developer_mode`` is
    # on — the index route forces it False otherwise, so an unprivileged caller
    # can never opt into the no-docs path. Optional + defaulted so ordinary
    # submissions are unchanged.
    graph_only: bool = Field(default=False, alias="graphOnly")
    # Optional branch/tag/sha to clone; null = the repo's default branch (the
    # behaviour when omitted is unchanged).
    ref: str | None = Field(default=None)
    # Ordered model ladder the indexer falls back through when its primary model
    # fails. ``None`` = inherit the configured fallback policy, which is what
    # every indexing run did before this field existed; a list overrides it for
    # this job only. Optional + defaulted, so existing submissions are unchanged.
    fallback_models: list[str] | None = Field(default=None, alias="fallbackModels")


# ── ProjectSettings (the durable, slug-keyed edit target) ──────────────────


class ProjectSettings(BaseModel):
    """The EDITABLE settings of an onboarded wiki project, keyed by slug.

    Why this exists. :class:`Project` is a DISPLAY snapshot —
    ``wiki_finalize`` / ``GraphOnlyIndexer`` rebuild it WHOLESALE on every
    successful (re)index, so any field written directly onto it is silently wiped
    by the next reindex. The settings a project is actually re-indexed WITH have
    always been the :class:`WizardSubmission` — but that was persisted as a
    JOB-keyed sidecar, which gave an editor no stable write target (and made
    "latest submission" a scan over jobs).

    This record is that target: ONE per slug, holding the submission contract
    minus the never-persisted ``token``, plus a ``desc`` display override.
    ``WikiIndexingJob.refresh`` consults it FIRST (falling back to the legacy
    per-job scan for projects onboarded before it existed) — which is what makes
    an edit actually take effect on the next index.

    Two fields are deliberately absent. ``slug`` is the store key for pages, jobs,
    credentials and freshness, so it is immutable — there is no rename primitive.
    ``token`` never lands here: credentials resolve through the ONE registry
    (``mewbo_graph.wiki.credentials``), and a secret in this record would be a
    third source of truth.
    """

    model_config = _CFG

    slug: str
    repo_url: str | None = Field(default=None, alias="repoUrl")
    platform: PlatformId
    model: str
    depth: DepthMode
    language: str
    filter_mode: FilterMode = Field(alias="filterMode")
    dirs: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    graph_only: bool = Field(default=False, alias="graphOnly")
    ref: str | None = None
    # Ordered model ladder the next index falls back through; ``None`` = inherit
    # the configured policy. It MUST round-trip through
    # ``from_submission``/``to_submission``: ``WikiIndexingJob.refresh`` rebuilds
    # its submission from THIS record, so a ladder the record cannot carry is
    # silently dropped from every index after the first — including for a project
    # that was onboarded with one.
    fallback_models: list[str] | None = Field(default=None, alias="fallbackModels")
    # User-set description override. ``None`` = no override, so finalize's
    # platform-API fetch wins (today's behaviour, unchanged). A non-empty value
    # SURVIVES a reindex — that is the read-preserve contract implemented once in
    # ``plugins.wiki.finalize._resolve_project_desc`` and shared by both indexers.
    desc: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")

    @classmethod
    def from_submission(
        cls, sub: WizardSubmission, *, desc: str | None = None
    ) -> ProjectSettings:
        """Project a :class:`WizardSubmission` onto the durable settings record.

        ``sub.token`` is dropped (the credential registry owns it). *desc* carries
        an existing override forward, so re-seeding this record from a submission
        — which every ``WikiIndexingJob.start`` does, including the one a refresh
        drives — cannot clobber a user's edited description.
        """
        return cls(
            slug=sub.slug,
            repoUrl=sub.repo_url,
            platform=sub.platform,
            model=sub.model,
            depth=sub.depth,
            language=sub.language,
            filterMode=sub.filter_mode,
            dirs=list(sub.dirs),
            files=list(sub.files),
            graphOnly=sub.graph_only,
            ref=sub.ref,
            fallbackModels=(
                list(sub.fallback_models) if sub.fallback_models is not None else None
            ),
            desc=desc,
        )

    def to_submission(self) -> WizardSubmission:
        """Rebuild the :class:`WizardSubmission` a re-index replays.

        Always token-less: the clone tool's ``resolve_chain`` reads the durable
        credential itself at clone time, so a refresh never needs to carry one.
        """
        return WizardSubmission(
            repoUrl=self.repo_url,
            slug=self.slug,
            platform=self.platform,
            token=None,
            depth=self.depth,
            language=self.language,
            model=self.model,
            filterMode=self.filter_mode,
            dirs=list(self.dirs),
            files=list(self.files),
            graphOnly=self.graph_only,
            ref=self.ref,
            fallbackModels=(
                list(self.fallback_models) if self.fallback_models is not None else None
            ),
        )


# ── Catalog document ingestion (non-git StructureProvider) ──────────────────


class CatalogDocument(BaseModel):
    """One programmatically-ingested catalog record (a product, FAQ, doc, …).

    The wire shape ``POST /v1/wiki/projects/{slug}/documents`` accepts. Each
    record becomes BOTH a ``WikiPage`` (BM25 + ``wiki_search_pages``) AND a
    graph node carrying the text (embeddings + ``wiki_code_search``) so the
    existing :class:`HybridRetriever` grounds it with no pipeline change.
    """

    model_config = _CFG

    id: str = Field(..., min_length=1, description="stable document id (idempotent upsert)")
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, description="full body — the grounding corpus")
    metadata: dict[str, str] = Field(default_factory=dict)


class CatalogIngestReport(BaseModel):
    """Outcome of a :class:`CatalogIngestor.ingest` call."""

    model_config = _CFG

    slug: str
    ingested: int = Field(description="number of documents written this call")
    embedded: int = Field(default=0, description="documents whose node was embedded")
    total_documents: int = Field(
        default=0, alias="totalDocuments", description="catalog size after this call"
    )
    bm25_only: bool = Field(
        default=False,
        alias="bm25Only",
        description="True when the embedder was absent → lexical-only grounding",
    )
    landing_page_id: str = Field(alias="landingPageId")


# ── RepoCredential ─────────────────────────────────────────────────────────────


class RepoCredential(BaseModel):
    """A persisted repository credential — a git token OR an SSH/deploy key.

    Stored per-scope in the isolated credential store (``CredentialScope`` —
    see ``mewbo_graph.wiki.credentials``, which also owns the host-covers-repo
    sharing rule). The credential itself carries NO scope field: the scope is
    the store KEY, and it is stamped into the blob at save time — one binding,
    not two that can disagree. Plaintext-at-rest behind the store's
    ``_encode``/``_decode`` seam; ALWAYS redacted in-flight (SSE / transcript /
    logs).
    """

    model_config = _CFG

    kind: Literal["token", "ssh_key"]
    value: str
    username: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")

    @field_validator("value", mode="before")
    @classmethod
    def _strip_value(cls, v: Any) -> Any:
        """Strip surrounding whitespace BEFORE validation — a real, silent footgun.

        A PAT pasted from a terminal or a file carries a trailing newline; git
        injects it verbatim into the URL/header and the remote answers 401 with
        no hint that an invisible character is the cause. Stripping is safe for
        an SSH key too: only the surrounding whitespace goes (internal newlines
        are preserved), and ``clone._ssh_env_for`` re-appends the terminating
        newline the key file requires.
        """
        return v.strip() if isinstance(v, str) else v

    @field_validator("value")
    @classmethod
    def _value_not_empty(cls, v: str) -> str:
        """Reject an empty credential — an empty token/key is never useful.

        Runs AFTER :meth:`_strip_value`, so a whitespace-only value is empty here.
        """
        if not v:
            raise ValueError("credential value must not be empty")
        return v

    @property
    def dedup_key(self) -> tuple[str, str, str | None]:
        """The identity two credentials must share to be considered the same one.

        ``username`` is part of it deliberately: a value shared by two usernames
        (a GitLab ``oauth2`` deploy token vs a PAT) authenticates DIFFERENTLY, so
        the resolution chain must try both rather than dedup the second away.
        """
        return (self.kind, self.value, self.username)


# ── IndexingJob ────────────────────────────────────────────────────────────────

IndexingStatus = Literal[
    "queued",
    "scanning",
    "finalizing",
    "interrupted",
    "complete",
    "cancelled",
    "failed",
]

# Fine-grained progress phase (defined alongside ``IndexingStatus`` so
# ``IndexingJob`` can reference it).
IndexingPhase = Literal["clone", "scan", "graph", "enrich", "plan", "pages", "finalize"]


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

# Lifecycle of a Q&A run as seen on the *snapshot* (``GET /v1/wiki/qa/<id>``).
# ``running`` until the run finalizes; the three terminal values mirror the
# terminal QA SSE events (``complete`` / ``cancelled`` / ``error``). A
# non-streaming consumer (the MCP ``ask_wiki`` poll) reads this top-level field
# as the authoritative done-signal instead of guessing from block churn.
QaStatus = Literal["running", "complete", "cancelled", "error"]

# Terminal values — the run is finished iff its status is in this set.
QA_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "cancelled", "error"})


class QaTurn(BaseModel):
    """One completed question+answer round within a continued QA answer.

    Snapshotted from ``QaAnswer``'s top-level fields when
    ``WikiQaSession.follow_up`` starts a new turn on the same answer/session
    — see ``QaAnswer.turns``.
    """

    model_config = _CFG

    question: str
    blocks: list[BlockUnion] = Field(default_factory=list)
    summary_sources: list[str] = Field(default_factory=list, alias="summarySources")
    accessed_sources: list[str] = Field(default_factory=list, alias="accessedSources")
    models_used: list[str] = Field(default_factory=list, alias="modelsUsed")
    status: QaStatus = Field(default="complete")


class QaAnswer(BaseModel):
    """Complete Q&A answer returned after streaming finishes.

    Top-level ``question``/``blocks``/``summary_sources``/``accessed_sources``/
    ``models_used``/``status`` always describe the LATEST turn — the shape a
    single-shot consumer (MCP ``ask_wiki``, a fresh ``GET``) already expects,
    byte-compatible with before ``turns`` existed. ``turns`` additively carries
    every PRIOR completed turn once a session is continued via
    ``WikiQaSession.follow_up``; a never-followed-up answer has an
    empty ``turns`` list.
    """

    model_config = _CFG

    answer_id: str = Field(alias="answerId")
    from_page_id: str = Field(alias="fromPageId")
    # The current/latest turn's question text. Empty for answers persisted
    # before this field existed (older snapshots validate unchanged).
    question: str = Field(default="")
    summary_sources: list[str] = Field(alias="summarySources")
    model: str
    blocks: list[BlockUnion]
    # Deterministic provenance of the CURRENT turn (NOT the LLM's hand-picked
    # citations). ``accessed_sources`` is the de-duplicated trail of every graph
    # node + source file + page the probes actually touched (the probe tools
    # record an ``access`` event per call; the finalizer folds them). ``models_used``
    # is the distinct set of models that ran across the hypervisor + its probes,
    # across every turn (session-wide, never reset — see ``QaFinalizer.enrich``).
    # Both surface so the UI can show "what was read" + "which models" alongside
    # the answer. Defaulted so older persisted answers validate unchanged.
    accessed_sources: list[str] = Field(default_factory=list, alias="accessedSources")
    models_used: list[str] = Field(default_factory=list, alias="modelsUsed")
    # Run lifecycle of the CURRENT turn on the persisted snapshot — ``running``
    # until a terminal event finalizes the run. ``QaFinalizer.close`` sets
    # ``complete``; ``WikiQaSession.cancel`` sets ``cancelled``. This is the
    # field the MCP ``ask_wiki`` poll keys off of (no fragile "blocks
    # unchanged" guess).
    status: QaStatus = Field(default="running")
    # Project slug that owns this answer. Persisted so ``resolve_qa_ctx``
    # can recover it after a process restart or any read-back path —
    # previously this field was ``exclude=True`` to keep it off the wire,
    # which also kept it out of the store and left ``slug=""`` on every
    # ctx lookup, breaking ``wiki_search_pages`` (empty BM25 corpus). The
    # FE TS type silently ignores the extra field.
    slug: str = Field(default="")
    # Prior completed turns, oldest first. Empty for a
    # single-shot (never-followed-up) answer.
    turns: list[QaTurn] = Field(default_factory=list)


# ── QaEvent discriminated union ────────────────────────────────────────────────


class MetaEvent(BaseModel):
    """First QA event of a turn, carrying answer ID and chosen model.

    Emitted once per turn — at the start of :meth:`WikiQaSession.start` AND
    :meth:`WikiQaSession.follow_up` — so it also marks where each turn's
    events begin in the append-only log (see ``QaFinalizer.current_turn_events``).
    """

    model_config = _CFG
    type: Literal["meta"]
    answer_id: str = Field(alias="answerId")
    model: str
    from_page_id: str = Field(alias="fromPageId")
    # The backing Mewbo session id — exposed so continuation is addressable /
    # traceable. Defaulted so an older persisted event replays
    # unchanged.
    session_id: str = Field(default="", alias="sessionId")


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


# ── Code graph (schema v2 — validated discriminated union) ─────
#
# Every node kind is a subclass of ``GraphNodeBase`` carrying a ``type`` Literal
# discriminator (SOTA precedent: Kythe kind+subkind, SCIP two-axis, CPG endpoint
# rules). ``subkind`` is the open per-kind refinement (e.g. Kotlin ``object`` vs
# ``companion object``); ``attributes`` is the namespaced extension bag
# (``<lang|tool>.<name>`` keys) so a new language adds facts WITHOUT a schema
# change. ``External`` and ``Folder`` stay admitted as VIEW-only kinds
# (synthesized by ``KnowledgeGraphView`` / ``FolderTree`` in the hierarchy wire
# mode — never persisted by the extractor) so the viewer reuses one serialiser.
# ``Object`` (Kotlin ``object``/``companion``) + ``Property`` (fields/constants)
# are the new kinds the Kotlin/Java child emits.
GraphNodeType = Literal[
    "File",
    "Module",
    "Class",
    "Function",
    "Method",
    "Interface",
    "Object",
    "Property",
    "External",
    "Folder",
]
GraphEdgeType = Literal["CONTAINS", "IMPORTS", "CALLS", "EXTENDS", "REFERENCES"]

# The widest value a namespaced extension fact may carry (a JSON scalar).
_JsonScalar = str | int | float | bool | None

# Node value objects are immutable (see ``GraphNodeBase`` docstring), so give
# them their own frozen config on top of the house ``_CFG``.
_NODE_CFG = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


def _require_namespaced(v: dict[str, _JsonScalar]) -> dict[str, _JsonScalar]:
    """Reject any attribute key not namespaced ``<lang|tool>.<name>``.

    A dotless key (or one with an empty segment) is a cross-language/tool
    collision waiting to happen, so every extension fact MUST be prefixed
    (``kotlin.visibility``, ``scip.symbol``). Enforced identically on nodes +
    edges — the ONE validator both axes share (DRY).
    """
    for key in v:
        head, dot, tail = key.partition(".")
        if not dot or not head or not tail:
            raise ValueError(
                f"attribute key {key!r} must be namespaced '<lang|tool>.<name>' "
                "(a dot separating two non-empty segments)"
            )
    return v


class GraphNodeBase(BaseModel):
    """Shared identity + provenance for every code-graph node kind.

    ``frozen`` — a node is a value object the extractor emits once and every
    downstream layer only READS (the view stamps hierarchy onto the wire dict,
    never the node), so immutability is free and makes nodes hashable/cacheable.
    Per-kind subclasses narrow ``type`` to a Literal; the discriminated
    :data:`GraphNode` union dispatches on it. Legacy persisted nodes predate
    ``subkind``/``attributes`` — the defaults make them validate unchanged.
    """

    model_config = _NODE_CFG

    slug: str
    node_id: str
    type: GraphNodeType
    name: str
    file: str
    range: tuple[int, int]
    docstring: str | None = None
    # Open per-kind refinement (Kythe subkind): e.g. ``companion`` for a Kotlin
    # companion object, ``const`` for a constant Property. ``None`` = unrefined.
    subkind: str | None = None
    # Namespaced extension bag (``<lang|tool>.<name>`` keys) — the zero-schema-
    # change seam for language-specific facts. Defaults empty so legacy nodes
    # (which lack it) validate unchanged.
    attributes: dict[str, _JsonScalar] = Field(default_factory=dict)
    # Per-job/commit attribution. ``commit_sha`` is the git commit whose index
    # produced this node; ``job_id`` the job that wrote it. The store stamps both
    # at write from the owning job ctx. ``commit_sha`` is what makes "the graph
    # for THIS commit is built" expressible (the resume skip predicate keys on
    # it) and what a completed re-index supersedes on: every prior-commit node is
    # reaped, so the store stops being the UNION of every commit ever indexed for
    # a slug. ``None`` on a commit-less catalog node and on any node written
    # before artifact isolation (the backfill stamps those); the default keeps
    # such legacy persisted nodes valid. NOT part of the wire shape — the graph
    # view assembles its Cytoscape payload field-by-field and never dumps a node.
    commit_sha: str | None = None
    job_id: str | None = None

    @field_validator("attributes")
    @classmethod
    def _ns_attributes(cls, v: dict[str, _JsonScalar]) -> dict[str, _JsonScalar]:
        """Enforce namespaced attribute keys (see :func:`_require_namespaced`)."""
        return _require_namespaced(v)


class FileNode(GraphNodeBase):
    """A source file — the container every in-file symbol hangs off."""

    type: Literal["File"] = "File"


class ModuleNode(GraphNodeBase):
    """An imported module target (synthetic cross-file IMPORTS endpoint)."""

    type: Literal["Module"] = "Module"


class ClassNode(GraphNodeBase):
    """A class / struct definition."""

    type: Literal["Class"] = "Class"


class InterfaceNode(GraphNodeBase):
    """An interface / trait / protocol definition."""

    type: Literal["Interface"] = "Interface"


class FunctionNode(GraphNodeBase):
    """A top-level (unbound) function."""

    type: Literal["Function"] = "Function"


class MethodNode(GraphNodeBase):
    """A method bound to a class/struct/object."""

    type: Literal["Method"] = "Method"


class ObjectNode(GraphNodeBase):
    """A singleton object (Kotlin ``object`` / ``companion object``; Scala later)."""

    type: Literal["Object"] = "Object"


class PropertyNode(GraphNodeBase):
    """A field / property / constant."""

    type: Literal["Property"] = "Property"


class ExternalNode(GraphNodeBase):
    """VIEW-only convergence node for an unresolved out-of-repo symbol."""

    type: Literal["External"] = "External"


class FolderNode(GraphNodeBase):
    """VIEW-only directory supernode (hierarchy wire mode)."""

    type: Literal["Folder"] = "Folder"


_GraphNodeAnnotated = Annotated[
    FileNode
    | ModuleNode
    | ClassNode
    | InterfaceNode
    | FunctionNode
    | MethodNode
    | ObjectNode
    | PropertyNode
    | ExternalNode
    | FolderNode,
    Field(discriminator="type"),
]

# Public alias — annotate ``list[GraphNode]`` / ``Iterable[GraphNode]`` with the
# discriminated union so a value is always one of the per-kind classes.
# Construct via the per-kind classes (or :func:`make_graph_node` for a dynamic
# ``type``); validate persisted dicts via :data:`GraphNodeAdapter`.
GraphNode = _GraphNodeAnnotated

# Rehydration entrypoint — dispatches a persisted node dict/json to its per-kind
# subclass by the ``type`` discriminator (the store's node-load seam uses this,
# so a loaded node is the right subclass, not a lossy base). ``model_construct``
# skips re-validation for an already-trusted persisted graph (perf).
GraphNodeAdapter: TypeAdapter[GraphNode] = TypeAdapter(_GraphNodeAnnotated)

_NODE_CLS_BY_TYPE: dict[str, type[GraphNodeBase]] = {
    "File": FileNode,
    "Module": ModuleNode,
    "Class": ClassNode,
    "Interface": InterfaceNode,
    "Function": FunctionNode,
    "Method": MethodNode,
    "Object": ObjectNode,
    "Property": PropertyNode,
    "External": ExternalNode,
    "Folder": FolderNode,
}


def make_graph_node(**data: Any) -> GraphNode:
    """Construct the per-kind ``GraphNode`` subclass for a dynamic ``type``.

    Thin factory over :data:`_NODE_CLS_BY_TYPE` for the (few) call sites whose
    ``type`` is only known at runtime — collapsing an otherwise-repeated
    per-kind branch (DRY). Static sites should use the per-kind class directly.
    """
    kind = data.get("type")
    try:
        cls = _NODE_CLS_BY_TYPE[kind]  # type: ignore[index]
    except KeyError:
        raise ValueError(f"unknown graph node type: {kind!r}") from None
    # ``cls`` is a concrete union member, but the type checker only knows it as
    # ``type[GraphNodeBase]`` — narrow to the union at this single seam.
    return cast("GraphNode", cls(**data))


class GraphEdge(BaseModel):
    """Directed edge in the code graph."""

    model_config = _CFG

    slug: str
    source: str  # node_id
    target: str  # node_id
    type: GraphEdgeType
    # Carry for cross-file IMPORTS/CALLS/EXTENDS whose target is NOT an in-repo
    # node. ``target`` then holds a synthetic external id and ``target_name`` the
    # raw symbol name, so the view can converge every reference to one named
    # ``External`` node (a view concern — the persisted node table stays
    # real-in-repo-symbols only). ``None`` for ordinary in-repo edges.
    target_name: str | None = None
    # Same open subkind + namespaced extension bag as the node axes.
    # Defaults keep legacy persisted edges validating unchanged.
    subkind: str | None = None
    attributes: dict[str, _JsonScalar] = Field(default_factory=dict)
    # Per-job/commit attribution — see ``GraphNodeBase``. Stamped at write and
    # superseded per commit alongside the nodes an edge connects.
    commit_sha: str | None = None
    job_id: str | None = None

    @field_validator("attributes")
    @classmethod
    def _ns_attributes(cls, v: dict[str, _JsonScalar]) -> dict[str, _JsonScalar]:
        """Enforce namespaced attribute keys (see :func:`_require_namespaced`)."""
        return _require_namespaced(v)


# CPG-style endpoint rule: the node kinds legally allowed as the SOURCE of
# each edge type, derived from what the tree-sitter extractor + scip resolver
# ACTUALLY emit today, widened to the near-term container kinds (Class→Method
# CONTAINS) and the new Kotlin Object/Property. A permissive superset — its job
# is to reject a clearly-wrong endpoint (e.g. a CONTAINS rooted at a Function),
# never to reject a graph the current pipeline produces. Checked only when the
# source resolves to an in-graph node; a by-name synthetic edge (``target_name``
# set) whose source is itself a by-name id — the tree-sitter EXTENDS convention,
# where the subclass-name byte offset differs from the class node id — is exempt.
_EDGE_SOURCE_KINDS: dict[str, frozenset[str]] = {
    "CONTAINS": frozenset({"File", "Folder", "Module", "Class", "Interface", "Object"}),
    "IMPORTS": frozenset({"File", "Module"}),
    "CALLS": frozenset({"File", "Function", "Method"}),
    "EXTENDS": frozenset({"Class", "Interface", "Object"}),
    "REFERENCES": frozenset(
        {"File", "Module", "Class", "Interface", "Function", "Method", "Object", "Property"}
    ),
}


class CodeGraph(BaseModel):
    """Whole-graph validated bundle of nodes + edges (schema v2).

    Assembled + validated ONCE at ingest (``build_graph_core``) before the store
    persists the flat lists. The ``model_validator`` enforces three invariants a
    per-node/edge check can't: (1) node-id uniqueness; (2) referential integrity
    — a non-synthetic edge's endpoints must both resolve to a node (a synthetic
    cross-file edge carrying ``target_name`` may point out-of-repo, and its
    source may itself be a by-name id); (3) CPG-style per-edge-type endpoint
    rules. An already-trusted persisted graph can skip re-validation via
    ``model_construct``.
    """

    model_config = _CFG

    schema_version: Literal["1"] = "1"
    nodes: list[_GraphNodeAnnotated] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_graph(self) -> CodeGraph:
        """Enforce id-uniqueness + referential integrity + endpoint rules."""
        id_type: dict[str, str] = {}
        for n in self.nodes:
            if n.node_id in id_type:
                raise ValueError(f"duplicate node_id: {n.node_id!r}")
            id_type[n.node_id] = n.type
        for e in self.edges:
            synthetic = e.target_name is not None
            if not synthetic:
                if e.source not in id_type:
                    raise ValueError(
                        f"{e.type} edge source {e.source!r} does not resolve to a node"
                    )
                if e.target not in id_type:
                    raise ValueError(
                        f"{e.type} edge target {e.target!r} does not resolve to a node "
                        "(and carries no target_name)"
                    )
            allowed = _EDGE_SOURCE_KINDS.get(e.type)
            src_type = id_type.get(e.source)
            if allowed is not None and src_type is not None and src_type not in allowed:
                raise ValueError(
                    f"{e.type} edge illegal source kind {src_type!r} "
                    f"(allowed: {sorted(allowed)})"
                )
        return self


class Embedding(BaseModel):
    """Dense embedding vector for a graph node."""

    model_config = _CFG

    slug: str
    node_id: str
    vector: list[float]
    model: str  # embedding model id
    dim: int
    # Per-job/commit attribution — see ``GraphNodeBase``. An embedding is reaped
    # with the node it vectorises when a completed re-index supersedes its commit.
    commit_sha: str | None = None
    job_id: str | None = None
