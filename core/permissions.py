#!/usr/bin/env python3
"""Permission policies for tool execution."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from typing import Any

import tomllib

from core.classes import ActionStep
from core.common import get_logger

logging = get_logger(name="core.permissions")


class PermissionDecision(str, Enum):
    """Possible outcomes for a permission check."""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PermissionRule:
    """Rule describing a tool/action permission decision."""
    tool_id: str = "*"
    action_type: str = "*"
    decision: PermissionDecision = PermissionDecision.ASK

    def matches(self, action_step: ActionStep) -> bool:
        """Return True when the action step matches the rule pattern.

        Args:
            action_step: Action step to evaluate against the rule.

        Returns:
            True when the rule matches the action step.
        """
        return fnmatch(action_step.action_consumer, self.tool_id) and fnmatch(
            action_step.action_type, self.action_type
        )


class PermissionPolicy:
    """Evaluate permission rules for action steps."""
    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        default_by_action: dict[str, PermissionDecision] | None = None,
        default_decision: PermissionDecision = PermissionDecision.ASK,
    ) -> None:
        """Initialize the permission policy.

        Args:
            rules: Optional list of explicit permission rules.
            default_by_action: Optional defaults keyed by action type.
            default_decision: Default decision when no rule matches.
        """
        self._rules = rules or []
        self._default_by_action = default_by_action or {}
        self._default_decision = default_decision

    def decide(self, action_step: ActionStep) -> PermissionDecision:
        """Return the permission decision for an action step.

        Args:
            action_step: Action step to evaluate.

        Returns:
            PermissionDecision based on the configured rules.
        """
        for rule in self._rules:
            if rule.matches(action_step):
                return rule.decision
        action_decision = self._default_by_action.get(action_step.action_type)
        if action_decision is not None:
            return action_decision
        return self._default_decision


def _parse_decision(value: str | None) -> PermissionDecision | None:
    """Parse a string value into a PermissionDecision.

    Args:
        value: Raw string from configuration.

    Returns:
        Parsed PermissionDecision or None when invalid.
    """
    if value is None:
        return None
    value = value.strip().lower()
    for decision in PermissionDecision:
        if decision.value == value:
            return decision
    return None


def _default_policy() -> PermissionPolicy:
    """Build the default permission policy when no configuration is provided.

    Returns:
        PermissionPolicy with built-in defaults.
    """
    rules = [
        PermissionRule(
            tool_id="talk_to_user_tool",
            action_type="*",
            decision=PermissionDecision.ALLOW,
        )
    ]
    default_by_action = {
        "get": PermissionDecision.ALLOW,
        "set": PermissionDecision.ASK,
    }
    return PermissionPolicy(
        rules=rules,
        default_by_action=default_by_action,
        default_decision=PermissionDecision.ASK,
    )


def _load_policy_data(path: str) -> dict[str, Any]:
    """Load permission policy data from TOML or JSON.

    Args:
        path: Filesystem path to a policy file.

    Returns:
        Raw policy payload dictionary.

    Raises:
        OSError: If the policy file cannot be read.
        json.JSONDecodeError: If a JSON policy file is invalid.
        tomllib.TOMLDecodeError: If a TOML policy file is invalid.
    """
    with open(path, "rb") as handle:
        if path.endswith(".toml"):
            return tomllib.load(handle)
        return json.load(handle)


def load_permission_policy(path: str | None = None) -> PermissionPolicy:
    """Load permission policy configuration from disk or defaults.

    Args:
        path: Optional explicit policy path. Defaults to env var when omitted.

    Returns:
        Loaded PermissionPolicy or default policy on error.
    """
    if path is None:
        path = os.getenv("MESEEKS_PERMISSION_POLICY")
    if not path:
        return _default_policy()
    if not os.path.exists(path):
        logging.warning("Permission policy file not found: {}", path)
        return _default_policy()
    try:
        payload = _load_policy_data(path)
    except (json.JSONDecodeError, OSError, tomllib.TOMLDecodeError) as exc:
        logging.warning("Failed to load permission policy: {}", exc)
        return _default_policy()

    rules: list[PermissionRule] = []
    for rule_data in payload.get("rules", []):
        decision = _parse_decision(rule_data.get("decision"))
        if decision is None:
            continue
        rules.append(
            PermissionRule(
                tool_id=str(rule_data.get("tool_id", "*")),
                action_type=str(rule_data.get("action_type", "*")),
                decision=decision,
            )
        )

    default_by_action: dict[str, PermissionDecision] = {}
    for key, value in payload.get("default_by_action", {}).items():
        parsed = _parse_decision(str(value))
        if parsed is not None:
            default_by_action[str(key)] = parsed

    default_decision = _parse_decision(payload.get("default_decision"))
    if default_decision is None:
        default_decision = PermissionDecision.ASK

    return PermissionPolicy(
        rules=rules,
        default_by_action=default_by_action,
        default_decision=default_decision,
    )


def approval_callback_from_env() -> Callable[[ActionStep], bool] | None:
    """Return an approval callback based on environment configuration.

    Returns:
        Callable that approves/denies actions or None to indicate no callback.
    """
    mode = os.getenv("MESEEKS_APPROVAL_MODE", "").strip().lower()
    if mode in {"allow", "auto", "approve", "yes"}:
        return lambda _: True
    if mode in {"deny", "never", "no"}:
        return lambda _: False
    return None


def auto_approve(_: ActionStep) -> bool:
    """Approval callback that always approves.

    Returns:
        True for all inputs.
    """
    return True


def auto_deny(_: ActionStep) -> bool:
    """Approval callback that always denies.

    Returns:
        False for all inputs.
    """
    return False


__all__ = [
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRule",
    "approval_callback_from_env",
    "auto_approve",
    "auto_deny",
    "load_permission_policy",
]
