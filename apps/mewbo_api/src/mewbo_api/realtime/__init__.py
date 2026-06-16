"""Realtime token-streaming draft synthesis API.

``init_realtime`` registers ``POST /v1/draft/stream`` — the session-full,
token-streaming, tool-light draft path (write-behind persistence, #78).

The no-loop structured synthesis lane (formerly ``POST /v1/structured/fast``) now
lives as ``mode: "synthesis"`` on ``POST /v1/structured`` (#85); this package
still owns the shared ``RealtimeSessionRecorder`` + ``WikiGroundingProvider`` glue
it reuses.

Controller wiring (one line in ``backend.py``)::

    from mewbo_api.realtime import init_realtime
    init_realtime(api, require_api_key, runtime)
"""
from .routes import draft_ns, init_realtime

__all__ = ["draft_ns", "init_realtime"]
