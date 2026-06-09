"""``ResumePlan`` — single source of truth for "what an interrupted index did".

The atomic record of what an interrupted index already finished (Gitea #54, B).

Checkpoint-aware recovery does NOT re-run the whole pipeline. ``clone`` + ``scan``
are cheap (seconds) and the cloned source is needed to write the remaining pages,
so they always run. The expensive, idempotent phases are SKIPPED when their store
artifacts already exist:

- ``graph``  — skip when the persisted code graph is non-empty.
- ``enrich`` — skip when abstract entities exist for the slug.
- ``plan``   — skip when the job has a committed page plan.
- ``pages``  — write only the plan pages NOT already in the store.

``ResumePlan`` is computed ONCE (``build``) at resume time and persisted as a tiny
dict on the job's resume sidecar (``store.save_resume_plan``). Each wiki phase tool
rebuilds it cheaply per call from that dict (``from_persisted``) so the skip guards
never re-query the graph — the build cost is paid once, not once-per-tool-call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.types import IndexingJob

# The expensive idempotent phases ``ResumePlan`` may skip. ``clone``/``scan`` are
# always re-run; ``pages`` is handled page-by-page (not a whole-phase skip);
# ``finalize`` is always re-run (idempotent upsert).
SKIPPABLE_PHASES: frozenset[str] = frozenset({"graph", "enrich", "plan"})


@dataclass(frozen=True)
class ResumePlan:
    """Immutable record of which phases an interrupted index already completed.

    ``skip`` is a subset of :data:`SKIPPABLE_PHASES`. ``pages_done`` is the set of
    plan page ids already persisted; ``pages_remaining`` is the ordered plan ids
    NOT yet written. ``node_count`` / ``entity_count`` / ``total_pages`` are carried
    only for the agent-facing :meth:`summary`.
    """

    skip: frozenset[str] = field(default_factory=frozenset)
    pages_done: frozenset[str] = field(default_factory=frozenset)
    pages_remaining: tuple[str, ...] = ()
    node_count: int = 0
    entity_count: int = 0
    total_pages: int = 0

    # -- construction --------------------------------------------------------

    @classmethod
    def build(cls, store: WikiStoreBase, job: IndexingJob) -> ResumePlan:
        """Inspect the store + *job* and compute what is already done.

        Cheap by design: a graph node count, an entity count, the committed plan
        ids, and the persisted page ids — never the full node/entity payloads. Run
        ONCE at resume time; persist via :meth:`to_persisted` so per-tool-call
        rebuilds use :meth:`from_persisted` instead of re-querying the graph.
        """
        slug = job.slug

        node_count = cls._count_graph(store, slug)
        entity_count = cls._count_entities(store, slug)

        skip: set[str] = set()
        if node_count > 0:
            skip.add("graph")
        if entity_count > 0:
            skip.add("enrich")

        plan_ids = cls._plan_page_ids(store, job.job_id)
        if plan_ids:
            skip.add("plan")

        done = cls._persisted_page_ids(store, slug)
        pages_done = frozenset(pid for pid in plan_ids if pid in done)
        pages_remaining = tuple(pid for pid in plan_ids if pid not in done)

        return cls(
            skip=frozenset(skip),
            pages_done=pages_done,
            pages_remaining=pages_remaining,
            node_count=node_count,
            entity_count=entity_count,
            total_pages=len(plan_ids),
        )

    @classmethod
    def from_persisted(cls, data: dict[str, Any] | None) -> ResumePlan | None:
        """Rebuild a :class:`ResumePlan` from its persisted dict (cheap, no I/O).

        Returns ``None`` for ``None``/empty input so a non-resume run carries no
        plan and every guard short-circuits to "not skipped".
        """
        if not data:
            return None
        skip = frozenset(str(p) for p in data.get("skip", []))
        pages_done = frozenset(str(p) for p in data.get("pages_done", []))
        pages_remaining = tuple(str(p) for p in data.get("pages_remaining", ()))
        return cls(
            skip=skip,
            pages_done=pages_done,
            pages_remaining=pages_remaining,
            node_count=int(data.get("node_count", 0)),
            entity_count=int(data.get("entity_count", 0)),
            total_pages=int(data.get("total_pages", 0)),
        )

    def to_persisted(self) -> dict[str, Any]:
        """Serialise to the tiny dict the resume sidecar stores (JSON-safe)."""
        return {
            "skip": sorted(self.skip),
            "pages_done": sorted(self.pages_done),
            "pages_remaining": list(self.pages_remaining),
            "node_count": self.node_count,
            "entity_count": self.entity_count,
            "total_pages": self.total_pages,
        }

    # -- queries -------------------------------------------------------------

    def should_skip(self, phase: str) -> bool:
        """True when *phase* is an already-completed expensive phase to skip."""
        return phase in self.skip

    def is_noop(self) -> bool:
        """True when nothing is reusable (empty graph) — resume == a full rebuild.

        An empty graph forces a full re-index; the only saved work is the
        re-clone/re-scan being deterministic. Callers may use this to decide
        whether to even persist the plan.
        """
        return not self.skip and not self.pages_done

    def summary(self) -> str:
        """Agent-facing instruction describing what to reuse vs. (re)build."""
        parts: list[str] = ["RESUME — reuse completed work, do NOT rebuild it."]
        if "graph" in self.skip:
            parts.append(f"graph already built ({self.node_count} nodes) — SKIP wiki_build_graph.")
        else:
            parts.append("graph is empty — rebuild it (wiki_build_graph).")
        if "enrich" in self.skip:
            parts.append(
                f"entities already minted ({self.entity_count}) — SKIP the enrich fan-out."
            )
        if "plan" in self.skip:
            parts.append(
                f"plan already committed ({self.total_pages} pages) — SKIP wiki_commit_plan."
            )
        else:
            parts.append("no plan yet — commit one (wiki_commit_plan).")
        if self.pages_done:
            done = ", ".join(sorted(self.pages_done))
            parts.append(f"pages already written: [{done}] — do NOT re-write them.")
        if self.pages_remaining:
            remaining = ", ".join(self.pages_remaining)
            parts.append(f"pages still to write: [{remaining}].")
        elif "plan" in self.skip:
            parts.append("all planned pages are written — go straight to wiki_finalize.")
        parts.append(
            "Always re-clone + re-scan first (the source must be on disk to write "
            "pages), then write only the remaining pages and call wiki_finalize."
        )
        return " ".join(parts)

    # -- internals (cheap counts only) ---------------------------------------

    @staticmethod
    def _count_graph(store: WikiStoreBase, slug: str) -> int:
        """Number of persisted code-graph nodes for *slug* (0 on any failure)."""
        try:
            return len(store.query_graph(slug))
        except Exception:
            return 0

    @staticmethod
    def _count_entities(store: WikiStoreBase, slug: str) -> int:
        """Number of persisted abstract entities for *slug* (0 on any failure)."""
        try:
            return len(store.query_entities(slug))
        except Exception:
            return 0

    @staticmethod
    def _plan_page_ids(store: WikiStoreBase, job_id: str) -> list[str]:
        """Ordered committed-plan page ids for *job_id* ([] when no plan)."""
        try:
            plan = store.get_job_plan(job_id)
        except Exception:
            return []
        if not plan:
            return []
        ids: list[str] = []
        for entry in plan:
            pid = entry.get("id")
            if isinstance(pid, str) and pid:
                ids.append(pid)
        return ids

    @staticmethod
    def _persisted_page_ids(store: WikiStoreBase, slug: str) -> set[str]:
        """Set of page ids already written to the store for *slug*."""
        try:
            return {p.id for p in store.list_pages(slug)}
        except Exception:
            return set()


__all__ = ["ResumePlan", "SKIPPABLE_PHASES"]
