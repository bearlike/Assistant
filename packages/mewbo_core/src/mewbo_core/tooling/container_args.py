#!/usr/bin/env python3
"""Decode a JSON STRING standing in for a declared list/dict tool argument.

Models routinely serialise a structured argument as a JSON string instead of
the array the schema declares, and Pydantic rejects it (``type=list_type``).
The tool call then fails and the model spends a whole round trip re-sending
the same content in a different shape. It is not one provider's quirk: the
same failure is on record from five different model families across wiki,
SCG and generative-UI tools, and its rate scales with payload size.

:class:`JsonContainerArguments` is the ONE home for the repair — every
SessionTool that validates a Pydantic args model with container-valued fields
routes its raw input through :meth:`decode` before ``model_validate``. It is
deliberately narrow, so a real mistake still surfaces as itself: a value is
rewritten only when the field DECLARES a list or dict, the supplied value is
a string, and that string parses to the declared container.

Two recoveries beyond a clean parse, both measured against captured payloads:

- **Single-key wrapper unwrap.** A model that also wrapped the array in a
  one-key object naming this very field (``'{"pages": [...]}'`` for a
  ``pages`` field) is the same content one layer down.
- **Valid-prefix salvage.** A long escaped payload frequently arrives with
  trailing garbage after a syntactically complete container — the model lost
  escape-depth tracking mid-emission. ``json.JSONDecoder().raw_decode``
  recovers the valid prefix; the dropped tail is REPORTED via a note the
  caller must surface in its result, so a partial render is never silent.
"""

from __future__ import annotations

import json
import types as _pytypes
import typing
from typing import Any, NamedTuple

from pydantic import BaseModel


class DecodedArguments(NamedTuple):
    """The (possibly rewritten) raw arguments plus what any salvage dropped.

    ``notes`` is non-empty ONLY when a valid-prefix salvage discarded trailing
    characters; a caller returns them with its result (success or rejection)
    so the model learns part of its payload was dropped.
    """

    arguments: dict[str, Any]
    notes: tuple[str, ...]


class JsonContainerArguments:
    """The one decoder for JSON strings standing in for declared containers.

    Stateless; every method is a classmethod so call sites read as
    ``JsonContainerArguments.decode(ArgsModel, raw)``. Kept as a class rather
    than loose functions so the salvage rule, the wrapper rule and the
    container check cannot drift apart across call sites.
    """

    @classmethod
    def container_origins(cls, annotation: Any) -> tuple[type, ...]:
        """Return the ``list``/``dict`` origins *annotation* accepts, if any.

        Unwraps unions so an optional field (``list[str] | None``) reports the
        same container its non-optional twin does.
        """
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, _pytypes.UnionType):
            found: list[type] = []
            for member in typing.get_args(annotation):
                found.extend(cls.container_origins(member))
            return tuple(dict.fromkeys(found))
        if origin in (list, dict):
            return (origin,)
        if annotation in (list, dict):
            return (annotation,)
        return ()

    @classmethod
    def decode_value(
        cls,
        value: Any,
        wanted: tuple[type, ...],
        *,
        field_name: str,
    ) -> tuple[Any, str | None]:
        """Decode ONE string value against its declared containers.

        Returns ``(parsed, note)`` when the string yields the declared
        container (``note`` set only when a valid prefix was salvaged), or
        ``(None, None)`` when the value is not recoverable — the caller then
        leaves the original in place so validation reports the real mistake.
        """
        if not isinstance(value, str) or not wanted:
            return None, None
        note: str | None = None
        try:
            parsed: Any = json.loads(value)
        except (ValueError, TypeError):
            parsed, note = cls._salvage_prefix(value, wanted, field_name=field_name)
            if parsed is None:
                return None, None
        # A single-key wrapper naming this very field is the same payload one
        # layer down; anything else keyed differently is not ours to
        # reinterpret.
        if isinstance(parsed, dict) and not isinstance(parsed, wanted):
            inner = parsed.get(field_name)
            if isinstance(inner, wanted):
                parsed = inner
        if not isinstance(parsed, wanted):
            return None, None
        return parsed, note

    @classmethod
    def decode(
        cls, args_cls: type[BaseModel], raw: dict[str, Any]
    ) -> DecodedArguments:
        """Rewrite JSON-string values for *args_cls*'s container-declared fields.

        Non-container fields, non-string values and strings that do not parse
        to the declared container are left byte-identical, so the subsequent
        ``model_validate`` reports the genuine mistake rather than a laundered
        one. Decoding feeds Pydantic; it never bypasses the model's own rules.
        """
        decoded: dict[str, Any] | None = None
        notes: list[str] = []
        for name, field in args_cls.model_fields.items():
            key = name if name in raw else (field.alias if field.alias in raw else None)
            if key is None:
                continue
            wanted = cls.container_origins(field.annotation)
            parsed, note = cls.decode_value(raw[key], wanted, field_name=name)
            if parsed is None:
                continue
            if decoded is None:
                decoded = dict(raw)
            decoded[key] = parsed
            if note:
                notes.append(note)
        return DecodedArguments(
            arguments=decoded if decoded is not None else raw,
            notes=tuple(notes),
        )

    @staticmethod
    def _salvage_prefix(
        value: str, wanted: tuple[type, ...], *, field_name: str
    ) -> tuple[Any, str | None]:
        """Recover a syntactically complete container prefix, or ``(None, None)``.

        ``raw_decode`` stops at the first complete JSON value, so a payload
        whose tail collapsed into garbage still yields everything emitted
        before the collapse. The note states how much was dropped — a partial
        recovery the caller does not report would render as a silently
        truncated result, which is the one failure a data surface must not
        have.
        """
        stripped = value.lstrip()
        try:
            parsed, end = json.JSONDecoder().raw_decode(stripped)
        except ValueError:
            return None, None
        if not isinstance(parsed, wanted):
            return None, None
        dropped = len(stripped) - end
        if dropped <= 0:
            # A full parse would have succeeded; nothing was salvaged.
            return None, None
        recovered = f"{len(parsed)} item(s)" if isinstance(parsed, list) else "an object"
        note = (
            f"{field_name}: the value arrived as a JSON string whose tail did not "
            f"parse; kept the valid prefix ({recovered}) and dropped "
            f"{dropped} trailing character(s)."
        )
        return parsed, note


__all__ = ["DecodedArguments", "JsonContainerArguments"]
