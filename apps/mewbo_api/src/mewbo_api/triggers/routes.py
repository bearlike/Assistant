"""REST contract for the reverse-invocation trigger subsystem (WP3).

The FROZEN v1 surface the console (WP5) is already built against — see
``apps/mewbo_console/src/api/triggers.ts``. Wire shape decisions that must not
drift:

* **DTO nests kind-specific config under ``args``.** The base
  :class:`~mewbo_core.triggers.spec.TriggerSpec` fields stay top-level
  (snake_case, via ``model_dump(mode="json")``); everything a concrete kind adds
  (``cron``/``at``/``repo``/``number``/``events``/``hmac_header``/``secret``)
  folds into ``args: {…}`` plus a computed ``next_fire_at``.
* **Webhook secret travels ONE direction.** It is included in ``args`` only on
  the responses that return a single trigger the operator explicitly addressed
  by id (the 201 create + the PATCH) so they can build the capability URL; it is
  stripped from every list response. This is an authenticated operator API
  (``X-API-KEY``) — the secret is the webhook's capability, not user PII.
* **The webhook fire route is UNauthenticated by design** — the capability URL
  (``/triggers/hook/<id>/<secret>``) IS the auth. Unknown id / wrong kind / not
  armed / bad secret all return the SAME uniform 404 (no oracle distinguishing
  a real id from a forged one).
* **A terminated session rejects arming with the shared 410 envelope** (code
  ``session_terminated``) so the console's terminated-sentinel path fires.

Paradigm (hardening): this module holds **no mutable module-level
wiring**. :class:`TriggerRoutesController` is the atomic class that owns the
injected collaborators (service/store/policy/runtime) as fields and
every serialization/existence/domain helper as a method — mirroring
``vcs_pickup.VcsPickupService``. The Flask-RESTX Resource classes are thin HTTP
adapters that receive the one controller instance by DEPENDENCY INJECTION via
``resource_class_kwargs`` (``init_trigger_routes``), so the request path reads no
module global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from flask import request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.triggers.spec import TriggerSpec, TriggerStatus, parse_trigger
from pydantic import ValidationError

from mewbo_api.auth.guard_registry import guard
from mewbo_api.responses import ApiResponseKit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mewbo_core.session_runtime import SessionRuntime
    from mewbo_core.triggers.policy import TriggerPolicy
    from mewbo_core.triggers.store import TriggerStoreBase

    from mewbo_api.triggers.service import TriggerService

logging = get_logger(name="api.triggers.routes")

triggers_ns = Namespace("triggers", description="Reverse-invocation trigger management")

# ---------------------------------------------------------------------------
# Controller — the atomic class owning collaborators + every helper
# ---------------------------------------------------------------------------


class TriggerRoutesController:
    """Owns the trigger REST behavior over its injected collaborators.

    Atomic feature class (the ``VcsPickupService`` idiom): the collaborators are
    its state and the serialization / existence / arm-list-mutate-fire pipeline
    are its methods. One instance is constructed by :func:`init_trigger_routes`
    and handed to every Resource via ``resource_class_kwargs`` — there is no
    module-level mutable wiring, and a test points the one registered controller
    at fresh stores by reassigning its fields.
    """

    # The TriggerSpec common fields stay top-level in the DTO; everything else a
    # concrete kind declares folds into ``args``. Computed once at class load.
    BASE_FIELDS: frozenset[str] = frozenset(TriggerSpec.model_fields)

    # Server-owned fields a client may never set on an arm request. Everything
    # else in the body (base arm fields + kind config) is handed to parse_trigger.
    CLIENT_OWNED_DENY: frozenset[str] = frozenset(
        {
            "id",
            "session_id",
            "status",
            "fires",
            "created_by",
            "created_at",
            "last_fired_at",
            "last_error",
            "provenance",
            "secret",
            "next_fire_at",
            "args",
        }
    )

    VALID_STATUSES: frozenset[str] = frozenset(
        {"armed", "paused", "completed", "failed", "cancelled", "expired"}
    )

    SUMMARY_MAX = 120

    def __init__(
        self,
        *,
        service: TriggerService,
        store: TriggerStoreBase,
        policy: TriggerPolicy,
        runtime: SessionRuntime,
    ) -> None:
        """Capture the injected collaborators as instance state.

        No auth guard is injected: every Resource declares its own requirement
        with ``@guard.requires``, which resolves the live ``AuthKit`` through
        ``guard_registry`` at request time.
        """
        self.service = service
        self.store = store
        self.policy = policy
        self.runtime = runtime

    # -- serialization + error helpers -------------------------------------

    @staticmethod
    def _now() -> datetime:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _dto(
        self, trigger: TriggerSpec, *, now: datetime, include_secret: bool = False
    ) -> dict[str, Any]:
        """Serialize a trigger to the frozen wire DTO (base fields + nested ``args``)."""
        dump = trigger.model_dump(mode="json")
        args = {k: v for k, v in dump.items() if k not in self.BASE_FIELDS}
        if not include_secret:
            args.pop("secret", None)
        out = {k: v for k, v in dump.items() if k in self.BASE_FIELDS}
        out["args"] = args
        out["next_fire_at"] = self._iso(trigger.next_fire_at(now))
        return out

    @staticmethod
    def _err(code: Any, reason: str, status: int) -> tuple[dict, int]:
        return {"error": {"code": code, "reason": reason}}, status

    def _not_found_trigger(self) -> tuple[dict, int]:
        return self._err(404, "Trigger not found.", 404)

    def _not_found_session(self, session_id: str) -> tuple[dict, int]:
        return self._err(404, f"No session with id {session_id!r}.", 404)

    def _session_exists(self, session_id: str) -> bool:
        return session_id in self.runtime.session_store.list_sessions()

    # -- domain operations (called by the thin Resource adapters) ----------

    def list_session_triggers(self, session_id: str) -> tuple[dict, int]:
        """List every trigger owned by one session."""
        if not self._session_exists(session_id):
            return self._not_found_session(session_id)
        now = self._now()
        triggers = self.store.list(session_id=session_id)
        return {"triggers": [self._dto(t, now=now) for t in triggers]}, 200

    def arm(self, session_id: str, body: Any) -> tuple[dict, int]:
        """Arm a new trigger for a session (validate → admit → persist → event)."""
        if not self._session_exists(session_id):
            return self._not_found_session(session_id)
        if self.runtime.is_terminated(session_id):
            return ApiResponseKit.terminated_response()
        if not isinstance(body, dict):
            return self._err("validation", "Request body must be a JSON object.", 400)

        raw: dict[str, Any] = {}
        inbound_args = body.get("args")
        if isinstance(inbound_args, dict):
            raw.update(
                {k: v for k, v in inbound_args.items() if k not in self.CLIENT_OWNED_DENY}
            )
        raw.update(
            {k: v for k, v in body.items() if k != "args" and k not in self.CLIENT_OWNED_DENY}
        )
        raw["session_id"] = session_id
        raw["created_by"] = "user"
        try:
            spec = parse_trigger(raw)
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)

        armed_count = len(self.store.list(session_id=session_id, status="armed"))
        try:
            spec = self.policy.admit(spec, armed_count)
        except ValueError as exc:
            return self._err("policy", str(exc), 400)

        # Snapshot the arming caller's authority onto the record so a LATER,
        # out-of-band fire re-engages under the same role ceiling instead of
        # ambient full power. ``authority_from_principal`` returns ``None`` for an
        # admin/unauthenticated/auth-disabled caller, so the field stays unset and
        # the fire path keeps today's behavior — byte-identical when auth is off.
        from mewbo_api.auth import current_principal
        from mewbo_api.auth.session_scope import authority_from_principal

        spec.authority = authority_from_principal(current_principal())

        self.store.create(spec)
        summary = f"Armed {spec.kind} trigger: {spec.wake_prompt[: self.SUMMARY_MAX]}"
        self.runtime.append_event(
            session_id,
            {
                "type": "trigger_armed",
                "payload": {
                    "trigger_id": spec.id,
                    "kind": spec.kind,
                    "summary": summary,
                },
            },
        )
        return self._dto(spec, now=self._now(), include_secret=True), 201

    def list_collection(
        self,
        *,
        session_id: str | None,
        kind: str | None,
        raw_status: str | None,
        limit: int | None,
    ) -> tuple[dict, int]:
        """List triggers across sessions with optional session/kind/status/limit filters."""
        status = cast(
            "TriggerStatus | None", raw_status if raw_status in self.VALID_STATUSES else None
        )
        triggers = self.store.list(
            session_id=session_id or None,
            kind=kind or None,
            status=status,
            limit=limit,
        )
        now = self._now()
        return {"triggers": [self._dto(t, now=now) for t in triggers]}, 200

    def pause_or_resume(self, trigger_id: str, body: Any) -> tuple[dict, int]:
        """Pause (``paused``) or resume (``armed``) a single trigger."""
        trigger = self.store.get(trigger_id)
        if trigger is None:
            return self._not_found_trigger()
        new_status = body.get("status") if isinstance(body, dict) else None
        if new_status not in ("armed", "paused"):
            return self._err("validation", "status must be 'armed' or 'paused'.", 400)
        if trigger.is_terminal:
            return self._err("conflict", f"cannot change a {trigger.status} trigger.", 409)
        trigger.transition(new_status)
        self.store.update(trigger)
        return self._dto(trigger, now=self._now(), include_secret=True), 200

    def cancel(self, trigger_id: str) -> tuple[dict, int]:
        """Cancel a trigger (idempotent — an already-terminal one returns its status)."""
        trigger = self.store.get(trigger_id)
        if trigger is None:
            return self._not_found_trigger()
        if not trigger.is_terminal:
            trigger.transition("cancelled")
            self.store.update(trigger)
        return {"id": trigger.id, "status": trigger.status}, 200

    def fire_hook(
        self, trigger_id: str, secret: str, headers: Any, raw_body: bytes
    ) -> tuple[dict, int]:
        """Verify the capability secret (+ optional HMAC) and fire a webhook trigger."""
        trigger = self.store.get(trigger_id)
        # Uniform 404 across every rejection reason — no oracle that lets a
        # caller distinguish "real id, wrong secret" from "no such id".
        if trigger is None or trigger.kind != "webhook" or trigger.status != "armed":
            return self._not_found_trigger()
        # spec.verify is constant-time (hmac.compare_digest) and validates the
        # optional HMAC over the RAW body — verify BEFORE truncating.
        if not trigger.verify(secret, headers, raw_body):
            return self._not_found_trigger()
        max_bytes = self.policy.webhook_payload_max_bytes
        truncated = raw_body[:max_bytes]
        payload = {
            "body": truncated.decode("utf-8", errors="replace"),
            "truncated": len(raw_body) > max_bytes,
        }
        self.service.fire(trigger, payload=payload)
        return {"accepted": True}, 202


# ---------------------------------------------------------------------------
# Flask-RESTX doc models (example= drives the Scalar sample bodies)
# ---------------------------------------------------------------------------

trigger_dto_model = triggers_ns.model(
    "Trigger",
    {
        "id": fields.String(example="3f8c1e9a2b7d4c05"),
        "session_id": fields.String(example="9e2d47c1a0b34f12"),
        "kind": fields.String(
            example="time.cron",
            description="One of time.at, time.cron, ci.workflow, forge.pr, webhook.",
        ),
        "status": fields.String(
            example="armed",
            description="armed, paused, completed, failed, cancelled, or expired.",
        ),
        "wake_prompt": fields.String(example="Check the nightly deploy and summarize failures."),
        "action": fields.String(
            example="message",
            description="message re-engages the session; start opens a fresh turn.",
        ),
        "args": fields.Raw(
            example={"cron": "0 9 * * *"},
            description="Kind config (cron/at/repo/number/events/hmac_header; secret on create).",
        ),
        "fires": fields.Integer(example=0),
        "max_fires": fields.Integer(example=None),
        "expires_at": fields.String(example="2026-07-20T09:00:00+00:00"),
        "next_fire_at": fields.String(example="2026-07-14T09:00:00+00:00"),
        "created_at": fields.String(example="2026-07-13T18:24:10.882001+00:00"),
        "created_by": fields.String(
            example="user",
            description="agent (via schedule_trigger) or user (console).",
        ),
        "provenance": fields.Raw(example={"agent_id": None, "step": None}),
        "last_fired_at": fields.String(example=None),
        "last_error": fields.String(example=None),
    },
)

trigger_list_model = triggers_ns.model(
    "TriggerList",
    {"triggers": fields.List(fields.Nested(trigger_dto_model))},
)

create_trigger_model = triggers_ns.model(
    "CreateTriggerRequest",
    {
        "kind": fields.String(required=True, example="time.cron"),
        "wake_prompt": fields.String(required=True, example="Summarize the nightly deploy."),
        "action": fields.String(example="message", description="message (default) or start."),
        "max_fires": fields.Integer(
            example=None, description="Cap on fires; omit for unlimited (policy-bounded)."
        ),
        "expires_at": fields.String(
            example=None, description="ISO deadline; omit for the deployment default."
        ),
        "cron": fields.String(
            example="0 9 * * *", description="kind=time.cron: 5-field cron expression."
        ),
        "at": fields.String(example=None, description="kind=time.at: ISO wall-clock instant."),
        "repo": fields.String(
            example="acme/widgets", description="kind=ci.workflow/forge.pr: owner/name."
        ),
        "workflow": fields.String(example="ci.yml", description="kind=ci.workflow: workflow name."),
        "run_id": fields.Integer(example=None, description="kind=ci.workflow: a specific run id."),
        "ref": fields.String(example="main", description="kind=ci.workflow: restrict to a ref."),
        "conclusion_filter": fields.List(fields.String, example=["success", "failure"]),
        "number": fields.Integer(example=42, description="kind=forge.pr: the PR number."),
        "events": fields.List(
            fields.String, example=["merged"], description="kind=forge.pr: which PR events fire."
        ),
        "hmac_header": fields.String(
            example=None, description="kind=webhook: optional HMAC signature header."
        ),
    },
)

cancel_response_model = triggers_ns.model(
    "TriggerCancelResponse",
    {"id": fields.String(example="3f8c1e9a2b7d4c05"), "status": fields.String(example="cancelled")},
)

hook_response_model = triggers_ns.model(
    "TriggerHookResponse",
    {"accepted": fields.Boolean(example=True)},
)


# ---------------------------------------------------------------------------
# Resource adapters — thin HTTP boundary; the injected controller does the work
# ---------------------------------------------------------------------------


class _ControllerResource(Resource):
    """Base Resource that receives the one controller via ``resource_class_kwargs``.

    Flask-RESTX passes the ``Api`` as the first positional arg to a Resource
    constructor; ``controller`` rides alongside it as an injected keyword so no
    Resource ever reaches into module scope for its collaborators.
    """

    def __init__(
        self, api: Any = None, *args: Any, controller: TriggerRoutesController, **kwargs: Any
    ) -> None:
        super().__init__(api, *args, **kwargs)
        self.controller = controller


class SessionTriggers(_ControllerResource):
    """Arm a new trigger on a session, or list that session's triggers."""

    @triggers_ns.doc(security="apikey")
    @triggers_ns.response(200, "The session's triggers.", trigger_list_model)
    @guard.requires("triggers.read")
    def get(self, session_id: str) -> tuple[dict, int]:
        """List every trigger owned by one session."""
        return self.controller.list_session_triggers(session_id)

    @triggers_ns.doc(security="apikey")
    @triggers_ns.expect(create_trigger_model)
    @triggers_ns.response(201, "Trigger armed.", trigger_dto_model)
    @guard.requires("triggers.arm")
    def post(self, session_id: str) -> tuple[dict, int]:
        """Arm a new trigger for a session.

        The body is ``{kind, wake_prompt, action?, max_fires?, expires_at?}``
        plus the kind's own config fields (spread flat, or nested under
        ``args``). Server-owned fields (id/status/fires/secret/…) in the body
        are ignored. Returns the created trigger (201) with the webhook secret
        included in ``args`` when applicable.
        """
        return self.controller.arm(session_id, request.get_json(silent=True) or {})


class TriggersCollection(_ControllerResource):
    """List triggers across sessions, filtered by session/kind/status."""

    @triggers_ns.doc(
        security="apikey",
        params={
            "session_id": "Only triggers owned by this session.",
            "kind": "Only triggers of this kind.",
            "status": "Only triggers in this status.",
            "limit": "Cap the number returned.",
        },
    )
    @triggers_ns.response(200, "Matching triggers.", trigger_list_model)
    @guard.requires("triggers.read")
    def get(self) -> tuple[dict, int]:
        """List triggers with optional session/kind/status/limit filters."""
        return self.controller.list_collection(
            session_id=request.args.get("session_id"),
            kind=request.args.get("kind"),
            raw_status=request.args.get("status"),
            limit=request.args.get("limit", type=int),
        )


class TriggerItem(_ControllerResource):
    """Pause/resume (PATCH) or cancel (DELETE) a single trigger."""

    @triggers_ns.doc(security="apikey")
    @triggers_ns.response(200, "Updated trigger.", trigger_dto_model)
    @guard.requires("triggers.manage")
    def patch(self, trigger_id: str) -> tuple[dict, int]:
        """Pause (``status:"paused"``) or resume (``status:"armed"``) a trigger.

        409 when the trigger is already terminal (a completed/failed/cancelled/
        expired trigger can't be re-armed or paused). Returns the trigger with
        its webhook secret so an operator can re-fetch the capability URL.
        """
        return self.controller.pause_or_resume(trigger_id, request.get_json(silent=True) or {})

    @triggers_ns.doc(security="apikey")
    @triggers_ns.response(200, "Trigger cancelled (idempotent).", cancel_response_model)
    @guard.requires("triggers.manage")
    def delete(self, trigger_id: str) -> tuple[dict, int]:
        """Cancel a trigger (idempotent).

        A repeat call, or an already-terminal trigger, returns 200 with the
        trigger's current status.
        """
        return self.controller.cancel(trigger_id)


class TriggerHook(_ControllerResource):
    """Inbound webhook that fires a ``webhook`` trigger by capability URL."""

    @triggers_ns.doc(
        security=None,
        description=(
            "Fire a webhook trigger. No API key — the capability URL (id + "
            "secret) is the sole credential. Unknown id, wrong kind, not-armed, "
            "or bad secret all return a uniform 404."
        ),
    )
    @triggers_ns.response(202, "Accepted; trigger fired.", hook_response_model)
    @guard.public(
        "capability-URL webhook; the unguessable secret in the path is the proof, "
        "not a principal"
    )
    def post(self, trigger_id: str, secret: str) -> tuple[dict, int]:
        """Verify the capability secret (+ optional HMAC) and fire the trigger."""
        body = request.get_data(cache=False) or b""
        return self.controller.fire_hook(trigger_id, secret, request.headers, body)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

# The single DI handle: construction result of ``init_trigger_routes``. The
# request path NEVER reads it (Resources receive the controller by injection);
# it exists so the composition root holds the instance and a route test can
# point the one registered controller at fresh stores by reassigning its fields.
_controller: TriggerRoutesController | None = None


def init_trigger_routes(
    api: Any,
    *,
    service: TriggerService,
    store: TriggerStoreBase,
    policy: TriggerPolicy,
    runtime: SessionRuntime,
) -> None:
    """Build the controller, DI it into the Resources, and register (once, at startup)."""
    global _controller  # noqa: PLW0603 - single composition-root handle, set once
    _controller = TriggerRoutesController(
        service=service,
        store=store,
        policy=policy,
        runtime=runtime,
    )
    injected = {"resource_class_kwargs": {"controller": _controller}}
    triggers_ns.add_resource(
        SessionTriggers, "/sessions/<string:session_id>/triggers", **injected
    )
    triggers_ns.add_resource(TriggersCollection, "/triggers", **injected)
    triggers_ns.add_resource(TriggerItem, "/triggers/<string:trigger_id>", **injected)
    triggers_ns.add_resource(
        TriggerHook, "/triggers/hook/<string:trigger_id>/<string:secret>", **injected
    )
    api.add_namespace(triggers_ns, path="/api")


__all__ = ["TriggerRoutesController", "triggers_ns", "init_trigger_routes"]
