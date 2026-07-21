#!/usr/bin/env python3
"""Structured, bounded description of why a run failed.

A run that dies inside the LLM call chain used to surface as one flat,
UNCAPPED string on the completion event (``error``/``last_error``) — whatever
the provider stack happened to produce. A live session emitted 5,887
characters there because LiteLLM embedded an entire upstream HTML error page
into the exception message, which then rode the payload into every client and
into the next run's ``recent_events`` bullet list.

:class:`RunError` is the bounded replacement, and it is ONE atomic model: the
classification table, the markup guard and every length clamp are members of
it, validated at definition, so no call site can construct an unbounded or
markup-bearing value. ``error``/``last_error`` stay on the payload (Aura and
the CLI read them) as :meth:`RunError.brief` output; the structured record
rides the additive ``error_detail`` key.

THE LOAD-BEARING RULE: ``kind`` and ``title`` derive from the EXCEPTION TYPE
and the LiteLLM error-class NAME — never from the response body. Every read of
a message goes through :meth:`_body_free_head`, which cuts an HTML body at the
first ``<`` (and leaves a non-HTML message whole, since a ``<`` there is
arithmetic, not markup), and an HTML body forces a SYNTHESIZED title instead of
a derived one. An upstream error page's ``<title>`` routinely names the
operator's own internal infrastructure; lifting it into a title would republish
that through every client and every stored payload — which is also why
:meth:`_strip_markup` DROPS a markup-bearing title outright instead of
stripping the delimiters and keeping what was between them.

The model imports no I/O — the exception and the model name arrive as
arguments, so a test drives every path without a provider, a clock or a
network.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Ceiling on the retained diagnostic. Generous enough to keep a real stack of
# provider messages readable, small enough that a runaway HTML page cannot
# dominate a stored event.
_DETAIL_MAX_CHARS = 10_000

# A title is a one-line label, not a message — it renders in a card header.
_TITLE_MAX_CHARS = 120

# A model/deployment name is short; a joined fallback-ladder list is the long
# case. Bounded because this renders in a badge.
_PROVIDER_MAX_CHARS = 200

# Ceiling on the legacy flat ``error``/``last_error`` keys. Matches the
# orchestrator's synthetic-closure budget so both bounded surfaces agree.
_BRIEF_MAX_CHARS = 500

# How far to walk an exception's ``last_error``/``__cause__``/``__context__``
# chain looking for a classifiable type.
_CHAIN_MAX_DEPTH = 5

_FALLBACK_TITLE = "Run failed"

# Case-insensitive markers that identify a response body as an HTML document.
_HTML_MARKERS = ("<!doctype", "<html", "<head")

# Excluding ``<`` from the class (not just ``>``) keeps this LINEAR. With
# ``<[^>]*>`` a long run of unclosed ``<`` backtracks quadratically — 32k of
# them took 1.26s, 200k did not finish.
_TAG_RE = re.compile(r"<[^<>]*>")

# A LiteLLM/stdlib error-class NAME as it appears in a rendered message
# (``litellm.BadGatewayError: ...``). Names only — this never matches body text.
# ``\w*`` must be allowed to match EMPTY: litellm renders a bare ``Timeout``
# (no prefix), which a mandatory leading character would silently skip.
_CLASS_TOKEN_RE = re.compile(r"\b(\w*(?:Error|Exception|Timeout))\b")

# ``LlmResilienceExhausted``'s own format string. Parsing it is parsing OUR
# message, not the provider's response.
_MODELS_TRIED_RE = re.compile(r"failed on all models \(([^)]{1,200})\)")
_WRAPPER_PREFIX_RE = re.compile(r"^LLM call failed on all models \([^)]*\):\s*")

# The tool-use loop's own marker for a failed tool result (``f"ERROR: {exc}"``),
# which is what lands in the sticky ``TaskQueue.last_error``.
_TOOL_RESULT_PREFIX = "ERROR: "

def render_exception(exc: BaseException, *, limit: int = 500) -> str:
    """Render *exc* as ``"<Type>: <msg>"``, never as the empty string.

    Several exception classes stringify to ``""`` (``TimeoutError`` is the one
    that bites hardest — a bare ``str(exc)`` is invisible in a log line), so
    the type name is the floor: it degrades to the bare name when the message
    is empty rather than preserving the void. Shared by
    ``llm_resilience.py:LlmResilienceExhausted.describe_error`` and
    ``components.py:_span_status_message`` — one rendering rule, so the two
    records of a single failure can never disagree about how much of it
    survives.
    """
    name = type(exc).__name__
    text = str(exc).strip()
    rendered = f"{name}: {text}" if text else name
    return rendered[:limit]


RunErrorKind = Literal[
    "upstream_bad_gateway",
    "rate_limited",
    "timeout",
    "auth",
    "context_overflow",
    "provider_unavailable",
    "tool_failure",
    "unknown",
]

# Exception/error-class NAME -> kind. Name-keyed rather than isinstance-keyed
# so classification needs no litellm import and still works on a message whose
# exception object is long gone (the sticky ``last_error`` path).
_KIND_BY_CLASS: dict[str, RunErrorKind] = {
    "BadGatewayError": "upstream_bad_gateway",
    "RateLimitError": "rate_limited",
    "Timeout": "timeout",
    "TimeoutError": "timeout",
    "APITimeoutError": "timeout",
    "AuthenticationError": "auth",
    "PermissionDeniedError": "auth",
    "ContextWindowExceededError": "context_overflow",
    "InternalServerError": "provider_unavailable",
    "ServiceUnavailableError": "provider_unavailable",
    "APIConnectionError": "provider_unavailable",
    "ToolInputError": "tool_failure",
    "CommandError": "tool_failure",
}

# Canonical upstream status per kind, consulted ONLY to synthesize the title
# for an HTML error page. Derived from the kind, never read off the body.
_STATUS_BY_KIND: dict[RunErrorKind, int] = {
    "upstream_bad_gateway": 502,
    "provider_unavailable": 503,
    "rate_limited": 429,
    "auth": 401,
    "timeout": 504,
}


class RunError(BaseModel):
    """Why a run failed, classified and length-bounded at definition."""

    model_config = ConfigDict(extra="forbid")

    kind: RunErrorKind = Field(
        description="Coarse failure class, derived from the exception type.",
    )
    title: str = Field(
        description="One-line, markup-free label safe to render in a card header.",
    )
    provider: str | None = Field(
        default=None,
        description="Model/deployment the call was attempted against, when known.",
    )
    detail: str = Field(
        description=f"Diagnostic message, capped at {_DETAIL_MAX_CHARS} characters.",
    )
    # Both are DERIVED from ``detail`` by ``_clamp_detail`` before validation
    # runs, so these defaults are unreachable whenever ``detail`` is a string —
    # they exist so a caller (and a type checker) need not restate a value the
    # model computes for itself.
    detail_chars: int = Field(
        default=0,
        ge=0,
        description="Length of the diagnostic BEFORE capping.",
    )
    truncated: bool = Field(
        default=False,
        description="True when ``detail`` was capped and is shorter than the original.",
    )

    # ------------------------------------------------------------------
    # Validation at definition
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _clamp_detail(cls, data: object) -> object:
        """Cap ``detail``, then derive ``detail_chars``/``truncated`` from it.

        A supplied ``detail_chars`` is honoured ONLY when CONSISTENT with the
        text handed over — that is the round-trip case, where ``detail`` was
        already capped and the count records the genuine pre-cap length. An
        inconsistent pair is re-derived rather than trusted: taking
        ``truncated`` at face value let a payload describe itself into a state
        the console renders wrongly (its "showing N of M" affordance vanishes),
        and presence alone is not consistency.

        ``truncated`` is now a pure function of ``(detail_chars, kept)``, so the
        three fields cannot disagree by construction.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("detail")
        if not isinstance(raw, str):
            return data
        # Copy before mutating: ``model_validate(mongo_doc)`` must not truncate
        # the caller's ``detail`` in place or inject derived keys into their
        # mapping. Validation is a read of the input, not a rewrite of it.
        data = dict(data)
        original = len(raw)
        kept = min(original, _DETAIL_MAX_CHARS)
        supplied = data.get("detail_chars")
        detail_chars = original
        # ``bool`` is an ``int`` subclass — exclude it, or ``truncated=True``
        # read as a count of 1.
        if isinstance(supplied, int) and not isinstance(supplied, bool):
            # Below the cap the text was NOT capped, so the count must match
            # exactly; at the cap it may legitimately record a longer original.
            if supplied >= original if original >= _DETAIL_MAX_CHARS else supplied == original:
                detail_chars = supplied
        data["detail"] = raw[:_DETAIL_MAX_CHARS]
        data["detail_chars"] = detail_chars
        data["truncated"] = detail_chars > kept
        return data

    @field_validator("title")
    @classmethod
    def _strip_markup(cls, value: str) -> str:
        """Drop a markup-bearing ``title`` wholesale; never sanitize one.

        Stripping the delimiters and keeping what is between them is NOT a
        guard — it preserves the payload. An upstream error page's
        ``<title>`` names the operator's internal infrastructure, so
        ``<title>502 - llm.internal.example</title>`` would survive as
        ``502 - llm.internal.example`` and render as the card headline: exactly
        what THE LOAD-BEARING RULE forbids, with the delimiters gone to hide it.

        A title is a LABEL. There is nothing worth salvaging out of a
        markup-bearing one, so it degrades to the fallback. A bare ``<`` with
        no closing ``>`` is arithmetic (``max_tokens < min_tokens``), not
        markup, and is kept.

        Enforced here rather than at the construction seam so a direct
        ``RunError(...)`` or a ``model_validate`` of a stored payload cannot
        reintroduce what :meth:`from_exception` refuses to produce.
        """
        if _TAG_RE.search(value):
            return _FALLBACK_TITLE
        text = " ".join(value.split())
        if len(text) > _TITLE_MAX_CHARS:
            text = text[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
        return text or _FALLBACK_TITLE

    @field_validator("provider")
    @classmethod
    def _clean_provider(cls, value: str | None) -> str | None:
        """Bound ``provider`` and hold it to the same markup rule as ``title``.

        It renders in a badge and it is the one field that was unvalidated:
        ``_models_tried`` joins a fallback ladder with no cap. A markup-bearing
        value is not a model name, so it degrades to ``None`` rather than to a
        sanitized string that would still carry its payload.
        """
        if value is None:
            return None
        if _TAG_RE.search(value):
            return None
        text = " ".join(value.split())
        if len(text) > _PROVIDER_MAX_CHARS:
            text = text[: _PROVIDER_MAX_CHARS - 1].rstrip() + "…"
        return text or None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_exception(cls, exc: BaseException, *, model: str | None = None) -> RunError:
        """Classify *exc* into a bounded :class:`RunError`.

        ``model`` is the caller's configured model name, used only when the
        exception itself does not name the models it tried.
        """
        return cls._build(
            class_names=cls._chain_class_names(exc),
            message=str(exc),
            provider=cls._models_tried(exc),
            model=model,
        )

    @classmethod
    def from_message(cls, message: str, *, model: str | None = None) -> RunError:
        """Classify a bare error STRING — the sticky ``TaskQueue.last_error``.

        A mid-run tool failure leaves a string behind, not an exception object,
        so the exception-type signal is unavailable; classification falls back
        to the error-class names rendered into the message head.
        """
        return cls._build(class_names=(), message=message, provider=None, model=model)

    @classmethod
    def _build(
        cls,
        *,
        class_names: tuple[str, ...],
        message: str,
        provider: str | None,
        model: str | None,
    ) -> RunError:
        """Assemble a :class:`RunError` from already-extracted signals."""
        head = cls._body_free_head(message)
        kind = cls._classify(class_names, head)
        if cls._looks_like_html(message):
            title = cls._html_page_title(kind)
        else:
            title = cls._title_from_head(head) or _FALLBACK_TITLE
        resolved = provider or cls._provider_from_head(head) or model
        return cls(kind=kind, title=title, provider=resolved, detail=message)

    # ------------------------------------------------------------------
    # Signal extraction — every message read is body-free by construction
    # ------------------------------------------------------------------

    @classmethod
    def _body_free_head(cls, message: str) -> str:
        """Return the slice of *message* that carries no response body.

        The single choke-point through which classification, title derivation
        and :meth:`brief` read a message. For an HTML body the cut at the first
        ``<`` is what makes "never derive from the response body" structural
        rather than incidental — an error-class name never contains ``<``.

        For anything else the message is returned WHOLE. Cutting
        unconditionally destroyed real diagnostics: ``max_tokens < min_tokens
        (8 < 16)`` became ``max_tokens``, and ``expected <int>, got str``
        became ``expected``. That truncation reached Aura, the CLI and the
        ``on_session_end`` hook, so the guard was costing more than it bought
        on the path where there is no body to guard against.
        """
        if not cls._looks_like_html(message):
            return message
        cut = message.find("<")
        return message if cut < 0 else message[:cut]

    @staticmethod
    def _looks_like_html(message: str) -> bool:
        """Return True when *message* carries an HTML document."""
        probe = message.lower()
        return any(marker in probe for marker in _HTML_MARKERS)

    @staticmethod
    def _chain_class_names(exc: BaseException) -> tuple[str, ...]:
        """Return type names along *exc*'s wrapped-exception chain, outermost first.

        ``LlmResilienceExhausted`` subclasses ``RuntimeError`` and carries the
        real provider exception on ``last_error``, so the classifiable type is
        one hop in — walk ``last_error``/``__cause__``/``__context__`` to reach it.
        """
        names: list[str] = []
        seen: set[int] = set()
        pending: list[BaseException] = [exc]
        while pending and len(names) < _CHAIN_MAX_DEPTH:
            current = pending.pop(0)
            if id(current) in seen:
                continue
            seen.add(id(current))
            names.append(type(current).__name__)
            for attr in ("last_error", "__cause__", "__context__"):
                nested = getattr(current, attr, None)
                if isinstance(nested, BaseException):
                    pending.append(nested)
        return tuple(names)

    @staticmethod
    def _models_tried(exc: BaseException) -> str | None:
        """Return the models an exhausted-chain exception recorded, if any."""
        tried = getattr(exc, "models_tried", None)
        if isinstance(tried, (list, tuple)):
            names = [str(name) for name in tried if str(name)]
            if names:
                return ", ".join(names)
        return None

    @staticmethod
    def _provider_from_head(head: str) -> str | None:
        """Return the model list embedded in our own exhausted-chain message."""
        match = _MODELS_TRIED_RE.search(head)
        return match.group(1).strip() or None if match else None

    @staticmethod
    def _classify(class_names: tuple[str, ...], head: str) -> RunErrorKind:
        """Map an exception chain (then message-rendered class names) to a kind."""
        for name in class_names:
            kind = _KIND_BY_CLASS.get(name)
            if kind is not None:
                return kind
        for token in _CLASS_TOKEN_RE.findall(head):
            kind = _KIND_BY_CLASS.get(token)
            if kind is not None:
                return kind
        if head.startswith(_TOOL_RESULT_PREFIX):
            return "tool_failure"
        return "unknown"

    @staticmethod
    def _html_page_title(kind: RunErrorKind) -> str:
        """Return a synthesized title for an HTML error page.

        The status comes from *kind*, never from the page: an upstream error
        page's own ``<title>`` names the operator's internal infrastructure.
        """
        status = _STATUS_BY_KIND.get(kind)
        if status is None:
            return "Upstream returned an HTML error page"
        return f"Upstream returned an HTML error page ({status})"

    @staticmethod
    def _title_from_head(head: str) -> str:
        """Condense a body-free message head into a one-line label.

        Drops our own exhausted-chain wrapper and the consecutive duplicate
        segments LiteLLM produces (``litellm.RateLimitError: RateLimitError:``).
        Length is clamped by the ``title`` validator, not here.
        """
        text = _WRAPPER_PREFIX_RE.sub("", head).strip()
        text = " ".join(text.split())
        if text.startswith("litellm."):
            text = text[len("litellm.") :]
        segments = text.split(": ")
        deduped = [
            seg
            for i, seg in enumerate(segments)
            if i == 0 or seg.strip() != segments[i - 1].strip()
        ]
        return ": ".join(deduped).strip().rstrip(",;-").strip()

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def brief(self) -> str:
        """Return the bounded, markup-free blurb for ``error``/``last_error``.

        Goes through the SAME guards as ``title`` — never a raw ``detail``
        slice. This is the field the non-console clients render (Aura's error
        card, the CLI read the flat key; only the console reads
        :attr:`title`), and it is PERSISTED on the completion event, so a raw
        slice would republish an upstream page's markup — and the internal
        infrastructure its ``<title>`` names — on every history replay.

        An HTML body degrades to :attr:`title`, since no part of such a
        message is safe to project. Anything else keeps the body-free head,
        which is the genuinely useful diagnostic (a provider's rate-limit
        reason, a timeout ceiling). :attr:`detail` is untouched either way —
        the full raw text stays available to the expandable view.

        Never longer than ``_BRIEF_MAX_CHARS`` INCLUDING the ellipsis.
        """
        if self._looks_like_html(self.detail):
            text = self.title
        else:
            text = self._body_free_head(self.detail).strip() or self.title
        if len(text) > _BRIEF_MAX_CHARS:
            return text[: _BRIEF_MAX_CHARS - 1] + "…"
        return text


__all__ = ["RunError", "RunErrorKind", "render_exception"]
