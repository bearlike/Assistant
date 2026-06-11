#!/usr/bin/env python3
"""Capability-gating primitives for agents, skills, and plugins.

Session capabilities come from the client-advertised
``X-Mewbo-Capabilities`` header (persisted on the session's context
event as ``client_capabilities``). Entries — agents, skills, or plugin
contributions — can declare ``requires_capabilities`` to stay hidden
from any session that hasn't advertised the matching capability.

Both functions are pure: no I/O, no state. They operate on tuples so
the results are hashable and safely cachable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Protocol, TypeVar

from mewbo_core.common import get_logger

logging = get_logger(name="core.capabilities")


class _HasRequiresCapabilities(Protocol):
    requires_capabilities: tuple[str, ...]


_T = TypeVar("_T", bound=_HasRequiresCapabilities)

# A runtime predicate that, given the capabilities a session ALREADY advertised,
# returns extra capability ids to grant it (or ``()``). The signature is the
# advertised tuple so a provider can no-op when its capability is already present.
SessionCapabilityProvider = Callable[[tuple[str, ...]], Iterable[str]]

# Down-only push seam (mirrors ``plugins.register_builtin_root`` /
# ``scg.map_phase.MapPhaseSink``): an OPTIONAL capability library above core in
# the DAG registers a provider so a capability can be granted by a RUNTIME
# predicate (e.g. ``scg`` once the SCG is enabled AND a source is mapped),
# without core ever importing up to evaluate that predicate. Empty by default —
# a lean install grants nothing extra.
_CAPABILITY_PROVIDERS: list[SessionCapabilityProvider] = []


def register_session_capability_provider(provider: SessionCapabilityProvider) -> None:
    """Register a runtime provider that may grant extra session capabilities.

    Idempotent on identity and down-only (a library above core pushes here on
    import). Called by :func:`augment_session_capabilities` per session-init, so
    a freshly-mapped graph flips a live process without a restart.
    """
    if provider not in _CAPABILITY_PROVIDERS:
        _CAPABILITY_PROVIDERS.append(provider)


def reset_session_capability_providers() -> None:
    """Drop all registered providers (test isolation seam)."""
    _CAPABILITY_PROVIDERS.clear()


def augment_session_capabilities(advertised: tuple[str, ...]) -> tuple[str, ...]:
    """Union *advertised* with every registered provider's runtime grant.

    Each provider is best-effort: a raising provider is logged and skipped so a
    flaky predicate (an unreachable store) never breaks session init. Returns a
    sorted, deduped tuple so the result stays hashable + cache-stable like
    :func:`parse_capabilities`. A no-provider install returns *advertised*
    unchanged.
    """
    if not _CAPABILITY_PROVIDERS:
        return advertised
    granted: set[str] = set(advertised)
    for provider in _CAPABILITY_PROVIDERS:
        try:
            granted.update(str(c).strip() for c in provider(advertised) if str(c).strip())
        except Exception as exc:  # noqa: BLE001 — a predicate must never break init
            logging.warning(
                "session capability provider failed: {}: {}", type(exc).__name__, exc
            )
    return tuple(sorted(granted))


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


__all__ = [
    "SessionCapabilityProvider",
    "augment_session_capabilities",
    "filter_by_capabilities",
    "overlay_capabilities",
    "parse_capabilities",
    "register_session_capability_provider",
    "reset_session_capability_providers",
]
