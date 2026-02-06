"""Tests for step reflection logic."""

from meeseeks_core.classes import ActionStep
from meeseeks_core.config import set_config_override
from meeseeks_core.reflection import StepReflection, StepReflector


def test_reflect_skips_without_objective():
    """Skip reflection when no objective or checklist is provided."""
    reflector = StepReflector(model_name="gpt-4")
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="ping",
    )
    assert reflector.reflect(step, "ok") is None


def test_reflect_disabled_by_env(monkeypatch):
    """Skip reflection when disabled via env."""
    set_config_override({"reflection": {"enabled": False}})
    reflector = StepReflector(model_name="gpt-4")
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="ping",
        objective="Check status",
    )
    assert reflector.reflect(step, "ok") is None


def test_reflect_skips_without_model(monkeypatch):
    """Skip reflection when no model is configured."""
    set_config_override(
        {
            "reflection": {"model": ""},
            "llm": {"action_plan_model": "", "default_model": ""},
        }
    )
    reflector = StepReflector(model_name=None)
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="ping",
        objective="Check status",
    )
    assert reflector.reflect(step, "ok") is None


def test_reflect_returns_result(monkeypatch):
    """Return a reflection decision from the model chain."""
    selection = StepReflection(status="retry", notes="retry", revised_argument="fix")

    class DummyChain:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return self

        def invoke(self, *_args, **_kwargs):
            return self._result

    class DummyPrompt:
        def __init__(self, result):
            self._result = result

        def __or__(self, _other):
            return DummyChain(self._result)

    monkeypatch.setattr(
        "meeseeks_core.reflection.ChatPromptTemplate",
        lambda *args, **kwargs: DummyPrompt(selection),
    )
    monkeypatch.setattr("meeseeks_core.reflection.build_chat_model", lambda **_k: object())
    reflector = StepReflector(model_name="gpt-4")
    step = ActionStep(
        action_consumer="home_assistant_tool",
        action_type="get",
        action_argument="ping",
        objective="Check status",
    )
    result = reflector.reflect(step, "ok")
    assert isinstance(result, StepReflection)
    assert result.status == "retry"
