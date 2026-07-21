"""REST contract for custom system instructions.

An operator writes ONE Jinja template (stored as a singleton document, id
``"global"``) that branches on which client/surface invoked the session
(``mewbo_core.system_instructions.InstructionContext`` — surface, origin,
is_mobile, session_id, model, cwd, platform, hostname, mewbo_version,
capabilities, tools, project). This module is the REST CRUD + preview surface
over that document; the web console is the sole consumer.

**SECURITY — operator-only, REST + API key ONLY.** This document is rendered
straight into the SYSTEM PROMPT of every session. There is deliberately no
agent-facing SessionTool or MCP tool that can read or write it: an agent that
could rewrite its own system prompt is a privilege-escalation path. Every route
here requires ``X-API-KEY`` — do not add an unauthenticated or agent-reachable
path to this document.

Wire shape decisions:

* **The doc is a SINGLETON, not session-scoped.** ``GET`` never 404s — an
  operator who has never written one gets the same defaults
  ``SystemInstructionsDoc()`` would construct, so the console's editor always
  has something to render.
* **``PUT`` is a full upsert, not a partial PATCH** (mirrors
  ``CredentialUpsert`` — a thin transport shell, ``extra="forbid"``, so a
  client smuggling a server-owned field like ``updated_at``/``last_error``
  gets a clean 400). The server stamps ``updated_at`` and clears
  ``last_error`` on every successful write; a template that fails to COMPILE
  (``SystemInstructionsDoc.validate_template()``) is rejected 400 before
  anything is persisted.
* **``POST /preview`` never 500s on a bad template.** A render failure comes
  back 200 with ``{rendered: "", error: "..."}`` — this is what lets an author
  see a broken template BEFORE it reaches a live session, not a debugging
  surface that itself breaks on the input it exists to catch.
* **``GET /variables`` is GENERATED, never hand-authored** — so the variable
  table the console renders can never drift from what the renderer actually
  exposes to a template. Its SHAPE comes from core
  (``InstructionContext.describe(catalog)``, which owns the meaning of its own
  fields); its VALUES come from :class:`InstructionValueSources`
  (``value_sources.py``) — the app-side I/O edge that asks this deployment what
  tools/capabilities/projects/models it actually has. Each of those probes is
  best-effort, so the page an operator opens to FIX a broken template cannot
  itself be taken down by a dead LLM proxy.

Paradigm (mirrors ``triggers/routes.py`` / ``TriggerRoutesController``): this
module holds no mutable module-level wiring. :class:`SystemInstructionsRoutesController`
is the atomic class owning the injected store + auth guard as fields and every
serialize/validate/render/schema helper as a method; the Flask-RESTX Resources
are thin HTTP adapters that receive the one controller instance by DEPENDENCY
INJECTION via ``resource_class_kwargs`` (``init_system_instructions_routes``),
so the request path reads no module global.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from flask import request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.system_instructions import (
    GLOBAL_INSTRUCTIONS_ID,
    InstructionContext,
    SystemInstructionsDoc,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_api.auth.guard_registry import guard
from mewbo_api.responses import ApiResponseKit

if TYPE_CHECKING:  # pragma: no cover - typing only

    from mewbo_core.system_instructions import InstructionVariable

    from mewbo_api.system_instructions.value_sources import InstructionValueSources

logging = get_logger(name="api.system_instructions.routes")

system_instructions_ns = Namespace(
    "system-instructions",
    description=(
        "Operator-authored Jinja template appended to every session's system "
        "prompt, branching on client/surface (#see InstructionContext)."
    ),
)

kit = ApiResponseKit(system_instructions_ns, prefix="SystemInstructions")


# ---------------------------------------------------------------------------
# Wire models — the APP owns its HTTP contract (transport only, never persisted)
# ---------------------------------------------------------------------------


class SystemInstructionsDto(BaseModel):
    """``GET``/``PUT /api/system-instructions`` response — the APP's wire shape.

    Deliberately NOT core's ``SystemInstructionsDoc``. That model is the
    PERSISTENCE shape; this is the HTTP contract, and the app owns it. Dumping
    the stored doc straight onto the wire would mean any field core later adds
    to its storage model auto-leaks to every console client — the app would not
    own its own boundary. Building this DTO **from** the doc makes the wire
    surface an explicit, reviewable list.

    camelCase via explicit per-field ``Field(alias=...)`` + ``populate_by_name``
    — the house idiom (``wiki/settings.py:ProjectSettingsPatch``,
    ``git_credentials_routes.py:CredentialValidateRequest``, and the
    ``valueHint``/``updatedAt`` rows the console already consumes). There is no
    repo-wide ``alias_generator`` and this must not introduce one. Serialize
    with ``by_alias=True``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    template: str
    enabled: bool
    updated_at: str = Field(alias="updatedAt")
    last_error: str | None = Field(default=None, alias="lastError")

    @classmethod
    def from_doc(cls, doc: SystemInstructionsDoc) -> SystemInstructionsDto:
        """Project the persisted document onto the wire — field by field, on purpose.

        The explicit mapping IS the trust boundary: a new field on core's
        storage model does not reach a client until someone adds it here.
        """
        return cls(
            id=doc.id,
            template=doc.template,
            enabled=doc.enabled,
            updatedAt=doc.updated_at,
            lastError=doc.last_error,
        )

    def to_wire(self) -> dict[str, Any]:
        """The camelCase JSON body."""
        return self.model_dump(mode="json", by_alias=True)


class InstructionVariableDto(BaseModel):
    """One row of ``GET /api/system-instructions/variables``.

    ``values`` is what an operator actually needs: the strings their template
    may compare a variable against. ``valuesKind`` says how far to trust it —
    ``closed`` (a session's value is ALWAYS one of these, e.g. ``origin``'s
    ``SessionOrigin`` members) versus ``known`` (installed/observed on THIS
    deployment, e.g. the tool ids or the proxy's model list — real, but not
    exhaustive). ``valuesNote`` is the one-line prose that distinction earns.

    There is deliberately no separate ``enum`` field: ``values`` +
    ``valuesKind: "closed"`` says the same thing strictly better, and two
    channels carrying the same fact is exactly the drift the house rules
    forbid.

    camelCase via explicit per-field ``Field(alias=...)`` + ``populate_by_name``
    (the house idiom, mirroring :class:`SystemInstructionsDto` — no
    ``alias_generator``). ``name``/``type``/``description``/``values`` are
    already camelCase-identical, so only the two-word keys carry aliases.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: str
    description: str
    values: list[str] | None = None
    values_kind: Literal["closed", "known"] | None = Field(default=None, alias="valuesKind")
    values_note: str | None = Field(default=None, alias="valuesNote")

    @classmethod
    def from_variable(cls, variable: InstructionVariable) -> InstructionVariableDto:
        """Project core's ``InstructionVariable`` onto the wire — the trust boundary.

        A variable with no source of values goes out with all three value keys
        ``null``. Core makes that ``candidates is None`` for BOTH "no source"
        and "the source came back empty" (a dead LLM proxy, say) precisely so
        the console never shows an operator "known values: (none)" — which
        reads as *this variable is always empty* rather than *we could not ask*.

        ``kind.value``, not ``str(kind)``: ``ValuesKind`` is a ``(str, Enum)``,
        whose ``__str__`` renders ``"ValuesKind.KNOWN"``. Only ``.value`` (or a
        bare equality test) yields the ``"known"`` the wire contract promises.
        """
        candidates = variable.candidates
        if candidates is None:
            return cls(name=variable.name, type=variable.type, description=variable.description)
        return cls(
            name=variable.name,
            type=variable.type,
            description=variable.description,
            values=list(candidates.values),
            valuesKind=candidates.kind.value,
            valuesNote=candidates.note,
        )

    def to_wire(self) -> dict[str, Any]:
        """The camelCase JSON row."""
        return self.model_dump(mode="json", by_alias=True)


class SystemInstructionsPutRequest(BaseModel):
    """``PUT /api/system-instructions`` body — full upsert of the singleton doc.

    ``extra="forbid"`` is load-bearing: a client smuggling ``id``/``updated_at``/
    ``last_error`` (all server-owned) gets a clean 400 instead of a silent
    no-op, mirroring ``CredentialUpsert``/``ProjectSettingsPatch``.
    """

    model_config = ConfigDict(extra="forbid")

    template: str
    enabled: bool = True


class SystemInstructionsPreviewRequest(BaseModel):
    """``POST /api/system-instructions/preview`` body — both fields optional.

    An omitted ``template`` previews the STORED template. ``context`` is a
    partial override (e.g. ``{"surface": "android"}``) merged onto a
    representative sample :class:`InstructionContext` so an author can compare
    surfaces without a live session.
    """

    model_config = ConfigDict(extra="forbid")

    template: str | None = None
    context: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Controller — the atomic class owning the store + every helper
# ---------------------------------------------------------------------------


class SystemInstructionsRoutesController:
    """Owns the system-instructions REST behavior over its injected store.

    Atomic feature class (the ``TriggerRoutesController`` idiom): the store is
    injected state; serialize / validate / render / schema-introspect are
    methods; the Resources below are thin HTTP adapters constructed once by
    :func:`init_system_instructions_routes` and handed the one instance via
    ``resource_class_kwargs`` — the request path reads no module global.
    """

    # Curated, realistic sample values overlaid onto ``InstructionContext``'s own
    # defaults for a far more useful preview than blank/empty fields — every
    # field in the model already has a default, so a field this dict doesn't
    # cover just falls back to that default rather than breaking preview when
    # the schema grows a new one.
    _SAMPLE_VALUES: dict[str, Any] = {
        "surface": "cli",
        "origin": "user",
        "is_mobile": False,
        "session_id": "9e2d47c1a0b34f12",
        "model": "anthropic/claude-opus-4-8",
        "cwd": "/home/user/project",
        "platform": "linux",
        "hostname": "dev-box",
        "mewbo_version": "0.1.1",
        "capabilities": ["wiki"],
        "tools": ["read_file", "write_file"],
        "project": "my-service",
    }

    def __init__(
        self,
        *,
        store: Any,
        value_sources: InstructionValueSources,
    ) -> None:
        """Bind the injected store and the value-source edge.

        No auth guard is injected: every Resource declares its own requirement
        with ``@guard.requires``, which resolves the live ``AuthKit`` through
        ``guard_registry`` at request time.
        """
        self.store = store
        self.value_sources = value_sources

    # -- serialization + error helpers -------------------------------------

    @staticmethod
    def _err(code: Any, reason: str, status: int, *, retryable: bool = False) -> tuple[dict, int]:
        return {"error": {"code": code, "reason": reason, "retryable": retryable}}, status

    def _dto(self, doc: SystemInstructionsDoc) -> dict[str, Any]:
        """Persisted doc -> the app-owned camelCase wire body."""
        return SystemInstructionsDto.from_doc(doc).to_wire()

    def _get_or_default(self) -> SystemInstructionsDoc:
        """The stored doc, or an in-memory default when none has been saved yet.

        A singleton settings record, not a session-scoped resource — GET must
        never 404 just because an operator hasn't written one, so it falls
        back to the same defaults ``SystemInstructionsDoc()`` constructs.
        """
        doc = self.store.get(doc_id=GLOBAL_INSTRUCTIONS_ID)
        return doc if doc is not None else SystemInstructionsDoc(id=GLOBAL_INSTRUCTIONS_ID)

    # -- domain operations (called by the thin Resource adapters) ----------

    def get(self) -> tuple[dict, int]:
        """Read the singleton document."""
        return self._dto(self._get_or_default()), 200

    def put(self, body: Any) -> tuple[dict, int]:
        """Validate (incl. Jinja compile) + upsert the singleton document."""
        if not isinstance(body, dict):
            return self._err("validation", "Request body must be a JSON object.", 400)
        try:
            req = SystemInstructionsPutRequest.model_validate(body)
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)
        try:
            # id/updated_at/last_error are server-owned: id is the fixed
            # singleton key, updated_at is re-stamped by the store's own put()
            # (its authoritative return value is what we serialize back), and
            # last_error is cleared on every successful write. Constructing the
            # model IS the size/extra-field gate — MAX_TEMPLATE_BYTES raises
            # here, at the write boundary, not at render time in a live turn.
            doc = SystemInstructionsDoc(
                id=GLOBAL_INSTRUCTIONS_ID,
                template=req.template,
                enabled=req.enabled,
                last_error=None,
            )
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)
        # Compilation is NOT a definition-time validator (see the model): the
        # write path is where a template that does not parse gets rejected.
        compile_error = doc.validate_template()
        if compile_error is not None:
            return self._err("validation", compile_error, 400)
        stamped = self.store.put(doc)
        return self._dto(stamped), 200

    def preview(self, body: Any) -> tuple[dict, int]:
        """Render the submitted-or-stored template against a sample context.

        Never 500s on a broken template — ``SystemInstructionsDoc.render`` never
        raises; a compile/render failure comes back 200 with
        ``{rendered: "", error: "..."}``.
        """
        if body is not None and not isinstance(body, dict):
            return self._err("validation", "Request body must be a JSON object.", 400)
        try:
            req = SystemInstructionsPreviewRequest.model_validate(body or {})
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)
        try:
            ctx = self._sample_context(req.context or {})
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)
        source = req.template if req.template is not None else self._get_or_default().template
        try:
            doc = SystemInstructionsDoc(id=GLOBAL_INSTRUCTIONS_ID, template=source)
        except ValidationError as exc:
            return self._err("validation", str(exc), 400)
        rendered = doc.render(ctx)
        return {"rendered": rendered.text, "error": rendered.error}, 200

    def variables(self) -> tuple[dict, int]:
        """The variable reference table: live values from the edge, shape from CORE.

        Three steps, none of which is schema introspection: resolve this
        deployment's live facts (:class:`InstructionValueSources` — the only
        I/O in this feature, and best-effort, so a dead source empties one row
        instead of failing the request), hand that pure catalog to core's
        ``InstructionContext.describe()``, and project each variable onto the
        wire DTO.

        The controller deliberately re-derives NOTHING about
        ``InstructionContext``'s own semantics. Four classmethods here used to
        walk its JSON schema resolving ``$ref``s and enums — an HTTP adapter
        re-implementing the model's meaning, which drifts the moment a field
        changes shape. That knowledge belongs to the model; the controller
        keeps only its wire ownership.
        """
        variables = InstructionContext.describe(self.value_sources.catalog())
        rows = [InstructionVariableDto.from_variable(var).to_wire() for var in variables]
        return {"variables": rows}, 200

    # -- preview context ------------------------------------------------------

    @classmethod
    def _sample_context(cls, overrides: dict[str, Any]) -> InstructionContext:
        """Build a representative ``InstructionContext``.

        Layers model defaults, then curated samples, then *overrides* (so
        e.g. ``{"surface": "android"}`` deterministically wins over the
        curated ``"cli"``). Every ``InstructionContext`` field carries its
        own default, so this can never break on a field the curated
        ``_SAMPLE_VALUES`` doesn't cover — it just falls back to that default.
        """
        base = InstructionContext().model_dump()
        base.update({k: v for k, v in cls._SAMPLE_VALUES.items() if k in base})
        base.update(overrides)
        return InstructionContext.model_validate(base)


# ---------------------------------------------------------------------------
# Flask-RESTX doc models (example= drives the Scalar sample bodies)
# ---------------------------------------------------------------------------

system_instructions_dto_model = system_instructions_ns.model(
    "SystemInstructionsDoc",
    {
        "id": fields.String(example="global"),
        "template": fields.String(
            example=(
                "{% if surface == 'android' %}"
                "Keep replies short; the user is on a phone."
                "{% else %}"
                "You are Mewbo, running via {{ surface }}."
                "{% endif %}"
            )
        ),
        "enabled": fields.Boolean(example=True),
        # camelCase on the wire — these keys must mirror SystemInstructionsDto's
        # aliases exactly (this doc model is what the published OpenAPI spec and
        # the Scalar samples show; the Pydantic DTO is what actually serializes).
        "updatedAt": fields.String(example="2026-07-14T09:00:00+00:00"),
        "lastError": fields.String(example=None),
    },
)

system_instructions_put_model = system_instructions_ns.model(
    "SystemInstructionsPutRequest",
    {
        "template": fields.String(
            required=True,
            example="{% if is_mobile %}Prefer short replies.{% endif %} You are on {{ surface }}.",
        ),
        "enabled": fields.Boolean(example=True, description="Defaults to true."),
    },
)

preview_request_model = system_instructions_ns.model(
    "SystemInstructionsPreviewRequest",
    {
        "template": fields.String(
            example=None,
            description="Optional template override; omit to preview the STORED template.",
        ),
        "context": fields.Raw(
            example={"surface": "android"},
            description="Partial InstructionContext override merged onto a representative sample.",
        ),
    },
)

preview_response_model = system_instructions_ns.model(
    "SystemInstructionsPreviewResponse",
    {
        "rendered": fields.String(example="Keep replies short; the user is on a phone."),
        "error": fields.String(example=None),
    },
)

variable_model = system_instructions_ns.model(
    "SystemInstructionsVariable",
    {
        "name": fields.String(example="capabilities"),
        "type": fields.String(example="array"),
        "description": fields.String(
            example=(
                "Capabilities granted to this session (e.g. wiki, scg). Available "
                "to the template as a list."
            )
        ),
        "values": fields.List(
            fields.String,
            required=False,
            example=["scg", "stlite", "wiki"],
            description=(
                "Values this variable can take, or null when nothing on this "
                "deployment can source them."
            ),
        ),
        "valuesKind": fields.String(
            required=False,
            enum=["closed", "known"],
            example="known",
            description=(
                "`closed`: a session's value is ALWAYS one of `values` (e.g. origin). "
                "`known`: the values installed/observed on THIS deployment — real, "
                "but not exhaustive."
            ),
        ),
        "valuesNote": fields.String(
            required=False,
            example="Capabilities declared by the plugins and agents installed here.",
            description="One-line prose explaining where these values came from.",
        ),
    },
)

variables_response_model = system_instructions_ns.model(
    "SystemInstructionsVariablesResponse",
    {"variables": fields.List(fields.Nested(variable_model))},
)


# ---------------------------------------------------------------------------
# Resource adapters — thin HTTP boundary; the injected controller does the work
# ---------------------------------------------------------------------------


class _ControllerResource(Resource):
    """Base Resource that receives the one controller via ``resource_class_kwargs``.

    Mirrors ``triggers/routes.py``'s ``_ControllerResource`` — no Resource ever
    reaches into module scope for its collaborators.
    """

    def __init__(
        self,
        api: Any = None,
        *args: Any,
        controller: SystemInstructionsRoutesController,
        **kwargs: Any,
    ) -> None:
        super().__init__(api, *args, **kwargs)
        self.controller = controller


class SystemInstructionsItem(_ControllerResource):
    """Read or upsert the singleton system-instructions document."""

    @system_instructions_ns.doc(security="apikey")
    @kit.auth_error()
    @system_instructions_ns.response(
        200, "The system-instructions document.", system_instructions_dto_model
    )
    @guard.requires("system_instructions.read")
    def get(self) -> tuple[dict, int]:
        """Read the singleton document (never 404s — defaults if unset)."""
        return self.controller.get()

    @system_instructions_ns.doc(security="apikey")
    @system_instructions_ns.expect(system_instructions_put_model)
    @kit.errors(400)
    @kit.auth_error()
    @system_instructions_ns.response(200, "Document upserted.", system_instructions_dto_model)
    @guard.requires("system_instructions.write")
    def put(self) -> tuple[dict, int]:
        """Upsert the document.

        400 when the body is malformed or the Jinja template does not
        compile (rejected before anything is persisted).
        """
        return self.controller.put(request.get_json(silent=True) or {})


class SystemInstructionsPreview(_ControllerResource):
    """Render the submitted-or-stored template against a sample context."""

    @system_instructions_ns.doc(security="apikey")
    @system_instructions_ns.expect(preview_request_model)
    @kit.errors(400)
    @kit.auth_error()
    @system_instructions_ns.response(
        200,
        "Rendered preview (a broken template returns error, never 500).",
        preview_response_model,
    )
    @guard.requires("system_instructions.read")
    def post(self) -> tuple[dict, int]:
        """Render for preview. Always 200 on a render failure (``error`` set)."""
        return self.controller.preview(request.get_json(silent=True))


class SystemInstructionsVariables(_ControllerResource):
    """The documented variable reference, generated from ``InstructionContext``."""

    @system_instructions_ns.doc(security="apikey")
    @kit.auth_error()
    @system_instructions_ns.response(
        200, "The InstructionContext variable table.", variables_response_model
    )
    @guard.requires("system_instructions.read")
    def get(self) -> tuple[dict, int]:
        """List every variable a template can reference, with type + description."""
        return self.controller.variables()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

# The single DI handle: construction result of ``init_system_instructions_routes``.
# The request path NEVER reads it (Resources receive the controller by
# injection); it exists so a route test can point the one registered
# controller at a fresh store by reassigning its fields.
_controller: SystemInstructionsRoutesController | None = None


def init_system_instructions_routes(
    api: Any,
    *,
    store: Any,
    value_sources: InstructionValueSources,
) -> None:
    """Build the controller, DI it into the Resources, and register (once, at startup).

    Mirrors ``init_trigger_routes``: the caller (``backend.py``'s
    ``init_system_instructions``) builds *store* via the core factory
    (``create_system_instructions_store()``) and *value_sources* over the
    managed-project store, then injects both here — this module performs no
    store I/O and no deployment probing of its own.
    """
    global _controller  # noqa: PLW0603 - single composition-root handle, set once
    _controller = SystemInstructionsRoutesController(
        store=store,
        value_sources=value_sources,
    )
    injected = {"resource_class_kwargs": {"controller": _controller}}
    system_instructions_ns.add_resource(SystemInstructionsItem, "/system-instructions", **injected)
    system_instructions_ns.add_resource(
        SystemInstructionsPreview, "/system-instructions/preview", **injected
    )
    system_instructions_ns.add_resource(
        SystemInstructionsVariables, "/system-instructions/variables", **injected
    )
    api.add_namespace(system_instructions_ns, path="/api")


__all__ = [
    "SystemInstructionsRoutesController",
    "system_instructions_ns",
    "init_system_instructions_routes",
]
