#!/usr/bin/env python3
"""Audio container identification — magic bytes over a declared header.

The deployed gateway answers every successful synthesis with
``Content-Type: audio/mpeg``, and the payload behind that header was never once
MPEG: it is RIFF/WAVE by default and ``fLaC`` when FLAC is requested (measured on
every successful response the TTS route produced, by both ``file`` and a raw hex
dump of the leading bytes). A caller that labels bytes from the declared header
therefore mislabels all of them — a browser handed ``audio/mpeg`` over WAV bytes
either refuses to play or guesses.

So the container is derived from the payload's own leading bytes. The declared
header is still carried alongside (``SynthesisResult.declared_content_type``) so
a mismatch stays visible rather than being silently corrected away.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class AudioContainer(str, Enum):
    """A container format, identified from a payload's leading bytes."""

    WAV = "wav"
    FLAC = "flac"
    MP3 = "mp3"
    OGG = "ogg"
    MP4 = "mp4"
    UNKNOWN = "unknown"

    @classmethod
    def sniff(cls, data: bytes | bytearray | memoryview) -> AudioContainer:
        """Identify *data*'s container from its magic bytes.

        Accepts any buffer, not just ``bytes``: audio arrives from an SDK
        response, a multipart upload and a test fixture, and narrowing to
        ``bytes`` only pushes a copy or a cast onto every one of those call
        sites for no gain — the prefix read below already normalises.

        Never raises and never guesses past the signatures below: an
        unrecognised payload is :attr:`UNKNOWN`, which a caller can surface as
        "the gateway returned something we cannot label" instead of mislabelling
        it. Order is loosest-last — the MPEG frame sync is two bytes wide and
        would otherwise claim payloads that carry a longer, exact signature.

        Cost class: ``O(1)`` — reads a fixed 12-byte prefix regardless of how
        many megabytes follow it.
        """
        head = bytes(data[:12])
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return cls.WAV
        if head[:4] == b"fLaC":
            return cls.FLAC
        if head[:4] == b"OggS":
            return cls.OGG
        if head[4:8] == b"ftyp":
            return cls.MP4
        if head[:3] == b"ID3":
            return cls.MP3
        # MPEG frame sync: eleven set bits spanning the first two bytes. Checked
        # last because two bytes match far more readily than a four-byte tag.
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return cls.MP3
        return cls.UNKNOWN

    @property
    def content_type(self) -> str:
        """The MIME type to label this container with on the way out."""
        return _CONTENT_TYPES[self]

    @property
    def extension(self) -> str:
        """The filename extension for this container, without the dot."""
        return "bin" if self is AudioContainer.UNKNOWN else self.value


# Kept at module scope rather than in the class body: a mapping declared inside
# an Enum becomes a member of it.
_CONTENT_TYPES: Final[dict[AudioContainer, str]] = {
    AudioContainer.WAV: "audio/wav",
    AudioContainer.FLAC: "audio/flac",
    AudioContainer.MP3: "audio/mpeg",
    AudioContainer.OGG: "audio/ogg",
    AudioContainer.MP4: "audio/mp4",
    AudioContainer.UNKNOWN: "application/octet-stream",
}
