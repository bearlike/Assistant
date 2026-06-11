"""Retrieval-only fast grounded synthesis — ``StructuredSynthesizer``.

A single async round-trip (no :class:`~mewbo_core.tool_use_loop.ToolUseLoop`,
no :class:`~mewbo_core.orchestrator.Orchestrator`) that:

1. Calls an optional :class:`GroundingProvider` to fetch :class:`Citation`
   records for the query.
2. Builds an ``emit_result`` tool from the caller's JSON Schema (reusing
   :func:`~mewbo_core.structured_response.build_emit_schema` and
   :class:`~mewbo_core.structured_response.EmitStructuredResponseTool` — DRY,
   no duplicated validation logic).
3. Issues ONE ``model.bind_tools([emit.schema]).ainvoke(messages)`` call.
4. Handles the tool call via the real ``emit.handle(action_step)`` — which
   validates against the schema.  On a validation failure it feeds the error
   back as a reask message and does ONE more ``ainvoke``.  A second failure
   raises :class:`~mewbo_core.structured_response.StructuredResponseError`.

p95 latency target: < 1.5 s for a small schema + modest workspace (single
model round-trip — no planner hops, no agent hypervisor, no session store).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from mewbo_core.common import get_logger
from mewbo_core.components import langfuse_invoke_config
from mewbo_core.config import get_config_value
from mewbo_core.llm import build_chat_model
from mewbo_core.structured_response import (
    DEFAULT_MAX_FAILURES,
    FORCE_EMIT_DIRECTIVE,
    EmitStructuredResponseTool,
    StructuredResponseError,
    build_emit_schema,
)

if TYPE_CHECKING:
    pass

logging = get_logger(name="core.structured_synthesis")


# ---------------------------------------------------------------------------
# Wire model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """A single grounded source record returned by :class:`GroundingProvider`.

    Fields are kept deliberately minimal — the wire representation only needs
    enough to let the LLM attribute its answer and let the caller render a
    footnote / expandable panel.
    """

    id: str
    kind: str
    snippet: str
    score: float
    source: str


# ---------------------------------------------------------------------------
# DI seam — keeps ``mewbo_core`` graph-free
# ---------------------------------------------------------------------------


class GroundingProvider(Protocol):
    """Injectable retrieval back-end.

    ``mewbo_graph.wiki``-based retrieval (:class:`WikiGroundingProvider`) is
    the production implementation; tests inject a fake.  Core never imports the
    concrete class — the Protocol is the only coupling point.
    """

    def search(self, slug: str, query: str, *, k: int = 8) -> list[Citation]:
        """Return up to *k* ranked :class:`Citation` records for *query*."""
        ...


# ---------------------------------------------------------------------------
# Compact grounded-context formatter
# ---------------------------------------------------------------------------

_GROUNDED_HEADER = (
    "## Grounded context\n\n"
    "The following snippets were retrieved from the workspace and MUST be "
    "used to populate the structured answer.  Cite only what is present here.\n\n"
)


def _format_citations(citations: list[Citation]) -> str:
    """Format citations into a compact block for the system message."""
    if not citations:
        return ""
    lines = [_GROUNDED_HEADER]
    for i, c in enumerate(citations, start=1):
        lines.append(
            f"[{i}] ({c.kind}) score={c.score:.3f} — {c.snippet.strip()}\n"
        )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Atomic synthesizer
# ---------------------------------------------------------------------------

_SYNTHESIS_SESSION_ID = "(synthesis)"


class StructuredSynthesizer:
    """One-shot schema-constrained synthesis with optional retrieval grounding.

    State is constructor-injected; the class has no mutable instance fields
    beyond what the constructor sets — each :meth:`synthesize` call is
    independent (safe for concurrent use with different ``await`` calls).

    Args:
        model_name: LiteLLM model name (e.g. ``"openai/gpt-4o"``).  Falls back
            to the configured default when ``None``.
        grounding_provider: Optional :class:`GroundingProvider`.  When
            ``None`` (or when *workspace* is not given) the synthesizer runs
            without retrieval context.
        max_failures: Maximum schema-validation failures before giving up.
            Matches :data:`~mewbo_core.structured_response.DEFAULT_MAX_FAILURES`.
    """

    def __init__(
        self,
        *,
        model_name: str | None = None,
        grounding_provider: GroundingProvider | None = None,
        max_failures: int = DEFAULT_MAX_FAILURES,
    ) -> None:
        """Initialise the synthesizer with DI-injected model name and grounding."""
        self.model_name = model_name
        self.grounding_provider = grounding_provider
        self.max_failures = max_failures

    async def synthesize(
        self,
        query: str,
        schema: dict[str, object],
        *,
        workspace: str | None = None,
        k: int = 8,
    ) -> tuple[object, list[Citation]]:
        """Run one (or one-reask) round-trip and return ``(payload, citations)``.

        Args:
            query: Natural-language question / instruction.
            schema: JSON Schema for the desired output.
            workspace: Optional wiki slug.  When provided together with
                :attr:`grounding_provider`, citations are fetched before the
                LLM call.
            k: Maximum citations to retrieve (passed through to
                :meth:`GroundingProvider.search`).

        Returns:
            A ``(payload, citations)`` tuple where *payload* validates against
            *schema* and *citations* is the (possibly empty) list of retrieved
            sources.

        Raises:
            :class:`~mewbo_core.structured_response.StructuredResponseError`:
                When the model fails to emit a valid structured result after the
                reask cap.
        """
        # 1. Retrieval grounding ------------------------------------------------
        citations: list[Citation] = []
        grounded_context = ""
        if workspace and self.grounding_provider is not None:
            try:
                citations = self.grounding_provider.search(workspace, query, k=k)
                grounded_context = _format_citations(citations)
            except Exception as exc:  # noqa: BLE001 — grounding is best-effort
                logging.warning(
                    "GroundingProvider.search failed for workspace={}: {}",
                    workspace,
                    exc,
                )

        # 2. Build the emit SessionTool (reuse schema validation machinery) -----
        emit = EmitStructuredResponseTool(
            session_id=_SYNTHESIS_SESSION_ID,
            schema=schema,
            max_failures=self.max_failures,
        )
        emit_schema = build_emit_schema(schema)

        # 3. Build model + messages -------------------------------------------
        # Resolve None → configured default (build_chat_model requires a str).
        model_name = self.model_name or str(
            get_config_value("llm", "default_model", default="") or ""
        )
        model = build_chat_model(model_name=model_name)
        bound_model = model.bind_tools([emit_schema])

        system_content = FORCE_EMIT_DIRECTIVE
        if grounded_context:
            system_content = grounded_context + "\n\n" + system_content

        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=query),
        ]

        # Attach the Langfuse CallbackHandler so this generation EXPORTS (the
        # surrounding ``langfuse_session_context`` only propagates attributes; with
        # no observation created inside, nothing lands in the trace — #87). Reads
        # the contextvar session/trace the recorder opened, so the generation joins
        # the session-grouped trace. ``{}`` (no-op config) when Langfuse is off.
        invoke_config = langfuse_invoke_config(
            user_id="mewbo-structured-fast",
            session_id="structured-fast",
            trace_name="mewbo-structured-fast",
        )

        # 4. First model call ---------------------------------------------------
        response = await bound_model.ainvoke(messages, config=invoke_config or None)
        result = await self._handle_response(response, emit)
        if result is not None:
            return result, citations

        # 5. One reask — feed the validation error back and retry ---------------
        # emit.handle returned a reask MockSpeaker; add it as assistant context
        # and drive once more.  We reconstruct from messages so the model sees
        # the validation feedback.
        if emit.failed:
            raise StructuredResponseError(
                f"Schema validation failed after {self.max_failures} attempts."
            )

        # The first response had a tool call that failed validation.
        # We feed back the reask message.
        reask_content = (
            "Your previous response did not validate against the schema. "
            "Correct the field errors and call emit_result again."
        )
        messages = messages + [
            response,
            HumanMessage(content=reask_content),
        ]
        response2 = await bound_model.ainvoke(messages, config=invoke_config or None)
        result2 = await self._handle_response(response2, emit)
        if emit.failed:
            raise StructuredResponseError(
                f"Schema validation failed after {self.max_failures} attempts."
            )
        if result2 is None:
            raise StructuredResponseError(
                "Run produced no structured output (the model never called emit_result)."
            )
        return result2, citations

    async def _handle_response(
        self,
        response: object,
        emit: EmitStructuredResponseTool,
    ) -> object | None:
        """Extract and drive the emit tool call from *response*.

        Returns the validated payload when emit succeeds, ``None`` otherwise
        (either validation failed → reask needed, or no tool call was found).
        """
        from mewbo_core.classes import ActionStep

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            # Model answered in prose — no tool call found.
            logging.debug(
                "StructuredSynthesizer: model returned no tool call; will reask."
            )
            return None

        # Find the emit_result call (there may be only one anyway).
        emit_call = None
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name == "emit_result":
                emit_call = tc
                break

        if emit_call is None:
            logging.debug(
                "StructuredSynthesizer: no emit_result call found in tool_calls."
            )
            return None

        args = (
            emit_call.get("args")
            if isinstance(emit_call, dict)
            else getattr(emit_call, "args", {})
        )
        action_step = ActionStep(
            tool_id="emit_result",
            operation="set",
            tool_input=args or {},
        )

        await emit.handle(action_step)
        if emit.payload is not None:
            return emit.payload
        # Validation failed — reask message was returned; payload is None.
        return None


__all__ = [
    "Citation",
    "GroundingProvider",
    "StructuredSynthesizer",
]
