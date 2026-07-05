"""``ScipPythonResolver`` — faithful Python symbol resolution via scip-python.

scip-python (Pyright-backed) emits a SCIP index whose ``Occurrence``s carry the
EXACT symbol each name resolves to. This resolver maps that index onto our
tree-sitter :class:`~mewbo_graph.wiki.types.GraphNode`s to produce precise
``REFERENCES`` / ``EXTENDS`` edges — replacing the name-match heuristic that
mislinks every same-named method to the same node.

The pipeline is decomposed into small, separately-testable concerns:

* **discovery** — find each ``[project]`` ``pyproject.toml`` (one scip-python
  run per root; the monorepo root alone yields an EMPTY index);
* **production** (injected :class:`ScipIndexProducer`) — generate a
  ``pyrightconfig.json`` whose ``extraPaths`` reach sibling packages so
  cross-package imports resolve without a virtualenv, run scip-python, and read
  it back as JSON via ``scip print --json``;
* **mapping** (``_build_result``, pure) — index definitions, then turn each
  reference occurrence into a real-id edge. This half has no subprocess and no
  filesystem beyond reading the already-cloned source bytes, so the unit tests
  drive it with a captured index + fixture nodes and never need scip installed.

The byte-vs-line:col gap: our nodes carry ``(start_byte, end_byte)`` ranges;
SCIP carries 0-based ``[line, col]`` with ``text_document_encoding = UTF-8``, so
``col`` is already a byte offset within its line. We build a line→byte table per
source file (one newline-find sweep) and look up the enclosing / matching node by
byte containment — never by re-parsing.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..graph import _stable_id
from ..types import ExternalNode, GraphEdge, GraphNode
from .base import ResolutionResult, ResolutionStats
from .descriptor import LeafKind, ScipSymbol

logger = logging.getLogger(__name__)

# SCIP SymbolRole bitmask — only the Definition bit matters for the AST graph
# (scip-python does not reliably set the Import bit, so every non-definition
# occurrence is treated uniformly as a REFERENCES site — see module docstring).
_ROLE_DEFINITION = 0x1

# node kinds a given SCIP leaf could map onto.
_TYPE_KINDS = frozenset({"Class", "Interface"})
_CALLABLE_KINDS = frozenset({"Method", "Function"})

_DEFAULT_EXCLUDES = ["**/node_modules", "**/.venv", "**/__pycache__", "**/.git"]

# Fixed scip-python ``--project-version`` — the version token is stripped during
# stitching, so any non-empty value works; pinning it avoids a git dependency.
_PROJECT_VERSION = "0"


# ── Production seam (DI so unit tests need no scip binaries) ─────────────────


class ScipIndexProducer(Protocol):
    """Produce a parsed SCIP index for one project root.

    Returns the ``scip print --json`` document as a dict, or ``None`` if the
    project could not be indexed (the resolver then skips it). ``extra_paths``
    are absolute sibling import roots to expose on ``sys.path`` via
    ``pyrightconfig.json`` so cross-package imports resolve.
    """

    def produce(
        self, project_root: Path, project_name: str, extra_paths: Sequence[Path]
    ) -> dict[str, Any] | None:
        """Index *project_root* and return its SCIP document (None on failure)."""
        ...


class SubprocessScipProducer:
    """Default producer: shells out to ``scip-python`` + ``scip``.

    Holds no per-call state beyond the configured binary names/timeout, so a
    single instance is reused across roots. Both binaries are external CLIs
    (Node + Go) — there is no Python dependency — so availability is gated on
    ``shutil.which`` rather than an import guard.
    """

    def __init__(
        self,
        *,
        scip_python_bin: str = "scip-python",
        scip_bin: str = "scip",
        timeout: float = 900.0,
    ) -> None:
        """Configure the binary names and the per-root subprocess timeout."""
        self._scip_python_bin = scip_python_bin
        self._scip_bin = scip_bin
        self._timeout = timeout

    def available(self) -> bool:
        """True only when BOTH the indexer and the reader binaries are on PATH."""
        return bool(
            shutil.which(self._scip_python_bin) and shutil.which(self._scip_bin)
        )

    def produce(
        self, project_root: Path, project_name: str, extra_paths: Sequence[Path]
    ) -> dict[str, Any] | None:
        """Index *project_root* and return its SCIP document, or ``None`` on failure."""
        with tempfile.TemporaryDirectory(prefix="mewbo-scip-") as tmp:
            out = Path(tmp) / "index.scip"
            with self._pyright_config(project_root, extra_paths):
                if not self._run_index(project_root, project_name, out):
                    return None
            return self._read_json(out)

    @contextmanager
    def _pyright_config(self, project_root: Path, extra_paths: Sequence[Path]):
        """Write a temporary ``pyrightconfig.json`` exposing sibling import roots.

        Reversible: any pre-existing config at *project_root* is restored on exit
        (and a config we created is removed), so running the resolver leaves even
        a non-ephemeral checkout byte-for-byte as it was found.
        """
        cfg_path = project_root / "pyrightconfig.json"
        backup = cfg_path.read_bytes() if cfg_path.exists() else None
        payload = {
            "extraPaths": [str(p) for p in extra_paths],
            "exclude": list(_DEFAULT_EXCLUDES),
        }
        try:
            cfg_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            yield
        finally:
            if backup is None:
                cfg_path.unlink(missing_ok=True)
            else:
                cfg_path.write_bytes(backup)

    def _run_index(
        self, project_root: Path, project_name: str, out: Path
    ) -> bool:
        """Run ``scip-python index`` from *project_root*; True on a non-empty output."""
        try:
            proc = subprocess.run(
                [
                    self._scip_python_bin,
                    "index",
                    "--project-name",
                    project_name,
                    # Pin a version so scip-python never falls back to the git
                    # revision (and crashes on a non-git checkout). The <pkg> <ver>
                    # tokens are stripped during descriptor stitching, so the value
                    # is immaterial — only that one exists.
                    "--project-version",
                    _PROJECT_VERSION,
                    "--output",
                    str(out),
                ],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("scip-python index failed for %s: %s", project_root, exc)
            return False
        if proc.returncode != 0:
            logger.warning(
                "scip-python index rc=%s for %s: %s",
                proc.returncode,
                project_root,
                proc.stderr.strip()[:500],
            )
            return False
        return out.exists() and out.stat().st_size > 0

    def _read_json(self, out: Path) -> dict[str, Any] | None:
        """Convert the protobuf ``.scip`` to JSON via ``scip print --json``."""
        try:
            proc = subprocess.run(
                [self._scip_bin, "print", "--json", str(out)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("scip print failed for %s: %s", out, exc)
            return None
        if proc.returncode != 0:
            logger.warning("scip print rc=%s: %s", proc.returncode, proc.stderr[:500])
            return None
        try:
            parsed: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("scip print produced non-JSON: %s", exc)
            return None
        return parsed


# ── Per-file helpers ────────────────────────────────────────────────────────


class _FileBytes:
    """Line→byte table for one source file (UTF-8, so SCIP ``col`` == byte)."""

    __slots__ = ("_line_starts", "_length")

    def __init__(self, data: bytes) -> None:
        """Index every line-start byte offset in *data* with one find-sweep."""
        starts = [0]
        idx = data.find(b"\n")
        while idx != -1:
            starts.append(idx + 1)
            idx = data.find(b"\n", idx + 1)
        self._line_starts = starts
        self._length = len(data)

    def byte_at(self, line0: int, col0: int) -> int | None:
        """Absolute byte offset of 0-based ``line0:col0`` (None if off the file)."""
        if line0 < 0 or line0 >= len(self._line_starts):
            return None
        pos = self._line_starts[line0] + col0
        return pos if 0 <= pos <= self._length else None


@dataclass(frozen=True, slots=True)
class _Span:
    """A node's byte span in one file, for containment lookup."""

    node_id: str
    type: str
    name: str
    start: int
    end: int

    def contains(self, byte: int) -> bool:
        """True when *byte* falls within this node's half-open range."""
        return self.start <= byte < self.end

    @property
    def width(self) -> int:
        """Range width — smaller == more deeply nested (innermost wins)."""
        return self.end - self.start


# ── The resolver ────────────────────────────────────────────────────────────


class ScipPythonResolver:
    """Map a scip-python index onto our tree-sitter nodes — faithfully.

    One instance per indexing job (bound to a slug + injected producer). The
    public surface is :meth:`resolve`; the heavy mapping is split into private
    helpers so each — descriptor stitching, def→node containment match, enclosing
    scope, external convergence — is unit-testable in isolation.
    """

    def __init__(
        self,
        slug: str,
        *,
        producer: ScipIndexProducer | None = None,
    ) -> None:
        """Bind to *slug*; default to the subprocess producer when none is given."""
        self._slug = slug
        self._producer = producer or SubprocessScipProducer()
        # Per-resolve mutable state (reset at the top of every resolve()).
        self._files: dict[str, _FileBytes | None] = {}
        self._spans: dict[str, list[_Span]] = {}
        self._file_node_for: dict[str, str] = {}

    @staticmethod
    def is_available() -> bool:
        """True when the default subprocess backend can run (both binaries present)."""
        return SubprocessScipProducer().available()

    # ── Orchestration ───────────────────────────────────────────────────

    def resolve(
        self, repo_root: Path, nodes: Sequence[GraphNode]
    ) -> ResolutionResult:
        """Run scip-python per project root and map the result onto *nodes*."""
        repo_root = repo_root.resolve()
        roots = self.discover_roots(repo_root)
        import_roots = {r: self._import_root(r) for r in roots}
        indexes: list[tuple[Path, dict[str, Any]]] = []
        for root in roots:
            extra = [import_roots[o] for o in roots if o != root]
            index = self._producer.produce(root, self._project_name(root), extra)
            if index is not None:
                indexes.append((root, index))
        return self._build_result(repo_root, indexes, nodes, project_roots=len(roots))

    @staticmethod
    def discover_roots(repo_root: Path) -> list[Path]:
        """Directories holding a ``pyproject.toml`` with a ``[project]`` table.

        Each becomes one scip-python invocation. Sorted for deterministic run
        order; ``.venv`` / ``node_modules`` / VCS dirs are pruned so a vendored
        dependency's ``pyproject`` is never mistaken for a first-party root.
        """
        roots: list[Path] = []
        skip = {".venv", "venv", "node_modules", ".git", "__pycache__", ".tox"}
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            if "pyproject.toml" in filenames and ScipPythonResolver._has_project_table(
                Path(dirpath) / "pyproject.toml"
            ):
                roots.append(Path(dirpath))
        return sorted(roots)

    @staticmethod
    def _has_project_table(pyproject: Path) -> bool:
        """True when *pyproject* declares a ``[project]`` table (a real package root).

        A line scan — not a TOML parse — keeps the resolver dependency-free across
        Python versions (no ``tomllib`` on 3.10); only a top-level ``[project]``
        header marks a PEP 621 root.
        """
        try:
            with pyproject.open(encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if (
                        line.startswith("[")
                        and line.endswith("]")
                        and line in ("[project]", '["project"]')
                    ):
                        return True
        except OSError:
            return False
        return False

    @staticmethod
    def _import_root(project_root: Path) -> Path:
        """Where this project's top-level packages are importable from.

        ``<root>/src`` for a src-layout project, else the root itself (flat
        layout, or a namespace-package directory like ``lib/llm`` whose ``demolib``
        package sits directly inside).
        """
        src = project_root / "src"
        return src if src.is_dir() else project_root

    @staticmethod
    def _project_name(project_root: Path) -> str:
        """A stable ``--project-name`` for a root.

        The package token is stripped during stitching, so the value only needs
        to be consistent within a single run; the directory name is sufficient
        and needs no TOML parse.
        """
        return project_root.name or "project"

    # ── Pure mapping (no subprocess, no discovery) ──────────────────────

    def _build_result(
        self,
        repo_root: Path,
        indexes: Sequence[tuple[Path, Mapping[str, Any]]],
        nodes: Sequence[GraphNode],
        *,
        project_roots: int,
    ) -> ResolutionResult:
        """Index definitions across all roots, then resolve every reference."""
        self._index_nodes(nodes)
        symbols = _SymbolIndex()
        # Pass 1 — definitions across every indexed root.
        for project_root, index in indexes:
            prefix = self._repo_prefix(repo_root, project_root)
            for doc in index.get("documents", []):
                self._index_definitions(repo_root, prefix, doc, symbols)
        # Pass 2 — references + inheritance relationships.
        counters = _Counters()
        edges = _EdgeAccumulator(self._slug)
        externals = _ExternalRegistry(self._slug)
        for project_root, index in indexes:
            prefix = self._repo_prefix(repo_root, project_root)
            for doc in index.get("documents", []):
                self._resolve_document(
                    repo_root, prefix, doc, symbols, edges, externals, counters
                )
        return ResolutionResult(
            edges=edges.edges(),
            externals=externals.nodes(),
            stats=ResolutionStats(
                project_roots=project_roots,
                indexed_roots=len(indexes),
                def_sites_mapped=symbols.mapped_count,
                resolved_exact=counters.exact,
                resolved_stitched=counters.stitched,
                external=counters.external,
                extends=counters.extends,
                dropped_unmodelled=counters.unmodelled,
                dropped_ambiguous=counters.ambiguous,
                dropped_unresolved=counters.unresolved,
            ),
        )

    def _index_nodes(self, nodes: Sequence[GraphNode]) -> None:
        """Bucket nodes by file for containment lookup; record each File node."""
        self._files = {}
        self._spans = {}
        self._file_node_for = {}
        for node in nodes:
            self._spans.setdefault(node.file, []).append(
                _Span(node.node_id, node.type, node.name, node.range[0], node.range[1])
            )
            if node.type == "File":
                self._file_node_for[node.file] = node.node_id

    def _index_definitions(
        self,
        repo_root: Path,
        prefix: str,
        doc: Mapping[str, Any],
        symbols: _SymbolIndex,
    ) -> None:
        """Record every definition occurrence in *doc* against the matching node."""
        rel = self._join(prefix, doc.get("relative_path", ""))
        for occ in doc.get("occurrences", []):
            if not int(occ.get("symbol_roles", 0)) & _ROLE_DEFINITION:
                continue
            sym = ScipSymbol.parse(occ.get("symbol", ""))
            if sym.is_local:
                continue
            symbols.note_def(sym.raw, sym.descriptor)
            node_id = self._map_def_to_node(repo_root, rel, occ, sym)
            if node_id is not None:
                symbols.map(sym.raw, sym.descriptor, node_id)

    def _map_def_to_node(
        self,
        repo_root: Path,
        rel: str,
        occ: Mapping[str, Any],
        sym: ScipSymbol,
    ) -> str | None:
        """Resolve a definition occurrence to OUR node id (None if unmodelled).

        ``MODULE`` leaves map to the file's File node; ``TYPE`` / ``CALLABLE``
        leaves map to the in-file node of the right kind whose byte range encloses
        the definition's name token and whose name matches — innermost wins, so a
        method shadowing a same-named function resolves to the method.
        """
        if sym.leaf_kind is LeafKind.MODULE:
            return self._file_node_for.get(rel)
        if sym.leaf_kind is LeafKind.TYPE:
            kinds: frozenset[str] = _TYPE_KINDS
        elif sym.leaf_kind is LeafKind.CALLABLE:
            kinds = _CALLABLE_KINDS
        else:
            return None
        byte = self._byte_of(repo_root, rel, occ)
        if byte is None or sym.leaf_name is None:
            return None
        return self._match_node(rel, byte, kinds, sym.leaf_name)

    def _resolve_document(
        self,
        repo_root: Path,
        prefix: str,
        doc: Mapping[str, Any],
        symbols: _SymbolIndex,
        edges: _EdgeAccumulator,
        externals: _ExternalRegistry,
        counters: _Counters,
    ) -> None:
        """Emit REFERENCES edges for references + EXTENDS for inheritance."""
        rel = self._join(prefix, doc.get("relative_path", ""))
        for occ in doc.get("occurrences", []):
            if int(occ.get("symbol_roles", 0)) & _ROLE_DEFINITION:
                continue
            sym = ScipSymbol.parse(occ.get("symbol", ""))
            if sym.is_local:
                continue
            byte = self._byte_of(repo_root, rel, occ)
            if byte is None:
                continue
            source = self._enclosing(rel, byte)
            if source is None:
                continue
            target = self._resolve_target(sym, symbols, externals, counters)
            if target is not None and target != source:
                edges.add(source, target, "REFERENCES")
        # Inheritance lives on SymbolInformation.relationships, not occurrences.
        for info in doc.get("symbols", []):
            self._resolve_relationships(info, symbols, edges, externals, counters)

    def _resolve_relationships(
        self,
        info: Mapping[str, Any],
        symbols: _SymbolIndex,
        edges: _EdgeAccumulator,
        externals: _ExternalRegistry,
        counters: _Counters,
    ) -> None:
        """Emit an EXTENDS edge per ``is_implementation`` relationship.

        Scoped to TYPE-kind subjects (Class/Interface) only. pyright/scip-python
        also sets ``is_implementation`` on a METHOD overriding an abstract base
        method — a routine SCIP convention, but a DIFFERENT relationship than
        class inheritance. Letting a CALLABLE-kind subject through would emit
        an EXTENDS edge sourced at a Method/Function node, which `CodeGraph`'s
        endpoint rule rejects (EXTENDS sources are Class/Interface/Object only)
        — failing the WHOLE graph's schema validation for any repo with a
        single overridden method, not just dropping that one edge.
        """
        relationships = info.get("relationships") or []
        impls = [r for r in relationships if r.get("is_implementation")]
        if not impls:
            return
        if ScipSymbol.parse(info.get("symbol", "")).leaf_kind is not LeafKind.TYPE:
            return
        subject = symbols.node_for_exact(info.get("symbol", ""))
        if subject is None:
            return
        for rel in impls:
            parent = self._resolve_target(
                ScipSymbol.parse(rel.get("symbol", "")), symbols, externals, counters
            )
            if parent is not None and parent != subject:
                edges.add(subject, parent, "EXTENDS")
                counters.extends += 1

    def _resolve_target(
        self,
        sym: ScipSymbol,
        symbols: _SymbolIndex,
        externals: _ExternalRegistry,
        counters: _Counters,
    ) -> str | None:
        """Resolve a referenced symbol to a real node id, or ``None`` to drop it.

        Order is exact-then-stitch-then-external, and every branch is honest:

        1. exact full-symbol hit → an in-project reference (no ambiguity);
        2. the symbol IS an in-repo definition but maps to no node (attribute,
           parameter) → drop, never fabricate;
        3. its descriptor maps to exactly one in-repo node → cross-project stitch;
        4. its descriptor maps to 2+ nodes → ambiguous, drop (no mislink);
        5. its descriptor is in-repo but unmodelled → drop;
        6. otherwise it is genuinely out-of-repo → an External node (converged by
           descriptor).
        """
        if sym.is_local:
            return None
        exact = symbols.node_for_exact(sym.raw)
        if exact is not None:
            counters.exact += 1
            return exact
        if symbols.is_known_def(sym.raw):
            counters.unmodelled += 1
            return None
        stitched = symbols.nodes_for_descriptor(sym.descriptor)
        if stitched is not None:
            if len(stitched) == 1:
                counters.stitched += 1
                return next(iter(stitched))
            counters.ambiguous += 1
            return None
        if symbols.is_known_descriptor(sym.descriptor):
            counters.unmodelled += 1
            return None
        counters.external += 1
        return externals.intern(sym)

    # ── Byte / containment primitives ───────────────────────────────────

    def _byte_of(
        self, repo_root: Path, rel: str, occ: Mapping[str, Any]
    ) -> int | None:
        """Absolute byte offset of an occurrence's start (None if unmappable)."""
        rng = occ.get("range")
        if not rng or len(rng) < 2:
            return None
        fb = self._file_bytes(repo_root, rel)
        if fb is None:
            return None
        return fb.byte_at(int(rng[0]), int(rng[1]))

    def _file_bytes(self, repo_root: Path, rel: str) -> _FileBytes | None:
        """Lazily read + index *rel*; cache (incl. a missing-file ``None``)."""
        if rel not in self._files:
            try:
                data = (repo_root / rel).read_bytes()
                self._files[rel] = _FileBytes(data)
            except OSError:
                self._files[rel] = None
        return self._files[rel]

    def _match_node(
        self, rel: str, byte: int, kinds: frozenset[str], name: str
    ) -> str | None:
        """Innermost in-file node of *kinds* named *name* enclosing *byte*."""
        best: _Span | None = None
        for span in self._spans.get(rel, ()):
            if span.type in kinds and span.name == name and span.contains(byte):
                if best is None or span.width < best.width:
                    best = span
        return best.node_id if best else None

    def _enclosing(self, rel: str, byte: int) -> str | None:
        """Innermost in-file node of ANY kind enclosing *byte* (File is the floor)."""
        best: _Span | None = None
        for span in self._spans.get(rel, ()):
            if span.contains(byte) and (best is None or span.width < best.width):
                best = span
        return best.node_id if best else None

    # ── SCIP path → repo-relative path (matches GraphNode.file) ─────────

    @staticmethod
    def _repo_prefix(repo_root: Path, project_root: Path) -> str:
        """Project-root path relative to the repo root (POSIX prefix; '' at top)."""
        try:
            rel = project_root.resolve().relative_to(repo_root)
        except ValueError:
            return ""
        text = rel.as_posix()
        return "" if text == "." else text

    @staticmethod
    def _join(prefix: str, relative_path: str) -> str:
        """Join a repo prefix with a SCIP document's project-relative path."""
        if not prefix:
            return relative_path
        return f"{prefix}/{relative_path}" if relative_path else prefix


# ── Mapping-pass collaborators ──────────────────────────────────────────────


class _SymbolIndex:
    """Definition indexes built in pass 1, queried in pass 2.

    Keeps three views of the def-sites so the resolver can be exact when it can
    and honest when it cannot: ``_exact`` (full symbol → node) for same-project
    precision, ``_by_descriptor`` (descriptor → set of nodes) for cross-project
    stitching + ambiguity detection, and the ``_known_*`` sets that distinguish
    "in-repo but unmodelled" (drop) from "out-of-repo" (External).
    """

    def __init__(self) -> None:
        """Start with empty def indexes."""
        self._exact: dict[str, str] = {}
        self._by_descriptor: dict[str, set[str]] = {}
        self._known_symbols: set[str] = set()
        self._known_descriptors: set[str] = set()

    def note_def(self, symbol: str, descriptor: str) -> None:
        """Record that *symbol* is an in-repo definition (whether or not modelled)."""
        self._known_symbols.add(symbol)
        if descriptor:
            self._known_descriptors.add(descriptor)

    def map(self, symbol: str, descriptor: str, node_id: str) -> None:
        """Bind a definition *symbol*/*descriptor* to its resolved node id."""
        self._exact.setdefault(symbol, node_id)
        if descriptor:
            self._by_descriptor.setdefault(descriptor, set()).add(node_id)

    def node_for_exact(self, symbol: str) -> str | None:
        """Node bound to this exact full symbol, if any."""
        return self._exact.get(symbol)

    def nodes_for_descriptor(self, descriptor: str) -> set[str] | None:
        """Nodes whose definition shares this descriptor (None if none modelled)."""
        return self._by_descriptor.get(descriptor)

    def is_known_def(self, symbol: str) -> bool:
        """True when this exact symbol is an in-repo definition."""
        return symbol in self._known_symbols

    def is_known_descriptor(self, descriptor: str) -> bool:
        """True when this descriptor names some in-repo definition."""
        return descriptor in self._known_descriptors

    @property
    def mapped_count(self) -> int:
        """Number of distinct def symbols mapped to a node."""
        return len(self._exact)


class _ExternalRegistry:
    """Converges out-of-repo references onto one External node per descriptor."""

    def __init__(self, slug: str) -> None:
        """Bind to *slug* (External ids are content-addressed within it)."""
        self._slug = slug
        self._by_id: dict[str, GraphNode] = {}

    def intern(self, sym: ScipSymbol) -> str:
        """Return the External node id for *sym*, minting it on first sight."""
        ext_id = _stable_id(self._slug, "External", sym.descriptor, "<external>", 0)
        if ext_id not in self._by_id:
            self._by_id[ext_id] = ExternalNode(
                slug=self._slug,
                node_id=ext_id,
                name=sym.readable,
                file="",
                range=(0, 0),
                docstring=None,
            )
        return ext_id

    def nodes(self) -> list[GraphNode]:
        """All synthesized External nodes (deterministic id order)."""
        return [self._by_id[i] for i in sorted(self._by_id)]


class _EdgeAccumulator:
    """De-duplicating edge sink keyed on ``(source, type, target)``."""

    def __init__(self, slug: str) -> None:
        """Bind to *slug*; preserve first-seen order for deterministic output."""
        self._slug = slug
        self._seen: dict[tuple[str, str, str], GraphEdge] = {}

    def add(self, source: str, target: str, edge_type: str) -> None:
        """Record one edge, collapsing repeats of the same triple."""
        key = (source, edge_type, target)
        if key not in self._seen:
            self._seen[key] = GraphEdge(
                slug=self._slug, source=source, target=target, type=edge_type  # type: ignore[arg-type]
            )

    def edges(self) -> list[GraphEdge]:
        """All distinct edges in first-seen order."""
        return list(self._seen.values())


@dataclass
class _Counters:
    """Mutable tally folded into :class:`ResolutionStats` at the end."""

    exact: int = 0
    stitched: int = 0
    external: int = 0
    extends: int = 0
    unmodelled: int = 0
    ambiguous: int = 0
    unresolved: int = 0


