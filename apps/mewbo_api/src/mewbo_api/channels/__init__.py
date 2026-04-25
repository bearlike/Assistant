"""Chat platform channel adapters for inbound webhook integration."""

from mewbo_api.channels.base import (
    ChannelAdapter,
    ChannelRegistry,
    DeduplicationGuard,
    InboundMessage,
)

__all__ = [
    "ChannelAdapter",
    "ChannelRegistry",
    "DeduplicationGuard",
    "InboundMessage",
]
