#!/usr/bin/env python3
"""Helpers for optional components and observability integration."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mewbo_core.common import get_logger
from mewbo_core.config import get_config, get_config_value, get_version
from mewbo_core.contracts.run_error import render_exception
from mewbo_core.contracts.types import JsonValue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
    from langfuse.types import TraceContext
else:
    TraceContext = dict[str, str]

logging = get_logger(name="core.components")

# Cap on the exception text stamped as a span's ``status_message``. Matches the
# slice the tool-use loop already applies at its own exhaustion seam, so the two
# writers can never disagree about how much of a provider error a trace keeps.
_SPAN_STATUS_MESSAGE_MAX = 500

# The observation types the Langfuse SDK renders differently. Kept as a closed
# ``Literal`` rather than an enum because every caller and the SDK itself want
# the bare string, and a typo in one is otherwise only visible in the UI.
ObservationKind = Literal[
    "span",
    "generation",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "embedding",
    "guardrail",
]

# What the user axis carries when no principal reached this seam. Copying the
# session id there instead makes the two axes identical, which reads in the UI
# as "every session is its own user" — an honest unknown is the lesser loss.
ANONYMOUS_USER_ID = "anonymous"

# Names the OTel resource so exported spans stop arriving as ``unknown_service``.
_OTEL_SERVICE_NAME = "mewbo"

# Langfuse accepts a lowercase ``[a-z0-9_-]`` environment slug and reserves the
# ``langfuse`` prefix for itself; anything else is rejected at ingest.
_ENVIRONMENT_DISALLOWED = re.compile(r"[^a-z0-9_-]+")

_LANGFUSE_TRACE_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "langfuse_trace_context",
    default=None,
)
_LANGFUSE_SESSION_ID: ContextVar[str | None] = ContextVar("langfuse_session_id", default=None)
_LANGFUSE_USER_ID: ContextVar[str | None] = ContextVar("langfuse_user_id", default=None)


@dataclass(frozen=True)
class ComponentStatus:
    """Describe whether a component is enabled and why."""

    name: str
    enabled: bool
    reason: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


def resolve_langfuse_status() -> ComponentStatus:
    """Determine whether Langfuse callbacks are available and configured."""
    enabled, reason, metadata = get_config().langfuse.evaluate()
    return ComponentStatus(name="langfuse", enabled=enabled, reason=reason, metadata=metadata)


def build_langfuse_handler(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    version: str,
    release: str,
    trace_context: TraceContext | None = None,
) -> LangfuseCallbackHandler | None:
    """Create a Langfuse callback handler when configured."""
    status = resolve_langfuse_status()
    if not status.enabled:
        logging.debug("Langfuse disabled: {}", status.reason)
        return None

    config = get_config().langfuse
    _ensure_langfuse_client(config)

    from langfuse.langchain import CallbackHandler

    trace_context = trace_context or _LANGFUSE_TRACE_CONTEXT.get()
    session_id_value = _LANGFUSE_SESSION_ID.get() or session_id
    user_id_value = _LANGFUSE_USER_ID.get() or user_id

    try:
        handler = CallbackHandler(public_key=config.public_key or None, trace_context=trace_context)
        _attach_langfuse_metadata(
            handler,
            user_id=user_id_value,
            session_id=session_id_value,
            trace_name=trace_name,
            version=version,
            release=release,
        )
        return handler
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Langfuse initialization failed: {}", exc)
        return None


def langfuse_invoke_config(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
) -> dict[str, Any]:
    """Build the LangChain ``invoke``/``astream`` ``config`` that exports a trace.

    The "langfuse_metadata 3-line pattern" (see ``tool_use_loop``), extracted so
    the no-loop synthesis primitives (``StructuredSynthesizer`` / ``DraftStreamer``)
    export to Langfuse too. ``langfuse_session_context`` only *propagates
    attributes* — it creates no observation, so a model call that doesn't ATTACH
    the ``CallbackHandler`` produces zero exported spans (a known defect: realtime
    synthesis traced nothing, ever). Attaching the handler is the seam that makes
    the generation land in the session-grouped trace.

    Returns a ``config`` dict carrying ``callbacks`` (+ ``metadata`` when the
    handler exposes ``langfuse_metadata``), or ``{}`` when Langfuse is disabled /
    unavailable — pass it straight to ``model.ainvoke(messages, config=cfg or None)``.
    Cheap to call (no network, no flush) so it is safe on a latency-critical path;
    the handler batches/exports asynchronously.

    ``version`` / ``release`` are read from config here so callers stay 3 args.
    """
    handler = build_langfuse_handler(
        user_id=user_id,
        session_id=session_id,
        trace_name=trace_name,
        version=get_version(),
        release=get_config_value("runtime", "envmode", default="Not Specified"),
    )
    if handler is None:
        return {}
    config: dict[str, Any] = {"callbacks": [handler]}
    metadata = getattr(handler, "langfuse_metadata", None)
    if isinstance(metadata, dict) and metadata:
        config["metadata"] = metadata
    return config


def resolve_home_assistant_status() -> ComponentStatus:
    """Determine whether the Home Assistant tool is configured."""
    enabled, reason, metadata = get_config().home_assistant.evaluate()
    return ComponentStatus(
        name="home_assistant_tool",
        enabled=enabled,
        reason=reason,
        metadata=metadata,
    )


def format_component_status(statuses: Iterable[ComponentStatus]) -> str:
    """Format component statuses for inclusion in prompts."""
    lines: list[str] = []
    for status in statuses:
        state = "enabled" if status.enabled else "disabled"
        reason = f" ({status.reason})" if status.reason else ""
        lines.append(f"- {status.name}: {state}{reason}")
    return "\n".join(lines)


def _is_hex_trace_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", value))


def _build_langfuse_trace_context(
    session_id: str | None,
    invocation_id: str | None = None,
) -> TraceContext | None:
    """Pin the trace id for an invocation, or ``None`` to let OTel decide.

    An *invocation_id* pins one trace per invocation, so user idle time between
    messages never bloats a trace's duration; ``session_id`` is propagated
    separately via ``propagate_attributes``, which is what still groups those
    traces into one session.

    **Without an invocation id this returns ``None``, and that is the correct
    answer rather than a degradation.** ``None`` means "no explicit context":
    an enclosing span, if there is one, becomes the real OTel parent, and with
    nothing ambient the SDK starts a fresh trace — one per invocation, which is
    the goal.

    **A trace id must never be derived from the session id, by any route.** Two
    ways of doing that collapsed every run of a session onto one trace, and the
    less obvious one is what actually fired: hashing the session as a seed is
    identical on every call, but so is handing the session straight through when
    it already looks like a trace id — and a session id is minted as
    ``uuid4().hex``, which is exactly the 32-hex shape that test accepts. So the
    passthrough matched first and every run of a session shared one trace, with
    ``trace_id == session_id`` byte for byte. *session_id* is accepted only to
    keep the call sites honest about what they hold; it never names the trace.
    """
    if invocation_id:
        tid = invocation_id if _is_hex_trace_id(invocation_id) else uuid4().hex
        return cast(TraceContext, {"trace_id": tid})
    return None


@dataclass(frozen=True, slots=True)
class LangfuseTraceLink:
    """The trace — and the observation inside it — a new span must attach to.

    ``trace_context`` is the ONLY channel through which a span can name a
    parent observation, and naming one is not optional: given a ``trace_id``
    with no ``parent_span_id`` the SDK mints a RANDOM 16-hex span id, wraps it
    in a ``NonRecordingSpan`` and parents the new span to it, so the exported
    ``parentObservationId`` points at an observation that is never sent. Every
    span opened that way is an orphan by construction, at every depth — which
    is why a span tree built from those spans cannot be walked root→child and
    per-agent token attribution from a trace alone is impossible.

    The cure is to pass ``trace_context`` only where it buys something:

    - **Inside one task**, ambient OTel context already carries the enclosing
      span, so a nested span needs no ``trace_context`` at all — omitting it is
      what makes it a real child rather than a phantom-parented root.
    - **Across an ``asyncio.create_task`` boundary**, the spawning span is no
      longer the ambient one by the time the child opens its first span. There
      the link must be captured on the spawning side and handed over, which is
      the one case where an explicit ``parent_span_id`` is the right answer.
    - **With nothing ambient at all** (the first span of an invocation) the
      trace id still has to be pinned, so the phantom parent is unavoidable —
      it lands once, on a genuine trace root, instead of on every span.

    Every read of live tracer state is best-effort: tracing is a side effect at
    the edge, so a link that cannot be captured degrades to ``None`` and the
    span falls back to the behaviour it had before rather than failing a run.
    """

    trace_id: str
    parent_span_id: str | None = None

    @classmethod
    def from_trace_context(cls, trace_context: TraceContext | None) -> LangfuseTraceLink | None:
        """Read a link out of the SDK's ``TraceContext`` mapping, if it holds one."""
        if not trace_context:
            return None
        trace_id = trace_context.get("trace_id")
        if not trace_id:
            return None
        return cls(trace_id=trace_id, parent_span_id=trace_context.get("parent_span_id"))

    @classmethod
    def current(cls) -> LangfuseTraceLink | None:
        """The link bound to this context by :func:`langfuse_session_context`."""
        return cls.from_trace_context(_LANGFUSE_TRACE_CONTEXT.get())

    @classmethod
    def capture(cls) -> LangfuseTraceLink | None:
        """Capture the span a task started from HERE, before the task starts.

        Call this on the spawning side of an ``asyncio.create_task`` boundary,
        while the spawning span is still ambient. The live ambient observation
        wins; a context with no ambient span falls back to whatever link is
        already bound, so capturing twice down one spawn path is idempotent
        rather than parent-erasing.
        """
        bound = cls.current()
        trace_id, span_id = cls._ambient_ids()
        if trace_id and span_id:
            return cls(trace_id=trace_id, parent_span_id=span_id)
        return bound

    @staticmethod
    def _ambient_ids() -> tuple[str | None, str | None]:
        """``(trace_id, observation_id)`` of the live ambient span, best-effort."""
        try:
            from langfuse import get_client

            client = get_client()
            trace_id = client.get_current_trace_id()
            span_id = client.get_current_observation_id()
        except Exception:  # pragma: no cover - defensive
            return None, None
        if not isinstance(trace_id, str) or not isinstance(span_id, str):
            return None, None
        return trace_id, span_id

    def as_trace_context(self) -> TraceContext:
        """Render this link as the mapping the Langfuse SDK accepts."""
        ctx: dict[str, str] = {"trace_id": self.trace_id}
        if self.parent_span_id:
            ctx["parent_span_id"] = self.parent_span_id
        return cast(TraceContext, ctx)

    def trace_context_for_new_span(self) -> TraceContext | None:
        """What a span opening under this link should pass as ``trace_context``.

        ``None`` means "nest ambiently" — the enclosing span of this same trace
        is already current, so OTel parents the new span for free and passing a
        context would replace that real parent with a phantom one.
        """
        ambient_trace_id, ambient_span_id = self._ambient_ids()
        if ambient_span_id and ambient_trace_id == self.trace_id:
            return None
        return self.as_trace_context()

    @contextmanager
    def bind(self) -> Iterator[None]:
        """Bind this link for the duration of the block.

        ``asyncio.create_task`` copies the calling context, so a task created
        inside this block carries the link and opens its first span as a real
        child of the captured observation.
        """
        token = _LANGFUSE_TRACE_CONTEXT.set(self.as_trace_context())
        try:
            yield
        finally:
            _LANGFUSE_TRACE_CONTEXT.reset(token)


@contextmanager
def langfuse_child_task_link(link: LangfuseTraceLink | None = None) -> Iterator[None]:
    """Hand a parent span to tasks created inside this block.

    The one seam for the ``asyncio.create_task`` boundary. It takes both shapes
    the boundary comes in: pass a *link* captured earlier when the task is
    launched somewhere other than where it was spawned (a deferred unit), or
    omit it to capture the ambient span right here. Either way it degrades to a
    plain no-op when there is nothing to hand over — Langfuse disabled, or no
    trace bound to this context.
    """
    resolved = link if link is not None else LangfuseTraceLink.capture()
    if resolved is None:
        yield
        return
    with resolved.bind():
        yield


# -- Propagation & spans ------------------------------------------------
# ``langfuse_propagate`` is defined *before* ``langfuse_session_context``
# because the latter calls it.


@contextmanager
def langfuse_propagate(
    *,
    tags: list[str] | None = None,
    metadata: dict[str, str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    version: str | None = None,
) -> Iterator[None]:
    """Propagate Langfuse attributes to all child observations.

    Thin wrapper around ``langfuse.propagate_attributes`` that gracefully
    degrades when Langfuse is disabled or unavailable.

    *trace_name* is the ONE channel that names a trace from outside an
    observation: a trace otherwise inherits the name of whatever runnable opened
    its root span, which is why an untouched export is a wall of identically
    named traces. *version* rides along for the same reason — both are
    first-class trace fields, never tags.
    """
    status = resolve_langfuse_status()
    if not status.enabled:
        yield
        return
    try:
        from langfuse import propagate_attributes
    except Exception:  # pragma: no cover - defensive
        yield
        return
    kwargs: dict[str, Any] = {}
    if tags:
        kwargs["tags"] = tags
    if metadata:
        kwargs["metadata"] = metadata
    if session_id:
        kwargs["session_id"] = session_id
    if user_id:
        kwargs["user_id"] = user_id
    if trace_name:
        kwargs["trace_name"] = trace_name
    if version:
        kwargs["version"] = version
    if not kwargs:
        yield
        return
    try:
        with propagate_attributes(**kwargs):
            yield
    except Exception:  # pragma: no cover - defensive
        yield


@contextmanager
def langfuse_session_context(
    session_id: str,
    *,
    user_id: str | None = None,
    invocation_id: str | None = None,
    source_platform: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, str] | None = None,
) -> Iterator[None]:
    """Bind a Langfuse trace context to the current invocation.

    Each call gets a **unique trace** (via *invocation_id*) while
    Langfuse groups traces under the same *session_id*.

    *trace_name* names that trace. Omitting it does not leave the trace unnamed:
    it leaves it named after whichever LangChain runnable happened to open the
    root span, which is a property of the client library rather than of the work.

    *tags* / *metadata* carry pre-derived trace provenance
    (see ``session_provenance.TraceProvenance``) and are merged into the
    propagated baseline so every child observation — including nested LangChain
    CallbackHandler generations and ``langfuse_trace_span`` spans — is filterable
    by product, workspace, project, repo, branch, surface, etc. This seam stays
    taxonomy-free: it propagates whatever it is handed. When provenance is
    supplied its ``surface:<…>`` tag supersedes the coarse
    ``channel:<source_platform>`` fallback (kept only for callers that pass
    *source_platform* alone).
    """
    trace_context = _build_langfuse_trace_context(session_id, invocation_id)
    token_ctx = _LANGFUSE_TRACE_CONTEXT.set(trace_context)
    token_session = _LANGFUSE_SESSION_ID.set(session_id)
    resolved_user = user_id or ANONYMOUS_USER_ID
    token_user = _LANGFUSE_USER_ID.set(resolved_user)

    # Use propagate_attributes so session_id, user_id, and baseline
    # tags automatically attach to every child observation (including
    # LangChain CallbackHandler generations).
    base_tags = ["mewbo"]
    base_metadata: dict[str, str] = {"sessionid": session_id[:12]}
    if tags or metadata:
        base_tags.extend(tags or [])
        base_metadata.update(metadata or {})
    elif source_platform:
        base_tags.append(f"channel:{source_platform}")
    propagate_cm = langfuse_propagate(
        session_id=session_id,
        user_id=resolved_user,
        trace_name=trace_name,
        version=get_version(),
        tags=base_tags,
        metadata=base_metadata,
    )
    propagate_cm.__enter__()
    try:
        yield
    finally:
        try:
            propagate_cm.__exit__(None, None, None)
        except Exception:  # pragma: no cover - defensive
            pass
        _LANGFUSE_TRACE_CONTEXT.reset(token_ctx)
        _LANGFUSE_SESSION_ID.reset(token_session)
        _LANGFUSE_USER_ID.reset(token_user)


@contextmanager
def langfuse_trace_span(
    name: str,
    *,
    as_type: ObservationKind = "span",
    metadata: dict[str, str] | None = None,
    input_data: Any = None,
    level: str | None = None,
    attributes: dict[str, str] | None = None,
) -> Iterator[object | None]:
    """Open a Langfuse observation bound to the current session trace context.

    *as_type* selects how Langfuse renders the observation — an agent, a tool
    call and a plain span are the same OTel span with different types, and a
    trace built entirely of untyped spans loses the one axis the UI groups by.
    *metadata* is attached for filtering. *input_data* is set as the input.
    *level* sets the log level (e.g. ``"ERROR"``). *attributes* are raw OTel
    span attributes, stamped as early as the SDK allows (see
    :func:`_stamp_span_attributes`).
    """
    status = resolve_langfuse_status()
    if not status.enabled:
        yield None
        return
    link = LangfuseTraceLink.current()
    if link is None:
        yield None
        return
    try:
        from langfuse import get_client
    except Exception:  # pragma: no cover - defensive
        yield None
        return
    # Setup phase: if Langfuse fails, yield None (graceful degradation).
    # Body exceptions MUST propagate — never suppress them.
    span = None
    cm = None
    try:
        langfuse = get_client()
        # ``None`` here is the load-bearing case, not a degradation: it means an
        # enclosing span of this trace is ambient, so OTel parents this one for
        # real. Passing a context unconditionally is what parented every span to
        # a freshly-minted id that was never exported.
        cm = langfuse.start_as_current_observation(
            # ``as_type`` is overloaded per literal in the SDK's stubs, so a
            # variable of the union type has to be widened for the call to
            # resolve; the SDK validates the value itself.
            as_type=cast(Any, as_type),
            name=name,
            trace_context=link.trace_context_for_new_span(),
        )
        span = cm.__enter__()
        if span is not None:
            _stamp_span_attributes(span, attributes)
            update_kwargs: dict[str, Any] = {}
            if metadata:
                update_kwargs["metadata"] = metadata
            if input_data is not None:
                update_kwargs["input"] = input_data
            if level:
                update_kwargs["level"] = level
            if update_kwargs:
                span.update(**update_kwargs)
    except Exception:  # pragma: no cover - defensive
        logging.debug("Langfuse trace span setup failed.", exc_info=True)
    try:
        yield span
    except GeneratorExit:
        # The generator was closed rather than the traced body failing; there is
        # no operation outcome to record.
        raise
    except BaseException as exc:
        # A traced operation that raised must leave its cause ON the span.
        # Exiting the observation clean (which is all the ``finally`` below
        # does) is why a whole failure corpus carried ERROR-level spans with an
        # empty status_message and no exception event: the span recorded THAT
        # something failed and never WHAT. ``BaseException`` so a cancellation
        # or a deadline kill — the shapes a wedged call actually takes — is
        # recorded too, then re-raised untouched.
        record_span_exception(span, exc)
        raise
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass


def _stamp_span_attributes(span: object, attributes: dict[str, str] | None) -> None:
    """Set raw OTel attributes on a freshly-opened *span*, best-effort.

    ``start_as_current_observation`` takes no attribute mapping — it accepts only
    the Langfuse observation fields — so the earliest a caller can set one is
    immediately after entering the observation, before any child work opens a
    span of its own. That ordering is the point: an attribute a sampler or a span
    processor reads has to exist while the span is being created, and one set at
    exit influences neither.
    """
    if not attributes:
        return
    otel = getattr(span, "_otel_span", None)
    if otel is None:
        return
    try:
        otel.set_attributes({str(k): str(v) for k, v in attributes.items()})
    except Exception:  # pragma: no cover - never disrupt the run
        logging.debug("Langfuse span attribute stamp failed.", exc_info=True)


def _span_status_message(exc) -> str:
    """Render *exc* as a bounded, never-empty span status message.

    ``str(exc)`` is empty for whole exception families — a bare ``TimeoutError``
    is the one that cost the most forensic effort — and an empty status message
    reads in a trace exactly like a failure nobody classified. The type name is
    always present, so it is the floor.

    Delegates to :func:`mewbo_core.contracts.run_error.render_exception`, the SAME
    renderer ``llm_resilience.py:LlmResilienceExhausted.describe_error`` uses,
    so the two records of one failure never disagree.
    """
    return render_exception(exc, limit=_SPAN_STATUS_MESSAGE_MAX)


def record_span_exception(span, exc=None, *, message=None, attributes=None):
    """Record an exception on a Langfuse span: ERROR status + an OTel event.

    Two writes, because Langfuse reads them from different places: the span's
    own ``level``/``status_message`` drive the UI's error surface, while the
    exception tooling queries OTel ``exception`` events — ``span.update(
    level="ERROR")`` alone sets status with no cause attached. Setting the
    status here (rather than leaving it to each call site) is what makes the
    non-empty guarantee in :func:`_span_status_message` hold for every writer.
    Fully graceful: no span / no otel / disabled → no-op.
    """
    if span is None:
        return
    detail = exc if exc is not None else (RuntimeError(message) if message else None)
    if detail is None:
        return
    try:
        span.update(level="ERROR", status_message=_span_status_message(detail))
    except Exception:  # pragma: no cover - never disrupt the run
        logging.debug("Langfuse span status update failed.", exc_info=True)
    otel = getattr(span, "_otel_span", None)
    try:
        if otel is not None and otel.is_recording():
            otel.record_exception(detail, attributes=attributes or None)
    except Exception:  # pragma: no cover - never disrupt the run
        logging.debug("Langfuse record_exception failed.", exc_info=True)


def _langfuse_environment() -> str | None:
    """The deployment label, slugified into what Langfuse accepts.

    The operator writes free text (``runtime.envmode``), and Langfuse rejects an
    environment that is not lowercase ``[a-z0-9_-]`` — a rejection that costs the
    whole ingest, so a label like ``Not Specified`` is worth slugifying rather
    than passing through and losing the traces with it.
    """
    raw = str(get_config_value("runtime", "envmode", default="") or "").strip().lower()
    slug = _ENVIRONMENT_DISALLOWED.sub("-", raw).strip("-")
    if not slug:
        return None
    # The ``langfuse`` prefix is reserved for the platform's own environments.
    return f"env-{slug}" if slug.startswith("langfuse") else slug


def _ensure_langfuse_client(config) -> None:
    if config is None:
        return
    if not config.public_key or not config.secret_key:
        return

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", config.secret_key)
    if config.host:
        os.environ.setdefault("LANGFUSE_BASE_URL", config.host)
        os.environ.setdefault("LANGFUSE_HOST", config.host)

    environment = _langfuse_environment()
    release = get_version() or None
    # The OTel resource is built ONCE, by whichever client first installs a
    # TracerProvider, and resource attributes are read from the environment at
    # that moment — so the service name has to be in place before the constructor
    # below runs. Set later it is simply ignored, which is how every exported span
    # ended up attributed to ``unknown_service``. ``setdefault`` throughout, so an
    # operator who exports these keeps ownership of them.
    os.environ.setdefault("OTEL_SERVICE_NAME", _OTEL_SERVICE_NAME)
    if environment:
        os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", environment)
    if release:
        os.environ.setdefault("LANGFUSE_RELEASE", release)

    try:
        from langfuse import Langfuse
    except Exception as exc:  # pragma: no cover - defensive
        logging.debug("Langfuse client unavailable: {}", exc)
        return

    base_kwargs: dict[str, Any] = {
        "public_key": config.public_key,
        "secret_key": config.secret_key,
        "base_url": config.host or None,
    }
    try:
        Langfuse(**base_kwargs, environment=environment, release=release)
    except TypeError as exc:
        # An SDK that renamed or dropped either kwarg would otherwise cost the
        # client entirely — losing all tracing to gain two fields. The env vars
        # set above still carry both.
        logging.debug("Langfuse client rejected a trace-field kwarg: {}", exc)
        try:
            Langfuse(**base_kwargs)
        except Exception as retry_exc:  # pragma: no cover - defensive
            logging.debug("Langfuse client init failed: {}", retry_exc)
    except Exception as exc:  # pragma: no cover - defensive
        logging.debug("Langfuse client init failed: {}", exc)


def _attach_langfuse_metadata(
    handler: object,
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    version: str,
    release: str,
) -> None:
    """Stamp the trace fields the LangChain handler reads off its metadata.

    The handler recognises exactly three keys — ``langfuse_user_id``,
    ``langfuse_session_id``, ``langfuse_trace_name`` — and forwards them to
    ``propagate_attributes`` when it opens the ROOT of a chain. A name pushed
    into ``langfuse_tags`` instead is not a name: it is unfilterable free text
    beside a trace still called after whichever runnable opened the root span.

    *version* and *release* are deliberately unused here. Both are client-level
    fields now (:func:`_ensure_langfuse_client`), set once per process rather
    than restated as a tag on every observation; the parameters stay so call
    sites that pass them keep working.
    """
    del version, release
    metadata: dict[str, object] = {}
    if user_id:
        metadata["langfuse_user_id"] = user_id
    if session_id:
        metadata["langfuse_session_id"] = session_id
    if trace_name:
        metadata["langfuse_trace_name"] = trace_name
    if metadata:
        setattr(handler, "langfuse_metadata", metadata)


__all__ = [
    "ANONYMOUS_USER_ID",
    "ComponentStatus",
    "LangfuseTraceLink",
    "ObservationKind",
    "build_langfuse_handler",
    "langfuse_child_task_link",
    "format_component_status",
    "langfuse_invoke_config",
    "langfuse_propagate",
    "langfuse_session_context",
    "langfuse_trace_span",
    "record_span_exception",
    "resolve_home_assistant_status",
    "resolve_langfuse_status",
]
