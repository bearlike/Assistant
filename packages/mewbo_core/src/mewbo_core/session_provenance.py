"""Session provenance — who/what spawned a session.

A session record carries no origin field: every session is created the same
way regardless of caller (see ``session_store``). Provenance is therefore
reconstructed from two durable signals written at creation time:

* the session's **tags** (e.g. ``wiki:job:<id>``, ``agentic_search:scg:<id>``,
  ``nextcloud-talk:room:<token>``) — the robust signal, present for every
  internally-spawned session and surviving even when the context event is
  empty (older wiki jobs stored no capabilities);
* the first ``context`` event's ``client_capabilities`` / ``source_platform``
  — the fallback when a tag is absent.

``SessionOrigin`` is the single place that maps those signals to a coarse
origin. The console badges and filters the landing page on this value, so the
enum members are stable wire strings.
"""

from __future__ import annotations

from enum import Enum


class SessionOrigin(str, Enum):
    """Coarse provenance of a session, derived from its tags + context."""

    USER = "user"
    WIKI = "wiki"
    SEARCH = "search"
    CHANNEL = "channel"

    @classmethod
    def classify(cls, tags: list[str], context: dict[str, object]) -> SessionOrigin:
        """Map a session's tags + merged context to an origin.

        Tags win over context because they are the more reliable signal.
        Prefixes are ordered most- to least-specific; channel tags are matched
        by the ``:room:`` / ``:thread:`` infix shared by every channel adapter.
        """
        tag_prefixes = (("wiki:", cls.WIKI), ("agentic_search:", cls.SEARCH))
        for tag in tags:
            for prefix, origin in tag_prefixes:
                if tag.startswith(prefix):
                    return origin
            if ":room:" in tag or ":thread:" in tag:
                return cls.CHANNEL
        if context.get("source_platform"):
            return cls.CHANNEL
        capabilities = context.get("client_capabilities")
        if isinstance(capabilities, list):
            if "wiki" in capabilities:
                return cls.WIKI
            if "scg" in capabilities:
                return cls.SEARCH
        return cls.USER
