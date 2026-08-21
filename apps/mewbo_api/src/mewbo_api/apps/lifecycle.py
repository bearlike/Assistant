"""``AppLifecycle`` — the state machine that drives an app across its phases.

This is the ONE place an app is created, submitted-to-live, paused, resumed,
rolled back, or archived. Every status move goes through
:meth:`AppSpec.transition` (never a bare ``self.status =``), and every clock
read is the injected ``now_fn`` (never the wall clock in a behavioral path) —
the same discipline ``TriggerSpec``/``TriggerService`` follow.

Collaborators are DI'd FIELDS (atomic-class rule): the app-manifest store, the
trigger store + policy (triggers stay owned by the trigger subsystem — this only
arms/pauses them), a minimal :class:`AppSessionBackend` for the
session operations it needs, and ``now_fn``. Tests inject fakes + a fixed NOW;
nothing here imports a concrete ``SessionRuntime`` or reaches for a real clock.

The maintainer session is the app's durable agent home: created + tagged
``app:<app_id>`` at submit, advertising the ``apps`` capability so a trigger
that later re-engages it sees the maintainer/repair AgentDefs. Pipeline triggers
are **armed on it** at submit from each pipeline's DECLARED ``schedule``: the
builder declares a ``time.cron``/``time.at`` schedule in its
manifest and the PLATFORM mints the trigger on the maintainer, stamping the id
into the (platform-owned) ``trigger_ref``. The builder does not self-arm on this
path. The secondary flow — the builder hand-arms a trigger on its own ephemeral
session and submit re-homes it — is still honoured for a chat-builder (see
``_rehome_pipeline_trigger``), but declared schedules are the primary path. See
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from mewbo_core.common import get_logger
from mewbo_core.session.session_provenance import APPS_TAG_PREFIX, SessionTag
from mewbo_core.triggers.spec import TriggerProvenance

from .models import (
    PIPELINE_ALLOWED_EXEC,
    AppFrontend,
    AppReadyEvent,
    AppSpec,
    AppUpdatedEvent,
    AppVersion,
    AppVersionAuthor,
    AppVersionSummary,
    PipelineIssue,
    PipelineSample,
    PipelineSpec,
    WorkspaceRef,
)
from .pipeline_runner import PipelineExecutionError
from .plugin.linter import derive_collection_writes
from .staging import AppStagingArea
from .store import new_app_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from mewbo_core.triggers.policy import TriggerPolicy
    from mewbo_core.triggers.spec import TriggerSpec
    from mewbo_core.triggers.store import TriggerStoreBase
    from mewbo_core.workspaces.project_catalog import ProjectCatalog

    from .models import PipelineVerdict
    from .pipeline_tracker import AppPipelineRunTracker
    from .store import AppStoreBase

logging = get_logger(name="api.apps.lifecycle")

# The capability a maintainer session advertises so trigger-driven re-engagement
# sees the app maintainer/repair AgentDefs (capability-gated, workstream B).
APPS_CAPABILITY = "apps"

# Session tag PREFIX that binds a session to its app. The ``/system/triggers`` +
# ``/triggers`` routes match on it. The literal is owned by core's
# ``session_provenance`` (so ``SessionOrigin.classify`` and this module read the
# same string and can never drift); re-exported under this name since it's this
# module's public/documented seam. The stamp sites build the WHOLE tag through
# ``SessionTag.app`` / ``SessionTag.app_fresh`` rather than concatenating this,
# so the two-segment and per-session-unique spellings stay in one home.
MAINTAINER_TAG_PREFIX = APPS_TAG_PREFIX

# A declared-schedule trigger is the app's durable heartbeat — it lives as long
# as the app does (cancelled by pause/archive, never by lapsing on its own). This
# far-future expiry is stamped EXPLICITLY at arm time precisely so
# ``TriggerPolicy.admit`` cannot stamp its 7-day ``default_expiry`` (which it
# applies ONLY when ``expires_at`` is None) and silently kill the heartbeat.
# Leaving it None makes an app refresh exactly once and then die.
_SCHEDULE_TRIGGER_TTL = timedelta(days=3650)

# Placeholder frontend for a freshly-created draft (ledger decision): a valid
# non-empty stlite bundle the console can mount while the builder works, replaced
# by the real frontend at submit.
_PLACEHOLDER_FRONTEND = AppFrontend(
    entrypoint="app.py",
    files={
        "app.py": (
            "import streamlit as st\n\n"
            'st.title("Building your app…")\n'
            'st.write("The builder is designing this app. This view updates when it goes live.")\n'
        )
    },
)


class AppSessionBackend(Protocol):
    """The session operations :class:`AppLifecycle` needs — a narrow DI seam.

    ``SessionRuntime`` satisfies it through the tiny :class:`RuntimeSessionBackend`
    adapter; tests inject a fake that records calls. Keeping the lifecycle bound
    to this Protocol (not the full runtime) is what makes ``submit`` testable
    without a live session store.
    """

    def create_session(self) -> str:
        """Mint a new empty session and return its id."""
        ...

    def tag_session(self, session_id: str, tag: str) -> None:
        """Attach a resolution tag to a session."""
        ...

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None:
        """Write a context event (capabilities / scope) onto a session."""
        ...

    def append_event(self, session_id: str, event: dict[str, object]) -> None:
        """Append a transcript event (the ``app_ready`` / ``app_updated`` wire event)."""
        ...

    def is_terminated(self, session_id: str) -> bool:
        """Whether *session_id* was permanently terminated (a dead end — never reusable)."""
        ...

    def tags_for_session(self, session_id: str) -> Sequence[str]:
        """The server-stamped tags on *session_id* (``()`` when it has none).

        The tag tier is what binds a session the server OPENED against an app but
        never made its maintainer — see :meth:`AppLifecycle._bound_app_for_session`.
        A backend that does not implement it degrades to no tag tier (the
        pre-tag behaviour), so it is read defensively rather than required.
        """
        ...


class AppRunStarter(Protocol):
    """Starts an agent run on an app's builder/maintainer session — a narrow DI seam.

    The lifecycle mints and tags the agent sessions, but STARTING a run needs the
    full ``SessionRuntime`` (gating-context reinjection, tool-grant derivation,
    cwd/budget resolution) that only ``backend.py`` composes — so the composition
    root injects a tiny adapter over the ``_trigger_deliver`` idle-start idiom, and
    a test injects a fake that records ``(session_id, message)``. ``None`` (the
    default) degrades every kick-off to a logged no-op, never a crash.
    """

    def start_app_run(self, session_id: str, message: str) -> str:
        """Start (or steer) a run on *session_id* with *message* — fire-and-forget.

        Returns a small landed signal so a caller (the ``/fire`` route) can honestly
        report what happened: ``"started"`` (an idle session began a fresh run),
        ``"steered"`` (a run was already live, so the message was enqueued into it),
        or ``"refused"`` (the runtime declined, or the start failed). The
        builder/repair kick-offs ignore the return; the fire seam surfaces it.
        """
        ...


class AppLifecycle:
    """Owns the app phase transitions over its injected collaborators.

    Atomic feature class: ``app_store`` / ``trigger_store`` / ``trigger_policy`` /
    ``sessions`` / ``now_fn`` are its state; ``create_draft`` / ``submit`` /
    ``pause`` / ``resume`` / ``rollback`` / ``archive`` are its behavior.
    """

    def __init__(
        self,
        *,
        app_store: AppStoreBase,
        trigger_store: TriggerStoreBase,
        trigger_policy: TriggerPolicy,
        sessions: AppSessionBackend,
        run_starter: AppRunStarter | None = None,
        tracker: AppPipelineRunTracker | None = None,
        background_runner: Callable[[Callable[[], None]], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        project_catalog: ProjectCatalog | None = None,
        allowed_exec_binaries: frozenset[str] = PIPELINE_ALLOWED_EXEC,
    ) -> None:
        """Capture the injected collaborators; default ``now_fn`` is UTC now.

        *run_starter* starts the builder run at ``create_draft`` and the repair
        run in :meth:`handle_pipeline_failure`; ``None`` degrades both to a logged
        no-op (the deployment simply hasn't wired the runtime yet).

        *tracker* is the pipeline-run ledger + fire seam (:class:`AppPipelineRunTracker`).
        The lifecycle drives it to SEED a first run of every pipeline at go-live and
        to seed a re-armed pipeline in :meth:`rearm`. Built AFTER the lifecycle in
        ``backend.py`` (it takes the lifecycle as its ``failure_handler``), so it is
        an assign-after-construction field; ``None`` (the default) makes both seed
        paths clean no-ops (a deployment without the fire seam wired).

        *background_runner* runs a code-pipeline seed OFF the request thread (a code
        fire executes synchronously, so seeding one inline would block ``submit`` /
        ``rearm``); default spawns a daemon thread. A test injects a synchronous
        ``lambda fn: fn()`` for determinism.

        *project_catalog* is the ONE name→directory catalog the rest of the API
        resolves a session's ``project`` through. It is read at the submit
        boundary only, to refuse a ``workspace_ref`` whose key no project answers
        to (:meth:`_validate_workspace_ref`). ``None`` means the deployment has
        not wired it: the check is SKIPPED and a warning is logged once, so an
        unwired deployment is visible rather than silently unguarded.
        """
        self.app_store = app_store
        self.trigger_store = trigger_store
        self.trigger_policy = trigger_policy
        self.sessions = sessions
        self.run_starter = run_starter
        self.tracker = tracker
        self.background_runner = background_runner or self._spawn_daemon
        self.now_fn = now_fn or self._utcnow
        self.project_catalog = project_catalog
        self.allowed_exec_binaries = allowed_exec_binaries
        # One warning per lifecycle for an unwired catalog: a submit must not
        # crash over missing wiring, but a deployment running the submit
        # boundary with the check disabled has to be readable in the log.
        self._catalog_unwired_logged = False
        # Per-app locks serializing :meth:`rearm` — two concurrent re-arm POSTs for
        # the same app would otherwise both mint a fresh trigger off a stale in-memory
        # copy, and the lost ``app_store.save`` update would orphan one armed trigger
        # (double-firing forever, with no sweep to reap it). Prod is gunicorn with a
        # SINGLE worker, so in-process locks suffice — it is the one-process
        # invariant that carries this, not the thread count, which is tunable.
        # Keyed by app_id so
        # one app's repair never blocks another's; the map is bounded by the app count.
        self._rearm_locks: dict[str, threading.Lock] = {}
        self._rearm_locks_guard = threading.Lock()

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _spawn_daemon(task: Callable[[], None]) -> None:
        """Run *task* on a fire-and-forget daemon thread (the default seed executor)."""
        threading.Thread(target=task, daemon=True).start()

    # -- workspace scope ---------------------------------------

    @staticmethod
    def _workspace_scope(workspace_ref: WorkspaceRef) -> dict[str, object]:
        """Map a workspace binding to the agent session's project-scope context.

        The maintainer + builder are the app's agent side, and the workspace binds
        them to the SAME project/workspace primitive ordinary sessions anchor to
        (the convention) — never a new workspace-like entity:

        * ``kind="shared"`` → the ``key`` IS that existing project's key (a config
          project name or ``managed:<id>``). Emitted as the session ``project``
          context field so trigger re-engagement resolves its cwd/MCP scope
          through ``_resolve_session_cwd`` exactly like a console-created session.
        * ``kind="own"`` → v1 isolated default scope: NO ``project`` key (the
          session runs in its default temp cwd), tagged to the app by ``app_id``
          alone. No workspace entity is minted.

        The frontend never inherits any of this — it is agent-side only.
        """
        if workspace_ref.kind == "shared" and workspace_ref.key.strip():
            return {"project": workspace_ref.key.strip()}
        return {}

    def _validate_workspace_ref(self, workspace_ref: WorkspaceRef) -> None:
        """Refuse a ``kind="shared"`` key no project answers to (submit boundary).

        :meth:`_workspace_scope` stamps that key verbatim as the agent session's
        ``project`` context field, and nothing between the two namespaces ever
        compared them: a key the catalog does not know (an app id, a title, a
        directory name) persisted happily and then failed on EVERY turn of the
        maintainer session, which resolves the same field through the catalog.
        The submit boundary is where model-authored data crosses into storage,
        so it is where the two namespaces are made to agree.

        **A method here, NOT a ``model_validator`` on ``WorkspaceRef``** — the
        ``ensure_unique_pipeline_names`` precedent, and the reason is
        load-bearing: the app store is append-only, so stored ``AppVersion``
        snapshots already holding a bad key MUST keep parsing, or every detail
        read of an affected app 500s. Validation of NEW data belongs at the
        trust boundary it crosses; stored history re-crosses the parse seam
        under the contract it was written with.

        **``resolve`` rather than ``find``**, because ``resolve`` is what the
        key eventually reaches. ``find`` would accept a key that is listed but
        has no usable directory (a registered repository with no checkout), so
        submit-time acceptance would stop meaning run-time resolvability — the
        same silent wedge in a narrower form. Its refusal message already embeds
        the catalog's own available-list, so the reask is correctable in one turn
        without reaching into the private ``_available_hint``.

        **But the runtime is not ``resolve`` — it is
        ``backend.py:_resolve_project_cwd``, which RECOVERS from one refusal**:
        an ``unavailable`` MANAGED project (or worktree) is a directory Mewbo
        owns, so the runtime creates it and succeeds. Refusing that here would
        make submit STRICTER than the thing it predicts, which is the opposite
        of the property this check exists for, and would block a builder binding
        to a perfectly good managed workspace. So that one arm is mirrored:
        ``unavailable`` + a managed/worktree entry with a path ⇒ accept. Every
        other code — ``not_found``, ``no_checkout``, ``auto_sentinel``,
        ``empty``, and ``unavailable`` on a CONFIGURED project, whose directory
        belongs to the operator — still refuses.

        **That mirroring is a SECOND COPY of a rule, and that is a cost, not a
        win.** ``backend.py:_resolve_project_cwd`` is the authority; this copy
        will drift the day that recovery changes. The real cure is for the
        recovery to live on :class:`ProjectCatalog` so both callers read one
        implementation — worth doing, deliberately not done here.

        **No side effects.** Unlike the runtime, this makes no directory: a
        validator that mutates the filesystem is the wrong shape. Only the
        accept/refuse DECISION is mirrored, never the repair.

        ``kind="own"`` writes no ``project`` at all and is never checked.
        """
        if workspace_ref.kind != "shared":
            return
        key = workspace_ref.key.strip()
        if not key:
            # An empty shared key writes no project either — same scope as own.
            return
        catalog = self.project_catalog
        if catalog is None:
            if not self._catalog_unwired_logged:
                self._catalog_unwired_logged = True
                logging.warning(
                    "No project catalog wired into AppLifecycle — a shared workspace_ref "
                    "key is being accepted unchecked, so an app can bind its agent "
                    "sessions to a project that does not exist."
                )
            return
        try:
            catalog.resolve(key)
        except ValueError as exc:
            # ProjectResolutionError IS a ValueError and carries a ``code``;
            # read it defensively so a plain ValueError still refuses.
            if getattr(exc, "code", None) == "unavailable":
                # The ONE arm ``_resolve_project_cwd`` recovers from: a managed
                # project's directory is Mewbo's own to create, so the runtime
                # makes it and succeeds. Mirror the DECISION only — no makedirs.
                entry = catalog.find(key)
                if entry is not None and entry.kind in {"managed", "worktree"} and entry.path:
                    return
            # ``submit`` already surfaces a ValueError to the builder as an
            # actionable reask.
            raise ValueError(
                f"workspace_ref key {key!r} is not a project this platform can resolve: "
                f"{exc} A kind='shared' key must be an EXISTING project key from the "
                "platform's project list — an app id, an app title or a directory name is "
                "never one. Use kind='own' if the app needs no project workspace."
            ) from exc

    def _agent_session_context(self, app_id: str, workspace_ref: WorkspaceRef) -> dict[str, object]:
        """The context event every app agent session carries: capability + app tag + scope."""
        return {
            "client_capabilities": [APPS_CAPABILITY],
            "app_id": app_id,
            **self._workspace_scope(workspace_ref),
        }

    def _mint_maintainer_session(
        self, app_id: str, workspace_ref: WorkspaceRef, *, canonical: bool = True
    ) -> str:
        """Mint + tag + scope a session bound to *app_id* — the ONE place one is minted.

        Shared by :meth:`submit` (the fresh-maintainer branch),
        :meth:`get_or_create_maintainer_session` and its ``fresh`` path, so the
        capability stamp and the workspace scope are written once rather than
        drifting between copies.

        ``canonical=False`` takes the per-session-unique ``app:<id>:<session>``
        tag instead of the bare ``app:<id>``. The bare one is held by the app's
        builder-or-maintainer session and a tag maps to exactly ONE session, so a
        second claimant would steal it — the additional session gets an
        un-stealable variant that still carries the same ``app_id`` facet, which
        is what :meth:`AppStagingArea.app_for_session` resolves it by.
        """
        session_id = self.sessions.create_session()
        self.sessions.tag_session(
            session_id,
            SessionTag.app(app_id)
            if canonical
            else SessionTag.app_fresh(app_id, session_id),
        )
        self.sessions.append_context_event(
            session_id, self._agent_session_context(app_id, workspace_ref)
        )
        return session_id

    # -- maintainer session (get-or-create) ---------------------------------

    def get_or_create_maintainer_session(
        self, app_id: str, *, fresh: bool = False
    ) -> tuple[str, bool] | None:
        """Return this app's durable maintainer session, minting one if it has none.

        ``submit`` mints (or reuses) a maintainer session implicitly, but nothing
        hands it back — an app whose console-streamed builder session already
        ended, or one reached only via REST/an operator, previously had NO way to
        open a conversation with its own maintainer. This is that route back.

        Resolution mirrors ``get_app``/``submit_app``'s ``_resolve_app`` scan
        (maintainer, then owner) so the session handed back is one that scan can
        already find: minting a THIRD session here would give the app two live
        claimants and make the scan's first-match order load-bearing. A builder
        session that never submitted (``owner_session_id`` set,
        ``maintainer_session_id`` still ``None``) is therefore REUSED, never
        replaced — the draft-in-progress case.

        **Decision: an ``archived`` app is NOT refused.** `submit`'s
        maintainer-resubmit branch overwrites ``status`` to ``"building"`` in
        memory before calling ``AppSpec.transition("live", ...)``, bypassing
        ``_ALLOWED_TRANSITIONS["archived"]`` (the empty, otherwise-terminal set)
        — the documented way an app's own maintainer revives an archived app (see
        :meth:`submit`'s docstring). Refusing this route for an archived app would
        strand the one session that can ever reach that revival path again, for
        no corresponding safety gain — archiving already cancelled every trigger
        the app owned, so a reachable maintainer session grants no more than an
        agent conversation, not a live re-arm.

        A session that was PERMANENTLY TERMINATED is never handed back (a
        terminated session is a documented dead end — it cannot be steered or
        re-engaged) — that case mints a replacement instead.

        **``fresh=True`` skips the reuse entirely and always mints**, for a caller
        that asked to START a conversation about this app rather than be handed
        the one already in progress — a composer submitting a turn, as against
        the "open session" action on the app detail header, which wants exactly
        the reuse above and is why that stays the default.

        A fresh session is deliberately NOT written back to
        ``maintainer_session_id``: the repair wake dereferences that field, and a
        second claimant would make the resolvers' first-match scan order decide
        which session keeps working. It is bound by its TAG instead — see
        :meth:`AppStagingArea.app_for_session`, which every app tool resolves
        through, so a tag-bound session can read, stage, dry-run, query and
        resubmit exactly as a maintainer can. What it is NOT is the app's OWN
        session: ``maintainer_session_id`` still points elsewhere, so the repair
        wake and the armed triggers keep belonging to that session and never to
        this one (see this class's ``submit`` for why the submitter is never read
        as the maintainer).

        Returns ``None`` for an unknown *app_id*; else ``(session_id, created)``.
        """
        app = self.app_store.get(app_id)
        if app is None:
            return None
        if fresh:
            return self._mint_maintainer_session(
                app_id, app.workspace_ref, canonical=False
            ), True
        if app.maintainer_session_id is not None and not self.sessions.is_terminated(
            app.maintainer_session_id
        ):
            return app.maintainer_session_id, False
        if app.owner_session_id is not None and not self.sessions.is_terminated(
            app.owner_session_id
        ):
            return app.owner_session_id, False
        session_id = self._mint_maintainer_session(app_id, app.workspace_ref)
        now = self.now_fn()
        self.app_store.save(
            app.model_copy(update={"maintainer_session_id": session_id, "updated_at": now})
        )
        return session_id, True

    # -- draft -------------------------------------------------------------

    def create_draft(self, intent: str, workspace_ref: WorkspaceRef) -> AppSpec:
        """Create a ``building`` draft app + its builder session, then KICK OFF the build.

        The draft carries a placeholder frontend (so the console can mount a
        live view immediately) and a freshly-minted builder session tagged
        ``app:<app_id>`` — the session the console streams the build progress
        from and the ``owner_session_id`` ``submit`` later reconciles. No version
        snapshot is written yet; ``submit`` records v1 with the real design.

        The builder RUN is started here: the session advertises the
        ``apps`` capability so the ``app-builder`` skill + AgentDef surface, and
        the kick-off message hands the root the intent + workspace choice + the
        assigned ``app_id`` (so ``submit_app`` reuses it) so the root spawns the
        ``app-builder`` leaf per the skill's delegation contract.
        """
        now = self.now_fn()
        app_id = new_app_id()
        builder_session_id = self.sessions.create_session()
        self.sessions.tag_session(builder_session_id, SessionTag.app(app_id))
        self.sessions.append_context_event(
            builder_session_id,
            self._agent_session_context(app_id, workspace_ref),
        )
        title = intent.strip()[:80] or "New app"
        spec = AppSpec(
            app_id=app_id,
            title=title,
            summary=intent.strip(),
            owner_session_id=builder_session_id,
            workspace_ref=workspace_ref,
            frontend=_PLACEHOLDER_FRONTEND,
            status="building",
            created_at=now,
            updated_at=now,
        )
        self.app_store.save(spec)
        self._start_builder_run(builder_session_id, intent, workspace_ref, app_id)
        return spec

    def _start_builder_run(
        self, session_id: str, intent: str, workspace_ref: WorkspaceRef, app_id: str
    ) -> None:
        """Kick off the build via the run-starter (no-op + warning when unwired)."""
        if self.run_starter is None:
            logging.warning(
                "Apps run-starter not configured; builder run for app {} not started "
                "(the draft stays 'building' with no build progress).",
                app_id,
            )
            return
        self.run_starter.start_app_run(
            session_id, self._builder_kickoff_message(intent, workspace_ref, app_id)
        )

    @staticmethod
    def _builder_kickoff_message(intent: str, workspace_ref: WorkspaceRef, app_id: str) -> str:
        """Compose the builder kick-off per the ``app-builder`` skill delegation contract."""
        if workspace_ref.kind == "shared" and workspace_ref.key.strip():
            workspace = f"shared (project '{workspace_ref.key.strip()}')"
        else:
            workspace = "own (a fresh private workspace)"
        return (
            "Build a Mewbo App for this request. Delegate to the `app-builder` agent per the "
            "`app-builder` skill (spawn_agent) — do NOT design the data model, frontend, or "
            "pipelines yourself.\n\n"
            f"Intent: {intent.strip()}\n"
            f"Workspace: {workspace}\n"
            f"App id: {app_id} — name the app directory and the `submit_app` `app_id` EXACTLY "
            "this so the platform reconciles it with the draft it already created.\n\n"
            "The build is done when the `app_ready` event is emitted."
        )

    # -- submit (build → live) ---------------------------------------------

    def _bound_app_for_session(self, session_id: str) -> AppSpec | None:
        """The app the SERVER bound *session_id* to, or ``None`` if it bound none.

        ONE resolution path, shared with ``get_app``:
        :meth:`AppStagingArea.app_for_session` — the two id FIELDS
        (``maintainer_session_id`` / ``owner_session_id``) first, then the
        server-stamped ``app:<id>[:<session>]`` TAG. Widening submit's membership
        test to the tag tier is what lets a session the console composer opened
        against an existing app update THAT app instead of being told to fork a
        new one; re-deriving the rule here instead would give the read surface and
        the write surface two answers that drift.

        **The tag, never the ``app_id`` CONTEXT key.** A request's context is
        merged verbatim into the session and is re-writable on any later turn, so
        it addresses an app while proving nothing about it. The tag is stamped
        only by this class, only for an app it just read.

        Reading the tags is a defensive call: an :class:`AppSessionBackend` that
        does not implement ``tags_for_session``, or a session store that is
        momentarily unavailable, degrades to no tag tier — i.e. exactly the
        pre-tag behaviour, which is the fail-CLOSED direction here (the tier only
        ever widens what a session may do).

        Cost: ``O(collection)`` in the number of stored apps, plus one ``O(1)``
        get per apps tag — the same scan ``get_app`` already performs per call.
        """
        reader = getattr(self.sessions, "tags_for_session", None)
        tags: tuple[str, ...] = ()
        if callable(reader):
            try:
                tags = tuple(reader(session_id))
            except Exception as exc:  # noqa: BLE001 - an unreadable tag is "no tag"
                logging.warning(
                    "apps: session tag read failed for {} ({}); submit falls back to "
                    "the id fields alone",
                    session_id,
                    exc,
                )
        return AppStagingArea(session_id=session_id).app_for_session(
            self.app_store, session_tags=tags
        )

    def submit(self, draft: AppSpec, *, builder_session_id: str) -> AppSpec:
        """Persist the built app as a new version, arm triggers, go live.

        Reconciles with the ``building`` draft row the gallery-create flow already
        wrote. When a row exists for ``draft.app_id`` it is the
        authority for the app's IDENTITY — ``owner_session_id`` / ``created_at`` /
        ``workspace_ref`` are preserved from it and only the builder-authored
        CONTENT (frontend, collections, pipelines, policies, title/summary/icon)
        is taken from *draft*. The one repair exception is a server-bound session
        replacing an unresolvable legacy ``workspace_ref``: retaining a poisoned
        identity would make every later app session unusable.

        A submit for an app whose row is NOT ``building``/``draft`` is REFUSED
        unless the calling session is one the SERVER bound to this app — the
        double-submit + live-overwrite guard (a chat builder reusing a live
        ``app_id`` would otherwise silently clobber it), surfaced to the builder
        as a reask. Membership is :meth:`_bound_app_for_session`, so BOTH the
        app's own maintainer (the ``app-repair`` AgentDef's documented "resubmit
        with the same ``app_id``" flow) and a session the console composer opened
        against the app (bound by its stamped tag) take the update branch: it
        proceeds as a version bump, reusing the app's EXISTING maintainer session
        — never the submitter, never a second mint, which would orphan the live
        maintainer's armed triggers — and recording the snapshot as a
        ``"repair"``-authored version.

        The mirror-image refusal guards the INSERT path: a session already bound
        to one app cannot mint a different one. Before it, a refused update left
        the model holding an edited bundle and a refusal that read as advice to
        fork, and a second app is what the operator got.

        Steps: reconcile identity, mint (or reuse) + tag the maintainer session,
        arm each pipeline's DECLARED schedule on it within :class:`TriggerPolicy`
        caps (re-homing a builder-armed trigger where there is no schedule),
        persist the manifest + version snapshot, transition to ``live``, and emit
        ``app_ready`` on the builder session the console is streaming.
        """
        now = self.now_fn()
        self._validate_code_pipelines(draft)
        draft = self._derive_pipeline_writes(draft)
        # Duplicate pipeline names silently shadow one another at every first-match
        # resolver (the fire seam, the ledger, the trigger binding), so refuse them
        # at the submit boundary before anything arms.
        draft.ensure_unique_pipeline_names()
        draft.ensure_exec_binaries_allowed(self.allowed_exec_binaries)
        # timeout_seconds parses up to 600 (the append-only-store bound — see the
        # field's own comment) but the real ceiling a pipeline may EXECUTE at is
        # narrower; refuse a new/updated pipeline over it here rather than let it
        # go live and get silently clamped at run time.
        draft.ensure_pipeline_timeouts_fit()
        app_id = draft.app_id
        existing = self.app_store.get(app_id)
        # The app the SERVER bound this session to, if any — the id fields it owns
        # OR the stamped tag it was opened against. It decides BOTH branches below:
        # which app this session may update, and which it may not create.
        bound = self._bound_app_for_session(builder_session_id)
        # An existing row normally owns workspace identity. A server-bound
        # resubmit may repair the one legacy shape that cannot safely persist:
        # a shared key the catalog cannot resolve. Select the value that will
        # actually be written before validating it, so validation cannot bless
        # the draft while persistence restores the poisoned row.
        workspace_ref = existing.workspace_ref if existing is not None else draft.workspace_ref
        if existing is not None and bound is not None and bound.app_id == app_id:
            try:
                self._validate_workspace_ref(existing.workspace_ref)
            except ValueError:
                workspace_ref = draft.workspace_ref
        # A shared workspace key becomes the agent sessions' ``project`` context
        # field, which the maintainer resolves through the SAME catalog on every
        # turn — so a key that catalog cannot resolve wedges the app's whole
        # agent side. Refuse it here, before anything persists.
        self._validate_workspace_ref(workspace_ref)
        is_maintainer_resubmit = False
        if bound is not None and bound.app_id != app_id:
            # A session bound to app A minting app B is the FORK this guard exists
            # to stop: a composer session opened against an existing app that is
            # refused an update must report the refusal, never create a second app
            # for the same purpose. It fires on the INSERT path too (where the
            # live-overwrite guard below never runs), which is where the fork
            # actually landed.
            raise ValueError(
                f"this session is bound to app {bound.app_id!r}, so it cannot submit "
                f"app {app_id!r}. Resubmit with app_id {bound.app_id!r} to ship a new "
                "version of the app this session was opened against; a genuinely "
                "different app has to be created from its own session."
            )
        if existing is not None and existing.status not in ("building", "draft"):
            if bound is None:
                # Not the maintainer, not tag-bound to this app: the chat-builder
                # collision this guard exists to catch. The remedy is NOT a new
                # app_id — that sentence is what produced a duplicate app — so the
                # message names the real next move and forbids the fork explicitly.
                raise ValueError(
                    f"app {app_id!r} is already {existing.status!r}, not building/draft, and "
                    "this session is not bound to it — refusing to overwrite a live app. "
                    "Do NOT submit under a different app_id: that creates a SECOND app "
                    "rather than updating this one. Report this refusal; an update has to "
                    "come from a session the server opened against this app (its maintainer, "
                    "or one minted by POST /api/apps/<app_id>/session)."
                )
            is_maintainer_resubmit = True
        # Verify every code pipeline actually RUNS before anything persists/arms — a
        # rehearsal failure refuses the submit (an agent-fixable reask, same UX as a
        # lint finding) rather than shipping a live app that breaks on first fire. It
        # runs AFTER the live-overwrite guard (so a collision reports as one, never
        # masked by a verification error) but BEFORE the maintainer session is minted
        # below, so a refusal never orphans a freshly-created session.
        verification = self._verify_pipelines(draft, now=now)
        # The row that owns the app's identity: the reconciled draft when present
        # (gallery-create flow), else the freshly-built draft (chat-builder flow).
        base = existing or draft

        if is_maintainer_resubmit:
            # The caller already IS this app's maintainer — reuse the session (its
            # tag/context stay intact; a second mint would orphan the live app). But
            # CANCEL its prior pipeline triggers before re-arming from the fresh
            # draft: the declared-schedule path mints a NEW trigger per pipeline
            # and cancels nothing (only the re-home path cancels its own source),
            # so without this every repair resubmit would LEAK the previous armed
            # trigger — it keeps firing a stale wake_prompt, its now-unmatched
            # trigger_id yields no pipeline_scope (broad session grants),
            # self-amplifies under on_pipeline_failure="repair", and accretes
            # against max_armed_per_session until admit rejects the CURRENT
            # schedule while the orphans keep firing. The fresh draft is the whole
            # truth for the trigger set, so every prior maintainer trigger is stale.
            #
            # ⚠️ THE MAINTAINER IS THE APP'S OWN SESSION, NEVER THE SUBMITTER.
            # The two coincide for an app-repair resubmit and DIVERGE for a
            # tag-bound composer session, which owns no triggers: reading the
            # submitter here would cancel an empty set while the real maintainer's
            # armed triggers survive and keep firing a stale wake_prompt — exactly
            # the leak the paragraph above exists to prevent — and would then arm
            # the new schedule on a session `maintainer_session_id` does not point
            # at, so the repair wake could never reach it. Do not "simplify" this
            # back to ``builder_session_id``.
            maintainer = existing.maintainer_session_id if existing is not None else None
            if maintainer is None:
                # A live app with no maintainer session is not reachable through
                # the normal paths (submit always stamps one); minting one is the
                # honest recovery and there are no prior triggers to cancel.
                maintainer = self._mint_maintainer_session(app_id, workspace_ref)
            else:
                self.trigger_store.cancel_for_session(maintainer)
        else:
            maintainer = self._mint_maintainer_session(app_id, workspace_ref)

        # The wakeability floor is enforced HERE, on the incoming draft — not at
        # model parse, where it would reject stored version snapshots carrying
        # none of the three fields (see PipelineSpec.ensure_wakeable).
        for p in draft.pipelines:
            p.ensure_wakeable()
        pipelines = [
            self._arm_pipeline(p, maintainer=maintainer, now=now) for p in draft.pipelines
        ]
        self._warn_on_unscheduled_pipelines(app_id, maintainer, pipelines)

        # The prior latest version's spec is the diff baseline for this version's
        # change summary (``None`` for v1 ⇒ everything is "added"). Read it BEFORE
        # persisting the new snapshot below.
        prior_latest = self.app_store.latest_version(app_id)
        version = prior_latest + 1 if is_maintainer_resubmit else 1
        author: AppVersionAuthor = "repair" if is_maintainer_resubmit else "builder"
        prev_spec: AppSpec | None = None
        if prior_latest >= 1:
            prev_version = self.app_store.get_version(app_id, prior_latest)
            prev_spec = prev_version.spec if prev_version is not None else None

        spec = draft.model_copy(
            update={
                "owner_session_id": base.owner_session_id,
                "workspace_ref": workspace_ref,
                "created_at": base.created_at,
                "maintainer_session_id": maintainer,
                "pipelines": pipelines,
                "version": version,
                # Always the honest in-flight state the submit transitions OUT of —
                # legal from "building" whether the true prior status was
                # draft/building (the guard above proved it) or live/paused/broken
                # (the maintainer-resubmit branch): building -> live is unconditionally
                # allowed, so this stays one code path for both.
                "status": "building",
                "updated_at": now,
            }
        )
        spec.transition("live", now=now)
        summary = AppVersionSummary.compute(prev_spec, spec)
        self.app_store.save(spec)
        self.app_store.save_version(
            AppVersion(
                app_id=app_id,
                version=version,
                spec=spec,
                author=author,
                note="repair resubmit" if is_maintainer_resubmit else "app submitted",
                summary=summary,
                verification=verification,
            )
        )
        self.sessions.append_event(
            builder_session_id,
            {
                "type": "app_ready",
                "payload": AppReadyEvent(
                    app_id=app_id,
                    title=spec.title,
                    summary=spec.summary,
                    version=spec.version,
                ).model_dump(),
            },
        )
        # Seed a first run of every pipeline so freshness is never born "Never
        # refreshed" — best-effort, after the app is durably live (a seed failure
        # must never fail the submit the app just completed).
        self._seed_pipelines(spec)
        return spec

    # -- seeding (go-live + re-arm) — a first run so freshness isn't born "Never" --

    def _seed_pipelines(self, app: AppSpec) -> None:
        """Fire every pipeline once at go-live, best-effort (no-op if unwired)."""
        if self.tracker is None:
            return
        for pipeline in app.pipelines:
            self._seed_pipeline(app, pipeline)

    def _seed_pipeline(self, app: AppSpec, pipeline: PipelineSpec) -> None:
        """Fire one pipeline once — a code fire OFF-thread (it blocks), agentic inline.

        A ``mode="code"`` fire executes the engine synchronously (it can call
        ``ctx.llm`` for seconds), so it runs on the injected ``background_runner``
        rather than blocking ``submit``/``rearm``; an agentic fire only opens a
        ledger row + dispatches a (non-blocking) wake, so it runs inline.
        """
        if pipeline.mode == "code":
            self.background_runner(lambda: self._run_seed_fire(app, pipeline))
        else:
            self._run_seed_fire(app, pipeline)

    def _run_seed_fire(self, app: AppSpec, pipeline: PipelineSpec) -> None:
        """Drive ONE seed fire through the tracker; log the outcome, never raise.

        Isolated so a single pipeline's seed failure (an exception OR a refusal
        such as a cooldown) never sinks the go-live/​re-arm it rode in on — one
        structured log line per outcome, nothing propagated.
        """
        if self.tracker is None:  # pragma: no cover - _seed_pipeline guards
            return
        try:
            outcome = self.tracker.fire_pipeline(app, pipeline, now=self.now_fn())
        except Exception:  # noqa: BLE001 - a seed must never break its caller
            logging.warning(
                "seed fire raised for app {} pipeline {}",
                app.app_id, pipeline.name, exc_info=True,
            )
            return
        if outcome.ok:
            logging.info(
                "seeded app {} pipeline {} ({} fire)", app.app_id, pipeline.name, outcome.mode
            )
        else:
            logging.info(
                "seed of app {} pipeline {} did not run: {}",
                app.app_id, pipeline.name, outcome.message,
            )

    @staticmethod
    def _derive_pipeline_writes(draft: AppSpec) -> AppSpec:
        """Fill empty materialization contracts from literal source-level writes.

        This runs after the entrypoint existence check and before every submit
        boundary validation, so an auto-derived name receives the same collection
        namespace check as an explicit one. It deliberately never rewrites an
        explicit contract and never rejects an unprovable source shape: static
        analysis is a convenience, not a second execution gate. A builder retains
        control for a computed collection name by declaring ``writes`` directly.
        """
        derived: list[PipelineSpec] = []
        for pipeline in draft.pipelines:
            if pipeline.mode != "code" or pipeline.tier != "materialize" or pipeline.writes:
                derived.append(pipeline)
                continue
            assert pipeline.entrypoint is not None  # validated immediately before this method
            names = derive_collection_writes(draft.frontend.files[pipeline.entrypoint])
            if not names:
                derived.append(pipeline)
                continue
            derived.append(pipeline.model_copy(update={"writes": tuple(sorted(names))}))
        return draft.model_copy(update={"pipelines": derived})

    @staticmethod
    def _validate_code_pipelines(draft: AppSpec) -> None:
        """Reject a ``mode="code"`` pipeline whose entrypoint isn't a bundle file (submit boundary).

        :class:`PipelineSpec` already enforces "entrypoint present IFF mode='code'"
        at definition, but it does NOT hold the frontend bundle, so it cannot check
        that the named file actually EXISTS among ``draft.frontend.files``. That
        cross-field check lives here — the one place submit sees both the pipeline
        list and the bundle. Raised as a ``ValueError`` so ``submit_app`` surfaces
        it as an actionable reask (write ``pipelines/<name>.py``, then resubmit)
        rather than shipping a live app whose scheduled pipeline can't be executed.
        """
        for pipeline in draft.pipelines:
            if pipeline.mode == "code" and pipeline.entrypoint not in draft.frontend.files:
                raise ValueError(
                    f"pipeline {pipeline.name!r} is mode='code' with entrypoint "
                    f"{pipeline.entrypoint!r}, which is not among the app bundle files "
                    f"{sorted(draft.frontend.files)} — write the pipeline file before submitting"
                )

    def _verify_pipelines(self, draft: AppSpec, *, now: datetime) -> dict[str, PipelineVerdict]:
        """Rehearse every code pipeline through the fire plane's runner before mutation.

        Each declared sample exercises its real params and permits ``ctx.exec`` while
        suppressing durable writes. A pipeline without samples retains the legacy
        ``params={}`` smoke, which may be skipped when its schema requires input.
        Calling ``execute`` directly creates no ledger row.
        """
        runner = self.tracker.pipeline_runner if self.tracker is not None else None
        verdicts: dict[str, PipelineVerdict] = {}
        logged_unwired = False
        for pipeline in draft.pipelines:
            if pipeline.mode != "code":
                verdicts[pipeline.name] = "skipped"
                continue
            if runner is None:
                verdicts[pipeline.name] = "skipped"
                if not logged_unwired:
                    logging.warning(
                        "Apps pipeline runner not wired; skipping submit-time "
                        "verification of code pipeline(s) for app {}",
                        draft.app_id,
                    )
                    logged_unwired = True
                continue

            # A pipeline that declares nothing still gets the legacy params-free
            # smoke — as ONE unlabelled sample rather than a ``None`` standing in
            # for a sample, so the loop body has a single type to reason about.
            # Whether samples were DECLARED is a separate question from what is
            # being run, and only the former decides how a params failure is read.
            declared_samples = bool(pipeline.samples)
            samples = pipeline.samples or [PipelineSample()]
            for index, sample in enumerate(samples, start=1):
                params = sample.params
                sample_name = sample.label or f"sample {index}"
                try:
                    runner.execute(
                        draft, pipeline, params, now=now, dry_run=False, rehearse=True
                    )
                except PipelineExecutionError as exc:
                    # A params-free legacy smoke cannot construct required input, and
                    # submit precedes maintainer workspace binding. Declared samples
                    # are executable contracts, so their parameter failures refuse.
                    if exc.code == "workspace" or (exc.code == "params" and not declared_samples):
                        verdicts[pipeline.name] = "skipped"
                        break
                    raise ValueError(
                        f"pipeline {pipeline.name!r} {sample_name!r} failed verification — "
                        f"{exc} — fix the pipeline and resubmit"
                    ) from exc
            else:
                verdicts[pipeline.name] = "pass"
        return verdicts

    def _warn_on_unscheduled_pipelines(
        self, app_id: str, maintainer: str, pipelines: list[PipelineSpec]
    ) -> None:
        """Log (never raise) the pipelines that end up with no ARMED trigger.

        Honesty, not enforcement: an on-demand pipeline (``trigger_ref``
        left ``None``) is a legitimate shape, but so is the less obvious gap this
        also catches — a policy-capped or already-terminal ``trigger_ref`` that
        ``_arm_pipeline_trigger`` left POINTING AT a trigger that isn't armed on
        THIS maintainer (a stale builder-owned id, or one that lapsed). Checking
        against the maintainer's own armed set (not merely ``is None``) is what
        makes this catch that case too. The same derivation re-runs at read time
        as the ``/system`` payload's top-level ``unscheduled_pipelines`` field
        (``AppsRoutesController._unscheduled_pipelines``) — this is the submit-time
        signal an operator watching logs would see first.
        """
        armed_ids = {t.id for t in self.trigger_store.list(session_id=maintainer, status="armed")}
        unscheduled = [p.name for p in pipelines if p.trigger_ref not in armed_ids]
        if unscheduled:
            logging.warning(
                "app {} submitted with unscheduled pipeline(s) (no armed trigger): {}",
                app_id,
                ", ".join(unscheduled),
            )

    def _arm_pipeline(
        self, pipeline: PipelineSpec, *, maintainer: str, now: datetime
    ) -> PipelineSpec:
        """Arm *pipeline*'s declared wake on the maintainer session.

        Two paths, dispatched off the model (no service-side ``if kind ==``):

        * **PRIMARY — a declared ``schedule``.** The PLATFORM mints a fresh
          trigger on the maintainer from ``schedule.to_trigger_spec`` and stamps
          its id into ``trigger_ref`` (which is platform-owned output). The
          builder does not touch the trigger store at all on this path.
        * **A builder-supplied ``trigger_ref`` with no schedule.** The builder
          armed a trigger on its own session; :meth:`_rehome_pipeline_trigger`
          re-homes it onto the maintainer. The chat-builder path.

        Neither ⇒ an on-demand pipeline, returned unchanged (no armed wake).
        """
        if pipeline.schedule is not None:
            return self._arm_scheduled_trigger(pipeline, maintainer=maintainer, now=now)
        if pipeline.trigger_ref is not None:
            return self._rehome_pipeline_trigger(pipeline, maintainer=maintainer)
        return pipeline

    def _arm_scheduled_trigger(
        self, pipeline: PipelineSpec, *, maintainer: str, now: datetime
    ) -> PipelineSpec:
        """Mint + arm the trigger a declared ``schedule`` describes on the maintainer.

        The schedule owns the trigger's SHAPE (``to_trigger_spec``); the lifecycle
        owns its long-lived expiry (``_SCHEDULE_TRIGGER_TTL``, stamped BEFORE
        admission so the policy's 7-day default never lands) and its admission
        through :class:`TriggerPolicy`. A policy rejection (over cap / cron too
        tight) skips arming this ONE trigger rather than sinking the whole submit
        — the pipeline stays effectively unscheduled, surfaced by
        :meth:`_warn_on_unscheduled_pipelines` and the ``/system`` payload.
        """
        assert pipeline.schedule is not None  # guarded by the caller
        trigger = pipeline.schedule.to_trigger_spec(
            wake_prompt=pipeline.wake_prompt, session_id=maintainer, now=now
        )
        trigger = trigger.model_copy(update={"expires_at": now + _SCHEDULE_TRIGGER_TTL})
        armed_count = len(self.trigger_store.list(session_id=maintainer, status="armed"))
        try:
            trigger = self.trigger_policy.admit(trigger, armed_count)
        except ValueError:
            return pipeline
        self.trigger_store.create(trigger)
        return pipeline.model_copy(update={"trigger_ref": trigger.id})

    def _rehome_pipeline_trigger(
        self, pipeline: PipelineSpec, *, maintainer: str
    ) -> PipelineSpec:
        """Re-home a builder-armed trigger onto the maintainer.

        The secondary arming path (a declared ``schedule`` is the primary one — see
        :meth:`_arm_pipeline`), for a chat-builder that arms its own trigger during
        the build: returns the pipeline with
        ``trigger_ref`` repointed to a fresh maintainer-owned copy. A pipeline
        whose ``trigger_ref`` is unresolvable / already-terminal is returned
        unchanged; a policy rejection skips arming that one trigger rather than
        failing the submit.
        """
        if pipeline.trigger_ref is None:
            return pipeline
        source = self.trigger_store.get(pipeline.trigger_ref)
        if source is None or source.is_terminal:
            return pipeline
        rehomed = source.model_copy(
            update={
                "id": uuid.uuid4().hex,
                "session_id": maintainer,
                "wake_prompt": pipeline.wake_prompt or source.wake_prompt,
                "created_by": "user",
                "status": "armed",
                "fires": 0,
                "last_fired_at": None,
                "last_error": None,
                "provenance": TriggerProvenance(),
            }
        )
        armed_count = len(self.trigger_store.list(session_id=maintainer, status="armed"))
        try:
            rehomed = self.trigger_policy.admit(rehomed, armed_count)
        except ValueError:
            # Over-cap or a too-tight cron — leave the pipeline on-demand rather
            # than sinking the submit; the maintainer keeps the ones that fit.
            return pipeline
        self.trigger_store.create(rehomed)
        self._cancel_trigger(source)
        return pipeline.model_copy(update={"trigger_ref": rehomed.id})

    # -- pause / resume (app == its triggers) ------------------------------

    def pause(self, app_id: str) -> AppSpec | None:
        """Pause the app and every armed trigger it owns."""
        app = self.app_store.get(app_id)
        if app is None:
            return None
        now = self.now_fn()
        for trigger in self.maintainer_triggers(app, status="armed"):
            self._transition_trigger(trigger, "paused")
        if app.status in ("live", "broken"):
            app.transition("paused", now=now)
            self.app_store.save(app)
        return app

    def resume(self, app_id: str) -> AppSpec | None:
        """Resume the app and re-arm every paused trigger it owns."""
        app = self.app_store.get(app_id)
        if app is None:
            return None
        now = self.now_fn()
        for trigger in self.maintainer_triggers(app, status="paused"):
            self._transition_trigger(trigger, "armed")
        if app.status == "paused":
            app.transition("live", now=now)
            self.app_store.save(app)
        return app

    # -- re-arm (operator repair for a live app whose schedules never took) --

    def rearm(self, app: AppSpec, *, seed: bool = False, now: datetime) -> dict[str, object]:
        """Re-mint the missing/dead schedule triggers for a LIVE app; optionally seed.

        The operator repair for the ``app-a2a5f299e0ad`` failure class: an app is
        ``live`` but a declared-schedule pipeline never armed (its ``trigger_ref``
        is ``None``) or its trigger lapsed/was cancelled (``trigger_ref`` no longer
        armed on the maintainer). For each such pipeline this mints + arms a FRESH
        trigger on the maintainer through the SAME seam ``submit`` uses
        (:meth:`_arm_scheduled_trigger` — long-lived TTL before ``TriggerPolicy.admit``)
        and persists the new platform-owned ``trigger_ref``. Pipelines already armed
        are left untouched (``unchanged``); a re-arm that a policy cap rejects stays
        unscheduled (surfaced by the ``/system`` ``unscheduled_pipelines`` field, not
        forced here).

        When *seed* is true, each RE-ARMED pipeline is additionally fired once
        through the fire seam (best-effort; a code fire off-thread, agentic inline)
        so a repaired app doesn't wait for the next tick to refresh. Returns
        ``{armed: [{pipeline, trigger_id}], unchanged: [names], seeded: [names]}``.
        The caller (the route) has already checked the app is live.

        Serialized per-app (``_rearm_locks``): the whole read-arm-save runs under
        one lock and RE-READS the manifest inside it, so a concurrent re-arm that
        already committed is visible here rather than clobbered.
        """
        with self._rearm_lock_for(app.app_id):
            return self._rearm_locked(app, seed=seed, now=now)

    def _rearm_lock_for(self, app_id: str) -> threading.Lock:
        """The per-app re-arm lock, created on first use behind the guard mutex."""
        with self._rearm_locks_guard:
            return self._rearm_locks.setdefault(app_id, threading.Lock())

    def _rearm_locked(self, app: AppSpec, *, seed: bool, now: datetime) -> dict[str, object]:
        """The locked body of :meth:`rearm` — re-reads the manifest, then arms + seeds."""
        # Re-read under the lock: a racing re-arm that already committed a fresh
        # trigger_ref must be visible, or this pass re-mints on top of it and orphans
        # the first's trigger (the lost-update the lock exists to prevent).
        app = self.app_store.get(app.app_id) or app
        maintainer = app.maintainer_session_id
        if maintainer is None:
            return {"armed": [], "unchanged": [], "seeded": []}
        armed_ids = {t.id for t in self.trigger_store.list(session_id=maintainer, status="armed")}
        armed_out: list[dict[str, str]] = []
        unchanged: list[str] = []
        rearmed: list[PipelineSpec] = []
        new_pipelines: list[PipelineSpec] = []
        for pipeline in app.pipelines:
            if pipeline.schedule is None:
                new_pipelines.append(pipeline)
                continue
            if pipeline.trigger_ref is not None and pipeline.trigger_ref in armed_ids:
                new_pipelines.append(pipeline)
                unchanged.append(pipeline.name)
                continue
            # Cancel the STALE referenced trigger before minting its replacement: a
            # non-None trigger_ref that isn't currently armed is typically a PAUSED
            # trigger (paused via the generic triggers route while the app stayed
            # live). Left alone, a later app pause/resume cycle re-arms BOTH it and
            # the replacement → double-fire. Mirrors submit's cancel-before-rearm,
            # scoped to the one stale trigger (never the maintainer's whole set).
            self._cancel_stale_trigger(pipeline.trigger_ref)
            armed = self._arm_scheduled_trigger(pipeline, maintainer=maintainer, now=now)
            new_pipelines.append(armed)
            if armed.trigger_ref is not None and armed.trigger_ref != pipeline.trigger_ref:
                armed_out.append({"pipeline": pipeline.name, "trigger_id": armed.trigger_ref})
                rearmed.append(armed)
            # else: a policy cap rejected it — left effectively unscheduled, not forced.
        if armed_out:
            app = app.model_copy(update={"pipelines": new_pipelines, "updated_at": now})
            self.app_store.save(app)
        seeded: list[str] = []
        if seed:
            for pipeline in rearmed:
                self._seed_pipeline(app, pipeline)
                seeded.append(pipeline.name)
        return {"armed": armed_out, "unchanged": unchanged, "seeded": seeded}

    def _cancel_stale_trigger(self, trigger_ref: str | None) -> None:
        """Cancel a non-armed, non-terminal referenced trigger before re-arming it."""
        if trigger_ref is None:
            return
        stale = self.trigger_store.get(trigger_ref)
        if stale is not None and not stale.is_terminal:
            self._cancel_trigger(stale)

    # -- rollback ----------------------------------------------------------

    def rollback(self, app_id: str, version: int) -> AppSpec | None:
        """Repoint the app to an earlier version's design as a NEW version.

        History is append-only: rollback never rewrites a snapshot —
        it copies the target version's design forward as ``latest + 1`` (keeping
        the live maintainer/owner/status) and records a new ``user`` snapshot.
        Returns ``None`` if the app or the target version is absent.
        """
        app = self.app_store.get(app_id)
        if app is None:
            return None
        snapshot = self.app_store.get_version(app_id, version)
        if snapshot is None:
            return None
        now = self.now_fn()
        next_version = self.app_store.latest_version(app_id) + 1
        rolled = snapshot.spec.model_copy(
            update={
                "version": next_version,
                "owner_session_id": app.owner_session_id,
                "maintainer_session_id": app.maintainer_session_id,
                "status": app.status,
                "updated_at": now,
            }
        )
        # The reverse diff: what reverting the CURRENT live app to the target
        # snapshot changes. ``verification`` stays None — the snapshot's pipelines
        # were verified when originally submitted, and rollback re-arms without
        # re-executing them (no fresh dry run happens here).
        summary = AppVersionSummary.compute(app, rolled)
        self.app_store.save(rolled)
        self.app_store.save_version(
            AppVersion(
                app_id=app_id,
                version=next_version,
                spec=rolled,
                author="user",
                note=f"rollback to v{version}",
                summary=summary,
            )
        )
        self._emit_updated(app, next_version, "user")
        return rolled

    # -- archive -----------------------------------------------------------

    def archive(self, app_id: str) -> AppSpec | None:
        """Archive the app (absorbing) and cancel every trigger it still owns."""
        app = self.app_store.get(app_id)
        if app is None:
            return None
        now = self.now_fn()
        for trigger in self.maintainer_triggers(app):
            if not trigger.is_terminal:
                self._transition_trigger(trigger, "cancelled")
        if app.status != "archived":
            app.transition("archived", now=now)
            self.app_store.save(app)
        return app

    # -- trigger helpers ---------------------------------------------------

    def maintainer_triggers(self, app: AppSpec, *, status: str | None = None) -> list[TriggerSpec]:
        """Every trigger owned by the app's maintainer session (optionally filtered).

        Public: the routes controller (which already holds this lifecycle) delegates
        here rather than re-deriving the same ``trigger_store.list(session_id=...)``
        query — the single home for "which triggers belong to this app".
        """
        if app.maintainer_session_id is None:
            return []
        return self.trigger_store.list(session_id=app.maintainer_session_id, status=status)

    def _transition_trigger(self, trigger: TriggerSpec, to: str) -> None:
        """Transition + persist a trigger, no-op on an illegal (already-terminal) move."""
        try:
            trigger.transition(to)
        except ValueError:
            return
        self._store_update(trigger)

    def _cancel_trigger(self, trigger: TriggerSpec) -> None:
        """Cancel the builder-owned source trigger so it can't fire into a dead session."""
        self._transition_trigger(trigger, "cancelled")

    def _store_update(self, trigger: TriggerSpec) -> None:
        try:
            self.trigger_store.update(trigger)
        except KeyError:  # pragma: no cover - trigger removed concurrently
            pass

    # -- pipeline-failure policy --

    def handle_pipeline_failure(self, app: AppSpec, issue: PipelineIssue) -> None:
        """React to a pipeline needing attention per ``policies.on_pipeline_failure``.

        Called by :class:`AppPipelineRunTracker` for the two reasons an autonomous
        pipeline needs a human or an agent: a run that closed ``failed``, and a run
        that SUCCEEDED while regressing a collection earlier runs wrote
        (:class:`PipelineIssue` carries which, and owns the prose for each). Three
        declarative reactions wired onto EXISTING seams (no new hook engine):
        ``repair`` starts a repair run on the maintainer via the same
        run-starter that kicks off the builder; ``pause`` pauses the app + its
        triggers; ``notify`` emits an ``app_issue`` event and does nothing else.

        The policy is honoured IDENTICALLY for both kinds — an operator who
        declared ``pause`` for a broken pipeline gets a pause for a pipeline that
        silently stopped writing, because that is the same declaration and
        second-guessing it here would make the policy mean two things. The ONE
        addition is the event: an issue that leaves no other user-visible trace
        emits ``app_issue`` on top of whatever the policy did (see
        :attr:`PipelineIssue.needs_own_event`), so ``repair``/``pause`` can never
        act on something the user was never told about.

        Best-effort — a raising reaction must not break the session-end hook chain
        the tracker rides.
        """
        policy = app.policies.on_pipeline_failure
        if policy == "repair":
            self._start_repair_run(app, issue)
        elif policy == "pause":
            self.pause(app.app_id)
        elif policy == "invalidate":
            app.transition("broken", now=self.now_fn())
            self.app_store.save(app)
            self._emit_app_issue(app, issue)
            return
        if policy == "notify" or issue.needs_own_event:
            self._emit_app_issue(app, issue)

    def _start_repair_run(self, app: AppSpec, issue: PipelineIssue) -> None:
        """Start a repair run on the maintainer (no-op + warning when unwired)."""
        if app.maintainer_session_id is None:
            return
        if self.run_starter is None:
            logging.warning(
                "Apps run-starter not configured; repair run for app {} not started.",
                app.app_id,
            )
            return
        self.run_starter.start_app_run(
            app.maintainer_session_id, self._repair_prompt(app, issue)
        )

    @staticmethod
    def _repair_prompt(app: AppSpec, issue: PipelineIssue) -> str:
        """Compose the repair kick-off per the ``app-repair`` AgentDef contract.

        Three concerns, three homes (see :meth:`PipelineIssue.repair_brief`): the
        issue states the FACTS, this names the SURFACES, and the AgentDef carries
        the durable HOW. A repair wakes in a FRESH context that holds no memory of
        the app and no staged files, so a prompt that names only the problem burns
        its first turns rediscovering its own toolbox — naming the surfaces here is
        what makes the wake productive. It stays a POINTER, not a manual: the
        depth (hypothesis set, ordering, verification bar) belongs to the AgentDef,
        which is also reached when a USER reports the same symptom and no prompt
        like this one exists.

        ``get_app``/``stage`` leads deliberately: staging is ephemeral and is
        normally gone by the time a repair fires, and it is the ONLY way back to a
        ``mode="code"`` pipeline's source — without it the agent cannot edit the
        very thing that broke.
        """
        return (
            f'App {app.app_id} ("{app.title}") needs repair.\n\n'
            f"{issue.repair_brief()}\n\n"
            "Delegate to the `app-repair` agent (spawn_agent). Its own instructions "
            "carry the full repair loop — follow those rather than improvising. The "
            "surfaces it holds, and the order they are normally used in:\n"
            "- `get_app` (operation `get`) — the LIVE manifest: status, versions, "
            "collections and their doc counts, every pipeline's mode/schedule/last "
            "run. Start here; a repair wakes in a fresh context and remembers "
            "nothing, so this is ground truth, not the transcript.\n"
            "- `get_app` (operation `stage`) — re-materializes the WHOLE stored "
            "bundle to disk, INCLUDING every `mode=\"code\"` pipeline SOURCE. The "
            "staging directory is ephemeral and is probably already gone, so stage "
            "before reading or editing any file; this is the only way to recover the "
            "pipeline source.\n"
            "- `run_pipeline(pipeline=<name>, dry_run=true)` — re-runs the real "
            "entrypoint against the real ctx with no durable write. Reproduce before "
            "editing, and confirm after.\n"
            "- `app_data` — query the app's own collections to see what IS and ISN'T "
            "there.\n"
            "- `submit_app` (the SAME app_id) — ship the corrected bundle as a new "
            "version.\n\n"
            "Apply the SMALLEST fix, and ship a new version only if the frontend/"
            "schema/pipeline changed (a data-only fix does not bump the version)."
        )

    def _emit_app_issue(self, app: AppSpec, issue: PipelineIssue) -> None:
        """Emit an ``app_issue`` notification on the maintainer session (best-effort).

        The payload keeps its shipped ``{app_id, error}`` shape — ``error`` stays
        the human line, verbatim for a failure — and gains the issue's structured
        fields additively, so an existing reader is unaffected and a new one can
        render which pipeline and which collections without parsing prose.
        """
        if app.maintainer_session_id is None:
            return
        self.sessions.append_event(
            app.maintainer_session_id,
            {
                "type": "app_issue",
                "payload": {
                    "app_id": app.app_id,
                    "error": issue.describe(),
                    "kind": issue.kind,
                    "pipeline": issue.pipeline_name,
                    "collections": issue.collections,
                },
            },
        )

    # -- events ------------------------------------------------------------

    def _emit_updated(self, app: AppSpec, version: int, author: AppVersionAuthor) -> None:
        """Emit ``app_updated`` on the maintainer session (best-effort)."""
        if app.maintainer_session_id is None:
            return
        self.sessions.append_event(
            app.maintainer_session_id,
            {
                "type": "app_updated",
                "payload": AppUpdatedEvent(
                    app_id=app.app_id, version=version, author=author
                ).model_dump(),
            },
        )


class RuntimeSessionBackend:
    """Adapts a ``SessionRuntime`` to the :class:`AppSessionBackend` Protocol.

    Thin translation only — every method is a runtime method. Kept out of
    ``backend.py`` so the lifecycle's whole session dependency stays in the
    package that owns it.
    """

    def __init__(self, runtime: object) -> None:
        """Capture the runtime whose session surface this exposes."""
        self._runtime = runtime

    def create_session(self) -> str:
        """Mint a new empty session.

        ``resolve_session`` with no tag is the runtime's mint-a-session seam;
        the app tags it separately because the two app sessions that use this
        (builder, maintainer) pick their session by branch, not by tag lookup.
        """
        return self._runtime.resolve_session()  # type: ignore[attr-defined]

    def tag_session(self, session_id: str, tag: str) -> None:
        """Attach a resolution tag to a session."""
        self._runtime.tag_session(session_id, tag)  # type: ignore[attr-defined]

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None:
        """Write a context event onto a session."""
        self._runtime.append_context_event(session_id, context)  # type: ignore[attr-defined]

    def append_event(self, session_id: str, event: dict[str, object]) -> None:
        """Append a transcript event onto a session."""
        self._runtime.append_event(session_id, event)  # type: ignore[attr-defined]

    def is_terminated(self, session_id: str) -> bool:
        """Whether the session was permanently terminated."""
        return self._runtime.is_terminated(session_id)  # type: ignore[attr-defined]

    def tags_for_session(self, session_id: str) -> list[str]:
        """The server-stamped tags on *session_id* (the store's own read).

        This is the PRODUCTION half of ``submit``'s tag tier: without it the
        deployment resolves no tags and the widened membership test is dead code
        — the fix would pass every test and change nothing for the operator whose
        composer session is refused. Reached through the runtime's session store,
        the same object ``backend.py`` reads tags from elsewhere.
        """
        return list(
            self._runtime.session_store.tags_for_session(session_id)  # type: ignore[attr-defined]
        )


__all__ = [
    "APPS_CAPABILITY",
    "MAINTAINER_TAG_PREFIX",
    "AppRunStarter",
    "AppSessionBackend",
    "AppLifecycle",
    "RuntimeSessionBackend",
]
