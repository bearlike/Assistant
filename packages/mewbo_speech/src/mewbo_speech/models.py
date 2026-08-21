#!/usr/bin/env python3
"""The speech-model descriptor and the closed vocabularies around it.

The gateway's ``/model/info`` route is the ONE place a model's capability is
stated: every entry carries ``model_info.mode``, and that field is what separates
a TTS route from an STT route from a chat route. Core fetches the same document
to hydrate LiteLLM's cost map and discards ``mode`` on the way through
(``llm.py:register_proxy_model_capabilities`` defaults it to ``"chat"`` and never
returns it), so nothing downstream of core can tell the three apart. This module
is where that field stops being discarded.

Classification lives ON :class:`SpeechModel`, not in the gateway client: a new
mode is a new branch of :meth:`SpeechModel.from_model_info`, never a widening
``if`` in whichever caller happened to be listing models that day.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewbo_speech.audio import AudioContainer


class SpeechMode(str, Enum):
    """A speech capability, spelled exactly as the gateway reports it."""

    SYNTHESIS = "audio_speech"
    TRANSCRIPTION = "audio_transcription"


# The OpenAI-canonical voices, offered as suggestions and NOT as a closed set.
#
# An earlier version validated against this tuple and refused everything else,
# on the evidence that every other value TRIED returned an opaque 500. That
# reasoning does not survive contact with a self-hosted backend: the values
# tried were guesses at generic names, and an operator-defined voice style is by
# definition not guessable. A deployment carrying its own trained voice had it
# rejected by this library with a message asserting the accepted set — the
# validator was more confident than the measurement behind it.
#
# So the gateway decides what a voice is, because it is the only thing that
# knows. An unknown name reaches the backend and fails there, which is a worse
# error message than a local one and the correct trade: a wrong rejection costs
# a capability that cannot be recovered by retrying, while a wrong acceptance
# costs one bad request.
SUGGESTED_VOICES: Final[tuple[str, ...]] = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)

# The containers the TTS backend will actually produce. `wav` and the omitted
# default are the same response byte for byte; `flac` returns real FLAC. Every
# other OpenAI-canonical value — `mp3`, `opus`, `aac`, `pcm` — returns the same
# opaque 500 as a bad voice, so they are refused at this boundary too.
SYNTHESIS_FORMATS: Final[tuple[AudioContainer, ...]] = (
    AudioContainer.WAV,
    AudioContainer.FLAC,
)

# The default TTS route (owner decision). The gateway advertises a second,
# `supertonic-3-hd`, which measured ~2x slower for BYTE-IDENTICAL output at the
# same voice and format — same 16-bit mono 44.1 kHz, same file size, twice the
# wait. "HD" buys nothing here, so the plain route is the default and the -hd one
# stays selectable rather than recommended.
DEFAULT_TTS_MODEL: Final[str] = "supertonic-3"

# The only transcription route the gateway advertises. Verified end to end
# against the live gateway for wav, webm/opus and mp3 — see TranscriptionRequest.
DEFAULT_STT_MODEL: Final[str] = "nova-3"


class SpeechModel(BaseModel):
    """A speech-capable model the gateway advertises.

    ``id`` is the BARE gateway id (``supertonic-3``, ``nova-3``) — the string the
    proxy's key ACL recognises and the string that goes on the wire. It is
    deliberately not the ``model_info.key`` field, which reads
    ``openai/supertonic-3`` / ``deepgram/nova-3`` and describes which upstream
    the proxy's own route consumes; sending either of those forms on the wire is
    a 403 ``key_model_access_denied``. See
    :meth:`mewbo_speech.operations.SpeechOperation.routed_model` for the one
    place a provider prefix is applied, and why.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    id: str = Field(min_length=1, description="Bare gateway model id.")
    mode: SpeechMode = Field(description="Which speech capability this model serves.")
    display_name: str = Field(default="", description="Human label; defaults to the id.")

    @model_validator(mode="after")
    def _default_display_name(self) -> SpeechModel:
        """Fall back to the id so a picker never renders an empty row."""
        if not self.display_name.strip():
            self.display_name = self.id
        return self

    @classmethod
    def from_model_info(cls, entry: Mapping[str, Any]) -> SpeechModel | None:
        """Classify one ``/model/info`` entry, or ``None`` if it is not speech.

        Returning ``None`` rather than raising is the point: the same document
        carries chat and embedding routes, and a listing that raised on the first
        chat model would surface zero speech models on a healthy gateway.

        Cost class: ``O(1)`` — reads two keys of one entry.
        """
        name = entry.get("model_name")
        info = entry.get("model_info")
        if not isinstance(name, str) or not name.strip() or not isinstance(info, Mapping):
            return None
        try:
            mode = SpeechMode(info.get("mode"))
        except ValueError:
            return None
        return cls(id=name.strip(), mode=mode)
