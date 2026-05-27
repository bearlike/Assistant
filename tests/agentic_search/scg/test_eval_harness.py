"""Offline evaluation harness (P13) for the Source Capability Graph (SCG).

This is the *eval* suite — distinct from the per-class unit tests. It stands up
a small **fake multi-connector catalog** (Jira + Linear OpenAPI docs and a
PagerDuty MCP tool-list) and pushes it through the **real SCG code paths**
(``ScgParser`` → ``ScgStore`` → ``ScgRouter`` → ``TypeAligner`` →
``ScgMemoryBridge``), injecting deterministic fakes *only* at the embedder / LLM
boundary. NO real LLM, NO network, NO MongoDB — it runs end-to-end against the
JSON store in a tmp dir.

Each eval is its own test asserting a *named metric* (not a smoke run) against a
threshold, and prints the metric so a CI log shows the score:

* ``eval_route_precision`` — ROUTE_PRECISION@k over gold (query → expected
  source) cases ≥ threshold.
* ``eval_routing_shrinkage`` — ROUTING_SHRINKAGE: routed candidate set is a small
  fraction of all capability nodes (the "fewer agents than one-per-source"
  claim).
* ``eval_cross_source_link`` — CROSS_SOURCE_LINK: ``TypeAligner`` emits a
  ``RESOLVES_TO`` edge for an equivalent type pair and abstains for a dissimilar
  one (precision = 1.0, no false positive).
* ``eval_flywheel_cold_vs_warm`` — FLYWHEEL_WARM_GAIN: after depositing a
  data-location-win insight via the memory bridge (``corpus="connector"``), a
  warm read surfaces it where the cold read returned nothing.

The fixtures (catalog descriptors + the keyword-bag fake embedder) live in
``conftest.py`` alongside this module so other SCG eval files can reuse them
without re-inventing fakes (DRY — same fake-embedder shape as ``test_router``
and ``test_memory_bridge``).
"""

from __future__ import annotations

from pathlib import Path

from mewbo_graph.scg.entity_resolution import TypeAligner
from mewbo_graph.scg.memory_bridge import (
    CONNECTOR_SLUG,
    ScgAnchorResolver,
    ScgMemoryBridge,
)
from mewbo_graph.scg.parser import ScgParser
from mewbo_graph.scg.providers import (
    McpToolListStructureProvider,
    OpenApiStructureProvider,
)
from mewbo_graph.scg.router import ScgRouter
from mewbo_graph.scg.store import JsonScgStore
from mewbo_graph.scg.types import RouteRecipe

from .conftest import EvalEmbedder, entity_type

# ── Gold routing cases (query → expected source_id of the top route) ─────────
#
# Each query is written to lexically favour exactly one connector's capability
# under the keyword-bag embedder: the matching capability's embed-text (name +
# doc + example queries) shares the most vocabulary tokens with the query.
_GOLD_ROUTES: list[tuple[str, str]] = [
    ("search jira issues by project", "jira"),
    ("find a linear ticket assigned to a user", "linear"),
    ("list pagerduty incidents that are triggered", "pagerduty"),
    ("which jira issue has this status", "jira"),
    ("open linear tickets for a team", "linear"),
    ("acknowledge the on-call incident in pagerduty", "pagerduty"),
]


# ── Harness builders ─────────────────────────────────────────────────────────


def _build_catalog(
    tmp_path: Path,
    descriptors: list,
    embedder: EvalEmbedder,
    *,
    link: bool = False,
) -> JsonScgStore:
    """Parse every descriptor into a fresh JSON store via the REAL ``ScgParser``.

    Wires the actual default providers + the deterministic eval embedder; the
    only injected fake is the embedder. Optionally runs the ``TypeAligner`` /
    In-N-Out joins so a route can chain across resolved types.
    """
    store = JsonScgStore(root_dir=tmp_path / "scg")
    parser = ScgParser(
        store=store,
        providers=[OpenApiStructureProvider(), McpToolListStructureProvider()],
        embedder=embedder,
        aligner=TypeAligner(store=store) if link else None,
    )
    for descriptor in descriptors:
        parser.parse_source(descriptor)
    if link:
        parser.compute_param_edges()
        parser.link_sources([d.source_id for d in descriptors])
    return store


def _seed_recipes(store: JsonScgStore) -> None:
    """Attach one ``RouteRecipe`` per capability node (anchor for routing).

    The router only returns recipes reachable from a vector-search seed. The
    parser does not synthesise recipes (that is the traversal engine's job in
    #19), so the eval seeds the obvious one-capability recipe per capability —
    the minimal scaffold the cheap pre-rank needs to return a route at all.
    """
    recipes = [
        RouteRecipe(source_key=cap.source_key, steps=[cap.source_key])
        for cap in store.query_nodes(kind="capability")
    ]
    store.upsert_recipes(recipes)


# ── EVAL 1 — route precision ─────────────────────────────────────────────────


def test_eval_route_precision(tmp_path: Path, eval_descriptors: list) -> None:
    """ROUTE_PRECISION@3: the gold source appears in the top-k routes.

    For each gold ``(query, expected_source)`` the router must surface a recipe
    whose ``source_id`` matches inside the top-k. We assert mean precision over
    the gold set clears a strict threshold (the deterministic embedder makes
    this reproducible, so the bar is high).
    """
    k = 3
    threshold = 0.83  # 5 of 6 gold cases must land their source in top-k.
    embedder = EvalEmbedder()
    store = _build_catalog(tmp_path, eval_descriptors, embedder)
    _seed_recipes(store)
    router = ScgRouter(store=store, embedder=embedder)

    hits = 0
    for query, expected_source in _GOLD_ROUTES:
        routes = router.route(query, k=k)
        routed_sources = {_source_of(r) for r in routes[:k]}
        if expected_source in routed_sources:
            hits += 1
    precision = hits / len(_GOLD_ROUTES)

    print(f"\nROUTE_PRECISION@{k} = {precision:.3f} ({hits}/{len(_GOLD_ROUTES)})")
    assert precision >= threshold, (
        f"route precision {precision:.3f} < {threshold} "
        f"({hits}/{len(_GOLD_ROUTES)} gold cases hit)"
    )


# ── EVAL 2 — routing shrinkage ───────────────────────────────────────────────


def test_eval_routing_shrinkage(tmp_path: Path, eval_descriptors: list) -> None:
    """ROUTING_SHRINKAGE: routed set ≪ all capabilities (fewer agents claim).

    Blind fan-out would spend one agent per capability node in the catalog. The
    graph-routed pre-rank must hand the traversal engine a *materially smaller*
    candidate set. We assert the mean routed-fraction over the gold queries
    stays at/below a fraction of the total capability count.
    """
    k = 3
    max_fraction = 0.5  # routed candidates ≤ half the catalog's capabilities.
    embedder = EvalEmbedder()
    store = _build_catalog(tmp_path, eval_descriptors, embedder)
    _seed_recipes(store)
    router = ScgRouter(store=store, embedder=embedder)

    total_caps = len(store.query_nodes(kind="capability"))
    assert total_caps > 0, "catalog must expose capabilities to route over"

    fractions: list[float] = []
    for query, _ in _GOLD_ROUTES:
        routed = len(router.route(query, k=k))
        fractions.append(routed / total_caps)
    mean_fraction = sum(fractions) / len(fractions)

    print(
        f"\nROUTING_SHRINKAGE: mean routed/total_caps = {mean_fraction:.3f} "
        f"(k={k}, total_caps={total_caps})"
    )
    assert mean_fraction <= max_fraction, (
        f"routed fraction {mean_fraction:.3f} > {max_fraction}: routing did "
        f"not shrink the {total_caps}-capability fan-out"
    )
    # And every individual route is itself bounded by k — never the whole catalog.
    assert all(f * total_caps <= k for f in fractions)


# ── EVAL 3 — cross-source link ───────────────────────────────────────────────


def test_eval_cross_source_link(tmp_path: Path, eval_descriptors: list) -> None:
    """CROSS_SOURCE_LINK: RESOLVES_TO for equivalents, abstain for the rest.

    Jira.Issue and Linear.Issue share schema fields → the aligner must emit one
    ``RESOLVES_TO`` hypothesis edge between them. PagerDuty.Incident is
    dissimilar → it must NOT be linked to either. We assert linking precision =
    1.0 (the one expected pair, zero false positives).

    The aligner's field-overlap heuristic reads ``entity_type.bindings``; the
    providers split entity fields into separate ``field`` nodes, so we seed the
    binding-bearing entity types (the spec §6 shape) onto the parsed catalog
    before running the REAL ``TypeAligner.align`` over the persisted nodes.
    """
    embedder = EvalEmbedder()
    store = _build_catalog(tmp_path, eval_descriptors, embedder)
    store.upsert_nodes(
        [
            entity_type("jira", "Issue", ["id", "title", "status", "assignee"]),
            entity_type("linear", "Issue", ["id", "title", "status", "assignee"]),
            # Dissimilar schema — the abstain case (no shared field names).
            entity_type("pagerduty", "Incident", ["urgency", "service", "escalation"]),
        ]
    )

    source_ids = [d.source_id for d in eval_descriptors]
    TypeAligner(store=store).align(source_ids)

    resolves = [
        (e.source, e.target) for e in store.list_edges(kind="RESOLVES_TO")
    ]
    linked_pairs = {frozenset(pair) for pair in resolves}
    expected_pair = frozenset({"jira#Issue", "linear#Issue"})

    true_positive = expected_pair in linked_pairs
    false_positives = [p for p in linked_pairs if p != expected_pair]
    precision = (1.0 if true_positive else 0.0) / max(len(linked_pairs), 1)

    print(
        f"\nCROSS_SOURCE_LINK: precision = {precision:.3f} "
        f"(linked={[sorted(p) for p in linked_pairs]})"
    )
    assert true_positive, "TypeAligner failed to link Jira.Issue <=> Linear.Issue"
    assert not false_positives, (
        f"TypeAligner emitted false-positive links: "
        f"{[sorted(p) for p in false_positives]}"
    )
    # No PagerDuty incident type bled into a cross-source link (abstain held).
    assert all("pagerduty#Incident" not in pair for pair in linked_pairs)
    assert all(e.method == "type_align" for e in store.list_edges(kind="RESOLVES_TO"))


# ── EVAL 4 — flywheel cold-vs-warm ───────────────────────────────────────────


def test_eval_flywheel_cold_vs_warm(
    tmp_path: Path, eval_descriptors: list, wiki_store
) -> None:
    """FLYWHEEL_WARM_GAIN: a deposited connector insight surfaces only when warm.

    Cold state: no connector insights exist → the memory read returns nothing.
    We then deposit a data-location-win insight (``corpus="connector"``) anchored
    to a real SCG entity-type ``source_key`` via the bridge. Warm state: the same
    query now surfaces the deposited insight. The gain (warm − cold) must be
    positive — warm state changes the routing seed.
    """
    embedder = EvalEmbedder()
    store = _build_catalog(tmp_path, eval_descriptors, embedder)

    bridge = ScgMemoryBridge(wiki_store=wiki_store, embedder=embedder, llm=None)
    bridge.resolver = ScgAnchorResolver(store)

    insight = "jira issue status is queryable by project key, not free text"
    qvec = embedder.embed_query("jira issue status project")

    # COLD read — nothing deposited yet.
    cold_hits = bridge.read_insights(CONNECTOR_SLUG, qvec, k=5)
    cold_contents = [n.content for n in cold_hits]

    # Deposit the data-location win anchored to the real entity-type source_key.
    res = bridge.write_insight(
        CONNECTOR_SLUG, insight, source_keys=["jira#Issue"]
    )
    assert res.ok, "insight deposit failed"

    # WARM read — same query, same embedder.
    warm_hits = bridge.read_insights(CONNECTOR_SLUG, qvec, k=5)
    warm_contents = [n.content for n in warm_hits]

    cold_n, warm_n = len(cold_contents), len(warm_contents)
    print(
        f"\nFLYWHEEL_WARM_GAIN: cold={cold_n} warm={warm_n} "
        f"(gain={warm_n - cold_n})"
    )
    assert cold_contents == [], "cold read should surface no connector insight"
    assert insight in warm_contents, "warm read must surface the deposited insight"
    assert warm_n > cold_n, "warm state did not change the routing seed"
    # The anchor created a live ANCHORS edge to the real SCG source_key.
    node_id = res.claims[0].node_id
    anchors = [
        e
        for e in wiki_store.list_memory_edges(CONNECTOR_SLUG, node_id=node_id)
        if e.type == "ANCHORS"
    ]
    assert [e.target for e in anchors] == ["jira#Issue"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _source_of(recipe: RouteRecipe) -> str:
    """The ``source_id`` of a recipe — the prefix before ``#`` in its source_key."""
    return recipe.source_key.split("#", 1)[0]
