"""Workspace-scoped routing over the GLOBAL SCG — :class:`ScgScope` + router (#75).

``docs/features-search.md``: the SCG is shared (a tenant of the multiplex graph),
NOT hard-partitioned per workspace. A workspace is a *scope* — a source-id
allowlist the router honours so ``scg_route`` only proposes pathways through the
workspace's own sources. These tests prove the isolation requirement
("two workspaces, same source ids in the shared graph, no bleed") via the scope
filter rather than a store partition:

* an unbound scope routes over every mapped source (historical global behavior);
* a bound scope drops any candidate recipe touching an out-of-scope source;
* two scopes over ONE shared graph never see each other's pathways;
* the scope is ``ContextVar``-isolated (a leaked bind never crosses the boundary).

A deterministic fake embedder is injected — NO real embedding API. The JSON store
runs end-to-end in a tmp dir (no MongoDB).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.router import ScgRouter
from mewbo_graph.scg.scope import ScgScope
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import (
    RouteRecipe,
    ScgEmbedding,
    ScgNode,
)


class _FakeEmbedder:
    """Maps known query strings to fixed vectors; everything else → zero vec."""

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = table or {}

    def embed_query(self, text: str) -> list[float]:
        return self.table.get(text, [0.0, 0.0, 0.0])


@pytest.fixture()
def store(tmp_path: Path) -> JsonScgStore:
    """A fresh JSON-backed SCG store under a throwaway temp dir."""
    return JsonScgStore(root_dir=tmp_path / "scg")


def _cap(source_id: str, name: str) -> ScgNode:
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind="capability",
        source_id=source_id,
        name=name,
    )


def _seed_three_source_graph(store: JsonScgStore) -> None:
    """One shared GLOBAL graph spanning github / slack / jira (all mapped).

    Each source has one capability + one route recipe; embeddings place the three
    capabilities at orthogonal corners so a query can favour any one of them.
    """
    gh, sl, ji = _cap("github", "search"), _cap("slack", "search"), _cap("jira", "search")
    store.upsert_nodes([gh, sl, ji])
    store.upsert_embeddings(
        [
            ScgEmbedding(node_id=gh.node_id, vector=[1.0, 0.0, 0.0], model="m", dim=3),
            ScgEmbedding(node_id=sl.node_id, vector=[0.0, 1.0, 0.0], model="m", dim=3),
            ScgEmbedding(node_id=ji.node_id, vector=[0.0, 0.0, 1.0], model="m", dim=3),
        ]
    )
    store.upsert_recipes(
        [
            RouteRecipe(source_key="github#search", steps=["github#search"]),
            RouteRecipe(source_key="slack#search", steps=["slack#search"]),
            RouteRecipe(source_key="jira#search", steps=["jira#search"]),
        ]
    )
    # Re-key the jira recipe to a CROSS-SOURCE pathway (jira → slack) anchored on
    # the jira#search seed, so it is a real router candidate AND routable only
    # when BOTH sources are in scope (proves the all-steps-permitted rule).
    store.upsert_recipes(
        [RouteRecipe(source_key="jira#search", steps=["jira#search", "slack#search"])]
    )


def _router(store: JsonScgStore) -> ScgRouter:
    return ScgRouter(
        store=store,
        embedder=_FakeEmbedder(
            {
                "all": [0.58, 0.58, 0.58],  # near every corner — surfaces all seeds
                "gh": [1.0, 0.0, 0.0],
            }
        ),
    )


# ── unbound scope = historical global behavior ──────────────────────────────


def test_unbound_scope_routes_over_every_source(store: JsonScgStore) -> None:
    """No active scope → every mapped source is routable (global default)."""
    _seed_three_source_graph(store)
    keys = {r.source_key for r in _router(store).route("all", k=10)}
    assert {"github#search", "slack#search", "jira#search"} <= keys


# ── bound scope filters the candidate set ───────────────────────────────────


def test_bound_scope_drops_out_of_scope_pathways(store: JsonScgStore) -> None:
    """A workspace scope of {github} routes ONLY github pathways."""
    _seed_three_source_graph(store)
    with ScgScope.use(["github"]):
        keys = {r.source_key for r in _router(store).route("all", k=10)}
    assert keys == {"github#search"}
    assert "slack#search" not in keys
    assert "jira#search" not in keys


def test_cross_source_recipe_requires_all_steps_in_scope(store: JsonScgStore) -> None:
    """The jira#search recipe (steps jira→slack) routes iff BOTH are in scope."""
    _seed_three_source_graph(store)
    # jira alone: the cross-source recipe (its steps reach slack too) is dropped.
    with ScgScope.use(["jira"]):
        keys = {r.source_key for r in _router(store).route("all", k=10)}
    assert "jira#search" not in keys
    # jira + slack: the recipe is now fully in scope and routable.
    with ScgScope.use(["jira", "slack"]):
        keys = {r.source_key for r in _router(store).route("all", k=10)}
    assert "jira#search" in keys


def test_scope_permits_recipe_steps_unit() -> None:
    """The all-steps-permitted rule, asserted directly (no router)."""
    with ScgScope.use(["jira"]):
        assert ScgScope.permits_recipe_steps(["jira#a", "jira#b"]) is True
        assert ScgScope.permits_recipe_steps(["jira#a", "slack#b"]) is False
    # Unbound scope permits any steps.
    assert ScgScope.permits_recipe_steps(["any#x", "other#y"]) is True


# ── two workspaces over ONE shared graph — no bleed ─────────────────────────


def test_two_workspace_scopes_no_bleed(store: JsonScgStore) -> None:
    """Same shared graph; workspace-A {github} and workspace-B {slack} disjoint.

    This is the issue's namespace-isolation requirement expressed as a scope: the
    per-source mapping is shared (one global graph), but neither workspace ever
    routes through the other's source.
    """
    _seed_three_source_graph(store)
    router = _router(store)
    with ScgScope.use(["github"]):
        a = {r.source_key for r in router.route("all", k=10)}
    with ScgScope.use(["slack"]):
        b = {r.source_key for r in router.route("all", k=10)}
    assert a == {"github#search"}
    assert b == {"slack#search"}
    assert a.isdisjoint(b)


# ── empty scope vs unbound scope ────────────────────────────────────────────


def test_empty_scope_routes_nothing(store: JsonScgStore) -> None:
    """An EMPTY selection binds an empty allowlist → no pathways (never widens)."""
    _seed_three_source_graph(store)
    with ScgScope.use([]):
        recipes = _router(store).route("all", k=10)
    assert recipes == []


def test_scope_is_reset_after_block(store: JsonScgStore) -> None:
    """The scope is ContextVar-isolated — it resets after the with-block."""
    _seed_three_source_graph(store)
    assert ScgScope.allowed() is None
    with ScgScope.use(["github"]):
        assert ScgScope.allowed() == frozenset({"github"})
    assert ScgScope.allowed() is None


def test_scope_resets_even_on_exception() -> None:
    """A raise inside the block still resets the scope (finally/ContextVar.reset)."""
    assert ScgScope.allowed() is None
    with pytest.raises(ValueError):  # noqa: PT012 — assert the reset, not the raise
        with ScgScope.use(["github"]):
            raise ValueError("boom")
    assert ScgScope.allowed() is None


# ── workspace attribution (#76) ─────────────────────────────────────────────


def test_workspace_binds_and_resets() -> None:
    """``use(workspace=...)`` binds the attribution id; it resets on exit."""
    assert ScgScope.workspace() is None
    with ScgScope.use(["github"], workspace="ws-7"):
        assert ScgScope.workspace() == "ws-7"
        assert ScgScope.allowed() == frozenset({"github"})
    assert ScgScope.workspace() is None


def test_workspace_defaults_none_for_legacy_callers() -> None:
    """A scope bound WITHOUT a workspace leaves the attribution id unbound.

    Existing ``use(source_ids)`` callers (#75) are unchanged — the workspace is
    additive and defaults to ``None``.
    """
    with ScgScope.use(["github"]):
        assert ScgScope.workspace() is None
