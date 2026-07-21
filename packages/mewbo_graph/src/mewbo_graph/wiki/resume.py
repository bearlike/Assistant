"""``ResumePlan`` — single source of truth for "what an interrupted index did".

The atomic record of what an interrupted index already finished.

Checkpoint-aware recovery does NOT re-run the whole pipeline. ``clone`` + ``scan``
are cheap (seconds) and the cloned source is needed to write the remaining pages,
so they always run. The expensive, idempotent phases are SKIPPED when their store
artifacts already exist:

- ``graph``  — skip when the graph FOR THIS COMMIT is non-empty.
- ``enrich`` — skip when abstract entities FOR THIS COMMIT exist.
- ``plan``   — skip when the job has a committed page plan.
- ``pages``  — write only the plan pages NOT already in the store.

The graph/enrich counts are keyed on ``(slug, commit_sha)``, not on the slug's
whole artifact set: since the store carries the UNION of every commit ever
indexed for a slug, "N nodes exist" answers "some commit built a graph", not
"this commit's graph is built" — the distinction the skip decision turns on.

``ResumePlan`` is computed ONCE (``build``) at resume time and persisted as a tiny
dict on the job's resume sidecar (``store.save_resume_plan``). Each wiki phase tool
rebuilds it cheaply per call from that dict (``from_persisted``) so the skip guards
never re-query the graph — the build cost is paid once, not once-per-tool-call.

Every store read ``build`` makes fails CLOSED (``ResumeCountError``). "I counted
zero" and "I could not count" are different answers, and only the first one may
select a rebuild: a rebuild costs a full re-index plus a re-embedding pass, so
inferring one from a transient read failure is the single most expensive mistake
this module can make.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from mewbo_core.common import get_logger

if TYPE_CHECKING:
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.types import IndexingJob

logging = get_logger(name="mewbo_graph.wiki.resume")

# The expensive idempotent phases ``ResumePlan`` may skip. ``clone``/``scan`` are
# always re-run; ``pages`` is handled page-by-page (not a whole-phase skip);
# ``finalize`` is always re-run (idempotent upsert).
SKIPPABLE_PHASES: frozenset[str] = frozenset({"graph", "enrich", "plan"})


class ResumeCountError(RuntimeError):
    """A store read the resume decision depends on could not be made.

    The distinction this type exists to preserve: an artifact count of zero
    means "nothing was ever built", and rebuilding is then the correct branch —
    but a count that FAILED means nothing at all, and rebuilding on the strength
    of it costs a full re-index plus a re-embedding pass. The reads used to
    swallow every exception and return 0, collapsing the second case into the
    first with no log line, so the triggering exception was never even
    identifiable after the fact.

    Raising instead makes the resume path fail CLOSED: a caller that cannot
    determine what is already built must refuse to resume rather than silently
    select the expensive branch.
    """


@dataclass(frozen=True)
class ResumePlan:
    """Immutable record of which phases an interrupted index already completed.

    ``skip`` is a subset of :data:`SKIPPABLE_PHASES`. ``pages_done`` is the set of
    plan page ids already persisted; ``pages_remaining`` is the ordered plan ids
    NOT yet written. ``node_count`` / ``entity_count`` / ``total_pages`` are carried
    only for the agent-facing :meth:`summary`.

    ``restart`` marks the plan as a deliberate rebuild-from-scratch rather than a
    checkpoint resume. Both carry an empty ``skip``, so the flag is the only thing
    that tells them apart — see :meth:`for_restart`.
    """

    skip: frozenset[str] = field(default_factory=frozenset)
    pages_done: frozenset[str] = field(default_factory=frozenset)
    pages_remaining: tuple[str, ...] = ()
    node_count: int = 0
    entity_count: int = 0
    total_pages: int = 0
    restart: bool = False

    # -- construction --------------------------------------------------------

    @classmethod
    def build(cls, store: WikiStoreBase, job: IndexingJob) -> ResumePlan:
        """Inspect the store + *job* and compute what is already done.

        Cheap by design: a graph node count, an entity count, the committed plan
        ids, and the persisted page ids — never the full node/entity payloads. Run
        ONCE at resume time; persist via :meth:`to_persisted` so per-tool-call
        rebuilds use :meth:`from_persisted` instead of re-querying the graph.

        Raises :class:`ResumeCountError` when any of those reads fails. That is
        deliberate and load-bearing: an unreadable store cannot be distinguished
        from an empty one by its return value, so the caller must refuse to
        resume rather than infer "nothing is built" and rebuild everything.
        """
        slug = job.slug

        # Commit-scoped: "the graph/entities for THIS commit are built" — a
        # union count over every commit ever indexed for the slug cannot answer
        # that, which is what let ``node_count: 53489`` read as "the graph for
        # this commit is done" when it meant "the graph for SOME commit is". A
        # commit-less job (catalog / a clone that never resolved a sha) counts
        # the ``None``-stamped rows, its only honest cohort.
        commit_sha = job.commit_sha
        node_count = cls._count_graph(store, slug, commit_sha)
        entity_count = cls._count_entities(store, slug, commit_sha)

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
    def for_restart(cls) -> ResumePlan:
        """The no-skip plan for a deliberate REBUILD-FROM-SCRATCH.

        Structurally identical to ``ResumePlan()`` — every guard short-circuits to
        "not skipped", so every phase re-runs — but it records the *intent*. A
        resume that happened to find nothing reusable and a restart that is
        discarding everything on purpose produce the same skip set, and until the
        intent was carried the agent was told to "reuse completed work" on a run
        whose entire purpose was to throw it away. :meth:`summary` renders the two
        as different instructions.
        """
        return cls(restart=True)

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
            restart=bool(data.get("restart", False)),
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
            "restart": self.restart,
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
        if self.restart:
            return (
                "RESTART — rebuild this index from scratch. Do NOT reuse anything a "
                "previous attempt left in the store: re-clone, re-scan, rebuild the "
                "graph (wiki_build_graph), re-run the enrich fan-out, commit a fresh "
                "plan (wiki_commit_plan), write EVERY planned page, then call "
                "wiki_finalize. The writes are idempotent upserts, so the stale "
                "artifacts are overwritten as you go."
            )
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
    #
    # Every read below fails CLOSED. An empty result is a real answer ("nothing
    # is built yet") that legitimately selects a rebuild; an exception is not an
    # answer at all, and treating it as an empty one is what turned a transient
    # store glitch into a full re-index. See :class:`ResumeCountError`.

    @staticmethod
    def _read_failed(what: str, key: str, exc: Exception) -> NoReturn:
        """Log *exc* and raise :class:`ResumeCountError` — the one failure path.

        Shared by all four reads so a failure is reported identically wherever it
        happens. The log line matters as much as the raise: the old handlers were
        silent, so nothing in the job log ever named the exception that had
        triggered a rebuild.
        """
        logging.warning(
            "ResumePlan: {} read failed for {} ({}: {}) — refusing to resume",
            what, key, type(exc).__name__, exc,
        )
        raise ResumeCountError(f"{what} read failed for {key}: {exc}") from exc

    @classmethod
    def _count_graph(cls, store: WikiStoreBase, slug: str, commit_sha: str | None) -> int:
        """Nodes persisted for *slug* by exactly *commit_sha* (the skip cohort)."""
        try:
            return store.count_graph_nodes(slug, commit_sha=commit_sha)
        except Exception as exc:
            cls._read_failed("graph node count", slug, exc)

    @classmethod
    def _count_entities(cls, store: WikiStoreBase, slug: str, commit_sha: str | None) -> int:
        """Entities minted for *slug* by exactly *commit_sha* (the skip cohort)."""
        try:
            return store.count_entities(slug, commit_sha=commit_sha)
        except Exception as exc:
            cls._read_failed("entity count", slug, exc)

    @classmethod
    def _plan_page_ids(cls, store: WikiStoreBase, job_id: str) -> list[str]:
        """Ordered committed-plan page ids for *job_id* ([] when no plan)."""
        try:
            plan = store.get_job_plan(job_id)
        except Exception as exc:
            cls._read_failed("committed plan", job_id, exc)
        if not plan:
            return []
        ids: list[str] = []
        for entry in plan:
            pid = entry.get("id")
            if isinstance(pid, str) and pid:
                ids.append(pid)
        return ids

    @classmethod
    def _persisted_page_ids(cls, store: WikiStoreBase, slug: str) -> set[str]:
        """Set of page ids already written to the store for *slug*."""
        try:
            return {p.id for p in store.list_pages(slug)}
        except Exception as exc:
            cls._read_failed("persisted pages", slug, exc)


__all__ = ["ResumeCountError", "ResumePlan", "SKIPPABLE_PHASES"]
