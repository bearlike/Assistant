"""``scg_observe`` SessionTool — agent-driven neighborhood reads over the SCG.

Single responsibility: project one SCG node's directed, typed neighborhood into a
compact, LLM-sized payload so the agent can *observe → think → navigate* —
``scg_route`` ranks ENTRY points, ``scg_observe`` reads the hops, the agent
decides where to step, probes execute, ``scg_memory`` deposits polarity.

Search-on-Graph (arXiv 2510.08825) inverts the usual retrieval split: the engine
ranks entries (``scg_route``'s cheap cosine+weight pre-rank), then the AGENT reads
a node's neighborhood and decides where to step — the *typed edges* carry the
routing information (``SUPPORTS_QUERY``/``PRODUCES``/``CONSUMES``/``RESOLVES_TO``),
so navigation is the agent's reasoning, not a second engine score. Given one or
more node references this returns each node's neighborhood — the node, its edges
(kind + weight + direction), compact neighbor descriptors, the recipes through it,
and the anchored connector memory notes the learned layer deposited.

Two stages (SoG's measured win, kept minimal — it falls straight out of the same
hop read): a node whose in-scope degree exceeds :data:`_SURVEY_THRESHOLD` with no
``edge_kinds`` filter returns a ``kinds_only`` SURVEY — the distinct edge / neighbor
kinds with counts — and the agent re-calls with an ``edge_kinds`` filter for the
instances it wants; a filtered or small read returns the neighbor rows directly,
hard-capped at :data:`_TRUNCATE_BACKSTOP`.

This is a thin PROJECTION over the SCG store + :class:`ScgGraphView`'s memory
assembly — no new traversal engine, no store read the view/store don't already
expose, no parallel node/edge types (it reuses :class:`ScgNode` / :class:`ScgEdge`
/ :class:`~mewbo_graph.scg.types.EdgeKind`). Read-only ⇒ ``concurrency_safe`` (the
agent may observe several seeds in parallel).

Scope invariant: the active :class:`ScgScope` is honoured — a neighbor / anchor
that reaches an out-of-scope source is dropped, so a workspace never observes a
hop into a source it never enabled.

Security invariant (spec §6): only the redacted structure the core persists is
echoed — ``auth_scope`` is never surfaced; a memory note is a propositional
reachability fact, never a record value, token, or credential.

Wire shape (the playbook author's contract — ``ObserveResult.to_wire()``)::

    {"count": <int>, "observed": [<ObservedNode.to_wire()>, ...]}

    # a resolved node, mode="rows" (small / filtered neighborhood):
    {"ref", "found": True, "key", "label", "kind", "source", "degree",
     "mode": "rows", "doc"?,
     "edges": [{"kind", "dir": "out"|"in", "to": <source_key>, "w": <float>}],
     "neighbors": [{"key", "label"?, "kind"?, "doc"?}],
     "recipes"?: [{"key", "steps": [<source_key>, ...]}],
     "memory"?: [{"text"}]}

    # a resolved node, mode="kinds_only" (large unfiltered neighborhood):
    {..., "mode": "kinds_only",
     "survey": {"edgeKinds": {"PRODUCES→": 12, "←CONSUMES": 3},
                "neighborKinds": {"field": 14, "capability": 1}},
     "hint": "Large neighborhood — re-call with `edge_kinds` …"}

    # an unresolvable reference:
    {"ref", "found": False}
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Literal, get_args

from mewbo_core.common import MockSpeaker, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_graph.plugins.scg._core import (
    SCG_CORE_UNAVAILABLE,
    ScgCore,
    SessionToolBase,
    err_result,
    ok_result,
)
from mewbo_graph.scg.types import EdgeKind, NodeKind, ScgNode

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

    from mewbo_graph.scg.store import ScgStore
    from mewbo_graph.scg.types import ScgEdge

# Which side of a typed edge a hop traverses (the navigation signal —
# PRODUCES outward vs CONSUMES inward mean different things to a search agent).
HopDirection = Literal["out", "in"]
# The request-facing direction filter (``both`` reads both sides).
Direction = Literal["incoming", "outgoing", "both"]
# Survey output mode: instance ``rows`` vs the ``kinds_only`` rollup (stage 1).
ObserveMode = Literal["rows", "kinds_only"]

# Two-stage caps (named — the wire stays LLM-sized). Above this many in-scope
# edges with NO filter, return a SURVEY (rollup counts) instead of instance rows
# — the agent then re-calls with an `edge_kinds` filter. Tuned for capabilities
# (a handful of typed edges each), so most reads never trip it.
_SURVEY_THRESHOLD = 50
# Hard truncation backstop on instance rows — a far smaller ceiling than a code
# graph's, since our nodes are capabilities; never truncates a real neighborhood
# while bounding a pathological one.
_TRUNCATE_BACKSTOP = 200
_MAX_NOTES = 5  # anchored connector memory notes per observed node.
_MAX_RECIPES = 10  # route recipes threading the observed node.
_MAX_SEEDS = 10  # observed nodes per call (the fan-in cap).
_NOTE_SNIPPET_CHARS = 200  # per-note descriptor truncation.
_DOC_SNIPPET_CHARS = 160  # one-line node/neighbor descriptor truncation.


# ── Request ──────────────────────────────────────────────────────────────────


class ScgObserveArgs(BaseModel):
    """Observe an SCG node's typed neighborhood, then navigate (Search-on-Graph).

    Observe → think → navigate: `scg_route` seeds entry points; `scg_observe` reads
    the hops off a node so YOU (not an engine score) pick the next step — the typed
    edges carry the meaning (SUPPORTS_QUERY = how to call it, PRODUCES = what it
    returns, CONSUMES = what it chains into, RESOLVES_TO = the same concept in
    another source); then probes execute and `scg_memory` deposits polarity on what
    worked. Pass `source_key`s (or node ids) to get each node's edges, 1-hop
    neighbors, recipes, and learned memory notes. On a large node, observe first
    returns a `kinds_only` survey (edge/neighbor kind counts) — re-call with
    `edge_kinds`/`direction` to pull just the hops you need. The run ends when you
    emit the structured answer, not when you stop observing.
    """

    model_config = ConfigDict(extra="forbid")

    nodes: list[str] = Field(
        min_length=1,
        max_length=_MAX_SEEDS,
        description=(
            "Node references to observe: a ``<source_id>#<Qualified.Name>`` "
            "source_key (e.g. ``github#search_issues``) or a 16-char node id. "
            f"≤{_MAX_SEEDS} per call."
        ),
    )
    direction: Direction = Field(
        default="both",
        description=(
            "Which typed hops to read: ``outgoing`` (what this capability "
            "PRODUCES / SUPPORTS_QUERY / CONSUMES into), ``incoming`` (what "
            "CONSUMES / produces into it), or ``both`` (default). Edges are "
            "directed and the distinction is the navigation signal."
        ),
    )
    edge_kinds: list[EdgeKind] = Field(
        default_factory=list,
        description=(
            "Selective-retrieval filter (stage 2): only return hops of these edge "
            "kinds (``HAS_ENTITY``/``HAS_FIELD``/``SUPPORTS_QUERY``/``PRODUCES``/"
            "``CONSUMES``/``RESOLVES_TO``). Empty = survey a large node, return all "
            "hops on a small one."
        ),
    )


# ── Typed response models (each owns its compact ``to_wire``) ─────────────────


class _Compact(BaseModel):
    """Base for the observe wire models — frozen, no-null compact projection.

    Subclasses build a strongly-typed object, then ``to_wire`` emits the short-key
    dict the agent consumes (omitting any ``None`` field), so the projection rule
    lives once per record rather than in a hand-rolled factory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservedEdge(_Compact):
    """One typed, directed, weighted hop off the observed node."""

    kind: EdgeKind
    direction: HopDirection
    to: str  # the partner ``source_key``.
    weight: float

    def to_wire(self) -> dict[str, object]:
        """Compact row: ``{kind, dir, to, w}``."""
        return {"kind": self.kind, "dir": self.direction, "to": self.to, "w": self.weight}


class NeighborCard(_Compact):
    """A one-line neighbor descriptor (machine key + human label + kind)."""

    key: str
    label: str | None = None
    kind: NodeKind | None = None
    doc: str | None = None

    def to_wire(self) -> dict[str, object]:
        """Compact card: ``{key, label?, kind?, doc?}`` (drops null fields)."""
        out: dict[str, object] = {"key": self.key}
        if self.label is not None:
            out["label"] = self.label
        if self.kind is not None:
            out["kind"] = self.kind
        if self.doc is not None:
            out["doc"] = self.doc
        return out


class RecipeRef(_Compact):
    """A route recipe threading the observed node (its ordered steps)."""

    key: str
    steps: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        """Compact ref: ``{key, steps}``."""
        return {"key": self.key, "steps": list(self.steps)}


class MemoryNote(_Compact):
    """An anchored connector memory note (a propositional reachability fact)."""

    text: str

    def to_wire(self) -> dict[str, object]:
        """Compact note: ``{text}``."""
        return {"text": self.text}


class NeighborhoodSurvey(_Compact):
    """Stage-1 rollup: edge-kind (by direction) + neighbor-kind histograms."""

    edge_kinds: dict[str, int]
    neighbor_kinds: dict[str, int]

    def to_wire(self) -> dict[str, object]:
        """Compact survey: ``{edgeKinds, neighborKinds}``."""
        return {"edgeKinds": dict(self.edge_kinds), "neighborKinds": dict(self.neighbor_kinds)}


class ObservedNode(_Compact):
    """One observed node — either a resolved neighborhood or a graceful miss."""

    ref: str
    found: bool
    key: str | None = None
    label: str | None = None
    kind: NodeKind | None = None
    source: str | None = None
    degree: int | None = None
    doc: str | None = None
    mode: ObserveMode | None = None
    edges: tuple[ObservedEdge, ...] = ()
    neighbors: tuple[NeighborCard, ...] = ()
    survey: NeighborhoodSurvey | None = None
    hint: str | None = None
    recipes: tuple[RecipeRef, ...] = ()
    memory: tuple[MemoryNote, ...] = ()

    def to_wire(self) -> dict[str, object]:
        """Compact, no-null projection — a miss collapses to ``{ref, found}``."""
        if not self.found:
            return {"ref": self.ref, "found": False}
        out: dict[str, object] = {
            "ref": self.ref,
            "found": True,
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "source": self.source,
            "degree": self.degree,
            "mode": self.mode,
        }
        if self.doc is not None:
            out["doc"] = self.doc
        if self.mode == "kinds_only" and self.survey is not None:
            out["survey"] = self.survey.to_wire()
            if self.hint is not None:
                out["hint"] = self.hint
        else:
            out["edges"] = [e.to_wire() for e in self.edges]
            out["neighbors"] = [n.to_wire() for n in self.neighbors]
        if self.recipes:
            out["recipes"] = [r.to_wire() for r in self.recipes]
        if self.memory:
            out["memory"] = [m.to_wire() for m in self.memory]
        return out


class ObserveResult(_Compact):
    """The whole tool result — the per-node observations in request order."""

    observed: tuple[ObservedNode, ...]

    def to_wire(self) -> dict[str, object]:
        """Compact result: ``{count, observed}``."""
        return {
            "count": len(self.observed),
            "observed": [o.to_wire() for o in self.observed],
        }


# A single directed hop as assembled off the store, pre-projection.
_Hop = tuple["ScgEdge", HopDirection, str]


# ── Tool ─────────────────────────────────────────────────────────────────────


class ScgObserveTool(SessionToolBase):
    """SessionTool: project a node's typed neighborhood for agent navigation.

    Observe → think → navigate: ``scg_route`` seeds entries, ``scg_observe`` reads
    a node's typed hops so the agent (not a second engine score) chooses the next
    step, probes execute, ``scg_memory`` deposits polarity. Read-only and
    ``ScgScope``-respecting (out-of-scope hops are dropped).
    """

    tool_id = "scg_observe"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Read-only — safe to observe several seeds in parallel.
    concurrency_safe = True
    schema = pydantic_to_openai_tool(ScgObserveArgs, name="scg_observe")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Resolve each reference and return its scope-filtered neighborhood."""
        try:
            args = ScgObserveArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        try:
            store = ScgCore.store()
            anchored = self._anchored_notes(store)
            kinds: set[EdgeKind] = set(args.edge_kinds)
            observed = tuple(
                self._observe(store, ref, anchored, args.direction, kinds)
                for ref in args.nodes
            )
        except ImportError:
            return err_result("internal", SCG_CORE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return err_result("internal", str(exc))
        return ok_result(ObserveResult(observed=observed).to_wire())

    # -- per-node projection ------------------------------------------------

    @classmethod
    def _observe(
        cls,
        store: ScgStore,
        ref: str,
        anchored: dict[str, tuple[MemoryNote, ...]],
        direction: Direction,
        edge_kinds: set[EdgeKind],
    ) -> ObservedNode:
        """Project one node reference into its neighborhood, or a miss marker.

        A reference is a ``source_key`` (resolved against the canonical node id) or
        a raw 16-char node id; an unresolvable reference yields a graceful
        ``ObservedNode(found=False)`` so a partly-stale seed list never fails.

        Two-stage: a large, unfiltered neighborhood returns a ``kinds_only``
        survey and the agent re-calls with ``edge_kinds`` for the instance rows; a
        filtered or small neighborhood returns the edge/neighbor rows directly.
        """
        node = cls._resolve(store, ref)
        if node is None:
            return ObservedNode(ref=ref, found=False)

        hops = cls._scoped_hops(store, node, direction)
        if edge_kinds:
            hops = [h for h in hops if h[0].kind in edge_kinds]

        doc = _trim(node.doc, _DOC_SNIPPET_CHARS) if node.doc else None
        recipes = cls._recipes(store, node)
        notes = anchored.get(node.source_key, ())[:_MAX_NOTES]

        if not edge_kinds and len(hops) > _SURVEY_THRESHOLD:
            return ObservedNode(
                ref=ref, found=True, key=node.source_key, label=node.name,
                kind=node.kind, source=node.source_id, degree=len(hops), doc=doc,
                mode="kinds_only", survey=cls._survey(store, hops),
                hint=(
                    "Large neighborhood — re-call with `edge_kinds` (and/or "
                    "`direction`) to retrieve the hops you need."
                ),
                recipes=recipes, memory=notes,
            )
        edges, neighbors = cls._instances(store, hops)
        return ObservedNode(
            ref=ref, found=True, key=node.source_key, label=node.name,
            kind=node.kind, source=node.source_id, degree=len(hops), doc=doc,
            mode="rows", edges=edges, neighbors=neighbors,
            recipes=recipes, memory=notes,
        )

    # -- resolution ---------------------------------------------------------

    @staticmethod
    def _resolve(store: ScgStore, ref: str) -> ScgNode | None:
        """Resolve a reference to a node by source_key OR raw node id.

        A ``source_key`` carries a ``#`` (``<source_id>#<Qualified.Name>``); a raw
        node id does not. The ``#`` form is content-addressed across every kind, so
        try each ``NodeKind`` derivation; fall back to a direct id lookup.
        """
        if "#" in ref:
            return _node_for_key(store, ref)
        return store.get_node(ref)

    # -- hop assembly (scope-filtered, directed) ----------------------------

    @staticmethod
    def _scoped_hops(
        store: ScgStore, node: ScgNode, direction: Direction
    ) -> list[_Hop]:
        """In-scope directed hops off *node* for the requested *direction*.

        Outbound edges come from ``neighbors(source_key)``; the reverse hop is the
        ``list_edges()`` rows whose ``target`` is this node (the router's "one hop
        in both directions"). A hop whose far endpoint is an out-of-scope source is
        dropped (no dangling navigation into a source the workspace never enabled).
        """
        from mewbo_graph.scg.scope import ScgScope  # noqa: PLC0415

        hops: list[_Hop] = []
        if direction in ("outgoing", "both"):
            hops.extend(
                (e, "out", e.target) for e in store.neighbors(node.source_key)
            )
        if direction in ("incoming", "both"):
            hops.extend(
                (e, "in", e.source)
                for e in store.list_edges()
                if e.target == node.source_key
            )
        return [h for h in hops if ScgScope.permits(h[2].split("#", 1)[0])]

    @staticmethod
    def _survey(store: ScgStore, hops: list[_Hop]) -> NeighborhoodSurvey:
        """Stage-1 rollup: edge-kind (by dir) + neighbor-kind histograms.

        The compact "what's here" view returned before the agent commits to a
        filter — counts, never instances, so it stays tiny regardless of degree.
        Just a ``Counter`` over the same ``hops`` list the instance path reads, so
        the two stages never diverge.
        """
        edge_kinds: Counter[str] = Counter()
        neighbor_kinds: Counter[str] = Counter()
        for edge, direction, partner in hops:
            arrow = f"{edge.kind}→" if direction == "out" else f"←{edge.kind}"
            edge_kinds[arrow] += 1
            partner_node = _node_for_key(store, partner)
            neighbor_kinds[partner_node.kind if partner_node else "unmapped"] += 1
        return NeighborhoodSurvey(
            edge_kinds=dict(edge_kinds), neighbor_kinds=dict(neighbor_kinds)
        )

    @classmethod
    def _instances(
        cls, store: ScgStore, hops: list[_Hop]
    ) -> tuple[tuple[ObservedEdge, ...], tuple[NeighborCard, ...]]:
        """Stage-2 rows: typed edge rows + deduped neighbor cards (backstop-capped).

        Neighbors are deduped on ``source_key``; both lists are bounded by
        :data:`_TRUNCATE_BACKSTOP` (a filtered read is normally far under it).
        """
        edge_rows: list[ObservedEdge] = []
        seen: dict[str, NeighborCard] = {}
        for edge, direction, partner in hops[:_TRUNCATE_BACKSTOP]:
            edge_rows.append(
                ObservedEdge(
                    kind=edge.kind, direction=direction, to=partner, weight=edge.weight
                )
            )
            if partner not in seen:
                seen[partner] = cls._neighbor_card(store, partner)
        return tuple(edge_rows), tuple(seen.values())

    @staticmethod
    def _neighbor_card(store: ScgStore, source_key: str) -> NeighborCard:
        """A one-line neighbor descriptor; ``key``-only if the partner is unmapped.

        Best-effort: an edge may point at a partner not (yet) materialised as a
        node (a cross-source ``RESOLVES_TO`` to an unmapped peer), so a missing
        node still yields a ``key``-only card rather than dropping the hop.
        """
        node = _node_for_key(store, source_key)
        if node is None:
            return NeighborCard(key=source_key)
        return NeighborCard(
            key=source_key,
            label=node.name,
            kind=node.kind,
            doc=_trim(node.doc, _DOC_SNIPPET_CHARS) if node.doc else None,
        )

    @staticmethod
    def _recipes(store: ScgStore, node: ScgNode) -> tuple[RecipeRef, ...]:
        """Route recipes threading this node (steps that include its source_key)."""
        out: list[RecipeRef] = []
        for recipe in store.list_recipes(source_id=node.source_id):
            if node.source_key not in recipe.steps:
                continue
            out.append(RecipeRef(key=recipe.source_key, steps=tuple(recipe.steps)))
            if len(out) >= _MAX_RECIPES:
                break
        return tuple(out)

    # -- memory layer (reuse ScgGraphView assembly) -------------------------

    @staticmethod
    def _anchored_notes(store: ScgStore) -> dict[str, tuple[MemoryNote, ...]]:
        """Connector memory notes grouped by the schema ``source_key`` they anchor.

        Reuses :class:`ScgGraphView`'s memory assembly (the ONE place connector
        notes are reconciled to live schema nodes via :class:`ScgAnchorResolver`)
        scoped to the active :class:`ScgScope` allowlist, then indexes each kept
        note under every in-scope schema ``source_key`` it anchors — so a per-node
        lookup is an O(1) dict hit during projection. Best-effort: a graph-only /
        empty memory store yields an empty index (never a raise), mirroring the
        view's own ``_memory_layer`` guard.
        """
        from mewbo_graph.scg.graph_view import ScgGraphView  # noqa: PLC0415
        from mewbo_graph.scg.scope import ScgScope  # noqa: PLC0415

        allowed = ScgScope.allowed()
        scope_ids = (
            sorted(allowed)
            if allowed is not None
            else sorted({n.source_id for n in store.query_nodes()})
        )
        try:
            wiki_store = ScgCore.memory_bridge(store)._store  # noqa: SLF001
            view = ScgGraphView.for_scope(store, wiki_store, scope_ids)
        except Exception:  # noqa: BLE001 — memory layer is best-effort, never fatal
            return {}

        # node_id → source_key, for mapping the view's cross ANCHORS back to keys.
        key_by_id = {n.node_id: n.source_key for n in view.schema_nodes}
        note_by_id = {m.node_id: m for m in view.memory_nodes}
        grouped: dict[str, list[MemoryNote]] = {}
        for note_id, schema_node_id in view.cross_edges:
            key = key_by_id.get(schema_node_id)
            note = note_by_id.get(note_id)
            if key is None or note is None:
                continue
            grouped.setdefault(key, []).append(
                MemoryNote(text=_trim(note.content.strip(), _NOTE_SNIPPET_CHARS))
            )
        return {key: tuple(notes) for key, notes in grouped.items()}


def _node_for_key(store: ScgStore, source_key: str) -> ScgNode | None:
    """Resolve a ``source_key`` to its node by trying each content-addressed kind.

    A ``node_id`` is ``sha1(source_key|kind)``, so the kind is part of identity;
    the one canonical "find the node for this key" lookup (used by resolution,
    neighbor cards, and the survey roll-up) tries each ``NodeKind`` derivation.
    """
    for kind in get_args(NodeKind):
        node = store.get_node(ScgNode.make_id(source_key, kind))
        if node is not None:
            return node
    return None


def _trim(text: str, limit: int) -> str:
    """Single-line, length-capped descriptor (collapse newlines, ellipsise)."""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


__all__ = [
    "Direction",
    "HopDirection",
    "MemoryNote",
    "NeighborCard",
    "NeighborhoodSurvey",
    "ObserveMode",
    "ObserveResult",
    "ObservedEdge",
    "ObservedNode",
    "RecipeRef",
    "ScgObserveArgs",
    "ScgObserveTool",
]
