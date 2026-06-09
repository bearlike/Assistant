"""Entity resolution — the ONE generalized ladder shared with insight dedup.

``rapidfuzz`` is optional (``mewbo-graph[retrieval]``). When absent, scoring
falls back to cosine-only; ``fuzzy_ratio`` degrades to a 1.0/0.0 exact match so
the ladder still runs, never crashes (optional = extras + import-guard).

The ladder is pure and strategy-injected so the SAME class backs both entity
resolution (block = ANN over entity embeddings; score = ``max(cosine, fuzzy)``)
and insight dedup (block = cosine-kNN; score = exact/fuzzy/llm tiers). It is
NOT coupled to entities — ``EntityResolver`` is just one caller.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mewbo_graph._util import cosine

from .types import Entity, normalize_entity_name

if TYPE_CHECKING:
    from mewbo_graph.wiki.embedder import EmbedderProtocol
    from mewbo_graph.wiki.store import WikiStoreBase

try:  # optional dep — graceful absence (extras + import-guard)
    from rapidfuzz.fuzz import ratio as _rf_ratio

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - exercised only on a lean install
    # Broaden the fallback's type so mypy accepts the None sentinel whether or
    # not rapidfuzz is resolvable at check time (the guard at the call site
    # narrows it back). Keep the guard intact — never strip this.
    _rf_ratio: Callable[..., float] | None = None  # type: ignore[no-redef]
    _HAS_RAPIDFUZZ = False


def fuzzy_ratio(a: str, b: str) -> float:
    """Normalized [0,1] string similarity; exact-match fallback when rapidfuzz absent."""
    if _HAS_RAPIDFUZZ and _rf_ratio is not None:
        return float(_rf_ratio(a, b)) / 100.0
    return 1.0 if a == b else 0.0


@dataclass(frozen=True)
class LadderDecision:
    """Verdict from the two-threshold ladder."""

    action: Literal["merge", "flag", "new"]
    target_id: str | None = None
    score: float = 0.0


class ResolutionLadder:
    """Generic block → hybrid-score → decide ladder shared by entities AND dedup.

    Strategies are injected so the SAME ladder drives entity resolution and
    insight dedup. Recommendation priors are consulted BEFORE the threshold
    decision: a ``merge`` prior on a pair forces a merge (and lifts the score to
    at least ``auto_merge``), a ``distinct`` prior forbids it; other actions are
    left for the caller to surface.

    Args:
        block: ``key -> [(candidate_id, candidate_label), ...]`` blocking step.
        score: ``(key, candidate_id) -> float`` hybrid similarity in [0,1].
        identity: ``candidate_id -> canonical_key`` for matching recommendations.
        auto_merge: ``>= auto_merge`` ⇒ merge.
        flag: ``flag <= s < auto_merge`` ⇒ flag (needs_review); ``< flag`` ⇒ new.
        recommendations: ``frozenset({key, canonical}) -> action`` priors.
    """

    def __init__(
        self,
        *,
        block: Callable[[str], list[tuple[str, str]]],
        score: Callable[[str, str], float],
        identity: Callable[[str], str],
        auto_merge: float = 0.9,
        flag: float = 0.6,
        recommendations: Mapping[frozenset[str], str] | None = None,
    ) -> None:
        """Wire the injected strategies + thresholds + recommendation priors."""
        self._block = block
        self._score = score
        self._identity = identity
        self._auto = auto_merge
        self._flag = flag
        self._recs = dict(recommendations or {})

    def decide(self, key: str) -> LadderDecision:
        """Resolve *key* against its blocked candidates with priors applied."""
        ranked: list[tuple[str, float]] = []
        for cand_id, _label in self._block(key):
            prior = self._recs.get(frozenset({key, self._identity(cand_id)}))
            if prior == "distinct":
                continue  # forced separation — never a candidate
            s = self._score(key, cand_id)
            if prior == "merge":
                return LadderDecision("merge", cand_id, max(s, self._auto))
            ranked.append((cand_id, s))
        ranked.sort(key=lambda t: t[1], reverse=True)
        if not ranked:
            return LadderDecision("new")
        best_id, best_s = ranked[0]
        if best_s >= self._auto:
            return LadderDecision("merge", best_id, best_s)
        if best_s >= self._flag:
            return LadderDecision("flag", best_id, best_s)
        return LadderDecision("new")


class EntityResolver:
    """Resolve a candidate entity against the store via the shared ladder.

    Block = ANN top-k over entity embeddings (the ``entity_vector_search`` seam)
    UNION the existing same-slug entities (so fuzzy can fire without a vector).
    Score = ``max(cosine, fuzzy_ratio(normalized_name))``. Decide via
    ``ResolutionLadder``: ``>=auto_merge`` merge / ``flag<=s<auto`` flag / ``<flag``
    new. Persisted ``EntityRecommendation``s are folded in as priors.
    """

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol,
        auto_merge: float = 0.85,
        flag: float = 0.6,
        block_k: int = 10,
    ) -> None:
        """Compose over an injected store + embedder; thresholds are tunable."""
        self._store = store
        self._embedder = embedder
        self._auto = auto_merge
        self._flag = flag
        self._k = block_k

    def resolve(self, slug: str, candidate: Entity) -> LadderDecision:
        """Return a ladder decision for *candidate* against existing entities."""
        try:
            qvec = self._embedder.embed_query(candidate.name)
        except Exception:
            qvec = []
        cand_vecs: dict[str, list[float]] = {}
        if qvec:
            for emb in self._store.entity_vector_search(slug, qvec, k=self._k):
                cand_vecs[emb.entity_id] = emb.vector
        # Always include same-slug existing entities so fuzzy can fire vectorless;
        # never the candidate itself (a fresh surface that has not been written).
        existing = {
            e.id: e for e in self._store.query_entities(slug) if e.id != candidate.id
        }
        priors = self._recommendation_priors(slug)

        def block(_key: str) -> list[tuple[str, str]]:
            ids = set(cand_vecs) | set(existing)
            return [(eid, existing[eid].normalized_name) for eid in ids if eid in existing]

        def score(_key: str, eid: str) -> float:
            cos = cosine(qvec, cand_vecs[eid]) if eid in cand_vecs and qvec else 0.0
            fz = fuzzy_ratio(candidate.normalized_name, existing[eid].normalized_name)
            return max(cos, fz)

        ladder = ResolutionLadder(
            block=block,
            score=score,
            identity=lambda eid: eid,
            auto_merge=self._auto,
            flag=self._flag,
            recommendations=priors,
        )
        return ladder.decide(candidate.id)

    def _recommendation_priors(self, slug: str) -> dict[frozenset[str], str]:
        """Fold persisted recommendations into ``{frozenset(pair): action}`` priors.

        The ladder keys priors on entity *ids*, but the page-writer surfaces
        ``subjects`` as canonical ``"<name>|<type>"`` strings (see
        ``wiki-page-writer.md``). Normalize each subject to its deterministic id
        so a ``merge``/``distinct`` prior actually matches — an id passes through
        unchanged, a ``name|type`` key is resolved via ``Entity.compute_id``.
        """
        priors: dict[frozenset[str], str] = {}
        for rec in self._store.get_entity_recommendations(slug):
            if rec.action in {"merge", "distinct"} and len(rec.subjects) == 2:
                pair = frozenset(self._subject_to_id(s) for s in rec.subjects)
                priors[pair] = rec.action
        return priors

    @staticmethod
    def _subject_to_id(subject: str) -> str:
        """Normalize a recommendation subject to a deterministic entity id.

        A ``"<name>|<type>"`` key is converted to ``Entity.compute_id`` (reusing
        the same normalization + hash the minter uses); an already-derived id
        (no ``|``) passes through untouched.
        """
        name, sep, type_ = subject.partition("|")
        if not sep:
            return subject  # already an entity id
        return Entity.compute_id(normalize_entity_name(name), type_)


__all__ = ["EntityResolver", "LadderDecision", "ResolutionLadder", "fuzzy_ratio"]
