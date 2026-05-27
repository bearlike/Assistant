"""Insight ingestion — the DRY write core behind all memory surfaces.

One ``InsightIngestor`` backs the SessionTool, the REST endpoint, and the MCP
tool (Gitea #13 §8). Given a claim (or raw text to condense) plus optional
anchors, it: condenses → embeds → resolves/auto-resolves anchors → runs the
3-tier dedup/merge ladder → upserts node + embedding + edges. Every
collaborator (store, embedder, structure provider, deduper, condenser, clock)
is constructor-injected so the core is unit-testable with stubs only at the
LLM/embedding I/O boundary.

Atomicity is load-bearing: a condensed blob becomes *several* ≤200-char notes,
and a merge keeps the crisper of two overlapping notes (Molecular Facts /
AtomicRAG) rather than concatenating them.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .embedder import Embedder, EmbedderProtocol
from .memory_types import (
    MAX_INSIGHT_CHARS,
    EntityKey,
    MemoryEdge,
    MemoryEmbedding,
    MemoryFilter,
    MemoryKind,
    MemoryNode,
    MemoryProvenance,
    MemorySource,
)

if TYPE_CHECKING:
    from .store import WikiStoreBase


@runtime_checkable
class AnchorResolver(Protocol):
    """The node-agnostic anchor seam the ingestor depends on.

    The ingestor only needs to map a ``node_id`` back to its ``entity_key`` and
    to test which anchor keys resolve to a *live* unit — it never inspects the
    resolved node itself. Typing that surface as ``Mapping[..., object]`` (a
    covariant value) lets any corpus's provider satisfy it: the wiki
    ``CodeStructureProvider`` (code graph) and the SCG ``ScgAnchorResolver``
    (connector graph) both conform without a cast, so an alternate corpus plugs
    its own node type in cleanly. ``StructureProvider`` is a structural subtype.
    """

    def resolve_many(
        self, slug: str, entity_keys: list[EntityKey]
    ) -> Mapping[EntityKey, object]:
        """Resolve a batch of anchor keys; misses are omitted from the result."""
        ...

    def entity_key_of(self, slug: str, node_id: str) -> EntityKey | None:
        """Return the ``entity_key`` for a resolved ``node_id``, or None."""
        ...

_CFG = ConfigDict(extra="forbid", populate_by_name=True)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string.

    The single clock used to stamp memory provenance and refresh timestamps;
    imported by ``refresh.py`` so both layers agree on the format.
    """
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokens(text: str, min_len: int) -> set[str]:
    """Lowercased alnum tokens of length ≥ *min_len* (noise-filtered)."""
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= min_len}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets (0.0 if either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def llm_text(llm: Any, prompt: str) -> str:
    """Invoke a chat model and coerce its reply to plain text."""
    resp = llm.invoke(prompt)
    content = getattr(resp, "content", resp)
    return content if isinstance(content, str) else str(content)


# ── result wire model ───────────────────────────────────────────────────────


class IngestedClaim(BaseModel):
    """Outcome for a single atomic claim processed by the ingestor."""

    model_config = _CFG

    action: Literal["created", "merged", "linked", "rejected"]
    content: str
    node_id: str | None = None
    tier: str | None = None
    anchors: list[EntityKey] = []
    warnings: list[str] = []


class IngestResult(BaseModel):
    """Aggregate result of one ``ingest`` call (one+ claims)."""

    model_config = _CFG

    claims: list[IngestedClaim]

    @property
    def ok(self) -> bool:
        """True if at least one claim was stored (not all rejected)."""
        return any(c.action != "rejected" for c in self.claims)


# ── condenser (raw → atomic claims) ─────────────────────────────────────────

_CONDENSE_PROMPT = (
    "Decompose the following note into the smallest possible set of atomic,"
    " self-contained factual claims about the codebase. Each claim must stand"
    " alone, name its subject explicitly (no pronouns), and be at most {cap}"
    " characters. Return one claim per line, no numbering.\n\nNOTE:\n{raw}"
)


class InsightCondenser:
    """LLM decomposition of raw text into atomic claims (raw path only)."""

    def __init__(self, llm: Any) -> None:
        """Inject a chat model (``.invoke`` → text)."""
        self._llm = llm

    def condense(self, raw: str) -> list[str]:
        """Return atomic claims for *raw*. May raise — callers treat as non-fatal."""
        text = llm_text(self._llm, _CONDENSE_PROMPT.format(cap=MAX_INSIGHT_CHARS, raw=raw))
        claims: list[str] = []
        for line in text.splitlines():
            cleaned = line.strip().lstrip("-*0123456789.) ").strip()
            if cleaned:
                claims.append(cleaned)
        return claims


# ── dedup ladder ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DedupDecision:
    """Verdict from the dedup ladder for a candidate note."""

    action: Literal["new", "merge", "link"]
    target_node_id: str | None = None
    tier: str | None = None


_DEDUP_PROMPT = (
    "Two atomic memory notes about a codebase:\n  A (new): {a}\n  B (existing):"
    " {b}\nReply with exactly one word: MERGE if they state the same fact, LINK"
    " if they are related but distinct, or NEW if unrelated."
)


class InsightDeduper:
    """3-tier dedup/merge: exact node_id → fuzzy Jaccard → LLM over cosine-kNN."""

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        llm: Any = None,
        fuzzy_jaccard: float = 0.85,
        dedup_k: int = 5,
        dedup_cosine: float = 0.6,
        min_token_len: int = 3,
    ) -> None:
        """Inject the store; *llm* optional (tier-3 degrades to NEW).

        Similarity uses the stateless ``Embedder.cosine`` + the store's
        ``memory_vector_search``, so no embedder instance is needed here.
        """
        self._store = store
        self._llm = llm
        self._fuzzy_jaccard = fuzzy_jaccard
        self._dedup_k = dedup_k
        self._dedup_cosine = dedup_cosine
        self._min_token_len = min_token_len

    def classify(
        self, slug: str, candidate: MemoryNode, *, candidate_vec: list[float] | None = None
    ) -> DedupDecision:
        """Classify *candidate* against existing notes (NONE-default → NEW).

        SCALE: tiers 2+3 run over the cosine-kNN candidate set (the single
        ``memory_vector_search`` ANN seam), NOT the whole store — so dedup is
        ``O(dedup_k)`` similarity work per ingest, and upgrading that one seam
        to a real ANN index makes the entire ladder sublinear. (A lexical
        near-duplicate is also an embedding near-duplicate, so scoping fuzzy
        to the kNN set never misses one.) Only when no embedding is available
        (BM25-only) does it fall back to a bounded full scan.
        """
        # Tier 1 — exact: identical normalized content ⇒ identical node_id
        # (indexed point lookup, never a scan).
        if self._store.get_memory_node(slug, candidate.node_id) is not None:
            return DedupDecision("merge", candidate.node_id, tier="exact")

        ranked = self._nearest(slug, candidate, candidate_vec)
        candidates = [n for n, _ in ranked]

        # Tier 2 — fuzzy: high lexical overlap among the kNN candidates.
        cand_tokens = _tokens(candidate.content, self._min_token_len)
        if cand_tokens:
            for node in candidates:
                if node.node_id == candidate.node_id:
                    continue
                other = _tokens(node.content, self._min_token_len)
                if _jaccard(cand_tokens, other) >= self._fuzzy_jaccard:
                    return DedupDecision("merge", node.node_id, tier="fuzzy")

        # Tier 3 — LLM over the nearest candidate above the cosine floor.
        if candidate_vec is not None and self._llm is not None:
            above = [(n, c) for n, c in ranked if c >= self._dedup_cosine]
            if above:
                target = above[0][0]
                verdict = self._decide(candidate, target)
                if verdict == "merge":
                    return DedupDecision("merge", target.node_id, tier="llm")
                if verdict == "link":
                    return DedupDecision("link", target.node_id, tier="llm")
        return DedupDecision("new", tier="new")

    def _nearest(
        self, slug: str, candidate: MemoryNode, candidate_vec: list[float] | None
    ) -> list[tuple[MemoryNode, float]]:
        """Return ``(node, cosine)`` candidates for the dedup tiers.

        With an embedding: the cosine-kNN set via the ANN seam (bounded). Without
        one (BM25-only): a full scan paired with a 0.0 score so the fuzzy tier
        still runs and tier-3 (which needs a vector) is naturally skipped.
        """
        if candidate_vec is None:
            return [
                (n, 0.0)
                for n in self._store.query_memory(slug, filt=MemoryFilter())
                if n.node_id != candidate.node_id
            ]
        out: list[tuple[MemoryNode, float]] = []
        for emb in self._store.memory_vector_search(
            slug, candidate_vec, k=self._dedup_k, filt=MemoryFilter()
        ):
            if emb.node_id == candidate.node_id:
                continue
            node = self._store.get_memory_node(slug, emb.node_id)
            if node is not None:
                out.append((node, Embedder.cosine(candidate_vec, emb.vector)))
        return out

    def _decide(self, candidate: MemoryNode, target: MemoryNode) -> str:
        """Ask the LLM merge|link|new; any failure defaults to ``new``."""
        try:
            text = llm_text(
                self._llm,
                _DEDUP_PROMPT.format(a=candidate.content, b=target.content),
            ).strip().lower()
        except Exception:
            return "new"
        if "merge" in text:
            return "merge"
        if "link" in text:
            return "link"
        return "new"


# ── ingestor ────────────────────────────────────────────────────────────────


class InsightIngestor:
    """DRY write core: condense → embed → anchor → dedup/merge → upsert."""

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol,
        provider: AnchorResolver,
        deduper: InsightDeduper,
        condenser: InsightCondenser | None = None,
        clock: Any = None,
        max_anchors: int = 8,
        max_chars: int = MAX_INSIGHT_CHARS,
        auto_anchor_k: int = 3,
    ) -> None:
        """Wire collaborators (all injected); *condenser* is optional."""
        self._store = store
        self._embedder = embedder
        self._provider = provider
        self._deduper = deduper
        self._condenser = condenser
        self._clock = clock or utc_now_iso
        self._max_anchors = max_anchors
        self._max_chars = max_chars
        self._auto_anchor_k = auto_anchor_k

    @classmethod
    def from_store(
        cls,
        store: WikiStoreBase,
        *,
        embedder: EmbedderProtocol | None = None,
        llm: Any = None,
        condenser: InsightCondenser | None = None,
        clock: Any = None,
        provider: AnchorResolver | None = None,
    ) -> InsightIngestor:
        """Build an ingestor with the standard collaborators (DRY across surfaces).

        The single construction path shared by the SessionTool, the REST
        endpoint, and the MCP tool, so every surface dedups/anchors
        identically. ``embedder`` defaults to the wiki ``Embedder`` (BM25-only
        ``_NullEmbedder`` when none can be built); ``llm``/``condenser`` are
        opt-in (the in-session tool leaves them off — agents pre-atomize).
        ``provider`` overrides the default ``CodeStructureProvider`` so an
        alternate corpus (e.g. the SCG connector graph) can resolve its own
        anchors; ``None`` keeps the code-graph default (backward-compatible).
        """
        from .structure_provider import CodeStructureProvider

        if embedder is None:
            from .embedder import make_embedder_or_none

            embedder = make_embedder_or_none() or _NullEmbedder()
        return cls(
            store=store,
            embedder=embedder,
            provider=provider or CodeStructureProvider(store),
            deduper=InsightDeduper(store=store, llm=llm),
            condenser=condenser,
            clock=clock,
        )

    def ingest(
        self,
        slug: str,
        content: str | None = None,
        *,
        raw: str | None = None,
        anchors: list[EntityKey] | None = None,
        links: list[str] | None = None,
        kind: MemoryKind = "propositional",
        labels: list[str] | None = None,
        corpus: str = "code",
        condense: bool = False,
        source: MemorySource = "indexer",
        author_agent: str = "insight",
        session_id: str | None = None,
    ) -> IngestResult:
        """Ingest a claim (or condensed raw blob) into the memory graph."""
        base_anchors = list(anchors or [])
        links = list(links or [])
        labels = list(labels or [])
        use_raw = raw is not None or condense
        claims, condense_warnings = self._claims_for(raw if raw is not None else content, use_raw)

        outcomes: list[IngestedClaim] = []
        if not claims:
            return IngestResult(
                claims=[
                    IngestedClaim(
                        action="rejected",
                        content=(raw or content or ""),
                        warnings=condense_warnings,
                    )
                ]
            )
        for claim in claims:
            outcomes.append(
                self._ingest_claim(
                    slug,
                    claim,
                    base_anchors=base_anchors,
                    links=links,
                    kind=kind,
                    labels=labels,
                    corpus=corpus,
                    source=source,
                    author_agent=author_agent,
                    session_id=session_id,
                    auto_anchor=use_raw,
                    seed_warnings=condense_warnings,
                )
            )
        return IngestResult(claims=outcomes)

    # -- claim derivation ----------------------------------------------------

    def _claims_for(self, text: str | None, use_raw: bool) -> tuple[list[str], list[str]]:
        """Split input into atomic claims; return (claims, warnings)."""
        text = (text or "").strip()
        if not text:
            return [], ["empty insight"]
        if not use_raw:
            return [text], []
        if self._condenser is not None:
            try:
                claims = [c for c in self._condenser.condense(text) if c.strip()]
                if claims:
                    return claims, []
            except Exception:
                pass  # non-fatal — fall through to single-claim fallback
        if len(text) <= self._max_chars:
            return [text], ["condenser unavailable; stored raw as a single claim"]
        return [], [f"insight exceeds {self._max_chars} chars and no condenser is available"]

    # -- per-claim pipeline --------------------------------------------------

    def _ingest_claim(
        self,
        slug: str,
        claim: str,
        *,
        base_anchors: list[EntityKey],
        links: list[str],
        kind: MemoryKind,
        labels: list[str],
        corpus: str,
        source: MemorySource,
        author_agent: str,
        session_id: str | None,
        auto_anchor: bool,
        seed_warnings: list[str],
    ) -> IngestedClaim:
        warnings = list(seed_warnings)
        claim = claim.strip()
        if not claim:
            return IngestedClaim(
                action="rejected", content=claim, warnings=warnings + ["empty claim"]
            )
        if len(claim) > self._max_chars:
            return IngestedClaim(
                action="rejected",
                content=claim,
                warnings=warnings + [f"claim exceeds {self._max_chars} chars"],
            )

        now = self._clock()
        candidate = MemoryNode(
            slug=slug,
            content=claim,
            kind=kind,
            labels=labels,
            corpus=corpus,
            provenance=MemoryProvenance(
                author_agent=author_agent,
                source=source,
                session_id=session_id,
                created_at=now,
                updated_at=now,
            ),
        )

        anchor_keys = list(base_anchors)
        if auto_anchor and not anchor_keys:
            anchor_keys = self._auto_anchor(slug, claim, warnings)
        if len(anchor_keys) > self._max_anchors:
            warnings.append(f"anchors capped to {self._max_anchors}")
            anchor_keys = anchor_keys[: self._max_anchors]
        resolved = self._resolve_anchors(slug, anchor_keys, warnings)

        vec = self._embed(candidate, warnings)
        decision = self._deduper.classify(slug, candidate, candidate_vec=vec)

        if decision.action == "merge" and decision.target_node_id:
            return self._apply_merge(slug, candidate, decision, resolved, links, now, warnings)
        if decision.action == "link" and decision.target_node_id:
            return self._apply_new(
                slug, candidate, resolved, links, vec, now, warnings,
                action="linked", relate_to=[decision.target_node_id], tier=decision.tier,
            )
        return self._apply_new(
            slug, candidate, resolved, links, vec, now, warnings, action="created"
        )

    # -- helpers -------------------------------------------------------------

    def _auto_anchor(self, slug: str, claim: str, warnings: list[str]) -> list[EntityKey]:
        """Embed the claim, NN-search code embeddings, map hits → entity_keys."""
        try:
            qvec = self._embedder.embed_query(claim)
        except Exception:
            return []
        keys: list[EntityKey] = []
        for emb in self._store.vector_search(slug, qvec, k=self._auto_anchor_k):
            key = self._provider.entity_key_of(slug, emb.node_id)
            if key and key not in keys:
                keys.append(key)
        if keys:
            noun = "entity" if len(keys) == 1 else "entities"
            warnings.append(f"auto-anchored to {len(keys)} code {noun}")
        return keys

    def _resolve_anchors(
        self, slug: str, keys: list[EntityKey], warnings: list[str]
    ) -> list[EntityKey]:
        """Drop anchors that don't resolve to a live code node."""
        if not keys:
            return []
        resolved = self._provider.resolve_many(slug, keys)
        out: list[EntityKey] = []
        for key in keys:
            if key in resolved:
                if key not in out:
                    out.append(key)
            else:
                warnings.append(f"dropped unresolved anchor: {key}")
        return out

    def _embed(self, node: MemoryNode, warnings: list[str]) -> list[float] | None:
        """Embed a node's content; non-fatal (None ⇒ BM25-only)."""
        try:
            rows = self._embedder.embed_nodes([(node.node_id, node.content)], slug=node.slug)
        except Exception:
            warnings.append("embedding unavailable; BM25 fallback")
            return None
        return list(rows[0].vector) if rows else None

    def _store_embedding(self, node: MemoryNode, vec: list[float] | None) -> None:
        if vec is None:
            return
        self._store.upsert_memory_embeddings(
            node.slug,
            [
                MemoryEmbedding(
                    slug=node.slug,
                    node_id=node.node_id,
                    vector=vec,
                    model=getattr(self._embedder, "model", ""),
                    dim=len(vec),
                )
            ],
        )

    def _apply_new(
        self,
        slug: str,
        node: MemoryNode,
        anchor_keys: list[EntityKey],
        link_ids: list[str],
        vec: list[float] | None,
        now: str,
        warnings: list[str],
        *,
        action: Literal["created", "linked"],
        relate_to: list[str] | None = None,
        tier: str | None = None,
    ) -> IngestedClaim:
        self._store.upsert_memory_nodes(slug, [node])
        self._store_embedding(node, vec)
        edges = self._anchor_edges(slug, node.node_id, anchor_keys, now)
        edges += self._relate_edges(slug, node.node_id, list(link_ids) + list(relate_to or []), now)
        if edges:
            self._store.upsert_memory_edges(slug, edges)
        return IngestedClaim(
            action=action, node_id=node.node_id, content=node.content,
            anchors=anchor_keys, tier=tier, warnings=warnings,
        )

    def _apply_merge(
        self,
        slug: str,
        candidate: MemoryNode,
        decision: DedupDecision,
        anchor_keys: list[EntityKey],
        link_ids: list[str],
        now: str,
        warnings: list[str],
    ) -> IngestedClaim:
        target = self._store.get_memory_node(slug, decision.target_node_id or "")
        if target is None:  # raced/absent — fall back to a fresh insert
            vec = self._embed(candidate, warnings)
            return self._apply_new(
                slug, candidate, anchor_keys, link_ids, vec, now, warnings, action="created"
            )

        existing = self._store.list_memory_edges(slug, node_id=target.node_id)
        union_anchor_keys = list(
            dict.fromkeys(
                [e.target for e in existing if e.type == "ANCHORS"] + anchor_keys
            )
        )
        carried_relates = [e.target for e in existing if e.type == "RELATES"] + list(link_ids)

        # Survivor keeps the crisper (shorter) text; ties keep the target.
        survivor_content = (
            candidate.content if len(candidate.content) < len(target.content) else target.content
        )
        survivor = MemoryNode(
            slug=slug,
            content=survivor_content,
            kind=target.kind,
            labels=list(dict.fromkeys(target.labels + candidate.labels)),
            corpus=target.corpus,
            provenance=MemoryProvenance(
                author_agent=target.provenance.author_agent,
                source=target.provenance.source,
                session_id=target.provenance.session_id,
                created_at=target.provenance.created_at,
                updated_at=now,
            ),
        )
        vec = self._embed(survivor, warnings)
        self._store.upsert_memory_nodes(slug, [survivor])
        self._store_embedding(survivor, vec)

        edges = self._anchor_edges(slug, survivor.node_id, union_anchor_keys, now)
        edges += self._relate_edges(slug, survivor.node_id, carried_relates, now)
        if edges:
            self._store.upsert_memory_edges(slug, edges)

        # Content changed identity ⇒ retire the old node: invalidate its edges
        # (history preserved) and drop the now-orphaned node + embedding so it
        # neither surfaces in retrieval nor pollutes the dedup ladder.
        if survivor.node_id != target.node_id:
            self._invalidate_edges(slug, existing, now)
            self._store.delete_memory_node(slug, target.node_id)

        return IngestedClaim(
            action="merged", node_id=survivor.node_id, content=survivor.content,
            anchors=union_anchor_keys, tier=decision.tier, warnings=warnings,
        )

    def _anchor_edges(
        self, slug: str, node_id: str, keys: list[EntityKey], now: str
    ) -> list[MemoryEdge]:
        return [
            MemoryEdge(slug=slug, source=node_id, target=key, type="ANCHORS", valid_at=now)
            for key in keys
        ]

    def _relate_edges(
        self, slug: str, node_id: str, target_ids: list[str], now: str
    ) -> list[MemoryEdge]:
        seen: set[str] = set()
        out: list[MemoryEdge] = []
        for tid in target_ids:
            if tid == node_id or tid in seen:
                continue
            seen.add(tid)
            out.append(
                MemoryEdge(slug=slug, source=node_id, target=tid, type="RELATES", valid_at=now)
            )
        return out

    def _invalidate_edges(self, slug: str, edges: list[MemoryEdge], now: str) -> None:
        retired = [e.model_copy(update={"invalid_at": now}) for e in edges if e.invalid_at is None]
        if retired:
            self._store.upsert_memory_edges(slug, retired)


# ── BM25-only fallback embedder (used by InsightIngestor.from_store) ─────────


class _NullEmbedder:
    """No-op embedder used when no embedding backend is configured.

    Returns no vectors, so the ingestor stores notes BM25-only without an
    exception path. ``embed_query`` returns ``[]`` so auto-anchor no-ops.
    """

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list[Any]:
        """Return no embeddings (BM25 fallback)."""
        return []

    def embed_query(self, text: str) -> list[float]:
        """Return an empty query vector (auto-anchor no-ops)."""
        return []


__all__ = [
    "AnchorResolver",
    "IngestedClaim",
    "IngestResult",
    "InsightCondenser",
    "InsightDeduper",
    "DedupDecision",
    "InsightIngestor",
    "utc_now_iso",
]
