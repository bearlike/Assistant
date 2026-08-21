#!/usr/bin/env python3
"""``AppPipelineRunner`` — the deterministic code-pipeline execution engine.

A ``mode="code"`` pipeline is a Python file in the app bundle exposing
``def run(params: dict, ctx) -> Any``. This class executes it — with NO LLM call
— at two seams: a trigger fire (the platform runs the engine synchronously and
writes a ``{kind:"scheduled"}`` ledger row, never re-engaging the maintainer
session) and on demand (the ``run_pipeline`` SessionTool + the REST run endpoint).
It is the ONE new primitive added for code pipelines; everything else rides existing seams.

## Execution substrate — in-process ``exec`` in a curated namespace (v1)

The pipeline code is agent-authored under the SAME trust envelope as an agentic
pipeline run, which already executes arbitrary shell (``aider_shell_tool``) in this
container — so a curated in-process ``exec`` is NOT a privilege escalation over what
the app substrate already permits. It is gated three ways, mirroring the app
linter's philosophy (allowlist-by-omission + no dynamic-exec) at the point of
execution rather than only at submit:

1. **A static lint** (reusing the widget linter's parse-once ``lint`` engine with a
   pipeline rule table: an import allowlist + the app linter's dynamic-exec ban)
   rejects the file before it ever runs.
2. **A curated ``__builtins__``** with ``open``/``eval``/``exec``/``compile``/``input``
   removed and a **guarded ``__import__``** that admits only
   :data:`PIPELINE_ALLOWED_MODULES` — so a name the static lint somehow missed still
   cannot import at runtime.
3. **A wall-clock watchdog** (a daemon worker thread joined with a timeout) and an
   **output size cap**.

**Honesty about the watchdog:** Python threads are not preemptible, so an
uncooperative infinite loop cannot be force-killed in-process — the watchdog
returns control to the caller with a ``timeout`` error, but the runaway daemon
thread lingers (near-zero cost if it blocks/sleeps; one core if it busy-spins)
until the process recycles. A subprocess harness with a hard kill is the phase-2.5
hardening; the in-process engine is the smallest coherent v1 and is documented as
such here rather than pretended otherwise.

**What the watchdog DOES bound is the durable half, and that is the correctness
half.** A lingering thread whose writes still land is not a slow run, it is a
FALSE report: the caller is told ``timeout``/``failed`` while the collection keeps
filling from a run nobody is waiting for and no ledger row describes. So the
watchdog sets a cooperative stop on the run's :class:`PipelineContext` BEFORE it
raises, and every side-effecting surface (``ctx.collection(…).upsert``/``delete``,
``ctx.exec``, ``ctx.llm``) checks it and REFUSES. The thread keeps burning CPU
until it exits; it stops changing the world. What it wrote BEFORE the deadline is
real, so it rides out on ``PipelineExecutionError.docs_written`` and into the
``failed`` row rather than being reported as nothing.

## Where pipeline code lives (the placement decision)

Pipeline files ride the SAME ``AppSpec.frontend.files`` map under ``pipelines/…``
keys (``PipelineSpec.entrypoint`` names one), NOT a new facet — the smallest diff
(the builder's ``submit_app`` reads every bundle file off disk already, so a
``pipelines/foo.py`` lands with ZERO submit-tool change) and the SDK-injection seam
already proves files-map mechanics.

:data:`PIPELINE_ALLOWED_MODULES` + :func:`lint_pipeline` are the ONE canonical
pipeline lint: ``submit_app`` routes ``mode="code"`` entrypoints to
:func:`lint_pipeline` at submit (frontend files still get the widget/app lint), and
this runner re-runs it at execution — the SAME allowlist gates both boundaries, no
frontend-vs-pipeline drift. The one accepted v1 consequence, documented not hidden:
pipeline source is served to the browser as part of the stlite bundle (waste + a
source leak to whoever can open the app). Harmless for the single-user apps of v1;
a non-served ``pipelines`` facet is the phase-2.5 cleanup.

## Collaborators (DI'd fields — atomic-class rule, models never import I/O)

``app_store`` (resolve ``app_id`` → :class:`AppSpec` for the id-keyed
``run_pipeline`` seam), ``app_data`` (the real data plane — ``ctx`` writes ride it
so schema validation + the ``max_docs`` cap are the SAME enforcement seam
``app_data`` uses, never duplicated), ``workspace_resolver`` (a callable mapping an
app to its workspace cwd — models never import I/O, so the app edge injects it),
``clock`` (the ``run_pipeline`` seam threads no ``now``; ``execute`` takes ``now``
as the authoritative arg and falls back to the clock), and ``cache`` (a
process-local dict — the declare-don't-infer TTL cache; a real store is out of
scope for v1).

## Controlled CLI/network egress — ``ctx.exec``

The sandbox's default posture is NO subprocess and NO network, which would
otherwise force a deterministic CLI-plumbed sync (git, tea, gh) into
``mode="agentic"`` purely to reach ``aider_shell_tool``.
:meth:`PipelineContext.exec` is the narrow, opt-in alternative: a pipeline that
DECLARES ``allow_exec``/``allow_egress`` on its
:class:`~mewbo_api.apps.models.PipelineSpec` may run one of the vetted binaries
in :data:`~mewbo_api.apps.models.PIPELINE_ALLOWED_EXEC`, argv-only (never
``shell=True`` — there is no command string to inject into), against hosts it
named. Undeclared (both lists empty, the default) ⇒ it raises before touching
:mod:`subprocess` at all, mirroring ``ctx.llm``'s declare-a-budget-or-it-raises
posture.

**What this actually bounds — read this before relying on it.** ``allow_exec``
is a DECLARATION and an accident-guard, NOT a sandbox. The runner does not
interpret subcommand semantics, and ``git`` can be driven to run other programs
through its own configuration options, so a pipeline that declares ``git``
holds roughly what the pipeline's own code already holds. That is consistent
with the substrate above (agent-authored code under the same trust envelope as
an agentic run, which already shells out) — the value here is that the
declaration is EXPLICIT, auditable on the manifest, and refuses the undeclared
by default. It is not a boundary against a hostile pipeline, and documenting it
as one would be the lie that gets relied upon.

The gates, all applied BEFORE a process is spawned:

1. **Binary + egress allowlist** — owned by
   :meth:`~mewbo_api.apps.models.PipelineSpec.check_exec_allowed`, on the model
   that declares the lists. ``argv[0]`` must be in ``allow_exec``, and every
   remote-shaped token (``scheme://host/…`` or git's ``[user@]host:path``, the
   user part OPTIONAL) must resolve to a declared host. A host-less invocation
   (``git log``) passes for free.
2. **Hardened argv + env** — git gets ``-c credential.helper=`` (the mounted
   read-only credential store otherwise turns an auth rejection into an EBUSY
   that masks the real error) and the shared
   :func:`mewbo_graph.plugins.wiki.clone.hardened_git_env`, which also blanks
   the GLOBAL and SYSTEM git config. That second half is what makes the
   ``-c`` ban below hold: an operator's global ``url.<a>.insteadOf=<b>``
   rewrites the remote AFTER ``allow_egress`` has vetted the argv, so an argv
   naming a declared host reached an undeclared one — the same redirect the
   banned flag exists to stop, arriving by the other door. Every binary gets a
   terminal-prompt-off/pager-off overlay over the SERVER PROCESS ENVIRONMENT,
   which it INHERITS in full — deliberately, because the credential posture
   below depends on it.
3. **Bounds** — a per-call wall-clock timeout clamped by the pipeline's own
   ``timeout_seconds``, killed as a PROCESS GROUP so a spawned helper dies with
   it; a byte-accurate output cap; and stdout/stderr *and the timeout message*
   redacted through :func:`mewbo_core.contracts.secret_redaction.get_secret_redactor`
   before reaching the pipeline or a ``PipelineRun.error`` ledger row.
4. **No dry runs** — a dry run REFUSES exec (code ``dry_run``). ``ctx.llm`` may
   run under one because it cannot mutate the world; a subprocess can, and
   ``AppLifecycle.submit`` dry-runs every code pipeline as its verifier.

**Credential HONESTY (documented, not hidden):** ``ctx.exec`` does NOT resolve
or inject a per-pipeline credential. Authentication rides whatever AMBIENT
state the deployment host already has for that CLI — an SSH agent, a `tea
login`/`gh auth login` session. NOT a ``git credential.helper``: the
``-c credential.helper=`` above disables it deliberately, so an HTTPS git remote
has no credential here at all and only SSH-agent auth reaches one. The same
posture ``aider_shell_tool`` has on the agentic path, and the reason the
env is inherited rather than scrubbed (an SSH agent needs ``SSH_AUTH_SOCK``;
`tea`/`gh` need ``HOME``). The accepted consequence, stated plainly: an
allowlisted binary sees the API process's environment. This unblocks the
read-mostly sync flows (`git log`/`git diff`/`tea issues list` against an
already-cloned, already-authenticated workspace); a per-pipeline STORED
credential (the wiki clone tool's `resolve_chain`) is a deliberate v2 candidate
once a real per-app need shows up, not speculated here.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
from mewbo_core.builtin_plugins.widget_builder.linter import lint
from mewbo_core.common import get_logger
from mewbo_core.contracts.secret_redaction import SecretRedactor, get_secret_redactor
from mewbo_graph.plugins.wiki.clone import hardened_git_env
from mewbo_tools.integration.landlock import ShellScope, scoped_preexec

from mewbo_api.apps.plugin.linter import (
    LintFinding,
    check_dynamic_execution,
    check_pipeline_error_swallow,
    format_findings,
)

from .models import (
    PIPELINE_ALLOWED_EXEC,
    PIPELINE_TIMEOUT_CEILING_SECONDS,
    PipelineEvidence,
    PipelineResult,
)
from .store import CollectionCapExceeded

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterable, Mapping, MutableMapping

    from .models import AppSpec, CollectionSpec, PipelineSpec
    from .store import AppDataStoreBase, AppStoreBase

logging = get_logger(name="api.apps.pipeline_runner")

# The byte cap on a pipeline's JSON-serialized output. The wall-clock ceiling is
# DECLARED per-pipeline (``PipelineSpec.timeout_seconds``) — see the watchdog
# honesty note in the module docstring — not a single global here.
_DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024

# Bound a single ``ctx.collection().query`` so a runaway ``limit`` can't pull a whole
# collection into the pipeline (mirrors ``app_data``'s ``_MAX_QUERY_LIMIT``).
_MAX_QUERY_LIMIT = 1000

# ``ctx.exec`` caps — deliberately independent of the pipeline's
# JSON-output cap: a CLI's raw stdout/stderr is captured text, not the
# eventual JSON-serialized return value, so it gets its own bound.
_MAX_EXEC_OUTPUT_BYTES = 256 * 1024
# The per-call wall-clock ceiling ``ctx.exec`` joins the subprocess with — a
# smaller, separate budget from the pipeline's OWN ``timeout_seconds`` (which
# still bounds the whole ``run()`` call, including every ``ctx.exec``) so one
# runaway CLI invocation can't silently eat the entire pipeline budget.
_DEFAULT_EXEC_TIMEOUT_SECONDS = 30

# Sentinel occupying the pipeline-name slot of a shared cache key for a ``ctx.llm``
# result (content-addressed by ``sha256(prompt+schema)`` in the LAST slot). A real
# pipeline name can never collide: ``PipelineSpec.name`` is a slug that must START
# alphanumeric (``models._SLUG_RE``), so a leading ``_`` is unrepresentable.
_LLM_CACHE_SLOT = "__llm__"

#: Modules a pipeline file may import — a server-side data-transform allowlist.
#: Every direct network client (``socket``/``http``/``urllib``/``requests``/``httpx``),
#: every filesystem/process module (``os``/``sys``/``subprocess``/``pathlib``), and
#: every dynamic-import escape (``importlib``/``pickle``/``marshal``) is banned by
#: OMISSION — the app linter's philosophy, applied at execution. Broader than the
#: frontend allowlist (this runs server-side, not in pyodide) but the submit-time
#: frontend lint is a stricter gate in v1 (see the module docstring).
PIPELINE_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "__future__",
        "base64",
        "binascii",
        "calendar",
        "collections",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "hmac",
        "html",
        "io",
        "itertools",
        "json",
        "math",
        "random",
        "re",
        "secrets",
        "statistics",
        "string",
        "struct",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
        "uuid",
        "zoneinfo",
    }
)

# Builtins a pipeline may reach. ``open``/``eval``/``exec``/``compile``/``input`` and
# the raw ``__import__`` are DELIBERATELY absent (``__import__`` is replaced by a
# guarded one below) — a pipeline reads files through ``ctx.read_file`` and reaches
# the data plane through ``ctx.collection``, never the filesystem or a dynamic import.
_SAFE_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        # constructors / conversions
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
        "callable", "chr", "complex", "dict", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "getattr", "hasattr", "hash", "hex",
        "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "object", "oct", "ord", "pow", "print", "range", "repr",
        "reversed", "round", "set", "setattr", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip",
        # class machinery (a pipeline may define a dataclass / helper class)
        "__build_class__", "classmethod", "staticmethod", "property", "super",
        # constants
        "True", "False", "None", "NotImplemented", "Ellipsis",
        # exceptions a pipeline legitimately raises / catches
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "RuntimeError", "StopIteration", "ArithmeticError", "ZeroDivisionError",
        "AttributeError", "LookupError", "NotImplementedError", "AssertionError",
        "OverflowError", "UnicodeDecodeError", "UnicodeEncodeError",
    }
)

_REAL_IMPORT = builtins.__import__


def _guarded_import(
    name: str,
    globals: dict[str, Any] | None = None,  # noqa: A002 - the __import__ signature
    locals: dict[str, Any] | None = None,  # noqa: A002 - the __import__ signature
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """A drop-in ``__import__`` that admits only :data:`PIPELINE_ALLOWED_MODULES`.

    The runtime backstop to the static import lint: even a name the AST walk missed
    (or a hand-crafted ``__import__("os")``) fails here. Relative imports (``level >
    0``) are refused outright — a single-file pipeline has no package to resolve.
    """
    if level != 0:
        raise ImportError("relative imports are not allowed in a pipeline")
    top = name.split(".", 1)[0]
    if top not in PIPELINE_ALLOWED_MODULES:
        raise ImportError(
            f"import of {name!r} is not allowed in a code pipeline "
            f"(reach data through `ctx`, files through `ctx.read_file`)"
        )
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


def _build_safe_builtins() -> dict[str, Any]:
    """The curated ``__builtins__`` mapping handed to an exec'd pipeline namespace."""
    safe = {
        name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)
    }
    safe["__import__"] = _guarded_import
    return safe


_SAFE_BUILTINS: dict[str, Any] = _build_safe_builtins()


# ---------------------------------------------------------------------------
# Failure — always RAISED, never encoded into a PipelineResult
# ---------------------------------------------------------------------------


class PipelineExecutionError(Exception):
    """A code-pipeline failure surfaced to the caller (never a ``PipelineResult``).

    ``code`` buckets the failure (``params``/``entrypoint``/``lint``/``traversal``/
    ``schema``/``cap``/``timeout``/``output``/``runtime``/…) so a caller can map it
    (the REST endpoint → an HTTP status; the fire seam → a ``failed`` ledger row).
    ``str(err)`` is a clean, agent-visible ``"<code>: <message>"`` — the
    ``run_pipeline`` tool renders it verbatim, mirroring ``submit_app``'s "a
    lifecycle rejection is agent-visible feedback" convention.

    ``docs_written`` carries what the run had ALREADY written when it failed, so
    the ledger records a partial write rather than an empty one — a failure is not
    a guarantee that nothing landed, and the ``PipelineResult`` a caller would
    otherwise read the counts off is only ever produced on success. ``evidence``
    carries the same run's bounded `ctx.glob`/`ctx.read_file` observations, so a
    caller can see what the code DID reach before it failed. The runner fills
    both from the run's own :class:`PipelineContext`; a failure raised BEFORE a
    context exists (params, lint, entrypoint) leaves them empty, which is then
    the truth.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        docs_written: dict[str, int] | None = None,
        evidence: PipelineEvidence | None = None,
    ) -> None:
        """Bind failure details plus partial writes and bounded execution evidence.

        The evidence follows the same rule as ``docs_written``: a failure after a
        context exists is not a promise that neither I/O observation nor a write
        occurred. Failures before context construction retain the empty default.
        """
        self.code = code
        self.message = message
        self.docs_written: dict[str, int] = dict(docs_written or {})
        self.evidence = evidence or PipelineEvidence()
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Pipeline lint — the widget engine, a pipeline rule table
# ---------------------------------------------------------------------------


def _check_pipeline_imports(tree: ast.AST, _src: str) -> Iterable[LintFinding]:
    """Reject any top-level import outside :data:`PIPELINE_ALLOWED_MODULES`.

    Same shape as the app linter's ``check_imports`` but resolved against the
    server-side pipeline allowlist; relative imports pass through (they resolve
    within the file and can't cross the allowlist boundary — the guarded importer
    refuses them at runtime regardless).
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                if top and top not in PIPELINE_ALLOWED_MODULES:
                    yield LintFinding(
                        rule="unsupported-import",
                        message=(
                            f"module {top!r} is not available in a code pipeline "
                            f"(reach data/files through `ctx`, never a raw import)"
                        ),
                        line=node.lineno,
                    )
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").partition(".")[0]
            if top and top not in PIPELINE_ALLOWED_MODULES:
                yield LintFinding(
                    rule="unsupported-import",
                    message=(
                        f"module {top!r} is not available in a code pipeline "
                        f"(reach data/files through `ctx`, never a raw import)"
                    ),
                    line=node.lineno,
                )


#: Ordered pipeline rule table fed to the reused widget ``lint`` runner — the
#: import allowlist, the app linter's dynamic-execution/allowlist-bypass ban
#: (``__import__``/``eval``/``exec``/``compile``/``importlib.import_module``),
#: and the swallowed-``PipelineExecutionError`` ban, all reused verbatim rather
#: than re-implemented. The last one is what keeps the submit-time verifier
#: meaningful: it can only classify what actually propagates out of
#: ``execute()``, so a pipeline catching a ``ctx.read_file``/``ctx.glob``
#: failure and returning a default verifies ``"pass"`` while reading nothing —
#: shipping a live app whose every run "succeeds" and writes no documents.
_PIPELINE_RULES = (
    _check_pipeline_imports,
    check_dynamic_execution,
    check_pipeline_error_swallow,
)


def lint_pipeline(source: str) -> list[LintFinding]:
    """Run the pipeline rule table over one file (syntax errors short-circuit)."""
    return lint(source, rules=_PIPELINE_RULES)


# ---------------------------------------------------------------------------
# Workspace helpers — the ONE glob/stat resolution shared by ``ctx`` and the
# source-mode fingerprint, so a recorded glob re-globs identically at verify time
# ---------------------------------------------------------------------------


def _glob_under_root(ws_root: Path, pattern: str) -> list[str]:
    """Workspace-relative POSIX paths matching *pattern*, traversal-guarded.

    The single source of truth for ``ctx.glob`` AND the source-mode liveness
    fingerprint: both re-glob through here so a recorded pattern resolves to the
    exact same set on a later run (a symlink escaping the root is skipped, never
    leaked). Only regular files are returned; results are sorted for determinism.
    """
    out: list[str] = []
    for path in sorted(ws_root.glob(pattern)):
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(ws_root)
        except ValueError:
            continue  # a symlink escaping the root — skip, never leak an outside path
        out.append(rel.as_posix())
    return out


def _stat_under_root(ws_root: Path, rel_path: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` for *rel_path* UNDER *ws_root*, or ``None`` if gone/escaping.

    Stat-only liveness input (no content read): a deleted, replaced, or
    traversal-escaping path returns ``None`` so it reads as a change in the
    fingerprint rather than silently matching.
    """
    try:
        candidate = (ws_root / rel_path).resolve()
        candidate.relative_to(ws_root)
        st = candidate.stat()
    except (OSError, ValueError):
        return None
    return st.st_mtime_ns, st.st_size


def _resolve_globs(ws_root: Path | None, patterns: Iterable[str]) -> dict[str, list[str]]:
    """Resolve each glob *pattern* to its CURRENT workspace-relative matches.

    The re-glob step of the PRE-run liveness check: a stored manifest carries only
    the patterns (a NEW file matching one must be discovered against the live
    filesystem, so this can't be avoided — it is the inherent walk). ``ws_root is
    None`` maps every pattern to ``[]``. Results are the sorted, traversal-guarded
    output of :func:`_glob_under_root`, so a pattern resolves identically here and
    at record time in :meth:`PipelineContext.glob` — the two fingerprints match iff
    the source is unchanged.
    """
    if ws_root is None:
        return {pattern: [] for pattern in sorted(set(patterns))}
    return {pattern: _glob_under_root(ws_root, pattern) for pattern in sorted(set(patterns))}


def _source_fingerprint(
    ws_root: Path | None,
    read_paths: Iterable[str],
    glob_results: Mapping[str, Iterable[str]],
) -> str:
    """A stat-based fingerprint of everything a source-mode run read/globbed.

    sha256 over the sorted ``(path, mtime_ns, size)`` stats of every recorded read
    file AND every glob result, PLUS the glob result SETS themselves — so a NEW file
    matching a recorded glob (which changes the result list) busts the cache even
    though the previously-read files are untouched. Pure stats, never file content.
    ``ws_root is None`` (a workspace-less app) yields a stable constant, so such a
    pipeline caches on params alone (nothing to be live against).

    *glob_results* is ALREADY resolved (``{pattern: [rel_paths]}``) — the post-run
    call feeds :attr:`PipelineContext.glob_results` recorded AT glob time (no
    re-walk), the pre-run call feeds :func:`_resolve_globs` over the stored
    patterns; both produce the same structure so an unchanged source fingerprints
    identically across the two seams.
    """
    globs = {pattern: sorted(results) for pattern, results in glob_results.items()}
    stat_targets: set[str] = set(read_paths)
    for results in globs.values():
        stat_targets.update(results)
    stats: list[list[Any]] = []
    for rel in sorted(stat_targets):
        st = _stat_under_root(ws_root, rel) if ws_root is not None else None
        stats.append([rel, st[0] if st else None, st[1] if st else None])
    material = json.dumps({"globs": globs, "stats": stats}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# ``ctx.exec`` — controlled, allowlisted subprocess egress
# ---------------------------------------------------------------------------


class PipelineExecutor:
    """Runs ONE allowlisted binary for a code pipeline — argv-only, bounded, redacted.

    The I/O EDGE of ``ctx.exec``. The AUTHORIZATION rule is deliberately NOT
    here: it lives on :meth:`~mewbo_api.apps.models.PipelineSpec.check_exec_allowed`,
    the model that declares ``allow_exec``/``allow_egress``. This class owns
    only what touches the process — argv hardening, env, spawn, timeout kill,
    output cap, redaction — with its collaborators (the pipeline, the workspace
    root, the redactor) injected as FIELDS so a test drives it without one.
    """

    def __init__(
        self,
        *,
        pipeline: PipelineSpec,
        workspace_root: Path,
        redactor: SecretRedactor,
        allowed_binaries: frozenset[str] = PIPELINE_ALLOWED_EXEC,
        default_timeout_seconds: float = _DEFAULT_EXEC_TIMEOUT_SECONDS,
        max_output_bytes: int = _MAX_EXEC_OUTPUT_BYTES,
    ) -> None:
        """Bind the declaring pipeline + workspace cwd + the collaborators and caps."""
        self._pipeline = pipeline
        self._workspace_root = workspace_root
        self._redactor = redactor
        self._allowed_binaries = allowed_binaries
        self._default_timeout_seconds = default_timeout_seconds
        self._max_output_bytes = max_output_bytes

    def run(self, argv: Sequence[str], *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Authorize, spawn, and bound one call; return ``{returncode, stdout, stderr}``."""
        try:
            self._pipeline.check_exec_allowed(
                argv, allowed_binaries=self._allowed_binaries
            )
        except ValueError as exc:
            raise PipelineExecutionError("exec", str(exc)) from None
        command = self._hardened_argv(list(argv))
        timeout = self._effective_timeout(timeout_seconds)
        # The active root is this call's OWN workspace — never the ambient
        # `active_project_root` contextvar, which names whatever project the
        # AGENT session (not this pipeline invocation) is working in. An app
        # bound to a `kind="shared"` workspace must keep that project reachable,
        # which is exactly what passing the workspace root as the active root
        # guarantees (`ShellScope.for_active_root` denies every OTHER configured
        # project and re-admits this one). ``scope`` is ``None`` — and the spawn
        # unscoped — whenever `agent.shell_sandbox` is off or nothing survived
        # compilation.
        scope = ShellScope.for_active_root(str(self._workspace_root))
        try:
            with scoped_preexec(scope) as hook:
                proc = subprocess.Popen(  # noqa: S603 - argv-only, allowlist-gated above
                    command,
                    cwd=str(self._workspace_root),
                    env=self._env(command[0]),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",  # binary output must not raise mid-communicate
                    start_new_session=True,
                    preexec_fn=hook,
                )
        except FileNotFoundError as exc:
            # Raised by the spawn itself, NOT by communicate() — the binary name is
            # one of the vetted set, so it carries no secret and needs no redaction.
            raise PipelineExecutionError(
                "exec", f"{command[0]!r} is not installed: {exc}"
            ) from None
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            raise PipelineExecutionError(
                "timeout",
                # Redacted like stdout/stderr: argv can carry a credential-bearing
                # remote URL, and this message is persisted verbatim onto the
                # `PipelineRun.error` ledger row that `/system` renders.
                self._redactor.redact_text(f"exec({list(argv)!r}) exceeded {timeout}s"),
            ) from None
        except BaseException:
            # ANY other exit from communicate() leaves a live child holding open
            # pipes — reap it before unwinding. Timeout is not the only way out:
            # a decode failure on binary output (`git cat-file blob`) raises here
            # too, and a bare `raise` would orphan the process group.
            self._kill_group(proc)
            raise
        return {
            "returncode": proc.returncode,
            "stdout": self._bounded(stdout),
            "stderr": self._bounded(stderr),
        }

    def _hardened_argv(self, argv: list[str]) -> list[str]:
        """Insert git's credential-helper disable, matching every other git call site.

        The container gitconfig sets ``credential.helper=store`` against a
        READ-ONLY bind-mounted ``~/.git-credentials``; on an auth rejection git
        tries to ERASE the entry and the rename fails with EBUSY, masking the
        real auth error. ``build_clone_command``/``build_ls_remote_command`` in
        the wiki clone tool carry the same ``-c credential.helper=`` for exactly
        this reason — a pipeline's own git call must not be the one path that
        re-opens the trap.
        """
        if argv[0] != "git":
            return argv
        return [argv[0], "-c", "credential.helper=", *argv[1:]]

    def _effective_timeout(self, timeout_seconds: float | None) -> float:
        """Clamp a per-call override to the pipeline's own declared ceiling.

        An override can only TIGHTEN: ``timeout_seconds`` is ``ge=1`` on the
        model, so the ``min`` holds for zero, a negative, or a huge value. NaN
        is the one input it does NOT hold for — ``min(nan, 600)`` returns
        ``nan`` (every comparison against NaN is False, so ``min`` keeps its
        first argument), which then reaches ``select()`` as a hard ValueError
        rather than a bounded wait. Rejected explicitly instead.
        """
        requested = (
            self._default_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if not isinstance(requested, (int, float)) or not math.isfinite(requested):
            raise PipelineExecutionError(
                "exec", f"exec(timeout_seconds=) must be a finite number, got {requested!r}"
            )
        return min(float(requested), float(self._pipeline.timeout_seconds))

    def _env(self, binary: str) -> dict[str, str]:
        """The subprocess env — the server process's own, plus a prompt/pager overlay.

        Full inheritance is DELIBERATE, not an oversight: ``ctx.exec`` rides
        AMBIENT credential state (an SSH agent needs ``SSH_AUTH_SOCK``,
        ``tea``/``gh`` need ``HOME``), so a scrubbed env would break the very
        authentication this surface documents. The accepted consequence is that
        an allowlisted binary sees the API process's environment — declare
        accordingly. ``git`` routes through the ONE shared env builder
        (``hardened_git_env``) rather than a second copy of it here.

        INHERITING ``HOME`` IS NOT REACHING IT. Under ``agent.shell_sandbox``
        the Landlock scope grants sibling DIRECTORIES at each ancestor level,
        so ``~/.config`` resolves and ``tea``/``gh`` keep their logins, while
        loose files at ``$HOME`` root (``.gitconfig``, ``.git-credentials``)
        do not and ``$HOME`` itself is not listable. That costs git nothing:
        its global config is blanked below and the argv already disables the
        credential helper. UNVERIFIED — whether an SSH agent still resolves.
        Its socket normally sits outside the denied set, but that is reasoning
        about the scope, not a measurement of it.

        AMBIENT ENV IS NOT AMBIENT GIT CONFIG, and only git is treated this
        way: ``hardened_git_env`` blanks the global and system git config, so a
        pipeline's git sees only the repository's own ``.git/config``. A
        pipeline depending on an operator's global ``insteadOf`` rewrite or
        ``http.*`` override therefore behaves differently here than the same
        command typed on the host — intended, since those settings redirect a
        call the argv gates already vetted. It costs no commit identity: the
        subcommand allowlist is read-shaped, so ``git commit`` is refused
        before a process is spawned, as is the ``git -c user.email=`` form
        that would supply one. Setting ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
        here would guard a path authorization makes unreachable.
        """
        env = hardened_git_env() if binary == "git" else dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        return env

    @staticmethod
    def _kill_group(proc: subprocess.Popen[str]) -> None:
        """SIGKILL the whole process group, then reap — a bare kill orphans children.

        ``start_new_session=True`` put the child in its own group precisely so a
        pager/helper it spawned dies with it; ``aider_shell_tool`` hardens its
        own timeout the same way.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)

    def _bounded(self, text: str) -> str:
        """Redact, then truncate to a real BYTE budget (a str slice counts codepoints)."""
        redacted = self._redactor.redact_text(text or "")
        raw = redacted.encode("utf-8")
        if len(raw) <= self._max_output_bytes:
            return redacted
        return raw[: self._max_output_bytes].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# The workspace-scoped execution context handed to run(params, ctx)
# ---------------------------------------------------------------------------


class _PipelineCollection:
    """One collection handle on ``ctx`` — schema + cap enforced via the real store.

    ``upsert`` rides ``AppDataStore.upsert`` with the owning ``CollectionSpec`` +
    the app's ``max_docs_per_collection`` cap, so validation + cap are the SAME
    enforcement seam ``app_data`` uses (never re-implemented). Under ``dry_run`` it
    still schema-validates (so a preview catches a bad doc) and COUNTS what would
    write, but performs no durable write.

    Every mutating method opens on ``ensure_side_effects_allowed`` — the injected cooperative
    stop check (:meth:`PipelineContext.ensure_side_effects_allowed`). It is what makes the
    watchdog's timeout TRUE: the worker thread outlives the join, so without this
    its writes keep landing durably after the caller has been told the run failed.
    The check precedes the store call, never follows it — ``_record`` runs AFTER
    the write, so guarding the accounting point alone would refuse the count and
    keep the write.
    """

    def __init__(
        self,
        *,
        app_id: str,
        name: str,
        spec: CollectionSpec,
        data_store: AppDataStoreBase,
        max_docs: int,
        dry_run: bool,
        record: Callable[[str, int], None],
        ensure_side_effects_allowed: Callable[[], None],
    ) -> None:
        """Bind the app/collection scope + the store, cap, dry-run flag, and counter."""
        self._app_id = app_id
        self._name = name
        self._spec = spec
        self._data_store = data_store
        self._max_docs = max_docs
        self._dry_run = dry_run
        self._record = record
        self._ensure_side_effects_allowed = ensure_side_effects_allowed

    def upsert(self, key: str, doc: dict[str, Any]) -> None:
        """Write one document (schema-validated, cap-enforced) — counted on the run."""
        self._ensure_side_effects_allowed()
        if not isinstance(key, str) or not key.strip():
            raise PipelineExecutionError("validation", "upsert requires a non-empty string key")
        if not isinstance(doc, dict):
            raise PipelineExecutionError("validation", "upsert doc must be a JSON object (dict)")
        try:
            self._spec.validate_doc(doc)  # same check the store runs, shared not duplicated
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise PipelineExecutionError("schema", f"doc field '{path}': {exc.message}") from None
        if self._dry_run:
            self._record(self._name, 1)
            return
        try:
            self._data_store.upsert(
                self._app_id, self._name, key, doc,
                collection_spec=self._spec, max_docs=self._max_docs,
            )
        except CollectionCapExceeded as exc:
            raise PipelineExecutionError("cap", str(exc)) from None
        self._record(self._name, 1)

    def query(
        self,
        filter: dict[str, Any] | None = None,  # noqa: A002 - mirrors the store signature
        limit: int = 100,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read documents back — ``{key, doc, updated_at}`` rows (updated_at as ISO)."""
        limit = int(limit)
        if limit > _MAX_QUERY_LIMIT:
            raise PipelineExecutionError(
                "validation",
                f"limit {limit} exceeds the maximum of {_MAX_QUERY_LIMIT}",
            )
        bounded = max(0, limit)
        docs = self._data_store.query(
            self._app_id, self._name, filter=filter, limit=bounded, sort=sort
        )
        return [
            {"key": d.key, "doc": d.doc, "updated_at": d.updated_at.isoformat()} for d in docs
        ]

    def delete(self, key: str) -> bool:
        """Delete one document; report whether it existed. A no-op under ``dry_run``."""
        self._ensure_side_effects_allowed()
        if not isinstance(key, str) or not key.strip():
            raise PipelineExecutionError("validation", "delete requires a non-empty string key")
        if self._dry_run:
            return self._data_store.get(self._app_id, self._name, key) is not None
        return self._data_store.delete(self._app_id, self._name, key)


class PipelineContext:
    """The ``ctx`` handed to ``run(params, ctx)`` — params + clock + scoped I/O + llm.

    ``params``/``now`` are plain data; ``glob``/``read_file`` are workspace-scoped
    and path-traversal-guarded (resolved UNDER the workspace root only — ``..`` and
    absolute escapes are refused); ``collection(name)`` returns a
    :class:`_PipelineCollection` riding the real data plane. :meth:`llm` is the
    bounded LLM step (Wave 5): a single schema-shaped model round-trip, gated
    by the pipeline's declared ``llm_budget_tokens`` and served from a
    content-addressed cache — the runner owns that policy, ``ctx`` only exposes the
    surface (``_llm_step`` is the runner-bound callback).

    For a ``cache_mode="source"`` pipeline the context RECORDS every path it
    read/globbed (``read_paths``/``glob_patterns``) so the runner can fingerprint the
    workspace state and serve the cache only while it is unchanged.
    """

    def __init__(
        self,
        *,
        app: AppSpec,
        pipeline: PipelineSpec,
        params: dict[str, Any],
        now: datetime,
        workspace_root: str | None,
        data_store: AppDataStoreBase,
        dry_run: bool,
        llm_step: Callable[[PipelineContext, str, dict[str, Any], int], dict[str, Any]]
        | None = None,
        rehearse: bool = False,
        allowed_exec_binaries: frozenset[str] = PIPELINE_ALLOWED_EXEC,
        llm_cache_salt: str | None = None,
        exec_timeout_seconds: float = _DEFAULT_EXEC_TIMEOUT_SECONDS,
    ) -> None:
        """Capture params/clock + the workspace root, data store, execution flags, and I/O policy.

        A rehearsal retains every dry-run mutation guard while permitting its declared
        subprocess leg. Submit verification is the only caller that opts into it: a
        preview must never make that choice on a caller's behalf.
        """
        self.params = dict(params)
        self.now = now
        self._app = app
        self._pipeline = pipeline
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self._data_store = data_store
        self._dry_run = dry_run
        self._rehearse = rehearse
        self._allowed_exec_binaries = allowed_exec_binaries
        self._exec_timeout_seconds = exec_timeout_seconds
        self._llm_step = llm_step
        # The source identity salt the runner threads into the ``ctx.llm`` cache key
        # in ``cache_mode="source"`` (``None`` in ttl/no-cache modes) so a source
        # change busts a stale llm answer even when the prompt string is unchanged.
        self._llm_cache_salt = llm_cache_salt
        self.docs_written: dict[str, int] = {}
        # Per-run cumulative ``ctx.llm()`` max_tokens (budget accounting) — the
        # context OWNS this state (atomic-class rule; readable by future ctx
        # consumers), the runner's llm step reads/increments it.
        self.llm_tokens_spent: int = 0
        self._collections = {c.name: c for c in app.collections}
        # Source-mode liveness manifest (recorded during execution, read by the
        # runner). ``glob_results`` records {pattern: resolved matches} AT glob time
        # so the post-run fingerprint consumes them instead of re-walking (the
        # pre-run check against a prior manifest still re-globs — inherent).
        self.read_paths: set[str] = set()
        self.glob_results: dict[str, list[str]] = {}
        # Cooperative stop — the ONLY lever the watchdog has over a worker thread
        # that outlives it. ``threading.Event`` because the two sides are genuinely
        # different threads: the runner sets it from the caller's thread once it
        # stops waiting, the pipeline's own thread reads it on every side effect.
        self._stopped = threading.Event()
        self._stop_reason = ""

    def stop(self, reason: str) -> None:
        """Refuse every LATER side effect on this run — the watchdog's only lever.

        Python cannot kill the worker thread, so a timed-out pipeline keeps
        running; what this stops is the part that matters, the durable half.
        After this, :meth:`ensure_side_effects_allowed` raises inside that thread, so a
        collection write, a ``ctx.exec`` spawn or a ``ctx.llm`` round-trip
        attempted past the deadline is refused instead of landing with no caller
        and no ledger row. A side effect already IN FLIGHT past its guard still
        completes — the bound is on what STARTS after the stop, not on what is
        mid-call.
        """
        self._stop_reason = reason
        self._stopped.set()

    def ensure_side_effects_allowed(self) -> None:
        """Raise unless this run is still permitted to cause side effects."""
        if self._stopped.is_set():
            raise PipelineExecutionError(
                "stopped",
                self._stop_reason or "this pipeline run was stopped; no further side effects",
            )

    def glob(self, pattern: str) -> list[str]:
        """Workspace-relative file paths matching *pattern* (empty if no workspace)."""
        if self._workspace_root is None:
            self.glob_results[pattern] = []  # recorded for source-mode liveness
            return []
        results = _glob_under_root(self._workspace_root, pattern)
        self.glob_results[pattern] = results  # recorded so the fingerprint needs no re-walk
        return results

    def read_file(self, rel_path: str) -> str:
        """Read a UTF-8 text file UNDER the workspace root (traversal-guarded)."""
        target = self._resolve_under_root(rel_path)
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PipelineExecutionError("read", f"could not read {rel_path!r}: {exc}") from None
        self.read_paths.add(rel_path)  # recorded for source-mode read-through liveness
        return text

    def exec(self, argv: Sequence[str], *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Run one allowlisted binary, argv-only, never a shell.

        Refuses BEFORE spawning anything unless the pipeline declared
        ``argv[0]`` in `allow_exec` AND every remote-shaped token in *argv*
        resolves to a host in `allow_egress`
        (:meth:`PipelineSpec.check_exec_allowed` owns that rule). ``cwd`` is
        ALWAYS pinned to the workspace root — never a silent fallback to the
        server process's own cwd — so a workspace-less app raises the SAME
        clean ``"workspace"`` error :meth:`read_file` does rather than running
        a CLI against wherever the API happened to start. Returns
        ``{"returncode", "stdout", "stderr"}`` — a non-zero exit is NOT an
        error here (the pipeline decides what a non-zero `git`/`tea` exit
        means); only a refusal, a missing binary, or a timeout raises.

        *timeout_seconds* can only TIGHTEN the pipeline's own declared
        ``timeout_seconds`` ceiling, never exceed it.

        REFUSED under an ordinary ``dry_run`` — unlike :meth:`llm`, which is
        read-only w.r.t. the world and so may run. A subprocess is not: ``git push`` /
        `tea pr create` reach a real remote. Submit verification needs to exercise
        the live-tool leg that this refusal would otherwise skip, so its explicit
        ``rehearse`` state retains every dry-run mutation guard while allowing the
        declared subprocess. A preview never opts into that state.
        """
        self.ensure_side_effects_allowed()
        if self._workspace_root is None:
            raise PipelineExecutionError("workspace", "no workspace is bound to this app")
        if self._dry_run and not self._rehearse:
            raise PipelineExecutionError(
                "dry_run",
                "ctx.exec does not run under a dry run — a subprocess can reach a real "
                "remote, so a preview never spawns one",
            )
        return PipelineExecutor(
            pipeline=self._pipeline,
            workspace_root=self._workspace_root,
            redactor=get_secret_redactor(),
            allowed_binaries=self._allowed_exec_binaries,
            default_timeout_seconds=self._exec_timeout_seconds,
        ).run(argv, timeout_seconds=timeout_seconds)

    def llm(
        self, prompt: str, output_schema: dict[str, Any], *, max_tokens: int = 1024
    ) -> dict[str, Any]:
        """One bounded, schema-shaped LLM step — returns a dict validated against *output_schema*.

        The "LLM tool-call answer as a pipeline structure": a single model
        round-trip whose result is validated against *output_schema* (one retry with
        the validation error appended, then it raises ``PipelineExecutionError`` with
        code ``"llm"``). *output_schema* MUST have root ``type: "object"`` — the
        synthesis seam returns a JSON object, so a non-object root is rejected up
        front (a clean ``"llm"`` error) BEFORE any model call rather than wasting a
        round-trip that would only fail re-validation.

        Gated by the pipeline's declared ``llm_budget_tokens`` (a ``0`` budget
        forbids this call) and served from a content-addressed cache keyed by
        ``sha256(prompt+schema[+source-salt])``, so a repeated fire with unchanged
        inputs skips the model entirely (in ``cache_mode="source"`` the salt busts
        the entry when the source changes). ALLOWED under ``dry_run`` (read-only
        w.r.t. collections, and the builder needs to test it): budget is still
        charged and a cache HIT still served, but a dry run never WRITES the cache
        (it must not mint an entry a later real run would serve un-provenanced). All
        policy lives in the runner's ``_llm_step`` callback; this surface delegates,
        passing ``self`` so the runner reads/increments this run's budget + salt.
        """
        self.ensure_side_effects_allowed()
        if self._llm_step is None:
            raise PipelineExecutionError(
                "llm", "llm step not configured (no model backend wired for this deployment)"
            )
        return self._llm_step(self, prompt, output_schema, max_tokens)

    def collection(self, name: str) -> _PipelineCollection:
        """A handle on one of the app's declared collections (``upsert``/``query``/``delete``)."""
        spec = self._collections.get(name)
        if spec is None:
            raise PipelineExecutionError(
                "collection",
                f"unknown collection {name!r}; declared: {sorted(self._collections)}",
            )
        return _PipelineCollection(
            app_id=self._app.app_id,
            name=name,
            spec=spec,
            data_store=self._data_store,
            max_docs=self._app.policies.max_docs_per_collection,
            dry_run=self._dry_run,
            record=self._record_write,
            ensure_side_effects_allowed=self.ensure_side_effects_allowed,
        )

    def _resolve_under_root(self, rel_path: str) -> Path:
        if self._workspace_root is None:
            raise PipelineExecutionError("workspace", "no workspace is bound to this app")
        if not isinstance(rel_path, str) or not rel_path:
            raise PipelineExecutionError("traversal", "read_file requires a relative path")
        if rel_path.startswith(("/", "\\")) or ":" in rel_path:
            raise PipelineExecutionError("traversal", f"{rel_path!r} must be a relative path")
        candidate = (self._workspace_root / rel_path).resolve()
        try:
            candidate.relative_to(self._workspace_root)
        except ValueError:
            raise PipelineExecutionError(
                "traversal", f"{rel_path!r} escapes the workspace"
            ) from None
        if not candidate.is_file():
            raise PipelineExecutionError("read", f"no file at {rel_path!r}")
        return candidate

    def _record_write(self, collection: str, count: int) -> None:
        self.docs_written[collection] = self.docs_written.get(collection, 0) + count


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SourceCacheEntry:
    """A ``cache_mode="source"`` cached result + the liveness manifest it stat-matches on.

    Hot in-process runtime state (never crosses a trust boundary), so a plain
    frozen dataclass — the Pydantic rule stops at the process boundary. ``output``
    is served verbatim on a hit; ``read_paths``/``glob_patterns`` are re-stat/re-glob
    inputs and ``fingerprint`` is the value a subsequent run recomputes and compares.
    """

    output: Any
    evaluated_at: datetime
    read_paths: frozenset[str]
    glob_patterns: frozenset[str]
    fingerprint: str


class AppPipelineRunner:
    """Executes ``mode="code"`` pipelines over its injected collaborators.

    Atomic feature class: ``app_store`` / ``app_data`` / ``workspace_resolver`` /
    ``clock`` / ``cache`` are its state; :meth:`execute` (object-keyed, the REST +
    fire seam) and :meth:`run_pipeline` (id-keyed, the ``run_pipeline`` tool's
    :class:`~mewbo_api.apps.plugin.runtime.PipelineRunner` Protocol) are its
    behavior. A failure is RAISED (:class:`PipelineExecutionError`); a
    :class:`~mewbo_api.apps.models.PipelineResult` only ever represents success.
    """

    def __init__(
        self,
        *,
        app_store: AppStoreBase,
        app_data: AppDataStoreBase,
        workspace_resolver: Callable[[AppSpec], str | None],
        clock: Callable[[], datetime] | None = None,
        cache: MutableMapping[tuple[str, int, str, str], tuple[Any, datetime]] | None = None,
        llm_invoke: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        allowed_exec_binaries: frozenset[str] = PIPELINE_ALLOWED_EXEC,
    ) -> None:
        """Capture the injected collaborators.

        ``app_store`` resolves the id-keyed ``run_pipeline`` seam (the ``execute``
        seam is handed the app object directly). ``workspace_resolver`` maps an app
        to its workspace cwd (models never import I/O, so the app edge injects it).
        ``clock`` is the fallback when ``execute`` is called without an explicit
        ``now`` (the ``run_pipeline`` seam threads none). ``cache`` is the
        process-local TTL/llm cache (a plain dict by default — documented v1 scope).
        ``llm_invoke`` is the bounded LLM step's model round-trip (a thin adapter
        over the structured-synthesis seam, injected by ``backend.py``): it takes
        ``(prompt, output_schema, max_tokens)`` and returns the model's structured
        dict; ``None`` (unwired) makes ``ctx.llm`` raise a clean "not configured".

        ``timeout_seconds`` is an OPTIONAL global override for the watchdog: ``None``
        (production) ⇒ each run uses its pipeline's DECLARED
        ``PipelineSpec.timeout_seconds`` (declare-don't-infer); a set value is a
        hard ceiling applied to every pipeline regardless (a deployment cap, and how
        a test drives a sub-second watchdog without the model's 1s field floor).
        """
        self._app_store = app_store
        self._app_data = app_data
        self._workspace_resolver = workspace_resolver
        self._clock = clock or self._utcnow
        self._cache: MutableMapping[tuple[str, int, str, str], tuple[Any, datetime]] = (
            cache if cache is not None else {}
        )
        # Read-through liveness store (``cache_mode="source"``): richer value than the
        # TTL/llm cache (a manifest + fingerprint), so its own mapping under the SAME
        # lock. Process-local, plain dict — hot runtime state, documented v1 scope.
        self._source_cache: MutableMapping[tuple[str, int, str, str], _SourceCacheEntry] = {}
        self._llm_invoke = llm_invoke
        self._cache_lock = threading.Lock()
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._allowed_exec_binaries = allowed_exec_binaries

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _canonical_digest(payload: Any) -> str:
        """sha256 of *payload* under ONE canonical JSON encoding — the cache-key primitive.

        The single canonicalize-then-hash the two content addresses share
        (:meth:`params_hash` and :meth:`_llm_digest`), so a sort-keys/separator/
        ``default=str`` tweak can never drift one from the other.
        """
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def params_hash(params: dict[str, Any] | None) -> str:
        """sha256 of the canonical invocation params — the cache-key discriminator.

        Static so the tracker/endpoint can stamp ``PipelineRun.params_hash`` with
        the exact value the cache keys on, without re-deriving it.
        """
        return AppPipelineRunner._canonical_digest(params or {})

    # -- the id-keyed adapter (the run_pipeline tool's Protocol) ------------

    def run_pipeline(
        self, app_id: str, pipeline_name: str, *, params: dict[str, Any], dry_run: bool
    ) -> dict[str, Any]:
        """Resolve ``(app_id, pipeline_name)`` and execute — the plugin Protocol seam.

        Returns ``{output, evaluated_at, docs_written, evidence, cache_hit}``
        (the shape the ``run_pipeline`` SessionTool renders); raises
        :class:`PipelineExecutionError` on any failure (the tool turns its message
        plus bounded partial evidence into agent-visible feedback).
        """
        app = self._app_store.get(app_id)
        if app is None:
            raise PipelineExecutionError("not_found", f"no app {app_id!r}")
        pipeline = next((p for p in app.pipelines if p.name == pipeline_name), None)
        if pipeline is None:
            raise PipelineExecutionError("not_found", f"unknown pipeline {pipeline_name!r}")
        result = self.execute(app, pipeline, params, now=self._clock(), dry_run=dry_run)
        return {
            "output": result.output,
            "evaluated_at": result.evaluated_at,
            "docs_written": dict(result.docs_written),
            "evidence": result.evidence.model_dump(mode="json"),
            "cache_hit": result.cache == "hit",
        }

    # -- the object-keyed engine (the REST + fire seam) --------------------

    def execute(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        params: dict[str, Any],
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        rehearse: bool = False,
    ) -> PipelineResult:
        """Validate, (cache-check), execute, enforce the output, and return the result.

        ``now`` is the authoritative clock read (tests inject it); it falls back to
        the injected ``clock`` when a caller threads none (the ``run_pipeline`` seam).
        ``dry_run`` exercises the IDENTICAL path (params validation, ``ctx``
        construction, ``run`` execution) but performs NO durable write and BYPASSES
        both caches — a "test it before you ship" preview whose ``docs_written``
        counts what a real run WOULD write. ``rehearse`` has the same mutation and
        cache guards while allowing declared ``ctx.exec`` calls, so submit
        verification exercises the live-tool leg that a preview must not invoke.
        The two states are mutually exclusive.

        Two cache tiers, per ``pipeline.cache_mode`` (both bypassed by either
        suppressed-write state):
        ``"ttl"`` serves a result for ``cache_ttl_seconds``; ``"source"`` is
        read-through liveness — it records the files/globs the run read and serves
        the cache only while their stat fingerprint is unchanged, IGNORING
        ``cache_ttl_seconds`` (a NEW file matching a recorded glob, a touched mtime,
        or a size change busts it; the first run has no manifest and always executes).
        """
        now = now or self._clock()
        if dry_run and rehearse:
            raise PipelineExecutionError(
                "execution_state", "dry_run and rehearse cannot both be enabled"
            )
        if pipeline.mode != "code":
            raise PipelineExecutionError(
                "mode", f"pipeline {pipeline.name!r} is not a code pipeline"
            )

        params = self._validate_params(pipeline, params)
        phash = self.params_hash(params)
        source_mode = pipeline.cache_mode == "source"
        write_suppressed = dry_run or rehearse

        # TTL tier — time-driven. "source" mode ignores cache_ttl_seconds entirely.
        use_ttl_cache = pipeline.cache_ttl_seconds > 0 and not write_suppressed and not source_mode
        if use_ttl_cache:
            cached = self._cache_get(app, pipeline, phash, now)
            if cached is not None:
                output, evaluated_at = cached
                return PipelineResult(
                    output=output, evaluated_at=evaluated_at, cache="hit", docs_written={}
                )

        workspace_root = self._workspace_resolver(app)
        ws_path = Path(workspace_root).resolve() if workspace_root else None

        # Source tier — read-through liveness. Compute the PRIOR manifest's CURRENT
        # fingerprint ONCE: it decides the cache hit AND salts the ctx.llm cache key
        # for this run (so a source change busts a stale llm answer that a fresh
        # prompt string alone could not). ``None`` prior ⇒ a stable empty-manifest
        # salt (the first source-mode run has nothing to be stale against).
        source_salt: str | None = None
        if source_mode and not write_suppressed:
            entry = self._source_cache_get(app, pipeline, phash)
            prior_globs = _resolve_globs(ws_path, entry.glob_patterns) if entry else {}
            prior_reads = entry.read_paths if entry else frozenset()
            current_fp = _source_fingerprint(ws_path, prior_reads, prior_globs)
            if entry is not None and current_fp == entry.fingerprint:
                return PipelineResult(
                    output=entry.output, evaluated_at=entry.evaluated_at, cache="hit",
                    docs_written={},
                )
            source_salt = current_fp

        source = self._load_entrypoint(app, pipeline.entrypoint)

        # The watchdog bound: the pipeline's DECLARED ceiling, unless a global
        # override is set on the runner (a deployment cap / a test's sub-second wall).
        #
        # The declared value is CLAMPED to PIPELINE_TIMEOUT_CEILING_SECONDS first —
        # ``PipelineSpec.timeout_seconds`` parses up to 600 (deliberately, so an
        # already-stored manifest keeps parsing; see that field's comment), so a
        # pipeline stored before the ceiling existed, or before it was lowered, can
        # still declare more than the platform now allows. Honouring it uncapped
        # would let a synchronous REST invoke outlive the single gunicorn worker.
        # Logged once per run at WARNING — a silently narrowed bound is the
        # fail-open shape this repo forbids, so the clamp announces itself naming
        # the app and pipeline.
        declared_timeout = min(pipeline.timeout_seconds, PIPELINE_TIMEOUT_CEILING_SECONDS)
        if declared_timeout != pipeline.timeout_seconds:
            logging.warning(
                "app {} pipeline {}: declared timeout_seconds={} exceeds the {}s ceiling; "
                "clamping this run to {}s",
                app.app_id, pipeline.name, pipeline.timeout_seconds,
                PIPELINE_TIMEOUT_CEILING_SECONDS, declared_timeout,
            )
        timeout = declared_timeout if self._timeout_seconds is None else self._timeout_seconds
        ctx = PipelineContext(
            app=app,
            pipeline=pipeline,
            params=params,
            now=now,
            workspace_root=workspace_root,
            data_store=self._app_data,
            dry_run=write_suppressed,
            rehearse=rehearse,
            llm_step=partial(self._run_llm, app, pipeline, now),
            allowed_exec_binaries=self._allowed_exec_binaries,
            llm_cache_salt=source_salt,
        )
        try:
            output = self._run_entrypoint(source, pipeline.entrypoint or "<pipeline>", ctx, timeout)
            try:
                pipeline.validate_result(output)
            except ValueError as exc:
                # A PipelineResult means success only; rendering an invalid typed result
                # would turn a shape break into silently wrong caller-visible data.
                raise PipelineExecutionError("result", str(exc)) from None
            output = self._enforce_output(output)
        except PipelineExecutionError as exc:
            # A failure is not a promise that nothing landed: a pipeline that
            # wrote three docs and then raised (or timed out) really did write
            # them. Carry the partial count out on the error so the ``failed``
            # ledger row records what happened instead of an empty ``docs_written``
            # — the ONE place this is attached, since it is the only one holding
            # both the ctx and every failure the run can raise.
            if not exc.docs_written:
                exc.docs_written = dict(ctx.docs_written)
            if not exc.evidence.globs and not exc.evidence.read_paths:
                exc.evidence = PipelineEvidence.from_observations(
                    glob_results=ctx.glob_results,
                    read_paths=ctx.read_paths,
                    workspace=workspace_root,
                )
            raise

        if use_ttl_cache:
            self._cache_put(app, pipeline, phash, output, now)
        if source_mode and not write_suppressed:
            # Post-run fingerprint consumes the globs RECORDED at glob time (no
            # re-walk); the manifest stores the patterns so the next run's pre-check
            # can re-glob them to catch a NEW matching file.
            fingerprint = _source_fingerprint(ws_path, ctx.read_paths, ctx.glob_results)
            self._source_cache_put(
                app, pipeline, phash, output, now,
                ctx.read_paths, frozenset(ctx.glob_results), fingerprint,
            )
        return PipelineResult(
            output=output,
            evaluated_at=now,
            cache="miss",
            docs_written=dict(ctx.docs_written),
            evidence=PipelineEvidence.from_observations(
                glob_results=ctx.glob_results,
                read_paths=ctx.read_paths,
                workspace=workspace_root,
            ),
        )

    def verify(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        result: PipelineResult,
        *,
        now: datetime,
    ) -> None:
        """Run the optional verifier against one successful result under its watchdog.

        A verifier observes a completed result through the same curated namespace as
        ``run``. Its context always suppresses durable mutation; it rehearses the
        verifier's declared subprocess leg so its execution checks are exercised.
        """
        verifier = pipeline.verifier
        if verifier is None:
            return
        try:
            source = self._load_entrypoint(app, verifier.entrypoint)
            workspace_root = self._workspace_resolver(app)
            declared_timeout = min(verifier.timeout_seconds, PIPELINE_TIMEOUT_CEILING_SECONDS)
            if declared_timeout != verifier.timeout_seconds:
                logging.warning(
                    "app {} pipeline {}: verifier timeout_seconds={} exceeds the {}s ceiling; "
                    "clamping this verification to {}s",
                    app.app_id,
                    pipeline.name,
                    verifier.timeout_seconds,
                    PIPELINE_TIMEOUT_CEILING_SECONDS,
                    declared_timeout,
                )
            timeout = min(
                declared_timeout,
                self._timeout_seconds if self._timeout_seconds is not None else declared_timeout,
            )
            ctx = PipelineContext(
                app=app,
                pipeline=pipeline,
                params={},
                now=now,
                workspace_root=workspace_root,
                data_store=self._app_data,
                dry_run=True,
                rehearse=True,
                llm_step=partial(self._run_llm, app, pipeline, now),
                allowed_exec_binaries=self._allowed_exec_binaries,
            )
            self._run_entrypoint(
                source,
                verifier.entrypoint,
                ctx,
                timeout,
                function_name="verify",
                args=(result.output, ctx),
            )
        except PipelineExecutionError as exc:
            raise PipelineExecutionError("verifier", exc.message) from None

    def _load_entrypoint(self, app: AppSpec, entrypoint: str | None) -> str:
        """Resolve and lint one bundle entrypoint before curated execution."""
        source = app.frontend.files.get(entrypoint) if entrypoint else None
        if source is None:
            raise PipelineExecutionError(
                "entrypoint", f"entrypoint {entrypoint!r} is not among the app bundle files"
            )
        findings = lint_pipeline(source)
        if findings:
            raise PipelineExecutionError(
                "lint", "pipeline code was rejected:\n" + format_findings(findings)
            )
        return source

    # -- params -------------------------------------------------------------

    def _validate_params(self, pipeline: PipelineSpec, params: dict[str, Any]) -> dict[str, Any]:
        """Validate the invocation params against ``pipeline.params_schema``.

        ``params_schema is None`` ⇒ the pipeline accepts NO params (a non-empty
        ``params`` is a clean ``params`` error). Otherwise the params are
        jsonschema-validated against it.
        """
        if not isinstance(params, dict):
            raise PipelineExecutionError("params", "params must be a JSON object")
        if pipeline.params_schema is None:
            if params:
                raise PipelineExecutionError(
                    "params",
                    f"pipeline {pipeline.name!r} accepts no params (declares no params_schema)",
                )
            return {}
        try:
            jsonschema.validate(instance=params, schema=pipeline.params_schema)
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise PipelineExecutionError(
                "params", f"params field '{path}': {exc.message}"
            ) from None
        except jsonschema.exceptions.SchemaError as exc:
            raise PipelineExecutionError(
                "params", f"params_schema is invalid: {exc.message}"
            ) from None
        return dict(params)

    # -- execution + output -------------------------------------------------

    def _run_entrypoint(
        self,
        source: str,
        filename: str,
        ctx: PipelineContext,
        timeout_seconds: float,
        *,
        function_name: str = "run",
        args: tuple[Any, ...] | None = None,
    ) -> Any:
        """Exec one curated entrypoint under the wall-clock watchdog.

        The whole ``exec`` + entrypoint call runs on a daemon worker thread joined
        with *timeout_seconds* (the pipeline's declared ceiling or the runner's
        override), so module-level code that loops is bounded too — not only the
        callable body. The verifier reuses this exact path to prevent its loader,
        lint, namespace, or timeout policy from drifting from ``run``.
        """
        box: dict[str, Any] = {}
        call_args = args if args is not None else (dict(ctx.params), ctx)

        def _target() -> None:
            try:
                namespace: dict[str, Any] = {
                    "__builtins__": _SAFE_BUILTINS,
                    "__name__": "mewbo_pipeline",
                }
                compiled = compile(source, filename, "exec")
                exec(compiled, namespace)  # noqa: S102 - curated builtins + guarded import + lint-gated
                entrypoint = namespace.get(function_name)
                if not callable(entrypoint):
                    signature = (
                        "`def run(params, ctx)`"
                        if function_name == "run"
                        else "`def verify(result, ctx)`"
                    )
                    raise PipelineExecutionError(
                        "entrypoint", f"{filename!r} must define {signature}"
                    )
                box["output"] = entrypoint(*call_args)
            except BaseException as exc:  # noqa: BLE001 - captured to re-raise on the caller thread
                box["error"] = exc

        worker = threading.Thread(target=_target, name="mewbo-pipeline", daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            # The thread survives the join; the STOP is what bounds it. Set it
            # BEFORE raising, so the window in which an abandoned worker can still
            # write durably is the raise itself rather than the rest of its life.
            ctx.stop(
                f"the pipeline exceeded its {timeout_seconds:g}s time limit and was stopped; "
                "no further writes are accepted from this run"
            )
            raise PipelineExecutionError(
                "timeout", f"pipeline exceeded the {timeout_seconds:g}s time limit"
            )
        if "error" in box:
            self._reraise(box["error"])
        return box.get("output")

    @staticmethod
    def _reraise(err: BaseException) -> None:
        """Re-raise a worker-thread failure on the caller thread, bucketing the code."""
        if isinstance(err, PipelineExecutionError):
            raise err
        if isinstance(err, SyntaxError):
            raise PipelineExecutionError("syntax", f"pipeline code is invalid: {err.msg}") from err
        if isinstance(err, ImportError):
            raise PipelineExecutionError("import", str(err)) from err
        raise PipelineExecutionError("runtime", f"{type(err).__name__}: {err}") from err

    def _enforce_output(self, output: Any) -> Any:
        """Reject a non-JSON-serializable or oversized result."""
        try:
            serialized = json.dumps(output)
        except (TypeError, ValueError) as exc:
            raise PipelineExecutionError(
                "output", f"pipeline output is not JSON-serializable: {exc}"
            ) from None
        if len(serialized.encode("utf-8")) > self._max_output_bytes:
            raise PipelineExecutionError(
                "output", f"pipeline output exceeds the {self._max_output_bytes}-byte cap"
            )
        return output

    # -- cache --------------------------------------------------------------

    @staticmethod
    def _cache_key(app: AppSpec, pipeline: PipelineSpec, phash: str) -> tuple[str, int, str, str]:
        return (app.app_id, app.version, pipeline.name, phash)

    def _cache_get(
        self, app: AppSpec, pipeline: PipelineSpec, phash: str, now: datetime
    ) -> tuple[Any, datetime] | None:
        with self._cache_lock:
            entry = self._cache.get(self._cache_key(app, pipeline, phash))
        if entry is None:
            return None
        output, evaluated_at = entry
        if (now - evaluated_at).total_seconds() >= pipeline.cache_ttl_seconds:
            return None
        return output, evaluated_at

    def _cache_put(
        self, app: AppSpec, pipeline: PipelineSpec, phash: str, output: Any, now: datetime
    ) -> None:
        with self._cache_lock:
            self._cache[self._cache_key(app, pipeline, phash)] = (output, now)

    # -- the bounded LLM step (ctx.llm) ------------------------------------

    def _run_llm(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        now: datetime,
        ctx: PipelineContext,
        prompt: str,
        output_schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        """The ``ctx.llm`` policy: budget + wiring + cache, then invoke (bound per run).

        The context's ``llm()`` delegates here (via a ``partial`` closing over *app*/
        *pipeline*/*now*), passing itself as *ctx* so the per-run budget
        (``ctx.llm_tokens_spent``), the ``dry_run`` flag, and the source-mode cache
        salt (``ctx._llm_cache_salt``) all live on the context, not a boxed closure.
        Checks in order — declared budget (``0`` FORBIDS), deployment wiring, arg
        shape, object-root schema (rejected BEFORE any model call — the synthesis
        seam returns an object, so a non-object root only wastes a round-trip that
        would fail re-validation), the content-addressed cache (a hit costs no
        tokens, even under ``dry_run``), then the per-run cumulative budget cap. Only
        a real (cache-miss) model call is charged against the budget; and a cache
        WRITE is suppressed under ``dry_run`` (a preview must not mint an entry a
        later real, charged run would then serve un-provenanced).
        """
        budget = pipeline.llm_budget_tokens
        if budget <= 0:
            raise PipelineExecutionError(
                "llm",
                f"ctx.llm() is not enabled for pipeline {pipeline.name!r} — declare a "
                "positive `llm_budget_tokens` on the pipeline to use it",
            )
        if self._llm_invoke is None:
            raise PipelineExecutionError(
                "llm", "llm step not configured (no model backend wired for this deployment)"
            )
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise PipelineExecutionError("llm", "max_tokens must be a positive integer")
        if not isinstance(output_schema, dict):
            raise PipelineExecutionError("llm", "output_schema must be a JSON Schema object (dict)")
        if output_schema.get("type") != "object":
            raise PipelineExecutionError("llm", "output_schema must have root type 'object'")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PipelineExecutionError("llm", "llm requires a non-empty prompt string")

        digest = self._llm_digest(prompt, output_schema, salt=ctx._llm_cache_salt)
        cached = self._llm_cache_get(app, digest)
        if cached is not None:
            return cached
        if ctx.llm_tokens_spent + max_tokens > budget:
            raise PipelineExecutionError(
                "llm",
                f"llm budget exceeded: this call requests {max_tokens} tokens but only "
                f"{budget - ctx.llm_tokens_spent} of the {budget}-token budget remain for this run",
            )
        result = self._invoke_llm_with_retry(prompt, output_schema, max_tokens)
        ctx.llm_tokens_spent += max_tokens
        if not ctx._dry_run:  # a dry-run preview reads the cache but never writes it
            self._llm_cache_put(app, digest, result, now)
        return result

    def _invoke_llm_with_retry(
        self, prompt: str, output_schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        """Invoke the model, validate against the schema, ONE retry with the error appended."""
        invoke = self._llm_invoke
        if invoke is None:  # pragma: no cover - guarded by the caller
            raise PipelineExecutionError("llm", "llm step not configured")
        current_prompt = prompt
        last_err: PipelineExecutionError | None = None
        for _attempt in range(2):
            try:
                raw = invoke(current_prompt, output_schema, max_tokens)
                return self._validate_llm_output(raw, output_schema)
            except PipelineExecutionError as exc:
                last_err = exc
            except Exception as exc:  # noqa: BLE001 - any backend failure is bucketed + retried once
                last_err = PipelineExecutionError(
                    "llm", f"llm call failed: {type(exc).__name__}: {exc}"
                )
            current_prompt = (
                f"{prompt}\n\n[Your previous response was rejected: {last_err.message}. "
                "Respond again with ONLY a JSON object matching the schema.]"
            )
        assert last_err is not None  # every failing branch above set it
        raise last_err

    @staticmethod
    def _validate_llm_output(raw: Any, output_schema: dict[str, Any]) -> dict[str, Any]:
        """Reject a non-dict / schema-violating model result (same lib the runner uses)."""
        if not isinstance(raw, dict):
            raise PipelineExecutionError(
                "llm", f"llm returned a {type(raw).__name__}, expected a JSON object"
            )
        try:
            jsonschema.validate(instance=raw, schema=output_schema)
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise PipelineExecutionError(
                "llm", f"llm output field '{path}': {exc.message}"
            ) from None
        except jsonschema.exceptions.SchemaError as exc:
            raise PipelineExecutionError(
                "llm", f"output_schema is not a valid JSON Schema: {exc.message}"
            ) from None
        return raw

    @staticmethod
    def _llm_digest(
        prompt: str, output_schema: dict[str, Any], *, salt: str | None = None
    ) -> str:
        """Content address of a ``ctx.llm`` call — the cache key.

        ``sha256(prompt+schema+salt)`` via the shared :meth:`_canonical_digest`. In
        ``cache_mode="source"`` the runner passes the run's source fingerprint as
        *salt*, so a source change yields a different key and busts a stale answer
        that a fresh prompt string alone could not; in ttl/no-cache modes *salt* is
        ``None`` and the key is the plain prompt+schema address.
        """
        return AppPipelineRunner._canonical_digest(
            {"prompt": prompt, "schema": output_schema, "salt": salt}
        )

    def _llm_cache_get(self, app: AppSpec, digest: str) -> dict[str, Any] | None:
        with self._cache_lock:
            entry = self._cache.get((app.app_id, app.version, _LLM_CACHE_SLOT, digest))
        return None if entry is None else entry[0]

    def _llm_cache_put(
        self, app: AppSpec, digest: str, output: dict[str, Any], now: datetime
    ) -> None:
        with self._cache_lock:
            self._cache[(app.app_id, app.version, _LLM_CACHE_SLOT, digest)] = (output, now)

    # -- the source (read-through liveness) cache --------------------------

    def _source_cache_get(
        self, app: AppSpec, pipeline: PipelineSpec, phash: str
    ) -> _SourceCacheEntry | None:
        with self._cache_lock:
            return self._source_cache.get(self._cache_key(app, pipeline, phash))

    def _source_cache_put(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        phash: str,
        output: Any,
        now: datetime,
        read_paths: Iterable[str],
        glob_patterns: Iterable[str],
        fingerprint: str,
    ) -> None:
        entry = _SourceCacheEntry(
            output=output,
            evaluated_at=now,
            read_paths=frozenset(read_paths),
            glob_patterns=frozenset(glob_patterns),
            fingerprint=fingerprint,
        )
        with self._cache_lock:
            self._source_cache[self._cache_key(app, pipeline, phash)] = entry


__all__ = [
    "PIPELINE_ALLOWED_MODULES",
    "AppPipelineRunner",
    "PipelineContext",
    "PipelineExecutionError",
    "PipelineExecutor",
    "lint_pipeline",
]
