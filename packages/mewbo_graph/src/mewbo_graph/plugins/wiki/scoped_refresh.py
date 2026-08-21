"""``ScopedRefreshRunner`` — the two-stage runner behind an incremental refresh.

A scoped refresh re-indexes only what a diff touched: clone the repo at its new
commit, walk it, hand the file list to
:class:`~mewbo_graph.wiki.refresh.RefreshOrchestrator` (change detect → graph
delta → memory reconcile → doc staleness), publish the resulting scope,
regenerate the pages that scope flagged, and finalize. Everything up to the
scope publication is deterministic and zero-LLM apart from the memory
reconciler's drift band, whose call count the scope preview reports verbatim —
which is what makes the Free tier's name an honest word rather than an
approximate one.

It reuses the EXISTING ``IndexingPhase`` vocabulary rather than minting names of
its own: ``clone`` and ``scan`` mean exactly what they always did, the whole
delta pass runs inside ``graph`` (it is a graph re-index, scoped), the act phase
below runs under ``pages``, and ``finalize`` publishes. So the console's phase
map, the progress bar and the resume machinery need no scoped-refresh special
case.

The runner itself owns no session: the API calls :meth:`ScopedRefreshRunner.run`
on a daemon thread, exactly as it drives
:class:`~mewbo_graph.plugins.wiki.graph_only.GraphOnlyIndexer`.

**Two stages, and only the second costs anything.** Stage 1 is everything above
— deterministic and zero-LLM apart from the drift band. Stage 2 (``_act``) hands
the pages the doc planner scored ``edit``/``regenerate`` to a page-regeneration
tier, which the api registers through the ``act_launcher`` seam; with no tier
registered the refresh finalizes with no stage 2 and the timeline says the
flagged pages are still the OLD pages. ``docs.new_pages`` — pages PROPOSED
for undocumented symbols — stays out of both stages: authoring documentation
nobody asked for is a separate, opt-in decision.
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger

from mewbo_graph.plugins.wiki._ctx import (
    ProgressReporter,
    emit_log,
    emit_phase,
    emit_scope_preview,
)
from mewbo_graph.plugins.wiki._jobless import JoblessIndexRunner, JoblessPhaseError
from mewbo_graph.plugins.wiki.finalize import _graph_is_populated, _supersede_stale_jobs
from mewbo_graph.wiki.refresh import RefreshOrchestrator

if TYPE_CHECKING:
    from mewbo_graph.wiki.refresh import RefreshReport
    from mewbo_graph.wiki.types import IndexFingerprint, ScopePreview

logging = get_logger(name="mewbo_graph.plugins.wiki.scoped_refresh")


class ScopedRefreshRunner(JoblessIndexRunner):
    """clone→scan→(scoped graph delta)→(act)→finalize refresh.

    Construct with the durable handles a run needs (``ctx`` + the ``submission``
    contract) — the same two-argument shape as ``GraphOnlyIndexer``, so the API
    drives both through one daemon-thread launcher — then call :meth:`run`.
    """

    _RUN_NOUN = "scoped refresh"
    _CLONE_NOTE = "scoped refresh"
    _FAILURE_NOUN = "scoped refresh"

    # The two values ``act_plan["stage"]`` takes. "act" means stage 1 finished
    # and the page work-list below it is owed; "finalize" means stage 2 is done
    # (or had nothing to do) and only publication remains. A resume reads this
    # to decide how much of the pipeline to re-drive — the record is written
    # BEFORE stage 2 starts precisely so a job that dies inside it still says so.
    _STAGE_ACT = "act"
    _STAGE_FINALIZE = "finalize"

    # The delta pass's own report, kept for the act phase that follows it. Not
    # persisted: the pages it names ARE persisted (the act sidecar), and the
    # rest of the report is already published as the scope-preview event.
    _report: RefreshReport | None = None

    def _phases(self) -> tuple[Callable[[], None], ...]:
        """Clone → scan → graph (the delta pass) → pages (the act phase) → finalize."""
        return (self._clone, self._scan, self._refresh_scope, self._act, self._finalize)

    # ── phases ──────────────────────────────────────────────────────────

    def _refresh_scope(self) -> None:
        """Run every deterministic refresh stage over the changed scope.

        Reported under the ``graph`` phase because that is what it is — a graph
        re-index narrowed to a diff — and because a new phase name would have to
        be added to ``PHASE_SEQUENCE``, renumbering the ordinal every stored
        progress comparison is derived from.

        The file list comes from the scan phase, never a second walk: the
        checkout is live on disk, so re-walking it can legitimately disagree
        with the list the scan events already told every reader this run is
        working on.
        """
        ctx = self._ctx
        if not ctx.clone_dir.exists():
            raise JoblessPhaseError("internal", f"clone dir missing: {ctx.clone_dir}")
        emit_phase(ctx, "graph")

        job = ctx.store.get_job(ctx.job_id)
        commit = (job.commit_sha if job else None) or ""
        if not commit:
            # ``GraphDeltaIndexer.apply`` requires a real commit for a reason
            # worth repeating at its caller: an artifact stamped with an explicit
            # ``None`` is spared by the supersede sweep AND missed by the
            # isolation backfill, so it can never be found again. Refusing costs
            # this refresh; proceeding would mint permanently un-reapable rows.
            raise JoblessPhaseError(
                "internal",
                "cannot refresh a scope with no commit: the clone resolved no "
                "HEAD, and every artifact this pass writes must be attributable "
                "to one or nothing can ever reap it",
            )

        # ``on_report`` lands the resolver's own account of which leg ran in the
        # JOB timeline rather than only the module logger. Without it the
        # report still exists and nobody reading the job can see it, which is
        # the exact silence that let a total cross-file resolution outage run
        # unnoticed on the full-index path.
        report = RefreshOrchestrator.from_store(
            ctx.store, slug=ctx.slug, on_report=lambda message: emit_log(ctx, message)
        ).refresh(
            ctx.slug,
            ctx.clone_dir,
            self._scanned_paths(),
            commit=commit,
        )
        self._report = report
        preview = report.scope_preview()
        emit_scope_preview(ctx, preview)
        emit_log(
            ctx,
            self._scope_line(
                preview, noop=report.is_noop, act=self._act_launcher() is not None
            ),
        )

    def _act(self) -> None:
        """Regenerate the pages the delta pass flagged, before finalize publishes.

        Stage 2 — the ONE part of a scoped refresh that costs an LLM. It runs
        under the EXISTING ``pages`` phase name, which the scoped pipeline had
        simply never used: ``PHASE_SEQUENCE`` already places it between ``graph``
        and ``finalize``, so no vocabulary changes and no stored ordinal moves.

        Ordering is load-bearing rather than tidy: :meth:`_finalize` writes
        ``status="complete"`` and the ``complete`` event, and the SSE stream
        CLOSES on that event — so a page written after it would be invisible to
        every reader watching the run.

        The pages come from ``RefreshReport.pages_to_regenerate`` (the planner's
        edit/regenerate verdicts) and NOT from ``docs.new_pages``: authoring a
        page for a symbol nobody has documented is a different, opt-in product
        decision, and one live run proposed 24 of them.

        The launcher seam is ``start`` THEN ``wait``, and both halves are
        mandatory here. ``start`` mints an async session and returns its id at
        once, so a caller that treated it as the whole phase would fall straight
        into :meth:`_finalize` and publish ``complete`` — closing the SSE stream
        — while the session was still rewriting pages. Nothing would raise; the
        run would simply announce it had finished work that was still running.
        """
        ctx = self._ctx
        report = self._report
        job = ctx.store.get_job(ctx.job_id)
        commit = (job.commit_sha if job else None) or ""
        page_ids = self._work_list(report)

        # Written unconditionally — including for an empty work-list and for a
        # deployment with no act tier at all. The record's job is to say that
        # stage 1 FINISHED, and that is true either way; withholding it on the
        # empty case would make a resume re-drive clone + scan + the whole delta
        # pass to rediscover that there was nothing to do.
        self._save_act(self._STAGE_ACT, page_ids, commit)

        launcher = self._act_launcher()
        if not page_ids or launcher is None:
            if page_ids:
                self._warn_not_regenerated(page_ids, "no page-regeneration tier is")
            self._save_act(self._STAGE_FINALIZE, page_ids, commit)
            return

        # BEFORE the session starts, not after: the page-writing tool it drives
        # reads this as its progress denominator (a scoped job commits no plan of
        # its own, so every page written would render as a fraction of nothing),
        # and a plan that lands mid-session is a plan the first page missed.
        # Titles come from the pages' own doc notes so the plan reads as pages
        # rather than as ids; the read is bounded by the narrowed list, never by
        # the project's page count.
        ctx.store.save_job_plan(ctx.job_id, [
            {"id": pid, "title": self._page_title(pid)} for pid in page_ids
        ])

        session_id = self._start_act(launcher, page_ids)
        if session_id is None:
            # A REGISTERED launcher that could not mint a session — the api has
            # no start body for this job. Same honest outcome as no tier at all,
            # and deliberately not a failure: nothing was started, so there is
            # nothing half-done for a resume to pick up.
            self._warn_not_regenerated(page_ids, "no act session could be started, so")
            self._save_act(self._STAGE_FINALIZE, page_ids, commit)
            return

        # Re-stamped with the session id BEFORE the blocking wait, which is the
        # whole reason the seam is split in two. A job that dies inside the wait
        # then resumes knowing not just that stage 2 was owed but WHICH session
        # was already running — without it a resume starts a second session that
        # rewrites the same pages concurrently with the first.
        self._save_act(self._STAGE_ACT, page_ids, commit, session_id=session_id)

        # Only now: a phase must be stamped by work that has actually started.
        emit_phase(ctx, "pages")
        emit_log(
            ctx,
            f"Regenerating {len(page_ids)} page(s) the refresh scored stale: "
            + ", ".join(page_ids[:8])
            + ("…" if len(page_ids) > 8 else "")
        )

        outcome = self._wait_act(launcher, session_id)
        if outcome is None or not outcome.ok:
            # Deliberately terminal, and deliberately NOT collapsed to one
            # message: a timeout and a refusal are different operational facts,
            # and the job log is the only place either is ever read. The sidecar
            # still says ``stage="act"`` with the session id on it, so a resume
            # picks the run up at stage 2 instead of re-doing stage 1 — which is
            # true only because this does not swallow the failure and finalize.
            detail = (
                "the launcher was withdrawn mid-run"
                if outcome is None
                else f"{outcome.status}" + (f": {outcome.error}" if outcome.error else "")
            )
            raise JoblessPhaseError(
                "internal", f"page regeneration did not complete — {detail}"
            )

        self._assert_pages_written(page_ids, session_id)
        self._save_act(
            self._STAGE_FINALIZE, page_ids, commit, session_id=session_id
        )

    def _finalize(self) -> None:
        """Advance the project to the refreshed commit; leave everything else alone.

        Deliberately NOT a rebuild of the ``Project`` record the way
        ``wiki_finalize`` and ``GraphOnlyIndexer`` do one. Those two regenerate
        the whole wiki, so rebuilding its display snapshot wholesale is correct;
        a scoped refresh regenerated no pages, so ``pages``, ``desc``,
        ``landing_page_id`` and ``fingerprint`` all still describe reality and
        must survive untouched. The fingerprint is equal by construction —
        ``RefreshDecision.decide`` forces the FULL path on any mismatch, so a
        scoped run can only ever be re-stamping the value already stored.
        """
        ctx = self._ctx
        emit_phase(ctx, "finalize")

        # Completion correctness, same gate the other two finalizers apply: a
        # refresh that leaves no graph behind has not refreshed anything.
        if not _graph_is_populated(ctx):
            raise JoblessPhaseError(
                "validation",
                "cannot finalize: knowledge graph is empty — the scoped refresh "
                "left no nodes behind",
            )

        project = ctx.store.get_project(ctx.slug)
        if project is None:
            # Unreachable through the decision seam (``no_prior_index`` sends a
            # projectless slug down the full path), so reaching it means a
            # caller bypassed the decision — fail loudly rather than mint a
            # half-populated Project from a submission this pass never read.
            raise JoblessPhaseError(
                "validation",
                "cannot finalize a scoped refresh: no project record exists for "
                f"{ctx.slug} — a scoped path requires a prior full index",
            )

        job = ctx.store.get_job(ctx.job_id)
        commit_sha = (job.commit_sha if job else None) or project.commit_sha
        indexed_at = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Carry the untouched majority forward onto the new commit BEFORE the
        # project row advances, and the order is the whole point. ``live_scope``
        # is ``at(project.commit_sha)``, so between advancing the project and
        # re-stamping its artifacts there is a window where every reader — the
        # graph view, the retriever, the agent graph tools — resolves a
        # generation that holds only the handful of files this refresh happened
        # to re-parse. Not an error, not a deletion: the rest of the codebase
        # simply stops being visible. Re-stamping first means that window never
        # exists. A prior commit is required to reach the scoped path at all
        # (``no_prior_index`` sends a projectless slug down the full one), so the
        # guard is for a project row that legitimately carries no commit yet.
        if project.commit_sha and commit_sha and project.commit_sha != commit_sha:
            moved = ctx.store.restamp_graph_artifacts(
                ctx.slug, from_commit=project.commit_sha, to_commit=commit_sha
            )
            emit_log(
                ctx,
                f"Carried {sum(moved.values())} untouched artifact(s) forward to "
                f"{commit_sha[:7]} — they describe the new commit exactly as well "
                "as the old one, and a reader scoped to it must still find them",
            )

        ctx.store.create_project(project.model_copy(update={
            "commit_sha": commit_sha,
            "commit_short": commit_sha[:7] if commit_sha else None,
            "indexed_at": indexed_at,
        }))

        # ── DO NOT add ``supersede_graph_artifacts`` here. ───────────────
        # That reaper deletes every artifact for the slug NOT stamped with the
        # kept commit. A FULL index re-stamps the entire graph with the new
        # commit, so calling it there reaps exactly the previous commit's rows —
        # which is why ``wiki_finalize`` and ``GraphOnlyIndexer`` both do.
        #
        # A SCOPED refresh re-stamps ONLY the files it re-parsed
        # (``GraphDeltaIndexer.apply`` passes ``commit=`` for that scope alone);
        # every untouched file's nodes, edges and vectors keep the commit of the
        # index that built them. Reaping to the new commit here would therefore
        # delete the entire untouched majority of the graph — the precise
        # data-loss shape the incremental path exists to avoid, reintroduced by
        # its own finalizer. ``tests/wiki/test_scoped_refresh.py`` fails if this
        # call comes back.
        #
        # The re-stamp above is what makes reaping unnecessary rather than merely
        # unsafe: once the previous generation has been carried forward, ONE
        # generation describes the whole slug, so there is nothing left at the
        # old commit for a sweep to find. Deleting and re-stamping are two
        # answers to the same question, and only one of them keeps the artifacts
        # a refresh deliberately did not rebuild.
        #
        # Generations OLDER than the one carried forward stay exactly where they
        # are. They describe code that is genuinely gone, so re-stamping them
        # would render deleted symbols as live — a worse bug than the one this
        # seam exists to prevent — and reaping them belongs to the next FULL
        # rebuild, which supersedes on a generation it actually built.

        ctx.store.update_job(
            ctx.job_id,
            status="complete",
            landing_page_id=project.landing_page_id,
            current_file=None,
        )
        # Sibling JOB records, not graph artifacts — a different reaper with a
        # different subject. This one retires the slug's other jobs and their
        # dead checkouts, which a scoped refresh owes just as much as a full one.
        _supersede_stale_jobs(ctx)

        emit_log(
            ctx,
            "Scoped refresh complete: the wiki now describes "
            f"{(commit_sha or '')[:7] or 'the current checkout'}",
        )
        ctx.store.append_job_event(ctx.job_id, {
            "type": "complete",
            "landingPageId": project.landing_page_id,
            "pageCount": project.pages,
        })

        # A scoped refresh's declared plan carries the FULL seven-phase
        # pipeline (the reporter declares it once, uniformly, for every run
        # shape), but this runner's own work never touches ``graph``/``enrich``/
        # ``plan`` through the ledger — the delta pass runs under the ``graph``
        # PHASE NAME but reports no ``graph.*`` steps, and ``enrich``/``plan``
        # never apply to an incremental pass at all. Left alone those groups
        # stay ``pending`` on every completed scoped refresh forever. Settling
        # here is what makes the group genuinely inapplicable read as
        # ``skipped`` rather than as unfinished work.
        # ``settle`` emits its own timeline warning for a step left unreported
        # beside a completed sibling, so this call site does not repeat it.
        ProgressReporter(ctx).settle()

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _act_launcher() -> Any | None:
        """The registered page-regeneration launcher, or ``None`` when absent.

        Imported LAZILY and behind two independent guards, the same shape the
        search launcher's registration uses: the module may not exist in a lean
        install, and it may exist with no implementation registered (the api is
        what registers one, and a graph-only deployment never does). Either way
        the answer is "no act tier", the refresh finalizes normally, and the
        timeline says so rather than the run pretending pages were rewritten.
        """
        try:
            from mewbo_graph.plugins.wiki.act_launcher import (  # noqa: PLC0415
                ActLauncher,
            )
        except ImportError:
            return None
        available = getattr(ActLauncher, "available", None)
        if callable(available) and not available():
            return None
        return ActLauncher

    def _work_list(self, report: RefreshReport | None) -> list[str]:
        """The pages stage 2 owes — recovered from the sidecar when one is owed.

        A RESUMED scoped refresh cannot re-derive its own work-list, and the
        reason is durable rather than transient. Stage 1's
        ``GraphDeltaIndexer.apply`` ends by advancing the file manifest for the
        scope it touched, so the resumed run's ``ChangeDetector.detect`` diffs
        the tree against an ALREADY-ADVANCED manifest, finds nothing, and
        ``RefreshOrchestrator.refresh`` short-circuits to
        ``RefreshReport.noop`` — whose ``pages_to_regenerate`` is empty. Trusting
        the report alone would finalize a run that rewrote none of the pages
        stage 1 scored stale.

        **And they would never be recovered.** The next refresh diffs against
        that same advanced manifest and also finds nothing, so the pages stay
        stale until some future commit happens to touch the same files again —
        silently, and rendering as a successful refresh throughout. Clicking
        Refresh cannot fix it.

        The sidecar already holds the list (it is written before stage 2 starts,
        unconditionally); nothing read it back until now. A record still reading
        ``stage="act"`` means an earlier attempt started and did not finish.

        The two lists are UNIONED rather than one replacing the other. Recovery
        first, because it is the authoritative record of what was owed; then any
        page THIS pass scored that the earlier one did not, since a resumed run
        whose manifest is only partly advanced can legitimately flag something
        new — and a page dropped here is dropped permanently, by the same
        argument as above, whereas a page regenerated twice merely costs a
        rewrite. Cheap and wrong in one direction, expensive and right in the
        other.
        """
        scored = list(report.pages_to_regenerate) if report is not None else []
        try:
            prior = self._ctx.store.get_act_plan(self._ctx.job_id)
        except Exception:  # pragma: no cover — best-effort, degrades to today
            prior = None
        if not prior or prior.get("stage") != self._STAGE_ACT:
            return scored

        owed = [str(p) for p in (prior.get("page_ids") or [])]
        if not owed:
            return scored
        recovered = owed + [pid for pid in scored if pid not in owed]
        emit_log(
            self._ctx,
            f"Resuming an unfinished act phase: {len(owed)} page(s) were still "
            "owed from the previous attempt. The delta pass already advanced "
            "this project's file manifest, so this run's own diff cannot see "
            "them — they come from the job's own record instead",
        )
        return recovered

    def _start_act(self, launcher: Any, page_ids: list[str]) -> str | None:
        """Mint the act session; a raise becomes a terminal phase failure.

        The submission travels with the request because the session the api
        mints needs the operator's own choices — model, depth, language — and
        this runner is the only thing holding them.
        """
        try:
            return launcher.start(
                job_id=self._ctx.job_id,
                slug=self._ctx.slug,
                page_ids=page_ids,
                submission=self._submission,
            )
        except Exception as exc:  # noqa: BLE001 — becomes a terminal job failure
            raise JoblessPhaseError(
                "internal", f"could not start page regeneration: {exc}"
            ) from exc

    def _wait_act(self, launcher: Any, session_id: str) -> Any | None:
        """Block until the act session settles; a raise is a terminal failure.

        No timeout is passed. The deadline is the concrete launcher's own
        (`DEFAULT_ACT_TIMEOUT_S` where it is registered), and naming a second
        number here would be one bound in two places — the shorter one silently
        winning and reading as a timeout the operator never configured.
        """
        try:
            return launcher.wait(session_id)
        except Exception as exc:  # noqa: BLE001 — becomes a terminal job failure
            raise JoblessPhaseError(
                "internal", f"page regeneration failed: {exc}"
            ) from exc

    def _assert_pages_written(self, page_ids: list[str], session_id: str) -> None:
        """Refuse to finalize a "successful" act phase that wrote nothing.

        A settled session says the agent STOPPED, never that it did the work.
        Nothing gates the act agent on actually calling ``wiki_submit_page`` —
        QA has ``required_terminal=True`` on ``wiki_emit_answer`` and the page
        writer has no equivalent — so a model that narrates its submissions
        instead of making them ends ``completed``, and every layer above reports
        a refresh that regenerated zero pages as a success. This runner is the
        only place the honest comparison exists, because it is the only thing
        holding both the work-list and the claim set.

        That failure mode is REASONED FROM THE MISSING GATE, not observed in a
        run — recorded so a later reader does not mistake this for the fix to a
        specific incident. What is certain is the asymmetry: nothing else can
        tell the difference, so the cost of being wrong here is one clear error
        on a run that produced nothing anyway.

        The two outcomes are deliberately NOT symmetrical:

        - **Nothing written** is terminal. The tier claimed success and produced
          nothing, so finalizing would publish a lie, and the sidecar staying at
          ``stage="act"`` is what lets a resume retry stage 2 alone.
        - **Some written** is not. Those pages genuinely improved and their doc
          notes are re-stamped, so the work is real and durable — but it has to
          be LOUD and name the pages it did not reach, because a silent partial
          success is how a staleness signal decays into noise.

        The claim set (``get_job_page_ids``) is the same atomic size-of-set
        record ``get_job_submitted_count`` counts, so this survives a resume for
        the same reason that counter does — and unlike the bare count it can
        name WHICH pages are still owed. It is intersected with the work-list
        rather than trusted wholesale: a page written that nobody asked for is
        not evidence that a page that WAS asked for got written.
        """
        ctx = self._ctx
        try:
            claimed = ctx.store.get_job_page_ids(ctx.slug, ctx.job_id)
        except Exception as exc:  # noqa: BLE001 — becomes a terminal job failure
            # Fails CLOSED, the same discipline ``ResumePlan.build`` applies to
            # its counts: "nothing was written" and "I could not tell" are
            # different answers, and only the first may finalize a refresh.
            raise JoblessPhaseError(
                "internal", f"could not confirm which pages were regenerated: {exc}"
            ) from exc

        done = [pid for pid in page_ids if pid in claimed]
        if not done:
            raise JoblessPhaseError(
                "validation",
                f"the act session ({session_id}) ended cleanly but submitted no "
                f"page: {len(page_ids)} page(s) were scored stale and every one "
                "of them still describes the previous commit",
            )

        missing = [pid for pid in page_ids if pid not in claimed]
        if missing:
            emit_log(
                ctx,
                f"Regenerated {len(done)} of {len(page_ids)} scored page(s). "
                f"{len(missing)} still describe(s) the previous commit and was "
                "NOT rewritten: "
                + ", ".join(missing[:8])
                + ("…" if len(missing) > 8 else ""),
                level="warn",
            )
            return
        emit_log(ctx, f"Regenerated all {len(page_ids)} scored page(s)")

    def _warn_not_regenerated(self, page_ids: list[str], because: str) -> None:
        """Say that scored pages were left alone, and why (timeline, not a log)."""
        emit_log(
            self._ctx,
            f"{len(page_ids)} page(s) need regenerating, but {because} available "
            "on this deployment — they stay as they were and the scope preview's "
            "counts stand",
            level="warn",
        )

    def _save_act(
        self,
        stage: str,
        page_ids: list[str],
        commit: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Write the act sidecar: which stage the job is in, and its work-list.

        The work-list is recorded alongside the stage rather than cleared when
        stage 2 ends: a finished act phase is exactly when a reader most wants
        to know WHICH pages it rewrote, and an empty list would be
        indistinguishable from a refresh that flagged nothing.

        ``session_id`` is always present as a key so a reader never has to
        distinguish "no session" from "older record with no such field".
        """
        self._ctx.store.save_act_plan(self._ctx.job_id, {
            "stage": stage,
            "page_ids": page_ids,
            "commit_sha": commit,
            "session_id": session_id,
        })

    def _page_title(self, page_id: str) -> str:
        """The page's stored title, falling back to its id (best-effort)."""
        try:
            note = self._ctx.store.get_doc_note(self._ctx.slug, page_id)
        except Exception:  # pragma: no cover — a title is cosmetic
            note = None
        return (note.title if note else None) or page_id

    @staticmethod
    def _scope_line(preview: ScopePreview, *, noop: bool, act: bool = False) -> str:
        """One timeline line naming what the pass re-did and what it skipped.

        The skipped side is the point of the feature, so it is stated rather
        than left to be inferred from a small "did" number — a reader comparing
        this against a full index's log has no other way to see that the
        untouched majority of the graph was deliberately left in place.

        The closing clause turns on whether an act tier is registered, because
        it is a promise about what happens NEXT: saying "page generation is not
        part of this pass" on a deployment that is about to regenerate them
        would be read as a claim that the old pages stand.
        """
        if noop:
            return (
                "Scoped refresh: no file changed since the last index — graph, "
                "memory and pages all left exactly as they were"
            )
        reparsed = preview.files_added + preview.files_modified
        return (
            f"Scoped refresh: re-parsed {reparsed} changed file(s), retracted "
            f"{preview.files_deleted} deleted, and left every other file's graph "
            f"in place ({preview.early_cutoff_files} re-parsed file(s) came back "
            f"identical and cost nothing downstream). {preview.affected_entities} "
            f"entity/entities affected; memory {preview.memory_kept} kept / "
            f"{preview.memory_invalidated} invalidated / "
            f"{preview.memory_revalidated} re-validated in "
            f"{preview.llm_calls} LLM call(s). Pages scored "
            f"{preview.pages_keep} keep / {preview.pages_edit} edit / "
            f"{preview.pages_regenerate} regenerate and {preview.new_pages} "
            "proposed — "
            + (
                "the scored pages are regenerated next; the proposed ones are not"
                if act
                else "page generation is not part of this pass"
            )
        )


def current_index_fingerprint(store: Any, slug: str) -> IndexFingerprint:
    """What an index started right now would be built with.

    The PREDICTION side of the fingerprint comparison, and its asymmetry with
    the stamped side is deliberate rather than an inconsistency to reconcile.
    ``build_graph_core`` records what a run ACTUALLY did — ``embedding_model``
    stays ``None`` there unless a vector was really persisted, because a
    fingerprint of a finished index must not claim a capability the artifacts
    do not have. Here nothing has run yet, so the honest answer is what the
    current configuration WOULD use. The same field therefore means "what
    happened" on the stored side and "what would happen" on this one, and
    comparing them is exactly the question a refresh needs answered.

    ``embedding_model`` is ``None`` when embedding is switched off, which is
    what makes the comparison catch the case worth catching: an index built
    WITH vectors read against a project that would now build none is genuinely
    stale, and collapsing that to a match would leave half the store
    unsearchable with nothing to show for it. It resolves *this slug*'s model
    from its settings (or the deployment default) without an embedding call or
    network round-trip, which keeps this request-path probe cheap.

    ``O(one record)``: one slug-keyed settings read, config reads, one installed
    package metadata lookup, and a ``shutil.which`` per resolver binary. Reuses
    ``build_graph``'s probes rather than re-deriving them, so both comparison
    sides agree on every non-project input.
    """
    from mewbo_graph.plugins.wiki.build_graph import (  # noqa: PLC0415
        _embeddings_enabled,
        _resolver_available,
        _tree_sitter_pack_version,
    )
    from mewbo_graph.wiki.types import CodeGraph, IndexFingerprint  # noqa: PLC0415

    embedding_model: str | None = None
    if _embeddings_enabled():
        from mewbo_graph.wiki.embedder import make_embedder_for  # noqa: PLC0415

        # Constructing the Embedder normalises the selected model name (the proxy
        # prefix rule lives on that class), so the comparison exactly matches what
        # the write path stamps. It makes no network request.
        embedder = make_embedder_for(store, slug)
        embedding_model = embedder.model if embedder is not None else None

    return IndexFingerprint(
        embedding_model=embedding_model,
        # The literal default on the model, never a hardcoded "1" here — a
        # schema bump must move both sides of the comparison at once.
        graph_schema_version=CodeGraph.model_fields["schema_version"].default,
        grammar_pack_version=_tree_sitter_pack_version(),
        resolver_available=_resolver_available(),
    )


__all__ = ["ScopedRefreshRunner", "current_index_fingerprint"]
