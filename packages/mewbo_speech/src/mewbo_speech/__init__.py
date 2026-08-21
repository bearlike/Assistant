#!/usr/bin/env python3
"""Mewbo Speech — the optional text-to-speech / speech-to-text substrate.

Speech-capable models are routes on the same LiteLLM gateway the chat models come
from, but nothing in Mewbo could previously tell them apart: core fetches the
proxy's ``/model/info`` document to hydrate LiteLLM's cost map and discards the
``mode`` field that distinguishes an ``audio_speech`` route from an
``audio_transcription`` one from a chat one. This package keeps that field, and
adds the contracts that turn the gateway's opaque failures into validation errors
raised before a call is ever made.

Layout — pure contracts first, I/O last:

* :mod:`mewbo_speech.audio` — :class:`AudioContainer`, which identifies a payload
  from its magic bytes because the gateway's declared ``Content-Type`` is wrong
  on every successful synthesis.
* :mod:`mewbo_speech.models` — :class:`SpeechModel` and the closed voice/format
  vocabularies, neither of which the gateway advertises.
* :mod:`mewbo_speech.operations` — the request/result discriminated unions, each
  variant owning its validators, its SDK argument shape and its response parsing.
* :mod:`mewbo_speech.verbalize` — :class:`MarkdownVerbalizer`, which turns an
  assistant's markdown into the text a listener should hear. Pure: it parses and
  walks, and never calls the gateway.
* :mod:`mewbo_speech.transport` — the one module that opens a socket, behind the
  ``gateway`` extra.
* :mod:`mewbo_speech.gateway` — :class:`SpeechGateway`, the atomic client.

Down-only: imports reach :mod:`mewbo_core`, pydantic and mistune, and nothing
else. A flat re-export is safe here because no module body does I/O — the
litellm and httpx imports are per-call and guarded, so ``import mewbo_speech``
costs nothing and works with the ``gateway`` extra uninstalled.
"""

from __future__ import annotations

from mewbo_speech.audio import AudioContainer
from mewbo_speech.gateway import (
    DEFAULT_MAX_CONCURRENT_CALLS,
    DEFAULT_ROUTE_PREFIX,
    DEFAULT_SPEECH_TIMEOUT,
    DEFAULT_SYNTHESIS_MAX_RETRIES,
    DEFAULT_TRANSCRIPTION_MAX_RETRIES,
    SpeechGateway,
)
from mewbo_speech.models import (
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    SUGGESTED_VOICES,
    SYNTHESIS_FORMATS,
    SpeechMode,
    SpeechModel,
)
from mewbo_speech.operations import (
    SpeechOperation,
    SpeechRequest,
    SpeechRequestAdapter,
    SpeechResult,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
    parse_speech_request,
)
from mewbo_speech.transport import (
    LiteLlmSpeechTransport,
    SpeechGatewayError,
    SpeechTransport,
    SpeechUnavailableError,
)
from mewbo_speech.verbalize import (
    CODE_BLOCK_NOTE,
    TABLE_HEADER_LEAD,
    MarkdownVerbalizer,
)

__all__ = [
    "CODE_BLOCK_NOTE",
    "DEFAULT_MAX_CONCURRENT_CALLS",
    "DEFAULT_ROUTE_PREFIX",
    "DEFAULT_SPEECH_TIMEOUT",
    "DEFAULT_STT_MODEL",
    "DEFAULT_SYNTHESIS_MAX_RETRIES",
    "DEFAULT_TRANSCRIPTION_MAX_RETRIES",
    "DEFAULT_TTS_MODEL",
    "SUGGESTED_VOICES",
    "SYNTHESIS_FORMATS",
    "TABLE_HEADER_LEAD",
    "AudioContainer",
    "LiteLlmSpeechTransport",
    "MarkdownVerbalizer",
    "SpeechGateway",
    "SpeechGatewayError",
    "SpeechMode",
    "SpeechModel",
    "SpeechOperation",
    "SpeechRequest",
    "SpeechRequestAdapter",
    "SpeechResult",
    "SpeechTransport",
    "SpeechUnavailableError",
    "SynthesisRequest",
    "SynthesisResult",
    "TranscriptionRequest",
    "TranscriptionResult",
    "parse_speech_request",
]
