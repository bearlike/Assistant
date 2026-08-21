#!/usr/bin/env python3
"""REST contract for the speech namespace — capabilities, synthesis, transcription.

Three routes over :mod:`mewbo_speech`, which owns every gateway rule this module
must not re-derive: the bare-vs-prefixed model id, the eleven accepted voices,
the two working containers, and the magic-byte sniff that corrects the gateway's
always-wrong ``Content-Type``. This module owns only the HTTP half — the wire
bodies, the bounds, and the refusals.

Paradigm: one atomic :class:`SpeechRoutesController` holds the collaborators as
FIELDS (a gateway reader, a config reader, the concurrency bound, a clock) and
every domain helper as a METHOD; the Flask-RESTX Resources are thin adapters
that receive it by dependency injection through ``resource_class_kwargs``. The
request path reads no module state.

**One envelope for the whole namespace, INCLUDING the 400s.** Every refusal is
``{"error": {"code", "reason", "retryable"}}`` — a client branches on ``code``
and never parses prose:

======  ============================  =========  ==========================================
Status  ``code``                      Retryable  Raised when
======  ============================  =========  ==========================================
400     ``invalid_request``           no         unknown voice/format, blank or over-long
                                                 text, an unknown body key
413     ``audio_too_large``           no         the upload exceeds the published cap
502     ``speech_gateway_error``      yes        the gateway refused or failed the call
502     ``speech_gateway_timeout``    yes        our own deadline elapsed first
503     ``speech_unavailable``        no         the library is absent or the gateway is
                                                 unconfigured for that direction
503     ``speech_capacity_exhausted`` yes        every in-flight slot is taken
======  ============================  =========  ==========================================

**Voice and format are validated HERE, before the call, and that is the whole
reason this boundary exists.** The deployed gateway answers a missing voice, a
bogus voice and an unsupported container with the same opaque HTTP 500 whose
body names no field and enumerates nothing, so a value that reaches the gateway
can never be diagnosed afterwards. :data:`~mewbo_speech.SPEECH_VOICES` and
:data:`~mewbo_speech.SYNTHESIS_FORMATS` are the vocabularies, read from the
package rather than restated, so a widened set cannot drift out of step here.

**Availability is derived, never probed.** ``GET /api/speech/capabilities`` is on
an interactive path — a client polls it to decide whether to render a microphone
— so it makes no health call. ``available`` is the conjunction of three O(1)
facts: the routes are mounted (true by construction, since this module is
imported only when ``mewbo_speech`` resolves), the gateway is configured and its
extra installed, and a model id is configured for that direction. A consequence
worth stating rather than discovering: ``transcription.available`` reads true
while the gateway's upstream credential is dead. That is correct — the
deployment IS configured for transcription, and the credential failure surfaces
as a 502 on the call that actually makes it, which is the only place it can be
observed without spending 5-17 s on every poll.

**Nothing here streams.** ``stream=true`` measured a no-op against this gateway
(time-to-first-byte equal to total time, because the backend buffers the whole
file before sending a byte), so there is nothing to render progressively and no
long-lived response to bound. What IS bounded is in-flight calls; see
:attr:`SpeechRoutesController.MAX_CONCURRENT_CALLS`.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, final

from flask import Response, request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.config import get_config
from mewbo_speech import (
    SUGGESTED_VOICES,
    SYNTHESIS_FORMATS,
    AudioContainer,
    MarkdownVerbalizer,
    SpeechGateway,
    SpeechGatewayError,
    SpeechMode,
    SpeechModel,
    SpeechRequest,
    SpeechResult,
    SpeechUnavailableError,
    SynthesisRequest,
    SynthesisResult,
    TranscriptionRequest,
    TranscriptionResult,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_api.auth.guard_registry import guard
from mewbo_api.errors import ApiError, CapabilityUnavailable, ErrorPayload, RequestInvalid
from mewbo_api.responses import ApiResponseKit
from mewbo_api.stream_capacity import StreamCapacity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_core.config import SpeechConfig, SpeechTtsConfig

logging = get_logger(name="api.speech.routes")

speech_ns = Namespace("speech", description="Text-to-speech and speech-to-text")
kit = ApiResponseKit(speech_ns, prefix="Speech")


# ---------------------------------------------------------------------------
# Refusals — the three codes this namespace adds to the shared taxonomy
# ---------------------------------------------------------------------------
# ``speech_unavailable`` reuses ``CapabilityUnavailable`` and ``invalid_request``
# reuses ``RequestInvalid`` (envelope shape, semantic code) — both already render
# exactly what the contract asks for. Only the three below have no existing home,
# and they live beside the handlers that raise them for the same reason
# ``wiki/errors.py`` does: the status is shared, the CODE belongs to the surface.


@final
class AudioTooLarge(ApiError):
    """413 — the uploaded audio exceeds the cap published in ``capabilities``.

    Not retryable and not negotiable: the same bytes will be refused again. The
    reason names the limit because a client that guessed wrong has no other way
    to learn the number it should have read from ``capabilities``.
    """

    status: ClassVar[int] = 413

    @classmethod
    def for_limit(cls, limit_bytes: int, *, actual_bytes: int | None = None) -> AudioTooLarge:
        """A 413 naming the cap, and the offending size when it is known."""
        seen = f" (received {actual_bytes} bytes)" if actual_bytes is not None else ""
        return cls(
            ErrorPayload(
                code="audio_too_large",
                reason=(
                    f"Audio upload exceeds the {limit_bytes} byte limit{seen}. "
                    "The limit is published as transcription.limits.max_audio_bytes."
                ),
                retryable=False,
            )
        )


@final
class SpeechGatewayFailure(ApiError):
    """502 — the call reached our boundary and the gateway did not answer it.

    Two codes, one status, because a client treats them identically (retry) and
    an operator does not: ``speech_gateway_error`` is the gateway's own refusal
    or failure — this is where a dead upstream credential lands, with the
    gateway's message carried verbatim because it is frequently the most
    specific thing that exists — while ``speech_gateway_timeout`` means OUR
    deadline elapsed and the gateway may still be working.
    """

    status: ClassVar[int] = 502

    @classmethod
    def refused(cls, reason: str) -> SpeechGatewayFailure:
        """The gateway refused or failed the call; *reason* is its own message."""
        return cls(
            ErrorPayload(code="speech_gateway_error", reason=reason, retryable=True),
        )

    @classmethod
    def timed_out(cls, deadline_s: float) -> SpeechGatewayFailure:
        """Our own *deadline_s* elapsed before the gateway answered."""
        return cls(
            ErrorPayload(
                code="speech_gateway_timeout",
                reason=(
                    f"The speech gateway did not answer within {deadline_s:g}s. "
                    "The request may still be running upstream; retry shortly."
                ),
                retryable=True,
            )
        )


@final
class SpeechCapacityExhausted(ApiError):
    """503 — every in-flight speech slot is taken, so this call is refused.

    Sibling of ``StreamCapacityExhausted`` and told apart from it — and from
    ``CapabilityUnavailable``, which shares the status — by ``code`` alone.
    Retryable, and it carries its own delay because the caller has no way to
    guess one. The reason names the bound, since a refusal that does not say
    which number was reached sends whoever is paged to read the source.
    """

    status: ClassVar[int] = 503

    #: Short enough that a client retries while the user is still looking at the
    #: screen, long enough that a fleet of them is not the next thundering herd.
    #: Matches ``StreamCapacityExhausted.RETRY_AFTER_SECONDS`` deliberately: two
    #: different delays for the same "come back shortly" would be noise.
    RETRY_AFTER_SECONDS: ClassVar[int] = 5

    @classmethod
    def for_limit(cls, limit: int) -> SpeechCapacityExhausted:
        """A retryable 503 naming the *limit* that was reached."""
        return cls(
            ErrorPayload(
                code="speech_capacity_exhausted",
                reason=(
                    f"Already serving {limit} concurrent speech calls. Retry "
                    "shortly — a slot frees as soon as one finishes."
                ),
                retryable=True,
            )
        )


# ---------------------------------------------------------------------------
# Wire models — Pydantic, extra="forbid", validated AT DEFINITION
# ---------------------------------------------------------------------------


class VerbalizedTextTooLong(ValueError):
    """Text that fitted the published cap but not the post-verbalization one.

    A distinct type rather than a bare ``ValueError`` so the controller can tell
    it from the ``ValidationError`` the same call raises for an unusable model
    id. The two carry opposite blame — this one is always the caller's text —
    and catching them together would report an operator misconfiguration for a
    client's oversized document.
    """


class SynthesizeBody(BaseModel):
    """The ``POST /api/speech/synthesize`` request body.

    ``extra="forbid"`` is load-bearing rather than hygiene: a client sending
    ``audio_format`` (the package's internal field name) instead of
    ``response_format`` gets a 400 naming the key, not a silent fall-back to the
    configured default that would leave it believing it had chosen FLAC.

    Every optional field is ``None`` when omitted, and the CONTROLLER applies the
    configured default — not a default declared here. Config stays the single
    source of truth for what "unspecified" means, so an operator changing the
    voice changes it for every client that did not ask for a specific one.
    """

    model_config = ConfigDict(extra="forbid")

    #: Roughly 20 s of speech on ``supertonic-3`` and 40 s on ``-hd`` at the
    #: measured rates. The cap exists because synthesis time scales with input
    #: length and a request holds one of the API's request threads for its whole
    #: duration — an unbounded text is an unbounded occupancy.
    MAX_TEXT_CHARS: ClassVar[int] = 2000

    text: str = Field(description="The text to speak.")
    model: str | None = Field(default=None, description="Bare gateway model id.")
    voice: str | None = Field(default=None, description="One of the accepted voices.")
    response_format: str | None = Field(default=None, description="``wav`` or ``flac``.")
    verbalize: bool = Field(
        default=True,
        description="Read the text as markdown; false speaks it exactly as sent.",
    )

    @field_validator("text")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        """Refuse blank or over-long text, naming the cap.

        Whitespace-only text is refused rather than sent: the gateway answers it
        with the same opaque 500 it answers every other parameter mistake with,
        so a caller who sent an empty textarea would get a failure that names
        nothing at all.

        **The cap is measured on the text AS SENT, before verbalization**, and
        that ordering is load-bearing. The cap exists to bound how long one call
        occupies a request thread, which scales with what is finally spoken —
        but verbalization is NOT monotonically shrinking (see
        :meth:`to_request`), so checking only the source would let a caller past
        the bound. The pair works because the SOURCE check is the one a client
        can predict: `max_text_chars` is published, a client sizes its chunk
        against it, and it is never refused for a length it could not compute.
        The post-verbalization bound is enforced separately, where a breach can
        be reported as a server-side fact rather than blamed on the caller.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        if len(cleaned) > cls.MAX_TEXT_CHARS:
            raise ValueError(
                f"text is {len(cleaned)} characters; the limit is {cls.MAX_TEXT_CHARS}. "
                "The limit is published as synthesis.limits.max_text_chars."
            )
        return cleaned

    @field_validator("voice")
    @classmethod
    def _a_named_voice(cls, value: str | None) -> str | None:
        """Normalise a voice without ruling on which names exist.

        This once validated against a closed list and refused everything else.
        The list held only OpenAI's canonical names, so a self-hosted backend's
        own trained voice style was rejected here with a message asserting the
        accepted set — a wrong answer delivered confidently. Which voices exist
        is the gateway's fact, and an unknown one now fails there.

        Case is preserved: lowercasing presumed the names were OpenAI's, and an
        operator is free to capitalise theirs.
        """
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("response_format")
    @classmethod
    def _known_format(cls, value: str | None) -> str | None:
        """Refuse a container the gateway does not actually produce.

        Checked against :data:`~mewbo_speech.SYNTHESIS_FORMATS` rather than the
        whole :class:`~mewbo_speech.AudioContainer` enum, which also carries the
        formats this gateway REJECTS (``mp3``, ``ogg``, ``mp4``). Letting the
        enum refuse would produce a message listing values that do not work.
        """
        if value is None:
            return None
        cleaned = value.strip().lower()
        accepted = tuple(fmt.value for fmt in SYNTHESIS_FORMATS)
        if cleaned not in accepted:
            raise ValueError(
                f"unsupported response_format {value!r}. Accepted: {', '.join(accepted)}."
            )
        return cleaned

    @field_validator("model")
    @classmethod
    def _present_model(cls, value: str | None) -> str | None:
        """Normalise an omitted-or-blank model to ``None`` so the default applies."""
        cleaned = (value or "").strip()
        return cleaned or None

    #: The ceiling on text AFTER verbalization. Sized at 1.5x
    #: :attr:`MAX_TEXT_CHARS` because verbalization can LENGTHEN text and the
    #: expansion is bounded but not by one: measured over 1,262 real assistant
    #: replies it lengthens 29.6% of them, by a median ratio of 0.99 and a p90
    #: of 1.05, with the largest real growth 4 characters. The expansion comes
    #: from a code block, whose fence is replaced by a whole sentence, so the
    #: adversarial worst case is a document made entirely of empty fences — 2.5x
    #: measured. No corpus reply under the source cap crosses even the source
    #: cap after verbalization, so this bound refuses only the adversarial
    #: shape, and refusing is what keeps the occupancy argument true.
    MAX_VERBALIZED_CHARS: ClassVar[int] = MAX_TEXT_CHARS * 3 // 2

    def to_request(
        self, defaults: SpeechTtsConfig, verbalizer: MarkdownVerbalizer
    ) -> SynthesisRequest:
        """Build the package request, filling every omitted field from *defaults*.

        *defaults* and *verbalizer* arrive as ARGUMENTS rather than being read
        here: this model imports no I/O and no config accessor, which is also
        what lets a test drive every combination without a config file.

        **Verbalization runs here, at the boundary, and is on by default.** The
        text a client posts is markdown — an assistant's reply is — and spoken
        verbatim its fences, pipes and URLs are read out as punctuation. Doing
        it server-side costs no extra round trip, because both clients already
        POST every chunk to this route. ``verbalize: false`` speaks the text
        exactly as sent, which is what a caller synthesizing a literal string
        wants.

        **Verbalized text is NOT guaranteed shorter than its source**, so the
        result is re-checked against :attr:`MAX_VERBALIZED_CHARS`. A breach
        raises a plain ``ValueError`` rather than this class's validators'
        message about the published cap: the caller respected the published cap,
        and telling them otherwise would send them to shorten text that was
        already short enough.

        ``response_format`` is always sent, deviating from the package's own
        "send it only when explicitly chosen" note. The reason is that the
        operator's configured container has to be honoured for a client that did
        not name one, and ``wav`` — the value that would otherwise be omitted —
        is a measured-accepted parameter, so sending it costs nothing.

        Cost class: ``O(text length)`` — one markdown parse and walk, measured
        warm at ~0.5 ms per KB against a synthesis call that takes seconds. No
        I/O.
        """
        spoken = self.text
        if self.verbalize:
            # A document whose whole content is markup — a lone horizontal rule,
            # an HTML block — verbalizes to nothing, and empty text is one more
            # thing the gateway answers with its undiagnosable 500. Speaking the
            # source is the honest fallback: the caller asked for audio and gets
            # audio, rather than an error naming a field they did not send.
            spoken = verbalizer.verbalize(self.text) or self.text
            if len(spoken) > self.MAX_VERBALIZED_CHARS:
                raise VerbalizedTextTooLong(
                    f"the text expands to {len(spoken)} characters once read as "
                    f"markdown, over the {self.MAX_VERBALIZED_CHARS}-character "
                    "ceiling. Send verbalize=false to speak it as written."
                )
        return SynthesisRequest(
            model=self.model or defaults.model,
            text=spoken,
            voice=self.voice or defaults.voice,
            audio_format=AudioContainer(self.response_format or defaults.response_format),
        )


# ---------------------------------------------------------------------------
# OpenAPI documentation models — doc-only; the Pydantic models above validate
# ---------------------------------------------------------------------------

speech_model_model = speech_ns.model(
    "SpeechModelInfo",
    {
        "id": fields.String(example="supertonic-3", description="Bare gateway model id."),
        "mode": fields.String(
            example="audio_speech",
            description="`audio_speech` or `audio_transcription`, as the gateway reports it.",
        ),
        "display_name": fields.String(
            example="supertonic-3", description="Human label; defaults to the id."
        ),
    },
)

synthesis_capability_model = speech_ns.model(
    "SpeechSynthesisCapability",
    {
        "available": fields.Boolean(
            example=True,
            description="Whether this deployment is configured to synthesize speech.",
        ),
        "models": fields.List(
            fields.Nested(speech_model_model),
            description="Synthesis models the gateway advertises; `[]` if it could not be reached.",
        ),
        "voices": fields.List(
            fields.String,
            example=list(SUGGESTED_VOICES),
            description="Common voices; a self-hosted gateway may accept others.",
        ),
        "formats": fields.List(
            fields.String,
            example=[fmt.value for fmt in SYNTHESIS_FORMATS],
            description="Containers the gateway actually produces.",
        ),
        "defaults": fields.Raw(
            example={"model": "supertonic-3", "voice": "nova", "response_format": "wav"},
            description="What an omitted field resolves to, from server config.",
        ),
        "limits": fields.Raw(
            example={"max_text_chars": SynthesizeBody.MAX_TEXT_CHARS},
            description="Bounds a client must respect to avoid a 400.",
        ),
        "verbalizes_markdown": fields.Boolean(
            example=True,
            description=(
                "Whether this server reads `text` as markdown before speaking it. "
                "When true a client can send an assistant's reply unmodified and "
                "does not need a markdown stripper of its own."
            ),
        ),
    },
)

transcription_capability_model = speech_ns.model(
    "SpeechTranscriptionCapability",
    {
        "available": fields.Boolean(
            example=True,
            description="Whether this deployment is configured to transcribe audio.",
        ),
        "models": fields.List(
            fields.Nested(speech_model_model),
            description="Transcription models the gateway advertises.",
        ),
        "defaults": fields.Raw(
            example={"model": "nova-3"}, description="What an omitted model resolves to."
        ),
        "limits": fields.Raw(
            example={"max_audio_bytes": 10485760},
            description="Bounds a client must respect to avoid a 413.",
        ),
    },
)

capabilities_model = speech_ns.model(
    "SpeechCapabilities",
    {
        "synthesis": fields.Nested(synthesis_capability_model),
        "transcription": fields.Nested(transcription_capability_model),
        "limits": fields.Raw(
            example={"max_concurrent_calls": 4},
            description="Bounds shared by both directions.",
        ),
    },
)

synthesize_request_model = speech_ns.model(
    "SpeechSynthesizeRequest",
    {
        "text": fields.String(
            required=True,
            example="The build finished in four minutes.",
            description=f"Text to speak; up to {SynthesizeBody.MAX_TEXT_CHARS} characters.",
        ),
        "model": fields.String(
            example="supertonic-3", description="Defaults to `speech.tts.model`."
        ),
        "voice": fields.String(example="nova", description="Defaults to `speech.tts.voice`."),
        "response_format": fields.String(
            example="wav", description="`wav` or `flac`; defaults to `speech.tts.response_format`."
        ),
        "verbalize": fields.Boolean(
            default=True,
            example=True,
            description=(
                "Read `text` as markdown and speak what it means: headings and list "
                "items become their own spoken units, a code block is announced once "
                "instead of read out, a link becomes its text, and a table's header "
                "is announced once before its rows. Send `false` to speak the string "
                "exactly as written."
            ),
        ),
    },
)

transcribe_response_model = speech_ns.model(
    "SpeechTranscribeResponse",
    {
        "text": fields.String(
            example="The build finished in four minutes.", description="The transcript."
        ),
        "model": fields.String(example="nova-3", description="The model that produced it."),
    },
)


# ---------------------------------------------------------------------------
# Controller — the atomic class owning collaborators + every helper
# ---------------------------------------------------------------------------


class SpeechRoutesController:
    """Owns the speech REST behaviour over its injected collaborators.

    Collaborators are FIELDS: a reader that produces a configured
    :class:`~mewbo_speech.SpeechGateway`, a reader for the live ``speech`` config
    section, the shared concurrency bound, and the clock the failure cache is
    measured on. One instance is built by :func:`init_speech_routes` and handed
    to every Resource; a test points it at a scripted gateway by reassigning
    ``gateway_reader``, with every request built and every response parsed still
    being production code.

    **The gateway is read per call, never captured.** ``speech.api_base`` /
    ``speech.api_key`` are editable through ``PATCH /api/config``, and a gateway
    captured at boot would keep answering from the old coordinates until a
    restart — a staleness with no symptom other than calls failing against a
    server the operator already re-pointed. Building one is ``O(1)`` (it reads
    the process-cached config and opens no socket), so there is nothing to
    amortise. **A reader must return a FRESH instance per call**: the catalogue
    leg lowers ``timeout`` on the instance it is handed, and a shared instance
    would carry that 3 s deadline into a 90 s synthesis.

    The model catalogue is therefore cached HERE rather than relying on the
    gateway's own process-life cache, which a per-call instance can never hit.
    """

    #: Synthesis and transcription SHARE this bound. The gateway's two TTS routes
    #: each declare ``max_parallel_requests: 2``, so beyond about four in flight a
    #: caller queues at the GATEWAY while still holding one of this API's 48
    #: request threads — occupancy the API pays for and cannot see. Capping at
    #: four keeps worst-case speech occupancy at 4/48 and makes the refusal
    #: happen here, where it is diagnosable, instead of there, where it is not.
    MAX_CONCURRENT_CALLS: ClassVar[int] = 4

    #: 10 MiB, published in ``capabilities`` as ``max_audio_bytes``.
    MAX_AUDIO_BYTES: ClassVar[int] = 10 * 1024 * 1024

    #: Per-call deadlines enforced at OUR boundary. The library passes no timeout
    #: to litellm, whose own default is 600 s — long enough that a wedged call
    #: would hold a request thread for ten minutes. Sized from measurement: the
    #: slowest observed synthesis is a few seconds and the dead-credential STT
    #: failure surfaces at ~17 s from litellm's retry loop, inside the 30 s bound.
    SYNTHESIS_DEADLINE_S: ClassVar[float] = 60.0
    TRANSCRIPTION_DEADLINE_S: ClassVar[float] = 30.0

    #: The catalogue read is on an interactive path, so it gets its own tight
    #: deadline rather than the configured (90 s) call timeout.
    CATALOGUE_TIMEOUT_S: ClassVar[float] = 3.0

    #: How long a failed catalogue read is remembered. Without it a gateway that
    #: is down turns every capabilities poll into a fresh 3 s stall — the polling
    #: client would then be slower than the one that never asked.
    CATALOGUE_FAILURE_TTL_S: ClassVar[float] = 60.0

    def __init__(
        self,
        *,
        gateway_reader: Callable[[], SpeechGateway],
        config_reader: Callable[[], SpeechConfig],
        capacity: StreamCapacity,
        monotonic: Callable[[], float] = time.monotonic,
        verbalizer: MarkdownVerbalizer | None = None,
    ) -> None:
        """Capture the injected collaborators as instance state.

        *monotonic* is injected so the failure cache's expiry is drivable in a
        test without sleeping. No auth guard is injected: every Resource declares
        its own requirement with ``@guard.requires``, resolved at request time.

        *verbalizer* is built once and SHARED, unlike the gateway, which is read
        per call. The two differ because the gateway holds live coordinates an
        operator can re-point mid-process while the verbalizer holds only a
        markdown parser; and because sharing is safe — mistune allocates its
        parse state per call, verified by parsing corpus documents concurrently
        across threads and getting trees identical to the single-threaded ones.
        Building one per request would pay the parser's construction on every
        synthesis for nothing.
        """
        self.gateway_reader = gateway_reader
        self.config_reader = config_reader
        self.capacity = capacity
        self.monotonic = monotonic
        self.verbalizer = verbalizer or MarkdownVerbalizer()
        self._catalogue: list[SpeechModel] | None = None
        self._catalogue_failed_until: float = 0.0

    # ── capabilities ────────────────────────────────────────────────────────

    def capabilities(self) -> dict[str, Any]:
        """The capability document, derived from config with no health probe.

        Cost class: ``O(1)`` on the cached path — three config reads and a
        dependency probe that hits ``sys.modules``. The first call per process
        adds ONE HTTP round trip bounded by :attr:`CATALOGUE_TIMEOUT_S`, and a
        failure is remembered for :attr:`CATALOGUE_FAILURE_TTL_S` so a dead
        gateway costs that stall once a minute rather than once a poll.

        **This never raises.** Every leg degrades: an unreachable gateway empties
        the model lists while defaults, voices, formats and limits still answer,
        because a client needs those to render a picker regardless of whether the
        gateway happened to be up when it asked.
        """
        speech = self.config_reader()
        configured = self._gateway_configured()
        tts_model = speech.tts.model.strip()
        stt_model = speech.stt.model.strip()
        return {
            "synthesis": {
                "available": configured and bool(tts_model),
                "models": self._offered_models(SpeechMode.SYNTHESIS),
                "voices": list(SUGGESTED_VOICES),
                "formats": [fmt.value for fmt in SYNTHESIS_FORMATS],
                "defaults": {
                    "model": tts_model,
                    "voice": speech.tts.voice,
                    "response_format": speech.tts.response_format,
                },
                "limits": {"max_text_chars": SynthesizeBody.MAX_TEXT_CHARS},
                # Advertised so a client knows the server already does this and
                # can stop stripping markdown itself. Absent, a client's only
                # safe assumption is that it must keep its own stripper, which
                # is the divergence this feature exists to end.
                "verbalizes_markdown": True,
            },
            "transcription": {
                "available": configured and bool(stt_model),
                "models": self._offered_models(SpeechMode.TRANSCRIPTION),
                "defaults": {"model": stt_model},
                "limits": {"max_audio_bytes": self.MAX_AUDIO_BYTES},
            },
            "limits": {"max_concurrent_calls": self.MAX_CONCURRENT_CALLS},
        }

    @staticmethod
    def _model_dto(model: SpeechModel) -> dict[str, str]:
        """Project one advertised model onto the wire. Cost class: ``O(1)``."""
        return {"id": model.id, "mode": model.mode.value, "display_name": model.display_name}

    def _gateway_or_none(self) -> SpeechGateway | None:
        """Build a gateway, or ``None`` when configuration cannot produce one.

        ``SpeechGateway.from_config`` validates the WHOLE app-config document on
        the way to reading two fields, so an unrelated invalid section — an
        ``${ENV_VAR}`` reference nothing sets, in a block speech never touches —
        raises here. That is shared behaviour, not something to patch around, but
        it does mean an unbuildable gateway is a state this surface has to report
        rather than an exception it can let escape.

        Cost class: ``O(1)`` — the process-cached config, no I/O.
        """
        try:
            return self.gateway_reader()
        except Exception as exc:  # noqa: BLE001 — absence is a state, not a failure
            logging.warning("the speech gateway could not be built from config: {}", exc)
            return None

    def _gateway_configured(self) -> bool:
        """Whether a gateway builds, has coordinates, AND has its extra installed.

        All three halves matter and none touches the network: coordinates alone
        would advertise a capability that dies on ``SpeechUnavailableError`` at
        the first call, and the dependency probe alone says nothing about where
        to send it.

        Cost class: ``O(1)``. Never raises.
        """
        gateway = self._gateway_or_none()
        return gateway is not None and gateway.is_available()

    def _require_gateway(self) -> SpeechGateway:
        """Resolve the gateway for a CALL, refusing with a 503 that says which fix.

        The two refusals are deliberately different sentences. "Set
        ``speech.api_base``" is wrong and misleading when the real failure is a
        config document that will not validate at all — the operator would go
        and look at a section that is already correct.
        """
        gateway = self._gateway_or_none()
        if gateway is None:
            raise CapabilityUnavailable.for_capability(
                "speech_unavailable",
                "The speech gateway could not be built: the app configuration "
                "document failed to validate. The failing section may be an "
                "unrelated one — the whole document is validated on the way to "
                "reading the speech fields. The server log names it.",
            )
        if not gateway.is_available():
            raise CapabilityUnavailable.for_capability(
                "speech_unavailable",
                "The speech gateway is not configured. Set speech.api_base and "
                "speech.api_key (or the llm.* equivalents), and install the "
                "mewbo-speech[gateway] extra.",
            )
        return gateway

    def _catalogue_by_mode(self) -> dict[SpeechMode, list[SpeechModel]]:
        """The advertised speech models grouped by mode, ``{}`` when unavailable.

        Cost class: ``O(1)`` once warmed or once failed; ``O(collection)`` in the
        gateway's advertised routes on the one read that populates it.
        """
        models = self._catalogue_models()
        grouped: dict[SpeechMode, list[SpeechModel]] = {}
        for model in models:
            grouped.setdefault(model.mode, []).append(model)
        return grouped

    def speech_model_ids(self) -> frozenset[str]:
        """Every id this gateway serves as a speech route, either direction.

        Exported through the package so the chat model picker can subtract
        them. It reuses the same discovery the capability document is built
        from, which is what keeps one answer rather than two: a hand-kept list
        of speech ids goes stale the moment an operator swaps a model, and the
        symptom is a picker offering a model the gateway no longer has while
        hiding the one it does.

        Cost class: ``O(1)`` once warmed — a cached lookup, no network call.
        """
        return frozenset(
            model.id
            for mode in (SpeechMode.SYNTHESIS, SpeechMode.TRANSCRIPTION)
            for model in self._catalogue_by_mode().get(mode, ())
        )

    def _offered_models(self, mode: SpeechMode) -> list[dict[str, str]]:
        """Every model a client should offer for *mode*.

        The gateway's own classification, plus the configured default so a
        picker is never empty while a deployment is nonetheless synthesizing
        with that model — which is the state a discovery outage produces, and
        the one where an empty list would read as "speech is unavailable".

        Cost class: ``O(collection)`` in the models offered, which is a handful.
        """
        offered: dict[str, dict[str, str]] = {
            model.id: self._model_dto(model)
            for model in self._catalogue_by_mode().get(mode, ())
        }
        speech = self.config_reader()
        leg = speech.tts if mode is SpeechMode.SYNTHESIS else speech.stt
        default = leg.model.strip()
        if default and default not in offered:
            offered[default] = {"id": default, "mode": mode.value}
        return list(offered.values())

    def _catalogue_models(self) -> list[SpeechModel]:
        """Fetch-and-cache the speech catalogue, degrading to ``[]`` on failure.

        **Empty is the expected answer on the deployment as it stands**, and that
        is an operational fact rather than a defect here: the runtime virtual key
        is allowed only ``llm_api_routes``, so ``/model/info`` answers 403 for it.
        ``/v1/models`` is not a substitute — it returns bare ids with no ``mode``,
        the one field that separates a TTS route from an STT one from a chat one.
        Until the key is granted the route (or discovery gets its own admin key),
        a client renders the configured defaults and lets the operator type a
        model id. **Do not add a name heuristic**: classifying ``supertonic-3`` as
        TTS because of what it is called is exactly the guess the package's mode
        field exists to avoid.
        """
        if self._catalogue is not None:
            return self._catalogue
        if self.monotonic() < self._catalogue_failed_until:
            return []
        try:
            gateway = self.gateway_reader()
            gateway.timeout = self.CATALOGUE_TIMEOUT_S
            catalogue = gateway.list_models()
        except Exception as exc:  # noqa: BLE001 — capabilities must never 500
            self._catalogue_failed_until = self.monotonic() + self.CATALOGUE_FAILURE_TTL_S
            logging.warning(
                "speech model listing failed ({}); reporting empty lists for {:g}s",
                exc,
                self.CATALOGUE_FAILURE_TTL_S,
            )
            return []
        self._catalogue = catalogue
        return catalogue

    # ── synthesis ───────────────────────────────────────────────────────────

    def synthesize(self, body: Mapping[str, Any]) -> SynthesisResult:
        """Validate *body*, apply configured defaults, and call the gateway.

        Cost class: ``O(text length)`` — measured at roughly half a second for a
        sentence and four seconds for a paragraph on ``supertonic-3``, bounded by
        :attr:`SYNTHESIS_DEADLINE_S`. The caller holds one request thread for the
        whole call; admission is the caller's job (see the route).
        """
        speech = self.config_reader()
        gateway = self._require_direction(SpeechMode.SYNTHESIS, bool(speech.tts.model.strip()))
        try:
            wire = SynthesizeBody.model_validate(dict(body))
        except ValidationError as exc:
            raise self._invalid(exc) from exc
        try:
            speech_request = wire.to_request(speech.tts, self.verbalizer)
        except VerbalizedTextTooLong as exc:
            # Always the client's text, and it names the opt-out — so it must
            # not reach the ValidationError arm below, which would blame the
            # operator's configured model whenever the client did not name one.
            raise RequestInvalid.field_error(
                "text", str(exc), code="invalid_request", shape="envelope"
            ) from exc
        except ValidationError as exc:
            # Split by WHO supplied the offending value. Everything the client
            # can set has already passed this module's own validators, so a
            # failure here comes from the client only when it named the model
            # itself; otherwise the operator's `speech.tts.model` is the one the
            # package refuses, and blaming the caller for it would send the
            # wrong person to debug it.
            if wire.model is not None:
                raise self._invalid(exc) from exc
            raise CapabilityUnavailable.for_capability(
                "speech_unavailable",
                f"speech.tts.model is not a usable gateway model id: {exc}",
            ) from exc
        result = self._run(gateway, speech_request, self.SYNTHESIS_DEADLINE_S)
        assert isinstance(result, SynthesisResult)  # noqa: S101 - variant invariant of run()
        return result

    # ── transcription ───────────────────────────────────────────────────────

    def transcribe(
        self,
        *,
        audio: bytes,
        filename: str,
        mimetype: str,
        model: str | None,
        language: str | None,
    ) -> TranscriptionResult:
        """Transcribe *audio*, using the configured model when none is named.

        Cost class: ``O(audio length)``, bounded by
        :attr:`TRANSCRIPTION_DEADLINE_S` and by :attr:`MAX_AUDIO_BYTES` on the
        input. The caller holds one request thread for the whole call.
        """
        speech = self.config_reader()
        gateway = self._require_direction(SpeechMode.TRANSCRIPTION, bool(speech.stt.model.strip()))
        self.ensure_within_size_limit(len(audio))
        named = (model or "").strip() or None
        try:
            speech_request = TranscriptionRequest(
                model=named or speech.stt.model,
                audio=audio,
                filename=self.format_hint(filename, mimetype),
                language=(language or "").strip() or None,
            )
        except ValidationError as exc:
            if named is not None:
                raise self._invalid(exc) from exc
            raise CapabilityUnavailable.for_capability(
                "speech_unavailable",
                f"speech.stt.model is not a usable gateway model id: {exc}",
            ) from exc
        result = self._run(gateway, speech_request, self.TRANSCRIPTION_DEADLINE_S)
        assert isinstance(result, TranscriptionResult)  # noqa: S101 - variant invariant of run()
        return result

    #: Extension per audio mimetype, consulted BEFORE ``mimetypes``. Not
    #: redundancy — the stdlib table misses exactly the two types this surface
    #: sees most: ``mimetypes.guess_extension`` answers ``None`` for BOTH
    #: ``audio/wav`` and ``audio/webm`` (only the legacy ``audio/x-wav`` and the
    #: ``video/webm`` spelling resolve), and maps ``audio/ogg`` to ``.oga``. So a
    #: browser recording — ``audio/webm`` from ``MediaRecorder``, the cheapest
    #: and now live-verified upload shape — would fall through to ``audio.wav``
    #: and tell the gateway the wrong container. Worse, the stdlib table is
    #: seeded from the host's ``/etc/mime.types``, so the answer would differ
    #: between a developer's box and the api image with nothing to notice.
    AUDIO_EXTENSIONS: ClassVar[dict[str, str]] = {
        "audio/wav": ".wav",
        "audio/wave": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "video/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "audio/aac": ".aac",
    }

    @classmethod
    def format_hint(cls, filename: str, mimetype: str) -> str:
        """Derive the filename whose EXTENSION is the gateway's format hint.

        The extension is read in preference to the part's declared
        ``Content-Type`` because a browser's ``MediaRecorder`` labels its blob
        with a full codec string (``audio/webm;codecs=opus``) that no extension
        table maps, while the filename the same client attaches is already the
        shape the multipart upload wants. The mimetype is the fallback, and
        ``audio.wav`` the last resort — chosen over refusing because the hint is
        advisory: the gateway forwards the upload intact and applies no format
        gate of its own.

        Cost class: ``O(1)``.
        """
        name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if name and "." in name.lstrip(".") and not name.endswith("."):
            return name
        base = (mimetype or "").split(";", 1)[0].strip().lower()
        guessed = cls.AUDIO_EXTENSIONS.get(base) or mimetypes.guess_extension(base)
        return f"audio{guessed}" if guessed else "audio.wav"

    def ensure_within_size_limit(self, nbytes: int | None) -> None:
        """Refuse an upload over :attr:`MAX_AUDIO_BYTES`; ``None`` is unknown.

        Called TWICE by the route — once on the declared ``Content-Length``,
        which fails fast without buffering a byte, and once on the real count,
        which is the only one that cannot be lied about. One implementation of
        the rule, so the two checks cannot disagree about the limit.

        Cost class: ``O(1)``.
        """
        if nbytes is not None and nbytes > self.MAX_AUDIO_BYTES:
            raise AudioTooLarge.for_limit(self.MAX_AUDIO_BYTES, actual_bytes=nbytes)

    # ── shared ──────────────────────────────────────────────────────────────

    def _require_direction(self, mode: SpeechMode, model_configured: bool) -> SpeechGateway:
        """Resolve the gateway, refusing when this deployment cannot serve *mode*.

        Returns the ONE gateway instance the rest of the request uses. Resolving
        it here rather than again at the call is what makes a request's refusal
        and its call read the same configuration — and it is the only reason the
        "a reader must return a fresh instance" rule can stay confined to the
        catalogue leg, which is the only other place one is built.
        """
        gateway = self._require_gateway()
        if not model_configured:
            direction = "speech.tts.model" if mode is SpeechMode.SYNTHESIS else "speech.stt.model"
            raise CapabilityUnavailable.for_capability(
                "speech_unavailable",
                f"No model is configured for this direction. Set {direction}.",
            )
        return gateway

    @staticmethod
    def _invalid(exc: ValidationError) -> ApiError:
        """Map a Pydantic failure to the namespace's 400, keeping the field name.

        Envelope shape with a semantic ``code``, unlike the ``/api`` routes'
        default ``{"message": ...}`` — this namespace has ONE wire shape and the
        400s are not an exception to it, so a client's error handling is a single
        branch on ``code`` rather than two body parsers.
        """
        return RequestInvalid.from_validation_error(exc, code="invalid_request", shape="envelope")

    def _run(
        self, gateway: SpeechGateway, speech_request: SpeechRequest, deadline_s: float
    ) -> SpeechResult:
        """Run one gateway call under *deadline_s*, mapping every failure to 502.

        ``asyncio.run`` per call rather than a shared loop: this is a one-shot
        awaited call from a synchronous WSGI thread, the same bridge the session
        title and structured-synthesis paths already use. The deadline bounds OUR
        wait, not the upstream work — a cancelled ``wait_for`` does not un-send
        the request, so a timed-out synthesis may still complete at the gateway.
        That is precisely why the timeout code is distinct from the refusal code:
        the two invite different follow-ups.

        Cost class: ``O(input length)``, hard-bounded by *deadline_s*.
        """
        try:
            return asyncio.run(
                asyncio.wait_for(gateway.run(speech_request), deadline_s)  # noqa: ASYNC109
            )
        except asyncio.TimeoutError as exc:
            logging.warning("speech call exceeded the {:g}s deadline", deadline_s)
            raise SpeechGatewayFailure.timed_out(deadline_s) from exc
        except SpeechUnavailableError as exc:
            raise CapabilityUnavailable.for_capability("speech_unavailable", str(exc)) from exc
        except SpeechGatewayError as exc:
            logging.warning("speech gateway call failed: {}", exc)
            raise SpeechGatewayFailure.refused(str(exc)) from exc


# ---------------------------------------------------------------------------
# HTTP adapters — thin Resources over the one injected controller
# ---------------------------------------------------------------------------


class _ControllerResource(Resource):
    """Base Resource that receives the one controller via ``resource_class_kwargs``.

    Flask-RESTX passes the ``Api`` as the first positional arg to a Resource
    constructor; ``controller`` rides alongside it as an injected keyword so no
    Resource ever reaches into module scope for its collaborators.
    """

    def __init__(
        self, api: Any = None, *args: Any, controller: SpeechRoutesController, **kwargs: Any
    ) -> None:
        super().__init__(api, *args, **kwargs)
        self.controller = controller

    def _refuse_capacity(self) -> Response:
        """The 503 a caller past the in-flight bound receives.

        Built as a ``Response`` rather than raised because it is the one refusal
        in this namespace that carries a header: ``Retry-After`` is how a client
        learns a delay it has no way to guess, and the shared ``ApiError``
        handler renders a body and a status only. Same shape the SSE stream
        limiter uses, deliberately.
        """
        refusal = SpeechCapacityExhausted.for_limit(self.controller.capacity.limit())
        body, status = refusal.response()
        return Response(
            json.dumps(body),
            status=status,
            mimetype="application/json",
            headers={"Retry-After": str(refusal.RETRY_AFTER_SECONDS)},
        )


class SpeechCapabilities(_ControllerResource):
    """What this deployment can do with speech, and within which bounds."""

    @speech_ns.doc(security="apikey")
    @speech_ns.response(200, "Speech capabilities, defaults and limits.", capabilities_model)
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def get(self) -> tuple[dict, int]:
        """Report speech capabilities.

        Returns whether synthesis and transcription are available, the models
        the gateway advertises for each, the accepted voices and containers, the
        server-side defaults an omitted field resolves to, and every limit a
        client must respect. Poll it to decide whether to render a speaker or a
        microphone.

        `available` is derived from configuration alone — no health call is made,
        so a direction can read available while its upstream credential is dead.
        That failure surfaces as a 502 on the call itself.

        Cost class: `O(1)`. No network on the warm path; the first call per
        process adds one 3s-bounded model listing, and a failed listing is
        remembered for 60s. This endpoint does not 500 and does not spend a
        concurrency slot.
        """
        return self.controller.capabilities(), 200


class SpeechSynthesize(_ControllerResource):
    """Turn text into audio bytes."""

    @speech_ns.doc(security="apikey")
    @speech_ns.expect(synthesize_request_model)
    @speech_ns.produces(["audio/wav", "audio/flac"])
    @speech_ns.response(200, "Raw audio bytes; `Content-Type` names the real container.")
    @kit.errors(
        400,
        502,
        503,
        descriptions={
            400: "Unknown voice or format, blank or over-long text, or an unknown body key.",
            502: "The gateway refused the call, or our deadline elapsed.",
            503: "Speech is unconfigured, or every in-flight slot is taken.",
        },
    )
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self) -> Response:
        """Synthesize speech.

        Accepts `{text, model?, voice?, response_format?, verbalize?}` and
        returns the RAW audio bytes — not base64, not an envelope. `Content-Type`
        is derived from the payload's own magic bytes rather than from what the
        gateway declared, because this gateway labels every successful synthesis
        `audio/mpeg` and has never once returned MPEG.

        **`text` is read as markdown by default.** Send an assistant's reply
        unmodified: headings and list items become their own spoken units, a
        code block is announced once rather than read out symbol by symbol, a
        link becomes its text, and a table's header is announced once before its
        rows so no row is ever dropped. `verbalize: false` speaks the string
        exactly as written.

        Omitted fields resolve to the server-configured defaults reported by
        `/api/speech/capabilities`. A voice or format outside the accepted sets
        is refused here, before any gateway call, because the gateway answers
        every parameter mistake with an identical error that names no field.

        Cost class: `O(text length)` — about half a second for a sentence and
        four seconds for a paragraph, bounded by a 60s deadline. Concurrency: at
        most 4 speech calls may be in flight across this route and `/transcribe`
        combined; the 5th caller gets `503 speech_capacity_exhausted` with
        `Retry-After`, rather than queueing behind the request thread pool.
        """
        if not self.controller.capacity.try_acquire():
            return self._refuse_capacity()
        try:
            result = self.controller.synthesize(request.get_json(silent=True) or {})
        finally:
            self.controller.capacity.release()
        return Response(result.audio, status=200, mimetype=result.content_type)


class SpeechTranscribe(_ControllerResource):
    """Turn recorded audio into text."""

    @speech_ns.doc(
        security="apikey",
        params={
            "file": {
                "description": "The recording, as a `multipart/form-data` file part.",
                "in": "formData",
                "type": "file",
                "required": True,
            },
            "model": {
                "description": "Optional model id; defaults to `speech.stt.model`.",
                "in": "formData",
                "type": "string",
            },
            "language": {
                "description": "Optional BCP-47 language hint.",
                "in": "formData",
                "type": "string",
            },
        },
    )
    @speech_ns.response(200, "The transcript.", transcribe_response_model)
    @kit.errors(
        400,
        413,
        502,
        503,
        descriptions={
            400: "No `file` part, or an unusable model id.",
            413: "The upload exceeds `transcription.limits.max_audio_bytes`.",
            502: "The gateway refused the call, or our deadline elapsed.",
            503: "Speech is unconfigured, or every in-flight slot is taken.",
        },
    )
    @kit.auth_error()
    @guard.requires("sessions.interact")
    def post(self) -> tuple[dict, int] | Response:
        """Transcribe audio.

        Accepts `multipart/form-data` with the recording under the `file` part,
        plus optional `model` and `language` text parts. The FILENAME's extension
        is what the gateway is given as a format hint, falling back to the part's
        mimetype and then to `audio.wav`.

        Cost class: `O(audio length)`, bounded by a 30s deadline and by a 10 MiB
        upload cap checked against `Content-Length` before anything is buffered.
        Concurrency: shares the 4-in-flight bound with `/synthesize`; the 5th
        caller gets `503 speech_capacity_exhausted` with `Retry-After`.
        """
        # FIRST, and the ordering is the whole point: `content_length` reads a
        # header, while touching `request.files` makes werkzeug parse and spool
        # the entire body. Checking the declared length here is what lets a
        # declared-oversize upload be refused without ever being spooled, and it
        # costs no slot — a caller that cannot be served should spend neither.
        self.controller.ensure_within_size_limit(request.content_length)
        upload = request.files.get("file")
        if upload is None:
            raise RequestInvalid.field_error(
                "file",
                "file: a `multipart/form-data` part named `file` carrying the "
                "recording is required.",
                code="invalid_request",
                shape="envelope",
            )
        # One byte past the cap, never the whole part. An upload with no declared
        # length — a chunked transfer — reaches here having been spooled by
        # werkzeug (to disk past its own threshold), so the bound this read adds
        # is on the Python `bytes` object: the extra byte is what lets the count
        # below refuse an oversize body without first materialising all of it.
        audio = upload.read(self.controller.MAX_AUDIO_BYTES + 1)
        if not self.controller.capacity.try_acquire():
            return self._refuse_capacity()
        try:
            result = self.controller.transcribe(
                audio=audio,
                filename=upload.filename or "",
                mimetype=upload.mimetype or "",
                model=request.form.get("model"),
                language=request.form.get("language"),
            )
        finally:
            self.controller.capacity.release()
        return {"text": result.text, "model": result.model}, 200


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------

# Single composition-root handle, set once at boot. Production never reads it —
# Flask bakes the controller into the view closure at registration — so it exists
# only so a test can point the ONE registered controller at a scripted gateway by
# reassigning its fields. Mirrors ``triggers/routes.py``.
_controller: SpeechRoutesController | None = None


def init_speech_routes(api: Any, *, controller: SpeechRoutesController | None = None) -> None:
    """Build the controller, DI it into the Resources, and register (once, at boot).

    Called from :func:`mewbo_api.speech.init_speech` only after ``mewbo_speech``
    has been confirmed importable, so nothing here is guarded: an ImportError
    raised from this module is a real defect and must not be reported as "the
    extra is not installed".

    *controller* is a test seam. The default reads the gateway per call through
    ``SpeechGateway.from_config`` — which resolves ``speech.api_base``/``api_key``
    and falls back to ``llm.*`` — so an operator re-pointing the gateway through
    ``PATCH /api/config`` takes effect without a restart.

    **A SINGLE ``asyncio.Semaphore`` is built once, here, and threaded into every
    ``from_config()`` call.** ``SpeechGateway`` is per-call by design (the
    docstring above says why), and its own ``concurrency`` bound is a FIELD on
    that per-call instance — passed a fresh default, a semaphore sized four
    would reset to "four free" on every request and never coordinate across
    them, silently defeating the bound the package exists to provide. This is
    the one construction site production actually uses, so it is also the one
    place that inertness would go unnoticed by ``tests/speech/``, which builds
    its own gateways directly.

    Cost class: ``O(1)`` — object construction and four route registrations. No
    network, so a gateway that is down does not slow or fail boot.
    """
    global _controller  # noqa: PLW0603 - single composition-root handle, set once
    speech_concurrency = asyncio.Semaphore(SpeechRoutesController.MAX_CONCURRENT_CALLS)
    _controller = controller or SpeechRoutesController(
        gateway_reader=lambda: SpeechGateway.from_config(concurrency=speech_concurrency),
        config_reader=lambda: get_config().speech,
        capacity=StreamCapacity(lambda: SpeechRoutesController.MAX_CONCURRENT_CALLS),
    )
    injected = {"resource_class_kwargs": {"controller": _controller}}
    speech_ns.add_resource(SpeechCapabilities, "/speech/capabilities", **injected)
    speech_ns.add_resource(SpeechSynthesize, "/speech/synthesize", **injected)
    speech_ns.add_resource(SpeechTranscribe, "/speech/transcribe", **injected)
    api.add_namespace(speech_ns, path="/api")


__all__ = [
    "AudioTooLarge",
    "SpeechCapacityExhausted",
    "SpeechGatewayFailure",
    "SpeechRoutesController",
    "SynthesizeBody",
    "VerbalizedTextTooLong",
    "init_speech_routes",
    "speech_ns",
]
