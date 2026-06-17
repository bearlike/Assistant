"""Agentic-search launcher — a down-only DI seam for self-facing search.

A task-spawned engine agent that wants to *run* an agentic search needs the
full run lifecycle: its own ``scg-search`` SESSION, a durable run record, and
the event log — all of which live **up** in the api
(``mewbo_api.agentic_search``), the transport/persistence layer. A SessionTool
here cannot import up to reach it. So the api registers a concrete launcher at
startup (:meth:`SearchLauncher.register`) and the ``agentic_search`` SessionTool
drives the run through it — exactly the :class:`~mewbo_graph.scg.map_phase.MapPhaseSink`
inversion, applied to the run lifecycle instead of cosmetic phase progress.

The seam is deliberately **async-by-handle**: :meth:`start` kicks a run off on
the runtime's managed worker and returns IMMEDIATELY with an idempotent
``run_id`` (a search session can take minutes — never block the calling agent's
loop on it). :meth:`fetch` reads the durable snapshot back (the cited answer +
when it was computed). The caller polls by re-invoking the tool with the
``run_id``.

With no launcher registered — a core/graph-only install, or the api never
initialised — every call returns ``None``: the tool degrades to a structured
"agentic search unavailable" error rather than crashing.
"""

from __future__ import annotations

from typing import ClassVar, Protocol


class SearchLauncherImpl(Protocol):
    """The concrete launcher the api registers (run-store + runtime bound)."""

    def start(
        self,
        query: str,
        *,
        workspace: str | None = None,
        tier: str | None = None,
    ) -> dict[str, object]:
        """Kick off (or idempotently reuse) a run; return its handle/snapshot.

        Returns an idempotent handle (``run_id`` + ``status``) for a run still
        in flight, or the full snapshot when the run is already terminal (a
        synchronous/echo run, or a reused recent completed run for the same
        query). Raises :class:`ValueError` with actionable guidance (e.g. the
        available workspace names) when the request can't be resolved.
        """
        ...

    def fetch(self, run_id: str) -> dict[str, object] | None:
        """Return a run's last-known snapshot, or ``None`` if the id is unknown."""
        ...


class SearchLauncher:
    """Process-wide injectable launcher for self-facing agentic search."""

    _impl: ClassVar[SearchLauncherImpl | None] = None

    @classmethod
    def register(cls, impl: SearchLauncherImpl | None) -> None:
        """Install the concrete launcher (called by the api at startup)."""
        cls._impl = impl

    @classmethod
    def reset(cls) -> None:
        """Clear the registered launcher (test isolation / core-only teardown)."""
        cls._impl = None

    @classmethod
    def available(cls) -> bool:
        """True when a concrete launcher is registered."""
        return cls._impl is not None

    @classmethod
    def start(
        cls, query: str, *, workspace: str | None = None, tier: str | None = None
    ) -> dict[str, object] | None:
        """Kick off a run via the registered launcher; ``None`` if none wired."""
        if cls._impl is None:
            return None
        return cls._impl.start(query, workspace=workspace, tier=tier)

    @classmethod
    def fetch(cls, run_id: str) -> dict[str, object] | None:
        """Fetch a run snapshot via the registered launcher; ``None`` if none."""
        if cls._impl is None:
            return None
        return cls._impl.fetch(run_id)


__all__ = ["SearchLauncher", "SearchLauncherImpl"]
