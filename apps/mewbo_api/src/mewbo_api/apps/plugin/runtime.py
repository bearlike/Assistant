#!/usr/bin/env python3
"""The apps plugin's collaborator contracts + the submitter wiring seam.

The two agent-facing session tools (:mod:`submit_app`, :mod:`app_data`) are
declared in ``.claude-plugin/plugin.json`` and BUILT through the ordinary
:class:`~mewbo_core.session_tools.SessionToolRegistry` plugin path, which feeds a
constructor only ``session_id`` + ``event_logger`` — so their real collaborators
have to be resolved some other way. Two resolution paths, matching how workstream
A exposes each collaborator:

* **The stores are process-wide singletons with factories** — ``app_data``
  resolves them directly via ``mewbo_api.apps.store``'s ``get_app_store`` /
  ``get_app_data_store`` / ``get_pipeline_run_store`` at handle time (A's
  docstrings name "the plugin" / "the ``app_data`` tool" as their consumers). No
  wiring seam is needed for those.
* **The lifecycle has no factory** (``AppLifecycle`` is constructed once in
  ``backend.py`` with the trigger store/policy + session backend), so
  ``submit_app`` resolves its submitter through the down-only
  :func:`register_app_submitter` push (mirrors ``register_builtin_root``): the API
  composition root pushes the concrete lifecycle at startup; the plugin never
  imports up to find it.

The collaborators are typed by **LOCAL Protocols** (not A's concrete classes) so
the plugin depends only on the method SHAPES the SDD ledger pinned, and a fake
satisfying the Protocol is all a test needs. Every signature here is verbatim
from ``.superpowers/sdd/progress.md`` ("Frozen cross-stream DI signatures"), with
the two additive optional kwargs A confirmed (``upsert(collection_spec=)``,
``query(sort=)``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mewbo_api.apps.models import AppDataDoc, AppSpec, CollectionSpec, PipelineRun


# ------------------------------------------------------------------
# Collaborator contracts (LOCAL Protocols — frozen signatures, spec §4 / ledger)
# ------------------------------------------------------------------


@runtime_checkable
class AppSubmitter(Protocol):
    """The submit half of the app lifecycle — persists a builder's draft.

    The concrete implementation is ``AppLifecycle`` (workstream A, ``lifecycle.py``):
    it persists spec v1, creates + tags the maintainer session, and — as of
    Phase 1 — ARMS each pipeline's declared ``schedule`` (``PipelineSpec.schedule``,
    a ``time.cron``/``time.at`` shape) directly onto the maintainer, stamping the
    resulting id into ``PipelineSpec.trigger_ref`` itself (PLATFORM-owned; the
    builder never sets it). An ``on_demand`` pipeline gets no trigger at all
    (``trigger_ref`` stays ``None``). Returns the durable :class:`AppSpec`.
    ``builder_session_id`` is the sub-agent session that authored the draft,
    distinct from the app's owner.
    """

    def submit(self, draft: AppSpec, *, builder_session_id: str) -> AppSpec:
        """Persist the builder's draft and return the durable, live :class:`AppSpec`."""
        ...


@runtime_checkable
class AppStore(Protocol):
    """Read side of the app manifest store (spec §4A ``AppStoreBase``)."""

    def get(self, app_id: str) -> AppSpec | None:
        """Return the app manifest for *app_id*, or ``None`` if there is none."""
        ...

    def list_apps(self, *, include_archived: bool = False) -> list[AppSpec]:
        """Every app (mirrors ``AppStoreBase.list_apps``).

        ``run_pipeline`` has no ``app_id`` argument (unlike ``app_data``) — it
        resolves "the app I maintain" by scanning for ``maintainer_session_id ==
        this session``, exactly like ``AppPipelineRunTracker._app_for_session``
        (``pipeline_tracker.py``) already does for the trigger-fire seam.
        """
        ...


@runtime_checkable
class AppDataStore(Protocol):
    """The app-ID-keyed data plane (spec §2.5 / §4A ``AppDataStoreBase``).

    ``upsert`` is idempotent by the compound ``(app_id, collection, key)`` and
    validates *doc* against ``collection_spec`` when one is supplied (raising
    ``jsonschema.ValidationError``); ``query`` returns validated documents (with an
    optional ``sort``); ``delete`` reports whether a document existed.
    """

    def upsert(
        self,
        app_id: str,
        collection: str,
        key: str,
        doc: dict,
        *,
        collection_spec: CollectionSpec | None = None,
        max_docs: int | None = None,
    ) -> None:
        """Idempotently write *doc*; validate against *collection_spec* if given.

        A NEW key that would exceed *max_docs* raises ``CollectionCapExceeded``
        (an update to an existing key is always allowed); both kwargs are additive.
        """
        ...

    def query(
        self,
        app_id: str,
        collection: str,
        *,
        filter: dict | None = None,  # noqa: A002 — matches the frozen store signature
        limit: int = 100,
        sort: str | None = None,
    ) -> list[AppDataDoc]:
        """Return documents from *collection*, optionally *filter*ed/*sort*ed, capped by *limit*."""
        ...

    def delete(self, app_id: str, collection: str, key: str) -> bool:
        """Remove one document; return whether it existed."""
        ...


@runtime_checkable
class PipelineRunner(Protocol):
    """Executes one CODE pipeline (Phase 2) — the ``run_pipeline`` tool's collaborator.

    The concrete implementation (``pipeline_runner.py:AppPipelineRunner``) loads
    the pipeline's ``entrypoint`` file from the app bundle, builds the
    workspace-scoped ``ctx`` (``params``/``now``/``glob``/``read_file``/
    ``collection(name).upsert|query|delete`` — schema + cap enforced, mirroring
    ``app_data``'s guarantees), and calls ``run(params, ctx)``. ``run_pipeline``
    is the id-keyed adapter of that class — its OTHER method, ``execute``
    (object-keyed: takes the resolved ``AppSpec``/``PipelineSpec`` + an explicit
    ``now``), is the collaborator ``apps/routes.py``'s REST invoke route and the
    trigger-fire seam use instead; both funnel into the SAME engine, so a
    fix/behavior change there reaches every caller.
    """

    def run_pipeline(
        self, app_id: str, pipeline_name: str, *, params: dict[str, Any], dry_run: bool
    ) -> dict[str, Any]:
        """Execute the named CODE pipeline; raise on any tool-visible failure.

        A failure (missing/unreadable entrypoint, a ``params`` shape rejected by
        the pipeline's ``params_schema``, or an unhandled exception inside
        ``run(params, ctx)``) is raised as a ``PipelineExecutionError``-shaped
        ``Exception`` — ``str(exc)`` is already a clean ``"<code>: <message>"``,
        which the tool renders verbatim (mirrors ``AppSubmitter.submit``'s "a
        lifecycle rejection is agent-visible feedback" convention). On success,
        returns::

            {
                "output": <JSON-serializable — whatever run(params, ctx) returned>,
                "evaluated_at": <aware datetime — when this execution finished>,
                "docs_written": {"<collection>": <count>, ...},
                "cache_hit": bool,
            }

        ``dry_run=True`` must exercise the IDENTICAL code path (schema validation,
        ``glob``/``read_file`` resolution, ``ctx`` construction) WITHOUT any
        durable collection write — entirely the runner's responsibility; the tool
        only forwards the flag. This is what makes ``run_pipeline`` a safe
        "test it before you ship" loop for the builder (spec: "schemas
        enforce what prose instructs").
        """
        ...


@runtime_checkable
class PipelineRunStore(Protocol):
    """The provenance ledger store (spec §2.8 / §4A ``PipelineRunStoreBase``).

    ``get_open`` returns the currently-``running`` ledger entry for a pipeline —
    opened at trigger fire by :class:`~mewbo_api.apps.pipeline_tracker.AppPipelineRunTracker`
    at the trigger-deliver seam, closed at the maintainer run's end — or ``None``.
    ``save`` persists an entry after the tool increments its ``docs_written``.
    """

    def get_open(self, app_id: str, pipeline_name: str) -> PipelineRun | None:
        """Return the ``running`` ledger entry for *pipeline_name*, or ``None``."""
        ...

    def save(self, run: PipelineRun) -> None:
        """Persist *run* after its ``docs_written`` was incremented."""
        ...


# ------------------------------------------------------------------
# The submitter push seam (down-only — the API pushes the lifecycle at startup)
# ------------------------------------------------------------------

_SUBMITTER: AppSubmitter | None = None


def register_app_submitter(submitter: AppSubmitter) -> None:
    """Push the concrete app submitter (the ``AppLifecycle``) — the API calls this once.

    Mirrors ``mewbo_core.plugins.register_builtin_root``: the composition root
    supplies the concrete lifecycle; the plugin never imports up to find it. Last
    write wins (a re-register in a test swaps the fake cleanly). Stores are NOT
    pushed here — they have their own process-wide factories in
    ``mewbo_api.apps.store`` that ``app_data`` resolves directly.
    """
    global _SUBMITTER
    _SUBMITTER = submitter


def current_app_submitter() -> AppSubmitter | None:
    """The wired submitter, or ``None`` when the deployment hasn't configured apps.

    A ``None`` return is the graceful-degradation signal ``submit_app`` turns into
    a clean error instead of a crash.
    """
    return _SUBMITTER


# ------------------------------------------------------------------
# The pipeline-runner push seam (down-only — mirrors the submitter seam above)
# ------------------------------------------------------------------

_PIPELINE_RUNNER: PipelineRunner | None = None


def register_pipeline_runner(runner: PipelineRunner) -> None:
    """Push the concrete code-pipeline executor (workstream C) — the API calls this once.

    Mirrors :func:`register_app_submitter` exactly: the composition root supplies
    the concrete runner at startup; the plugin never imports up to find it. Last
    write wins (a re-register in a test swaps the fake cleanly).
    """
    global _PIPELINE_RUNNER
    _PIPELINE_RUNNER = runner


def current_pipeline_runner() -> PipelineRunner | None:
    """The wired runner, or ``None`` when the deployment hasn't configured pipeline execution.

    A ``None`` return is the graceful-degradation signal ``run_pipeline`` turns
    into a clean error instead of a crash.
    """
    return _PIPELINE_RUNNER


__all__ = [
    "AppDataStore",
    "AppStore",
    "AppSubmitter",
    "PipelineRunStore",
    "PipelineRunner",
    "current_app_submitter",
    "current_pipeline_runner",
    "register_app_submitter",
    "register_pipeline_runner",
]
