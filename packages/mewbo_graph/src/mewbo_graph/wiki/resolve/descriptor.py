"""SCIP symbol-string parsing — the byte-faithful seam for descriptor stitching.

A SCIP Python symbol string is::

    scip-python python <pkg> <ver> <descriptor>

or a file-local symbol::

    local <id>

The ``<descriptor>`` tail is a sequence of *components*, each a name plus a
one-character suffix that encodes its kind (the SCIP descriptor grammar):

    name '/'         namespace / module / package
    name '#'         type (class)
    name '.'         term (attribute / module-level variable)
    name '().'       method / function  (``name(<disambiguator>).``)
    name ':'         meta (a module's own self-symbol, e.g. ``__init__:``)
    name '!'         macro (unused for Python)
    '(' name ')'     parameter
    '[' name ']'     type parameter

Two facts drive the resolver and live here so they are parsed in ONE place:

* **Descriptor stitching.** Cross-*project* references inside a monorepo carry
  the WRONG ``<pkg> <ver>`` tokens (no virtualenv → no distribution mapping),
  but the ``<descriptor>`` tail is byte-identical to the defining project's
  definition descriptor. Stripping the prefix and linking by descriptor stitches
  the projects back together — see :class:`ScipSymbol.descriptor`.
* **Leaf kind.** Only the LAST component decides which of our graph node kinds a
  definition could map to (a ``type`` leaf → ``Class``; a ``method`` leaf →
  ``Method``/``Function``; a ``meta`` leaf → the ``File`` node). Everything else
  (term/parameter/type-parameter) has no node in our tree-sitter graph.

This module has no third-party dependency and never imports tree-sitter.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Single-character descriptor suffixes → component kind.
_SUFFIX_KIND: dict[str, str] = {
    "/": "namespace",
    "#": "type",
    ".": "term",
    ":": "meta",
    "!": "macro",
}
# Characters that terminate a bare (un-escaped) descriptor name.
_NAME_TERMINATORS = frozenset("/#.:!()[]")

_SCIP_PYTHON_PREFIX = "scip-python python "


class LeafKind(str, Enum):
    """Kind of the final descriptor component — the only one that maps to a node.

    ``MODULE`` (a ``:`` meta leaf) is the module self-symbol and maps to the File
    node of its document; ``TYPE`` → a ``Class``/``Interface`` node; ``CALLABLE``
    → a ``Method``/``Function`` node. ``OTHER`` (term/parameter/namespace/…) has
    no node in the tree-sitter graph and is never mapped — references to it are
    dropped as in-repo-but-unmodelled rather than fabricated.
    """

    TYPE = "type"
    CALLABLE = "callable"
    MODULE = "module"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ScipSymbol:
    """Parsed SCIP symbol string.

    ``is_local`` marks a ``local <id>`` file-local symbol (a parameter or block
    variable) — never a graph node and never linkable across files, so the
    resolver skips it entirely. For a global symbol, ``descriptor`` is the
    ``<pkg> <ver>``-stripped tail (the stitching key), ``leaf_name`` is the last
    component's name, and ``leaf_kind`` classifies what node it could map to.
    """

    raw: str
    is_local: bool
    descriptor: str
    leaf_name: str | None
    leaf_kind: LeafKind

    @classmethod
    def parse(cls, raw: str) -> ScipSymbol:
        """Parse a SCIP symbol string into its stitching key + leaf classification."""
        if raw.startswith("local "):
            return cls(
                raw=raw,
                is_local=True,
                descriptor="",
                leaf_name=None,
                leaf_kind=LeafKind.OTHER,
            )
        descriptor = cls._strip_prefix(raw)
        components = cls._tokenize(descriptor)
        if not components:
            return cls(
                raw=raw,
                is_local=False,
                descriptor=descriptor,
                leaf_name=None,
                leaf_kind=LeafKind.OTHER,
            )
        leaf_name, leaf_suffix = components[-1]
        return cls(
            raw=raw,
            is_local=False,
            descriptor=descriptor,
            leaf_name=leaf_name or None,
            leaf_kind=cls._leaf_kind(leaf_suffix),
        )

    @property
    def readable(self) -> str:
        """Dotted, human-readable name for an External convergence node.

        Joins every component name (``rich.console`` + ``Console`` + ``print`` →
        ``rich.console.Console.print``); the module/namespace name already
        carries its own dots through the backtick-escape.
        """
        names = [name for name, _ in self._tokenize(self.descriptor) if name]
        return ".".join(names) if names else self.descriptor

    # ── Descriptor grammar (static parsing helpers owned by this class) ──

    @staticmethod
    def _strip_prefix(raw: str) -> str:
        """Strip ``scip-python python <pkg> <ver> `` → the descriptor tail.

        ``<pkg>`` and ``<ver>`` never contain spaces, so a 4-way split is exact;
        the descriptor is returned verbatim (it is the stitching key, so it must
        stay byte-identical across projects).
        """
        if not raw.startswith(_SCIP_PYTHON_PREFIX):
            return raw
        parts = raw.split(" ", 4)
        return parts[4] if len(parts) == 5 else ""

    @staticmethod
    def _leaf_kind(suffix: str) -> LeafKind:
        """Map a final component's suffix kind to a node-mappable :class:`LeafKind`."""
        if suffix == "type":
            return LeafKind.TYPE
        if suffix == "method":
            return LeafKind.CALLABLE
        if suffix == "meta":
            return LeafKind.MODULE
        return LeafKind.OTHER

    @classmethod
    def _tokenize(cls, descriptor: str) -> list[tuple[str, str]]:
        r"""Split a descriptor into ``(name, suffix_kind)`` components.

        Handles backtick-escaped names (``\`pkg.mod\``), the ``name().`` method
        form, and the ``(name)`` / ``[name]`` parameter forms. Resilient by
        construction: an unrecognised character advances one position so a
        malformed descriptor can never loop forever — it degrades to
        fewer/`unknown` components, never a hang.
        """
        components: list[tuple[str, str]] = []
        i = 0
        n = len(descriptor)
        while i < n:
            char = descriptor[i]
            if char == "(":  # parameter: (name)
                name, i = cls._read_name(descriptor, i + 1)
                if i < n and descriptor[i] == ")":
                    i += 1
                components.append((name, "parameter"))
                continue
            if char == "[":  # type parameter: [name]
                name, i = cls._read_name(descriptor, i + 1)
                if i < n and descriptor[i] == "]":
                    i += 1
                components.append((name, "typeparameter"))
                continue
            name, i = cls._read_name(descriptor, i)
            if i >= n:
                components.append((name, "unknown"))
                break
            suffix = descriptor[i]
            if suffix in _SUFFIX_KIND:
                components.append((name, _SUFFIX_KIND[suffix]))
                i += 1
            elif suffix == "(":  # method: name(<disambiguator>).
                j = descriptor.find(")", i + 1)
                i = (j + 1) if j != -1 else n
                if i < n and descriptor[i] == ".":
                    i += 1
                components.append((name, "method"))
            else:  # unexpected separator — skip it, keep going
                components.append((name, "unknown"))
                i += 1
        return components

    @staticmethod
    def _read_name(descriptor: str, i: int) -> tuple[str, int]:
        r"""Read one component name starting at *i*; return ``(name, next_index)``.

        A backtick-escaped name runs to its closing backtick (a doubled ``\`\```
        is a literal backtick); a bare name runs up to the next terminator.
        """
        n = len(descriptor)
        if i < n and descriptor[i] == "`":
            i += 1
            buf: list[str] = []
            while i < n:
                if descriptor[i] == "`":
                    if i + 1 < n and descriptor[i + 1] == "`":
                        buf.append("`")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(descriptor[i])
                i += 1
            return "".join(buf), i
        start = i
        while i < n and descriptor[i] not in _NAME_TERMINATORS:
            i += 1
        return descriptor[start:i], i
