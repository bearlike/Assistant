"""Pydantic v2 mirrors of the frontend wiki API wire types.

Every model here corresponds 1-to-1 with a TypeScript interface or type alias
declared in ``apps/mewbo_console/src/components/wiki/api/types.ts``.

Conventions:
- ``model_config = ConfigDict(extra="forbid", populate_by_name=True)``
- Python attributes are snake_case; camelCase wire names use ``Field(alias=...)``.
- Discriminated unions are wrapped in ``RootModel`` for ``model_validate`` access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast

from mewbo_core.contracts.progress import ProgressLedger, StepRecord
from mewbo_core.workspaces.repositories import PlatformId
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

# The one timestamp spelling every job-progress field is written and read in
# (``phase_started_at``, ``last_progress_at``). Second precision, explicit Z —
# a wire format, so it is parsed here rather than re-guessed at each reader.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ``PlatformId`` lives in ``mewbo_core.workspaces.repositories`` — the repository
# registry is reachable from a BASE install, which cannot import this optional
# library. It is imported above (and used throughout this module), so it stays
# re-exported here: dropping the re-export would break every importer of
# ``mewbo_graph.wiki.types.PlatformId``.


# ── Project ────────────────────────────────────────────────────────────────────


class StepMeasurement(BaseModel):
    """One completed step's observed elapsed time and final unit count.

    The record belongs on :class:`Project`, the stable current-index snapshot,
    rather than a job that the next index has to search for. It stays bounded by
    the declared plan: one measurement per step, never per file, node, or page.
    """

    model_config = _CFG

    seconds: float = Field(ge=0)
    units: int | None = Field(default=None, ge=0)

    @classmethod
    def from_record(
        cls, record: StepRecord, now: datetime
    ) -> StepMeasurement | None:
        """Project one completed non-skipped ledger record into a measurement.

        Cost: ``O(1)``. The clock arrives from the caller for testability. A
        skipped resumed phase has no fresh cost, so it must not replace an earlier
        measurement with a near-zero duration.
        """
        if record.state != "done":
            return None
        seconds = record.elapsed_seconds(now)
        if seconds is None or seconds < 0:
            return None
        return cls(seconds=seconds, units=record.total)

    def blended_with(self, observed: StepMeasurement) -> StepMeasurement:
        """Blend a newer reading into this one. Cost: ``O(1)``.

        The previous reading keeps 75% of the result and the newest completed run
        supplies 25%, which damps one-off noise without making calibration stale.
        When both readings count units, blend their rates projected onto the new
        count — otherwise a doubled repository would inherit half its time.
        """
        if self.units and observed.units:
            prior_at_new_size = (self.seconds / self.units) * observed.units
            seconds = (prior_at_new_size * 0.75) + (observed.seconds * 0.25)
        else:
            seconds = (self.seconds * 0.75) + (observed.seconds * 0.25)
        return StepMeasurement(seconds=seconds, units=observed.units)


class Project(BaseModel):
    """Landing-card model for a wiki project.

    Slug is fully qualified — ``host/owner/repo`` — so the identity is
    unambiguous across self-hosted and enterprise instances. A two-segment
    slug (``owner/repo``) also reads, with ``host`` then ``None``.
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
    # from the IndexingJob; absent when no job stamped one (the FE atomic
    # class hides absent values).
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    commit_short: str | None = Field(default=None, alias="commitShort")
    # True when the cloned repo carried a ``.mewbo/wiki.json`` or
    # ``.devin/wiki.json`` grounder file at finalize time. Sole driver of
    # the "Maintainer Edited" badge — absent means un-edited.
    maintainer_edited: bool = Field(default=False, alias="maintainerEdited")
    # True when the project was indexed in graph-only (developer) mode: the AST
    # code graph was built with NO documentation pages and NO LLM. Stamped at
    # finalize by ``GraphOnlyIndexer``; drives the console's "No documentation
    # available" empty state and makes the doc-content read seam raise
    # ``DocumentationUnavailableError``. Absent means documented.
    graph_only: bool = Field(default=False, alias="graphOnly")
    # Copied from ``IndexingJob.fingerprint`` at finalize — see
    # ``IndexFingerprint`` (defined below; forward ref resolved by
    # ``Project.model_rebuild()`` right after it). ``None`` when no job
    # recorded one — read downstream as "cannot compare, full rebuild",
    # never as a silent match.
    fingerprint: IndexFingerprint | None = None
    # What exact cross-file symbol resolution achieved for the indexed commit
    # — see ``GraphResolution`` (defined below; forward ref resolved by
    # ``Project.model_rebuild()``). Written by the graph phase. ``None`` means
    # the question was never recorded for this project, which reads as unknown
    # and never as a healthy pass.
    resolution: GraphResolution | None = None
    # The prior completed index's observed step costs. One row per declared step,
    # never per repository unit, so loading a project stays ``O(one record)``.
    step_measurements: dict[str, StepMeasurement] = Field(
        default_factory=dict, alias="stepMeasurements"
    )

    def measured_steps(
        self,
        records: list[StepRecord],
        *,
        declared_keys: set[str],
        now: datetime,
    ) -> dict[str, StepMeasurement]:
        """Blend this run's completed records into the calibrated step costs.

        Cost: ``O(declared steps)``. A 75/25 rolling blend retains most of the
        prior reading while admitting a repository's current shape; one run is
        noisy, but a repository can also change size between indexes. Only
        declared, completed records participate, so resume-skipped steps retain
        the previous reading instead of becoming falsely free.
        """
        measurements = {
            key: value
            for key, value in self.step_measurements.items()
            if key in declared_keys
        }
        for record in records:
            if record.key not in declared_keys:
                continue
            observed = StepMeasurement.from_record(record, now)
            if observed is None:
                continue
            previous = measurements.get(record.key)
            measurements[record.key] = (
                observed if previous is None else previous.blended_with(observed)
            )
        return measurements


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

# Ceiling on operator-authored indexing guidance. It is a per-PAGE cost, not a
# per-index one — see ``WizardSubmission.check_custom_instructions``.
MAX_CUSTOM_INSTRUCTIONS_CHARS = 4000


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
    # fails. ``None`` = inherit the configured fallback policy; a list
    # overrides it for this job only.
    fallback_models: list[str] | None = Field(default=None, alias="fallbackModels")
    # Embedding model this project's vectors are built and searched with.
    # ``None`` = inherit the deployment default (``wiki.embedding.model``), which
    # is what every project indexed before this field existed carries.
    #
    # It is per-PROJECT rather than per-job because the write side and the read
    # side have to agree: two embedding models rarely share a vector width, and a
    # store holding both returns wrong neighbours rather than erroring. Changing
    # it is therefore a full rebuild, which nothing here has to arrange —
    # ``IndexFingerprint.embedding_model`` already records the model a run
    # actually embedded with, and a mismatch against it forces the full path.
    embedding_model: str | None = Field(default=None, alias="embeddingModel")
    # Free-text operator guidance appended to the indexer's playbook.
    # ``None`` = no guidance.
    custom_instructions: str | None = Field(default=None, alias="customInstructions")
    # External MCP servers to attach for the duration of an index, in the
    # standard ``{name: {command|url, …}}`` MCP config shape. ``None`` = attach
    # nothing (today's behaviour). Operator-set at onboarding or in settings
    # ONLY — an MCP server entry names a process to spawn, so no per-run path
    # and nothing an agent can reach may write it.
    mcp_servers: dict[str, dict] | None = Field(default=None, alias="mcpServers")

    @field_validator("custom_instructions")
    @classmethod
    def check_custom_instructions(cls, v: str | None) -> str | None:
        """Strip, collapse blank to ``None``, and cap the length.

        THE rule for this field, delegated to by :class:`ProjectSettings` and by
        the api's ``ProjectSettingsPatch`` so the three hops cannot disagree
        about what a valid value is.

        The cap is why this is a validator rather than a bare field: the text is
        appended to the page-writer prompt of EVERY page in the fan-out, so it
        is paid once per page, not once per index — a repository with 200 pages
        pays for it 200 times.
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if len(stripped) > MAX_CUSTOM_INSTRUCTIONS_CHARS:
            raise ValueError(
                f"customInstructions is {len(stripped)} characters, over the "
                f"{MAX_CUSTOM_INSTRUCTIONS_CHARS}-character limit. This text is "
                "appended to the prompt of every page the indexer writes, so it "
                "is paid once per page rather than once per index."
            )
        return stripped

    @field_validator("mcp_servers")
    @classmethod
    def check_mcp_servers(cls, v: dict[str, dict] | None) -> dict[str, dict] | None:
        """Validate the server map's shape; an empty map collapses to ``None``.

        THE rule for this field, shared by the same three hops as
        :meth:`check_custom_instructions`. Deliberately shallow: the VALUE is the
        standard MCP server-config shape that ``get_merged_mcp_config`` consumes
        verbatim, and re-modelling it here would be a second, drifting copy of a
        schema this package does not own. What is checked is what this layer
        genuinely owns — that the map is name→object, with usable names.
        """
        if v is None:
            return None
        if not v:
            return None
        for name, entry in v.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("mcpServers keys must be non-empty server names")
            if not isinstance(entry, dict):
                raise ValueError(
                    f"mcpServers[{name!r}] must be an object describing one MCP server"
                )
        return {name.strip(): entry for name, entry in v.items()}


# ── ProjectSettings (the durable, slug-keyed edit target) ──────────────────


class ProjectSettings(BaseModel):
    """The EDITABLE settings of an INDEXED wiki project, keyed by slug.

    Why this exists. :class:`Project` is a DISPLAY snapshot —
    ``wiki_finalize`` / ``GraphOnlyIndexer`` rebuild it WHOLESALE on every
    successful (re)index, so any field written directly onto it is silently wiped
    by the next reindex. The settings a project is actually re-indexed WITH have
    always been the :class:`WizardSubmission` — but that was persisted as a
    JOB-keyed sidecar, which gave an editor no stable write target (and made
    "latest submission" a scan over jobs).

    This record is that target: ONE per slug, holding the submission contract
    minus the never-persisted ``token``, plus a ``desc`` display override.
    ``WikiIndexingJob.refresh`` consults it FIRST, falling back to the per-job
    scan when a project has no record — which is what makes an edit actually
    take effect on the next index.

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
    # that was first indexed with one.
    fallback_models: list[str] | None = Field(default=None, alias="fallbackModels")
    # Embedding model the next index builds this project's vectors with, and that
    # every read of them must embed its query with. ``None`` = inherit the
    # deployment default. Same round-trip obligation as the ladder above; see
    # ``WizardSubmission.embedding_model`` for why it is per-project.
    embedding_model: str | None = Field(default=None, alias="embeddingModel")
    # Operator guidance appended to the indexer playbook on the next index, and
    # the external MCP servers attached to it. Both carry the SAME round-trip
    # obligation the ladder above spells out — a value this record cannot carry
    # is dropped from every index after the first.
    custom_instructions: str | None = Field(default=None, alias="customInstructions")
    mcp_servers: dict[str, dict] | None = Field(default=None, alias="mcpServers")
    # User-set description override. ``None`` = no override, so finalize's
    # platform-API fetch wins (today's behaviour, unchanged). A non-empty value
    # SURVIVES a reindex — that is the read-preserve contract implemented once in
    # ``plugins.wiki.finalize._resolve_project_desc`` and shared by both indexers.
    desc: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")

    @field_validator("custom_instructions")
    @classmethod
    def _check_instructions(cls, v: str | None) -> str | None:
        """Delegate to the submission's rule — this record is also PATCH-written.

        It is a validation boundary in its own right, not merely a projection of
        a submission that was already checked: ``ProjectSettingsPatch`` writes
        here directly, and the store re-reads here on every refresh.
        """
        return WizardSubmission.check_custom_instructions(v)

    @field_validator("mcp_servers")
    @classmethod
    def _check_servers(cls, v: dict[str, dict] | None) -> dict[str, dict] | None:
        """Delegate to the submission's rule — see :meth:`_check_instructions`."""
        return WizardSubmission.check_mcp_servers(v)

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
            embeddingModel=sub.embedding_model,
            customInstructions=sub.custom_instructions,
            mcpServers=(
                dict(sub.mcp_servers) if sub.mcp_servers is not None else None
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
            embeddingModel=self.embedding_model,
            customInstructions=self.custom_instructions,
            mcpServers=(
                dict(self.mcp_servers) if self.mcp_servers is not None else None
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

# The four lifecycle questions asked of a job's ``status``. They are FOUR
# genuinely different questions, not one set with four names — ``interrupted``
# is why: a restart-stranded job is simultaneously still live, worth re-driving,
# not yet settled, and resumable from its checkpoints. Merging them would be a
# behaviour change, not a cleanup.
#
# Read them through the ``IndexingJob`` predicates below, never by testing
# ``status`` at a consumer: a private literal hardcoded at each read site is
# how four readers come to classify the same status four different ways, with
# no single home to fix.

# Still working — the "Indexing now" question. ``interrupted`` belongs here
# because a restart-stranded job is awaiting recovery, not finished.
_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"queued", "scanning", "finalizing", "interrupted"}
)
# Worth re-driving after a process restart. ``interrupted`` is included on
# purpose: a process that died AFTER marking a job interrupted but BEFORE
# recovery re-drove it must still retry on the next boot.
_RECOVERABLE_STATUSES: frozenset[str] = frozenset(
    {"queued", "scanning", "finalizing", "interrupted"}
)
# Settled on the job's OWN terms — a finished index, or a deliberate user stop
# whose session wrapping up cleanly right after is the CORRECT outcome. This is
# narrower than "will never run again": ``failed`` is deliberately absent, since
# a session that ends clean on a failed job is still a mismatch worth asserting.
_SETTLED_STATUSES: frozenset[str] = frozenset({"complete", "cancelled"})
# Nothing left to resume: the index either finished or was deliberately stopped.
# Everything else — ``failed`` included — is a candidate, and ``ResumePlan``
# decides how much of it can actually be reused.
_NON_RESUMABLE_STATUSES: frozenset[str] = frozenset({"complete", "cancelled"})

# Fine-grained progress phase (defined alongside ``IndexingStatus`` so
# ``IndexingJob`` can reference it).
IndexingPhase = Literal["clone", "scan", "graph", "enrich", "plan", "pages", "finalize"]

# The pipeline order the literal above already implies, made readable so the
# "is this a forward move" question has ONE answer. Index position is the only
# meaning carried here — never persist or wire an ordinal, since inserting a
# phase would renumber every stored value.
PHASE_SEQUENCE: tuple[IndexingPhase, ...] = (
    "clone",
    "scan",
    "graph",
    "enrich",
    "plan",
    "pages",
    "finalize",
)


# ── Index fingerprint — non-content invalidators ────────────────────────────
#
# A hash-identical file set can still require a full rebuild: the embedding
# model, the graph schema, the tree-sitter grammar pack, or the availability
# of the faithful Python symbol resolver can all change between two indexes
# of byte-identical source. ``IndexFingerprint`` records the four inputs that
# can invalidate a "nothing changed" read; ``FingerprintDecision`` is the
# three-state, honest comparison of two fingerprints — mirroring
# ``RepoFreshness.check``'s ``up_to_date``/``behind_by`` idiom
# (``plugins/wiki/freshness.py``): "never compared" must never collapse into
# "compared and matched".


class IndexFingerprint(BaseModel):
    """The non-content inputs that can invalidate a hash-identical index.

    Stamped by ``build_graph_core`` at the moment it actually builds the
    graph — never re-derived later, and never re-probed live at finalize (a
    live probe would describe "now", not "what built the artifacts actually
    in the store" — see the comment at the stamp site for the resume-skip
    case this avoids). Lives on TWO records for the same reason ``commit_sha``
    already does: ``IndexingJob.fingerprint`` is what THIS run used,
    ``Project.fingerprint`` is a copy taken at finalize — the snapshot of
    what produced the index currently in the store.
    """

    model_config = _CFG

    # ``None`` means no embedding was made this run — embeddings disabled
    # (``wiki.embedding.enabled=false``), or the embed pass failed before any
    # vector was actually persisted. Deliberately NOT a config fallback: a
    # fingerprint records what happened, not what would have run. An index
    # with zero vectors is genuinely stale against one that has them, and a
    # captured value must be able to say so rather than paper over it with
    # "what config says right now".
    embedding_model: str | None = Field(default=None, alias="embeddingModel")
    graph_schema_version: str = Field(alias="graphSchemaVersion")
    # ``None`` when the installed ``tree-sitter-language-pack`` distribution
    # metadata isn't readable (the ``treesitter`` extra absent) — an honest
    # absence, matched against itself below, never coerced into a mismatch.
    grammar_pack_version: str | None = Field(default=None, alias="grammarPackVersion")
    resolver_available: bool = Field(alias="resolverAvailable")


# The closed vocabulary of fingerprinted fields — shared by ``FingerprintMismatch.field``
# and ``FingerprintDecision.compute``'s comparison loop, so a field added to
# ``IndexFingerprint`` but never joined here would silently never be compared.
FingerprintField = Literal[
    "embedding_model", "graph_schema_version", "grammar_pack_version", "resolver_available"
]
_FINGERPRINT_FIELDS: tuple[FingerprintField, ...] = (
    "embedding_model",
    "graph_schema_version",
    "grammar_pack_version",
    "resolver_available",
)


class FingerprintMismatch(BaseModel):
    """One field where a prior fingerprint and the current one disagree."""

    model_config = _CFG

    field: FingerprintField
    expected: str | bool | None
    actual: str | bool | None


class FingerprintDecision(BaseModel):
    """The three-state, honest verdict from comparing two fingerprints.

    Mirrors ``RepoFreshness.check``: "no prior fingerprint to compare against"
    (``reason="unknown"``) must never collapse into "compared and it matched"
    (``reason="match"``) — the first means a full rebuild is the safe default,
    the second means reuse is permitted, and rendering them the same would be
    exactly the false-green ``RepoFreshness`` already refuses to produce.

    ``can_reuse`` is DERIVED from ``reason``, never independently settable —
    a plain ``@property``, not a ``computed_field``: this model is frozen (a
    computed verdict, not mutable state), and a derived key inside
    ``model_dump`` would be a second writer of the same fact ``reason``
    already carries.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    reason: Literal["unknown", "match", "mismatch"]
    mismatches: list[FingerprintMismatch] = Field(default_factory=list)

    @property
    def can_reuse(self) -> bool:
        """True only when a prior fingerprint was compared and every field matched."""
        return self.reason == "match"

    @classmethod
    def compute(
        cls, current: IndexFingerprint, prior: IndexFingerprint | None
    ) -> FingerprintDecision:
        """Compare *current* against *prior* — ``prior=None`` reads ``unknown``.

        Accumulates EVERY mismatching field rather than stopping at the
        first, so a caller (a log line, a console readout) can report every
        reason a rebuild is needed in one pass, per :data:`_FINGERPRINT_FIELDS`.
        """
        if prior is None:
            return cls(reason="unknown")
        mismatches = [
            FingerprintMismatch(
                field=f, expected=getattr(prior, f), actual=getattr(current, f)
            )
            for f in _FINGERPRINT_FIELDS
            if getattr(prior, f) != getattr(current, f)
        ]
        if mismatches:
            return cls(reason="mismatch", mismatches=mismatches)
        return cls(reason="match")


# ── Cross-file symbol resolution outcome ────────────────────────────────────


class GraphResolution(BaseModel):
    """What exact cross-file symbol resolution achieved for one indexed commit.

    A code graph built without exact resolution is not visibly broken — it is
    fully populated, passes validation and renders — so "were this graph's
    cross-file edges resolved exactly, or guessed by name?" cannot be answered
    from the graph itself. It is answerable from this record, which the graph
    phase writes onto the project it indexed.

    Lives on TWO records for the same reason ``IndexFingerprint`` does:
    ``IndexingJob.resolution`` is what THIS run achieved, ``Project.resolution``
    is the snapshot describing the graph currently in the store.
    """

    model_config = _CFG

    # False when the resolver could not run at all (its backend is not
    # installed on this deployment), which is a different repair from one that
    # ran and covered only part of the repository — hence a field of its own
    # rather than an inference from a zero count.
    available: bool
    # Package roots the resolver found in the repository, and how many of them
    # it indexed. Fewer indexed than discovered is a PARTIAL pass: the roots it
    # missed keep their name-matched edges, so the two counts together are what
    # separates "exact everywhere" from "exact in places".
    roots_discovered: int = Field(default=0, ge=0, alias="rootsDiscovered")
    roots_indexed: int = Field(default=0, ge=0, alias="rootsIndexed")
    # Cross-file edges contributed by exact resolution. Zero alongside a
    # complete pass means the repository genuinely has no resolvable
    # cross-file references in the resolver's language.
    resolved_edges: int = Field(default=0, ge=0, alias="resolvedEdges")

    # Both questions below are plain properties, NOT ``computed_field``: this
    # model is ``extra="forbid"`` and round-trips through the store, so a
    # derived key inside ``model_dump`` would make every persisted record fail
    # to re-validate — the reasoning ``FingerprintDecision.can_reuse`` already
    # states for the same trade.

    @property
    def faithful(self) -> bool:
        """True when exact resolution ran and covered every root it discovered.

        This is the one condition under which a name-matched cross-file edge
        may be dropped in favour of a resolved one: a partial pass leaves the
        roots it missed with no exact edges at all, so dropping theirs would
        remove the only edges those files have.
        """
        return (
            self.available
            and self.roots_indexed > 0
            and self.roots_indexed >= self.roots_discovered
        )

    @property
    def degraded(self) -> bool:
        """True when the stored graph's cross-file edges are not exact ones.

        The single question a reader asks of this record: a graph whose
        resolution never ran, covered part of the repository, or produced no
        edges answers every "who calls this" by name matching. Reading it off
        the project record is what makes that visible without counting edges by
        hand.
        """
        return not self.faithful or self.resolved_edges == 0

    def describe(self) -> str:
        """One line naming the outcome — for a job log or an operator readout.

        On the model rather than at the call site because every surface that
        reports a pass wants the same sentence, and the counters only mean
        something together.
        """
        if not self.available:
            return "exact symbol resolution unavailable — cross-file edges are name matches"
        coverage = f"{self.roots_indexed}/{self.roots_discovered} project roots indexed"
        if self.roots_indexed == 0:
            return (
                f"exact symbol resolution ran but indexed nothing ({coverage}) — "
                "cross-file edges are name matches"
            )
        if self.faithful:
            return f"exact symbol resolution: {coverage}, {self.resolved_edges} edges"
        return (
            f"exact symbol resolution covered part of the repository ({coverage}, "
            f"{self.resolved_edges} edges) — the roots it missed keep their "
            "name-matched edges"
        )


# ── Refresh scope preview ───────────────────────────────────────────────────


class ScopePreview(BaseModel):
    """Counts describing what one scoped refresh actually touched.

    Produced by ``RefreshReport.scope_preview()`` in
    ``mewbo_graph.wiki.refresh`` and carried on both the job's SSE stream and
    its snapshot. It lives HERE rather than beside its producer because
    ``IndexingJob`` persists it: a model the store round-trips is a wire type,
    and ``refresh.py`` already imports this module, so defining it there and
    importing it back would close a cycle.

    Deliberately FLAT rather than mirroring ``RefreshReport``'s four-stage
    shape. The reader is a progress panel rendering a row of counts, and a
    nested payload would make it walk three levels to reach an integer it
    displays verbatim.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    files_added: int = Field(alias="filesAdded")
    files_modified: int = Field(alias="filesModified")
    files_deleted: int = Field(alias="filesDeleted")
    # Files re-parsed whose entity + edge signatures came back identical — the
    # Salsa early cutoff. High relative to ``files_modified`` means the diff was
    # mostly comments/formatting and cost nothing downstream.
    early_cutoff_files: int = Field(alias="earlyCutoffFiles")
    affected_entities: int = Field(alias="affectedEntities")
    memory_kept: int = Field(alias="memoryKept")
    memory_invalidated: int = Field(alias="memoryInvalidated")
    memory_revalidated: int = Field(alias="memoryRevalidated")
    pages_keep: int = Field(alias="pagesKeep")
    pages_edit: int = Field(alias="pagesEdit")
    pages_regenerate: int = Field(alias="pagesRegenerate")
    new_pages: int = Field(alias="newPages")
    # LLM calls the deterministic pass actually made — the memory reconciler's
    # drift band is the only stage that can reach one. Non-zero here is what
    # makes "Free" an honest word rather than an approximate one.
    llm_calls: int = Field(alias="llmCalls")


# ── Refresh path decision ───────────────────────────────────────────────────

# What a CALLER may ask for. ``auto`` takes the cheap scoped path wherever it
# is safe and silently falls back to a full rebuild otherwise; ``full`` is the
# escape hatch that always rebuilds. There is deliberately no ``scoped`` here:
# a caller cannot demand a scoped refresh of a project whose artifacts nothing
# fingerprinted, so the only honest knob is "try" versus "don't".
RefreshMode = Literal["auto", "full"]

# What was actually CHOSEN. A separate literal from ``RefreshMode`` on purpose
# — collapsing them would let a request value (``auto``) be stored as an
# outcome, and "auto" is not something a job can have run.
RefreshPath = Literal["scoped", "full"]

# Why a full rebuild was chosen. Every member is reachable: ``requested`` from
# the mode knob, the next two from project state, the last two from the
# fingerprint comparison. A catalog project is deliberately ABSENT — the
# refresh route refuses those before a decision is ever computed, so a member
# for it would be a reason nothing can mint.
RefreshFullReason = Literal[
    "requested",
    "graph_only",
    "no_prior_index",
    "fingerprint_unknown",
    "fingerprint_mismatch",
]


class RefreshDecision(BaseModel):
    """Which refresh path a project takes, and — when it is full — why.

    The ONE answer to "scoped or full", computed once per refresh and then
    carried everywhere it is needed: the route's response body, the
    ``IndexingJob`` snapshot (so a console can say why a rebuild was full), and
    the resume branch that must re-drive a stranded scoped job the same way it
    ran the first time. One record rather than a mode flag plus a reason string
    plus a mismatch list, because those three can only ever disagree.

    :meth:`decide` is PURE — it takes the already-probed inputs as arguments and
    touches no store, no clock and no subprocess, so the whole policy is
    testable without a repository on disk. Probing belongs to the callers at the
    edges.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    path: RefreshPath
    # ``None`` IFF ``path == "scoped"`` — enforced below rather than left to
    # each reader, the same "credential is None IFF source is anonymous"
    # discipline ``CredentialCandidate`` already uses: a consumer checks one
    # field, never two that could contradict.
    reason: RefreshFullReason | None = None
    # Populated only for ``fingerprint_mismatch``; every disagreeing field, so a
    # console renders each reason a rebuild was needed in one pass.
    mismatches: list[FingerprintMismatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reason_iff_full(self) -> RefreshDecision:
        """A full path always names its reason; a scoped path never has one."""
        if (self.reason is None) is (self.path == "full"):
            raise ValueError(
                "RefreshDecision.reason must be set for path='full' and absent "
                f"for path='scoped' (got path={self.path!r}, reason={self.reason!r})"
            )
        return self

    @classmethod
    def decide(
        cls,
        *,
        mode: RefreshMode,
        project: Project,
        current: IndexFingerprint,
    ) -> RefreshDecision:
        """Choose the refresh path for *project* under *mode*.

        Ordered cheapest-and-most-decisive first, so the reason a reader is
        shown is the one that would still hold if everything after it were
        fixed. ``current`` is what THIS refresh would build with; it is compared
        against what the stored index was actually built with
        (``Project.fingerprint``) through the same three-state
        :class:`FingerprintDecision` the graph phase already stamps — "never
        compared" stays distinct from "compared and matched", so an index
        nothing fingerprinted rebuilds instead of silently reusing.
        """
        if mode == "full":
            return cls(path="full", reason="requested")
        # A graph-only project builds no manifest, so a content diff against it
        # would read every file as added on every run.
        if project.graph_only:
            return cls(path="full", reason="graph_only")
        # Nothing to compute a delta against, and nothing to attribute it to.
        if not project.commit_sha:
            return cls(path="full", reason="no_prior_index")
        verdict = FingerprintDecision.compute(current, project.fingerprint)
        if verdict.reason == "unknown":
            return cls(path="full", reason="fingerprint_unknown")
        if verdict.reason == "mismatch":
            return cls(
                path="full",
                reason="fingerprint_mismatch",
                mismatches=verdict.mismatches,
            )
        return cls(path="scoped")


# Resolve Project's forward reference now that IndexFingerprint is defined.
Project.model_rebuild()


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
    # Generic per-phase progress — ONE mechanism for every phase beyond
    # scan/pages (today ``graph`` and ``enrich``; a future phase needs no new
    # field). Per-phase field pairs were the alternative and they are how
    # ``scanned_count``/``current_file`` became untrustworthy: N pairs for N
    # phases, each left frozen at its phase's last value and still readable as
    # if it described the current one.
    #
    # THE INVARIANT that makes these trustworthy: ``emit_phase`` clears all
    # three on every transition, so a non-null ``phase_progress_current``
    # always belongs to the phase named in ``phase``. A ``None`` total means "a
    # running count with no knowable total" — a real status line but not a
    # fraction. ``unit`` is the plural noun the reader renders ("files",
    # "nodes", "entities"); absent, a consumer falls back to a generic label.
    # The durable per-step model. It is bounded by DECLARED steps, never by
    # repository units, so every snapshot read stays ``O(1)`` in repository
    # size. It supersedes the legacy triple below, retained only so an in-flight
    # job and older clients keep working during the migration.
    progress: ProgressLedger | None = Field(default=None)
    phase_progress_current: int | None = Field(default=None, alias="phaseProgressCurrent")
    phase_progress_total: int | None = Field(default=None, alias="phaseProgressTotal")
    phase_progress_unit: str | None = Field(default=None, alias="phaseProgressUnit")
    # ISO timestamp of the most recent write by WHATEVER phase is running — the
    # one honest "this job is still moving" signal. ``scanned_count`` and
    # ``current_file`` cannot answer that question: they are the scan phase's
    # private bookkeeping and nothing touches them again until finalize, so they
    # sit frozen — and freshly plausible — through graph, enrich, plan and
    # pages. Read via :meth:`seconds_since_progress`, which is what separates a
    # long phase from a dead one.
    last_progress_at: str | None = Field(default=None, alias="lastProgressAt")
    # Git snapshot resolved at clone time. ``finalize`` reads these off
    # the snapshot when persisting the Project record — no extra args
    # threaded through the tool chain.
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commitSha")
    # forward ref to WikiError — resolved by IndexingJob.model_rebuild() below
    error: WikiError | None = None
    # Non-content invalidators captured when THIS run last built the graph
    # (``build_graph_core``, at the end of the ``graph`` phase) — never
    # re-probed at finalize. ``update_job`` is an unlocked read-modify-write
    # on both backends (a known, separately-tracked defect) and a concurrent
    # writer — a Cancel from the request thread is the documented case — can
    # lose this stamp exactly as it can lose any other field here. That
    # failure mode is SAFE by construction: an absent fingerprint reads as
    # ``FingerprintDecision(reason="unknown")``, which forces a full rebuild
    # rather than silently permitting reuse of artifacts nothing actually
    # fingerprinted. Do not "optimise" a missing value into an assumed match.
    fingerprint: IndexFingerprint | None = None
    # What exact cross-file symbol resolution achieved when THIS run built the
    # graph (``build_graph_core``, at the end of the ``graph`` phase). Written
    # beside the fingerprint, and lost to the same known unlocked
    # read-modify-write in ``update_job`` under a concurrent writer — safe by
    # construction for the same reason: an absent record reads as unknown,
    # never as a pass that resolved everything.
    resolution: GraphResolution | None = None
    # Which refresh path this job took, and why — stamped ONCE at creation by
    # ``WikiIndexingJob.refresh`` and never rewritten. ``None`` means the job is
    # a FIRST index rather than a refresh, which is a third state and not the
    # same as "full": nothing was reused because there was nothing to reuse.
    # Two readers depend on it beyond display — the resume branch, which must
    # re-drive a stranded scoped job the way it ran the first time, and the
    # console, which renders the full-rebuild reason beside the scope preview.
    refresh_decision: RefreshDecision | None = Field(
        default=None, alias="refreshDecision"
    )
    # The committed scope of a scoped refresh, written at the end of its delta
    # pass through the SAME ``emit_*`` seam that writes the event — one write,
    # two transports, so the live stream and the snapshot cannot disagree.
    # ``None`` on every full rebuild: a full index has no delta to preview, and
    # rendering zeros there would claim it examined a scope and found nothing.
    scope_preview: ScopePreview | None = Field(default=None, alias="scopePreview")

    # ── Lifecycle questions ────────────────────────────────────────────
    # Plain properties, NOT ``computed_field``: this model is ``extra="forbid"``
    # and round-trips through the store, so a derived value in ``model_dump``
    # would make every persisted snapshot fail to re-validate. The wire gets
    # the derived flag stamped at its one serialisation seam instead.

    @property
    def is_active(self) -> bool:
        """True while the job is still working — the "Indexing now" question.

        A restart-stranded (``interrupted``) job counts as active: it is
        awaiting recovery, not finished, and hiding it would leave a repository
        looking un-indexed while its job is still queued for a re-drive.
        """
        return self.status in _ACTIVE_STATUSES

    @property
    def is_recoverable(self) -> bool:
        """True when restart recovery should re-drive this job on boot.

        Deliberately excludes ``failed`` even though a failed job may still hold
        reusable checkpoints: an automatic re-drive of a job that already
        exhausted its retry budget is how a dying index loops the API. A human
        can still resume it — see :attr:`is_resumable`.
        """
        return self.status in _RECOVERABLE_STATUSES

    @property
    def is_terminal(self) -> bool:
        """True when the job settled on its own terms (finished, or stopped).

        The question a session-end reconciler asks: *may I leave this alone?*
        ``failed`` answers False on purpose — a session ending cleanly on a
        failed job is a mismatch between what the run believed and what it
        built, and that mismatch is worth reporting.
        """
        return self.status in _SETTLED_STATUSES

    @property
    def is_resumable(self) -> bool:
        """True when a checkpoint resume is worth attempting.

        Broader than :attr:`is_recoverable`: a ``failed`` job is not re-driven
        automatically but a user may still ask to resume it, and ``ResumePlan``
        decides what of it can actually be reused.
        """
        return self.status not in _NON_RESUMABLE_STATUSES

    def regresses_to(self, phase: str) -> bool:
        """True when *phase* sits BEFORE the one this job already reached.

        A resume is told to re-clone and re-scan so the source is back on disk
        before pages are written, so those tools legitimately run again and
        re-stamp phases the job passed long ago. Both progress surfaces read
        ``phase`` off this snapshot, so without this question being asked the
        bar walks backwards mid-resume — a job that had already built its graph
        reported ``scan`` again, with a ``phase_started_at`` later than the
        graph build's.

        Unknown phase names are never a regression: an unrecognised value is a
        vocabulary the caller knows about and this model does not, and silently
        swallowing its transition would hide real progress.
        """
        if self.phase is None or phase == self.phase:
            return False
        try:
            return PHASE_SEQUENCE.index(phase) < PHASE_SEQUENCE.index(self.phase)
        except ValueError:
            return False

    @staticmethod
    def format_stamp(now: datetime) -> str:
        """Render *now* in the one spelling this model's timestamp fields use.

        The write half of :meth:`seconds_since_progress`. Both live here so the
        format is stated once: a writer that spells it differently produces a
        stamp its own reader cannot parse, and the reader's failure mode is a
        silent ``None`` rather than an exception.
        """
        return now.strftime(_TS_FORMAT)

    def seconds_since_progress(self, now: datetime) -> float | None:
        """Seconds between *now* and the last progress write, or ``None``.

        ``None`` means "no usable baseline" — either nothing has reported
        progress yet or the stored stamp is unreadable. Both answers say the
        same thing to a caller (there is nothing to compare against), and
        neither may be reported as "0 seconds ago", which would read as a job
        that just moved.

        The clock arrives as an ARGUMENT: this model is persisted and wired, and
        a model that reads a clock cannot be tested without patching one.
        """
        if not self.last_progress_at:
            return None
        try:
            stamp = datetime.strptime(self.last_progress_at, _TS_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        return (now - stamp).total_seconds()


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
# A scanned-file counter alone reaches 96% the moment the indexer hits the
# (much longer) page-generation phase, then stalls there with no visibility
# into what is happening. The events below are the server-side state machine
# the FE renders honestly:
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
    # The current/latest turn's question text. Empty when unrecorded.
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
    # Which Q&A agent shape ran: ``deep`` is the hypervisor that fans out
    # retrieval probes; ``fast`` is a single root holding the retrieval surface
    # itself. Fixed for the life of an answer — a follow-up turn keeps the mode
    # its session started in, which is why ``QaTurn`` carries no copy. An
    # answer with no stored mode ran the probe fan-out, hence the ``deep``
    # default; the NEW-request default is a separate decision made at the wire
    # boundary.
    mode: Literal["fast", "deep"] = Field(default="deep")
    # Project slug that owns this answer. Persisted so ``resolve_qa_ctx``
    # can recover it after a process restart or any read-back path. NOT
    # ``exclude=True``: that would keep it off the wire but also out of the
    # store, leaving ``slug=""`` on every ctx lookup and breaking
    # ``wiki_search_pages`` (empty BM25 corpus). The FE TS type silently
    # ignores the extra field.
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


@dataclass(frozen=True, slots=True)
class CommitScope:
    """Which generation of a slug's persisted graph a read covers.

    The store holds the UNION of every commit ever indexed for a slug, so a
    reader has to say which generation it means. There are exactly two
    answers, they are not orderable, and one of them is not expressible as a
    commit sha — hence a type rather than another optional string:

    - ``CommitScope.at(sha)`` — rows stamped exactly *sha*. ``at(None)``
      matches rows stamped NULL (a commit-less catalog node, or any unstamped
      node), which is a real generation, not "no filter".
    - ``CommitScope.every()`` — every generation ever indexed.

    **Why this is not a ``commit_sha: str | None`` parameter.**
    ``count_graph_nodes`` and ``supersede_graph_artifacts`` already take that
    parameter, and ``None`` there means "stamped NULL" — an exact match, which
    is precisely how they count and preserve the pre-isolation generation.
    Adding ``commit_sha: str | None = None`` to ``query_graph`` with "unscoped"
    semantics would give one parameter name OPPOSITE meanings on two methods of
    the same class, so a reader who learned it on one would be wrong on the
    other with nothing to warn them.

    Both projections live here rather than in the drivers because the two
    drivers filter through different mechanisms — the JSON driver tests a
    loaded row, Mongo narrows a query document — and a rule typed twice is a
    rule that drifts. ``filter_fields`` returns plain field equality, not a
    Mongo operator, so it stays storage-agnostic.
    """

    sha: str | None = None
    scoped: bool = False

    @classmethod
    def at(cls, sha: str | None) -> CommitScope:
        """Scope to rows stamped exactly *sha* (``None`` matches NULL-stamped)."""
        return cls(sha=sha, scoped=True)

    @classmethod
    def every(cls) -> CommitScope:
        """Every generation ever indexed for the slug (the union)."""
        return cls(sha=None, scoped=False)

    def matches(self, row_commit: str | None) -> bool:
        """Does a row stamped *row_commit* fall in this scope?"""
        return row_commit == self.sha if self.scoped else True

    def filter_fields(self) -> dict[str, str | None]:
        """Field-equality predicate for this scope; empty dict when unscoped."""
        return {"commit_sha": self.sha} if self.scoped else {}

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
    :data:`GraphNode` union dispatches on it. ``subkind``/``attributes``
    default, so a persisted node lacking them still validates.
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
    # change seam for language-specific facts. Defaults empty so a persisted
    # node lacking it still validates.
    attributes: dict[str, _JsonScalar] = Field(default_factory=dict)
    # Per-job/commit attribution. ``commit_sha`` is the git commit whose index
    # produced this node; ``job_id`` the job that wrote it. The store stamps both
    # at write from the owning job ctx. ``commit_sha`` is what makes "the graph
    # for THIS commit is built" expressible (the resume skip predicate keys on
    # it) and what a completed re-index supersedes on: every prior-commit node is
    # reaped, so the store stops being the UNION of every commit ever indexed for
    # a slug. ``None`` on a commit-less catalog node and on any unstamped node
    # (the backfill stamps those); the default keeps those valid. NOT part of
    # the wire shape — the graph
    # view assembles its Cytoscape payload field-by-field and never dumps a node.
    commit_sha: str | None = None
    job_id: str | None = None

    @field_validator("attributes")
    @classmethod
    def _ns_attributes(cls, v: dict[str, _JsonScalar]) -> dict[str, _JsonScalar]:
        """Enforce namespaced attribute keys (see :func:`_require_namespaced`)."""
        return _require_namespaced(v)

    @property
    def embedding_text(self) -> str:
        """The text an embedder vectorises this node as.

        Lives ON the node because it is a projection of the node's own fields,
        and because two paths embed the same graph — the full index and the
        scoped incremental refresh. Held as a helper beside one of them, the
        other silently embeds a DIFFERENT string, and the same symbol lands in a
        different vector neighbourhood depending on which path last touched its
        file. The ``file`` segment is dropped when it merely repeats ``name``
        (a File node names itself) so it never counts twice.
        """
        parts = [self.name]
        if self.docstring:
            parts.append(self.docstring)
        if self.file and self.file != self.name:
            parts.append(self.file)
        return " — ".join(parts)


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
    # Defaults keep a persisted edge lacking them valid.
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
