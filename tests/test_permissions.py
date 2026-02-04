"""Tests for permission policy decisions."""

from meeseeks_core import permissions as permissions_module
from meeseeks_core.classes import ActionStep
from meeseeks_core.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    approval_callback_from_env,
    auto_approve,
    auto_deny,
    load_permission_policy,
)


def test_default_policy_allows_get():
    """Allow default get actions under permissive policy."""
    policy = PermissionPolicy(
        rules=[],
        default_by_action={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_rule_override_denies():
    """Deny set actions when an explicit rule overrides defaults."""
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                tool_id="home_assistant_tool",
                action_type="set",
                decision=PermissionDecision.DENY,
            )
        ],
        default_by_action={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="heater",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_default_decision_fallback():
    """Use default decision when no rule or action-specific match exists."""
    policy = PermissionPolicy(
        rules=[],
        default_by_action={},
        default_decision=PermissionDecision.DENY,
    )
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="custom",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_from_json(tmp_path, monkeypatch):
    """Load policy configuration from a JSON file."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        """
        {
          "rules": [
            {"tool_id": "home_assistant_tool", "action_type": "set", "decision": "deny"}
          ],
          "default_by_action": {"get": "allow", "set": "ask"},
          "default_decision": "ask"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_PERMISSION_POLICY", str(policy_path))
    policy = load_permission_policy()
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_from_toml(tmp_path, monkeypatch):
    """Load policy configuration from a TOML file."""
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        """
        [[rules]]
        tool_id = "home_assistant_tool"
        action_type = "set"
        decision = "deny"

        [default_by_action]
        get = "allow"
        set = "ask"

        default_decision = "ask"
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_PERMISSION_POLICY", str(policy_path))
    policy = load_permission_policy()
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_skips_invalid_rules_and_defaults(tmp_path, monkeypatch):
    """Skip invalid decisions and fall back to default ask decision."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        """
        {
          "rules": [
            {"tool_id": "home_assistant_tool", "action_type": "set", "decision": "maybe"}
          ],
          "default_by_action": {},
          "default_decision": "unknown"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("MESEEKS_PERMISSION_POLICY", str(policy_path))
    policy = load_permission_policy()
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="set",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.ASK


def test_parse_decision_invalid_and_valid():
    """Return None for invalid decisions and parse known values."""
    assert permissions_module._parse_decision(None) is None
    assert permissions_module._parse_decision("nope") is None
    assert permissions_module._parse_decision("allow") == PermissionDecision.ALLOW


def test_load_policy_missing_file(monkeypatch):
    """Fall back to defaults when the policy file is missing."""
    monkeypatch.setenv("MESEEKS_PERMISSION_POLICY", "/tmp/missing-policy.json")
    policy = load_permission_policy()
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_load_policy_invalid_json(tmp_path, monkeypatch):
    """Fall back to defaults when policy JSON is invalid."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{invalid-json}", encoding="utf-8")
    monkeypatch.setenv("MESEEKS_PERMISSION_POLICY", str(policy_path))
    policy = load_permission_policy()
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_approval_callback_from_env(monkeypatch):
    """Resolve approval callbacks from env flags."""
    monkeypatch.setenv("MESEEKS_APPROVAL_MODE", "allow")
    callback = approval_callback_from_env()
    assert callback is not None
    assert callback(
        ActionStep(action_consumer="home_assistant_tool", action_type="get", action_argument="x")
    )
    monkeypatch.setenv("MESEEKS_APPROVAL_MODE", "deny")
    callback = approval_callback_from_env()
    assert callback is not None
    assert (
        callback(
            ActionStep(
                action_consumer="home_assistant_tool", action_type="get", action_argument="x"
            )
        )
        is False
    )
    monkeypatch.setenv("MESEEKS_APPROVAL_MODE", "maybe")
    assert approval_callback_from_env() is None


def test_auto_approve_and_deny():
    """Cover explicit approve/deny helpers."""
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="lights",
    )
    assert auto_approve(step) is True
    assert auto_deny(step) is False
