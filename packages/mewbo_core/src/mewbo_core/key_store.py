#!/usr/bin/env python3
"""API key storage and verification.

Provides a ``KeyStoreBase`` ABC, a filesystem-backed ``KeyStore``
implementation, and a ``create_key_store()`` factory that returns the
configured driver (json or mongodb) — mirroring ``session_store.py``.

A key is high-entropy (``secrets.token_urlsafe``) so it is hashed with a single
unsalted SHA-256 pass (NOT bcrypt/argon2 — those defend against low-entropy
passwords, which is not the threat model here). Only the hash is persisted; the
plaintext is returned exactly once, at creation.

A record may additionally carry optional identity/authorization fields
(``owner_subject``, ``roles``, ``scopes``, ``team_id``, ``expires_at``) so a key
can name the principal it authenticates as, the roles/scopes it confers, and an
expiry. These are ALL optional and absent by default — a record without them is
a legacy, full-power key, and the mapping of a record to a principal lives in
the app-side AuthKit (this package stays a plain store and never imports the IAM
kernel). ``scopes`` honours a three-state law when read: absent/``None`` means
unrestricted-legacy, ``[]`` means explicitly-none; the two are NOT interchangeable.
:class:`KeyScopes` wraps that law as behavior on one class (``.matches(required)``)
so no caller re-derives it with a scattered ``if``.

Owner-scoped queries (``list_keys_for_owner``/``revoke_keys_for_owner``),
key rotation (``rotate_key``), and a bounded expiry sweep (``purge_expired``)
are composed on :class:`KeyStoreBase` from the same abstract primitives every
driver already implements — see their docstrings for the per-driver atomicity
notes.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TypedDict

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.config import get_config_value, resolve_mewbo_home

logging = get_logger(name="core.key_store")

_KEY_PREFIX = "mk_"


class _KeyIdentityFields(TypedDict, total=False):
    """Optional identity/authorization fields a key record MAY carry.

    Every field is absent by default (``total=False``): a record without them is
    a legacy, full-power key. ``scopes`` follows the three-state law when read —
    absent/``None`` = unrestricted-legacy, ``[]`` = explicitly-none — so a
    consumer must distinguish a missing key from a ``None`` value from ``[]``.
    ``expires_at`` is an ISO-8601 UTC string; a record past it is treated as
    revoked by :meth:`KeyStoreBase.resolve_key`.
    """

    owner_subject: str
    roles: list[str]
    scopes: list[str] | None
    team_id: str
    expires_at: str | None


class PublicKeyRecord(_KeyIdentityFields):
    """An API key record with the secret hash stripped — safe to return.

    Carries the required metadata plus any optional identity fields above.
    """

    id: str
    label: str
    created_at: str
    revoked_at: str | None


class KeyRecord(PublicKeyRecord):
    """A persisted API key record — a :class:`PublicKeyRecord` plus the hash."""

    key_hash: str


def _generate_key() -> str:
    """Return a fresh high-entropy plaintext API key."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(plaintext: str) -> str:
    """Hash a plaintext key with SHA-256 (hex digest)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _public_record(record: KeyRecord) -> PublicKeyRecord:
    """Strip the secret hash from a stored record before returning it.

    Optional identity fields (owner/roles/scopes/team/expiry) pass through — only
    ``key_hash`` is removed — so a resolved public record still names the
    principal the caller needs.
    """
    return {k: v for k, v in record.items() if k != "key_hash"}  # type: ignore[return-value]


def _record_expired(record: KeyRecord | PublicKeyRecord, now: datetime) -> bool:
    """Whether ``record`` has an ``expires_at`` at or before ``now``.

    A missing/``None`` expiry never expires. An unparseable expiry fails CLOSED
    (treated as expired) so a corrupt value can never leave a key usable forever.
    ``now`` is passed in (never read from a clock here) so callers stay testable.
    """
    raw = record.get("expires_at")
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return now >= expiry


class KeyScopes:
    """The record's three-state ``scopes`` value, with matching as behavior.

    Wraps ``record.get("scopes")`` so no call site re-derives the three-state
    law with a scattered ``if``: ``None`` — unrestricted, every ``matches()``
    call returns ``True``; ``()``/``[]`` — explicitly none, every call returns
    ``False``; a non-empty collection — the exact granted set. Constructed from
    any iterable (a stored ``list[str]`` or an IAM ``Principal``'s
    ``tuple[str, ...]``) so both a :class:`KeyRecord` reader and the app-side
    IAM kernel can share this ONE matcher instead of each re-implementing it —
    this class is the pure matcher `mewbo_iam` is meant to import; it never
    imports anything from `mewbo_iam` itself (core stays the lean base of the
    dependency DAG).

    Matching rule: an exact scope id, or a granted id ending in ``.*`` whose
    prefix (through the dot) is a prefix of the required id — one wildcard per
    segment family, e.g. ``sessions.*`` matches ``sessions.read``,
    ``sessions.write``, even ``sessions.admin.export`` (anything under that
    family), but never ``sessionsx.read``. A bare ``"*"`` is deliberately NOT a
    wildcard — only the ``<family>.*`` form narrows; "unrestricted" stays
    exclusively ``None``'s job so it can never be re-derived from a scope list.
    """

    __slots__ = ("_scopes",)

    def __init__(self, scopes: Iterable[str] | None) -> None:
        """Capture *scopes* (``None`` for unrestricted, else any iterable)."""
        self._scopes = None if scopes is None else tuple(scopes)

    @classmethod
    def from_record(cls, record: KeyRecord | PublicKeyRecord) -> KeyScopes:
        """Build from a stored record's ``scopes`` field (absent reads as ``None``)."""
        return cls(record.get("scopes"))

    @property
    def unrestricted(self) -> bool:
        """Whether this is the ``None`` (unrestricted-legacy) state."""
        return self._scopes is None

    @property
    def granted(self) -> tuple[str, ...] | None:
        """The raw granted scopes, or ``None`` when unrestricted."""
        return self._scopes

    def matches(self, required: str) -> bool:
        """Whether *required* is permitted under these scopes.

        See the class docstring for the three-state + wildcard rule.
        """
        if self._scopes is None:
            return True
        for granted in self._scopes:
            if granted == required:
                return True
            if granted.endswith(".*") and required.startswith(granted[:-1]):
                return True
        return False

    def __repr__(self) -> str:
        """Debug repr — the raw granted state, not the matching rule."""
        return f"KeyScopes({self._scopes!r})"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class KeyStoreBase(abc.ABC):
    """Abstract interface for API key storage backends.

    A persisted record is ``{id, label, key_hash, created_at, revoked_at}``.
    ``created_at``/``revoked_at`` are ISO-8601 UTC strings; ``revoked_at`` is
    ``None`` while the key is active. Subclasses implement only persistence;
    the shared key-minting and hash-comparison logic lives here.
    """

    @staticmethod
    def _mint_record(label: str, id_field: str = "id") -> tuple[str, KeyRecord]:
        """Build a fresh ``(plaintext, persisted_record)`` pair.

        ``id_field`` is the record's primary-key name (``"id"`` for the file
        driver, ``"_id"`` for Mongo). The plaintext is returned once and only
        its hash is stored.
        """
        plaintext = _generate_key()
        record = {
            id_field: uuid.uuid4().hex,
            "label": label,
            "key_hash": _hash_key(plaintext),
            "created_at": utc_now_iso(),
            "revoked_at": None,
        }
        return plaintext, record  # type: ignore[return-value]

    @staticmethod
    def _match_active(
        plaintext: str, active_records: Iterable[KeyRecord]
    ) -> PublicKeyRecord | None:
        """Return the public form of the active record matching ``plaintext``.

        ``active_records`` must already exclude revoked keys. Compares each
        stored hash with ``hmac.compare_digest`` to avoid timing leaks.
        """
        if not plaintext:
            return None
        candidate = _hash_key(plaintext)
        for record in active_records:
            if hmac.compare_digest(record.get("key_hash", ""), candidate):
                return _public_record(record)
        return None

    @staticmethod
    def _match_active_unexpired(
        plaintext: str, active_records: Iterable[KeyRecord], now: datetime
    ) -> PublicKeyRecord | None:
        """Like :meth:`_match_active`, but also rejects an expired record.

        Identical hash matching, then :func:`_record_expired` filters a matched
        record whose ``expires_at`` has passed (``now`` supplied by the caller).
        A legacy record with no expiry is returned exactly as ``_match_active``
        would, so resolving a legacy key stays byte-for-byte the same result.
        """
        if not plaintext:
            return None
        candidate = _hash_key(plaintext)
        for record in active_records:
            if hmac.compare_digest(record.get("key_hash", ""), candidate):
                if _record_expired(record, now):
                    return None
                return _public_record(record)
        return None

    @staticmethod
    def _mint_rotated_record(
        source: PublicKeyRecord, id_field: str = "id"
    ) -> tuple[str, KeyRecord]:
        """Build a replacement ``(plaintext, record)`` pair carrying *source*'s identity.

        Copies ``owner_subject``/``roles``/``scopes``/``team_id``/``expires_at``
        and ``label`` field-for-field. A field ABSENT on *source* stays absent on
        the replacement — never defaulted to ``None``/``[]`` — so a legacy key
        rotates into another legacy key and the scopes three-state law survives
        a rotation unchanged rather than being silently narrowed or widened.
        """
        plaintext, record = KeyStoreBase._mint_record(source.get("label", ""), id_field=id_field)
        if "owner_subject" in source:
            record["owner_subject"] = source["owner_subject"]
        if "roles" in source:
            record["roles"] = source["roles"]
        if "scopes" in source:
            record["scopes"] = source["scopes"]
        if "team_id" in source:
            record["team_id"] = source["team_id"]
        if "expires_at" in source:
            record["expires_at"] = source["expires_at"]
        return plaintext, record

    @staticmethod
    def _mint_scoped_record(
        label: str,
        id_field: str,
        *,
        owner_subject: str | None,
        roles: list[str] | None,
        scopes: list[str] | None,
        team_id: str | None,
        expires_at: str | None,
    ) -> tuple[str, KeyRecord]:
        """Build a fresh ``(plaintext, record)`` pair, stamping only PASSED fields.

        The identity-aware sibling of :meth:`_mint_record`. ``owner_subject``/
        ``team_id``/``expires_at`` treat ``None`` as "omitted" (no meaningful
        blocked-all state exists for them). ``roles``/``scopes`` are NOT
        symmetric with those three: ``None`` still means "omitted" (the record
        reads back as legacy/default), but ``[]`` is a genuine explicit value —
        "grant no roles" / "explicitly no scopes" — and is written through
        unchanged rather than treated as omission. A caller that wants the
        three-state law to start AT mint time (not just at read time) passes
        ``scopes=[]``; a caller that wants a legacy unrestricted key passes
        ``scopes=None`` or omits it.
        """
        plaintext, record = KeyStoreBase._mint_record(label, id_field=id_field)
        if owner_subject is not None:
            record["owner_subject"] = owner_subject
        if roles is not None:
            record["roles"] = roles
        if scopes is not None:
            record["scopes"] = scopes
        if team_id is not None:
            record["team_id"] = team_id
        if expires_at is not None:
            record["expires_at"] = expires_at
        return plaintext, record

    @abc.abstractmethod
    def create_key(self, label: str) -> tuple[str, PublicKeyRecord]:
        """Mint a new key.

        Returns ``(plaintext_key, record_without_hash)``. The plaintext is
        returned exactly once and is never persisted or recoverable.
        """

    @abc.abstractmethod
    def create_scoped_key(
        self,
        label: str,
        *,
        owner_subject: str | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        team_id: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, PublicKeyRecord]:
        """Mint a new key, optionally stamped with identity/authorization fields.

        The identity-aware sibling of :meth:`create_key` — a SEPARATE method
        rather than new keyword params bolted onto ``create_key``, so
        ``create_key``'s signature and behavior stay byte-identical for every
        existing caller. Calling this with every keyword omitted mints a record
        indistinguishable from ``create_key(label)`` (legacy, unrestricted). See
        :meth:`_mint_scoped_record` for the ``None``-vs-``[]`` distinction on
        ``roles``/``scopes``. Returns ``(plaintext_key, record_without_hash)``,
        exactly like :meth:`create_key`.
        """

    @abc.abstractmethod
    def list_keys(self) -> list[PublicKeyRecord]:
        """Return metadata for all keys — never the hash or plaintext."""

    @abc.abstractmethod
    def revoke_key(self, key_id: str) -> bool:
        """Revoke a key by ID. Returns ``True`` if a key was revoked."""

    @abc.abstractmethod
    def verify_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the active record matching ``plaintext``, else ``None``.

        A revoked key never matches. The returned record excludes the hash.
        Deliberately does NOT consult ``expires_at`` — its behavior is frozen for
        the existing callers; expiry-aware resolution is :meth:`resolve_key`.
        """

    @abc.abstractmethod
    def resolve_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the matched active, unexpired record, else ``None``.

        The expiry-aware sibling of :meth:`verify_key`: a revoked key never
        matches (same as verify) AND a matched record past its ``expires_at`` is
        treated as invalid. The returned public record carries whatever optional
        identity fields (owner/roles/scopes/team) the record holds, so a caller
        learns WHICH record authenticated — the seam the app-side AuthKit maps to
        a principal. For a legacy record (no scopes/roles/expiry) this returns the
        same record ``verify_key`` would, so key auth is unchanged for them.
        """

    @abc.abstractmethod
    def rotate_key(self, key_id: str) -> tuple[str, PublicKeyRecord] | None:
        """Mint a replacement for *key_id* and revoke the original. ``None`` if unknown.

        Returns ``(new_plaintext, new_public_record)`` — the plaintext is shown
        exactly once, here, exactly like :meth:`create_key`'s; the caller must
        display/store it immediately, nothing about it is retained. The
        replacement carries the SAME ``owner_subject``/``roles``/``scopes``/
        ``team_id``/``expires_at``/``label`` as the original (via
        :meth:`_mint_rotated_record`) under a fresh id and secret. Each driver's
        docstring documents its own atomicity limits for the mint-then-revoke
        pair — see :class:`KeyStore` and ``MongoKeyStore``.
        """

    def list_keys_for_owner(self, subject: str) -> list[PublicKeyRecord]:
        """Metadata for every key whose ``owner_subject`` equals *subject*.

        Default implementation: filters :meth:`list_keys` (never the hash). A
        driver that can push the filter into its query (Mongo) overrides this
        for efficiency; the JSON driver has no cheaper option than the full scan
        :meth:`list_keys` already does, so it uses this default unchanged.
        """
        return [record for record in self.list_keys() if record.get("owner_subject") == subject]

    def revoke_keys_for_owner(self, subject: str) -> int:
        """Revoke every ACTIVE key owned by *subject*. Returns the count revoked.

        Default implementation: composed from :meth:`list_keys_for_owner` +
        :meth:`revoke_key` — already-revoked keys are skipped (not counted), so
        this is an honest "how many did I just kill" number for a caller like a
        SCIM deprovision hook. A driver that can express this as one write
        (Mongo's ``update_many``) overrides it for a tighter atomicity story —
        see ``MongoKeyStore.revoke_keys_for_owner``.
        """
        swept = 0
        for record in self.list_keys_for_owner(subject):
            if record.get("revoked_at") is None and self.revoke_key(record["id"]):
                swept += 1
        return swept

    def purge_expired(self, now: datetime) -> int:
        """Revoke every ACTIVE record whose ``expires_at`` is at/before *now*.

        A bounded, single-pass sweep the API calls on its own schedule (a route,
        a cron trigger) — this store never schedules itself, no background
        thread. *now* decides which records read as expired (so a test can pin
        it, mirroring :func:`_record_expired`'s own contract); the REVOCATION
        timestamp each swept record gets is :meth:`revoke_key`'s own wall clock,
        same as any other revocation this store performs, so a swept-expired key
        and a deliberately-revoked one are indistinguishable in ``revoked_at`` —
        only ``expires_at`` explains why. Reuses :func:`_record_expired`'s
        parsing (including its fail-closed rule for an unparseable expiry)
        rather than re-deriving date comparison in a driver query, which is why
        this has no Mongo override: a query-pushdown version would have to
        re-implement that same fail-closed parsing and risk drifting from it.
        Already-revoked and unexpired records are untouched and not counted.
        """
        swept = 0
        for record in self.list_keys():
            if record.get("revoked_at") is not None:
                continue
            if _record_expired(record, now) and self.revoke_key(record["id"]):
                swept += 1
        return swept


# ---------------------------------------------------------------------------
# Filesystem (JSON) driver
# ---------------------------------------------------------------------------


class KeyStore(KeyStoreBase):
    """Filesystem-backed API key storage.

    Persists to ``$MEWBO_HOME/api_keys.json`` (the writable data dir, resolved
    via :func:`resolve_mewbo_home`). Writes are atomic (tmp file + ``os.replace``)
    to avoid corrupting the file on concurrent or interrupted writes.
    """

    def __init__(self, path: str | None = None) -> None:
        """Initialize the store, ensuring the parent directory exists."""
        if path is None:
            path = str(resolve_mewbo_home() / "api_keys.json")
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _load(self) -> list[KeyRecord]:
        """Load all key records from disk, or an empty list."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        keys = data.get("keys", [])
        return keys if isinstance(keys, list) else []

    def _save(self, keys: list[KeyRecord]) -> None:
        """Persist key records atomically (tmp file + replace)."""
        tmp = f"{self.path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"keys": keys}, handle, indent=2)
        os.replace(tmp, self.path)

    def create_key(self, label: str) -> tuple[str, PublicKeyRecord]:
        """Mint a new key — the legacy, unscoped form of :meth:`create_scoped_key`.

        Every optional identity/authorization keyword omitted is exactly
        what makes a record indistinguishable from a legacy key (see
        :meth:`create_scoped_key`'s docstring), so this delegates rather than
        re-implementing the mint-append-save sequence a second time.
        """
        return self.create_scoped_key(label)

    def create_scoped_key(
        self,
        label: str,
        *,
        owner_subject: str | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        team_id: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, PublicKeyRecord]:
        """Mint a new key, optionally stamped with identity/authorization fields."""
        plaintext, record = self._mint_scoped_record(
            label,
            "id",
            owner_subject=owner_subject,
            roles=roles,
            scopes=scopes,
            team_id=team_id,
            expires_at=expires_at,
        )
        keys = self._load()
        keys.append(record)
        self._save(keys)
        return plaintext, _public_record(record)

    def list_keys(self) -> list[PublicKeyRecord]:
        """Return metadata for all keys (no hashes)."""
        return [_public_record(record) for record in self._load()]

    def revoke_key(self, key_id: str) -> bool:
        """Mark a key revoked. Returns ``True`` if a matching key was found."""
        keys = self._load()
        for record in keys:
            if record.get("id") == key_id:
                if record.get("revoked_at") is None:
                    record["revoked_at"] = utc_now_iso()
                    self._save(keys)
                return True
        return False

    def verify_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the active record matching ``plaintext``, else ``None``."""
        active = (r for r in self._load() if r.get("revoked_at") is None)
        return self._match_active(plaintext, active)

    def resolve_key(self, plaintext: str) -> PublicKeyRecord | None:
        """Return the matched active, unexpired record, else ``None``."""
        active = (r for r in self._load() if r.get("revoked_at") is None)
        return self._match_active_unexpired(plaintext, active, datetime.now(timezone.utc))

    def rotate_key(self, key_id: str) -> tuple[str, PublicKeyRecord] | None:
        """Mint a replacement for *key_id* and revoke the original. ``None`` if unknown.

        Atomicity: the mint and the original's revocation are folded into ONE
        in-memory list, written by a SINGLE ``_save`` call (append the new
        record, stamp ``revoked_at`` on the old, one atomic ``os.replace``). For
        this driver that is genuinely atomic — a crash mid-write leaves the PRIOR
        file untouched (both keys exactly as they were), never a half-rotated
        state where the replacement exists but the original wasn't revoked, or
        vice versa.
        """
        keys = self._load()
        source = next((r for r in keys if r.get("id") == key_id), None)
        if source is None:
            return None
        plaintext, new_record = self._mint_rotated_record(source)
        if source.get("revoked_at") is None:
            source["revoked_at"] = utc_now_iso()
        keys.append(new_record)
        self._save(keys)
        return plaintext, _public_record(new_record)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_key_store(path: str | None = None) -> KeyStoreBase:
    """Return the configured key store driver.

    Reuses the same ``storage.driver`` config the session store reads so the
    key store follows the same backend as sessions (json or mongodb).

    ``path`` is honored only by the json driver (the on-disk store location);
    it is ignored when the configured driver is ``mongodb``.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.key_store_mongo import MongoKeyStore

        try:
            return MongoKeyStore()
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return KeyStore(path=path)


__all__ = [
    "KeyRecord",
    "PublicKeyRecord",
    "KeyScopes",
    "KeyStoreBase",
    "KeyStore",
    "create_key_store",
]
