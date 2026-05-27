"""Shared base class for the wiki SessionTools.

Every wiki built-in tool is a ``SessionTool`` constructed per agent with a
``session_id``. They all share the same lifecycle boilerplate — the ctor, the
``should_terminate_run`` flag, the runtime/ctx resolution, validating args and
serialising the result. That boilerplate lived copy-pasted across ~14 modules
(the exact smell ``source_tools._SourceToolShim`` and
``graph_neighbors.WikiGraphNeighbors`` already factored out for their suites).

``WikiSessionTool`` is the one home for it. A concrete tool subclasses it, sets
``tool_id``/``args_cls``/``schema``, and implements :meth:`run` over a resolved
ctx + validated args; everything else is inherited.

Test seam: each tool module keeps a module-level ``_resolve_runtime`` function
that delegates to :func:`mewbo_graph.plugins.wiki._ctx.resolve_runtime`. The
base resolves it through the subclass's own module at call time so existing
tests can still ``patch.object(<tool_module>, "_resolve_runtime", ...)``.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ValidationError

from mewbo_graph.plugins.wiki._ctx import (
    WikiJobCtx,
    WikiQaCtx,
    resolve_job_ctx,
    resolve_qa_ctx,
    resolve_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event


def _err_result(code: str, message: str) -> MockSpeaker:
    """Return a MockSpeaker carrying a structured error payload."""
    return MockSpeaker(content=str({"error": {"code": code, "message": message}}))


class WikiSessionTool:
    """Base for all wiki SessionTools — owns the shared lifecycle.

    Subclasses set the class attributes ``tool_id``/``args_cls``/``schema`` and
    implement :meth:`run`. The base supplies the ctor, ``should_terminate_run``,
    runtime resolution (patchable per-module test seam), arg validation and the
    structured error/result serialisation.
    """

    tool_id: str = ""
    args_cls: type[BaseModel] = BaseModel
    schema: dict[str, object] = {}
    modes = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id.

        ``event_logger`` is part of the SessionTool construction signature but
        is unused — all wiki event emission goes through the store
        (``append_job_event`` / ``emit_log`` / ``append_qa_event``).
        """
        self._session_id = session_id

    def should_terminate_run(self) -> bool:
        """Wiki tools never request loop termination (no tool sets a flag)."""
        return False

    # ── Resolution helpers (shared) ─────────────────────────────────────

    def _runtime(self) -> Any | None:
        """Resolve the wiki runtime via the subclass module's ``_resolve_runtime``.

        Honours per-module test patching: tests do
        ``patch.object(<tool_module>, "_resolve_runtime", ...)``. Falls back to
        the canonical ``_ctx.resolve_runtime`` when a module declares no alias.
        """
        module = sys.modules.get(type(self).__module__)
        resolver = getattr(module, "_resolve_runtime", resolve_runtime)
        return resolver()

    def _job_ctx(self) -> WikiJobCtx | None:
        """Resolve the indexing-job ctx for this session, or ``None``."""
        runtime = self._runtime()
        return resolve_job_ctx(self._session_id, runtime) if runtime is not None else None

    def _qa_ctx(self) -> WikiQaCtx | None:
        """Resolve the QA ctx for this session, or ``None``."""
        runtime = self._runtime()
        return resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None

    # ── Arg parsing + result serialisation (shared) ─────────────────────

    @staticmethod
    def _parse_args(args_cls: type[BaseModel], action_step: ActionStep) -> Any:
        """Validate ``action_step.tool_input`` against *args_cls*.

        Returns the validated model on success, or a :class:`MockSpeaker`
        carrying a structured ``validation`` error the caller can return as-is.
        """
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            return args_cls.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))


__all__ = ["WikiSessionTool", "_err_result"]
