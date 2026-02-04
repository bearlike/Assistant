"""Tests for common helpers."""

import meeseeks_core.common as common


def test_get_logger_uses_env_and_defaults(monkeypatch):
    """Exercise logging config paths driven by env values."""
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("MEESEEKS_LOG_STYLE", "dark")
    monkeypatch.setattr(common, "_LOG_CONFIGURED", False)

    logger = common.get_logger()
    assert common._LOG_CONFIGURED is True
    assert logger is not None
