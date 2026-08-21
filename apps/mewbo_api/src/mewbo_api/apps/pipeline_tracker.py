"""The :class:`PipelineRun` ledger seam — open at trigger fire, close at run end.

Opens the ledger when a fired trigger re-engages an app maintainer and closes it
at the maintainer run's end.

The ledger is the app sub-product's provenance chain, freshness signal,
``/system`` backing, and repair-loop input all at once — but it only becomes
LIVE once something OPENS a run when a pipeline fires and CLOSES it when the
maintainer run ends. This atomic class is that seam, wired at the two existing
seams ``backend.py`` already owns:

* **Open** — the trigger-deliver path (``_trigger_deliver``). A fired trigger
  re-engaging a maintainer session resolves to the pipeline whose
  ``PipelineSpec.trigger_ref`` matches the firing ``trigger_id`` and opens a
  ``running`` :class:`PipelineRun`. The ``app_data`` write hook then increments
  its ``docs_written`` (workstream B), so freshness/provenance/system counts go
  live the moment this lands.
* **Close** — the ``HookManager.on_session_end`` hook (chosen over the
  deliver-closure completion path because ``start_async`` returns a ``run_id``
  IMMEDIATELY and never observes run completion; the session-end hook fires when
  the run actually ends, carrying its ``error`` — the outcome we classify on).

On a ``failed`` close it hands the app + error to the lifecycle's
``handle_pipeline_failure`` (policy dispatch — repair/pause/notify).

Collaborators are DI'd FIELDS (atomic-class rule): the run + app stores, a
``failure_handler`` (the lifecycle), and ``now_fn``. NOW is always a method arg
downstream (``PipelineRun.open``/``close``); nothing here reads the wall clock in
a behavioral path. Tests inject fakes + a fixed NOW.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Protocol

from mewbo_core.common import get_logger

from .models import PipelineIssue, PipelineRun
from .pipeline_runner import PipelineExecutionError
from .store import new_run_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from .lifecycle import AppRunStarter
    from .models import AppSpec, PipelineResult, PipelineSpec
    from .pipeline_runner import AppPipelineRunner
    from .store import AppStoreBase, PipelineRunStoreBase

logging = get_logger(name="api.apps.pipeline_tracker")

# The semantic reasons a manual fire is refused; the REST route maps each to an
# HTTP status (see ``AppsRoutesController._FIRE_REFUSAL_STATUS``). Kept as domain
# vocabulary here — the tracker never names an HTTP code (KISS: one owner per
# concern).
FireRefusal = Literal[
    "not_configured",
    "not_live",
    "no_maintainer",
    "already_running",
    "cooldown",
    "code_failed",
    "wake_refused",
]


@dataclass(frozen=True)
class PipelineFireResult:
    """Outcome of ONE on-demand pipeline fire — an in-process value, not a wire model.

    Crosses no trust boundary (it is produced and consumed inside the process at
    the fire seam), so it is a frozen dataclass, not a Pydantic model (the house
    rule: hot runtime bookkeeping stays a plain dataclass). The REST route maps it
    to HTTP; the go-live/re-arm seed paths read only ``ok``/``message``.

    * ``mode`` — which tier fired (``"code"`` ran the engine synchronously;
      ``"agentic"`` opened a ledger row + woke the maintainer).
    * ``ok`` — the fire succeeded (a code run produced a ``result``; an agentic
      wake ``landed``).
    * ``refusal``/``message`` — why a fire did NOT run (``ok`` is ``False``); the
      route maps ``refusal`` to a status and surfaces ``message``.
    * ``retry_after_seconds`` — set ONLY for a ``"cooldown"`` refusal.
    * ``result`` — the code run's :class:`PipelineResult` (success only).
    * ``landed`` — the agentic wake's honest signal (``"started"``/``"steered"``/
      ``"refused"``) from :class:`AppRunStarter`.
    """

    mode: Literal["code", "agentic"]
    ok: bool = False
    refusal: FireRefusal | None = None
    message: str | None = None
    retry_after_seconds: int | None = None
    result: PipelineResult | None = None
    landed: str | None = None

# The maintainer's data tool — every pipeline needs it to write its results, so a
# non-empty per-pipeline allowlist is unioned with it (least privilege).
# Kept as a local constant to avoid importing the heavier plugin module; it must
# match ``mewbo_api.apps.plugin.app_data.APP_DATA_TOOL_ID``.
_APP_DATA_TOOL_ID = "app_data"


class PipelineFailureHandler(Protocol):
    """The lifecycle's failure-policy dispatch — the close seam's only outward call."""

    def handle_pipeline_failure(self, app: AppSpec, issue: PipelineIssue) -> None:
        """React to a pipeline needing attention per the ``on_pipeline_failure`` policy."""
        ...


class AppPipelineRunTracker:
    """Opens/closes the pipeline-run ledger around a maintainer run.

    Atomic feature class: ``run_store`` / ``app_store`` / ``failure_handler`` /
    ``pipeline_runner`` / ``run_starter`` / ``now_fn`` are its state;
    :meth:`open_run` / :meth:`close_runs` / :meth:`pipeline_scope` /
    :meth:`fire_pipeline` are its behavior.
    """

    # Anti-spam cooldown (seconds) for a manual AGENTIC fire only — a code fire is
    # already TTL/source-cache protected, so it never cools down. A fresh
    # :meth:`fire_pipeline` is refused when this pipeline's most-recent run started
    # within this window (see ``PipelineRun.cooldown_remaining``).
    FIRE_COOLDOWN_SECONDS = 300

    # How many of a pipeline's most recent ledger rows the integrity check reads to
    # establish "this pipeline has written that collection before". A BOUND on the scan,
    # not a tuning knob: the baseline only needs enough history to distinguish a
    # regression from a collection that was never populated, and an unbounded
    # list_runs would grow with the app's whole lifetime on every scheduled close.
    # Deliberately NOT a time window — the question is ordinal ("did earlier runs of
    # this pipeline write it"), so a slow hourly pipeline and a fast one both get a
    # baseline of the same shape rather than one that empties on a quiet week.
    INTEGRITY_HISTORY_LIMIT = 20

    def __init__(
        self,
        *,
        run_store: PipelineRunStoreBase,
        app_store: AppStoreBase,
        failure_handler: PipelineFailureHandler | None = None,
        pipeline_runner: AppPipelineRunner | None = None,
        run_starter: AppRunStarter | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Capture the injected collaborators; default ``now_fn`` is UTC now.

        *failure_handler* (the :class:`AppLifecycle`) dispatches the
        ``on_pipeline_failure`` policy on a failed close; ``None`` skips that
        dispatch (the ledger still closes honestly). *pipeline_runner*
        executes a ``mode="code"`` pipeline at the fire seam WITHOUT
        re-engaging the maintainer LLM session; ``None`` means every fire takes
        the agentic re-engage path (a deployment without code execution wired).
        *run_starter* wakes the maintainer for an on-demand AGENTIC
        :meth:`fire_pipeline`; ``None`` degrades an agentic fire to a clean
        "not configured" refusal (the run-starter isn't wired yet).
        """
        self.run_store = run_store
        self.app_store = app_store
        self.failure_handler = failure_handler
        self.pipeline_runner = pipeline_runner
        self.run_starter = run_starter
        self.now_fn = now_fn or self._utcnow
        # Serializes the get_open CHECK + row OPEN across the two seams that open a
        # persistent ``running`` row (:meth:`open_run` for a scheduled fire and
        # :meth:`_fire_agentic` for a manual one). Without it two near-simultaneous
        # opens for the same (app, pipeline) both pass the dedup guard and strand a
        # second running row that ``close_runs`` never settles. Prod is gunicorn with a
        # SINGLE worker, so one in-process lock is sufficient serialization — the
        # guarantee comes from there being one process, not from how many threads it
        # serves; a coarse lock is the smallest correct scope at fire/​fire
        # frequency (an open is rare — a manual fire or a trigger tick).
        self._open_lock = threading.Lock()
        # Semantic verification has no failed ledger row: the execution result was
        # real and stays succeeded. Keep its edge state separate from provenance.
        self._verifier_failures: dict[tuple[str, str], list[datetime]] = {}
        self._verifier_failures_lock = threading.Lock()

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    # -- open (trigger-fire seam) ------------------------------------------

    def open_run(self, session_id: str, trigger_id: str | None) -> None:
        """Open a ``running`` ledger entry when a fired trigger is an app pipeline.

        No-op for a fire that isn't an app pipeline trigger (the common case — most
        triggers aren't apps), or when a run is already open for that pipeline
        (one ``running`` entry per pipeline at a time — never stack a second).
        """
        match = self._resolve_pipeline(trigger_id)
        if match is None:
            return
        app, pipeline = match
        # Check + open under the shared lock so this can't race a manual fire (or a
        # second delivery of the same trigger) into two running rows.
        with self._open_lock:
            if self.run_store.get_open(app.app_id, pipeline.name) is not None:
                return
            run = PipelineRun.open(
                run_key=new_run_key(),
                app_id=app.app_id,
                pipeline_name=pipeline.name,
                now=self.now_fn(),
                trigger_id=trigger_id,
                session_run_id=session_id,
                cursor_before=dict(pipeline.cursor),
            )
            self.run_store.open_run(run)

    # -- code-pipeline fire seam ---------------------------------------

    def run_code_pipeline_fire(
        self, trigger_id: str | None, *, now: datetime | None = None
    ) -> bool:
        """Run a fired ``mode="code"`` pipeline's ENGINE synchronously; report if it handled it.

        The trigger-deliver seam calls this FIRST. When the firing trigger belongs
        to a ``mode="code"`` pipeline (and a runner is wired), it executes the
        engine, writes ONE closed ``{kind:"scheduled"}`` ledger row with the
        result, and returns ``True`` — the maintainer LLM session is NEVER
        re-engaged (that is the whole point of a materialized pipeline: no LLM call
        for deterministic work). It returns ``False`` for an agentic pipeline, a
        non-app fire, or an unwired runner — the caller then proceeds with the
        normal agentic re-engage + :meth:`open_run` path unchanged.
        """
        if self.pipeline_runner is None:
            return False
        match = self._resolve_pipeline(trigger_id)
        if match is None:
            return False
        app, pipeline = match
        if pipeline.mode != "code":
            return False
        self.record_code_run(
            app, pipeline, params={}, kind="scheduled", trigger_id=trigger_id, now=now
        )
        return True

    def record_code_run(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        *,
        params: dict[str, object],
        kind: Literal["scheduled", "on_request"],
        trigger_id: str | None = None,
        now: datetime | None = None,
        dispatch_failure: bool = True,
        require_effect: bool = False,
    ) -> tuple[PipelineRun, PipelineResult | None]:
        """Execute a code pipeline, write ONE closed ``{kind}`` row, return ``(run, result)``.

        The single home for "run a code pipeline + record its provenance", shared by
        two callers:

        * the **scheduled fire seam** (``kind="scheduled"``, ``dispatch_failure=True``)
          — ignores the returned ``result`` and permits a failure-policy dispatch only
          when the pipeline's failure budget reaches its edge;
        * the **on-request REST invoke endpoint** (``kind="on_request"``,
          ``dispatch_failure=False``, ``require_effect=True``) — reads
          ``result.output`` for its response, and explicitly vetoes auto-repair/pause
          for run failures. The endpoint maps the failure to its HTTP status from
          ``run.error`` (``result is None`` ⇒ it failed).

        It NEVER raises — a runner failure closes the row ``failed`` (with the error)
        and returns ``(failed_run, None)``, so a caller reads the outcome off the row
        rather than catching. The persisted row carries ``docs_written`` + ``cache``
        hit/miss + ``params_hash``; ``result`` is the (frozen) success payload or
        ``None`` on failure.

        *require_effect* (default ``False`` — the scheduled-fire behavior is
        UNCHANGED) gates whether a SUCCESSFUL run is durably persisted: when
        ``True``, only a run that had a real effect (``cache="miss"`` AND a
        non-empty ``docs_written``) is saved to the ledger — the anti-spam
        ruling for the on-request REST invoke path, where a cache hit or a
        read-only render must never mint a row (a polling client on a
        ``cache_ttl_seconds=0`` pipeline would otherwise spam the ledger on every
        request). A FAILURE is ALWAYS persisted regardless of this flag — a
        genuine break is real provenance no matter how the pipeline was invoked.
        The run object is still RETURNED either way; only the durable write is
        suppressed.
        """
        runner = self.pipeline_runner
        if runner is None:  # pragma: no cover - callers guard; belt-and-suspenders for mypy
            raise RuntimeError("record_code_run called with no pipeline_runner configured")
        now = now or self.now_fn()
        run = PipelineRun.open(
            run_key=new_run_key(),
            app_id=app.app_id,
            pipeline_name=pipeline.name,
            now=now,
            trigger_id=trigger_id,
            session_run_id=None,
            cursor_before=dict(pipeline.cursor),
            kind=kind,
            params_hash=runner.params_hash(dict(params)),
        )
        try:
            result = runner.execute(app, pipeline, dict(params), now=now)
        except PipelineExecutionError as exc:
            # A failed run is not a run that wrote nothing. ``docs_written`` is
            # normally read off the ``PipelineResult``, which only success ever
            # produces — so a timeout or a mid-run raise used to close the row with
            # an EMPTY count while the documents it had already written sat in the
            # collection, making /system freshness, ``stale`` and
            # ``unwritten_collections`` all describe a run that never happened.
            # The runner carries the partial count out on the error instead.
            for collection, count in exc.docs_written.items():
                run.record_write(collection, count)
            run.close(now=now, status="failed", error=str(exc))
            self.run_store.save(run)  # a failure is never suppressed by require_effect
            if dispatch_failure:
                self._dispatch_failure(
                    app, pipeline, PipelineIssue.run_failed(pipeline.name, str(exc)), now=now
                )
            return run, None
        for collection, count in result.docs_written.items():
            run.record_write(collection, count)
        run.close(now=now, status="succeeded", cache=result.cache)
        had_effect = result.cache == "miss" and (
            bool(result.docs_written) or not pipeline.expects_writes()
        )
        persisted = not require_effect or had_effect
        if persisted:
            self.run_store.save(run)
        if dispatch_failure and pipeline.expects_writes():
            self._dispatch_integrity(app, run)
        if persisted and result.cache == "miss":
            self._start_verifier(
                app, pipeline, result, now=now, dispatch_failure=dispatch_failure
            )
        return run, result

    # -- on-demand fire seam (manual refresh: /fire route, go-live + re-arm seed) --

    def fire_pipeline(
        self, app: AppSpec, pipeline: PipelineSpec, *, now: datetime | None = None
    ) -> PipelineFireResult:
        """Fire ONE pipeline on demand, dispatched off its declared ``mode``.

        The single seam three sites call — the ``POST .../pipelines/<name>/fire``
        route, ``AppLifecycle``'s go-live seed, and its re-arm seed — so an
        explicit refresh (and the first seeded run at go-live) rides EXACTLY the
        same execution path a schedule would (no new engine).

        * ``mode="code"`` runs the engine synchronously via :meth:`record_code_run`
          (``kind="on_request"``, ``dispatch_failure=False``, ``require_effect=False``
          — an explicit fire ALWAYS ledgers, unlike the anti-spam REST invoke; and a
          user-triggered failure never auto-repairs the app). Returns the result, or
          a ``code_failed`` refusal carrying ``run.error``.
        * ``mode="agentic"`` guards (live app, present maintainer, no run already
          open for this pipeline, past the fire cooldown), then OPENS the ledger row
          FIRST (so a fast maintainer run has a row to close) and wakes the
          maintainer with ``pipeline.wake_prompt`` — NEVER the trigger's stale copy.
          If the wake is REFUSED (``start_app_run`` → ``"refused"``) the just-opened
          row is closed ``failed`` (never stranded) and a ``wake_refused`` refusal is
          returned; the wire therefore never carries ``status:"refused"`` on a 202.
          The check + cooldown + open run under the shared lock (see ``_open_lock``).

        NOW is an argument (defaulting to the injected clock); nothing here reads a
        wall clock in a behavioral path.
        """
        now = now or self.now_fn()
        if pipeline.mode == "code":
            return self._fire_code(app, pipeline, now=now)
        return self._fire_agentic(app, pipeline, now=now)

    def _fire_code(
        self, app: AppSpec, pipeline: PipelineSpec, *, now: datetime
    ) -> PipelineFireResult:
        """Run a code pipeline synchronously and ledger it; map (run, result) -> outcome."""
        if self.pipeline_runner is None:
            return PipelineFireResult(
                mode="code", refusal="not_configured",
                message="pipeline execution is not configured on this deployment",
            )
        run, result = self.record_code_run(
            app, pipeline, params={}, kind="on_request",
            dispatch_failure=False, require_effect=False, now=now,
        )
        if result is None:
            return PipelineFireResult(
                mode="code", refusal="code_failed",
                message=run.error or "pipeline execution failed",
            )
        return PipelineFireResult(mode="code", ok=True, result=result)

    def _fire_agentic(
        self, app: AppSpec, pipeline: PipelineSpec, *, now: datetime
    ) -> PipelineFireResult:
        """Guard, open the ledger row, then wake the maintainer with the wake prompt."""
        if self.run_starter is None:
            return PipelineFireResult(
                mode="agentic", refusal="not_configured",
                message="agentic pipeline fire is not configured on this deployment",
            )
        if app.status != "live":
            return PipelineFireResult(
                mode="agentic", refusal="not_live",
                message=f"app is {app.status!r}, not live — cannot fire a pipeline",
            )
        maintainer = app.maintainer_session_id
        if not maintainer:
            return PipelineFireResult(
                mode="agentic", refusal="no_maintainer",
                message="app has no maintainer session — cannot fire a pipeline",
            )
        # The get_open check, the cooldown check, and the row OPEN are one atomic
        # section (under the shared lock) — else a concurrent fire (or a scheduled
        # ``open_run``) both pass the guard and strand a second running row. The row
        # is opened FIRST (before the wake) so a maintainer run that completes before
        # this returns finds an open row for ``close_runs`` to settle (open/close are
        # deliberately different seams — ``start_app_run`` never observes completion).
        with self._open_lock:
            if self.run_store.get_open(app.app_id, pipeline.name) is not None:
                return PipelineFireResult(
                    mode="agentic", refusal="already_running",
                    message=f"pipeline {pipeline.name!r} already has a run in progress",
                )
            remaining = PipelineRun.cooldown_remaining(
                self.run_store.list_runs(app.app_id, pipeline_name=pipeline.name),
                now=now, cooldown_seconds=self.FIRE_COOLDOWN_SECONDS,
            )
            if remaining is not None:
                return PipelineFireResult(
                    mode="agentic", refusal="cooldown", retry_after_seconds=remaining,
                    message=(
                        f"pipeline {pipeline.name!r} was fired recently; "
                        f"retry in {remaining}s"
                    ),
                )
            run = self.open_on_request_run(app, pipeline, now=now)
        # Wake OUTSIDE the lock — ``start_app_run`` can be slow, and the open row
        # already 409s a racing fire, so nothing needs the lock held here.
        landed = self.run_starter.start_app_run(maintainer, pipeline.wake_prompt)
        if landed == "refused":
            # The wake never landed (the run ended between ``is_running`` and the
            # enqueue, the runtime refused the start, or ``start_app_run`` swallowed
            # an exception), so the row we just opened has NO run behind it. Close it
            # ``failed`` now rather than strand it as a false "in progress" — mirrors
            # ``sweep_orphaned_runs``'s wording. (A later, UNRELATED session-end could
            # otherwise mis-settle it.) The manual-fire failure never auto-repairs.
            run.close(now=now, status="failed", error="interrupted: wake refused, no run started")
            self.run_store.save(run)
            return PipelineFireResult(
                mode="agentic", refusal="wake_refused",
                message=(
                    f"could not start a run for pipeline {pipeline.name!r} — "
                    "the maintainer session refused the wake"
                ),
            )
        return PipelineFireResult(mode="agentic", ok=True, landed=landed)

    def open_on_request_run(
        self, app: AppSpec, pipeline: PipelineSpec, *, now: datetime
    ) -> PipelineRun:
        """Open a ``running`` ``kind="on_request"`` ledger row from a RESOLVED pipeline.

        The manual-fire sibling of :meth:`open_run` (which resolves the pipeline
        from a firing ``trigger_id``): here the (app, pipeline) are already known
        and there is no trigger, so ``trigger_id`` is ``None`` and ``kind`` is
        ``"on_request"``. Callers guard the one-open-run-per-pipeline invariant
        BEFORE calling (see :meth:`_fire_agentic`).
        """
        run = PipelineRun.open(
            run_key=new_run_key(),
            app_id=app.app_id,
            pipeline_name=pipeline.name,
            now=now,
            trigger_id=None,
            session_run_id=app.maintainer_session_id,
            cursor_before=dict(pipeline.cursor),
            kind="on_request",
        )
        self.run_store.open_run(run)
        return run

    # -- close (session-end seam) ------------------------------------------

    def close_runs(self, session_id: str, error: str | None = None) -> None:
        """Close every open ledger entry for the app this maintainer session backs.

        Signature matches ``HookManager.on_session_end`` (``session_id, error``) so
        it registers directly. A non-app session (the overwhelming majority of
        session ends) resolves to no app and is a clean no-op. Run outcome →
        ledger status: an ``error`` closes the run ``failed``, else ``succeeded``.

        **Kind-aware failure dispatch:** a failed ``kind="scheduled"`` run may
        dispatch the ``on_pipeline_failure`` policy when its failure budget reaches
        an edge. A failed ``kind="on_request"`` run does not dispatch here, in
        parity with the manual REST-invoke veto. The run is ledgered ``failed``
        either way — provenance is real; only the reaction is vetoed.
        """
        app = self._app_for_session(session_id)
        if app is None:
            return
        now = self.now_fn()
        status: Literal["succeeded", "failed"] = "failed" if error else "succeeded"
        failed_pipeline: str | None = None
        for pipeline in app.pipelines:
            run = self.run_store.get_open(app.app_id, pipeline.name)
            if run is None:
                continue
            run_kind = run.kind
            run.close(now=now, status=status, error=error or None)
            self.run_store.save(run)
            if status == "failed" and run_kind == "scheduled" and failed_pipeline is None:
                failed_pipeline = pipeline.name
            if pipeline.expects_writes():
                if status == "succeeded" and run_kind == "scheduled":
                    # A run can close green and still have stopped doing its job. Same
                    # kind-gating as the failure dispatch above: only an autonomous
                    # schedule reacts, never a manual /fire (kind="on_request").
                    self._dispatch_integrity(app, run)
                if run.wrote_nothing:
                    logging.warning(
                        "pipeline run {} for app {} succeeded but wrote no documents",
                        run.run_key,
                        app.app_id,
                    )
                else:
                    # wrote_nothing already covers the all-empty case (every declared
                    # collection would show up below too); only worth a SEPARATE log
                    # when the run wrote SOMETHING but silently missed one collection.
                    unwritten = run.unwritten_collections([c.name for c in app.collections])
                    if unwritten:
                        logging.warning(
                            "pipeline run {} for app {} succeeded but left declared "
                            "collection(s) {} untouched",
                            run.run_key,
                            app.app_id,
                            unwritten,
                        )
        if failed_pipeline is not None:
            pipeline = next(p for p in app.pipelines if p.name == failed_pipeline)
            self._dispatch_failure(
                app, pipeline, PipelineIssue.run_failed(failed_pipeline, error), now=now
            )

    # -- failure + integrity dispatch ----------------------------------------

    def _dispatch_failure(
        self, app: AppSpec, pipeline: PipelineSpec, issue: PipelineIssue, *, now: datetime
    ) -> None:
        """Dispatch a non-vetoed issue only when the failure budget reaches its edge."""
        if self.failure_handler is None:
            return
        history = self.run_store.list_runs(
            app.app_id, pipeline_name=pipeline.name, limit=self.INTEGRITY_HISTORY_LIMIT
        )
        if PipelineRun.should_dispatch_failure(history, pipeline.failure_budget, now=now):
            self.failure_handler.handle_pipeline_failure(app, issue)

    def _start_verifier(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        result: PipelineResult,
        *,
        now: datetime,
        dispatch_failure: bool,
    ) -> None:
        """Verify a settled result off-thread so semantic checks cannot delay callers."""
        if pipeline.verifier is None or self.pipeline_runner is None:
            return
        threading.Thread(
            target=lambda: self._verify_result(
                app, pipeline, result, now=now, dispatch_failure=dispatch_failure
            ),
            daemon=True,
        ).start()

    def _verify_result(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        result: PipelineResult,
        *,
        now: datetime,
        dispatch_failure: bool,
    ) -> None:
        """Run one bounded verifier and route its failure through the policy dispatcher."""
        runner = self.pipeline_runner
        if runner is None:  # pragma: no cover - guarded by _start_verifier
            return
        try:
            runner.verify(app, pipeline, result, now=now)
        except PipelineExecutionError as exc:
            logging.warning(
                "pipeline verifier failed for app {} pipeline {}: {}",
                app.app_id,
                pipeline.name,
                exc,
            )
            if exc.code != "verifier":
                return
            self._record_verifier_failure(
                app, pipeline, str(exc), now=now, dispatch_failure=dispatch_failure
            )
        except Exception:
            logging.warning(
                "pipeline verifier raised for app {} pipeline {}",
                app.app_id,
                pipeline.name,
                exc_info=True,
            )
        else:
            logging.info(
                "pipeline verifier passed for app {} pipeline {}", app.app_id, pipeline.name
            )
            with self._verifier_failures_lock:
                self._verifier_failures.pop((app.app_id, pipeline.name), None)

    def _record_verifier_failure(
        self,
        app: AppSpec,
        pipeline: PipelineSpec,
        error: str,
        *,
        now: datetime,
        dispatch_failure: bool,
    ) -> None:
        """Count semantic failures locally because their successful runs stay honest."""
        verifier = pipeline.verifier
        if verifier is None:  # pragma: no cover - guarded by _start_verifier
            return
        key = (app.app_id, pipeline.name)
        window_start = now - timedelta(seconds=pipeline.failure_budget.window_seconds)
        with self._verifier_failures_lock:
            failures = [at for at in self._verifier_failures.get(key, []) if at >= window_start]
            failures.append(now)
            self._verifier_failures[key] = failures
            consecutive = len(failures)
        issue = PipelineIssue.verifier_failed(pipeline.name, error)
        if consecutive == verifier.consecutive_failures_to_invalidate:
            if self.failure_handler is not None:
                self.failure_handler.handle_pipeline_failure(
                    app.model_copy(
                        update={
                            "policies": app.policies.model_copy(
                                update={"on_pipeline_failure": "invalidate"}
                            )
                        }
                    ),
                    issue,
                )
        elif dispatch_failure and consecutive == pipeline.failure_budget.consecutive_failures:
            if self.failure_handler is not None:
                self.failure_handler.handle_pipeline_failure(app, issue)

    # -- integrity dispatch (a green run that stopped doing its job) --------

    def _dispatch_integrity(self, app: AppSpec, run: PipelineRun) -> None:
        """Dispatch the failure policy for a SUCCEEDED run that REGRESSED a collection.

        The close of the detect→repair loop: ``unwritten_collections`` made the
        violation visible, this makes it actionable, through the SAME
        ``on_pipeline_failure`` policy a raising run already goes through — no
        second dispatcher, no second policy, no second repair path.

        The run keeps its honest ``succeeded`` status; the reaction rides
        :class:`PipelineIssue` alongside it (see that model for why status is not
        the place to encode this). The two anti-spam rules that keep a green-run
        signal from becoming a repair storm both live on
        :meth:`PipelineRun.new_integrity_violations` — a collection must have been
        written by this pipeline before to count at all, and a violation the
        previous run already reported is not re-reported. This method only feeds
        it the ledger history and hands the verdict to the policy.
        """
        if self.failure_handler is None:
            return
        prior_runs = self.run_store.list_runs(
            app.app_id, pipeline_name=run.pipeline_name, limit=self.INTEGRITY_HISTORY_LIMIT
        )
        pipeline = next((p for p in app.pipelines if p.name == run.pipeline_name), None)
        regressed = run.new_integrity_violations(
            [c.name for c in app.collections],
            prior_runs=prior_runs,
            expected_writes=pipeline.writes if pipeline is not None else (),
        )
        if not regressed:
            return
        logging.warning(
            "pipeline run {} for app {} succeeded but regressed collection(s) {} — "
            "dispatching the {} policy",
            run.run_key,
            app.app_id,
            regressed,
            app.policies.on_pipeline_failure,
        )
        self.failure_handler.handle_pipeline_failure(
            app, PipelineIssue.unwritten(run.pipeline_name, regressed)
        )

    # -- startup sweep (process-restart honesty) ---------------------------

    def sweep_orphaned_runs(self, now: datetime) -> int:
        """Close every still-``running`` ledger entry as an interrupted restart.

        A run left ``running`` across a process restart has no live maintainer
        session behind it anymore — the ``on_session_end`` close seam that would
        normally settle it never fires, because the process that was going to run
        it is gone. Left alone it sits forever as a false "in progress" signal on
        ``/system``. Call this ONCE at startup, before any trigger can fire again
        (see the app-side ``init_apps`` wiring). Returns the count closed, for a
        startup log line.
        """
        closed = 0
        for app in self.app_store.list_apps(include_archived=True):
            for run in self.run_store.list_runs(app.app_id):
                if run.status != "running":
                    continue
                run.close(now=now, status="failed", error="interrupted: process restart")
                self.run_store.save(run)
                closed += 1
        return closed

    # -- I3: least-privilege tool scope for an unattended pipeline fire -----

    def pipeline_scope(self, trigger_id: str | None) -> tuple[list[str], bool] | None:
        """The ``(allowed_tools, strict_tool_scope)`` a pipeline fire runs under.

        Enforces ``PipelineSpec.tools_allowlist`` as AUTHORITATIVE
        least privilege (the default flipped from permissive):

        * NOT an app pipeline ⇒ ``None`` (the caller keeps the session's grants).
        * EMPTY allowlist ⇒ the MINIMUM scope ``["app_data"]`` under strict scope.
          A pipeline that declared no tools writes its data and nothing else; a
          pipeline that wants more (a connector, ``web_search``, a file read)
          must DECLARE it. An undeclared unattended fire is never permissive.
        * NON-EMPTY allowlist ⇒ that list unioned with ``app_data`` (which every
          pipeline needs to write results), under strict scope so it caps
          built-ins too, not just MCP tools.
        """
        match = self._resolve_pipeline(trigger_id)
        if match is None:
            return None
        _, pipeline = match
        allow = list(dict.fromkeys([*pipeline.tools_allowlist, _APP_DATA_TOOL_ID]))
        return allow, True

    # -- resolution helpers ------------------------------------------------

    def _resolve_pipeline(self, trigger_id: str | None) -> tuple[AppSpec, PipelineSpec] | None:
        """The (app, pipeline) whose ``trigger_ref`` == *trigger_id*, or ``None``."""
        if not trigger_id:
            return None
        for app in self.app_store.list_apps(include_archived=True):
            for pipeline in app.pipelines:
                if pipeline.trigger_ref == trigger_id:
                    return app, pipeline
        return None

    def _app_for_session(self, session_id: str) -> AppSpec | None:
        """The app whose ``maintainer_session_id`` == *session_id*, or ``None``."""
        for app in self.app_store.list_apps(include_archived=True):
            if app.maintainer_session_id == session_id:
                return app
        return None


__all__ = [
    "AppPipelineRunTracker",
    "FireRefusal",
    "PipelineFailureHandler",
    "PipelineFireResult",
]
