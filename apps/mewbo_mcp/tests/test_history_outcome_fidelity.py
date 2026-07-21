"""The per-turn tier must report the outcome the run actually had.

The defect: ``get_session_history(level="turns")`` returned ``done_reason: null``
for every turn of sessions the ``overview`` tier correctly reported as failed.
An erased failure is worse than a missing one — it reads as a clean turn, which
is exactly backwards for the failure analysis this tier exists to serve.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_mcp import tools


def run(coro):
    return asyncio.run(coro)


def _user(text: str, ts: str) -> dict[str, Any]:
    return {"type": "user", "ts": ts, "payload": {"text": text}}


def _assistant(text: str, ts: str) -> dict[str, Any]:
    return {"type": "assistant", "ts": ts, "payload": {"text": text}}


def _completion(reason: str, ts: str, **payload: Any) -> dict[str, Any]:
    return {"type": "completion", "ts": ts, "payload": {"done_reason": reason, **payload}}


def _llm_call(ts: str, tokens_in: int, tokens_out: int, depth: int = 0) -> dict[str, Any]:
    return {
        "type": "llm_call_end",
        "ts": ts,
        "payload": {"depth": depth, "input_tokens": tokens_in, "output_tokens": tokens_out},
    }


def _events(*events: dict[str, Any], **meta: Any) -> dict[str, Any]:
    return {"events": list(events), **meta}


def _turns(fake_rest, payload: dict[str, Any]) -> list[dict[str, Any]]:
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))
    return out["turns"]


def test_ordinary_completed_turn_reports_its_outcome(fake_rest):
    """The dominant shape: a final assistant event, THEN the completion."""
    rows = _turns(
        fake_rest,
        _events(
            _user("q1", "t0"),
            _assistant("a1", "t1"),
            _completion("completed", "t2"),
        ),
    )

    assert rows[0]["done_reason"] == "completed"
    assert rows[0]["assistant_text"] == "a1"
    assert "error" not in rows[0]


def test_failed_turn_no_longer_reads_as_a_clean_turn(fake_rest):
    """A failure landing on the orchestrator's placeholder closure."""
    rows = _turns(
        fake_rest,
        _events(
            _user("index the repo", "t0"),
            _assistant("(Run interrupted by error: upstream died)", "t1"),
            _completion("error", "t2", error="upstream died"),
        ),
    )

    assert rows[0]["done_reason"] == "error"
    assert rows[0]["error"] == "upstream died"


def test_failure_beside_real_prose_is_reported_without_destroying_the_answer(fake_rest):
    """The failure row carries no turn metadata, so it used to be skipped whole."""
    rows = _turns(
        fake_rest,
        _events(
            _user("q", "t0"),
            _assistant("Here is the real answer.", "t1"),
            _completion("error", "t2", error="run died after the answer"),
        ),
    )

    assert rows[0]["done_reason"] == "error"
    assert rows[0]["error"] == "run died after the answer"
    assert rows[0]["assistant_text"] == "Here is the real answer."


def test_halted_outcome_is_reported_verbatim(fake_rest):
    """A halt is not a failure to the assembler, and certainly not a success."""
    rows = _turns(
        fake_rest,
        _events(
            _user("go", "t0"),
            _assistant("(Run stopped: no progress)", "t1"),
            _completion("halted_no_progress", "t2"),
        ),
    )

    assert rows[0]["done_reason"] == "halted_no_progress"


def test_interrupted_turn_reports_the_synthesised_outcome(fake_rest):
    """No completion event exists for it, so "interrupted" is the only truth."""
    rows = _turns(
        fake_rest,
        _events(
            _user("long thing", "t0"),
            _user("what happened?", "t5"),
            _assistant("cut short", "t6"),
        ),
    )

    assert rows[0]["done_reason"] == "interrupted"


def test_each_turn_keeps_its_own_outcome(fake_rest):
    """Attribution follows the prompt boundary — outcomes must not bleed."""
    rows = _turns(
        fake_rest,
        _events(
            _user("q1", "t0"),
            _assistant("a1", "t1"),
            _completion("completed", "t2"),
            _user("q2", "t3"),
            _assistant("(Run interrupted by error: boom)", "t4"),
            _completion("error", "t5", error="boom"),
        ),
    )

    assert [r["done_reason"] for r in rows] == ["completed", "error"]
    assert "error" not in rows[0]
    assert rows[1]["error"] == "boom"


def test_running_turn_has_no_outcome_yet(fake_rest):
    """A live turn must not be handed a borrowed reason."""
    rows = _turns(fake_rest, _events(_user("q1", "t0")))

    assert rows[0]["done_reason"] is None


def test_turns_tier_agrees_with_the_overview_tier(fake_rest):
    """The two tiers disagreeing is the bug; pin them together."""
    payload = _events(
        _user("q", "t0"),
        _assistant("(Run interrupted by error: upstream died)", "t1"),
        _completion("error", "t2", error="upstream died"),
        status="failed",
        done_reason="error",
    )
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)
    session = tools.SessionTools(fake.client())

    overview = run(session.history(session_id="s1", level="overview"))
    turns = run(session.history(session_id="s1", level="turns"))

    assert overview["done_reason"] == "error"
    assert turns["turns"][-1]["done_reason"] == overview["done_reason"]


def test_error_text_is_bounded_at_the_turns_tier(fake_rest):
    """A classified detail can run to thousands of characters; the list tier
    stays cheap or it stops being the cheap tier."""
    rows = _turns(
        fake_rest,
        _events(
            _user("q", "t0"),
            _assistant("(Run interrupted by error: x)", "t1"),
            _completion("error", "t2", error="z" * 4000),
        ),
    )

    assert len(rows[0]["error"]) == tools.SessionTools.TURN_TEXT_TRUNC + 1


def test_full_tier_carries_the_outcome_and_the_error(fake_rest):
    payload = _events(
        _user("q", "t0"),
        _assistant("(Run interrupted by error: upstream died)", "t1"),
        _completion("error", "t2", error="upstream died"),
    )
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)

    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert out["done_reason"] == "error"
    assert out["error"] == "upstream died"


def test_overview_separates_context_pressure_from_billed_input(fake_rest):
    """Two different quantities: the peak per turn, and every call's input.

    Reporting only the first under a "total" name is what read as a metering
    discrepancy against the per-call event log.
    """
    payload = _events(
        _user("q", "t0"),
        _llm_call("t1", 100, 10),
        _llm_call("t2", 400, 10),
        _llm_call("t3", 900, 10),
        _assistant("a", "t4"),
        _completion("completed", "t5"),
    )
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)

    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))

    assert out["total_input_tokens"] == 900  # peak — context pressure
    assert out["total_billed_input_tokens"] == 1400  # sum of every call's input
    assert out["total_output_tokens"] == 30
