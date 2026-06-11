#!/usr/bin/env python3
"""Regression tests for the agent-pickup CI workflow contract.

The workflow runs on BOTH GitHub Actions and Gitea Actions and posts to
``POST /api/automation/vcs-pickup`` — these tests pin the trigger set, the
bot-login guard expression (incl. the Gitea assignee fallbacks and the
self-comment exclusion), the API call contract, the payload keys, the
read-only permissions block, and the injection-safety rule that event payload
values only flow through ``env:`` (never inline ``${{ github.event.* }}``
inside ``run:`` scripts).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENT_PICKUP_WORKFLOW = ROOT / ".github" / "workflows" / "agent-pickup.yml"


def _load_workflow() -> dict:
    with AGENT_PICKUP_WORKFLOW.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _workflow_text() -> str:
    return AGENT_PICKUP_WORKFLOW.read_text(encoding="utf-8")


def _run_scripts(workflow: dict) -> dict[str, str]:
    """Map ``"<job>/<step name>"`` → the step's ``run`` script."""
    scripts: dict[str, str] = {}
    for job_id, job in workflow["jobs"].items():
        for idx, step in enumerate(job.get("steps", [])):
            if "run" in step:
                scripts[f"{job_id}/{step.get('name', idx)}"] = step["run"]
    return scripts


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_agent_pickup_trigger_set() -> None:
    workflow = _load_workflow()

    # PyYAML parses the bare `on:` key as boolean True.
    triggers = workflow[True]

    assert triggers["issues"]["types"] == ["assigned"]
    assert triggers["pull_request"]["types"] == ["assigned"]
    assert triggers["issue_comment"]["types"] == ["created"]

    dispatch = triggers["workflow_dispatch"]
    assert "issue_number" in dispatch["inputs"]
    assert dispatch["inputs"]["issue_number"]["required"] is True


# ---------------------------------------------------------------------------
# Guard expression (bot-login gate + Gitea fallbacks + self-comment exclusion)
# ---------------------------------------------------------------------------


def test_agent_pickup_guard_expression() -> None:
    workflow = _load_workflow()
    guard = workflow["jobs"]["start-session"]["if"]

    # Bot login must be configured at all.
    assert "vars.AGENT_BOT_LOGIN != ''" in guard
    # GitHub assignment events carry the just-assigned user.
    assert "github.event.assignee.login == vars.AGENT_BOT_LOGIN" in guard
    # Gitea payloads lack a top-level assignee — fall back to the item's list.
    assert "contains(github.event.issue.assignees.*.login, vars.AGENT_BOT_LOGIN)" in guard
    assert "contains(github.event.pull_request.assignees.*.login, vars.AGENT_BOT_LOGIN)" in guard
    # Comment trigger requires an @mention of the bot...
    assert "contains(github.event.comment.body, format('@{0}', vars.AGENT_BOT_LOGIN))" in guard
    # ...and must never fire on the bot's own comments (self-trigger loop).
    assert "github.event.comment.user.login != vars.AGENT_BOT_LOGIN" in guard
    # Manual dispatch stays allowed as the override.
    assert "github.event_name == 'workflow_dispatch'" in guard


# ---------------------------------------------------------------------------
# API call contract
# ---------------------------------------------------------------------------


def test_agent_pickup_posts_to_vcs_pickup_endpoint() -> None:
    text = _workflow_text()

    assert "secrets.MEWBO_API_URL" in text
    assert "secrets.MEWBO_API_TOKEN" in text
    assert "X-API-Key" in text
    assert "/api/automation/vcs-pickup" in text


def test_agent_pickup_payload_carries_routing_keys() -> None:
    workflow = _load_workflow()
    start_script = next(
        script
        for name, script in _run_scripts(workflow).items()
        if "/api/automation/vcs-pickup" in script
    )

    for key in ("head_ref", "base_ref", "comment", "bot_login", "repository", "kind", "number"):
        assert f"{key}:" in start_script, f"payload missing key '{key}'"


def test_agent_pickup_permissions_are_read_only() -> None:
    workflow = _load_workflow()
    permissions = workflow["permissions"]

    assert permissions == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }


# ---------------------------------------------------------------------------
# Injection safety: event payload values flow through env:, never inline
# ---------------------------------------------------------------------------


def test_agent_pickup_run_scripts_never_inline_event_payload() -> None:
    """Untrusted event fields (issue body, comment text, ...) must reach run
    scripts via ``env:`` indirection only — an inline ``${{ github.event.* }}``
    inside a ``run:`` block is a shell-injection vector."""
    workflow = _load_workflow()
    scripts = _run_scripts(workflow)
    assert scripts, "expected at least one run step"

    offenders = {name: script for name, script in scripts.items() if "${{ github.event." in script}
    assert not offenders, f"run scripts inline github.event payloads: {sorted(offenders)}"

    # The env block is the sanctioned carrier — it must exist on the job.
    env = workflow["jobs"]["start-session"]["env"]
    assert "ITEM_BODY" in env
    assert "COMMENT_BODY" in env
