"""Workspace-scoped VIEW over the GLOBAL Source Capability Graph (#75).

``docs/features-search.md`` is explicit: the SCG is "a tenant of the same
three-layer multiplex graph that powers the Agentic Wiki" and the layers
cross-pollinate "without any explicit wiring". So a workspace does **not** get a
hard-partitioned copy of the graph — per-source mappings stay GLOBAL and
content-addressed (``node_id = sha1(source_key|kind)[:16]``), so a re-map of one
source is a cheap idempotent upsert *every* workspace that maps it benefits from.

A workspace is instead a **scope filter**: the set of source ids its enabled
sources resolve to. :class:`ScgScope` carries that allowlist on a
``ContextVar`` — per-thread/per-task isolated, so two concurrent search drives
never see each other's scope — and :class:`ScgRouter` consults it at query time
so ``scg_route`` only ranks pathways through the workspace's own sources. The
(un-owned) ``scg`` plugin tools call ``get_scg_store()`` / ``ScgCore.router`` with
no scope argument, so the scope MUST ride this ambient seam, which the search
drive binds for its worker thread — no plugin-tool change needed.

The default scope (``None``) is unrestricted — every mapped source is routable,
preserving the historical global behavior for any caller that never binds one.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# The source-id allowlist for the CURRENT execution context. ``None`` == no
# restriction (route over every mapped source — the historical default); a frozen
# set restricts routing to exactly those source ids.
_active_scope: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "scg_active_source_scope", default=None
)

# The workspace id for the CURRENT execution context — ATTRIBUTION ONLY (#76),
# never a partition. A connector insight deposited inside a bound scope is tagged
# with this id so the multiplex can say which workspace LEARNED a fact, while the
# shared graph still lets every workspace READ it (cross-pollination). ``None``
# when no workspace is bound (a bare map/route with no run context).
_active_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scg_active_workspace", default=None
)


class ScgScope:
    """Ambient workspace source-scope — bind it; the router honours it.

    Stateless façade over one ``ContextVar`` (the atomic-class idiom: no
    instances, class methods over shared context state). A search drive calls
    :meth:`use` around its session run; :meth:`allowed` is the read the router
    applies. Keeping the scope here (in the engine library) — not threaded as a
    router ctor argument — is what lets the un-owned plugin tools stay untouched:
    they construct the router with no scope, and it reads the ambient one.
    """

    @staticmethod
    def allowed() -> frozenset[str] | None:
        """Return the active source-id allowlist, or ``None`` (unrestricted)."""
        return _active_scope.get()

    @staticmethod
    def workspace() -> str | None:
        """Return the active workspace id for attribution, or ``None`` (#76).

        Attribution only — a deposit reads this to TAG which workspace learned a
        fact; routing/reading never partitions on it (cross-pollination stays).
        """
        return _active_workspace.get()

    @staticmethod
    def permits(source_id: str) -> bool:
        """True if *source_id* is routable under the active scope.

        Unrestricted (``None``) permits everything; a bound scope permits only
        its members — the rule the router applies to every candidate pathway.
        """
        scope = _active_scope.get()
        return scope is None or source_id in scope

    @classmethod
    def permits_recipe_steps(cls, steps: list[str]) -> bool:
        """True iff EVERY step's source id is permitted under the active scope.

        A :class:`RouteRecipe`'s steps are ``<source_id>#<Qualified.Name>`` keys;
        a recipe is routable only if all of its sources are in scope, so a probe
        can never be handed a pathway that reaches a source the workspace did not
        enable.
        """
        return all(cls.permits(step.split("#", 1)[0]) for step in steps)

    @staticmethod
    @contextmanager
    def use(
        source_ids: list[str] | None, *, workspace: str | None = None
    ) -> Iterator[None]:
        """Bind *source_ids* as the active scope for this context block.

        ``None`` (or an empty list) binds the unrestricted scope — useful when a
        run has no source selection yet (it should route over nothing useful, but
        we never silently widen: an EMPTY selection binds an empty frozenset, so
        the router returns no pathways rather than the whole catalog). Resets on
        exit even if the block raises, so a drive never leaks its scope onto the
        worker thread's next task.

        ``workspace`` (optional) binds the workspace id for deposit attribution
        (#76) for the same block — additive, so existing ``use(source_ids)``
        callers are unchanged. Both reset together on exit.
        """
        scope = None if source_ids is None else frozenset(source_ids)
        token = _active_scope.set(scope)
        ws_token = _active_workspace.set(workspace)
        try:
            yield
        finally:
            _active_scope.reset(token)
            _active_workspace.reset(ws_token)


__all__ = ["ScgScope"]
