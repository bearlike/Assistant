#!/usr/bin/env python3
"""Pluggable AST linter for stlite widget code.

Design — functional pipeline, not an OO hierarchy:

* A :data:`LintRule` is a pure function ``(tree, source) -> Iterable[LintFinding]``.
* :data:`DEFAULT_RULES` is an explicit, ordered tuple of rules — composition
  is visible at a glance and swappable per-call.
* :func:`lint` parses once and fans the AST out to every rule, collecting
  findings into a flat list.

Adding a rule is a three-line change: write a function with the right
signature, append it to ``DEFAULT_RULES``, unit-test it with an AST fixture.
No registry, no decorator, no base class, no import-order magic.

Paradigm rationale (vs alternatives):

* OO rule classes — adds subclass boilerplate for zero behavior; each rule
  is one function, a class would be noise.
* Decorator registry — implicit order, breaks with import-order changes.
* Visitor-per-rule — each visitor walks the tree separately (wasteful)
  or they share state (couples rules to each other).

Future splits are trivial: this file → ``linter/__init__.py`` + per-rule
modules when it grows past ~5 rules.  Public API (``lint``,
``LintFinding``, ``DEFAULT_RULES``, ``ALLOWED_MODULES``) stays stable.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "ALLOWED_MODULES",
    "DEFAULT_RULES",
    "LintFinding",
    "LintRule",
    "check_forbidden_patterns",
    "check_imports",
    "format_findings",
    "lint",
]


# ------------------------------------------------------------------
# Data
# ------------------------------------------------------------------

#: Modules a widget's ``app.py`` may import.  Authoritative — the agent
#: prompt mirrors this list in prose and a parity test enforces the match.
#:
#: Scope: stdlib bits commonly used in small data widgets, plus the
#: stlite-compatible data libs that ship as pre-built pyodide wheels or
#: install cleanly via micropip.
ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # Stlite runtime core
        "streamlit",
        # Data libraries (pre-built pyodide wheels or pure-Python)
        "pandas",
        "numpy",
        "altair",
        "plotly",
        # Stdlib used by small widgets
        #
        # ``__future__`` is a compile-time directive (e.g. ``from __future__
        # import annotations``), not a runtime import.  Always available in
        # every Python 3.x, zero runtime cost, no stlite implications.
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "html",
        "itertools",
        "json",
        "math",
        "random",
        "re",
        "statistics",
        "textwrap",
        "typing",
        "uuid",
    }
)


@dataclass(frozen=True)
class LintFinding:
    """A single issue surfaced by a lint rule.

    Keep this flat — the point is that ``MockSpeaker.content`` can render a
    finding as one line without the agent needing to parse nested structure.
    """

    rule: str  #: Stable ID (e.g. ``"unsupported-import"``). Used for filtering/suppression.
    message: str  #: Human-readable; self-contained; names the offender.
    line: int  #: 1-based line number. ``0`` means "file-level" (e.g. SyntaxError).


#: A lint rule: pure function of the parsed AST + source text.
LintRule = Callable[[ast.AST, str], Iterable[LintFinding]]


# ------------------------------------------------------------------
# Rules
# ------------------------------------------------------------------


def _top_level(module: str | None) -> str | None:
    """Return the top-level package of a dotted import path.

    ``foo.bar.baz`` → ``"foo"``; ``None`` or empty → ``None``.  Relative
    imports (``from .sibling import x``) have ``module`` ``None`` and
    are ignored — they can't reach across the allowlist boundary.
    """
    if not module:
        return None
    head, _, _ = module.partition(".")
    return head or None


def check_forbidden_patterns(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject Streamlit APIs that are incompatible with or inappropriate for the widget panel.

    Hard failures (not rendered / actively breaks the widget):
    - ``st.sidebar.*`` — the sidebar DOM is not present in the stlite panel.
    - ``st.set_page_config()`` — stlite config is owned by the console; calling
      this conflicts with the console's ``streamlitConfig`` init options.
    """
    for node in ast.walk(tree):
        # st.sidebar.* — detect the st.sidebar attribute node itself so we fire
        # once per access site regardless of which sidebar method is called.
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "sidebar"
            and isinstance(node.value, ast.Name)
            and node.value.id == "st"
        ):
            yield LintFinding(
                rule="forbidden-sidebar",
                message=(
                    "st.sidebar is not rendered in the widget panel — "
                    "use st.columns() for side-by-side layout instead"
                ),
                line=node.lineno,
            )
        # st.set_page_config() — detect the call node
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_page_config"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
        ):
            yield LintFinding(
                rule="forbidden-set-page-config",
                message=(
                    "st.set_page_config() is controlled by the Mewbo console — remove this call"
                ),
                line=node.lineno,
            )


def check_imports(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject any top-level import whose module isn't in :data:`ALLOWED_MODULES`.

    Covers ``import X``, ``import X.Y``, ``import X as Z``,
    ``from X import Y``, ``from X.Y import Z``.  Relative imports
    (``from . import x``) pass through — they resolve within the widget
    directory and can't pull in external modules.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level(alias.name)
                if top and top not in ALLOWED_MODULES:
                    yield LintFinding(
                        rule="unsupported-import",
                        message=(
                            f"module {top!r} is not available in stlite/pyodide"
                        ),
                        line=node.lineno,
                    )
        elif isinstance(node, ast.ImportFrom):
            top = _top_level(node.module)
            if top and top not in ALLOWED_MODULES:
                yield LintFinding(
                    rule="unsupported-import",
                    message=(
                        f"module {top!r} is not available in stlite/pyodide"
                    ),
                    line=node.lineno,
                )


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------


#: Default rule set — ordered, explicit.  Add new rules by appending here.
DEFAULT_RULES: tuple[LintRule, ...] = (check_forbidden_patterns, check_imports)


def lint(
    source: str,
    rules: Sequence[LintRule] = DEFAULT_RULES,
) -> list[LintFinding]:
    """Run every *rule* against *source* and return all findings.

    Parses once; rules consume the shared AST.  A syntax error short-circuits
    with a single ``"syntax"`` finding — no rule sees a broken tree.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            LintFinding(
                rule="syntax",
                message=exc.msg or "invalid syntax",
                line=exc.lineno or 0,
            )
        ]
    return [finding for rule in rules for finding in rule(tree, source)]


def format_findings(findings: Sequence[LintFinding]) -> str:
    """Render findings as a newline-delimited, agent-readable list.

    Format: ``- line N: <message> [<rule>]``.  Keeps the rule ID visible so
    a later suppress-comment mechanism can reference it by name.
    """
    return "\n".join(
        f"- line {f.line}: {f.message} [{f.rule}]" for f in findings
    )
