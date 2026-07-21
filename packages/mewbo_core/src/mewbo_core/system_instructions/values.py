#!/usr/bin/env python3
"""Candidate VALUES for the instruction-template variables.

``spec.py`` declares WHAT an operator's template may reference; this module
answers WHAT THOSE VARIABLES CAN CONTAIN. It exists because a variable reference
that shows a bare type word ("string", "array") tells an operator nothing they
can branch on — they have to guess whether the surface is ``android`` or
``aura-android``, whether the capability is ``wiki`` or ``wiki_search``, and a
wrong guess fails SILENTLY (``ChainableUndefined`` renders a bad comparison as
blank rather than raising).

Two kinds of answer, and conflating them is the trap this module is shaped to
avoid:

* **CLOSED** (:attr:`ValuesKind.CLOSED`) — the value is ALWAYS one of these.
  Only a genuinely enum-typed field earns this: ``origin`` is a real
  ``SessionOrigin``, so its members come from the schema's ``$defs`` and an
  operator can rely on an ``else`` branch being unreachable.
* **KNOWN** (:attr:`ValuesKind.KNOWN`) — what this DEPLOYMENT currently has
  installed or observed. It is a SUPERSET CLAIM ABOUT THE DEPLOYMENT, never an
  exhaustive claim about a SESSION: a session's ``tools`` are narrower than the
  catalog's (scoped by project/plugins/allowlist), its ``capabilities`` are only
  the ones its client advertised, and a brand-new client can introduce a
  ``surface`` nobody has seen. So an operator must TEST for a value
  (``'wiki' in capabilities``) rather than assume the list is what they get.

:class:`InstructionValueCatalog` is PLAIN DATA with ZERO I/O — it never reads a
registry, a config file, or a proxy. The app resolves this deployment's facts at
its edge and INJECTS them, the same rule that keeps ``TriggerSpec`` taking the
clock as an argument (``mewbo_core/triggers/spec.py``): a model that reaches out
for its own inputs is untestable and drags I/O into the wrong layer. Its
defaults are the two vocabularies core genuinely owns (surfaces, platforms);
everything else defaults EMPTY, which renders as no candidates at all rather
than as a misleading empty list.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from mewbo_core.session_provenance import KNOWN_SURFACES

# The operating systems Mewbo reports. ``InstructionContext.platform`` is
# ``platform.system().lower()`` (``orchestrator.py``), so these are the
# lowercased CPython ``platform.system()`` values for the hosts Mewbo runs on.
# Its one home: nothing else in the codebase names them.
KNOWN_PLATFORMS: tuple[str, ...] = ("darwin", "linux", "windows")


class ValuesKind(str, Enum):
    """Whether a candidate list is exhaustive, or merely what exists here.

    ``(str, Enum)`` rather than ``enum.StrEnum``: ``StrEnum`` is 3.11+, this
    package's floor is ``>=3.10``, and the sibling ``SessionOrigin`` already
    establishes this idiom. Identical wire value either way.
    """

    CLOSED = "closed"
    """A session's value is ALWAYS one of these (a real enum, e.g. ``origin``)."""

    KNOWN = "known"
    """Installed/observed on this deployment. NOT exhaustive — test, don't assume."""


class CandidateValues(BaseModel):
    """The values a variable can take, plus how much an operator may trust the list.

    ``kind`` is what stops the console from rendering a KNOWN list as though it
    were CLOSED — the difference between "branch on this safely" and "this is
    what happens to be installed right now".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: tuple[str, ...]
    kind: ValuesKind
    note: str


class InstructionVariable(BaseModel):
    """One row of the variable reference, derived from ``InstructionContext``'s schema.

    Built by :meth:`InstructionContext.describe`, never hand-authored — the table
    an operator reads is generated from the very model the renderer hands to
    Jinja, so the two cannot drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str
    description: str
    candidates: CandidateValues | None = None


class InstructionValueCatalog(BaseModel):
    """This deployment's facts as PLAIN DATA — the app injects them, core never reads them.

    ZERO I/O by construction (see the module docstring). Each field is one
    SOURCE that an ``InstructionContext`` field points at via its ``x-values``
    schema key; :meth:`candidates_for` is the single lookup that turns a source
    into the operator-facing :class:`CandidateValues`.

    An empty source yields ``None``, NOT an empty list: an operator must never be
    shown "Known here: (nothing)", which reads as "this variable is always empty"
    when it actually means "this deployment did not tell us".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = KNOWN_SURFACES
    platforms: tuple[str, ...] = KNOWN_PLATFORMS

    # Operator-facing prose, one line per SOURCE. It lives on the catalog rather
    # than on the ``InstructionContext`` field because it is a fact about where
    # the values COME FROM (and how far to trust them), not about what the field
    # means — the field's own ``description`` already says that. Membership here
    # is also the source vocabulary itself: an ``x-values`` key absent from this
    # map resolves to no candidates rather than reaching for a stray attribute.
    #
    # Copy standard: no em dashes, full sentences. These render in the console's
    # Settings surface.
    _NOTES: ClassVar[Mapping[str, str]] = {
        "tools": (
            "Every tool this deployment can bind, across the global configuration and each "
            "project you have configured. A session's actual list is narrower, because it is "
            "scoped by the project it runs in, the plugins that loaded, and any tool allowlist "
            "the caller passed. One gap to know about: a tool reachable only through a project "
            "Mewbo manages for you, rather than one you configured, may be missing from this "
            "list even though a session there would hold it."
        ),
        "capabilities": (
            "Declared by the plugins and agent definitions installed here. A session only "
            "carries the ones its client advertised, so test for one rather than assuming it."
        ),
        "projects": (
            "Projects registered here, both the ones configured in app.json and the ones "
            "Mewbo manages. A session bound to a managed worktree reports no project at all."
        ),
        "models": (
            "Models this deployment's LLM proxy currently serves. A caller can also override "
            "the model per request."
        ),
        "surfaces": (
            "Every surface that stamps a session today. A new client can introduce its own, "
            "so treat this as the current vocabulary rather than a closed set."
        ),
        "platforms": "The operating systems Mewbo reports, lowercased.",
    }

    def candidates_for(self, source_key: str) -> CandidateValues | None:
        """The candidates for one source, or ``None`` when there is nothing to show.

        ``None`` on BOTH an unknown *source_key* (a typo'd or since-removed
        ``x-values``, which must degrade to a plain row rather than raise into
        the operator's settings page) and an empty source. Every source here is
        :attr:`ValuesKind.KNOWN`; the only CLOSED list in the reference comes
        from a real enum's ``$defs``, which :meth:`InstructionContext.describe`
        resolves without consulting this catalog at all.
        """
        note = self._NOTES.get(source_key)
        if note is None:
            return None
        values = getattr(self, source_key, ())
        if not values:
            return None
        return CandidateValues(values=tuple(values), kind=ValuesKind.KNOWN, note=note)


__all__ = [
    "KNOWN_PLATFORMS",
    "CandidateValues",
    "InstructionValueCatalog",
    "InstructionVariable",
    "ValuesKind",
]
