"""On-demand incremental refresh — change detection + scoped graph delta.

The refresh control plane (Gitea #13 §10) recomputes *only* the scope a diff
touches, across graph → memory → docs. This module hosts the first two atomic
stages; memory reconciliation, doc staleness, and the plan-then-act
orchestrator land alongside them.

* ``ChangeDetector`` — content-hash diff of the working tree vs the persisted
  ``FileManifest`` (mtime is unreliable across checkouts; hashing is robust and
  collapses N stacked commits into one end-state comparison — the Mimir
  pattern). Produces an ``added / modified / deleted`` ``ChangeSet``.
* ``GraphDeltaIndexer`` — retracts the stale graph for dirty files, re-parses
  ``modified ∪ added``, and computes the **affected-entity set** Δ via a
  reverse-dependency closure (CodePlan change-may-impact). Salsa-style early
  cutoff: a file whose re-parse yields identical entity/edge signatures
  contributes nothing downstream.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mewbo_graph.wiki.memory_types import DocPageNote, EntityKey, FileManifest
from mewbo_graph.wiki.structure_provider import entity_key_for_node

if TYPE_CHECKING:
    from mewbo_graph.wiki.embedder import Embedder
    from mewbo_graph.wiki.graph import GraphParseResult
    from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.structure_provider import StructureProvider
    from mewbo_graph.wiki.types import GraphNode, WikiPage


class _Parser(Protocol):
    """Minimal seam the graph delta indexer drives (the real ``GraphIndex``)."""

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        """Parse one file into nodes/edges (stubbable in tests)."""
        ...


# ── change detection ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChangeSet:
    """Working-tree delta vs the last-indexed manifest (relative POSIX paths)."""

    added: list[str]
    modified: list[str]
    deleted: list[str]
    current_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def dirty(self) -> list[str]:
        """Files that must be re-parsed (added ∪ modified)."""
        return self.added + self.modified

    @property
    def is_empty(self) -> bool:
        """True when nothing changed since the last index."""
        return not (self.added or self.modified or self.deleted)


class ChangeDetector:
    """Content-hash diff of a working tree against the stored file manifest."""

    def __init__(self, store: WikiStoreBase) -> None:
        """Compose over the wiki store (holds the prior manifest)."""
        self._store = store

    def detect(self, slug: str, repo_root: Path, files: list[Path]) -> ChangeSet:
        """Return the added/modified/deleted set for *slug* under *repo_root*."""
        current: dict[str, str] = {}
        for path in files:
            try:
                rel = str(path.relative_to(repo_root))
            except ValueError:
                continue
            current[rel] = self._hash_file(path)
        manifest = {m.path: m.content_hash for m in self._store.list_file_manifest(slug)}
        added = sorted(p for p in current if p not in manifest)
        modified = sorted(
            p for p in current if p in manifest and current[p] != manifest[p]
        )
        deleted = sorted(p for p in manifest if p not in current)
        return ChangeSet(
            added=added, modified=modified, deleted=deleted, current_hashes=current
        )

    @staticmethod
    def _hash_file(path: Path) -> str:
        """SHA-256 of a file's bytes (empty string on read failure)."""
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""


# ── scoped graph delta ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GraphDelta:
    """The affected-entity set Δ produced by an incremental graph re-index."""

    added_keys: frozenset[EntityKey]
    modified_keys: frozenset[EntityKey]
    removed_keys: frozenset[EntityKey]
    affected: frozenset[EntityKey]  # direct dirty ∪ reverse-dependency closure
    early_cutoff_files: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        """True when the change had no graph impact (full early cutoff)."""
        return not self.affected


class GraphDeltaIndexer:
    """Retract → re-parse → diff → reverse-dependency closure for dirty files.

    ``parser`` is any object exposing ``parse_file(slug, path, *, repo_root)``
    → ``GraphParseResult`` (the real ``GraphIndex``; stubbable in tests). The
    closure over-approximates by qualified name against the existing graph's
    CALLS/IMPORTS/EXTENDS targets (a false positive is wasted work and safe; a
    false negative would leave a stale index, which is not).
    """

    _CALLER_EDGE_TYPES = ("CALLS", "IMPORTS", "EXTENDS")

    def __init__(
        self,
        store: WikiStoreBase,
        *,
        parser: _Parser,
        closure_max_depth: int = 4,
    ) -> None:
        """Inject the store + a tree-sitter parser and bound the closure depth."""
        self._store = store
        self._parser = parser
        self._closure_max_depth = closure_max_depth

    def apply(
        self, slug: str, repo_root: Path, change: ChangeSet, *, commit: str | None = None
    ) -> GraphDelta:
        """Re-index the dirty scope and return the affected-entity set Δ."""
        modified, added, deleted = change.modified, change.added, change.deleted

        # 1. Snapshot pre-state per dirty/deleted file (keys + edge signature).
        pre_keys = self._keys_by_file(slug, set(modified) | set(deleted))
        pre_sig = self._edge_sig_by_file(slug, set(modified))

        # 2. Retract stale nodes + their forward edges for modified ∪ deleted.
        for f in set(modified) | set(deleted):
            self._store.delete_edges_by_source_file(slug, f)
            self._store.delete_nodes_by_file(slug, f)

        # 3. Re-parse modified ∪ added; collect post-state keys + edge signature.
        post_keys: dict[str, set[EntityKey]] = {}
        post_sig: dict[str, frozenset[tuple[str, str, str]]] = {}
        for f in set(modified) | set(added):
            result = self._parser.parse_file(slug, repo_root / f, repo_root=repo_root)
            self._store.upsert_nodes(slug, result.nodes)
            self._store.upsert_edges(slug, result.edges)
            post_keys[f] = {entity_key_for_node(n) for n in result.nodes}
            post_sig[f] = frozenset(
                (e.source, e.target, e.type) for e in result.edges
            )

        # 4. Per-file diff with Salsa early cutoff.
        added_keys: set[EntityKey] = set()
        modified_keys: set[EntityKey] = set()
        removed_keys: set[EntityKey] = set()
        early_cutoff: list[str] = []
        for f in modified:
            pre, post = pre_keys.get(f, set()), post_keys.get(f, set())
            if pre == post and pre_sig.get(f) == post_sig.get(f):
                early_cutoff.append(f)
                continue
            added_keys |= post - pre
            removed_keys |= pre - post
            modified_keys |= post & pre
        for f in added:
            added_keys |= post_keys.get(f, set())
        for f in deleted:
            removed_keys |= pre_keys.get(f, set())

        direct = added_keys | modified_keys | removed_keys
        affected = self._reverse_closure(slug, direct)

        # 5. Refresh the manifest for the scope we touched.
        self._update_manifest(slug, change, post_keys, commit)

        return GraphDelta(
            added_keys=frozenset(added_keys),
            modified_keys=frozenset(modified_keys),
            removed_keys=frozenset(removed_keys),
            affected=frozenset(affected),
            early_cutoff_files=tuple(early_cutoff),
        )

    # -- helpers -------------------------------------------------------------

    def _keys_by_file(self, slug: str, files: set[str]) -> dict[str, set[EntityKey]]:
        out: dict[str, set[EntityKey]] = {f: set() for f in files}
        if not files:
            return out
        for node in self._store.query_graph(slug):
            if node.file in files:
                out[node.file].add(entity_key_for_node(node))
        return out

    def _edge_sig_by_file(
        self, slug: str, files: set[str]
    ) -> dict[str, frozenset[tuple[str, str, str]]]:
        if not files:
            return {}
        nodes_by_id = {n.node_id: n for n in self._store.query_graph(slug)}
        acc: dict[str, set[tuple[str, str, str]]] = {f: set() for f in files}
        for edge in self._store.list_edges(slug):
            src = nodes_by_id.get(edge.source)
            if src is not None and src.file in files:
                acc[src.file].add((edge.source, edge.target, edge.type))
        return {f: frozenset(v) for f, v in acc.items()}

    def _reverse_closure(
        self, slug: str, direct: set[EntityKey]
    ) -> set[EntityKey]:
        """BFS over reverse CALLS/IMPORTS/EXTENDS edges (name-matched), depth-capped."""
        if not direct:
            return set()
        edges = self._store.list_edges(slug)
        nodes_by_id = {n.node_id: n for n in self._store.query_graph(slug)}
        callers_of: dict[str, set[EntityKey]] = defaultdict(set)
        for edge in edges:
            if edge.type in self._CALLER_EDGE_TYPES:
                src = nodes_by_id.get(edge.source)
                if src is not None:
                    callers_of[edge.target].add(entity_key_for_node(src))

        affected = set(direct)
        frontier = set(direct)
        for _ in range(self._closure_max_depth):
            target_ids: set[str] = set()
            for key in frontier:
                name = self._name_of(key)
                if name:
                    target_ids |= self._synthetic_target_ids(slug, name)
            new: set[EntityKey] = set()
            for tid in target_ids:
                new |= callers_of.get(tid, set())
            new -= affected
            if not new:
                break
            affected |= new
            frontier = new
        return affected

    @staticmethod
    def _name_of(key: EntityKey) -> str | None:
        """Symbol name from ``file#Name`` (None for a bare File key)."""
        return key.split("#", 1)[1] if "#" in key else None

    @staticmethod
    def _synthetic_target_ids(slug: str, name: str) -> set[str]:
        """Synthetic ids the AST extractor assigns to external call/import/extend targets."""
        from mewbo_graph.wiki.graph import _stable_id

        return {
            _stable_id(slug, "Function", name, "<external>", 0),
            _stable_id(slug, "Class", name, "<external>", 0),
            _stable_id(slug, "Module", name, name, 0),
        }

    def _update_manifest(
        self,
        slug: str,
        change: ChangeSet,
        post_keys: dict[str, set[EntityKey]],
        commit: str | None,
    ) -> None:
        entries = [
            FileManifest(
                slug=slug,
                path=f,
                content_hash=change.current_hashes.get(f, ""),
                last_indexed_commit=commit,
                entity_keys=sorted(post_keys.get(f, set())),
            )
            for f in set(change.modified) | set(change.added)
        ]
        if entries:
            self._store.upsert_file_manifest(slug, entries)
        for f in change.deleted:
            self._store.delete_file_manifest(slug, f)


# ── memory reconciliation ───────────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryReconcileResult:
    """Per-memory outcome of reconciling anchors against Δ."""

    kept: tuple[str, ...]
    invalidated: tuple[str, ...]  # node_ids with no live anchor remaining
    revalidated: tuple[str, ...]  # node_ids whose drift band hit the LLM gate
    llm_calls: int


_REVALIDATE_PROMPT = (
    "A memory note about a codebase and the code entity it is anchored to may"
    " have drifted apart after an edit.\n  NOTE: {claim}\n  ENTITY (now): {entity}\n"
    "Reply with one word: VALID if the note still holds, UPDATE if it needs"
    " rewording but the link stands, or OUTDATED if the note no longer applies."
)


class MemoryReconciler:
    """Reconcile memory anchors against the affected-entity set Δ.

    Drift ladder per anchored memory (Graphiti invalidate-don't-delete; Mem0
    NONE-default): a **removed** entity invalidates its anchor; a **modified**
    entity is gated on embedding drift — ``≥drift_keep`` keeps with no LLM,
    ``<drift_invalidate`` invalidates, the band in between defers to one LLM
    re-validation. Idempotent within a refresh via ``anchor_checked_at`` and
    safe for user-curated (``override``-labelled) notes.
    """

    _OVERRIDE_LABEL = "override"

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: Embedder | None = None,
        llm: Any = None,
        provider: StructureProvider | None = None,
        drift_keep: float = 0.90,
        drift_invalidate: float = 0.75,
    ) -> None:
        """Inject store + (optional) embedder/llm/provider and drift thresholds."""
        self._store = store
        self._embedder = embedder
        self._llm = llm
        self._provider = provider
        self._drift_keep = drift_keep
        self._drift_invalidate = drift_invalidate
        self._llm_calls = 0

    @property
    def provider(self) -> StructureProvider:
        """Lazily build the default code structure provider."""
        if self._provider is None:
            from mewbo_graph.wiki.structure_provider import CodeStructureProvider
            self._provider = CodeStructureProvider(self._store)
        return self._provider

    def reconcile(
        self, slug: str, delta: GraphDelta, *, refresh_started_at: str
    ) -> MemoryReconcileResult:
        """Re-validate every memory anchored to a changed entity (O(|Δ|))."""
        removed, modified = set(delta.removed_keys), set(delta.modified_keys)
        # Resolve every modified entity ONCE (O(N) graph scan), not once per
        # anchor — the drift gate then reads from this cache.
        entities = self.provider.resolve_many(slug, list(modified))
        kept: list[str] = []
        invalidated: list[str] = []
        revalidated: list[str] = []
        calls_before = self._llm_calls

        for mid in self._store.memories_anchored_to(slug, list(removed | modified)):
            node = self._store.get_memory_node(slug, mid)
            if node is None:
                continue
            if node.anchor_checked_at and node.anchor_checked_at >= refresh_started_at:
                continue  # already reconciled this pass — idempotent
            if self._OVERRIDE_LABEL in node.labels:  # user-curated → immutable
                self._stamp(slug, node, refresh_started_at)
                kept.append(mid)
                continue

            if self._apply_ladder(slug, node, removed, modified, entities, refresh_started_at):
                revalidated.append(mid)
            self._stamp(slug, node, refresh_started_at)
            live = self._store.list_memory_edges(slug, node_id=mid)
            target = kept if any(e.type == "ANCHORS" for e in live) else invalidated
            target.append(mid)

        return MemoryReconcileResult(
            kept=tuple(kept),
            invalidated=tuple(invalidated),
            revalidated=tuple(revalidated),
            llm_calls=self._llm_calls - calls_before,
        )

    # -- ladder --------------------------------------------------------------

    def _apply_ladder(
        self,
        slug: str,
        node: MemoryNode,
        removed: set[str],
        modified: set[str],
        entities: dict[EntityKey, GraphNode],
        now: str,
    ) -> bool:
        """Invalidate stale anchors of *node*; return True if the LLM gate ran."""
        used_llm = False
        for edge in self._store.list_memory_edges(slug, node_id=node.node_id):
            if edge.type != "ANCHORS":
                continue
            if edge.target in removed:
                self._invalidate(slug, edge, now)
            elif edge.target in modified:
                action, llm = self._gate(node, entities.get(edge.target))
                used_llm = used_llm or llm
                if action == "invalidate":
                    self._invalidate(slug, edge, now)
        return used_llm

    def _gate(self, node: MemoryNode, entity: GraphNode | None) -> tuple[str, bool]:
        """Drift gate for one modified anchor → (``keep``|``invalidate``, used_llm)."""
        if entity is None:  # entity vanished from the graph
            return "invalidate", False
        if self._embedder is None:
            return "keep", False  # no evidence → NONE default
        sim = self._similarity(node, entity)
        if sim >= self._drift_keep:
            return "keep", False
        if sim < self._drift_invalidate:
            return "invalidate", False
        if self._llm is None:
            return "keep", False  # band but no judge → NONE default
        verdict = self._llm_validate(node.content, self._entity_repr(entity))
        return ("invalidate" if verdict == "OUTDATED" else "keep"), True

    def _similarity(self, node: MemoryNode, entity: GraphNode) -> float:
        from mewbo_graph.wiki.embedder import Embedder

        assert self._embedder is not None  # gated by caller (_gate)
        mem_vec = self._embedder.embed_query(node.content)
        ent_vec = self._embedder.embed_query(self._entity_repr(entity))
        return Embedder.cosine(mem_vec, ent_vec)

    def _llm_validate(self, claim: str, entity_text: str) -> str:
        from mewbo_graph.wiki.memory import llm_text

        self._llm_calls += 1
        try:
            text = llm_text(
                self._llm, _REVALIDATE_PROMPT.format(claim=claim, entity=entity_text)
            ).strip().upper()
        except Exception:
            return "VALID"  # NONE default on failure
        if "OUTDATED" in text:
            return "OUTDATED"
        return "UPDATE" if "UPDATE" in text else "VALID"

    def _invalidate(self, slug: str, edge: MemoryEdge, now: str) -> None:
        self._store.upsert_memory_edges(slug, [edge.model_copy(update={"invalid_at": now})])

    def _stamp(self, slug: str, node: MemoryNode, now: str) -> None:
        if node.anchor_checked_at != now:
            self._store.upsert_memory_nodes(
                slug, [node.model_copy(update={"anchor_checked_at": now})]
            )

    @staticmethod
    def _entity_repr(node: GraphNode) -> str:
        """Text representation of a code entity for drift comparison."""
        return (node.name + " " + (node.docstring or "")).strip()


# ── documentation staleness ─────────────────────────────────────────────────


@dataclass(frozen=True)
class DocPlanEntry:
    """Per-page staleness verdict from the doc planner."""

    page_id: str
    staleness: float
    policy: Literal["keep", "edit", "regenerate"]
    needs_review: bool
    reason: str


@dataclass(frozen=True)
class DocPlan:
    """Scope of documentation work for one refresh: page updates + new pages."""

    pages: tuple[DocPlanEntry, ...]
    new_pages: tuple[str, ...]  # uncovered files proposed for fresh documentation

    @property
    def actionable(self) -> tuple[DocPlanEntry, ...]:
        """Pages that need an edit or regenerate (policy != keep)."""
        return tuple(p for p in self.pages if p.policy != "keep")


class DocStalenessPlanner:
    """Maps generated pages onto the memory dimension as ``DocPageNote`` nodes.

    Each wiki page is a first-class node anchored (via its frontmatter
    ``relevantSources``) to the code it documents. Given the affected-entity
    set Δ, propagate change impact onto each page's anchors and pick a
    generation policy (RepoDoc selective-regen): ``staleness = 0.5·direct +
    0.3·drift + 0.2·deleted_fraction`` (drift is 0 in v1 — pages aren't
    embedded). New-page proposals come from uncovered public entities.
    """

    _PUBLIC_PREFIX_SKIP = "_"

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: Embedder | None = None,
        weights: tuple[float, float, float] = (0.5, 0.3, 0.2),
        keep: float = 0.05,
        edit: float = 0.35,
        regen: float = 0.70,
        new_page_min: int = 5,
    ) -> None:
        """Inject store + thresholds (per-page-type tuning is a later pass)."""
        self._store = store
        self._embedder = embedder
        self._w_direct, self._w_drift, self._w_deleted = weights
        self._keep = keep
        self._edit = edit
        self._regen = regen
        self._new_page_min = new_page_min

    def migrate(self, slug: str) -> int:
        """One-time: build a ``DocPageNote`` for each page not yet mapped.

        Idempotent — only pages without an existing note are added. Returns
        the number of notes created.
        """
        existing = {d.page_id for d in self._store.list_doc_notes(slug)}
        notes = [
            DocPageNote(
                slug=slug,
                page_id=page.id,
                title=page.title,
                content_hash=self._hash(page.body),
                page_type="concept",
                anchor_keys=self._anchor_keys(page),
            )
            for page in self._store.list_pages(slug)
            if page.id not in existing
        ]
        if notes:
            self._store.upsert_doc_notes(slug, notes)
        return len(notes)

    def plan(self, slug: str, delta: GraphDelta) -> DocPlan:
        """Score every page against Δ and persist the staleness + policy."""
        self.migrate(slug)  # ensure notes exist (idempotent)
        affected, removed = set(delta.affected), set(delta.removed_keys)
        entries: list[DocPlanEntry] = []
        updated: list[DocPageNote] = []
        for note in self._store.list_doc_notes(slug):
            entry, new_note = self._assess(note, affected, removed)
            entries.append(entry)
            updated.append(new_note)
        if updated:
            self._store.upsert_doc_notes(slug, updated)
        return DocPlan(pages=tuple(entries), new_pages=self._propose_new_pages(slug, delta))

    # -- scoring -------------------------------------------------------------

    def _assess(
        self, note: DocPageNote, affected: set[str], removed: set[str]
    ) -> tuple[DocPlanEntry, DocPageNote]:
        anchors = set(note.anchor_keys)
        if anchors:
            direct = len(anchors & affected) / len(anchors)
            deleted_frac = len(anchors & removed) / len(anchors)
        else:
            direct = deleted_frac = 0.0
        # drift term is 0 in v1 (pages carry no embedding yet).
        staleness = self._w_direct * direct + self._w_deleted * deleted_frac
        needs_review = staleness >= self._regen or deleted_frac > 0.5
        policy = self._policy(staleness)
        reason = self._reason(staleness, deleted_frac)
        entry = DocPlanEntry(
            page_id=note.page_id,
            staleness=round(staleness, 4),
            policy=policy,
            needs_review=needs_review,
            reason=reason,
        )
        new_note = note.model_copy(
            update={
                "staleness_score": round(staleness, 4),
                "staleness_reason": reason,
                "generation_policy": policy,
            }
        )
        return entry, new_note

    def _policy(self, staleness: float) -> Literal["keep", "edit", "regenerate"]:
        if staleness < self._keep:
            return "keep"
        if staleness < self._edit:
            return "edit"
        return "regenerate"

    @staticmethod
    def _reason(staleness: float, deleted_frac: float) -> str:
        if deleted_frac > 0.5:
            return "anchors deleted"
        if staleness >= 0.35:
            return "anchors changed"
        if staleness >= 0.05:
            return "minor anchor change"
        return "clean"

    def _propose_new_pages(self, slug: str, delta: GraphDelta) -> tuple[str, ...]:
        """Files with ≥``new_page_min`` uncovered public symbols → new-page hints."""
        covered = {
            key.split("#", 1)[0]
            for note in self._store.list_doc_notes(slug)
            for key in note.anchor_keys
        }
        per_file: dict[str, int] = defaultdict(int)
        for key in delta.added_keys:
            if "#" not in key:
                continue  # File-level, not a symbol
            file, _, name = key.partition("#")
            if file in covered or name.rsplit(".", 1)[-1].startswith(self._PUBLIC_PREFIX_SKIP):
                continue
            per_file[file] += 1
        return tuple(sorted(f for f, n in per_file.items() if n >= self._new_page_min))

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _anchor_keys(page: WikiPage) -> list[EntityKey]:
        seen: list[EntityKey] = []
        for src in page.frontmatter.relevant_sources or []:
            if src.path and src.path not in seen:
                seen.append(src.path)
        return seen

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()


# ── orchestration (plan-then-act) ────────────────────────────────────────────


@dataclass(frozen=True)
class RefreshReport:
    """The committed scope of one incremental refresh (the plan + applied Δ).

    The deterministic stages (graph delta, memory reconciliation, doc staleness
    scoring) are already applied when this is returned — that is the **Free**
    tier. ``pages_to_regenerate`` (Gated: needs an LLM page-writer) and
    ``docs.new_pages`` (Blocked: needs opt-in) are the act-phase work-list.
    """

    change: ChangeSet
    graph: GraphDelta
    memory: MemoryReconcileResult
    docs: DocPlan

    @classmethod
    def noop(cls, change: ChangeSet) -> RefreshReport:
        """An empty report for a refresh that found nothing to do."""
        empty_delta = GraphDelta(
            frozenset(), frozenset(), frozenset(), frozenset(), ()
        )
        return cls(
            change=change,
            graph=empty_delta,
            memory=MemoryReconcileResult((), (), (), 0),
            docs=DocPlan((), ()),
        )

    @property
    def is_noop(self) -> bool:
        """True when nothing changed since the last index."""
        return self.change.is_empty

    @property
    def pages_to_regenerate(self) -> tuple[str, ...]:
        """Page ids the act phase must edit/regenerate (Gated tier)."""
        return tuple(p.page_id for p in self.docs.pages if p.policy != "keep")

    def scope_preview(self) -> dict[str, int]:
        """Counts for the scope-preview event (human-gateable before act)."""
        pol = [p.policy for p in self.docs.pages]
        return {
            "filesAdded": len(self.change.added),
            "filesModified": len(self.change.modified),
            "filesDeleted": len(self.change.deleted),
            "earlyCutoffFiles": len(self.graph.early_cutoff_files),
            "affectedEntities": len(self.graph.affected),
            "memoryKept": len(self.memory.kept),
            "memoryInvalidated": len(self.memory.invalidated),
            "memoryRevalidated": len(self.memory.revalidated),
            "pagesKeep": pol.count("keep"),
            "pagesEdit": pol.count("edit"),
            "pagesRegenerate": pol.count("regenerate"),
            "newPages": len(self.docs.new_pages),
            "llmCalls": self.memory.llm_calls,
        }


class RefreshOrchestrator:
    """Plan-then-act conductor for an on-demand incremental refresh.

    Composes the four atomic stages (change detect → graph delta → memory
    reconcile → doc staleness) and runs them in order, returning a
    ``RefreshReport`` that doubles as the committed scope. On-demand ONLY — the
    caller decides when to run it; an empty change set short-circuits to a
    no-op so nothing is re-indexed unnecessarily.
    """

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        graph_indexer: GraphDeltaIndexer,
        change_detector: ChangeDetector | None = None,
        reconciler: MemoryReconciler | None = None,
        doc_planner: DocStalenessPlanner | None = None,
        clock: Any = None,
    ) -> None:
        """Inject the store + the four stages (graph_indexer carries the parser)."""
        from mewbo_graph.wiki.memory import utc_now_iso

        self._store = store
        self._change_detector = change_detector or ChangeDetector(store)
        self._graph_indexer = graph_indexer
        self._reconciler = reconciler or MemoryReconciler(store=store)
        self._doc_planner = doc_planner or DocStalenessPlanner(store=store)
        self._clock = clock or utc_now_iso

    @classmethod
    def from_store(
        cls,
        store: WikiStoreBase,
        *,
        parser: _Parser | None = None,
        embedder: Embedder | None = None,
        llm: Any = None,
        clock: Any = None,
    ) -> RefreshOrchestrator:
        """Build with the real tree-sitter parser + standard stages."""
        if parser is None:
            from mewbo_graph.wiki.graph import GraphIndex

            parser = GraphIndex()
        return cls(
            store=store,
            graph_indexer=GraphDeltaIndexer(store, parser=parser),
            reconciler=MemoryReconciler(store=store, embedder=embedder, llm=llm),
            doc_planner=DocStalenessPlanner(store=store, embedder=embedder),
            clock=clock,
        )

    def refresh(
        self,
        slug: str,
        repo_root: Path,
        files: list[Path],
        *,
        commit: str | None = None,
    ) -> RefreshReport:
        """Run the deterministic refresh stages over the changed scope."""
        change = self._change_detector.detect(slug, repo_root, files)
        if change.is_empty:
            return RefreshReport.noop(change)
        started_at = self._clock()
        delta = self._graph_indexer.apply(slug, repo_root, change, commit=commit)
        memory = self._reconciler.reconcile(slug, delta, refresh_started_at=started_at)
        docs = self._doc_planner.plan(slug, delta)
        return RefreshReport(change=change, graph=delta, memory=memory, docs=docs)


__all__ = [
    "ChangeSet",
    "ChangeDetector",
    "GraphDelta",
    "GraphDeltaIndexer",
    "MemoryReconcileResult",
    "MemoryReconciler",
    "DocPlanEntry",
    "DocPlan",
    "DocStalenessPlanner",
    "RefreshReport",
    "RefreshOrchestrator",
]
