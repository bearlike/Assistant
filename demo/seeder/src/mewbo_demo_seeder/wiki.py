#!/usr/bin/env python3
"""Wiki seed-bundle models + the ``WikiSeeder`` atomic class (demo-as-code).

A :class:`WikiSeedBundle` is the deterministic description of an Agentic-Wiki
world: a full gallery of finished, real-looking projects (Grove + siblings) with
curated Markdown pages, a dense synthetic code graph for the richest project, a
wall of resumable/in-flight indexing jobs (the "Incomplete indexes" landing +
the indexing-progress card), and completed Q&A answers. It is authored as OFFSETS
from a frozen ``T0`` for every wall-clock instant it carries (``indexed_at``, a
job's ``phase_started_at``) so re-seeding is byte-identical — an absolute time
baked into the fixture would drift every rendered "indexed 2 days ago" label on
each run, the exact staleness hazard the whole demo stack exists to avoid.

Mirrors ``mewbo_demo_seeder.seeder``/``models`` exactly: the bundle JSON is a
trust boundary, so every model is ``extra="forbid"`` and validates AT DEFINITION
(``field_validator``/``model_validator``); behaviour that is intrinsic to the
data (project→``Project``, page→``WikiPage``, a page-indexed synthetic graph)
lives ON the models / the seeder, never as a service-side ``if kind ==`` switch.
Models never import I/O: the rebased wall-clock instant arrives as a method
argument (the seeder holds the clock), and every Mongo write happens at the
seeder's edge THROUGH the real ``WikiStoreBase`` store contracts (``create_project``
/ ``save_page`` / ``upsert_nodes`` / ``upsert_edges`` / ``create_job`` /
``append_job_event`` / ``save_qa``) — no live indexer, no LLM, no network. Every
write is a keyed upsert EXCEPT the one job's append-only timeline log, which is
made byte-identical on re-seed by a create-if-absent guard (see
:meth:`WikiSeeder._seed_job`), so a re-run converges to an identical Mongo state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import (
    BlockUnion,
    ClassNode,
    FileNode,
    Frontmatter,
    FunctionNode,
    GraphEdge,
    GraphNode,
    IndexingJob,
    IndexingPhase,
    IndexingStatus,
    MethodNode,
    PlatformId,
    Project,
    QaAnswer,
    SourceRef,
    WikiPage,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Synthetic Code-Galaxy generation catalogue (module CONSTANTS — data, not logic)
# ---------------------------------------------------------------------------
#
# A fixed, Grove-shaped file layout + symbol-name stems the graph generator
# cycles by INDEX (never random) so the 3D "Code Galaxy" renders populated with
# a run-independent node/edge set. Multiple directories give the folder-collapse
# view (``?hierarchy=1`` → ``FolderTree``) real clusters to fold.

_GRAPH_FILES: tuple[str, ...] = (
    "grove/__init__.py",
    "grove/session.py",
    "grove/worktree.py",
    "grove/store.py",
    "grove/registry.py",
    "grove/cli.py",
    "grove/config.py",
    "grove/tmux.py",
    "grove/git.py",
    "grove/daemon.py",
    "grove/models.py",
    "grove/hooks.py",
    "grove/providers/base.py",
    "grove/providers/tmux_provider.py",
    "grove/providers/worktree_provider.py",
    "grove/api/routes.py",
    "grove/api/schemas.py",
    "grove/util/paths.py",
    "grove/util/naming.py",
    "grove/util/proc.py",
)

_CLASS_STEMS: tuple[str, ...] = (
    "Session",
    "Worktree",
    "Workspace",
    "Registry",
    "Store",
    "Config",
    "TmuxAdapter",
    "GitRunner",
    "Daemon",
    "Provider",
    "Router",
    "Hook",
    "Manifest",
    "Snapshot",
)

_FUNC_STEMS: tuple[str, ...] = (
    "create",
    "resume",
    "pause",
    "kill",
    "list",
    "peek",
    "attach",
    "detach",
    "sync",
    "prune",
    "render",
    "resolve",
    "load",
    "save",
    "validate",
    "spawn",
)


# ---------------------------------------------------------------------------
# Bundle models
# ---------------------------------------------------------------------------


class WikiSeedPage(BaseModel):
    """One curated wiki page: identity, title, and a Markdown body.

    ``body`` is GFM Markdown rendered by the console (remark-gfm +
    rehype-highlight; ```mermaid`` fences render as diagrams). ``sources`` are
    the relevant source paths surfaced in the page's frontmatter; ``toc``/``nav``
    are intentionally absent — the API re-derives them server-side, so the
    persisted page keeps them empty (mirroring the real indexer).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    body: str
    sources: list[str] = Field(default_factory=list)

    def to_page(self) -> WikiPage:
        """Materialise the store-level :class:`WikiPage` for this page.

        ``frontmatter.slug`` is the PAGE id (matching the real indexer's
        ``submit_page`` / ``CatalogIngestor`` — a page's slug is its own id, not
        the project slug), so a seeded page round-trips exactly like an authored one.
        """
        relevant = [SourceRef(path=path) for path in self.sources] or None
        frontmatter = Frontmatter(
            title=self.title, slug=self.id, relevant_sources=relevant
        )
        return WikiPage(
            id=self.id,
            title=self.title,
            frontmatter=frontmatter,
            body=self.body,
            toc=[],
            nav=[],
        )


class WikiSeedProject(BaseModel):
    """A finished wiki project: landing-card metadata + its pages + graph size.

    ``pages`` on the wire :class:`Project` is derived (never authored) from the
    real number of authored pages, so the card's page count can never lie.
    ``indexed_offset_seconds`` is the project's ``indexed_at`` relative to ``T0``
    (negative = in the past); the seeder rebases it so "indexed N ago" is stable.
    ``graph_nodes`` requests a deterministic synthetic Code-Galaxy of that size
    (0 = none). ``graph_only`` is deliberately never exposed: a demo project is
    always documented (the page-content read seam must not raise).
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    source: PlatformId
    lang: str
    desc: str
    landing_page_id: str
    host: str
    commit_sha: str
    commit_short: str
    branch: str = "main"
    repo_url: str | None = None
    primary: bool = False
    indexed_offset_seconds: int = Field(
        description="Project indexed_at relative to T0, in seconds (negative = past)."
    )
    graph_nodes: int = Field(
        default=0, ge=0, description="Deterministic synthetic Code-Galaxy node count."
    )
    page_count: int | None = Field(
        default=None,
        description=(
            "Landing-card page count. Defaults to the number of AUTHORED pages; an "
            "explicit value (>= authored) lets a heavily-indexed gallery project "
            "advertise its true size while the demo only authors the few pages a "
            "screenshot opens."
        ),
    )
    pages: list[WikiSeedPage] = Field(default_factory=list)

    @model_validator(mode="after")
    def _landing_resolves(self) -> WikiSeedProject:
        """The landing page id MUST equal a real authored page id.

        The FE reads ``landing_page_id`` to open the project; a dangling id lands
        the gallery card on a 404. Enforced here at bundle-load, not seed time.
        Also bounds ``page_count`` from below by the authored count so the card can
        never claim FEWER pages than the project actually serves.
        """
        ids = {page.id for page in self.pages}
        if len(ids) != len(self.pages):
            raise ValueError(f"project {self.slug!r} has duplicate page ids")
        if self.pages and self.landing_page_id not in ids:
            raise ValueError(
                f"project {self.slug!r} landing_page_id {self.landing_page_id!r} "
                "is not one of its page ids"
            )
        if self.page_count is not None and self.page_count < len(self.pages):
            raise ValueError(
                f"project {self.slug!r} page_count {self.page_count} is below its "
                f"authored page count {len(self.pages)}"
            )
        return self

    def to_project(self, t0: datetime) -> Project:
        """Build the store-level :class:`Project` against a rebased clock."""
        indexed_at = (t0 + timedelta(seconds=self.indexed_offset_seconds)).isoformat()
        pages = self.page_count if self.page_count is not None else len(self.pages)
        return Project(
            slug=self.slug,
            source=self.source,
            lang=self.lang,
            indexed_at=indexed_at,
            pages=pages,
            primary=self.primary or None,
            desc=self.desc,
            landing_page_id=self.landing_page_id,
            repo_url=self.repo_url,
            host=self.host,
            branch=self.branch,
            commit_sha=self.commit_sha,
            commit_short=self.commit_short,
            graph_only=False,
        )


class WikiSeedJob(BaseModel):
    """The mid-index job behind the indexing-progress card (screenshot #09).

    Frozen at an in-flight "generating pages" state: ``status`` is the coarse
    lifecycle bucket, ``phase`` the fine-grained progress state the honest
    progress bar reads. ``phase_started_offset_seconds`` rebases ``phase_started_at``
    off ``T0`` so the FE's in-phase ETA extrapolation is run-independent.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    slug: str
    status: IndexingStatus = "scanning"
    phase: IndexingPhase = "pages"
    scanned_count: int
    total_count: int
    total_pages: int | None = None
    pages_submitted: int = 0
    current_file: str | None = None
    model: str
    host: str | None = None
    platform: PlatformId | None = None
    branch: str | None = None
    commit_sha: str | None = None
    landing_page_id: str | None = None
    phase_started_offset_seconds: int = Field(
        default=0, description="phase_started_at relative to T0, in seconds."
    )
    log_lines: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form indexer milestone lines for the progress timeline. Each is "
            "appended as a ``{type: log, level: info, text}`` job event (the shape "
            "the console's log reducer keys on). Empty for a doc-only resumable job."
        ),
    )
    resume_graph_nodes: int = Field(
        default=0,
        ge=0,
        description=(
            "Synthetic graph nodes to seed on THIS job's slug so it reads as "
            "recoverable. `/jobs/recoverable` keeps a failed/interrupted/cancelled "
            "job only when `ResumePlan.build` is non-no-op, and a non-empty graph is "
            "the cheapest artifact that flips it (adds `graph` to the reuse set). "
            "Pair with a `failed`/`cancelled` status: `interrupted` is ALSO an active "
            "status, which excludes the job from the landing 'Incomplete indexes' band."
        ),
    )

    @model_validator(mode="after")
    def _recoverable_is_not_active(self) -> WikiSeedJob:
        """A job that seeds a resume graph must not use an ACTIVE status.

        `interrupted`/`queued`/`scanning`/`finalizing` are active — the landing
        band subtracts active slugs, so a resumable job in one of those states
        would never appear there. Guard it at bundle-load rather than silently
        seeding a job that can't render where it's meant to.
        """
        if self.resume_graph_nodes > 0 and self.status not in {
            "failed",
            "cancelled",
        }:
            raise ValueError(
                f"job {self.job_id!r} seeds a resume graph but status "
                f"{self.status!r} is active — use 'failed' or 'cancelled' so it "
                "lands in the 'Incomplete indexes' band"
            )
        return self

    def to_job(self, t0: datetime) -> IndexingJob:
        """Build the store-level :class:`IndexingJob` against a rebased clock."""
        phase_started_at = (
            t0 + timedelta(seconds=self.phase_started_offset_seconds)
        ).isoformat()
        return IndexingJob(
            job_id=self.job_id,
            slug=self.slug,
            status=self.status,
            scanned_count=self.scanned_count,
            total_count=self.total_count,
            current_file=self.current_file,
            landing_page_id=self.landing_page_id,
            platform=self.platform,
            host=self.host,
            model=self.model,
            phase=self.phase,
            total_pages=self.total_pages,
            pages_submitted=self.pages_submitted,
            phase_started_at=phase_started_at,
            branch=self.branch,
            commit_sha=self.commit_sha,
        )


class WikiSeedQa(BaseModel):
    """A completed Q&A answer (screenshot #08).

    ``blocks`` are authored as the wire ``BlockUnion`` discriminated union so a
    malformed block fails at bundle-load, not seed time. ``status`` is forced to
    ``complete`` at materialisation — a seeded answer is, by construction, done.
    """

    model_config = ConfigDict(extra="forbid")

    answer_id: str
    slug: str
    from_page_id: str
    question: str
    model: str
    summary_sources: list[str] = Field(default_factory=list)
    accessed_sources: list[str] = Field(default_factory=list)
    models_used: list[str] = Field(default_factory=list)
    blocks: list[BlockUnion]

    def to_answer(self) -> QaAnswer:
        """Build the store-level :class:`QaAnswer` (terminal ``complete`` state)."""
        return QaAnswer(
            answer_id=self.answer_id,
            from_page_id=self.from_page_id,
            question=self.question,
            summary_sources=self.summary_sources,
            model=self.model,
            blocks=self.blocks,
            accessed_sources=self.accessed_sources,
            models_used=self.models_used,
            status="complete",
            slug=self.slug,
        )


class WikiSeedBundle(BaseModel):
    """The whole deterministic wiki world: projects + resumable/in-flight jobs + Q&A."""

    model_config = ConfigDict(extra="forbid")

    projects: list[WikiSeedProject]
    jobs: list[WikiSeedJob] = Field(default_factory=list)
    qa: list[WikiSeedQa] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_refs(self) -> WikiSeedBundle:
        """Unique project slugs + job ids + answer ids; every Q&A targets a real page."""
        if not self.projects:
            raise ValueError("a wiki bundle must define at least one project")
        slugs = [project.slug for project in self.projects]
        if len(set(slugs)) != len(slugs):
            raise ValueError("duplicate project slug in bundle")
        job_ids = [job.job_id for job in self.jobs]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("duplicate job id in bundle")
        answer_ids = [answer.answer_id for answer in self.qa]
        if len(set(answer_ids)) != len(answer_ids):
            raise ValueError("duplicate qa answer id in bundle")
        by_slug = {project.slug: project for project in self.projects}
        for answer in self.qa:
            owner = by_slug.get(answer.slug)
            if owner is None:
                raise ValueError(
                    f"qa answer references unknown project slug {answer.slug!r}"
                )
            if owner.pages and answer.from_page_id not in {p.id for p in owner.pages}:
                raise ValueError(
                    f"qa answer from_page_id {answer.from_page_id!r} is not a page "
                    f"of project {answer.slug!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class WikiSeedReport:
    """What a :meth:`WikiSeeder.seed` run wrote — a plain in-process value.

    A dataclass (not Pydantic): it crosses no trust boundary, exactly like
    ``mewbo_demo_seeder.seeder.SeedReport``.
    """

    projects: list[str] = field(default_factory=list)
    pages: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    jobs: list[str] = field(default_factory=list)
    qa: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


class WikiSeeder:
    """Idempotently materialise a :class:`WikiSeedBundle` into a wiki store.

    Collaborators are injected as FIELDS — the store, the parsed bundle, and the
    frozen ``t0`` — so a test drives the real ``seed()`` against a mongomock-backed
    :class:`~mewbo_graph.wiki.store.MongoWikiStore` with a fixed clock and never
    patches time. ``seed`` rebases every offset against ``t0`` and writes THROUGH
    the store's upsert contracts, so re-running it converges to an identical Mongo
    state (every collection touched is keyed + upserted; no append-only log is
    written, which is the one shape a re-seed could not make byte-identical).
    """

    def __init__(
        self,
        *,
        wiki_store: WikiStoreBase,
        bundle: WikiSeedBundle,
        t0: datetime,
    ) -> None:
        """Store the injected collaborators and the frozen ``t0`` clock."""
        self._store = wiki_store
        self._bundle = bundle
        self._t0 = t0

    def seed(self) -> WikiSeedReport:
        """Write every project (+ pages + graph), every job, and every Q&A answer."""
        report = WikiSeedReport()
        for project in self._bundle.projects:
            pages, nodes, edges = self._seed_project(project)
            report.projects.append(project.slug)
            report.pages += pages
            report.graph_nodes += nodes
            report.graph_edges += edges
        for job in self._bundle.jobs:
            job_id, nodes, edges = self._seed_job(job)
            report.jobs.append(job_id)
            report.graph_nodes += nodes
            report.graph_edges += edges
        for answer in self._bundle.qa:
            self._store.save_qa(answer.to_answer())
            report.qa.append(answer.answer_id)
        return report

    def _seed_project(self, project: WikiSeedProject) -> tuple[int, int, int]:
        """Materialise one project's record, pages, and synthetic code graph."""
        self._store.create_project(project.to_project(self._t0))
        for page in project.pages:
            self._store.save_page(project.slug, page.to_page())
        nodes, edges = self._synthesize_graph(project.slug, project.graph_nodes)
        if nodes:
            # Stamped with the project's own commit so a commit-scoped count
            # (``count_graph_nodes(slug, commit_sha=...)``) finds this graph too.
            self._store.upsert_nodes(
                project.slug, nodes, commit_sha=project.commit_sha
            )
            self._store.upsert_edges(
                project.slug, edges, commit_sha=project.commit_sha
            )
        return len(project.pages), len(nodes), len(edges)

    def _synthesize_graph(
        self, slug: str, n: int
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Deterministically synthesise ``n`` graph nodes + CONTAINS/CALLS edges.

        Pure and index-based (NO random, NO clock): a File node per catalogue
        file, then Class/Function/Method symbols distributed round-robin with
        ``CONTAINS`` edges (File→symbol, Class→Method) and a deterministic
        ``CALLS`` fan across the call-graph so the Code Galaxy looks connected.
        Node ids embed a monotonically-growing byte offset, so every id is unique
        and stable across runs; edge endpoint kinds obey the CPG source-kind rules
        (``File``/``Class`` may CONTAIN, ``Function``/``Method`` may CALL). Used
        for BOTH a project's full Code Galaxy AND the handful of nodes a resumable
        job's slug needs so ``ResumePlan.build`` sees reusable graph artifacts (a
        non-empty graph is what flips ``is_noop()`` to False → the job shows in the
        landing "Incomplete indexes" band).
        """
        if n <= 0:
            return [], []
        file_count = min(len(_GRAPH_FILES), max(6, n // 12))
        files = list(_GRAPH_FILES[:file_count])

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        file_ids: list[str] = []
        for path in files:
            fid = f"{path}#0"
            nodes.append(
                FileNode(
                    slug=slug,
                    node_id=fid,
                    name=path.rsplit("/", 1)[-1],
                    file=path,
                    range=(0, 1),
                )
            )
            file_ids.append(fid)

        idx = 0
        while len(nodes) < n:
            pos = idx % len(files)
            path = files[pos]
            fid = file_ids[pos]
            start = 10 + idx * 30
            if idx % 4 == 0:
                cname = f"{_CLASS_STEMS[idx % len(_CLASS_STEMS)]}{idx}"
                cid = f"{path}#{start}"
                nodes.append(
                    ClassNode(
                        slug=slug, node_id=cid, name=cname, file=path,
                        range=(start, start + 24),
                    )
                )
                edges.append(
                    GraphEdge(slug=slug, source=fid, target=cid, type="CONTAINS")
                )
                for m in range(3):
                    if len(nodes) >= n:
                        break
                    mstart = start + 4 + m * 6
                    mname = _FUNC_STEMS[(idx + m) % len(_FUNC_STEMS)]
                    mid = f"{path}#{mstart}"
                    nodes.append(
                        MethodNode(
                            slug=slug, node_id=mid, name=mname, file=path,
                            range=(mstart, mstart + 4),
                        )
                    )
                    edges.append(
                        GraphEdge(slug=slug, source=cid, target=mid, type="CONTAINS")
                    )
            else:
                gname = f"{_FUNC_STEMS[idx % len(_FUNC_STEMS)]}_{idx}"
                gid = f"{path}#{start}"
                nodes.append(
                    FunctionNode(
                        slug=slug, node_id=gid, name=gname, file=path,
                        range=(start, start + 8),
                    )
                )
                edges.append(
                    GraphEdge(slug=slug, source=fid, target=gid, type="CONTAINS")
                )
            idx += 1

        callables = [node for node in nodes if node.type in ("Function", "Method")]
        if len(callables) > 1:
            for j, caller in enumerate(callables):
                callee = callables[(j * 7 + 3) % len(callables)]
                if callee.node_id != caller.node_id:
                    edges.append(
                        GraphEdge(
                            slug=slug,
                            source=caller.node_id,
                            target=callee.node_id,
                            type="CALLS",
                        )
                    )
        return nodes, edges

    def _seed_job(self, job: WikiSeedJob) -> tuple[str, int, int]:
        """Create-if-absent a job + its log events + resume graph; return (id, nodes, edges).

        Guarded on :meth:`get_job` because the timeline events are written through
        the append-only ``append_job_event`` log — the ONE store family a blind
        re-run could not make byte-identical (each append re-collides on the
        ``(job_id, idx)`` unique key). A job already present is left exactly as the
        first seed wrote it, so a re-seed converges bit-for-bit. A resumable job
        also gets a handful of graph nodes on its OWN slug (no Project needed) so
        ``ResumePlan.build`` reads a non-no-op plan and the job surfaces in the
        landing "Incomplete indexes" band.
        """
        if self._store.get_job(job.job_id) is not None:
            return job.job_id, 0, 0
        self._store.create_job(job.to_job(self._t0))
        for line in job.log_lines:
            self._store.append_job_event(
                job.job_id, {"type": "log", "level": "info", "text": line}
            )
        nodes, edges = self._synthesize_graph(job.slug, job.resume_graph_nodes)
        if nodes:
            # Stamped with the job's own commit_sha/job_id: ResumePlan.build reads
            # a commit-scoped count (``count_graph_nodes(slug, commit_sha=job.commit_sha)``),
            # so an unstamped (None-commit) graph would never match the job's commit
            # and the job would look like it has nothing reusable.
            self._store.upsert_nodes(
                job.slug, nodes, commit_sha=job.commit_sha, job_id=job.job_id
            )
            self._store.upsert_edges(
                job.slug, edges, commit_sha=job.commit_sha, job_id=job.job_id
            )
        return job.job_id, len(nodes), len(edges)


__all__ = [
    "WikiSeedPage",
    "WikiSeedProject",
    "WikiSeedJob",
    "WikiSeedQa",
    "WikiSeedBundle",
    "WikiSeedReport",
    "WikiSeeder",
]
