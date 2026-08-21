#!/usr/bin/env python3
"""The gateway I/O seam — the only module here that opens a socket.

Everything above this line is pure: contracts that validate, an enum that sniffs
bytes. This module is the other side, and it exists as a Protocol plus one
implementation so a test injects a scripted transport and exercises the real
request-building and response-parsing code rather than mocking them away.

It also owns the SDK's shape. ``litellm.aspeech`` returns an
``HttpxBinaryResponseContent`` and ``litellm.atranscription`` a
``TranscriptionResponse``; both are unwrapped HERE into a plain mapping, so no
contract in this package imports litellm or duck-types an SDK object.

**Absence is a first-class state.** With the ``gateway`` extra uninstalled every
method raises :class:`SpeechUnavailableError` naming the extra — the feature is
absent, and a host asking :meth:`SpeechGateway.is_available` first never sees an
exception at all.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

#: Third-party modules the ``gateway`` extra must provide.
_GATEWAY_REQUIRES: tuple[str, ...] = ("litellm", "httpx")


class SpeechUnavailableError(RuntimeError):
    """The speech transport's dependencies are not installed."""


class SpeechGatewayError(RuntimeError):
    """The gateway refused or failed a speech call.

    Carries the underlying exception as ``__cause__``. The deployed proxy answers
    every parameter-validation failure with an opaque HTTP 500 whose body is the
    literal string ``Internal server error`` — no field, no hint — so this
    exception's message is frequently the most specific thing available. That is
    the reason the contracts reject a bad voice or format before the call: this
    error cannot be made more specific after the fact.
    """


@runtime_checkable
class SpeechTransport(Protocol):
    """The gateway calls a :class:`~mewbo_speech.gateway.SpeechGateway` makes."""

    def fetch_model_info(
        self, *, api_base: str, api_key: str, timeout: float
    ) -> list[Mapping[str, Any]]:
        """Return the ``data`` array of the gateway's ``/model/info`` document."""
        ...

    async def invoke(
        self,
        operation: str,
        /,
        *,
        api_base: str,
        api_key: str,
        timeout: float,
        max_retries: int,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Run one named litellm audio coroutine and normalise its response.

        The gateway coordinates are EXPLICIT parameters rather than part of
        ``kwargs`` so an implementation cannot quietly omit them — see the
        implementation's note on why that is not hypothetical. ``max_retries``
        joins them for the same reason: left inside ``kwargs`` a transport could
        silently drop it, and the SDK would then fall back to its own default
        (``openai.DEFAULT_MAX_RETRIES``, currently 2) — a per-operation retry
        POLICY quietly overridden by a library constant nobody chose.
        """
        ...


class LiteLlmSpeechTransport:
    """The real transport — litellm for calls, httpx for the model listing.

    The listing does not route through litellm on purpose: ``/model/info`` is a
    proxy-administration route with no SDK equivalent, and the SDK's model
    helpers read its bundled cost map rather than the live gateway.
    """

    def __init__(self) -> None:
        """Construct the transport; dependencies are probed lazily, per call."""

    @staticmethod
    def is_available() -> bool:
        """Whether the ``gateway`` extra's dependencies are importable.

        Cost class: ``O(1)`` after the first call — the modules are cached in
        ``sys.modules``.
        """
        try:
            LiteLlmSpeechTransport._require()
        except SpeechUnavailableError:
            return False
        return True

    @staticmethod
    def _require(*names: str) -> dict[str, Any]:
        """Import the named modules, or raise a message naming the extra.

        Probing is PER LEG, not per package: the model listing needs only httpx
        and a synthesis call needs only litellm, so importing both on either
        would make a working listing depend on litellm's ~seconds-long import and
        would fail a leg for a dependency it never touches. Same reasoning as the
        identity kernel's per-extra driver guard.

        Keyed by module name rather than returned positionally, so adding a
        dependency cannot silently rebind an existing call site's unpacking.
        """
        wanted = names or _GATEWAY_REQUIRES
        loaded: dict[str, Any] = {}
        missing: list[str] = []
        for name in wanted:
            try:
                loaded[name] = importlib.import_module(name)
            except ImportError:
                missing.append(name)
        if missing:
            raise SpeechUnavailableError(
                "the speech gateway requires the 'gateway' extra "
                f"(missing: {', '.join(missing)}). Install with "
                "`pip install mewbo-speech[gateway]`."
            )
        return loaded

    def fetch_model_info(
        self, *, api_base: str, api_key: str, timeout: float
    ) -> list[Mapping[str, Any]]:
        """Fetch ``<api_base>/model/info`` and return its ``data`` entries.

        Cost class: ``O(collection)`` in the models the gateway advertises — one
        HTTP round trip returning every route the key can see. Callers cache it;
        see :meth:`SpeechGateway.list_models`.
        """
        httpx = self._require("httpx")["httpx"]
        url = f"{api_base.rstrip('/')}/model/info"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = httpx.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json() or {}
        except Exception as exc:  # noqa: BLE001 — normalised at this boundary
            raise SpeechGatewayError(f"model listing failed: {exc}") from exc
        entries = payload.get("data") or []
        return [entry for entry in entries if isinstance(entry, Mapping)]

    async def invoke(
        self,
        operation: str,
        /,
        *,
        api_base: str,
        api_key: str,
        timeout: float,
        max_retries: int,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Run ``litellm.<operation>`` and normalise the SDK object it returns.

        *operation* comes from the request variant's ``SDK_OPERATION`` class
        attribute, so dispatch is data on the model rather than a branch here.

        **``api_base``/``api_key`` are not optional and are not in ``kwargs``.**
        Omit them and litellm falls back to its own provider resolution, which
        for the ``openai/`` route means the ``OPENAI_API_KEY`` environment
        variable — so the call does not fail as "unauthorised against the
        gateway", it fails as ``OpenAIException - Missing credentials``, naming
        an OpenAI variable nobody set and never mentioning our proxy at all.
        That misdirection is why they are explicit keyword parameters here and
        on the protocol: a transport that forgets them cannot typecheck.

        **``max_retries`` is likewise explicit rather than left to litellm's own
        default.** ``litellm.main.speech``/``transcription`` fall back to
        ``litellm.num_retries or openai.DEFAULT_MAX_RETRIES`` (2) when
        unset — measured directly against this gateway: a call guaranteed to
        500 (a rejected ``response_format``) took 0.30s at ``max_retries=0``,
        1.00s at 1, and 2.02s at 2, roughly linear per attempt. litellm has NO
        visible retry event for this path (unlike the chat completion ladder in
        ``mewbo_core.llm``, which sets ``max_retries=0`` and owns retries itself
        for exactly that reason) — an SDK-level retry here is silent, so the
        caller must be able to choose it deliberately rather than inherit
        whatever the SDK ships as a default.

        Cost class: ``O(input length)`` for synthesis — measured at ~0.5s for a
        32-character sentence and ~4s for a 410-character paragraph on
        ``supertonic-3``, roughly double that on ``supertonic-3-hd``. Nothing
        about this call is interactive-latency; a caller needs a pending state.
        """
        # THE CALL SITE THE PREFIX RULE IS ABOUT. `kwargs["model"]` arrives here
        # PREFIXED (`openai/supertonic-3`) because the SDK's provider dispatch
        # refuses a bare id locally, and the SDK strips the prefix before writing
        # the body — the wire carries `{"model":"supertonic-3"}`. Both spellings
        # look right and each fails in a DIFFERENT layer: a bare id dies here
        # with `LLM Provider NOT provided` before any socket opens, while a
        # prefixed id sent by a hand-rolled HTTP call is a 403 from the proxy's
        # key ACL. Never hand-roll raw HTTP with the prefixed form.
        litellm = self._require("litellm")["litellm"]
        call = getattr(litellm, operation, None)
        if call is None:
            raise SpeechUnavailableError(
                f"litellm exposes no {operation!r} coroutine — the installed "
                "version predates the audio SDK surface (needs >=1.88)."
            )
        try:
            raw = await call(
                api_base=api_base,
                api_key=api_key,
                timeout=timeout,
                max_retries=max_retries,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — normalised at this boundary
            raise SpeechGatewayError(f"{operation} failed: {exc}") from exc
        return self.normalize(raw)

    @staticmethod
    def normalize(raw: Any) -> dict[str, Any]:
        """Flatten an SDK response object into a plain mapping.

        Binary responses (``HttpxBinaryResponseContent``) carry ``.content`` and
        wrap the httpx response whose declared ``content-type`` is preserved here
        SO THAT the caller can compare it against the sniffed container — the
        gateway's header is wrong on every successful synthesis, and dropping it
        would hide that rather than fix it. Transcriptions carry ``.text``.

        Cost class: ``O(1)``.
        """
        content = getattr(raw, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return {
                "audio": bytes(content),
                "content_type": LiteLlmSpeechTransport._declared_content_type(raw),
            }
        text = getattr(raw, "text", None)
        if isinstance(text, str):
            # `language` and `duration` are two bounded scalars the gateway
            # already sends. `words[]` and `segments[]` ride the same body and
            # are deliberately NOT carried: they grow with recording length, so
            # forwarding them would put an unbounded array in every response.
            duration = getattr(raw, "duration", None)
            language = getattr(raw, "language", None)
            return {
                "text": text,
                "language": language if isinstance(language, str) else None,
                "duration": float(duration) if isinstance(duration, (int, float)) else None,
            }
        if isinstance(raw, Mapping):
            return dict(raw)
        raise SpeechGatewayError(f"unrecognised gateway response of type {type(raw).__name__}")

    @staticmethod
    def _declared_content_type(raw: Any) -> str | None:
        """Read the declared content type off an SDK binary response, if present.

        Best-effort by design: the header is advisory (and, on this gateway,
        wrong on every successful synthesis), so a response object that does not
        expose one is not an error.

        Cost class: ``O(1)``.
        """
        headers = getattr(getattr(raw, "response", None), "headers", None)
        if headers is None:
            return None
        try:
            value = headers.get("content-type")
        except Exception:  # noqa: BLE001 — an exotic header mapping is not a failure
            return None
        return value if isinstance(value, str) else None
