"""Source-access tools for ``wiki-qa``: read_file, grep, list_files.

State + behaviour live on a single atomic class — ``WikiSourceAccess`` —
that owns the QA-session-scoped clone dir and exposes the three
capabilities as instance methods. The three ``Tool`` shells below are
thin shims that the plugin loader registers: each one resolves a fresh
``WikiSourceAccess`` per call and delegates straight to the right
method. Keeps the safety semantics and clone-dir lookup in one place;
all the per-tool boilerplate ends up being four near-identical lines.

The grep implementation uses pure-Python ``re`` over files matching a
glob — fast enough for code-search-sized clones (sub-second for
hundreds-of-files repos) and dropping a ripgrep dep keeps the image
slim. Swap the inner loop for ``subprocess.run("rg")`` behind the same
wire shape if scale ever forces it.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    WikiQaCtx,
    resolve_qa_clone_dir,
    resolve_qa_ctx,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.source_tools")


# ---------------------------------------------------------------------------
# Argument schemas
# ---------------------------------------------------------------------------


class WikiReadFileArgs(BaseModel):
    """Arguments for ``wiki_read_file``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Repo-relative file path (no leading slash, no ``..``).")
    start_line: int | None = Field(
        default=None, ge=1,
        description="1-based first line to return (inclusive). Default = 1.",
    )
    end_line: int | None = Field(
        default=None, ge=1,
        description="1-based last line to return (inclusive). Default = end of file.",
    )


class WikiGrepArgs(BaseModel):
    """Arguments for ``wiki_grep``."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(description="Python regex pattern (case-insensitive).")
    glob: str | None = Field(
        default=None,
        description="Optional fnmatch glob to scope the search (e.g. ``**/*.py``).",
    )
    max_hits: int = Field(
        default=30, ge=1, le=60,
        description="Cap on hits returned. Defaults to 30.",
    )


class WikiListFilesArgs(BaseModel):
    """Arguments for ``wiki_list_files``."""

    model_config = ConfigDict(extra="forbid")

    glob: str = Field(
        default="**/*",
        description="fnmatch glob (e.g. ``src/**/*.py``). Defaults to all files.",
    )
    max_results: int = Field(
        default=200, ge=1, le=500,
        description="Cap on results returned.",
    )


# ---------------------------------------------------------------------------
# Atomic class — owns state + behaviour
# ---------------------------------------------------------------------------


class WikiSourceAccess:
    """Per-QA-session source-file capability.

    State: the resolved QA ctx + its clone dir.
    Behaviour: ``read_file``, ``grep``, ``list_files`` — each returns
    a wire-ready dict (callers wrap in ``MockSpeaker``).
    Statics: path-safety + decode helpers.

    Construct via :meth:`for_session`; that's the only path that goes
    through the runtime + store lookups. Direct ``__init__`` is for
    tests + reuse.
    """

    # Hard caps so a misbehaving agent or huge file can't blow up the
    # context window. Picked conservatively — far above any sane single
    # tool-call payload.
    MAX_FILE_BYTES = 200_000
    MAX_GREP_FILES_SCANNED = 2_000

    __slots__ = ("ctx", "clone_dir")

    def __init__(self, ctx: WikiQaCtx, clone_dir: Path) -> None:
        """Initialise with the QA ctx and resolved clone directory."""
        self.ctx = ctx
        self.clone_dir = clone_dir

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def for_session(cls, session_id: str) -> WikiSourceAccess | MockSpeaker:
        """Resolve QA ctx + clone dir for *session_id*.

        Returns a fresh ``WikiSourceAccess`` on success, or the
        :func:`_err_result` ``MockSpeaker`` the caller can hand straight
        back to the LLM.
        """
        runtime = cls._resolve_runtime()
        ctx = resolve_qa_ctx(session_id, runtime) if runtime else None
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")
        clone_dir = resolve_qa_clone_dir(ctx.slug, ctx.store)
        if clone_dir is None:
            return _err_result(
                "not_found",
                (
                    "no completed indexing clone is available for this slug; "
                    "source files cannot be inspected — fall back to wiki "
                    "pages / graph for this answer"
                ),
            )
        return cls(ctx=ctx, clone_dir=clone_dir)

    # ── Capabilities (instance methods) ─────────────────────────────

    def read_file(self, args: WikiReadFileArgs) -> dict[str, Any]:
        """Return a slice (or all) of a file from the indexed clone."""
        target = self._safe_path(args.path, self.clone_dir)
        if target is None or not target.is_file():
            return _err_result("not_found", f"file not found inside clone: {args.path!r}")

        try:
            data = target.read_bytes()
        except OSError as exc:
            return _err_result("internal", f"read failed: {exc}")

        if (
            len(data) > self.MAX_FILE_BYTES
            and args.start_line is None
            and args.end_line is None
        ):
            return _err_result(
                "too_large",
                (
                    f"file is {len(data)} bytes; pass start_line/end_line to "
                    f"read a slice (max {self.MAX_FILE_BYTES} bytes per call)"
                ),
            )

        text = self._decode(data)
        lines = text.splitlines()
        total = len(lines)
        start = max(0, (args.start_line or 1) - 1)
        end = min(total, args.end_line if args.end_line is not None else total)
        if start >= end:
            return _err_result(
                "validation",
                f"empty range: start_line={args.start_line} end_line={args.end_line}",
            )
        return {
            "path": args.path,
            "startLine": start + 1,
            "endLine": end,
            "totalLines": total,
            "content": "\n".join(lines[start:end]),
        }

    def grep(self, args: WikiGrepArgs) -> dict[str, Any]:
        """Case-insensitive regex search over the indexed clone."""
        try:
            regex = re.compile(args.pattern, re.IGNORECASE)
        except re.error as exc:
            return _err_result("validation", f"invalid regex: {exc}")

        glob = args.glob or "**/*"
        hits: list[dict[str, object]] = []
        scanned = 0
        for path in self.clone_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.clone_dir).as_posix()
            if not fnmatch.fnmatch(rel, glob):
                continue
            scanned += 1
            if scanned > self.MAX_GREP_FILES_SCANNED:
                break
            try:
                if path.stat().st_size > self.MAX_FILE_BYTES * 4:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append({"path": rel, "line": lineno, "text": line.strip()[:300]})
                    if len(hits) >= args.max_hits:
                        break
            if len(hits) >= args.max_hits:
                break

        return {
            "hits": hits,
            "filesScanned": scanned,
            "truncated": len(hits) >= args.max_hits or scanned > self.MAX_GREP_FILES_SCANNED,
        }

    def list_files(self, args: WikiListFilesArgs) -> dict[str, Any]:
        """List paths under the indexed clone matching *args.glob*."""
        paths: list[str] = []
        truncated = False
        for p in self.clone_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.clone_dir).as_posix()
            if not fnmatch.fnmatch(rel, args.glob):
                continue
            paths.append(rel)
            if len(paths) >= args.max_results:
                truncated = True
                break
        paths.sort()
        return {"paths": paths, "count": len(paths), "truncated": truncated}

    # ── Statics (pure helpers) ──────────────────────────────────────

    @staticmethod
    def _safe_path(rel: str, clone_dir: Path) -> Path | None:
        """Resolve *rel* under *clone_dir*; refuse absolute paths and ``..`` escapes.

        ``resolve()`` collapses ``..`` and dereferences symlinks, so a target
        that lands outside ``clone_dir`` fails ``relative_to`` → ``None``. Static
        + clone-dir-parameterised so the API source route can reuse the guard
        without constructing a ``WikiSourceAccess``/``WikiQaCtx`` just to read it.
        """
        if not rel or rel.startswith("/"):
            return None
        candidate = (clone_dir / rel).resolve()
        try:
            candidate.relative_to(clone_dir.resolve())
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _resolve_runtime() -> Any:
        try:
            from ._ctx import resolve_runtime  # noqa: PLC0415
            return resolve_runtime()
        except ImportError:
            return None


# ---------------------------------------------------------------------------
# Tool shims — registry entries that delegate to the atomic class
# ---------------------------------------------------------------------------


class _SourceToolShim(WikiSessionTool):
    """Shared boilerplate for the three source-access tool shims.

    Subclasses set ``tool_id``, ``args_cls``, ``schema``, and override
    ``_call(access, args)`` to delegate to the right
    :class:`WikiSourceAccess` method. Session glue + terminate live on
    :class:`WikiSessionTool`; ctx resolution is via ``for_session``.
    """

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Resolve the per-session source access and delegate to ``_call``."""
        access = WikiSourceAccess.for_session(self._session_id)
        if isinstance(access, MockSpeaker):  # err payload from _err_result
            return access

        args = self._parse_args(self.args_cls, action_step)
        if isinstance(args, MockSpeaker):
            return args

        result = self._call(access, args)
        # access.ctx is the WikiQaCtx (carries answer_id) — record the file(s)
        # this read touched for the deterministic citation trail. list_files
        # only *lists* paths (no read), so it records nothing (default []).
        self._record_qa_access(access.ctx, self._access_refs(args, result))
        return MockSpeaker(content=str(result))

    def _call(self, access: WikiSourceAccess, args: Any) -> dict[str, Any]:
        """Delegate to the right :class:`WikiSourceAccess` method (subclass)."""
        raise NotImplementedError

    def _access_refs(self, args: Any, result: Any) -> list[str]:
        """Citation refs for the file(s) this tool actually read. Default: none."""
        return []


class WikiReadFileTool(_SourceToolShim):
    """SessionTool: read a slice (or all) of a file from the indexed clone."""

    tool_id = "wiki_read_file"
    args_cls = WikiReadFileArgs
    schema = pydantic_to_openai_tool(WikiReadFileArgs, name="wiki_read_file")

    def _call(self, access: WikiSourceAccess, args: WikiReadFileArgs) -> dict[str, Any]:
        return access.read_file(args)

    def _access_refs(self, args: WikiReadFileArgs, result: Any) -> list[str]:
        if not isinstance(result, dict) or "error" in result:
            return []
        if args.start_line and args.end_line:
            return [f"{args.path}#L{args.start_line}-{args.end_line}"]
        return [args.path]


class WikiGrepTool(_SourceToolShim):
    """SessionTool: case-insensitive regex search over the indexed clone."""

    tool_id = "wiki_grep"
    args_cls = WikiGrepArgs
    schema = pydantic_to_openai_tool(WikiGrepArgs, name="wiki_grep")

    def _call(self, access: WikiSourceAccess, args: WikiGrepArgs) -> dict[str, Any]:
        return access.grep(args)

    def _access_refs(self, args: WikiGrepArgs, result: Any) -> list[str]:
        if not isinstance(result, dict):
            return []
        out: list[str] = []
        for hit in result.get("hits", []):
            path = hit.get("path")
            if path and path not in out:
                out.append(path)
        return out


class WikiListFilesTool(_SourceToolShim):
    """SessionTool: list paths under the indexed clone matching a glob."""

    tool_id = "wiki_list_files"
    args_cls = WikiListFilesArgs
    schema = pydantic_to_openai_tool(WikiListFilesArgs, name="wiki_list_files")

    def _call(self, access: WikiSourceAccess, args: WikiListFilesArgs) -> dict[str, Any]:
        return access.list_files(args)


__all__ = [
    "WikiGrepArgs",
    "WikiGrepTool",
    "WikiListFilesArgs",
    "WikiListFilesTool",
    "WikiReadFileArgs",
    "WikiReadFileTool",
    "WikiSourceAccess",
]
