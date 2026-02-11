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


def test_session_log_context_reuses_and_releases_sinks(monkeypatch, tmp_path):
    """Reuse session log sinks and clean them up."""
    monkeypatch.setattr(common, "_SESSION_SINKS", {})
    monkeypatch.setattr(common, "_LOG_CONFIGURED", False)

    session_id = "session-logs-1"
    common._ensure_session_log_sink(session_id, log_dir=str(tmp_path))
    assert common._SESSION_SINKS[session_id]["count"] == 1

    common._ensure_session_log_sink(session_id, log_dir=str(tmp_path))
    assert common._SESSION_SINKS[session_id]["count"] == 2

    common._release_session_log_sink("missing-session")
    common._release_session_log_sink(session_id)
    assert common._SESSION_SINKS[session_id]["count"] == 1

    common._release_session_log_sink(session_id)
    assert session_id not in common._SESSION_SINKS
