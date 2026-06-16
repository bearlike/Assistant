"""``scg_results`` SessionTool — the search agents' result-emit step.

A search RUN's terminal is an NL ``AnswerSynthesis`` (the cited answer), but the
console's ResultsPanel also renders a list of normalized result CARDS — the
discrete hits the answer is built from. Before this tool the agents had no way
to surface those cards, so a fast-tier run that inlined all work (no probe
sub-agents) produced ``results: []`` even with a real answer.

EVERY search agent emits through it, ONCE each (#95 root, #102 probes): each
``scg-path-probe`` emits the hits its pathway grounded right before its
evidence block, and the ``scg-search`` root emits only the hits it grounded
inline (the fast-tier root-inline path) right before the synthesis. The tool
itself is **transcript-as-transport**: it only VALIDATES the entries and returns
``{ok, count}`` — it never writes the api run store (the library may not import
the app). A child loop inherits the parent session's event logger, so a probe's
emit rides the SAME session transcript stamped with the probe's ``agent_id``;
the API projects each ``tool_result`` transcript event into the run's
``result`` events + ``RunPayload.results`` at stream/settle time (mirroring how
the probe ``sub_agent`` events become the trace), salting result ids by the
emitting probe so concurrent emitters never collide.

Thin wrapper over a Pydantic args model validated at definition
(``ConfigDict(extra="forbid")``) — the sibling shape of ``scg_route`` /
``scg_memory``. No SCG core dependency: it validates and echoes, so it works on
any install (no ``mewbo-graph`` extra needed for the emit itself).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mewbo_core.common import MockSpeaker, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_graph.plugins.scg._core import (
    SessionToolBase,
    err_result,
    ok_result,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

# The result kinds the console's ResultsPanel filter rail knows how to render —
# kept in lockstep with the api wire ``ResultKindLiteral`` (schemas.py). A new
# kind must be added on BOTH sides or the api drops the entry on validation.
ResultKind = Literal["docs", "code", "threads", "design", "tickets", "web"]

# Hard cap on the entries one emit may carry — a search answer cites a focused
# set, not a firehose. Mirrors the probe-count fan-out discipline (route ``k``).
_MAX_RESULTS = 50

# ``meta`` caps — the structured-fact sidecar a card carries (stars/version/year…).
# A card's facts are a focused fingerprint, not an arbitrary blob: cap the key
# COUNT, the key LENGTH, and string-value LENGTH so a hallucinated payload can't
# bloat the run wire. Validate-only tool ⇒ REJECT (the model retries) rather than
# silently truncate — a rejected emit is a clearer signal than a mangled one.
_MAX_META_KEYS = 12
_MAX_META_KEY_CHARS = 40
_MAX_META_VALUE_CHARS = 200

# ``related_questions`` caps — a SHORT run-level follow-up list (the register that
# replaces conversational "If you want, I can…" offers in the synthesis).
_MAX_RELATED_QUESTIONS = 5
_MAX_RELATED_QUESTION_CHARS = 140

# The scalar types a ``meta`` value may hold — a structured FACT, never nested
# structure. Mirrors the api wire (``meta`` projects verbatim onto the card).
MetaValue = str | int | float | bool


class ScgResultEntry(BaseModel):
    """One normalized result card the search answer is built from.

    Each entry MUST be grounded in a connector return — ``source`` is the SCG
    source id it came from (the provenance anchor), ``relevance`` ranks it within
    the answer, ``confidence`` (when defensible) reports how sure the agent is the
    entry actually answers the query. Omit any entry you cannot ground in a real
    connector result — an unearned card is worse than a missing one.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, description="The result's display title.")
    source: str = Field(
        min_length=1,
        description="The SCG source id this hit came from (provenance anchor).",
    )
    snippet: str = Field(
        default="", description="A short grounded excerpt/summary of the hit."
    )
    url: str | None = Field(
        default=None, description="A link to the hit, when the source provides one."
    )
    kind: ResultKind = Field(
        default="docs",
        description="The result kind for the ResultsPanel filter rail.",
    )
    relevance: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="How relevant this hit is to the query (0..1).",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How sure the entry answers the query (0..1), when defensible.",
    )
    meta: dict[str, MetaValue] | None = Field(
        default=None,
        description=(
            "Structured per-card FACTS the connector returned, rendered as the "
            "card's footer — every quantitative/enumerable value belongs here, "
            "NOT in the prose ``snippet``. PROPOSE the facts that make this hit "
            "read richer; vocab is OPEN (use the connector's own field names): "
            "repo→stars/forks/language/updated; package→version/downloads/"
            "license; paper→authors/year/venue/citations; model/dataset→"
            "downloads/likes; issue/PR/ticket→state or status/assignee/priority/"
            "comments/updated; document→size/words/updated. A ``state``/"
            "``status`` value renders as a colour-coded badge; a byte ``size`` "
            "as a humanized size; dates as relative time. "
            f"≤{_MAX_META_KEYS} keys; key ≤{_MAX_META_KEY_CHARS} chars; string "
            f"value ≤{_MAX_META_VALUE_CHARS} chars; scalar values only."
        ),
    )

    @field_validator("meta")
    @classmethod
    def _check_meta(
        cls, value: dict[str, MetaValue] | None
    ) -> dict[str, MetaValue] | None:
        """Reject an over-budget ``meta`` (validate-only tool ⇒ the model retries)."""
        if value is None:
            return None
        if len(value) > _MAX_META_KEYS:
            raise ValueError(
                f"meta has {len(value)} keys (max {_MAX_META_KEYS})"
            )
        for key, val in value.items():
            if len(key) > _MAX_META_KEY_CHARS:
                raise ValueError(
                    f"meta key {key!r} exceeds {_MAX_META_KEY_CHARS} chars"
                )
            # ``bool`` is a subclass of ``int`` — both are valid scalars; only
            # cap the LENGTH of genuine strings (numbers are inherently bounded).
            if isinstance(val, str) and len(val) > _MAX_META_VALUE_CHARS:
                raise ValueError(
                    f"meta[{key!r}] string exceeds {_MAX_META_VALUE_CHARS} chars"
                )
        return value


class ScgResultsArgs(BaseModel):
    """Emit the discrete result cards backing the search answer.

    Call this ONCE, right before you write your final text (a probe's evidence
    block / the root's synthesis), with the entries directly relevant to the
    query — each grounded in a connector return YOU saw, with its ``source``
    provenance + ``relevance`` (and ``confidence`` where defensible). Omit
    anything you cannot ground in a real connector result. The console renders
    these as result cards beside the cited answer. This does NOT end the run —
    finish by writing your evidence block / synthesis as normal.

    ``related_questions`` (root only) replaces conversational "If you want, I
    can…" offers in the synthesis prose: put 2–4 natural follow-up queries here
    and keep the final answer offer-free.
    """

    model_config = ConfigDict(extra="forbid")

    results: list[ScgResultEntry] = Field(
        default_factory=list,
        max_length=_MAX_RESULTS,
        description="The grounded result cards (≤50), most relevant first.",
    )
    related_questions: list[str] | None = Field(
        default=None,
        max_length=_MAX_RELATED_QUESTIONS,
        description=(
            "Run-level follow-up queries (≤5, each ≤140 chars) the console "
            "surfaces as suggestions — the structured home for follow-up offers "
            "the synthesis must NOT phrase conversationally."
        ),
    )

    @field_validator("related_questions")
    @classmethod
    def _check_related_questions(cls, value: list[str] | None) -> list[str] | None:
        """Reject an over-long follow-up (validate-only tool ⇒ the model retries)."""
        if value is None:
            return None
        for q in value:
            if len(q) > _MAX_RELATED_QUESTION_CHARS:
                raise ValueError(
                    f"related question exceeds {_MAX_RELATED_QUESTION_CHARS} chars: {q[:40]!r}…"
                )
        return value


class ScgResultsTool(SessionToolBase):
    """SessionTool: validate + echo the search answer's result cards.

    Transcript-as-transport — the handler validates the entries and returns
    ``{ok, count}``; the api projects the ``tool_result`` event into the run's
    ``result`` events + ``RunPayload.results``. It writes NOTHING (no run store,
    no SCG mutation), so it is ``concurrency_safe`` and carries no SCG-core
    dependency.
    """

    tool_id = "scg_results"
    modes = DEFAULT_SESSION_TOOL_MODES
    # Pure validate-and-echo: no mutation, safe under the loop's partitioning.
    concurrency_safe = True
    schema = pydantic_to_openai_tool(ScgResultsArgs, name="scg_results")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the emitted entries and echo ``{ok, count}`` (no store write)."""
        try:
            args = ScgResultsArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return err_result("validation", str(ve))
        return ok_result({"ok": True, "count": len(args.results)})


__all__ = ["ScgResultEntry", "ScgResultsArgs", "ScgResultsTool"]
