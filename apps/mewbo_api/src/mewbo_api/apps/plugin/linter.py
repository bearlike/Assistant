#!/usr/bin/env python3
"""AST linter for Mewbo Apps frontend code — the widget linter, re-tabled for apps.

An **app** is a full multi-page stlite frontend, not the single result-card a
*widget* is, so this linter is the widget linter (`mewbo_core.builtin_plugins.
widget_builder.linter`) with a deliberately different *table*, not a fork of the
engine:

* **`lint()` / `LintFinding` / `LintRule` / `format_findings` are reused verbatim**
  (imported below) — the parse-once-fan-out runner and the finding shape are
  identical, and `lint(source, rules=...)` already takes the rule set as a
  parameter, so this module supplies :data:`APP_RULES` instead of the widget's
  `DEFAULT_RULES`.
* **The allowlist GAINS `mewbo_app`** (the injected SDK, the app's ONE sanctioned
  network path — see `sdk/mewbo_app.py`) and nothing else. Every direct network
  module (`requests`/`httpx`/`urllib`/`aiohttp`/`socket`/`http`) and every WASM
  escape hatch (`js`, `pyodide`, `pyodide.http`) is STILL banned, because it is
  simply absent from the allowlist — the allowlist approach bans by omission.
* **The page-chrome ban is DROPPED** (`st.header`/`st.subheader`/`st.divider`,
  `st.sidebar`, `st.tabs` are all fine): a widget is one bordered card, an app is
  a page — multi-section layout and sidebar navigation are the point. The delta
  from the widget rules is exactly this relaxation.
* **`st.set_page_config()` stays banned** — that is a *host-config* conflict, not a
  layout opinion: stlite config is owned by the console/WebView host
  (`streamlitConfig` in `stliteBoot.ts`), and an app calling it fights the host
  the same way a widget would, app-vs-widget notwithstanding.
* **Dynamic execution is banned for apps** (`__import__`, `importlib.import_module`,
  `eval`, `exec`, `compile`) — an app is a DURABLE, re-woken entity whose only
  sanctioned egress is the SDK, so a static allowlist that a `__import__("requests")`
  could walk straight around would be security theatre. Widgets are ephemeral
  display and don't carry this rule; apps do. This is the "keep banning js-raw"
  requirement generalized to every allowlist-bypass, not just the `js` name.

A `mode="code"` PIPELINE file (Phase 2) is NOT linted by this module at
all — `pipeline_runner.py:lint_pipeline`/`PIPELINE_ALLOWED_MODULES` is the
ONE canonical pipeline lint (it gates ACTUAL execution: an import outside its
allowlist fails the guarded `__import__` at runtime regardless of any static
check, so a second, independent pipeline rule table here would just be a
second place to keep in sync — or drift). `submit_app.py`'s `_lint_frontend`
imports `lint_pipeline` FROM `pipeline_runner.py` and routes a pipeline's
`entrypoint` file to it instead of `lint_app` (the browser import allowlist
below does not apply server-side and would reject ordinary stdlib
data-processing imports like `csv`/`hashlib` with a false positive). This
module's `check_dynamic_execution` is reused BY `pipeline_runner.py` (one
function, two callers) — see that module for the pipeline rule table itself.
`check_pipeline_error_swallow` is the same story: a second pipeline-only rule
defined here so it shares the parse-once-fan-out runner, meant to be imported
into `pipeline_runner.py`'s rule table the same way — it is deliberately
absent from `APP_RULES` below, since a browser frontend file never calls
`ctx.read_file`/`ctx.glob`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from typing import ClassVar

from mewbo_core.builtin_plugins.widget_builder.linter import (
    ALLOWED_MODULES as _WIDGET_ALLOWED_MODULES,
    LintFinding,
    LintRule,
    format_findings,
    lint,
)

__all__ = [
    "ALLOWED_MODULES",
    "APP_RULES",
    "LintFinding",
    "LintRule",
    "check_dynamic_execution",
    "check_forbidden_patterns",
    "check_imports",
    "check_pipeline_error_swallow",
    "format_findings",
    "lint_app",
]


# ------------------------------------------------------------------
# Data — the widget allowlist plus the injected SDK
# ------------------------------------------------------------------

#: Modules an app's Python files may import. The widget allowlist (streamlit,
#: pandas/numpy/altair/plotly, and the small-widget stdlib set) PLUS ``mewbo_app``
#: — the SDK file injected into every app bundle, which owns the ONLY sanctioned
#: network path (``data.query`` / ``system.*``). Everything not listed here — every
#: direct HTTP client and every WASM/JS bridge — is banned by omission.
ALLOWED_MODULES: frozenset[str] = frozenset(_WIDGET_ALLOWED_MODULES | {"mewbo_app"})

#: Call names an app may not invoke — the dynamic-execution / allowlist-bypass set.
#: A durable, network-capable app must not be able to reach around the static
#: import allowlist (``__import__("requests")``) or the SDK-only egress rule.
_BANNED_CALLS: frozenset[str] = frozenset(
    {"__import__", "eval", "exec", "compile"}
)


# ------------------------------------------------------------------
# Rules
# ------------------------------------------------------------------


def _top_level(module: str | None) -> str | None:
    """Top-level package of a dotted import path (``foo.bar`` -> ``"foo"``).

    Local reimplementation of the widget linter's private helper (a one-liner
    not part of its public API) rather than reaching for its underscore name.
    Relative imports (``module`` is ``None``) return ``None`` and are ignored —
    they resolve within the app bundle and can't cross the allowlist boundary.
    """
    if not module:
        return None
    head, _, _ = module.partition(".")
    return head or None


def check_imports(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject any top-level import whose module isn't in :data:`ALLOWED_MODULES`.

    Same shape as the widget rule, but resolved against the APP allowlist (which
    adds ``mewbo_app``). Covers ``import X``/``import X.Y``/``from X import Y``;
    relative imports pass through.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level(alias.name)
                if top and top not in ALLOWED_MODULES:
                    yield LintFinding(
                        rule="unsupported-import",
                        message=(
                            f"module {top!r} is not available in stlite/pyodide "
                            f"(reach the network through the injected `mewbo_app` SDK, "
                            f"never a raw HTTP client)"
                        ),
                        line=node.lineno,
                    )
        elif isinstance(node, ast.ImportFrom):
            top = _top_level(node.module)
            if top and top not in ALLOWED_MODULES:
                yield LintFinding(
                    rule="unsupported-import",
                    message=(
                        f"module {top!r} is not available in stlite/pyodide "
                        f"(reach the network through the injected `mewbo_app` SDK, "
                        f"never a raw HTTP client)"
                    ),
                    line=node.lineno,
                )


def check_forbidden_patterns(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject ``st.set_page_config()`` — a host-config conflict, app or widget.

    Unlike the widget rule this does NOT flag ``st.sidebar`` or the section
    primitives (``st.header``/``st.subheader``/``st.divider``): an app is a full
    page and legitimately uses them. ``st.set_page_config`` alone stays banned —
    stlite config is owned by the console/WebView host, and an app calling it
    conflicts with the host's ``streamlitConfig`` init options.
    """
    for node in ast.walk(tree):
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
                    "st.set_page_config() is controlled by the Mewbo host "
                    "(console/WebView) — remove this call"
                ),
                line=node.lineno,
            )


def check_dynamic_execution(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject dynamic-execution / allowlist-bypass calls.

    ``__import__``/``eval``/``exec``/``compile`` and ``importlib.import_module``
    each let app code reach around the static import allowlist (and thus the
    SDK-only network rule). An app is durable and network-capable, so this is a
    security boundary, not a style rule. Widgets omit it (ephemeral, no SDK).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _BANNED_CALLS:
            yield LintFinding(
                rule="forbidden-dynamic-exec",
                message=(
                    f"{func.id}() is not allowed — it bypasses the import "
                    f"allowlist and the SDK-only network rule"
                ),
                line=node.lineno,
            )
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        ):
            yield LintFinding(
                rule="forbidden-dynamic-exec",
                message=(
                    "importlib.import_module() is not allowed — it bypasses the "
                    "import allowlist"
                ),
                line=node.lineno,
            )


# ------------------------------------------------------------------
# Pipeline-only rule — the submit-time verifier disarm trap
#
# ``ctx.read_file``/``ctx.glob`` (the pipeline `ctx` surface — see
# ``PipelineContext`` in ``pipeline_runner.py``) raise ``PipelineExecutionError``
# on a bad path (absolute, traversal-escaping, or missing). A pipeline that
# wraps one of those calls in a broad `except` and does not re-raise turns a
# genuine failure into "read nothing, report success" — and, worse, makes the
# submit-time verifier (`AppLifecycle._verify_pipelines`) unable to catch it:
# the verifier dry-runs the pipeline and refuses a submit whose
# `PipelineExecutionError` reaches it, but a swallowed error never does.
# ------------------------------------------------------------------

#: ``except`` type names that would catch a ``PipelineExecutionError`` — the two
#: universal bases, and the class itself by name. A pipeline's sandboxed exec
#: namespace never binds the name ``PipelineExecutionError`` (it isn't importable
#: — not in :data:`~mewbo_api.apps.pipeline_runner.PIPELINE_ALLOWED_MODULES`, and
#: not injected as a global), so `except PipelineExecutionError:` NameErrors on
#: its first real failure rather than ever matching — still flagged here on
#: purpose: it signals the same swallow-intent, and treating it identically
#: costs nothing while covering a future where the sandbox exposes the name.
_ERROR_SWALLOWING_NAMES: frozenset[str] = frozenset(
    {"Exception", "BaseException", "PipelineExecutionError"}
)

#: `ctx` attribute names guarded by workspace/traversal checks that raise
#: `PipelineExecutionError`. Matched by attribute name alone, not receiver
#: identity — `ctx` is only the SDK's conventional `run(params, ctx)` parameter
#: name, never an enforced one, so there is no reliable binding to check against.
#:
#: `exec` belongs here for the SAME reason `read_file` does: it raises the same
#: error class, so `except Exception: return {}` around it reproduces the disarm
#: trap verbatim — the submit-time verifier can only classify what PROPAGATES,
#: so a swallowed refusal ships a live app whose every run reports success and
#: reads nothing. Adding a name here IS a new lint rule, which the live-fleet law
#: says can retroactively fail an already-live pipeline at its next fire; it is
#: free ONLY because no stored app version can contain a `ctx.exec` call yet.
#: Adding the next one will not be free — lint every stored version first.
_GUARDED_CTX_CALLS: frozenset[str] = frozenset({"read_file", "glob", "exec"})


class _GuardedTry:
    """One ``try`` statement, analysed for a swallowed pipeline-error signal.

    An atomic analysis unit: the node is the state, every question asked of it
    is a method. The alternative — a cluster of free functions each re-deriving
    which handler and which calls are in play — is the shape this class exists
    to avoid, since all of them operate on exactly this one node.

    The RULE stays a module-level function (:func:`check_pipeline_error_swallow`)
    because the shared ``lint`` runner consumes a table of rule callables; this
    class is what that thin function delegates to.
    """

    #: Exception types whose capture would also capture ``PipelineExecutionError``.
    SWALLOWING_TYPES: ClassVar[frozenset[str]] = _ERROR_SWALLOWING_NAMES
    #: ``ctx`` attribute names guarded by checks that raise ``PipelineExecutionError``.
    GUARDED_CALLS: ClassVar[frozenset[str]] = _GUARDED_CTX_CALLS

    def __init__(self, node: ast.Try) -> None:
        self._node = node

    @staticmethod
    def _walk(stmts: Sequence[ast.stmt]) -> Iterable[ast.AST]:
        """``ast.walk`` fanned out over a statement list (a bare list isn't a node)."""
        for stmt in stmts:
            yield from ast.walk(stmt)

    @property
    def guarded_calls(self) -> list[ast.Call]:
        """Every guarded ``ctx`` call in the try body, at ANY nesting depth.

        Depth matters: the call may sit inside an ``if``/``for``/``with``, or
        even inside a NESTED try's own handler — still lexically and
        dynamically inside this try, so still swallowed by this try's handler.
        """
        return [
            call
            for call in self._walk(self._node.body)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in self.GUARDED_CALLS
        ]

    @classmethod
    def _catches_pipeline_error(cls, expr: ast.expr | None) -> bool:
        """True if an ``except`` clause's type expression would catch the signal.

        Covers a bare ``except:`` (*expr* is ``None``), a name in
        :attr:`SWALLOWING_TYPES`, or any of those inside an ``except (A, B):``
        tuple. A narrow, unrelated exception (``ValueError``, a custom error)
        returns ``False`` — the rule targets swallowing the pipeline's own
        failure signal, not ordinary exception handling.
        """
        if expr is None:
            return True
        if isinstance(expr, ast.Name):
            return expr.id in cls.SWALLOWING_TYPES
        if isinstance(expr, ast.Tuple):
            return any(cls._catches_pipeline_error(elt) for elt in expr.elts)
        return False

    @classmethod
    def _reraises(cls, handler: ast.ExceptHandler) -> bool:
        """True if *handler* contains a ``raise`` anywhere in its body.

        DECISION (deliberate, not the obvious default): "contains a ``raise``
        anywhere" — not "every path raises". A real repair for this exact bug
        class narrows on ONE tolerable failure and re-raises everything else:

        ```python
        except Exception as exc:
            if getattr(exc, "code", None) == "read":
                return []      # a genuinely-missing optional file
            raise              # everything else propagates
        ```

        That ``raise`` sits under an ``if``, so "every path through the handler
        re-raises" is FALSE — a stricter reachability/control-flow check would
        flag this as a swallow and BLOCK A CORRECT FIX. Syntactic presence is
        the looser, deliberately-chosen bar, at the KNOWN cost that a contrived
        ``if False: raise`` could satisfy it while never re-raising. That gap
        requires deliberate evasion, not the sloppiness this rule targets — a
        linter that blocks a legitimate narrowed-catch repair is worse than one
        that misses a hand-crafted bypass.
        """
        return any(isinstance(node, ast.Raise) for node in cls._walk(handler.body))

    @property
    def swallowing_handler(self) -> ast.ExceptHandler | None:
        """The FIRST handler that catches the signal without re-raising, if any.

        First-only by design: one swallowing handler already explains every
        guarded call under this try, so reporting per-handler would multiply
        one defect into N findings.
        """
        for handler in self._node.handlers:
            if self._catches_pipeline_error(handler.type) and not self._reraises(handler):
                return handler
        return None

    @staticmethod
    def _describe(call: ast.Call) -> str:
        """Best-effort ``receiver.method()`` label for a finding message.

        Only ever reached for a call whose ``func`` :attr:`guarded_calls`
        already matched as an ``ast.Attribute``; the ``isinstance`` re-narrows
        for the type checker rather than asserting it.
        """
        func = call.func
        if not isinstance(func, ast.Attribute):
            return "<pipeline call>"
        receiver = func.value
        if isinstance(receiver, ast.Name):
            return f"{receiver.id}.{func.attr}()"
        return f".{func.attr}()"

    def findings(self) -> Iterable[LintFinding]:
        """One finding per guarded call, iff a handler swallows the signal."""
        calls = self.guarded_calls
        if not calls:
            return
        handler = self.swallowing_handler
        if handler is None:
            return
        for call in calls:
            yield LintFinding(
                rule="swallowed-pipeline-error",
                message=(
                    f"{self._describe(call)} can raise PipelineExecutionError "
                    f"on a bad path; the `except` at line {handler.lineno} catches it "
                    "and does not re-raise, so this pipeline can read nothing and still "
                    "report success. This also disarms the submit-time verifier "
                    "(AppLifecycle._verify_pipelines), which can only refuse a submit "
                    "whose failure actually propagates — re-raise the exception, or "
                    "catch a narrower one instead."
                ),
                line=call.lineno,
            )


def check_pipeline_error_swallow(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject a broad `except` that swallows a `ctx.read_file`/`ctx.glob` failure.

    A thin adapter over :class:`_GuardedTry` — the rule table wants a callable,
    the analysis wants a cohesive home. See that class for every decision this
    rule embodies (nesting depth, which exception types count, and why
    re-raise detection is syntactic rather than control-flow exact).
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            yield from _GuardedTry(node).findings()


# ------------------------------------------------------------------
# Pipeline (the app rule table's assembly + entry point)
# ------------------------------------------------------------------

#: Ordered, explicit app rule set — supplied to the reused widget ``lint`` runner.
APP_RULES: tuple[LintRule, ...] = (
    check_imports,
    check_forbidden_patterns,
    check_dynamic_execution,
)


def lint_app(source: str) -> list[LintFinding]:
    """Run the app rule set over one Python file (thin alias for ``lint(..., APP_RULES)``).

    Reuses the widget linter's parse-once-fan-out runner verbatim (syntax errors
    short-circuit to a single ``"syntax"`` finding); only the rule table differs.
    """
    return lint(source, rules=APP_RULES)
