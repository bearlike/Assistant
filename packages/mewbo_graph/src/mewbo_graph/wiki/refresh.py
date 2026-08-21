"""On-demand incremental refresh — change detection + scoped graph delta.

The refresh control plane recomputes *only* the scope a diff
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
* ``ScopedEdgeResolver`` — the delta path's faithful-resolution leg. A raw
  re-parse emits cross-file edges by NAME, so without it every scoped refresh
  degraded the edges the full ``graph`` phase had resolved exactly, and a
  project's resolved graph decayed one refresh at a time.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mewbo_core.common import get_logger

from mewbo_graph.wiki.memory_types import DocPageNote, EntityKey, FileManifest
from mewbo_graph.wiki.structure_provider import entity_key_for_node
from mewbo_graph.wiki.types import CommitScope, ScopePreview

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from mewbo_graph.wiki.embedder import EmbedderProtocol
    from mewbo_graph.wiki.graph import GraphParseResult
    from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode
    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.structure_provider import StructureProvider
    from mewbo_graph.wiki.types import GraphEdge, GraphNode, WikiPage

logging = get_logger(name="mewbo_graph.wiki.refresh")


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


class _EdgeResolver(Protocol):
    """The faithful-resolution swap, as a callable seam.

    Structurally the ``plugins.wiki.build_graph._apply_resolver`` contract — the
    ONE implementation of "drop tree-sitter's name-matched edges, substitute the
    scip resolver's exact ones, and say through ``on_report`` which leg ran". It
    is a Protocol here rather than an import so this module keeps importing only
    DOWNWARDS: the plugin package composes over ``wiki/``, so a module-level
    import back into it would close a cycle. :meth:`ScopedEdgeResolver.default`
    is the one place that reaches for the concrete function, lazily.
    """

    def __call__(
        self,
        slug: str,
        repo_root: Path,
        result: GraphParseResult,
        *,
        on_report: Callable[[str], None] | None = None,
    ) -> GraphParseResult:
        """Return *result* with resolved edges substituted for name-matched ones."""
        ...


@dataclass(frozen=True)
class ScopedEdgeResolver:
    """Faithful symbol resolution applied to the DIRTY scope of a refresh.

    A scoped refresh re-parses only the files a diff touched, and a raw
    tree-sitter re-parse emits cross-file edges BY NAME. Persisting those is not
    a neutral choice: the full ``graph`` phase replaces the same edges with the
    resolver's exact ones, so every scoped refresh that skipped resolution
    overwrote a resolved file's edges with name-matched ones and the project's
    resolved graph decayed monotonically, one refresh at a time.

    **Why the resolver runs over the WHOLE clone and not just the dirty files.**
    scip indexes per ``[project]`` root, and a reference in a dirty file usually
    points at a definition in a file the diff never touched — hand the resolver
    the dirty nodes alone and every such reference resolves to nothing, which is
    the same edge loss wearing a cheaper disguise. So the resolver gets the whole
    project's node view (``universe``: the re-parsed dirty nodes plus the store's
    nodes for every untouched file) and the OUTPUT is what gets scoped: only
    edges whose source file is dirty are kept, because those are the only files
    whose edges :meth:`GraphDeltaIndexer.apply` retracted. An untouched file's
    edges stay exactly as the last full index left them — a refresh repairs the
    scope it owns, never the whole graph.

    The leg is optional at BOTH layers, the same way every other collaborator
    here is: an indexer built with no resolver, or one whose binaries are absent
    (``ScipPythonResolver.is_available()``, probed inside :attr:`apply`), keeps
    today's behaviour byte-for-byte — the raw edges pass straight through in
    input order. It also never fails a refresh: a resolver that raises degrades
    to the raw edges with a warning, because a name-matched graph is worse than a
    resolved one but far better than an interrupted refresh.

    Cost: ``O(repo)`` — a scip subprocess per project root plus one union read of
    the project's nodes. That is offline-class work and it belongs to a refresh
    JOB, never to a request path; :attr:`suffixes` is what keeps a docs-only diff
    from paying it at all.
    """

    #: The swap itself (``_apply_resolver``) — probes availability, reports.
    apply: _EdgeResolver
    #: Source suffixes the resolver supersedes. A dirty scope containing none of
    #: them cannot gain a single resolved edge, so the whole leg is skipped
    #: before the union read — a markdown-only refresh must not run pyright.
    suffixes: tuple[str, ...]
    #: Where "which leg ran" goes. Injected because this class knows nothing
    #: about the job whose log the message belongs to; the module logger is the
    #: floor, so the degradation is never silent even with nothing wired.
    on_report: Callable[[str], None] | None = None

    @classmethod
    def default(
        cls, *, on_report: Callable[[str], None] | None = None
    ) -> ScopedEdgeResolver | None:
        """Bind the production leg, or ``None`` when the plugin suite is absent.

        The import is function-local and guarded for the two reasons that always
        pair here: it reaches into the plugin package that composes over this
        module (so it cannot be a top-level import), and a lean install may not
        have it at all (so its absence is a missing feature, not a crash).
        """
        try:
            from mewbo_graph.plugins.wiki.build_graph import (  # noqa: PLC0415
                _PYTHON_SUFFIXES,
                _apply_resolver,
            )
        except ImportError:
            return None
        return cls(
            apply=_apply_resolver, suffixes=_PYTHON_SUFFIXES, on_report=on_report
        )

    def covers(self, files: Iterable[str]) -> bool:
        """True when *files* contain at least one source this leg can resolve."""
        return any(f.endswith(self.suffixes) for f in files)

    def resolve(
        self,
        slug: str,
        repo_root: Path,
        *,
        universe: list[GraphNode],
        raw_edges: list[GraphEdge],
        dirty: set[str],
    ) -> tuple[list[GraphEdge], list[GraphNode]]:
        """Return ``(edges to persist for the dirty scope, External nodes they need)``.

        ``universe`` is the whole project's nodes; ``raw_edges`` the dirty files'
        freshly parsed ones. The returned externals are ONLY those an actually
        kept edge points at — the resolver mints one per out-of-repo descriptor
        for the whole repository, and persisting the rest would attribute the
        entire clone's external surface to a two-file diff.
        """
        report = self.on_report or (lambda message: logging.info(message))
        result = self._as_parse_result(universe, raw_edges)
        try:
            resolved = self.apply(slug, repo_root, result, on_report=report)
        except Exception as exc:  # noqa: BLE001 — never fail a refresh over this
            report(
                f"Scoped refresh of {slug}: faithful symbol resolution failed "
                f"({exc}); the re-parsed scope keeps name-matched edges until "
                "the next full index."
            )
            return list(raw_edges), []

        node_by_id = {n.node_id: n for n in resolved.nodes}
        kept: list[GraphEdge] = []
        for edge in resolved.edges:
            # A synthetic edge (``target_name`` set) can only have come from the
            # dirty input — the resolver never emits one — and its source id may
            # legitimately resolve to no node at all (the documented tree-sitter
            # EXTENDS trap), so it is kept without asking where it came from.
            # Everything else is attributed by its source node's file, which is
            # what scopes the resolver's whole-repo output down to this diff.
            if edge.target_name is not None:
                kept.append(edge)
                continue
            source = node_by_id.get(edge.source)
            if source is not None and source.file in dirty:
                kept.append(edge)

        known = {n.node_id for n in universe}
        targets = {e.target for e in kept}
        externals = [
            n for n in resolved.nodes if n.node_id in targets and n.node_id not in known
        ]
        report(
            f"Scoped refresh of {slug}: {len(kept)} edges for {len(dirty)} "
            f"re-parsed files ({len(kept) - len(raw_edges):+d} vs the raw parse), "
            f"{len(externals)} external targets"
        )
        return kept, externals

    @staticmethod
    def _as_parse_result(
        nodes: list[GraphNode], edges: list[GraphEdge]
    ) -> GraphParseResult:
        """Wrap the scoped inputs in the parse-result shape :attr:`apply` takes."""
        from mewbo_graph.wiki.graph import GraphParseResult  # noqa: PLC0415

        return GraphParseResult(nodes=nodes, edges=edges, skipped=[])


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
    """Retract → re-parse → re-embed → diff → reverse-dependency closure.

    ``parser`` is any object exposing ``parse_file(slug, path, *, repo_root)``
    → ``GraphParseResult`` (the real ``GraphIndex``; stubbable in tests). The
    closure over-approximates by qualified name against the existing graph's
    CALLS/IMPORTS/EXTENDS targets (a false positive is wasted work and safe; a
    false negative would leave a stale index, which is not).

    ``embedder`` is optional and its ABSENCE is a supported mode, not an error:
    the re-parsed scope is then left unvectorised and retrieval over it degrades
    to BM25 + graph traversal, exactly as the full index degrades when no
    embedding backend answers. What is NOT optional is the retract — see
    :meth:`_embed` for why the old vectors go regardless.

    ``resolver`` is the faithful-resolution leg (:class:`ScopedEdgeResolver`).
    Absent, the re-parsed scope persists raw tree-sitter edges — which is what
    this path did unconditionally, and why a heavily-refreshed project's resolved
    graph decayed to almost nothing. Its default is resolved at
    :meth:`RefreshOrchestrator.from_store`, the one composer a production caller
    uses, for exactly the reason the embedder's is.
    """

    _CALLER_EDGE_TYPES = ("CALLS", "IMPORTS", "EXTENDS")

    def __init__(
        self,
        store: WikiStoreBase,
        *,
        parser: _Parser,
        embedder: EmbedderProtocol | None = None,
        resolver: ScopedEdgeResolver | None = None,
        closure_max_depth: int = 4,
    ) -> None:
        """Inject the store, a parser + optional embedder/resolver; bound the closure."""
        self._store = store
        self._parser = parser
        self._embedder = embedder
        self._resolver = resolver
        self._closure_max_depth = closure_max_depth

    def apply(
        self, slug: str, repo_root: Path, change: ChangeSet, *, commit: str
    ) -> GraphDelta:
        """Re-index the dirty scope and return the affected-entity set Δ.

        ``commit`` is REQUIRED, and that is a correctness constraint rather than
        an ergonomic one. Attribution is what makes an artifact reapable: a row
        stamped with a real commit is deleted by the next index's supersede
        sweep, and a row whose ``commit_sha`` field is ABSENT is picked up by
        the isolation backfill. A row stamped with an explicit ``None`` is
        neither — supersede's filter spares null, and the backfill matches only
        a MISSING field, so it falls between the two and no tool in the
        codebase can ever find it again. Defaulting this to ``None`` therefore
        offered callers a quiet way to mint permanently un-reapable artifacts
        on every refresh, which is the exact defect the scoped path exists to
        stop creating.
        """
        modified, added, deleted = change.modified, change.added, change.deleted

        # 1. Snapshot pre-state per dirty/deleted file (keys + edge signature).
        pre_keys = self._keys_by_file(slug, set(modified) | set(deleted))
        pre_sig = self._edge_sig_by_file(slug, set(modified))

        # 2. Retract stale nodes + their forward edges for modified ∪ deleted.
        for f in set(modified) | set(deleted):
            self._store.delete_edges_by_source_file(slug, f)
            self._store.delete_nodes_by_file(slug, f)

        # 3. Re-parse modified ∪ added; collect post-state keys + the raw edges.
        # Sorted so the batch handed to the resolver below is deterministic.
        dirty = sorted(set(modified) | set(added))
        post_keys: dict[str, set[EntityKey]] = {}
        reparsed: list[GraphNode] = []
        raw_edges: list[GraphEdge] = []
        for f in dirty:
            result = self._parser.parse_file(slug, repo_root / f, repo_root=repo_root)
            self._validate(slug, f, result)
            # Stamp the re-parsed scope with the commit being refreshed so the
            # incremental path attributes its nodes the same way the full index
            # does. A scoped refresh does not carry a job_id (no indexing job runs
            # it), so only the commit is threaded.
            self._store.upsert_nodes(slug, result.nodes, commit_sha=commit)
            reparsed.extend(result.nodes)
            raw_edges.extend(result.edges)
            post_keys[f] = {entity_key_for_node(n) for n in result.nodes}

        # 3a. Resolve the scope's edges faithfully, then persist them in ONE
        # batch. The edge write is deferred out of the loop above BECAUSE of
        # this: which edges survive is not decidable per file — the resolver
        # needs the whole project's nodes in hand before it can say. Nodes still
        # go per file, since the resolver reads them and never rewrites them.
        edges, externals = self._resolve(
            slug,
            repo_root,
            raw_edges,
            dirty=set(dirty),
            deleted=set(deleted),
            reparsed=reparsed,
        )
        if externals:
            self._store.upsert_nodes(slug, externals, commit_sha=commit)
        self._store.upsert_edges(slug, edges, commit_sha=commit)
        # The post-state signature is read off what was PERSISTED, never off the
        # raw parse, and it is attributed by source node exactly the way
        # ``_edge_sig_by_file`` attributes the pre-state. Comparing a raw parse
        # against a resolved store would make the two sides incomparable for
        # every resolved file and disable the early cutoff wholesale.
        post_sig = self._sig_by_file(
            edges, {n.node_id: n for n in reparsed}, set(modified)
        )

        # 3b. Re-embed the whole re-parsed scope in ONE call. It covers every
        # re-parsed file, INCLUDING the ones the early cutoff below finds
        # unchanged: step 2 already deleted their vectors along with their nodes,
        # so "no downstream work" still owes them their vectors back.
        self._embed(slug, reparsed, commit)

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

    def _resolve(
        self,
        slug: str,
        repo_root: Path,
        raw_edges: list[GraphEdge],
        *,
        dirty: set[str],
        deleted: set[str],
        reparsed: list[GraphNode],
    ) -> tuple[list[GraphEdge], list[GraphNode]]:
        """Edges to persist for the dirty scope + the External nodes they need.

        Both gates are checked BEFORE the whole-project node view is assembled,
        because assembling it is a union read of every node in the project: no
        resolver injected, or a dirty scope in no language the resolver
        supersedes, must cost nothing at all over today's path.
        """
        if self._resolver is None or not self._resolver.covers(dirty):
            return raw_edges, []
        universe = self._universe(slug, reparsed, exclude=dirty | deleted)
        return self._resolver.resolve(
            slug, repo_root, universe=universe, raw_edges=raw_edges, dirty=dirty
        )

    def _universe(
        self, slug: str, reparsed: list[GraphNode], *, exclude: set[str]
    ) -> list[GraphNode]:
        """The whole project's nodes as of mid-transition: fresh dirty ∪ stored rest.

        ``exclude`` is the dirty ∪ deleted file set, whose stored nodes are
        either superseded by *reparsed* or gone — reading them back would offer
        the resolver two definitions of the same symbol to choose between.
        De-duplicated by ``node_id`` because this read is deliberately UNSCOPED
        (see the note above :meth:`_keys_by_file`) and the union across two
        commit generations can carry a byte-identical node twice.

        Cost: ``O(collection)`` in the project's stored nodes — the same class as
        the three reads below it, and paid only when the resolver leg runs.
        """
        by_id = {n.node_id: n for n in reparsed}
        for node in self._store.query_graph(slug, scope=CommitScope.every()):
            if node.file in exclude or node.node_id in by_id:
                continue
            by_id[node.node_id] = node
        return list(by_id.values())

    def _embed(self, slug: str, nodes: list[GraphNode], commit: str) -> None:
        """Vectorise the re-parsed scope; leave it unvectorised on any failure.

        Degrades exactly the way the full index's embedding step does: with no
        embedder injected, or with a backend that errors, the affected symbols
        stay reachable through BM25 + graph traversal instead of the whole
        refresh failing over a retrieval nicety.

        What does NOT degrade is the retract in :meth:`apply`, which drops the
        previous vectors unconditionally. That asymmetry is deliberate: keeping
        a vector whose node id no longer exists would preserve recall by ranking
        queries against a symbol the working tree does not have, and nothing
        downstream could ever find that vector again to reap it. Missing
        vectors are a quality loss the next full index repairs; orphaned ones
        are permanent.
        """
        if self._embedder is None or not nodes:
            return
        try:
            embeddings = self._embedder.embed_nodes(
                [(n.node_id, n.embedding_text) for n in nodes], slug=slug
            )
        except Exception as exc:  # noqa: BLE001 — degrade to BM25, never fail a refresh
            logging.warning(
                "scoped refresh of {}: embeddings unavailable for {} re-parsed "
                "nodes; that scope stays BM25-only until the next full index. "
                "Reason: {}",
                slug,
                len(nodes),
                exc,
            )
            return
        # A backend that returns FEWER records than it was given is not an
        # exception, so it would otherwise take the silent path: the shortfall
        # keeps no vector, and step 2 already deleted the one it had. Say so —
        # a partial vector set is indistinguishable from a complete one at
        # every read site, so the log line is the only way anyone learns of it.
        if len(embeddings) != len(nodes):
            logging.warning(
                "scoped refresh of {}: embedder returned {} vectors for {} "
                "re-parsed nodes; the {} unmatched stay BM25-only until the "
                "next full index",
                slug,
                len(embeddings),
                len(nodes),
                len(nodes) - len(embeddings),
            )
        if embeddings:
            self._store.upsert_embeddings(slug, embeddings, commit_sha=commit)

    @staticmethod
    def _validate(slug: str, file: str, result: GraphParseResult) -> None:
        """Validate one file's parse result via ``CodeGraph`` before it's upserted.

        Mirrors ``build_graph_core``'s ingest-time gate (schema v2) — the FIRST
        full index was validated there, but a steady-state
        refresh went straight from ``parse_file`` to ``upsert_nodes``/
        ``upsert_edges`` with no gate at all (this review finding). A
        PER-FILE result validates standalone exactly like the full-repo case
        does: its own CONTAINS edges are in-batch (both endpoints present in
        ``result.nodes``), and cross-file IMPORTS/CALLS/EXTENDS/REFERENCES
        edges carry ``target_name`` (a synthetic external target) —
        ``CodeGraph`` exempts those from referential integrity on BOTH ends,
        the same tolerance the full-repo validation already relies on.
        """
        from pydantic import ValidationError

        from mewbo_graph.wiki.types import CodeGraph

        try:
            CodeGraph(nodes=result.nodes, edges=result.edges)
        except ValidationError as exc:
            raise ValueError(
                f"graph schema validation failed for {slug} @ {file}: {exc}"
            ) from exc

    # An incremental refresh observes the store MID-TRANSITION, which is why
    # every read below is deliberately unscoped. ``apply`` retracts the dirty
    # files' nodes and re-parses them stamped with the NEW commit, while every
    # untouched file's nodes stay stamped with the PREVIOUS one — so between
    # step 2 and finalize's supersede sweep the graph legitimately spans two
    # generations, and a single-commit scope would be wrong for one of them
    # (``_reverse_closure`` in particular has to walk across both). Scoping
    # these is only safe once the delta path re-stamps what it keeps.
    def _keys_by_file(self, slug: str, files: set[str]) -> dict[str, set[EntityKey]]:
        out: dict[str, set[EntityKey]] = {f: set() for f in files}
        if not files:
            return out
        for node in self._store.query_graph(slug, scope=CommitScope.every()):
            if node.file in files:
                out[node.file].add(entity_key_for_node(node))
        return out

    def _edge_sig_by_file(
        self, slug: str, files: set[str]
    ) -> dict[str, frozenset[tuple[str, str, str]]]:
        if not files:
            return {}
        nodes_by_id = {
            n.node_id: n
            for n in self._store.query_graph(slug, scope=CommitScope.every())
        }
        return self._sig_by_file(
            self._store.list_edges(slug, scope=CommitScope.every()), nodes_by_id, files
        )

    @staticmethod
    def _sig_by_file(
        edges: Iterable[GraphEdge],
        nodes_by_id: dict[str, GraphNode],
        files: set[str],
    ) -> dict[str, frozenset[tuple[str, str, str]]]:
        """Group an edge signature per source FILE, ignoring unmappable sources.

        The one attribution rule, shared by the pre-state (persisted edges) and
        the post-state (the edges about to be persisted). Sharing it is what
        makes the early-cutoff comparison meaningful: an edge whose source id
        resolves to no node — the documented tree-sitter EXTENDS trap — is absent
        from BOTH sides rather than from only one, which would otherwise leave
        every file carrying such an edge permanently ineligible for the cutoff.
        """
        acc: dict[str, set[tuple[str, str, str]]] = {f: set() for f in files}
        for edge in edges:
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
        edges = self._store.list_edges(slug, scope=CommitScope.every())
        nodes_by_id = {
            n.node_id: n
            for n in self._store.query_graph(slug, scope=CommitScope.every())
        }
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
        embedder: EmbedderProtocol | None = None,
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
        embedder: EmbedderProtocol | None = None,
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

    def migrate(self, slug: str, *, commit: str | None = None) -> int:
        """One-time: build a ``DocPageNote`` for each page not yet mapped.

        Idempotent — only pages without an existing note are added. Returns
        the number of notes created.

        ``commit`` is optional HERE and required on :meth:`plan` because the two
        answer different questions. A standalone migration of an
        already-indexed project genuinely may not know which commit those pages
        were written at, and stamping a guess would be worse than stamping
        nothing; a refresh always knows.
        """
        existing = {d.page_id for d in self._store.list_doc_notes(slug)}
        notes = [
            self._note_for(slug, page, commit)
            for page in self._store.list_pages(slug)
            if page.id not in existing
        ]
        if notes:
            self._store.upsert_doc_notes(slug, notes)
        return len(notes)

    def plan(self, slug: str, delta: GraphDelta, *, commit: str) -> DocPlan:
        """Score every page against Δ and persist the staleness + policy.

        ``commit`` arrives as an ARGUMENT rather than being read from ambient
        state for the reason every threshold here does: the stage is pure DI, so
        a test constructs it without a config file or a job record. It is
        stamped on every note this pass touches — creating it in :meth:`migrate`
        alone would leave the field null forever on every project that was
        already migrated, since ``migrate`` only ever visits pages that have no
        note yet.
        """
        self.migrate(slug, commit=commit)  # ensure notes exist (idempotent)
        affected, removed = set(delta.affected), set(delta.removed_keys)
        entries: list[DocPlanEntry] = []
        updated: list[DocPageNote] = []
        for note in self._store.list_doc_notes(slug):
            entry, new_note = self._assess(note, affected, removed, commit)
            entries.append(entry)
            updated.append(new_note)
        if updated:
            self._store.upsert_doc_notes(slug, updated)
        return DocPlan(pages=tuple(entries), new_pages=self._propose_new_pages(slug, delta))

    def restamp_page(
        self, slug: str, page: WikiPage, *, commit: str | None
    ) -> DocPageNote:
        """Re-derive a page's note from the page as just written, and clear it.

        The page-writer is the only thing that can close the loop this planner
        opens: it scores a page stale, an act phase rewrites it, and unless the
        note is re-derived from the NEW body the next refresh scores the same
        page against the same anchors, reaches the same verdict, and pays for
        the same rewrite again — every refresh, indefinitely.

        The verdict is RESET rather than re-scored. A rewritten page describes
        the commit it was written at, and re-scoring would buy little anyway:
        a doc note's anchor keys are whole file paths, so the intersection
        ``_assess`` computes is quantised to files, not symbols.

        The anchors come from the frontmatter actually submitted, not from the
        note's stored set: a rewritten page may legitimately document a
        different list of files, and keeping the old set would score it against
        code it no longer describes.
        """
        existing = self._store.get_doc_note(slug, page.id)
        note = (
            self._note_for(slug, page, commit)
            if existing is None
            else existing.model_copy(
                update={
                    "title": page.title,
                    "content_hash": self._hash(page.body),
                    "anchor_keys": self._anchor_keys(page),
                    "last_indexed_commit": commit,
                    "staleness_score": 0.0,
                    "staleness_reason": "clean",
                    "generation_policy": "keep",
                    "stale_anchor_keys": [],
                    "deleted_anchor_keys": [],
                }
            )
        )
        self._store.upsert_doc_notes(slug, [note])
        return note

    # -- scoring -------------------------------------------------------------

    def _assess(
        self,
        note: DocPageNote,
        affected: set[str],
        removed: set[str],
        commit: str | None,
    ) -> tuple[DocPlanEntry, DocPageNote]:
        anchors = set(note.anchor_keys)
        # Keep the intersections themselves, not just their sizes. These ARE the
        # act phase's hint: which of this page's anchors moved, and which were
        # deleted outright. Sorted so a re-run over an unchanged delta writes a
        # byte-identical note (set iteration order is not stable across runs).
        stale = sorted(anchors & affected)
        deleted = sorted(anchors & removed)
        if anchors:
            direct = len(stale) / len(anchors)
            deleted_frac = len(deleted) / len(anchors)
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
                "last_indexed_commit": commit,
                # Overwritten wholesale, never accumulated: the hint describes
                # THIS refresh, and a union across runs would keep naming
                # anchors a later pass already accounted for.
                "stale_anchor_keys": stale,
                "deleted_anchor_keys": deleted,
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

    def _note_for(
        self, slug: str, page: WikiPage, commit: str | None
    ) -> DocPageNote:
        """Build a fresh, clean note for *page* (the one construction site)."""
        return DocPageNote(
            slug=slug,
            page_id=page.id,
            title=page.title,
            content_hash=self._hash(page.body),
            page_type="concept",
            anchor_keys=self._anchor_keys(page),
            last_indexed_commit=commit,
        )

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

    def scope_preview(self) -> ScopePreview:
        """Counts for the scope-preview event (human-gateable before act).

        A MODEL rather than a bare ``dict[str, int]``,
        because the same counts are persisted on ``IndexingJob.scope_preview``
        and streamed over SSE. A dict crossing those two boundaries carries no
        schema: a renamed key reaches the console as a missing field rather than
        a validation error, and nothing between here and the panel would notice.
        """
        pol = [p.policy for p in self.docs.pages]
        return ScopePreview(
            filesAdded=len(self.change.added),
            filesModified=len(self.change.modified),
            filesDeleted=len(self.change.deleted),
            earlyCutoffFiles=len(self.graph.early_cutoff_files),
            affectedEntities=len(self.graph.affected),
            memoryKept=len(self.memory.kept),
            memoryInvalidated=len(self.memory.invalidated),
            memoryRevalidated=len(self.memory.revalidated),
            pagesKeep=pol.count("keep"),
            pagesEdit=pol.count("edit"),
            pagesRegenerate=pol.count("regenerate"),
            newPages=len(self.docs.new_pages),
            llmCalls=self.memory.llm_calls,
        )


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
        slug: str,
        parser: _Parser | None = None,
        embedder: EmbedderProtocol | None = None,
        resolver: ScopedEdgeResolver | None = None,
        on_report: Callable[[str], None] | None = None,
        llm: Any = None,
        clock: Any = None,
    ) -> RefreshOrchestrator:
        """Build with the real tree-sitter parser + standard stages.

        ``embedder`` reaches all THREE stages that need one — the graph delta
        re-embeds what it re-parses, the reconciler measures anchor drift with
        it, and the doc planner holds it for the drift term. Handing it to only
        the latter two is what left a refreshed graph unvectorised.

        It is RESOLVED here when the caller passes none, rather than being read
        as "skip embedding". Every OTHER stage degrades harmlessly without one,
        so threading a bare ``None`` down looked correct — but the graph stage
        does not degrade harmlessly: it has already deleted the previous
        vectors by the time it would embed, so a missing embedder there does
        not mean "leave things as they were", it means "strip the vectors off
        everything this refresh touched". Since this classmethod is the only
        way a production caller builds the composer, resolving the default
        HERE is the difference between the delta path embedding and silently
        not.

        The switch is consulted ONLY on that default. An explicitly injected
        embedder overrides it, as does constructing the stages directly — a DI
        seam an operator setting could veto would be the worse design, and
        tests have to be able to inject a fake regardless of deployment config.
        So read this as "the switch decides what the DEFAULT is", never as "the
        switch decides whether embedding happens": someone debugging why a
        refresh still embeds after they turned embedding off is looking for an
        injected collaborator, not for a bug here.

        The ``wiki.refresh.*`` thresholds are read HERE for the same reason, and
        the reason is worth stating because the failure mode is invisible: the
        three stages take every threshold as a keyword argument whose default
        equals the config default, so a stage built with none of them behaves
        EXACTLY like a correctly configured one on a default deployment. An
        operator who retunes a threshold sees no error and no effect. Reading
        them at the one composition root a production caller uses, and passing
        them down as arguments, keeps the stages pure DI — a stage that read
        config itself could not be constructed in a test without one, and a
        directly-constructed stage would silently start obeying a deployment
        setting a test had no way to see.

        ``resolver`` is resolved here on the same argument, and it is the reason
        the argument is worth making twice. A scoped refresh re-parses files the
        full ``graph`` phase had already resolved faithfully, so a delta stage
        built with no resolver does not merely skip an improvement — it OVERWRITES
        resolved edges with name-matched ones, every refresh, until a project's
        cross-file graph is gone. There is no config switch: the leg gates itself
        on ``ScipPythonResolver.is_available()`` and reports which way it went, so
        a deployment without the binaries lands on exactly its prior behaviour.
        ``on_report`` is where that line goes — default the module logger, so the
        degradation is never silent; a caller with a job log should pass its own
        emitter so the answer lives beside the refresh it describes.
        """
        from mewbo_graph.wiki.embedder import Embedder, make_embedder_for_or_none

        if parser is None:
            from mewbo_graph.wiki.graph import GraphIndex

            parser = GraphIndex()
        if embedder is None and Embedder.enabled():
            embedder = make_embedder_for_or_none(store, slug)
        if resolver is None:
            resolver = ScopedEdgeResolver.default(on_report=on_report)
        return cls(
            store=store,
            graph_indexer=GraphDeltaIndexer(
                store,
                parser=parser,
                embedder=embedder,
                resolver=resolver,
                closure_max_depth=int(cls._threshold("closure_max_depth", 4)),
            ),
            reconciler=MemoryReconciler(
                store=store,
                embedder=embedder,
                llm=llm,
                drift_keep=float(cls._threshold("drift_keep", 0.90)),
                drift_invalidate=float(cls._threshold("drift_invalidate", 0.75)),
            ),
            doc_planner=DocStalenessPlanner(
                store=store,
                embedder=embedder,
                keep=float(cls._threshold("page_keep", 0.05)),
                edit=float(cls._threshold("page_edit", 0.35)),
                regen=float(cls._threshold("page_regen", 0.70)),
                new_page_min=int(cls._threshold("new_page_min", 5)),
            ),
            clock=clock,
        )

    @staticmethod
    def _threshold(field: str, default: float) -> float:
        """Read one ``wiki.refresh.*`` threshold, falling back to *default*.

        The default passed at each call site is the stage constructor's own, so
        a graph-less/config-less caller lands on exactly the behaviour it had
        before these knobs were wired.
        """
        from mewbo_core.config import get_config_value

        value = get_config_value("wiki", "refresh", field, default=default)
        return default if value is None else value

    def refresh(
        self,
        slug: str,
        repo_root: Path,
        files: list[Path],
        *,
        commit: str,
    ) -> RefreshReport:
        """Run the deterministic refresh stages over the changed scope.

        ``commit`` is required for the reason :meth:`GraphDeltaIndexer.apply`
        gives: everything this writes has to be attributable, or it becomes
        un-reapable by construction.
        """
        change = self._change_detector.detect(slug, repo_root, files)
        if change.is_empty:
            return RefreshReport.noop(change)
        started_at = self._clock()
        delta = self._graph_indexer.apply(slug, repo_root, change, commit=commit)
        memory = self._reconciler.reconcile(slug, delta, refresh_started_at=started_at)
        docs = self._doc_planner.plan(slug, delta, commit=commit)
        return RefreshReport(change=change, graph=delta, memory=memory, docs=docs)


__all__ = [
    "ChangeSet",
    "ChangeDetector",
    "GraphDelta",
    "GraphDeltaIndexer",
    "ScopedEdgeResolver",
    "MemoryReconcileResult",
    "MemoryReconciler",
    "DocPlanEntry",
    "DocPlan",
    "DocStalenessPlanner",
    "RefreshReport",
    "RefreshOrchestrator",
]
