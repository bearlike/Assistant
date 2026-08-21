"""``wiki_scan_tree`` SessionTool — walks the cloned tree, applies filters, emits events."""
from __future__ import annotations

import asyncio
import fnmatch
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import ProgressReporter
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam
from mewbo_graph.plugins.wiki.step_plans import (  # noqa: F401 — compatibility export
    SCAN_STEPS,
    planned_steps_for_slug,
)
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.refresh import ChangeDetector
from mewbo_graph.wiki.types import IndexingJob

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

    from mewbo_graph.plugins.wiki._ctx import WikiJobCtx

logging = get_logger(name="mewbo_graph.plugins.wiki.scan")

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

# DEFAULT ceiling for one tree walk; the operator sizes the real value
# (``wiki.phase_timeouts.scan_s``). The scan READS AND HASHES every file it
# keeps, so its honest cost tracks total bytes on disk rather than file count,
# and it touches no network and no subprocess — which is why its default is a
# fraction of the graph build's. A repository of a few thousand files scans in
# the order of seconds, so this leaves roughly two orders of magnitude of
# headroom before a walk is called wedged.
_SCAN_BUDGET_S: float = 900.0

# Headroom between this tool's deadline and the loop's ceiling, so the TOOL
# expires first. Not operator-tunable — it encodes which layer expires, not a
# quantity about this deployment (mirrors ``ask_user.QUESTION_TIMEOUT_MARGIN_S``).
_CEILING_MARGIN_S: float = 60.0

# The one line that makes the bounded result actionable rather than merely
# small: the indexer session's working directory IS the clone
# (``jobs._start_indexer_session``) and its allowlist already carries
# ``read_file``/``glob``/``grep``/``ls``, so a planner reaches any subtree in one
# call. Without saying so, a model handed only aggregates re-derives the file
# list turn after turn instead of trusting the tools it holds.
_DETAIL_HINT: str = (
    "Per-file paths are deliberately not returned. The clone IS this session's "
    "working directory: use ls / glob / grep / read_file to list or read any "
    "subtree on demand (e.g. glob('packages/**/*.py'), ls('apps'))."
)


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
# Bounded model-facing result
# ---------------------------------------------------------------------------


class ScanSummary:
    """Repo shape folded into counters — the payload the model actually reads.

    The per-file manifest this replaces was O(files): ~180,000 characters for a
    ~2,000-file repository, resent byte-identical on every resume, within 10% of
    the session-tool result cap — and NOTHING downstream consumed it
    (``build_graph`` re-walks the clone itself). What a page planner needs from a
    scan is shape, not an inventory: how big the repo is, which languages it is
    written in, and which subtrees are worth a page. All three fold into
    counters whose rendered SIZE is fixed by the caps below rather than by the
    number of files, so a repository ten times larger costs the same context.

    Detail is reachable on demand — see :data:`_DETAIL_HINT`.
    """

    _TOP_EXTENSIONS = 15
    _TOP_DIRECTORIES = 30
    _MAX_ROOT_FILES = 20
    # Group on two path segments. One segment collapses a monorepo into
    # ``packages``/``apps``, which names no planning unit; two resolves
    # ``packages/mewbo_core`` while keeping the group count a function of the
    # repo's LAYOUT (dozens) rather than its file count (thousands).
    _GROUP_DEPTH = 2

    def __init__(self) -> None:
        """Start an empty fold."""
        self.files = 0
        self.bytes = 0
        self._ext_files: Counter[str] = Counter()
        self._ext_bytes: Counter[str] = Counter()
        self._dir_files: Counter[str] = Counter()
        self._dir_bytes: Counter[str] = Counter()
        self._root_files: list[str] = []

    def record(self, rel: Path, size: int) -> None:
        """Fold one scanned file (repo-relative *rel*, *size* bytes) in."""
        self.files += 1
        self.bytes += size
        ext = rel.suffix or "(none)"
        self._ext_files[ext] += 1
        self._ext_bytes[ext] += size
        segments = rel.parts[:-1]
        group = "/".join(segments[: self._GROUP_DEPTH]) if segments else "."
        self._dir_files[group] += 1
        self._dir_bytes[group] += size
        # Root files name the ecosystem in one glance (README, pyproject,
        # package.json, Makefile). Dotfiles are excluded because they sort FIRST
        # and would spend the whole cap before reaching any of them — measured on
        # a real tree, 14 of 20 slots went to `.gitignore`-class entries and
        # editor droppings. They are still counted in ``totals`` and in the "."
        # directory group; nothing is hidden, only deprioritised.
        if (
            not segments
            and not rel.name.startswith(".")
            and len(self._root_files) < self._MAX_ROOT_FILES
        ):
            self._root_files.append(rel.name)

    def as_result(self) -> dict[str, Any]:
        """Return the bounded result payload.

        ``totals`` carries the FULL extension/directory cardinality alongside the
        truncated lists, so the elision is visible: a model that sees 15 of 140
        extensions knows to look further instead of reading the list as complete.
        """
        return {
            "totals": {
                "files": self.files,
                "bytes": self.bytes,
                "extensions": len(self._ext_files),
                "directories": len(self._dir_files),
            },
            "extensions": [
                {"ext": ext, "files": count, "bytes": self._ext_bytes[ext]}
                for ext, count in self._ext_files.most_common(self._TOP_EXTENSIONS)
            ],
            "directories": [
                {"dir": name, "files": count, "bytes": self._dir_bytes[name]}
                for name, count in self._dir_files.most_common(self._TOP_DIRECTORIES)
            ],
            "root_files": self._root_files,
            "detail": _DETAIL_HINT,
        }


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiScanTreeTool(WikiSessionTool):
    """SessionTool: walk the cloned tree, apply filters, emit scanning/scanned events."""

    tool_id = "wiki_scan_tree"
    args_cls = WikiScanArgs
    schema: dict[str, object] = pydantic_to_openai_tool(WikiScanArgs, name="wiki_scan_tree")

    # Sized from the artifact, per the session-tool guidance. Measured: a
    # repo-shaped 1,150-file tree renders 2,682 characters, and the WIDEST
    # payload :class:`ScanSummary` can render — every cap saturated with long
    # two-segment directory names — renders 4,594. This carries roughly twice
    # the realistic figure and comfortably clears the saturated one.
    #
    # It is a CEILING on a structurally bounded payload, not a budget to spend:
    # unlike the 200,000-character default it inherited, nothing here grows with
    # the repository, so a truncation at this cap would mean a bug in the fold.
    max_result_chars = 8_000

    def _budget_s(self) -> float:
        """This deployment's ceiling for one tree walk, in seconds."""
        return float(
            get_config_value("wiki", "phase_timeouts", "scan_s", default=_SCAN_BUDGET_S)
        )

    def execution_timeout(self, tool_input: object) -> float | None:
        """The loop's outer ceiling for this call: this tool's budget + margin.

        A SessionTool has no ``ToolSpec``, so leaving this undeclared means the
        registry's flat 120s — which a large tree exceeds while working
        perfectly. The margin keeps the TOOL the thing that expires.
        """
        return self._budget_s() + _CEILING_MARGIN_S

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_scan_tree`` tool call, off the event loop.

        The body walks the tree, stats and hashes every kept file, and writes to
        the store on a 50ms cadence — all blocking, with no ``await`` in it. Run
        inline it held the loop for the length of the walk; off-loop the walk's
        own progress writes are the only thing that has to keep up.
        """
        budget_s = self._budget_s()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._handle_blocking, action_step), timeout=budget_s
            )
        except asyncio.TimeoutError:
            return self._over_budget_result(budget_s)

    def _over_budget_result(self, budget_s: float) -> MockSpeaker:
        """The bounded outcome for a tree walk that outran its budget.

        The walk is not stopped by abandoning the wait, and unlike the clone it
        has a partial result that is genuinely useful: ``scanned_count`` and the
        per-file events it already flushed stay on the job. So the message points
        at those rather than implying the phase produced nothing.

        No ``_headline`` is stamped here on purpose: this tool declares no
        ``result_headline`` hook, so a headline written here would be a value
        nothing reads.
        """
        budget = f"{budget_s:g}s"
        ctx = self._job_ctx()
        if ctx is not None:
            from mewbo_graph.plugins.wiki._ctx import emit_log  # noqa: PLC0415

            emit_log(ctx, f"Scan exceeded its budget of {budget}", level="warn")
        return _err_result(
            "timeout",
            f"scan exceeded its budget of {budget}. The walk is still running "
            "and the files it has already scanned are recorded on the job — do "
            "not call wiki_scan_tree again for this job.",
        )

    def _handle_blocking(self, action_step: ActionStep) -> MockSpeaker:
        """The synchronous body of the tool call — runs on a worker thread."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiScanArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Collect files using the ctx clone_dir.
        clone_dir = ctx.clone_dir
        if not clone_dir.exists():
            return _err_result("internal", f"clone_dir does not exist: {clone_dir}")

        from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase  # noqa: PLC0415
        emit_phase(ctx, "scan")
        progress = ProgressReporter(ctx)
        progress.declare(planned_steps_for_slug(ctx.store, ctx.slug, "scan"))

        with progress.step("scan.discover"):
            files = _collect_files(clone_dir, args)
            total = len(files)
            emit_log(ctx, f"Scanning {total} files in {clone_dir.name}…")

        # 4. Emit scanning/scanned events, fold the summary, build the manifest.
        summary = ScanSummary()
        entries: list[FileManifest] = []
        last_flush = time.monotonic()
        pending_events: list[dict[str, Any]] = []

        with progress.step("scan.inspect_files", total=total) as step:
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
                    #
                    # currentFile rides that SAME write rather than an unconditional
                    # update_job per file, which would cost one store round-trip for
                    # every file in the repo (~2,000 on a real one) for a field the
                    # UI merely samples. The last file always flushes (``idx ==
                    # total - 1``), so a finished job still names the file it ended
                    # on; in between, progress advances every 50ms, which is faster
                    # than anyone reads it.
                    # ``last_progress_at`` rides this SAME write rather than getting
                    # a PhaseProgress of its own. Scan already owns an honest
                    # per-file counter pair and a 50ms flush; routing it through the
                    # 5s phase-progress throttle would either halve the live scan
                    # cadence or double this write — and this write was deliberately
                    # collapsed down from one-per-file. One extra field on a write
                    # that already happens buys scan its place in the "is this job
                    # still moving" signal for nothing.
                    ctx.store.update_job(
                        ctx.job_id,
                        scanned_count=idx + 1,
                        current_file=file_str,
                        last_progress_at=IndexingJob.format_stamp(
                            datetime.now(timezone.utc)
                        ),
                    )

                size = abs_path.stat().st_size
                summary.record(rel, size)
                entries.append(
                    FileManifest(
                        slug=ctx.slug,
                        path=file_str,
                        # Reuses the reader's own hash so the two sides agree by
                        # construction: ``ChangeDetector`` diffs the working tree
                        # against exactly this field, and a manifest hashed any
                        # other way would report every file modified forever.
                        content_hash=ChangeDetector._hash_file(abs_path),
                        last_indexed_commit=ctx.commit_sha,
                    )
                )
                step.advance(idx + 1, total, detail=file_str)

        # Flush any remaining events (handles total == 0 case cleanly).
        for evt in pending_events:
            ctx.store.append_job_event(ctx.job_id, evt)

        with progress.step("scan.persist_manifest") as step:
            self._persist_manifest(ctx, entries)
            step.log(f"Scanned {total} files")

        return MockSpeaker(content=str(summary.as_result()))

    @staticmethod
    def _persist_manifest(ctx: WikiJobCtx, entries: list[FileManifest]) -> None:
        """Persist the per-file manifest for *ctx*'s slug, stamped with its commit.

        The scan is the only phase that knows the full file set at a commit, so
        it is where the manifest baseline has to be written — ``build_graph``
        sees only what it could parse. Best-effort with a VISIBLE failure: the
        manifest has no consumer inside this run (it exists for a later
        incremental refresh), so a store hiccup must not kill an otherwise
        healthy index, but it must not vanish silently either — the warning lands
        in the job event log the indexing page renders.

        **Ordering constraint for whoever wires the scoped-refresh ACT path:**
        this write moves the baseline to the commit just scanned, so a
        ``ChangeDetector.detect`` that runs AFTER it sees an empty diff and
        refreshes nothing. Detect first, then let the scan re-stamp.
        """
        if not entries:
            return
        from mewbo_graph.plugins.wiki._ctx import emit_log  # noqa: PLC0415

        try:
            ctx.store.upsert_file_manifest(ctx.slug, entries)
        except Exception as exc:  # pragma: no cover — store backend failure
            emit_log(ctx, f"Could not persist the file manifest: {exc}", level="warn")


__all__ = [
    "ScanSummary",
    "WikiScanArgs",
    "WikiScanTreeTool",
]
