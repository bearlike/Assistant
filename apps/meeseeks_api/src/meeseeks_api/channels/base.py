"""Channel adapter protocol, registry, and shared data types."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    """Platform-agnostic inbound message from a chat channel."""

    platform: str
    channel_id: str
    thread_id: str | None
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: str
    room_name: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Protocol that each chat platform adapter implements.

    Three methods cover the full lifecycle: authenticate the inbound
    webhook, translate it to an ``InboundMessage``, and send a response
    back to the originating thread/channel.
    """

    platform: str

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Return True if the webhook request is authentic."""
        ...

    def parse_inbound(
        self, headers: dict[str, str], body: bytes
    ) -> InboundMessage | None:
        """Parse a raw webhook payload into an InboundMessage.

        Return ``None`` for non-message events (e.g. Update, Delete,
        Join) that should be silently acknowledged.
        """
        ...

    def send_response(
        self,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a text response back to the platform.

        Returns the message ID of the sent message, or ``None`` on
        failure.
        """
        ...

    @property
    def system_context(self) -> str:
        """Brief system prompt note about this chat interface.

        Injected into the LLM system prompt so it knows which client
        the conversation is flowing through.  Keep it short.
        """
        ...


class ChannelRegistry:
    """Lookup adapters by platform name."""

    def __init__(self) -> None:  # noqa: D107
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        """Register an adapter for its declared platform."""
        self._adapters[adapter.platform] = adapter

    def get(self, platform: str) -> ChannelAdapter | None:
        """Return the adapter for *platform*, or ``None``."""
        return self._adapters.get(platform)

    def platforms(self) -> list[str]:
        """Return all registered platform names."""
        return list(self._adapters)


class DeduplicationGuard:
    """In-memory set with TTL for webhook replay protection."""

    def __init__(self, ttl: float = 3600.0) -> None:  # noqa: D107
        self._seen: dict[str, float] = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen within the TTL window."""
        now = time.monotonic()
        with self._lock:
            # Prune expired entries lazily (keep it simple)
            if len(self._seen) > 10_000:
                cutoff = now - self._ttl
                self._seen = {
                    k: v for k, v in self._seen.items() if v > cutoff
                }
            if key in self._seen and (now - self._seen[key]) < self._ttl:
                return True
            self._seen[key] = now
            return False
