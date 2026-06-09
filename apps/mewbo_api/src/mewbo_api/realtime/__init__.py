"""Realtime fast-grounded structured synthesis API.

``init_realtime`` registers ``POST /v1/structured/fast`` — the sessionless,
retrieval-only, single-round-trip path.

Controller wiring (one line in ``backend.py``)::

    from mewbo_api.realtime import init_realtime
    init_realtime(api, require_api_key, runtime)
"""
from .routes import init_realtime, realtime_ns

__all__ = ["init_realtime", "realtime_ns"]
