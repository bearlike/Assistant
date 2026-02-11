"""Tests for permission policy decisions."""

from meeseeks_core import permissions as permissions_module
from meeseeks_core.classes import ActionStep
from meeseeks_core.config import set_config_override
from meeseeks_core.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    approval_callback_from_config,
    auto_approve,
    auto_deny,
    load_permission_policy,
)


def test_default_policy_allows_get():
    """Allow default get actions under permissive policy."""
    policy = PermissionPolicy(
        rules=[],
        default_by_operation={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_rule_override_denies():
    """Deny set actions when an explicit rule overrides defaults."""
    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                tool_id="home_assistant_tool",
                operation="set",
                decision=PermissionDecision.DENY,
            )
        ],
        default_by_operation={"get": PermissionDecision.ALLOW, "set": PermissionDecision.ASK},
        default_decision=PermissionDecision.ASK,
    )
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input="heater",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_default_decision_fallback():
    """Use default decision when no rule or action-specific match exists."""
    policy = PermissionPolicy(
        rules=[],
        default_by_operation={},
        default_decision=PermissionDecision.DENY,
    )
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="custom",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_from_json(tmp_path, monkeypatch):
    """Load policy configuration from a JSON file."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        """
        {
          "rules": [
            {"tool_id": "home_assistant_tool", "operation": "set", "decision": "deny"}
          ],
          "default_by_operation": {"get": "allow", "set": "ask"},
          "default_decision": "ask"
        }
        """,
        encoding="utf-8",
    )
    set_config_override({"permissions": {"policy_path": str(policy_path)}})
    policy = load_permission_policy()
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_from_toml(tmp_path, monkeypatch):
    """Load policy configuration from a TOML file."""
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        """
        [[rules]]
        tool_id = "home_assistant_tool"
        operation = "set"
        decision = "deny"

        [default_by_operation]
        get = "allow"
        set = "ask"

        default_decision = "ask"
        """,
        encoding="utf-8",
    )
    set_config_override({"permissions": {"policy_path": str(policy_path)}})
    policy = load_permission_policy()
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.DENY


def test_load_policy_skips_invalid_rules_and_defaults(tmp_path, monkeypatch):
    """Skip invalid decisions and fall back to default ask decision."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        """
        {
          "rules": [
            {"tool_id": "home_assistant_tool", "operation": "set", "decision": "maybe"}
          ],
          "default_by_operation": {},
          "default_decision": "unknown"
        }
        """,
        encoding="utf-8",
    )
    set_config_override({"permissions": {"policy_path": str(policy_path)}})
    policy = load_permission_policy()
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="set",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.ASK


def test_parse_decision_invalid_and_valid():
    """Return None for invalid decisions and parse known values."""
    assert permissions_module._parse_decision(None) is None
    assert permissions_module._parse_decision("nope") is None
    assert permissions_module._parse_decision("allow") == PermissionDecision.ALLOW


def test_load_policy_missing_file(monkeypatch):
    """Fall back to defaults when the policy file is missing."""
    set_config_override({"permissions": {"policy_path": "/tmp/missing-policy.json"}})
    policy = load_permission_policy()
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_load_policy_invalid_json(tmp_path, monkeypatch):
    """Fall back to defaults when policy JSON is invalid."""
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{invalid-json}", encoding="utf-8")
    set_config_override({"permissions": {"policy_path": str(policy_path)}})
    policy = load_permission_policy()
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input="lights",
    )
    assert policy.decide(step) == PermissionDecision.ALLOW


def test_approval_callback_from_config(monkeypatch):
    """Resolve approval callbacks from config flags."""
    set_config_override({"permissions": {"approval_mode": "allow"}})
    callback = approval_callback_from_config()
    assert callback is not None
    assert callback(ActionStep(tool_id="home_assistant_tool", operation="get", tool_input="x"))
    set_config_override({"permissions": {"approval_mode": "deny"}})
    callback = approval_callback_from_config()
    assert callback is not None
    assert (
        callback(ActionStep(tool_id="home_assistant_tool", operation="get", tool_input="x"))
        is False
    )
    set_config_override({"permissions": {"approval_mode": "maybe"}})
    assert approval_callback_from_config() is None


def test_auto_approve_and_deny():
    """Cover explicit approve/deny helpers."""
    step = ActionStep(
        tool_id="home_assistant_tool",
        operation="get",
        tool_input="lights",
    )
    assert auto_approve(step) is True
    assert auto_deny(step) is False
