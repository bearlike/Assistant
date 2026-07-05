"""The Q&A retrieval-trail contract — a bounded, score-ranked access record.

A wiki-qa probe BOTH navigates the code graph (unranked walks over thousands of
edges) AND grounds against ranked search hits. Only the ranked grounding hits +
a handful of confirmation reads belong on the answer's "retrieval details" trail;
unranked graph navigation must NEVER flood it (a real run recorded ~200 nodes
while only ~7 were cited). ``QaAccessRecord`` is the ONE typed shape every
retrieval tool emits and ``QaFinalizer._accessed_from_events`` folds — it carries
an optional ranking ``score`` so the fold can dedupe-by-ref, score-order, and cap
to a tight top-N.

Producer side (the plugin tools, importing this DOWN):
    * scored search hits → :meth:`from_ranked_hits` (drops the marginal tail at a
      ratio of the top hit's score, carrying the real ``score``/``rank``);
    * a navigation seed or a file/page read → :meth:`touch` (unscored).

Consumer side (``mewbo_graph.wiki.qa``, same layer): :meth:`from_event` parses an
``access`` event back into records (tolerating the legacy bare-``refs`` shape) and
:meth:`fold` produces the bounded, ordered ``accessed_sources`` ref list.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict


def _env_int(name: str, default: int) -> int:
    """Read a positive int from *name*; fall back to *default* on absent/garbage."""
    try:
        value = int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float_ratio(name: str, default: float) -> float:
    """Read a ``(0, 1]`` float ratio from *name*; fall back to *default* otherwise."""
    try:
        value = float((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if 0.0 < value <= 1.0 else default


# Top-N cap on the folded trail + the score floor (a ratio of the top hit's score)
# below which a search hit is dropped at record time. Both env-overridable; the
# defaults keep the trail a tight ~dozen high-signal entries without dropping the
# strongest hits.
ACCESS_TOPN: int = _env_int("MEWBO_WIKI_QA_ACCESS_TOPN", 12)
ACCESS_SCORE_RATIO: float = _env_float_ratio("MEWBO_WIKI_QA_ACCESS_SCORE_RATIO", 0.5)


class QaAccessRecord(BaseModel):
    """One entry on the Q&A retrieval trail.

    ``ref`` is ALWAYS present — the citation id grammar (``graph:<node_id>`` /
    ``<path>#L<a>-<b>`` / bare ``<path>`` / ``wiki:<page_id>``) — so downstream
    resolution (``AccessedSourceResolver``) still works. ``score``/``rank`` are set
    only for ranked search hits; ``None`` marks an unscored navigation seed or
    grounding read (file/page), which sort AFTER the scored hits. ``tool``/``op``/
    ``ok`` are provenance for inspecting the trail. ``extra="ignore"`` keeps the
    fold tolerant of an event written by an older or newer producer.
    """

    model_config = ConfigDict(extra="ignore")

    ref: str
    score: float | None = None
    rank: int | None = None
    tool: str = ""
    op: str = ""
    ok: bool = True

    # ── Producer constructors ───────────────────────────────────────────────

    @classmethod
    def touch(
        cls, ref: str, *, tool: str = "", op: str = "read", ok: bool = True
    ) -> QaAccessRecord:
        """An UNSCORED entry — a navigation seed or a grounding file/page read."""
        return cls(ref=ref, tool=tool, op=op, ok=ok)

    @classmethod
    def from_ranked_hits(
        cls,
        ranked: list[tuple[str, float]],
        *,
        tool: str = "",
        ratio: float = ACCESS_SCORE_RATIO,
    ) -> list[QaAccessRecord]:
        """Build records for the hits clearing the score floor (``ratio`` x top score).

        *ranked* is the retriever's already-sorted ``[(ref, score), ...]`` output
        (descending) — the ONE ranking engine; this never re-ranks. Every hit at or
        above ``top_score * ratio`` becomes a record carrying its real ``score`` +
        1-based ``rank``, so a tail of marginal hits never lands on the trail. A
        degenerate non-positive top score keeps only the single best hit as an
        unscored touch; empty input yields nothing.
        """
        if not ranked:
            return []
        top = ranked[0][1]
        if top <= 0:
            return [cls.touch(ranked[0][0], tool=tool, op="search")]
        floor = top * ratio
        out: list[QaAccessRecord] = []
        for index, (ref, score) in enumerate(ranked):
            if score < floor:
                break  # ranked descending → everything after is below the floor
            out.append(cls(ref=ref, score=float(score), rank=index + 1, tool=tool, op="search"))
        return out

    # ── Consumer (fold) ─────────────────────────────────────────────────────

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> list[QaAccessRecord]:
        """Parse one ``access`` event into records, tolerating the legacy shape.

        A current event carries ``records: [{ref, score, ...}, ...]``; a legacy
        event carries ``refs: [str, ...]`` (all unscored). Both fold — a stored
        event missing the new fields still resolves.
        """
        out: list[QaAccessRecord] = []
        for item in event.get("records") or []:
            if isinstance(item, dict) and item.get("ref"):
                try:
                    out.append(cls.model_validate(item))
                except Exception:  # pragma: no cover — defensive: skip a malformed record
                    continue
            elif isinstance(item, str) and item:
                out.append(cls(ref=item))
        for ref in event.get("refs") or []:  # legacy bare-string trail
            if isinstance(ref, str) and ref:
                out.append(cls(ref=ref))
        return out

    @classmethod
    def fold(cls, records: list[QaAccessRecord], *, topn: int = ACCESS_TOPN) -> list[str]:
        """Dedupe by ref (best score wins), score-rank, cap to top-N → ordered refs.

        Scored search hits come first in descending score order; unscored
        navigation seeds and grounding reads follow in first-seen order. The result
        is the bounded, score-ranked retrieval trail surfaced as
        ``QaAnswer.accessed_sources``.
        """
        best: dict[str, QaAccessRecord] = {}
        order: list[str] = []
        for rec in records:
            ref = (rec.ref or "").strip()
            if not ref:
                continue
            prev = best.get(ref)
            if prev is None:
                best[ref] = rec
                order.append(ref)
            elif cls._score_key(rec) > cls._score_key(prev):
                best[ref] = rec  # keep the better-scored sighting of this ref
        deduped = [best[ref] for ref in order]
        scored = sorted(
            (r for r in deduped if r.score is not None),
            key=cls._score_key,
            reverse=True,
        )
        unscored = [r for r in deduped if r.score is None]
        ordered = scored + unscored
        return [r.ref for r in ordered[:topn]]

    @staticmethod
    def _score_key(rec: QaAccessRecord) -> float:
        """Sort key: the score, or ``-inf`` for an unscored entry (sorts last)."""
        return rec.score if rec.score is not None else float("-inf")


__all__ = ["ACCESS_SCORE_RATIO", "ACCESS_TOPN", "QaAccessRecord"]
