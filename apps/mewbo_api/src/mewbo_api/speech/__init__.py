#!/usr/bin/env python3
"""Speech backend — opt-in, mounted only when ``mewbo_speech`` is installed.

``init_speech(api)`` mounts ``/api/speech*`` when the optional capability
library resolves, and returns ``False`` silently otherwise so the API server
starts cleanly with the feature simply absent.

**That absence IS the availability signal**, and it is the reason there is no
"speech is enabled" flag anywhere in this app. A client asks by calling
``GET /api/speech/capabilities``: a deployment without the library answers the
app-level JSON 404, so the capability answer cannot drift from whether the
routes exist —
there is no second registry to keep in step with reality. The endpoint's own
body then reports the finer-grained question the mount cannot answer, namely
whether the gateway behind it is configured and reachable.

The guard is on the ``mewbo_speech`` import rather than on the route module's,
which is a deliberate difference from a plain ``try: from .routes import …``.
Guarding the route import swallows an ``ImportError`` raised by a genuine bug
inside ``routes.py`` and reports it as "the extra is not installed" — the two
failures need telling apart, so the optional dependency is probed on its own and
the route import that follows is unguarded.
"""

from __future__ import annotations

from typing import Any

from mewbo_core.common import get_logger

logging = get_logger(name="api.speech")


def init_speech(api: Any) -> bool:
    """Mount ``/api/speech*`` on *api*. Returns ``False`` when speech is absent.

    Called once at import time from the composition root, alongside
    ``init_wiki``. Registers a Flask-RESTX namespace and nothing else: speech
    holds no store, no background thread and no hook, so there is no state to
    recover and nothing to reconcile at startup.

    Cost class: ``O(1)`` — an import probe and one namespace registration; no
    network call, so a gateway that is down does not slow or fail boot.
    """
    try:
        import mewbo_speech  # noqa: F401, PLC0415 — presence probe for the optional library
    except ImportError as exc:
        logging.info("speech library not installed ({}); skipping /api/speech* routes", exc)
        return False

    from .routes import init_speech_routes  # noqa: PLC0415 — after the probe, deliberately

    init_speech_routes(api)
    logging.info("speech routes mounted at /api/speech*")
    return True


def speech_model_ids() -> frozenset[str]:
    """Every gateway model id that serves speech, in either direction.

    The ONE answer to "is this id a speech route", exported so the chat model
    picker can subtract them without deriving its own version. The controller
    already discovers and caches the gateway's modes, so this is a cached read
    on the request path.

    Empty when the library is absent, the routes never mounted, or discovery is
    unavailable — every one of which means "this deployment cannot tell", and a
    caller must degrade rather than treat emptiness as "there are none".

    Cost class: ``O(1)`` — a cached lookup, no network call.
    """
    try:
        from .routes import _controller  # noqa: PLC0415 — optional, absent without the extra
    except ImportError:
        return frozenset()
    if _controller is None:
        return frozenset()
    return _controller.speech_model_ids()


__all__ = ["init_speech", "speech_model_ids"]
