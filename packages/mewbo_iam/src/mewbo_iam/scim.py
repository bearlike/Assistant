#!/usr/bin/env python3
"""SCIM 2.0 resource models — an RFC 7643 (schema) / RFC 7644 (protocol) subset.

Enough of the standard for an enterprise IdP (Entra, Okta, Keycloak) to push
``User``/``Group`` resources and deprovision them: ``ScimUser``, ``ScimGroup``,
``ScimPatchOp`` (RFC 7644 PatchOp over a flat, IdP-common path subset),
``ScimListResponse``, and the RFC 7644 ``ScimError`` envelope. Every mapping
between a SCIM resource and its durable kernel record (``UserRecord``/
``TeamRecord``) is a pure method ON the SCIM model — no I/O, no clock reads,
no store access — mirroring ``TriggerSpec``/``AuthAuditEvent``: the model picks
the mapping, the app-side controller supplies the missing collaborators (an
existing record to merge into, "now", a resolved member list) as arguments.

**``extra="ignore"`` is the one deliberate exception to the house
``extra="forbid"`` rule, and it is deliberate for a reason specific to SCIM's
own extensibility model.** SCIM resources are designed to carry vendor/IdP
schema extensions this subset does not implement — Entra's enterprise-user
extension (``urn:ietf:params:scim:schemas:extension:enterprise:2.0:User``),
custom attributes, provider-specific ``meta`` fields. RFC 7643 §3.3 makes
``schemas`` an open, additive list precisely so an unrecognized extension is
something a receiver tolerates, not a wire error. ``extra="forbid"`` would turn
every IdP's routine extension payload into a hard-rejected create/update —
provisioning would fail for the exact clients this surface exists to serve.
This is the ONE place in the codebase where "unknown field ⇒ reject" is the
wrong default, because the wire FORMAT itself defines unknown fields as
optional metadata, not smuggled state.

Group provisioning needs two durable things this module deliberately does not
reach for itself, both of which now exist on the kernel side:

* ``TeamRecord.external_id`` (``teams.py``) holds the IdP's own group id, and
  ``TeamStoreBase.get_by_external_id`` is the lookup-before-create seam. Match
  there FIRST and fall back to :meth:`ScimGroup.derived_slug` only when the
  directory sent no ``externalId`` — the slug is derived from the display name,
  so matching on it forks a duplicate team the moment a group is renamed.
* ``TeamStoreBase`` persists membership (``add_member``/``remove_member``/
  ``list_members``). :meth:`ScimPatchOp.group_member_delta` still only COMPUTES
  the declarative add/remove/replace a PATCH expresses — applying it is the
  controller's job, because resolving each member id against the user store is
  I/O and models never perform it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_iam.principal import ExternalSubject
from mewbo_iam.teams import TeamRecord
from mewbo_iam.users import UserRecord

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

# The ``ExternalSubject.issuer`` stamped on every identity SCIM provisions —
# the join key JIT/federated login already uses, so a user linked by SCIM and
# later logging in through the same IdP's OIDC app resolves to ONE UserRecord.
SCIM_ISSUER = "scim"

ScimPatchVerb = Literal["add", "replace", "remove"]


class ScimUnsupportedPatchError(ValueError):
    """A PATCH operation targeted a path outside this subset.

    Raised by the pure ``apply_to_*`` methods below; the app-side route is
    where this becomes a 501 ``ScimError`` — models raise, they never format
    an HTTP response.
    """

    def __init__(self, path: str) -> None:
        """Capture the offending SCIM path for the caller's 501 detail."""
        self.path = path
        super().__init__(f"unsupported SCIM PATCH path: {path!r}")


# ---------------------------------------------------------------------------
# User sub-objects
# ---------------------------------------------------------------------------


class ScimName(BaseModel):
    """RFC 7643 §4.1.1 ``name`` complex attribute — the subset we read."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    given_name: str | None = Field(default=None, alias="givenName")
    family_name: str | None = Field(default=None, alias="familyName")
    formatted: str | None = None


class ScimEmail(BaseModel):
    """One entry of the ``emails`` multi-valued attribute."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str | None = None
    primary: bool = False
    type: str | None = None


class ScimPhoto(BaseModel):
    """One entry of the ``photos`` multi-valued attribute."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str | None = None
    type: str | None = None


class ScimMeta(BaseModel):
    """RFC 7643 §3.1 ``meta`` — resource metadata every representation carries."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    resource_type: str | None = Field(default=None, alias="resourceType")
    created: datetime | None = None
    last_modified: datetime | None = Field(default=None, alias="lastModified")
    location: str | None = None


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class ScimUser(BaseModel):
    """RFC 7643 §4.1 ``User`` resource — the subset an enterprise IdP pushes."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schemas: tuple[str, ...] = (SCIM_USER_SCHEMA,)
    id: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    user_name: str = Field(alias="userName")
    name: ScimName | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    emails: tuple[ScimEmail, ...] = ()
    active: bool = True
    photos: tuple[ScimPhoto, ...] = ()
    meta: ScimMeta | None = None

    @field_validator("user_name")
    @classmethod
    def _require_username(cls, value: str) -> str:
        """``userName`` is the one attribute RFC 7643 requires present + non-blank."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("userName must not be blank")
        return stripped

    @property
    def primary_email(self) -> str | None:
        """The email marked ``primary``, else the first with a value, else ``None``."""
        for email in self.emails:
            if email.primary and email.value:
                return email.value
        for email in self.emails:
            if email.value:
                return email.value
        return None

    @property
    def primary_photo(self) -> str | None:
        """The first photo with a value, else ``None``."""
        for photo in self.photos:
            if photo.value:
                return photo.value
        return None

    def _resolved_display_name(self) -> str | None:
        """``displayName`` if sent, else ``given family``, else ``name.formatted``."""
        if self.display_name:
            return self.display_name
        if self.name is not None:
            parts = [p for p in (self.name.given_name, self.name.family_name) if p]
            if parts:
                return " ".join(parts)
            if self.name.formatted:
                return self.name.formatted
        return None

    def to_user_record(self, *, now: datetime, existing: UserRecord | None = None) -> UserRecord:
        """Map this SCIM push onto a durable :class:`UserRecord`.

        ``(issuer="scim", subject)`` is the JIT/SCIM join key: ``subject`` is
        the IdP's ``externalId`` when sent (Entra/Okta always send one), else
        ``userName`` (some on-prem Keycloak configurations omit ``externalId``).
        *existing* is the already-linked record (the controller resolves it via
        ``UserStore.get_by_external`` or a direct ``.get(id)``); when given, its
        ``id``/``created_at``/``roles`` are preserved and its OTHER external
        identities are kept alongside the SCIM one — a SCIM-provisioned user who
        also logs in through a federated IdP keeps both links, never loses one
        for the other.
        """
        scim_identity = ExternalSubject(
            issuer=SCIM_ISSUER, subject=self.external_id or self.user_name
        )
        if existing is not None:
            others = tuple(i for i in existing.external_identities if i != scim_identity)
            identities = (*others, scim_identity)
        else:
            identities = (scim_identity,)
        return UserRecord(
            id=existing.id if existing is not None else f"user:{uuid4().hex}",
            external_identities=identities,
            email=self.primary_email,
            picture_url=self.primary_photo,
            email_verified=existing.email_verified if existing is not None else None,
            display_name=self._resolved_display_name(),
            status="active" if self.active else "disabled",
            roles=existing.roles if existing is not None else (),
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )

    @classmethod
    def from_user_record(cls, user: UserRecord, *, location: str | None = None) -> ScimUser:
        """The read-side projection — what GET/POST/PUT/PATCH return for *user*."""
        scim_identity = next(
            (identity for identity in user.external_identities if identity.issuer == SCIM_ISSUER),
            None,
        )
        user_name = scim_identity.subject if scim_identity is not None else (user.email or user.id)
        return cls(
            id=user.id,
            externalId=scim_identity.subject if scim_identity is not None else None,
            userName=user_name,
            displayName=user.display_name,
            emails=(ScimEmail(value=user.email, primary=True),) if user.email else (),
            active=user.status == "active",
            photos=(ScimPhoto(value=user.picture_url, type="photo"),) if user.picture_url else (),
            meta=ScimMeta(
                resourceType="User",
                created=user.created_at,
                lastModified=user.updated_at,
                location=location,
            ),
        )


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


class ScimGroupMember(BaseModel):
    """One entry of a Group's ``members`` multi-valued attribute."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str
    display: str | None = None
    ref: str | None = Field(default=None, alias="$ref")


class ScimGroup(BaseModel):
    """RFC 7643 §4.2 ``Group`` resource."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    _SLUG_UNSAFE_RE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

    schemas: tuple[str, ...] = (SCIM_GROUP_SCHEMA,)
    id: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    display_name: str = Field(alias="displayName")
    members: tuple[ScimGroupMember, ...] = ()
    meta: ScimMeta | None = None

    @field_validator("display_name")
    @classmethod
    def _require_display_name(cls, value: str) -> str:
        """``displayName`` is the one attribute RFC 7643 requires for a Group."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("displayName must not be blank")
        return stripped

    def derived_slug(self) -> str:
        """A ``TeamRecord.slug``-valid kebab-case derivation of ``displayName``.

        SCIM carries no slug concept; ``TeamRecord.slug`` requires lowercase
        kebab-case, so this is the one deterministic derivation every write uses
        — and the fallback identity a caller matches an existing team by for
        lookup-before-create (see the module docstring on why ``externalId``
        can't fill that role). Public: the controller needs it to look up a
        team BEFORE calling :meth:`to_team_record`.

        **The fallback must stay deterministic.** A displayName with no
        alphanumerics at all derives an empty slug, so it falls back to a digest
        of the name itself. Keying that fallback on anything random would make
        the lookup-before-create identity differ on every push, forking a fresh
        duplicate team on each sync instead of matching the one already stored.
        """
        lowered = self._SLUG_UNSAFE_RE.sub("-", self.display_name.strip().lower()).strip("-")
        if lowered:
            return lowered
        digest = hashlib.sha256(self.display_name.encode("utf-8")).hexdigest()
        return f"group-{digest[:8]}"

    def to_team_record(self, *, existing: TeamRecord | None = None) -> TeamRecord:
        """Map this SCIM push onto a durable :class:`TeamRecord`.

        *existing* is the already-matched record (the controller resolves it by
        ``TeamStore.get_by_external_id``, falling back to :meth:`derived_slug`);
        when given, its ``id``/``slug``/``description`` are preserved so a group
        rename updates the team in place rather than re-slugging it — the slug
        is the team's stable url identity, the display name is not.

        ``external_id`` is never CLEARED by a push that omits it: a directory
        that once identified this group keeps that link even if a later sync
        (or a different client) sends no ``externalId``, since dropping it would
        strand the team behind the slug fallback again.
        """
        return TeamRecord(
            id=existing.id if existing is not None else f"team:{uuid4().hex}",
            slug=existing.slug if existing is not None else self.derived_slug(),
            name=self.display_name,
            description=existing.description if existing is not None else "",
            external_id=self.external_id or (existing.external_id if existing else None),
        )

    @classmethod
    def from_team_record(
        cls,
        team: TeamRecord,
        *,
        members: Sequence[ScimGroupMember] = (),
        location: str | None = None,
    ) -> ScimGroup:
        """The read-side projection.

        *members* is supplied by the caller: models never import I/O, so the
        controller reads ``TeamStore.list_members`` and resolves each edge's
        ``user_id`` against the user store before handing the result here. An
        id that no longer resolves is skipped there, which is what keeps a
        stranded edge out of the wire representation.
        """
        return cls(
            id=team.id,
            externalId=team.external_id,
            displayName=team.name,
            members=tuple(members),
            meta=ScimMeta(resourceType="Group", location=location),
        )


class ScimGroupMemberDelta(BaseModel):
    """The declarative member-list change one or more PATCH Operations express.

    Pure data — resolving each ``value`` against the user store (skipping an
    unknown id with a warning, never failing the whole batch) and persisting
    the result is the controller's job. ``replace`` is ``None`` when no
    wholesale-replace operation was sent (distinct from an empty replace,
    which clears the group).
    """

    model_config = ConfigDict(frozen=True)

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    replace: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# PatchOp — RFC 7644 §3.5.2
# ---------------------------------------------------------------------------


class ScimPatchOperation(BaseModel):
    """One entry of a PatchOp's ``Operations`` array."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    op: ScimPatchVerb
    path: str | None = None
    value: Any = None

    @field_validator("op", mode="before")
    @classmethod
    def _lower_op(cls, value: object) -> object:
        """RFC 7644 treats ``op`` case-insensitively (``"Replace"`` is common)."""
        return value.lower() if isinstance(value, str) else value


class ScimPatchOp(BaseModel):
    """RFC 7644 §3.5.2 PatchOp request body.

    :meth:`apply_to_user_record` / :meth:`apply_to_team_record` apply the flat,
    IdP-common attribute subset directly onto the existing kernel record —
    ``active``/``displayName`` for a User, ``displayName`` for a Group — and
    raise :class:`ScimUnsupportedPatchError` for anything else (e.g.
    ``emails[type eq "work"].value``), which the route maps to 501.
    :meth:`group_member_delta` is separate because a membership change needs
    store access this pure model cannot perform.
    """

    _MEMBER_VALUE_EQ_RE: ClassVar[re.Pattern[str]] = re.compile(r'^members\[value eq "([^"]+)"\]$')

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schemas: tuple[str, ...] = (SCIM_PATCH_SCHEMA,)
    operations: tuple[ScimPatchOperation, ...] = Field(default=(), alias="Operations")

    def apply_to_user_record(self, user: UserRecord, *, now: datetime) -> UserRecord:
        """Apply the supported subset onto *user*, returning a NEW record.

        Supports a bare-value shape too (``{"op":"replace","value":{"active":
        false}}``, no ``path`` — a form some IdPs send for User deactivation) by
        treating each key of ``value`` as its own path.
        """
        updates: dict[str, Any] = {}
        for operation in self.operations:
            path = (operation.path or "").strip()
            if not path and isinstance(operation.value, dict):
                for key, val in operation.value.items():
                    updates.update(self._user_field_update(key, val, operation.op))
                continue
            updates.update(self._user_field_update(path, operation.value, operation.op))
        if not updates:
            return user
        updates["updated_at"] = now
        return user.model_copy(update=updates)

    @staticmethod
    def _user_field_update(path: str, value: Any, op: ScimPatchVerb) -> dict[str, Any]:
        """The ``UserRecord`` field change one supported path expresses.

        Returns the change rather than writing into a caller-supplied dict, so
        merge order stays visible at the one call site that owns it — matching
        :meth:`apply_to_team_record`, which builds its updates the same way.
        """
        if path == "active":
            return {"status": "active" if bool(value) else "disabled"}
        if path == "displayName":
            if op == "remove":
                return {"display_name": None}
            return {"display_name": str(value) if value is not None else None}
        raise ScimUnsupportedPatchError(path or "(root)")

    def apply_to_team_record(self, team: TeamRecord) -> TeamRecord:
        """Apply the supported subset (``displayName`` rename) onto *team*.

        Member-path operations are deliberately SKIPPED here (not raised) —
        they are handled by :meth:`group_member_delta`; a PATCH mixing a
        supported ``displayName`` op with a ``members`` op must not have the
        member path trip this method's unsupported-path guard.
        """
        updates: dict[str, Any] = {}
        for operation in self.operations:
            path = (operation.path or "").strip()
            if path in ("", "members") or path.startswith("members["):
                continue
            if path == "displayName":
                updates["name"] = None if operation.op == "remove" else str(operation.value)
                continue
            raise ScimUnsupportedPatchError(path)
        if not updates:
            return team
        return team.model_copy(update=updates)

    def group_member_delta(self) -> ScimGroupMemberDelta:
        """The declarative member add/remove/replace this PatchOp expresses.

        Handles both the whole-list form (``path="members"``, ``value`` an
        array of ``{"value": "<id>"}``) and Entra/Azure AD's common per-member
        remove (``path='members[value eq "<id>"]'``, ``op="remove"``).
        """
        add: list[str] = []
        remove: list[str] = []
        replace: list[str] | None = None
        for operation in self.operations:
            path = (operation.path or "").strip()
            if path == "members":
                ids = self._member_ids(operation.value)
                if operation.op == "remove":
                    remove.extend(ids)
                elif operation.op == "replace":
                    replace = list(ids) if replace is None else replace + ids
                else:
                    add.extend(ids)
                continue
            match = self._MEMBER_VALUE_EQ_RE.match(path)
            if match is not None and operation.op == "remove":
                remove.append(match.group(1))
        return ScimGroupMemberDelta(
            add=tuple(add),
            remove=tuple(remove),
            replace=tuple(replace) if replace is not None else None,
        )

    @staticmethod
    def _member_ids(value: Any) -> list[str]:
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                if isinstance(item, dict) and "value" in item:
                    out.append(str(item["value"]))
                elif isinstance(item, str):
                    out.append(item)
            return out
        if isinstance(value, dict) and "value" in value:
            return [str(value["value"])]
        return []


# ---------------------------------------------------------------------------
# Protocol envelopes — RFC 7644 §3.4.2 (ListResponse) / §3.12 (Error)
# ---------------------------------------------------------------------------


class ScimListResponse(BaseModel):
    """RFC 7644 §3.4.2 ``ListResponse`` — the wire shape for every collection GET.

    ``extra="ignore"`` for the module-wide reason: this envelope wraps resources
    an IdP may have decorated with its own schema extensions.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schemas: tuple[str, ...] = (SCIM_LIST_SCHEMA,)
    total_results: int = Field(alias="totalResults")
    items_per_page: int = Field(alias="itemsPerPage")
    start_index: int = Field(alias="startIndex")
    # Typed resources, not pre-dumped dicts: the page carries models until the
    # caller serializes the whole envelope, so ``by_alias``/``exclude_none``
    # are decided once, at that dump, rather than baked in here.
    resources: tuple[ScimUser | ScimGroup, ...] = Field(default=(), alias="Resources")

    @classmethod
    def for_page(
        cls,
        resources: Sequence[ScimUser | ScimGroup],
        *,
        total_results: int,
        start_index: int = 1,
    ) -> ScimListResponse:
        """Build a page from already-sliced resources."""
        page = tuple(resources)
        return cls(
            totalResults=total_results,
            itemsPerPage=len(page),
            startIndex=start_index,
            Resources=page,
        )


class ScimError(BaseModel):
    """RFC 7644 §3.12 error response — the ONLY error shape this surface returns.

    ``status`` is a STRING per the spec (not a JSON number) — SCIM clients
    parse it as such. ``extra="ignore"`` for the module-wide reason.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schemas: tuple[str, ...] = (SCIM_ERROR_SCHEMA,)
    status: str
    detail: str | None = None
    scim_type: str | None = Field(default=None, alias="scimType")

    @classmethod
    def for_status(cls, status: int, detail: str, *, scim_type: str | None = None) -> ScimError:
        """Build an error for HTTP *status* with a human-readable *detail*."""
        return cls(status=str(status), detail=detail, scimType=scim_type)


__all__ = [
    "SCIM_USER_SCHEMA",
    "SCIM_GROUP_SCHEMA",
    "SCIM_LIST_SCHEMA",
    "SCIM_PATCH_SCHEMA",
    "SCIM_ERROR_SCHEMA",
    "SCIM_ISSUER",
    "ScimPatchVerb",
    "ScimUnsupportedPatchError",
    "ScimName",
    "ScimEmail",
    "ScimPhoto",
    "ScimMeta",
    "ScimUser",
    "ScimGroupMember",
    "ScimGroup",
    "ScimGroupMemberDelta",
    "ScimPatchOperation",
    "ScimPatchOp",
    "ScimListResponse",
    "ScimError",
]
