"""RelatedQuestionsRunner — a parallel structured follow-up generator.

The "Related Questions" rail used to depend on the search agent VOLUNTARILY
emitting a top-level ``related_questions`` list on its ``scg_results`` call
(last-write-wins, frequently empty). This runner makes the rail deterministic:
ONE schema-constrained round-trip that, given the query + the synthesized
answer, proposes a few natural follow-ups — fired ALONGSIDE the synthesis
reveal (its own LLM call, off the main answer path) and projected onto
``RunPayload.related_questions``.

It is **not** a second control loop and adds no orchestration: it reuses the
core no-loop :class:`~mewbo_core.structured_synthesis.StructuredSynthesizer`
(one emit + one reask, the same validation machinery as ``/v1/structured``
synthesis mode), so the only new surface is the prompt + the tiny output
schema. Best-effort by contract — a follow-up list is never load-bearing, so
any failure (no LLM, validation, timeout) returns ``[]`` and the caller falls
back to the agent-emitted transcript value; it must NEVER fail a run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.structured_synthesis import StructuredSynthesizer

logging = get_logger(name="api.agentic_search.scg.related_questions")

# The answer can be a long synthesis; cap what we feed the follow-up call so it
# stays cheap (the model only needs the gist to propose next questions).
_ANSWER_CHAR_CAP = 2000


@dataclass
class RelatedQuestionsRunner:
    """Generate follow-up questions in ONE parallel structured round-trip.

    Args:
        model: LiteLLM model name for the call; ``None`` → the configured
            default. The caller picks a cheap brain (this is a trivial task).
        max_questions: Upper bound on the suggestions returned.
        synthesizer: Injectable for tests (stub the LLM seam). Defaults to a
            fresh ungrounded :class:`StructuredSynthesizer` per call.
    """

    model: str | None = None
    max_questions: int = 5
    synthesizer: StructuredSynthesizer | None = None

    def run(self, query: str, answer: str) -> list[str]:
        """Return up to :attr:`max_questions` follow-ups, or ``[]`` on any failure.

        Synchronous (the settle worker is a plain thread): drives the async
        synthesizer via :func:`asyncio.run`. Never raises — a follow-up list is
        decorative, so a failed call degrades to an empty list, not a failed run.
        """
        if not query.strip() or not answer.strip():
            return []
        synthesizer = self.synthesizer or StructuredSynthesizer(model_name=self.model)
        try:
            payload, _ = asyncio.run(
                synthesizer.synthesize(self._prompt(query, answer), self._schema())
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail the run
            logging.debug("related-questions generation failed: {}", exc)
            return []
        return self._clean(payload)

    def _schema(self) -> dict[str, Any]:
        """The emit schema — a bounded array of follow-up question strings."""
        return {
            "type": "object",
            "properties": {
                "related_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": self.max_questions,
                }
            },
            "required": ["related_questions"],
        }

    def _prompt(self, query: str, answer: str) -> str:
        """The human-turn instruction (the system turn is the force-emit directive)."""
        trimmed = answer.strip()[:_ANSWER_CHAR_CAP]
        return (
            "A user ran a search and received the answer below. Propose up to "
            f"{self.max_questions} concise, specific follow-up questions a curious "
            "user would naturally ask next. Each must stand on its own (no "
            '"it"/"that" referring to the answer), stay on the same topic, and add '
            "a NEW angle — never restate the original query. Emit them via the "
            "structured tool.\n\n"
            f"SEARCH QUERY:\n{query.strip()}\n\nANSWER:\n{trimmed}"
        )

    def _clean(self, payload: Any) -> list[str]:
        """Project the validated emit onto a deduped, capped list of strings."""
        raw = payload.get("related_questions") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= self.max_questions:
                break
        return out


__all__ = ["RelatedQuestionsRunner"]
