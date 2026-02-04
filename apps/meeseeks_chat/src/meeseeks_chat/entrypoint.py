"""Streamlit entrypoint for the packaged chat UI."""

from __future__ import annotations

import os
import sys
from importlib import resources

from streamlit.web import cli as stcli


def main() -> None:
    """Launch the Streamlit chat UI."""
    script = resources.files("meeseeks_chat").joinpath("chat_master.py")
    with resources.as_file(script) as path:
        argv = ["streamlit", "run", str(path)]
        port = os.getenv("STREAMLIT_SERVER_PORT")
        if port:
            argv.extend(["--server.port", port])
        address = os.getenv("STREAMLIT_SERVER_ADDRESS")
        if address:
            argv.extend(["--server.address", address])
        sys.argv = argv
        raise SystemExit(stcli.main())
