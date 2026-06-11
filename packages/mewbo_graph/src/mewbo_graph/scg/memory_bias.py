"""Memory-aware routing bias over the SCG learned layer (#76).

``docs/features-search.md``: "Before each query, the top-k relevant memory notes
are retrieved via vector search and surfaced to ``scg_route``, biasing routing
toward pathways that have produced results and away from dead ends already
discovered." This module is the retrieval-plus-arithmetic that realises that one
sentence — and **nothing more**: it is a *zero-LLM* step (a vector read + a sum),
so the router's deterministic ``cosine + edge weight`` core stays intact.

The flow, per query:

1. embed the query (the router already did this — the vector is handed in);
2. read the top-k connector-corpus notes + their live anchors + cosine score
   via :meth:`ScgMemoryBridge.read_anchored_insights`;
3. fold each note's ``score × polarity`` onto every ``source_key`` it anchors —
   positive notes ADD, dead-end notes SUBTRACT (a pathway already discovered to
   return nothing is damped, not hidden);
4. expose, per ``source_key``: the blended boost (added to the recipe rank) AND
   the short anchored hint texts (parameter-usage guidance the probe reads
   without a second lookup).

Scope-respecting: a note whose anchor's source id is outside the ambient
:class:`ScgScope` contributes NOTHING — out-of-workspace learning never biases
an in-workspace route (mirrors the router's recipe-step filter).

Best-effort: if no embedding backend is configured the bridge's vector search
returns nothing, so the bias is empty and routing degrades to structure-alone —
exactly the "best-effort embedding" stance the docs describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .memory_bridge import polarity_of
from .scope import ScgScope
from .types import SourceKey

if TYPE_CHECKING:
    from .memory_bridge import ScgMemoryBridge

# ── Boost weights (named constants, each with a one-line rationale) ──────────
#
# A note's cosine score ∈ [0, 1] is multiplied by one of these and SUMMED onto
# the anchored capability's recipe rank. The router's seed similarity is also
# ∈ [0, 1] with a small additive edge weight (~1.0), so a single strong positive
# note (~+0.6) can reorder near-ties without swamping a genuinely better seed
# match — the docs' "bias", not "override".
_POSITIVE_WEIGHT = 0.6  # a pathway that produced results gets a real, bounded lift.
# Dead ends are damped HARDER than positives lift (asymmetric, by design): a
# pathway already discovered to return nothing is worse than an unknown one, so
# its penalty must be able to push it below an unbiased sibling.
_DEAD_END_WEIGHT = -0.8
# Per-recipe hint cap — keeps the route-result payload compact (the probe needs
# a few usage hints, not the whole anchored corpus).
_MAX_HINTS_PER_KEY = 3
# Hint text cap — notes are already ≤200 chars, but a defensive ceiling keeps
# one over-long note from bloating the projection.
_MAX_HINT_CHARS = 200


@dataclass(frozen=True, slots=True)
class CapabilityHint:
    """One anchored connector insight surfaced to a routed recipe.

    ``source_key`` is the ``<source_id>#<Qualified.Name>`` the insight hangs off;
    ``text`` is the (capped) note content — "how to call this right" guidance the
    probe reads inline, no second memory lookup.
    """

    source_key: SourceKey
    text: str


@dataclass(slots=True)
class _KeyBias:
    """Accumulator for one capability ``source_key`` during a fold."""

    boost: float = 0.0
    hints: list[CapabilityHint] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ScgMemoryBias:
    """The per-``source_key`` boost + hint map a query's learned memory yields.

    Built by :meth:`for_query` (a vector read + a polarity-weighted fold, no LLM).
    :meth:`boost_for_steps` returns the additive rank contribution for a recipe's
    pathway (the max over its steps — one strong anchored capability lifts the
    whole pathway); :meth:`hints_for_steps` returns the capped anchored hints for
    the orchestrator/probes. Immutable once built — safe to reuse across the
    recipes of one route call.
    """

    # source_key → (blended boost, anchored hints). Empty when memory is absent
    # or every note fell out of the ambient scope.
    by_key: dict[SourceKey, _KeyBias]

    @classmethod
    def empty(cls) -> ScgMemoryBias:
        """The no-bias map — routing falls back to structure-alone."""
        return cls(by_key={})

    @classmethod
    def for_query(
        cls,
        bridge: ScgMemoryBridge,
        slug: str,
        query_vec: list[float],
        *,
        k: int = 10,
    ) -> ScgMemoryBias:
        """Retrieve top-``k`` connector notes and fold them into a bias map.

        Pure retrieval + arithmetic (zero-LLM). Each note's cosine score is
        weighted by its polarity and summed onto every ``source_key`` it anchors,
        respecting the ambient :class:`ScgScope` (out-of-scope anchors skipped).
        A note with no resolvable anchor contributes nothing (it can't name a
        capability to bias).
        """
        try:
            rows = bridge.read_anchored_insights(slug, query_vec, k=k)
        except Exception:  # noqa: BLE001 — bias is best-effort; never fail a route
            return cls.empty()

        by_key: dict[SourceKey, _KeyBias] = {}
        for note, score, anchors in rows:
            polarity = polarity_of(note)
            weight = _POSITIVE_WEIGHT if polarity == "positive" else _DEAD_END_WEIGHT
            contribution = score * weight
            text = note.content.strip()[:_MAX_HINT_CHARS]
            for source_key in anchors:
                source_id = source_key.split("#", 1)[0]
                if not ScgScope.permits(source_id):
                    continue  # out-of-workspace learning never biases this route
                bias = by_key.setdefault(source_key, _KeyBias())
                bias.boost += contribution
                # Only positive notes carry parameter-usage hints — a dead end is
                # not "how to call this right", and the cap stays for the useful kind.
                if polarity == "positive" and len(bias.hints) < _MAX_HINTS_PER_KEY:
                    bias.hints.append(CapabilityHint(source_key=source_key, text=text))
        return cls(by_key=by_key)

    # ── Read surface (router + plugin projection) ───────────────────────────

    def boost_for_steps(self, steps: list[SourceKey]) -> float:
        """Additive rank boost for a recipe whose pathway visits *steps*.

        The MAX over the pathway's per-step boosts: one capability the learned
        layer marks as productive lifts the whole recipe (and one marked a dead
        end damps it), rather than averaging the signal away across steps that
        memory has nothing to say about. ``0.0`` when no step is biased.
        """
        boosts = [self.by_key[s].boost for s in steps if s in self.by_key]
        return max(boosts) if boosts else 0.0

    def hints_for_steps(self, steps: list[SourceKey]) -> list[CapabilityHint]:
        """The capped anchored hints for a recipe's pathway (deduped, ordered).

        Deterministic order (by step order then insertion order) and globally
        capped so the route-result projection stays compact regardless of how
        many notes anchor the pathway.
        """
        out: list[CapabilityHint] = []
        seen: set[tuple[str, str]] = set()
        for step in steps:
            bias = self.by_key.get(step)
            if bias is None:
                continue
            for hint in bias.hints:
                ident = (hint.source_key, hint.text)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(hint)
                if len(out) >= _MAX_HINTS_PER_KEY:
                    return out
        return out


__all__ = ["CapabilityHint", "ScgMemoryBias"]
