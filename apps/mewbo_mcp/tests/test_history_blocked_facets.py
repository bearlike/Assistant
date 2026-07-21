"""A blocked run must read as blocked at every MCP surface, with the code.

The loop leaves ``done_reason`` at ``"completed"`` for a run that hit a
user-actionable wall (a credential, a network path, a permission, a quota) and
carries the real fact in ``blocked_code``. A surface that forwards ``done_reason``
but drops ``blocked_code`` tells an external agent the run succeeded — higher
stakes than a human misreading a badge, because the agent then acts on it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_mcp import tools
from mewbo_mcp.timeline import build_timeline


def run(coro):
    return asyncio.run(coro)


def _user(text: str, ts: str) -> dict[str, Any]:
    return {"type": "user", "ts": ts, "payload": {"text": text}}


def _assistant(text: str, ts: str) -> dict[str, Any]:
    return {"type": "assistant", "ts": ts, "payload": {"text": text}}


def _completion(reason: str, ts: str, **payload: Any) -> dict[str, Any]:
    return {"type": "completion", "ts": ts, "payload": {"done_reason": reason, **payload}}


def _events(*events: dict[str, Any], **meta: Any) -> dict[str, Any]:
    return {"events": list(events), **meta}


def _assertion(reason: str, ts: str, detail: str = "") -> dict[str, Any]:
    return {"type": "outcome_assertion", "ts": ts, "payload": {"reason": reason, "detail": detail}}


def _blocked_completed_session() -> dict[str, Any]:
    """A run that hit a repo-access wall but the loop stamped ``completed``."""
    return _events(
        _user("index the repo", "t0"),
        _assistant("(Run interrupted by error: cannot reach origin)", "t1"),
        _completion("completed", "t2", blocked_code="repo_access"),
        status="blocked",
        blocked_code="repo_access",
        recoverable=True,
    )


def _unmet_goal_session() -> dict[str, Any]:
    """A wiki index that ended CLEAN yet a session-end hook says never completed."""
    return _events(
        _user("index the repo", "t0"),
        _assistant("Indexed what I could.", "t1"),
        _completion("completed", "t2"),
        _assertion("index_incomplete", "t3", detail="0 of 42 pages written"),
        status="unmet_goal",
    )


# ---------------------------------------------------------------------------
# Turn reconstruction — the raw fact, before any tier projects it
# ---------------------------------------------------------------------------


def test_blocked_completion_overrides_the_laundered_done_reason():
    turns = build_timeline(_blocked_completed_session()["events"])

    assert turns[0].blocked_code == "repo_access"
    # The derived ``blocked`` status wins over the loop's laundered "completed".
    assert turns[0].done_reason == "blocked"
    assert "repo_access" in turns[0].error


def test_unrecognised_blocked_code_never_widens_the_vocabulary():
    """Guarding against core's canonical set is what keeps the outcome honest."""
    events = [
        _user("go", "t0"),
        _completion("completed", "t1", blocked_code="not_a_real_code"),
    ]

    turns = build_timeline(events)

    assert turns[0].blocked_code == ""
    assert turns[0].done_reason == "completed"


# ---------------------------------------------------------------------------
# unmet_goal — a clean completion an outcome_assertion contradicts
# ---------------------------------------------------------------------------


def test_outcome_assertion_promotes_a_clean_completion_to_unmet_goal():
    turns = build_timeline(_unmet_goal_session()["events"])

    # The derived ``unmet_goal`` overrides the loop's clean "completed".
    assert turns[0].done_reason == "unmet_goal"
    assert turns[0].unmet_goal_reason == "index_incomplete"
    assert turns[0].error == "0 of 42 pages written"


def test_a_completion_with_no_assertion_is_unchanged():
    events = [_user("q", "t0"), _assistant("a", "t1"), _completion("completed", "t2")]

    turns = build_timeline(events)

    assert turns[0].done_reason == "completed"
    assert turns[0].unmet_goal_reason == ""


def test_only_a_clean_completion_is_promoted_not_a_failure():
    """A failed run is already honest; an assertion must not relabel it."""
    events = [
        _user("q", "t0"),
        _completion("error", "t1", error="boom"),
        _assertion("index_incomplete", "t2"),
    ]

    turns = build_timeline(events)

    assert turns[0].done_reason == "error"
    assert turns[0].unmet_goal_reason == ""


def test_blocked_outranks_unmet_goal():
    """Both signals present: blocked is the more specific, actionable one."""
    events = [
        _user("q", "t0"),
        _completion("completed", "t1", blocked_code="network"),
        _assertion("index_incomplete", "t2"),
    ]

    turns = build_timeline(events)

    assert turns[0].done_reason == "blocked"
    assert turns[0].blocked_code == "network"
    assert turns[0].unmet_goal_reason == ""


def test_a_stray_assertion_before_the_completion_is_ignored():
    """The hook always appends AFTER the terminal; a pre-completion one is noise."""
    events = [
        _user("q", "t0"),
        _assertion("index_incomplete", "t1"),
        _completion("completed", "t2"),
    ]

    turns = build_timeline(events)

    assert turns[0].done_reason == "completed"
    assert turns[0].unmet_goal_reason == ""


def test_a_malformed_assertion_is_dropped_not_fatal():
    """A stored record is a trust boundary; core's model validates it."""
    events = [
        _user("q", "t0"),
        _completion("completed", "t1"),
        {"type": "outcome_assertion", "ts": "t2", "payload": {"detail": "no reason field"}},
    ]

    turns = build_timeline(events)

    assert turns[0].done_reason == "completed"
    assert turns[0].unmet_goal_reason == ""


def test_turns_tier_surfaces_unmet_goal_and_its_reason(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _unmet_goal_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))

    row = out["turns"][0]
    assert row["done_reason"] == "unmet_goal"
    assert row["unmet_goal_reason"] == "index_incomplete"
    assert row["error"] == "0 of 42 pages written"


def test_full_tier_surfaces_unmet_goal_and_its_reason(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _unmet_goal_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert out["done_reason"] == "unmet_goal"
    assert out["unmet_goal_reason"] == "index_incomplete"


def test_untroubled_turn_carries_no_unmet_goal_reason(fake_rest):
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _events(_user("q", "t0"), _assistant("a", "t1"), _completion("completed", "t2")),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))

    assert "unmet_goal_reason" not in out["turns"][0]


# ---------------------------------------------------------------------------
# turns / full tiers
# ---------------------------------------------------------------------------


def test_turns_tier_surfaces_blocked_and_its_code(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _blocked_completed_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))

    row = out["turns"][0]
    assert row["done_reason"] == "blocked"
    assert row["blocked_code"] == "repo_access"


def test_full_tier_surfaces_the_blocked_code(fake_rest):
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _blocked_completed_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="full", turn=1))

    assert out["done_reason"] == "blocked"
    assert out["blocked_code"] == "repo_access"


def test_untroubled_turn_carries_no_blocked_code(fake_rest):
    fake = fake_rest.on(
        "GET",
        "/api/sessions/s1/events",
        _events(_user("q", "t0"), _assistant("a", "t1"), _completion("completed", "t2")),
    )
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="turns"))

    assert "blocked_code" not in out["turns"][0]


# ---------------------------------------------------------------------------
# overview tier — the four honest-outcome facets
# ---------------------------------------------------------------------------


def test_overview_reconstructs_blocked_code_and_forwards_recoverable(fake_rest):
    """blocked_code rides the transcript now; recoverable rides the meta."""
    fake = fake_rest.on("GET", "/api/sessions/s1/events", _blocked_completed_session())
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))

    assert out["status"] == "blocked"
    assert out["blocked_code"] == "repo_access"
    assert out["recoverable"] is True


def test_overview_forwards_failure_facets_once_the_api_carries_them(fake_rest):
    """failure_reason/models_tried ride the /events meta when the API forwards them."""
    payload = _events(
        _user("q", "t0"),
        _assistant("(Run interrupted by error: overloaded)", "t1"),
        _completion("error", "t2", error="all models failed"),
        status="failed",
        recoverable=True,
        failure_reason="RateLimitError",
        models_tried=["claude-sonnet-5", "gpt-5.2"],
    )
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))

    assert out["failure_reason"] == "RateLimitError"
    assert out["models_tried"] == ["claude-sonnet-5", "gpt-5.2"]
    assert out["recoverable"] is True


def test_overview_of_a_clean_session_carries_no_failure_facets(fake_rest):
    payload = _events(
        _user("q", "t0"),
        _assistant("a", "t1"),
        _completion("completed", "t2"),
        status="completed",
        recoverable=False,
    )
    fake = fake_rest.on("GET", "/api/sessions/s1/events", payload)
    out = run(tools.SessionTools(fake.client()).history(session_id="s1", level="overview"))

    assert out["recoverable"] is False
    assert "blocked_code" not in out
    assert "failure_reason" not in out
    assert "models_tried" not in out


# ---------------------------------------------------------------------------
# list_sessions — pure forwarding off the summary row
# ---------------------------------------------------------------------------


def _sessions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sessions": rows}


def test_list_sessions_forwards_all_four_facets(fake_rest):
    row = {
        "session_id": "s1",
        "title": "index",
        "status": "blocked",
        "done_reason": "completed",
        "created_at": "t0",
        "recoverable": True,
        "blocked_code": "quota_exceeded",
        "failure_reason": "RateLimitError",
        "models_tried": ["claude-sonnet-5"],
        "context": {},
    }
    fake = fake_rest.on("GET", "/api/sessions", _sessions([row]))
    out = run(tools.SessionTools(fake.client()).list_sessions())

    shaped = out["sessions"][0]
    assert shaped["status"] == "blocked"
    assert shaped["blocked_code"] == "quota_exceeded"
    assert shaped["failure_reason"] == "RateLimitError"
    assert shaped["models_tried"] == ["claude-sonnet-5"]
    assert shaped["recoverable"] is True


def test_list_sessions_untroubled_row_is_unchanged(fake_rest):
    row = {
        "session_id": "s2",
        "title": "chat",
        "status": "completed",
        "done_reason": "completed",
        "created_at": "t0",
        "recoverable": False,
        "context": {},
    }
    fake = fake_rest.on("GET", "/api/sessions", _sessions([row]))
    out = run(tools.SessionTools(fake.client()).list_sessions())

    shaped = out["sessions"][0]
    assert shaped["recoverable"] is False
    for absent in ("blocked_code", "failure_reason", "models_tried"):
        assert absent not in shaped
