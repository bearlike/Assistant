"""SCIM 2.0 provisioning surface — ``/api/scim/v2/*``.

Enterprise IdPs (Entra, Okta, Keycloak) push ``User``/``Group`` resources here
and deprovision them on offboarding. Wire shape decisions that must not drift:

* **Every response is ``application/scim+json``, ``ScimError`` shape on
  failure — never the app's ``{"error": {code, reason, retryable}}`` envelope.**
  A SCIM client parses ``status``/``detail``/``scimType``, not this app's
  conventions; mixing the two shapes on one surface breaks every IdP's error
  handling non-deterministically depending on which route failed.
* **Auth is a bearer secret, not ``X-API-Key``.** ``Authorization: Bearer
  <api.auth.scim.secret>``, constant-time compared. This is a SEPARATE
  credential from the operator's API key on purpose: it is the one thing an
  IdP admin pastes into a third-party console, so it must be independently
  rotatable/revocable without touching the operator's own key.
* **Filter support is deliberately narrow: ``userName eq "..."`` and
  ``externalId eq "..."`` for Users, ``displayName eq "..."`` for Groups** —
  exactly what lookup-before-create IdP sync actually sends. Anything else is
  a 501, not a silent empty result (an empty result reads as "no match, safe
  to create a duplicate" to a syncing IdP, which is worse than an honest
  "I don't support that").
* **Group membership is durable, and a pushed member id is still validated
  first.** ``_resolve_members`` checks each id against the user store — an
  unknown one (a user the IdP has not pushed yet, a stale id) is logged and
  SKIPPED, never a batch failure, because IdPs retry membership syncs
  aggressively and one bad id must not block the rest of a roster. What
  survives that filter is persisted through ``TeamStoreBase``, so a Group
  response reports its real roster.
* **Groups are matched by ``externalId`` before slug.** The slug is derived
  from ``displayName``, so a slug-only match treats a group RENAMED at the IdP
  as a new one and forks a duplicate team. See :meth:`_match_group`.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, jsonify, request
from mewbo_core.common import get_logger
from mewbo_iam import (
    AuthAuditStoreBase,
    AuthSettings,
    ExternalSubject,
    ScimDeprovisionedEvent,
    ScimProvisionedEvent,
    TeamRecord,
    TeamStoreBase,
    UserRecord,
    UserStoreBase,
)
from mewbo_iam.scim import (
    SCIM_ISSUER,
    ScimError,
    ScimGroup,
    ScimGroupMember,
    ScimGroupMemberDelta,
    ScimListResponse,
    ScimPatchOp,
    ScimUnsupportedPatchError,
    ScimUser,
)
from pydantic import ValidationError

logging = get_logger(name="api.scim")

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 200

# The one filter shape this subset understands: ``attr eq "value"``.
_FILTER_RE = re.compile(r'^\s*(\w+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


def _utcnow() -> datetime:
    """Default clock — a tz-aware UTC now (injected so tests can pin it)."""
    return datetime.now(timezone.utc)


class ScimFilterUnsupportedError(ValueError):
    """A filter expression outside the ``attr eq "value"`` subset was sent."""

    def __init__(self, expr: str) -> None:
        """Capture the offending filter expression for the caller's 501 detail."""
        self.expr = expr
        super().__init__(f"unsupported SCIM filter: {expr!r}")


class ScimRoutesController:
    """Owns the SCIM 2.0 REST behavior over its injected collaborators.

    Atomic feature class (the ``TriggerRoutesController``/``VcsPickupService``
    idiom): every collaborator is a FIELD, every request-shaping/domain rule is
    a METHOD, and the Blueprint handlers built by :func:`register` are thin
    adapters constructed once and closing over this ONE instance — no
    module-level mutable wiring, and a test drives the class directly without
    a Flask app.

    ``deprovision`` is the one collaborator this controller does not implement
    itself: a ``Callable[[str], None]`` invoked with the deprovisioned user's
    ``UserRecord.id`` (e.g. ``"user:<uuid>"``) whenever a write sets
    ``active: false`` or a DELETE arrives. **Contract:** revoke every API/
    service key this subject owns and terminate every live session it holds.
    This controller owns neither a key store nor a session registry — the app
    composition root supplies the real implementation; ``lambda subject: None``
    is a legal, inert stand-in for a deployment that hasn't wired one yet.
    Called best-effort: an exception is logged, never raised into the request
    — a key/session cleanup failure must not fail the IdP's deprovisioning
    sync, which would leave an account looking still-active to the IdP while
    it is already disabled here.
    """

    def __init__(
        self,
        *,
        user_store: UserStoreBase,
        team_store: TeamStoreBase,
        audit_store: AuthAuditStoreBase | None,
        settings: AuthSettings,
        deprovision: Callable[[str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Capture the injected collaborators as instance state."""
        self.user_store = user_store
        self.team_store = team_store
        self.audit_store = audit_store
        self.settings = settings
        self.deprovision = deprovision
        self.clock = clock or _utcnow

    # ── enable / auth guards ────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Whether SCIM provisioning is switched on for this deployment."""
        return self.settings.enabled and self.settings.scim.enabled

    def require_auth(self) -> tuple[dict, int] | None:
        """Authorize a SCIM request — the ONE guard every route calls first.

        A missing/malformed ``Authorization: Bearer`` header, or one that
        doesn't match the configured secret (constant-time compare), is a 401
        ``ScimError``. No secret configured means SCIM is enabled but
        unusable — fails CLOSED (every request 401s) rather than silently
        admitting anyone.
        """
        secret = self.settings.scim.secret
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else None
        if not secret or not token or not hmac.compare_digest(token, secret):
            return self._error(401, "invalid or missing SCIM bearer token")
        return None

    # ── ServiceProviderConfig (RFC 7643 §5) ─────────────────────────────────

    def service_provider_config(self) -> tuple[dict, int]:
        """The capability document — honest about what this subset supports."""
        return (
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                "patch": {"supported": True},
                "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": _MAX_PAGE_SIZE},
                "changePassword": {"supported": False},
                "sort": {"supported": False},
                "etag": {"supported": False},
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer Token",
                        "description": (
                            "Authenticate with a bearer token issued out-of-band by the operator."
                        ),
                        "primary": True,
                    }
                ],
            },
            200,
        )

    # ── Users ────────────────────────────────────────────────────────────────

    def list_users(self) -> tuple[dict, int]:
        """``GET /Users`` — optionally filtered, always paginated."""
        try:
            matches = self._filtered_users(request.args.get("filter"))
        except ScimFilterUnsupportedError as exc:
            return self._error(501, f"unsupported filter: {exc.expr}")
        return self._list_response(matches, kind="user")

    def get_user(self, user_id: str) -> tuple[dict, int]:
        """``GET /Users/<id>``."""
        record = self.user_store.get(user_id)
        if record is None:
            return self._error(404, f"no such user: {user_id}")
        return self._user_body(record), 200

    def create_user(self) -> tuple[dict, int]:
        """``POST /Users`` — idempotent on the SCIM external identity.

        IdPs occasionally retry a POST whose 201 response was lost in transit;
        matching by external identity before writing means a retried create
        updates the same record instead of forking a duplicate user.
        """
        try:
            scim_user = ScimUser.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        existing = self._match_user(scim_user)
        record = scim_user.to_user_record(now=self.clock(), existing=existing)
        if existing is not None:
            saved = self.user_store.update(record)
        else:
            saved = self.user_store.create(record)
        self._on_user_saved(previous=existing, current=saved)
        return self._user_body(saved), (200 if existing is not None else 201)

    def replace_user(self, user_id: str) -> tuple[dict, int]:
        """``PUT /Users/<id>`` — full replace; ``active: false`` deprovisions."""
        existing = self.user_store.get(user_id)
        if existing is None:
            return self._error(404, f"no such user: {user_id}")
        try:
            scim_user = ScimUser.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        record = scim_user.to_user_record(now=self.clock(), existing=existing)
        saved = self.user_store.update(record)
        self._on_user_saved(previous=existing, current=saved)
        return self._user_body(saved), 200

    def patch_user(self, user_id: str) -> tuple[dict, int]:
        """``PATCH /Users/<id>`` — the ``active``/``displayName`` subset."""
        existing = self.user_store.get(user_id)
        if existing is None:
            return self._error(404, f"no such user: {user_id}")
        try:
            patch = ScimPatchOp.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        try:
            record = patch.apply_to_user_record(existing, now=self.clock())
        except ScimUnsupportedPatchError as exc:
            return self._error(501, f"unsupported PATCH path: {exc.path}", scim_type="invalidPath")
        saved = self.user_store.update(record)
        self._on_user_saved(previous=existing, current=saved)
        return self._user_body(saved), 200

    def delete_user(self, user_id: str) -> tuple[dict, int]:
        """``DELETE /Users/<id>`` — deprovisions; never hard-deletes the record.

        The account is disabled rather than erased so its audit history and
        any resource ownership it stamped stay resolvable. See the deprovision
        contract on the class docstring.
        """
        existing = self.user_store.get(user_id)
        if existing is None:
            return self._error(404, f"no such user: {user_id}")
        record = existing.model_copy(update={"status": "disabled", "updated_at": self.clock()})
        saved = self.user_store.update(record)
        self._on_user_saved(previous=existing, current=saved)
        return {}, 204

    # ── Groups ───────────────────────────────────────────────────────────────

    def list_groups(self) -> tuple[dict, int]:
        """``GET /Groups`` — optionally filtered, always paginated."""
        try:
            matches = self._filtered_groups(request.args.get("filter"))
        except ScimFilterUnsupportedError as exc:
            return self._error(501, f"unsupported filter: {exc.expr}")
        return self._list_response(matches, kind="group")

    def get_group(self, group_id: str) -> tuple[dict, int]:
        """``GET /Groups/<id>``."""
        record = self.team_store.get(group_id)
        if record is None:
            return self._error(404, f"no such group: {group_id}")
        return self._group_body(record), 200

    def create_group(self) -> tuple[dict, int]:
        """``POST /Groups`` — idempotent on the directory's own group identity.

        The roster in the body is applied as a full replace, because a POST
        body is the complete resource the client is creating: a retried create
        carries the same members and therefore settles on the same roster.
        """
        try:
            scim_group = ScimGroup.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        existing = self._match_group(scim_group)
        record = scim_group.to_team_record(existing=existing)
        try:
            saved = (
                self.team_store.update(record)
                if existing is not None
                else self.team_store.create(record)
            )
        except ValueError as exc:
            return self._error(409, str(exc), scim_type="uniqueness")
        self.team_store.replace_members(saved.id, self._resolve_members(scim_group.members))
        return self._group_body(saved), (200 if existing is not None else 201)

    def replace_group(self, group_id: str) -> tuple[dict, int]:
        """``PUT /Groups/<id>`` — full replace of the group's identity and roster.

        An empty ``members`` clears the roster: PUT means the body IS the
        resource, so omitting a member is how a client expresses a removal.
        """
        existing = self.team_store.get(group_id)
        if existing is None:
            return self._error(404, f"no such group: {group_id}")
        try:
            scim_group = ScimGroup.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        record = scim_group.to_team_record(existing=existing)
        try:
            saved = self.team_store.update(record)
        except ValueError as exc:
            return self._error(409, str(exc), scim_type="uniqueness")
        self.team_store.replace_members(saved.id, self._resolve_members(scim_group.members))
        return self._group_body(saved), 200

    def patch_group(self, group_id: str) -> tuple[dict, int]:
        """``PATCH /Groups/<id>`` — ``displayName`` rename + member-delta validation."""
        existing = self.team_store.get(group_id)
        if existing is None:
            return self._error(404, f"no such group: {group_id}")
        try:
            patch = ScimPatchOp.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return self._validation_error(exc)
        try:
            record = patch.apply_to_team_record(existing)
        except ScimUnsupportedPatchError as exc:
            return self._error(501, f"unsupported PATCH path: {exc.path}", scim_type="invalidPath")
        try:
            saved = self.team_store.update(record)
        except ValueError as exc:
            return self._error(409, str(exc), scim_type="uniqueness")
        self._apply_member_delta(saved.id, patch.group_member_delta())
        return self._group_body(saved), 200

    def delete_group(self, group_id: str) -> tuple[dict, int]:
        """``DELETE /Groups/<id>`` — removes the team record."""
        existing = self.team_store.get(group_id)
        if existing is None:
            return self._error(404, f"no such group: {group_id}")
        self.team_store.delete(group_id)
        return {}, 204

    # ── user helpers ─────────────────────────────────────────────────────────

    def _match_user(self, scim_user: ScimUser) -> UserRecord | None:
        """Lookup-before-create join: match by the SCIM external identity."""
        identity = ExternalSubject(
            issuer=SCIM_ISSUER, subject=scim_user.external_id or scim_user.user_name
        )
        return self.user_store.get_by_external(identity)

    def _filtered_users(self, filter_expr: str | None) -> list[UserRecord]:
        if not filter_expr:
            return self.user_store.list()
        attr, value = self._parse_filter(filter_expr)
        attr = attr.lower()
        if attr == "username":
            return [
                u for u in self.user_store.list() if ScimUser.from_user_record(u).user_name == value
            ]
        if attr == "externalid":
            identity = ExternalSubject(issuer=SCIM_ISSUER, subject=value)
            match = self.user_store.get_by_external(identity)
            return [match] if match is not None else []
        raise ScimFilterUnsupportedError(filter_expr)

    def _on_user_saved(self, *, previous: UserRecord | None, current: UserRecord) -> None:
        """Fire the deprovision callback + audit trail for one user write.

        A THREE-way transition, not a two-way one — a redundant deactivate
        (DELETE or PATCH ``active:false`` on an already-disabled user, which
        IdPs retry aggressively) must be a true no-op, not a repeated
        ``deprovision`` call or a misleading ``scim_provisioned`` entry for a
        write that changed nothing but a timestamp:

        * was active → now disabled: a deprovision event (calls
          ``deprovision`` once, records ``scim_deprovisioned``).
        * was already disabled → still disabled: no-op, nothing fires.
        * everything else (a fresh create, a profile update, a
          re-activation): ``scim_provisioned`` — the audit union's own
          docstring covers "provisioned OR UPDATED" by design.

        Both callbacks are best-effort: a failure here must never fail the
        request that already committed the store write.
        """
        was_active = previous is None or previous.status == "active"
        is_active = current.status == "active"
        if was_active and not is_active:
            self._safe_deprovision(current.id)
            self._append_audit(
                ScimDeprovisionedEvent(ts=self.clock(), source="scim", user_id=current.id)
            )
            return
        if not was_active and not is_active:
            return
        scim_identity = next(
            (i for i in current.external_identities if i.issuer == SCIM_ISSUER), None
        )
        self._append_audit(
            ScimProvisionedEvent(
                ts=self.clock(),
                source="scim",
                user_id=current.id,
                issuer=SCIM_ISSUER,
                external_subject=scim_identity.subject if scim_identity is not None else current.id,
            )
        )

    def _safe_deprovision(self, subject: str) -> None:
        try:
            self.deprovision(subject)
        except Exception:  # noqa: BLE001 - best-effort, never raises into the request
            logging.warning("scim: deprovision callback failed for {}", subject, exc_info=True)

    def _append_audit(self, event: ScimProvisionedEvent | ScimDeprovisionedEvent) -> None:
        if self.audit_store is None:
            return
        try:
            self.audit_store.append(event)
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the request
            logging.warning("scim: audit write failed", exc_info=True)

    @staticmethod
    def _user_body(record: UserRecord) -> dict:
        return ScimUser.from_user_record(
            record, location=ScimRoutesController._user_location(record.id)
        ).model_dump(mode="json", by_alias=True, exclude_none=True)

    @staticmethod
    def _user_location(user_id: str) -> str:
        return f"/api/scim/v2/Users/{user_id}"

    # ── group helpers ────────────────────────────────────────────────────────

    def _match_group(self, scim_group: ScimGroup) -> TeamRecord | None:
        """Lookup-before-create join: the directory's own id first, slug as fallback.

        Matching on ``externalId`` is what makes a group RENAME at the IdP
        update the team in place. The slug is derived from ``displayName``, so
        a slug-only match sees a renamed group as a brand-new one and forks a
        duplicate team, stranding every membership and ownership stamp on the
        original.

        The slug fallback still runs when ``externalId`` is absent OR matches
        nothing — the latter is how a team created before the IdP knew about
        it (by an operator, or by an earlier client that sent no ``externalId``)
        gets adopted and stamped rather than duplicated.
        """
        if scim_group.external_id is not None:
            by_external = self.team_store.get_by_external_id(scim_group.external_id)
            if by_external is not None:
                return by_external
        return self.team_store.get_by_slug(scim_group.derived_slug())

    def _apply_member_delta(self, team_id: str, delta: ScimGroupMemberDelta) -> None:
        """Persist the roster change a PATCH expressed.

        A wholesale ``replace`` goes through ``replace_members`` rather than a
        hand-rolled drop-then-add, which would demote every ``team_admin`` in
        the team (a SCIM member list carries no roles).

        Removals deliberately do NOT go through :meth:`_resolve_members`. That
        filter skips ids the user store no longer knows, which is right for an
        add — but applying it to a removal would REFUSE to detach a deleted
        user, leaving exactly the stranded edge the removal was sent to clear.
        ``remove_member`` is already a no-op for a member who is not there.
        """
        if delta.replace is not None:
            self.team_store.replace_members(
                team_id, self._resolve_members(self._as_members(delta.replace))
            )
            return
        for user_id in self._resolve_members(self._as_members(delta.add)):
            self.team_store.add_member(team_id, user_id)
        for user_id in delta.remove:
            self.team_store.remove_member(team_id, user_id)

    @staticmethod
    def _as_members(user_ids: Sequence[str]) -> tuple[ScimGroupMember, ...]:
        """Lift bare delta ids into the shape :meth:`_resolve_members` validates."""
        return tuple(ScimGroupMember(value=user_id) for user_id in user_ids)

    def _group_members(self, team_id: str) -> tuple[ScimGroupMember, ...]:
        """The wire roster for one team: durable edges, minus what no longer resolves.

        This is the read half of the store's deliberate non-cascade — deleting
        a user leaves its edges behind, and resolving each ``user_id`` here is
        what keeps a stranded edge out of the response instead of surfacing a
        member the deployment no longer has.
        """
        members: list[ScimGroupMember] = []
        for edge in self.team_store.list_members(team_id):
            user = self.user_store.get(edge.user_id)
            if user is None:
                continue
            members.append(
                ScimGroupMember(
                    value=edge.user_id,
                    display=user.display_name,
                    ref=self._user_location(edge.user_id),
                )
            )
        return tuple(members)

    def _filtered_groups(self, filter_expr: str | None) -> list[TeamRecord]:
        if not filter_expr:
            return self.team_store.list()
        attr, value = self._parse_filter(filter_expr)
        if attr.lower() == "displayname":
            return [g for g in self.team_store.list() if g.name == value]
        raise ScimFilterUnsupportedError(filter_expr)

    def _resolve_members(self, members: Sequence[ScimGroupMember]) -> list[str]:
        """Validate member ids against the user store — 404-tolerant per member.

        IdPs retry group-membership syncs aggressively; an unknown member id (a
        user the IdP hasn't pushed yet, a stale id) is logged and SKIPPED,
        never turned into a batch failure. Returns the ids that resolved, which
        is what the caller then persists.

        This filter belongs on ADDS only — see :meth:`_apply_member_delta` for
        why applying it to a removal would strand the edge it was sent to clear.
        """
        resolved: list[str] = []
        for member in members:
            if self.user_store.get(member.value) is None:
                logging.warning(
                    "scim: group member {} does not resolve to a known user; skipping", member.value
                )
                continue
            resolved.append(member.value)
        return resolved

    def _group_body(self, record: TeamRecord) -> dict:
        return self._group_resource(record).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    def _group_resource(self, record: TeamRecord) -> ScimGroup:
        """The ONE group projection — single GET and list page alike.

        Both surfaces go through here so a list can never report ``members:
        []`` for a group whose GET shows a roster; an IdP reconciling the two
        would read that difference as a pile of removals.
        """
        return ScimGroup.from_team_record(
            record,
            members=self._group_members(record.id),
            location=self._group_location(record.id),
        )

    @staticmethod
    def _group_location(group_id: str) -> str:
        return f"/api/scim/v2/Groups/{group_id}"

    # ── shared: filter parsing, pagination, error/validation envelopes ─────────

    @staticmethod
    def _parse_filter(expr: str) -> tuple[str, str]:
        match = _FILTER_RE.match(expr)
        if match is None:
            raise ScimFilterUnsupportedError(expr)
        return match.group(1), match.group(2).replace('\\"', '"')

    def _list_response(self, matches: Sequence[Any], *, kind: str) -> tuple[dict, int]:
        start_index = self._int_arg("startIndex", default=1, minimum=1)
        count = self._int_arg(
            "count", default=_DEFAULT_PAGE_SIZE, minimum=0, maximum=_MAX_PAGE_SIZE
        )
        page = matches[start_index - 1 : start_index - 1 + count] if count else []
        if kind == "user":
            resources = [
                ScimUser.from_user_record(record, location=self._user_location(record.id))
                for record in page
            ]
        else:
            resources = [self._group_resource(record) for record in page]
        response = ScimListResponse.for_page(
            resources, total_results=len(matches), start_index=start_index
        )
        return response.model_dump(mode="json", by_alias=True, exclude_none=True), 200

    @staticmethod
    def _int_arg(name: str, *, default: int, minimum: int, maximum: int | None = None) -> int:
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        value = max(value, minimum)
        if maximum is not None:
            value = min(value, maximum)
        return value

    @staticmethod
    def _error(status: int, detail: str, *, scim_type: str | None = None) -> tuple[dict, int]:
        return (
            ScimError.for_status(status, detail, scim_type=scim_type).model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            status,
        )

    @staticmethod
    def _validation_error(exc: ValidationError) -> tuple[dict, int]:
        return ScimRoutesController._error(400, str(exc), scim_type="invalidValue")


# ---------------------------------------------------------------------------
# Blueprint — thin HTTP adapters over the one controller instance
# ---------------------------------------------------------------------------


def register(app: Any, controller: ScimRoutesController) -> None:
    """Mount ``/api/scim/v2/*`` on *app*, bound to *controller*.

    Every route consults ``controller.enabled`` first (via the blueprint's
    ``before_request`` gate) and 404s cleanly while SCIM is off — no store is
    touched and no auth header is compared on that path, so mounting is safe
    even for a deployment that never turns SCIM on.
    """
    app.register_blueprint(_build_blueprint(controller), url_prefix="/api/scim/v2")


def _build_blueprint(controller: ScimRoutesController) -> Blueprint:
    bp = Blueprint("scim", __name__)

    @bp.before_request
    def _gate() -> Response | None:
        if not controller.enabled:
            return _scim_response(*controller._error(404, "SCIM provisioning is not enabled"))
        auth = controller.require_auth()
        if auth is not None:
            return _scim_response(*auth)
        return None

    @bp.route("/ServiceProviderConfig", methods=["GET"])
    def service_provider_config() -> Response:
        return _scim_response(*controller.service_provider_config())

    @bp.route("/Users", methods=["GET"])
    def list_users() -> Response:
        return _scim_response(*controller.list_users())

    @bp.route("/Users", methods=["POST"])
    def create_user() -> Response:
        return _scim_response(*controller.create_user())

    @bp.route("/Users/<user_id>", methods=["GET"])
    def get_user(user_id: str) -> Response:
        return _scim_response(*controller.get_user(user_id))

    @bp.route("/Users/<user_id>", methods=["PUT"])
    def replace_user(user_id: str) -> Response:
        return _scim_response(*controller.replace_user(user_id))

    @bp.route("/Users/<user_id>", methods=["PATCH"])
    def patch_user(user_id: str) -> Response:
        return _scim_response(*controller.patch_user(user_id))

    @bp.route("/Users/<user_id>", methods=["DELETE"])
    def delete_user(user_id: str) -> Response:
        return _scim_response(*controller.delete_user(user_id))

    @bp.route("/Groups", methods=["GET"])
    def list_groups() -> Response:
        return _scim_response(*controller.list_groups())

    @bp.route("/Groups", methods=["POST"])
    def create_group() -> Response:
        return _scim_response(*controller.create_group())

    @bp.route("/Groups/<group_id>", methods=["GET"])
    def get_group(group_id: str) -> Response:
        return _scim_response(*controller.get_group(group_id))

    @bp.route("/Groups/<group_id>", methods=["PUT"])
    def replace_group(group_id: str) -> Response:
        return _scim_response(*controller.replace_group(group_id))

    @bp.route("/Groups/<group_id>", methods=["PATCH"])
    def patch_group(group_id: str) -> Response:
        return _scim_response(*controller.patch_group(group_id))

    @bp.route("/Groups/<group_id>", methods=["DELETE"])
    def delete_group(group_id: str) -> Response:
        return _scim_response(*controller.delete_group(group_id))

    return bp


def _scim_response(body: dict, status: int) -> Response:
    """Every SCIM response, including errors, is ``application/scim+json``."""
    if status == 204:
        return Response(status=204, mimetype="application/scim+json")
    resp = jsonify(body)
    resp.status_code = status
    resp.mimetype = "application/scim+json"
    return resp


__all__ = ["ScimRoutesController", "ScimFilterUnsupportedError", "register"]
