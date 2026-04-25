#!/usr/bin/env python3
"""Capability-gating primitives for agents, skills, and plugins.

Session capabilities come from the client-advertised
``X-Meeseeks-Capabilities`` header (persisted on the session's context
event as ``client_capabilities``). Entries — agents, skills, or plugin
contributions — can declare ``requires_capabilities`` to stay hidden
from any session that hasn't advertised the matching capability.

Both functions are pure: no I/O, no state. They operate on tuples so
the results are hashable and safely cachable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Protocol, TypeVar


class _HasRequiresCapabilities(Protocol):
    requires_capabilities: tuple[str, ...]


_T = TypeVar("_T", bound=_HasRequiresCapabilities)


def parse_capabilities(raw: object) -> tuple[str, ...]:
    """Normalise frontmatter/manifest input into a deterministic tuple.

    Accepts ``None`` (→ ``()``), a single string (``"stlite"`` → ``("stlite",)``),
    or a list of strings (``["b", "a"]`` → ``("a", "b")`` — sorted, deduped,
    trimmed). Any other shape returns ``()``.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        value = raw.strip()
        return (value,) if value else ()
    if isinstance(raw, list):
        items = sorted({str(v).strip() for v in raw if v and str(v).strip()})
        return tuple(items)
    return ()


def filter_by_capabilities(
    items: Iterable[_T], session_capabilities: Iterable[str]
) -> list[_T]:
    """Return items whose ``requires_capabilities`` is a subset of the session's.

    Items with empty ``requires_capabilities`` are always included — they
    require nothing. The comparison uses set semantics; ordering of
    ``session_capabilities`` does not matter.
    """
    session = set(session_capabilities)
    return [
        item
        for item in items
        if not item.requires_capabilities
        or set(item.requires_capabilities).issubset(session)
    ]


def overlay_capabilities(spec: _T, extra: Iterable[str]) -> _T:
    """Return a copy of *spec* with *extra* unioned into ``requires_capabilities``.

    Used when a plugin declares bundle-level ``requires-capabilities`` that
    must fan out over every contributed agent and skill. A no-op when
    *extra* is empty; otherwise produces a sorted, deduped tuple.
    """
    extra_tuple = tuple(extra)
    if not extra_tuple:
        return spec
    merged = tuple(sorted({*extra_tuple, *spec.requires_capabilities}))
    # All concrete call-sites pass frozen dataclasses (AgentDef, SkillSpec);
    # the protocol can't enforce dataclass-ness, so narrow here.
    return replace(spec, requires_capabilities=merged)  # type: ignore[type-var]


__all__ = ["filter_by_capabilities", "overlay_capabilities", "parse_capabilities"]
