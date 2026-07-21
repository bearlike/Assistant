"""Reverse-invocation trigger subsystem — API surface (WP3).

The orchestrator-side peer of the domain package ``mewbo_core.triggers``: the
:class:`~mewbo_api.triggers.service.TriggerService` watcher that fires armed
triggers and re-invokes sessions, the REST contract (:mod:`routes`), and the
forge-polling client (:mod:`forge`). ``backend.py:init_triggers`` composes these
(mirroring ``init_channels`` / ``init_vcs_pickup``): it owns the app-private
re-engage seam + the terminate cascade, so this package stays free of any
``backend`` import (no cycle).
"""

from mewbo_api.triggers.forge import (
    ForgeClient,
    HttpForgeClient,
    build_forge_client_factory,
    resolve_forge_client,
)
from mewbo_api.triggers.routes import init_trigger_routes, triggers_ns
from mewbo_api.triggers.service import TriggerFireContext, TriggerService

__all__ = [
    "TriggerService",
    "TriggerFireContext",
    "init_trigger_routes",
    "triggers_ns",
    "ForgeClient",
    "HttpForgeClient",
    "build_forge_client_factory",
    "resolve_forge_client",
]
