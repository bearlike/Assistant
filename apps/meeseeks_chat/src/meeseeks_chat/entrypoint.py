"""Streamlit entrypoint for the packaged chat UI."""

from __future__ import annotations

import sys
from importlib import resources

from meeseeks_core.config import get_config, get_config_value, start_preflight
from streamlit.web import cli as stcli


def main() -> None:
    """Launch the Streamlit chat UI."""
    config = get_config()
    if config.runtime.preflight_enabled:
        start_preflight(config)
    script = resources.files("meeseeks_chat").joinpath("chat_master.py")
    with resources.as_file(script) as path:
        argv = ["streamlit", "run", str(path)]
        port = get_config_value("chat", "streamlit_port")
        if port:
            argv.extend(["--server.port", str(port)])
        address = get_config_value("chat", "streamlit_address")
        if address:
            argv.extend(["--server.address", address])
        sys.argv = argv
        raise SystemExit(stcli.main())
