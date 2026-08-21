#!/usr/bin/env python3
"""Multimodal tool results — the image channel a tool result can carry.

Two concerns, two classes, both pure logic so they are testable without a
model, a device or a clock:

:class:`ToolResultContent` splits what a tool returned into the text the
existing cap/truncation machinery already handles and the image parts that
must NOT go through it. :class:`ImageHistoryStrip` takes images back out of a
message list at compaction time.

**Why the image bypasses truncation rather than teaching truncation about
images.** ``_windowed``, the ANSI strip and the char cap all reason in
characters. A base64 data URI has no meaningful character count, windowing it
produces a corrupt image, and letting it reach the ``tool_result`` event
payload would persist every screenshot into the session store and replay it on
every transcript read. Splitting the parts HERE keeps all of that machinery
string-only and unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# What a stripped image leaves behind. It is a REFERENCE, not an epitaph: the
# model is told the image is gone and that asking again is how to get one. For
# a device screenshot that is the only honest recovery anyway — the screen has
# moved on, so a cached copy of the old one would be a stale answer to a
# question about the current screen.
IMAGE_STRIPPED_PLACEHOLDER = (
    "[Image omitted from history to save context. Request it again if you still need it.]"
)


def _is_image_part(part: object) -> bool:
    """True for a LiteLLM/OpenAI ``image_url`` content part."""
    return isinstance(part, dict) and part.get("type") == "image_url"


@dataclass(frozen=True)
class ToolResultContent:
    """One tool result split into its model-facing text and its image parts.

    ``text`` stays a plain ``str`` so every existing cap, ANSI strip and event
    snapshot keeps working on it untouched. ``images`` are LiteLLM-style
    ``{"type": "image_url", ...}`` parts that ride to the model INSIDE the tool
    result and never reach the event payload.
    """

    text: str
    images: tuple[dict[str, Any], ...] = ()

    @property
    def has_images(self) -> bool:
        """True when this result carries at least one image part."""
        return bool(self.images)

    @classmethod
    def parse(cls, content: object) -> ToolResultContent:
        """Split *content* into text + image parts.

        A list of content parts is the multimodal form; anything else is
        ordinary and comes back with no images, so every non-multimodal tool
        takes exactly the path it took before. Text parts are joined so a tool
        may narrate alongside its image.
        """
        if not isinstance(content, list):
            return cls(text="" if content is None else str(content))
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        for part in content:
            if _is_image_part(part):
                images.append(dict(part))
            elif isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                texts.append(part)
            else:
                texts.append(str(part))
        return cls(text="\n".join(t for t in texts if t), images=tuple(images))

    def for_model(self, text: str) -> str | list[str | dict]:
        """The message content: *text* alone, or text followed by the images.

        *text* is passed in rather than read off ``self`` because by this point
        the caller has already capped, ANSI-stripped and possibly export-
        replaced it — the image parts are what this class still owns.

        Returns a plain ``str`` when there is no image, so a cache prefix built
        from ordinary results stays byte-identical to what it was before this
        seam existed.
        """
        if not self.images:
            return text
        parts: list[str | dict] = [{"type": "text", "text": text}]
        parts.extend(self.images)
        return parts


class ImageHistoryStrip:
    """Takes images out of a retained message list, at compaction time only.

    **Compaction is the right and only moment.** Every alternative pays a cost
    this one does not:

    - Stripping on a TURN INTERVAL changes which images are present while the
      conversation is otherwise stable, so it invalidates the prompt cache on
      a turn that would otherwise have hit it — and cached reads cost ~10% of
      full price, so the re-sent prefix routinely costs more than the images
      saved. Compaction has ALREADY rewritten the message list and voided that
      prefix, so stripping here costs zero additional invalidations.
    - Stripping EAGERLY (keep only the newest N, every turn) is the same defect
      with a shorter period.

    The newest image survives, because a run driving a phone that loses sight
    of the current screen mid-task has to spend a turn re-observing it. Older
    ones become :data:`IMAGE_STRIPPED_PLACEHOLDER`, which tells the model the
    image is re-requestable rather than merely absent.

    Converged behaviour, not invention: ``opencode`` compacts with
    ``stripMedia: true`` and leaves ``[Attached image/png: cat.png]``,
    ``kilocode``'s ``stripHistoricalMedia`` strips everything older than the
    most recent message, and ``codex`` substitutes placeholder text. All three
    replace rather than delete, so the turn structure survives.
    """

    def strip(self, messages: list[Any]) -> int:
        """Replace all but the newest image with a placeholder; return the count.

        Mutates each affected message's ``content`` in place. A message whose
        content is a plain string carries no image and is never touched.
        """
        positions: list[tuple[Any, int]] = []
        for message in messages:
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for index, part in enumerate(content):
                if _is_image_part(part):
                    positions.append((message, index))

        # Everything except the newest.
        stale = positions[:-1]
        for message, index in stale:
            content = list(message.content)
            content[index] = {"type": "text", "text": IMAGE_STRIPPED_PLACEHOLDER}
            message.content = content
        return len(stale)


__all__ = [
    "IMAGE_STRIPPED_PLACEHOLDER",
    "ImageHistoryStrip",
    "ToolResultContent",
]
