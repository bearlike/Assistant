"""``JoblessIndexRunner`` — the scaffolding every SESSIONLESS runner shares.

Two runners drive a wiki job with no Mewbo session and no LLM agent behind them:
:class:`~mewbo_graph.plugins.wiki.graph_only.GraphOnlyIndexer` (developer mode's
deterministic ``clone → scan → graph → finalize``) and
:class:`~mewbo_graph.plugins.wiki.scoped_refresh.ScopedRefreshRunner` (the
incremental refresh's deterministic Free tier). They differ only in their TAIL
phases; their head — clone the repo, walk the tree, poll for a cooperative
cancel, record a terminal failure, and never leak an exception into the daemon
thread — is the same ~120 lines.

**Why this lives in its own module rather than beside either runner.** Putting
it in ``scoped_refresh.py`` would make the older, simpler runner import the
newer, more specialised one, so a future third runner would inherit from
whichever file happened to be written second. A leading underscore matches the
suite's other shared-internals modules (``_ctx``, ``_base``, ``_platform_api``)
and marks it as a seam for this package, not a public entry point.

The specialisation is a TEMPLATE METHOD: :meth:`JoblessIndexRunner.run` iterates
whatever :meth:`JoblessIndexRunner._phases` returns, so a subclass declares its
pipeline as data and inherits the cancellation, failure and never-raise
behaviour rather than re-implementing them next to its own tail.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from mewbo_core.common import get_logger

from mewbo_graph.plugins.wiki._ctx import ProgressReporter, emit_log, emit_phase
from mewbo_graph.plugins.wiki.clone import _git_rev_parse, clone_with_fallback
from mewbo_graph.plugins.wiki.scan import WikiScanArgs, _collect_files

if TYPE_CHECKING:
    from pathlib import Path

    from mewbo_graph.plugins.wiki._ctx import WikiJobCtx
    from mewbo_graph.wiki.types import WizardSubmission

logging = get_logger(name="mewbo_graph.plugins.wiki._jobless")


class JoblessPhaseError(Exception):
    """Internal carrier of a (code, message) phase failure within a runner."""

    def __init__(self, code: str, message: str) -> None:
        """Store the wiki-error code alongside the message."""
        self.code = code
        super().__init__(message)


class JoblessIndexRunner:
    """Clone + scan + cooperative cancellation for a runner with no session.

    Construct with the durable handles a run needs (``ctx`` + the ``submission``
    contract), then call :meth:`run`. All state lives in the wiki store via the
    same event/snapshot writes the agent path uses — this class owns only the
    per-run bookkeeping (the collected file list) that its tail phases read.
    """

    # ── wording knobs ───────────────────────────────────────────────────
    # The inherited phases emit job EVENTS, and both the SSE timeline and the
    # error card render their text verbatim — so the wording is part of what
    # the two runners keep distinct, not decoration. Three attributes for
    # three sentence positions, rather than one token woven into sentences it
    # does not fit grammatically.
    _RUN_NOUN: str = "index"  # "<Noun> cancelled — stopping"
    _CLONE_NOTE: str = ""  # parenthetical on the clone line
    _FAILURE_NOUN: str = "indexing"  # "<noun> failed: <exc>"

    def __init__(self, ctx: WikiJobCtx, submission: WizardSubmission) -> None:
        """Bind the job ctx and the submission contract for this run."""
        self._ctx = ctx
        self._submission = submission
        # Repo-relative paths the scan phase collected. A tail phase that needs
        # the scope re-reads THIS rather than re-walking the tree: a second walk
        # can disagree with the first (the checkout is live on disk), and the
        # scan events already told every reader which list the run is working on.
        self._files: list[Path] = []

    # ── public entry point ──────────────────────────────────────────────

    def run(self) -> None:
        """Drive this runner's phases in order; mark the job failed on error.

        Never raises into the caller (mirrors a session that ends cleanly): a
        phase failure is recorded on the job snapshot + event log so the SSE
        stream + landing card show an honest terminal state, exactly as the
        agent path's error handling does.
        """
        ctx = self._ctx
        try:
            # Cooperative cancellation: ``WikiIndexingJob.cancel`` marks the job
            # record ``cancelled`` from the request thread, but this daemon thread
            # owns the terminal write. Re-read the job status BEFORE each phase and
            # bail WITHOUT overwriting ``cancelled`` — otherwise the run would clobber
            # the user's cancel with a later ``complete``/``failed``.
            for phase in self._phases():
                if self._cancelled():
                    emit_log(ctx, f"{self._RUN_NOUN.capitalize()} cancelled — stopping")
                    return
                phase()
        except JoblessPhaseError as exc:
            if not self._cancelled():
                self._fail(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — never leak into the worker thread
            logging.warning(
                "{} failed for {}: {}", self._RUN_NOUN, ctx.slug, exc
            )
            if not self._cancelled():
                self._fail("internal", f"{self._FAILURE_NOUN} failed: {exc}")

    def _phases(self) -> tuple[Callable[[], None], ...]:
        """Return this runner's phase callables, in pipeline order.

        Declared as data so :meth:`run` — and every behaviour it owns — is
        inherited whole rather than re-typed beside each runner's tail.
        """
        raise NotImplementedError

    # ── shared phases ───────────────────────────────────────────────────

    def _clone(self) -> None:
        """Shallow-clone the repo into ``ctx.clone_dir`` and stamp git metadata.

        Delegates to the SAME :func:`clone_with_fallback` the ``wiki_clone_repo``
        tool uses (credential chain: submission token → durable repo/host store →
        ambient git credential → anonymous; per-attempt dir reset; helper-disable
        + prompt-off env; secret redaction) — no duplicated resolution/SSH logic.
        """
        ctx = self._ctx
        sub = self._submission
        url = sub.repo_url or ""
        if not url:
            raise JoblessPhaseError(
                "validation", f"{self._RUN_NOUN} requires a repo URL"
            )

        clone_dir = ctx.clone_dir
        emit_phase(ctx, "clone")
        note = f" ({self._CLONE_NOTE})" if self._CLONE_NOTE else ""
        emit_log(ctx, f"Cloning {url}{note}…")

        def on_log(
            text: str, *, level: Literal["info", "warn", "error"] = "info"
        ) -> None:
            emit_log(ctx, text, level=level)

        outcome = clone_with_fallback(
            url,
            clone_dir,
            ref=sub.ref,
            store=ctx.store,
            slug=ctx.slug,
            arg_token=sub.token,
            on_log=on_log,
        )
        if not outcome.ok:
            raise JoblessPhaseError("repo_access", outcome.stderr)

        total = self._count_files(clone_dir)
        head = self._rev_parse(clone_dir, ["HEAD"]) or ""
        branch = self._rev_parse(clone_dir, ["--abbrev-ref", "HEAD"]) or ""
        if not branch or branch == "HEAD":
            branch = ""

        ctx.store.update_job(
            ctx.job_id,
            status="scanning",
            total_count=total,
            branch=branch or None,
            commit_sha=head or None,
        )
        ctx.store.append_job_event(ctx.job_id, {
            "type": "queued",
            "jobId": ctx.job_id,
            "slug": ctx.slug,
            "totalCount": total,
        })
        emit_log(ctx, f"Cloned {total} files into {clone_dir.name}")

    def _scan(self) -> None:
        """Walk the clone tree applying the submission's filters; emit scan events.

        Reuses :func:`scan._collect_files` so the always-exclude + glob-filter
        logic is shared with the tool. Emits per-file ``scanning``/``scanned``
        events + ``scanned_count`` so the scan-phase sub-progress bar fills.
        """
        ctx = self._ctx
        clone_dir = ctx.clone_dir
        if not clone_dir.exists():
            raise JoblessPhaseError("internal", f"clone dir missing: {clone_dir}")

        emit_phase(ctx, "scan")
        progress = ProgressReporter(ctx)
        args = WikiScanArgs(
            filter_mode=self._submission.filter_mode,
            dirs=list(self._submission.dirs),
            files=list(self._submission.files),
        )
        with progress.step("scan.discover") as step:
            files = _collect_files(clone_dir, args)
            self._files = files
            total = len(files)
            step.log(f"Scanning {total} files in {clone_dir.name}…")
        with progress.step("scan.inspect_files", total=total) as step:
            for idx, rel in enumerate(files):
                file_str = str(rel)
                ctx.store.append_job_event(ctx.job_id, {
                    "type": "scanning", "file": file_str, "index": idx, "totalCount": total,
                })
                ctx.store.append_job_event(ctx.job_id, {
                    "type": "scanned", "file": file_str, "index": idx, "totalCount": total,
                })
                ctx.store.update_job(ctx.job_id, current_file=file_str, scanned_count=idx + 1)
                step.advance(idx + 1, total, file_str)
        with progress.step("scan.persist_manifest") as step:
            step.log(f"Scanned {total} files")

    # ── helpers ─────────────────────────────────────────────────────────

    def _scanned_paths(self) -> list[Path]:
        """The scan phase's file list as ABSOLUTE paths under the clone dir.

        ``_collect_files`` yields repo-relative paths (that is what the scan
        events and the manifest are keyed by), while every consumer that reads
        bytes off disk needs a real path. Rooting them here keeps the two
        representations from being derived independently at each call site.
        """
        return [self._ctx.clone_dir / rel for rel in self._files]

    def _rev_parse(self, clone_dir: Path, args: list[str]) -> str | None:
        """Read one ``git rev-parse`` fact from *clone_dir* (``None`` if absent).

        A METHOD rather than a direct call to the imported helper so a subclass
        whose own module is the established patch seam can keep it — see
        ``GraphOnlyIndexer._rev_parse``. Overriding it is also how a runner that
        needs different git facts says so, instead of re-implementing ``_clone``.
        """
        return _git_rev_parse(clone_dir, args)

    def _cancelled(self) -> bool:
        """True iff the job was cancelled out-of-band (re-read from the store).

        ``WikiIndexingJob.cancel`` sets status ``cancelled`` from the request
        thread; this daemon thread polls it at phase boundaries to stop early
        without clobbering the cancel. Best-effort — a store hiccup reads as
        not-cancelled (the run proceeds, no worse than today).
        """
        try:
            job = self._ctx.store.get_job(self._ctx.job_id)
        except Exception:  # pragma: no cover — best-effort
            return False
        return job is not None and job.status == "cancelled"

    @staticmethod
    def _count_files(clone_dir: Path) -> int:
        """Count files in *clone_dir*, skipping ``.git`` internals."""
        return sum(
            1 for p in clone_dir.rglob("*") if p.is_file() and ".git" not in p.parts
        )

    def _fail(self, code: str, message: str) -> None:
        """Record a terminal failure on the job snapshot + event log (best-effort)."""
        ctx = self._ctx
        try:
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error", "error": {"code": code, "message": message},
            })
            ctx.store.update_job(ctx.job_id, status="failed", current_file=None)
        except Exception:  # pragma: no cover — best-effort honesty
            logging.warning(
                "{} fail-record for {} failed", self._RUN_NOUN, ctx.slug, exc_info=True
            )


__all__ = ["JoblessIndexRunner", "JoblessPhaseError"]
