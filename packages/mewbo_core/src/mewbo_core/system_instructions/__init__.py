"""Custom system instructions — operator text appended to every system prompt.

An operator authors a Jinja template; its RENDERED output is appended to the
system prompt of every session, and it can branch on the invoking client surface
(``{% if surface == "android" %}``) so each client gets tailored instructions.

Public surface: :class:`SystemInstructionsDoc` — the stored document, which also
renders itself in ``INSTRUCTION_SANDBOX`` and returns a :class:`RenderedInstructions`
— plus the :class:`InstructionContext` variable contract handed to the template
(``spec.py``), which also DESCRIBES itself for the operator via
:meth:`InstructionContext.describe` against an :class:`InstructionValueCatalog`
(``values.py``: this deployment's candidate values as plain data, injected by the
app — core reads no registry of its own), and the ``SystemInstructionsStoreBase`` /
``JsonSystemInstructionsStore`` / ``MongoSystemInstructionsStore`` backends
(``store.py`` / ``store_mongo.py``). ``MongoSystemInstructionsStore`` is imported
lazily via :func:`create_system_instructions_store` (mirrors
``create_trigger_store``), not re-exported here, so importing this package never
requires a reachable MongoDB.

The template renders ONCE per run in ``Orchestrator``; the resulting plain string
is handed to ``ToolUseLoop(user_instructions=...)``, which does no Jinja and no
DB work of its own.
"""

from mewbo_core.system_instructions.spec import (
    GLOBAL_INSTRUCTIONS_ID,
    INSTRUCTION_SANDBOX,
    MAX_RENDERED_BYTES,
    MAX_TEMPLATE_BYTES,
    InstructionContext,
    RenderedInstructions,
    SystemInstructionsDoc,
)
from mewbo_core.system_instructions.store import (
    JsonSystemInstructionsStore,
    SystemInstructionsStoreBase,
    create_system_instructions_store,
)
from mewbo_core.system_instructions.values import (
    KNOWN_PLATFORMS,
    CandidateValues,
    InstructionValueCatalog,
    InstructionVariable,
    ValuesKind,
)

__all__ = [
    "GLOBAL_INSTRUCTIONS_ID",
    "INSTRUCTION_SANDBOX",
    "KNOWN_PLATFORMS",
    "MAX_RENDERED_BYTES",
    "MAX_TEMPLATE_BYTES",
    "CandidateValues",
    "InstructionContext",
    "InstructionValueCatalog",
    "InstructionVariable",
    "JsonSystemInstructionsStore",
    "RenderedInstructions",
    "SystemInstructionsDoc",
    "SystemInstructionsStoreBase",
    "ValuesKind",
    "create_system_instructions_store",
]
