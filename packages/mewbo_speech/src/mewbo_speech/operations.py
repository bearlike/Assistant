#!/usr/bin/env python3
"""The speech operation family — one discriminated union, no dispatch switch.

Synthesis and transcription are two variants of one thing: a request the gateway
answers. They are modelled as a discriminated union on ``kind`` whose members own
their own validators, their own SDK argument shape, and their own response
parsing — so :class:`~mewbo_speech.gateway.SpeechGateway` reads
``request.SDK_OPERATION`` and ``request.parse_response(...)`` and contains no
``if kind ==`` branch. A third mode (a diarisation route, a voice-clone route) is
a new class here and nothing else.

**Models never import I/O.** A variant builds the kwargs for a call and parses an
already-fetched payload; the socket lives in :mod:`mewbo_speech.transport`. That
is what lets every rule below be tested without a gateway.

The three rules encoded here were each measured against the deployed gateway, and
each one turns an undiagnosable failure into a validation error:

* **A model id is BARE on the wire.** ``openai/supertonic-3`` in the request body
  is a 403 ``key_model_access_denied``; ``supertonic-3`` passes the key ACL. The
  ``openai/`` prefix is a litellm-SDK routing directive that the SDK strips
  client-side before the request leaves the process — verified by capturing the
  outgoing httpx request, whose body reads ``"model":"supertonic-3"`` for a call
  made with ``model="openai/supertonic-3"``. So the id and the SDK argument are
  two different strings, and :meth:`SpeechOperation.routed_model` is the single
  seam between them.
* **``voice`` is required for synthesis.** Omitting it is an HTTP 500 whose body
  is byte-identical to the one a bogus voice produces: no field name, no hint.
* **Only ``wav`` and ``flac`` come back as audio.** ``mp3``/``opus``/``aac``/
  ``pcm`` produce that same opaque 500.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from mewbo_speech.audio import AudioContainer
from mewbo_speech.models import SUGGESTED_VOICES, SYNTHESIS_FORMATS, SpeechMode


class SpeechOperation(BaseModel):
    """Shared envelope for every speech request variant."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    #: The gateway capability a model must declare to serve this variant.
    REQUIRED_MODE: ClassVar[SpeechMode]
    #: The litellm module-level coroutine that performs this variant's call.
    SDK_OPERATION: ClassVar[str]

    model: str = Field(min_length=1, description="Bare gateway model id.")

    @field_validator("model")
    @classmethod
    def _reject_provider_prefix(cls, value: str) -> str:
        """Refuse a provider-prefixed id rather than silently stripping it.

        A caller passing ``openai/supertonic-3`` or ``deepgram/nova-3`` holds a
        wrong belief about what this field is, and stripping the prefix would
        leave that belief intact until the caller hand-rolled a REST call and got
        a 403 with no explanation. The prefix belongs to the SDK call, not to the
        model id; :meth:`routed_model` applies it.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model must not be blank")
        if "/" in cleaned:
            head = cleaned.split("/", 1)[0]
            raise ValueError(
                f"model must be the bare gateway id, not {cleaned!r} — the "
                f"{head!r} prefix is a client-side SDK routing directive and is "
                "rejected on the wire as key_model_access_denied. Pass "
                f"{cleaned.rsplit('/', 1)[-1]!r}."
            )
        return cleaned

    def routed_model(self, route_prefix: str) -> str:
        """Return the model string for the litellm SDK's LOCAL provider dispatch.

        The SDK refuses a bare id with ``LLM Provider NOT provided`` before ever
        opening a socket, and strips the prefix it demands before writing the
        request body — so this string is what the SDK is called with and never
        what the gateway sees.

        Cost class: ``O(1)``.
        """
        prefix = route_prefix.strip().strip("/")
        return f"{prefix}/{self.model}" if prefix else self.model

    # Abstract, not stubs that raise: pydantic's ModelMetaclass derives from
    # ABCMeta, so these genuinely make `SpeechOperation` uninstantiable and a
    # variant that forgets one fails at construction rather than at call time.
    # The parameters are the declared contract every variant overrides against,
    # which is also why they cannot simply be deleted — dropping them here makes
    # every subclass an incompatible override.
    @abstractmethod
    def litellm_kwargs(self, route_prefix: str) -> dict[str, Any]:
        """Return the keyword arguments for this variant's SDK call."""

    @abstractmethod
    def parse_response(self, payload: Mapping[str, Any]) -> SpeechResult:
        """Turn a transport-normalised payload into this variant's result."""


class SynthesisRequest(SpeechOperation):
    """Text in, audio out — a call to a ``audio_speech`` model."""

    REQUIRED_MODE: ClassVar[SpeechMode] = SpeechMode.SYNTHESIS
    SDK_OPERATION: ClassVar[str] = "aspeech"

    kind: Literal["synthesis"] = "synthesis"
    text: str = Field(min_length=1, description="The text to speak.")
    voice: str = Field(default="", description="One of the accepted gateway voices.")
    audio_format: AudioContainer | None = Field(
        default=None,
        description="Container to request; omitted means the gateway default (WAV).",
    )

    @field_validator("voice")
    @classmethod
    def _require_a_voice(cls, value: str) -> str:
        """Require a voice to be NAMED, without deciding which names are real.

        Presence is checked here because the gateway answers an omitted voice
        with an opaque 500, and "you did not pass a voice" is a fact this side
        can establish. Which voices EXIST is not: a self-hosted backend carries
        operator-defined styles that no list here can predict, and an earlier
        closed allowlist refused exactly such a voice while asserting it knew
        the accepted set.

        Case is preserved for the same reason. Lowercasing assumed the names
        were OpenAI's, and a custom style is free to be capitalised.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(
                "voice is required for synthesis (the gateway answers an omitted "
                f"voice with an opaque HTTP 500). Common choices: "
                f"{', '.join(SUGGESTED_VOICES)} — a self-hosted backend may accept others."
            )
        return cleaned

    @field_validator("audio_format")
    @classmethod
    def _require_supported_format(cls, value: AudioContainer | None) -> AudioContainer | None:
        """Reject a container the backend answers with an opaque 500."""
        if value is not None and value not in SYNTHESIS_FORMATS:
            accepted = ", ".join(fmt.value for fmt in SYNTHESIS_FORMATS)
            raise ValueError(f"unsupported synthesis format {value.value!r}. Accepted: {accepted}.")
        return value

    def litellm_kwargs(self, route_prefix: str) -> dict[str, Any]:
        """Return ``litellm.aspeech`` kwargs.

        ``response_format`` is sent only when explicitly chosen: the omitted
        default and an explicit ``wav`` produce byte-identical responses, so
        sending it adds a parameter the backend can reject for nothing.

        Cost class: ``O(1)``.
        """
        kwargs: dict[str, Any] = {
            "model": self.routed_model(route_prefix),
            "input": self.text,
            "voice": self.voice,
        }
        if self.audio_format is not None:
            kwargs["response_format"] = self.audio_format.value
        return kwargs

    def parse_response(self, payload: Mapping[str, Any]) -> SynthesisResult:
        """Build a :class:`SynthesisResult`, sniffing the real container.

        Cost class: ``O(1)`` — the sniff reads a fixed prefix; the audio itself
        is moved, not copied field by field.
        """
        audio = payload.get("audio")
        if not isinstance(audio, (bytes, bytearray)):
            raise ValueError("synthesis payload carried no audio bytes")
        declared = payload.get("content_type")
        return SynthesisResult(
            model=self.model,
            voice=self.voice,
            audio=bytes(audio),
            container=AudioContainer.sniff(audio),
            declared_content_type=declared if isinstance(declared, str) else None,
        )


class TranscriptionRequest(SpeechOperation):
    """Audio in, text out — a call to an ``audio_transcription`` model.

    **Verified end to end against the live gateway** by driving
    :meth:`SpeechGateway.run`, once the proxy's upstream credential was rotated.
    All three containers round-trip to a real transcript:

    ===========  ==========  ========
    container    bytes       latency
    ===========  ==========  ========
    wav          215,084     0.75s
    webm/opus     22,094     0.21s
    mp3           28,827     0.42s
    ===========  ==========  ========

    **``webm``/``opus`` IS accepted** — that is what a browser ``MediaRecorder``
    produces, so no transcode step is needed and none should be built. It is also
    the cheapest of the three by an order of magnitude in bytes.

    The gateway's success body carries more than the transcript — ``task``,
    ``language``, ``duration``, and ``words[]``/``segments[]``. ``language`` and
    ``duration`` are carried through onto :class:`TranscriptionResult` as
    optional scalars; the arrays are dropped, because they grow with recording
    length and would put an unbounded payload into every response.
    """

    REQUIRED_MODE: ClassVar[SpeechMode] = SpeechMode.TRANSCRIPTION
    SDK_OPERATION: ClassVar[str] = "atranscription"

    kind: Literal["transcription"] = "transcription"
    audio: bytes = Field(min_length=1, description="The recorded audio payload.")
    filename: str = Field(
        default="audio.wav",
        min_length=1,
        description="Multipart part name; its extension is the backend's format hint.",
    )
    language: str | None = Field(default=None, description="Optional BCP-47 language hint.")

    def litellm_kwargs(self, route_prefix: str) -> dict[str, Any]:
        """Return ``litellm.atranscription`` kwargs.

        The file arrives as a ``(filename, bytes)`` tuple — the multipart shape
        the SDK accepts without a filesystem round trip, so a browser recording
        never has to be spooled to disk to be transcribed.

        Cost class: ``O(1)`` in call count; the payload is referenced, not copied.
        """
        kwargs: dict[str, Any] = {
            "model": self.routed_model(route_prefix),
            "file": (self.filename, self.audio),
        }
        if self.language:
            kwargs["language"] = self.language
        return kwargs

    def parse_response(self, payload: Mapping[str, Any]) -> TranscriptionResult:
        """Build a :class:`TranscriptionResult`.

        Cost class: ``O(1)``.
        """
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("transcription payload carried no text")
        language = payload.get("language")
        duration = payload.get("duration")
        return TranscriptionResult(
            model=self.model,
            text=text,
            language=language if isinstance(language, str) else None,
            duration=float(duration) if isinstance(duration, (int, float)) else None,
        )


class SynthesisResult(BaseModel):
    """Audio produced by a synthesis call.

    ``container`` is sniffed from the bytes; ``declared_content_type`` is what the
    gateway claimed. The two disagree on every successful response the deployed
    backend produces, and keeping both is what makes that visible instead of
    turning it into a mislabelled download.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    kind: Literal["synthesis"] = "synthesis"
    model: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    audio: bytes = Field(min_length=1)
    container: AudioContainer
    declared_content_type: str | None = None

    @property
    def content_type(self) -> str:
        """The MIME type to serve these bytes as — derived, never declared."""
        return self.container.content_type

    @property
    def declared_type_was_wrong(self) -> bool:
        """Whether the gateway's own header disagrees with the payload."""
        declared = (self.declared_content_type or "").split(";", 1)[0].strip().lower()
        return bool(declared) and declared != self.content_type


class TranscriptionResult(BaseModel):
    """Text produced by a transcription call, plus the gateway's two scalars.

    ``language`` and ``duration`` are optional and default to ``None`` so every
    existing caller is unaffected — and so a gateway that omits them (or a
    transport that predates them) yields "not reported" rather than a validation
    error. ``duration`` is the recording's length in seconds, which is the
    natural thing to meter or display for a capture.

    The same response body also carries ``words[]`` and ``segments[]``, and
    those are deliberately dropped: they grow with recording length, so carrying
    them would put an unbounded array into every response for no consumer.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    kind: Literal["transcription"] = "transcription"
    model: str = Field(min_length=1)
    text: str
    language: str | None = Field(default=None, description="BCP-47 tag the gateway detected.")
    duration: float | None = Field(default=None, description="Audio length in seconds.")


SpeechRequest = Annotated[
    SynthesisRequest | TranscriptionRequest,
    Field(discriminator="kind"),
]
SpeechResult = Annotated[
    SynthesisResult | TranscriptionResult,
    Field(discriminator="kind"),
]

# The ONE parse seam for the request family — the shape `mewbo_core.triggers.spec`
# uses. A caller validating a wire payload goes through this, never through a
# hand-written `kind` lookup.
SpeechRequestAdapter: TypeAdapter[SpeechRequest] = TypeAdapter(SpeechRequest)
parse_speech_request = SpeechRequestAdapter.validate_python
