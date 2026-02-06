"""Tests for common helpers."""

import meeseeks_core.common as common
from meeseeks_core.config import set_config_override


def test_get_logger_uses_config_and_defaults(monkeypatch):
    """Exercise logging config paths driven by config values."""
    set_config_override({"runtime": {"log_level": "info", "log_style": "dark"}})
    monkeypatch.setattr(common, "_LOG_CONFIGURED", False)

    logger = common.get_logger()
    assert common._LOG_CONFIGURED is True
    assert logger is not None
