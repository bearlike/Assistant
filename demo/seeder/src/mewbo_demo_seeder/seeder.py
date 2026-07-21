#!/usr/bin/env python3
"""The demo seeder — one atomic class that writes a bundle into live stores."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mewbo_core.key_store_mongo import MongoKeyStore
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.triggers.store import TriggerStoreBase

from mewbo_demo_seeder.models import SeedApiKey, SeedBundle, SeedSession, SeedTrigger

# Salt for a seeded key's stored hash. It is never rendered and never returned,
# so no value visible in a shot is the plaintext for the hash beside it — the
# seeded keys are deliberately unusable for authentication.
_KEY_HASH_SALT = "mewbo-demo-unusable-key:"


@dataclass
class SeedReport:
    """What a :meth:`DemoSeeder.seed` run wrote — the ids of seeded rows.

    A plain dataclass (not Pydantic): it never crosses a trust boundary, it is
    an in-process return value only.
    """

    sessions: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    api_keys: list[str] = field(default_factory=list)


class DemoSeeder:
    """Idempotently materialise a :class:`SeedBundle` into a session + trigger store.

    Collaborators are injected as FIELDS — the two stores, the parsed bundle,
    and the frozen ``t0`` — so a test drives the real ``seed()`` against
    mongomock-backed stores with a fixed clock and never patches time. ``seed``
    clears only the ids the bundle owns, then rebases every offset against
    ``t0`` and writes THROUGH the existing store contracts.
    """

    def __init__(
        self,
        *,
        session_store: SessionStoreBase,
        trigger_store: TriggerStoreBase,
        bundle: SeedBundle,
        t0: datetime,
        key_store: MongoKeyStore | None = None,
    ) -> None:
        """Store the injected collaborators and the frozen ``t0`` clock.

        ``key_store`` is optional so the existing unit tests (sessions +
        triggers against mongomock) keep constructing the seeder unchanged; a
        bundle carrying ``api_keys`` without a key store fails loudly in
        :meth:`seed` rather than silently skipping them.
        """
        self._sessions = session_store
        self._triggers = trigger_store
        self._bundle = bundle
        self._t0 = t0
        self._keys = key_store

    def seed(self) -> SeedReport:
        """Write every session, trigger and API key in the bundle."""
        report = SeedReport()
        for session in self._bundle.sessions:
            self._seed_session(session)
            report.sessions.append(session.id)
        for trigger in self._bundle.triggers:
            self._seed_trigger(trigger)
            report.triggers.append(trigger.id)
        if self._bundle.api_keys:
            if self._keys is None:
                raise ValueError(
                    "bundle declares api_keys but no key_store was injected — "
                    "the Security settings shot would render an empty list"
                )
            for api_key in self._bundle.api_keys:
                self._seed_api_key(api_key)
                report.api_keys.append(api_key.id)
        return report

    def _seed_session(self, session: SeedSession) -> None:
        """Materialise one session's document, events, title, and summary."""
        sid = session.id
        # Materialise the sessions doc first so ``list_sessions`` (which reads the
        # sessions collection, not the events log) can see it — then idempotently
        # drop every prior event so a re-seed is byte-identical.
        # ``truncate_after(sid, "")`` is the store contract for "delete all
        # events": every ISO-8601 ts sorts strictly after the empty string.
        self._sessions.ensure_session(sid)
        self._sessions.truncate_after(sid, "")

        agent_id = self._root_agent_id(sid)
        start = self._t0 + timedelta(seconds=session.offset_seconds)
        for event in session.events:
            ts = start + timedelta(seconds=event.at_seconds)
            # ``append_event`` stamps its own ts UNLESS the event carries one; the
            # rebased ts we pass overrides it (record = {"ts": now, **event}), which
            # is what gives a seeded transcript historical timestamps.
            self._sessions.append_event(
                sid,
                event.to_event(
                    ts,
                    session_model=session.model,
                    agent_id=agent_id,
                    session_id=sid,
                ),
            )

        self._sessions.save_title(sid, session.title)
        if session.summary:
            self._sessions.save_summary(sid, session.summary)

    def _seed_trigger(self, trigger: SeedTrigger) -> None:
        """Create-or-replace one trigger by id, entirely through the store contract."""
        spec = trigger.to_spec(self._t0, session_id=trigger.session)
        # Idempotent: a re-run replaces the existing row rather than colliding on
        # the store's unique ``id`` index.
        if self._triggers.get(spec.id) is not None:
            self._triggers.update(spec)
        else:
            self._triggers.create(spec)

    def _seed_api_key(self, api_key: SeedApiKey) -> None:
        """Upsert one issued-key record with a FIXED id and a rebased timestamp.

        This is the one seeder path that writes a document rather than calling
        the store contract, and the reason is intrinsic rather than convenient:
        ``KeyStoreBase.create_key`` mints ``uuid4().hex`` and stamps
        ``utc_now_iso()``, so every re-seed would produce a different id and a
        different "Created" line — the issued-keys list could never be
        byte-identical. There is no contract seam that accepts a caller-supplied
        id, so determinism requires writing the record. The document SHAPE is
        still the store's own ``KeyRecord`` (``_id``/``label``/``key_hash``/
        ``created_at``/``revoked_at``), read back through ``list_keys`` and
        ``_normalize`` exactly like a real key.
        """
        assert self._keys is not None  # guarded by seed()
        created = self._t0 + timedelta(seconds=api_key.created_at_offset)
        revoked = (
            None
            if api_key.revoked_at_offset is None
            else (self._t0 + timedelta(seconds=api_key.revoked_at_offset)).isoformat()
        )
        record = {
            "_id": api_key.id,
            "label": api_key.label,
            "key_hash": hashlib.sha256(
                (_KEY_HASH_SALT + api_key.id).encode("utf-8")
            ).hexdigest(),
            "created_at": created.isoformat(),
            "revoked_at": revoked,
        }
        # Idempotent by id so a re-seed replaces rather than duplicates.
        self._keys._col().replace_one({"_id": api_key.id}, record, upsert=True)

    @staticmethod
    def _root_agent_id(session_id: str) -> str:
        """Derive a stable 12-hex root agent id from a session id.

        Real root agents use ``uuid4().hex[:12]``; deriving one deterministically
        keeps the seeded ``tool_result``/``agent_message`` agent-id badges the same
        SHAPE as a real run without being random (so re-seeds stay byte-identical).
        """
        return hashlib.sha1(session_id.encode()).hexdigest()[:12]


__all__ = ["DemoSeeder", "SeedReport"]
