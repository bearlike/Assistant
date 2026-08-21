#!/usr/bin/env python3
"""Model configuration helpers for ChatLiteLLM."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any, ClassVar, Protocol, cast

from langchain_core.messages import BaseMessage

from mewbo_core.config import get_config_value

# The LLM-call timeouts live together in ``llm_resilience`` so nobody tunes one
# believing it bounds another; this module owns only the HTTP leg. ``CallDeadline``
# is imported for the opposite direction: this module is where the streamed
# chunks physically pass, so it is where the in-flight call reports progress.
from mewbo_core.llm.llm_resilience import DEFAULT_REQUEST_TIMEOUT, CallDeadline

_logger = logging.getLogger(__name__)

# Tracks api_base URLs whose /v1/model/info we've already pulled, so we register
# proxy capabilities exactly once per process per distinct base. Re-registration
# is harmless (litellm.register_model is idempotent) but the HTTP fetch is not.
_REGISTERED_PROXY_BASES: set[str] = set()

# Eagerly bind the LiteLLM capability lookup at module import — re-importing
# litellm submodules lazily inside hot helpers can blow up in tests that have
# polluted ``sys.modules`` (HA tests stub a couple of aiohttp helpers, which
# breaks litellm's lazy http_handler load on the second import).  None means
# "litellm not installed"; we treat that as "no caching" downstream.
try:
    from litellm.utils import supports_prompt_caching as _litellm_supports_prompt_caching
except Exception:  # pragma: no cover - dependency guard
    _litellm_supports_prompt_caching = None  # type: ignore[assignment]


class ChatModel(Protocol):
    """Protocol for LangChain-compatible chat models."""

    def invoke(
        self, input_data: object, config: object | None = None, **kwargs: object
    ) -> BaseMessage:
        """Invoke the model synchronously."""

    async def ainvoke(
        self, input_data: object, config: object | None = None, **kwargs: object
    ) -> BaseMessage:
        """Invoke the model asynchronously."""


def _normalize_model_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    return []


def _strip_provider(model_name: str | None) -> str:
    if not model_name:
        return ""
    return model_name.split("/", 1)[-1].strip().lower()


def _matches_model_list(model_name: str, entries: Iterable[str]) -> bool:
    for entry in entries:
        if entry.endswith("*") and model_name.startswith(entry[:-1]):
            return True
        if model_name == entry:
            return True
    return False


def model_prefers_structured_patch(model_name: str | None) -> bool:
    """Return True if the model works better with the per-file structured_patch tool.

    GPT-5-class, o3/o4, and Codex models use structured JSON tool calls that
    map naturally to ``file_edit_tool`` (structured_patch).  Claude and Gemini
    are trained on diff/patch text formats and work better with
    ``aider_edit_block_tool`` (search_replace_block).

    Precedence (the model→tool-variant map is now controllable data):
    1. ``llm.structured_patch_models`` config allowlist (runtime override layer).
    2. The operator-tunable ``prompts/model_variants.yaml`` map, loaded through
       ``ModelVariantRegistry`` — this is where the built-in defaults now live
       (gpt-5/o3/o4/codex/gpt-4), so they are editable without touching code.
       Its conservative ``defaults.edit_tool`` (``search_replace_block``) is the
       sane built-in fallback when no profile matches.
    """
    if not model_name:
        return False
    normalized = _strip_provider(model_name)
    raw = model_name.lower()
    allowlist = _normalize_model_list(
        get_config_value("llm", "structured_patch_models", default=[])
    )
    if _matches_model_list(raw, allowlist) or _matches_model_list(normalized, allowlist):
        return True
    # Controllable data file (migrated built-in defaults; operator-editable).
    from mewbo_core.llm.model_variants import get_model_variant_registry

    return get_model_variant_registry().edit_tool_for(model_name) == "structured_patch"


def register_proxy_model_capabilities(
    api_base: str | None,
    api_key: str | None,
    *,
    timeout: float = 5.0,
) -> int:
    """Pull proxy ``/v1/model/info`` and register advertised models.

    Hydrates LiteLLM's local ``model_cost`` map with the routes the proxy
    operator defined.

    This is the bridge that lets ``litellm.utils.supports_prompt_caching`` (and
    every other ``supports_*`` helper) report accurately for proxy-fronted
    custom model names — the SDK only consults its bundled ``model_cost.json``
    by default, which doesn't know about routes the proxy operator defined.

    Idempotent per process: each distinct ``api_base`` is fetched at most once.
    Failures are logged and swallowed — Stage 1's per-model gate just stays
    conservative, never crashes.

    Returns the number of models newly registered (0 if cached, no-op, or
    error).
    """
    if not api_base:
        return 0
    base = api_base.rstrip("/")
    if base in _REGISTERED_PROXY_BASES:
        return 0
    _REGISTERED_PROXY_BASES.add(base)
    try:
        import httpx
        import litellm

        url = base + "/model/info"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        entries = (resp.json() or {}).get("data") or []
    except Exception as exc:
        _logger.info(
            "Skipping proxy model_info registration for %s: %s",
            base,
            exc,
        )
        return 0

    registered = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name")
        info = entry.get("model_info") or {}
        if not isinstance(name, str) or not isinstance(info, dict) or not info:
            continue
        # FORCED, never defaulted. Every model here is reached through the proxy
        # as an OpenAI-compatible endpoint, which is why the key is ``openai/``;
        # the upstream's own provider label describes how the PROXY reaches the
        # model and is not ours to carry. Letting it through was a silent break:
        # the proxy reports ``anthropic`` for a Claude-backed route, so the entry
        # was stored under an ``openai/`` key while declaring a different
        # provider, and the lookup — which infers the provider from that very
        # prefix — rejected its own record as a mismatch.
        info["litellm_provider"] = "openai"
        info.setdefault("mode", "chat")
        try:
            litellm.register_model({f"openai/{name}": info})
            registered += 1
        except Exception as exc:  # pragma: no cover - litellm guard
            _logger.debug("register_model failed for %s: %s", name, exc)
    if registered:
        # The budget layer memoizes catalogue answers, INCLUDING misses, and the
        # API answers usage reads without ever constructing a client — so a poll
        # arriving before this hydration would pin the pre-hydration window for
        # the life of the worker. Imported at the call site: ``session`` sits
        # above ``llm`` in the package graph, and this is the only edge.
        from mewbo_core.session.token_budget import forget_cached_context_windows

        forget_cached_context_windows()
    return registered


def model_supports_prompt_caching(model_name: str | None) -> bool:
    """Return True when LiteLLM reports the model supports prompt caching.

    Single source of truth: ``litellm.utils.supports_prompt_caching``, which
    reads the bundled ``model_cost.json`` (extensible at runtime via
    ``litellm.register_model``).  Returns False on unknown models or any
    lookup error so the caller can skip caching gracefully without crashing
    the agent loop.
    """
    if not model_name or _litellm_supports_prompt_caching is None:
        return False
    try:
        return bool(_litellm_supports_prompt_caching(_strip_provider(model_name)))
    except Exception:
        return False


def model_supports_reasoning_effort(model_name: str | None) -> bool:
    """Return True if the model is known to support reasoning_effort.

    LiteLLM translates reasoning_effort per-provider:
    - Claude → output_config.effort
    - Gemini → thinking budget_tokens or thinking_level
    - OpenAI (o3/gpt-5) → native reasoning_effort
    """
    if not model_name:
        return False
    raw = model_name.lower()
    normalized = _strip_provider(model_name)
    allowlist = _normalize_model_list(
        get_config_value("llm", "reasoning_effort_models", default=[])
    )
    if _matches_model_list(raw, allowlist) or _matches_model_list(normalized, allowlist):
        return True
    return (
        normalized.startswith("gpt-5")
        or normalized.startswith("o3")
        or "claude" in normalized
        or "gemini" in normalized
    )


def resolve_reasoning_effort(model_name: str | None) -> str | None:
    """Resolve the reasoning effort for a model.

    Returns the configured value if set, otherwise ``None`` (let the
    provider decide).  Only returns a value when the model supports
    the parameter *and* a value is explicitly configured.
    LiteLLM translates this per-provider:
    - Claude: output_config.effort
    - Gemini: thinking budget_tokens or thinking_level
    - OpenAI: native reasoning_effort
    """
    if not model_supports_reasoning_effort(model_name):
        return None
    configured = get_config_value("llm", "reasoning_effort", default="")
    if isinstance(configured, str) and configured.strip():
        return configured.strip().lower()
    env = os.environ.get("MEWBO_REASONING_EFFORT", "").strip().lower()
    if env:
        return env
    return None


def _resolve_litellm_model(
    model_name: str,
    openai_api_base: str | None,
    proxy_prefix: str = "openai",
) -> str:
    if not openai_api_base:
        return model_name
    prefix = proxy_prefix.strip().strip("/") or "openai"
    if model_name.startswith(f"{prefix}/"):
        return model_name
    return f"{prefix}/{model_name}"


class _ToolNameNormalizer:
    """Map a returned tool name back onto the name that was actually bound.

    Some providers do not round-trip the declared function name verbatim: the
    observed shapes are a namespace prefix leaking into the name
    (``default_api_search`` / ``default_api:search``) and case drift. Downstream
    every one of those is simply an unknown tool, so the call is wasted and the
    model is told it asked for something that does not exist.

    Per the cross-model normalization law this is fixed at the adapter seam and
    never by pattern-matching a model's text in the orchestration loop. It sits
    on the litellm client because that is the one place BOTH halves of the round
    trip are in scope: the request's ``tools`` list is the authoritative set of
    bound names, and the response is what has to be reconciled against it.

    Matching is against those bound names only — never a blind prefix strip. A
    name folds to lowercase alphanumerics, which collapses prefix, separator and
    case drift into one lookup; a fold shared by two bound tools is dropped from
    the index, so an ambiguous match resolves to nothing rather than to the
    wrong tool. An unresolved name passes through UNCHANGED (reported once per
    process, since a systematic mangling would otherwise log per call): renaming
    to a tool that was not bound would manufacture a call the caller never made.
    """

    # The namespace some providers prepend, in folded form. Applied only after
    # an exact and a folded lookup have both missed, so a tool genuinely named
    # ``default_api_*`` still resolves to itself.
    _LEAKED_NAMESPACE_FOLD = "defaultapi"
    _warned: ClassVar[set[str]] = set()

    def __init__(self, bound_names: Iterable[str]) -> None:
        self._exact: set[str] = {n for n in bound_names if n}
        by_fold: dict[str, set[str]] = {}
        for name in self._exact:
            by_fold.setdefault(self._fold(name), set()).add(name)
        self._by_fold = {k: next(iter(v)) for k, v in by_fold.items() if len(v) == 1}

    @classmethod
    def for_request(cls, tools: Any) -> _ToolNameNormalizer | None:
        """Build a normalizer from a request's ``tools``, or ``None`` if unbound."""
        if not isinstance(tools, (list, tuple)) or not tools:
            return None
        names: list[str] = []
        for entry in tools:
            fn = (
                entry.get("function")
                if isinstance(entry, dict)
                else getattr(entry, "function", None)
            )
            name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            if isinstance(name, str) and name:
                names.append(name)
        return cls(names) if names else None

    @staticmethod
    def _fold(name: str) -> str:
        return "".join(ch for ch in name.lower() if ch.isalnum())

    def resolve(self, name: str) -> str:
        """Return the bound name *name* refers to, or *name* itself when unknown."""
        if not name or name in self._exact:
            return name
        fold = self._fold(name)
        bound = self._by_fold.get(fold)
        if bound is None and fold.startswith(self._LEAKED_NAMESPACE_FOLD):
            bound = self._by_fold.get(fold[len(self._LEAKED_NAMESPACE_FOLD) :])
        if bound is None:
            if name not in self._warned:
                self._warned.add(name)
                _logger.warning(
                    "Tool name '%s' returned by the model matches no bound tool; "
                    "passing it through unchanged.",
                    name,
                )
            return name
        if bound != name:
            _logger.debug("Normalized returned tool name '%s' to bound '%s'.", name, bound)
        return bound

    @staticmethod
    def _read(obj: Any, key: str) -> Any:
        """Read *key* off a mapping or an object — litellm returns either shape."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def apply(self, obj: Any) -> Any:
        """Rewrite every tool-call name on a response or streaming chunk, in place."""
        try:
            for choice in self._read(obj, "choices") or ():
                # A streaming chunk carries ``delta``; a complete response carries
                # ``message``. Both hold the same tool-call shape.
                for holder in (self._read(choice, "message"), self._read(choice, "delta")):
                    if holder is None:
                        continue
                    for call in self._read(holder, "tool_calls") or ():
                        self._rewrite(self._read(call, "function"))
                    self._rewrite(self._read(holder, "function_call"))
        except Exception as exc:  # pragma: no cover - never break the model call over a name
            _logger.debug("Tool name normalization failed: %s", exc, exc_info=True)
        return obj

    def _rewrite(self, fn: Any) -> None:
        name = self._read(fn, "name")
        # A streaming delta can carry a partial name; it resolves to nothing and
        # is left alone rather than guessed at.
        if not isinstance(name, str) or not name:
            return
        resolved = self.resolve(name)
        if resolved == name:
            return
        if isinstance(fn, dict):
            fn["name"] = resolved
        else:
            fn.name = resolved


class _UsageNormalizingLiteLLM:
    """Normalize a litellm response before the LangChain adapter reads it.

    Two normalizations ride this one seam — token usage (below) and tool NAMES
    (:class:`_ToolNameNormalizer`) — because both are defects in what the
    provider/proxy returns rather than in how the engine drives it, and this
    wrapper is the last point before ``langchain-litellm`` converts the raw
    response into an ``AIMessage``. Rewriting the name here fixes BOTH slots the
    adapter populates from it (``AIMessage.tool_calls`` and
    ``additional_kwargs.tool_calls``) with one write.

    Surface token usage that litellm strands in ``_hidden_params``.

    litellm can leave the real token counts in ``response._hidden_params``
    instead of ``response.usage``: streaming chunks have usage *stripped* from
    the emitted chunk and re-attached only to ``_hidden_params``
    (``streaming_handler.py``, the ``stream_options is None`` path), and some
    proxy/provider non-streaming shapes never populate the field either.
    ``langchain-litellm`` then builds ``usage_metadata`` from ``model_dump()``
    /``response.get("usage")`` — both of which read empty because
    ``_hidden_params`` is a private attribute — so every ``llm_call_end`` token
    count collapses to zero (upstream litellm#12233 /
    litellm#17476, no fix release as of litellm 1.88.0).

    This shim copies the stranded usage back onto the field the adapter reads,
    for both the non-streaming response and every streaming chunk. It is
    feature-detecting, not version-sniffing: it acts ONLY when ``usage`` is
    absent/all-zero, so it is a no-op the moment upstream (or the proxy) puts
    usage on the field — remove it once that ships. ``client`` is injected (the
    real ``litellm`` module), and every other attribute delegates through.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @staticmethod
    def _usage_is_empty(usage: Any) -> bool:
        """True when ``usage`` carries no token counts (missing or all-zero)."""
        if usage is None:
            return True

        def _get(key: str) -> int:
            raw = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return 0

        return (_get("prompt_tokens") + _get("completion_tokens") + _get("total_tokens")) == 0

    @classmethod
    def _surface_hidden_usage(cls, obj: Any) -> Any:
        """Copy ``obj._hidden_params['usage']`` onto ``obj.usage`` when empty."""
        try:
            hidden = getattr(obj, "_hidden_params", None)
            hidden_usage = hidden.get("usage") if isinstance(hidden, dict) else None
            if hidden_usage is not None and cls._usage_is_empty(getattr(obj, "usage", None)):
                obj.usage = hidden_usage
        except Exception:  # pragma: no cover - never break the model call over usage
            pass
        return obj

    def completion(self, **kwargs: Any) -> Any:
        result = self._inner.completion(**kwargs)
        names = _ToolNameNormalizer.for_request(kwargs.get("tools"))
        if kwargs.get("stream"):
            return self._normalize_sync_stream(result, names)
        return self._normalize(result, names)

    async def acompletion(self, **kwargs: Any) -> Any:
        result = await self._inner.acompletion(**kwargs)
        names = _ToolNameNormalizer.for_request(kwargs.get("tools"))
        if kwargs.get("stream"):
            return self._normalize_async_stream(result, names)
        return self._normalize(result, names)

    @classmethod
    def _normalize(cls, obj: Any, names: _ToolNameNormalizer | None) -> Any:
        if names is not None:
            names.apply(obj)
        return cls._surface_hidden_usage(obj)

    def _normalize_sync_stream(
        self, stream: Any, names: _ToolNameNormalizer | None
    ) -> Iterator[Any]:
        for chunk in stream:
            yield self._normalize(chunk, names)

    async def _normalize_async_stream(
        self, stream: Any, names: _ToolNameNormalizer | None
    ) -> AsyncIterator[Any]:
        # This generator is the one place every streamed chunk physically passes
        # on its way from the provider client to the loop, which makes it the
        # only honest place to report that the call is still producing. The
        # deadline it reports to (``llm_resilience.CallDeadline``) is what tells
        # a slow-but-producing stream apart from one that returned 200 and went
        # silent; without a signal from here the two are indistinguishable and
        # only a single total ceiling can bound either. A call with no deadline
        # armed (a direct client use, a test) is a no-op.
        async for chunk in stream:
            CallDeadline.note_progress()
            yield self._normalize(chunk, names)


# Keyed by the ``ChatLiteLLM`` base a call resolved, so a test that swaps the
# library gets its own subclass instead of one built over a stale base.
_OBSERVABILITY_SUBCLASS_BY_BASE: dict[type[Any], type[Any]] = {}


def _observability_chat_litellm_class() -> type[Any]:
    """Return a ``ChatLiteLLM`` subclass that reports a bare model id to tracing.

    ``ChatLiteLLM._get_ls_params`` sets ``ls_model_name`` to ``self.model`` —
    the exact string LiteLLM dispatches on, gateway prefix included (e.g.
    ``openai/claude-opus-5``, or ``openai/z-ai/glm-5.2`` behind a nested
    routing alias). Langfuse's LangChain integration reads that same
    ``ls_model_name`` off the callback metadata to price a generation,
    matching it against a Model Definition's ``match_pattern`` regex — a
    gateway-prefixed name matches no default pattern, so Langfuse computes
    cost 0 rather than raising. The miss is silent by construction.

    Overriding ONLY ``_get_ls_params`` keeps ``self.model``/``self.model_name``
    untouched, so ``_default_params``/``_identifying_params`` — what actually
    gets sent to ``litellm.acompletion`` — still carry the full routing
    string. This is a tracing-facing rename, not a routing change.

    Cached BY BASE CLASS, not as a module-level singleton. Building the
    subclass is Pydantic metaclass work — measured at 9.4 ms, which a caller
    would otherwise pay on every model build — but a plain singleton would pin
    whichever ``ChatLiteLLM`` was imported first, and anything swapping
    ``sys.modules["langchain_litellm"]`` (every test that fakes the library)
    would then silently get a subclass of the wrong base. Keying on the class
    object itself keeps both properties: one build per distinct base, and a
    swapped base gets its own. The map is bounded by how many distinct
    ``ChatLiteLLM`` classes a process ever imports — one, outside tests.
    """
    from langchain_litellm import ChatLiteLLM

    cached = _OBSERVABILITY_SUBCLASS_BY_BASE.get(ChatLiteLLM)
    if cached is not None:
        return cached

    class _ObservabilityChatLiteLLM(ChatLiteLLM):
        def _get_ls_params(  # type: ignore[override]
            self, stop: list[str] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            params = super()._get_ls_params(stop=stop, **kwargs)
            reported = params.get("ls_model_name")
            if isinstance(reported, str):
                params["ls_model_name"] = _strip_provider(reported) or reported
            return params

    _OBSERVABILITY_SUBCLASS_BY_BASE[ChatLiteLLM] = _ObservabilityChatLiteLLM
    return _ObservabilityChatLiteLLM


def build_chat_model(
    model_name: str,
    *,
    openai_api_base: str | None = None,
    api_key: str | None = None,
) -> ChatModel:
    """Build a ChatLiteLLM model with reasoning-effort compatibility.

    ``openai_api_base`` and ``api_key`` default to ``llm.api_base`` and
    ``llm.api_key`` from config when ``None``. Pass them explicitly only
    to override the configured values (e.g. tests, multi-tenant routing).
    """
    try:
        ChatLiteLLM = _observability_chat_litellm_class()
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError("langchain-litellm is required to build ChatLiteLLM") from exc

    if openai_api_base is None:
        openai_api_base = str(get_config_value("llm", "api_base", default="") or "")
    if api_key is None:
        api_key = str(get_config_value("llm", "api_key", default="") or "")
    proxy_prefix = (
        str(get_config_value("llm", "proxy_model_prefix", default="openai") or "openai")
        .strip()
        .strip("/")
        or "openai"
    )

    # Hydrate proxy capabilities once per process per distinct api_base, so the
    # cache-support gate below sees the proxy's advertised model_info instead
    # of just LiteLLM's bundled allowlist. No-op when api_base is unset.
    if openai_api_base:
        register_proxy_model_capabilities(openai_api_base, api_key)

    reasoning_effort = resolve_reasoning_effort(model_name)

    model_kwargs: dict[str, Any] = {
        "drop_params": True,  # Must be in model_kwargs to reach litellm.acompletion();
        # ChatLiteLLM has no drop_params field so top-level kwarg is silently ignored.
        #
        # The SAME routing trap, and it costs far more than a dropped param.
        # ``ChatLiteLLM.max_retries`` (set to 0 below) is consumed by the
        # LangChain-level tenacity decorator and is NEVER forwarded into
        # ``litellm.acompletion`` — it appears in neither ``_default_params`` nor
        # ``_client_params`` — so litellm builds the provider SDK client with its
        # own default of 2, i.e. three attempts. Each attempt is bounded by
        # ``request_timeout`` and re-sends the whole prompt, so the HTTP timeout
        # an operator reads as a 60s idle bound is really ~3x that before
        # anything surfaces, which is how a silent provider consumed the entire
        # per-call ceiling with no retry event and no error text. Retries belong
        # to the ToolUseLoop, which bounds and REPORTS each one.
        "max_retries": 0,
    }
    if reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = reasoning_effort

    if model_supports_prompt_caching(model_name):
        # LiteLLM's hook attaches the right native marker for whichever provider
        # the call routes to (Anthropic content-block cache_control, Bedrock
        # with ttl-sanitisation).  We only declare *where* — at the system
        # message — and let LiteLLM own the per-provider syntax.  Auto-cache
        # providers (OpenAI) silently drop the kwarg and still surface savings
        # via usage_metadata.input_token_details.cache_read.
        model_kwargs["cache_control_injection_points"] = [
            {"location": "message", "role": "system", "control": {"type": "ephemeral"}}
        ]

    kwargs: dict[str, Any] = {
        "model": _resolve_litellm_model(model_name, openai_api_base, proxy_prefix),
        # Streaming is what makes every other bound mean what its docstring says.
        # ``ChatLiteLLM``'s constructor puts ``streaming`` into ``model_fields_set``
        # with ``False``, and langchain-core's ``_streaming_disabled`` treats that
        # as an opt-out that OVERRIDES an affirmative ``stream=True`` — so
        # ``astream()`` silently degraded to one buffered ``ainvoke``. Three things
        # were dead as a result: the transport bound became a ceiling on total
        # generation time (a buffered response sends nothing until it is complete,
        # so the wait for the first byte IS the whole generation), the first-token
        # and stream-idle arms of ``CallDeadline`` never armed because nothing
        # reported chunk progress, and ``agent_message_delta`` never fired, so the
        # CLI and console token streams both rendered nothing.
        "streaming": True,
        # Disable LiteLLM's built-in retries — the ToolUseLoop manages
        # retries itself with per-attempt timeouts and visible retry events.
        "request_timeout": float(
            get_config_value("llm", "request_timeout", default=DEFAULT_REQUEST_TIMEOUT)
            or DEFAULT_REQUEST_TIMEOUT
        ),
        "max_retries": 0,
    }
    if openai_api_base:
        kwargs["api_base"] = openai_api_base
    if api_key:
        kwargs["api_key"] = api_key
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    chat = ChatLiteLLM(**kwargs)
    # Rescue token usage that litellm strands in ``_hidden_params`` so it
    # reaches ``usage_metadata`` (see ``_UsageNormalizingLiteLLM``). ChatLiteLLM
    # sets ``client`` to the ``litellm`` module at construction; wrap that. Skip
    # when absent (test doubles that stub ChatLiteLLM never set a client).
    inner_client = getattr(chat, "client", None)
    if inner_client is not None:
        chat.client = _UsageNormalizingLiteLLM(inner_client)
    return cast(ChatModel, chat)


def response_text(response: Any) -> str:
    """Visible assistant text from an LLM response, whatever shape it arrives in.

    A cross-model normalization, which is why it lives at this seam rather than
    at each caller (see this package's CLAUDE.md: format differences are fixed
    here, never detected upstream). Three shapes reach us:

    * a plain ``str`` — most models;
    * a list of ``{"type": "text", "text": ...}`` blocks — Anthropic-style;
    * a list mixing ``thinking``/``reasoning`` blocks with the answer as a BARE
      STRING element — what a reasoning model returns through the proxy.

    Callers used to take the FIRST ``type == "text"`` dict, which yields ``""``
    for that third shape. The failure is silent — no exception, no log, just an
    empty answer — so a session title and a compaction summary each simply
    stopped being produced the moment a reasoning model became the default.
    Reasoning blocks are deliberately dropped: they are the model's scratchpad,
    not its answer.
    """
    raw = response.content if hasattr(response, "content") else response
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return str(raw)
    parts: list[str] = []
    for block in raw:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


#: Upper bounds a grammar-constrained backend expands into literal repetition.
#: Dropped from every tool schema — see :func:`sanitize_tool_schema`.
_UNBOUNDED_KEYWORDS = ("maxLength", "maxItems", "maxProperties")


def sanitize_tool_schema(schema: Any) -> Any:
    """Recursively fix JSON Schema issues that strict LLM providers reject.

    Runs at the ``specs_to_langchain_tools`` funnel so every tool schema —
    MCP, built-in, plugin — is covered.  Fixes are valid JSON Schema, safe
    for all providers.

    Current fixes:
    - ``array`` without ``items`` → add ``"items": {}`` (required by OpenAI).
    - drop ``maxLength`` / ``maxItems`` / ``maxProperties``, which make a
      grammar-constrained backend fail to start.

    WHY THE UPPER BOUNDS GO. A backend that constrains decoding with a grammar
    (llama.cpp/Ollama, and anything else compiling JSON Schema to GBNF) expands
    an upper bound into that many literal repetitions, and inlines every
    ``$ref`` while doing it. In a RECURSIVE schema the two multiply. Measured:
    ``present_ui`` — whose ``Card.children`` is a ``oneOf`` over
    eleven component types including ``Card`` itself — made Ollama answer
    ``400 Failed to initialize samplers: failed to parse grammar`` for the whole
    17-tool request. Dropping these three keywords fixed it; dropping
    ``minLength``, ``pattern``, ``const``, ``default``, ``discriminator``,
    ``anyOf`` or ``additionalProperties`` did not.

    Magnitude is what bites, not presence: the same schema compiled with
    ``maxLength`` forced to 8, and failed again with ``maxItems`` raised to
    2000. **A size threshold would still be unsound**, because the blow-up
    scales with nesting depth as well as with the bound, so a limit that is
    safe at one depth is fatal one level down. Dropping unconditionally is the
    only rule that does not need to know the shape of the schema.

    Dropping is strictly PERMISSIVE and cannot truncate or reject a valid
    argument — it only stops advertising a ceiling. Lower bounds stay: they are
    small in practice and carry real intent. Callers that need the ceiling
    enforced still get it, because tool arguments are validated against the
    original schema on our side (``EmitStructuredResponseTool.handle``).
    """
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNBOUNDED_KEYWORDS:
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            result[key] = {k: sanitize_tool_schema(v) for k, v in value.items()}
        elif key in ("additionalProperties", "items") and isinstance(value, dict):
            result[key] = sanitize_tool_schema(value)
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems", "items") and isinstance(value, list):
            result[key] = [sanitize_tool_schema(v) for v in value]
        else:
            result[key] = value

    # Array without items → add permissive default.
    schema_type = result.get("type")
    is_array = schema_type == "array" or (isinstance(schema_type, list) and "array" in schema_type)
    if is_array and "items" not in result:
        result["items"] = {}

    return result


def specs_to_langchain_tools(specs: list[object]) -> list[dict[str, Any]]:
    """Convert ToolSpecs to LangChain bind_tools() format.

    Each spec must have ``tool_id``, ``description``, and ``metadata["schema"]``.
    Specs without a schema are silently skipped.

    Delegates to LangChain's :func:`convert_to_openai_tool` (Anthropic-format
    input) so that schema normalisation is handled by the library rather than
    hand-rolled here.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    tools: list[dict[str, Any]] = []
    for spec in specs:
        if not getattr(spec, "enabled", True):
            continue
        metadata = getattr(spec, "metadata", None) or {}
        schema = metadata.get("schema")
        if not isinstance(schema, dict):
            continue
        # Anthropic-format dict: LangChain maps input_schema → parameters.
        tools.append(
            convert_to_openai_tool(
                {
                    "name": getattr(spec, "tool_id", ""),
                    "description": getattr(spec, "description", ""),
                    "input_schema": sanitize_tool_schema(schema),
                }
            )
        )
    return tools


__all__ = [
    "build_chat_model",
    "ChatModel",
    "model_prefers_structured_patch",
    "model_supports_prompt_caching",
    "register_proxy_model_capabilities",
    "model_supports_reasoning_effort",
    "resolve_reasoning_effort",
    "response_text",
    "sanitize_tool_schema",
    "specs_to_langchain_tools",
]
