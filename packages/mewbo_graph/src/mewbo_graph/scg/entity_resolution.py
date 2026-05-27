"""Type-level cross-source entity resolution — spec §6 (abstain-by-default).

:class:`TypeAligner` runs **at map time**: it compares ``entity_type`` nodes
*across* the given sources and deposits durable ``RESOLVES_TO`` edges
(``method="type_align"``) for the type correspondences it is confident about —
e.g. ``Jira.Issue <=> Linear.Ticket``. The edge is a *weighted, provenanced
hypothesis* (Graphiti validity window already on :class:`ScgEdge`), never an
asserted truth: traversal weighs it, it is not a hard join.

**Abstain by default.** An edge is emitted only on *positive* evidence:

* a name/field-overlap heuristic produces a similarity in ``[0, 1]``;
* pairs at/above ``confident_threshold`` are emitted on the heuristic alone;
* pairs in the ambiguous *band* (``band_low..confident_threshold``) are emitted
  **only** if an *injected* LLM affirms them (one call per band pair); with no
  LLM injected, band pairs abstain (NONE-default — mirrors the memory layer's
  dedup tier-3 stance);
* everything below ``band_low`` abstains.

Cross-source only: same-source pairs and non-``entity_type`` nodes are skipped.

**Instance-level ER is explicitly NOT here.** Resolving two concrete records
("is Jira issue #42 the same work item as Linear ticket ENG-7?") happens
*online*, inside the probe agent, which keys-blocks and selects natively over
live data. This class owns only the offline, *type-level* schema correspondence
that scopes where the probe agent should even look — never the data behind it.

Security invariant (spec §6): operates purely over SCG structure nodes, which
carry only redacted descriptors — no token, credential, or record value.
"""

from __future__ import annotations

from collections.abc import Callable

from mewbo_core.config import get_config_value

from .store import ScgStore
from .types import ScgEdge, ScgNode, SourceKey, field_leaf

# Calibrated thresholds for the abstain-by-default heuristic, read once from
# ``scg.entity_resolution`` config (these are the code defaults). High bar to
# emit on the heuristic alone; a band below it where one LLM call decides; a
# floor under which we abstain entirely.
_DEFAULT_CONFIDENT_THRESHOLD = 0.6
_DEFAULT_BAND_LOW = 0.15


class TypeAligner:
    """Map-time, type-level cross-source entity resolver (abstain-by-default).

    Dependency-injected: a :class:`ScgStore` to read ``entity_type`` nodes from
    and persist hypothesis edges into, plus an optional ``Callable[[str], str]``
    that disambiguates only the ambiguous *band* (the confident and reject tiers
    never spend a token). Thresholds are read once from ``scg.entity_resolution``
    config with calibrated code defaults, so the whole feature stays gated and
    tunable without editing this class.
    """

    _PROMPT = (
        "Two entity types from different data sources may describe the SAME "
        "real-world concept. Reply with exactly 'yes' or 'no'.\n"
        "A: {a_source}.{a_name} fields={a_fields}\n"
        "B: {b_source}.{b_name} fields={b_fields}\n"
        "Do A and B describe the same kind of entity? Answer yes or no."
    )

    def __init__(
        self, *, store: ScgStore, llm: Callable[[str], str] | None = None
    ) -> None:
        """Inject the store and (optionally) the band-disambiguation LLM."""
        self._store = store
        self._llm = llm
        # Per-``align`` memo of ``source_key -> field-name set``. ``_field_names``
        # depends only on the node, but is read 4-6× per cross-source pair (an
        # O(pairs) loop), and each miss scans the whole edge file on the JSON
        # backend. Compute once per node, reuse for every pair (spec: compute once).
        self._fields_cache: dict[SourceKey, set[str]] = {}
        self._confident = float(
            get_config_value(
                "scg",
                "entity_resolution",
                "confident_threshold",
                default=_DEFAULT_CONFIDENT_THRESHOLD,
            )
        )
        self._band_low = float(
            get_config_value(
                "scg", "entity_resolution", "band_low", default=_DEFAULT_BAND_LOW
            )
        )

    # -- public API ---------------------------------------------------------

    def align(self, source_ids: list[str]) -> list[ScgEdge]:
        """Emit durable ``RESOLVES_TO`` edges for confident type correspondences.

        Compares every cross-source pair of ``entity_type`` nodes drawn from
        *source_ids*, abstains by default, and upserts the surviving hypothesis
        edges into the store. Returns the edges emitted (empty when none clear
        the bar). Deterministic for a fixed store + injected LLM.
        """
        # Fresh per-align memo so a re-map sees the current graph, not a stale set.
        self._fields_cache = {}
        types_by_source = {sid: self._entity_types(sid) for sid in dict.fromkeys(source_ids)}
        edges: list[ScgEdge] = []
        sources = sorted(types_by_source)
        for i, left in enumerate(sources):
            for right in sources[i + 1 :]:
                for a in types_by_source[left]:
                    for b in types_by_source[right]:
                        edge = self._resolve_pair(a, b)
                        if edge is not None:
                            edges.append(edge)
        if edges:
            self._store.upsert_edges(edges)
        return edges

    # -- per-pair decision --------------------------------------------------

    def _resolve_pair(self, a: ScgNode, b: ScgNode) -> ScgEdge | None:
        """Decide one cross-source type pair; return an edge or None (abstain)."""
        score = self._similarity(a, b)
        if score >= self._confident:
            return self._edge(a, b, weight=score, evidence=self._evidence(a, b, score))
        if score >= self._band_low and self._llm_affirms(a, b):
            evidence = [*self._evidence(a, b, score), "llm: affirmed band pair"]
            # Damp the weight: an LLM-promoted band pair is a softer hypothesis
            # than a heuristic-confident one (calibrated, capped at confident).
            return self._edge(a, b, weight=min(score + 0.1, self._confident), evidence=evidence)
        return None

    def _llm_affirms(self, a: ScgNode, b: ScgNode) -> bool:
        """Ask the injected LLM once whether the band pair is the same type."""
        if self._llm is None:
            return False  # NONE-default: no LLM -> abstain on band pairs.
        prompt = self._PROMPT.format(
            a_source=a.source_id,
            a_name=a.name,
            a_fields=sorted(self._field_names(a)),
            b_source=b.source_id,
            b_name=b.name,
            b_fields=sorted(self._field_names(b)),
        )
        return self._llm(prompt).strip().lower().startswith("y")

    # -- heuristic ----------------------------------------------------------

    def _similarity(self, a: ScgNode, b: ScgNode) -> float:
        """Blend name + field-overlap into a calibrated ``[0, 1]`` similarity.

        Field overlap (Jaccard over field names) is the strong signal; an exact
        name match adds a bounded bonus. Names alone never clear the confident
        bar — schema overlap is what makes a type correspondence executable.
        """
        field_sim = self._jaccard(self._field_names(a), self._field_names(b))
        name_bonus = 0.2 if a.name.lower() == b.name.lower() else 0.0
        return min(field_sim + name_bonus, 1.0)

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard similarity of two name sets (0.0 if both empty)."""
        if not a and not b:
            return 0.0
        union = a | b
        return len(a & b) / len(union) if union else 0.0

    def _field_names(self, node: ScgNode) -> set[str]:
        """The field *names* of an entity type (last ``.`` segment, lower-cased).

        Memoized per ``source_key`` for the duration of one ``align`` so the
        whole-edge-file scan below runs once per node, not once per pair.

        Freshly-parsed sources (OpenAPI / MCP) emit each field as a separate
        ``field`` node linked by a ``HAS_FIELD`` edge and leave
        ``entity_type.bindings`` empty — so the field set is derived from the
        entity type's ``HAS_FIELD`` neighbours in the graph. Falls back to
        ``bindings`` (the parser's In-N-Out producer/consumer pass populates
        them) when no ``HAS_FIELD`` edges are present, so ER fires on both the
        parsed-graph and the binding-bearing path.
        """
        cached = self._fields_cache.get(node.source_key)
        if cached is not None:
            return cached
        names = {
            field_leaf(edge.target)
            for edge in self._store.neighbors(node.source_key)
            if edge.kind == "HAS_FIELD"
        }
        if not names:
            names = {field_leaf(binding.field_key) for binding in node.bindings}
        self._fields_cache[node.source_key] = names
        return names

    def _evidence(self, a: ScgNode, b: ScgNode, score: float) -> list[str]:
        """Human-readable provenance strings for the emitted edge."""
        shared = sorted(self._field_names(a) & self._field_names(b))
        out = [f"field_overlap={score:.2f}"]
        if shared:
            out.append(f"shared_fields={shared}")
        if a.name.lower() == b.name.lower():
            out.append(f"name_match={a.name}")
        return out

    # -- node loading + edge construction -----------------------------------

    def _entity_types(self, source_id: str) -> list[ScgNode]:
        """Deterministically-ordered ``entity_type`` nodes for one source."""
        nodes = self._store.query_nodes(source_id=source_id, kind="entity_type")
        return sorted(nodes, key=lambda n: n.source_key)

    @staticmethod
    def _edge(
        a: ScgNode, b: ScgNode, *, weight: float, evidence: list[str]
    ) -> ScgEdge:
        """Build a canonically-oriented ``RESOLVES_TO`` hypothesis edge."""
        source, target = sorted((a.source_key, b.source_key))
        binds: tuple[SourceKey, SourceKey] = (source, target)
        return ScgEdge(
            source=source,
            target=target,
            kind="RESOLVES_TO",
            weight=weight,
            binds=binds,
            method="type_align",
            evidence=evidence,
        )


__all__ = ["TypeAligner"]
