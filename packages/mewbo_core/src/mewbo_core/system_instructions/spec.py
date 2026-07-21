#!/usr/bin/env python3
"""Custom system-instructions contracts — the render context + the stored doc.

Two Pydantic models at two different trust boundaries:

* :class:`InstructionContext` is the **public variable contract** handed to the
  operator's Jinja template. Every field is deterministic plain data (no live
  objects, no callables) because the template is operator-authored and rendered
  in a sandbox — a callable in scope is an escape hatch. The API serves this
  model's ``model_json_schema()`` as the variable reference the console renders,
  which is why every field carries a ``description``.
* :class:`SystemInstructionsDoc` is the persisted document (a singleton keyed by
  ``id``) AND the renderer of its own template: an oversized template is rejected
  at definition (the write boundary), while :meth:`SystemInstructionsDoc.render`
  turns any broken or hostile template into "inject nothing" rather than a dead
  turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jinja2 import ChainableUndefined, TemplateSyntaxError, UndefinedError
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_core.common import get_logger, utc_now_iso
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.system_instructions.values import (
    CandidateValues,
    InstructionValueCatalog,
    InstructionVariable,
    ValuesKind,
)

logging = get_logger(name="core.system_instructions.spec")

# Hard cap on the SOURCE an operator may store. Rejected at definition (the PUT
# boundary), so an oversized template can never reach a render in a live turn.
MAX_TEMPLATE_BYTES = 64 * 1024

# Hard cap on RENDERED output. ``MAX_TEMPLATE_BYTES`` does not bound this — a
# small template with a loop can expand without limit — and this text lands in
# the system prompt of every LLM call in the run.
MAX_RENDERED_BYTES = 16 * 1024

_TRUNCATION_MARKER = "\n[... truncated]"

# The singleton document id. Keyed rather than hardcoded so per-project /
# per-user scoping can be added later with no schema migration.
GLOBAL_INSTRUCTIONS_ID = "global"

# THE sandbox for operator-authored text — one environment, deliberately
# separate from ``PromptRegistry._env`` (non-sandboxed, ``StrictUndefined``,
# shared by every engine prompt). Never render operator text on that one: it is
# untrusted in a way engine prompts are not.
#
# Four settings carry the security/robustness contract:
#
# * ``SandboxedEnvironment`` — attribute-access breakouts
#   (``''.__class__.__mro__``) raise ``SecurityError`` instead of evaluating.
# * ``loader=None`` — ``{% include %}`` / ``{% import %}`` / ``{% extends %}``
#   have no source to pull from and fail closed, so a template cannot reach the
#   filesystem.
# * ``ChainableUndefined`` — INVERTS the registry's ``StrictUndefined``: an
#   unknown or since-removed variable renders BLANK. The render sits upstream of
#   the first LLM call, so a template written against a future field must not
#   raise inside a live turn.
# * ``autoescape=False`` — the rendered output is an LLM prompt fragment, never
#   browser-facing HTML; HTML-entity-encoding it would corrupt the model input.
INSTRUCTION_SANDBOX = SandboxedEnvironment(
    loader=None,
    autoescape=False,
    undefined=ChainableUndefined,
    keep_trailing_newline=True,
)


@dataclass(frozen=True, slots=True)
class RenderedInstructions:
    """The outcome of one render: the text to inject, and why it is empty.

    A plain frozen dataclass, not a Pydantic model — it never crosses a trust
    boundary (in-process return value only), and returning the error ALONGSIDE
    the text is what keeps :meth:`SystemInstructionsDoc.render` free of hidden
    mutation: the persisted ``last_error`` field is written by the caller that
    owns the store, not by the model rendering itself.
    """

    text: str
    error: str | None = None


class InstructionContext(BaseModel):
    """The variable contract exposed to an operator's instruction template.

    Frozen + ``extra="forbid"``: the field set IS the documented API. Adding a
    field is additive (a template referencing an unknown name renders blank
    rather than raising — see ``INSTRUCTION_SANDBOX``), but removing or renaming
    one silently changes every operator's template, so treat this as a wire
    contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: str = Field(
        default="unknown",
        # The candidate values live in the CATALOG (``x-values``), never in this
        # prose. A hand-written list here would be a second home for the surface
        # vocabulary and would drift the moment a client stamped a new one —
        # which is exactly what it did (it advertised surfaces by hand while the
        # real stamp sites moved on). Say what the field MEANS; let the catalog
        # say what it can CONTAIN.
        description=(
            "The client surface that started this session. It is 'unknown' when the "
            "caller did not stamp one."
        ),
        json_schema_extra={"x-values": "surfaces"},
    )
    origin: SessionOrigin = Field(
        default=SessionOrigin.USER,
        # No ``x-values``: this is a REAL enum, so its closed member list comes
        # from the schema's ``$defs`` and stays in its one home (SessionOrigin).
        description="Coarse provenance of the session: who or what started it.",
    )
    is_mobile: bool = Field(
        default=False,
        description="True when the invoking surface is a mobile client (Android/iOS/Aura).",
    )
    session_id: str = Field(
        default="",
        description="Identifier of the session being run.",
    )
    model: str = Field(
        default="",
        description="Name of the LLM driving this run.",
        json_schema_extra={"x-values": "models"},
    )
    cwd: str = Field(
        default="",
        description="Working directory (project root) the agent operates in.",
    )
    platform: str = Field(
        default="",
        description="Operating system of the host running the agent, lowercased (e.g. linux).",
        json_schema_extra={"x-values": "platforms"},
    )
    hostname: str = Field(
        default="",
        description="Hostname of the machine running the agent.",
    )
    mewbo_version: str = Field(
        default="",
        description="Version of Mewbo serving this run.",
    )
    capabilities: tuple[str, ...] = Field(
        default=(),
        description=(
            "Capabilities this session's client advertised, available to the template as "
            "a list. Test for one ('wiki' in capabilities) rather than assuming it is there."
        ),
        json_schema_extra={"x-values": "capabilities"},
    )
    tools: tuple[str, ...] = Field(
        default=(),
        # This description is the WHOLE POINT of the field's widening: it used to
        # claim "the tools available to this session" while carrying only the
        # ToolRegistry's specs, so ``'wiki_search' in tools`` was silently False
        # for an agent that genuinely held the tool. It now says exactly what it
        # contains AND what it omits. If the contents ever change, change this
        # line in the same commit.
        description=(
            "Tools available to this session, available to the template as a list. It "
            "covers the registry tools bound for this run, including tool_search, plus "
            "the session tools built for the root agent (wiki, scg, widget and trigger "
            "tools, for example). It omits the five internals the agent loop injects for "
            "itself (spawn_agent, spawn_agents, update_todos, exit_plan_mode and "
            "activate_skill), because those are decided later in the run. Plan mode also "
            "filters separately, so the model can end up restricted to a narrower set "
            "than this list shows."
        ),
        json_schema_extra={"x-values": "tools"},
    )
    project: str | None = Field(
        default=None,
        description=(
            "Project the session is bound to, or null. It is null both when the session "
            "is not project-scoped and when it runs in a managed worktree, which is "
            "reported as a worktree rather than a project."
        ),
        json_schema_extra={"x-values": "projects"},
    )

    @classmethod
    def describe(cls, catalog: InstructionValueCatalog) -> tuple[InstructionVariable, ...]:
        """Describe THIS model as the operator-facing variable reference.

        The model documents itself: the rows are walked out of
        ``model_json_schema()`` — the very schema the renderer exposes — so the
        table an operator reads can never drift from what a template can actually
        reference. Lives in core, not in the HTTP controller that used to host
        it, because it describes CORE's own schema; the app now calls this and
        renders the result.

        Candidates resolve in a strict order, and the order is the contract:

        1. a real **enum** (``origin``) yields its ``$defs`` members as
           :attr:`ValuesKind.CLOSED` — a value it is ALWAYS one of;
        2. else a field declaring an ``x-values`` source yields that source from
           *catalog* as :attr:`ValuesKind.KNOWN` (or nothing, when the deployment
           supplied nothing for it);
        3. else no candidates at all (``session_id``, ``cwd``, ``hostname``, ...
           are free-form and a list would be a lie).
        """
        schema = cls.model_json_schema()
        defs = schema.get("$defs", {})
        rows: list[InstructionVariable] = []
        for name, prop in schema.get("properties", {}).items():
            enum_values = cls._schema_enum_values(prop, defs)
            if enum_values:
                candidates = CandidateValues(
                    values=tuple(enum_values),
                    kind=ValuesKind.CLOSED,
                    note="This is always one of these values.",
                )
            else:
                # ``x-values`` is declared INLINE on the field, so there is no
                # second field->source table to fall out of sync. A field with no
                # source (or a source this deployment left empty) gets no
                # candidates rather than an empty list.
                source = prop.get("x-values")
                candidates = (
                    catalog.candidates_for(source) if isinstance(source, str) else None
                )
            rows.append(
                InstructionVariable(
                    name=name,
                    type=cls._schema_json_type(prop, defs) or "string",
                    description=prop.get("description", ""),
                    candidates=candidates,
                )
            )
        return tuple(rows)

    # -- JSON-schema introspection (private; describes THIS model's schema) ----

    @staticmethod
    def _resolve_schema_ref(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
        """Resolve a bare or ``allOf``-wrapped ``$ref`` against *defs*.

        An enum-typed field (``origin: SessionOrigin``) does NOT serialize inline
        the way a plain ``str``/``bool`` field does — Pydantic v2 emits a ``$ref``
        into ``$defs`` (siblings like ``description``/``default`` sit alongside it
        on the property itself), and some schema shapes wrap it as
        ``allOf: [{"$ref": ...}]`` instead. Both resolve to the same ``$defs``
        entry, and this is the ONE place that knows the rule — generic over any
        field, never special-cased to ``origin``. A property with no ref at all
        (the common case) passes through unchanged.
        """
        ref = prop.get("$ref")
        if ref is None:
            for sub in prop.get("allOf") or []:
                ref = sub.get("$ref")
                if ref is not None:
                    break
        if ref is None:
            return prop
        key = ref.rsplit("/", 1)[-1]
        return defs.get(key, prop)

    @classmethod
    def _schema_json_type(cls, prop: dict[str, Any], defs: dict[str, Any]) -> str | None:
        """The field's JSON type, seeing through a ``$ref`` and an optional union."""
        resolved = cls._resolve_schema_ref(prop, defs)
        json_type = resolved.get("type")
        if json_type is not None:
            return json_type
        for sub in resolved.get("anyOf") or resolved.get("oneOf") or []:
            sub_resolved = cls._resolve_schema_ref(sub, defs)
            sub_type = sub_resolved.get("type")
            if sub_type not in (None, "null"):
                return sub_type
        return None

    @classmethod
    def _schema_enum_values(cls, prop: dict[str, Any], defs: dict[str, Any]) -> list[str] | None:
        """The field's legal values when it's a closed enum, else ``None``.

        Same resolve-then-look shape as :meth:`_schema_json_type` (including the
        ``anyOf``/``oneOf`` arm, for a future ``Optional[SomeEnum]`` field) so an
        operator sees the exact set to compare against in a template, not just
        the word "string".
        """
        resolved = cls._resolve_schema_ref(prop, defs)
        enum_values = resolved.get("enum")
        if enum_values:
            return list(enum_values)
        for sub in resolved.get("anyOf") or resolved.get("oneOf") or []:
            sub_resolved = cls._resolve_schema_ref(sub, defs)
            sub_enum = sub_resolved.get("enum")
            if sub_enum:
                return list(sub_enum)
        return None

    def to_render_vars(self) -> dict[str, object]:
        """Return the data-only mapping handed to Jinja as the template globals.

        ``mode="json"`` is load-bearing, not cosmetic: it is the primitive
        projection at the Jinja boundary. The model holds the STRONG type
        (``origin`` is a ``SessionOrigin`` str-enum member), but Jinja must get
        PLAIN data — a bare ``model_dump()`` would hand the template the enum
        member itself, and ``{{ origin }}`` would render ``SessionOrigin.WIKI``
        instead of ``"wiki"``, silently corrupting every operator's prompt. The
        json mode also serializes the tuple fields (``capabilities``, ``tools``)
        to lists for free, so an operator's template can use the idioms they'd
        expect (``in``, ``|join``, ``|length``) without surprises.
        """
        return self.model_dump(mode="json")


class SystemInstructionsDoc(BaseModel):
    """The stored custom-instructions template (a singleton, keyed by ``id``).

    The document renders ITSELF: the template is the model's data, so compiling
    and rendering it are behavior intrinsic to that data rather than a service's
    job. The two operations are deliberately asymmetric —
    :meth:`validate_template` is the WRITE path's gate (it may reject), while
    :meth:`render` is the RUN path's and cannot fail: it degrades a broken or
    hostile template to no injection at all.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        default=GLOBAL_INSTRUCTIONS_ID,
        description="Scope key for this document. Currently always 'global'.",
    )
    template: str = Field(
        default="",
        description="Jinja source whose rendered output is appended to the system prompt.",
    )
    enabled: bool = Field(
        default=True,
        description="When false, the template is stored but never rendered into a run.",
    )
    updated_at: str = Field(
        default_factory=utc_now_iso,
        description="ISO-8601 timestamp of the last write.",
    )
    last_error: str | None = Field(
        default=None,
        description=(
            "Error recorded by the most recent failed render, or null. Surfaced in "
            "the settings UI so a broken template is visible even though it never "
            "breaks a run."
        ),
    )

    @field_validator("id")
    @classmethod
    def _id_non_empty(cls, value: str) -> str:
        """Reject a blank scope key — it is the store's primary key."""
        if not value.strip():
            raise ValueError("id must not be empty")
        return value

    @field_validator("template")
    @classmethod
    def _template_within_cap(cls, value: str) -> str:
        """Reject an oversized template at DEFINITION, not at render time.

        SIZE is validated here because it is an immutable fact about the data.
        Whether the template COMPILES deliberately is NOT — that stays
        :meth:`validate_template`, an explicit check the write path calls. Making
        it a validator would make an already-stored template that no longer
        parses UNLOADABLE, so the ``GET`` whose whole job is to show an operator
        their broken template so they can fix it would 500 instead. Do not move
        the compile check up here.
        """
        size = len(value.encode("utf-8"))
        if size > MAX_TEMPLATE_BYTES:
            raise ValueError(
                f"template is {size} bytes, over the {MAX_TEMPLATE_BYTES}-byte limit"
            )
        return value

    def validate_template(self) -> str | None:
        """Compile the template without rendering it — the WRITE path's gate.

        Returns an operator-facing error message, or ``None`` when it compiles.
        A syntax error is reported at authoring time rather than discovered as a
        silently-empty section on the next run.
        """
        try:
            INSTRUCTION_SANDBOX.from_string(self.template)
        except TemplateSyntaxError as exc:
            return f"Template syntax error on line {exc.lineno}: {exc.message}"
        except Exception as exc:  # defensive: never let validation itself raise
            return f"Template is invalid: {exc}"
        return None

    def render(self, ctx: InstructionContext) -> RenderedInstructions:
        """Render against *ctx* — the RUN path, and it NEVER raises.

        A failure yields empty text plus the reason: a broken or hostile template
        degrades the run to exactly today's behaviour (no injection) instead of
        breaking it, because this render sits upstream of the first LLM call and
        an exception here would kill the turn before the model ever ran.
        """
        if not self.template.strip():
            return RenderedInstructions(text="")
        try:
            compiled = INSTRUCTION_SANDBOX.from_string(self.template)
            rendered = compiled.render(**ctx.to_render_vars())
        except TemplateSyntaxError as exc:
            error = f"Template syntax error on line {exc.lineno}: {exc.message}"
        except SecurityError as exc:
            error = f"Template attempted a disallowed operation: {exc}"
        except UndefinedError as exc:
            error = f"Template referenced an undefined value: {exc}"
        except Exception as exc:  # defensive: a filter/extension can raise anything
            error = f"Template failed to render: {exc}"
        else:
            return RenderedInstructions(text=self._truncate(rendered.strip()))

        logging.warning(
            "Custom system instructions failed to render; injecting nothing. {}", error
        )
        return RenderedInstructions(text="", error=error)

    @staticmethod
    def _truncate(text: str) -> str:
        """Bound the rendered text at :data:`MAX_RENDERED_BYTES`."""
        encoded = text.encode("utf-8")
        if len(encoded) <= MAX_RENDERED_BYTES:
            return text
        clipped = encoded[:MAX_RENDERED_BYTES].decode("utf-8", errors="ignore")
        return clipped + _TRUNCATION_MARKER


__all__ = [
    "GLOBAL_INSTRUCTIONS_ID",
    "INSTRUCTION_SANDBOX",
    "MAX_RENDERED_BYTES",
    "MAX_TEMPLATE_BYTES",
    "CandidateValues",
    "InstructionContext",
    "InstructionValueCatalog",
    "InstructionVariable",
    "RenderedInstructions",
    "SystemInstructionsDoc",
    "ValuesKind",
]
