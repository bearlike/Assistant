"""Custom system instructions — API surface.

REST CRUD + preview over the operator-authored Jinja template
(``mewbo_core.system_instructions.SystemInstructionsDoc``) appended to every
session's system prompt. See ``routes.py`` for the wire contract and the
security rationale (operator-only, REST + API key, no agent-facing tool).
``backend.py`` composes this via :func:`init_system_instructions_routes`
(mirroring ``init_triggers``).
"""

from mewbo_api.system_instructions.routes import (
    SystemInstructionsRoutesController,
    init_system_instructions_routes,
    system_instructions_ns,
)
from mewbo_api.system_instructions.value_sources import InstructionValueSources

__all__ = [
    "InstructionValueSources",
    "SystemInstructionsRoutesController",
    "init_system_instructions_routes",
    "system_instructions_ns",
]
