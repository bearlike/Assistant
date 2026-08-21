#!/usr/bin/env python3
"""First-party plugin implementation of the ``present_ui`` SessionTool.

The agent describes a small structured panel with the typed vocabulary in
:mod:`.nodes`; this tool validates it, degrades it to plain text server-side,
and emits ONE ``generative_ui`` event. The console renders the tree; every
other surface renders the precomputed ``alt_text`` and never has to learn the
component vocabulary.

Terminal-free, like ``submit_widget`` and ``update_todos``: the panel renders
off the emitted event, not off run termination, so presenting one is an
ordinary step and the agent still gets to write its closing message.
"""

from __future__ import annotations

import json
import types as _pytypes
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from mewbo_core.builtin_plugins.generative_ui.nodes import (
    ComponentTagSchema,
    ContainerId,
    GenerativeUISpec,
)
from mewbo_core.capabilities import GENERATIVE_UI_CAPABILITY
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.tooling.container_args import JsonContainerArguments
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.contracts.types import Event

logging = get_logger(name="core.builtin_plugins.generative_ui")

PRESENT_UI_TOOL_ID = "present_ui"

# Transcript event kind. FROZEN — a console TS type mirrors it, and a Kotlin
# mirror is expected to follow.
GENERATIVE_UI_EVENT = "generative_ui"

# ``GENERATIVE_UI_CAPABILITY`` is re-exported from its real home in
# ``mewbo_core.capabilities``, which owns the first-party capability registry and
# the ``X-Mewbo-Capabilities`` wire seam. It stays importable from here because
# existing call sites reach for it at this path; new code imports it from
# ``mewbo_core.capabilities``.

# The upsert key's shape. Model-authorable ON PURPOSE: the old
# ``^gui-[0-9a-f]{8}$`` never prevented collision — models fabricated matching
# hex and silently created a NEW panel they believed they were replacing — it
# only prevented READABLE ids. A readable id (``team-directory``) is one the
# model can re-derive from what the panel shows, so an id lost to compaction is
# recoverable instead of guessed. Generated ids keep the ``gui-`` prefix (which
# still matches) so the two origins stay distinguishable in a transcript.
UI_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{2,63}$"

UiId = Annotated[str, StringConstraints(pattern=UI_ID_PATTERN)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class PresentUiArgs(GenerativeUISpec):
    """Render a structured UI panel in the conversation.

    Use this to SHOW structured information — a status board, a comparison
    table, a set of results — instead of describing it in prose. Keep it small:
    a panel is a summary a reader takes in at a glance, not a document.

    `root` is a LIST of component objects. Each object names its type in
    `component` and carries that type's fields beside it. Only `Card` and
    `Stack` may hold `children`, and either may carry an `id` so a later call
    can address it.

    Build a rich panel INCREMENTALLY: present a small skeleton first, then
    `operation="append"` more nodes onto it (or into a named container) call
    by call. Small calls are far more likely to arrive intact than one large
    tree.

    Non-visual clients receive an automatic plain-text rendering, so restating
    the panel's contents in your reply adds nothing. Say what it shows and move
    on — though a reader who asked HOW something works is asking about the
    panel, not for a copy of it, and answering that is not a restatement.
    """

    # SUBCLASSES the spec rather than wrapping it in a ``spec`` field, which is
    # a deletion and not a refactor: the wrapper was the single most-failed part
    # of this tool. Across two traced sessions on two unrelated small models,
    # five of seven rejections were wrapper-shape errors — ``spec`` sent as a
    # bare list, ``root`` sent as an object, node fields spread onto the
    # wrapper, ``$defs`` class names used as its keys. A level that carries no
    # information is a level that can only be got wrong.
    #
    # Inheriting also means ``root``, the tree-limit validator, ``to_wire`` and
    # ``to_text`` arrive as they are: the args model IS a panel, plus how to
    # announce and address it. Nothing is duplicated and nothing can drift.
    #
    # The EMITTED event still carries ``spec: {"root": [...]}`` — the wire shape
    # is frozen and mirrored by a console TS type. Only the model-facing
    # argument changed.
    summary: Summary = Field(
        description="One short line naming what the panel shows, e.g. 'CI status for main'."
    )
    ui_id: UiId | None = Field(
        default=None,
        description=(
            "Omit to create a new panel. Pass the id returned by an earlier "
            "present_ui call to address that panel instead of adding another "
            "one below it. A readable id you author yourself (e.g. "
            "'team-directory') is also accepted when creating."
        ),
    )
    operation: Literal["replace", "append", "update"] = Field(
        default="replace",
        description=(
            "How `root` lands on the addressed panel. 'replace' (default) "
            "redraws the whole panel. 'append' ADDS the nodes in `root` to an "
            "existing panel — after its current nodes, or inside the container "
            "named by `target` — so a rich panel is built across several small "
            "calls instead of one large one. 'update' replaces the ONE "
            "container named by `target` with the single node in `root`."
        ),
    )
    target: ContainerId | None = Field(
        default=None,
        description=(
            "A container id (the `id` you gave a Card/Stack) that `append` "
            "adds into, or that `update` replaces. Only for append/update."
        ),
    )

    @model_validator(mode="after")
    def _operation_contract(self) -> PresentUiArgs:
        """Refuse an operation/argument combination that cannot mean anything.

        Stated here rather than discovered downstream so the rejection names
        the exact missing piece — the same close-the-gap rule the node aliases
        follow.
        """
        if self.operation == "replace" and self.target is not None:
            raise ValueError(
                "target only addresses a container for operation='append' or "
                "'update'; replace redraws the whole panel"
            )
        if self.operation in ("append", "update") and not self.ui_id:
            raise ValueError(
                f"operation='{self.operation}' modifies an existing panel; pass "
                "the ui_id an earlier present_ui call returned (or use "
                "operation='replace' to create one)"
            )
        if self.operation == "update":
            if self.target is None:
                raise ValueError(
                    "operation='update' replaces ONE addressed container; pass "
                    "target=<container id>"
                )
            if len(self.root) != 1:
                raise ValueError(
                    "operation='update' replaces the addressed container with "
                    "exactly ONE node; send a single node in root"
                )
        return self


# Derived from the args model — the same seam ``submit_widget`` and the core
# spawn schema use, so the model-facing schema can never drift from validation.
# ``ComponentTagSchema`` keys each ``$defs`` entry by its component tag, so an
# identifier a model copies out of the schema is one that validates.
PRESENT_UI_SCHEMA: dict[str, object] = pydantic_to_openai_tool(
    PresentUiArgs, name=PRESENT_UI_TOOL_ID, schema_generator=ComponentTagSchema
)
# The vocabulary again, flat, in the field a model reads before deciding to call
# anything — a JSON Schema states it only through ``$ref``, and small models
# measurably do not follow that hop. Derived, so it cannot go stale.
PRESENT_UI_SCHEMA["function"]["description"] += (  # type: ignore[index]
    "\n\n" + GenerativeUISpec.component_guide()
)


class GenerativeUIPayload(BaseModel):
    """Typed payload for the ``generative_ui`` event.

    Owned here rather than as an arm on ``mewbo_core.contracts.types.EventPayload`` — that
    union is generic infrastructure shared by every event kind, and a
    plugin-specific arm there would re-couple core to this vocabulary (the
    ``WidgetReadyPayload`` precedent). The snake_case wire shape is FROZEN: a
    console TS type mirrors it, so changing a key or dropping ``extra="forbid"``
    breaks the client.
    """

    model_config = ConfigDict(extra="forbid")

    ui_id: UiId
    session_id: str
    # The RENDERER-facing tree, always produced by ``GenerativeUISpec.to_wire``
    # — never hand-assembled. ``JsonValue`` is what keeps that promise checkable
    # at the boundary: a non-JSON value smuggled in here would otherwise only
    # fail much later, at the store or the SSE encoder.
    spec: dict[str, JsonValue]
    # Derived from the tree, not model-authored, so it inherits the tree's size
    # ceiling and needs no cap of its own.
    alt_text: str
    summary: str

    @field_validator("spec")
    @classmethod
    def _exact_wire_shape(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Require the frozen ``{"root": [...]}`` shape."""
        if set(value) != {"root"} or not isinstance(value["root"], list):
            raise ValueError(f"spec must be exactly {{'root': [...]}}, got {sorted(value)}")
        return value


class PresentUiTool:
    """Handles ``present_ui`` tool calls.

    Validates the tree through :class:`PresentUiArgs`, mints or reuses the
    ``ui_id`` upsert key, computes ``alt_text`` server-side from the tree's own
    ``to_text`` and emits one ``generative_ui`` event.
    """

    # Class-level attributes satisfy the ``SessionTool`` Protocol natively.
    tool_id: str = PRESENT_UI_TOOL_ID
    schema: dict[str, object] = PRESENT_UI_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    # Sized for the FAILURE case, which is the only large result this tool
    # produces: a success is a few lines, but a rejected tree returns every
    # error, the offending node rewritten to the declared shape, and the
    # component guide. The registry's 2000-char default, which a SessionTool
    # inherits when it declares nothing, would truncate that correction
    # mid-sentence.
    max_result_chars: int = 12_000

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            session_id: Session identifier stamped on the emitted event.
            event_logger: Callback for emitting ``generative_ui`` events;
                usually ``agent_context.event_logger``.
        """
        self._session_id = session_id
        self._event_logger = event_logger
        # The last ACCEPTED tree per ui_id, held on the instance — one tool
        # instance serves an agent for its whole run, which is exactly the
        # window incremental composition targets. The event log needs no delta
        # semantics because every emitted event carries the FULL merged tree:
        # replay and compaction see ordinary replace-by-ui_id events, and
        # `to_text` always degrades the whole panel. After a process restart
        # (or in a later run) this map starts empty and append/update refuse
        # with an instruction to rebuild via replace.
        self._panels: dict[str, GenerativeUISpec] = {}

    def should_terminate_run(self) -> bool:
        """Never terminate — the panel renders off the event, not off the run ending."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol.

        Defined explicitly because ``SessionTool`` is a structural Protocol: its
        default method bodies are NOT inherited by a standalone implementer, so
        a tool missing this crashes the loop's terminal poll with an
        ``AttributeError`` — which is exactly how ``submit_widget`` broke every
        call it served.
        """
        return "awaiting_approval"

    def _emit(self, event: Event) -> None:
        if self._event_logger is None:
            return
        try:
            self._event_logger(event)
        except Exception as exc:
            logging.warning("present_ui event emit failed: {}", exc)

    @classmethod
    def _rejection(
        cls,
        raw: dict[str, Any],
        exc: ValidationError,
        salvage_notes: tuple[str, ...] = (),
    ) -> str:
        """Return a rejection the model can act on without guessing.

        A bare pydantic error names the path that failed and stops there, which
        leaves the caller to INFER the contract from a sequence of refusals.
        Traced models do exactly that, out loud — "Correct Pattern discovered",
        "this is a test to see if 'root' is indeed the required entry point" —
        and one of them inferred it wrongly, announced the wrong shape as a key
        insight, and sent it. Deriving a wire contract from error strings is a
        process that can converge on the wrong answer.

        So the refusal closes the gap itself: EVERY error (not the first alone),
        the ONE offending node rewritten to the declared shape with the model's
        own values kept where they validate, and the derived component guide.
        Nothing is coerced: the validation stays exactly as strict, and this is
        additive text. The result cap (:attr:`max_result_chars`) is sized for
        this message.
        """
        lines = ["ERROR: invalid present_ui args. Every problem, not just the first:"]
        for err in exc.errors():
            lines.append(f"- {cls._loc_path(err['loc'])}: {err['msg']}")
        node, path = cls._offending_node(raw, exc.errors()[0]["loc"])
        if node is not None:
            corrected, dropped = cls._corrected_node(node)
            if corrected is not None:
                head = f"\nYour node at {path}, rewritten to the declared shape"
                if dropped:
                    head += f" (unknown key(s) {', '.join(sorted(dropped))} dropped)"
                head += ' — replace every "..." with your own content:'
                lines.append(head)
                lines.append(json.dumps(corrected, ensure_ascii=False))
        lines.append(
            "\nCorrect shape — root is a LIST, and it is a TOP-LEVEL argument "
            "(there is no 'spec' wrapper):\n"
            '{"root": [{"component": "Alert", "body": "..."}], "summary": "..."}'
        )
        lines.append("")
        lines.append(GenerativeUISpec.component_guide())
        if salvage_notes:
            lines.append("")
            lines.append("Note: " + " ".join(salvage_notes))
        return "\n".join(lines)

    @staticmethod
    def _loc_path(loc: tuple[int | str, ...]) -> str:
        """Render a pydantic error location as a readable path.

        Union discriminator tags stay in the path on purpose — they name WHICH
        variant the error is about, which is the fact the model needs first.
        """
        parts: list[str] = []
        for element in loc:
            if isinstance(element, int):
                parts.append(f"[{element}]")
            else:
                parts.append(f".{element}" if parts else str(element))
        return "".join(parts) or "(arguments)"

    @staticmethod
    def _offending_node(
        raw: dict[str, Any], loc: tuple[int | str, ...]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Locate the deepest component-carrying dict on an error's path.

        Walks the RAW input (never the validated model — there is none), so
        union tag elements in *loc* that are not real keys are simply skipped.
        Returns ``(None, None)`` when the path never crosses a node, e.g. a
        top-level argument error.
        """
        value: Any = raw
        best: dict[str, Any] | None = None
        best_path: str | None = None
        path_parts: list[str] = []
        for element in loc:
            if (
                isinstance(element, int)
                and isinstance(value, list)
                and -len(value) <= element < len(value)
            ):
                path_parts.append(f"[{element}]")
                value = value[element]
            elif isinstance(element, str) and isinstance(value, dict) and element in value:
                path_parts.append(f".{element}" if path_parts else element)
                value = value[element]
            else:
                continue
            if isinstance(value, dict) and isinstance(value.get("component"), str):
                best = value
                best_path = "".join(path_parts)
        return best, best_path

    @classmethod
    def _corrected_node(
        cls, node: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Rewrite ONE node to its variant's declared shape.

        The model's own values are KEPT wherever they validate field-wise, a
        failing or missing required value becomes a placeholder skeleton
        derived from the declared type, and unknown keys are reported rather
        than silently dropped. Derived from the union, so it cannot drift.
        Returns ``(None, [])`` for a node whose ``component`` names no variant
        — the guide beneath the errors covers that case.
        """
        variants = {m.component_tag(): m for m in GenerativeUISpec._variants()}
        variant = variants.get(node.get("component", ""))
        if variant is None:
            return None, []
        corrected: dict[str, Any] = {"component": variant.component_tag()}
        for name, field in variant.model_fields.items():
            if name == "component":
                continue
            if name in node:
                value = node[name]
                try:
                    TypeAdapter(field.annotation).validate_python(value)
                    corrected[name] = value
                except ValidationError:
                    corrected[name] = cls._skeleton_for(field.annotation)
            elif field.is_required():
                corrected[name] = cls._skeleton_for(field.annotation)
        dropped = [key for key in node if key != "component" and key not in variant.model_fields]
        return corrected, dropped

    @classmethod
    def _skeleton_for(cls, annotation: Any) -> Any:
        """A fill-in placeholder matching the declared shape of one field."""
        ann = GenerativeUISpec._unwrap_annotation(annotation)
        origin = get_origin(ann)
        if origin is Literal:
            return get_args(ann)[0]
        if origin is list:
            inner = GenerativeUISpec._unwrap_annotation(get_args(ann)[0])
            if get_origin(inner) is list:
                return [["..."]]
            if get_origin(inner) in (Union, _pytypes.UnionType):
                return [{"component": "..."}]
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return [
                    {
                        name: "..."
                        for name, field in inner.model_fields.items()
                        if field.is_required()
                    }
                ]
            return ["..."]
        return "..."

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``present_ui`` tool call."""
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        # A `root` emitted as a JSON string — the dominant rejection family on
        # this tool — is decoded through the one shared law before validation;
        # a salvaged valid prefix carries a note the model must see.
        decoded = JsonContainerArguments.decode(PresentUiArgs, raw)
        raw = decoded.arguments
        salvage = decoded.notes
        try:
            args = PresentUiArgs.model_validate(raw)
        except ValidationError as exc:
            # Narrow on purpose, even though the tree is recursive and a
            # pathologically nested input looks like it could exhaust the stack
            # outside this arm: pydantic-core carries its own recursion guard
            # and reports the overflow AS a validation error (verified against a
            # 3,000-deep tree), so nothing is left uncovered. A broader catch
            # would only buy the chance to report a genuine bug in this module
            # to the model as if it were its own malformed arguments.
            return MockSpeaker(content=self._rejection(raw, exc, salvage))

        if args.operation == "replace":
            ui_id = args.ui_id or f"gui-{uuid.uuid4().hex[:8]}"
            panel = GenerativeUISpec(root=args.root)
        else:
            ui_id = args.ui_id or ""
            base = self._panels.get(ui_id)
            if base is None:
                known = ", ".join(self._panels) or "none yet in this run"
                return MockSpeaker(
                    content=(
                        f"ERROR: no panel '{ui_id}' was presented by this run, so "
                        f"operation='{args.operation}' has nothing to modify "
                        f"(panels are addressable only within the run that "
                        f"presented them; known here: {known}). Re-send the full "
                        f"tree with operation=\"replace\" and this ui_id to "
                        f"rebuild it."
                    )
                )
            try:
                if args.operation == "append":
                    panel = base.with_appended(args.root, into=args.target)
                else:
                    panel = base.with_replaced(args.target or "", args.root[0])
            except LookupError:
                ids = ", ".join(base.container_ids())
                hint = ids or "none — give a Card or Stack an `id` first"
                return MockSpeaker(
                    content=(
                        f"ERROR: panel '{ui_id}' has no container with id "
                        f"'{args.target}'. Addressable container ids: {hint}. "
                        f"The panel is unchanged."
                    )
                )
            except ValidationError as exc:
                return MockSpeaker(
                    content=(
                        f"ERROR: applying operation='{args.operation}' to panel "
                        f"'{ui_id}' would break a tree limit; the panel is "
                        f"unchanged: {exc}"
                    )
                )

        payload = GenerativeUIPayload(
            ui_id=ui_id,
            session_id=self._session_id,
            spec=panel.to_wire(),
            alt_text=panel.to_text(),
            summary=args.summary,
        )
        self._emit({"type": GENERATIVE_UI_EVENT, "payload": payload.model_dump()})
        self._panels[ui_id] = panel

        # The result is a receipt AND a working surface: the panel's real
        # measured state plus the ids now addressable, so the next call is
        # informed by what the panel IS rather than by what the model last
        # remembers sending. The id is here because it is the ONE fact the
        # model does not have.
        depth, nodes = panel.measure()
        parts: list[str] = []
        if salvage:
            parts.append("Note: " + " ".join(salvage))
        parts.append(f"Presented UI {ui_id} ({nodes} nodes, depth {depth}).")
        ids = panel.container_ids()
        if ids:
            parts.append("Addressable containers: " + ", ".join(ids) + ".")
        parts.append(
            f'Compose incrementally: ui_id="{ui_id}" with operation="append" '
            "adds nodes (target=<container id> appends inside it); "
            'operation="update" with target replaces that container; '
            'operation="replace" (default) redraws the panel. Omit ui_id to '
            "create a new panel."
        )
        return MockSpeaker(content=" ".join(parts))


__all__ = [
    "GENERATIVE_UI_CAPABILITY",
    "GENERATIVE_UI_EVENT",
    "PRESENT_UI_SCHEMA",
    "PRESENT_UI_TOOL_ID",
    "UI_ID_PATTERN",
    "GenerativeUIPayload",
    "PresentUiArgs",
    "PresentUiTool",
]
