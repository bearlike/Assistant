#!/usr/bin/env python3
"""Lossless pre-compaction utilities.

This module intentionally contains only one thing: a ``pre_compact`` hook
that strips ANSI escapes and truncates huge tool outputs before the LLM
summarizer sees them. Zero tokens, zero risk.

Prior versions also shipped ``should_compact`` (an event-count heuristic)
and ``summarize_events`` (a fallback "summary" that concatenated raw
event text). Both were deleted: compaction decisions are now driven
purely by the API-reported ``usage_metadata.input_tokens``, and failed
structured compaction must not be masked with raw-text noise.
"""

from __future__ import annotations

import re

from meeseeks_core.common import get_logger
from meeseeks_core.types import EventRecord

logging = get_logger(name="core.compaction")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MAX_RESULT_CHARS = 2000


def micro_compact_events(events: list[EventRecord]) -> list[EventRecord]:
    """Lossless pre-compaction: strip ANSI escapes and truncate large tool outputs.

    Intended for use as a ``pre_compact`` hook — no LLM call, zero cost.
    """
    compacted: list[EventRecord] = []
    for event in events:
        if event.get("type") != "tool_result":
            compacted.append(event)
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            compacted.append(event)
            continue
        result = payload.get("result")
        if not isinstance(result, str) or len(result) <= _MAX_RESULT_CHARS:
            compacted.append(event)
            continue
        payload = dict(payload)
        payload["result"] = _ANSI_RE.sub("", result[:_MAX_RESULT_CHARS]) + "\n[truncated]"
        compacted.append({**event, "payload": payload})
    return compacted
