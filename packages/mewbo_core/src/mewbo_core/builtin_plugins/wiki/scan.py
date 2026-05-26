"""``wiki_scan_tree`` SessionTool — walks the cloned tree, applies filters, emits events."""
from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import resolve_job_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result, _resolve_runtime
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.scan")

# ---------------------------------------------------------------------------
# Baseline always-excluded directory names
# ---------------------------------------------------------------------------

_ALWAYS_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
})

# Flush scanning/scanned events at most every this many seconds to avoid
# hammering the store on large repos. Tests run synchronously so they always
# flush (elapsed > threshold after the first call in wall-clock tests).
_FLUSH_INTERVAL_S: float = 0.05


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiScanArgs(BaseModel):
    """Arguments for ``wiki_scan_tree``."""

    model_config = ConfigDict(extra="forbid")

    filter_mode: Literal["exclude", "include"] = Field(default="exclude")
    dirs: list[str] = Field(
        default_factory=list,
        description="Dir-name globs (e.g. 'node_modules', 'tests/*').",
    )
    files: list[str] = Field(
        default_factory=list,
        description="Filename globs (e.g. '*.lock', 'LICENSE').",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _should_include(
    rel: Path,
    *,
    filter_mode: str,
    dir_globs: list[str],
    file_globs: list[str],
) -> bool:
    """Return True if *rel* passes the filter.

    Always-exclude dirs are handled upstream (the walk prunes them before
    calling this function). This function only applies the user-supplied globs.
    """
    filename = rel.name
    parts = rel.parts  # relative parts including filename

    dir_match = any(
        any(fnmatch.fnmatch(seg, g) for seg in parts[:-1])
        for g in dir_globs
    )
    file_match = any(fnmatch.fnmatch(filename, g) for g in file_globs)
    matched = dir_match or file_match

    if filter_mode == "exclude":
        return not matched
    # include mode
    return matched


def _collect_files(clone_dir: Path, args: WikiScanArgs) -> list[Path]:
    """Walk *clone_dir*, skip always-excluded dirs, apply filter, return sorted list."""
    included: list[Path] = []

    for path in clone_dir.rglob("*"):
        if not path.is_file():
            continue

        rel = path.relative_to(clone_dir)

        # Skip if any path segment is in the always-exclude set OR is a hidden dir
        skip = False
        for part in rel.parts[:-1]:  # directory segments only
            if part in _ALWAYS_EXCLUDE_DIRS or (part.startswith(".") and part != "."):
                skip = True
                break
        if skip:
            continue

        if not _should_include(
            rel,
            filter_mode=args.filter_mode,
            dir_globs=args.dirs,
            file_globs=args.files,
        ):
            continue

        included.append(rel)

    included.sort()
    return included


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiScanTreeTool:
    """SessionTool: walk the cloned tree, apply filters, emit scanning/scanned events."""

    tool_id = "wiki_scan_tree"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, object] = pydantic_to_openai_tool(WikiScanArgs, name="wiki_scan_tree")

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_scan_tree`` tool call."""
        # 1. Resolve runtime and job ctx.
        runtime = _resolve_runtime()
        ctx = resolve_job_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiScanArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        # 3. Collect files using the ctx clone_dir.
        clone_dir = ctx.clone_dir
        if not clone_dir.exists():
            return _err_result("internal", f"clone_dir does not exist: {clone_dir}")

        from mewbo_core.builtin_plugins.wiki._ctx import emit_log, emit_phase  # noqa: PLC0415
        emit_phase(ctx, "scan")

        files = _collect_files(clone_dir, args)
        total = len(files)
        emit_log(ctx, f"Scanning {total} files in {clone_dir.name}…")

        # 4. Emit scanning/scanned events and build manifest.
        manifest: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        pending_events: list[dict[str, Any]] = []

        for idx, rel in enumerate(files):
            abs_path = clone_dir / rel
            file_str = str(rel)

            scanning_evt: dict[str, Any] = {
                "type": "scanning",
                "file": file_str,
                "index": idx,
                "totalCount": total,
            }
            scanned_evt: dict[str, Any] = {
                "type": "scanned",
                "file": file_str,
                "index": idx,
                "totalCount": total,
            }

            pending_events.extend([scanning_evt, scanned_evt])

            # Update currentFile on the job record as we process each file.
            ctx.store.update_job(ctx.job_id, current_file=file_str)

            now = time.monotonic()
            if now - last_flush >= _FLUSH_INTERVAL_S or idx == total - 1:
                for evt in pending_events:
                    ctx.store.append_job_event(ctx.job_id, evt)
                pending_events = []
                last_flush = now
                # Persist scanned_count on the same flush cadence so the
                # /v1/wiki/index/<id> snapshot (used by the landing-page
                # "Indexing now" tile) shows real progress. SSE consumers
                # fold events live and don't need this; HTTP pollers do.
                ctx.store.update_job(ctx.job_id, scanned_count=idx + 1)

            manifest.append({
                "path": file_str,
                "size": abs_path.stat().st_size,
                "ext": abs_path.suffix,
            })

        # Flush any remaining events (handles total == 0 case cleanly).
        for evt in pending_events:
            ctx.store.append_job_event(ctx.job_id, evt)

        emit_log(ctx, f"Scanned {total} files")
        return MockSpeaker(content=str({"files": manifest}))


__all__ = [
    "WikiScanArgs",
    "WikiScanTreeTool",
]
