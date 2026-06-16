"""Unwrap anyio/``ExceptionGroup`` wrappers and classify the real cause.

The MCP SDK runs its session/transport under an anyio ``TaskGroup``. When a
connect/discover/call task fails, the failure surfaces as an
``ExceptionGroup`` whose ``str()`` renders the opaque
``"unhandled errors in a TaskGroup (1 sub-exception)"`` — hiding the real
cause (DNS, connection refused, auth, timeout). These helpers peel the group
to the innermost real exception, render an actionable message, and classify
the cause into a coarse, machine-filterable ``reason``.

This is the single shared home for the unwrap + classify primitives: the MCP
connection pool (`mcp_pool`) reuses them for its structured failure logs and
its quarantine/backoff policy (Gitea #130/#132), and the legacy one-shot path
(`mcp`) reuses them for its discovery/runtime failure logs.

Duck-typed on the ``exceptions`` attribute (a tuple of sub-exceptions) rather
than the builtin ``ExceptionGroup`` type, so it works on Python 3.10 (no
builtin group) and across anyio versions.
"""

from __future__ import annotations

import asyncio

# Coarse failure reasons, ordered roughly by how actionable they are. Consumed
# by the failure logs here and by the quarantine/backoff policy in the
# non-blocking-init work (Gitea #130): ``auth``/``config`` must never retry;
# ``dns``/``refused``/``timeout`` should back off.
REASON_DNS = "dns"
REASON_REFUSED = "refused"
REASON_TIMEOUT = "timeout"
REASON_AUTH = "auth"
REASON_CONFIG = "config"
REASON_OTHER = "other"


def _is_config_kwarg_error(exc: BaseException) -> bool:
    """True when *exc* indicates the MCP adapter rejected an unknown config key."""
    return isinstance(exc, TypeError) and "unexpected keyword argument" in str(exc)


def _subexceptions(exc: BaseException) -> tuple[BaseException, ...] | None:
    """Return the sub-exceptions of an ``ExceptionGroup``-like *exc*, else None."""
    subs = getattr(exc, "exceptions", None)
    if isinstance(subs, (tuple, list)) and subs:
        return tuple(subs)
    return None


def unwrap_exception_group(exc: BaseException) -> BaseException:
    """Peel single-child exception groups down to the innermost real cause.

    anyio/MCP transports run the session under a ``TaskGroup``; a failed
    connect surfaces as an ``ExceptionGroup`` whose ``str()`` is the opaque
    ``"unhandled errors in a TaskGroup (1 sub-exception)"``. Recursively
    unwrap single-child groups so callers see the actual error (DNS failure,
    connection refused, auth, timeout). Multi-child groups are returned
    unchanged so the caller can render every child. Guards against
    pathological self-referential groups.
    """
    seen: set[int] = set()
    current: BaseException = exc
    while True:
        if id(current) in seen:
            return current
        seen.add(id(current))
        subs = _subexceptions(current)
        if subs is None or len(subs) != 1:
            return current
        current = subs[0]


def describe_exception_group(exc: BaseException) -> str:
    """Render an actionable message naming the real cause(s).

    Single-cause chains collapse to the innermost exception's message; a
    multi-child group joins its children's ``repr`` so every distinct cause is
    named (never the opaque ``"... (N sub-exceptions)"`` wrapper). Falls back
    to ``repr`` when an exception carries no message.
    """
    inner = unwrap_exception_group(exc)
    subs = _subexceptions(inner)
    if subs is not None:
        # Multi-child group: name every distinct cause.
        return "; ".join(repr(sub) for sub in subs)
    return str(inner) or repr(inner)


def classify_connect_failure(exc: BaseException) -> str:
    """Bucket a connect/runtime failure into a coarse, actionable reason.

    Returns one of ``auth`` / ``config`` / ``dns`` / ``refused`` / ``timeout``
    / ``other``, computed on the UNWRAPPED cause. Drives the quarantine-vs-
    backoff decision (Gitea #130) and feeds the structured log reason (#132).
    """
    cause = unwrap_exception_group(exc)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or isinstance(
        cause, (TimeoutError, asyncio.TimeoutError)
    ):
        return REASON_TIMEOUT
    if _is_config_kwarg_error(cause):
        return REASON_CONFIG
    text = str(cause).lower()
    if any(
        token in text
        for token in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "authentication",
            "not authorized",
            "permission denied",
        )
    ):
        return REASON_AUTH
    if any(
        token in text
        for token in (
            "resolve host",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "getaddrinfo",
        )
    ):
        return REASON_DNS
    if any(
        token in text
        for token in (
            "connection refused",
            "refused",
            "connect call failed",
            "no route to host",
            "network is unreachable",
        )
    ):
        return REASON_REFUSED
    if "timed out" in text or "timeout" in text:
        return REASON_TIMEOUT
    return REASON_OTHER
